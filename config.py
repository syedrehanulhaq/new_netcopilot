"""
NetCopilot application configuration.

Device credentials (CISCO_USER/PASS, JUNIPER_USER/PASS, NOKIA_USER/PASS,
NETCONF_PORT, NETCONF_TIMEOUT) are consumed directly by each MCP server
from the environment — they are NOT needed here.
"""
import os
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

OPENAI_MODEL = get_setting("OPENAI_MODEL", "gpt-4.1-mini")

ANTHROPIC_API_KEY = get_setting("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = get_setting("ANTHROPIC_MODEL", "claude-sonnet-4-6")

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
    return {
        "registry_path": get_setting(
            "DEVICE_REGISTRY_PATH",
            os.path.join(os.path.dirname(__file__), "devices.yaml"),
        ),
        "netbox_url": NETBOX_URL,
        "netbox_token": NETBOX_TOKEN,
        "netbox_verify_ssl": NETBOX_VERIFY_SSL,
        "netbox_filter": NETBOX_FILTER,
        "nautobot_url": NAUTOBOT_URL,
        "nautobot_token": NAUTOBOT_TOKEN,
        "nautobot_verify_ssl": NAUTOBOT_VERIFY_SSL,
        "nautobot_filter": NAUTOBOT_FILTER,
    }


def reload_inventory_devices() -> dict[str, int]:
    """Reload device inventory from YAML / NetBox / Nautobot.

    Returns a count dict for logging; the loaded data is available via
    devices.yaml (read by each MCP server) rather than in-memory dicts.
    """
    params = _inventory_params()
    cisco = load_inventory_devices(
        manufacturer_netbox=NETBOX_MANUFACTURER_CISCO,
        manufacturer_nautobot=NAUTOBOT_MANUFACTURER_CISCO,
        registry_key="cisco_devices",
        **params,
    )
    juniper = load_inventory_devices(
        manufacturer_netbox=NETBOX_MANUFACTURER_JUNIPER,
        manufacturer_nautobot=NAUTOBOT_MANUFACTURER_JUNIPER,
        registry_key="juniper_devices",
        **params,
    )
    nokia = load_inventory_devices(
        manufacturer_netbox=NETBOX_MANUFACTURER_NOKIA,
        manufacturer_nautobot=NAUTOBOT_MANUFACTURER_NOKIA,
        registry_key="nokia_devices",
        **params,
    )
    return {
        "cisco": len(cisco),
        "juniper": len(juniper),
        "nokia": len(nokia),
    }
