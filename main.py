#!/usr/bin/env python3
"""Coleta posts de um perfil do X que contenham cashtags ou o caractere $.

O script usa uma sessao persistente do Chromium para permitir login manual e
percorre a timeline card a card, inclusive quando o X virtualiza/remover cards
antigos do DOM.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait


CASHTAG_RE = re.compile(r"(?<![\w$])\$([A-Za-z][A-Za-z0-9_]{0,14})")
STATUS_RE = re.compile(r"/([^/]+)/status/(\d+)")


class JsonFormatter(logging.Formatter):
    """Formata cada log como um objeto JSON em uma unica linha."""

    _reserved = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._reserved and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(log_path: Path) -> logging.Logger:
    logger = logging.getLogger("x_crawler")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = JsonFormatter()
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(stream)
    logger.addHandler(file_handler)
    return logger


def downloads_dir() -> Path:
    """Retorna Downloads, com override opcional via X_CRAWLER_DOWNLOADS."""
    configured = os.getenv("X_CRAWLER_DOWNLOADS")
    path = Path(configured).expanduser() if configured else Path.home() / "Downloads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def find_vivaldi() -> Path:
    """Localiza o executavel do Vivaldi nas pastas padrao do Windows."""
    candidates = [
        Path(os.getenv("LOCALAPPDATA", "")) / "Vivaldi/Application/vivaldi.exe",
        Path(os.getenv("PROGRAMFILES", "")) / "Vivaldi/Application/vivaldi.exe",
        Path(os.getenv("PROGRAMFILES(X86)", "")) / "Vivaldi/Application/vivaldi.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "Vivaldi nao encontrado. Instale-o em uma das pastas padrao do Windows."
    )


def detect_chromium_major(executable: Path, profile_root: Path) -> str:
    """Descobre a versao interna do Chromium usada pelo Vivaldi."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])

    probe_profile = profile_root.parent / "x-replies-crawler-version-probe"
    probe_profile.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [
            str(executable),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={probe_profile}",
            "--headless=new",
            "--no-first-run",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/version", timeout=1
                ) as response:
                    metadata = json.load(response)
                match = re.search(r"Chrome/(\d+)", metadata.get("User-Agent", ""))
                if match:
                    return match.group(1)
                raise RuntimeError("A versao interna do Chromium nao foi identificada.")
            except urllib.error.URLError:
                time.sleep(0.25)
        raise RuntimeError("O Vivaldi nao respondeu ao teste de versao.")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def ensure_chromedriver(
    chromium_major: str, cache_root: Path, logger: logging.Logger
) -> Path:
    """Baixa e mantem em cache o ChromeDriver do Chromium interno do Vivaldi."""
    driver_dir = cache_root / "drivers" / chromium_major
    driver_path = driver_dir / "chromedriver.exe"
    if driver_path.is_file():
        logger.info("chromedriver_cache_hit", extra={"path": str(driver_path)})
        return driver_path

    metadata_url = (
        "https://googlechromelabs.github.io/chrome-for-testing/"
        "latest-versions-per-milestone-with-downloads.json"
    )
    logger.info("chromedriver_download_started", extra={"major": chromium_major})
    with urllib.request.urlopen(metadata_url, timeout=30) as response:
        metadata = json.load(response)
    milestone = metadata.get("milestones", {}).get(chromium_major)
    if not milestone:
        raise RuntimeError(
            f"ChromeDriver para Chromium {chromium_major} ainda nao esta disponivel."
        )
    downloads = milestone.get("downloads", {}).get("chromedriver", [])
    download_url = next(
        (item["url"] for item in downloads if item.get("platform") == "win64"), None
    )
    if not download_url:
        raise RuntimeError("Download win64 do ChromeDriver nao encontrado.")

    driver_dir.mkdir(parents=True, exist_ok=True)
    zip_path = driver_dir / "chromedriver.zip"
    urllib.request.urlretrieve(download_url, zip_path)
    with zipfile.ZipFile(zip_path) as archive:
        member = next(
            name for name in archive.namelist() if name.endswith("/chromedriver.exe")
        )
        with archive.open(member) as source, driver_path.open("wb") as destination:
            destination.write(source.read())
    zip_path.unlink(missing_ok=True)
    logger.info("chromedriver_download_completed", extra={"path": str(driver_path)})
    return driver_path


def start_vivaldi_for_selenium(
    executable: Path,
    profile_dir: Path,
    initial_url: str,
    logger: logging.Logger,
) -> tuple[subprocess.Popen[Any], str]:
    """Abre o Vivaldi numa pagina web real e expoe a porta para o Selenium."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    address = f"127.0.0.1:{port}"
    process = subprocess.Popen(
        [
            str(executable),
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            initial_url,
        ]
    )
    logger.info("vivaldi_launch_started", extra={"debug_port": port})
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("O Vivaldi encerrou antes de criar uma aba web.")
        try:
            with urllib.request.urlopen(
                f"http://{address}/json/list", timeout=1
            ) as response:
                targets = json.load(response)
            if any(
                target.get("type") == "page"
                and str(target.get("url", "")).startswith(("http://", "https://"))
                for target in targets
            ):
                logger.info("vivaldi_web_tab_ready", extra={"debug_port": port})
                return process, address
        except urllib.error.URLError:
            pass
        time.sleep(0.25)
    process.terminate()
    raise RuntimeError("O Vivaldi nao criou uma aba web em 30 segundos.")


def extract_card(card: Any, username: str) -> dict[str, Any] | None:
    """Extrai somente cards cujo autor e exatamente o usuario procurado."""
    username_key = username.casefold()
    author_links = card.find_elements(By.CSS_SELECTOR, '[data-testid="User-Name"] a[href]')
    is_target_author = False
    for author_link in author_links:
        href = (author_link.get_attribute("href") or "").rstrip("/")
        path = re.sub(r"^https?://(?:www\.)?x\.com", "", href, flags=re.IGNORECASE)
        if path.casefold() == f"/{username_key}":
            is_target_author = True
            break
    if not is_target_author:
        return None

    text_nodes = card.find_elements(By.CSS_SELECTOR, '[data-testid="tweetText"]')
    if not text_nodes:
        return None
    text_node = text_nodes[0]
    text = text_node.text.strip()

    cashtags = {match.group(1) for match in CASHTAG_RE.finditer(text)}

    permalink = None
    tweet_id = None
    # Usa somente links que contem a data/hora do proprio card. Isso evita
    # confundir o post com links de citacoes ou de outros cards incorporados.
    status_links = card.find_elements(By.CSS_SELECTOR, 'a[href*="/status/"]:has(time)')
    for status_link in status_links:
        href = status_link.get_attribute("href") or ""
        match = STATUS_RE.search(href)
        if match and match.group(1).casefold() == username_key:
            tweet_id = match.group(2)
            permalink = f"https://x.com/{username}/status/{tweet_id}"
            break
    if not tweet_id:
        return None

    time_nodes = card.find_elements(By.CSS_SELECTOR, "time")
    published_at = time_nodes[0].get_attribute("datetime") if time_nodes else None

    return {
        "id": tweet_id,
        "usuario": f"@{username}",
        "texto": text,
        "cashtags": sorted(cashtags, key=str.casefold),
        "publicado_em": published_at,
        "url": permalink,
    }


def visible_tweet_ids(driver: webdriver.Chrome) -> set[str]:
    ids: set[str] = set()
    for link in driver.find_elements(
        By.CSS_SELECTOR, 'article[data-testid="tweet"] a[href*="/status/"]:has(time)'
    ):
        match = STATUS_RE.search(link.get_attribute("href") or "")
        if match:
            ids.add(match.group(2))
    return ids


def visible_years(driver: webdriver.Chrome) -> list[int]:
    years: list[int] = []
    for element in driver.find_elements(
        By.CSS_SELECTOR, 'article[data-testid="tweet"] time[datetime]'
    ):
        value = element.get_attribute("datetime") or ""
        match = re.match(r"(\d{4})-", value)
        if match:
            years.append(int(match.group(1)))
    return years


def dismiss_possible_dialogs(driver: webdriver.Chrome) -> None:
    """Fecha apenas dialogs opcionais conhecidos, sem falhar se nao existirem."""
    for label in ("Fechar", "Close"):
        buttons = driver.find_elements(
            By.XPATH, f'//button[@aria-label="{label}" or normalize-space()="{label}"]'
        )
        if buttons:
            try:
                buttons[0].click()
            except Exception:
                pass


def crawl(
    driver: webdriver.Chrome,
    username: str,
    delay_seconds: float,
    html_wait_seconds: float,
    stop_year: int,
    max_stale_rounds: int,
    max_scrolls: int,
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    target_url = f"https://x.com/{username}/with_replies"
    if not driver.current_url.startswith(target_url):
        logger.info("navigation_started", extra={"url": target_url})
        driver.get(target_url)
    else:
        logger.info("target_page_confirmed", extra={"url": driver.current_url})
    dismiss_possible_dialogs(driver)

    try:
        WebDriverWait(driver, 60).until(
            lambda current: current.find_elements(
                By.CSS_SELECTOR, 'article[data-testid="tweet"]'
            )
        )
    except TimeoutException as exc:
        logger.error(
            "timeline_not_found",
            extra={
                "hint": "Confirme o login no X e se a pagina do perfil esta acessivel."
            },
        )
        raise RuntimeError("Nenhum card apareceu na timeline.") from exc

    captured: dict[str, dict[str, Any]] = {}
    stale_rounds = 0

    for scroll_number in range(1, max_scrolls + 1):
        cards = driver.find_elements(By.CSS_SELECTOR, 'article[data-testid="tweet"]')
        visible_count = len(cards)
        new_ids_this_round = 0
        target_years_this_round: list[int] = []

        for index in range(visible_count):
            try:
                item = extract_card(cards[index], username)
            except Exception:
                logger.warning(
                    "card_parse_failed",
                    extra={"scroll": scroll_number, "card_index": index},
                    exc_info=True,
                )
                continue

            if not item:
                continue
            published = item.get("publicado_em") or ""
            year_match = re.match(r"(\d{4})-", published)
            if year_match:
                target_years_this_round.append(int(year_match.group(1)))
            if item["id"] in captured:
                continue
            captured[item["id"]] = item
            new_ids_this_round += 1
            logger.info(
                "card_captured_in_memory",
                extra={
                    "tweet_id": item["id"],
                    "has_dollar": "$" in item["texto"],
                    "cashtags": item["cashtags"],
                    "url": item["url"],
                },
            )

        oldest_visible_year = (
            min(target_years_this_round) if target_years_this_round else None
        )
        newest_visible_year = (
            max(target_years_this_round) if target_years_this_round else None
        )
        state = driver.execute_script(
            """return {
                y: Math.round(window.scrollY),
                height: Math.round(document.documentElement.scrollHeight),
                viewport: Math.round(window.innerHeight)
            }"""
        )
        logger.info(
            "scroll_round_completed",
            extra={
                "scroll": scroll_number,
                "cards_in_dom": visible_count,
                "new_target_cards": new_ids_this_round,
                "captured_total": len(captured),
                "oldest_visible_year": oldest_visible_year,
                "newest_visible_year": newest_visible_year,
                "scroll_y": state["y"],
                "document_height": state["height"],
                "stale_rounds": stale_rounds,
            },
        )

        # Evita parar por causa de um card-pai antigo exibido ao lado de uma
        # resposta recente: na fronteira real, todos os cards do alvo visiveis
        # ja estao no ano limite ou no ano imediatamente seguinte.
        if (
            oldest_visible_year is not None
            and newest_visible_year is not None
            and oldest_visible_year <= stop_year
            and newest_visible_year <= stop_year + 1
        ):
            logger.info(
                "year_boundary_reached",
                extra={"stop_year": stop_year, "oldest_visible_year": oldest_visible_year},
            )
            break

        before_ids = visible_tweet_ids(driver)
        driver.execute_script("window.scrollBy(0, Math.round(window.innerHeight * 0.85))")
        try:
            WebDriverWait(driver, html_wait_seconds).until(
                lambda current: visible_tweet_ids(current) != before_ids
            )
            html_refreshed = True
        except TimeoutException:
            html_refreshed = False

        # Mesmo depois de os IDs mudarem, espera imagens/textos e o DOM virtual
        # assentarem antes de capturar a proxima tela.
        time.sleep(delay_seconds)
        dismiss_possible_dialogs(driver)

        after_state = driver.execute_script(
            """return {
                y: Math.round(window.scrollY),
                height: Math.round(document.documentElement.scrollHeight)
            }"""
        )
        after_ids = visible_tweet_ids(driver)
        no_movement = after_state["y"] <= state["y"] + 1
        no_dom_change = after_ids == before_ids and after_state["height"] == state["height"]
        stale_rounds = stale_rounds + 1 if no_movement and no_dom_change else 0
        logger.info(
            "html_settled_after_scroll",
            extra={
                "scroll": scroll_number,
                "html_refreshed": html_refreshed,
                "visible_ids_before": len(before_ids),
                "visible_ids_after": len(after_ids),
                "stale_rounds": stale_rounds,
            },
        )
        if stale_rounds >= max_stale_rounds:
            logger.info("bottom_confirmed", extra={"scroll": scroll_number})
            break
    else:
        logger.warning("max_scrolls_reached", extra={"max_scrolls": max_scrolls})

    logger.info("collection_phase_completed", extra={"captured_total": len(captured)})
    matches = [item for item in captured.values() if "$" in item["texto"]]
    matches.sort(key=lambda item: item.get("publicado_em") or "", reverse=True)
    logger.info("memory_filter_completed", extra={"matches_total": len(matches)})
    return matches


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Coleta posts/replies de um usuario do X que contenham $."
    )
    parser.add_argument("--usuario", default="lenekuuu", help="Usuario sem @.")
    parser.add_argument(
        "--delay", type=float, default=5.0, help="Pausa para o HTML estabilizar."
    )
    parser.add_argument(
        "--espera-html", type=float, default=15.0, help="Espera maxima por novos cards."
    )
    parser.add_argument(
        "--ano-limite", type=int, default=2021, help="Para ao encontrar cards deste ano."
    )
    parser.add_argument(
        "--tentativas-fim",
        type=int,
        default=5,
        help="Rodadas imoveis necessarias para confirmar o fim.",
    )
    parser.add_argument(
        "--max-scrolls", type=int, default=10_000, help="Limite de seguranca de rolagens."
    )
    parser.add_argument(
        "--login-timeout",
        type=int,
        default=300,
        help="Segundos para concluir login manual quando necessario.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    username = args.usuario.strip().lstrip("@")
    target_url = f"https://x.com/{username}/with_replies"
    output_dir = downloads_dir()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"x_{username}_cashtags_{stamp}.json"
    log_path = output_dir / f"x_{username}_crawler_{stamp}.jsonl"
    logger = setup_logging(log_path)

    logger.info(
        "crawler_started",
        extra={
            "username": username,
            "condition": "texto contem $",
            "stop_year": args.ano_limite,
            "output_path": str(output_path),
            "log_path": str(log_path),
        },
    )

    profile_dir = Path(os.getenv("LOCALAPPDATA", str(Path.cwd()))) / "x-replies-crawler"
    profile_dir.mkdir(parents=True, exist_ok=True)

    driver: webdriver.Chrome | None = None
    vivaldi_process: subprocess.Popen[Any] | None = None
    try:
        vivaldi_path = find_vivaldi()
        logger.info(
            "vivaldi_found",
            extra={"executable_path": str(vivaldi_path), "profile_path": str(profile_dir)},
        )
        options = Options()
        options.binary_location = str(vivaldi_path)
        chromium_major = detect_chromium_major(vivaldi_path, profile_dir)
        logger.info("chromium_version_detected", extra={"major": chromium_major})
        driver_path = ensure_chromedriver(chromium_major, profile_dir, logger)
        vivaldi_process, debugger_address = start_vivaldi_for_selenium(
            vivaldi_path, profile_dir, target_url, logger
        )
        options.debugger_address = debugger_address

        logger.info("selenium_driver_started")
        driver = webdriver.Chrome(
            service=Service(
                executable_path=str(driver_path),
                service_args=["--disable-build-check"],
            ),
            options=options,
        )
        driver.set_page_load_timeout(60)
        logger.info("selenium_driver_ready")

        logger.info("target_navigation_started", extra={"url": target_url})
        driver.get(target_url)
        if "/login" in driver.current_url or driver.find_elements(
            By.CSS_SELECTOR, 'input[autocomplete="username"]'
        ):
            logger.info(
                "login_required",
                extra={"timeout_seconds": args.login_timeout},
            )
            print(
                "\nFaça login na janela do X. O crawler continuará automaticamente.\n",
                flush=True,
            )
            try:
                WebDriverWait(driver, args.login_timeout).until(
                    lambda current: bool(
                        re.search(
                            r"https://x\.com/(home|[^/]+/with_replies)",
                            current.current_url,
                        )
                    )
                )
            except TimeoutException as exc:
                raise RuntimeError("Tempo de login esgotado.") from exc

        # Mesmo que o X mande o usuario para /home depois do login, retorna
        # explicitamente para a aba de respostas antes de iniciar a coleta.
        if driver.current_url.rstrip("/") != target_url.rstrip("/"):
            logger.info(
                "target_navigation_retried",
                extra={"current_url": driver.current_url, "target_url": target_url},
            )
            driver.get(target_url)

        results = crawl(
            driver=driver,
            username=username,
            delay_seconds=args.delay,
            html_wait_seconds=args.espera_html,
            stop_year=args.ano_limite,
            max_stale_rounds=args.tentativas_fim,
            max_scrolls=args.max_scrolls,
            logger=logger,
        )

        output_path.write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info(
            "export_completed",
            extra={"items": len(results), "output_path": str(output_path)},
        )
    except KeyboardInterrupt:
        logger.warning("crawler_interrupted_by_user")
        return 130
    except Exception:
        logger.error("crawler_failed", exc_info=True)
        return 1
    finally:
        if driver is not None:
            driver.quit()
        if vivaldi_process is not None and vivaldi_process.poll() is None:
            vivaldi_process.terminate()

    print(f"\nConcluído: {len(results)} item(ns) salvo(s) em {output_path}")
    print(f"Logs: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
