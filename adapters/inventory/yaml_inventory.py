import os
from typing import Dict


class YAMLInventoryAdapter:
    name = "yaml"

    def __init__(self, registry_path: str):
        self.registry_path = registry_path

    def load_devices(self, *, manufacturer: str, registry_key: str) -> Dict[str, str]:
        try:
            import yaml
        except Exception:
            print("WARNING: PyYAML not installed. Using empty device registry.")
            return {}

        path = self.registry_path
        if not path:
            path = os.path.join(os.path.dirname(__file__), "..", "..", "devices.yaml")
            path = os.path.abspath(path)

        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}
        except FileNotFoundError:
            print(f"WARNING: Device registry not found at {path}.")
            return {}

        devices = data.get(registry_key, {})
        if not isinstance(devices, dict):
            print(f"WARNING: '{registry_key}' in devices.yaml is not a mapping.")
            return {}

        return {str(k): str(v) for k, v in devices.items()}
