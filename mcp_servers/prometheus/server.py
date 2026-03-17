"""
Prometheus MCP Server

Exposes Prometheus query tools and link-flap analysis over MCP/SSE.

Start:  python -m mcp_servers.prometheus.server
Port:   MCP_PROMETHEUS_PORT (default 8105)
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

logger = logging.getLogger("netcopilot.mcp.prometheus")

MCP_PORT = int(os.environ.get("MCP_PROMETHEUS_PORT", 8105))
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "")
PROMETHEUS_FLAP_QUERY = os.environ.get(
    "PROMETHEUS_FLAP_QUERY",
    "topk({limit}, netcopilot_link_flaps_total)",
)
PROMETHEUS_LAST_FLAP_QUERY = os.environ.get(
    "PROMETHEUS_LAST_FLAP_QUERY",
    "netcopilot_link_last_flap_timestamp_seconds",
)

mcp = FastMCP("prometheus", port=MCP_PORT)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _prom_get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if not PROMETHEUS_URL:
        return {"error": "PROMETHEUS_URL not configured."}
    url = f"{PROMETHEUS_URL.rstrip('/')}{path}"
    try:
        resp = httpx.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        return {"error": str(exc)}


def _instant_query(query: str) -> List[Dict[str, Any]]:
    """Execute a Prometheus instant query; return result list or raise."""
    result = _prom_get("/api/v1/query", {"query": query})
    if "error" in result:
        raise RuntimeError(result["error"])
    if result.get("status") != "success":
        raise RuntimeError("Prometheus returned non-success status.")
    return result.get("data", {}).get("result", [])


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def prometheus_query(query: str) -> Dict[str, Any]:
    """
    Execute a raw PromQL instant query against Prometheus.

    Args:
        query: PromQL expression string.
    Returns:
        Raw Prometheus API response (status, data, result).
    """
    return _prom_get("/api/v1/query", {"query": query})


@mcp.tool()
def prometheus_range_query(
    query: str,
    start: str,
    end: str,
    step: str = "60s",
) -> Dict[str, Any]:
    """
    Execute a PromQL range query against Prometheus.

    Args:
        query: PromQL expression string.
        start: Start time as RFC3339 or Unix timestamp.
        end: End time as RFC3339 or Unix timestamp.
        step: Resolution step, e.g. '60s', '5m'.
    """
    return _prom_get(
        "/api/v1/query_range",
        {"query": query, "start": start, "end": end, "step": step},
    )


@mcp.tool()
def get_top_flapping_links(
    limit: int = 10,
    since_hours: int = 24,
) -> List[Dict[str, Any]]:
    """
    Return the top N most-flapping links from Prometheus.

    Args:
        limit: Maximum number of links to return (default 10).
        since_hours: Look-back window in hours (default 24).
    Returns:
        List of dicts with device, interface, flap_count, last_flap, etc.
    """
    if not PROMETHEUS_URL:
        return [{"error": "PROMETHEUS_URL not configured."}]

    try:
        flap_query = PROMETHEUS_FLAP_QUERY.format(
            limit=limit, since_hours=since_hours
        )
        results = _instant_query(flap_query)
    except Exception as exc:
        return [{"error": f"Prometheus flap query failed: {exc}"}]

    # Build last-flap timestamp map
    last_flap_map: Dict[tuple, float] = {}
    try:
        last_results = _instant_query(PROMETHEUS_LAST_FLAP_QUERY)
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
        flaps.append(
            {
                "device": device,
                "interface": interface,
                "flap_count": float(val) if val is not None else 0.0,
                "last_flap": (
                    datetime.fromtimestamp(last_ts, tz=timezone.utc).isoformat()
                    if last_ts
                    else None
                ),
                "description": metric.get("description", ""),
                "region": metric.get("region", ""),
                "source": "prometheus",
            }
        )

    flaps.sort(key=lambda x: x.get("flap_count", 0), reverse=True)
    return flaps[:limit]


@mcp.tool()
def get_metric_labels(metric_name: str) -> Dict[str, Any]:
    """
    Return all label names for a given Prometheus metric.

    Args:
        metric_name: Prometheus metric name, e.g. 'netcopilot_link_flaps_total'.
    """
    return _prom_get("/api/v1/labels", {"match[]": metric_name})


@mcp.tool()
def get_metric_series(metric_name: str, limit: int = 50) -> Dict[str, Any]:
    """
    Return all active time series for a Prometheus metric.

    Args:
        metric_name: Prometheus metric name.
        limit: Maximum number of series to return (default 50).
    """
    result = _prom_get("/api/v1/series", {"match[]": metric_name})
    if "data" in result and isinstance(result["data"], list):
        result["data"] = result["data"][:limit]
    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logger.info("Starting Prometheus MCP server on port %d", MCP_PORT)
    mcp.run(transport="sse")
