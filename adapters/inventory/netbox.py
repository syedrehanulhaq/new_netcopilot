from typing import Dict


class NetBoxInventoryAdapter:
    name = "netbox"

    def __init__(self, base_url: str, token: str, verify_ssl: bool, filter_query: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.verify_ssl = verify_ssl
        self.filter_query = filter_query

    def load_devices(self, *, manufacturer: str, registry_key: str) -> Dict[str, str]:
        if not self.base_url or not self.token:
            return {}

        url = f"{self.base_url}/api/dcim/devices/"
        params = {
            "status": "active",
            "manufacturer": manufacturer,
            "limit": 1000,
        }
        if self.filter_query:
            for pair in self.filter_query.split("&"):
                if "=" in pair:
                    key, value = pair.split("=", 1)
                    params[key] = value

        headers = {"Authorization": f"Token {self.token}"}
        try:
            import httpx

            with httpx.Client(timeout=15, verify=self.verify_ssl) as client:
                resp = client.get(url, headers=headers, params=params)
            if resp.status_code >= 300:
                print(f"WARNING: NetBox API error {resp.status_code}.")
                return {}
            data = resp.json()
        except Exception as exc:
            print(f"WARNING: NetBox fetch failed: {exc}")
            return {}

        devices: Dict[str, str] = {}
        for item in data.get("results", []):
            name = item.get("name")
            ip = None
            primary_ip4 = item.get("primary_ip4") or {}
            primary_ip6 = item.get("primary_ip6") or {}
            ip = primary_ip4.get("address") or primary_ip6.get("address")
            if ip and "/" in ip:
                ip = ip.split("/", 1)[0]
            if name and ip:
                devices[str(name)] = str(ip)
        return devices
