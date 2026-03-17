"""
Nokia SR OS MCP Server

Exposes Nokia device diagnostic tools over MCP/SSE.
Devices are loaded from devices.yaml (nokia_devices section).
Uses Netmiko for CLI commands; ncclient NETCONF for structured data
where Nokia YANG RPCs are confirmed stable.

Start:  python -m mcp_servers.nokia.server
Port:   MCP_NOKIA_PORT (default 8103)
"""

import json
import logging
import os
from contextlib import contextmanager
from typing import Any, Dict, Optional, Tuple

import sys

import yaml
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from settings import get_setting  # noqa: E402

logger = logging.getLogger("netcopilot.mcp.nokia")

MCP_PORT = int(os.environ.get("MCP_NOKIA_PORT", 8103))
NETCONF_PORT = int(os.environ.get("NETCONF_PORT", 830))
NETCONF_TIMEOUT = int(os.environ.get("NETCONF_TIMEOUT", 10))

REGISTRY_PATH = os.environ.get(
    "DEVICE_REGISTRY_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "devices.yaml"),
)

mcp = FastMCP("nokia-devices", port=MCP_PORT)


# ---------------------------------------------------------------------------
# Device registry
# ---------------------------------------------------------------------------

def _load_devices() -> Dict[str, str]:
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("nokia_devices", {})
    except FileNotFoundError:
        logger.warning("devices.yaml not found at %s", REGISTRY_PATH)
        return {}


devices: Dict[str, str] = _load_devices()


def _resolve_host_port(entry: str) -> Tuple[str, Optional[int]]:
    if ":" in entry:
        base, port = entry.rsplit(":", 1)
        if port.isdigit():
            return base, int(port)
    return entry, None


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

@contextmanager
def _ssh(device_name: str):
    """Netmiko SSH context manager for Nokia SR OS."""
    from netmiko import ConnectHandler

    if device_name not in devices:
        raise ValueError(
            f"Unknown Nokia device '{device_name}'. Known: {list(devices.keys())}"
        )
    nokia_user = get_setting("NOKIA_USER", "")
    nokia_pass = get_setting("NOKIA_PASS", "")
    if not nokia_user or not nokia_pass:
        raise RuntimeError("NOKIA_USER or NOKIA_PASS not set.")

    host, port = _resolve_host_port(devices[device_name])
    params: Dict[str, Any] = {
        "device_type": "nokia_sros",
        "host": host,
        "username": nokia_user,
        "password": nokia_pass,
        "fast_cli": False,
    }
    if port:
        params["port"] = port

    conn = ConnectHandler(**params)
    try:
        yield conn
    finally:
        try:
            conn.disconnect()
        except Exception:
            pass


@contextmanager
def _netconf(device_name: str):
    """ncclient NETCONF context manager for Nokia SR OS."""
    from ncclient import manager

    if device_name not in devices:
        raise ValueError(
            f"Unknown Nokia device '{device_name}'. Known: {list(devices.keys())}"
        )
    nokia_user = get_setting("NOKIA_USER", "")
    nokia_pass = get_setting("NOKIA_PASS", "")
    if not nokia_user or not nokia_pass:
        raise RuntimeError("NOKIA_USER or NOKIA_PASS not set.")

    host, port = _resolve_host_port(devices[device_name])
    nc_port = port or NETCONF_PORT

    mgr = manager.connect(
        host=host,
        port=nc_port,
        username=nokia_user,
        password=nokia_pass,
        hostkey_verify=False,
        timeout=NETCONF_TIMEOUT,
        device_params={"name": "nokia_sros"},
    )
    try:
        yield mgr
    finally:
        try:
            mgr.close_session()
        except Exception:
            pass


def _run_cli(device_name: str, command: str) -> Dict[str, Any]:
    try:
        with _ssh(device_name) as conn:
            output = conn.send_command(command)
        return {"device": device_name, "command": command, "output": output}
    except Exception as exc:
        return {"device": device_name, "command": command, "error": str(exc)}


def _known_devices() -> str:
    return ", ".join(sorted(devices.keys())) or "(none loaded)"


# ---------------------------------------------------------------------------
# MCP Tools — Interface
# ---------------------------------------------------------------------------

@mcp.tool()
def get_interface_health(device: str, interface: str) -> Dict[str, Any]:
    """Get admin/oper state, last change, and IP info for a Nokia port/interface."""
    port = _run_cli(device, f"show port {interface}")
    rif = _run_cli(device, f"show router interface {interface}")
    return {
        "device": device,
        "interface": interface,
        "port": port,
        "router_interface": rif,
    }


@mcp.tool()
def get_interface_status(device: str) -> Dict[str, Any]:
    """List all ports on a Nokia device with their admin/oper state."""
    return _run_cli(device, "show port")


@mcp.tool()
def get_interface_counters(device: str, interface: str) -> Dict[str, Any]:
    """Get traffic counters and error stats for a Nokia port."""
    return _run_cli(device, f"show port {interface} statistics")


@mcp.tool()
def get_lldp_neighbor(device: str, interface: str) -> Dict[str, Any]:
    """Return the LLDP neighbor seen on a Nokia port."""
    result = _run_cli(device, "show system lldp neighbor")
    if "output" not in result:
        return result
    import re
    neighbor_device = None
    neighbor_interface = None
    for line in result["output"].splitlines():
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


@mcp.tool()
def get_mtu_info(device: str, interface: str) -> Dict[str, Any]:
    """Get MTU configuration for a Nokia port."""
    return _run_cli(device, f"show port {interface}")


# ---------------------------------------------------------------------------
# MCP Tools — Routing Protocols
# ---------------------------------------------------------------------------

@mcp.tool()
def get_bgp_neighbor_status(device: str, neighbor: str) -> Dict[str, Any]:
    """Get BGP session state for a specific peer on a Nokia device."""
    return _run_cli(device, f"show router bgp neighbor {neighbor}")


@mcp.tool()
def get_bgp_summary(device: str) -> Dict[str, Any]:
    """Get BGP summary (all peers) from a Nokia device."""
    return _run_cli(device, "show router bgp summary")


@mcp.tool()
def get_ospf_neighbors(device: str) -> Dict[str, Any]:
    """Get OSPF neighbor adjacencies from a Nokia device."""
    return _run_cli(device, "show router ospf neighbor")


@mcp.tool()
def get_isis_neighbors(device: str) -> Dict[str, Any]:
    """Get IS-IS adjacencies from a Nokia device."""
    return _run_cli(device, "show router isis adjacency")


@mcp.tool()
def get_isis_interface(device: str, interface: str) -> Dict[str, Any]:
    """Get IS-IS interface details for a specific Nokia interface."""
    return _run_cli(device, f"show router isis interface {interface}")


# ---------------------------------------------------------------------------
# MCP Tools — System Health
# ---------------------------------------------------------------------------

@mcp.tool()
def get_cpu_memory(device: str) -> Dict[str, Any]:
    """Get CPU and memory utilisation from a Nokia device."""
    cpu = _run_cli(device, "show system cpu")
    mem = _run_cli(device, "show system memory")
    return {"device": device, "cpu": cpu, "memory": mem}


@mcp.tool()
def get_chassis_health(device: str) -> Dict[str, Any]:
    """Get chassis, card, and alarm state from a Nokia device."""
    chassis = _run_cli(device, "show chassis")
    cards = _run_cli(device, "show card")
    alarms = _run_cli(device, "show system alarms")
    return {"device": device, "chassis": chassis, "cards": cards, "alarms": alarms}


# ---------------------------------------------------------------------------
# MCP Tools — Routing / Forwarding
# ---------------------------------------------------------------------------

@mcp.tool()
def get_route_lookup(device: str, prefix: str) -> Dict[str, Any]:
    """Look up a specific prefix in the Nokia routing table."""
    return _run_cli(device, f"show router route-table {prefix}")


@mcp.tool()
def get_bgp_route(device: str, prefix: str) -> Dict[str, Any]:
    """Get BGP path attributes for a specific prefix on a Nokia device."""
    return _run_cli(device, f"show router bgp routes {prefix}")


# ---------------------------------------------------------------------------
# MCP Tools — ACL / Policy
# ---------------------------------------------------------------------------

@mcp.tool()
def get_acl_hits(device: str, acl_name: str) -> Dict[str, Any]:
    """Get hit counts for a Nokia IP filter (ACL)."""
    return _run_cli(device, f"show filter ip {acl_name}")


# ---------------------------------------------------------------------------
# MCP Tools — Connectivity
# ---------------------------------------------------------------------------

@mcp.tool()
def get_ping(device: str, destination: str, count: int = 5) -> Dict[str, Any]:
    """Run a ping from a Nokia device to a destination."""
    return _run_cli(device, f"ping {destination} count {count}")


@mcp.tool()
def get_traceroute(device: str, destination: str) -> Dict[str, Any]:
    """Run a traceroute from a Nokia device to a destination."""
    return _run_cli(device, f"traceroute {destination}")


# ---------------------------------------------------------------------------
# MCP Tools — Free-form CLI
# ---------------------------------------------------------------------------

@mcp.tool()
def run_command(device: str, command: str) -> Dict[str, Any]:
    """
    Run any read-only CLI command on a Nokia device.
    Allowed prefixes: show, debug, admin show, ping, traceroute.
    """
    allowed = ("show ", "debug ", "admin show", "ping ", "traceroute ")
    if not any(command.strip().lower().startswith(p) for p in allowed):
        return {
            "device": device,
            "command": command,
            "error": f"Command rejected. Only read-only commands are allowed: {allowed}",
        }
    return _run_cli(device, command)


# ---------------------------------------------------------------------------
# MCP Tools — Inventory
# ---------------------------------------------------------------------------

@mcp.tool()
def list_devices() -> Dict[str, Any]:
    """Return all Nokia devices registered in devices.yaml."""
    return {"devices": list(devices.keys())}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logger.info("Starting Nokia MCP server on port %d", MCP_PORT)
    mcp.run(transport="sse")
