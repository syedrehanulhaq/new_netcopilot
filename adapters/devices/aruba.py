"""
Aruba Device Adapter — direct async wrapper for NetCopilot.

Supports Aruba AOS-CX (6300/6400/8400 series) via Netmiko SSH.
Legacy AOS-Switch (ProCurve) is also supported via the platform field in devices.yaml.

Each public function runs a sync Netmiko SSH call inside an executor,
participates in the DevicePool circuit breaker, and resolves credentials
via credentials.get_secret().

Export:
    TOOLS        dict[str, callable]   base-name → async function
    TOOL_SCHEMAS list[dict]            OpenAI function-call schemas (aruba_ prefixed)

devices.yaml vendor field : aruba
devices.yaml platform options:
  aoscx     → Aruba AOS-CX  (default, Netmiko device_type: aruba_aoscx)
  aosswitch → Aruba AOS-Switch / ProCurve (Netmiko device_type: aruba_osswitch)
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Optional

from credentials import get_secret
from adapters.devices.registry import resolve_host, get_device_info
from adapters.devices.pool import get_pool, CircuitOpenError

logger = logging.getLogger("netcopilot.adapters.aruba")

# ---------------------------------------------------------------------------
# SSH connection cache — reuse sessions instead of reconnecting each call
# ---------------------------------------------------------------------------

_SSH_TTL = 90.0
_SSH_MAX_CACHED = 20

_conn_cache: dict[str, tuple[Any, float]] = {}
_conn_lock = threading.Lock()


def _get_cached_ssh(device_name: str):
    with _conn_lock:
        entry = _conn_cache.get(device_name)
        if entry is None:
            return None
        conn, last_used = entry
        if time.monotonic() - last_used > _SSH_TTL:
            _conn_cache.pop(device_name, None)
            try:
                conn.disconnect()
            except Exception:
                pass
            return None
        _conn_cache[device_name] = (conn, time.monotonic())
        return conn


def _put_cached_ssh(device_name: str, conn) -> None:
    with _conn_lock:
        if len(_conn_cache) >= _SSH_MAX_CACHED and device_name not in _conn_cache:
            oldest = min(_conn_cache, key=lambda k: _conn_cache[k][1])
            old_conn, _ = _conn_cache.pop(oldest)
            try:
                old_conn.disconnect()
            except Exception:
                pass
        _conn_cache[device_name] = (conn, time.monotonic())


def _evict_ssh(device_name: str) -> None:
    with _conn_lock:
        entry = _conn_cache.pop(device_name, None)
    if entry:
        try:
            entry[0].disconnect()
        except Exception:
            pass


def _device_type(device_name: str) -> str:
    """Return Netmiko device_type based on platform field in devices.yaml."""
    try:
        info = get_device_info(device_name)
        platform = (info.get("platform") or "aoscx").lower()
    except Exception:
        platform = "aoscx"

    if platform in ("aosswitch", "procurve", "legacy"):
        return "aruba_osswitch"
    return "aruba_aoscx"


@contextmanager
def _ssh(device_name: str):
    """Yield a Netmiko SSH session, reusing a cached connection when available."""
    from netmiko import ConnectHandler

    conn = _get_cached_ssh(device_name)
    fresh = conn is None

    if fresh:
        host, port = resolve_host(device_name)
        user = get_secret("ARUBA_USER", "")
        passwd = get_secret("ARUBA_PASS", "")
        if not user or not passwd:
            raise RuntimeError("ARUBA_USER or ARUBA_PASS not configured.")

        params: dict[str, Any] = {
            "device_type": _device_type(device_name),
            "host": host,
            "username": user,
            "password": passwd,
            "fast_cli": False,
            "conn_timeout": int(get_secret("SSH_TIMEOUT", "30")),
        }
        if port:
            params["port"] = port
        conn = ConnectHandler(**params)
        logger.debug("SSH new connection to %s", device_name)
    else:
        logger.debug("SSH reusing cached connection to %s", device_name)

    try:
        yield conn
        _put_cached_ssh(device_name, conn)
    except Exception:
        _evict_ssh(device_name)
        raise


def _cli(device: str, command: str) -> str:
    """Run CLI command via SSH, retrying once with a fresh connection on failure."""
    try:
        with _ssh(device) as conn:
            return conn.send_command(command)
    except Exception:
        logger.debug("SSH retry with fresh connection to %s", device)
        with _ssh(device) as conn:
            return conn.send_command(command)


def _run_cli(device: str, command: str) -> dict[str, Any]:
    """Run CLI command and return structured dict with output."""
    try:
        output = _cli(device, command)
        return {"device": device, "command": command, "output": output, "raw_output": output}
    except Exception as exc:
        return {"device": device, "command": command, "error": str(exc)}


_EXECUTOR_TIMEOUT = 60.0


def _scrub_error(msg: str) -> str:
    msg = re.sub(r"(?i)(password|passwd|secret|token)\s*[=:]\s*\S+", r"\1=***", msg)
    msg = re.sub(r"Authentication failed.*", "Authentication failed.", msg)
    return msg


def _err(device: str, exc: Exception, **extra) -> dict:
    return {"device": device, "error": _scrub_error(str(exc)), **extra}


async def _run_tool(device: str, fn, *fn_args, **err_extra) -> dict:
    """Run sync fn(*fn_args) in executor with circuit breaker, timeout, and error scrubbing."""
    _pool = get_pool()
    try:
        async with _pool.acquire(device):
            loop = asyncio.get_event_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, fn, *fn_args),
                timeout=_EXECUTOR_TIMEOUT,
            )
            _pool.record_success(device)
            return result
    except asyncio.TimeoutError:
        msg = f"Device '{device}' timed out after {_EXECUTOR_TIMEOUT:.0f}s."
        _pool.record_failure(device, msg)
        return {"error": msg, "device": device, **err_extra}
    except CircuitOpenError as exc:
        return {"error": str(exc), "device": device, **err_extra}
    except Exception as exc:
        _pool.record_failure(device, _scrub_error(str(exc)))
        return _err(device, exc, **err_extra)


# ---------------------------------------------------------------------------
# Synchronous implementations (executor targets)
# ---------------------------------------------------------------------------

def _sync_get_interface_health(device: str, interface: str) -> dict:
    detail = _run_cli(device, f"show interface {interface}")
    stats  = _run_cli(device, f"show interface {interface} statistics")
    return {
        "device": device,
        "interface": interface,
        "detail": detail,
        "statistics": stats,
    }


def _sync_get_interface_status(device: str) -> dict:
    return _run_cli(device, "show interface brief")


def _sync_get_interface_counters(device: str, interface: str) -> dict:
    return _run_cli(device, f"show interface {interface} statistics")


def _sync_get_lldp_neighbor(device: str, interface: str) -> dict:
    result = _run_cli(device, f"show lldp neighbor-info {interface}")
    if "error" in result:
        return result
    neighbor_device = None
    neighbor_interface = None
    for line in result.get("output", "").splitlines():
        line_lower = line.lower()
        if "system name" in line_lower or "chassis id" in line_lower:
            parts = line.split(":", 1)
            if len(parts) == 2:
                neighbor_device = parts[1].strip()
        if "port id" in line_lower:
            parts = line.split(":", 1)
            if len(parts) == 2:
                neighbor_interface = parts[1].strip()
    return {
        **result,
        "neighbor_device": neighbor_device,
        "neighbor_interface": neighbor_interface,
    }


def _sync_get_bgp_neighbor_status(device: str, neighbor: str) -> dict:
    return _run_cli(device, f"show bgp all-vrf all neighbors {neighbor}")


def _sync_get_bgp_summary(device: str) -> dict:
    return _run_cli(device, "show bgp all-vrf all summary")


def _sync_get_ospf_neighbors(device: str) -> dict:
    return _run_cli(device, "show ip ospf neighbors")


def _sync_get_cpu_memory(device: str) -> dict:
    resources = _run_cli(device, "show system resource-utilization")
    return {"device": device, "resources": resources}


def _sync_get_chassis_health(device: str) -> dict:
    environment = _run_cli(device, "show environment all")
    inventory   = _run_cli(device, "show system")
    return {"device": device, "environment": environment, "system": inventory}


def _sync_get_route_lookup(device: str, prefix: str) -> dict:
    return _run_cli(device, f"show ip route {prefix}")


def _sync_get_bgp_route(device: str, prefix: str) -> dict:
    return _run_cli(device, f"show bgp all-vrf all {prefix}")


def _sync_get_acl_hits(device: str, acl_name: str) -> dict:
    return _run_cli(device, f"show access-list {acl_name} statistics")


def _sync_get_mtu_info(device: str, interface: str) -> dict:
    result = _run_cli(device, f"show interface {interface}")
    if "error" in result:
        return result
    mtu = None
    for line in result.get("output", "").splitlines():
        if "mtu" in line.lower():
            m = re.search(r"(\d+)", line)
            if m:
                mtu = int(m.group(1))
                break
    return {**result, "mtu": mtu, "interface": interface}


def _sync_get_ping(device: str, destination: str, count: int = 5) -> dict:
    return _run_cli(device, f"ping {destination} repetitions {count}")


def _sync_get_traceroute(device: str, destination: str) -> dict:
    return _run_cli(device, f"traceroute {destination}")


def _sync_get_vlan_info(device: str, vlan_id: Optional[str] = None) -> dict:
    if vlan_id:
        return _run_cli(device, f"show vlan {vlan_id}")
    return _run_cli(device, "show vlan")


def _sync_run_command(device: str, command: str) -> dict:
    allowed = ("show ", "ping ", "traceroute ")
    if not any(command.strip().lower().startswith(p) for p in allowed):
        return {
            "device": device, "command": command,
            "error": f"Command rejected. Only read-only commands allowed: {allowed}",
        }
    return _run_cli(device, command)


# ---------------------------------------------------------------------------
# Async public API — each wraps a sync impl inside the circuit breaker
# ---------------------------------------------------------------------------

async def get_interface_health(device: str, interface: str) -> dict:
    """Get admin/oper state, speed, duplex, and error counters for an Aruba interface."""
    return await _run_tool(device, _sync_get_interface_health, device, interface, interface=interface)


async def get_interface_status(device: str) -> dict:
    """List all interfaces on an Aruba device with their admin/oper state (brief)."""
    return await _run_tool(device, _sync_get_interface_status, device)


async def get_interface_counters(device: str, interface: str) -> dict:
    """Get traffic counters and error stats for an Aruba interface."""
    return await _run_tool(device, _sync_get_interface_counters, device, interface, interface=interface)


async def get_lldp_neighbor(device: str, interface: str) -> dict:
    """Return the LLDP neighbor seen on an Aruba interface."""
    return await _run_tool(device, _sync_get_lldp_neighbor, device, interface, interface=interface)


async def get_bgp_neighbor_status(device: str, neighbor: str) -> dict:
    """Get BGP session state for a specific peer on an Aruba device."""
    return await _run_tool(device, _sync_get_bgp_neighbor_status, device, neighbor, neighbor=neighbor)


async def get_bgp_summary(device: str) -> dict:
    """Get BGP summary (all peers, all VRFs) from an Aruba device."""
    return await _run_tool(device, _sync_get_bgp_summary, device)


async def get_ospf_neighbors(device: str) -> dict:
    """Get OSPF neighbor adjacencies from an Aruba device."""
    return await _run_tool(device, _sync_get_ospf_neighbors, device)


async def get_cpu_memory(device: str) -> dict:
    """Get CPU and memory utilisation from an Aruba device."""
    return await _run_tool(device, _sync_get_cpu_memory, device)


async def get_chassis_health(device: str) -> dict:
    """Get environment (fans, PSUs, temperature) and system info from an Aruba device."""
    return await _run_tool(device, _sync_get_chassis_health, device)


async def get_route_lookup(device: str, prefix: str) -> dict:
    """Look up a specific prefix in the Aruba routing table."""
    return await _run_tool(device, _sync_get_route_lookup, device, prefix, prefix=prefix)


async def get_bgp_route(device: str, prefix: str) -> dict:
    """Get BGP path attributes for a specific prefix on an Aruba device."""
    return await _run_tool(device, _sync_get_bgp_route, device, prefix, prefix=prefix)


async def get_acl_hits(device: str, acl_name: str) -> dict:
    """Get hit counts for a named ACL on an Aruba device."""
    return await _run_tool(device, _sync_get_acl_hits, device, acl_name, acl_name=acl_name)


async def get_mtu_info(device: str, interface: str) -> dict:
    """Get MTU configuration for an Aruba interface."""
    return await _run_tool(device, _sync_get_mtu_info, device, interface, interface=interface)


async def get_ping(device: str, destination: str, count: int = 5) -> dict:
    """Run a ping from an Aruba device to a destination."""
    return await _run_tool(device, _sync_get_ping, device, destination, count, destination=destination)


async def get_traceroute(device: str, destination: str) -> dict:
    """Run a traceroute from an Aruba device to a destination."""
    return await _run_tool(device, _sync_get_traceroute, device, destination, destination=destination)


async def get_vlan_info(device: str, vlan_id: Optional[str] = None) -> dict:
    """Get VLAN configuration and membership. Optionally filter by VLAN ID."""
    return await _run_tool(device, _sync_get_vlan_info, device, vlan_id)


async def run_command(device: str, command: str) -> dict:
    """Run any read-only CLI command on an Aruba device (show/ping/traceroute only)."""
    return await _run_tool(device, _sync_run_command, device, command, command=command)


# ---------------------------------------------------------------------------
# Tool registry — base-name → async function
# ---------------------------------------------------------------------------

TOOLS: dict[str, Callable] = {
    "get_interface_health":   get_interface_health,
    "get_interface_status":   get_interface_status,
    "get_interface_counters": get_interface_counters,
    "get_lldp_neighbor":      get_lldp_neighbor,
    "get_bgp_neighbor_status":get_bgp_neighbor_status,
    "get_bgp_summary":        get_bgp_summary,
    "get_ospf_neighbors":     get_ospf_neighbors,
    "get_cpu_memory":         get_cpu_memory,
    "get_chassis_health":     get_chassis_health,
    "get_route_lookup":       get_route_lookup,
    "get_bgp_route":          get_bgp_route,
    "get_acl_hits":           get_acl_hits,
    "get_mtu_info":           get_mtu_info,
    "get_ping":               get_ping,
    "get_traceroute":         get_traceroute,
    "get_vlan_info":          get_vlan_info,
    "run_command":            run_command,
}

# ---------------------------------------------------------------------------
# OpenAI-format tool schemas — consumed by ToolDispatcher.get_all_tools()
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "aruba_get_interface_health",
            "description": "Get admin/oper state, speed, duplex, and error counters for an Aruba AOS-CX interface.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device":    {"type": "string", "description": "Device name from inventory"},
                    "interface": {"type": "string", "description": "Interface name, e.g. 1/1/1 or 1/1/uplink"},
                },
                "required": ["device", "interface"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "aruba_get_interface_status",
            "description": "List all interfaces on an Aruba device with admin/oper state (brief view).",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                },
                "required": ["device"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "aruba_get_interface_counters",
            "description": "Get traffic counters and error statistics for an Aruba interface.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device":    {"type": "string"},
                    "interface": {"type": "string"},
                },
                "required": ["device", "interface"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "aruba_get_lldp_neighbor",
            "description": "Return the LLDP neighbor seen on an Aruba interface.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device":    {"type": "string"},
                    "interface": {"type": "string"},
                },
                "required": ["device", "interface"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "aruba_get_bgp_neighbor_status",
            "description": "Get BGP session state for a specific peer on an Aruba device.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device":   {"type": "string"},
                    "neighbor": {"type": "string", "description": "BGP peer IP address"},
                },
                "required": ["device", "neighbor"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "aruba_get_bgp_summary",
            "description": "Get BGP summary (all peers, all VRFs) from an Aruba device.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                },
                "required": ["device"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "aruba_get_ospf_neighbors",
            "description": "Get OSPF neighbor adjacencies from an Aruba device.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                },
                "required": ["device"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "aruba_get_cpu_memory",
            "description": "Get CPU and memory utilisation from an Aruba device.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                },
                "required": ["device"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "aruba_get_chassis_health",
            "description": "Get environment health (fans, PSUs, temperature) and system info from an Aruba device.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                },
                "required": ["device"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "aruba_get_route_lookup",
            "description": "Look up a specific prefix in the Aruba routing table.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "prefix": {"type": "string"},
                },
                "required": ["device", "prefix"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "aruba_get_bgp_route",
            "description": "Get BGP path attributes for a specific prefix on an Aruba device.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "prefix": {"type": "string"},
                },
                "required": ["device", "prefix"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "aruba_get_acl_hits",
            "description": "Get hit counts for a named ACL on an Aruba device.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device":   {"type": "string"},
                    "acl_name": {"type": "string"},
                },
                "required": ["device", "acl_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "aruba_get_mtu_info",
            "description": "Get MTU configuration for an Aruba interface.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device":    {"type": "string"},
                    "interface": {"type": "string"},
                },
                "required": ["device", "interface"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "aruba_get_ping",
            "description": "Run a ping from an Aruba device to a destination.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device":      {"type": "string"},
                    "destination": {"type": "string"},
                    "count":       {"type": "integer", "default": 5},
                },
                "required": ["device", "destination"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "aruba_get_traceroute",
            "description": "Run a traceroute from an Aruba device to a destination.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device":      {"type": "string"},
                    "destination": {"type": "string"},
                },
                "required": ["device", "destination"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "aruba_get_vlan_info",
            "description": "Get VLAN configuration and port membership on an Aruba switch. Optionally filter by VLAN ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device":  {"type": "string"},
                    "vlan_id": {"type": "string", "description": "VLAN ID to filter (optional — omit for all VLANs)"},
                },
                "required": ["device"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "aruba_run_command",
            "description": "Run any read-only CLI command on an Aruba device (show/ping/traceroute only).",
            "parameters": {
                "type": "object",
                "properties": {
                    "device":  {"type": "string"},
                    "command": {"type": "string", "description": "CLI command to run"},
                },
                "required": ["device", "command"],
            },
        },
    },
]
