# TwitterDelete

Dois scripts em Python que automatizam o navegador para apagar posts antigos do X (Twitter):

1. `main.py` percorre o perfil e salva num JSON os posts e respostas que contêm `$`.
2. `delete_comments.py` lê esse JSON e exclui cada post pela interface do X.

Não usa a API do X. O Selenium controla o Vivaldi com uma sessão persistente, então o login é feito à mão só na primeira vez.

## Requisitos

- Windows
- [Vivaldi](https://vivaldi.com) instalado no local padrão
- Python 3.10+

O ChromeDriver compatível com o Chromium do Vivaldi é baixado automaticamente e fica em cache em `%LOCALAPPDATA%\x-replies-crawler\drivers`.

```bash
python -m venv venv
```

```bash
venv\Scripts\activate
```

```bash
pip install -r requirements.txt
```

## 1. Coletar posts (`main.py`)

```bash
python main.py --usuario seu_usuario
```

Se o X pedir login, entre na janela do Vivaldi que abriu. O script continua sozinho depois.

Ele abre `x.com/<usuario>/with_replies`, rola a timeline e guarda em memória os cards cujo autor é o usuário. Para quando chega ao `--ano-limite` ou quando a página para de carregar. No fim, filtra os textos com `$` e grava em `Downloads`:

- `x_<usuario>_cashtags_<data>.json`: lista com `id`, `texto`, `cashtags`, `publicado_em` e `url`
- `x_<usuario>_crawler_<data>.jsonl`: log

| Opção | Padrão | Uso |
| --- | --- | --- |
| `--usuario` | `lenekuuu` | Perfil, sem `@` |
| `--ano-limite` | `2021` | Para ao alcançar posts deste ano |
| `--delay` | `5` | Segundos de espera após cada rolagem |
| `--espera-html` | `15` | Espera máxima por novos cards |
| `--tentativas-fim` | `5` | Rolagens sem mudança para considerar o fim |
| `--max-scrolls` | `10000` | Limite de rolagens |
| `--login-timeout` | `300` | Segundos para fazer login |

A pasta de saída pode ser trocada com a variável `X_CRAWLER_DOWNLOADS`.

## 2. Excluir posts (`delete_comments.py`)

Revise o JSON antes. Apague do arquivo o que não deve ser excluído.

Só valida o arquivo e lista as URLs, sem abrir o navegador:

```bash
python delete_comments.py caminho\para\x_usuario_cashtags.json
```

Abre o primeiro post e testa o menu de três pontos, sem excluir:

```bash
python delete_comments.py caminho\para\x_usuario_cashtags.json --testar-interface
```

Exclui de verdade. Pede para digitar `DELETAR <quantidade>` antes de começar:

```bash
python delete_comments.py caminho\para\x_usuario_cashtags.json --executar
```

Para cada post, o script abre a URL, localiza o card pelo link de data/hora, abre o menu, confere se o menu pertence ao mesmo ID e clica em Excluir. Posts que já não existem são marcados como `missing`.

O progresso fica em `<arquivo>_delete_progress.json`, ao lado do JSON de entrada. Se a execução cair, rodar o mesmo comando retoma de onde parou. Opções: `--delay` (2s entre exclusões), `--timeout` (30s), `--tentativas` (3), `--login-timeout` (300s).

## O que dá para melhorar

- **Usuário fixo no código.** `delete_comments.py` tem `USERNAME = "lenekuuu"` e um caminho de arquivo padrão cravados. Deveria aceitar `--usuario` como o `main.py`, ou ler o usuário do próprio JSON.
- **Código duplicado.** Logging, busca do Vivaldi, detecção de versão, download do ChromeDriver e abertura do navegador estão copiados nos dois scripts. Dá para mover para um módulo comum.
- **Só Windows e Vivaldi.** Os caminhos e o download `win64` estão fixos. Aceitar Chrome/Edge ou um caminho de binário por parâmetro tornaria o projeto portável.
- **Filtro fixo.** O critério é "texto contém `$`". Um parâmetro com regex ou palavras-chave permitiria outros usos, como apagar tudo antes de uma data.
- **Dados pessoais no repositório.** Os `.json`/`.jsonl` gerados e a pasta `.idea` estão versionados. Vale colocar `x_*.json*` e `.idea/` no `.gitignore` e removê-los do histórico.
- **Dependência do HTML do X.** Seletores como `data-testid="caret"` e os textos "Excluir"/"Delete" quebram se o X mudar a interface ou o idioma. Usar o arquivo de dados exportado pelo X como fonte dos IDs evitaria depender da rolagem da timeline na coleta.
- **Sem testes.** Funções puras como `extract_card`, `load_targets` e o regex de cashtags podem ser testadas sem navegador.
- **Saídas em lugares diferentes.** A coleta grava em `Downloads` e a exclusão grava ao lado do arquivo de entrada. Uma pasta de saída configurável única seria mais previsível.
