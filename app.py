import base64
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from logging.handlers import RotatingFileHandler

from typing import Any, Optional

from urllib.parse import parse_qs

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel, Field
from openai import OpenAI
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
    CISCO_DEVICES,
    JIRA_BASE_URL,
    JIRA_DEFAULT_ISSUE_TYPE,
    JIRA_DEFAULT_PRIORITY,
    JIRA_DEFAULT_TEAM,
    LOG_LEVEL,
    LLM_PROVIDER,
    LOCAL_LLM_API_KEY,
    LOCAL_LLM_BASE_URL,
    LOCAL_LLM_MODEL,
    NETCOPILOT_API_KEYS,
    OPENAI_MODEL,
    OPENAI_API_KEY,
    RATE_LIMIT_PER_MIN,
    SESSION_SECRET,
    SLACK_ALERT_CHANNEL,
    SLACK_BOT_TOKEN,
    SLACK_SIGNING_SECRET,
    SLACK_VERIFICATION_TOKEN,
    SLACK_ROLE,
    TEAMS_VERIFICATION_TOKEN,
    TEAMS_ROLE,
    ALERT_INGEST_TOKEN,
)
from tools.device_access import get_interface_health, get_interface_status
from llm.prompts import SYSTEM_PROMPT
from llm.tools import TOOLS
from tools.counters import get_interface_counters
from tools.flapping import get_top_flapping_links
from tools.jira import add_ticket_comment, close_ticket, create_ticket
from tools.operations import (
    get_acl_hits,
    get_bgp_neighbor_status,
    get_bgp_route,
    get_chassis_health,
    get_cpu_memory,
    get_link_health,
    get_isis_health,
    get_isis_neighbors,
    get_mtu_info,
    get_ospf_neighbors,
    get_ping,
    get_route_lookup,
    get_traceroute,
)
from settings import get_setting, list_settings, set_settings

# ---------- CONFIG ----------

if LLM_PROVIDER == "openai" and not OPENAI_API_KEY:
    print("WARNING: OPENAI_API_KEY is not set. The /chat endpoint will not work.")

if LLM_PROVIDER == "local":
    if not LOCAL_LLM_BASE_URL:
        print("WARNING: LOCAL_LLM_BASE_URL is not set. The /chat endpoint will not work.")
    client = OpenAI(api_key=LOCAL_LLM_API_KEY, base_url=LOCAL_LLM_BASE_URL)
else:
    client = OpenAI(api_key=OPENAI_API_KEY)

app = FastAPI(title="NetCopilot - Cisco IOS XR Edition")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

APP_VERSION = "0.1.0"

logger = logging.getLogger("netcopilot")
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
if not logger.handlers:
    os.makedirs("logs", exist_ok=True)
    handler = RotatingFileHandler("logs/netcopilot.log", maxBytes=1_000_000, backupCount=3)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)

audit_logger = logging.getLogger("netcopilot.audit")
audit_logger.setLevel(logging.INFO)
if not audit_logger.handlers:
    audit_handler = RotatingFileHandler("logs/audit.log", maxBytes=1_000_000, backupCount=5)
    audit_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    audit_logger.addHandler(audit_handler)

_metrics_lock = threading.Lock()
_metrics = {
    "requests_total": 0,
    "tool_calls_total": 0,
    "tool_errors_total": 0,
    "tool_counts": {},
}

_rate_limit_lock = threading.Lock()
_rate_limit_window = 60
_rate_limit_max = RATE_LIMIT_PER_MIN
_rate_limit: dict[str, list[float]] = {}

_memory_lock = threading.Lock()
_memory_limit = 12
_memory_db_path = "memory.db"
_alert_db_path = "alerts.db"

_SETTINGS_SCHEMA = [
    {"key": "LLM_PROVIDER", "label": "LLM provider", "category": "LLM"},
    {"key": "OPENAI_API_KEY", "label": "OpenAI API key", "category": "LLM", "secret": True},
    {"key": "OPENAI_MODEL", "label": "OpenAI model", "category": "LLM"},
    {"key": "LOCAL_LLM_BASE_URL", "label": "Local LLM base URL", "category": "LLM"},
    {"key": "LOCAL_LLM_API_KEY", "label": "Local LLM API key", "category": "LLM", "secret": True},
    {"key": "LOCAL_LLM_MODEL", "label": "Local LLM model", "category": "LLM"},
    {"key": "CISCO_USER", "label": "Cisco username", "category": "Devices"},
    {"key": "CISCO_PASS", "label": "Cisco password", "category": "Devices", "secret": True},
    {"key": "JUNIPER_USER", "label": "Juniper username", "category": "Devices"},
    {"key": "JUNIPER_PASS", "label": "Juniper password", "category": "Devices", "secret": True},
    {"key": "NOKIA_USER", "label": "Nokia username", "category": "Devices"},
    {"key": "NOKIA_PASS", "label": "Nokia password", "category": "Devices", "secret": True},
    {"key": "NETCONF_PORT", "label": "NETCONF port", "category": "Devices"},
    {"key": "NETCONF_TIMEOUT", "label": "NETCONF timeout (seconds)", "category": "Devices"},
    {"key": "NETBOX_URL", "label": "NetBox URL", "category": "Inventory"},
    {"key": "NETBOX_TOKEN", "label": "NetBox token", "category": "Inventory", "secret": True},
    {"key": "NETBOX_VERIFY_SSL", "label": "NetBox verify SSL", "category": "Inventory"},
    {"key": "NETBOX_FILTER", "label": "NetBox filter query", "category": "Inventory"},
    {"key": "NETBOX_MANUFACTURER_CISCO", "label": "NetBox Cisco manufacturer", "category": "Inventory"},
    {"key": "NETBOX_MANUFACTURER_JUNIPER", "label": "NetBox Juniper manufacturer", "category": "Inventory"},
    {"key": "NETBOX_MANUFACTURER_NOKIA", "label": "NetBox Nokia manufacturer", "category": "Inventory"},
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
    {"key": "SESSION_SECRET", "label": "Session secret", "category": "Security", "secret": True},
]


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
        return "noc"
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
        if not item:
            continue
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


def _enforce_rate_limit(client_id: str) -> None:
    now = time.time()
    with _rate_limit_lock:
        timestamps = _rate_limit.get(client_id, [])
        timestamps = [ts for ts in timestamps if now - ts < _rate_limit_window]
        if len(timestamps) >= _rate_limit_max:
            raise HTTPException(status_code=429, detail="Rate limit exceeded.")
        timestamps.append(now)
        _rate_limit[client_id] = timestamps


def _is_tool_allowed(role: str, tool_name: str) -> bool:
    role = role.lower()
    if role == "admin":
        return True
    diagnostic_tools = {
        "get_top_flapping_links",
        "get_interface_health",
        "get_interface_status",
        "get_interface_counters",
        "get_bgp_neighbor_status",
        "get_ospf_neighbors",
        "get_isis_neighbors",
        "get_cpu_memory",
        "get_chassis_health",
        "get_link_health",
        "get_isis_health",
        "get_route_lookup",
        "get_bgp_route",
        "get_acl_hits",
        "get_mtu_info",
        "get_ping",
        "get_traceroute",
    }
    ticket_tools = {"create_ticket", "close_ticket", "add_ticket_comment"}
    if role == "noc":
        return tool_name in diagnostic_tools
    if role == "noc-ticket":
        return tool_name in diagnostic_tools.union(ticket_tools)
    return False


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
    with sqlite3.connect(_memory_db_path) as conn:
        conn.executemany(
            "INSERT INTO memory (user, role, content, created_at) VALUES (?, ?, ?, ?)",
            [(user, t["role"], t["content"], now) for t in turns],
        )
        conn.execute(
            """
            DELETE FROM memory
            WHERE user = ? AND id NOT IN (
                SELECT id FROM memory WHERE user = ? ORDER BY id DESC LIMIT ?
            )
            """,
            (user, user, _memory_limit * 2),
        )


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
    with sqlite3.connect(_alert_db_path) as conn:
        conn.execute(
            "INSERT INTO alerts (channel, source, payload, created_at) VALUES (?, ?, ?, ?)",
            (channel, source, json.dumps(payload), now),
        )


def _get_latest_alert(channel: str) -> Optional[dict[str, Any]]:
    with sqlite3.connect(_alert_db_path) as conn:
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


def _post_slack_message(channel: str, text: str) -> Optional[str]:
    if not SLACK_BOT_TOKEN:
        return "SLACK_BOT_TOKEN not configured."
    try:
        import httpx

        resp = httpx.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            json={"channel": channel, "text": text},
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            return data.get("error", "Slack API error.")
    except Exception as exc:
        return str(exc)
    return None


def _format_alert_context(alert: dict[str, Any]) -> str:
    parts = []
    text = alert.get("text")
    if text:
        parts.append(text)
    if alert.get("device"):
        parts.append(f"device={alert['device']}")
    if alert.get("interface"):
        parts.append(f"interface={alert['interface']}")
    if alert.get("severity"):
        parts.append(f"severity={alert['severity']}")
    return " | ".join(parts) if parts else json.dumps(alert)


def _ticket_from_alert(alert: dict[str, Any]) -> dict[str, str]:
    summary = f"Alert: {alert.get('text', 'NetCopilot alert')}"
    details = []
    if alert.get("device"):
        details.append(f"Device: {alert['device']}")
    if alert.get("interface"):
        details.append(f"Interface: {alert['interface']}")
    if alert.get("severity"):
        details.append(f"Severity: {alert['severity']}")
    description = summary
    if details:
        description = summary + "\n\n" + "\n".join(details)
    return {
        "ticket_type": JIRA_DEFAULT_ISSUE_TYPE,
        "summary": summary,
        "description": description,
        "team": JIRA_DEFAULT_TEAM,
        "priority": JIRA_DEFAULT_PRIORITY,
    }

# ---------- REQUEST MODEL ----------
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


# ---------- MAIN /chat ENDPOINT ----------
def _run_chat(message: str, user: str, role: str) -> dict:
    request_id = str(uuid.uuid4())
    start_time = time.time()
    logger.info("[request %s] /chat start user=%s role=%s", request_id, user, role)
    audit_logger.info("request_id=%s action=chat user=%s role=%s", request_id, user, role)

    if LLM_PROVIDER == "openai" and not OPENAI_API_KEY:
        logger.warning("[request %s] missing OPENAI_API_KEY", request_id)
        return {"reply": "OPENAI_API_KEY is missing."}
    if LLM_PROVIDER == "local" and not LOCAL_LLM_BASE_URL:
        logger.warning("[request %s] missing LOCAL_LLM_BASE_URL", request_id)
        return {"reply": "LOCAL_LLM_BASE_URL is missing."}

    with _metrics_lock:
        _metrics["requests_total"] += 1

    with _memory_lock:
        history = _load_memory(user)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history, {"role": "user", "content": message}]

    try:
        model = LOCAL_LLM_MODEL if LLM_PROVIDER == "local" else OPENAI_MODEL
        first = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )
    except Exception as exc:
        logger.exception("[request %s] OpenAI request failed: %s", request_id, exc)
        return {"request_id": request_id, "reply": "LLM request failed. Please retry later."}

    msg = first.choices[0].message

    # If LLM wants to call a tool
    if msg.tool_calls:
        tool_results: dict[str, Any] = {}
        messages.append(msg)
        for tc in msg.tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments)
            tool_start = time.time()
            logger.info("[request %s] tool_start %s args=%s", request_id, name, args)
            with _metrics_lock:
                _metrics["tool_calls_total"] += 1
                _metrics["tool_counts"][name] = _metrics["tool_counts"].get(name, 0) + 1

            if not _is_tool_allowed(role, name):
                result = {"error": f"Role '{role}' not permitted to run '{name}'."}
            elif name == "get_top_flapping_links":
                result = get_top_flapping_links(
                    args.get("limit", 10),
                    args.get("since_hours", 24),
                )

            elif name == "get_interface_health":
                result = get_interface_health(
                    args["device"],
                    args["interface"],
                )
            elif name == "get_interface_status":
                result = get_interface_status(args["device"])
            elif name == "get_interface_counters":
                result = get_interface_counters(
                    args["device"],
                    args["interface"],
                    args.get("window_minutes", 60),
                )
            elif name == "create_ticket":
                result = create_ticket(
                    args["ticket_type"],
                    args["summary"],
                    args["description"],
                    args["team"],
                    args["priority"],
                )
            elif name == "close_ticket":
                result = close_ticket(
                    args["issue_key"],
                    args.get("transition_name", "Done"),
                )
            elif name == "add_ticket_comment":
                result = add_ticket_comment(
                    args["issue_key"],
                    args["comment"],
                )
            elif name == "get_bgp_neighbor_status":
                result = get_bgp_neighbor_status(args["device"], args["neighbor"])
            elif name == "get_ospf_neighbors":
                result = get_ospf_neighbors(args["device"])
            elif name == "get_isis_neighbors":
                result = get_isis_neighbors(args["device"])
            elif name == "get_cpu_memory":
                result = get_cpu_memory(args["device"])
            elif name == "get_chassis_health":
                result = get_chassis_health(args["device"])
            elif name == "get_link_health":
                result = get_link_health(args["device"], args["interface"])
            elif name == "get_isis_health":
                result = get_isis_health(args["device"], args["interface"])
            elif name == "get_route_lookup":
                result = get_route_lookup(args["device"], args["prefix"])
            elif name == "get_bgp_route":
                result = get_bgp_route(args["device"], args["prefix"])
            elif name == "get_acl_hits":
                result = get_acl_hits(args["device"], args["acl_name"])
            elif name == "get_mtu_info":
                result = get_mtu_info(args["device"], args["interface"])
            elif name == "get_ping":
                result = get_ping(args["device"], args["destination"], args.get("count", 5))
            elif name == "get_traceroute":
                result = get_traceroute(args["device"], args["destination"])
            else:
                result = {"error": f"Unknown tool '{name}'"}

            tool_results[name] = result
            tool_elapsed_ms = int((time.time() - tool_start) * 1000)
            logger.info("[request %s] tool_end %s ms=%s", request_id, name, tool_elapsed_ms)
            audit_logger.info(
                "request_id=%s action=tool name=%s user=%s role=%s ms=%s error=%s",
                request_id,
                name,
                user,
                role,
                tool_elapsed_ms,
                "error" in result,
            )
            if isinstance(result, dict) and "error" in result:
                with _metrics_lock:
                    _metrics["tool_errors_total"] += 1

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": name,
                    "content": json.dumps(result),
                }
            )

        if len(msg.tool_calls) == 1 and msg.tool_calls[0].function.name == "get_link_health":
            reply = _format_link_health(tool_results.get("get_link_health", {}))
            with _memory_lock:
                _save_memory(
                    user,
                    [
                        {"role": "user", "content": message},
                        {"role": "assistant", "content": reply},
                    ],
                )
            return {"request_id": request_id, "reply": reply}

        try:
            final = client.chat.completions.create(
                model=model,
                messages=messages,
            )
        except Exception as exc:
            logger.exception("[request %s] OpenAI request failed: %s", request_id, exc)
            return {"request_id": request_id, "reply": "LLM request failed. Please retry later."}
        total_ms = int((time.time() - start_time) * 1000)
        logger.info("[request %s] /chat end ms=%s", request_id, total_ms)
        reply = final.choices[0].message.content
        with _memory_lock:
            _save_memory(
                user,
                [
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": reply},
                ],
            )
        return {"request_id": request_id, "reply": reply}

    # If no tool call
    total_ms = int((time.time() - start_time) * 1000)
    logger.info("[request %s] /chat end ms=%s (no tool)", request_id, total_ms)
    reply = msg.content
    with _memory_lock:
        _save_memory(
            user,
            [
                {"role": "user", "content": message},
                {"role": "assistant", "content": reply},
            ],
        )
    return {"request_id": request_id, "reply": reply}


def _parse_ticket_command(text: str) -> Optional[dict]:
    normalized = text.strip()
    lower = normalized.lower()
    prefix = None
    if lower.startswith("ticket "):
        prefix = "ticket"
    elif lower.startswith("create ticket "):
        prefix = "create ticket"
    elif "ticket" in lower:
        prefix = "ticket"
    if not prefix:
        return None

    remainder = normalized
    if lower.startswith("ticket "):
        remainder = normalized[len("ticket"):].strip()
    elif lower.startswith("create ticket "):
        remainder = normalized[len("create ticket"):].strip()
    else:
        remainder = re.sub(r"(?i)ticket", "", normalized).strip()
    if "|" in remainder:
        parts = [part.strip() for part in remainder.split("|")]
        summary = parts[0] if len(parts) > 0 and parts[0] else "NetCopilot ticket"
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


def _settings_snapshot() -> list[dict[str, Any]]:
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
    return schema


def _mask_custom_settings(values: dict[str, str]) -> dict[str, str]:
    masked: dict[str, str] = {}
    for key, value in values.items():
        if re.search(r"(secret|token|pass|key)", key, re.IGNORECASE):
            masked[key] = "********"
        else:
            masked[key] = value
    return masked


def _format_link_health(result: dict[str, Any]) -> str:
    local = result.get("local", {})
    neighbor = result.get("neighbor", {})
    remote = result.get("remote", {})

    local_parsed = local.get("parsed_interface") or {}
    local_admin = local_parsed.get("admin_status") or local_parsed.get("admin_status_raw", "unknown")
    local_oper = local_parsed.get("oper_status") or local_parsed.get("oper_status_raw", "unknown")
    last_flap = local_parsed.get("last_flapped") or local_parsed.get("last_link_flapped")
    alarms = local_parsed.get("active_alarms")
    defects = local_parsed.get("active_defects")
    input_errors = local_parsed.get("input_errors") if "input_errors" in local_parsed else None
    output_errors = local_parsed.get("output_errors") if "output_errors" in local_parsed else None
    crc_errors = local_parsed.get("crc_errors") if "crc_errors" in local_parsed else None

    lines = [
        f"Link health for {result.get('device')} {result.get('interface')}:",
        f"- Local status: admin={local_admin}, oper={local_oper}",
    ]
    if last_flap:
        lines.append(f"- Last flap: {last_flap}")
    if alarms:
        lines.append(f"- Active alarms: {alarms}")
    if defects:
        lines.append(f"- Active defects: {defects}")
    lines.append(
        f"- Errors: input={input_errors or 0}, output={output_errors or 0}, crc={crc_errors or 0}"
    )

    local_optics = result.get("local_optics", {}).get("parsed_optics")
    if local_optics:
        lines.append(
            f"- Optics (local): rx={local_optics.get('rx_power_dbm')} dBm, "
            f"tx={local_optics.get('tx_power_dbm')} dBm"
        )

    if neighbor.get("neighbor_device") or neighbor.get("neighbor_interface"):
        lines.append(
            f"- LLDP neighbor: {neighbor.get('neighbor_device', 'unknown')} "
            f"{neighbor.get('neighbor_interface', '')}".strip()
        )
    else:
        lines.append("- LLDP neighbor: not detected")

    if "error" in remote:
        lines.append(f"- Remote check: {remote['error']}")
    else:
        remote_parsed = remote.get("parsed_interface") or {}
        remote_admin = remote_parsed.get("admin_status") or remote_parsed.get("admin_status_raw", "unknown")
        remote_oper = remote_parsed.get("oper_status") or remote_parsed.get("oper_status_raw", "unknown")
        lines.append(f"- Remote status: admin={remote_admin}, oper={remote_oper}")
        remote_errors = [
            f"in={remote_parsed.get('input_errors', 0)}",
            f"out={remote_parsed.get('output_errors', 0)}",
            f"crc={remote_parsed.get('crc_errors', 0)}",
        ]
        lines.append(f"- Errors (remote): {', '.join(remote_errors)}")

        remote_optics = result.get("remote_optics", {}).get("parsed_optics")
        if remote_optics:
            lines.append(
                f"- Optics (remote): rx={remote_optics.get('rx_power_dbm')} dBm, "
                f"tx={remote_optics.get('tx_power_dbm')} dBm"
            )

    local_ok = str(local_admin).lower().startswith("en") or str(local_admin).lower() == "up"
    local_oper_up = str(local_oper).lower() == "up"
    remote_admin_ok = True
    remote_oper_up = True
    if "error" in remote:
        remote_admin_ok = False
        remote_oper_up = False
    else:
        remote_admin_ok = str(remote_admin).lower().startswith("en") or str(remote_admin).lower() == "up"
        remote_oper_up = str(remote_oper).lower() == "up"

    if local_ok and local_oper_up and remote_admin_ok and remote_oper_up:
        diagnosis = "Diagnosis: Link is healthy on both ends. No immediate action required."
        next_steps = "Next steps: Continue monitoring. If alerts persist, verify the monitoring source."
    elif local_ok and not local_oper_up:
        diagnosis = "Diagnosis: Local interface is admin up but operationally down (possible physical issue or remote side down)."
        next_steps = "Next steps: Check optics/cabling, verify remote interface state, and review recent changes."
    elif not local_ok:
        diagnosis = "Diagnosis: Local interface appears administratively down."
        next_steps = "Next steps: Confirm if a shutdown was intentional; re-enable if appropriate."
    else:
        diagnosis = "Diagnosis: Link status inconsistent; further checks needed."
        next_steps = "Next steps: Collect detailed interface stats and verify LLDP neighbor data."

    lines.append(diagnosis)
    lines.append(next_steps)
    lines.append("Checklist: Status → Evidence → Likely cause → Next steps")
    return "\n".join(lines)


@app.post("/chat")
async def chat(req: ChatRequest, request: Request):
    _enforce_rate_limit(request.client.host if request.client else "unknown")
    role = _get_role_from_request(request)
    return _run_chat(req.message, req.user, role)


@app.post("/alerts/ingest")
async def ingest_alert(req: AlertIngestRequest, request: Request):
    token = request.headers.get("X-Alert-Token", "")
    if ALERT_INGEST_TOKEN and token != ALERT_INGEST_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid alert ingest token.")

    channel = req.channel or SLACK_ALERT_CHANNEL or "default"
    payload = req.model_dump()
    _save_alert(channel, req.source, payload)

    message = req.text
    if req.device or req.interface or req.severity:
        details = " ".join(
            [
                f"device={req.device}" if req.device else "",
                f"interface={req.interface}" if req.interface else "",
                f"severity={req.severity}" if req.severity else "",
            ]
        ).strip()
        if details:
            message = f"{req.text} ({details})"
    message = f"ALERT: {message}\nRun `/netcopilot investigate` to analyze."

    slack_error = None
    if channel != "default":
        slack_error = _post_slack_message(channel, message)
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
    result = _run_chat(req.text, user, TEAMS_ROLE)
    return {"type": "message", "text": result["reply"]}


@app.post("/slack/command")
async def slack_command(
    request: Request,
):
    _enforce_rate_limit("slack")
    raw_body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if SLACK_SIGNING_SECRET:
        if not timestamp.isdigit():
            raise HTTPException(status_code=403, detail="Invalid Slack signature.")
        if abs(int(time.time()) - int(timestamp)) > 60 * 5:
            raise HTTPException(status_code=403, detail="Slack request expired.")
        base = f"v0:{timestamp}:{raw_body.decode('utf-8')}"
        digest = hmac.new(
            SLACK_SIGNING_SECRET.encode("utf-8"),
            base.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        expected = f"v0={digest}"
        if not hmac.compare_digest(expected, signature):
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
    if ticket_payload and (
        ticket_payload.get("summary") == "NetCopilot ticket"
        or ticket_payload.get("description") == ticket_payload.get("summary")
    ):
        alert = _get_latest_alert(channel_id or "default") or (
            _get_latest_alert(channel_name) if channel_name else None
        )
        if alert:
            ticket_payload = _ticket_from_alert(alert)
    investigate_text = None
    if text.strip().lower().startswith("investigate"):
        remainder = text.strip()[len("investigate"):].strip()
        if remainder:
            investigate_text = remainder
        else:
            alert = _get_latest_alert(channel_id or "default")
            if alert:
                investigate_text = _format_alert_context(alert)
            else:
                if channel_name:
                    alert = _get_latest_alert(channel_name)
                if alert:
                    investigate_text = _format_alert_context(alert)
                else:
                    investigate_text = ""
    if response_url:
        def _respond_async() -> None:
            if investigate_text is not None:
                if not investigate_text.strip():
                    reply_text = "No recent alert found for this channel."
                else:
                    result = _run_chat(f"Investigate alert: {investigate_text}", user_name, SLACK_ROLE)
                    reply_text = result["reply"]
            elif ticket_payload and _is_tool_allowed(SLACK_ROLE, "create_ticket"):
                result = create_ticket(**ticket_payload)
                if "error" not in result:
                    reply_text = (
                        f"Ticket created: {result.get('ticket_id')} "
                        f"{result.get('ticket_url', '')}".strip()
                    )
                else:
                    details = result.get("details")
                    reply_text = (
                        f"Ticket creation failed: {result.get('error')}"
                        + (f" | {details}" if details else "")
                    )
            else:
                result = _run_chat(text, user_name, SLACK_ROLE)
                reply_text = result["reply"]
            try:
                import httpx

                with httpx.Client(timeout=10) as client:
                    client.post(
                        response_url,
                        json={"response_type": "in_channel", "text": reply_text},
                    )
            except Exception:
                logger.exception("Slack response_url post failed.")

        threading.Thread(target=_respond_async, daemon=True).start()
        return {
            "response_type": "ephemeral",
            "text": "Investigating alert..." if investigate_text else ("Creating ticket..." if ticket_payload else "Working on it..."),
        }

    if investigate_text is not None:
        if not investigate_text.strip():
            return {"response_type": "ephemeral", "text": "No recent alert found for this channel."}
        result = _run_chat(f"Investigate alert: {investigate_text}", user_name, SLACK_ROLE)
        return {"response_type": "in_channel", "text": result["reply"]}

    if ticket_payload and _is_tool_allowed(SLACK_ROLE, "create_ticket"):
        result = create_ticket(**ticket_payload)
        if "error" in result:
            details = result.get("details")
            message = f"Ticket creation failed: {result['error']}"
            if details:
                message = f"{message} | {details}"
            return {"response_type": "ephemeral", "text": message}
        return {
            "response_type": "in_channel",
            "text": f"Ticket created: {result.get('ticket_id')} {result.get('ticket_url', '')}".strip(),
        }

    result = _run_chat(text, user_name, SLACK_ROLE)
    return {"response_type": "in_channel", "text": result["reply"]}


@app.on_event("startup")
async def startup_summary():
    _init_memory_db()
    _init_alerts_db()
    logger.info(
        "startup version=%s openai_configured=%s jira_configured=%s devices=%s",
        APP_VERSION,
        bool(OPENAI_API_KEY),
        bool(JIRA_BASE_URL),
        len(CISCO_DEVICES),
    )


@app.get("/health")
async def health():
    ok = bool(OPENAI_API_KEY)
    return {
        "status": "ok" if ok else "degraded",
        "version": APP_VERSION,
        "openai_configured": ok,
    }


@app.get("/health/llm")
async def health_llm():
    if LLM_PROVIDER == "openai" and not OPENAI_API_KEY:
        return {"status": "degraded", "error": "OPENAI_API_KEY missing"}
    if LLM_PROVIDER == "local" and not LOCAL_LLM_BASE_URL:
        return {"status": "degraded", "error": "LOCAL_LLM_BASE_URL missing"}
    model = LOCAL_LLM_MODEL if LLM_PROVIDER == "local" else OPENAI_MODEL
    try:
        client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )
        return {"status": "ok", "provider": LLM_PROVIDER, "model": model}
    except Exception as exc:
        return {"status": "degraded", "provider": LLM_PROVIDER, "model": model, "error": str(exc)}


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
    redirect_uri = AUTH0_CALLBACK_URL
    return await oauth.auth0.authorize_redirect(
        request,
        redirect_uri=redirect_uri,
        audience=AUTH0_AUDIENCE or None,
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
            payload = json.loads(
                base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8")
            )
            if AUTH0_GROUP_CLAIM and AUTH0_GROUP_CLAIM in payload:
                claim_value = payload.get(AUTH0_GROUP_CLAIM)
                if isinstance(claim_value, list):
                    group_role = _role_from_groups([str(v) for v in claim_value])
            claim_value = payload.get(AUTH0_ROLE_CLAIM)
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
    logout_url = (
        f"https://{AUTH0_DOMAIN}/v2/logout?returnTo={AUTH0_LOGOUT_URL}"
        f"&client_id={AUTH0_CLIENT_ID}"
    )
    return RedirectResponse(url=logout_url)


@app.get("/admin/settings")
async def admin_settings(request: Request):
    role = _get_role_from_request(request)
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    return {
        "settings": _settings_snapshot(),
        "custom_settings": _mask_custom_settings(list_settings()),
        "note": "Most settings require a server restart to take effect.",
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
    set_settings(values)
    audit_logger.info("admin_settings_update keys=%s", ",".join(sorted(values.keys())))
    return {"status": "ok", "updated": list(values.keys()), "restart_required": True}


@app.get("/version")
async def version():
    return {
        "version": APP_VERSION,
    }


@app.get("/metrics")
async def metrics():
    with _metrics_lock:
        return dict(_metrics)
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
