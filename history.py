"""
Investigation history: every chat turn (UI, API, Slack, Teams, alert automation)
with the question, the answer, which tools ran (with timing/errors), the redacted
evidence they returned, playbook severity and any ticket created.

Users see their own investigations; admins see everyone's.
Retention: HISTORY_RETENTION_DAYS (default 90).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Optional

from settings import data_path, get_setting_int

_DB = data_path("history.db")
_lock = threading.Lock()
_EVIDENCE_MAX = 4_000   # chars kept per tool result


def init_history_db() -> None:
    with sqlite3.connect(_DB) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS investigations (
                id TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                user TEXT NOT NULL,
                role TEXT,
                source TEXT NOT NULL,
                question TEXT NOT NULL,
                reply TEXT NOT NULL,
                tools_json TEXT NOT NULL,
                severity TEXT,
                ticket TEXT,
                status TEXT NOT NULL,
                duration_ms INTEGER,
                usage_json TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS inv_user_time ON investigations (user, created_at)")


def _tool_steps(messages: list) -> tuple[list[dict], Optional[str], Optional[str]]:
    """Pair assistant tool calls with their results; pull out playbook severity and ticket id."""
    calls: dict[str, dict] = {}
    for m in messages:
        tool_calls = getattr(m, "tool_calls", None) or (m.get("tool_calls") if isinstance(m, dict) else None)
        for tc in tool_calls or []:
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            try:
                args = json.loads(fn.arguments or "{}")
            except (TypeError, ValueError):
                args = {}
            calls[getattr(tc, "id", "")] = {"name": fn.name, "args": args}

    steps, severity, ticket = [], None, None
    for m in messages:
        if not (isinstance(m, dict) and m.get("role") == "tool"):
            continue
        content = str(m.get("content", ""))
        call = calls.get(m.get("tool_call_id", ""), {"name": m.get("name"), "args": {}})
        error = None
        try:
            parsed = json.loads(content)
        except ValueError:
            parsed = None
        if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
            error = parsed[0].get("error")
        if isinstance(parsed, dict):
            error = parsed.get("error")
            if call["name"] == "run_playbook" and parsed.get("severity"):
                severity = parsed["severity"]
            if "ticket" in str(call["name"]) and parsed.get("ticket_id"):
                ticket = str(parsed.get("ticket_url") or parsed["ticket_id"])
        steps.append({
            "name": call["name"],
            "args": call["args"],
            "error": error,
            "result": content[:_EVIDENCE_MAX] + ("…[truncated]" if len(content) > _EVIDENCE_MAX else ""),
        })
    return steps, severity, ticket


def record(
    *,
    user: str,
    role: str,
    source: str,
    question: str,
    reply: str,
    messages: list,
    status: str,
    duration_ms: int,
    usage: Optional[dict] = None,
) -> str:
    inv_id = uuid.uuid4().hex[:12]
    steps, severity, ticket = _tool_steps(messages)
    now = time.time()
    with _lock, sqlite3.connect(_DB) as conn:
        conn.execute(
            "INSERT INTO investigations (id, created_at, user, role, source, question, reply, tools_json, "
            "severity, ticket, status, duration_ms, usage_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (inv_id, now, user, role, source, question, reply, json.dumps(steps, default=str),
             severity, ticket, status, duration_ms, json.dumps(usage or {})),
        )
        cutoff = now - get_setting_int("HISTORY_RETENTION_DAYS", 90) * 86400
        conn.execute("DELETE FROM investigations WHERE created_at < ?", (cutoff,))
    return inv_id


def _row(r: tuple, full: bool) -> dict[str, Any]:
    item = {
        "id": r[0], "created_at": r[1], "user": r[2], "role": r[3], "source": r[4],
        "question": r[5], "severity": r[8], "ticket": r[9], "status": r[10], "duration_ms": r[11],
    }
    steps = json.loads(r[7] or "[]")
    item["tools"] = [s["name"] for s in steps]
    if full:
        item["reply"] = r[6]
        item["steps"] = steps
        item["usage"] = json.loads(r[12] or "{}")
    else:
        item["reply_preview"] = (r[6] or "")[:240]
    return item


_COLUMNS = "id, created_at, user, role, source, question, reply, tools_json, severity, ticket, status, duration_ms, usage_json"


def list_investigations(user: Optional[str], query: str = "", limit: int = 50) -> list[dict]:
    clauses, params = [], []
    if user is not None:
        clauses.append("user = ?")
        params.append(user)
    if query:
        clauses.append("(question LIKE ? OR reply LIKE ?)")
        params += [f"%{query}%", f"%{query}%"]
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit), 500)))
    with sqlite3.connect(_DB) as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM investigations {where} ORDER BY created_at DESC LIMIT ?", params
        ).fetchall()
    return [_row(r, full=False) for r in rows]


def get_investigation(inv_id: str) -> Optional[dict]:
    with sqlite3.connect(_DB) as conn:
        row = conn.execute(f"SELECT {_COLUMNS} FROM investigations WHERE id = ?", (inv_id,)).fetchone()
    return _row(row, full=True) if row else None
