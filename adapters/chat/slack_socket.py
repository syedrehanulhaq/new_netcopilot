"""
Slack Socket Mode.

NetCopilot opens an outbound WebSocket to Slack and receives slash commands and
button clicks over it, so Slack never has to reach NetCopilot: no public URL,
no tunnel, no inbound firewall rule. The connection is authenticated with an
app-level token (SLACK_APP_TOKEN, scope connections:write), so requests arriving
on it come from Slack without needing request signatures.

The HTTP endpoints (/slack/command, /slack/actions) keep working for
deployments that prefer them; the same handlers serve both.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("netcopilot.slack_socket")

CommandHandler = Callable[[dict], Awaitable[dict]]  # slash-command fields → the reply shown to the user
ActionHandler = Callable[[dict], Awaitable[Any]]    # block_actions payload → None (work continues in background)


class SlackSocketMode:
    def __init__(self, app_token: str, on_command: CommandHandler, on_action: ActionHandler) -> None:
        self.app_token = app_token
        self.on_command = on_command
        self.on_action = on_action
        self.client: Any = None
        self.error = ""
        self._connecting: Any = None

    async def start(self) -> None:
        """Check the token, then connect in the background (never blocks or fails startup)."""
        import asyncio

        from slack_sdk.socket_mode.aiohttp import SocketModeClient
        from slack_sdk.web.async_client import AsyncWebClient

        web = AsyncWebClient()
        try:
            # One call validates the token and returns the first WebSocket URL.
            opened = await web.apps_connections_open(app_token=self.app_token)
        except Exception as exc:
            # The SDK's connect() would retry a bad token forever; report it once instead.
            detail = str(exc)
            try:
                detail = exc.response.get("error") or detail  # SlackApiError: e.g. invalid_auth, not_allowed_token_type
            except Exception:
                pass
            self.error = f"Slack Socket Mode not started: {detail}"
            logger.error("%s (check SLACK_APP_TOKEN: an app-level token with connections:write)", self.error)
            return
        self.client = SocketModeClient(app_token=self.app_token, web_client=web, logger=logger)
        self.client.wss_uri = opened["url"]
        self.client.socket_mode_request_listeners.append(self.handle)
        self._connecting = asyncio.ensure_future(self.client.connect())
        logger.info("Slack Socket Mode connecting (no inbound connection needed)")

    async def stop(self) -> None:
        if self.client is not None:
            try:
                await self.client.close()
            except Exception:
                logger.exception("Closing Slack Socket Mode failed.")

    async def status(self) -> dict:
        connected = False
        if self.client is not None:
            try:
                connected = bool(await self.client.is_connected())
            except Exception:
                connected = False
        return {"mode": "socket", "connected": connected, **({"error": self.error} if self.error else {})}

    async def handle(self, client: Any, req: Any) -> None:
        """Route one Socket Mode request and acknowledge it (Slack expects the ack within 3 s)."""
        from slack_sdk.socket_mode.response import SocketModeResponse

        reply: Optional[dict] = None
        try:
            if req.type == "slash_commands":
                reply = await self.on_command(dict(req.payload or {}))
            elif req.type == "interactive" and (req.payload or {}).get("type") == "block_actions":
                await self.on_action(dict(req.payload))
        except Exception:
            logger.exception("Slack Socket Mode %s request failed.", req.type)
            if req.type == "slash_commands":
                reply = {"response_type": "ephemeral", "text": "NetCopilot could not handle that command."}
        # A slash command's reply travels in the ack; other requests are acknowledged empty.
        await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id, payload=reply))
