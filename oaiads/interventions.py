"""GrowLead patch: write ticket gate (GL-ADS governance, ADR-052).

Každý skutečný zápis (`--confirm`) musí mít podpis: ticket z gl-ads
(`interventions/claim`) a důvod (`--why`). CLI před zápisem ověří ticket u gl-ads
(stav `claimed`, správný účet) a po zápisu nahlásí `mark-executed` s id entit,
které mutace vrátila. Bez ticketu se nic nevolá.

Režimy (env `GL_ADS_TICKET_GATE`, výchozí `strict`, čte se JEN nesuffixovaná
proměnná, per-account varianta se záměrně ignoruje):
  strict  ticket povinný, preflight GET + postflight POST na gl-ads.
  lite    ticket nepovinný, řádek do deníčku `GL_ADS_JOURNAL_FILE` (přechodný režim,
          než je gl-ads GOV-1 nasazený; formát = ADS-GOVERNANCE.md §6.2).
  off     jen dev/test; hlasitě varuje na stderr.

Postflight se hlásí jen tehdy, když se příkaz o mutaci skutečně pokusil
(`mark_write_attempt()` volají `_api_call` pro ne-GET metody). Když příkaz skončí
dřív (validace, `_die`, nenalezený objekt), ticket zůstává `claimed` a dá se použít
znovu.

Exit kódy: 2 = gate odmítl zápis (chybí ticket/důvod/env, ticket nesedí),
3 = gl-ads nedostupné nebo vrátilo nesmysl, zápis odmítnut. Dry-run (bez
`--confirm`) tento modul vůbec nevolá.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from oaiads import api
from oaiads.formatting import _die, _err

GATE_MODES = ("strict", "lite", "off")
HTTP_TIMEOUT = 10
MIN_WHY_LEN = 10
MAX_WHY_LEN = 1000
DEFAULT_AGENT = "cli/chatgpt-ads-app"
PRAGUE = ZoneInfo("Europe/Prague")

# Stav jednoho běhu CLI: posbírané ids + zda se o mutaci vůbec pokusilo.
_collected: list[str] = []
_write_attempted = False


@dataclass
class TicketContext:
    mode: str
    command: str
    account_name: str
    why: str
    ticket: str | None = None
    url: str | None = None
    api_key: str | None = None
    agent: str = DEFAULT_AGENT
    journal_path: Path | None = None
    started_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))


# ---------------------------------------------------------------------------
# Run state
# ---------------------------------------------------------------------------
def gate_mode() -> str:
    """Režim gate. Záměrně `os.getenv`, ne `_env`: per-account suffix
    (`GL_ADS_TICKET_GATE_<ACCOUNT>`) nesmí gate potichu vypnout."""
    mode = (os.getenv("GL_ADS_TICKET_GATE") or "strict").strip().lower()
    if mode not in GATE_MODES:
        _die(f"GL_ADS_TICKET_GATE={mode!r} není platný režim (strict|lite|off).", 2)
    return mode


def reset_collected() -> None:
    global _write_attempted
    _collected.clear()
    _write_attempted = False


def mark_write_attempt() -> None:
    global _write_attempted
    _write_attempted = True


def write_attempted() -> bool:
    return _write_attempted


def record_result(response: dict | list | None) -> None:
    """Zeptá se odpovědi na id upravené/vytvořené entity. Sbírá vše."""
    if not isinstance(response, dict):
        return
    try:
        if isinstance(response.get("id"), str):
            if response["id"] not in _collected:
                _collected.append(response["id"])
        
        for key in ("data", "results"):
            items = response.get(key)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and isinstance(item.get("id"), str):
                        if item["id"] not in _collected:
                            _collected.append(item["id"])
    except Exception:  # noqa: BLE001
        pass


def collected() -> list[str]:
    # record_result already de-duplicates; keep insertion order so the
    # externalChangeIds sent to gl-ads are deterministic.
    return list(_collected)


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}


def _journal_line(ctx: TicketContext, exit_ok: bool, ext_id: str | None) -> str:
    stamp = dt.datetime.now(PRAGUE).strftime("%Y-%m-%d %H:%M")
    agent = ctx.agent if ctx.agent != DEFAULT_AGENT else "cli"
    outcome = "--confirm" if exit_ok else "--confirm (SELHALO, zkontroluj účet)"
    acct = ext_id if ext_id else ctx.account_name
    return (f"- {stamp} · {agent} (cli/chatgpt-ads-app) · chatgpt {acct} · "
            f"{ctx.command} · {outcome}. Důvod: {ctx.why}.\n")


def _intervention_payload(data) -> dict | None:
    """gl-ads vrací `{intervention: {...}, adAccount: {...}}` (GOV-1 REST), nebo přímo
    řádek. Sloučí obě podoby: stav z řádku, účet z obálky nebo z řádku.
    Cokoliv, co není objekt, vrátí None."""
    if not isinstance(data, dict):
        return None
    row = data.get("intervention")
    if not isinstance(row, dict):
        return data
    merged = dict(row)
    if isinstance(data.get("adAccount"), dict):
        merged["adAccount"] = data["adAccount"]
    return merged


def _env_plain(key: str) -> str | None:
    return os.getenv(key)

# ---------------------------------------------------------------------------
# Preflight — před zápisem
# ---------------------------------------------------------------------------
def preflight(args) -> TicketContext | None:
    """Vrátí kontext ticketu, nebo None pro dry-run. Při odmítnutí ukončí proces."""
    if not getattr(args, "confirm", False):
        return None

    mode = gate_mode()
    why = (getattr(args, "why", None) or "").strip()
    if len(why) < MIN_WHY_LEN:
        _die(f"--confirm vyžaduje --why (min. {MIN_WHY_LEN} znaků): důvod zásahu se zapisuje "
             "do audit logu gl-ads. Nic nebylo zapsáno.", 2)
    if len(why) > MAX_WHY_LEN:
        _die(f"--why je delší než {MAX_WHY_LEN} znaků. Nic nebylo zapsáno.", 2)

    account_name = getattr(args, "account", None) or api.ACTIVE_ACCOUNT
    
    ctx = TicketContext(
        mode=mode,
        command=getattr(args, "command", None) or "?",
        account_name=account_name,
        why=why,
        ticket=(getattr(args, "ticket", None) or "").strip() or None,
        agent=_env_plain("GL_ADS_AGENT") or DEFAULT_AGENT,
    )
    reset_collected()

    if mode == "off":
        if not os.getenv("GL_ADS_TEST_SILENCE_OFF_WARNING"):
            _err("⚠️  GL_ADS_TICKET_GATE=off — zápis bez ticketu a bez deníčku (jen dev/test).")
        return ctx

    if mode == "lite":
        journal = _env_plain("GL_ADS_JOURNAL_FILE")
        if not journal:
            _die("GL_ADS_TICKET_GATE=lite vyžaduje GL_ADS_JOURNAL_FILE (cesta k deníčku). "
                 "Nic nebylo zapsáno.", 2)
        ctx.journal_path = Path(journal).expanduser()
        try:
            ctx.journal_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _die(f"Deníček {ctx.journal_path} nejde založit ({exc}). Nic nebylo zapsáno.", 2)
        return ctx

    # strict
    if not ctx.ticket:
        _die("--confirm vyžaduje --ticket <id> z gl-ads (interventions/claim). "
             "Bez ticketu se do účtu nezapisuje. Nic nebylo zapsáno.", 2)
    ctx.url = (_env_plain("GL_ADS_URL") or "").rstrip("/") or None
    ctx.api_key = _env_plain("GL_ADS_API_KEY")
    for name, value in (("GL_ADS_URL", ctx.url), ("GL_ADS_API_KEY", ctx.api_key)):
        if not value:
            _die(f"Chybí {name} v .env (ticket gate strict). Nic nebylo zapsáno.", 2)

    try:
        meta = api.account_meta(refresh=True)
        external_id = str(meta.get("id") or "").strip()
        if not external_id:
            _die("Nepodařilo se zjistit ID účtu z OpenAI (api.account_meta(refresh=True) nevrátilo 'id'). "
                 "Nic nebylo zapsáno.", 3)
    except Exception as e:
        _die(f"Nepodařilo se zjistit ID účtu z OpenAI (api.account_meta selhalo: {e}). "
             "Nic nebylo zapsáno.", 3)

    try:
        resp = requests.get(f"{ctx.url}/api/v1/interventions/{ctx.ticket}",
                            headers=_headers(ctx.api_key), timeout=HTTP_TIMEOUT)
    except requests.RequestException as exc:
        _die(f"gl-ads nedostupné ({exc.__class__.__name__}), zápis odmítnut. "
             "Nic nebylo zapsáno.", 3)
    if resp.status_code >= 500:
        _die(f"gl-ads vrátilo HTTP {resp.status_code}, zápis odmítnut. Nic nebylo zapsáno.", 3)
    if resp.status_code == 404:
        _die(f"Ticket {ctx.ticket} neexistuje nebo nepatří tvé organizaci. Nic nebylo zapsáno.", 2)
    if resp.status_code in (401, 403):
        _die(f"gl-ads odmítlo API klíč (HTTP {resp.status_code}). Nic nebylo zapsáno.", 2)
    if resp.status_code != 200:
        _die(f"gl-ads vrátilo HTTP {resp.status_code} pro ticket {ctx.ticket}. Nic nebylo zapsáno.", 2)

    try:
        data = _intervention_payload(resp.json())
    except (ValueError, TypeError):
        data = None
    if data is None:
        _die("gl-ads vrátilo neplatnou odpověď pro ticket (očekávám JSON objekt). "
             "Nic nebylo zapsáno.", 3)

    status = data.get("status")
    if status != "claimed":
        _die(f"Ticket {ctx.ticket} je ve stavu {status!r}, zápis vyžaduje 'claimed'. "
             "Nic nebylo zapsáno.", 2)
    account_info = data.get("adAccount")
    if not isinstance(account_info, dict):
        account_info = {}
    external = str(account_info.get("externalAccountId") or "").strip()
    if external != external_id:
        _die(f"Ticket {ctx.ticket} patří účtu {external or '?'}, příkaz míří na "
             f"{external_id}. Nic nebylo zapsáno.", 2)

    return ctx


# ---------------------------------------------------------------------------
# Postflight — po zápisu
# ---------------------------------------------------------------------------
def postflight(ctx: TicketContext | None, exit_ok: bool) -> None:
    """Nahlásí výsledek. Nikdy nevyhodí (ani při chybě uvnitř), aby nepřekryla
    původní chybu příkazu. Bez pokusu o mutaci nic nehlásí a ticket zůstává
    `claimed`."""
    try:
        _postflight(ctx, exit_ok)
    except BaseException as exc:  # noqa: BLE001 — poslední záchranná síť
        _err(f"⚠️  postflight selhal ({exc.__class__.__name__}: {exc}).")


def _postflight(ctx: TicketContext | None, exit_ok: bool) -> None:
    if ctx is None or ctx.mode == "off":
        return
    if not write_attempted():
        if ctx.mode == "strict":
            print(f"   ticket {ctx.ticket}: žádná mutace neproběhla, ticket zůstává claimed.",
                  file=sys.stderr)
        return

    ext_id = None
    try:
        meta = api.account_meta(refresh=False)
        ext_id = str(meta.get("id") or "").strip() or None
    except Exception:
        pass

    if ctx.mode == "lite":
        if ctx.journal_path is None:
            return
        with ctx.journal_path.open("a", encoding="utf-8") as fh:
            fh.write(_journal_line(ctx, exit_ok, ext_id))
        return

    names = collected()
    payload = {
        "externalChangeIds": names,
        "after": {
            "command": ctx.command,
            "ok": exit_ok,
            "resourceNames": len(names),
            "agent": ctx.agent,
            "startedAt": ctx.started_at.isoformat(),
        },
    }
    try:
        resp = requests.post(f"{ctx.url}/api/v1/interventions/{ctx.ticket}/executed",
                             json=payload, headers=_headers(ctx.api_key or ""),
                             timeout=HTTP_TIMEOUT)
        if resp.status_code >= 300:
            raise requests.HTTPError(f"HTTP {resp.status_code}")
        print(f"   ticket {ctx.ticket}: mark-executed OK ({len(names)} resource names)",
              file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        _err(f"⚠️  Zápis proběhl, ale mark-executed pro ticket {ctx.ticket} selhal "
             f"({exc.__class__.__name__}: {exc}). Nahlas ho ručně ze session přes "
             f"MCP `interventions/mark-executed` s externalChangeIds={names!r}.")
