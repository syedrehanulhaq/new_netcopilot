import os
import sqlite3
from typing import Any, Dict


def data_path(filename: str) -> str:
    """Location of a runtime data file (SQLite DBs). NETCOPILOT_DATA_DIR puts them on a volume."""
    data_dir = os.environ.get("NETCOPILOT_DATA_DIR", "")
    if data_dir:
        os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, filename)


_SETTINGS_DB = data_path("settings.db")


def _init_settings_db() -> None:
    with sqlite3.connect(_SETTINGS_DB) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )


def get_setting(key: str, default: str = "") -> str:
    _init_settings_db()
    with sqlite3.connect(_SETTINGS_DB) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row and row[0] is not None:
        return row[0]
    return os.environ.get(key, default)


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
    return {key: value for key, value in rows}


def set_settings(values: Dict[str, Any]) -> None:
    _init_settings_db()
    with sqlite3.connect(_SETTINGS_DB) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            [(str(k), str(v)) for k, v in values.items()],
        )
