"""Request shape sent to the OpenAI API (no network: the SDK client is faked)."""

from types import SimpleNamespace

from adapters.llm.openai_adapter import OpenAIAdapter

TOOLS = [{"type": "function", "function": {"name": "cisco_get_cpu_memory", "parameters": {"type": "object"}}}]


class _Completions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(choices=[])


def _adapter(**kw):
    adapter = OpenAIAdapter(api_key="sk-test", **kw)
    completions = _Completions()
    models = SimpleNamespace(retrieved=[])
    models.retrieve = lambda model: models.retrieved.append(model)
    adapter.client = SimpleNamespace(chat=SimpleNamespace(completions=completions), models=models)
    return adapter, completions, models


def test_never_sends_deprecated_max_tokens():
    adapter, completions, _ = _adapter()
    adapter.chat(model="gpt-5.6-sol", messages=[{"role": "user", "content": "hi"}], tools=TOOLS, tool_choice="auto")
    sent = completions.calls[0]
    assert "max_tokens" not in sent and "max_completion_tokens" not in sent and "reasoning_effort" not in sent
    assert sent["tools"] == TOOLS and sent["tool_choice"] == "auto"


def test_reasoning_effort_and_completion_limit_when_configured():
    adapter, completions, _ = _adapter(reasoning_effort="High", max_completion_tokens=12000)
    adapter.chat(model="gpt-5.6-sol", messages=[{"role": "user", "content": "hi"}])
    sent = completions.calls[0]
    assert sent["reasoning_effort"] == "high" and sent["max_completion_tokens"] == 12000
    assert "tools" not in sent


def test_health_checks_model_access_without_generating():
    adapter, completions, models = _adapter()
    assert adapter.health("gpt-5.6-sol") == {"status": "ok", "model": "gpt-5.6-sol"}
    assert models.retrieved == ["gpt-5.6-sol"] and completions.calls == []


def test_registry_reads_openai_settings(monkeypatch):
    from adapters.llm.registry import get_llm_adapter

    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "medium")
    monkeypatch.setenv("OPENAI_MAX_COMPLETION_TOKENS", "16000")
    adapter = get_llm_adapter()
    assert adapter.reasoning_effort == "medium" and adapter.max_completion_tokens == 16000
