"""
Tolerant text parsers for vendors without structured (NETCONF/XML) output.

Each parser extracts only the vendor-neutral fields playbooks and the LLM rely
on (admin_status, oper_status, state, errors, ...). They are additive: raw
output is always kept, and a line that doesn't match simply yields no field —
never a wrong one. Output formats differ between software releases, so treat
a missing field as "unknown", not as "zero" or "down".
"""

from __future__ import annotations

import re
from typing import Any


def _first(pattern: str, text: str, flags: int = re.MULTILINE | re.IGNORECASE) -> str | None:
    m = re.search(pattern, text or "", flags)
    return m.group(1).strip() if m else None


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _updown(value: str | None) -> str | None:
    if value is None:
        return None
    v = value.lower()
    if v.startswith("up"):
        return "up"
    if v.startswith("down"):
        return "down"
    return v


def _set(result: dict, key: str, value: Any) -> None:
    if value is not None and value != "":
        result[key] = value


# ---------------------------------------------------------------------------
# Nokia SR OS (classic CLI)
# ---------------------------------------------------------------------------

def parse_nokia_port_detail(text: str) -> dict:
    """'show port <port>' — two-column 'Key : value' layout."""
    result: dict[str, Any] = {}
    _set(result, "admin_status", _updown(_first(r"^\s*Admin State\s*:\s*(\S+)", text)))
    _set(result, "oper_status", _updown(_first(r"^\s*Oper State\s*:\s*(\S+)", text)))
    _set(result, "last_flapped", _first(r"Last State Change\s*:\s*(.+?)(?:\s{2,}|$)", text))
    _set(result, "description", _first(r"^\s*Description\s*:\s*(.+?)\s*$", text))
    _set(result, "speed", _first(r"Oper Speed\s*:\s*(.+?)(?:\s{2,}|$)", text))
    return result


def parse_nokia_bgp_neighbor(text: str) -> dict:
    """'show router bgp neighbor <ip>'."""
    result: dict[str, Any] = {}
    _set(result, "state", _first(r"^\s*State\s*:\s*(\w+)", text))
    _set(result, "last_state", _first(r"Last State\s*:\s*(\w+)", text))
    _set(result, "last_event", _first(r"Last Event\s*:\s*(.+?)(?:\s{2,}|$)", text))
    _set(result, "last_error", _first(r"Last Error\s*:\s*(.+?)(?:\s{2,}|$)", text))
    _set(result, "peer_as", _first(r"Peer AS\s*:\s*(\d+)", text))
    return result


# ---------------------------------------------------------------------------
# Aruba AOS-CX
# ---------------------------------------------------------------------------

def parse_aruba_interface(text: str) -> dict:
    """'show interface <if>' (AOS-CX 10.x)."""
    result: dict[str, Any] = {}
    _set(result, "oper_status", _updown(_first(r"^\s*(?:Interface|Aggregate)\s+\S+\s+is\s+(\w+)", text)))
    _set(result, "admin_status", _updown(_first(r"^\s*Admin\s*state\s*is\s*(\w+)", text)))
    _set(result, "carrier_transitions", _int(_first(r"Link\s*transitions\s*:\s*(\d+)", text)))
    _set(result, "description", _first(r"^\s*Description\s*:\s*(.+?)\s*$", text))
    _set(result, "mtu", _int(_first(r"^\s*MTU\s*:?\s*(\d+)", text)))
    # Statistics table: "Errors   <rx>   <tx>   <total>" and "CRC/FCS  <rx> ..."
    errors = re.search(r"^\s*Errors\s+(\d+)\s+(\d+)", text or "", re.MULTILINE)
    if errors:
        result["input_errors"] = int(errors.group(1))
        result["output_errors"] = int(errors.group(2))
    _set(result, "crc_errors", _int(_first(r"^\s*CRC/FCS\s+(\d+)", text)))
    # Older layout: "<n> input errors" / "<n> CRC/FCS"
    if "input_errors" not in result:
        _set(result, "input_errors", _int(_first(r"(\d+)\s+(?:input\s+)?errors", text)))
    return result


def parse_aruba_bgp_summary(text: str) -> list[dict]:
    """'show bgp all-vrfs all summary' neighbor rows."""
    peers = []
    for m in re.finditer(
        r"^\s+(\d{1,3}(?:\.\d{1,3}){3}|[0-9a-fA-F:]+:[0-9a-fA-F:]*)\s+(\d+)\s+\d+\s+\d+\s+\S+\s+(\w+)\s+(\w+)",
        text or "", re.MULTILINE,
    ):
        peers.append({"address": m.group(1), "as": m.group(2), "state": m.group(3), "admin_status": m.group(4)})
    return peers
