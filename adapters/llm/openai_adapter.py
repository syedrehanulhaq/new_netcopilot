"""
OpenAI adapter — presents the chat-completions-shaped interface app.py expects.

Two backends:
  responses : OpenAI Responses API (default for api.openai.com). Needed for
              GPT-5.x reasoning models, which reject function tools combined
              with reasoning on /v1/chat/completions.
  chat      : Chat Completions (default for OpenAI-compatible local servers
              such as LiteLLM or vLLM, which mostly implement only that).

Responses calls use store=False (OpenAI does not retain the conversation) and
request encrypted reasoning so the model's reasoning carries across tool calls:
output items are passed back unchanged, like Anthropic thinking blocks.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from openai import OpenAI


# ---------------------------------------------------------------------------
# Chat-completions-shaped response objects (what app.py reads)
# ---------------------------------------------------------------------------

@dataclass
class _FunctionCall:
    name: str
    arguments: str  # JSON string


@dataclass
class _ToolCall:
    id: str
    function: _FunctionCall
    type: str = "function"


@dataclass
class _Message:
    content: Optional[str]
    role: str = "assistant"
    tool_calls: Optional[List[_ToolCall]] = None
    _openai_output: Any = field(default=None, repr=False)  # raw Responses output items, replayed as-is


@dataclass
class _Choice:
    message: _Message
    finish_reason: str = "stop"
    index: int = 0


@dataclass
class _Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class _Response:
    choices: List[_Choice]
    usage: Optional[_Usage] = None


# ---------------------------------------------------------------------------
# Conversions for the Responses API
# ---------------------------------------------------------------------------

def _responses_tools(tools: list) -> list:
    """Chat-completions tool schemas → Responses function tools (non-strict: our schemas have optional fields)."""
    converted = []
    for t in tools or []:
        if t.get("type") != "function":
            continue
        fn = t["function"]
        converted.append({
            "type": "function",
            "name": fn["name"],
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
            "strict": False,
        })
    return converted


def _responses_input(messages: list) -> tuple[str, list]:
    """Chat-format history → (instructions, Responses input items)."""
    instructions: list[str] = []
    items: list = []
    for m in messages:
        if isinstance(m, _Message):
            if m._openai_output is not None:
                items.extend(m._openai_output)  # function_call + reasoning items, unchanged
            elif m.content:
                items.append({"role": "assistant", "content": m.content})
            continue
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "system":
            instructions.append(str(m.get("content", "")))
        elif role in ("user", "assistant"):
            items.append({"role": role, "content": str(m.get("content", ""))})
        elif role == "tool":
            items.append({"type": "function_call_output", "call_id": m["tool_call_id"],
                          "output": str(m.get("content", ""))})
    return "\n\n".join(instructions), items


def _from_responses(response: Any) -> _Response:
    text_parts: list[str] = []
    refusals: list[str] = []
    tool_calls: list[_ToolCall] = []
    for item in response.output or []:
        if item.type == "function_call":
            tool_calls.append(_ToolCall(id=item.call_id, function=_FunctionCall(item.name, item.arguments)))
        elif item.type == "message":
            for part in item.content or []:
                if part.type == "output_text":
                    text_parts.append(part.text)
                elif part.type == "refusal":
                    refusals.append(part.refusal)

    content = "".join(text_parts) or None
    if not content and not tool_calls:
        if refusals:
            content = "The model declined to answer this request."
        elif getattr(response, "status", "") == "incomplete":
            content = "The response was cut off (output token limit reached). Please narrow the question."

    usage = getattr(response, "usage", None)
    return _Response(
        choices=[_Choice(
            message=_Message(content=content, tool_calls=tool_calls or None, _openai_output=list(response.output or [])),
            finish_reason="tool_calls" if tool_calls else "stop",
        )],
        usage=_Usage(
            prompt_tokens=getattr(usage, "input_tokens", 0) or 0,
            completion_tokens=getattr(usage, "output_tokens", 0) or 0,
        ),
    )


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class OpenAIAdapter:
    name = "openai"

    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        reasoning_effort: str = "",
        max_completion_tokens: int = 0,
        api_mode: str = "auto",
    ):
        self.client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
        self.local = bool(base_url)
        self.reasoning_effort = reasoning_effort.strip().lower()  # none | low | medium | high | xhigh | max
        self.max_completion_tokens = max_completion_tokens
        mode = (api_mode or "auto").strip().lower()
        self.api_mode = mode if mode in ("responses", "chat") else ("chat" if self.local else "responses")

    def chat(
        self,
        *,
        model: str,
        messages: List[Dict[str, str]],
        tools: Optional[list] = None,
        tool_choice: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> Any:
        limit = max_tokens or self.max_completion_tokens
        if self.api_mode == "responses":
            instructions, items = _responses_input(messages)
            kwargs: Dict[str, Any] = {
                "model": model,
                "input": items,
                "store": False,                                # OpenAI does not retain the conversation
                "include": ["reasoning.encrypted_content"],    # carry reasoning across tool calls statelessly
            }
            if instructions:
                kwargs["instructions"] = instructions
            if tools:
                kwargs["tools"] = _responses_tools(tools)
                kwargs["tool_choice"] = tool_choice or "auto"
            if limit:
                kwargs["max_output_tokens"] = limit
            if self.reasoning_effort:
                kwargs["reasoning"] = {"effort": self.reasoning_effort}
            return _from_responses(self.client.responses.create(**kwargs))

        # Chat Completions (OpenAI-compatible servers). The deprecated max_tokens is never sent.
        kwargs = {"model": model, "messages": [
            {"role": "assistant", "content": m.content} if isinstance(m, _Message) else m for m in messages
        ]}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"
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
            return {"status": "ok", "model": model, "api": self.api_mode}
        except Exception as exc:
            return {"status": "degraded", "model": model, "error": str(exc)}
