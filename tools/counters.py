from typing import Any, Dict

from config import JUNIPER_DEVICES, NOKIA_DEVICES
from devices.iosxr import get_interface_counters as iosxr_get_interface_counters
from devices.junos import get_interface_counters as junos_get_interface_counters
from devices.nokia import get_interface_counters as nokia_get_interface_counters


def get_interface_counters(
    device: str, interface: str, window_minutes: int
) -> Dict[str, Any]:
    """
    Snapshot counters from the device (Cisco IOS XR).
    """
    if device in JUNIPER_DEVICES:
        return junos_get_interface_counters(device, interface)
    if device in NOKIA_DEVICES:
        return nokia_get_interface_counters(device, interface)
    return iosxr_get_interface_counters(device, interface, window_minutes)
