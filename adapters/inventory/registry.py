from typing import Dict

from adapters.inventory.netbox import NetBoxInventoryAdapter
from adapters.inventory.nautobot import NautobotInventoryAdapter
from adapters.inventory.yaml_inventory import YAMLInventoryAdapter


def load_inventory_devices(
    *,
    manufacturer_netbox: str,
    manufacturer_nautobot: str,
    registry_key: str,
    registry_path: str,
    netbox_url: str,
    netbox_token: str,
    netbox_verify_ssl: bool,
    netbox_filter: str,
    nautobot_url: str,
    nautobot_token: str,
    nautobot_verify_ssl: bool,
    nautobot_filter: str,
) -> Dict[str, str]:
    devices: Dict[str, str] = {}
    netbox = NetBoxInventoryAdapter(netbox_url, netbox_token, netbox_verify_ssl, netbox_filter)
    devices.update(netbox.load_devices(manufacturer=manufacturer_netbox, registry_key=registry_key))

    nautobot = NautobotInventoryAdapter(
        nautobot_url, nautobot_token, nautobot_verify_ssl, nautobot_filter
    )
    devices.update(nautobot.load_devices(manufacturer=manufacturer_nautobot, registry_key=registry_key))

    if registry_path:
        yaml_adapter = YAMLInventoryAdapter(registry_path)
        devices.update(
            yaml_adapter.load_devices(
                manufacturer=manufacturer_netbox or manufacturer_nautobot,
                registry_key=registry_key,
            )
        )

    return devices
