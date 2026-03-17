"""
Syslog MCP Server

Exposes syslog / Loki search tools over MCP/SSE.
Supports any HTTP syslog backend (Loki, Graylog, custom).

Start:  python -m mcp_servers.syslog.server
Port:   MCP_SYSLOG_PORT (default 8106)
"""

import logging
import os
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

logger = logging.getLogger("netcopilot.mcp.syslog")

MCP_PORT = int(os.environ.get("MCP_SYSLOG_PORT", 8106))
SYSLOG_URL = os.environ.get("SYSLOG_URL", "")
SYSLOG_TOKEN = os.environ.get("SYSLOG_TOKEN", "")
SYSLOG_QUERY_PARAM = os.environ.get("SYSLOG_QUERY_PARAM", "query")

mcp = FastMCP("syslog", port=MCP_PORT)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _headers() -> Dict[str, str]:
    h: Dict[str, str] = {}
    if SYSLOG_TOKEN:
        h["Authorization"] = f"Bearer {SYSLOG_TOKEN}"
    return h


def _search(query: str, limit: int, extra_params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not SYSLOG_URL:
        return {"error": "SYSLOG_URL not configured."}
    params: Dict[str, Any] = {SYSLOG_QUERY_PARAM: query, "limit": limit}
    if extra_params:
        params.update(extra_params)
    try:
        resp = httpx.get(SYSLOG_URL, params=params, headers=_headers(), timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def syslog_search(query: str, limit: int = 50) -> Dict[str, Any]:
    """
    Search syslog messages using a free-text or structured query.

    Args:
        query: Search string or structured query (format depends on backend,
               e.g. Loki LogQL or Graylog GELF query).
        limit: Maximum number of log lines to return (default 50).
    """
    return _search(query, limit)


@mcp.tool()
def syslog_search_device(device: str, limit: int = 100) -> Dict[str, Any]:
    """
    Retrieve recent syslog messages for a specific device.

    Args:
        device: Device hostname or IP to filter logs for.
        limit: Maximum number of log lines to return (default 100).
    """
    query = f'{{host="{device}"}}'
    result = _search(query, limit)
    if "error" in result:
        # Fall back to plain text search
        result = _search(device, limit)
    return result


@mcp.tool()
def syslog_search_interface(
    device: str,
    interface: str,
    limit: int = 100,
) -> Dict[str, Any]:
    """
    Search syslog for events related to a specific device interface.

    Args:
        device: Device hostname to filter.
        interface: Interface name to search for in log messages.
        limit: Maximum number of log lines (default 100).
    """
    query = f'{device} {interface}'
    return _search(query, limit)


@mcp.tool()
def syslog_search_severity(
    severity: str,
    device: Optional[str] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    """
    Search syslog for messages at a given severity level.

    Args:
        severity: Syslog severity keyword: critical, error, warning, notice, info.
        device: Optional device hostname to filter.
        limit: Maximum number of log lines (default 100).
    """
    query = severity
    if device:
        query = f"{device} {severity}"
    return _search(query, limit)


@mcp.tool()
def syslog_search_timerange(
    query: str,
    start: str,
    end: str,
    limit: int = 200,
) -> Dict[str, Any]:
    """
    Search syslog within a specific time range.

    Args:
        query: Search string.
        start: Start time as ISO8601 string or Unix timestamp.
        end: End time as ISO8601 string or Unix timestamp.
        limit: Maximum log lines to return (default 200).
    """
    return _search(query, limit, extra_params={"start": start, "end": end})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logger.info("Starting Syslog MCP server on port %d", MCP_PORT)
    mcp.run(transport="sse")
