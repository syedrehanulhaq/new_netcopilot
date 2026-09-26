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
import re
import time
from datetime import datetime, timezone
from functools import partial
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

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
    headers: Dict[str, str] = {}
    token = _syslog_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    tenant = get_secret("SYSLOG_LOKI_TENANT", "")
    if tenant:
        headers["X-Scope-OrgID"] = tenant  # multi-tenant Loki
    return headers


def _backend() -> str:
    """'loki' or 'generic'. SYSLOG_BACKEND wins; otherwise inferred from the URL."""
    configured = get_secret("SYSLOG_BACKEND", "").strip().lower()
    if configured in ("loki", "generic"):
        return configured
    url = _syslog_url()
    parts = urlsplit(url)
    if "loki" in url.lower() or parts.port == 3100:
        return "loki"
    return "generic"


# ---------------------------------------------------------------------------
# Grafana Loki (LogQL over /loki/api/v1/query_range)
# ---------------------------------------------------------------------------

_LABELS_KEPT = ("host", "hostname", "device", "severity", "level", "facility", "app", "program", "job")
_SEVERITY_WORD = re.compile(r"^[A-Za-z]{2,16}$")


def _loki_url() -> str:
    base = _syslog_url().rstrip("/")
    return base if base.endswith("/loki/api/v1/query_range") else base + "/loki/api/v1/query_range"


def _logql_string(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _selector(device: Optional[str] = None) -> str:
    if device:
        label = get_secret("SYSLOG_LOKI_DEVICE_LABEL", "host") or "host"
        return "{" + f"{label}={_logql_string(device)}" + "}"
    return get_secret("SYSLOG_LOKI_SELECTOR", '{job=~".+"}') or '{job=~".+"}'


def _to_ns(value: Any) -> str:
    """Unix seconds / nanoseconds or RFC3339 → a value Loki accepts."""
    text = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        number = float(text)
        return str(int(number * 1e9)) if number < 1e12 else str(int(number))
    return text


def _loki_query(logql: str, limit: int, start: Optional[str] = None, end: Optional[str] = None) -> Dict[str, Any]:
    now = time.time()
    try:
        hours = float(get_secret("SYSLOG_DEFAULT_HOURS", "24") or 24)
    except ValueError:
        hours = 24.0
    limit = max(1, min(int(limit), 1000))
    params = {
        "query": logql,
        "limit": limit,
        "direction": "backward",
        "start": _to_ns(start) if start else str(int((now - hours * 3600) * 1e9)),
        "end": _to_ns(end) if end else str(int(now * 1e9)),
    }
    try:
        r = httpx.get(_loki_url(), params=params, headers=_headers(), timeout=15)
    except Exception as exc:
        return {"error": f"Loki request failed: {exc}", "query": logql}
    if r.status_code >= 400:
        return {"error": f"Loki query failed ({r.status_code}): {r.text[:300]}", "query": logql}
    try:
        streams = r.json().get("data", {}).get("result", [])
    except ValueError:
        return {"error": "Loki returned a non-JSON response.", "query": logql}

    entries = []
    for stream in streams:
        labels = stream.get("stream", {}) or {}
        kept = {k: v for k, v in labels.items() if k in _LABELS_KEPT}
        for ts, line in stream.get("values", []):
            entries.append((int(ts), kept, line))
    entries.sort(key=lambda e: e[0], reverse=True)
    lines = [
        {"time": datetime.fromtimestamp(ts / 1e9, tz=timezone.utc).isoformat(), "labels": labels, "line": line}
        for ts, labels, line in entries[:limit]
    ]
    return {"backend": "loki", "query": logql, "count": len(lines), "lines": lines}


# ---------------------------------------------------------------------------
# Generic HTTP search backend (Graylog / custom): GET <SYSLOG_URL>?<param>=...&limit=...
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


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _not_configured() -> Optional[Dict[str, Any]]:
    return None if _syslog_url() else {"error": "SYSLOG_URL not configured."}


def _sync_syslog_search(query: str, limit: int = 50) -> Dict[str, Any]:
    if _backend() != "loki":
        return _sync_search(query, limit)
    if _not_configured():
        return _not_configured()
    logql = query if query.strip().startswith("{") else f"{_selector()} |= {_logql_string(query)}"
    return _loki_query(logql, limit)


def _sync_syslog_search_device(device: str, limit: int = 100) -> Dict[str, Any]:
    if _backend() != "loki":
        query = f'{{host="{device}"}}'
        result = _sync_search(query, limit)
        if "error" in result:
            result = _sync_search(device, limit)
        return result
    if _not_configured():
        return _not_configured()
    result = _loki_query(_selector(device), limit)
    if not result.get("error") and result.get("count") == 0:
        # The device label may differ (hostname vs IP) — fall back to a line match.
        result = _loki_query(f"{_selector()} |= {_logql_string(device)}", limit)
    return result


def _sync_syslog_search_interface(device: str, interface: str,
                                   limit: int = 100) -> Dict[str, Any]:
    if _backend() != "loki":
        return _sync_search(f"{device} {interface}", limit)
    if _not_configured():
        return _not_configured()
    return _loki_query(f"{_selector(device)} |= {_logql_string(interface)}", limit)


def _sync_syslog_search_severity(severity: str, device: Optional[str] = None,
                                  limit: int = 100) -> Dict[str, Any]:
    if _backend() != "loki":
        query = f"{device} {severity}" if device else severity
        return _sync_search(query, limit)
    if _not_configured():
        return _not_configured()
    if not _SEVERITY_WORD.match(severity or ""):
        return {"error": "severity must be a single word such as critical, error or warning."}
    return _loki_query(f"{_selector(device)} |~ {_logql_string('(?i)' + severity)}", limit)


def _sync_syslog_search_timerange(query: str, start: str, end: str,
                                   limit: int = 200) -> Dict[str, Any]:
    if _backend() != "loki":
        return _sync_search(query, limit, extra_params={"start": start, "end": end})
    if _not_configured():
        return _not_configured()
    logql = query if query.strip().startswith("{") else f"{_selector()} |= {_logql_string(query)}"
    return _loki_query(logql, limit, start=start, end=end)


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
