from typing import Optional

import httpx


class SlackAdapter:
    name = "slack"

    def __init__(self, token: str):
        self.token = token

    def send_message(self, channel: str, text: str) -> Optional[str]:
        if not self.token:
            return "SLACK_BOT_TOKEN not configured."
        try:
            resp = httpx.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {self.token}"},
                json={"channel": channel, "text": text},
                timeout=10,
            )
            data = resp.json()
            if not data.get("ok"):
                return data.get("error", "unknown_error")
            return None
        except Exception as exc:
            return str(exc)
