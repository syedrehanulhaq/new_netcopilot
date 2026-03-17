from typing import Any, Dict, List, Optional, Protocol


class LLMAdapter(Protocol):
    name: str

    def chat(
        self,
        *,
        model: str,
        messages: List[Dict[str, str]],
        tools: Optional[list] = None,
        tool_choice: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> Any: ...

    def health(self, model: str) -> Dict[str, Any]: ...
