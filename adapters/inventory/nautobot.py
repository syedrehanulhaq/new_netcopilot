from typing import Dict

_MAX_PAGES = 100  # 100 pages x 1000 devices


class NautobotInventoryAdapter:
    name = "nautobot"

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
        results: list = []
        try:
            import httpx

            with httpx.Client(timeout=15, verify=self.verify_ssl) as client:
                next_url, next_params = url, params
                for _ in range(_MAX_PAGES):
                    resp = client.get(next_url, headers=headers, params=next_params)
                    if resp.status_code >= 300:
                        print(f"WARNING: Nautobot API error {resp.status_code}.")
                        return {}
                    data = resp.json()
                    results.extend(data.get("results", []))
                    next_url = data.get("next")
                    if not next_url:
                        break
                    next_params = None  # "next" already carries the query string
        except Exception as exc:
            print(f"WARNING: Nautobot fetch failed: {exc}")
            return {}

        devices: Dict[str, str] = {}
        for item in results:
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
