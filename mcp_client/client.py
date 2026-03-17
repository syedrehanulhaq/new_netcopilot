"""
NetCopilot MCP Client

Connects to all registered MCP servers at startup, discovers their tools
dynamically, and routes LLM tool calls to the correct server.

Adding a new vendor or integration = deploy a new MCP server + add it to
mcp_servers.yaml. Zero changes required here.
"""

import json
import logging
import os
from typing import Any

import yaml

logger = logging.getLogger("netcopilot.mcp_client")

# anyio ClosedResourceError signals a dropped SSE session.
try:
    from anyio import ClosedResourceError as _ClosedResourceError
except ImportError:
    _ClosedResourceError = OSError  # fallback


class _ServerConn:
    """Manages a single persistent connection to an MCP server.

    Supports two transports:
      sse              — classic SSE (default, url ends in /sse)
      streamable_http  — newer unified transport (url is the base endpoint)
    """

    def __init__(self, name: str, url: str, transport: str = "sse"):
        self.name = name
        self.url = url
        self.transport = transport
        self.session = None
        self._transport_cm = None
        self._sess_cm = None

    async def connect(self) -> None:
        from mcp import ClientSession

        if self.transport == "streamable_http":
            try:
                from mcp.client.streamable_http import streamablehttp_client
                self._transport_cm = streamablehttp_client(self.url)
                read, write, _ = await self._transport_cm.__aenter__()
            except ImportError:
                # Older mcp SDK without streamable_http — fall back to SSE
                logger.warning(
                    "streamable_http not available in installed mcp SDK, "
                    "falling back to SSE for '%s'", self.name
                )
                from mcp.client.sse import sse_client
                self._transport_cm = sse_client(self.url)
                read, write = await self._transport_cm.__aenter__()
        else:
            from mcp.client.sse import sse_client
            self._transport_cm = sse_client(self.url)
            read, write = await self._transport_cm.__aenter__()

        self._sess_cm = ClientSession(read, write)
        self.session = await self._sess_cm.__aenter__()
        await self.session.initialize()

    async def disconnect(self) -> None:
        for cm in (self._sess_cm, self._transport_cm):
            if cm:
                try:
                    await cm.__aexit__(None, None, None)
                except Exception:
                    pass
        self.session = None
        self._transport_cm = None
        self._sess_cm = None

    async def reconnect(self) -> None:
        """Tear down and re-establish the SSE session."""
        await self.disconnect()
        await self.connect()

    async def is_alive(self) -> bool:
        """Lightweight liveness probe — returns False if session is dead."""
        if not self.session:
            return False
        try:
            await self.session.list_tools()
            return True
        except Exception:
            return False

    async def list_tools(self):
        return await self.session.list_tools()

    async def call_tool(self, name: str, arguments: dict):
        return await self.session.call_tool(name, arguments)


def _parse_mcp_result(result) -> Any:
    """Extract usable Python data from an MCP tool result."""
    if not result or not result.content:
        return {}
    content = result.content[0]
    if hasattr(content, "text") and content.text:
        try:
            return json.loads(content.text)
        except (json.JSONDecodeError, ValueError):
            return {"output": content.text}
    return {}


class MCPClient:
    """
    Dynamic MCP client for NetCopilot.

    At startup it reads mcp_servers.yaml, connects to each enabled server,
    and builds:
      - self._tools          : list of tool definitions in OpenAI function format
      - self.tool_to_server  : tool_name → server_name mapping for routing
      - self.server_roles    : server_name → allowed roles

    _run_chat in app.py calls:
      client.get_tools(role)          → filtered tool list for the LLM
      await client.call_tool(name, args)  → route and execute
    """

    def __init__(self, config_path: str = "mcp_servers.yaml"):
        self.config_path = config_path
        self._connections: dict[str, _ServerConn] = {}
        self.tool_to_server: dict[str, str] = {}
        self._tools: list[dict] = []
        self.server_roles: dict[str, list[str]] = {}

    async def start(self) -> None:
        """Connect to all enabled MCP servers and discover their tools."""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
        except FileNotFoundError:
            logger.warning("mcp_servers.yaml not found — no MCP servers loaded.")
            return

        for server_cfg in config.get("servers", []):
            if not server_cfg.get("enabled", True):
                continue
            name = server_cfg["name"]
            # Allow env var override: e.g. JIRA_MCP_URL=http://jira-mcp:8104/sse
            url_env_key = f"{name.upper().replace('-', '_')}_MCP_URL"
            url = os.environ.get(url_env_key) or server_cfg.get("url", "")
            roles = server_cfg.get("roles", ["admin"])
            transport = server_cfg.get("transport", "sse")

            conn = _ServerConn(name, url, transport=transport)
            try:
                await conn.connect()
                tools_result = await conn.list_tools()
                self._connections[name] = conn
                self.server_roles[name] = roles

                for tool in tools_result.tools:
                    prefixed = f"{name}_{tool.name}"
                    self.tool_to_server[prefixed] = name
                    self._tools.append(self._to_openai(tool, prefixed))

                logger.info(
                    "MCP server connected: %s  tools=%d  url=%s",
                    name,
                    len(tools_result.tools),
                    url,
                )
            except Exception as exc:
                logger.warning(
                    "MCP server unavailable: %s  url=%s  reason=%s",
                    name,
                    url,
                    exc,
                )

    async def stop(self) -> None:
        """Disconnect from all MCP servers gracefully."""
        for conn in self._connections.values():
            await conn.disconnect()
        self._connections.clear()
        logger.info("MCP client stopped.")

    # ------------------------------------------------------------------
    # Called by app.py
    # ------------------------------------------------------------------

    def get_tools(self, role: str) -> list[dict]:
        """Return tool definitions (OpenAI format) visible to this role."""
        if role == "admin":
            return list(self._tools)
        allowed_servers = {
            name
            for name, roles in self.server_roles.items()
            if role in roles
        }
        return [
            t for t in self._tools
            if self.tool_to_server.get(t["function"]["name"]) in allowed_servers
        ]

    async def call_tool(self, name: str, arguments: dict) -> Any:
        """Route a tool call to the correct MCP server and return the result."""
        server_name = self.tool_to_server.get(name)
        if not server_name:
            return {"error": f"Tool '{name}' not found in any registered MCP server."}
        conn = self._connections.get(server_name)
        if not conn:
            return {"error": f"MCP server '{server_name}' is not available."}

        # Strip server prefix to get the actual tool name the MCP server knows
        prefix = f"{server_name}_"
        actual_name = name[len(prefix):] if name.startswith(prefix) else name

        try:
            result = await conn.call_tool(actual_name, arguments)
            return _parse_mcp_result(result)
        except _ClosedResourceError:
            # SSE session silently dropped — reconnect once and retry
            logger.warning(
                "SSE session closed for '%s' — reconnecting and retrying.", server_name
            )
            try:
                await conn.reconnect()
                result = await conn.call_tool(actual_name, arguments)
                logger.info("Reconnect successful for '%s'.", server_name)
                return _parse_mcp_result(result)
            except Exception as retry_exc:
                # Reconnect failed — remove from connections so health shows it
                self._connections.pop(server_name, None)
                logger.exception(
                    "Reconnect failed for '%s': %s", server_name, retry_exc
                )
                return {"error": f"MCP server '{server_name}' connection lost and could not reconnect."}
        except Exception as exc:
            logger.exception("Tool call failed: %s  server=%s", name, server_name)
            return {"error": f"Tool execution failed: {exc}"}

    def server_status(self) -> list[dict]:
        """Return connection status for all configured servers (for /health)."""
        status = []
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
        except FileNotFoundError:
            return status
        for srv in config.get("servers", []):
            name = srv["name"]
            conn = self._connections.get(name)
            # A server is connected only if we have a conn with a live session
            connected = conn is not None and conn.session is not None
            tool_count = sum(1 for s in self.tool_to_server.values() if s == name)
            status.append({
                "name": name,
                "url": srv.get("url"),
                "enabled": srv.get("enabled", True),
                "connected": connected,
                "tools": tool_count,
            })
        return status

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _to_openai(tool, name_override: str | None = None) -> dict:
        """Convert an MCP tool definition to OpenAI function-call format."""
        return {
            "type": "function",
            "function": {
                "name": name_override or tool.name,
                "description": tool.description or "",
                "parameters": tool.inputSchema or {
                    "type": "object",
                    "properties": {},
                },
            },
        }
