"""Anthropic Claude adapter — presents an OpenAI-compatible interface to app.py.

app.py accesses responses as:
  resp.choices[0].message          -> has .content and .tool_calls
  tc.function.name                 -> str
  tc.function.arguments            -> JSON string
  tc.id                            -> str
  messages.append(msg)             -> the message object is added back to the list
                                      and re-sent on the next call as a list element

The adapter converts between:
  - OpenAI tool schema  (type/function/parameters)  <-> Anthropic (name/description/input_schema)
  - OpenAI messages     (role/content/tool_calls)   <-> Anthropic messages
  - Anthropic response                              -> fake OpenAI response objects
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import anthropic


# ---------------------------------------------------------------------------
# Fake OpenAI-compatible response objects
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
    """Mimics openai.types.chat.ChatCompletionMessage"""
    content: Optional[str]
    role: str = "assistant"
    tool_calls: Optional[List[_ToolCall]] = None
    # Store raw Anthropic content blocks so we can round-trip them
    _anthropic_content: Any = field(default=None, repr=False)


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
# Conversion helpers
# ---------------------------------------------------------------------------

def _convert_tools(openai_tools: list) -> list:
    """Convert OpenAI tool schema to Anthropic tool schema."""
    result = []
    for t in openai_tools:
        if t.get("type") != "function":
            continue
        fn = t["function"]
        result.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return result


def _is_error_result(content: Any) -> bool:
    """Tool results are JSON objects; an 'error' key marks a failed call."""
    try:
        parsed = json.loads(content) if isinstance(content, str) else content
    except (TypeError, ValueError):
        return False
    return isinstance(parsed, dict) and "error" in parsed


def _convert_messages(messages: list) -> tuple[Optional[str], list]:
    """Convert OpenAI-format messages to Anthropic format.

    Returns (system_prompt, anthropic_messages).
    - Extracts the system message (Anthropic takes it as a separate param).
    - Converts _Message objects (our fake objects added back by app.py) to
      Anthropic's assistant format with tool_use content blocks.
    - Converts tool-role messages into Anthropic's tool_result user blocks.
    """
    system_prompt: Optional[str] = None
    anthropic_messages: list = []

    i = 0
    while i < len(messages):
        msg = messages[i]

        # Handle dict messages
        if isinstance(msg, dict):
            role = msg.get("role")

            if role == "system":
                system_prompt = msg.get("content", "")
                i += 1
                continue

            if role == "user":
                anthropic_messages.append({"role": "user", "content": msg["content"]})
                i += 1
                continue

            if role == "assistant":
                # Plain assistant text message (no tool calls)
                anthropic_messages.append({"role": "assistant", "content": msg.get("content", "")})
                i += 1
                continue

            if role == "tool":
                # Collect consecutive tool results into a single user message
                tool_results = []
                while i < len(messages):
                    m = messages[i]
                    r = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
                    if r != "tool":
                        break
                    if isinstance(m, dict):
                        block = {
                            "type": "tool_result",
                            "tool_use_id": m["tool_call_id"],
                            "content": m.get("content", ""),
                        }
                        if _is_error_result(block["content"]):
                            block["is_error"] = True
                        tool_results.append(block)
                    i += 1
                anthropic_messages.append({"role": "user", "content": tool_results})
                continue

        # Handle our fake _Message objects (re-appended by app.py after tool calls)
        elif isinstance(msg, _Message):
            if msg._anthropic_content is not None:
                # Re-use the original Anthropic content blocks
                anthropic_messages.append({
                    "role": "assistant",
                    "content": msg._anthropic_content,
                })
            elif msg.tool_calls:
                # Reconstruct tool_use blocks from fake objects
                blocks = []
                if msg.content:
                    blocks.append({"type": "text", "text": msg.content})
                for tc in msg.tool_calls:
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.function.name,
                        "input": json.loads(tc.function.arguments),
                    })
                anthropic_messages.append({"role": "assistant", "content": blocks})
            else:
                anthropic_messages.append({
                    "role": "assistant",
                    "content": msg.content or "",
                })
            i += 1
            continue

        i += 1

    return system_prompt, anthropic_messages


def _build_response(anthropic_response) -> _Response:
    """Convert an Anthropic response to a fake OpenAI response."""
    content_blocks = anthropic_response.content  # list of TextBlock / ToolUseBlock

    text_parts = []
    tool_calls = []

    for block in content_blocks:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_calls.append(
                _ToolCall(
                    id=block.id,
                    function=_FunctionCall(
                        name=block.name,
                        arguments=json.dumps(block.input),
                    ),
                )
            )

    text_content = "\n".join(text_parts) if text_parts else None

    stop_reason = anthropic_response.stop_reason  # "end_turn" / "tool_use" / "refusal" / "max_tokens"
    if not text_content and not tool_calls:
        if stop_reason == "refusal":
            text_content = "The model declined to answer this request."
        elif stop_reason == "max_tokens":
            text_content = "The response was cut off (max_tokens reached). Please narrow the question."

    msg = _Message(
        content=text_content,
        role="assistant",
        tool_calls=tool_calls if tool_calls else None,
        _anthropic_content=content_blocks,
    )

    finish_reason = "tool_calls" if stop_reason == "tool_use" else "stop"

    usage = getattr(anthropic_response, "usage", None)
    return _Response(
        choices=[_Choice(message=msg, finish_reason=finish_reason)],
        usage=_Usage(
            prompt_tokens=getattr(usage, "input_tokens", 0) or 0,
            completion_tokens=getattr(usage, "output_tokens", 0) or 0,
        ),
    )


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

# Models that support the server-side refusal fallback (beta). On a policy decline
# (e.g. a cyber-classifier false positive on network-security wording) the API
# retries the same request on Anthropic's recommended model instead of failing.
_FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5")
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicAdapter:
    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        workspace_id: str = "",
        effort: str = "",
        max_tokens: int = 16000,
        fallback: str = "auto",
    ):
        # Keys not scoped to a workspace must name one on every request.
        headers = {"anthropic-workspace-id": workspace_id} if workspace_id else None
        self.client = anthropic.Anthropic(api_key=api_key, default_headers=headers)
        self.effort = effort.strip().lower()          # low | medium | high | xhigh | max; "" = model default
        self.max_tokens = max_tokens                  # thinking counts toward this on current models
        self.fallback = (fallback or "auto").strip().lower()  # auto | on | off

    def _use_fallback(self, model: str) -> bool:
        if self.fallback == "off":
            return False
        return self.fallback == "on" or model.startswith(_FALLBACK_MODELS)

    def chat(
        self,
        *,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[list] = None,
        tool_choice: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> _Response:
        system_prompt, anthropic_messages = _convert_messages(messages)

        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": anthropic_messages,
            "max_tokens": max_tokens or self.max_tokens,
        }
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        if system_prompt:
            kwargs["system"] = system_prompt
        if tools:
            kwargs["tools"] = _convert_tools(tools)
            if tool_choice == "auto" or tool_choice is None:
                kwargs["tool_choice"] = {"type": "auto"}

        if self._use_fallback(model):
            response = self.client.beta.messages.create(betas=[_FALLBACK_BETA], fallbacks="default", **kwargs)
        else:
            response = self.client.messages.create(**kwargs)
        return _build_response(response)

    def health(self, model: str) -> Dict[str, Any]:
        try:
            self.client.messages.create(
                model=model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
            return {"status": "ok", "model": model}
        except Exception as exc:
            return {"status": "degraded", "model": model, "error": str(exc)}
