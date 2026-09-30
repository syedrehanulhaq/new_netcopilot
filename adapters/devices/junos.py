"""
Junos Device Adapter — async wrapper around PyEZ (junos-eznc / NETCONF).

Each public function mirrors a tool from the Junos MCP server but:
  - runs in a thread executor so the FastAPI event loop is never blocked
  - participates in the shared DevicePool circuit breaker
  - resolves device hostnames from adapters.devices.registry
  - reads credentials from secrets.get_secret()

Export:
    TOOLS   dict[str, callable]   maps tool base-name -> async function
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import contextmanager
from typing import Any, Callable, Optional

from credentials import get_secret
from adapters.devices.registry import get_device_info, resolve_host
from adapters.devices.pool import get_pool, CircuitOpenError
from adapters.devices.safety import UnsafeInputError, validate_cli_command
from adapters.devices.ssh_cache import SSHSessionCache
from adapters.devices.ssh_options import check_reachable, junos_host_key_params

logger = logging.getLogger("netcopilot.adapters.junos")

_EXECUTOR_TIMEOUT = 60.0


def _scrub_error(msg: str) -> str:
    msg = re.sub(r"(?i)(password|passwd|secret|token)\s*[=:]\s*\S+", r"\1=***", msg)
    msg = re.sub(r"Authentication failed.*", "Authentication failed.", msg)
    return msg


def _netconf_alive(dev) -> bool:
    conn = getattr(dev, "_conn", None)
    return bool(dev.connected and conn is not None and conn.connected)


# NETCONF sessions are reused between calls: opening one (SSH + NETCONF hello)
# costs ~2 s, the RPC itself well under 1 s. Exclusive checkout, as for SSH.
def _netconf_target(device_name: str) -> tuple[str, int]:
    """(host, NETCONF port): the device's netconf_port wins, then its port (NETCONF over SSH), then NETCONF_PORT."""
    host, port = resolve_host(device_name)
    netconf_port = int((get_device_info(device_name) or {}).get("netconf_port")
                       or port or get_secret("NETCONF_PORT", "830"))
    return host, netconf_port


_SESSIONS = SSHSessionCache("junos", alive=_netconf_alive, target=_netconf_target)


def _is_transport_error(exc: BaseException) -> bool:
    """The session itself failed (closed by the device, broken pipe), as opposed to an RPC error."""
    from jnpr.junos.exception import ConnectClosedError
    from ncclient.transport.errors import TransportError

    return isinstance(exc, (ConnectClosedError, TransportError, EOFError, OSError))


async def _run_tool(device: str, fn_callable, pool=None, **err_extra) -> dict:
    """Run callable in executor with circuit breaker, timeout, and error scrubbing."""
    _pool = pool or get_pool()
    try:
        async with _pool.acquire(device):
            loop = asyncio.get_event_loop()
            reused = _SESSIONS.idle_count(device) > 0
            try:
                result = await asyncio.wait_for(loop.run_in_executor(None, fn_callable), timeout=_EXECUTOR_TIMEOUT)
            except Exception as exc:
                if not (reused and _is_transport_error(exc)):
                    raise
                # A cached session had died (device closed it while idle): retry once on a new one.
                _SESSIONS.discard(device)
                result = await asyncio.wait_for(loop.run_in_executor(None, fn_callable), timeout=_EXECUTOR_TIMEOUT)
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
        return {"error": _scrub_error(str(exc)), "device": device, **err_extra}

# ---------------------------------------------------------------------------
# Internal helpers — XML / value parsing (synchronous, stateless)
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


# ---------------------------------------------------------------------------
# PyEZ connection helper (synchronous — runs inside executor)
# ---------------------------------------------------------------------------

@contextmanager
def _connect(device_name: str):
    """
    Resolve host, read credentials, yield a PyEZ Device (a cached NETCONF session when one is idle).

    Raises:
        ValueError   if device is not in the registry
        RuntimeError if credentials are missing or connection fails
    """
    host, netconf_port = _netconf_target(device_name)

    user = get_secret("JUNIPER_USER", "")
    passwd = get_secret("JUNIPER_PASS", "")
    if not user or not passwd:
        raise RuntimeError("JUNIPER_USER or JUNIPER_PASS not configured.")
    timeout = int(get_secret("NETCONF_TIMEOUT", "30"))

    def _open():
        from jnpr.junos import Device
        from jnpr.junos.exception import ConnectError

        check_reachable(host, netconf_port)
        dev = Device(
            host=host,
            port=netconf_port,
            user=user,
            passwd=passwd,
            auto_probe=0,
            gather_facts=False,
            conn_open_timeout=timeout,
            **junos_host_key_params(),
        )
        try:
            dev.open(auto_probe=0)
        except ConnectError as exc:
            raise RuntimeError(f"Cannot connect to {device_name} ({host}:{netconf_port}): {exc}") from exc
        return dev

    # Reused while healthy; closed (not returned to the cache) if the call raises.
    with _SESSIONS.session(device_name, _open) as dev:
        yield dev


# ---------------------------------------------------------------------------
# Synchronous tool implementations (run in executor)
# ---------------------------------------------------------------------------

def _sync_get_interface_health(device: str, interface: str) -> dict:
    """Get detailed interface health: admin/oper status, errors, counters, last flap."""
    with _connect(device) as dev:
        rpc = dev.rpc.get_interface_information(interface_name=interface, extensive=True)
        phys = rpc.find(".//physical-interface")
        if phys is None:
            return {"device": device, "interface": interface, "error": "Interface not found"}

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
            "raw_output": "interface_health_via_netconf",
        }


def _sync_get_interface_status(device: str) -> dict:
    """Get admin/oper status of all interfaces on the device."""
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


def _sync_get_interface_counters(device: str, interface: str) -> dict:
    """Get detailed interface error counters and traffic statistics."""
    with _connect(device) as dev:
        rpc = dev.rpc.get_interface_information(interface_name=interface, extensive=True)
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
            "raw_output": "interface_counters_via_netconf",
        }


def _sync_get_lldp_neighbor(device: str, interface: str) -> dict:
    """Get LLDP neighbor information for an interface."""
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
                "raw_output": "lldp_neighbor_via_netconf",
            }
        return {
            "device": device,
            "interface": interface,
            "error": "No LLDP neighbor found on this interface.",
        }


def _sync_get_bgp_neighbor_status(device: str, neighbor: str) -> dict:
    """Get BGP peer state, last event, last error, and route counts."""
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
            "raw_output": "bgp_neighbor_via_netconf",
        }


def _sync_get_bgp_summary(device: str) -> dict:
    """Get BGP summary table showing all peers and their states."""
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
            "raw_output": "bgp_summary_via_netconf",
        }


def _sync_get_ospf_neighbors(device: str) -> dict:
    """Get OSPF neighbor table with state and interface."""
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
        full = sum(1 for n in neighbors if n["state"].lower() == "full")
        return {
            "device": device,
            "neighbors": neighbors,
            "summary": {"total": len(neighbors), "full": full},
            "raw_output": "ospf_neighbors_via_netconf",
        }


def _sync_get_isis_neighbors(device: str) -> dict:
    """Get IS-IS adjacency table with state and circuit info."""
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
            "raw_output": "isis_neighbors_via_netconf",
        }


def _sync_get_isis_interface(device: str, interface: str) -> dict:
    """Get IS-IS interface details including level, metric, and hello state."""
    with _connect(device) as dev:
        rpc = dev.rpc.get_isis_interface_information(interface_name=interface)
        iface = rpc.find(".//isis-interface")
        if iface is None:
            return {"device": device, "interface": interface, "error": "ISIS interface not found"}
        return {
            "device": device,
            "interface": interface,
            "level": _find(iface, "level"),
            "passive": _find(iface, "isis-interface-passive"),
            "adjacency_count": _int(_find(iface, ".//adjacency-count")),
            "circuit_id": _find(iface, "isis-circuit-id"),
            "lsp_interval": _find(iface, "lsp-interval"),
            "raw_output": "isis_interface_via_netconf",
        }


def _sync_get_cpu_memory(device: str) -> dict:
    """Get routing engine CPU utilization and memory usage."""
    with _connect(device) as dev:
        rpc = dev.rpc.get_route_engine_information()
        engine = rpc.find(".//route-engine")
        if engine is None:
            return {"device": device, "error": "Route engine info not found"}

        cpu_idle_str = _find(engine, "cpu-idle")
        cpu_idle = _int(cpu_idle_str)
        # Tag names vary by RE type; classic and virtual REs only report memory-buffer-utilization.
        mem_util_str = (_find(engine, "memory-system-total-used-percent")
                        or _find(engine, "memory-system-total-util")
                        or _find(engine, "memory-buffer-utilization"))
        # cpu-idle of 0 means 100% busy — only a missing value is unknown
        cpu_percent = 100 - cpu_idle if cpu_idle_str else None
        mem_percent = _int(mem_util_str) if mem_util_str else None
        dram = re.match(r"\s*(\d+)\s*MB", _find(engine, "memory-dram-size"))

        summary = f"CPU {cpu_percent}% used" if cpu_percent is not None else "Route engine data retrieved"
        if mem_percent is not None:
            summary += f", memory {mem_percent}% used"
        return {
            "device": device,
            "model": _find(engine, "model"),
            "uptime": _find(engine, "up-time") or _find(engine, "uptime"),
            "cpu_percent": cpu_percent,
            "cpu_idle_percent": cpu_idle,
            "memory_used_percent": mem_percent,
            "memory_total_mb": int(dram.group(1)) if dram else (_int(_find(engine, "memory-system-total")) // 1024 or None),
            "temperature_c": _re_temperature_c(engine),
            "last_reboot_reason": _find(engine, "last-reboot-reason"),
            "summary": summary,
        }


def _re_temperature_c(engine) -> Optional[float]:
    """RE temperature in °C: <temperature celsius="36">36 degrees C / 96 degrees F</temperature>; None when absent (e.g. vMX)."""
    node = engine.find("temperature")
    if node is None:
        return None
    match = re.match(r"\s*(-?\d+(?:\.\d+)?)", node.get("celsius") or node.text or "")
    return float(match.group(1)) if match else None


def _sync_get_chassis_health(device: str) -> dict:
    """Get chassis health: FPC/PIC states, alarms, and hardware inventory."""
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
        except Exception as exc:
            result["fpcs_error"] = str(exc)
            result["fpcs"] = []

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
            result["chassis_alarms"] = []
            result["has_alarms"] = False

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

        result["summary"] = (
            f"{len(result.get('chassis_alarms', []))} chassis alarm(s), "
            f"{len(result.get('fpcs', []))} FPC(s) found"
        )
        return result


def _sync_get_route_lookup(device: str, prefix: str) -> dict:
    """Look up routing table entry for a prefix."""
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
            "summary": f"{len(routes)} route(s) found for {prefix}",
        }


def _sync_get_bgp_route(device: str, prefix: str) -> dict:
    """Look up BGP routing table entry for a prefix."""
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
            "summary": f"{len(routes)} BGP route(s) found for {prefix}",
        }


def _sync_get_acl_hits(device: str, acl_name: str) -> dict:
    """Get firewall filter (ACL) term counters and hit counts."""
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
            "summary": f"Filter '{acl_name}': {len(terms)} term(s)",
        }


def _sync_get_mtu_info(device: str, interface: str) -> dict:
    """Get MTU configuration and details for an interface."""
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
            "summary": f"Physical MTU: {mtu}, logical MTU: {logical_mtu or 'N/A'}",
        }


def _sync_get_ping(device: str, destination: str, count: int = 5) -> dict:
    """Run ping from the device to a destination."""
    with _connect(device) as dev:
        rpc = dev.rpc.ping(host=destination, count=str(count), no_resolve=True)
        transmitted = _int(
            _find(rpc, ".//packets-transmitted") or _find(rpc, ".//probe-count")
        )
        received = _int(_find(rpc, ".//packets-received"))
        loss_pct = _float(_find(rpc, ".//packet-loss"))
        return {
            "device": device,
            "destination": destination,
            "count": count,
            "transmitted": transmitted,
            "received": received,
            "packet_loss_pct": loss_pct,
            "rtt_min_ms": _float(_find(rpc, ".//rtt-minimum")),
            "rtt_avg_ms": _float(_find(rpc, ".//rtt-average")),
            "rtt_max_ms": _float(_find(rpc, ".//rtt-maximum")),
            "summary": f"Ping {destination}: {received}/{transmitted} received, {loss_pct}% loss",
        }


def _sync_get_traceroute(device: str, destination: str) -> dict:
    """Run traceroute from the device to a destination."""
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
        return {
            "device": device,
            "destination": destination,
            "hops": hops,
            "summary": f"Traceroute to {destination}: {len(hops)} hop(s)",
        }


def _sync_get_optics(device: str, interface: str) -> dict:
    """Get optical diagnostic info: Rx/Tx power levels in dBm."""
    with _connect(device) as dev:
        rpc = dev.rpc.get_interface_optics_diagnostics_information(
            interface_name=interface
        )
        diag = rpc.find(".//optics-diagnostics")
        if diag is None:
            return {
                "device": device,
                "interface": interface,
                "error": "Optics data not available for this interface.",
            }
        rx = _float(_find(diag, ".//laser-rx-optical-power-dbm"))
        tx = _float(_find(diag, ".//laser-output-power-dbm"))
        return {
            "device": device,
            "interface": interface,
            "rx_power_dbm": rx,
            "tx_power_dbm": tx,
            "module_temp_c": _float(_find(diag, ".//module-temperature")),
            "laser_bias_ma": _float(_find(diag, ".//laser-bias-current")),
            "summary": f"Optics {interface}: Rx {rx} dBm, Tx {tx} dBm",
        }


def _sync_get_recent_changes(device: str, count: int = 10) -> dict:
    """Commit history (show system commit)."""
    with _connect(device) as dev:
        rpc = dev.rpc.get_commit_information()
        commits = []
        for entry in _findall(rpc, ".//commit-history")[: max(1, min(int(count), 50))]:
            commits.append({
                "sequence": _find(entry, "sequence-number"),
                "user": _find(entry, "user"),
                "client": _find(entry, "client"),
                "time": _find(entry, "date-time"),
                "comment": _find(entry, "comment") or _find(entry, "log"),
            })
        result: dict[str, Any] = {"device": device, "changes": commits}
        if commits:
            result["last_change"] = commits[0]
            result["summary"] = f"{len(commits)} recent commit(s); latest by {commits[0]['user']} at {commits[0]['time']}"
        return result


def _sync_get_config_diff(device: str, rollback: int = 1) -> dict:
    """Diff of the active configuration against rollback N (show configuration | compare rollback N)."""
    rollback = int(rollback)
    if not 1 <= rollback <= 49:
        return {"device": device, "error": "rollback must be between 1 and 49."}
    with _connect(device) as dev:
        # get_config takes an options dict; the raw get_configuration RPC rejects one.
        rpc = dev.rpc.get_config(options={"compare": "rollback", "rollback": str(rollback), "format": "text"})
        node = rpc.find(".//configuration-output")
        diff = node.text if node is not None and node.text else ""
        return {
            "device": device,
            "compared_to": f"rollback {rollback}",
            "diff": diff.strip() or "(no differences)",
        }


def _sync_run_command(device: str, command: str) -> dict:
    """Run an arbitrary show/diagnostic CLI command on a Junos device."""
    with _connect(device) as dev:
        output = dev.cli(command, warning=False)
        return {
            "device": device,
            "command": command,
            "output": output,
            "raw_output": output,
        }


# ---------------------------------------------------------------------------
# Async public API — each wraps the sync impl with pool + circuit breaker
# ---------------------------------------------------------------------------

async def get_interface_health(device: str, interface: str, pool=None) -> dict:
    """Get detailed interface health: admin/oper status, errors, counters, last flap."""
    return await _run_tool(device, lambda: _sync_get_interface_health(device, interface), pool=pool)


async def get_interface_status(device: str, pool=None) -> dict:
    """Get admin/oper status of all interfaces on the device."""
    return await _run_tool(device, lambda: _sync_get_interface_status(device), pool=pool)


async def get_interface_counters(device: str, interface: str, pool=None) -> dict:
    """Get detailed interface error counters and traffic statistics."""
    return await _run_tool(device, lambda: _sync_get_interface_counters(device, interface), pool=pool)


async def get_lldp_neighbor(device: str, interface: str, pool=None) -> dict:
    """Get LLDP neighbor information for an interface."""
    return await _run_tool(device, lambda: _sync_get_lldp_neighbor(device, interface), pool=pool)


async def get_bgp_neighbor_status(device: str, neighbor: str, pool=None) -> dict:
    """Get BGP peer state, last event, last error, and route counts."""
    return await _run_tool(device, lambda: _sync_get_bgp_neighbor_status(device, neighbor), pool=pool)


async def get_bgp_summary(device: str, pool=None) -> dict:
    """Get BGP summary table showing all peers and their states."""
    return await _run_tool(device, lambda: _sync_get_bgp_summary(device), pool=pool)


async def get_ospf_neighbors(device: str, pool=None) -> dict:
    """Get OSPF neighbor table with state and interface."""
    return await _run_tool(device, lambda: _sync_get_ospf_neighbors(device), pool=pool)


async def get_isis_neighbors(device: str, pool=None) -> dict:
    """Get IS-IS adjacency table with state and circuit info."""
    return await _run_tool(device, lambda: _sync_get_isis_neighbors(device), pool=pool)


async def get_isis_interface(device: str, interface: str, pool=None) -> dict:
    """Get IS-IS interface details including level, metric, and hello state."""
    return await _run_tool(device, lambda: _sync_get_isis_interface(device, interface), pool=pool)


async def get_cpu_memory(device: str, pool=None) -> dict:
    """Get routing engine CPU utilization and memory usage."""
    return await _run_tool(device, lambda: _sync_get_cpu_memory(device), pool=pool)


async def get_chassis_health(device: str, pool=None) -> dict:
    """Get chassis health: FPC/PIC states, alarms, and hardware inventory."""
    return await _run_tool(device, lambda: _sync_get_chassis_health(device), pool=pool)


async def get_route_lookup(device: str, prefix: str, pool=None) -> dict:
    """Look up routing table entry for a prefix."""
    return await _run_tool(device, lambda: _sync_get_route_lookup(device, prefix), pool=pool)


async def get_bgp_route(device: str, prefix: str, pool=None) -> dict:
    """Look up BGP routing table entry for a prefix."""
    return await _run_tool(device, lambda: _sync_get_bgp_route(device, prefix), pool=pool)


async def get_acl_hits(device: str, acl_name: str, pool=None) -> dict:
    """Get firewall filter (ACL) term counters and hit counts."""
    return await _run_tool(device, lambda: _sync_get_acl_hits(device, acl_name), pool=pool)


async def get_mtu_info(device: str, interface: str, pool=None) -> dict:
    """Get MTU configuration and details for an interface."""
    return await _run_tool(device, lambda: _sync_get_mtu_info(device, interface), pool=pool)


async def get_ping(device: str, destination: str, count: int = 5, pool=None) -> dict:
    """Run ping from the device to a destination."""
    return await _run_tool(device, lambda: _sync_get_ping(device, destination, count), pool=pool)


async def get_traceroute(device: str, destination: str, pool=None) -> dict:
    """Run traceroute from the device to a destination."""
    return await _run_tool(device, lambda: _sync_get_traceroute(device, destination), pool=pool)


async def get_optics(device: str, interface: str, pool=None) -> dict:
    """Get optical diagnostic info: Rx/Tx power levels in dBm."""
    return await _run_tool(device, lambda: _sync_get_optics(device, interface), pool=pool)


async def get_recent_changes(device: str, count: int = 10, pool=None) -> dict:
    """List recent configuration commits (show system commit)."""
    return await _run_tool(device, lambda: _sync_get_recent_changes(device, count), pool=pool)


async def get_config_diff(device: str, rollback: int = 1, pool=None) -> dict:
    """Configuration diff against rollback N (default 1 = what the last commit changed)."""
    return await _run_tool(device, lambda: _sync_get_config_diff(device, rollback), pool=pool)


async def run_command(device: str, command: str, pool=None) -> dict:
    """Run a read-only show/ping/traceroute CLI command on a Junos device."""
    try:
        command = validate_cli_command("junos", command)
    except UnsafeInputError as exc:
        return {"device": device, "command": command, "error": f"Command rejected: {exc}"}
    return await _run_tool(device, lambda: _sync_run_command(device, command), pool=pool)


# ---------------------------------------------------------------------------
# TOOLS export — maps tool base-name to async function
# Allows registry-style dispatch: TOOLS["get_cpu_memory"](device="pe1")
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
    "get_optics": get_optics,
    "get_recent_changes": get_recent_changes,
    "get_config_diff": get_config_diff,
    "run_command": run_command,
}

# ---------------------------------------------------------------------------
# OpenAI-format tool schemas — consumed by ToolDispatcher.get_all_tools()
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "junos_get_recent_changes",
            "description": "List recent configuration commits on a Juniper Junos device (who, when, how). "
                           "Use this to check whether a change preceded an incident.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "count": {"type": "integer", "default": 10, "description": "Number of commits (max 50)"},
                },
                "required": ["device"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "junos_get_config_diff",
            "description": "Show what changed in the configuration compared with rollback N on a Juniper Junos device (default 1 = the last commit). Secrets are redacted.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "rollback": {"type": "integer", "default": 1, "description": "Rollback number 1-49"},
                },
                "required": ["device"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "junos_get_interface_health",
            "description": "Get Juniper Junos interface admin/oper status, errors, counters, and last flap.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string", "description": "Device name from inventory"},
                    "interface": {"type": "string", "description": "Interface name, e.g. xe-0/0/0"},
                },
                "required": ["device", "interface"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "junos_get_interface_status",
            "description": "List all Juniper Junos interfaces with admin/oper state and description.",
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
            "name": "junos_get_interface_counters",
            "description": "Get traffic counters (input/output rates, errors) for a Juniper Junos interface.",
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
            "name": "junos_get_lldp_neighbor",
            "description": "Get LLDP neighbor connected to a Juniper Junos interface.",
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
            "name": "junos_get_bgp_neighbor_status",
            "description": "Get BGP session state, last reset reason, and prefix counts for a Juniper Junos peer.",
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
            "name": "junos_get_bgp_summary",
            "description": "Get BGP summary table (all peers) from a Juniper Junos device.",
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
            "name": "junos_get_ospf_neighbors",
            "description": "Get OSPF neighbor adjacency table from a Juniper Junos device.",
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
            "name": "junos_get_isis_neighbors",
            "description": "Get IS-IS adjacency table from a Juniper Junos device.",
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
            "name": "junos_get_isis_interface",
            "description": "Get IS-IS interface state and configuration on a Juniper Junos device.",
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
            "name": "junos_get_cpu_memory",
            "description": "Get CPU utilization and memory usage from a Juniper Junos device.",
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
            "name": "junos_get_chassis_health",
            "description": "Get chassis alarms and system health from a Juniper Junos device.",
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
            "name": "junos_get_route_lookup",
            "description": "Look up a prefix in the Juniper Junos routing table.",
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
            "name": "junos_get_bgp_route",
            "description": "Get BGP path attributes for a prefix on a Juniper Junos device.",
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
            "name": "junos_get_acl_hits",
            "description": "Get hit counters for a firewall filter (ACL) on a Juniper Junos device.",
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
            "name": "junos_get_mtu_info",
            "description": "Get MTU configuration for a Juniper Junos interface.",
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
            "name": "junos_get_ping",
            "description": "Run a ping from a Juniper Junos device to a destination.",
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
            "name": "junos_get_traceroute",
            "description": "Run a traceroute from a Juniper Junos device to a destination.",
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
            "name": "junos_get_optics",
            "description": "Get optical transceiver power levels (Rx/Tx dBm) for a Juniper Junos interface.",
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
            "name": "junos_run_command",
            "description": "Run an arbitrary read-only CLI command on a Juniper Junos device.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "command": {"type": "string", "description": "CLI command to run"},
                },
                "required": ["device", "command"],
            },
        },
    },
]
