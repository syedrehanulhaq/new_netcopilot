import asyncio
import base64
import csv
import hashlib
import hmac
import json
import logging
import logging.handlers
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode

import httpx
import yaml
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from adapters.chat import get_slack_adapter
from adapters.llm import get_llm_adapter
from config import (
    AUTH0_AUDIENCE,
    AUTH0_CALLBACK_URL,
    AUTH0_CLIENT_ID,
    AUTH0_CLIENT_SECRET,
    AUTH0_DEFAULT_ROLE,
    AUTH0_DOMAIN,
    AUTH0_GROUP_CLAIM,
    AUTH0_GROUP_ROLE_MAP,
    AUTH0_LOGOUT_URL,
    AUTH0_ROLE_CLAIM,
    ALERT_INGEST_TOKEN,
    JIRA_BASE_URL,
    JIRA_DEFAULT_ISSUE_TYPE,
    JIRA_DEFAULT_PRIORITY,
    JIRA_DEFAULT_TEAM,
    LOG_DIR,
    LOG_LEVEL,
    LOG_TO_FILE,
    NETCOPILOT_API_KEYS,
    RATE_LIMIT_PER_MIN,
    SESSION_SECRET,
    SLACK_ALERT_CHANNEL,
    SLACK_BOT_TOKEN,
    SLACK_ROLE,
    SLACK_SIGNING_SECRET,
    SLACK_VERIFICATION_TOKEN,
    TEAMS_ROLE,
    TEAMS_VERIFICATION_TOKEN,
    reload_inventory_devices,
)
from adapters.devices.dispatcher import ToolDispatcher
from adapters.devices.registry import (
    device_inventory_summary,
    list_devices_detailed,
    normalize_vendor,
    registry_path,
)
from llm.prompts import build_system_prompt
from mcp_client.client import MCPClient
from playbooks.store import (
    delete_playbook,
    format_playbook_summary,
    list_playbooks,
    upsert_playbooks,
)
from settings import (
    data_path,
    get_setting,
    get_setting_bool,
    get_setting_int,
    list_settings,
    set_settings,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

APP_VERSION = "2.0.0"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=True)


logger = logging.getLogger("netcopilot")
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
if not logger.handlers:
    if LOG_TO_FILE:
        os.makedirs(LOG_DIR, exist_ok=True)
        _handler: logging.Handler = logging.handlers.RotatingFileHandler(
            os.path.join(LOG_DIR, "netcopilot.log"),
            maxBytes=1_000_000,
            backupCount=3,
        )
    else:
        _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(JsonFormatter())
    logger.addHandler(_handler)

audit_logger = logging.getLogger("netcopilot.audit")
audit_logger.setLevel(logging.INFO)
if not audit_logger.handlers:
    if LOG_TO_FILE:
        os.makedirs(LOG_DIR, exist_ok=True)
        _audit_handler: logging.Handler = logging.handlers.RotatingFileHandler(
            os.path.join(LOG_DIR, "audit.log"),
            maxBytes=1_000_000,
            backupCount=5,
        )
    else:
        _audit_handler = logging.StreamHandler(sys.stdout)
    _audit_handler.setFormatter(JsonFormatter())
    audit_logger.addHandler(_audit_handler)


# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------

_mcp_client: Optional[MCPClient] = None
_dispatcher: Optional[ToolDispatcher] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _mcp_client, _dispatcher
    if SESSION_SECRET == "change-me":
        logger.warning(
            "SESSION_SECRET is set to the insecure default 'change-me'. "
            "Set a strong random value in production."
        )
    if not NETCOPILOT_API_KEYS and not AUTH0_DOMAIN:
        logger.warning(
            "No API keys and no AUTH0_DOMAIN configured — all requests get admin access. "
            "Set NETCOPILOT_API_KEYS or AUTH0_DOMAIN before deploying."
        )
    _init_memory_db()
    _init_alerts_db()
    # devices.yaml + NetBox/Nautobot (if configured) → device registry
    await asyncio.to_thread(reload_inventory_devices)
    _mcp_client = MCPClient(config_path="mcp_servers.yaml")
    await _mcp_client.start()
    _dispatcher = ToolDispatcher(mcp_client=_mcp_client)
    logger.info(
        "startup version=%s vendor_mcp_connected=%d",
        APP_VERSION,
        len(_mcp_client._connections),
    )
    yield
    await _mcp_client.stop()


app = FastAPI(title="NetCopilot", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site="lax",
    https_only=False,
)
app.mount("/static", StaticFiles(directory="static"), name="static")


# Security headers on every response
from starlette.middleware.base import BaseHTTPMiddleware

class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-XSS-Protection", "1; mode=block")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return response

app.add_middleware(_SecurityHeadersMiddleware)


# ---------------------------------------------------------------------------
# Auth0 OAuth (optional)
# ---------------------------------------------------------------------------

if AUTH0_DOMAIN:
    from authlib.integrations.starlette_client import OAuth

    oauth = OAuth()
    oauth.register(
        name="auth0",
        client_id=AUTH0_CLIENT_ID,
        client_secret=AUTH0_CLIENT_SECRET,
        server_metadata_url=f"https://{AUTH0_DOMAIN}/.well-known/openid-configuration",
        client_kwargs={"scope": "openid profile email"},
    )
else:
    oauth = None


# ---------------------------------------------------------------------------
# Settings schema (for admin UI)
# ---------------------------------------------------------------------------

_SETTINGS_SCHEMA = [
    {"key": "LLM_PROVIDER", "label": "LLM provider (openai / anthropic / local)", "category": "LLM"},
    {"key": "OPENAI_API_KEY", "label": "OpenAI API key", "category": "LLM", "secret": True},
    {"key": "OPENAI_MODEL", "label": "OpenAI model", "category": "LLM"},
    {"key": "ANTHROPIC_API_KEY", "label": "Anthropic API key", "category": "LLM", "secret": True},
    {"key": "ANTHROPIC_MODEL", "label": "Anthropic model", "category": "LLM"},
    {"key": "LOCAL_LLM_BASE_URL", "label": "Local LLM base URL", "category": "LLM"},
    {"key": "LOCAL_LLM_API_KEY", "label": "Local LLM API key", "category": "LLM", "secret": True},
    {"key": "LOCAL_LLM_MODEL", "label": "Local LLM model", "category": "LLM"},
    {"key": "CISCO_USER", "label": "Cisco username", "category": "Devices"},
    {"key": "CISCO_PASS", "label": "Cisco password", "category": "Devices", "secret": True},
    {"key": "JUNIPER_USER", "label": "Juniper username", "category": "Devices"},
    {"key": "JUNIPER_PASS", "label": "Juniper password", "category": "Devices", "secret": True},
    {"key": "NOKIA_USER", "label": "Nokia username", "category": "Devices"},
    {"key": "NOKIA_PASS", "label": "Nokia password", "category": "Devices", "secret": True},
    {"key": "ARUBA_USER", "label": "Aruba username", "category": "Devices"},
    {"key": "ARUBA_PASS", "label": "Aruba password", "category": "Devices", "secret": True},
    {"key": "NETCONF_PORT", "label": "NETCONF port", "category": "Devices"},
    {"key": "NETCONF_TIMEOUT", "label": "NETCONF timeout (seconds)", "category": "Devices"},
    {"key": "NETBOX_URL", "label": "NetBox URL", "category": "Inventory"},
    {"key": "NETBOX_TOKEN", "label": "NetBox token", "category": "Inventory", "secret": True},
    {"key": "NETBOX_VERIFY_SSL", "label": "NetBox verify SSL", "category": "Inventory"},
    {"key": "NETBOX_FILTER", "label": "NetBox filter query", "category": "Inventory"},
    {"key": "NETBOX_MANUFACTURER_CISCO", "label": "NetBox Cisco manufacturer", "category": "Inventory"},
    {"key": "NETBOX_MANUFACTURER_JUNIPER", "label": "NetBox Juniper manufacturer", "category": "Inventory"},
    {"key": "NETBOX_MANUFACTURER_NOKIA", "label": "NetBox Nokia manufacturer", "category": "Inventory"},
    {"key": "NETBOX_MANUFACTURER_ARUBA", "label": "NetBox Aruba manufacturer", "category": "Inventory"},
    {"key": "NAUTOBOT_URL", "label": "Nautobot URL", "category": "Inventory"},
    {"key": "NAUTOBOT_TOKEN", "label": "Nautobot token", "category": "Inventory", "secret": True},
    {"key": "NAUTOBOT_VERIFY_SSL", "label": "Nautobot verify SSL", "category": "Inventory"},
    {"key": "NAUTOBOT_FILTER", "label": "Nautobot filter query", "category": "Inventory"},
    {"key": "NAUTOBOT_MANUFACTURER_CISCO", "label": "Nautobot Cisco manufacturer", "category": "Inventory"},
    {"key": "NAUTOBOT_MANUFACTURER_JUNIPER", "label": "Nautobot Juniper manufacturer", "category": "Inventory"},
    {"key": "NAUTOBOT_MANUFACTURER_NOKIA", "label": "Nautobot Nokia manufacturer", "category": "Inventory"},
    {"key": "NAUTOBOT_MANUFACTURER_ARUBA", "label": "Nautobot Aruba manufacturer", "category": "Inventory"},
    {
        "key": "INVENTORY_WEBHOOK_TOKEN",
        "label": "Inventory webhook token (X-Webhook-Token)",
        "category": "Inventory",
        "secret": True,
    },
    {
        "key": "INVENTORY_WEBHOOK_DEBOUNCE_SECONDS",
        "label": "Inventory webhook debounce (seconds)",
        "category": "Inventory",
    },
    {"key": "JIRA_BASE_URL", "label": "Jira base URL", "category": "Ticketing"},
    {"key": "JIRA_EMAIL", "label": "Jira email", "category": "Ticketing"},
    {"key": "JIRA_API_TOKEN", "label": "Jira API token", "category": "Ticketing", "secret": True},
    {"key": "JIRA_PROJECT_KEY", "label": "Jira project key", "category": "Ticketing"},
    {"key": "JIRA_DEFAULT_ISSUE_TYPE", "label": "Jira default issue type", "category": "Ticketing"},
    {"key": "JIRA_DEFAULT_PRIORITY", "label": "Jira default priority", "category": "Ticketing"},
    {"key": "JIRA_DEFAULT_TEAM", "label": "Jira default team", "category": "Ticketing"},
    {"key": "SLACK_VERIFICATION_TOKEN", "label": "Slack verification token", "category": "Slack", "secret": True},
    {"key": "SLACK_SIGNING_SECRET", "label": "Slack signing secret", "category": "Slack", "secret": True},
    {"key": "SLACK_BOT_TOKEN", "label": "Slack bot token", "category": "Slack", "secret": True},
    {"key": "SLACK_ALERT_CHANNEL", "label": "Slack alert channel", "category": "Slack"},
    {"key": "SLACK_ROLE", "label": "Slack default role", "category": "Slack"},
    {"key": "TEAMS_VERIFICATION_TOKEN", "label": "Teams verification token", "category": "Teams", "secret": True},
    {"key": "TEAMS_ROLE", "label": "Teams default role", "category": "Teams"},
    {"key": "PROMETHEUS_URL", "label": "Prometheus base URL", "category": "Prometheus"},
    {"key": "PROMETHEUS_FLAP_QUERY", "label": "Flap query", "category": "Prometheus"},
    {"key": "PROMETHEUS_LAST_FLAP_QUERY", "label": "Last flap query", "category": "Prometheus"},
    {"key": "SYSLOG_URL", "label": "Syslog/Loki query URL", "category": "Syslog"},
    {"key": "SYSLOG_TOKEN", "label": "Syslog API token", "category": "Syslog", "secret": True},
    {"key": "SYSLOG_QUERY_PARAM", "label": "Syslog query param", "category": "Syslog"},
    {"key": "ALERT_INGEST_TOKEN", "label": "Alert ingest token", "category": "Alerts", "secret": True},
    {"key": "AUTH0_DOMAIN", "label": "Auth0 domain", "category": "Auth"},
    {"key": "AUTH0_CLIENT_ID", "label": "Auth0 client ID", "category": "Auth"},
    {"key": "AUTH0_CLIENT_SECRET", "label": "Auth0 client secret", "category": "Auth", "secret": True},
    {"key": "AUTH0_CALLBACK_URL", "label": "Auth0 callback URL", "category": "Auth"},
    {"key": "AUTH0_LOGOUT_URL", "label": "Auth0 logout URL", "category": "Auth"},
    {"key": "AUTH0_AUDIENCE", "label": "Auth0 audience", "category": "Auth"},
    {"key": "AUTH0_DEFAULT_ROLE", "label": "Auth0 default role", "category": "Auth"},
    {"key": "AUTH0_ROLE_CLAIM", "label": "Auth0 role claim", "category": "Auth"},
    {"key": "AUTH0_GROUP_CLAIM", "label": "Auth0 group claim", "category": "Auth"},
    {"key": "AUTH0_GROUP_ROLE_MAP", "label": "Auth0 group role map", "category": "Auth"},
    {"key": "NETCOPILOT_API_KEYS", "label": "API keys (key:role)", "category": "Security", "secret": True},
    {"key": "RATE_LIMIT_PER_MIN", "label": "Rate limit per minute", "category": "Security"},
    {"key": "TOOL_ALLOWLIST", "label": "Tool allowlist (comma-separated)", "category": "Security"},
    {"key": "TOOL_DENYLIST", "label": "Tool denylist (comma-separated)", "category": "Security"},
    {"key": "TOOL_RESULT_MAX_CHARS", "label": "Max characters per tool result sent to the LLM", "category": "Security"},
    {"key": "CHAT_RETENTION_DAYS", "label": "Chat retention (days)", "category": "Security"},
    {"key": "ALERT_RETENTION_DAYS", "label": "Alert retention (days)", "category": "Security"},
    {"key": "LOG_REDACTION_ENABLED", "label": "Log redaction enabled", "category": "Security"},
    {"key": "LOG_REDACTION_FIELDS", "label": "Log redaction fields", "category": "Security"},
    {"key": "SESSION_SECRET", "label": "Session secret", "category": "Security", "secret": True},
    {"key": "LOG_TO_FILE", "label": "Log to file (true/false)", "category": "Logging"},
    {"key": "LOG_DIR", "label": "Log directory", "category": "Logging"},
]


# ---------------------------------------------------------------------------
# Rate limiting, metrics
# ---------------------------------------------------------------------------

_metrics_lock = threading.Lock()
_metrics: dict[str, Any] = {
    "requests_total": 0,
    "tool_calls_total": 0,
    "tool_errors_total": 0,
    "tool_counts": {},
}

_rate_limit_lock = threading.Lock()
_rate_limit: dict[str, list[float]] = {}
_rate_limit_window = 60
_rate_limit_max = RATE_LIMIT_PER_MIN


_RATE_LIMIT_MAX_CLIENTS = 10_000  # cap dict size to prevent unbounded growth


def _enforce_rate_limit(client_id: str) -> None:
    now = time.time()
    with _rate_limit_lock:
        timestamps = [ts for ts in _rate_limit.get(client_id, []) if now - ts < _rate_limit_window]
        if len(timestamps) >= _rate_limit_max:
            raise HTTPException(status_code=429, detail="Rate limit exceeded.")
        timestamps.append(now)
        _rate_limit[client_id] = timestamps
        # Evict fully-expired entries when dict grows too large
        if len(_rate_limit) > _RATE_LIMIT_MAX_CLIENTS:
            stale = [k for k, v in _rate_limit.items()
                     if k != client_id and not any(now - ts < _rate_limit_window for ts in v)]
            for k in stale:
                del _rate_limit[k]


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _parse_api_keys() -> dict[str, str]:
    keys: dict[str, str] = {}
    for item in NETCOPILOT_API_KEYS.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            key, role = item.split(":", 1)
            keys[key.strip()] = role.strip()
        else:
            keys[item] = "noc"
    return keys


def _get_role_from_request(request: Request) -> str:
    keys = _parse_api_keys()
    if not keys and not AUTH0_DOMAIN:
        return "admin"
    api_key = request.headers.get("X-API-Key", "")
    role = keys.get(api_key)
    if role:
        return role
    user = request.session.get("user") if hasattr(request, "session") else None
    if user:
        return user.get("role", AUTH0_DEFAULT_ROLE)
    raise HTTPException(status_code=403, detail="Invalid API key.")


def _parse_group_role_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in AUTH0_GROUP_ROLE_MAP.split(","):
        item = item.strip()
        if ":" in item:
            group, role = item.split(":", 1)
            mapping[group.strip()] = role.strip()
    return mapping


def _role_from_groups(groups: list[str]) -> Optional[str]:
    mapping = _parse_group_role_map()
    for group in groups:
        if group in mapping:
            return mapping[group]
    return None


# ---------------------------------------------------------------------------
# Tool filtering (allowlist/denylist — role access handled by ToolDispatcher.get_all_tools)
# ---------------------------------------------------------------------------

def _is_tool_allowed(tool_name: str) -> bool:
    denylist = {
        item.strip()
        for item in get_setting("TOOL_DENYLIST", "").split(",")
        if item.strip()
    }
    allowlist = {
        item.strip()
        for item in get_setting("TOOL_ALLOWLIST", "").split(",")
        if item.strip()
    }
    if tool_name in denylist:
        return False
    if allowlist and tool_name not in allowlist:
        return False
    return True


# ---------------------------------------------------------------------------
# Memory (conversation history)
# ---------------------------------------------------------------------------

_memory_lock = threading.Lock()
_memory_limit = 12
_memory_db_path = data_path("memory.db")


def _init_memory_db() -> None:
    with sqlite3.connect(_memory_db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )


def _build_device_inventory_summary() -> str:
    """Return a compact vendor→device list for the system prompt (from registry)."""
    return device_inventory_summary()


def _load_memory(user: str) -> list[dict[str, str]]:
    with sqlite3.connect(_memory_db_path) as conn:
        rows = conn.execute(
            "SELECT role, content FROM memory WHERE user = ? ORDER BY id DESC LIMIT ?",
            (user, _memory_limit * 2),
        ).fetchall()
    rows.reverse()
    return [{"role": role, "content": content} for role, content in rows]


def _save_memory(user: str, turns: list[dict[str, str]]) -> None:
    now = time.time()
    retention_days = get_setting_int("CHAT_RETENTION_DAYS", 14)
    cutoff = now - retention_days * 86400
    with sqlite3.connect(_memory_db_path) as conn:
        conn.executemany(
            "INSERT INTO memory (user, role, content, created_at) VALUES (?, ?, ?, ?)",
            [(user, t["role"], t["content"], now) for t in turns],
        )
        conn.execute("DELETE FROM memory WHERE created_at < ?", (cutoff,))
        conn.execute(
            """
            DELETE FROM memory
            WHERE user = ? AND id NOT IN (
                SELECT id FROM memory WHERE user = ? ORDER BY id DESC LIMIT ?
            )
            """,
            (user, user, _memory_limit * 2),
        )


def _clear_memory(user: str) -> int:
    with sqlite3.connect(_memory_db_path) as conn:
        return conn.execute("DELETE FROM memory WHERE user = ?", (user,)).rowcount


def _resolve_user(request: Request, supplied: str) -> str:
    """
    Identity used to key conversation memory.

    A signed-in (Auth0) session always wins over the client-supplied name, so
    one user cannot read or pollute another user's history by sending their
    name. API-key callers name their own users, namespaced per key. With no
    auth configured the client-supplied id is used as-is.
    """
    session_user = request.session.get("user") if "session" in request.scope else None
    if session_user and (session_user.get("email") or session_user.get("name")):
        return str(session_user.get("email") or session_user.get("name"))
    supplied = (supplied or "unknown").strip()[:128] or "unknown"
    api_key = request.headers.get("X-API-Key", "")
    if api_key and api_key in _parse_api_keys():
        key_id = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:10]
        return f"apikey-{key_id}:{supplied}"
    return supplied


# ---------------------------------------------------------------------------
# Alerts DB
# ---------------------------------------------------------------------------

_alert_db_path = data_path("alerts.db")
_inventory_webhook_lock = threading.Lock()
_inventory_webhook_last_reload = 0.0


def _init_alerts_db() -> None:
    with sqlite3.connect(_alert_db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                source TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )


def _save_alert(channel: str, source: str, payload: dict[str, Any]) -> None:
    now = time.time()
    retention_days = get_setting_int("ALERT_RETENTION_DAYS", 7)
    cutoff = now - retention_days * 86400
    with sqlite3.connect(_alert_db_path) as conn:
        conn.execute(
            "INSERT INTO alerts (channel, source, payload, created_at) VALUES (?, ?, ?, ?)",
            (channel, source, json.dumps(payload), now),
        )
        conn.execute("DELETE FROM alerts WHERE created_at < ?", (cutoff,))


def _get_latest_alert(
    channel: str,
    source: Optional[str] = None,
    exclude_source: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    with sqlite3.connect(_alert_db_path) as conn:
        if source:
            row = conn.execute(
                "SELECT payload FROM alerts WHERE channel = ? AND source = ? ORDER BY id DESC LIMIT 1",
                (channel, source),
            ).fetchone()
        elif exclude_source:
            row = conn.execute(
                "SELECT payload FROM alerts WHERE channel = ? AND source != ? ORDER BY id DESC LIMIT 1",
                (channel, exclude_source),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT payload FROM alerts WHERE channel = ? ORDER BY id DESC LIMIT 1",
                (channel,),
            ).fetchone()
    if row and row[0]:
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return None
    return None


def _get_latest_analysis(channel: str) -> Optional[str]:
    payload = _get_latest_alert(channel, source="analysis")
    if payload and isinstance(payload, dict):
        return payload.get("analysis") or payload.get("summary")
    return None


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

def _post_slack_message(channel: str, text: str) -> Optional[str]:
    adapter = get_slack_adapter()
    return adapter.send_message(channel, text)


def _format_alert_context(alert: dict[str, Any]) -> str:
    parts = []
    if alert.get("text"):
        parts.append(alert["text"])
    if alert.get("device"):
        parts.append(f"device={alert['device']}")
    if alert.get("interface"):
        parts.append(f"interface={alert['interface']}")
    if alert.get("severity"):
        parts.append(f"severity={alert['severity']}")
    return " | ".join(parts) if parts else json.dumps(alert)


def _ticket_from_alert(alert: dict[str, Any], analysis: Optional[str] = None) -> dict[str, str]:
    base_text = alert.get("text", "NetCopilot alert")
    device = alert.get("device")
    interface = alert.get("interface")
    summary_parts = [p for p in [base_text, f"on {device}" if device else None, interface] if p]
    summary = " ".join(summary_parts).strip()

    details = []
    if device:
        details.append(f"Device: {device}")
    if interface:
        details.append(f"Interface: {interface}")
    if alert.get("severity"):
        details.append(f"Severity: {alert['severity']}")

    description = summary
    if details:
        description += "\n\n" + "\n".join(details)
    if analysis:
        description += "\n\nInvestigation summary:\n" + analysis

    return {
        "ticket_type": JIRA_DEFAULT_ISSUE_TYPE,
        "summary": summary,
        "description": description,
        "team": JIRA_DEFAULT_TEAM,
        "priority": JIRA_DEFAULT_PRIORITY,
    }


def _get_redaction_fields() -> set[str]:
    base_fields = {"password", "pass", "secret", "token", "api_key", "authorization", "session_secret"}
    extra = get_setting("LOG_REDACTION_FIELDS", "")
    if extra:
        for item in extra.split(","):
            item = item.strip().lower()
            if item:
                base_fields.add(item)
    return base_fields


def _redact_payload(payload: Any) -> Any:
    if not get_setting_bool("LOG_REDACTION_ENABLED", True):
        return payload
    fields = _get_redaction_fields()
    if isinstance(payload, dict):
        return {
            k: "***" if str(k).lower() in fields else _redact_payload(v)
            for k, v in payload.items()
        }
    if isinstance(payload, list):
        return [_redact_payload(item) for item in payload]
    return payload


def _audit_log(payload: dict[str, Any]) -> None:
    audit_logger.info(json.dumps(_redact_payload(payload), ensure_ascii=True))


# Imperative change requests ("configure ...", "please shut down ge-0/0/1").
# Anchored at the start so questions about changes ("when was the last commit?",
# "did someone change the config?") and alert texts ("state change detected")
# are not refused. The hard guarantee is enforced in the tool layer
# (adapters/devices/safety.py): no device tool can send a non-read-only command.
_CHANGE_REQUEST = re.compile(
    r"^\s*(?:(?:please|pls|kindly|can you|could you|would you|go ahead and|now)\s+)*"
    r"(?:configure|conf\s+t|apply|commit|rollback|roll\s+back|shut\s*down|shut|no\s+shut(?:down)?|"
    r"enable|disable|set|delete|remove|add|change|modify|update|push|deploy|provision|"
    r"reboot|reload|restart|clear|bounce|reset)\b",
    re.IGNORECASE,
)

_READ_ONLY_REPLY = (
    "NetCopilot is in read-only mode and cannot provide configuration commands. "
    "If you want, I can open a change ticket instead."
)


# Ticket operations ("update NOC-123 priority", "add a comment to the ticket") are allowed actions.
_TICKET_WORD = re.compile(r"\b(?:ticket|jira|issue)\b", re.IGNORECASE)
_JIRA_KEY = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")  # case-sensitive: "ge-0/0/1" is not a key


def _is_config_request(message: str) -> bool:
    message = message or ""
    if not _CHANGE_REQUEST.match(message):
        return False
    return not (_TICKET_WORD.search(message) or _JIRA_KEY.search(message))


def _get_llm_settings() -> dict[str, str]:
    provider = get_setting("LLM_PROVIDER", "openai").lower()
    return {
        "provider": provider,
        "openai_api_key": get_setting("OPENAI_API_KEY", ""),
        "openai_model": get_setting("OPENAI_MODEL", "gpt-4.1-mini"),
        "anthropic_api_key": get_setting("ANTHROPIC_API_KEY", ""),
        "anthropic_model": get_setting("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        "local_base_url": get_setting("LOCAL_LLM_BASE_URL", ""),
        "local_api_key": get_setting("LOCAL_LLM_API_KEY", "local"),
        "local_model": get_setting("LOCAL_LLM_MODEL", "llama3.1"),
    }


# ---------------------------------------------------------------------------
# HTTP middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def audit_http_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    duration_ms = int((time.time() - start) * 1000)
    user = None
    if hasattr(request, "session") and request.session:
        user = request.session.get("user", {}).get("email")
    _audit_log(
        {
            "event": "http_request",
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": duration_ms,
            "user": user,
        }
    )
    return response


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    user: str = "unknown"


class TeamsWebhookRequest(BaseModel):
    text: str
    user: str = "teams"
    from_: Optional[Any] = Field(default=None, alias="from")
    token: Optional[str] = None


class AlertIngestRequest(BaseModel):
    text: str
    source: str = "demo"
    channel: Optional[str] = None
    device: Optional[str] = None
    interface: Optional[str] = None
    severity: Optional[str] = None


# ---------------------------------------------------------------------------
# Core chat loop (async — MCP tool calls are awaited)
# ---------------------------------------------------------------------------

_TOOL_RESULT_MAX_CHARS = 24_000


def _compact_tool_result(result: Any) -> str:
    """
    Serialize a tool result for the LLM, bounded in size.

    Adapters often return parsed fields plus the full CLI text (sometimes twice,
    as 'output' and 'raw_output'). Unbounded, one large output (e.g. a full
    routing table) overflows the model's context and fails the whole request.
    """
    limit = max(2_000, get_setting_int("TOOL_RESULT_MAX_CHARS", _TOOL_RESULT_MAX_CHARS))
    if isinstance(result, dict) and "raw_output" in result and result.get("output") == result["raw_output"]:
        result = {k: v for k, v in result.items() if k != "raw_output"}
    text = json.dumps(result, default=str)
    if len(text) <= limit:
        return text

    if isinstance(result, dict):
        trimmed = dict(result)
        overflow = len(text) - limit
        for key in ("raw_output", "output"):
            value = trimmed.get(key)
            if isinstance(value, str) and overflow > 0:
                keep = max(1_000, len(value) - overflow - 200)
                if keep < len(value):
                    overflow -= len(value) - keep
                    trimmed[key] = value[:keep] + "\n...[output truncated]"
        trimmed["truncated"] = True
        text = json.dumps(trimmed, default=str)
        if len(text) <= limit:
            return text
    return json.dumps({"truncated": True, "partial_result": text[:limit]})


async def _agentic_loop(
    request_id: str,
    messages: list,
    tools: list,
    model: str,
    user: str,
    role: str,
):
    """
    Async generator — the core agentic tool loop.

    Each iteration:
      1. Call the LLM (in a worker thread — the SDK clients are synchronous).
      2. If no tool calls → yield {"type": "reply", "content": ...} and return.
      3. If tool calls → run them ALL in parallel, append results, repeat.

    Yields event dicts consumed by both _run_chat (blocking) and /chat/stream (SSE).
      {"type": "tool_start", "name": str, "args": dict}
      {"type": "tool_end",   "name": str, "ms": int}
      {"type": "reply",      "content": str}
      {"type": "error",      "text": str}
    """
    MAX_TOOL_TURNS = 10

    llm_adapter = get_llm_adapter()
    if not llm_adapter:
        yield {"type": "error", "text": "LLM not configured."}
        return

    # The role-filtered list the LLM was offered is also what it may execute.
    offered_tools = {t["function"]["name"] for t in tools if isinstance(t, dict) and "function" in t}

    async def _exec_one(tc) -> tuple[dict, dict]:
        """Execute one tool call; returns (end_event, tool_result_message)."""
        name = tc.function.name
        try:
            args = json.loads(tc.function.arguments)
        except (json.JSONDecodeError, ValueError):
            args = {}
        if not isinstance(args, dict):
            args = {}

        tool_start = time.time()
        logger.info("[request %s] tool_start %s args=%s", request_id, name, args)
        with _metrics_lock:
            _metrics["tool_calls_total"] += 1
            _metrics["tool_counts"][name] = _metrics["tool_counts"].get(name, 0) + 1

        if name not in offered_tools:
            result: Any = {"error": f"Tool '{name}' is not available to role '{role}'."}
        elif not _is_tool_allowed(name):
            result = {"error": f"Tool '{name}' is blocked by server policy."}
        elif not _dispatcher:
            result = {"error": "Dispatcher not initialised."}
        else:
            try:
                result = await _dispatcher.call_tool(name, args)
            except Exception as exc:
                logger.exception("[request %s] tool %s raised", request_id, name)
                result = {"error": f"Tool execution failed: {exc}"}

        tool_elapsed_ms = int((time.time() - tool_start) * 1000)
        logger.info("[request %s] tool_end %s ms=%d", request_id, name, tool_elapsed_ms)
        is_error = isinstance(result, dict) and "error" in result
        _audit_log(
            {
                "event": "tool_call",
                "request_id": request_id,
                "name": name,
                "args": args,
                "user": user,
                "role": role,
                "ms": tool_elapsed_ms,
                "error": is_error,
            }
        )
        if is_error:
            with _metrics_lock:
                _metrics["tool_errors_total"] += 1

        end_event = {"type": "tool_end", "name": name, "ms": tool_elapsed_ms}
        tool_msg = {
            "role": "tool",
            "tool_call_id": tc.id,
            "name": name,
            "content": _compact_tool_result(result),
        }
        return end_event, tool_msg

    for _turn in range(MAX_TOOL_TURNS):
        try:
            response = await asyncio.to_thread(
                llm_adapter.chat,
                model=model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )
        except Exception as exc:
            logger.exception("[request %s] LLM request failed: %s", request_id, exc)
            yield {"type": "error", "text": "LLM request failed. Please retry later."}
            return

        msg = response.choices[0].message
        if not msg.tool_calls:
            yield {"type": "reply", "content": msg.content or ""}
            return

        # Announce all tool calls before executing
        messages.append(msg)
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments)
            except (json.JSONDecodeError, ValueError):
                args = {}
            yield {"type": "tool_start", "name": tc.function.name, "args": args}

        # Execute tool calls in parallel, capped at 5 concurrent device calls
        _sem = asyncio.Semaphore(5)

        async def _exec_bounded(tc):
            async with _sem:
                return await _exec_one(tc)

        pairs = await asyncio.gather(*[_exec_bounded(tc) for tc in msg.tool_calls])
        for end_event, tool_msg in pairs:
            yield end_event
            messages.append(tool_msg)

    yield {"type": "reply", "content": "Reached maximum reasoning depth. Please rephrase your question."}


def _llm_model(llm_settings: dict[str, str]) -> str:
    provider = llm_settings["provider"]
    if provider == "local":
        return llm_settings["local_model"]
    if provider == "anthropic":
        return llm_settings["anthropic_model"]
    return llm_settings["openai_model"]


def _chat_precheck(message: str, check_config: bool) -> Optional[dict]:
    """Return an immediate reply/error event if the request can't go to the LLM."""
    if check_config and _is_config_request(message):
        return {"type": "reply", "content": _READ_ONLY_REPLY}
    llm_settings = _get_llm_settings()
    provider = llm_settings["provider"]
    if provider == "openai" and not llm_settings["openai_api_key"]:
        return {"type": "error", "text": "OPENAI_API_KEY is missing."}
    if provider == "anthropic" and not llm_settings["anthropic_api_key"]:
        return {"type": "error", "text": "ANTHROPIC_API_KEY is missing."}
    if provider == "local" and not llm_settings["local_base_url"]:
        return {"type": "error", "text": "LOCAL_LLM_BASE_URL is missing."}
    return None


def _build_chat_messages(message: str, user: str, role: str) -> tuple[list, list]:
    """Return (messages, tools) for a chat turn: system prompt + memory + user message."""
    with _memory_lock:
        history = _load_memory(user)
    tools = _dispatcher.get_all_tools(role) if _dispatcher else []
    system_prompt = build_system_prompt(format_playbook_summary(), _build_device_inventory_summary())
    messages = [
        {"role": "system", "content": system_prompt},
        *history,
        {"role": "user", "content": message},
    ]
    return messages, tools


def _remember_turn(user: str, message: str, reply: str, messages: list) -> None:
    """
    Save the turn to memory. Tool outputs are kept (truncated to 800 chars
    each) so follow-ups such as ticket creation can cite the evidence.
    """
    _TOOL_RESULT_MAX = 800
    tool_findings: list[str] = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "tool":
            content = m.get("content", "")
            if len(content) > _TOOL_RESULT_MAX:
                content = content[:_TOOL_RESULT_MAX] + "…[truncated]"
            tool_findings.append(f"[{m.get('name', 'tool')}]: {content}")

    if tool_findings:
        enriched_reply = (
            "Investigation findings (tool outputs):\n"
            + "\n".join(tool_findings)
            + "\n\nAssistant summary: "
            + reply
        )
    else:
        enriched_reply = reply

    with _memory_lock:
        _save_memory(
            user,
            [
                {"role": "user", "content": message},
                {"role": "assistant", "content": enriched_reply},
            ],
        )


async def _run_chat(message: str, user: str, role: str, check_config: bool = True) -> dict:
    """
    Run one chat turn to completion.

    check_config=False is for NetCopilot's own prompts (alert investigations,
    ticket creation) whose text may quote alerts like "shutdown by admin".
    """
    request_id = str(uuid.uuid4())
    start_time = time.time()
    logger.info("[request %s] /chat start user=%s role=%s", request_id, user, role)
    _audit_log({"event": "chat_start", "request_id": request_id, "user": user, "role": role})

    early = _chat_precheck(message, check_config)
    if early:
        return {"request_id": request_id, "reply": early.get("content") or early.get("text", "")}

    with _metrics_lock:
        _metrics["requests_total"] += 1

    model = _llm_model(_get_llm_settings())
    messages, tools = _build_chat_messages(message, user, role)

    reply = ""
    async for event in _agentic_loop(request_id, messages, tools, model, user, role):
        if event["type"] == "reply":
            reply = event["content"]
        elif event["type"] == "error":
            return {"request_id": request_id, "reply": event["text"]}

    total_ms = int((time.time() - start_time) * 1000)
    logger.info("[request %s] /chat end ms=%d", request_id, total_ms)

    _remember_turn(user, message, reply, messages)
    return {"request_id": request_id, "reply": reply}


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------

def _parse_ticket_command(text: str) -> Optional[dict]:
    normalized = text.strip()
    lower = normalized.lower()
    if "ticket" not in lower:
        return None
    if lower.startswith("ticket "):
        remainder = normalized[len("ticket"):].strip()
    elif lower.startswith("create ticket "):
        remainder = normalized[len("create ticket"):].strip()
    else:
        remainder = re.sub(r"(?i)ticket", "", normalized).strip()

    if "|" in remainder:
        parts = [p.strip() for p in remainder.split("|")]
        summary = parts[0] or "NetCopilot ticket"
        description = parts[1] if len(parts) > 1 and parts[1] else summary
        team = parts[2] if len(parts) > 2 and parts[2] else JIRA_DEFAULT_TEAM
        priority = parts[3] if len(parts) > 3 and parts[3] else JIRA_DEFAULT_PRIORITY
        ticket_type = parts[4] if len(parts) > 4 and parts[4] else JIRA_DEFAULT_ISSUE_TYPE
    else:
        summary = remainder or "NetCopilot ticket"
        description = summary
        team = JIRA_DEFAULT_TEAM
        priority = JIRA_DEFAULT_PRIORITY
        ticket_type = JIRA_DEFAULT_ISSUE_TYPE

    return {
        "ticket_type": ticket_type,
        "summary": summary,
        "description": description,
        "team": team,
        "priority": priority,
    }


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------

@app.post("/chat")
async def chat(req: ChatRequest, request: Request):
    _enforce_rate_limit(request.client.host if request.client else "unknown")
    role = _get_role_from_request(request)
    return await _run_chat(req.message, _resolve_user(request, req.user), role)


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest, request: Request):
    """
    Streaming version of /chat.
    Returns text/event-stream with one JSON object per line:
      data: {"type": "tool_start", "name": "cisco_get_cpu_memory", "args": {...}}
      data: {"type": "tool_end",   "name": "cisco_get_cpu_memory", "ms": 412}
      data: {"type": "reply",      "content": "CPU is 23%..."}
      data: {"type": "done"}
    """
    _enforce_rate_limit(request.client.host if request.client else "unknown")
    role = _get_role_from_request(request)
    user = _resolve_user(request, req.user)
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

    request_id = str(uuid.uuid4())
    _audit_log({"event": "chat_start", "request_id": request_id, "user": user, "role": role})

    early = _chat_precheck(req.message, check_config=True)
    if early:
        async def _early():
            yield f"data: {json.dumps(early)}\n\n"
            yield f'data: {json.dumps({"type": "done"})}\n\n'
        return StreamingResponse(_early(), media_type="text/event-stream", headers=headers)

    with _metrics_lock:
        _metrics["requests_total"] += 1

    model = _llm_model(_get_llm_settings())
    messages, tools = _build_chat_messages(req.message, user, role)

    async def event_generator():
        reply = ""
        async for event in _agentic_loop(request_id, messages, tools, model, user, role):
            if event["type"] == "reply":
                reply = event["content"]
            yield f"data: {json.dumps(event)}\n\n"
        yield f'data: {json.dumps({"type": "done"})}\n\n'
        if reply:
            _remember_turn(user, req.message, reply, messages)

    return StreamingResponse(event_generator(), media_type="text/event-stream", headers=headers)


class ChatResetRequest(BaseModel):
    user: str = "unknown"


@app.post("/chat/reset")
async def chat_reset(req: ChatResetRequest, request: Request):
    """Forget this user's conversation history (the UI's "New chat" button)."""
    _enforce_rate_limit(request.client.host if request.client else "unknown")
    _get_role_from_request(request)
    user = _resolve_user(request, req.user)
    with _memory_lock:
        removed = _clear_memory(user)
    _audit_log({"event": "chat_reset", "user": user, "messages_removed": removed})
    return {"status": "ok", "messages_removed": removed}


@app.post("/alerts/ingest")
async def ingest_alert(req: AlertIngestRequest, request: Request):
    _enforce_rate_limit(request.client.host if request.client else "alert-ingest")
    token = request.headers.get("X-Alert-Token", "")
    if ALERT_INGEST_TOKEN and token != ALERT_INGEST_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid alert ingest token.")

    channel = req.channel or SLACK_ALERT_CHANNEL or "default"
    _save_alert(channel, req.source, req.model_dump())

    slack_error = None
    if channel != "default":
        adapter = get_slack_adapter()
        slack_error = await asyncio.to_thread(
            adapter.send_alert_with_button,
            channel=channel,
            alert_text=req.text,
            device=req.device or "",
            interface=req.interface or "",
            severity=req.severity or "",
        )
    return {"status": "ok", "channel": channel, "slack_error": slack_error}


@app.post("/teams/webhook")
async def teams_webhook(req: TeamsWebhookRequest):
    _enforce_rate_limit("teams")
    if TEAMS_VERIFICATION_TOKEN and req.token != TEAMS_VERIFICATION_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid Teams verification token.")
    user = req.user
    if isinstance(req.from_, dict):
        user = req.from_.get("name") or req.from_.get("id") or user
    elif isinstance(req.from_, str):
        user = req.from_
    result = await _run_chat(req.text, user, TEAMS_ROLE)
    return {"type": "message", "text": result["reply"]}


@app.post("/slack/command")
async def slack_command(request: Request, background_tasks: BackgroundTasks):
    _enforce_rate_limit("slack")
    raw_body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if SLACK_SIGNING_SECRET:
        if not timestamp.isdigit():
            raise HTTPException(status_code=403, detail="Invalid Slack signature.")
        if abs(int(time.time()) - int(timestamp)) > 300:
            raise HTTPException(status_code=403, detail="Slack request expired.")
        base = f"v0:{timestamp}:{raw_body.decode('utf-8')}"
        digest = hmac.new(
            SLACK_SIGNING_SECRET.encode("utf-8"),
            base.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(f"v0={digest}", signature):
            raise HTTPException(status_code=403, detail="Invalid Slack signature.")

    form = parse_qs(raw_body.decode("utf-8"))
    token = (form.get("token") or [""])[0]
    user_name = (form.get("user_name") or ["slack"])[0]
    text = (form.get("text") or [""])[0]
    response_url = (form.get("response_url") or [""])[0]
    channel_id = (form.get("channel_id") or [""])[0]
    channel_name = (form.get("channel_name") or [""])[0]

    if SLACK_VERIFICATION_TOKEN and token != SLACK_VERIFICATION_TOKEN and not SLACK_SIGNING_SECRET:
        raise HTTPException(status_code=403, detail="Invalid Slack verification token.")

    ticket_payload = _parse_ticket_command(text)
    if ticket_payload and ticket_payload.get("summary") == "NetCopilot ticket":
        channel_key = channel_id or "default"
        alert = _get_latest_alert(channel_key, exclude_source="analysis") or (
            _get_latest_alert(channel_name, exclude_source="analysis") if channel_name else None
        )
        if alert:
            analysis = _get_latest_analysis(channel_key or channel_name or "default")
            ticket_payload = _ticket_from_alert(alert, analysis=analysis)

    investigate_text = None
    if text.strip().lower().startswith("investigate"):
        remainder = text.strip()[len("investigate"):].strip()
        if remainder:
            investigate_text = remainder
        else:
            channel_key = channel_id or "default"
            alert = _get_latest_alert(channel_key, exclude_source="analysis")
            if not alert and channel_name:
                alert = _get_latest_alert(channel_name, exclude_source="analysis")
            investigate_text = _format_alert_context(alert) if alert else ""

    if response_url:
        # Slack needs an ack within 3s — do the work after responding, on this
        # event loop (device pools, locks and MCP sessions are bound to it).
        async def _respond() -> None:
            reply_text = "An error occurred."
            try:
                if investigate_text is not None:
                    if not investigate_text.strip():
                        reply_text = "No recent alert found for this channel."
                    else:
                        result = await _run_chat(
                            f"Investigate alert: {investigate_text}", user_name, SLACK_ROLE,
                            check_config=False,
                        )
                        reply_text = result["reply"]
                        channel_key = channel_id or channel_name or "default"
                        _save_alert(channel_key, "analysis", {"analysis": reply_text})
                elif ticket_payload and _dispatcher:
                    # Route through _run_chat so the LLM has access to the
                    # prior investigation in memory and fills the description
                    # correctly — direct dispatcher.call_tool bypasses the LLM
                    # and produces empty/incomplete ticket descriptions.
                    channel_key = channel_id or channel_name or "default"
                    analysis = _get_latest_analysis(channel_key)
                    if analysis:
                        ticket_prompt = (
                            f"Create a Jira ticket for the investigation you just completed. "
                            f"The ticket summary should be: {ticket_payload.get('summary', 'Network incident')}. "
                            f"Fill the description with all investigation findings, evidence, "
                            f"severity, and recommended next steps from the investigation."
                        )
                    else:
                        ticket_prompt = (
                            f"Create a Jira ticket: summary={ticket_payload.get('summary', 'Network incident')}, "
                            f"description={ticket_payload.get('description', '')}, "
                            f"priority={ticket_payload.get('priority', 'Medium')}."
                        )
                    ticket_result = await _run_chat(ticket_prompt, user_name, SLACK_ROLE, check_config=False)
                    reply_text = ticket_result.get("reply", "Ticket creation failed.")
                else:
                    result = await _run_chat(text, user_name, SLACK_ROLE)
                    reply_text = result["reply"]
            except Exception:
                logger.exception("Slack async response failed.")
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(
                        response_url,
                        json={"response_type": "in_channel", "text": reply_text},
                    )
            except Exception:
                logger.exception("Slack response_url post failed.")

        background_tasks.add_task(_respond)
        ack = (
            "Investigating alert..."
            if investigate_text is not None
            else ("Creating ticket..." if ticket_payload else "Working on it...")
        )
        return {"response_type": "ephemeral", "text": ack}

    # Synchronous path (no response_url)
    if investigate_text is not None:
        if not investigate_text.strip():
            return {"response_type": "ephemeral", "text": "No recent alert found for this channel."}
        result = await _run_chat(
            f"Investigate alert: {investigate_text}", user_name, SLACK_ROLE, check_config=False
        )
        return {"response_type": "in_channel", "text": result["reply"]}

    if ticket_payload and _dispatcher:
        ticket_result = await _dispatcher.call_tool("jira_create_ticket", ticket_payload)
        if "error" in ticket_result:
            return {
                "response_type": "ephemeral",
                "text": f"Ticket creation failed: {ticket_result['error']}",
            }
        return {
            "response_type": "in_channel",
            "text": f"Ticket created: {ticket_result.get('ticket_id')} {ticket_result.get('ticket_url', '')}".strip(),
        }

    result = await _run_chat(text, user_name, SLACK_ROLE)
    return {"response_type": "in_channel", "text": result["reply"]}


@app.post("/slack/actions")
async def slack_actions(request: Request, background_tasks: BackgroundTasks):
    """
    Handle Slack interactive component callbacks (button clicks).
    Slack requires a response within 3 seconds — we ack immediately and
    do all work in a BackgroundTask that posts back via response_url.

    Supported action_ids:
      - netcopilot_investigate   → runs investigation on the alert
      - netcopilot_create_ticket → creates a Jira ticket from the alert
    """
    # Read body FIRST — Slack disconnects after 3s and starlette raises
    # ClientDisconnect if we do anything blocking before reading.
    try:
        raw_body = await request.body()
    except Exception:
        return JSONResponse({"text": "ok"}, status_code=200)
    raw_str = raw_body.decode("utf-8")

    _enforce_rate_limit("slack-actions")

    # Verify Slack signature
    signature = request.headers.get("X-Slack-Signature", "")
    timestamp  = request.headers.get("X-Slack-Request-Timestamp", "")
    if SLACK_SIGNING_SECRET:
        if not timestamp.isdigit() or abs(int(time.time()) - int(timestamp)) > 300:
            raise HTTPException(status_code=403, detail="Slack request expired.")
        try:
            digest = hmac.new(
                SLACK_SIGNING_SECRET.encode("utf-8"),
                f"v0:{timestamp}:{raw_str}".encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(f"v0={digest}", signature):
                raise HTTPException(status_code=403, detail="Invalid Slack signature.")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=403, detail="Signature verification failed.")

    form = parse_qs(raw_str)
    payload_str = (form.get("payload") or ["{}"])[0]
    try:
        payload = json.loads(payload_str)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid payload JSON.")

    actions      = payload.get("actions", [])
    user_info    = payload.get("user", {})
    user_name    = user_info.get("username") or user_info.get("name") or "slack"
    response_url = payload.get("response_url", "")
    channel      = (payload.get("channel") or {}).get("id", "") or SLACK_ALERT_CHANNEL or "default"

    if not actions:
        return JSONResponse({"ok": True})

    action    = actions[0]
    action_id = action.get("action_id", "")
    value     = action.get("value", "")

    # value format: "device|interface|severity|alert_text"
    parts      = value.split("|", 3)
    device     = parts[0] if len(parts) > 0 else ""
    interface  = parts[1] if len(parts) > 1 else ""
    severity   = parts[2] if len(parts) > 2 else ""
    alert_text = parts[3] if len(parts) > 3 else value

    async def _do_action() -> None:
        reply_text = "An error occurred."
        try:
            if action_id == "netcopilot_investigate":
                context = alert_text
                if device:    context += f" device={device}"
                if interface: context += f" interface={interface}"
                if severity:  context += f" severity={severity}"
                # Reset circuit breaker so stale OPEN state doesn't block the investigation
                if device:
                    from adapters.devices.pool import get_pool
                    get_pool().force_probe(device)
                # Explicitly bypass run_playbook so the LLM calls device tools directly
                # and returns precise data (or a specific error if SSH fails)
                inv_prompt = (
                    f"Investigate this alert. Call vendor device tools DIRECTLY — "
                    f"do NOT use run_playbook. Use tools such as cisco_get_interface_health, "
                    f"cisco_get_interface_status, cisco_get_interface_counters for Cisco devices. "
                    f"Alert details: {context}"
                )
                result = await _run_chat(inv_prompt, user_name, SLACK_ROLE, check_config=False)
                reply_text = result["reply"]
                _save_alert(channel, "analysis", {"analysis": reply_text})

                # Post findings with a Create Ticket button and return early
                if response_url:
                    try:
                        adapter = get_slack_adapter()
                        await asyncio.to_thread(
                            adapter.post_investigation_result,
                            response_url=response_url,
                            findings=reply_text,
                            device=device,
                            interface=interface,
                            severity=severity,
                            alert_text=alert_text,
                        )
                    except Exception:
                        logger.exception("Failed to post investigation result with button.")
                return

            elif action_id == "netcopilot_create_ticket":
                analysis = _get_latest_analysis(channel)

                # If no analysis or analysis is incomplete (SSH failed etc),
                # run a fresh investigation first so the ticket has real data
                _incomplete_markers = (
                    "unable to retrieve",
                    "no health data",
                    "connection error",
                    "ssh",
                    "not enough data",
                    "no detailed health",
                    "could not",
                    "failed to",
                    "no specific evidence",
                    "automated playbook ran",
                    "no supporting device data",
                    "circuit is open",
                )
                analysis_incomplete = (
                    not analysis
                    or any(m in analysis.lower() for m in _incomplete_markers)
                )

                if analysis_incomplete:
                    # Re-investigate then ticket in one LLM call
                    context = alert_text
                    if device:    context += f" device={device}"
                    if interface: context += f" interface={interface}"
                    if severity:  context += f" severity={severity}"
                    if device:
                        from adapters.devices.pool import get_pool
                        get_pool().force_probe(device)
                    inv_result = await _run_chat(
                        f"Investigate this alert. Call vendor device tools DIRECTLY — "
                        f"do NOT use run_playbook. Alert details: {context}",
                        user_name, SLACK_ROLE, check_config=False,
                    )
                    _save_alert(channel, "analysis", {"analysis": inv_result["reply"]})
                    ticket_prompt = (
                        f"Based on the investigation you just completed, create a Jira ticket. "
                        f"Alert: {alert_text} (device={device}, interface={interface}, severity={severity}). "
                        f"Use the investigation findings above for the description."
                    )
                else:
                    ticket_prompt = (
                        f"Create a Jira ticket for the investigation you just completed. "
                        f"Alert: {alert_text} (device={device}, interface={interface}, severity={severity}). "
                        f"Fill the description with all investigation findings, evidence, severity, and next steps."
                    )

                result = await _run_chat(ticket_prompt, user_name, SLACK_ROLE, check_config=False)
                reply_text = result.get("reply", "Ticket creation failed.")
        except Exception:
            logger.exception("Slack action handler failed.")

        if response_url:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(
                        response_url,
                        json={"response_type": "in_channel", "text": reply_text},
                    )
            except Exception:
                logger.exception("Slack action response_url post failed.")

    background_tasks.add_task(_do_action)

    # Immediate ack — must be within 3 seconds
    if action_id == "netcopilot_investigate":
        ack = "🔍 Investigating alert..."
    elif action_id == "netcopilot_create_ticket":
        ack = "🎫 Creating ticket..."
    else:
        ack = "Working on it..."

    return JSONResponse({"response_type": "ephemeral", "text": ack})


# ---------------------------------------------------------------------------
# Health / metrics / version
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    from credentials import is_vault_degraded
    llm_ok = get_llm_adapter() is not None
    vault_degraded = is_vault_degraded()
    backends = _dispatcher.server_status() if _dispatcher else []
    integrations_connected = sum(
        1 for s in backends
        if s.get("type") in ("integration_adapter", "vendor_mcp") and s.get("connected")
    )
    status = "ok" if llm_ok and not vault_degraded else "degraded"
    return {
        "status": status,
        "version": APP_VERSION,
        "llm_configured": llm_ok,
        "vault_degraded": vault_degraded,
        "backends": backends,
        "integrations_connected": integrations_connected,
    }


@app.get("/health/llm")
async def health_llm():
    llm_settings = _get_llm_settings()
    llm_adapter = get_llm_adapter()
    if not llm_adapter:
        return {"status": "degraded", "error": "LLM not configured"}
    provider = llm_settings["provider"]
    model = _llm_model(llm_settings)
    result = await asyncio.to_thread(llm_adapter.health, model)
    result["provider"] = provider
    return result


@app.get("/metrics")
async def metrics():
    with _metrics_lock:
        return dict(_metrics)


@app.get("/version")
async def version():
    return {"version": APP_VERSION}


# ---------------------------------------------------------------------------
# Static / auth
# ---------------------------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/auth/me")
async def auth_me(request: Request):
    user = request.session.get("user") if hasattr(request, "session") else None
    return {"user": user}


@app.get("/auth/login")
async def auth_login(request: Request):
    if not oauth:
        raise HTTPException(status_code=400, detail="Auth0 not configured.")
    return await oauth.auth0.authorize_redirect(
        request, AUTH0_CALLBACK_URL, audience=AUTH0_AUDIENCE or None
    )


@app.get("/auth/callback")
async def auth_callback(request: Request):
    if not oauth:
        raise HTTPException(status_code=400, detail="Auth0 not configured.")
    token = await oauth.auth0.authorize_access_token(request)
    userinfo = token.get("userinfo") or {}
    role = AUTH0_DEFAULT_ROLE
    group_role = None

    if AUTH0_GROUP_CLAIM and AUTH0_GROUP_CLAIM in userinfo:
        claim_value = userinfo.get(AUTH0_GROUP_CLAIM)
        if isinstance(claim_value, list):
            group_role = _role_from_groups([str(v) for v in claim_value])

    if not group_role and AUTH0_ROLE_CLAIM and AUTH0_ROLE_CLAIM in userinfo:
        claim_value = userinfo.get(AUTH0_ROLE_CLAIM)
        if isinstance(claim_value, list) and claim_value:
            role = str(claim_value[0])
        elif isinstance(claim_value, str):
            role = claim_value or role
    elif "role" in userinfo:
        role = userinfo.get("role") or role
    elif AUTH0_ROLE_CLAIM and "id_token" in token:
        try:
            payload_b64 = token["id_token"].split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            decoded = json.loads(base64.urlsafe_b64decode(payload_b64.encode()).decode())
            if AUTH0_GROUP_CLAIM and AUTH0_GROUP_CLAIM in decoded:
                groups = decoded.get(AUTH0_GROUP_CLAIM)
                if isinstance(groups, list):
                    group_role = _role_from_groups([str(v) for v in groups])
            claim_value = decoded.get(AUTH0_ROLE_CLAIM)
            if isinstance(claim_value, list) and claim_value:
                role = str(claim_value[0])
            elif isinstance(claim_value, str):
                role = claim_value or role
        except Exception:
            pass

    if group_role:
        role = group_role
    request.session["user"] = {
        "email": userinfo.get("email"),
        "name": userinfo.get("name"),
        "role": role,
    }
    return RedirectResponse(url="/")


@app.get("/auth/logout")
async def auth_logout(request: Request):
    request.session.clear()
    query = urlencode({"returnTo": AUTH0_LOGOUT_URL, "client_id": AUTH0_CLIENT_ID})
    return RedirectResponse(url=f"https://{AUTH0_DOMAIN}/v2/logout?{query}")


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

@app.get("/admin/settings")
async def admin_settings(request: Request):
    role = _get_role_from_request(request)
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    schema = []
    for entry in _SETTINGS_SCHEMA:
        key = entry["key"]
        raw_value = get_setting(key, "")
        is_secret = bool(entry.get("secret"))
        schema.append(
            {
                "key": key,
                "label": entry.get("label", key),
                "category": entry.get("category", "General"),
                "secret": is_secret,
                "has_value": bool(raw_value),
                "value": "" if is_secret else raw_value,
                "restart_required": True,
            }
        )
    return {
        "settings": schema,
        "custom_settings": {
            k: "********" if re.search(r"(secret|token|pass|key)", k, re.IGNORECASE) else v
            for k, v in list_settings().items()
        },
        "note": "Settings apply on next restart. Use Reload Inventory to pick up devices.yaml changes.",
    }


@app.post("/admin/settings")
async def update_admin_settings(request: Request):
    role = _get_role_from_request(request)
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    payload = await request.json()
    values = payload.get("settings") if isinstance(payload, dict) else None
    if not isinstance(values, dict):
        raise HTTPException(status_code=400, detail="Expected JSON body with 'settings' object.")

    secret_keys = {e["key"] for e in _SETTINGS_SCHEMA if e.get("secret")}
    filtered: dict[str, Any] = {}
    for k, v in values.items():
        key = str(k)
        value = "" if v is None else str(v)
        if key in secret_keys and not value.strip():
            continue
        if value == "********" and re.search(r"(secret|token|pass|key)", key, re.IGNORECASE):
            continue
        filtered[key] = value

    if not filtered:
        return {"status": "ok", "updated": [], "restart_required": False}

    set_settings(filtered)
    _audit_log({"event": "admin_settings_update", "keys": sorted(filtered.keys())})
    return {"status": "ok", "updated": list(filtered.keys()), "restart_required": True}


@app.post("/admin/reload-inventory")
async def reload_inventory(request: Request):
    role = _get_role_from_request(request)
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    counts = await asyncio.to_thread(reload_inventory_devices)
    _audit_log({"event": "admin_inventory_reload", "counts": counts})
    return {"status": "ok", "counts": counts}


@app.get("/admin/inventory")
async def admin_inventory(request: Request):
    role = _get_role_from_request(request)
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    devices = list_devices_detailed()
    counts: dict[str, int] = {}
    for device in devices:
        counts[device["vendor"]] = counts.get(device["vendor"], 0) + 1
    return {"counts": counts, "devices": devices, "registry_path": registry_path()}


@app.post("/webhooks/nautobot/inventory")
async def nautobot_inventory_webhook(request: Request):
    expected = get_setting("INVENTORY_WEBHOOK_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="INVENTORY_WEBHOOK_TOKEN not configured.")
    provided = (request.headers.get("X-Webhook-Token", "") or "").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=403, detail="Invalid webhook token.")

    debounce = get_setting_int("INVENTORY_WEBHOOK_DEBOUNCE_SECONDS", 5)
    now = time.time()
    with _inventory_webhook_lock:
        global _inventory_webhook_last_reload
        if debounce > 0 and now - _inventory_webhook_last_reload < debounce:
            return {"status": "ok", "skipped": True, "reason": "debounced"}
        _inventory_webhook_last_reload = now

    webhook_payload: dict = {}
    try:
        webhook_payload = await request.json()
    except Exception:
        pass

    counts = await asyncio.to_thread(reload_inventory_devices)
    _audit_log(
        {
            "event": "inventory_webhook_reload",
            "source": "nautobot",
            "counts": counts,
            "webhook_event": webhook_payload.get("event") if isinstance(webhook_payload, dict) else None,
        }
    )
    return {"status": "ok", "counts": counts}


@app.post("/admin/inventory/import-csv")
async def import_inventory_csv(request: Request, file: UploadFile):
    """
    Merge devices from a CSV into devices.yaml (the `devices:` section).

    Columns: device|name|hostname, vendor|platform, ip|host|address, and
    optionally port, platform, site. Existing devices not in the CSV are kept.
    """
    role = _get_role_from_request(request)
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Please upload a CSV file.")

    content = await file.read()
    try:
        rows = list(csv.DictReader(content.decode("utf-8-sig").splitlines()))
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="CSV must be UTF-8 encoded.")
    if not rows:
        raise HTTPException(status_code=400, detail="CSV file is empty.")

    path = registry_path()
    raw: dict = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise HTTPException(status_code=500, detail=f"{path} is not a YAML mapping; refusing to overwrite it.")
    devices_section = raw.get("devices") or {}
    if not isinstance(devices_section, dict):
        raise HTTPException(status_code=500, detail=f"'devices' in {path} is not a mapping.")

    legacy_names = {
        str(name)
        for key in ("cisco_devices", "juniper_devices", "nokia_devices", "aruba_devices")
        for name in (raw.get(key) or {})
    }

    added = updated = skipped = 0
    errors: list[str] = []

    _SAFE_DEVICE_NAME = re.compile(r"^[a-zA-Z0-9._-]{1,64}$")
    _SAFE_HOST = re.compile(r"^[a-zA-Z0-9._:-]{1,255}$")

    for line_no, row in enumerate(rows, start=2):
        row = {str(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        name = row.get("device") or row.get("name") or row.get("hostname") or ""
        vendor_raw = row.get("vendor") or row.get("platform") or ""
        host = row.get("ip") or row.get("host") or row.get("address") or ""
        port = row.get("port") or ""
        if not name or not vendor_raw or not host:
            skipped += 1
            errors.append(f"Line {line_no}: missing device, vendor or ip/host.")
            continue
        if not _SAFE_DEVICE_NAME.match(name):
            skipped += 1
            errors.append(f"Line {line_no}: invalid device name '{name}' — only alphanumeric, dots, hyphens allowed.")
            continue
        if "/" in host:
            host = host.split("/", 1)[0]  # NetBox-style 10.0.0.1/32
        if not _SAFE_HOST.match(host):
            skipped += 1
            errors.append(f"Line {line_no}: invalid host value '{host}' for '{name}'.")
            continue
        if port and not (port.isdigit() and 0 < int(port) < 65536):
            skipped += 1
            errors.append(f"Line {line_no}: invalid port '{port}' for '{name}'.")
            continue
        vendor = normalize_vendor(vendor_raw)
        if not vendor:
            skipped += 1
            errors.append(f"Line {line_no}: unsupported vendor '{vendor_raw}' for '{name}'.")
            continue

        entry: dict[str, Any] = {"host": host, "vendor": vendor}
        if port:
            entry["port"] = int(port)
        platform = row.get("platform") if row.get("vendor") else ""
        if platform:
            entry["platform"] = platform
        if row.get("site"):
            entry["site"] = row["site"]

        if name in devices_section or name in legacy_names:
            updated += 1
        else:
            added += 1
        devices_section[name] = entry

    raw["devices"] = devices_section
    try:
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f, sort_keys=False)
    except OSError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot write {path} ({exc.strerror}). In Docker/Kubernetes devices.yaml is "
                   "usually mounted read-only — update the mounted file or ConfigMap instead.",
        )
    counts = await asyncio.to_thread(reload_inventory_devices)

    _audit_log(
        {
            "event": "inventory_csv_import",
            "added": added,
            "updated": updated,
            "skipped": skipped,
            "file": file.filename,
            "counts": counts,
        }
    )
    return {
        "status": "ok",
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "errors": errors[:10],
        "counts": counts,
    }


@app.get("/admin/playbooks")
async def admin_playbooks(request: Request):
    role = _get_role_from_request(request)
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    return {"status": "ok", "playbooks": list_playbooks()}


@app.post("/admin/playbooks")
async def update_playbooks(request: Request):
    role = _get_role_from_request(request)
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    payload = await request.json()
    items = payload.get("playbooks") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="Expected JSON body with 'playbooks' list.")
    upsert_playbooks(items)
    _audit_log({"event": "playbooks_update", "count": len(items)})
    return {"status": "ok", "count": len(items)}


@app.delete("/admin/playbooks/{playbook_id}")
async def delete_playbook_endpoint(playbook_id: str, request: Request):
    role = _get_role_from_request(request)
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    removed = delete_playbook(playbook_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Playbook not found.")
    _audit_log({"event": "playbook_delete", "id": playbook_id})
    return {"status": "ok", "id": playbook_id}


@app.post("/admin/playbooks/test-step")
async def test_playbook_step(request: Request):
    """Run a single playbook step with the given inputs (Admin UI "Test step")."""
    role = _get_role_from_request(request)
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    payload = await request.json()
    step = payload.get("step") if isinstance(payload, dict) else None
    inputs = payload.get("inputs") if isinstance(payload, dict) else None
    if not isinstance(step, dict) or not isinstance(inputs or {}, dict):
        raise HTTPException(status_code=400, detail="Expected JSON body with 'step' and 'inputs' objects.")
    if not _dispatcher:
        raise HTTPException(status_code=503, detail="Dispatcher not initialised.")

    from adapters.devices.registry import get_vendor
    from playbooks.runner import plan_step

    params = {k: v for k, v in (inputs or {}).items() if v not in (None, "")}
    device_vendor = get_vendor(str(params.get("device", ""))) or ""
    tool_name, args, skip_reason = plan_step(step, params, device_vendor)
    if skip_reason:
        raise HTTPException(status_code=400, detail=f"Step would be skipped: {skip_reason}")

    result = await _dispatcher.call_tool_unprefixed(tool_name, args)
    _audit_log({"event": "playbook_test_step", "tool": tool_name, "args": args})
    return {"status": "ok", "tool": tool_name, "args": args, "result": result}
