from typing import Dict, Protocol


class InventoryAdapter(Protocol):
    name: str

    def load_devices(self, *, manufacturer: str, registry_key: str) -> Dict[str, str]: ...
