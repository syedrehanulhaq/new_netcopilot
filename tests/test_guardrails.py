import base64
import hashlib
import hmac
import json
import sqlite3
import time
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

import app as appmod
import guardrails
import settings
from redaction import redact_text
from test_chat import FakeLLM, _Msg


@pytest.fixture
def client():
    with TestClient(appmod.app) as c:
        yield c


@pytest.fixture
def no_anonymous(monkeypatch):
    monkeypatch.setenv("ALLOW_ANONYMOUS_ACCESS", "false")


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def test_unauthenticated_remote_clients_are_refused(client, no_anonymous):
    r = client.get("/admin/settings")
    assert r.status_code == 401
    assert "NETCOPILOT_API_KEYS" in r.json()["detail"]
    assert client.post("/chat", json={"message": "hi"}).status_code == 401


def test_local_requests_are_allowed_without_auth_but_not_via_proxy():
    assert guardrails.is_local_request("127.0.0.1", {})
    assert not guardrails.is_local_request("127.0.0.1", {"x-forwarded-for": "203.0.113.9"})
    assert not guardrails.is_local_request("10.1.2.3", {})


def test_api_keys_and_roles(client, no_anonymous, monkeypatch):
    monkeypatch.setenv("NETCOPILOT_API_KEYS", "adminkey:admin,nockey:noc")
    assert client.get("/admin/settings", headers={"X-API-Key": "adminkey"}).status_code == 200
    assert client.get("/admin/settings", headers={"X-API-Key": "nockey"}).status_code == 403
    assert client.get("/admin/settings", headers={"X-API-Key": "wrong"}).status_code == 401
    me = client.get("/auth/me", headers={"X-API-Key": "nockey"}).json()
    assert me["auth_mode"] == "api_key" and me["role"] == "noc" and me["authenticated"]


# ---------------------------------------------------------------------------
# Secrets at rest and the guardrail lock
# ---------------------------------------------------------------------------

def test_secret_settings_are_encrypted_at_rest():
    settings.set_settings({"JIRA_API_TOKEN": "super-secret-token", "JIRA_PROJECT_KEY": "NOC"})
    with sqlite3.connect(settings._SETTINGS_DB) as conn:
        rows = dict(conn.execute("SELECT key, value FROM settings").fetchall())
    assert rows["JIRA_API_TOKEN"].startswith("enc:v1:")
    assert "super-secret-token" not in rows["JIRA_API_TOKEN"]
    assert rows["JIRA_PROJECT_KEY"] == "NOC"  # non-secret stays readable
    assert settings.get_setting("JIRA_API_TOKEN") == "super-secret-token"


def test_legacy_plaintext_secrets_are_migrated(monkeypatch):
    with sqlite3.connect(settings._SETTINGS_DB) as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('CISCO_PASS', 'old-plain')")
    monkeypatch.setattr(settings, "_migrated", False)
    assert settings.get_setting("CISCO_PASS") == "old-plain"
    with sqlite3.connect(settings._SETTINGS_DB) as conn:
        stored = conn.execute("SELECT value FROM settings WHERE key = 'CISCO_PASS'").fetchone()[0]
    assert stored.startswith("enc:v1:")
    settings.set_settings({"CISCO_PASS": "p"})


def test_locked_guardrails_ignore_db_and_refuse_ui_changes(client, monkeypatch):
    settings.set_settings({"REDACT_DEVICE_SECRETS": "false"})
    monkeypatch.setenv("NETCOPILOT_LOCK_GUARDRAILS", "true")
    assert settings.get_setting_bool("REDACT_DEVICE_SECRETS", True) is True  # env/default wins
    r = client.post("/admin/settings", json={"settings": {"REDACT_DEVICE_SECRETS": "false"}})
    assert r.status_code == 403
    monkeypatch.delenv("NETCOPILOT_LOCK_GUARDRAILS")
    settings.set_settings({"REDACT_DEVICE_SECRETS": "true"})


# ---------------------------------------------------------------------------
# Redaction before the LLM
# ---------------------------------------------------------------------------

def test_device_secrets_are_redacted_before_the_llm():
    config = (
        'set system root-authentication encrypted-password "$6$abc$def"\n'
        "snmp-server community Pr1vate RO\n"
        " key-string password 7 1234ABCD\n"
        "  Community: 65000:100\n"
    )
    text = appmod._compact_tool_result({"device": "xr1", "output": config})
    for secret in ("$6$abc$def", "Pr1vate", "1234ABCD"):
        assert secret not in text
    data = json.loads(text)
    assert data["secrets_redacted"] >= 3
    assert "65000:100" in data["output"]  # BGP communities are kept


@pytest.mark.parametrize("line", [
    'secret "$9$abcdefg"; ## SECRET-DATA',
    "password ciphertext AQBapZ8mXyz",
    'community "private" rw version both',
    "set snmp community public authorization read-only",
])
def test_redaction_patterns(line):
    out, n = redact_text(line)
    assert n >= 1 and "<redacted>" in out


# ---------------------------------------------------------------------------
# Webhooks: Slack, Teams, alerts
# ---------------------------------------------------------------------------

def _slack_headers(secret: str, body: str, ts: int) -> dict:
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": str(ts), "X-Slack-Signature": sig,
            "Content-Type": "application/x-www-form-urlencoded"}


def test_slack_signature_is_enforced(client, monkeypatch, no_anonymous):
    monkeypatch.setattr(appmod, "get_llm_adapter", lambda: FakeLLM([_Msg("bgp is fine")]))
    body = urlencode({"text": "bgp status on pe1", "user_name": "bob"})
    now = int(time.time())

    assert client.post("/slack/command", content=body).status_code == 503  # not configured

    monkeypatch.setenv("SLACK_SIGNING_SECRET", "shh")
    ok = client.post("/slack/command", content=body, headers=_slack_headers("shh", body, now))
    assert ok.status_code == 200 and ok.json()["text"] == "bgp is fine"
    bad = client.post("/slack/command", content=body, headers=_slack_headers("wrong", body, now))
    assert bad.status_code == 403
    replay = client.post("/slack/command", content=body, headers=_slack_headers("shh", body, now - 3600))
    assert replay.status_code == 403


def test_teams_hmac_is_enforced(client, monkeypatch, no_anonymous):
    monkeypatch.setattr(appmod, "get_llm_adapter", lambda: FakeLLM([_Msg("pe1 looks healthy")]))
    secret = base64.b64encode(b"teams-shared-secret").decode()
    monkeypatch.setenv("TEAMS_HMAC_SECRET", secret)
    body = json.dumps({"type": "message", "text": "<at>NetCopilot</at> health of pe1?",
                       "from": {"id": "29:1", "name": "Alice"}}).encode()
    sig = base64.b64encode(hmac.new(b"teams-shared-secret", body, hashlib.sha256).digest()).decode()

    ok = client.post("/teams/webhook", content=body, headers={"Authorization": f"HMAC {sig}"})
    assert ok.status_code == 200 and ok.json()["text"] == "pe1 looks healthy"
    assert client.post("/teams/webhook", content=body, headers={"Authorization": "HMAC bogus"}).status_code == 401
    assert client.post("/teams/webhook", content=body).status_code == 401


def test_teams_text_strips_mention_and_html():
    assert appmod._teams_text("<at>NetCopilot</at>&nbsp;show <b>bgp</b> on pe1") == "show bgp on pe1"


def test_alert_ingest_requires_token(client, monkeypatch, no_anonymous):
    alert = {"text": "Interface down", "device": "pe1", "channel": "default"}
    assert client.post("/alerts/ingest", json=alert).status_code == 503
    monkeypatch.setenv("ALERT_INGEST_TOKEN", "tok")
    assert client.post("/alerts/ingest", json=alert, headers={"X-Alert-Token": "nope"}).status_code == 403
    assert client.post("/alerts/ingest", json=alert, headers={"Authorization": "Bearer tok"}).status_code == 200


# ---------------------------------------------------------------------------
# Browser protections, audit, posture
# ---------------------------------------------------------------------------

def test_cross_origin_cookie_writes_are_blocked(client):
    client.cookies.set("session", "anything")
    evil = client.post("/chat/reset", json={"user": "u"}, headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403
    same = client.post("/chat/reset", json={"user": "u"}, headers={"Origin": "http://testserver"})
    assert same.status_code == 200
    client.cookies.clear()


def test_origin_check_behind_a_host_rewriting_proxy(client):
    client.cookies.set("session", "anything")
    headers = {"Origin": "https://netcopilot.example.com", "X-Forwarded-Host": "netcopilot.example.com"}
    assert client.post("/chat/reset", json={"user": "u"}, headers=headers).status_code == 200
    spoofed = {"Origin": "https://evil.example", "X-Forwarded-Host": "netcopilot.example.com"}
    assert client.post("/chat/reset", json={"user": "u"}, headers=spoofed).status_code == 403
    client.cookies.clear()


def test_security_headers(client):
    r = client.get("/")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]


def test_audit_trail_records_admin_changes_and_exports_csv(client):
    client.post("/admin/settings", json={"settings": {"JIRA_DEFAULT_TEAM": "NOC"}})
    events = client.get("/admin/audit", params={"event": "admin_settings_update"}).json()["events"]
    assert events and events[0]["detail"]["keys"] == ["JIRA_DEFAULT_TEAM"]
    csv_text = client.get("/admin/audit.csv").text
    assert csv_text.startswith("id,time_utc,event,user,role,detail")
    assert "admin_settings_update" in csv_text


def test_security_posture_report(client):
    data = client.get("/admin/security-posture").json()
    ids = {c["id"]: c["status"] for c in data["checks"]}
    assert ids["read_only"] == "pass"
    assert ids["auth"] == "fail"          # ALLOW_ANONYMOUS_ACCESS=true in the test env
    assert ids["redaction"] == "pass"
    assert ids["ssh_host_keys"] == "warn"


def test_strict_host_key_checking_params(monkeypatch):
    from adapters.devices import ssh_options

    assert ssh_options.netmiko_host_key_params() == {}
    monkeypatch.setenv("SSH_STRICT_HOST_KEY_CHECKING", "true")
    monkeypatch.setenv("SSH_KNOWN_HOSTS_FILE", "/etc/netcopilot/known_hosts")
    params = ssh_options.netmiko_host_key_params()
    assert params["ssh_strict"] is True and params["alt_key_file"] == "/etc/netcopilot/known_hosts"
    assert ssh_options.junos_host_key_params() == {"hostkey_verify": True}
