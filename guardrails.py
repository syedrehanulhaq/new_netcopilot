"""
Security guardrails: authentication decisions, session secret, CSRF origin
checks, and the security posture report shown to admins / security reviewers.

Settings are read at call time, so rotating a key or token in the Admin UI
takes effect immediately (except Auth0 client settings, which need a restart).
"""

from __future__ import annotations

import hmac
import os
import secrets
from typing import Any, Optional
from urllib.parse import urlsplit

from settings import (
    data_path,
    encryption_key_source,
    get_setting,
    get_setting_bool,
    get_setting_int,
    guardrails_locked,
)

_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
_PROXY_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip")


# ---------------------------------------------------------------------------
# Session secret
# ---------------------------------------------------------------------------

def session_secret() -> tuple[str, str]:
    """
    Return (secret, source). An explicit SESSION_SECRET wins; otherwise a random
    secret is generated once into the data dir (mode 0600) so sessions survive
    restarts and no deployment runs with a guessable default.
    """
    explicit = os.environ.get("SESSION_SECRET", "") or get_setting("SESSION_SECRET", "")
    if explicit and explicit != "change-me" and len(explicit) >= 16:
        return explicit, "configured"
    path = data_path(".session_secret")
    if not os.path.exists(path):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(48))
        except FileExistsError:
            pass
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip(), "generated"


# ---------------------------------------------------------------------------
# API keys and anonymous access
# ---------------------------------------------------------------------------

def api_keys() -> dict[str, str]:
    """NETCOPILOT_API_KEYS = 'key1:role,key2:role' (role defaults to noc)."""
    keys: dict[str, str] = {}
    for item in get_setting("NETCOPILOT_API_KEYS", "").split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            key, role = item.split(":", 1)
            keys[key.strip()] = role.strip() or "noc"
        else:
            keys[item] = "noc"
    return keys


def role_for_api_key(presented: str) -> Optional[str]:
    """Constant-time lookup of the role for a presented API key."""
    if not presented:
        return None
    found = None
    for key, role in api_keys().items():
        if hmac.compare_digest(presented.encode("utf-8"), key.encode("utf-8")):
            found = role
    return found


def auth_configured(auth0_domain: str) -> bool:
    return bool(auth0_domain) or bool(api_keys())


def is_local_request(client_host: Optional[str], headers: Any) -> bool:
    """A direct request from this machine (not relayed by a reverse proxy)."""
    if client_host not in _LOCAL_HOSTS:
        return False
    return not any(headers.get(h) for h in _PROXY_HEADERS)


def anonymous_allowed(client_host: Optional[str], headers: Any) -> bool:
    """
    Without any authentication configured, only requests from this machine are
    served — unless ALLOW_ANONYMOUS_ACCESS=true is set for a trusted lab network.
    """
    if get_setting_bool("ALLOW_ANONYMOUS_ACCESS", False):
        return True
    return is_local_request(client_host, headers)


# ---------------------------------------------------------------------------
# CSRF: same-origin check for cookie-authenticated state changes
# ---------------------------------------------------------------------------

def allowed_origins() -> set[str]:
    return {o.strip().rstrip("/").lower() for o in get_setting("ALLOWED_ORIGINS", "").split(",") if o.strip()}


def origin_is_allowed(origin_or_referer: str, host_header: str) -> bool:
    parts = urlsplit(origin_or_referer)
    if not parts.scheme or not parts.netloc:
        return False
    origin = f"{parts.scheme}://{parts.netloc}".lower()
    if parts.netloc.lower() == (host_header or "").lower():
        return True
    return origin in allowed_origins()


# ---------------------------------------------------------------------------
# Security posture report
# ---------------------------------------------------------------------------

def _check(check_id: str, title: str, status: str, detail: str, remediation: str = "") -> dict:
    return {"id": check_id, "title": title, "status": status, "detail": detail, "remediation": remediation}


def posture_report(auth0_domain: str) -> list[dict]:
    """
    Checklist of guardrails with pass / warn / fail / info status.
    Intended for the Admin UI and for security reviews of a deployment.
    """
    checks: list[dict] = []

    if auth0_domain:
        checks.append(_check("auth", "User authentication", "pass", "SSO via Auth0 (OIDC)."))
    elif api_keys():
        checks.append(_check("auth", "User authentication", "pass",
                             f"{len(api_keys())} API key(s) configured; roles enforced per key.",
                             "Prefer SSO (Auth0/OIDC) for named-user accountability."))
    elif get_setting_bool("ALLOW_ANONYMOUS_ACCESS", False):
        checks.append(_check("auth", "User authentication", "fail",
                             "ALLOW_ANONYMOUS_ACCESS=true — anyone who can reach the service is admin.",
                             "Configure Auth0 or NETCOPILOT_API_KEYS and remove ALLOW_ANONYMOUS_ACCESS."))
    else:
        checks.append(_check("auth", "User authentication", "warn",
                             "No authentication configured; only local (127.0.0.1) requests are served.",
                             "Configure Auth0 or NETCOPILOT_API_KEYS before exposing the service."))

    checks.append(_check("read_only", "Read-only device access", "pass",
                         "Device commands are limited to show/ping/traceroute; file-writing pipes, "
                         "redirection, newlines and large ping counts are rejected in code (not configurable)."))

    if get_setting_bool("REDACT_DEVICE_SECRETS", True):
        checks.append(_check("redaction", "Secret redaction before LLM", "pass",
                             "Passwords, keys, hashes and SNMP communities in tool output are masked "
                             "before reaching the LLM, memory or history."))
    else:
        checks.append(_check("redaction", "Secret redaction before LLM", "fail",
                             "REDACT_DEVICE_SECRETS=false — device secrets can be sent to the LLM provider.",
                             "Set REDACT_DEVICE_SECRETS=true."))

    provider = get_setting("LLM_PROVIDER", "openai").lower()
    if provider == "local":
        endpoint = get_setting("LOCAL_LLM_BASE_URL", "") or "(not set)"
        checks.append(_check("llm_egress", "LLM data egress", "info",
                             f"Self-hosted/proxied LLM at {endpoint}; no data leaves your network if this endpoint is internal."))
    else:
        checks.append(_check("llm_egress", "LLM data egress", "info",
                             f"Prompts and (redacted) tool output are sent to the {provider} API.",
                             "Use LLM_PROVIDER=local with an internal endpoint (e.g. LiteLLM → Bedrock/Azure) "
                             "if data must stay in your cloud tenancy."))

    if get_setting_bool("SSH_STRICT_HOST_KEY_CHECKING", False):
        checks.append(_check("ssh_host_keys", "SSH host key verification", "pass",
                             "Unknown or changed device host keys are rejected."))
    else:
        checks.append(_check("ssh_host_keys", "SSH host key verification", "warn",
                             "Device host keys are not verified (MITM could capture device credentials).",
                             "Provide a known_hosts file and set SSH_STRICT_HOST_KEY_CHECKING=true."))

    enc_source = encryption_key_source()
    checks.append(_check(
        "secrets_at_rest", "Secrets encrypted at rest",
        "pass" if enc_source == "env" else "warn",
        "Credentials saved in the Admin UI are encrypted (Fernet) with a key from "
        + ("NETCOPILOT_ENCRYPTION_KEY." if enc_source == "env" else "a key file stored next to the database."),
        "" if enc_source == "env" else "Set NETCOPILOT_ENCRYPTION_KEY from your secret manager.",
    ))

    _, secret_source = session_secret()
    secure_cookie = get_setting_bool("SESSION_COOKIE_SECURE", False)
    checks.append(_check(
        "session", "Session cookies",
        "pass" if secure_cookie else "warn",
        f"Session secret {secret_source}; cookie Secure flag {'on' if secure_cookie else 'off'}; "
        f"sessions expire after {get_setting_int('SESSION_MAX_AGE_HOURS', 12)}h; SameSite=Lax + origin check on writes.",
        "" if secure_cookie else "Serve over HTTPS and set SESSION_COOKIE_SECURE=true.",
    ))

    checks.append(_check(
        "guardrail_lock", "Guardrails locked to deployment config",
        "pass" if guardrails_locked() else "warn",
        "Security settings can only be changed via environment/secret manager."
        if guardrails_locked() else "Admins can weaken guardrails (e.g. disable redaction) from the Admin UI.",
        "" if guardrails_locked() else "Set NETCOPILOT_LOCK_GUARDRAILS=true in the deployment.",
    ))

    ingest_token = get_setting("ALERT_INGEST_TOKEN", "")
    checks.append(_check(
        "alert_ingest", "Alert webhook authentication",
        "pass" if ingest_token else "warn",
        "Alert webhooks require X-Alert-Token." if ingest_token
        else "No ALERT_INGEST_TOKEN — alert webhooks only accepted from anonymous-allowed clients.",
        "" if ingest_token else "Set ALERT_INGEST_TOKEN and configure it in Alertmanager / your NMS.",
    ))

    if get_setting("SLACK_BOT_TOKEN", "") or get_setting("SLACK_SIGNING_SECRET", ""):
        signing = bool(get_setting("SLACK_SIGNING_SECRET", ""))
        checks.append(_check(
            "slack", "Slack request signing", "pass" if signing else "fail",
            "Slack requests are verified with the signing secret (HMAC, 5-minute replay window)."
            if signing else "Slack is configured without SLACK_SIGNING_SECRET.",
            "" if signing else "Set SLACK_SIGNING_SECRET from the Slack app settings.",
        ))

    deny = get_setting("TOOL_DENYLIST", "")
    allow = get_setting("TOOL_ALLOWLIST", "")
    checks.append(_check("tool_policy", "Tool allow/deny policy", "info",
                         f"Allowlist: {allow or '(all role tools)'}; denylist: {deny or '(none)'}. "
                         "Jira write tools are limited to roles noc-ticket and admin."))

    checks.append(_check("audit", "Audit trail", "pass",
                         f"Logins, chats, every tool call with arguments, admin changes and denied requests "
                         f"are recorded; retention {get_setting_int('AUDIT_RETENTION_DAYS', 90)} days "
                         "(Admin → Audit, CSV export)."))
    return checks
