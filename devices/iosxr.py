import re
import threading
import time
from typing import Any, Dict

from netmiko import ConnectHandler

from config import CISCO_DEVICES, CISCO_PASS, CISCO_USER

_SSH_POOL_LOCK = threading.Lock()
_SSH_POOL_TTL = 60
_SSH_POOL: Dict[str, tuple[Any, float]] = {}


def _get_ssh_connection(host: str, device_params: Dict[str, Any]):
    now = time.time()
    with _SSH_POOL_LOCK:
        conn_tuple = _SSH_POOL.get(host)
        if conn_tuple:
            conn, last_used = conn_tuple
            if now - last_used < _SSH_POOL_TTL and conn.is_alive():
                _SSH_POOL[host] = (conn, now)
                return conn
            try:
                conn.disconnect()
            except Exception:
                pass
            _SSH_POOL.pop(host, None)

        conn = ConnectHandler(**device_params)
        _SSH_POOL[host] = (conn, now)
        return conn


def _touch_ssh_connection(host: str) -> None:
    now = time.time()
    with _SSH_POOL_LOCK:
        conn_tuple = _SSH_POOL.get(host)
        if conn_tuple:
            conn, _ = conn_tuple
            _SSH_POOL[host] = (conn, now)


def _normalize_status(value: str) -> str:
    value_lower = value.lower()
    if "down" in value_lower:
        return "down"
    if "up" in value_lower:
        return "up"
    return "unknown"


def _parse_counts_from_line(line: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for name, value in re.findall(r"([A-Za-z][A-Za-z ]*?)\s+(\d+)", line):
        key = name.strip().lower().replace(" ", "_")
        counts[key] = int(value)
    return counts


def _parse_comma_counts(line: str) -> Dict[str, int]:
    """
    Parse lines like:
    "0 input errors, 0 CRC, 0 frame, 0 overrun, 0 ignored, 0 abort"
    """
    counts: Dict[str, int] = {}
    for part in line.split(","):
        match = re.search(r"(\d+)\s+([A-Za-z][A-Za-z ]+)", part.strip())
        if match:
            value = int(match.group(1))
            key = match.group(2).strip().lower().replace(" ", "_")
            counts[key] = value
    return counts


def _parse_show_interface(output: str) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {}

    status_match = re.search(
        r"^(\S+) is ([^,]+), line protocol is ([^\s]+)",
        output,
        re.MULTILINE,
    )
    if status_match:
        parsed["admin_status_raw"] = status_match.group(2).strip()
        parsed["oper_status_raw"] = status_match.group(3).strip()
        parsed["admin_status"] = _normalize_status(parsed["admin_status_raw"])
        parsed["oper_status"] = _normalize_status(parsed["oper_status_raw"])

    for line in output.splitlines():
        line_lower = line.lower()
        if "input errors" in line_lower:
            parsed.update(_parse_comma_counts(line))
        elif "output errors" in line_lower:
            parsed.update(_parse_comma_counts(line))
        elif "total input drops" in line_lower:
            counts = _parse_comma_counts(line)
            if "total_input_drops" in counts:
                parsed["input_drops"] = counts["total_input_drops"]
        elif "total output drops" in line_lower:
            counts = _parse_comma_counts(line)
            if "total_output_drops" in counts:
                parsed["output_drops"] = counts["total_output_drops"]
        elif "carrier transitions" in line_lower:
            counts = _parse_comma_counts(line)
            if "carrier_transitions" in counts:
                parsed["carrier_transitions"] = counts["carrier_transitions"]
        elif "last link flapped" in line_lower:
            match = re.search(r"last link flapped\s+(.+)$", line, re.IGNORECASE)
            if match:
                parsed["last_link_flapped"] = match.group(1).strip()
        elif "last input" in line_lower and "output" in line_lower:
            match = re.search(r"last input\s+([^,]+),\s+output\s+(.+)$", line, re.IGNORECASE)
            if match:
                parsed["last_input"] = match.group(1).strip()
                parsed["last_output"] = match.group(2).strip()
        elif "packets input" in line_lower and "bytes" in line_lower:
            match = re.search(r"(\d+)\s+packets input,\s+(\d+)\s+bytes", line, re.IGNORECASE)
            if match:
                parsed["input_packets"] = int(match.group(1))
                parsed["input_bytes"] = int(match.group(2))
        elif "packets output" in line_lower and "bytes" in line_lower:
            match = re.search(r"(\d+)\s+packets output,\s+(\d+)\s+bytes", line, re.IGNORECASE)
            if match:
                parsed["output_packets"] = int(match.group(1))
                parsed["output_bytes"] = int(match.group(2))
        elif "minute input rate" in line_lower:
            match = re.search(
                r"(\d+)\s+minute input rate\s+(\d+)\s+bits/sec,\s+(\d+)\s+packets/sec",
                line,
                re.IGNORECASE,
            )
            if match:
                parsed["input_rate_interval_min"] = int(match.group(1))
                parsed["input_rate_bps"] = int(match.group(2))
                parsed["input_rate_pps"] = int(match.group(3))
        elif "minute output rate" in line_lower:
            match = re.search(
                r"(\d+)\s+minute output rate\s+(\d+)\s+bits/sec,\s+(\d+)\s+packets/sec",
                line,
                re.IGNORECASE,
            )
            if match:
                parsed["output_rate_interval_min"] = int(match.group(1))
                parsed["output_rate_bps"] = int(match.group(2))
                parsed["output_rate_pps"] = int(match.group(3))

    return parsed


def _parse_show_controllers(output: str) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {}
    if not output or "not supported" in output.lower():
        return parsed

    for line in output.splitlines():
        line_lower = line.lower()
        if "los" in line_lower and ("loss of signal" in line_lower or "los:" in line_lower):
            parsed["loss_of_signal"] = "detected" if "yes" in line_lower else "not_detected"
        if "lof" in line_lower and ("loss of frame" in line_lower or "lof:" in line_lower):
            parsed["loss_of_frame"] = "detected" if "yes" in line_lower else "not_detected"

        if "rx power" in line_lower:
            match = re.search(r"rx power\s*[:=]?\s*([-\d\.]+)\s*dBm", line, re.IGNORECASE)
            if match:
                parsed["rx_power_dbm"] = float(match.group(1))
        if "tx power" in line_lower:
            match = re.search(r"tx power\s*[:=]?\s*([-\d\.]+)\s*dBm", line, re.IGNORECASE)
            if match:
                parsed["tx_power_dbm"] = float(match.group(1))
        if "laser bias" in line_lower:
            match = re.search(r"laser bias\s*[:=]?\s*([-\d\.]+)\s*mA", line, re.IGNORECASE)
            if match:
                parsed["laser_bias_ma"] = float(match.group(1))
        if "temperature" in line_lower:
            match = re.search(r"temperature\s*[:=]?\s*([-\d\.]+)\s*c", line, re.IGNORECASE)
            if match:
                parsed["temperature_c"] = float(match.group(1))

    return parsed


def _compute_deltas(first: Dict[str, Any], second: Dict[str, Any]) -> Dict[str, int]:
    deltas: Dict[str, int] = {}
    for key, value in second.items():
        if isinstance(value, int) and isinstance(first.get(key), int):
            deltas[key] = max(0, value - int(first[key]))
    return deltas


def _parse_lldp_detail(output: str) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {}
    for line in output.splitlines():
        line = line.strip()
        if line.lower().startswith("system name"):
            parsed["neighbor_device"] = line.split(":", 1)[1].strip()
        elif line.lower().startswith("port id"):
            parsed["neighbor_interface"] = line.split(":", 1)[1].strip()
        elif line.lower().startswith("port description") and "neighbor_interface" not in parsed:
            parsed["neighbor_interface"] = line.split(":", 1)[1].strip()
    return parsed


def get_lldp_neighbor(device: str, interface: str) -> Dict[str, Any]:
    if device not in CISCO_DEVICES:
        return {
            "device": device,
            "interface": interface,
            "error": f"Unknown device '{device}'. Known devices: {list(CISCO_DEVICES.keys())}",
        }

    if not CISCO_USER or not CISCO_PASS:
        return {
            "device": device,
            "interface": interface,
            "error": "CISCO_USER or CISCO_PASS not set in environment.",
        }

    host = CISCO_DEVICES[device]
    try:
        device_params = {
            "device_type": "cisco_xr",
            "host": host,
            "username": CISCO_USER,
            "password": CISCO_PASS,
            "fast_cli": False,
        }
        conn = _get_ssh_connection(host, device_params)
        cmd = f"show lldp neighbors interface {interface} detail"
        output = conn.send_command(cmd)
        _touch_ssh_connection(host)
        parsed = _parse_lldp_detail(output)
        if parsed.get("neighbor_device") or parsed.get("neighbor_interface"):
            parsed.update({"device": device, "interface": interface, "command": cmd, "output": output})
            return parsed
        return {
            "device": device,
            "interface": interface,
            "command": cmd,
            "output": output,
            "error": "No LLDP neighbor found on this interface.",
        }
    except Exception as exc:
        return {
            "device": device,
            "interface": interface,
            "command": f"show lldp neighbors interface {interface} detail",
            "error": f"Error connecting to {host}: {str(exc)}",
        }


def _parse_show_interfaces_description(output: str) -> list[Dict[str, Any]]:
    interfaces: list[Dict[str, Any]] = []
    lines = output.splitlines()
    for line in lines:
        if not line.strip():
            continue
        if line.lower().startswith("interface"):
            continue
        if set(line.strip()) == {"-"}:
            continue
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 3:
            continue
        entry = {
            "interface": parts[0],
            "status": parts[1],
            "protocol": parts[2],
            "description": parts[3] if len(parts) > 3 else "",
        }
        interfaces.append(entry)
    return interfaces


def get_interface_status(device: str) -> Dict[str, Any]:
    if device not in CISCO_DEVICES:
        return {
            "device": device,
            "error": f"Unknown device '{device}'. Known devices: {list(CISCO_DEVICES.keys())}",
        }

    if not CISCO_USER or not CISCO_PASS:
        return {
            "device": device,
            "error": "CISCO_USER or CISCO_PASS not set in environment.",
        }

    host = CISCO_DEVICES[device]
    try:
        device_params = {
            "device_type": "cisco_xr",
            "host": host,
            "username": CISCO_USER,
            "password": CISCO_PASS,
            "fast_cli": False,
        }
        conn = _get_ssh_connection(host, device_params)
        cmd = "show interfaces description"
        output = conn.send_command(cmd)
        _touch_ssh_connection(host)
        return {
            "device": device,
            "command": cmd,
            "interfaces": _parse_show_interfaces_description(output),
            "output": output,
        }
    except Exception as exc:
        return {
            "device": device,
            "command": "show interfaces description",
            "error": f"Error connecting to {host}: {str(exc)}",
        }


def get_interface_counters(device: str, interface: str, window_minutes: int) -> Dict[str, Any]:
    """
    Compute counter deltas over a short sampling window using 'show interface'.
    """
    if device not in CISCO_DEVICES:
        return {
            "device": device,
            "interface": interface,
            "window_minutes": window_minutes,
            "error": f"Unknown device '{device}'. Known devices: {list(CISCO_DEVICES.keys())}",
        }

    if not CISCO_USER or not CISCO_PASS:
        return {
            "device": device,
            "interface": interface,
            "window_minutes": window_minutes,
            "error": "CISCO_USER or CISCO_PASS not set in environment.",
        }

    host = CISCO_DEVICES[device]
    try:
        device_params = {
            "device_type": "cisco_xr",
            "host": host,
            "username": CISCO_USER,
            "password": CISCO_PASS,
            "fast_cli": False,
        }

        conn = _get_ssh_connection(host, device_params)
        show_int_cmd = f"show interface {interface}"

        first_out = conn.send_command(show_int_cmd)
        first_parsed = _parse_show_interface(first_out)

        requested_seconds = max(0, int(window_minutes * 60))
        sample_seconds = min(requested_seconds, 10)
        if sample_seconds > 0:
            time.sleep(sample_seconds)

        second_out = conn.send_command(show_int_cmd)
        _touch_ssh_connection(host)

        second_parsed = _parse_show_interface(second_out)

        counters_keys = [
            "input_packets",
            "output_packets",
            "input_bytes",
            "output_bytes",
            "input_errors",
            "output_errors",
            "input_drops",
            "output_drops",
            "crc",
            "frame",
            "overrun",
            "ignored",
            "abort",
            "underruns",
            "applique",
            "resets",
            "carrier_transitions",
        ]
        rate_keys = [
            "input_rate_interval_min",
            "input_rate_bps",
            "input_rate_pps",
            "output_rate_interval_min",
            "output_rate_bps",
            "output_rate_pps",
        ]

        first_counters = {key: first_parsed[key] for key in counters_keys if key in first_parsed}
        second_counters = {key: second_parsed[key] for key in counters_keys if key in second_parsed}
        deltas = _compute_deltas(first_counters, second_counters)

        rates = {key: second_parsed[key] for key in rate_keys if key in second_parsed}

        return {
            "device": device,
            "interface": interface,
            "window_minutes": window_minutes,
            "window_seconds_requested": requested_seconds,
            "window_seconds_actual": sample_seconds,
            "snapshot": False,
            "counters_start": first_counters,
            "counters_end": second_counters,
            "counters_delta": deltas,
            "rates": rates,
            "last_input": second_parsed.get("last_input"),
            "last_output": second_parsed.get("last_output"),
            "cli_show_interface_start": first_out,
            "cli_show_interface_end": second_out,
            "notes": "Counters are deltas over the actual sampling window.",
        }
    except Exception as e:
        return {
            "device": device,
            "interface": interface,
            "window_minutes": window_minutes,
            "error": f"Error connecting to {host}: {str(e)}",
        }


def get_interface_health(device: str, interface: str) -> Dict[str, Any]:
    """
    Real implementation using Netmiko on Cisco IOS XR.
    Returns raw CLI output plus any error details.
    """
    if device not in CISCO_DEVICES:
        return {
            "device": device,
            "interface": interface,
            "error": f"Unknown device '{device}'. Known devices: {list(CISCO_DEVICES.keys())}",
        }

    if not CISCO_USER or not CISCO_PASS:
        return {
            "device": device,
            "interface": interface,
            "error": "CISCO_USER or CISCO_PASS not set in environment.",
        }

    host = CISCO_DEVICES[device]
    try:
        device_params = {
            "device_type": "cisco_xr",
            "host": host,
            "username": CISCO_USER,
            "password": CISCO_PASS,
            "fast_cli": False,
        }

        conn = _get_ssh_connection(host, device_params)

        show_int_cmd = f"show interface {interface}"
        show_ctrl_cmd = f"show controllers {interface}"

        show_int_out = conn.send_command(show_int_cmd)

        try:
            show_ctrl_out = conn.send_command(show_ctrl_cmd)
        except Exception:
            show_ctrl_out = "Controller/optics command failed or not supported."

        _touch_ssh_connection(host)

        parsed_interface = _parse_show_interface(show_int_out)

        parsed_interface = _parse_show_interface(show_int_out)
        parsed_controllers = _parse_show_controllers(show_ctrl_out)

        return {
            "device": device,
            "interface": interface,
            "parsed_interface": parsed_interface,
            "parsed_controllers": parsed_controllers,
            "cli_show_interface": show_int_out,
            "cli_show_controllers": show_ctrl_out,
            "notes": "Real data from Cisco IOS XR. Parsed by LLM.",
        }
    except Exception as e:
        return {
            "device": device,
            "interface": interface,
            "error": f"Error connecting to {host}: {str(e)}",
        }


def _run_command(device: str, command: str) -> Dict[str, Any]:
    if device not in CISCO_DEVICES:
        return {
            "device": device,
            "error": f"Unknown device '{device}'. Known devices: {list(CISCO_DEVICES.keys())}",
        }

    if not CISCO_USER or not CISCO_PASS:
        return {
            "device": device,
            "error": "CISCO_USER or CISCO_PASS not set in environment.",
        }

    host = CISCO_DEVICES[device]
    try:
        device_params = {
            "device_type": "cisco_xr",
            "host": host,
            "username": CISCO_USER,
            "password": CISCO_PASS,
            "fast_cli": False,
        }
        conn = _get_ssh_connection(host, device_params)
        output = conn.send_command(command)
        _touch_ssh_connection(host)
        return {"device": device, "command": command, "output": output}
    except Exception as exc:
        return {
            "device": device,
            "command": command,
            "error": f"Error connecting to {host}: {str(exc)}",
        }


def get_bgp_neighbor_status(device: str, neighbor: str) -> Dict[str, Any]:
    cmd = f"show bgp neighbor {neighbor}"
    return _run_command(device, cmd)


def get_ospf_neighbors(device: str) -> Dict[str, Any]:
    cmd = "show ospf neighbor"
    return _run_command(device, cmd)


def get_isis_neighbors(device: str) -> Dict[str, Any]:
    cmd = "show isis neighbors"
    return _run_command(device, cmd)


def get_isis_interface(device: str, interface: str) -> Dict[str, Any]:
    cmd = f"show isis interface {interface}"
    return _run_command(device, cmd)


def get_isis_log(device: str) -> Dict[str, Any]:
    cmd = "show log | include ISIS"
    return _run_command(device, cmd)


def get_cpu_memory(device: str) -> Dict[str, Any]:
    cpu = _run_command(device, "show processes cpu")
    mem = _run_command(device, "show memory summary")
    parsed_cpu = _parse_iosxr_cpu(cpu.get("output", "")) if "output" in cpu else {}
    parsed_mem = _parse_iosxr_memory(mem.get("output", "")) if "output" in mem else {}
    return {
        "device": device,
        "cpu": cpu,
        "memory": mem,
        "parsed_cpu": parsed_cpu,
        "parsed_memory": parsed_mem,
    }


def _parse_iosxr_cpu(output: str) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {}
    match = re.search(
        r"five seconds:\s+(\d+)%.*one minute:\s+(\d+)%.*five minutes:\s+(\d+)%",
        output,
        re.IGNORECASE | re.DOTALL,
    )
    if match:
        parsed["cpu_5s"] = int(match.group(1))
        parsed["cpu_1m"] = int(match.group(2))
        parsed["cpu_5m"] = int(match.group(3))
    return parsed


def _parse_iosxr_memory(output: str) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {}
    match = re.search(r"Physical Memory:\s+(\d+)K\((\d+)K used\)", output, re.IGNORECASE)
    if match:
        total_kb = int(match.group(1))
        used_kb = int(match.group(2))
        parsed["mem_total_kb"] = total_kb
        parsed["mem_used_kb"] = used_kb
        if total_kb:
            parsed["mem_used_percent"] = round((used_kb / total_kb) * 100, 2)
    match = re.search(r"Processor pool:\s+Total:\s+(\d+)\s+Used:\s+(\d+)\s+Free:\s+(\d+)", output, re.IGNORECASE)
    if match:
        parsed["processor_pool_total"] = int(match.group(1))
        parsed["processor_pool_used"] = int(match.group(2))
        parsed["processor_pool_free"] = int(match.group(3))
    return parsed


def get_route_lookup(device: str, prefix: str) -> Dict[str, Any]:
    cmd = f"show route {prefix}"
    return _run_command(device, cmd)


def get_bgp_route(device: str, prefix: str) -> Dict[str, Any]:
    cmd = f"show bgp {prefix}"
    return _run_command(device, cmd)


def get_acl_hits(device: str, acl_name: str) -> Dict[str, Any]:
    cmd = f"show access-lists {acl_name}"
    return _run_command(device, cmd)


def get_mtu_info(device: str, interface: str) -> Dict[str, Any]:
    cmd = f"show interface {interface}"
    result = _run_command(device, cmd)
    if "output" in result:
        match = re.search(r"MTU\\s+(\\d+)\\s+bytes", result["output"])
        if match:
            result["mtu_bytes"] = int(match.group(1))
    return result


def get_ping(device: str, destination: str, count: int) -> Dict[str, Any]:
    cmd = f"ping {destination} count {count}"
    return _run_command(device, cmd)


def get_traceroute(device: str, destination: str) -> Dict[str, Any]:
    cmd = f"traceroute {destination}"
    return _run_command(device, cmd)
