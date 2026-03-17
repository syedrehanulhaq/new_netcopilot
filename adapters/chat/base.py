from typing import Optional, Protocol


class ChatAdapter(Protocol):
    name: str

    def send_message(self, channel: str, text: str) -> Optional[str]: ...
