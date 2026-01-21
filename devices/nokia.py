import re
import threading
import time
from typing import Any, Dict, Optional, Tuple

from netmiko import ConnectHandler

from config import NOKIA_DEVICES, NOKIA_PASS, NOKIA_USER, NETCONF_PORT, NETCONF_TIMEOUT

_SSH_POOL_LOCK = threading.Lock()
_SSH_POOL_TTL = 60
_SSH_POOL: Dict[str, tuple[Any, float]] = {}


def _get_ssh_connection(key: str, device_params: Dict[str, Any]):
    now = time.time()
    with _SSH_POOL_LOCK:
        conn_tuple = _SSH_POOL.get(key)
        if conn_tuple:
            conn, last_used = conn_tuple
            if now - last_used < _SSH_POOL_TTL and conn.is_alive():
                _SSH_POOL[key] = (conn, now)
                return conn
            try:
                conn.disconnect()
            except Exception:
                pass
            _SSH_POOL.pop(key, None)

        conn = ConnectHandler(**device_params)
        _SSH_POOL[key] = (conn, now)
        return conn


def _touch_ssh_connection(key: str) -> None:
    now = time.time()
    with _SSH_POOL_LOCK:
        conn_tuple = _SSH_POOL.get(key)
        if conn_tuple:
            conn, _ = conn_tuple
            _SSH_POOL[key] = (conn, now)


def _resolve_host_port(device: str) -> Tuple[str, Optional[int]]:
    host = NOKIA_DEVICES[device]
    if ":" in host:
        base, port = host.rsplit(":", 1)
        if port.isdigit():
            return base, int(port)
    return host, None


def _run_command(device: str, command: str) -> Dict[str, Any]:
    if device not in NOKIA_DEVICES:
        return {
            "device": device,
            "error": f"Unknown device '{device}'. Known devices: {list(NOKIA_DEVICES.keys())}",
        }

    if not NOKIA_USER or not NOKIA_PASS:
        return {
            "device": device,
            "error": "NOKIA_USER or NOKIA_PASS not set in environment.",
        }

    host, port = _resolve_host_port(device)
    key = f"{host}:{port or 22}"
    try:
        device_params = {
            "device_type": "nokia_sros",
            "host": host,
            "username": NOKIA_USER,
            "password": NOKIA_PASS,
            "fast_cli": False,
        }
        if port:
            device_params["port"] = port
        conn = _get_ssh_connection(key, device_params)
        output = conn.send_command(command)
        _touch_ssh_connection(key)
        return {"device": device, "command": command, "output": output}
    except Exception as exc:
        return {
            "device": device,
            "command": command,
            "error": f"Error connecting to {host}{f':{port}' if port else ''}: {str(exc)}",
        }


def _run_netconf_rpc(device: str, rpc: str) -> Dict[str, Any]:
    try:
        from ncclient import manager
    except Exception:
        return {
            "device": device,
            "error": "ncclient not installed. NETCONF for Nokia is unavailable.",
        }

    if device not in NOKIA_DEVICES:
        return {
            "device": device,
            "error": f"Unknown device '{device}'. Known devices: {list(NOKIA_DEVICES.keys())}",
        }

    if not NOKIA_USER or not NOKIA_PASS:
        return {
            "device": device,
            "error": "NOKIA_USER or NOKIA_PASS not set in environment.",
        }

    host, port = _resolve_host_port(device)
    port = port or NETCONF_PORT
    try:
        with manager.connect(
            host=host,
            port=port,
            username=NOKIA_USER,
            password=NOKIA_PASS,
            hostkey_verify=False,
            timeout=NETCONF_TIMEOUT,
            device_params={"name": "nokia_sros"},
        ) as mgr:
            result = mgr.dispatch(rpc)
            return {
                "device": device,
                "netconf": True,
                "rpc": rpc,
                "raw_xml": str(result),
            }
    except Exception as exc:
        return {
            "device": device,
            "rpc": rpc,
            "error": f"NETCONF error connecting to {host}:{port}: {str(exc)}",
        }


def _parse_port_state(output: str) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {}
    for line in output.splitlines():
        if "Admin State" in line:
            match = re.search(r"Admin State\s*:\s*(\S+)", line, re.IGNORECASE)
            if match:
                parsed["admin_state"] = match.group(1)
        if "Oper State" in line:
            match = re.search(r"Oper State\s*:\s*(\S+)", line, re.IGNORECASE)
            if match:
                parsed["oper_state"] = match.group(1)
        if "Last State Change" in line:
            parsed["last_change"] = line.split(":", 1)[-1].strip()
        if "MTU" in line and "MTU" not in parsed:
            match = re.search(r"MTU\s*:\s*(\d+)", line, re.IGNORECASE)
            if match:
                parsed["mtu"] = int(match.group(1))
    return parsed


def get_interface_health(device: str, interface: str) -> Dict[str, Any]:
    # NETCONF support to be added once Nokia YANG/RPCs are confirmed.
    port = _run_command(device, f"show port {interface}")
    router_if = _run_command(device, f"show router interface {interface}")
    parsed = {}
    if "output" in port:
        parsed = _parse_port_state(port["output"])
    return {
        "device": device,
        "interface": interface,
        "port": port,
        "router_interface": router_if,
        "parsed_interface": parsed,
    }


def get_interface_status(device: str) -> Dict[str, Any]:
    return _run_command(device, "show port")


def get_interface_counters(device: str, interface: str) -> Dict[str, Any]:
    return _run_command(device, f"show port {interface}")


def get_lldp_neighbor(device: str, interface: str) -> Dict[str, Any]:
    result = _run_command(device, "show system lldp neighbor")
    if "output" not in result:
        return result
    neighbor_device = None
    neighbor_interface = None
    for line in result["output"].splitlines():
        if interface in line:
            parts = re.split(r"\s+", line.strip())
            if len(parts) >= 3:
                neighbor_interface = parts[-1]
                neighbor_device = parts[-2]
                break
    result["neighbor_device"] = neighbor_device
    result["neighbor_interface"] = neighbor_interface
    return result


def get_bgp_neighbor_status(device: str, neighbor: str) -> Dict[str, Any]:
    return _run_command(device, f"show router bgp neighbor {neighbor}")


def get_isis_neighbors(device: str) -> Dict[str, Any]:
    return _run_command(device, "show router isis adjacency")


def get_isis_interface(device: str, interface: str) -> Dict[str, Any]:
    return _run_command(device, f"show router isis interface {interface}")


def get_isis_log(device: str) -> Dict[str, Any]:
    return _run_command(device, "show log log-id isis")


def get_cpu_memory(device: str) -> Dict[str, Any]:
    cpu = _run_command(device, "show system cpu")
    mem = _run_command(device, "show system memory")
    return {"device": device, "cpu": cpu, "memory": mem}


def get_chassis_health(device: str) -> Dict[str, Any]:
    chassis = _run_command(device, "show chassis")
    cards = _run_command(device, "show card")
    alarms = _run_command(device, "show system alarms")
    return {"device": device, "chassis": chassis, "cards": cards, "alarms": alarms}


def get_route_lookup(device: str, prefix: str) -> Dict[str, Any]:
    return _run_command(device, f"show router route-table {prefix}")


def get_bgp_route(device: str, prefix: str) -> Dict[str, Any]:
    return _run_command(device, f"show router bgp routes {prefix}")


def get_acl_hits(device: str, acl_name: str) -> Dict[str, Any]:
    return _run_command(device, f"show filter match {acl_name}")


def get_mtu_info(device: str, interface: str) -> Dict[str, Any]:
    return _run_command(device, f"show port {interface}")


def get_ping(device: str, destination: str, count: int) -> Dict[str, Any]:
    return _run_command(device, f"ping {destination} count {count}")


def get_traceroute(device: str, destination: str) -> Dict[str, Any]:
    return _run_command(device, f"traceroute {destination}")
