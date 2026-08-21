from typing import Any, Optional

import httpx


class SlackAdapter:
    name = "slack"

    def __init__(self, token: str):
        self.token = token

    def _post(self, payload: dict) -> Optional[str]:
        if not self.token:
            return "SLACK_BOT_TOKEN not configured."
        try:
            resp = httpx.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {self.token}"},
                json=payload,
                timeout=10,
            )
            data = resp.json()
            if not data.get("ok"):
                return data.get("error", "unknown_error")
            return None
        except Exception as exc:
            return str(exc)

    def send_message(self, channel: str, text: str) -> Optional[str]:
        return self._post({"channel": channel, "text": text})

    def send_alert_with_button(
        self,
        channel: str,
        alert_text: str,
        device: str = "",
        interface: str = "",
        severity: str = "",
        action_id: str = "netcopilot_investigate",
    ) -> Optional[str]:
        """Post an alert message with an Investigate button using Slack Block Kit."""
        severity_emoji = {"critical": "🔴", "major": "🟠", "minor": "🟡", "info": "🔵"}.get(
            (severity or "").lower(), "⚠️"
        )
        header = f"{severity_emoji} *ALERT: {alert_text}*"
        context_parts = [p for p in [
            f"Device: `{device}`" if device else "",
            f"Interface: `{interface}`" if interface else "",
            f"Severity: *{severity.upper()}*" if severity else "",
        ] if p]

        blocks: list[dict[str, Any]] = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": header},
            },
        ]
        if context_parts:
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": " · ".join(context_parts)}],
            })
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🔍 Investigate", "emoji": True},
                    "style": "primary",
                    "action_id": action_id,
                    "value": f"{device}|{interface}|{severity}|{alert_text}",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🎫 Create Ticket", "emoji": True},
                    "action_id": "netcopilot_create_ticket",
                    "value": f"{device}|{interface}|{severity}|{alert_text}",
                },
            ],
        })

        return self._post({
            "channel": channel,
            "text": f"ALERT: {alert_text}",  # fallback for notifications
            "blocks": blocks,
        })

    def post_investigation_result(
        self,
        response_url: str,
        findings: str,
        device: str = "",
        interface: str = "",
        severity: str = "",
        alert_text: str = "",
    ) -> None:
        """Post investigation findings with a Create Ticket button via response_url."""
        # Truncate findings to stay within Slack's 3000-char block text limit
        max_len = 2800
        display = findings if len(findings) <= max_len else findings[:max_len] + "\n_…truncated_"

        button_value = f"{device}|{interface}|{severity}|{alert_text}"

        blocks: list[dict[str, Any]] = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Investigation findings:*\n{display}"},
            },
            {"type": "divider"},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "🎫 Create Ticket", "emoji": True},
                        "style": "primary",
                        "action_id": "netcopilot_create_ticket",
                        "value": button_value,
                    },
                ],
            },
        ]

        try:
            httpx.post(
                response_url,
                json={
                    "response_type": "in_channel",
                    "text": findings,  # fallback
                    "blocks": blocks,
                    "replace_original": False,
                },
                timeout=10,
            )
        except Exception:
            pass
