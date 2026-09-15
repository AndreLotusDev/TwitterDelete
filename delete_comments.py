#!/usr/bin/env python3
"""Exclui posts do proprio usuario listados em um JSON do crawler do X."""

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


USERNAME = "lenekuuu"
DEFAULT_INPUT = Path.home() / "Downloads" / "x_lenekuuu_cashtags_20260914_230814.json"
URL_RE = re.compile(
    rf"^https://(?:www\.)?x\.com/{USERNAME}/status/(\d+)/?$", re.IGNORECASE
)
MISSING_POST_MESSAGES = (
    "esta página não existe",
    "esta pagina não existe",
    "este post foi excluído",
    "este post foi excluido",
    "post indisponível",
    "post indisponivel",
    "this page doesn’t exist",
    "this page doesn't exist",
    "this post was deleted",
    "post unavailable",
)


class PostUnavailable(RuntimeError):
    """O X informou que o post nao existe mais ou nao esta acessivel."""


class JsonFormatter(logging.Formatter):
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


def setup_logging(path: Path) -> logging.Logger:
    logger = logging.getLogger("x_delete_bot")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = JsonFormatter()
    for handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(path, encoding="utf-8")):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


def find_vivaldi() -> Path:
    candidates = [
        Path(os.getenv("LOCALAPPDATA", "")) / "Vivaldi/Application/vivaldi.exe",
        Path(os.getenv("PROGRAMFILES", "")) / "Vivaldi/Application/vivaldi.exe",
        Path(os.getenv("PROGRAMFILES(X86)", "")) / "Vivaldi/Application/vivaldi.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Vivaldi nao encontrado nas pastas padrao do Windows.")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def detect_chromium_major(executable: Path, cache_root: Path) -> str:
    port = free_port()
    probe = cache_root.parent / "x-replies-crawler-version-probe"
    probe.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [
            str(executable),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={probe}",
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
                raise RuntimeError("Versao interna do Chromium nao identificada.")
            except urllib.error.URLError:
                time.sleep(0.25)
        raise RuntimeError("O Vivaldi nao respondeu ao teste de versao.")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def ensure_chromedriver(major: str, cache_root: Path, logger: logging.Logger) -> Path:
    folder = cache_root / "drivers" / major
    driver_path = folder / "chromedriver.exe"
    if driver_path.is_file():
        logger.info("chromedriver_cache_hit", extra={"path": str(driver_path)})
        return driver_path

    metadata_url = (
        "https://googlechromelabs.github.io/chrome-for-testing/"
        "latest-versions-per-milestone-with-downloads.json"
    )
    logger.info("chromedriver_download_started", extra={"major": major})
    with urllib.request.urlopen(metadata_url, timeout=30) as response:
        metadata = json.load(response)
    milestone = metadata.get("milestones", {}).get(major)
    if not milestone:
        raise RuntimeError(f"ChromeDriver para Chromium {major} indisponivel.")
    url = next(
        (
            item["url"]
            for item in milestone.get("downloads", {}).get("chromedriver", [])
            if item.get("platform") == "win64"
        ),
        None,
    )
    if not url:
        raise RuntimeError("Download win64 do ChromeDriver nao encontrado.")

    folder.mkdir(parents=True, exist_ok=True)
    archive_path = folder / "chromedriver.zip"
    urllib.request.urlretrieve(url, archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        member = next(name for name in archive.namelist() if name.endswith("/chromedriver.exe"))
        with archive.open(member) as source, driver_path.open("wb") as destination:
            destination.write(source.read())
    archive_path.unlink(missing_ok=True)
    logger.info("chromedriver_download_completed", extra={"path": str(driver_path)})
    return driver_path


def start_vivaldi(
    executable: Path, profile: Path, initial_url: str, logger: logging.Logger
) -> tuple[subprocess.Popen[Any], str]:
    port = free_port()
    address = f"127.0.0.1:{port}"
    process = subprocess.Popen(
        [
            str(executable),
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            initial_url,
        ]
    )
    logger.info("vivaldi_launch_started", extra={"url": initial_url, "debug_port": port})
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("O Vivaldi encerrou antes de criar uma aba web.")
        try:
            with urllib.request.urlopen(f"http://{address}/json/list", timeout=1) as response:
                targets = json.load(response)
            if any(
                target.get("type") == "page"
                and str(target.get("url", "")).startswith(("http://", "https://"))
                for target in targets
            ):
                return process, address
        except urllib.error.URLError:
            pass
        time.sleep(0.25)
    process.terminate()
    raise RuntimeError("O Vivaldi nao criou uma aba web em 30 segundos.")


def load_targets(path: Path) -> list[dict[str, str]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("O JSON precisa conter uma lista.")
    targets: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"Item {index} nao e um objeto.")
        url = str(item.get("url", "")).strip()
        match = URL_RE.fullmatch(url)
        if not match:
            raise ValueError(f"URL recusada no item {index}: {url!r}")
        tweet_id = match.group(1)
        if str(item.get("id", tweet_id)) != tweet_id:
            raise ValueError(f"ID e URL divergem no item {index}.")
        if str(item.get("usuario", f"@{USERNAME}")).casefold() != f"@{USERNAME}".casefold():
            raise ValueError(f"Usuario inesperado no item {index}.")
        if tweet_id not in seen:
            targets.append({"id": tweet_id, "url": url})
            seen.add(tweet_id)
    return targets


def read_progress(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"selector_version": 2, "deleted": [], "missing": [], "failed": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("selector_version") != 2:
        # O seletor anterior podia confundir o post-alvo com o card pai.
        # Nao confia em IDs marcados por aquela versao; verifica todos novamente.
        return {"selector_version": 2, "deleted": [], "missing": [], "failed": {}}
    return data


def save_progress(path: Path, progress: dict[str, Any]) -> None:
    progress["selector_version"] = 2
    progress["updated_at"] = datetime.now(timezone.utc).isoformat()
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def wait_for_login(driver: webdriver.Chrome, timeout: int, target_url: str) -> None:
    needs_login = (
        "/login" in driver.current_url
        or "/i/flow/" in driver.current_url
        or bool(driver.find_elements(By.CSS_SELECTOR, 'input[autocomplete="username"]'))
    )
    if not needs_login:
        return
    print("\nFaça login no X na janela do Vivaldi.\n", flush=True)
    WebDriverWait(driver, timeout).until(
        lambda current: "/login" not in current.current_url
        and "/i/flow/" not in current.current_url
        and not current.find_elements(By.CSS_SELECTOR, 'input[autocomplete="username"]')
    )
    driver.get(target_url)


def target_article(driver: webdriver.Chrome, tweet_id: str, timeout: int) -> Any | None:
    # Em uma pagina de conversa existem varios articles. O link de data/hora
    # identifica o post principal do card sem confundir links de respostas,
    # citacoes, analytics ou cards incorporados.
    xpath = (
        f'//article[@data-testid="tweet"]'
        f'[.//a[@href="/{USERNAME}/status/{tweet_id}"]/time]'
    )

    def locate(current: webdriver.Chrome) -> Any | bool:
        article = next(
            (element for element in current.find_elements(By.XPATH, xpath) if element.is_displayed()),
            False,
        )
        if article:
            return article
        body_elements = current.find_elements(By.TAG_NAME, "body")
        body_text = body_elements[0].text.casefold() if body_elements else ""
        message = next(
            (phrase for phrase in MISSING_POST_MESSAGES if phrase in body_text), None
        )
        if message:
            raise PostUnavailable(message)
        return False

    try:
        return WebDriverWait(driver, timeout).until(locate)
    except TimeoutException:
        return None


def open_target_menu(driver: webdriver.Chrome, tweet_id: str, timeout: int) -> Any:
    article_xpath = (
        f'//article[@data-testid="tweet"]'
        f'[.//a[@href="/{USERNAME}/status/{tweet_id}"]/time]'
    )
    caret_xpath = article_xpath + '//button[@data-testid="caret"]'
    caret = WebDriverWait(driver, timeout).until(
        lambda current: next(
            (element for element in current.find_elements(By.XPATH, caret_xpath) if element.is_displayed()),
            False,
        )
    )
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", caret)
    try:
        caret.click()
    except Exception:
        driver.execute_script("arguments[0].click();", caret)

    dropdown = WebDriverWait(driver, timeout).until(
        lambda current: next(
            (
                element
                for element in current.find_elements(By.CSS_SELECTOR, '[data-testid="Dropdown"]')
                if element.is_displayed()
            ),
            False,
        )
    )

    # Trava de seguranca: links de quotes/analytics no menu precisam apontar
    # para o mesmo status solicitado. Se for o menu do card pai, nao exclui.
    menu_hrefs = [
        element.get_attribute("href") or ""
        for element in dropdown.find_elements(By.CSS_SELECTOR, 'a[href*="/status/"]')
    ]
    if not any(re.search(rf"/status/{re.escape(tweet_id)}(?:/|$)", href) for href in menu_hrefs):
        raise RuntimeError(
            f"Menu recusado: ele nao pertence ao status {tweet_id}."
        )
    return dropdown


def click_delete(driver: webdriver.Chrome, tweet_id: str, timeout: int) -> None:
    dropdown = open_target_menu(driver, tweet_id, timeout)

    delete_item = WebDriverWait(driver, timeout).until(
        lambda _current: next(
            (
                element
                for element in dropdown.find_elements(By.CSS_SELECTOR, '[role="menuitem"]')
                if element.text.strip().casefold() in {"excluir", "delete"}
            ),
            False,
        )
    )
    try:
        delete_item.click()
    except Exception:
        driver.execute_script("arguments[0].click();", delete_item)

    confirm = WebDriverWait(driver, timeout).until(
        lambda current: next(
            iter(
                current.find_elements(
                    By.CSS_SELECTOR,
                    '[data-testid="confirmationSheetDialog"] '
                    '[data-testid="confirmationSheetConfirm"]',
                )
            ),
            False,
        )
    )
    confirm.click()
    WebDriverWait(driver, timeout).until(
        lambda current: not current.find_elements(
            By.CSS_SELECTOR, '[data-testid="confirmationSheetConfirm"]'
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Exclui posts listados no JSON do crawler.")
    parser.add_argument("arquivo", nargs="?", type=Path, default=DEFAULT_INPUT)
    parser.add_argument(
        "--executar",
        action="store_true",
        help="Executa exclusoes. Sem esta opcao, apenas valida e mostra a lista.",
    )
    parser.add_argument(
        "--testar-interface",
        action="store_true",
        help="Abre o primeiro post e valida os tres pontos sem excluir.",
    )
    parser.add_argument("--delay", type=float, default=2.0, help="Pausa entre exclusoes.")
    parser.add_argument("--timeout", type=int, default=30, help="Espera por elementos do X.")
    parser.add_argument(
        "--tentativas",
        type=int,
        default=3,
        help="Tentativas para erros temporarios em cada post.",
    )
    parser.add_argument("--login-timeout", type=int, default=300)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = args.arquivo.expanduser().resolve()
    targets = load_targets(input_path)
    print(f"Arquivo validado: {len(targets)} post(s) de @{USERNAME}.")
    for target in targets:
        print(f"- {target['url']}")
    if not args.executar and not args.testar_interface:
        print("\nPrévia concluída. Nada foi excluído.")
        print("Para excluir, execute novamente acrescentando --executar.")
        return 0

    if args.executar:
        expected = f"DELETAR {len(targets)}"
        typed = input(f"\nEsta ação é irreversível. Digite {expected!r} para continuar: ")
        if typed.strip() != expected:
            print("Confirmação incorreta. Nada foi excluído.")
            return 2

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = input_path.parent / f"x_delete_{stamp}.jsonl"
    progress_path = input_path.parent / f"{input_path.stem}_delete_progress.json"
    logger = setup_logging(log_path)
    progress = read_progress(progress_path)
    completed = set(progress.get("deleted", [])) | set(progress.get("missing", []))
    pending = [target for target in targets if target["id"] not in completed]
    logger.info(
        "delete_run_started",
        extra={"total": len(targets), "pending": len(pending), "input": str(input_path)},
    )
    if not pending:
        logger.info("nothing_pending")
        return 0

    driver: webdriver.Chrome | None = None
    vivaldi_process: subprocess.Popen[Any] | None = None
    try:
        profile = Path(os.getenv("LOCALAPPDATA", str(Path.cwd()))) / "x-replies-crawler"
        profile.mkdir(parents=True, exist_ok=True)
        vivaldi = find_vivaldi()
        major = detect_chromium_major(vivaldi, profile)
        chromedriver = ensure_chromedriver(major, profile, logger)
        vivaldi_process, address = start_vivaldi(vivaldi, profile, pending[0]["url"], logger)

        options = Options()
        options.binary_location = str(vivaldi)
        options.debugger_address = address
        driver = webdriver.Chrome(
            service=Service(
                executable_path=str(chromedriver),
                service_args=["--disable-build-check"],
            ),
            options=options,
        )
        driver.set_page_load_timeout(60)
        wait_for_login(driver, args.login_timeout, pending[0]["url"])

        if args.testar_interface and not args.executar:
            target = pending[0]
            driver.get(target["url"])
            if target_article(driver, target["id"], args.timeout) is None:
                raise RuntimeError("Card exato do primeiro post pendente nao encontrado.")
            open_target_menu(driver, target["id"], args.timeout)
            logger.info(
                "delete_menu_validated_without_deletion",
                extra={"tweet_id": target["id"], "url": target["url"]},
            )
            print("\nInterface validada. Nenhum post foi excluído.")
            return 0

        for position, target in enumerate(pending, start=1):
            tweet_id, url = target["id"], target["url"]
            logger.info(
                "delete_item_started",
                extra={"position": position, "pending_total": len(pending), "tweet_id": tweet_id, "url": url},
            )
            for attempt in range(1, max(1, args.tentativas) + 1):
                try:
                    driver.get(url)
                    article = target_article(driver, tweet_id, args.timeout)
                    if article is None:
                        raise TimeoutException("Card nao carregou dentro do tempo limite.")
                    click_delete(driver, tweet_id, args.timeout)
                    progress.setdefault("deleted", []).append(tweet_id)
                    progress.setdefault("failed", {}).pop(tweet_id, None)
                    logger.info("post_deleted", extra={"tweet_id": tweet_id, "url": url})
                    break
                except PostUnavailable as exc:
                    if tweet_id not in progress.setdefault("missing", []):
                        progress["missing"].append(tweet_id)
                    progress.setdefault("failed", {}).pop(tweet_id, None)
                    logger.warning(
                        "post_missing_skipped",
                        extra={"tweet_id": tweet_id, "url": url, "reason": str(exc)},
                    )
                    break
                except Exception as exc:
                    if attempt < max(1, args.tentativas):
                        logger.warning(
                            "post_delete_retry",
                            extra={
                                "tweet_id": tweet_id,
                                "url": url,
                                "attempt": attempt,
                                "max_attempts": max(1, args.tentativas),
                                "reason": str(exc),
                            },
                        )
                        time.sleep(args.delay * attempt)
                        continue
                    progress.setdefault("failed", {})[tweet_id] = str(exc)
                    logger.error(
                        "post_delete_failed",
                        extra={"tweet_id": tweet_id, "url": url, "attempts": attempt},
                        exc_info=True,
                    )
            save_progress(progress_path, progress)
            time.sleep(args.delay)
    except TimeoutException:
        logger.error("login_or_page_timeout", exc_info=True)
        return 1
    except Exception:
        logger.error("delete_run_failed", exc_info=True)
        return 1
    finally:
        if driver is not None:
            driver.quit()
        if vivaldi_process is not None and vivaldi_process.poll() is None:
            vivaldi_process.terminate()

    logger.info(
        "delete_run_completed",
        extra={
            "deleted_total": len(set(progress.get("deleted", []))),
            "missing_total": len(set(progress.get("missing", []))),
            "failed_total": len(progress.get("failed", {})),
            "progress_path": str(progress_path),
        },
    )
    print(f"\nConcluído. Progresso: {progress_path}\nLogs: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
