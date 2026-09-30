"""Slack Socket Mode: requests arrive over NetCopilot's outbound WebSocket (no network: the SDK client is faked)."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from slack_sdk.socket_mode.request import SocketModeRequest

import app as appmod
from adapters.chat.slack_socket import SlackSocketMode


class _FakeSocketClient:
    def __init__(self):
        self.acks = []

    async def send_socket_mode_response(self, response):
        self.acks.append(response.to_dict())


def _handle(socket, req_type, payload):
    client = _FakeSocketClient()
    req = SocketModeRequest(type=req_type, envelope_id="env-1", payload=payload)
    asyncio.run(socket.handle(client, req))
    return client.acks


def test_slash_command_reply_travels_in_the_ack(monkeypatch):
    appmod._init_alerts_db()  # done at startup, before Socket Mode connects
    monkeypatch.setenv("SLACK_ROLE", "noc")
    socket = SlackSocketMode("xapp-test", appmod._slack_socket_command, appmod._slack_socket_action)
    acks = _handle(socket, "slash_commands",
                   {"text": "ticket BGP flapping on pe1", "user_name": "carol", "channel_id": "C9"})
    assert acks[0]["envelope_id"] == "env-1"
    assert "noc-ticket" in acks[0]["payload"]["text"]  # same role check as the HTTP endpoint


def test_button_click_is_acked_and_investigated_in_the_background(monkeypatch):
    chats = []

    async def fake_run_chat(message, user, role, check_config=True, source="api"):
        chats.append((message, user, source))
        return {"reply": "ge-0/0/0 is admin up, oper down."}

    monkeypatch.setattr(appmod, "_run_chat", fake_run_chat)
    appmod._actions_in_progress.clear()
    socket = SlackSocketMode("xapp-test", appmod._slack_socket_command, appmod._slack_socket_action)
    payload = {"type": "block_actions", "user": {"username": "carol"}, "channel": {"id": "C9"}, "response_url": "",
               "actions": [{"action_id": "netcopilot_investigate", "value": "pe1|ge-0/0/1|critical|Link down"}]}

    async def main():
        client = _FakeSocketClient()
        await socket.handle(client, SocketModeRequest(type="interactive", envelope_id="env-2", payload=payload))
        for _ in range(100):  # the investigation runs as a background task after the ack
            if chats:
                break
            await asyncio.sleep(0.01)
        return client.acks

    acks = asyncio.run(main())
    assert acks == [{"envelope_id": "env-2"}]      # acknowledged immediately, nothing else in the ack
    assert chats and chats[0][1] == "carol" and chats[0][2] == "slack"
    assert "device=pe1" in chats[0][0] and "interface=ge-0/0/1" in chats[0][0]
    assert appmod._actions_in_progress == set()   # released when the investigation finished


def test_failures_and_other_requests_are_still_acknowledged():
    async def broken(_):
        raise RuntimeError("boom")

    called = []

    async def record(payload):
        called.append(payload)

    socket = SlackSocketMode("xapp-test", broken, record)
    acks = _handle(socket, "slash_commands", {"text": "hi"})
    assert acks[0]["payload"]["text"] == "NetCopilot could not handle that command."
    acks = _handle(socket, "events_api", {"event": {"type": "app_mention"}})
    assert acks == [{"envelope_id": "env-1"}] and called == []   # not handled, but never left unanswered


def test_bad_app_token_is_reported_once_and_startup_continues(monkeypatch):
    from slack_sdk.errors import SlackApiError
    from slack_sdk.web.async_client import AsyncWebClient

    async def reject(self, **kwargs):
        raise SlackApiError("invalid", {"ok": False, "error": "invalid_auth"})

    monkeypatch.setattr(AsyncWebClient, "apps_connections_open", reject)
    socket = SlackSocketMode("xapp-bad", appmod._slack_socket_command, appmod._slack_socket_action)
    asyncio.run(socket.start())
    assert socket.client is None and "invalid_auth" in socket.error
    assert asyncio.run(socket.status()) == {"mode": "socket", "connected": False, "error": socket.error}


def test_startup_uses_socket_mode_only_when_an_app_token_is_set(monkeypatch):
    started = []

    async def fake_start(self):
        started.append(self.app_token)

    async def fake_status(self):
        return {"mode": "socket", "connected": True}

    async def fake_stop(self):
        pass

    monkeypatch.setattr(SlackSocketMode, "start", fake_start)
    monkeypatch.setattr(SlackSocketMode, "status", fake_status)
    monkeypatch.setattr(SlackSocketMode, "stop", fake_stop)

    with TestClient(appmod.app) as client:
        assert client.get("/health").json()["slack"] == {"mode": "http"}
    assert started == []

    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-1-test")
    with TestClient(appmod.app) as client:
        assert client.get("/health").json()["slack"] == {"mode": "socket", "connected": True}
    assert started == ["xapp-1-test"]
    appmod._slack_socket = None


@pytest.mark.parametrize("env,status", [({"SLACK_APP_TOKEN": "xapp-1"}, "pass"),
                                        ({"SLACK_BOT_TOKEN": "xoxb-1"}, "fail")])
def test_posture_prefers_socket_mode(monkeypatch, env, status):
    import guardrails

    for k, v in env.items():
        monkeypatch.setenv(k, v)
    slack = next(c for c in guardrails.posture_report("") if c["id"] == "slack")
    assert slack["status"] == status
    if status == "pass":
        assert "Socket Mode" in slack["detail"]
