"""
Junos MCP Server — PyEZ/NETCONF based

Exposes all Juniper Junos diagnostic operations as MCP tools.
Uses junos-eznc (PyEZ) — Juniper's official Python library.

PyEZ uses NETCONF under the hood. The RPC schemas are vendor-defined and
stable across Junos versions. This eliminates the fragile CLI regex parsing
that breaks with OS upgrades.

Run:
    python -m mcp_servers.junos.server

Config (environment variables):
    JUNIPER_USER       SSH/NETCONF username
    JUNIPER_PASS       SSH/NETCONF password
    NETCONF_PORT       NETCONF port (default: 830)
    NETCONF_TIMEOUT    Connection timeout in seconds (default: 30)
    DEVICE_REGISTRY_PATH   Path to devices.yaml (default: ./devices.yaml)
"""

import json
import logging
import os
from contextlib import contextmanager
from typing import Any

import sys

import yaml
from mcp.server.fastmcp import FastMCP

# Allow importing settings from the project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from settings import get_setting  # noqa: E402

logger = logging.getLogger("netcopilot.mcp.junos")

# ---------------------------------------------------------------------------
# Configuration  (static at startup — credentials read live at connect time)
# ---------------------------------------------------------------------------

NETCONF_PORT = int(os.environ.get("NETCONF_PORT", "830"))
NETCONF_TIMEOUT = int(os.environ.get("NETCONF_TIMEOUT", "30"))
DEVICE_REGISTRY_PATH = os.environ.get("DEVICE_REGISTRY_PATH", "devices.yaml")
MCP_PORT = int(os.environ.get("JUNOS_MCP_PORT", "8101"))

mcp = FastMCP("junos", port=MCP_PORT)


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
        return {str(k): str(v) for k, v in data.get("juniper_devices", {}).items()}
    except FileNotFoundError:
        logger.warning("devices.yaml not found at %s", path)
        return {}


def _resolve_host_port(target: str) -> tuple[str, int]:
    if ":" in target:
        host, port = target.rsplit(":", 1)
        if port.isdigit():
            return host, int(port)
    return target, NETCONF_PORT


# ---------------------------------------------------------------------------
# PyEZ connection helper
# ---------------------------------------------------------------------------

@contextmanager
def _connect(device_name: str):
    """Open a PyEZ Device connection, yield it, then close."""
    devices = _load_devices()
    if device_name not in devices:
        raise ValueError(
            f"Unknown device '{device_name}'. Known: {sorted(devices.keys())}"
        )
    user = get_setting("JUNIPER_USER", "")
    passwd = get_setting("JUNIPER_PASS", "")
    if not user or not passwd:
        raise RuntimeError("JUNIPER_USER or JUNIPER_PASS not set.")

    from jnpr.junos import Device
    from jnpr.junos.exception import ConnectError

    host, port = _resolve_host_port(devices[device_name])
    dev = Device(
        host=host,
        port=port,
        user=user,
        passwd=passwd,
        auto_probe=0,
        gather_facts=False,
    )
    try:
        dev.open(auto_probe=0)
        yield dev
    except ConnectError as exc:
        raise RuntimeError(f"Cannot connect to {device_name} ({host}:{port}): {exc}") from exc
    finally:
        try:
            dev.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------

def _find(elem, path: str, default: str = "") -> str:
    if elem is None:
        return default
    node = elem.find(path)
    if node is not None and node.text:
        return node.text.strip()
    return default


def _findall(elem, path: str) -> list:
    if elem is None:
        return []
    return elem.findall(path)


def _int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _float(value: str, default: float = 0.0) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _err(device: str, exc: Exception, **extra) -> dict:
    return {"device": device, "error": str(exc), **extra}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_interface_health(device: str, interface: str) -> dict:
    """Get detailed interface health: admin/oper status, errors, counters, last flap."""
    try:
        with _connect(device) as dev:
            rpc = dev.rpc.get_interface_information(
                interface_name=interface, extensive=True
            )
            phys = rpc.find(".//physical-interface")
            if phys is None:
                return {"device": device, "interface": interface,
                        "error": "Interface not found"}

            # Alarms / defects
            alarm_node = phys.find("active-alarms/interface-alarms")
            alarms = (
                "None"
                if alarm_node is not None and alarm_node.find("alarm-not-present") is not None
                else _find(phys, "active-alarms")
            )
            defect_node = phys.find("active-defects/interface-defects")
            defects = (
                "None"
                if defect_node is not None and defect_node.find("defect-not-present") is not None
                else _find(phys, "active-defects")
            )

            return {
                "device": device,
                "interface": interface,
                "admin_status": _find(phys, "admin-status"),
                "oper_status": _find(phys, "oper-status"),
                "description": _find(phys, "description"),
                "mtu": _find(phys, "mtu"),
                "speed": _find(phys, "speed"),
                "last_flapped": (
                    _find(phys, "statistics/interface-flap-statistics/last-flapped")
                    or _find(phys, "interface-flap-statistics/last-flapped")
                ),
                "active_alarms": alarms,
                "active_defects": defects,
                "input_errors": _int(_find(phys, ".//input-error-count")),
                "output_errors": _int(_find(phys, ".//output-error-count")),
                "crc_errors": _int(_find(phys, ".//crc-errors")),
                "input_packets": _int(_find(phys, ".//input-packets")),
                "output_packets": _int(_find(phys, ".//output-packets")),
                "input_bytes": _int(_find(phys, ".//input-bytes")),
                "output_bytes": _int(_find(phys, ".//output-bytes")),
                "carrier_transitions": _int(_find(phys, ".//carrier-transitions")),
            }
    except Exception as exc:
        return _err(device, exc, interface=interface)


@mcp.tool()
def get_interface_status(device: str) -> dict:
    """Get admin/oper status of all interfaces on the device."""
    try:
        with _connect(device) as dev:
            rpc = dev.rpc.get_interface_information(terse=True)
            interfaces = []
            for phys in _findall(rpc, ".//physical-interface"):
                name = _find(phys, "name")
                if not name:
                    continue
                interfaces.append({
                    "interface": name,
                    "admin_status": _find(phys, "admin-status"),
                    "oper_status": _find(phys, "oper-status"),
                    "description": _find(phys, "description"),
                })
                for li in _findall(phys, "logical-interface"):
                    li_name = _find(li, "name")
                    if li_name:
                        interfaces.append({
                            "interface": li_name,
                            "admin_status": _find(li, "admin-status") or _find(phys, "admin-status"),
                            "oper_status": _find(li, "oper-status"),
                            "description": _find(li, "description"),
                            "logical": True,
                        })
            up = sum(1 for i in interfaces if i["oper_status"].lower() == "up")
            return {
                "device": device,
                "interfaces": interfaces,
                "summary": {"total": len(interfaces), "up": up, "down": len(interfaces) - up},
            }
    except Exception as exc:
        return _err(device, exc)


@mcp.tool()
def get_interface_counters(device: str, interface: str) -> dict:
    """Get detailed interface error counters and traffic statistics."""
    try:
        with _connect(device) as dev:
            rpc = dev.rpc.get_interface_information(
                interface_name=interface, extensive=True
            )
            phys = rpc.find(".//physical-interface")
            if phys is None:
                return {"device": device, "interface": interface, "error": "Interface not found"}

            stats = phys.find("traffic-statistics") or phys
            return {
                "device": device,
                "interface": interface,
                "input_packets": _int(_find(stats, "input-packets")),
                "output_packets": _int(_find(stats, "output-packets")),
                "input_bytes": _int(_find(stats, "input-bytes")),
                "output_bytes": _int(_find(stats, "output-bytes")),
                "input_errors": _int(_find(phys, ".//input-error-count")),
                "output_errors": _int(_find(phys, ".//output-error-count")),
                "crc_errors": _int(_find(phys, ".//crc-errors")),
                "carrier_transitions": _int(_find(phys, ".//carrier-transitions")),
                "input_drops": _int(_find(phys, ".//input-drops")),
                "output_drops": _int(_find(phys, ".//output-drops")),
            }
    except Exception as exc:
        return _err(device, exc, interface=interface)


@mcp.tool()
def get_lldp_neighbor(device: str, interface: str) -> dict:
    """Get LLDP neighbor information for an interface."""
    try:
        with _connect(device) as dev:
            rpc = dev.rpc.get_lldp_neighbors_information()
            for nbr in _findall(rpc, ".//lldp-neighbor-information"):
                local_if = _find(nbr, "lldp-local-interface") or _find(nbr, "lldp-local-port-id")
                if local_if.split(".")[0] != interface.split(".")[0]:
                    continue
                return {
                    "device": device,
                    "interface": interface,
                    "neighbor_device": (
                        _find(nbr, "lldp-remote-system-name")
                        or _find(nbr, "lldp-remote-chassis-id")
                    ),
                    "neighbor_interface": (
                        _find(nbr, "lldp-remote-port-id")
                        or _find(nbr, "lldp-remote-port-description")
                    ),
                    "neighbor_description": _find(nbr, "lldp-remote-port-description"),
                }
            return {
                "device": device,
                "interface": interface,
                "error": "No LLDP neighbor found on this interface.",
            }
    except Exception as exc:
        return _err(device, exc, interface=interface)


@mcp.tool()
def get_bgp_neighbor_status(device: str, neighbor: str) -> dict:
    """Get BGP peer state, last event, last error, and route counts."""
    try:
        with _connect(device) as dev:
            rpc = dev.rpc.get_bgp_neighbor_information(neighbor_address=neighbor)
            peer = rpc.find(".//bgp-peer")
            if peer is None:
                return {"device": device, "neighbor": neighbor, "error": "BGP peer not found"}
            return {
                "device": device,
                "neighbor": neighbor,
                "peer_address": _find(peer, "peer-address"),
                "peer_as": _find(peer, "peer-as"),
                "local_as": _find(peer, "local-as"),
                "state": _find(peer, "peer-state"),
                "last_state": _find(peer, "last-state"),
                "last_event": _find(peer, "last-event"),
                "last_error": _find(peer, "last-error"),
                "active_prefixes": _int(_find(peer, ".//active-prefix-count")),
                "received_prefixes": _int(_find(peer, ".//received-prefix-count")),
                "accepted_prefixes": _int(_find(peer, ".//accepted-prefix-count")),
                "flap_count": _int(_find(peer, "flap-count")),
            }
    except Exception as exc:
        return _err(device, exc, neighbor=neighbor)


@mcp.tool()
def get_bgp_summary(device: str) -> dict:
    """Get BGP summary table showing all peers and their states."""
    try:
        with _connect(device) as dev:
            rpc = dev.rpc.get_bgp_summary_information()
            peers = []
            for peer in _findall(rpc, ".//bgp-peer"):
                peers.append({
                    "address": _find(peer, "peer-address"),
                    "as": _find(peer, "peer-as"),
                    "state": _find(peer, "peer-state"),
                    "last_event": _find(peer, "last-event"),
                    "active_prefixes": _int(_find(peer, ".//active-prefix-count")),
                    "flap_count": _int(_find(peer, "flap-count")),
                })
            down = sum(1 for p in peers if p["state"].lower() != "established")
            return {
                "device": device,
                "peers": peers,
                "summary": {
                    "total": len(peers),
                    "established": len(peers) - down,
                    "down": down,
                },
            }
    except Exception as exc:
        return _err(device, exc)


@mcp.tool()
def get_ospf_neighbors(device: str) -> dict:
    """Get OSPF neighbor table with state and interface."""
    try:
        with _connect(device) as dev:
            rpc = dev.rpc.get_ospf_neighbor_information()
            neighbors = []
            for nbr in _findall(rpc, ".//ospf-neighbor"):
                neighbors.append({
                    "neighbor_id": _find(nbr, "neighbor-id"),
                    "interface": _find(nbr, "interface-name"),
                    "state": _find(nbr, "ospf-neighbor-state"),
                    "priority": _find(nbr, "neighbor-priority"),
                    "dead_timer": _find(nbr, "neighbor-dead-timer"),
                })
            full = sum(1 for n in neighbors if n["state"].upper() == "Full".upper())
            return {
                "device": device,
                "neighbors": neighbors,
                "summary": {"total": len(neighbors), "full": full},
            }
    except Exception as exc:
        return _err(device, exc)


@mcp.tool()
def get_isis_neighbors(device: str) -> dict:
    """Get IS-IS adjacency table with state and circuit info."""
    try:
        with _connect(device) as dev:
            rpc = dev.rpc.get_isis_adjacency_information()
            neighbors = []
            for adj in _findall(rpc, ".//isis-adjacency"):
                neighbors.append({
                    "system": _find(adj, "system-name"),
                    "interface": _find(adj, "interface-name"),
                    "state": _find(adj, "adjacency-state"),
                    "level": _find(adj, "level"),
                    "hold": _find(adj, "holdtime"),
                    "circuit_id": _find(adj, "circuit-id"),
                })
            up = sum(1 for n in neighbors if n["state"].lower() == "up")
            return {
                "device": device,
                "neighbors": neighbors,
                "summary": {"total": len(neighbors), "up": up},
            }
    except Exception as exc:
        return _err(device, exc)


@mcp.tool()
def get_isis_interface(device: str, interface: str) -> dict:
    """Get IS-IS interface details including level, metric, and hello state."""
    try:
        with _connect(device) as dev:
            rpc = dev.rpc.get_isis_interface_information(interface_name=interface)
            iface = rpc.find(".//isis-interface")
            if iface is None:
                return {"device": device, "interface": interface,
                        "error": "ISIS interface not found"}
            return {
                "device": device,
                "interface": interface,
                "level": _find(iface, "level"),
                "passive": _find(iface, "isis-interface-passive"),
                "adjacency_count": _int(_find(iface, ".//adjacency-count")),
                "circuit_id": _find(iface, "isis-circuit-id"),
                "lsp_interval": _find(iface, "lsp-interval"),
            }
    except Exception as exc:
        return _err(device, exc, interface=interface)


@mcp.tool()
def get_cpu_memory(device: str) -> dict:
    """Get routing engine CPU utilization and memory usage."""
    try:
        with _connect(device) as dev:
            rpc = dev.rpc.get_route_engine_information()
            re = rpc.find(".//route-engine")
            if re is None:
                return {"device": device, "error": "Route engine info not found"}

            cpu_idle = _int(_find(re, "cpu-idle"))
            mem_util_str = _find(re, "memory-system-total-used-percent")

            return {
                "device": device,
                "model": _find(re, "model"),
                "uptime": _find(re, "uptime"),
                "cpu_percent": 100 - cpu_idle if cpu_idle else None,
                "cpu_idle_percent": cpu_idle,
                "memory_used_percent": _int(mem_util_str) if mem_util_str else None,
                "memory_total_mb": _int(_find(re, "memory-system-total")) // 1024 or None,
                "temperature_c": _float(_find(re, "temperature")),
                "last_reboot_reason": _find(re, "last-reboot-reason"),
            }
    except Exception as exc:
        return _err(device, exc)


@mcp.tool()
def get_chassis_health(device: str) -> dict:
    """Get chassis health: FPC/PIC states, alarms, and hardware inventory."""
    try:
        with _connect(device) as dev:
            result: dict[str, Any] = {"device": device}

            # FPC status
            try:
                fpc_rpc = dev.rpc.get_fpc_information()
                fpcs = []
                for fpc in _findall(fpc_rpc, ".//fpc"):
                    fpcs.append({
                        "slot": _find(fpc, "slot"),
                        "state": _find(fpc, "state"),
                        "temperature_c": _find(fpc, "temperature"),
                        "cpu_total": _find(fpc, "cpu-total"),
                        "memory_dram_mb": _find(fpc, "memory-dram-size"),
                    })
                result["fpcs"] = fpcs
            except Exception as e:
                result["fpcs_error"] = str(e)

            # Chassis alarms
            try:
                alarm_rpc = dev.rpc.get_chassis_alarm_information()
                alarms = []
                for alarm in _findall(alarm_rpc, ".//alarm-detail"):
                    alarms.append({
                        "time": _find(alarm, "alarm-time"),
                        "class": _find(alarm, "alarm-class"),
                        "description": _find(alarm, "alarm-description"),
                    })
                result["chassis_alarms"] = alarms
                result["has_alarms"] = len(alarms) > 0
            except Exception:
                pass

            # Routing engine health
            try:
                re_rpc = dev.rpc.get_route_engine_information()
                re = re_rpc.find(".//route-engine")
                if re is not None:
                    cpu_idle = _int(_find(re, "cpu-idle"))
                    result["routing_engine"] = {
                        "model": _find(re, "model"),
                        "uptime": _find(re, "uptime"),
                        "cpu_percent": 100 - cpu_idle,
                        "memory_used_percent": _int(_find(re, "memory-system-total-used-percent")),
                        "temperature_c": _float(_find(re, "temperature")),
                    }
            except Exception:
                pass

            return result
    except Exception as exc:
        return _err(device, exc)


@mcp.tool()
def get_route_lookup(device: str, prefix: str) -> dict:
    """Look up routing table entry for a prefix."""
    try:
        with _connect(device) as dev:
            rpc = dev.rpc.get_route_information(destination=prefix)
            routes = []
            for entry in _findall(rpc, ".//route-entry"):
                routes.append({
                    "prefix": _find(entry, "destination") or prefix,
                    "protocol": _find(entry, "protocol-name"),
                    "preference": _find(entry, "preference"),
                    "age": _find(entry, "age"),
                    "next_hop": _find(entry, ".//to"),
                    "via": _find(entry, ".//via"),
                    "active": entry.find("active-tag") is not None,
                })
            return {
                "device": device,
                "prefix": prefix,
                "routes": routes,
                "found": len(routes) > 0,
                "has_default_route": any(r["prefix"] == "0.0.0.0/0" for r in routes),
            }
    except Exception as exc:
        return _err(device, exc, prefix=prefix)


@mcp.tool()
def get_bgp_route(device: str, prefix: str) -> dict:
    """Look up BGP routing table entry for a prefix."""
    try:
        with _connect(device) as dev:
            rpc = dev.rpc.get_route_information(destination=prefix, protocol="bgp")
            routes = []
            for entry in _findall(rpc, ".//route-entry"):
                routes.append({
                    "prefix": _find(entry, "destination") or prefix,
                    "protocol": _find(entry, "protocol-name"),
                    "preference": _find(entry, "preference"),
                    "local_preference": _find(entry, ".//local-preference"),
                    "next_hop": _find(entry, ".//to"),
                    "as_path": _find(entry, ".//as-path"),
                    "communities": _find(entry, ".//communities"),
                    "active": entry.find("active-tag") is not None,
                })
            return {
                "device": device,
                "prefix": prefix,
                "bgp_routes": routes,
                "found": len(routes) > 0,
            }
    except Exception as exc:
        return _err(device, exc, prefix=prefix)


@mcp.tool()
def get_acl_hits(device: str, acl_name: str) -> dict:
    """Get firewall filter (ACL) term counters and hit counts."""
    try:
        with _connect(device) as dev:
            rpc = dev.rpc.get_firewall_filter_information(filter_name=acl_name)
            terms = []
            for term in _findall(rpc, ".//filter-term"):
                terms.append({
                    "term": _find(term, "term-name"),
                    "bytes": _int(_find(term, ".//byte-count")),
                    "packets": _int(_find(term, ".//packet-count")),
                })
            return {
                "device": device,
                "acl_name": acl_name,
                "terms": terms,
                "total_terms": len(terms),
            }
    except Exception as exc:
        return _err(device, exc, acl_name=acl_name)


@mcp.tool()
def get_mtu_info(device: str, interface: str) -> dict:
    """Get MTU configuration and details for an interface."""
    try:
        with _connect(device) as dev:
            rpc = dev.rpc.get_interface_information(interface_name=interface)
            phys = rpc.find(".//physical-interface")
            if phys is None:
                return {"device": device, "interface": interface, "error": "Interface not found"}
            mtu = _find(phys, "mtu")
            link_mtu = _find(phys, "link-mtu")
            logical_mtu = None
            li = phys.find(".//logical-interface")
            if li is not None:
                logical_mtu = _find(li, "mtu") or _find(li, ".//inet-mtu")
            return {
                "device": device,
                "interface": interface,
                "mtu": mtu,
                "link_mtu": link_mtu,
                "logical_mtu": logical_mtu,
            }
    except Exception as exc:
        return _err(device, exc, interface=interface)


@mcp.tool()
def get_ping(device: str, destination: str, count: int = 5) -> dict:
    """Run ping from the device to a destination."""
    try:
        with _connect(device) as dev:
            rpc = dev.rpc.ping(host=destination, count=str(count), no_resolve=True)
            return {
                "device": device,
                "destination": destination,
                "count": count,
                "transmitted": _int(_find(rpc, ".//probe-count") or _find(rpc, ".//packets-transmitted")),
                "received": _int(_find(rpc, ".//packets-received")),
                "packet_loss_pct": _float(_find(rpc, ".//packet-loss")),
                "rtt_min_ms": _float(_find(rpc, ".//rtt-minimum")),
                "rtt_avg_ms": _float(_find(rpc, ".//rtt-average")),
                "rtt_max_ms": _float(_find(rpc, ".//rtt-maximum")),
            }
    except Exception as exc:
        return _err(device, exc, destination=destination)


@mcp.tool()
def get_traceroute(device: str, destination: str) -> dict:
    """Run traceroute from the device to a destination."""
    try:
        with _connect(device) as dev:
            rpc = dev.rpc.traceroute(host=destination, no_resolve=True)
            hops = []
            for hop in _findall(rpc, ".//traceroute-hop"):
                probes = []
                for probe in _findall(hop, "probe-result"):
                    probes.append({
                        "ip": _find(probe, "ip-address"),
                        "rtt_ms": _float(_find(probe, "rtt")),
                    })
                hops.append({
                    "ttl": _int(_find(hop, "ttl-value")),
                    "probes": probes,
                })
            return {"device": device, "destination": destination, "hops": hops}
    except Exception as exc:
        return _err(device, exc, destination=destination)


@mcp.tool()
def get_optics(device: str, interface: str) -> dict:
    """Get optical diagnostic info: Rx/Tx power levels in dBm."""
    try:
        with _connect(device) as dev:
            rpc = dev.rpc.get_interface_optics_diagnostics_information(
                interface_name=interface
            )
            diag = rpc.find(".//optics-diagnostics")
            if diag is None:
                return {"device": device, "interface": interface,
                        "error": "Optics data not available for this interface."}
            return {
                "device": device,
                "interface": interface,
                "rx_power_dbm": _float(_find(diag, ".//laser-rx-optical-power-dbm")),
                "tx_power_dbm": _float(_find(diag, ".//laser-output-power-dbm")),
                "module_temp_c": _float(_find(diag, ".//module-temperature")),
                "laser_bias_ma": _float(_find(diag, ".//laser-bias-current")),
            }
    except Exception as exc:
        return _err(device, exc, interface=interface)


@mcp.tool()
def run_command(device: str, command: str) -> dict:
    """Run an arbitrary show/diagnostic CLI command on a Junos device."""
    try:
        with _connect(device) as dev:
            output = dev.cli(command, warning=False)
            return {"device": device, "command": command, "output": output}
    except Exception as exc:
        return _err(device, exc, command=command)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    logger.info("Starting Junos MCP server on port %d", MCP_PORT)
    mcp.run(transport="sse")
