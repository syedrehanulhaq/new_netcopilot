import hashlib
import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

import yaml

from settings import data_path


PLAYBOOK_DB = data_path("playbooks.db")
PLAYBOOK_YAML = os.path.join(os.path.dirname(__file__), "playbooks.yaml")


def _init_db() -> None:
    with sqlite3.connect(PLAYBOOK_DB) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS playbooks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                intent TEXT NOT NULL,
                vendor TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                inputs_json TEXT NOT NULL,
                steps_json TEXT NOT NULL,
                checks_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE TABLE IF NOT EXISTS playbook_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")


def _slugify(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "_" for ch in value.strip())
    cleaned = "_".join(part for part in cleaned.split("_") if part)
    return cleaned or "playbook"


def _infer_inputs(tools: List[str]) -> List[str]:
    inputs: List[str] = []
    tool_inputs = {
        "get_interface_health": ["device", "interface"],
        "get_interface_counters": ["device", "interface"],
        "get_interface_status": ["device"],
        "get_lldp_neighbor": ["device", "interface"],
        "get_top_flapping_links": [],
        "get_bgp_neighbor_status": ["device", "neighbor"],
        "get_bgp_summary": ["device"],
        "get_ospf_neighbors": ["device"],
        "get_isis_neighbors": ["device"],
        "get_isis_interface": ["device", "interface"],
        "get_optics": ["device", "interface"],
        "get_route_lookup": ["device", "prefix"],
        "get_bgp_route": ["device", "prefix"],
        "get_acl_hits": ["device", "acl_name"],
        "get_mtu_info": ["device", "interface"],
        "get_ping": ["device", "destination"],
        "get_traceroute": ["device", "destination"],
        "get_cpu_memory": ["device"],
        "get_chassis_health": ["device"],
    }
    for tool in tools:
        for item in tool_inputs.get(tool, []):
            if item not in inputs:
                inputs.append(item)
    return inputs


def _seed_from_yaml() -> None:
    """Seed built-in playbooks from playbooks.yaml.

    Runs when the DB is empty or playbooks.yaml has changed since the last
    seed, so fixes to built-in playbooks reach existing installs. Playbooks
    created in the Admin UI (ids not in the YAML) are left untouched.
    """
    if not os.path.exists(PLAYBOOK_YAML):
        return
    with open(PLAYBOOK_YAML, "rb") as handle:
        content = handle.read()
    digest = hashlib.sha256(content).hexdigest()
    with sqlite3.connect(PLAYBOOK_DB) as conn:
        row = conn.execute("SELECT value FROM playbook_meta WHERE key = 'yaml_sha256'").fetchone()
        existing = conn.execute("SELECT COUNT(1) FROM playbooks").fetchone()
    if row and row[0] == digest and existing and existing[0] > 0:
        return
    raw = yaml.safe_load(content) or []
    playbooks: List[Dict[str, Any]] = []
    for item in raw:
        tools = item.get("tools") or []
        steps = [{"type": "tool", "tool": tool} for tool in tools]
        inputs = _infer_inputs(tools)
        playbooks.append(
            {
                "id": _slugify(item.get("intent") or item.get("name") or "playbook"),
                "title": item.get("name") or "Playbook",
                "intent": item.get("intent") or "",
                "vendor": "any",
                "enabled": True,
                "inputs": inputs,
                "steps": steps,
                "checks": {
                    "severity_rules": item.get("severity_rules", {}),
                    "evidence_fields": item.get("evidence_fields", []),
                },
            }
        )
    upsert_playbooks(playbooks)
    with sqlite3.connect(PLAYBOOK_DB) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO playbook_meta (key, value) VALUES ('yaml_sha256', ?)",
            (digest,),
        )


def list_playbooks() -> List[Dict[str, Any]]:
    _init_db()
    _seed_from_yaml()
    with sqlite3.connect(PLAYBOOK_DB) as conn:
        rows = conn.execute(
            """
            SELECT id, title, intent, vendor, enabled, inputs_json, steps_json, checks_json, updated_at
            FROM playbooks
            ORDER BY title
            """
        ).fetchall()
    playbooks: List[Dict[str, Any]] = []
    for row in rows:
        playbooks.append(
            {
                "id": row[0],
                "title": row[1],
                "intent": row[2],
                "vendor": row[3],
                "enabled": bool(row[4]),
                "inputs": json.loads(row[5]) if row[5] else [],
                "steps": json.loads(row[6]) if row[6] else [],
                "checks": json.loads(row[7]) if row[7] else {},
                "updated_at": row[8],
            }
        )
    return playbooks


def get_playbook(playbook_id: str) -> Optional[Dict[str, Any]]:
    _init_db()
    _seed_from_yaml()
    with sqlite3.connect(PLAYBOOK_DB) as conn:
        row = conn.execute(
            """
            SELECT id, title, intent, vendor, enabled, inputs_json, steps_json, checks_json, updated_at
            FROM playbooks
            WHERE id = ?
            """,
            (playbook_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "title": row[1],
        "intent": row[2],
        "vendor": row[3],
        "enabled": bool(row[4]),
        "inputs": json.loads(row[5]) if row[5] else [],
        "steps": json.loads(row[6]) if row[6] else [],
        "checks": json.loads(row[7]) if row[7] else {},
        "updated_at": row[8],
    }


def upsert_playbooks(playbooks: List[Dict[str, Any]]) -> None:
    _init_db()
    now = datetime.utcnow().isoformat() + "Z"
    rows = []
    for pb in playbooks:
        pb_id = pb.get("id") or _slugify(pb.get("title") or pb.get("intent") or "playbook")
        rows.append(
            (
                pb_id,
                pb.get("title") or pb_id,
                pb.get("intent") or "",
                pb.get("vendor") or "any",
                1 if pb.get("enabled", True) else 0,
                json.dumps(pb.get("inputs") or []),
                json.dumps(pb.get("steps") or []),
                json.dumps(pb.get("checks") or {}),
                now,
            )
        )
    with sqlite3.connect(PLAYBOOK_DB) as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO playbooks
            (id, title, intent, vendor, enabled, inputs_json, steps_json, checks_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


def delete_playbook(playbook_id: str) -> bool:
    _init_db()
    with sqlite3.connect(PLAYBOOK_DB) as conn:
        cur = conn.execute("DELETE FROM playbooks WHERE id = ?", (playbook_id,))
        return cur.rowcount > 0


def format_playbook_summary(playbooks: Optional[List[Dict[str, Any]]] = None) -> str:
    items = playbooks if playbooks is not None else list_playbooks()
    lines: List[str] = []
    for item in items:
        name = item.get("title") or item.get("id") or "Playbook"
        intent = item.get("intent") or "unknown"
        steps = ", ".join(
            step.get("tool", step.get("type", "step")) for step in (item.get("steps") or [])
        )
        lines.append(f"- {name} (intent={intent}) steps=[{steps}]")
    return "\n".join(lines) if lines else "- No playbooks configured"

