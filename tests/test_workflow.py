import time

import pytest
from fastapi.testclient import TestClient

import app as appmod
from test_chat import FakeLLM, _Msg, _ToolCall


class FakeSlack:
    def __init__(self):
        self.alerts, self.thread_replies = [], []
        self.last_response = {}

    def send_alert_with_button(self, **kwargs):
        self.alerts.append(kwargs)
        self.last_response = {"ok": True, "channel": "C123", "ts": "1700000000.000100"}
        return None

    def send_thread_reply(self, channel, thread_ts, text):
        self.thread_replies.append((channel, thread_ts, text))
        return None


@pytest.fixture
def fake_slack(monkeypatch):
    slack = FakeSlack()
    monkeypatch.setattr(appmod, "get_slack_adapter", lambda: slack)
    return slack


@pytest.fixture(autouse=True)
def reset_auto_state():
    appmod._auto_recent.clear()
    appmod._auto_starts.clear()
    yield


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


ALERTMANAGER_PAYLOAD = {
    "version": "4",
    "status": "firing",
    "alerts": [{
        "status": "firing",
        "labels": {"alertname": "InterfaceDown", "instance": "10.0.0.1:9116", "ifName": "ge-0/0/1",
                   "severity": "critical", "slack_channel": "#noc"},
        "annotations": {"summary": "Interface ge-0/0/1 down on pe1"},
        "fingerprint": "abc123",
    }],
}


def test_alertmanager_alert_is_mapped_investigated_and_threaded(monkeypatch, fake_slack):
    monkeypatch.setenv("AUTO_INVESTIGATE", "true")
    llm = FakeLLM([_Msg("ge-0/0/1 is down; far end admin down.")])
    monkeypatch.setattr(appmod, "get_llm_adapter", lambda: llm)
    with TestClient(appmod.app) as client:
        r = client.post("/alerts/alertmanager", json=ALERTMANAGER_PAYLOAD)
        assert r.status_code == 200
        alert = r.json()["alerts"][0]
        assert alert["auto_investigation"] == "started"
        assert _wait_for(lambda: fake_slack.thread_replies)

        # device resolved from the instance IP; alert posted with interface
        assert fake_slack.alerts[0]["device"] == "pe1" and fake_slack.alerts[0]["interface"] == "ge-0/0/1"
        channel, ts, text = fake_slack.thread_replies[0]
        assert (channel, ts) == ("C123", "1700000000.000100") and "far end admin down" in text
        # the automatic investigation used the read-only role and is in history
        items = client.get("/investigations", params={"scope": "all"}).json()["investigations"]
        auto = [i for i in items if i["source"] == "alert"]
        assert auto and auto[0]["role"] == "noc" and auto[0]["user"] == "alert:abc123"

        # same alert again → deduplicated, no second LLM call
        again = client.post("/alerts/alertmanager", json=ALERTMANAGER_PAYLOAD).json()["alerts"][0]
        assert again["auto_investigation"].startswith("skipped (duplicate")
    assert len(llm.calls) == 1


def test_auto_investigation_hourly_cap(monkeypatch, fake_slack):
    monkeypatch.setenv("AUTO_INVESTIGATE", "true")
    monkeypatch.setenv("AUTO_INVESTIGATE_MAX_PER_HOUR", "1")
    monkeypatch.setattr(appmod, "get_llm_adapter", lambda: FakeLLM([_Msg("a"), _Msg("b")]))
    with TestClient(appmod.app) as client:
        first = client.post("/alerts/ingest", json={"text": "BGP down", "device": "pe1", "channel": "#noc"}).json()
        second = client.post("/alerts/ingest", json={"text": "CPU high", "device": "xr1", "channel": "#noc"}).json()
    assert first["auto_investigation"] == "started"
    assert second["auto_investigation"] == "skipped (hourly limit of 1 automatic investigations reached)"


def test_resolved_alerts_are_recorded_not_investigated(monkeypatch, fake_slack):
    monkeypatch.setenv("AUTO_INVESTIGATE", "true")
    payload = {**ALERTMANAGER_PAYLOAD, "status": "resolved",
               "alerts": [{**ALERTMANAGER_PAYLOAD["alerts"][0], "status": "resolved"}]}
    with TestClient(appmod.app) as client:
        result = client.post("/alerts/alertmanager", json=payload).json()
    assert result["alerts"] == [{"fingerprint": "abc123", "status": "resolved"}]
    assert fake_slack.alerts == []


def test_history_records_tools_and_is_private(monkeypatch):
    monkeypatch.setenv("NETCOPILOT_API_KEYS", "k-alice:noc,k-bob:noc")
    monkeypatch.setenv("ALLOW_ANONYMOUS_ACCESS", "false")
    llm = FakeLLM([
        _Msg(tool_calls=[_ToolCall("run_playbook", {"intent": "cpu_high", "device": "pe1"})]),
        _Msg("CPU is fine."),
    ])
    monkeypatch.setattr(appmod, "get_llm_adapter", lambda: llm)
    alice, bob = {"X-API-Key": "k-alice"}, {"X-API-Key": "k-bob"}
    with TestClient(appmod.app) as client:
        reply = client.post("/chat", json={"message": "cpu on pe1?", "user": "alice"}, headers=alice).json()
        inv_id = reply["investigation_id"]
        mine = client.get("/investigations", params={"user": "alice"}, headers=alice).json()["investigations"]
        assert [i["id"] for i in mine] == [inv_id]
        assert mine[0]["tools"] == ["run_playbook"] and mine[0]["source"] == "api"
        detail = client.get(f"/investigations/{inv_id}", params={"user": "alice"}, headers=alice).json()
        assert detail["reply"] == "CPU is fine." and detail["steps"][0]["name"] == "run_playbook"
        # another key cannot see it, even when claiming the same user name
        assert client.get(f"/investigations/{inv_id}", params={"user": "alice"}, headers=bob).status_code == 404
        assert client.get("/investigations", params={"user": "alice"}, headers=bob).json()["investigations"] == []
        # noc users cannot list everyone's history
        assert client.get("/investigations", params={"scope": "all"}, headers=bob).json()["investigations"] == []


def test_devices_endpoint_lists_names_without_addresses():
    with TestClient(appmod.app) as client:
        devices = client.get("/devices").json()["devices"]
    assert {d["name"] for d in devices} == {"pe1", "xr1", "sr1", "sw1"}
    assert all("host" not in d for d in devices)


def test_ticket_button_passes_findings_explicitly(monkeypatch):
    """A ticket created from Slack must carry the stored investigation, not rely on chat memory."""
    captured = []

    async def fake_run_chat(message, user, role, check_config=True, source="api"):
        captured.append(message)
        return {"reply": "Ticket NOC-1 created", "investigation_id": "x"}

    monkeypatch.setattr(appmod, "_run_chat", fake_run_chat)
    monkeypatch.setattr(appmod, "get_slack_adapter", lambda: FakeSlack())
    appmod._save_alert("C9", "analysis", {"analysis": "Far end pe2 xe-0/0/1 is admin down (commit by bob)."})
    import json
    from urllib.parse import urlencode
    payload = {"actions": [{"action_id": "netcopilot_create_ticket", "value": "pe1|ge-0/0/1|critical|Link down"}],
               "user": {"username": "carol"}, "channel": {"id": "C9"}, "response_url": ""}
    with TestClient(appmod.app) as client:
        r = client.post("/slack/actions", content=urlencode({"payload": json.dumps(payload)}),
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 200
    assert _wait_for(lambda: captured)
    assert "Far end pe2 xe-0/0/1 is admin down" in captured[-1]
