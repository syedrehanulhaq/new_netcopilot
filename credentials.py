"""
Secrets abstraction for NetCopilot.

Priority order (first match wins):
  1. HashiCorp Vault   — if VAULT_ADDR is set
  2. settings.db       — UI-managed credentials (always wins over .env)
  3. Environment vars  — os.environ / .env fallback

Usage:
    from secrets import get_secret

    password = get_secret("CISCO_PASS")

To enable Vault:
    export VAULT_ADDR=https://vault.example.com
    export VAULT_TOKEN=s.xxxxx          # or VAULT_ROLE_ID + VAULT_SECRET_ID for AppRole
    export VAULT_SECRET_PATH=secret/netcopilot  # KV v2 path

All credential keys are read from the Vault path as a flat KV map.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Optional

logger = logging.getLogger("netcopilot.secrets")

# ---------------------------------------------------------------------------
# Vault provider (optional)
# ---------------------------------------------------------------------------

_vault_cache: dict[str, str] = {}
_vault_loaded = False
_vault_degraded = False  # True if Vault was configured but failed to load


def is_vault_degraded() -> bool:
    """Returns True if Vault is configured but unreachable (silently using fallback)."""
    return _vault_degraded


def _load_vault() -> None:
    global _vault_cache, _vault_loaded, _vault_degraded
    if _vault_loaded:
        return
    _vault_loaded = True

    vault_addr = os.environ.get("VAULT_ADDR", "")
    if not vault_addr:
        return

    secret_path = os.environ.get("VAULT_SECRET_PATH", "secret/netcopilot")
    token = os.environ.get("VAULT_TOKEN", "")

    # AppRole auth if token not provided
    if not token:
        role_id = os.environ.get("VAULT_ROLE_ID", "")
        secret_id = os.environ.get("VAULT_SECRET_ID", "")
        if role_id and secret_id:
            token = _vault_approle_login(vault_addr, role_id, secret_id)

    if not token:
        logger.warning("VAULT_ADDR is set but no VAULT_TOKEN or AppRole credentials found.")
        return

    try:
        import urllib.request
        import json

        # KV v2: /v1/secret/data/<path>
        url = f"{vault_addr.rstrip('/')}/v1/{secret_path.replace('secret/', 'secret/data/', 1)}"
        req = urllib.request.Request(url, headers={"X-Vault-Token": token})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        _vault_cache = data.get("data", {}).get("data", {})
        logger.info("Vault secrets loaded from %s (%d keys)", secret_path, len(_vault_cache))
    except Exception as exc:
        logger.error(
            "Failed to load secrets from Vault: %s — falling back to settings.db/env vars. "
            "Credentials may be less secure until Vault is restored.",
            exc,
        )
        _vault_degraded = True


def _vault_approle_login(addr: str, role_id: str, secret_id: str) -> str:
    try:
        import urllib.request
        import json

        payload = json.dumps({"role_id": role_id, "secret_id": secret_id}).encode()
        url = f"{addr.rstrip('/')}/v1/auth/approle/login"
        req = urllib.request.Request(url, data=payload, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        return data["auth"]["client_token"]
    except Exception as exc:
        logger.error("Vault AppRole login failed: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# SQLite settings fallback
# ---------------------------------------------------------------------------

def _from_settings_db(key: str) -> Optional[str]:
    """Read from settings.db — last resort for dev environments."""
    try:
        from settings import get_setting
        val = get_setting(key, "")
        return val if val else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_secret(key: str, default: str = "") -> str:
    """
    Retrieve a secret by key.

    Search order: Vault → settings.db → env var → default

    settings.db takes priority over env vars / .env so that credentials
    updated via the UI take effect immediately without editing .env.
    Vault (if configured) still wins over everything for production deployments.
    """
    # 1. Vault
    _load_vault()
    if key in _vault_cache:
        return _vault_cache[key]

    # 2. settings.db (UI-managed credentials win over .env)
    db_val = _from_settings_db(key)
    if db_val:
        return db_val

    # 3. Environment variable / .env fallback
    env_val = os.environ.get(key, "")
    if env_val:
        return env_val

    return default


def has_secret(key: str) -> bool:
    """Return True if a secret value exists (non-empty)."""
    return bool(get_secret(key))
