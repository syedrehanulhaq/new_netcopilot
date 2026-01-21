from typing import Any, Dict

from config import JUNIPER_DEVICES, NOKIA_DEVICES
from devices.iosxr import (
    get_interface_health as iosxr_get_interface_health,
    get_interface_status as iosxr_get_interface_status,
)
from devices.junos import get_interface_health as junos_get_interface_health
from devices.junos import get_interface_status as junos_get_interface_status
from devices.nokia import get_interface_health as nokia_get_interface_health
from devices.nokia import get_interface_status as nokia_get_interface_status


def get_interface_health(device: str, interface: str) -> Dict[str, Any]:
    if device in JUNIPER_DEVICES:
        return junos_get_interface_health(device, interface)
    if device in NOKIA_DEVICES:
        return nokia_get_interface_health(device, interface)
    return iosxr_get_interface_health(device, interface)


def get_interface_status(device: str) -> Dict[str, Any]:
    if device in JUNIPER_DEVICES:
        return junos_get_interface_status(device)
    if device in NOKIA_DEVICES:
        return nokia_get_interface_status(device)
    return iosxr_get_interface_status(device)
