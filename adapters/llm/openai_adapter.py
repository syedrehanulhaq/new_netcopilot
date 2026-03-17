from typing import Any, Dict, List, Optional

from openai import OpenAI


class OpenAIAdapter:
    name = "openai"

    def __init__(self, api_key: str, base_url: Optional[str] = None):
        self.client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)

    def chat(
        self,
        *,
        model: str,
        messages: List[Dict[str, str]],
        tools: Optional[list] = None,
        tool_choice: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> Any:
        return self.client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=max_tokens,
        )

    def health(self, model: str) -> Dict[str, Any]:
        try:
            self.client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
            return {"status": "ok", "model": model}
        except Exception as exc:
            return {"status": "degraded", "model": model, "error": str(exc)}
