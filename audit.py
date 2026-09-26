"""
Persistent audit trail (SQLite) alongside the JSON audit log on stdout.

Records who did what: chats, every tool call with its arguments, admin changes,
logins and denied requests. Queryable from the Admin UI and exportable as CSV.
Retention: AUDIT_RETENTION_DAYS (default 90).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Optional

from settings import data_path, get_setting_int

_AUDIT_DB = data_path("audit.db")
_lock = threading.Lock()
_writes = 0


def init_audit_db() -> None:
    with sqlite3.connect(_AUDIT_DB) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                event TEXT NOT NULL,
                user TEXT,
                role TEXT,
                detail TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS audit_ts ON audit (ts)")


def record(payload: dict[str, Any]) -> None:
    global _writes
    now = time.time()
    try:
        with _lock, sqlite3.connect(_AUDIT_DB) as conn:
            conn.execute(
                "INSERT INTO audit (ts, event, user, role, detail) VALUES (?, ?, ?, ?, ?)",
                (
                    now,
                    str(payload.get("event", "unknown")),
                    payload.get("user"),
                    payload.get("role"),
                    json.dumps(payload, default=str, ensure_ascii=True),
                ),
            )
            _writes += 1
            if _writes % 500 == 1:
                cutoff = now - get_setting_int("AUDIT_RETENTION_DAYS", 90) * 86400
                conn.execute("DELETE FROM audit WHERE ts < ?", (cutoff,))
    except sqlite3.OperationalError:
        init_audit_db()


def query(
    event: Optional[str] = None,
    user: Optional[str] = None,
    since: Optional[float] = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    clauses, params = [], []
    if event:
        clauses.append("event = ?")
        params.append(event)
    if user:
        clauses.append("user = ?")
        params.append(user)
    if since:
        clauses.append("ts >= ?")
        params.append(since)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit), 5000)))
    with sqlite3.connect(_AUDIT_DB) as conn:
        rows = conn.execute(
            f"SELECT id, ts, event, user, role, detail FROM audit {where} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
    return [
        {"id": r[0], "ts": r[1], "event": r[2], "user": r[3], "role": r[4], "detail": json.loads(r[5])}
        for r in rows
    ]
