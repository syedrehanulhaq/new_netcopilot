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
class _Response:
    choices: List[_Choice]


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
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": m["tool_call_id"],
                            "content": m.get("content", ""),
                        })
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

    msg = _Message(
        content=text_content,
        role="assistant",
        tool_calls=tool_calls if tool_calls else None,
        _anthropic_content=content_blocks,
    )

    stop_reason = anthropic_response.stop_reason  # "end_turn" / "tool_use"
    finish_reason = "tool_calls" if stop_reason == "tool_use" else "stop"

    return _Response(choices=[_Choice(message=msg, finish_reason=finish_reason)])


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class AnthropicAdapter:
    name = "anthropic"

    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key)

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
            "max_tokens": max_tokens or 4096,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if tools:
            kwargs["tools"] = _convert_tools(tools)
            if tool_choice == "auto" or tool_choice is None:
                kwargs["tool_choice"] = {"type": "auto"}

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
