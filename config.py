"""
NetCopilot application configuration.

Device credentials (CISCO_USER/PASS, JUNIPER_USER/PASS, NOKIA_USER/PASS,
NETCONF_PORT, NETCONF_TIMEOUT) are consumed directly by each MCP server
from the environment — they are NOT needed here.
"""
from typing import Dict

from dotenv import load_dotenv

from adapters.inventory import load_inventory_devices
from settings import get_setting, get_setting_bool, get_setting_int

load_dotenv()

OPENAI_API_KEY = get_setting("OPENAI_API_KEY", "")

NETBOX_URL = get_setting("NETBOX_URL", "")
NETBOX_TOKEN = get_setting("NETBOX_TOKEN", "")
NETBOX_VERIFY_SSL = get_setting_bool("NETBOX_VERIFY_SSL", True)
NETBOX_FILTER = get_setting("NETBOX_FILTER", "")
NETBOX_MANUFACTURER_CISCO = get_setting("NETBOX_MANUFACTURER_CISCO", "Cisco")
NETBOX_MANUFACTURER_JUNIPER = get_setting("NETBOX_MANUFACTURER_JUNIPER", "Juniper")
NETBOX_MANUFACTURER_NOKIA = get_setting("NETBOX_MANUFACTURER_NOKIA", "Nokia")

NAUTOBOT_URL = get_setting("NAUTOBOT_URL", "")
NAUTOBOT_TOKEN = get_setting("NAUTOBOT_TOKEN", "")
NAUTOBOT_VERIFY_SSL = get_setting_bool("NAUTOBOT_VERIFY_SSL", True)
NAUTOBOT_FILTER = get_setting("NAUTOBOT_FILTER", "")
NAUTOBOT_MANUFACTURER_CISCO = get_setting("NAUTOBOT_MANUFACTURER_CISCO", "Cisco")
NAUTOBOT_MANUFACTURER_JUNIPER = get_setting("NAUTOBOT_MANUFACTURER_JUNIPER", "Juniper")
NAUTOBOT_MANUFACTURER_NOKIA = get_setting("NAUTOBOT_MANUFACTURER_NOKIA", "Nokia")

JIRA_BASE_URL = get_setting("JIRA_BASE_URL", "")
JIRA_EMAIL = get_setting("JIRA_EMAIL", "")
JIRA_API_TOKEN = get_setting("JIRA_API_TOKEN", "")
JIRA_PROJECT_KEY = get_setting("JIRA_PROJECT_KEY", "")
JIRA_DEFAULT_ISSUE_TYPE = get_setting("JIRA_DEFAULT_ISSUE_TYPE", "Task")
JIRA_DEFAULT_PRIORITY = get_setting("JIRA_DEFAULT_PRIORITY", "Medium")
JIRA_DEFAULT_TEAM = get_setting("JIRA_DEFAULT_TEAM", "NOC")

LOG_LEVEL = get_setting("LOG_LEVEL", "INFO").upper()
LOG_TO_FILE = get_setting_bool("LOG_TO_FILE", False)
LOG_DIR = get_setting("LOG_DIR", "logs")

TEAMS_VERIFICATION_TOKEN = get_setting("TEAMS_VERIFICATION_TOKEN", "")

SLACK_VERIFICATION_TOKEN = get_setting("SLACK_VERIFICATION_TOKEN", "")
SLACK_SIGNING_SECRET = get_setting("SLACK_SIGNING_SECRET", "")
SLACK_BOT_TOKEN = get_setting("SLACK_BOT_TOKEN", "")
SLACK_ALERT_CHANNEL = get_setting("SLACK_ALERT_CHANNEL", "")

OPENAI_MODEL = get_setting("OPENAI_MODEL", "gpt-5.6-sol")

ANTHROPIC_API_KEY = get_setting("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = get_setting("ANTHROPIC_MODEL", "claude-opus-5-5")

LLM_PROVIDER = get_setting("LLM_PROVIDER", "openai").lower()
LOCAL_LLM_BASE_URL = get_setting("LOCAL_LLM_BASE_URL", "")
LOCAL_LLM_API_KEY = get_setting("LOCAL_LLM_API_KEY", "local")
LOCAL_LLM_MODEL = get_setting("LOCAL_LLM_MODEL", "llama3.1")

NETCOPILOT_API_KEYS = get_setting("NETCOPILOT_API_KEYS", "")
SLACK_ROLE = get_setting("SLACK_ROLE", "noc")
TEAMS_ROLE = get_setting("TEAMS_ROLE", "noc")

RATE_LIMIT_PER_MIN = get_setting_int("RATE_LIMIT_PER_MIN", 60)

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


def _inventory_params() -> dict[str, str | bool]:
    # Read at call time so Admin UI changes apply on "Reload Inventory".
    return {
        "registry_path": "",  # devices.yaml is loaded by the device registry itself
        "netbox_url": get_setting("NETBOX_URL", ""),
        "netbox_token": get_setting("NETBOX_TOKEN", ""),
        "netbox_verify_ssl": get_setting_bool("NETBOX_VERIFY_SSL", True),
        "netbox_filter": get_setting("NETBOX_FILTER", ""),
        "nautobot_url": get_setting("NAUTOBOT_URL", ""),
        "nautobot_token": get_setting("NAUTOBOT_TOKEN", ""),
        "nautobot_verify_ssl": get_setting_bool("NAUTOBOT_VERIFY_SSL", True),
        "nautobot_filter": get_setting("NAUTOBOT_FILTER", ""),
    }


# adapter vendor → (NetBox manufacturer setting, Nautobot manufacturer setting, default)
_INVENTORY_VENDORS: dict[str, tuple[str, str, str]] = {
    "cisco": ("NETBOX_MANUFACTURER_CISCO", "NAUTOBOT_MANUFACTURER_CISCO", "Cisco"),
    "junos": ("NETBOX_MANUFACTURER_JUNIPER", "NAUTOBOT_MANUFACTURER_JUNIPER", "Juniper"),
    "nokia": ("NETBOX_MANUFACTURER_NOKIA", "NAUTOBOT_MANUFACTURER_NOKIA", "Nokia"),
    "aruba": ("NETBOX_MANUFACTURER_ARUBA", "NAUTOBOT_MANUFACTURER_ARUBA", "Aruba"),
}


def reload_inventory_devices() -> dict[str, int]:
    """Reload the device registry from devices.yaml plus NetBox / Nautobot.

    Devices found in NetBox/Nautobot become routable tool targets; entries in
    devices.yaml override them on name collisions. Returns per-vendor counts
    of the resulting registry.
    """
    from adapters.devices.registry import list_devices, reload_registry, set_external_devices

    params = _inventory_params()
    external: dict[str, dict] = {}
    if params["netbox_url"] or params["nautobot_url"]:
        for vendor, (nb_key, nauto_key, default) in _INVENTORY_VENDORS.items():
            found = load_inventory_devices(
                manufacturer_netbox=get_setting(nb_key, default),
                manufacturer_nautobot=get_setting(nauto_key, default),
                registry_key=f"{vendor}_devices",
                **params,
            )
            for name, host in found.items():
                external[name] = {
                    "host": host,
                    "port": None,
                    "vendor": vendor,
                    "platform": vendor,
                    "site": "",
                    "netconf_port": None,
                    "source": "inventory",
                }
    set_external_devices(external)
    reload_registry()

    counts = {vendor: 0 for vendor in _INVENTORY_VENDORS}
    for device in list_devices():
        counts[device["vendor"]] = counts.get(device["vendor"], 0) + 1
    return counts
