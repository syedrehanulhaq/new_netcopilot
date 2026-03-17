"""
Prometheus Integration Adapter — direct httpx calls, no MCP.

Exposes the same tool API as the old prometheus MCP server but runs in-process.

Export:
    TOOLS        dict[str, callable]   base-name → async function
    TOOL_SCHEMAS list[dict]            OpenAI function-call schemas (prometheus_ prefixed)
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from functools import partial
from typing import Any, Dict, List, Optional

import httpx
from credentials import get_secret

logger = logging.getLogger("netcopilot.adapters.prometheus")


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _prom_url() -> str:
    return get_secret("PROMETHEUS_URL", "").rstrip("/")

def _flap_query_template() -> str:
    return get_secret(
        "PROMETHEUS_FLAP_QUERY",
        "topk({limit}, netcopilot_link_flaps_total)",
    )

def _last_flap_query() -> str:
    return get_secret(
        "PROMETHEUS_LAST_FLAP_QUERY",
        "netcopilot_link_last_flap_timestamp_seconds",
    )


# ---------------------------------------------------------------------------
# Sync helpers
# ---------------------------------------------------------------------------

def _sync_prom_get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    url = _prom_url()
    if not url:
        return {"error": "PROMETHEUS_URL not configured."}
    try:
        r = httpx.get(f"{url}{path}", params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


def _sync_instant_query(query: str) -> List[Dict[str, Any]]:
    result = _sync_prom_get("/api/v1/query", {"query": query})
    if "error" in result:
        raise RuntimeError(result["error"])
    if result.get("status") != "success":
        raise RuntimeError("Prometheus returned non-success status.")
    return result.get("data", {}).get("result", [])


def _sync_prometheus_query(query: str) -> Dict[str, Any]:
    return _sync_prom_get("/api/v1/query", {"query": query})


def _sync_prometheus_range_query(query: str, start: str, end: str,
                                  step: str = "60s") -> Dict[str, Any]:
    return _sync_prom_get("/api/v1/query_range",
                          {"query": query, "start": start, "end": end, "step": step})


def _sync_get_top_flapping_links(limit: int = 10,
                                  since_hours: int = 24) -> List[Dict[str, Any]]:
    if not _prom_url():
        return [{"error": "PROMETHEUS_URL not configured."}]

    try:
        flap_q = _flap_query_template().format(limit=limit, since_hours=since_hours)
        results = _sync_instant_query(flap_q)
    except Exception as exc:
        return [{"error": f"Prometheus flap query failed: {exc}"}]

    # Build last-flap timestamp map
    last_flap_map: Dict[tuple, float] = {}
    try:
        last_results = _sync_instant_query(_last_flap_query())
        for item in last_results:
            metric = item.get("metric", {})
            val = item.get("value", [None, None])[1]
            if val is not None:
                key = (metric.get("device", "unknown"), metric.get("interface", "unknown"))
                last_flap_map[key] = float(val)
    except Exception:
        pass

    flaps: List[Dict[str, Any]] = []
    for item in results:
        metric = item.get("metric", {})
        val = item.get("value", [None, None])[1]
        device = metric.get("device", "unknown")
        interface = metric.get("interface", "unknown")
        last_ts = last_flap_map.get((device, interface))
        flaps.append({
            "device": device,
            "interface": interface,
            "flap_count": float(val) if val is not None else 0.0,
            "last_flap": (
                datetime.fromtimestamp(last_ts, tz=timezone.utc).isoformat()
                if last_ts else None
            ),
            "description": metric.get("description", ""),
            "region": metric.get("region", ""),
            "source": "prometheus",
        })

    flaps.sort(key=lambda x: x.get("flap_count", 0), reverse=True)
    return flaps[:limit]


def _sync_get_metric_labels(metric_name: str) -> Dict[str, Any]:
    return _sync_prom_get("/api/v1/labels", {"match[]": metric_name})


def _sync_get_metric_series(metric_name: str, limit: int = 50) -> Dict[str, Any]:
    result = _sync_prom_get("/api/v1/series", {"match[]": metric_name})
    if "data" in result and isinstance(result["data"], list):
        result["data"] = result["data"][:limit]
    return result


# ---------------------------------------------------------------------------
# Async wrappers
# ---------------------------------------------------------------------------

async def _run(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(fn, *args, **kwargs))


async def prometheus_query(query: str) -> Dict[str, Any]:
    return await _run(_sync_prometheus_query, query)

async def prometheus_range_query(query: str, start: str, end: str,
                                  step: str = "60s") -> Dict[str, Any]:
    return await _run(_sync_prometheus_range_query, query, start, end, step)

async def get_top_flapping_links(limit: int = 10,
                                  since_hours: int = 24) -> List[Dict[str, Any]]:
    return await _run(_sync_get_top_flapping_links, limit, since_hours)

async def get_metric_labels(metric_name: str) -> Dict[str, Any]:
    return await _run(_sync_get_metric_labels, metric_name)

async def get_metric_series(metric_name: str, limit: int = 50) -> Dict[str, Any]:
    return await _run(_sync_get_metric_series, metric_name, limit)


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOLS: Dict[str, Any] = {
    "prometheus_query": prometheus_query,
    "prometheus_range_query": prometheus_range_query,
    "get_top_flapping_links": get_top_flapping_links,
    "get_metric_labels": get_metric_labels,
    "get_metric_series": get_metric_series,
}

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "prometheus_prometheus_query",
            "description": "Execute a raw PromQL instant query against Prometheus.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "PromQL expression string."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "prometheus_prometheus_range_query",
            "description": "Execute a PromQL range query against Prometheus.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "PromQL expression string."},
                    "start": {"type": "string", "description": "Start time (RFC3339 or Unix timestamp)."},
                    "end": {"type": "string", "description": "End time (RFC3339 or Unix timestamp)."},
                    "step": {"type": "string", "description": "Resolution step, e.g. '60s', '5m'."},
                },
                "required": ["query", "start", "end"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "prometheus_get_top_flapping_links",
            "description": "Return the top N most-flapping links from Prometheus over the last N hours.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max links to return (default 10)."},
                    "since_hours": {"type": "integer", "description": "Look-back window in hours (default 24)."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "prometheus_get_metric_labels",
            "description": "Return all label names for a given Prometheus metric.",
            "parameters": {
                "type": "object",
                "properties": {
                    "metric_name": {"type": "string", "description": "Prometheus metric name."},
                },
                "required": ["metric_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "prometheus_get_metric_series",
            "description": "Return all active time series for a Prometheus metric.",
            "parameters": {
                "type": "object",
                "properties": {
                    "metric_name": {"type": "string", "description": "Prometheus metric name."},
                    "limit": {"type": "integer", "description": "Max series to return (default 50)."},
                },
                "required": ["metric_name"],
            },
        },
    },
]
