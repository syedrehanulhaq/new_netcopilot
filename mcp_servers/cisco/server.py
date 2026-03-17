"""
Cisco IOS XR MCP Server — NETCONF + Netmiko based

Uses NETCONF (ncclient) for structured data wherever IOS XR supports it.
Falls back to Netmiko SSH for commands not covered by NETCONF.
This is far more stable than pure CLI regex parsing.

Run:
    python -m mcp_servers.cisco.server

Config (environment variables):
    CISCO_USER             SSH/NETCONF username
    CISCO_PASS             SSH/NETCONF password
    NETCONF_PORT           NETCONF port (default: 830)
    NETCONF_TIMEOUT        Connection timeout (default: 30)
    DEVICE_REGISTRY_PATH   Path to devices.yaml
"""

import logging
import os
import re
from contextlib import contextmanager
from typing import Any
from xml.etree import ElementTree

import sys

import yaml
from mcp.server.fastmcp import FastMCP

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from settings import get_setting  # noqa: E402

logger = logging.getLogger("netcopilot.mcp.cisco")

NETCONF_PORT = int(os.environ.get("NETCONF_PORT", "830"))
NETCONF_TIMEOUT = int(os.environ.get("NETCONF_TIMEOUT", "30"))
DEVICE_REGISTRY_PATH = os.environ.get("DEVICE_REGISTRY_PATH", "devices.yaml")
MCP_PORT = int(os.environ.get("CISCO_MCP_PORT", "8102"))

mcp = FastMCP("cisco", port=MCP_PORT)

# IOS XR NETCONF namespaces
_NS = {
    "int": "http://cisco.com/ns/yang/Cisco-IOS-XR-pfi-im-cmd-oper",
    "bgp": "http://cisco.com/ns/yang/Cisco-IOS-XR-ipv4-bgp-oper",
    "isis": "http://cisco.com/ns/yang/Cisco-IOS-XR-clns-isis-oper",
    "ospf": "http://cisco.com/ns/yang/Cisco-IOS-XR-ipv4-ospf-oper",
    "rt": "http://cisco.com/ns/yang/Cisco-IOS-XR-ip-rib-ipv4-oper",
    "lldp": "http://cisco.com/ns/yang/Cisco-IOS-XR-ethernet-lldp-oper",
}


# ---------------------------------------------------------------------------
# Device inventory
# ---------------------------------------------------------------------------

def _load_devices() -> dict[str, str]:
    path = DEVICE_REGISTRY_PATH
    if not os.path.exists(path):
        path = os.path.join(os.path.dirname(__file__), "..", "..", "devices.yaml")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return {str(k): str(v) for k, v in data.get("cisco_devices", {}).items()}
    except FileNotFoundError:
        return {}


def _resolve_host(device: str) -> str:
    devices = _load_devices()
    if device not in devices:
        raise ValueError(f"Unknown device '{device}'. Known: {sorted(devices.keys())}")
    return devices[device]


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

@contextmanager
def _netconf(device: str):
    """Open an ncclient NETCONF session."""
    from ncclient import manager

    host = _resolve_host(device)
    cisco_user = get_setting("CISCO_USER", "")
    cisco_pass = get_setting("CISCO_PASS", "")
    if not cisco_user or not cisco_pass:
        raise RuntimeError("CISCO_USER or CISCO_PASS not set.")
    with manager.connect(
        host=host,
        port=NETCONF_PORT,
        username=cisco_user,
        password=cisco_pass,
        hostkey_verify=False,
        device_params={"name": "iosxr"},
        timeout=NETCONF_TIMEOUT,
    ) as m:
        yield m


@contextmanager
def _ssh(device: str):
    """Open a Netmiko SSH session for commands not available via NETCONF."""
    from netmiko import ConnectHandler

    host = _resolve_host(device)
    cisco_user = get_setting("CISCO_USER", "")
    cisco_pass = get_setting("CISCO_PASS", "")
    if not cisco_user or not cisco_pass:
        raise RuntimeError("CISCO_USER or CISCO_PASS not set.")
    conn = ConnectHandler(
        device_type="cisco_xr",
        host=host,
        username=cisco_user,
        password=cisco_pass,
        fast_cli=False,
        conn_timeout=NETCONF_TIMEOUT,
    )
    try:
        yield conn
    finally:
        try:
            conn.disconnect()
        except Exception:
            pass


def _nc_get(device: str, filter_xml: str) -> ElementTree.Element | None:
    """Run NETCONF get-config/get with an XML filter, return root element."""
    try:
        with _netconf(device) as m:
            reply = m.get(filter=("subtree", filter_xml))
            return ElementTree.fromstring(reply.data_xml)
    except Exception as exc:
        logger.debug("NETCONF get failed for %s: %s", device, exc)
        return None


def _nc_rpc(device: str, rpc_xml: str) -> ElementTree.Element | None:
    """Send a raw NETCONF RPC, return the reply element."""
    try:
        with _netconf(device) as m:
            reply = m.dispatch(ElementTree.fromstring(rpc_xml))
            return ElementTree.fromstring(reply.xml)
    except Exception as exc:
        logger.debug("NETCONF RPC failed for %s: %s", device, exc)
        return None


def _cli(device: str, command: str) -> str:
    """Run CLI command via SSH, return raw output."""
    with _ssh(device) as conn:
        return conn.send_command(command)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _find(elem, path: str, default: str = "") -> str:
    if elem is None:
        return default
    node = elem.find(path)
    if node is not None and node.text:
        return node.text.strip()
    return default


def _int(v: str, default: int = 0) -> int:
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


def _float(v: str, default: float = 0.0) -> float:
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _err(device: str, exc: Exception, **extra) -> dict:
    return {"device": device, "error": str(exc), **extra}


def _parse_show_interface(output: str) -> dict:
    """Parse IOS XR 'show interface' output into a structured dict."""
    parsed: dict[str, Any] = {}
    m = re.search(r"^(\S+) is ([^,]+), line protocol is (\S+)", output, re.MULTILINE)
    if m:
        parsed["admin_status"] = "up" if "up" in m.group(2).lower() else "down"
        parsed["oper_status"] = "up" if "up" in m.group(3).lower() else "down"
        parsed["admin_status_raw"] = m.group(2).strip()
        parsed["oper_status_raw"] = m.group(3).strip()

    for line in output.splitlines():
        ll = line.lower()
        if "last link flapped" in ll:
            m2 = re.search(r"last link flapped\s+(.+)$", line, re.IGNORECASE)
            if m2:
                parsed["last_link_flapped"] = m2.group(1).strip()
        elif re.search(r"\d+ input errors", ll):
            for name, val in re.findall(r"(\d+)\s+([a-z][a-z ]+?)\s*(?:,|$)", ll):
                parsed[val.strip().replace(" ", "_")] = int(name)
        elif re.search(r"\d+ output errors", ll):
            for name, val in re.findall(r"(\d+)\s+([a-z][a-z ]+?)\s*(?:,|$)", ll):
                parsed[val.strip().replace(" ", "_")] = int(name)
        elif "carrier transitions" in ll:
            m2 = re.search(r"(\d+)\s+carrier transitions", ll)
            if m2:
                parsed["carrier_transitions"] = int(m2.group(1))
        elif "minute input rate" in ll:
            m2 = re.search(r"(\d+)\s+minute input rate\s+(\d+)\s+bits.*?(\d+)\s+packets", ll)
            if m2:
                parsed["input_rate_bps"] = int(m2.group(2))
                parsed["input_rate_pps"] = int(m2.group(3))
        elif "minute output rate" in ll:
            m2 = re.search(r"(\d+)\s+minute output rate\s+(\d+)\s+bits.*?(\d+)\s+packets", ll)
            if m2:
                parsed["output_rate_bps"] = int(m2.group(2))
                parsed["output_rate_pps"] = int(m2.group(3))

    return parsed


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_interface_health(device: str, interface: str) -> dict:
    """Get interface admin/oper status, errors, counters, and last flap."""
    try:
        output = _cli(device, f"show interface {interface}")
        parsed = _parse_show_interface(output)
        try:
            ctrl = _cli(device, f"show controllers {interface}")
            for line in ctrl.splitlines():
                ll = line.lower()
                if "rx power" in ll:
                    m = re.search(r"([-\d.]+)\s*dBm", line, re.IGNORECASE)
                    if m:
                        parsed["rx_power_dbm"] = float(m.group(1))
                if "tx power" in ll:
                    m = re.search(r"([-\d.]+)\s*dBm", line, re.IGNORECASE)
                    if m:
                        parsed["tx_power_dbm"] = float(m.group(1))
        except Exception:
            pass
        return {"device": device, "interface": interface, **parsed}
    except Exception as exc:
        return _err(device, exc, interface=interface)


@mcp.tool()
def get_interface_status(device: str) -> dict:
    """Get admin/oper status of all interfaces on the device."""
    try:
        output = _cli(device, "show interfaces description")
        interfaces = []
        for line in output.splitlines():
            if not line.strip() or line.lower().startswith("interface") or set(line.strip()) == {"-"}:
                continue
            parts = re.split(r"\s{2,}", line.strip())
            if len(parts) < 3:
                continue
            interfaces.append({
                "interface": parts[0],
                "admin_status": parts[1].lower(),
                "oper_status": parts[2].lower(),
                "description": parts[3] if len(parts) > 3 else "",
            })
        up = sum(1 for i in interfaces if i["oper_status"] == "up")
        return {
            "device": device,
            "interfaces": interfaces,
            "summary": {"total": len(interfaces), "up": up, "down": len(interfaces) - up},
        }
    except Exception as exc:
        return _err(device, exc)


@mcp.tool()
def get_lldp_neighbor(device: str, interface: str) -> dict:
    """Get LLDP neighbor on an interface."""
    try:
        output = _cli(device, f"show lldp neighbors interface {interface} detail")
        parsed: dict[str, Any] = {}
        for line in output.splitlines():
            ll = line.strip().lower()
            if ll.startswith("system name"):
                parsed["neighbor_device"] = line.split(":", 1)[1].strip()
            elif ll.startswith("port id"):
                parsed["neighbor_interface"] = line.split(":", 1)[1].strip()
            elif ll.startswith("port description") and "neighbor_interface" not in parsed:
                parsed["neighbor_interface"] = line.split(":", 1)[1].strip()
        if parsed:
            return {"device": device, "interface": interface, **parsed}
        return {"device": device, "interface": interface,
                "error": "No LLDP neighbor found on this interface."}
    except Exception as exc:
        return _err(device, exc, interface=interface)


@mcp.tool()
def get_bgp_neighbor_status(device: str, neighbor: str) -> dict:
    """Get BGP peer state, last error, and prefix counts."""
    try:
        output = _cli(device, f"show bgp neighbor {neighbor}")
        parsed: dict[str, Any] = {"device": device, "neighbor": neighbor}
        for line in output.splitlines():
            ll = line.lower().strip()
            if ll.startswith("bgp state"):
                m = re.search(r"BGP state\s*=\s*(\w+)", line, re.IGNORECASE)
                if m:
                    parsed["state"] = m.group(1)
            elif "last reset" in ll:
                parsed["last_reset"] = line.split(":", 1)[-1].strip()
            elif "prefixes" in ll and "accepted" in ll:
                m = re.search(r"(\d+)\s+prefixes accepted", line, re.IGNORECASE)
                if m:
                    parsed["accepted_prefixes"] = int(m.group(1))
            elif "notification" in ll and "sent" in ll:
                parsed["notifications_sent"] = line.strip()
        return parsed
    except Exception as exc:
        return _err(device, exc, neighbor=neighbor)


@mcp.tool()
def get_bgp_summary(device: str) -> dict:
    """Get BGP summary table for all peers."""
    try:
        output = _cli(device, "show bgp summary")
        peers = []
        for line in output.splitlines():
            m = re.match(
                r"^(\d{1,3}(?:\.\d{1,3}){3})\s+\d+\s+(\d+)\s+(\d+)\s+\d+\s+\d+\s+\S+\s+(\S+)",
                line.strip(),
            )
            if m:
                peers.append({
                    "neighbor": m.group(1),
                    "msg_rcvd": int(m.group(2)),
                    "msg_sent": int(m.group(3)),
                    "state_or_prefixes": m.group(4),
                    "established": m.group(4).isdigit(),
                })
        down = sum(1 for p in peers if not p["established"])
        return {
            "device": device,
            "peers": peers,
            "summary": {"total": len(peers), "established": len(peers) - down, "down": down},
        }
    except Exception as exc:
        return _err(device, exc)


@mcp.tool()
def get_ospf_neighbors(device: str) -> dict:
    """Get OSPF neighbor table."""
    try:
        output = _cli(device, "show ospf neighbor")
        neighbors = []
        for line in output.splitlines():
            if not line.strip() or line.lower().startswith("neighbor"):
                continue
            parts = re.split(r"\s+", line.strip())
            if len(parts) >= 6 and re.match(r"\d+\.\d+\.\d+\.\d+", parts[0]):
                neighbors.append({
                    "neighbor_id": parts[0],
                    "priority": parts[1],
                    "state": parts[2],
                    "dead_timer": parts[3],
                    "address": parts[4],
                    "interface": parts[5],
                })
        full = sum(1 for n in neighbors if "full" in n["state"].lower())
        return {
            "device": device,
            "neighbors": neighbors,
            "summary": {"total": len(neighbors), "full": full},
        }
    except Exception as exc:
        return _err(device, exc)


@mcp.tool()
def get_isis_neighbors(device: str) -> dict:
    """Get IS-IS adjacency table."""
    try:
        output = _cli(device, "show isis neighbors")
        neighbors = []
        for line in output.splitlines():
            if not line.strip() or line.lower().startswith("system"):
                continue
            parts = re.split(r"\s+", line.strip())
            if len(parts) >= 4:
                neighbors.append({
                    "system": parts[0],
                    "interface": parts[1],
                    "snpa": parts[2] if len(parts) > 2 else "",
                    "state": parts[3] if len(parts) > 3 else "",
                    "hold": parts[4] if len(parts) > 4 else "",
                })
        up = sum(1 for n in neighbors if n["state"].upper() == "UP")
        return {
            "device": device,
            "neighbors": neighbors,
            "summary": {"total": len(neighbors), "up": up},
        }
    except Exception as exc:
        return _err(device, exc)


@mcp.tool()
def get_isis_interface(device: str, interface: str) -> dict:
    """Get IS-IS interface configuration and state."""
    try:
        output = _cli(device, f"show isis interface {interface}")
        return {"device": device, "interface": interface, "output": output}
    except Exception as exc:
        return _err(device, exc, interface=interface)


@mcp.tool()
def get_cpu_memory(device: str) -> dict:
    """Get CPU utilization and memory usage summary."""
    try:
        mem_out = _cli(device, "show memory summary")
        result: dict[str, Any] = {"device": device, "mem_raw": mem_out}

        # IOS XR CPU — command varies by version; try in order until one works
        cpu_out = ""
        for cpu_cmd in [
            "show processes cpu",           # IOS XR 6.x/7.x standard
            "show cpu",                     # some IOS XR variants
            "show processes brief",         # fallback — no CPU % but shows load
        ]:
            try:
                cpu_out = _cli(device, cpu_cmd)
                if "Invalid input" not in cpu_out and "No such process" not in cpu_out:
                    result["cpu_raw"] = cpu_out
                    break
            except Exception:
                continue

        # IOS XR format: "CPU utilization for five seconds:  3%/1%; one minute:  2%; five minutes:  2%"
        m = re.search(
            r"five seconds:\s+(\d+)%.*?one minute:\s+(\d+)%.*?five minutes:\s+(\d+)%",
            cpu_out, re.IGNORECASE | re.DOTALL,
        )
        if m:
            result["cpu_5s_percent"] = int(m.group(1))
            result["cpu_1m_percent"] = int(m.group(2))
            result["cpu_5m_percent"] = int(m.group(3))

        # IOS XR 7.x format: "Physical Memory: 24027M total (15128M available)"
        m2 = re.search(
            r"Physical Memory:\s+(\d+)M\s+total\s+\((\d+)M\s+available\)",
            mem_out, re.IGNORECASE,
        )
        if m2:
            total_mb = int(m2.group(1))
            avail_mb = int(m2.group(2))
            used_mb = total_mb - avail_mb
            result["mem_total_mb"] = total_mb
            result["mem_used_mb"] = used_mb
            result["mem_free_mb"] = avail_mb
            result["mem_used_percent"] = round(used_mb / total_mb * 100, 1) if total_mb else None
        else:
            # Older format: "Physical Memory: 3145728K(2097152K used)"
            m2b = re.search(
                r"Physical Memory:\s+(\d+)K\((\d+)K used\)",
                mem_out, re.IGNORECASE,
            )
            if m2b:
                total_kb = int(m2b.group(1))
                used_kb = int(m2b.group(2))
                result["mem_total_mb"] = total_kb // 1024
                result["mem_used_mb"] = used_kb // 1024
                result["mem_free_mb"] = (total_kb - used_kb) // 1024
                result["mem_used_percent"] = round(used_kb / total_kb * 100, 1) if total_kb else None

        return result
    except Exception as exc:
        return _err(device, exc)


@mcp.tool()
def get_route_lookup(device: str, prefix: str) -> dict:
    """Look up routing table entry for a prefix."""
    try:
        output = _cli(device, f"show route {prefix}")
        return {"device": device, "prefix": prefix, "output": output}
    except Exception as exc:
        return _err(device, exc, prefix=prefix)


@mcp.tool()
def get_bgp_route(device: str, prefix: str) -> dict:
    """Look up BGP routing table entry for a prefix."""
    try:
        output = _cli(device, f"show bgp {prefix}")
        return {"device": device, "prefix": prefix, "output": output}
    except Exception as exc:
        return _err(device, exc, prefix=prefix)


@mcp.tool()
def get_acl_hits(device: str, acl_name: str) -> dict:
    """Get ACL (access-list) hit counters."""
    try:
        output = _cli(device, f"show access-lists {acl_name}")
        return {"device": device, "acl_name": acl_name, "output": output}
    except Exception as exc:
        return _err(device, exc, acl_name=acl_name)


@mcp.tool()
def get_mtu_info(device: str, interface: str) -> dict:
    """Get MTU configuration for an interface."""
    try:
        output = _cli(device, f"show interface {interface}")
        mtu = None
        m = re.search(r"MTU\s+(\d+)\s+bytes", output)
        if m:
            mtu = int(m.group(1))
        return {"device": device, "interface": interface, "mtu": mtu, "output": output}
    except Exception as exc:
        return _err(device, exc, interface=interface)


@mcp.tool()
def get_ping(device: str, destination: str, count: int = 5) -> dict:
    """Run ping from the device to a destination."""
    try:
        output = _cli(device, f"ping {destination} count {count}")
        m = re.search(r"(\d+)/(\d+).*?(\d+)%", output)
        received = int(m.group(1)) if m else None
        sent = int(m.group(2)) if m else None
        loss_pct = int(m.group(3)) if m else None
        return {
            "device": device, "destination": destination, "count": count,
            "received": received, "sent": sent, "packet_loss_pct": loss_pct,
            "output": output,
        }
    except Exception as exc:
        return _err(device, exc, destination=destination)


@mcp.tool()
def get_traceroute(device: str, destination: str) -> dict:
    """Run traceroute from the device to a destination."""
    try:
        output = _cli(device, f"traceroute {destination}")
        return {"device": device, "destination": destination, "output": output}
    except Exception as exc:
        return _err(device, exc, destination=destination)


@mcp.tool()
def get_chassis_health(device: str) -> dict:
    """Get chassis alarms and system health summary."""
    try:
        return {
            "device": device,
            "alarms": _cli(device, "show alarms"),
            "platform": _cli(device, "show platform"),
        }
    except Exception as exc:
        return _err(device, exc)


@mcp.tool()
def run_command(device: str, command: str) -> dict:
    """Run an arbitrary show/diagnostic CLI command on the device."""
    try:
        output = _cli(device, command)
        return {"device": device, "command": command, "output": output}
    except Exception as exc:
        return _err(device, exc, command=command)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logger.info("Starting Cisco MCP server on port %d", MCP_PORT)
    mcp.run(transport="sse")
