from __future__ import annotations

import os
from typing import Any

import yaml

PLAYBOOK_PATH = os.path.join(os.path.dirname(__file__), "playbooks.yaml")


def _load_playbooks() -> list[dict[str, Any]]:
    try:
        with open(PLAYBOOK_PATH, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or []
        if isinstance(data, list):
            return data
    except Exception:
        return []
    return []


def _format_playbooks() -> str:
    lines: list[str] = []
    for item in _load_playbooks():
        name = item.get("name") or "Playbook"
        tools = item.get("tools") or []
        evidence = item.get("evidence_fields") or []
        severity = item.get("severity_rules") or {}
        hints = item.get("vendor_hints") or {}
        severity_str = ", ".join(f"{k}={v}" for k, v in severity.items()) if severity else "none"
        hints_str = ", ".join(f"{k}:{v}" for k, v in hints.items()) if hints else "none"
        lines.append(
            f"- {name}: tools={', '.join(tools)} | evidence={', '.join(evidence)} | "
            f"severity={severity_str} | hints={hints_str}"
        )
    return "\n".join(lines)


PLAYBOOK_SUMMARY = _format_playbooks()
