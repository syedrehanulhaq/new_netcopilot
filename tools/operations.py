from typing import Any, Dict

from config import JUNIPER_DEVICES, NOKIA_DEVICES
from devices.iosxr import (
    get_acl_hits as iosxr_get_acl_hits,
    get_bgp_neighbor_status as iosxr_get_bgp_neighbor_status,
    get_bgp_route as iosxr_get_bgp_route,
    get_lldp_neighbor as iosxr_get_lldp_neighbor,
    get_cpu_memory as iosxr_get_cpu_memory,
    get_isis_interface as iosxr_get_isis_interface,
    get_isis_log as iosxr_get_isis_log,
    get_isis_neighbors as iosxr_get_isis_neighbors,
    get_mtu_info as iosxr_get_mtu_info,
    get_ospf_neighbors as iosxr_get_ospf_neighbors,
    get_ping as iosxr_get_ping,
    get_route_lookup as iosxr_get_route_lookup,
    get_traceroute as iosxr_get_traceroute,
)
from devices.junos import (
    get_acl_hits as junos_get_acl_hits,
    get_bgp_neighbor_status as junos_get_bgp_neighbor_status,
    get_bgp_route as junos_get_bgp_route,
    get_chassis_health as junos_get_chassis_health,
    get_lldp_neighbor as junos_get_lldp_neighbor,
    get_cpu_memory as junos_get_cpu_memory,
    get_isis_interface as junos_get_isis_interface,
    get_isis_log as junos_get_isis_log,
    get_isis_neighbors as junos_get_isis_neighbors,
    get_mtu_info as junos_get_mtu_info,
    get_ospf_neighbors as junos_get_ospf_neighbors,
    get_optics as junos_get_optics,
    get_ping as junos_get_ping,
    get_route_lookup as junos_get_route_lookup,
    get_traceroute as junos_get_traceroute,
)
from devices.nokia import (
    get_acl_hits as nokia_get_acl_hits,
    get_bgp_neighbor_status as nokia_get_bgp_neighbor_status,
    get_bgp_route as nokia_get_bgp_route,
    get_chassis_health as nokia_get_chassis_health,
    get_lldp_neighbor as nokia_get_lldp_neighbor,
    get_cpu_memory as nokia_get_cpu_memory,
    get_isis_interface as nokia_get_isis_interface,
    get_isis_log as nokia_get_isis_log,
    get_isis_neighbors as nokia_get_isis_neighbors,
    get_mtu_info as nokia_get_mtu_info,
    get_ping as nokia_get_ping,
    get_route_lookup as nokia_get_route_lookup,
    get_traceroute as nokia_get_traceroute,
)
from tools.device_access import get_interface_health


def get_bgp_neighbor_status(device: str, neighbor: str) -> Dict[str, Any]:
    if device in JUNIPER_DEVICES:
        return junos_get_bgp_neighbor_status(device, neighbor)
    if device in NOKIA_DEVICES:
        return nokia_get_bgp_neighbor_status(device, neighbor)
    return iosxr_get_bgp_neighbor_status(device, neighbor)


def get_ospf_neighbors(device: str) -> Dict[str, Any]:
    if device in JUNIPER_DEVICES:
        return junos_get_ospf_neighbors(device)
    if device in NOKIA_DEVICES:
        return {"device": device, "error": "OSPF neighbors not implemented for Nokia yet."}
    return iosxr_get_ospf_neighbors(device)


def get_isis_neighbors(device: str) -> Dict[str, Any]:
    if device in JUNIPER_DEVICES:
        return junos_get_isis_neighbors(device)
    if device in NOKIA_DEVICES:
        return nokia_get_isis_neighbors(device)
    return iosxr_get_isis_neighbors(device)


def get_cpu_memory(device: str) -> Dict[str, Any]:
    if device in JUNIPER_DEVICES:
        return junos_get_cpu_memory(device)
    if device in NOKIA_DEVICES:
        return nokia_get_cpu_memory(device)
    return iosxr_get_cpu_memory(device)


def get_route_lookup(device: str, prefix: str) -> Dict[str, Any]:
    if device in JUNIPER_DEVICES:
        return junos_get_route_lookup(device, prefix)
    if device in NOKIA_DEVICES:
        return nokia_get_route_lookup(device, prefix)
    return iosxr_get_route_lookup(device, prefix)


def get_bgp_route(device: str, prefix: str) -> Dict[str, Any]:
    if device in JUNIPER_DEVICES:
        return junos_get_bgp_route(device, prefix)
    if device in NOKIA_DEVICES:
        return nokia_get_bgp_route(device, prefix)
    return iosxr_get_bgp_route(device, prefix)


def get_acl_hits(device: str, acl_name: str) -> Dict[str, Any]:
    if device in JUNIPER_DEVICES:
        return junos_get_acl_hits(device, acl_name)
    if device in NOKIA_DEVICES:
        return nokia_get_acl_hits(device, acl_name)
    return iosxr_get_acl_hits(device, acl_name)


def get_mtu_info(device: str, interface: str) -> Dict[str, Any]:
    if device in JUNIPER_DEVICES:
        return junos_get_mtu_info(device, interface)
    if device in NOKIA_DEVICES:
        return nokia_get_mtu_info(device, interface)
    return iosxr_get_mtu_info(device, interface)


def get_ping(device: str, destination: str, count: int) -> Dict[str, Any]:
    if device in JUNIPER_DEVICES:
        return junos_get_ping(device, destination, count)
    if device in NOKIA_DEVICES:
        return nokia_get_ping(device, destination, count)
    return iosxr_get_ping(device, destination, count)


def get_traceroute(device: str, destination: str) -> Dict[str, Any]:
    if device in JUNIPER_DEVICES:
        return junos_get_traceroute(device, destination)
    if device in NOKIA_DEVICES:
        return nokia_get_traceroute(device, destination)
    return iosxr_get_traceroute(device, destination)


def get_chassis_health(device: str) -> Dict[str, Any]:
    if device in JUNIPER_DEVICES:
        return junos_get_chassis_health(device)
    if device in NOKIA_DEVICES:
        return nokia_get_chassis_health(device)
    return {"device": device, "error": "Chassis health is currently supported for Junos only."}


def get_link_health(device: str, interface: str) -> Dict[str, Any]:
    local = get_interface_health(device, interface)
    if device in JUNIPER_DEVICES:
        neighbor = junos_get_lldp_neighbor(device, interface)
    elif device in NOKIA_DEVICES:
        neighbor = nokia_get_lldp_neighbor(device, interface)
    else:
        neighbor = iosxr_get_lldp_neighbor(device, interface)

    result: Dict[str, Any] = {
        "device": device,
        "interface": interface,
        "local": local,
        "neighbor": neighbor,
    }

    if device in JUNIPER_DEVICES:
        result["local_optics"] = junos_get_optics(device, interface)

    neighbor_device = neighbor.get("neighbor_device")
    neighbor_interface = neighbor.get("neighbor_interface")
    if neighbor_device and neighbor_interface:
        result["remote"] = get_interface_health(neighbor_device, neighbor_interface)
        if neighbor_device in JUNIPER_DEVICES:
            result["remote_optics"] = junos_get_optics(neighbor_device, neighbor_interface)
    else:
        result["remote"] = {
            "error": "Neighbor not discovered via LLDP. Unable to check remote side."
        }
    return result


def get_isis_health(device: str, interface: str) -> Dict[str, Any]:
    neighbors = get_isis_neighbors(device)
    if device in JUNIPER_DEVICES:
        isis_interface = junos_get_isis_interface(device, interface)
        isis_log = junos_get_isis_log(device)
    elif device in NOKIA_DEVICES:
        isis_interface = nokia_get_isis_interface(device, interface)
        isis_log = nokia_get_isis_log(device)
    else:
        isis_interface = iosxr_get_isis_interface(device, interface)
        isis_log = iosxr_get_isis_log(device)
    link = get_link_health(device, interface)
    mtu = get_mtu_info(device, interface)
    return {
        "device": device,
        "interface": interface,
        "isis_neighbors": neighbors,
        "isis_interface": isis_interface,
        "isis_log": isis_log,
        "link_health": link,
        "mtu": mtu,
    }
