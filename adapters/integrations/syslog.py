"""
Syslog Integration Adapter — direct httpx calls, no MCP.

Supports any HTTP syslog backend (Loki, Graylog, custom).
Exposes the same tool API as the old syslog MCP server but runs in-process.

Export:
    TOOLS        dict[str, callable]   base-name → async function
    TOOL_SCHEMAS list[dict]            OpenAI function-call schemas (syslog_ prefixed)
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any, Dict, List, Optional

import httpx
from credentials import get_secret

logger = logging.getLogger("netcopilot.adapters.syslog")


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _syslog_url() -> str:
    return get_secret("SYSLOG_URL", "")

def _syslog_token() -> str:
    return get_secret("SYSLOG_TOKEN", "")

def _query_param() -> str:
    return get_secret("SYSLOG_QUERY_PARAM", "query")

def _headers() -> Dict[str, str]:
    token = _syslog_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


# ---------------------------------------------------------------------------
# Sync helpers
# ---------------------------------------------------------------------------

def _sync_search(query: str, limit: int,
                  extra_params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = _syslog_url()
    if not url:
        return {"error": "SYSLOG_URL not configured."}
    params: Dict[str, Any] = {_query_param(): query, "limit": limit}
    if extra_params:
        params.update(extra_params)
    try:
        r = httpx.get(url, params=params, headers=_headers(), timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


def _sync_syslog_search(query: str, limit: int = 50) -> Dict[str, Any]:
    return _sync_search(query, limit)


def _sync_syslog_search_device(device: str, limit: int = 100) -> Dict[str, Any]:
    query = f'{{host="{device}"}}'
    result = _sync_search(query, limit)
    if "error" in result:
        result = _sync_search(device, limit)
    return result


def _sync_syslog_search_interface(device: str, interface: str,
                                   limit: int = 100) -> Dict[str, Any]:
    return _sync_search(f"{device} {interface}", limit)


def _sync_syslog_search_severity(severity: str, device: Optional[str] = None,
                                  limit: int = 100) -> Dict[str, Any]:
    query = f"{device} {severity}" if device else severity
    return _sync_search(query, limit)


def _sync_syslog_search_timerange(query: str, start: str, end: str,
                                   limit: int = 200) -> Dict[str, Any]:
    return _sync_search(query, limit, extra_params={"start": start, "end": end})


# ---------------------------------------------------------------------------
# Async wrappers
# ---------------------------------------------------------------------------

async def _run(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(fn, *args, **kwargs))


async def syslog_search(query: str, limit: int = 50) -> Dict[str, Any]:
    return await _run(_sync_syslog_search, query, limit)

async def syslog_search_device(device: str, limit: int = 100) -> Dict[str, Any]:
    return await _run(_sync_syslog_search_device, device, limit)

async def syslog_search_interface(device: str, interface: str,
                                   limit: int = 100) -> Dict[str, Any]:
    return await _run(_sync_syslog_search_interface, device, interface, limit)

async def syslog_search_severity(severity: str, device: Optional[str] = None,
                                  limit: int = 100) -> Dict[str, Any]:
    return await _run(_sync_syslog_search_severity, severity, device, limit)

async def syslog_search_timerange(query: str, start: str, end: str,
                                   limit: int = 200) -> Dict[str, Any]:
    return await _run(_sync_syslog_search_timerange, query, start, end, limit)


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOLS: Dict[str, Any] = {
    "syslog_search": syslog_search,
    "syslog_search_device": syslog_search_device,
    "syslog_search_interface": syslog_search_interface,
    "syslog_search_severity": syslog_search_severity,
    "syslog_search_timerange": syslog_search_timerange,
}

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "syslog_syslog_search",
            "description": "Search syslog messages using a free-text or structured query (Loki LogQL, Graylog GELF, etc.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search string or structured query."},
                    "limit": {"type": "integer", "description": "Max log lines to return (default 50)."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "syslog_syslog_search_device",
            "description": "Retrieve recent syslog messages for a specific device.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string", "description": "Device hostname or IP."},
                    "limit": {"type": "integer", "description": "Max log lines (default 100)."},
                },
                "required": ["device"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "syslog_syslog_search_interface",
            "description": "Search syslog for events related to a specific device interface.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string", "description": "Device hostname."},
                    "interface": {"type": "string", "description": "Interface name."},
                    "limit": {"type": "integer", "description": "Max log lines (default 100)."},
                },
                "required": ["device", "interface"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "syslog_syslog_search_severity",
            "description": "Search syslog for messages at a given severity level (critical, error, warning, notice, info).",
            "parameters": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "description": "Severity keyword."},
                    "device": {"type": "string", "description": "Optional device hostname filter."},
                    "limit": {"type": "integer", "description": "Max log lines (default 100)."},
                },
                "required": ["severity"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "syslog_syslog_search_timerange",
            "description": "Search syslog within a specific time range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search string."},
                    "start": {"type": "string", "description": "Start time (ISO8601 or Unix timestamp)."},
                    "end": {"type": "string", "description": "End time (ISO8601 or Unix timestamp)."},
                    "limit": {"type": "integer", "description": "Max log lines (default 200)."},
                },
                "required": ["query", "start", "end"],
            },
        },
    },
]
