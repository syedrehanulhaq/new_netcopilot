"""Request shape sent to the Anthropic API (no network: the SDK client is faked)."""

from types import SimpleNamespace

from adapters.llm.anthropic_adapter import AnthropicAdapter, _convert_messages

TOOLS = [{"type": "function", "function": {"name": "junos_get_bgp_summary", "description": "d",
                                           "parameters": {"type": "object", "properties": {}}}}]


def _block(**kw):
    return SimpleNamespace(**kw)


def _response(blocks, stop_reason="end_turn"):
    return SimpleNamespace(content=blocks, stop_reason=stop_reason,
                           usage=SimpleNamespace(input_tokens=100, output_tokens=20))


class _Recorder:
    def __init__(self, response):
        self.calls = []
        self._response = response

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


def _adapter(response, **kw):
    adapter = AnthropicAdapter(api_key="sk-ant-test", **kw)
    plain, beta = _Recorder(response), _Recorder(response)
    adapter.client = SimpleNamespace(messages=plain, beta=SimpleNamespace(messages=beta))
    return adapter, plain, beta


def test_opus_5_5_request_uses_fallback_effort_and_room_for_thinking():
    adapter, plain, beta = _adapter(_response([_block(type="text", text="ok")]), effort="medium")
    adapter.chat(model="claude-opus-5-5", messages=[{"role": "user", "content": "hi"}], tools=TOOLS, tool_choice="auto")
    assert plain.calls == []
    sent = beta.calls[0]
    assert sent["betas"] == ["server-side-fallback-2026-07-01"] and sent["fallbacks"] == "default"
    assert sent["output_config"] == {"effort": "medium"}
    assert sent["max_tokens"] == 16000
    assert sent["tool_choice"] == {"type": "auto"}
    assert "thinking" not in sent  # thinking is always on for Opus 5.5; disabling it is a 400


def test_other_models_use_plain_messages_without_effort_by_default():
    adapter, plain, beta = _adapter(_response([_block(type="text", text="ok")]))
    adapter.chat(model="claude-haiku-4-5", messages=[{"role": "user", "content": "hi"}])
    assert beta.calls == [] and "output_config" not in plain.calls[0]


def test_fallback_can_be_turned_off():
    adapter, plain, beta = _adapter(_response([_block(type="text", text="ok")]), fallback="off")
    adapter.chat(model="claude-opus-5-5", messages=[{"role": "user", "content": "hi"}])
    assert beta.calls == [] and len(plain.calls) == 1


def test_thinking_blocks_are_replayed_unchanged_in_the_tool_loop():
    thinking = _block(type="thinking", thinking="", signature="sig-abc")
    tool_use = _block(type="tool_use", id="toolu_1", name="junos_get_bgp_summary", input={"device": "pe1"})
    adapter, _, beta = _adapter(_response([thinking, tool_use], stop_reason="tool_use"))
    resp = adapter.chat(model="claude-opus-5-5", messages=[{"role": "user", "content": "bgp?"}], tools=TOOLS)
    msg = resp.choices[0].message
    assert msg.tool_calls[0].function.name == "junos_get_bgp_summary"

    history = [{"role": "system", "content": "sys"}, {"role": "user", "content": "bgp?"}, msg,
               {"role": "tool", "tool_call_id": "toolu_1", "name": "junos_get_bgp_summary", "content": "{}"}]
    system, converted = _convert_messages(history)
    assert system == "sys"
    assert converted[1]["content"] is msg._anthropic_content          # same blocks, untouched
    assert converted[1]["content"][0] is thinking                      # thinking block first, as produced
    assert converted[2] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "{}"}]}


def test_refusal_is_reported_not_silent():
    adapter, _, _ = _adapter(_response([], stop_reason="refusal"))
    resp = adapter.chat(model="claude-opus-5-5", messages=[{"role": "user", "content": "x"}])
    assert "declined" in resp.choices[0].message.content
