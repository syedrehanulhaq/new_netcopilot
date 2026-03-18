"""
Cisco IOS XR Device Adapter — direct async wrapper for NetCopilot.

Each public function runs a sync Netmiko SSH call inside an executor,
participates in the DevicePool circuit breaker, and resolves credentials
via secrets.get_secret().

Export:
    TOOLS        dict[str, callable]   base-name → async function
    TOOL_SCHEMAS list[dict]            OpenAI function-call schemas (cisco_ prefixed)
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable

from credentials import get_secret
from adapters.devices.registry import resolve_host
from adapters.devices.pool import get_pool, CircuitOpenError, DevicePool

logger = logging.getLogger("netcopilot.adapters.cisco")

# ---------------------------------------------------------------------------
# SSH connection cache — reuse sessions instead of reconnecting each tool call
# ---------------------------------------------------------------------------

_SSH_TTL = 90.0          # seconds to keep an idle connection alive
_SSH_MAX_CACHED = 20     # max cached connections across all devices

_conn_cache: dict[str, tuple[Any, float]] = {}  # device → (conn, last_used)
_conn_lock = threading.Lock()


def _get_cached_ssh(device_name: str):
    """Return a live cached Netmiko connection, or None if not available."""
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
            # Evict the oldest entry
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


@contextmanager
def _ssh(device_name: str):
    """
    Yield a Netmiko SSH session, reusing a cached connection when available.
    On exception the connection is evicted so the next call reconnects fresh.
    """
    from netmiko import ConnectHandler

    conn = _get_cached_ssh(device_name)
    fresh = conn is None

    if fresh:
        host, port = resolve_host(device_name)
        user = get_secret("CISCO_USER", "")
        passwd = get_secret("CISCO_PASS", "")
        if not user or not passwd:
            raise RuntimeError("CISCO_USER or CISCO_PASS not configured.")

        params: dict[str, Any] = {
            "device_type": "cisco_xr",
            "host": host,
            "username": user,
            "password": passwd,
            "fast_cli": True,
            "conn_timeout": int(get_secret("NETCONF_TIMEOUT", "30")),
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
        if not fresh:
            # Cached connection may have gone stale — retry once with a fresh one
            raise
        raise


def _cli(device: str, command: str) -> str:
    """Run a CLI command via SSH, return raw output string."""
    with _ssh(device) as conn:
        return conn.send_command(command)


_EXECUTOR_TIMEOUT = 60.0  # seconds before an SSH call is considered hung


def _scrub_error(msg: str) -> str:
    """Strip potential credentials from exception text before logging/returning."""
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
# Parsing helpers
# ---------------------------------------------------------------------------

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
# Synchronous implementations (executor targets)
# ---------------------------------------------------------------------------

def _sync_get_interface_health(device: str, interface: str) -> dict:
    # Reuse a single SSH session for both commands
    with _ssh(device) as conn:
        output = conn.send_command(f"show interface {interface}")
        parsed = _parse_show_interface(output)
        parsed["raw_output"] = output
        try:
            ctrl = conn.send_command(f"show controllers {interface}")
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


def _sync_get_interface_status(device: str) -> dict:
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
        "raw_output": output,
    }


def _sync_get_interface_counters(device: str, interface: str) -> dict:
    output = _cli(device, f"show interface {interface}")
    parsed = _parse_show_interface(output)
    parsed["raw_output"] = output
    return {"device": device, "interface": interface, **parsed}


def _sync_get_lldp_neighbor(device: str, interface: str) -> dict:
    output = _cli(device, f"show lldp neighbors interface {interface} detail")
    parsed: dict[str, Any] = {"raw_output": output}
    for line in output.splitlines():
        ll = line.strip().lower()
        if ll.startswith("system name"):
            parsed["neighbor_device"] = line.split(":", 1)[1].strip()
        elif ll.startswith("port id"):
            parsed["neighbor_interface"] = line.split(":", 1)[1].strip()
        elif ll.startswith("port description") and "neighbor_interface" not in parsed:
            parsed["neighbor_interface"] = line.split(":", 1)[1].strip()
    if "neighbor_device" in parsed or "neighbor_interface" in parsed:
        return {"device": device, "interface": interface, **parsed}
    return {
        "device": device, "interface": interface,
        "error": "No LLDP neighbor found on this interface.",
        "raw_output": output,
    }


def _sync_get_bgp_neighbor_status(device: str, neighbor: str) -> dict:
    output = _cli(device, f"show bgp neighbor {neighbor}")
    parsed: dict[str, Any] = {"device": device, "neighbor": neighbor, "raw_output": output}
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


def _sync_get_bgp_summary(device: str) -> dict:
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
        "raw_output": output,
    }


def _sync_get_ospf_neighbors(device: str) -> dict:
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
        "raw_output": output,
    }


def _sync_get_isis_neighbors(device: str) -> dict:
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
        "raw_output": output,
    }


def _sync_get_isis_interface(device: str, interface: str) -> dict:
    output = _cli(device, f"show isis interface {interface}")
    return {"device": device, "interface": interface, "output": output, "raw_output": output}


def _sync_get_cpu_memory(device: str) -> dict:
    mem_out = _cli(device, "show memory summary")
    result: dict[str, Any] = {"device": device, "mem_raw": mem_out, "raw_output": mem_out}

    # IOS XR CPU — command varies by version
    cpu_out = ""
    for cpu_cmd in [
        "show processes cpu",    # IOS XR 6.x/7.x standard
        "show cpu",              # some IOS XR variants
        "show processes brief",  # fallback — no CPU % but shows load
    ]:
        try:
            cpu_out = _cli(device, cpu_cmd)
            if "Invalid input" not in cpu_out and "No such process" not in cpu_out:
                result["cpu_raw"] = cpu_out
                break
        except Exception:
            continue

    # IOS XR: "CPU utilization for five seconds:  3%/1%; one minute:  2%; five minutes:  2%"
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
        m2b = re.search(r"Physical Memory:\s+(\d+)K\((\d+)K used\)", mem_out, re.IGNORECASE)
        if m2b:
            total_kb = int(m2b.group(1))
            used_kb = int(m2b.group(2))
            result["mem_total_mb"] = total_kb // 1024
            result["mem_used_mb"] = used_kb // 1024
            result["mem_free_mb"] = (total_kb - used_kb) // 1024
            result["mem_used_percent"] = round(used_kb / total_kb * 100, 1) if total_kb else None

    return result


def _sync_get_route_lookup(device: str, prefix: str) -> dict:
    output = _cli(device, f"show route {prefix}")
    return {"device": device, "prefix": prefix, "output": output, "raw_output": output}


def _sync_get_bgp_route(device: str, prefix: str) -> dict:
    output = _cli(device, f"show bgp {prefix}")
    return {"device": device, "prefix": prefix, "output": output, "raw_output": output}


def _sync_get_acl_hits(device: str, acl_name: str) -> dict:
    output = _cli(device, f"show access-lists {acl_name}")
    return {"device": device, "acl_name": acl_name, "output": output, "raw_output": output}


def _sync_get_mtu_info(device: str, interface: str) -> dict:
    output = _cli(device, f"show interface {interface}")
    mtu = None
    m = re.search(r"MTU\s+(\d+)\s+bytes", output)
    if m:
        mtu = int(m.group(1))
    return {"device": device, "interface": interface, "mtu": mtu, "output": output, "raw_output": output}


def _sync_get_ping(device: str, destination: str, count: int = 5) -> dict:
    output = _cli(device, f"ping {destination} count {count}")
    m = re.search(r"(\d+)/(\d+).*?(\d+)%", output)
    received = int(m.group(1)) if m else None
    sent = int(m.group(2)) if m else None
    loss_pct = int(m.group(3)) if m else None
    return {
        "device": device, "destination": destination, "count": count,
        "received": received, "sent": sent, "packet_loss_pct": loss_pct,
        "output": output, "raw_output": output,
    }


def _sync_get_traceroute(device: str, destination: str) -> dict:
    output = _cli(device, f"traceroute {destination}")
    return {"device": device, "destination": destination, "output": output, "raw_output": output}


def _sync_get_chassis_health(device: str) -> dict:
    alarms = _cli(device, "show alarms")
    platform = _cli(device, "show platform")
    return {
        "device": device, "alarms": alarms, "platform": platform,
        "raw_output": f"=== show alarms ===\n{alarms}\n\n=== show platform ===\n{platform}",
    }


def _sync_run_command(device: str, command: str) -> dict:
    output = _cli(device, command)
    return {"device": device, "command": command, "output": output, "raw_output": output}


# ---------------------------------------------------------------------------
# Async public API — each wraps a sync impl inside the circuit breaker
# ---------------------------------------------------------------------------

def _make_async(sync_fn, *extra_keys):
    """Factory that returns an async wrapper for a sync tool function."""
    async def wrapper(**kwargs) -> dict:
        device = kwargs.get("device", "")
        _pool = get_pool()
        try:
            async with _pool.acquire(device):
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, lambda: sync_fn(**kwargs))
                _pool.record_success(device)
                return result
        except CircuitOpenError as exc:
            return {"error": str(exc), "device": device}
        except Exception as exc:
            _pool.record_failure(device, str(exc))
            return _err(device, exc)
    wrapper.__name__ = sync_fn.__name__.lstrip("_sync")
    return wrapper


async def get_interface_health(device: str, interface: str) -> dict:
    """Get interface admin/oper status, errors, counters, and last flap."""
    return await _run_tool(device, _sync_get_interface_health, device, interface, interface=interface)


async def get_interface_status(device: str) -> dict:
    """Get admin/oper status of all interfaces on the device."""
    return await _run_tool(device, _sync_get_interface_status, device)


async def get_interface_counters(device: str, interface: str) -> dict:
    """Get traffic counters and error statistics for an interface."""
    return await _run_tool(device, _sync_get_interface_counters, device, interface, interface=interface)


async def get_lldp_neighbor(device: str, interface: str) -> dict:
    """Get LLDP neighbor on an interface."""
    return await _run_tool(device, _sync_get_lldp_neighbor, device, interface, interface=interface)


async def get_bgp_neighbor_status(device: str, neighbor: str) -> dict:
    """Get BGP peer state, last error, and prefix counts."""
    return await _run_tool(device, _sync_get_bgp_neighbor_status, device, neighbor, neighbor=neighbor)


async def get_bgp_summary(device: str) -> dict:
    """Get BGP summary table for all peers."""
    return await _run_tool(device, _sync_get_bgp_summary, device)


async def get_ospf_neighbors(device: str) -> dict:
    """Get OSPF neighbor table."""
    return await _run_tool(device, _sync_get_ospf_neighbors, device)


async def get_isis_neighbors(device: str) -> dict:
    """Get IS-IS adjacency table."""
    return await _run_tool(device, _sync_get_isis_neighbors, device)


async def get_isis_interface(device: str, interface: str) -> dict:
    """Get IS-IS interface configuration and state."""
    return await _run_tool(device, _sync_get_isis_interface, device, interface, interface=interface)


async def get_cpu_memory(device: str) -> dict:
    """Get CPU utilization and memory usage summary."""
    return await _run_tool(device, _sync_get_cpu_memory, device)


async def get_route_lookup(device: str, prefix: str) -> dict:
    """Look up routing table entry for a prefix."""
    return await _run_tool(device, _sync_get_route_lookup, device, prefix, prefix=prefix)


async def get_bgp_route(device: str, prefix: str) -> dict:
    """Look up BGP routing table entry for a prefix."""
    return await _run_tool(device, _sync_get_bgp_route, device, prefix, prefix=prefix)


async def get_acl_hits(device: str, acl_name: str) -> dict:
    """Get ACL (access-list) hit counters."""
    return await _run_tool(device, _sync_get_acl_hits, device, acl_name, acl_name=acl_name)


async def get_mtu_info(device: str, interface: str) -> dict:
    """Get MTU configuration for an interface."""
    return await _run_tool(device, _sync_get_mtu_info, device, interface, interface=interface)


async def get_ping(device: str, destination: str, count: int = 5) -> dict:
    """Run ping from the device to a destination."""
    return await _run_tool(device, _sync_get_ping, device, destination, count, destination=destination)


async def get_traceroute(device: str, destination: str) -> dict:
    """Run traceroute from the device to a destination."""
    return await _run_tool(device, _sync_get_traceroute, device, destination, destination=destination)


async def get_chassis_health(device: str) -> dict:
    """Get chassis alarms and system health summary."""
    return await _run_tool(device, _sync_get_chassis_health, device)


async def run_command(device: str, command: str) -> dict:
    """Run an arbitrary show/diagnostic CLI command on the device."""
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
    "get_route_lookup": get_route_lookup,
    "get_bgp_route": get_bgp_route,
    "get_acl_hits": get_acl_hits,
    "get_mtu_info": get_mtu_info,
    "get_ping": get_ping,
    "get_traceroute": get_traceroute,
    "get_chassis_health": get_chassis_health,
    "run_command": run_command,
}

# ---------------------------------------------------------------------------
# OpenAI-format tool schemas — consumed by ToolDispatcher.get_all_tools()
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "cisco_get_interface_health",
            "description": "Get Cisco IOS XR interface admin/oper status, error counters, last flap time, and optical power.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string", "description": "Device name from inventory"},
                    "interface": {"type": "string", "description": "Full interface name, e.g. GigabitEthernet0/0/0/0"},
                },
                "required": ["device", "interface"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cisco_get_interface_status",
            "description": "List all Cisco IOS XR interfaces with admin/oper state and description.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string", "description": "Device name from inventory"},
                },
                "required": ["device"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cisco_get_interface_counters",
            "description": "Get traffic counters (input/output rates, errors) for a Cisco IOS XR interface.",
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
            "name": "cisco_get_lldp_neighbor",
            "description": "Get LLDP neighbor connected to a Cisco IOS XR interface.",
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
            "name": "cisco_get_bgp_neighbor_status",
            "description": "Get BGP session state, last reset reason, and prefix counts for a Cisco IOS XR peer.",
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
            "name": "cisco_get_bgp_summary",
            "description": "Get BGP summary table (all peers) from a Cisco IOS XR device.",
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
            "name": "cisco_get_ospf_neighbors",
            "description": "Get OSPF neighbor adjacency table from a Cisco IOS XR device.",
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
            "name": "cisco_get_isis_neighbors",
            "description": "Get IS-IS adjacency table from a Cisco IOS XR device.",
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
            "name": "cisco_get_isis_interface",
            "description": "Get IS-IS interface state and configuration on a Cisco IOS XR device.",
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
            "name": "cisco_get_cpu_memory",
            "description": "Get CPU utilization (5s/1m/5m) and memory usage from a Cisco IOS XR device.",
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
            "name": "cisco_get_route_lookup",
            "description": "Look up a prefix in the Cisco IOS XR routing table.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "prefix": {"type": "string", "description": "IP prefix or address to look up"},
                },
                "required": ["device", "prefix"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cisco_get_bgp_route",
            "description": "Get BGP path attributes for a prefix on a Cisco IOS XR device.",
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
            "name": "cisco_get_acl_hits",
            "description": "Get hit counters for an ACL (access-list) on a Cisco IOS XR device.",
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
            "name": "cisco_get_mtu_info",
            "description": "Get MTU configuration for a Cisco IOS XR interface.",
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
            "name": "cisco_get_ping",
            "description": "Run a ping from a Cisco IOS XR device to a destination.",
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
            "name": "cisco_get_traceroute",
            "description": "Run a traceroute from a Cisco IOS XR device to a destination.",
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
            "name": "cisco_get_chassis_health",
            "description": "Get chassis alarms and platform health from a Cisco IOS XR device.",
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
            "name": "cisco_run_command",
            "description": "Run an arbitrary read-only CLI command on a Cisco IOS XR device.",
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
