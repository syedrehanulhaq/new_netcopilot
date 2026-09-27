"""Request shape sent to the OpenAI API (no network: the SDK client is faked)."""

import json
from types import SimpleNamespace

from adapters.llm.openai_adapter import OpenAIAdapter, _Message

TOOLS = [{"type": "function", "function": {"name": "cisco_get_cpu_memory", "description": "CPU/mem",
                                           "parameters": {"type": "object", "properties": {"device": {"type": "string"}}}}}]


class _Recorder:
    def __init__(self, response=None):
        self.calls = []
        self.response = response

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _adapter(response=None, **kw):
    adapter = OpenAIAdapter(api_key="sk-test", **kw)
    chat, responses = _Recorder(SimpleNamespace(choices=[])), _Recorder(response)
    models = SimpleNamespace(retrieved=[])
    models.retrieve = lambda model: models.retrieved.append(model)
    adapter.client = SimpleNamespace(chat=SimpleNamespace(completions=chat), responses=responses, models=models)
    return adapter, chat, responses, models


def _item(**kw):
    return SimpleNamespace(**kw)


def _resp(output, status="completed"):
    return SimpleNamespace(output=output, status=status,
                           usage=SimpleNamespace(input_tokens=500, output_tokens=40))


# ---------------------------------------------------------------- Responses API (default for OpenAI)

def test_openai_defaults_to_responses_api_with_no_retention():
    reasoning = _item(type="reasoning", id="rs_1", encrypted_content="enc")
    call = _item(type="function_call", call_id="call_1", name="cisco_get_cpu_memory", arguments='{"device": "xr1"}')
    adapter, chat, responses, _ = _adapter(_resp([reasoning, call]))
    history = [{"role": "system", "content": "You are NetCopilot."}, {"role": "user", "content": "cpu on xr1?"}]
    result = adapter.chat(model="gpt-5.6-sol", messages=history, tools=TOOLS, tool_choice="auto")

    assert chat.calls == []
    sent = responses.calls[0]
    assert sent["store"] is False and sent["include"] == ["reasoning.encrypted_content"]
    assert sent["instructions"] == "You are NetCopilot."
    assert sent["input"] == [{"role": "user", "content": "cpu on xr1?"}]
    assert sent["tools"][0] == {"type": "function", "name": "cisco_get_cpu_memory", "description": "CPU/mem",
                                "parameters": TOOLS[0]["function"]["parameters"], "strict": False}
    assert "max_output_tokens" not in sent and "reasoning" not in sent
    tc = result.choices[0].message.tool_calls[0]
    assert (tc.id, tc.function.name, json.loads(tc.function.arguments)) == ("call_1", "cisco_get_cpu_memory", {"device": "xr1"})
    assert result.usage.prompt_tokens == 500


def test_tool_loop_replays_reasoning_and_sends_outputs():
    reasoning = _item(type="reasoning", id="rs_1", encrypted_content="enc")
    call = _item(type="function_call", call_id="call_1", name="cisco_get_cpu_memory", arguments="{}")
    adapter, _, responses, _ = _adapter(_resp([reasoning, call]))
    first = adapter.chat(model="gpt-5.6-sol", messages=[{"role": "user", "content": "q"}], tools=TOOLS)
    msg = first.choices[0].message
    history = [{"role": "user", "content": "q"}, msg,
               {"role": "tool", "tool_call_id": "call_1", "name": "cisco_get_cpu_memory", "content": '{"cpu_percent": 12}'}]
    responses.response = _resp([_item(type="message", content=[_item(type="output_text", text="CPU is 12%.")])])
    final = adapter.chat(model="gpt-5.6-sol", messages=history, tools=TOOLS)

    sent = responses.calls[1]["input"]
    assert sent[1] is reasoning and sent[2] is call              # replayed unchanged, in order
    assert sent[3] == {"type": "function_call_output", "call_id": "call_1", "output": '{"cpu_percent": 12}'}
    assert final.choices[0].message.content == "CPU is 12%."


def test_effort_and_limit_are_sent_only_when_configured():
    adapter, _, responses, _ = _adapter(_resp([]), reasoning_effort="High", max_completion_tokens=12000)
    adapter.chat(model="gpt-5.6-sol", messages=[{"role": "user", "content": "hi"}])
    assert responses.calls[0]["reasoning"] == {"effort": "high"}
    assert responses.calls[0]["max_output_tokens"] == 12000


def test_refusal_and_truncation_are_reported():
    adapter, _, _, _ = _adapter(_resp([_item(type="message", content=[_item(type="refusal", refusal="no")])]))
    assert "declined" in adapter.chat(model="m", messages=[]).choices[0].message.content
    adapter, _, _, _ = _adapter(_resp([], status="incomplete"))
    assert "cut off" in adapter.chat(model="m", messages=[]).choices[0].message.content


# ---------------------------------------------------------------- Chat Completions (local / OpenAI-compatible)

def test_chat_mode_never_sends_deprecated_max_tokens():
    adapter, chat, responses, _ = _adapter(api_mode="chat")
    adapter.chat(model="llama3.1", messages=[{"role": "user", "content": "hi"}], tools=TOOLS, tool_choice="auto")
    sent = chat.calls[0]
    assert responses.calls == []
    assert "max_tokens" not in sent and "max_completion_tokens" not in sent and "reasoning_effort" not in sent
    assert sent["tools"] == TOOLS and sent["tool_choice"] == "auto"


def test_local_endpoints_default_to_chat_completions():
    assert OpenAIAdapter(api_key="x", base_url="http://litellm:4000/v1").api_mode == "chat"
    assert OpenAIAdapter(api_key="x").api_mode == "responses"


def test_health_checks_model_access_without_generating():
    adapter, chat, responses, models = _adapter()
    assert adapter.health("gpt-5.6-sol")["status"] == "ok"
    assert models.retrieved == ["gpt-5.6-sol"] and chat.calls == [] and responses.calls == []


def test_registry_reads_openai_settings(monkeypatch):
    from adapters.llm.registry import get_llm_adapter

    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "medium")
    monkeypatch.setenv("OPENAI_MAX_COMPLETION_TOKENS", "16000")
    adapter = get_llm_adapter()
    assert adapter.reasoning_effort == "medium" and adapter.max_completion_tokens == 16000
    assert adapter.api_mode == "responses"


def test_assistant_memory_text_is_sent_as_assistant_message():
    adapter, _, responses, _ = _adapter(_resp([]))
    adapter.chat(model="m", messages=[{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
                                      {"role": "user", "content": "c"}])
    assert responses.calls[0]["input"] == [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
                                           {"role": "user", "content": "c"}]
    assert isinstance(_Message(content="x"), _Message)
