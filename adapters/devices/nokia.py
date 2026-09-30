"""
Nokia SR OS Device Adapter — direct async wrapper for NetCopilot.

Each public function runs a sync Netmiko SSH call inside an executor,
participates in the DevicePool circuit breaker, and resolves credentials
via secrets.get_secret().

Export:
    TOOLS        dict[str, callable]   base-name → async function
    TOOL_SCHEMAS list[dict]            OpenAI function-call schemas (nokia_ prefixed)
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable, Optional

from credentials import get_secret
from adapters.devices.registry import resolve_host
from adapters.devices.parsers import parse_nokia_bgp_neighbor, parse_nokia_port_detail
from adapters.devices.pool import get_pool, CircuitOpenError
from adapters.devices.safety import UnsafeInputError, reject_control_chars, validate_cli_command
from adapters.devices.ssh_cache import SSHSessionCache
from adapters.devices.ssh_options import check_reachable, netmiko_host_key_params

logger = logging.getLogger("netcopilot.adapters.nokia")

# ---------------------------------------------------------------------------
# SSH sessions — reused across tool calls, each checked out exclusively
# ---------------------------------------------------------------------------

_SESSIONS = SSHSessionCache("nokia", target=resolve_host)


def _connect(device_name: str):
    from netmiko import ConnectHandler

    host, port = resolve_host(device_name)
    user = get_secret("NOKIA_USER", "")
    passwd = get_secret("NOKIA_PASS", "")
    if not user or not passwd:
        raise RuntimeError("NOKIA_USER or NOKIA_PASS not configured.")

    params: dict[str, Any] = {
        "device_type": "nokia_sros",
        "host": host,
        "username": user,
        "password": passwd,
        "fast_cli": True,
        "conn_timeout": int(get_secret("NETCONF_TIMEOUT", "30")),
    }
    if port:
        params["port"] = port
    check_reachable(host, port or 22)
    params.update(netmiko_host_key_params())
    return ConnectHandler(**params)


def _ssh(device_name: str, fresh: bool = False):
    """Yield an exclusive Netmiko session; it is dropped if the block raises."""
    return _SESSIONS.session(device_name, lambda: _connect(device_name), fresh=fresh)


def _cli(device: str, command: str) -> str:
    """Run a CLI command via SSH; retry once with a fresh connection if a reused session failed."""
    reused = _SESSIONS.idle_count(device) > 0
    try:
        with _ssh(device) as conn:
            return conn.send_command(command)
    except Exception:
        if not reused:
            raise  # a brand-new connection failed: retrying would just double the wait
        logger.debug("SSH retry with fresh connection to %s", device)
        with _ssh(device, fresh=True) as conn:
            return conn.send_command(command)


def _run_cli(device: str, command: str) -> dict[str, Any]:
    """Run CLI command and return structured dict with output."""
    try:
        output = _cli(device, command)
        return {"device": device, "command": command, "output": output, "raw_output": output}
    except Exception as exc:
        return {"device": device, "command": command, "error": _scrub_error(str(exc))}


_EXECUTOR_TIMEOUT = 60.0


def _scrub_error(msg: str) -> str:
    msg = re.sub(r"(?i)(password|passwd|secret|token)\s*[=:]\s*\S+", r"\1=***", msg)
    msg = re.sub(r"Authentication failed.*", "Authentication failed.", msg)
    return msg


def _err(device: str, exc: Exception, **extra) -> dict:
    return {"device": device, "error": _scrub_error(str(exc)), **extra}


async def _run_tool(device: str, fn, *fn_args, **err_extra) -> dict:
    """Run sync fn(*fn_args) in executor with circuit breaker, timeout, and error scrubbing."""
    try:
        reject_control_chars(device, *fn_args)
    except UnsafeInputError as exc:
        return {"error": f"Rejected unsafe input: {exc}", "device": device, **err_extra}
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
    port = _run_cli(device, f"show port {interface}")
    rif = _run_cli(device, f"show router interface {interface}")
    return {
        "device": device, "interface": interface,
        **parse_nokia_port_detail(port.get("output", "")),
        "port": port, "router_interface": rif,
    }


def _sync_get_interface_status(device: str) -> dict:
    return _run_cli(device, "show port")


def _sync_get_interface_counters(device: str, interface: str) -> dict:
    return _run_cli(device, f"show port {interface} statistics")


def _sync_get_lldp_neighbor(device: str, interface: str) -> dict:
    result = _run_cli(device, "show system lldp neighbor")
    if "error" in result:
        return result
    neighbor_device = None
    neighbor_interface = None
    for line in result.get("output", "").splitlines():
        if interface in line:
            parts = re.split(r"\s+", line.strip())
            if len(parts) >= 3:
                neighbor_interface = parts[-1]
                neighbor_device = parts[-2]
                break
    return {
        **result,
        "neighbor_device": neighbor_device,
        "neighbor_interface": neighbor_interface,
    }


def _sync_get_bgp_neighbor_status(device: str, neighbor: str) -> dict:
    result = _run_cli(device, f"show router bgp neighbor {neighbor}")
    return {**result, "neighbor": neighbor, **parse_nokia_bgp_neighbor(result.get("output", ""))}


def _sync_get_bgp_summary(device: str) -> dict:
    return _run_cli(device, "show router bgp summary")


def _sync_get_ospf_neighbors(device: str) -> dict:
    return _run_cli(device, "show router ospf neighbor")


def _sync_get_isis_neighbors(device: str) -> dict:
    return _run_cli(device, "show router isis adjacency")


def _sync_get_isis_interface(device: str, interface: str) -> dict:
    return _run_cli(device, f"show router isis interface {interface}")


def _sync_get_cpu_memory(device: str) -> dict:
    cpu = _run_cli(device, "show system cpu")
    mem = _run_cli(device, "show system memory")
    return {"device": device, "cpu": cpu, "memory": mem}


def _sync_get_chassis_health(device: str) -> dict:
    chassis = _run_cli(device, "show chassis")
    cards = _run_cli(device, "show card")
    alarms = _run_cli(device, "show system alarms")
    return {"device": device, "chassis": chassis, "cards": cards, "alarms": alarms}


def _sync_get_route_lookup(device: str, prefix: str) -> dict:
    return _run_cli(device, f"show router route-table {prefix}")


def _sync_get_bgp_route(device: str, prefix: str) -> dict:
    return _run_cli(device, f"show router bgp routes {prefix}")


def _sync_get_acl_hits(device: str, acl_name: str) -> dict:
    return _run_cli(device, f"show filter ip {acl_name}")


def _sync_get_mtu_info(device: str, interface: str) -> dict:
    return _run_cli(device, f"show port {interface}")


def _sync_get_ping(device: str, destination: str, count: int = 5) -> dict:
    return _run_cli(device, f"ping {destination} count {count}")


def _sync_get_traceroute(device: str, destination: str) -> dict:
    return _run_cli(device, f"traceroute {destination}")


def _sync_run_command(device: str, command: str) -> dict:
    try:
        command = validate_cli_command("nokia", command)
    except UnsafeInputError as exc:
        return {"device": device, "command": command, "error": f"Command rejected: {exc}"}
    return _run_cli(device, command)


# ---------------------------------------------------------------------------
# Async public API — each wraps a sync impl inside the circuit breaker
# ---------------------------------------------------------------------------

async def get_interface_health(device: str, interface: str) -> dict:
    """Get admin/oper state, last change, and IP info for a Nokia port/interface."""
    return await _run_tool(device, _sync_get_interface_health, device, interface, interface=interface)


async def get_interface_status(device: str) -> dict:
    """List all ports on a Nokia device with their admin/oper state."""
    return await _run_tool(device, _sync_get_interface_status, device)


async def get_interface_counters(device: str, interface: str) -> dict:
    """Get traffic counters and error stats for a Nokia port."""
    return await _run_tool(device, _sync_get_interface_counters, device, interface, interface=interface)


async def get_lldp_neighbor(device: str, interface: str) -> dict:
    """Return the LLDP neighbor seen on a Nokia port."""
    return await _run_tool(device, _sync_get_lldp_neighbor, device, interface, interface=interface)


async def get_bgp_neighbor_status(device: str, neighbor: str) -> dict:
    """Get BGP session state for a specific peer on a Nokia device."""
    return await _run_tool(device, _sync_get_bgp_neighbor_status, device, neighbor, neighbor=neighbor)


async def get_bgp_summary(device: str) -> dict:
    """Get BGP summary (all peers) from a Nokia device."""
    return await _run_tool(device, _sync_get_bgp_summary, device)


async def get_ospf_neighbors(device: str) -> dict:
    """Get OSPF neighbor adjacencies from a Nokia device."""
    return await _run_tool(device, _sync_get_ospf_neighbors, device)


async def get_isis_neighbors(device: str) -> dict:
    """Get IS-IS adjacencies from a Nokia device."""
    return await _run_tool(device, _sync_get_isis_neighbors, device)


async def get_isis_interface(device: str, interface: str) -> dict:
    """Get IS-IS interface details for a specific Nokia interface."""
    return await _run_tool(device, _sync_get_isis_interface, device, interface, interface=interface)


async def get_cpu_memory(device: str) -> dict:
    """Get CPU and memory utilisation from a Nokia device."""
    return await _run_tool(device, _sync_get_cpu_memory, device)


async def get_chassis_health(device: str) -> dict:
    """Get chassis, card, and alarm state from a Nokia device."""
    return await _run_tool(device, _sync_get_chassis_health, device)


async def get_route_lookup(device: str, prefix: str) -> dict:
    """Look up a specific prefix in the Nokia routing table."""
    return await _run_tool(device, _sync_get_route_lookup, device, prefix, prefix=prefix)


async def get_bgp_route(device: str, prefix: str) -> dict:
    """Get BGP path attributes for a specific prefix on a Nokia device."""
    return await _run_tool(device, _sync_get_bgp_route, device, prefix, prefix=prefix)


async def get_acl_hits(device: str, acl_name: str) -> dict:
    """Get hit counts for a Nokia IP filter (ACL)."""
    return await _run_tool(device, _sync_get_acl_hits, device, acl_name, acl_name=acl_name)


async def get_mtu_info(device: str, interface: str) -> dict:
    """Get MTU configuration for a Nokia port."""
    return await _run_tool(device, _sync_get_mtu_info, device, interface, interface=interface)


async def get_ping(device: str, destination: str, count: int = 5) -> dict:
    """Run a ping from a Nokia device to a destination."""
    return await _run_tool(device, _sync_get_ping, device, destination, count, destination=destination)


async def get_traceroute(device: str, destination: str) -> dict:
    """Run a traceroute from a Nokia device to a destination."""
    return await _run_tool(device, _sync_get_traceroute, device, destination, destination=destination)


async def run_command(device: str, command: str) -> dict:
    """Run any read-only CLI command on a Nokia device."""
    return await _run_tool(device, _sync_run_command, device, command, command=command)


# ---------------------------------------------------------------------------
# Tool registry — base-name → async function
# ---------------------------------------------------------------------------

TOOLS: dict[str, Callable] = {
    "get_interface_health": get_interface_health,
    "get_interface_status": get_interface_status,
    "get_interface_counters": get_interface_counters,
    "get_lldp_neighbor": get_lldp_neighbor,
    "get_bgp_neighbor_status": get_bgp_neighbor_status,
    "get_bgp_summary": get_bgp_summary,
    "get_ospf_neighbors": get_ospf_neighbors,
    "get_isis_neighbors": get_isis_neighbors,
    "get_isis_interface": get_isis_interface,
    "get_cpu_memory": get_cpu_memory,
    "get_chassis_health": get_chassis_health,
    "get_route_lookup": get_route_lookup,
    "get_bgp_route": get_bgp_route,
    "get_acl_hits": get_acl_hits,
    "get_mtu_info": get_mtu_info,
    "get_ping": get_ping,
    "get_traceroute": get_traceroute,
    "run_command": run_command,
}

# ---------------------------------------------------------------------------
# OpenAI-format tool schemas — consumed by ToolDispatcher.get_all_tools()
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "nokia_get_interface_health",
            "description": "Get admin/oper state, last change, and IP info for a Nokia SR OS port/interface.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string", "description": "Device name from inventory"},
                    "interface": {"type": "string", "description": "Port/interface name, e.g. 1/1/1"},
                },
                "required": ["device", "interface"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "nokia_get_interface_status",
            "description": "List all ports on a Nokia SR OS device with their admin/oper state.",
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
            "name": "nokia_get_interface_counters",
            "description": "Get traffic counters and error stats for a Nokia SR OS port.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "interface": {"type": "string"},
                },
                "required": ["device", "interface"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "nokia_get_lldp_neighbor",
            "description": "Return the LLDP neighbor seen on a Nokia SR OS port.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "interface": {"type": "string"},
                },
                "required": ["device", "interface"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "nokia_get_bgp_neighbor_status",
            "description": "Get BGP session state for a specific peer on a Nokia SR OS device.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "neighbor": {"type": "string", "description": "BGP peer IP address"},
                },
                "required": ["device", "neighbor"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "nokia_get_bgp_summary",
            "description": "Get BGP summary (all peers) from a Nokia SR OS device.",
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
            "name": "nokia_get_ospf_neighbors",
            "description": "Get OSPF neighbor adjacencies from a Nokia SR OS device.",
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
            "name": "nokia_get_isis_neighbors",
            "description": "Get IS-IS adjacencies from a Nokia SR OS device.",
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
            "name": "nokia_get_isis_interface",
            "description": "Get IS-IS interface details for a specific Nokia SR OS interface.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "interface": {"type": "string"},
                },
                "required": ["device", "interface"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "nokia_get_cpu_memory",
            "description": "Get CPU and memory utilisation from a Nokia SR OS device.",
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
            "name": "nokia_get_chassis_health",
            "description": "Get chassis, card, and alarm state from a Nokia SR OS device.",
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
            "name": "nokia_get_route_lookup",
            "description": "Look up a specific prefix in the Nokia SR OS routing table.",
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
            "name": "nokia_get_bgp_route",
            "description": "Get BGP path attributes for a specific prefix on a Nokia SR OS device.",
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
            "name": "nokia_get_acl_hits",
            "description": "Get hit counts for a Nokia SR OS IP filter (ACL).",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "acl_name": {"type": "string"},
                },
                "required": ["device", "acl_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "nokia_get_mtu_info",
            "description": "Get MTU configuration for a Nokia SR OS port.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "interface": {"type": "string"},
                },
                "required": ["device", "interface"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "nokia_get_ping",
            "description": "Run a ping from a Nokia SR OS device to a destination.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "destination": {"type": "string"},
                    "count": {"type": "integer", "default": 5},
                },
                "required": ["device", "destination"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "nokia_get_traceroute",
            "description": "Run a traceroute from a Nokia SR OS device to a destination.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "destination": {"type": "string"},
                },
                "required": ["device", "destination"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "nokia_run_command",
            "description": "Run any read-only CLI command on a Nokia SR OS device.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "command": {"type": "string", "description": "CLI command to run (show/ping/traceroute only)"},
                },
                "required": ["device", "command"],
            },
        },
    },
]
