import re
import threading
import time
from typing import Any, Dict, Optional, Tuple
from xml.etree import ElementTree

from netmiko import ConnectHandler

from config import JUNIPER_DEVICES, JUNIPER_PASS, JUNIPER_USER, NETCONF_PORT, NETCONF_TIMEOUT

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
    host = JUNIPER_DEVICES[device]
    if ":" in host:
        base, port = host.rsplit(":", 1)
        if port.isdigit():
            return base, int(port)
    return host, None


def _run_command(device: str, command: str) -> Dict[str, Any]:
    if device not in JUNIPER_DEVICES:
        return {
            "device": device,
            "error": f"Unknown device '{device}'. Known devices: {list(JUNIPER_DEVICES.keys())}",
        }

    if not JUNIPER_USER or not JUNIPER_PASS:
        return {
            "device": device,
            "error": "JUNIPER_USER or JUNIPER_PASS not set in environment.",
        }

    host, port = _resolve_host_port(device)
    key = f"{host}:{port or 22}"
    try:
        device_params = {
            "device_type": "juniper_junos",
            "host": host,
            "username": JUNIPER_USER,
            "password": JUNIPER_PASS,
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


def _parse_netconf_interface(xml_text: str) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {}
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return parsed

    def _find_text(path: str) -> str:
        node = root.find(path)
        return node.text.strip() if node is not None and node.text else ""

    parsed["admin_status"] = _find_text(".//physical-interface/admin-status")
    parsed["oper_status"] = _find_text(".//physical-interface/oper-status")
    parsed["last_flapped"] = _find_text(".//physical-interface/last-flapped")
    parsed["mtu"] = _find_text(".//physical-interface/mtu")

    for key, path in [
        ("input_errors", ".//input-error-count"),
        ("output_errors", ".//output-error-count"),
        ("input_packets", ".//input-packets"),
        ("output_packets", ".//output-packets"),
        ("input_bytes", ".//input-bytes"),
        ("output_bytes", ".//output-bytes"),
        ("carrier_transitions", ".//carrier-transitions"),
    ]:
        value = _find_text(path)
        if value.isdigit():
            parsed[key] = int(value)

    return parsed


def _parse_netconf_interfaces_list(xml_text: str) -> list[Dict[str, Any]]:
    interfaces: list[Dict[str, Any]] = []
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return interfaces

    for phys in root.findall(".//physical-interface"):
        name = (phys.findtext("name") or "").strip()
        if not name:
            continue
        interfaces.append(
            {
                "interface": name,
                "admin_status": (phys.findtext("admin-status") or "").strip(),
                "oper_status": (phys.findtext("oper-status") or "").strip(),
                "description": (phys.findtext("description") or "").strip(),
                "mtu": (phys.findtext("mtu") or "").strip(),
                "last_flapped": (phys.findtext("last-flapped") or "").strip(),
            }
        )
    return interfaces


def _parse_netconf_bgp_neighbor(xml_text: str) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {}
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return parsed

    def _find_text(path: str) -> str:
        node = root.find(path)
        return node.text.strip() if node is not None and node.text else ""

    parsed["peer_address"] = _find_text(".//bgp-peer/address")
    parsed["peer_as"] = _find_text(".//bgp-peer/peer-as")
    parsed["local_as"] = _find_text(".//bgp-peer/local-as")
    parsed["state"] = _find_text(".//bgp-peer/peer-state")
    parsed["last_event"] = _find_text(".//bgp-peer/last-event")
    parsed["last_state"] = _find_text(".//bgp-peer/last-state")
    parsed["last_error"] = _find_text(".//bgp-peer/last-error")
    return parsed


def _parse_netconf_ospf_neighbors(xml_text: str) -> list[Dict[str, Any]]:
    neighbors: list[Dict[str, Any]] = []
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return neighbors

    for nbr in root.findall(".//ospf-neighbor"):
        neighbors.append(
            {
                "neighbor_id": (nbr.findtext("neighbor-id") or "").strip(),
                "interface": (nbr.findtext("interface-name") or "").strip(),
                "state": (nbr.findtext("neighbor-state") or "").strip(),
                "priority": (nbr.findtext("neighbor-priority") or "").strip(),
                "dead": (nbr.findtext("neighbor-dead-timer") or "").strip(),
            }
        )
    return neighbors


def _parse_netconf_isis_neighbors(xml_text: str) -> list[Dict[str, Any]]:
    neighbors: list[Dict[str, Any]] = []
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return neighbors

    for adj in root.findall(".//isis-adjacency"):
        neighbors.append(
            {
                "system": (adj.findtext("system-name") or "").strip(),
                "interface": (adj.findtext("interface-name") or "").strip(),
                "state": (adj.findtext("adjacency-state") or "").strip(),
                "level": (adj.findtext("level") or "").strip(),
                "hold": (adj.findtext("holdtime") or "").strip(),
            }
        )
    return neighbors


def _parse_netconf_route(xml_text: str) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {"routes": []}
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return parsed

    for entry in root.findall(".//route-entry"):
        parsed["routes"].append(
            {
                "prefix": (entry.findtext("destination") or "").strip(),
                "protocol": (entry.findtext("protocol-name") or "").strip(),
                "preference": (entry.findtext("preference") or "").strip(),
                "age": (entry.findtext("age") or "").strip(),
            }
        )
    parsed["has_default_route"] = any(r.get("prefix") == "0.0.0.0/0" for r in parsed["routes"])
    return parsed


def _parse_netconf_chassis_inventory(xml_text: str) -> list[Dict[str, Any]]:
    items: list[Dict[str, Any]] = []
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return items

    for item in root.findall(".//chassis") + root.findall(".//chassis-module"):
        name = (item.findtext("name") or "").strip()
        description = (item.findtext("description") or "").strip()
        serial = (item.findtext("serial-number") or "").strip()
        part = (item.findtext("part-number") or "").strip()
        if name or description or serial or part:
            items.append(
                {
                    "name": name,
                    "description": description,
                    "serial": serial,
                    "part_number": part,
                }
            )
    return items


def _parse_netconf_fpc(xml_text: str) -> list[Dict[str, Any]]:
    fpcs: list[Dict[str, Any]] = []
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return fpcs

    for fpc in root.findall(".//fpc"):
        fpcs.append(
            {
                "slot": (fpc.findtext("slot") or "").strip(),
                "state": (fpc.findtext("state") or "").strip(),
                "temperature": (fpc.findtext("temperature") or "").strip(),
                "memory_dram": (fpc.findtext("memory-dram-size") or "").strip(),
                "cpu_total": (fpc.findtext("cpu-total") or "").strip(),
            }
        )
    return fpcs


def _parse_netconf_pic(xml_text: str) -> list[Dict[str, Any]]:
    pics: list[Dict[str, Any]] = []
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return pics

    for pic in root.findall(".//pic"):
        pics.append(
            {
                "fpc_slot": (pic.findtext("fpc-slot") or "").strip(),
                "pic_slot": (pic.findtext("pic-slot") or "").strip(),
                "state": (pic.findtext("state") or "").strip(),
                "description": (pic.findtext("description") or "").strip(),
            }
        )
    return pics


def _run_netconf_rpc(device: str, rpc_xml: str) -> Dict[str, Any]:
    if device not in JUNIPER_DEVICES:
        return {
            "device": device,
            "error": f"Unknown device '{device}'. Known devices: {list(JUNIPER_DEVICES.keys())}",
        }

    if not JUNIPER_USER or not JUNIPER_PASS:
        return {
            "device": device,
            "error": "JUNIPER_USER or JUNIPER_PASS not set in environment.",
        }

    try:
        from ncclient import manager
    except Exception:
        return {"device": device, "error": "ncclient not installed."}

    host, port = _resolve_host_port(device)
    try:
        with manager.connect(
            host=host,
            port=port or NETCONF_PORT,
            username=JUNIPER_USER,
            password=JUNIPER_PASS,
            hostkey_verify=False,
            device_params={"name": "junos"},
            timeout=NETCONF_TIMEOUT,
        ) as m:
            response = m.dispatch(ElementTree.fromstring(rpc_xml))
            xml_text = response.xml
        return {"device": device, "netconf": True, "raw_xml": xml_text}
    except Exception as exc:
        return {"device": device, "error": f"Netconf error: {exc}"}


def _run_netconf_interface(device: str, interface: str) -> Dict[str, Any]:
    rpc = f"<get-interface-information><interface-name>{interface}</interface-name></get-interface-information>"
    result = _run_netconf_rpc(device, rpc)
    if "error" not in result:
        result["interface"] = interface
        result["parsed_interface"] = _parse_netconf_interface(result.get("raw_xml", ""))
    return result


def _parse_show_interfaces(output: str) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {}
    header_match = re.search(
        r"Physical interface:\s+\S+,\s+(\w+),\s+Physical link is\s+(\w+)",
        output,
    )
    if header_match:
        parsed["admin_status"] = header_match.group(1).strip()
        parsed["oper_status"] = header_match.group(2).strip()
    for line in output.splitlines():
        line_lower = line.lower().strip()

        if line_lower.startswith("physical link is"):
            parsed["physical_link"] = line.split("is", 1)[1].strip()
        elif line_lower.startswith("device flags"):
            parsed["device_flags"] = line.split(":", 1)[1].strip()
        elif line_lower.startswith("interface flags"):
            parsed["interface_flags"] = line.split(":", 1)[1].strip()
        elif line_lower.startswith("active alarms"):
            parsed["active_alarms"] = line.split(":", 1)[1].strip()
        elif line_lower.startswith("active defects"):
            parsed["active_defects"] = line.split(":", 1)[1].strip()
        elif line_lower.startswith("link-level type:"):
            parsed["link_level_type"] = line.split(":", 1)[1].strip()
        elif line_lower.startswith("mtu:"):
            match = re.search(r"mtu:\s*(\d+)", line_lower)
            if match:
                parsed["mtu_bytes"] = int(match.group(1))
        elif line_lower.startswith("speed:"):
            parsed["speed"] = line.split(":", 1)[1].strip()
        elif "last flapped" in line_lower:
            parsed["last_flapped"] = line.split(":", 1)[1].strip()
        elif line_lower.startswith("input errors:"):
            match = re.search(r"input errors:\s*(\d+)", line_lower)
            if match:
                parsed["input_errors"] = int(match.group(1))
        elif line_lower.startswith("output errors:"):
            match = re.search(r"output errors:\s*(\d+)", line_lower)
            if match:
                parsed["output_errors"] = int(match.group(1))
        elif line_lower.startswith("crc errors:"):
            match = re.search(r"crc errors:\s*(\d+)", line_lower)
            if match:
                parsed["crc_errors"] = int(match.group(1))
        elif line_lower.startswith("framing errors:"):
            match = re.search(r"framing errors:\s*(\d+)", line_lower)
            if match:
                parsed["framing_errors"] = int(match.group(1))
        elif line_lower.startswith("carrier transitions:"):
            match = re.search(r"carrier transitions:\s*(\d+)", line_lower)
            if match:
                parsed["carrier_transitions"] = int(match.group(1))
        elif line_lower.startswith("input packets:"):
            match = re.search(r"input packets:\s*(\d+)", line_lower)
            if match:
                parsed["input_packets"] = int(match.group(1))
        elif line_lower.startswith("output packets:"):
            match = re.search(r"output packets:\s*(\d+)", line_lower)
            if match:
                parsed["output_packets"] = int(match.group(1))
        elif line_lower.startswith("input bytes:"):
            match = re.search(r"input bytes:\s*(\d+)", line_lower)
            if match:
                parsed["input_bytes"] = int(match.group(1))
        elif line_lower.startswith("output bytes:"):
            match = re.search(r"output bytes:\s*(\d+)", line_lower)
            if match:
                parsed["output_bytes"] = int(match.group(1))

    return parsed


def _parse_show_interfaces_terse(output: str) -> list[Dict[str, Any]]:
    interfaces: list[Dict[str, Any]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        if line.lower().startswith("interface"):
            continue
        if line.lower().startswith("physical"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        interface = parts[0]
        admin = parts[1]
        oper = parts[2]
        protocol = parts[3] if len(parts) > 3 else ""
        interfaces.append(
            {
                "interface": interface,
                "admin_status": admin,
                "oper_status": oper,
                "protocol": protocol,
                "logical": "." in interface,
            }
        )
    return interfaces


def _parse_lldp_neighbors(output: str, interface: str) -> Dict[str, str]:
    for line in output.splitlines():
        if not line.strip():
            continue
        if line.lower().startswith("local interface"):
            continue
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 5:
            parts = re.split(r"\s+", line.strip())
        if len(parts) < 5:
            continue
        # Columns: Local Interface | Parent | Chassis Id | Port info | System Name
        local_if = parts[0].split()[0]
        if local_if != interface:
            continue
        neighbor_interface = parts[3].strip()
        neighbor_device = parts[4].strip()
        return {"neighbor_device": neighbor_device, "neighbor_interface": neighbor_interface}
    return {}


def get_lldp_neighbor(device: str, interface: str) -> Dict[str, Any]:
    cmd = "show lldp neighbors"
    result = _run_command(device, cmd)
    if "output" in result:
        parsed = _parse_lldp_neighbors(result["output"], interface)
        if parsed:
            parsed.update({"device": device, "interface": interface, "command": cmd, "output": result["output"]})
            return parsed
        result["error"] = "No LLDP neighbor found on this interface."
    return result


def _parse_show_chassis_hardware(output: str) -> list[Dict[str, Any]]:
    items: list[Dict[str, Any]] = []
    for line in output.splitlines():
        if not line.strip() or line.lower().startswith("hardware"):
            continue
        if line.lower().startswith("item"):
            continue
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 2:
            continue
        entry = {
            "item": parts[0],
            "version": parts[1] if len(parts) > 1 else "",
            "part_number": parts[2] if len(parts) > 2 else "",
            "serial": parts[3] if len(parts) > 3 else "",
            "description": parts[4] if len(parts) > 4 else "",
        }
        items.append(entry)
    return items


def _parse_show_chassis_fpc(output: str) -> list[Dict[str, Any]]:
    fpcs: list[Dict[str, Any]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        if line.lower().startswith("slot"):
            continue
        parts = re.split(r"\s+", line.strip())
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        slot = parts[0]
        state = parts[1]
        temp = parts[2]
        entry = {"slot": slot, "state": state, "temperature_c": temp}
        if len(parts) > 4:
            entry["cpu_total"] = parts[3]
            entry["cpu_interrupt"] = parts[4]
        fpcs.append(entry)
    return fpcs


def _parse_show_chassis_pic(output: str) -> list[Dict[str, Any]]:
    pics: list[Dict[str, Any]] = []
    current: Dict[str, Any] = {}
    for line in output.splitlines():
        if line.startswith("FPC slot"):
            if current:
                pics.append(current)
            current = {"header": line.strip()}
        elif line.strip().startswith("Type"):
            current["type"] = line.split(None, 1)[1].strip()
        elif line.strip().startswith("State"):
            current["state"] = line.split(None, 1)[1].strip()
    if current:
        pics.append(current)
    return pics


def _parse_log_hardware_faults(output: str) -> list[Dict[str, Any]]:
    faults: list[Dict[str, Any]] = []
    keywords = ("fail", "error", "alarm", "offline", "down", "missing", "unresponsive", "overheat")
    for line in output.splitlines():
        if not line.strip():
            continue
        severity = "info"
        if any(word in line.lower() for word in keywords):
            severity = "warning"
        faults.append({"message": line.strip(), "severity": severity})
    return faults


def _parse_optics(output: str) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {}
    for line in output.splitlines():
        line_lower = line.lower()
        if "rx power" in line_lower or "laser rx power" in line_lower:
            match = re.search(r"([-\d\.]+)\s*dBm", line)
            if match:
                parsed["rx_power_dbm"] = float(match.group(1))
        if "tx power" in line_lower or "laser output power" in line_lower:
            match = re.search(r"([-\d\.]+)\s*dBm", line)
            if match:
                parsed["tx_power_dbm"] = float(match.group(1))
    return parsed


def get_optics(device: str, interface: str) -> Dict[str, Any]:
    cmd = f"show interfaces diagnostics optics {interface}"
    result = _run_command(device, cmd)
    if "output" in result:
        parsed = _parse_optics(result["output"])
        if parsed:
            result["parsed_optics"] = parsed
        else:
            result["error"] = "Optics data not available for this interface."
    return result


def get_interface_status(device: str) -> Dict[str, Any]:
    rpc = "<get-interface-information/>"
    netconf_result = _run_netconf_rpc(device, rpc)
    if "error" not in netconf_result:
        netconf_result["interfaces"] = _parse_netconf_interfaces_list(netconf_result.get("raw_xml", ""))
        if netconf_result.get("interfaces"):
            return netconf_result

    cmd = "show interfaces terse"
    result = _run_command(device, cmd)
    if "error" not in result:
        parsed = _parse_show_interfaces_terse(result.get("output", ""))
        result["interfaces"] = parsed
        if parsed:
            result["summary"] = {
                "total": len(parsed),
                "up": sum(1 for i in parsed if i["oper_status"].lower() == "up"),
                "down": sum(1 for i in parsed if i["oper_status"].lower() == "down"),
            }
    return result


def _parse_bgp_neighbor(output: str) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {}
    for line in output.splitlines():
        line_lower = line.lower().strip()
        if line_lower.startswith("peer:"):
            parsed["peer"] = line.split(":", 1)[1].strip()
        elif line_lower.startswith("state:"):
            parsed["state"] = line.split(":", 1)[1].strip()
        elif line_lower.startswith("last state:"):
            parsed["last_state"] = line.split(":", 1)[1].strip()
        elif line_lower.startswith("last event:"):
            parsed["last_event"] = line.split(":", 1)[1].strip()
        elif line_lower.startswith("last error:"):
            parsed["last_error"] = line.split(":", 1)[1].strip()
        elif line_lower.startswith("peer as:"):
            parsed["peer_as"] = line.split(":", 1)[1].strip()
        elif line_lower.startswith("local as:"):
            parsed["local_as"] = line.split(":", 1)[1].strip()
        elif line_lower.startswith("peer id:"):
            parsed["peer_id"] = line.split(":", 1)[1].strip()

    return parsed


def get_interface_health(device: str, interface: str) -> Dict[str, Any]:
    netconf_result = _run_netconf_interface(device, interface)
    if "error" not in netconf_result and netconf_result.get("parsed_interface"):
        return netconf_result

    cmd = f"show interfaces {interface}"
    result = _run_command(device, cmd)
    if "output" in result:
        result["parsed_interface"] = _parse_show_interfaces(result["output"])
        result["netconf"] = False
    return result


def get_interface_counters(device: str, interface: str) -> Dict[str, Any]:
    rpc = (
        "<get-interface-information>"
        f"<interface-name>{interface}</interface-name>"
        "<extensive/>"
        "</get-interface-information>"
    )
    netconf_result = _run_netconf_rpc(device, rpc)
    if "error" not in netconf_result:
        netconf_result["parsed_interface"] = _parse_netconf_interface(netconf_result.get("raw_xml", ""))
        if netconf_result.get("parsed_interface"):
            netconf_result["interface"] = interface
            return netconf_result

    cmd = f"show interfaces {interface} extensive"
    result = _run_command(device, cmd)
    if "output" in result:
        result["parsed_counters"] = _parse_show_interfaces(result["output"])
    return result


def get_chassis_health(device: str) -> Dict[str, Any]:
    inventory_rpc = "<get-chassis-inventory/>"
    fpc_rpc = "<get-fpc-information/>"
    pic_rpc = "<get-pic-information/>"

    result: Dict[str, Any] = {"device": device, "netconf": False}
    netconf_inventory = _run_netconf_rpc(device, inventory_rpc)
    netconf_fpc = _run_netconf_rpc(device, fpc_rpc)
    netconf_pic = _run_netconf_rpc(device, pic_rpc)
    if "error" not in netconf_inventory:
        result["inventory"] = _parse_netconf_chassis_inventory(netconf_inventory.get("raw_xml", ""))
    if "error" not in netconf_fpc:
        result["fpcs"] = _parse_netconf_fpc(netconf_fpc.get("raw_xml", ""))
    if "error" not in netconf_pic:
        result["pics"] = _parse_netconf_pic(netconf_pic.get("raw_xml", ""))

    if result.get("inventory") or result.get("fpcs") or result.get("pics"):
        result["netconf"] = True
        return result

    result = {"device": device, "netconf": False}
    hardware = _run_command(device, "show chassis hardware")
    fpc = _run_command(device, "show chassis fpc")
    fpc_detail = _run_command(device, "show chassis fpc detail")
    pic = _run_command(device, "show chassis pic")
    logs = _run_command(device, 'show log messages | match "(FPC|PIC|RE|SIB|LCC|CHASSIS|FAN|PSU|POWER|TEMP)"')

    if "output" in hardware:
        result["hardware"] = _parse_show_chassis_hardware(hardware["output"])
    if "output" in fpc:
        result["fpcs"] = _parse_show_chassis_fpc(fpc["output"])
    if "output" in fpc_detail:
        result["fpc_detail"] = fpc_detail["output"]
    if "output" in pic:
        result["pics"] = _parse_show_chassis_pic(pic["output"])
    if "output" in logs:
        result["log_faults"] = _parse_log_hardware_faults(logs["output"])

    return result


def get_bgp_neighbor_status(device: str, neighbor: str) -> Dict[str, Any]:
    cmd = f"show bgp neighbor {neighbor}"
    rpc = f"<get-bgp-neighbor-information><neighbor-address>{neighbor}</neighbor-address></get-bgp-neighbor-information>"
    netconf_result = _run_netconf_rpc(device, rpc)
    if "error" not in netconf_result:
        netconf_result["parsed_bgp_neighbor"] = _parse_netconf_bgp_neighbor(netconf_result.get("raw_xml", ""))
        if netconf_result.get("parsed_bgp_neighbor"):
            netconf_result["neighbor"] = neighbor
            return netconf_result

    result = _run_command(device, cmd)
    if "output" in result:
        result["parsed_bgp_neighbor"] = _parse_bgp_neighbor(result["output"])
    return result


def get_ospf_neighbors(device: str) -> Dict[str, Any]:
    rpc = "<get-ospf-neighbor-information/>"
    netconf_result = _run_netconf_rpc(device, rpc)
    if "error" not in netconf_result:
        netconf_result["parsed_neighbors"] = _parse_netconf_ospf_neighbors(netconf_result.get("raw_xml", ""))
        if netconf_result.get("parsed_neighbors"):
            return netconf_result

    cmd = "show ospf neighbor"
    result = _run_command(device, cmd)
    if "output" in result:
        result["parsed_neighbors"] = _parse_ospf_neighbors(result["output"])
    return result


def get_isis_neighbors(device: str) -> Dict[str, Any]:
    rpc = "<get-isis-adjacency-information/>"
    netconf_result = _run_netconf_rpc(device, rpc)
    if "error" not in netconf_result:
        netconf_result["parsed_neighbors"] = _parse_netconf_isis_neighbors(netconf_result.get("raw_xml", ""))
        if netconf_result.get("parsed_neighbors"):
            return netconf_result

    cmd = "show isis adjacency"
    result = _run_command(device, cmd)
    if "output" in result:
        result["parsed_neighbors"] = _parse_isis_neighbors(result["output"])
    return result


def get_isis_interface(device: str, interface: str) -> Dict[str, Any]:
    cmd = f"show isis interface {interface}"
    return _run_command(device, cmd)


def get_isis_log(device: str) -> Dict[str, Any]:
    cmd = "show log messages | last 50 | match ISIS"
    return _run_command(device, cmd)


def get_cpu_memory(device: str) -> Dict[str, Any]:
    rpc = "<get-route-engine-information/>"
    netconf_result = _run_netconf_rpc(device, rpc)
    if "error" not in netconf_result:
        netconf_result["parsed_re"] = _parse_netconf_routing_engine(netconf_result.get("raw_xml", ""))
        if netconf_result.get("parsed_re"):
            return netconf_result

    cmd = "show chassis routing-engine"
    result = _run_command(device, cmd)
    if "output" in result:
        result["parsed_re"] = _parse_routing_engine(result["output"])
    return result


def _parse_ospf_neighbors(output: str) -> list[Dict[str, Any]]:
    neighbors: list[Dict[str, Any]] = []
    for line in output.splitlines():
        if not line.strip() or line.lower().startswith("address"):
            continue
        parts = re.split(r"\s+", line.strip())
        if len(parts) < 6:
            continue
        neighbors.append(
            {
                "address": parts[0],
                "interface": parts[1],
                "state": parts[2],
                "id": parts[3],
                "priority": parts[4],
                "dead": parts[5],
            }
        )
    return neighbors


def _parse_isis_neighbors(output: str) -> list[Dict[str, Any]]:
    neighbors: list[Dict[str, Any]] = []
    for line in output.splitlines():
        if not line.strip() or line.lower().startswith("interface"):
            continue
        parts = re.split(r"\s+", line.strip())
        if len(parts) < 6:
            continue
        neighbors.append(
            {
                "interface": parts[0],
                "system": parts[1],
                "level": parts[2],
                "state": parts[3],
                "hold": parts[4],
                "circuit_id": parts[5],
            }
        )
    return neighbors


def _parse_routing_engine(output: str) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {}
    for line in output.splitlines():
        line_lower = line.lower().strip()
        if line_lower.startswith("cpu utilization:"):
            match = re.search(r"cpu utilization:\s*([\d\.]+)\s*percent", line_lower)
            if match:
                parsed["cpu_percent"] = float(match.group(1))
        elif line_lower.startswith("memory utilization:"):
            match = re.search(r"memory utilization:\s*([\d\.]+)\s*percent", line_lower)
            if match:
                parsed["memory_percent"] = float(match.group(1))
        elif line_lower.startswith("temperature:"):
            match = re.search(r"temperature:\s*([\d\.]+)\s*degrees", line_lower)
            if match:
                parsed["temperature_c"] = float(match.group(1))
        elif line_lower.startswith("model:"):
            parsed["model"] = line.split(":", 1)[1].strip()
        elif line_lower.startswith("uptime:"):
            parsed["uptime"] = line.split(":", 1)[1].strip()

    return parsed


def _parse_netconf_routing_engine(xml_text: str) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {}
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return parsed

    def _find_text(path: str) -> str:
        node = root.find(path)
        return node.text.strip() if node is not None and node.text else ""

    parsed["model"] = _find_text(".//route-engine/model")
    parsed["uptime"] = _find_text(".//route-engine/uptime")
    cpu_idle = _find_text(".//route-engine/cpu-idle")
    if cpu_idle.isdigit():
        parsed["cpu_percent"] = 100 - int(cpu_idle)
    memory_util = _find_text(".//route-engine/memory-system-total-used-percent")
    if memory_util.isdigit():
        parsed["memory_percent"] = int(memory_util)
    temperature = _find_text(".//route-engine/temperature")
    if temperature:
        try:
            parsed["temperature_c"] = float(temperature)
        except ValueError:
            pass

    return parsed


def get_route_lookup(device: str, prefix: str) -> Dict[str, Any]:
    rpc = f"<get-route-information><destination>{prefix}</destination></get-route-information>"
    netconf_result = _run_netconf_rpc(device, rpc)
    if "error" not in netconf_result:
        netconf_result["parsed_routes"] = _parse_netconf_route(netconf_result.get("raw_xml", ""))
        if netconf_result.get("parsed_routes"):
            netconf_result["prefix"] = prefix
            return netconf_result

    cmd = f"show route {prefix}"
    result = _run_command(device, cmd)
    if "output" in result:
        result["parsed_routes"] = _parse_show_route(result["output"])
    return result


def get_bgp_route(device: str, prefix: str) -> Dict[str, Any]:
    rpc = f"<get-route-information><destination>{prefix}</destination><protocol>bgp</protocol></get-route-information>"
    netconf_result = _run_netconf_rpc(device, rpc)
    if "error" not in netconf_result:
        netconf_result["parsed_routes"] = _parse_netconf_route(netconf_result.get("raw_xml", ""))
        if netconf_result.get("parsed_routes"):
            netconf_result["prefix"] = prefix
            return netconf_result

    cmd = f"show route protocol bgp {prefix}"
    return _run_command(device, cmd)


def get_acl_hits(device: str, acl_name: str) -> Dict[str, Any]:
    cmd = f"show firewall filter {acl_name}"
    return _run_command(device, cmd)


def get_mtu_info(device: str, interface: str) -> Dict[str, Any]:
    netconf_result = _run_netconf_interface(device, interface)
    if "error" not in netconf_result:
        parsed = netconf_result.get("parsed_interface") or {}
        if parsed.get("mtu"):
            return netconf_result

    cmd = f"show interfaces {interface}"
    return _run_command(device, cmd)


def get_ping(device: str, destination: str, count: int) -> Dict[str, Any]:
    cmd = f"ping {destination} count {count}"
    return _run_command(device, cmd)


def get_traceroute(device: str, destination: str) -> Dict[str, Any]:
    cmd = f"traceroute {destination}"
    return _run_command(device, cmd)


def _parse_show_route(output: str) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {"routes": []}
    current = None
    for line in output.splitlines():
        line = line.rstrip()
        if not line.strip():
            continue

        if line.startswith("inet.") or line.startswith("inet6."):
            match = re.search(r"(\d+)\s+destinations,\s+(\d+)\s+routes", line)
            if match:
                parsed["destinations"] = int(match.group(1))
                parsed["routes_total"] = int(match.group(2))
            detail = re.search(r"\((\d+)\s+active,\s+(\d+)\s+holddown,\s+(\d+)\s+hidden\)", line)
            if detail:
                parsed["routes_active"] = int(detail.group(1))
                parsed["routes_holddown"] = int(detail.group(2))
                parsed["routes_hidden"] = int(detail.group(3))
            continue

        if re.match(r"^\S+/\d+\s+", line):
            parts = re.split(r"\s+", line.strip(), maxsplit=3)
            prefix = parts[0]
            details = parts[1] if len(parts) > 1 else ""
            age = parts[2] if len(parts) > 2 else ""
            proto_match = re.search(r"\[(\w+)/(\d+)\]", details)
            protocol = proto_match.group(1) if proto_match else ""
            preference = int(proto_match.group(2)) if proto_match else None
            active = "*" in details
            current = {
                "prefix": prefix,
                "protocol": protocol,
                "preference": preference,
                "age": age,
                "active": active,
            }
            parsed["routes"].append(current)
            continue

        if current and " via " in line and " to " in line:
            match = re.search(r"to\s+(\S+)\s+via\s+(\S+)", line)
            if match:
                current["next_hop"] = match.group(1)
                current["interface"] = match.group(2)

    parsed["has_default_route"] = any(r.get("prefix") == "0.0.0.0/0" for r in parsed["routes"])
    return parsed
