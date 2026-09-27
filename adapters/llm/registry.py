from typing import Optional

from adapters.llm.base import LLMAdapter
from adapters.llm.openai_adapter import OpenAIAdapter
from settings import get_setting


def _int_setting(key: str, default: int) -> int:
    try:
        return int(get_setting(key, str(default)))
    except ValueError:
        return default


def get_llm_adapter() -> Optional[LLMAdapter]:
    provider = get_setting("LLM_PROVIDER", "openai").lower()

    if provider == "anthropic":
        from adapters.llm.anthropic_adapter import AnthropicAdapter  # noqa: PLC0415
        api_key = get_setting("ANTHROPIC_API_KEY", "")
        if not api_key:
            return None
        try:
            max_tokens = int(get_setting("ANTHROPIC_MAX_TOKENS", "16000"))
        except ValueError:
            max_tokens = 16000
        return AnthropicAdapter(
            api_key=api_key,
            workspace_id=get_setting("ANTHROPIC_WORKSPACE_ID", ""),
            effort=get_setting("ANTHROPIC_EFFORT", ""),
            max_tokens=max_tokens,
            fallback=get_setting("ANTHROPIC_FALLBACK", "auto"),
        )

    if provider == "local":
        local_base_url = get_setting("LOCAL_LLM_BASE_URL", "")
        if not local_base_url:
            return None
        local_api_key = get_setting("LOCAL_LLM_API_KEY", "local")
        return OpenAIAdapter(
            api_key=local_api_key,
            base_url=local_base_url,
            reasoning_effort=get_setting("LOCAL_LLM_REASONING_EFFORT", ""),
            max_completion_tokens=_int_setting("LOCAL_LLM_MAX_COMPLETION_TOKENS", 0),
            api_mode=get_setting("LOCAL_LLM_API_MODE", "chat"),
        )

    openai_api_key = get_setting("OPENAI_API_KEY", "")
    if not openai_api_key:
        return None
    return OpenAIAdapter(
        api_key=openai_api_key,
        reasoning_effort=get_setting("OPENAI_REASONING_EFFORT", ""),
        max_completion_tokens=_int_setting("OPENAI_MAX_COMPLETION_TOKENS", 0),
        api_mode=get_setting("OPENAI_API_MODE", "responses"),
    )
