import os
from typing import Dict

from dotenv import load_dotenv
from settings import get_setting, get_setting_bool, get_setting_int

load_dotenv()

OPENAI_API_KEY = get_setting("OPENAI_API_KEY", "")

CISCO_USER = get_setting("CISCO_USER", "")
CISCO_PASS = get_setting("CISCO_PASS", "")

JUNIPER_USER = get_setting("JUNIPER_USER", "")
JUNIPER_PASS = get_setting("JUNIPER_PASS", "")
NOKIA_USER = get_setting("NOKIA_USER", "")
NOKIA_PASS = get_setting("NOKIA_PASS", "")
NETCONF_PORT = get_setting_int("NETCONF_PORT", 830)
NETCONF_TIMEOUT = get_setting_int("NETCONF_TIMEOUT", 10)

NETBOX_URL = get_setting("NETBOX_URL", "")
NETBOX_TOKEN = get_setting("NETBOX_TOKEN", "")
NETBOX_VERIFY_SSL = get_setting_bool("NETBOX_VERIFY_SSL", True)
NETBOX_FILTER = get_setting("NETBOX_FILTER", "")
NETBOX_MANUFACTURER_CISCO = get_setting("NETBOX_MANUFACTURER_CISCO", "Cisco")
NETBOX_MANUFACTURER_JUNIPER = get_setting("NETBOX_MANUFACTURER_JUNIPER", "Juniper")
NETBOX_MANUFACTURER_NOKIA = get_setting("NETBOX_MANUFACTURER_NOKIA", "Nokia")
JIRA_BASE_URL = get_setting("JIRA_BASE_URL", "")
JIRA_EMAIL = get_setting("JIRA_EMAIL", "")
JIRA_API_TOKEN = get_setting("JIRA_API_TOKEN", "")
JIRA_PROJECT_KEY = get_setting("JIRA_PROJECT_KEY", "")
JIRA_DEFAULT_ISSUE_TYPE = get_setting("JIRA_DEFAULT_ISSUE_TYPE", "Task")
JIRA_DEFAULT_PRIORITY = get_setting("JIRA_DEFAULT_PRIORITY", "Medium")
JIRA_DEFAULT_TEAM = get_setting("JIRA_DEFAULT_TEAM", "NOC")

LOG_LEVEL = get_setting("LOG_LEVEL", "INFO").upper()

TEAMS_VERIFICATION_TOKEN = get_setting("TEAMS_VERIFICATION_TOKEN", "")

SLACK_VERIFICATION_TOKEN = get_setting("SLACK_VERIFICATION_TOKEN", "")
SLACK_SIGNING_SECRET = get_setting("SLACK_SIGNING_SECRET", "")
SLACK_BOT_TOKEN = get_setting("SLACK_BOT_TOKEN", "")
SLACK_ALERT_CHANNEL = get_setting("SLACK_ALERT_CHANNEL", "")

OPENAI_MODEL = get_setting("OPENAI_MODEL", "gpt-4.1-mini")

LLM_PROVIDER = get_setting("LLM_PROVIDER", "openai").lower()
LOCAL_LLM_BASE_URL = get_setting("LOCAL_LLM_BASE_URL", "")
LOCAL_LLM_API_KEY = get_setting("LOCAL_LLM_API_KEY", "local")
LOCAL_LLM_MODEL = get_setting("LOCAL_LLM_MODEL", "llama3.1")

NETCOPILOT_API_KEYS = get_setting("NETCOPILOT_API_KEYS", "")
SLACK_ROLE = get_setting("SLACK_ROLE", "noc")
TEAMS_ROLE = get_setting("TEAMS_ROLE", "noc")

RATE_LIMIT_PER_MIN = get_setting_int("RATE_LIMIT_PER_MIN", 60)

PROMETHEUS_URL = get_setting("PROMETHEUS_URL", "")
PROMETHEUS_FLAP_QUERY = get_setting(
    "PROMETHEUS_FLAP_QUERY",
    "topk({limit}, netcopilot_link_flaps_total)",
)
PROMETHEUS_LAST_FLAP_QUERY = get_setting(
    "PROMETHEUS_LAST_FLAP_QUERY",
    "netcopilot_link_last_flap_timestamp_seconds",
)

ALERT_INGEST_TOKEN = get_setting("ALERT_INGEST_TOKEN", "")

AUTH0_DOMAIN = get_setting("AUTH0_DOMAIN", "")
AUTH0_CLIENT_ID = get_setting("AUTH0_CLIENT_ID", "")
AUTH0_CLIENT_SECRET = get_setting("AUTH0_CLIENT_SECRET", "")
AUTH0_CALLBACK_URL = get_setting("AUTH0_CALLBACK_URL", "http://127.0.0.1:8001/auth/callback")
AUTH0_LOGOUT_URL = get_setting("AUTH0_LOGOUT_URL", "http://127.0.0.1:8001/")
AUTH0_AUDIENCE = get_setting("AUTH0_AUDIENCE", "")
AUTH0_DEFAULT_ROLE = get_setting("AUTH0_DEFAULT_ROLE", "noc")
AUTH0_ROLE_CLAIM = get_setting("AUTH0_ROLE_CLAIM", "")
AUTH0_GROUP_CLAIM = get_setting("AUTH0_GROUP_CLAIM", "")
AUTH0_GROUP_ROLE_MAP = get_setting("AUTH0_GROUP_ROLE_MAP", "")
SESSION_SECRET = get_setting("SESSION_SECRET", "change-me")


def _load_cisco_devices() -> Dict[str, str]:
    registry_path = get_setting(
        "DEVICE_REGISTRY_PATH",
        os.path.join(os.path.dirname(__file__), "devices.yaml"),
    )
    try:
        import yaml
    except Exception:
        print("WARNING: PyYAML not installed. Using empty device registry.")
        return {}

    try:
        with open(registry_path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except FileNotFoundError:
        print(f"WARNING: Device registry not found at {registry_path}.")
        return {}

    devices = data.get("cisco_devices", {})
    if not isinstance(devices, dict):
        print("WARNING: 'cisco_devices' in devices.yaml is not a mapping.")
        return {}

    return {str(k): str(v) for k, v in devices.items()}


def _load_juniper_devices() -> Dict[str, str]:
    registry_path = get_setting(
        "DEVICE_REGISTRY_PATH",
        os.path.join(os.path.dirname(__file__), "devices.yaml"),
    )
    try:
        import yaml
    except Exception:
        print("WARNING: PyYAML not installed. Using empty Juniper device registry.")
        return {}

    try:
        with open(registry_path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except FileNotFoundError:
        print(f"WARNING: Device registry not found at {registry_path}.")
        return {}

    devices = data.get("juniper_devices", {})
    if not isinstance(devices, dict):
        print("WARNING: 'juniper_devices' in devices.yaml is not a mapping.")
        return {}

    return {str(k): str(v) for k, v in devices.items()}


def _load_nokia_devices() -> Dict[str, str]:
    registry_path = get_setting(
        "DEVICE_REGISTRY_PATH",
        os.path.join(os.path.dirname(__file__), "devices.yaml"),
    )
    try:
        import yaml
    except Exception:
        print("WARNING: PyYAML not installed. Using empty Nokia device registry.")
        return {}

    try:
        with open(registry_path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except FileNotFoundError:
        print(f"WARNING: Device registry not found at {registry_path}.")
        return {}

    devices = data.get("nokia_devices", {})
    if not isinstance(devices, dict):
        print("WARNING: 'nokia_devices' in devices.yaml is not a mapping.")
        return {}

    return {str(k): str(v) for k, v in devices.items()}


def _load_devices_from_netbox(manufacturer: str) -> Dict[str, str]:
    if not NETBOX_URL or not NETBOX_TOKEN:
        return {}

    base = NETBOX_URL.rstrip("/")
    url = f"{base}/api/dcim/devices/"
    params = {
        "status": "active",
        "manufacturer": manufacturer,
        "limit": 1000,
    }
    if NETBOX_FILTER:
        for pair in NETBOX_FILTER.split("&"):
            if "=" in pair:
                key, value = pair.split("=", 1)
                params[key] = value

    headers = {"Authorization": f"Token {NETBOX_TOKEN}"}
    try:
        import httpx

        with httpx.Client(timeout=15, verify=NETBOX_VERIFY_SSL) as client:
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


# Name used in chat -> actual hostname/IP
CISCO_DEVICES: Dict[str, str] = _load_devices_from_netbox(NETBOX_MANUFACTURER_CISCO)
CISCO_DEVICES.update(_load_cisco_devices())
if not CISCO_DEVICES:
    print("WARNING: No Cisco devices loaded. Check devices.yaml or DEVICE_REGISTRY_PATH.")

JUNIPER_DEVICES: Dict[str, str] = _load_devices_from_netbox(NETBOX_MANUFACTURER_JUNIPER)
JUNIPER_DEVICES.update(_load_juniper_devices())
if not JUNIPER_DEVICES:
    print("WARNING: No Juniper devices loaded. Check devices.yaml or DEVICE_REGISTRY_PATH.")

NOKIA_DEVICES: Dict[str, str] = _load_devices_from_netbox(NETBOX_MANUFACTURER_NOKIA)
NOKIA_DEVICES.update(_load_nokia_devices())
if not NOKIA_DEVICES:
    print("WARNING: No Nokia devices loaded. Check devices.yaml or DEVICE_REGISTRY_PATH.")
