import datetime as dt
import re

import pytest
import requests

from oaiads import cli
from oaiads import interventions
from oaiads import api

TICKET = "GL-ABC-1234"
WHY = "Oprava překlepů v reklamách podle #1234."
URL = "https://g.test"

class _Resp:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data

    def json(self):
        if self._json is RuntimeError:
            raise ValueError("not json")
        return self._json

class _Http:
    RequestException = requests.RequestException
    HTTPError = requests.HTTPError

    def __init__(self, get_resp=None, post_resp=None, get_exc=None):
        self.get_resp = get_resp or _Resp(200, {})
        self.post_resp = post_resp or _Resp(200, {})
        self.get_exc = get_exc
        self.gets = []
        self.posts = []

    def get(self, url, headers=None, timeout=None):
        self.gets.append(url)
        if self.get_exc:
            raise self.get_exc
        return self.get_resp

    def post(self, url, json, headers=None, timeout=None):
        self.posts.append((url, json))
        return self.post_resp

def _claimed(external="acct_1", status="claimed", envelope=False):
    row = {"status": status, "adAccount": {"externalAccountId": external}}
    if envelope:
        return _Resp(200, {"intervention": row, "adAccount": row["adAccount"]})
    return _Resp(200, row)

class _Args:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

def _confirmed(**kw):
    a = _Args(confirm=True, command="campaign-status", account="acct_1",
              ticket=TICKET, why=WHY, func=lambda args: None)
    for k, v in kw.items():
        setattr(a, k, v)
    return a

@pytest.fixture
def strict_env(monkeypatch):
    monkeypatch.setenv("GL_ADS_TICKET_GATE", "strict")
    monkeypatch.setenv("GL_ADS_URL", URL)
    monkeypatch.setenv("GL_ADS_API_KEY", "secret")
    monkeypatch.setattr(api, "account_meta", lambda refresh: {"id": "acct_1"})
    return monkeypatch

# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------
def test_dry_run_ignores_gate(monkeypatch, strict_env):
    monkeypatch.delenv("GL_ADS_URL")
    ctx = interventions.preflight(_Args(confirm=False))
    assert ctx is None

# ---------------------------------------------------------------------------
# Strict preflight
# ---------------------------------------------------------------------------
def test_strict_rejects_missing_why(strict_env):
    with pytest.raises(SystemExit) as exc:
        interventions.preflight(_confirmed(why="short"))
    assert exc.value.code == 2

def test_strict_rejects_missing_ticket(strict_env):
    with pytest.raises(SystemExit) as exc:
        interventions.preflight(_confirmed(ticket=None))
    assert exc.value.code == 2

def test_strict_rejects_missing_env(strict_env, monkeypatch):
    monkeypatch.delenv("GL_ADS_URL")
    with pytest.raises(SystemExit) as exc:
        interventions.preflight(_confirmed())
    assert exc.value.code == 2

def test_strict_accepts_valid_ticket(strict_env, monkeypatch):
    http = _Http(get_resp=_claimed())
    monkeypatch.setattr(interventions, "requests", http)
    ctx = interventions.preflight(_confirmed())
    assert ctx is not None and ctx.ticket == TICKET
    assert len(http.gets) == 1

def test_strict_rejects_wrong_status(strict_env, monkeypatch):
    http = _Http(get_resp=_claimed(status="executed"))
    monkeypatch.setattr(interventions, "requests", http)
    with pytest.raises(SystemExit) as exc:
        interventions.preflight(_confirmed())
    assert exc.value.code == 2

def test_strict_rejects_wrong_account(strict_env, monkeypatch):
    http = _Http(get_resp=_claimed(external="acct_other"))
    monkeypatch.setattr(interventions, "requests", http)
    with pytest.raises(SystemExit) as exc:
        interventions.preflight(_confirmed())
    assert exc.value.code == 2

def test_strict_rejects_404(strict_env, monkeypatch):
    http = _Http(get_resp=_Resp(404, {}))
    monkeypatch.setattr(interventions, "requests", http)
    with pytest.raises(SystemExit) as exc:
        interventions.preflight(_confirmed())
    assert exc.value.code == 2

def test_strict_network_error_exits_3(strict_env, monkeypatch):
    http = _Http(get_exc=requests.ConnectionError("x"))
    monkeypatch.setattr(interventions, "requests", http)
    with pytest.raises(SystemExit) as exc:
        interventions.preflight(_confirmed())
    assert exc.value.code == 3

def test_strict_non_object_json_exits_3(strict_env, monkeypatch):
    http = _Http(get_resp=_Resp(200, []))
    monkeypatch.setattr(interventions, "requests", http)
    with pytest.raises(SystemExit) as exc:
        interventions.preflight(_confirmed())
    assert exc.value.code == 3

def test_missing_openai_id_exits_3(strict_env, monkeypatch):
    monkeypatch.setattr(api, "account_meta", lambda refresh: {})
    with pytest.raises(SystemExit) as exc:
        interventions.preflight(_confirmed())
    assert exc.value.code == 3

# ---------------------------------------------------------------------------
# Strict postflight
# ---------------------------------------------------------------------------
def test_strict_postflight_posts_collected_ids(strict_env, monkeypatch):
    http = _Http(get_resp=_claimed())
    monkeypatch.setattr(interventions, "requests", http)
    
    def func(args):
        interventions.mark_write_attempt()
        interventions.record_result({"id": "cmpn_1"})
        
    cli._dispatch(_confirmed(func=func))
    
    assert len(http.posts) == 1
    url, json = http.posts[0]
    assert url.endswith(f"/interventions/{TICKET}/executed")
    assert json["externalChangeIds"] == ["cmpn_1"]
    assert json["after"]["ok"] is True

def test_strict_postflight_no_post_if_no_attempt(strict_env, monkeypatch):
    http = _Http(get_resp=_claimed())
    monkeypatch.setattr(interventions, "requests", http)
    cli._dispatch(_confirmed())
    assert len(http.posts) == 0

def test_strict_postflight_reraises_and_posts_ok_false(strict_env, monkeypatch):
    http = _Http(get_resp=_claimed())
    monkeypatch.setattr(interventions, "requests", http)
    
    def func(args):
        interventions.mark_write_attempt()
        raise RuntimeError("boom")
        
    with pytest.raises(RuntimeError):
        cli._dispatch(_confirmed(func=func))
        
    assert len(http.posts) == 1
    url, json = http.posts[0]
    assert json["after"]["ok"] is False

# ---------------------------------------------------------------------------
# lite mode
# ---------------------------------------------------------------------------
def test_lite_appends_journal_line_without_http(monkeypatch, tmp_path):
    journal = tmp_path / "journal" / "log.md"
    monkeypatch.setenv("GL_ADS_TICKET_GATE", "lite")
    monkeypatch.setenv("GL_ADS_JOURNAL_FILE", str(journal))
    monkeypatch.setenv("GL_ADS_AGENT", "Petr")
    monkeypatch.setattr(api, "account_meta", lambda refresh: {"id": "acct_1"})
    
    cli._dispatch(_confirmed(ticket=None, func=lambda a: interventions.mark_write_attempt()))
    line = journal.read_text(encoding="utf-8")
    assert re.match(r"^- \d{4}-\d{2}-\d{2} \d{2}:\d{2} · Petr \(cli/chatgpt-ads-app\) · "
                    r"chatgpt acct_1 · campaign-status · --confirm\. Důvod: " + re.escape(WHY)
                    + r"\.\n$", line)

def test_lite_marks_failed_attempt(monkeypatch, tmp_path):
    journal = tmp_path / "j.md"
    monkeypatch.setenv("GL_ADS_TICKET_GATE", "lite")
    monkeypatch.setenv("GL_ADS_JOURNAL_FILE", str(journal))
    monkeypatch.setattr(api, "account_meta", lambda refresh: {"id": "acct_1"})

    def boom(args):
        interventions.mark_write_attempt()
        raise RuntimeError("api exploded")

    with pytest.raises(RuntimeError):
        cli._dispatch(_confirmed(ticket=None, func=boom))
    assert "SELHALO" in journal.read_text(encoding="utf-8")

def test_lite_requires_journal_file(monkeypatch):
    monkeypatch.setenv("GL_ADS_TICKET_GATE", "lite")
    with pytest.raises(SystemExit) as exc:
        interventions.preflight(_confirmed(ticket=None))
    assert exc.value.code == 2

# ---------------------------------------------------------------------------
# off mode
# ---------------------------------------------------------------------------
def test_off_mode_warns_and_runs(monkeypatch, capsys):
    monkeypatch.setenv("GL_ADS_TICKET_GATE", "off")
    monkeypatch.delenv("GL_ADS_TEST_SILENCE_OFF_WARNING", raising=False)
    ran = []
    cli._dispatch(_confirmed(ticket=None, func=lambda a: ran.append(a)))
    assert len(ran) == 1
    assert "GL_ADS_TICKET_GATE=off" in capsys.readouterr().err

def test_invalid_mode_exits_2(monkeypatch):
    monkeypatch.setenv("GL_ADS_TICKET_GATE", "yolo")
    with pytest.raises(SystemExit) as exc:
        interventions.preflight(_confirmed())
    assert exc.value.code == 2

# ---------------------------------------------------------------------------
# record_result
# ---------------------------------------------------------------------------
def test_record_result_collects_ids():
    interventions.reset_collected()
    interventions.record_result({"id": "cmpn_1"})
    assert interventions.collected() == ["cmpn_1"]
    
    interventions.record_result({"data": [{"id": "ad_1"}, {"id": "ad_2"}]})
    assert set(interventions.collected()) == {"cmpn_1", "ad_1", "ad_2"}
    
    interventions.record_result({"results": [{"id": "rs_1"}]})
    assert set(interventions.collected()) == {"cmpn_1", "ad_1", "ad_2", "rs_1"}

def test_record_result_never_raises():
    interventions.reset_collected()
    interventions.record_result(object())
    interventions.record_result(None)
    assert interventions.collected() == []

# ---------------------------------------------------------------------------
# _api_call integration
# ---------------------------------------------------------------------------
def test_api_call_marks_write_attempt_and_collects(monkeypatch, capture_requests, dummy_resp):
    calls, queue = capture_requests
    queue.append(dummy_resp({"id": "cmpn_9"}))
    monkeypatch.setattr(api, "API_BASE", "https://g.test/v1")
    monkeypatch.setattr(api, "API_HOST", "https://g.test")
    monkeypatch.setattr(api, "_auth_headers", lambda: {})
    
    interventions.reset_collected()
    
    api._api_call("POST", "/write", json_body={})
    
    assert interventions.write_attempted()
    assert interventions.collected() == ["cmpn_9"]

def test_default_mode_is_strict(monkeypatch):
    monkeypatch.delenv("GL_ADS_TICKET_GATE", raising=False)
    monkeypatch.delenv("GL_ADS_TEST_SILENCE_OFF_WARNING", raising=False)
    assert interventions.gate_mode() == "strict"


# ---------------------------------------------------------------------------
# Review additions (Claude, 2026-09-14): cases from the brief the port missed
# ---------------------------------------------------------------------------
def test_strict_rejects_403_without_running_command(strict_env, monkeypatch):
    http = _Http(get_resp=_Resp(403, {}))
    monkeypatch.setattr(interventions, "requests", http)
    ran = []
    with pytest.raises(SystemExit) as exc:
        cli._dispatch(_confirmed(func=lambda a: ran.append(a)))
    assert exc.value.code == 2
    assert ran == [] and http.posts == []


def test_strict_5xx_exits_3_without_running_command(strict_env, monkeypatch):
    http = _Http(get_resp=_Resp(503, {}))
    monkeypatch.setattr(interventions, "requests", http)
    ran = []
    with pytest.raises(SystemExit) as exc:
        cli._dispatch(_confirmed(func=lambda a: ran.append(a)))
    assert exc.value.code == 3
    assert ran == [] and http.posts == []


def test_strict_die_before_write_keeps_exit_code_and_ticket(strict_env, monkeypatch, capsys):
    from oaiads.formatting import _die
    http = _Http(get_resp=_claimed())
    monkeypatch.setattr(interventions, "requests", http)
    with pytest.raises(SystemExit) as exc:
        cli._dispatch(_confirmed(func=lambda a: _die("campaign not found", 1)))
    assert exc.value.code == 1
    assert http.posts == []
    assert "claimed" in capsys.readouterr().err


def test_postflight_http_failure_never_raises(strict_env, monkeypatch, capsys):
    http = _Http(get_resp=_claimed(), post_resp=_Resp(500, {}))
    monkeypatch.setattr(interventions, "requests", http)

    def func(args):
        interventions.mark_write_attempt()
        interventions.record_result({"id": "cmpn_1"})

    cli._dispatch(_confirmed(func=func))
    err = capsys.readouterr().err
    assert "mark-executed" in err and TICKET in err and "HTTPError" in err


def test_postflight_internal_error_never_masks_command(strict_env, monkeypatch, capsys):
    http = _Http(get_resp=_claimed())
    monkeypatch.setattr(interventions, "requests", http)

    def boom():
        raise RuntimeError("collector exploded")

    monkeypatch.setattr(interventions, "collected", boom)
    cli._dispatch(_confirmed(func=lambda a: interventions.mark_write_attempt()))
    assert "postflight selhal" in capsys.readouterr().err


def test_gate_mode_ignores_per_account_suffix(monkeypatch):
    monkeypatch.delenv("GL_ADS_TICKET_GATE", raising=False)
    monkeypatch.setenv("GL_ADS_TICKET_GATE_ACCT_1", "off")
    assert interventions.gate_mode() == "strict"


def test_api_call_get_does_not_mark_write(monkeypatch, capture_requests, dummy_resp):
    calls, queue = capture_requests
    queue.append(dummy_resp({"id": "cmpn_read"}))
    monkeypatch.setattr(api, "API_BASE", "https://g.test/v1")
    monkeypatch.setattr(api, "API_HOST", "https://g.test")
    monkeypatch.setattr(api, "_auth_headers", lambda: {})
    interventions.reset_collected()
    api._api_call("GET", "/campaigns")
    assert not interventions.write_attempted()
    assert interventions.collected() == []


def test_collected_keeps_insertion_order_and_dedupes():
    interventions.reset_collected()
    interventions.record_result({"id": "cmpn_b"})
    interventions.record_result({"id": "cmpn_a"})
    interventions.record_result({"data": [{"id": "ad_1"}, {"id": "cmpn_b"}]})
    assert interventions.collected() == ["cmpn_b", "cmpn_a", "ad_1"]
