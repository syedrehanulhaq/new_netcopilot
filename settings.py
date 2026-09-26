"""
Runtime settings (Admin UI) stored in SQLite, falling back to environment variables.

Secret-looking settings (names containing secret/token/pass/key) are encrypted at
rest with Fernet (AES-128-CBC + HMAC-SHA256). The key comes from
NETCOPILOT_ENCRYPTION_KEY, or is generated once into <data dir>/.settings.key
(mode 0600). Supply the key from a secret manager in production so a copy of the
data volume alone does not reveal credentials.
"""

import logging
import os
import re
import sqlite3
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger("netcopilot.settings")


def data_path(filename: str) -> str:
    """Location of a runtime data file (SQLite DBs). NETCOPILOT_DATA_DIR puts them on a volume."""
    data_dir = os.environ.get("NETCOPILOT_DATA_DIR", "")
    if data_dir:
        os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, filename)


_SETTINGS_DB = data_path("settings.db")
_ENC_PREFIX = "enc:v1:"
_SECRET_NAME = re.compile(r"(secret|token|pass|key)", re.IGNORECASE)

_fernet = None
_fernet_lock = threading.Lock()
_migrated = False

# Security-relevant settings. With NETCOPILOT_LOCK_GUARDRAILS=true they are read
# only from the environment (deployment / secret manager) and cannot be changed
# from the Admin UI — an application admin cannot weaken the guardrails.
GUARDRAIL_KEYS = frozenset({
    "ALLOW_ANONYMOUS_ACCESS",
    "ALLOWED_ORIGINS",
    "AUDIT_RETENTION_DAYS",
    "DEVICE_REGISTRY_PATH",
    "LLM_PROVIDER",
    "LOCAL_LLM_BASE_URL",
    "LOG_REDACTION_ENABLED",
    "LOG_REDACTION_FIELDS",
    "NETCOPILOT_API_KEYS",
    "REDACT_DEVICE_SECRETS",
    "SESSION_COOKIE_SECURE",
    "SESSION_MAX_AGE_HOURS",
    "SSH_KNOWN_HOSTS_FILE",
    "SSH_STRICT_HOST_KEY_CHECKING",
    "TOOL_ALLOWLIST",
    "TOOL_DENYLIST",
    "TOOL_RESULT_MAX_CHARS",
})


class LockedSettingError(ValueError):
    """Raised when the Admin UI tries to change a locked guardrail setting."""


def guardrails_locked() -> bool:
    return os.environ.get("NETCOPILOT_LOCK_GUARDRAILS", "").strip().lower() in {"1", "true", "yes", "on"}


# Names that match the secret pattern but hold non-sensitive values.
_NOT_SECRET = frozenset({"JIRA_PROJECT_KEY", "ANTHROPIC_MAX_TOKENS"})


def is_secret_setting(key: str) -> bool:
    return bool(_SECRET_NAME.search(key or "")) and key not in _NOT_SECRET


def _write_private_file(path: str, content: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(content)


def encryption_key_source() -> str:
    """'env' when NETCOPILOT_ENCRYPTION_KEY is set, else 'file' (auto-generated key file)."""
    return "env" if os.environ.get("NETCOPILOT_ENCRYPTION_KEY") else "file"


def _get_fernet():
    global _fernet
    if _fernet is not None:
        return _fernet
    from cryptography.fernet import Fernet

    with _fernet_lock:
        if _fernet is not None:
            return _fernet
        key = os.environ.get("NETCOPILOT_ENCRYPTION_KEY", "").strip().encode()
        if not key:
            key_path = data_path(".settings.key")
            if not os.path.exists(key_path):
                try:
                    _write_private_file(key_path, Fernet.generate_key())
                    logger.warning(
                        "Generated a settings encryption key at %s. In production set "
                        "NETCOPILOT_ENCRYPTION_KEY from your secret manager instead.", key_path,
                    )
                except FileExistsError:
                    pass  # another worker created it first
            with open(key_path, "rb") as f:
                key = f.read().strip()
        _fernet = Fernet(key)
        return _fernet


def _encrypt(value: str) -> str:
    return _ENC_PREFIX + _get_fernet().encrypt(value.encode("utf-8")).decode("ascii")


def _decrypt(stored: str) -> Optional[str]:
    if not stored.startswith(_ENC_PREFIX):
        return stored  # legacy plaintext row (encrypted by the migration pass)
    from cryptography.fernet import InvalidToken

    try:
        return _get_fernet().decrypt(stored[len(_ENC_PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken:
        logger.error("Cannot decrypt a stored setting — was NETCOPILOT_ENCRYPTION_KEY changed?")
        return None


def _init_settings_db() -> None:
    global _migrated
    with sqlite3.connect(_SETTINGS_DB) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        if not _migrated:
            # Encrypt secrets that older versions stored in plaintext.
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
            plaintext = [
                (k, v) for k, v in rows
                if is_secret_setting(k) and v and not v.startswith(_ENC_PREFIX)
            ]
            if plaintext:
                conn.executemany(
                    "UPDATE settings SET value = ? WHERE key = ?",
                    [(_encrypt(v), k) for k, v in plaintext],
                )
                logger.info("Encrypted %d plaintext secret setting(s) at rest.", len(plaintext))
            _migrated = True


def get_setting(key: str, default: str = "") -> str:
    if key in GUARDRAIL_KEYS and guardrails_locked():
        return os.environ.get(key, "") or default
    _init_settings_db()
    with sqlite3.connect(_SETTINGS_DB) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row and row[0]:
        value = _decrypt(row[0])
        if value:
            return value
    # A blank stored value means "not set here": fall back to env, then the default.
    return os.environ.get(key, "") or default


def get_setting_int(key: str, default: int) -> int:
    value = get_setting(key, str(default))
    try:
        return int(value)
    except ValueError:
        return default


def get_setting_bool(key: str, default: bool) -> bool:
    value = get_setting(key, "true" if default else "false").lower()
    return value in {"1", "true", "yes", "on"}


def list_settings() -> Dict[str, str]:
    _init_settings_db()
    with sqlite3.connect(_SETTINGS_DB) as conn:
        rows = conn.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
    result: Dict[str, str] = {}
    for key, value in rows:
        decrypted = _decrypt(value)
        result[key] = decrypted if decrypted is not None else ""
    return result


def set_settings(values: Dict[str, Any]) -> None:
    if guardrails_locked():
        locked = sorted(k for k in values if str(k) in GUARDRAIL_KEYS)
        if locked:
            raise LockedSettingError(
                f"Locked by NETCOPILOT_LOCK_GUARDRAILS (set via deployment config): {', '.join(locked)}"
            )
    _init_settings_db()
    rows, cleared = [], []
    for k, v in values.items():
        key, value = str(k), str(v).strip()
        if not value:
            cleared.append((key,))  # blank = back to the environment / built-in default
            continue
        rows.append((key, _encrypt(value) if is_secret_setting(key) else value))
    with sqlite3.connect(_SETTINGS_DB) as conn:
        conn.executemany("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", rows)
        conn.executemany("DELETE FROM settings WHERE key = ?", cleared)
