from datetime import datetime, timezone
from typing import Any, Dict, List

import httpx

from config import (
    PROMETHEUS_FLAP_QUERY,
    PROMETHEUS_LAST_FLAP_QUERY,
    PROMETHEUS_URL,
)


def get_top_flapping_links(limit: int, since_hours: int) -> List[Dict[str, Any]]:
    """
    Return top flapping links from Prometheus when configured, otherwise mock data.
    """
    if PROMETHEUS_URL:
        try:
            return _get_flaps_from_prometheus(limit, since_hours)
        except Exception as exc:
            results = _mock_flaps(limit)
            if results:
                results[0]["warning"] = f"Prometheus query failed: {exc}"
            return results
    return _mock_flaps(limit)


def _prom_query(query: str) -> list[dict[str, Any]]:
    base = PROMETHEUS_URL.rstrip("/")
    url = f"{base}/api/v1/query"
    resp = httpx.get(url, params={"query": query}, timeout=10)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != "success":
        raise RuntimeError("Prometheus query failed.")
    return payload.get("data", {}).get("result", [])


def _get_flaps_from_prometheus(limit: int, since_hours: int) -> List[Dict[str, Any]]:
    if not PROMETHEUS_FLAP_QUERY.strip():
        raise RuntimeError("PROMETHEUS_FLAP_QUERY is empty.")
    query = PROMETHEUS_FLAP_QUERY.format(limit=limit, since_hours=since_hours)
    results = _prom_query(query)
    last_flap_map: Dict[tuple[str, str], float] = {}
    try:
        last_results = _prom_query(PROMETHEUS_LAST_FLAP_QUERY)
        for item in last_results:
            metric = item.get("metric", {})
            value = item.get("value", [None, None])[1]
            device = metric.get("device", "unknown")
            interface = metric.get("interface", "unknown")
            if value is not None:
                last_flap_map[(device, interface)] = float(value)
    except Exception:
        last_flap_map = {}

    flaps: List[Dict[str, Any]] = []
    for item in results:
        metric = item.get("metric", {})
        value = item.get("value", [None, None])[1]
        device = metric.get("device", "unknown")
        interface = metric.get("interface", "unknown")
        description = metric.get("description", "")
        region = metric.get("region", "")
        flap_count = float(value) if value is not None else 0.0
        last_flap_ts = last_flap_map.get((device, interface))
        last_flap = None
        if last_flap_ts:
            last_flap = datetime.fromtimestamp(last_flap_ts, tz=timezone.utc).isoformat()
        flaps.append(
            {
                "device": device,
                "interface": interface,
                "flap_count": flap_count,
                "last_flap": last_flap,
                "description": description,
                "region": region,
                "source": "prometheus",
            }
        )
    flaps.sort(key=lambda item: item.get("flap_count", 0), reverse=True)
    return flaps[:limit]


def _mock_flaps(limit: int) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for i in range(limit):
        results.append(
            {
                "device": f"iosxr-pe-{(i % 3) + 1}",
                "interface": f"TenGigE0/0/0/{i % 4}",
                "flap_count": (limit - i) * 3,
                "last_flap": f"2026-01-10T14:{i:02d}:00Z",
                "description": "Mock IOS XR interface for dev",
                "region": "EU",
                "source": "mock",
            }
        )
    return results
