from typing import Any, Dict, List, Optional

from openai import OpenAI


class OpenAIAdapter:
    """
    OpenAI Chat Completions (also any OpenAI-compatible endpoint via base_url).

    Current OpenAI reasoning models (GPT-5.x, o-series) reject the deprecated
    `max_tokens`; only `max_completion_tokens` / `reasoning_effort` are sent,
    and only when configured, so older and local models keep working too.
    """

    name = "openai"

    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        reasoning_effort: str = "",
        max_completion_tokens: int = 0,
    ):
        self.client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
        self.local = bool(base_url)
        self.reasoning_effort = reasoning_effort.strip().lower()  # none | low | medium | high | xhigh | max
        self.max_completion_tokens = max_completion_tokens

    def chat(
        self,
        *,
        model: str,
        messages: List[Dict[str, str]],
        tools: Optional[list] = None,
        tool_choice: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> Any:
        kwargs: Dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"
        limit = max_tokens or self.max_completion_tokens
        if limit:
            kwargs["max_completion_tokens"] = limit
        if self.reasoning_effort:
            kwargs["reasoning_effort"] = self.reasoning_effort
        return self.client.chat.completions.create(**kwargs)

    def health(self, model: str) -> Dict[str, Any]:
        try:
            if self.local:
                # Local servers don't always implement /models/{id}; a tiny completion works everywhere.
                self.client.chat.completions.create(model=model, messages=[{"role": "user", "content": "ping"}])
            else:
                self.client.models.retrieve(model)  # checks key + model access without generating
            return {"status": "ok", "model": model}
        except Exception as exc:
            return {"status": "degraded", "model": model, "error": str(exc)}
