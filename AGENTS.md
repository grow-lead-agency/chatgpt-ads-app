# AGENTS.md — chatgpt-ads-app (GrowLead fork)

> Primary context: [CLAUDE.md](./CLAUDE.md) (kód, příkazy, API quirks). Platformní kontext: [../AGENTS.md](../AGENTS.md) (GL-ADS, PMRS parent).
>
> Fork `faborsky/chatgpt-ads-app` (MIT). Remote `origin` = grow-lead-agency/chatgpt-ads-app, `upstream` = faborsky. Default větev `growlead-safety` = upstream `main` + GrowLead patch (write ticket gate, ADR-052). Opravy z upstreamu: `git fetch upstream && git merge upstream/main`.

## Testy a CI (GrowLead tooling, GRO-1302)

Python Standard v1, adopce vlna 1. Tooling je jen v nových souborech, které upstream nemá:
`pyproject.toml`, `uv.lock`, `.python-version` (3.12), `Makefile`, `.gitleaksignore`, `AGENTS.md`,
`.github/workflows/{ci,security}.yml`, `.github/scripts/check_requirements_sync.py`. Upstream soubory
(`requirements*.txt`, `setup.sh`, `run.sh`, `CLAUDE.md`, kód) se tooling PR nemění.

| Příkaz | Co dělá |
|---|---|
| `make setup` | `uv sync --locked` (Python 3.12 z `.python-version`) |
| `make test` | `uv run pytest -q`: offline sada, `requests.request` je monkeypatchnutý (`tests/conftest.py`), fake `OPENAI_ADS_API_KEY`, `GL_ADS_TICKET_GATE=off`. Žádná síť, žádný `.env`, žádná `.usage/` |
| `make lint` | `uv run ruff check .`, ve vlně 1 jen syntaktické chyby (`E9,F63,F7,F82`) |
| `make deps-check` | `uv lock --check` + `requirements*.txt` musí sedět 1:1 s `pyproject.toml` |
| `make docs-check` | upstream `scripts/check_docs_consistency.py` |
| `make check` | deps-check + lint + test + docs-check = brána před commitem a před "hotovo" |
| `make audit` | pip-audit nad `uv export --no-dev` (+ gitleaks, pokud je nainstalovaný) |
| `make lock` | `uv lock` |

Záměrně chybí `make fmt` (ruff format ani `ruff check --fix` na upstream kód nepouštět, rozbilo by merge
z upstreamu) a `make type` (pyrefly je vlna 4).

**Co je zápis do produkce:** jakýkoli příkaz CLI s `--confirm` proti skutečnému účtu mění ostrý ChatGPT Ads
účet s reálným rozpočtem (`GL_ADS_TICKET_GATE=strict` je default, vyžaduje `--ticket` a `--why`). Bez
`--confirm` je zápis lokální dry-run (lint + plán requestu, nic se neodešle). Výjimky: `image-upload` a
`file-upload` zapisují rovnou (jen média, bez útraty), `bulk-submit` dry-run posílá serverový `validate_only` job.
`*-archive` je nevratný. Zápis jen ze session se schválením podle `../AGENTS.md` (permission tier "ask").

**CI nikdy nevolá reálné OpenAI Ads API:** workflow nemá žádná tajemství (`permissions: contents: read`,
`persist-credentials: false`), testy nahrazují `requests`. Runner je natvrdo `ubuntu-latest`, protože repo je
veřejné a org proměnná `CI_RUNNER` míří na self-hosted runner. Pytest běží s `--disable-socket` (pytest-socket, jen v `pyproject.toml`): test,
který by obešel mock `requests` a sáhl na síť, spadne na `SocketBlockedError`. Nový test nesmí sahat na síť ani na credentials;
kdyby musel, dostane marker `integration` a do CI nepatří.

**gitleaks:** `.gitleaksignore` drží jen ověřené false positive podle fingerprintu (dnes fake klíč v testu
redakce `tests/test_api.py:16`). Nový nález neignorovat bez ověření, že nejde o skutečné tajemství.

**Upstream merge a závislosti:** runtime (`setup.sh`) instaluje z `requirements.txt`, CI z `uv.lock`. Když merge
z upstreamu změní `requirements*.txt`, CI spadne na `deps-check`: dorovnat `pyproject.toml`, `make lock`,
commitnout `uv.lock`. `--locked` z CI nikdy neodebírat.
