import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

import app as appmod


class _Fn:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


class _ToolCall:
    def __init__(self, name, args):
        self.id = f"call_{name}"
        self.function = _Fn(name, json.dumps(args))


class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content, self.tool_calls = content, tool_calls


class FakeLLM:
    """Synchronous like the real SDK clients. Replies with scripted messages in order."""

    def __init__(self, script, delay=0.0):
        self.script = list(script)
        self.delay = delay
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        time.sleep(self.delay)
        msg = self.script.pop(0) if self.script else _Msg("done")
        return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()

    def health(self, model):
        return {"status": "ok", "model": model}


@pytest.mark.parametrize("message,blocked", [
    ("When was the last commit on pe1?", False),
    ("Did anyone change the config on pe1 yesterday?", False),
    ("Investigate alert: BGP neighbor 10.0.0.2 state change detected", False),
    ("any update on the flapping link?", False),
    ("show configuration on pe1", False),
    ("update NOC-123 priority to High", False),
    ("add a comment to the ticket saying the optic was replaced", False),
    ("configure bgp neighbor 10.0.0.2 on pe1", True),
    ("please shut down ge-0/0/1 on pe1", True),
    ("Can you clear bgp neighbor 10.0.0.2?", True),
    ("reload xr1", True),
])
def test_read_only_guard(message, blocked):
    assert appmod._is_config_request(message) is blocked


def test_stream_endpoint_applies_read_only_guard(monkeypatch):
    fake = FakeLLM([_Msg("I would configure it")])
    monkeypatch.setattr(appmod, "get_llm_adapter", lambda: fake)
    with TestClient(appmod.app) as client:
        r = client.post("/chat/stream", json={"message": "configure bgp on pe1", "user": "t1"})
    assert "read-only mode" in r.text
    assert fake.calls == []  # never reached the LLM


def test_llm_call_does_not_block_the_event_loop(monkeypatch):
    fake = FakeLLM([_Msg("answer")], delay=0.5)
    monkeypatch.setattr(appmod, "get_llm_adapter", lambda: fake)

    async def main():
        max_lag = 0.0

        async def ticker():
            nonlocal max_lag
            for _ in range(8):
                t = time.monotonic()
                await asyncio.sleep(0.05)
                max_lag = max(max_lag, time.monotonic() - t - 0.05)

        async def chat():
            await asyncio.sleep(0.12)  # let the ticker get going before the LLM call starts
            msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
            return [e async for e in appmod._agentic_loop("r", msgs, [], "m", "u", "noc")]

        events, _ = await asyncio.gather(chat(), ticker())
        return events, max_lag

    events, max_lag = asyncio.run(main())
    assert events[-1] == {"type": "reply", "content": "answer"}
    assert max_lag < 0.2, f"event loop stalled for {max_lag:.2f}s during the LLM call"


def test_tools_not_offered_to_the_role_are_refused(monkeypatch):
    fake = FakeLLM([_Msg(tool_calls=[_ToolCall("jira_create_ticket", {"summary": "x"})]), _Msg("ok")])
    monkeypatch.setattr(appmod, "get_llm_adapter", lambda: fake)
    offered = [{"type": "function", "function": {"name": "junos_get_bgp_summary", "parameters": {}}}]

    async def main():
        msgs = [{"role": "user", "content": "q"}]
        [e async for e in appmod._agentic_loop("r", msgs, offered, "m", "u", "noc")]
        return msgs

    msgs = asyncio.run(main())
    tool_msg = [m for m in msgs if isinstance(m, dict) and m.get("role") == "tool"][0]
    assert "not available to role 'noc'" in tool_msg["content"]


def test_large_tool_output_is_bounded_and_deduplicated():
    big = "x" * 200_000
    text = appmod._compact_tool_result({"device": "xr1", "output": big, "raw_output": big})
    assert len(text) <= appmod._TOOL_RESULT_MAX_CHARS
    data = json.loads(text)
    assert "raw_output" not in data and data["truncated"] is True and data["device"] == "xr1"

    small = {"device": "xr1", "output": "a", "raw_output": "a"}
    assert json.loads(appmod._compact_tool_result(small)) == {"device": "xr1", "output": "a"}


def test_chat_reset_forgets_history(monkeypatch):
    fake = FakeLLM([_Msg("first"), _Msg("second")])
    monkeypatch.setattr(appmod, "get_llm_adapter", lambda: fake)
    with TestClient(appmod.app) as client:
        client.post("/chat", json={"message": "hello", "user": "reset-user"})
        assert appmod._load_memory("reset-user")
        r = client.post("/chat/reset", json={"user": "reset-user"})
        assert r.json()["messages_removed"] == 2
        assert appmod._load_memory("reset-user") == []


def test_api_key_callers_get_namespaced_memory(monkeypatch):
    monkeypatch.setenv("NETCOPILOT_API_KEYS", "k1:noc,k2:noc")

    class _Req:
        scope: dict = {}
        headers = {"X-API-Key": "k1"}

    user_k1 = appmod._resolve_user(_Req(), "alice")
    _Req.headers = {"X-API-Key": "k2"}
    user_k2 = appmod._resolve_user(_Req(), "alice")
    assert user_k1 != user_k2 and user_k1.endswith(":alice")


def test_stdlib_secrets_module_is_not_shadowed():
    import secrets

    from conftest import ROOT

    assert callable(secrets.choice) and callable(secrets.randbelow)
    assert not secrets.__file__.startswith(ROOT)
