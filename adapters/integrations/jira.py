"""
Jira Integration Adapter — direct httpx calls, no MCP.

Exposes the same tool API as the old jira MCP server but runs in-process.

Export:
    TOOLS        dict[str, callable]   base-name → async function
    TOOL_SCHEMAS list[dict]            OpenAI function-call schemas (jira_ prefixed)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from functools import partial
from typing import Any, Dict, List, Optional

import httpx
from credentials import get_secret

logger = logging.getLogger("netcopilot.adapters.jira")


# ---------------------------------------------------------------------------
# Config helpers (read at call time so settings.db updates are picked up)
# ---------------------------------------------------------------------------

def _base() -> str:
    return get_secret("JIRA_BASE_URL", "").rstrip("/")

def _auth() -> tuple[str, str]:
    return (get_secret("JIRA_EMAIL", ""), get_secret("JIRA_API_TOKEN", ""))

def _project_key() -> str:
    return get_secret("JIRA_PROJECT_KEY", "NOC")

def _default_issue_type() -> str:
    return get_secret("JIRA_DEFAULT_ISSUE_TYPE", "Task")

def _default_priority() -> str:
    return get_secret("JIRA_DEFAULT_PRIORITY", "Medium")

def _check_config() -> Optional[str]:
    missing = [k for k in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY")
               if not get_secret(k, "")]
    return f"Missing Jira config: {', '.join(missing)}" if missing else None


# ---------------------------------------------------------------------------
# Sync helpers (run in executor)
# ---------------------------------------------------------------------------

def _sync_resolve_issue_type(ticket_type: str) -> str:
    try:
        r = httpx.get(f"{_base()}/rest/api/3/project/{_project_key()}", auth=_auth(), timeout=10)
        pid = r.json().get("id") if r.is_success else None
        if pid:
            r2 = httpx.get(f"{_base()}/rest/api/3/issuetype/project",
                           params={"projectId": pid}, auth=_auth(), timeout=10)
            if r2.is_success:
                types = r2.json()
                target = ticket_type.strip().lower()
                for t in types:
                    if str(t.get("name", "")).strip().lower() == target:
                        return t["name"]
                if types:
                    return types[0]["name"]
    except Exception:
        pass
    return _default_issue_type()


def _sync_resolve_priority(priority: str) -> str:
    try:
        r = httpx.get(f"{_base()}/rest/api/3/priority", auth=_auth(), timeout=10)
        if r.is_success:
            target = priority.strip().lower()
            for p in r.json():
                if str(p.get("name", "")).strip().lower() == target:
                    return p["name"]
    except Exception:
        pass
    return priority or _default_priority()


def _sync_resolve_transition(issue_key: str, transition_name: str) -> str:
    try:
        r = httpx.get(f"{_base()}/rest/api/3/issue/{issue_key}/transitions",
                      auth=_auth(), timeout=10)
        if r.is_success:
            target = transition_name.strip().lower()
            for t in r.json().get("transitions", []):
                if str(t.get("name", "")).strip().lower() == target:
                    return str(t["id"])
    except Exception:
        pass
    return ""


def _sync_resolve_assignee(query: str) -> str:
    if not query:
        return ""
    try:
        r = httpx.get(f"{_base()}/rest/api/3/user/search",
                      params={"query": query}, auth=_auth(), timeout=10)
        if r.is_success:
            users = r.json()
            q = query.strip().lower()
            for u in users:
                if q in str(u.get("emailAddress", "")).lower() or \
                   q == str(u.get("displayName", "")).lower():
                    return str(u.get("accountId", ""))
            if users:
                return str(users[0].get("accountId", ""))
    except Exception:
        pass
    return ""


def _sync_get_labels(issue_key: str) -> List[str]:
    try:
        r = httpx.get(f"{_base()}/rest/api/3/issue/{issue_key}",
                      params={"fields": "labels"}, auth=_auth(), timeout=10)
        if r.is_success:
            return [str(l) for l in r.json().get("fields", {}).get("labels", [])]
    except Exception:
        pass
    return []


def _normalize_label(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", value.strip().lower())
    return re.sub(r"_{2,}", "_", cleaned).strip("_") or "team"


def _doc_text(text: str) -> dict:
    """Jira ADF paragraph node."""
    return {
        "type": "doc", "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


# ---------------------------------------------------------------------------
# Sync tool implementations
# ---------------------------------------------------------------------------

def _sync_create_ticket(
    ticket_type: str,
    summary: str,
    description: str,
    team: str,
    priority: str,
) -> Dict[str, Any]:
    err = _check_config()
    if err:
        return {"error": err}

    resolved_type = _sync_resolve_issue_type(ticket_type)
    resolved_priority = _sync_resolve_priority(priority)
    label = _normalize_label(team)

    payload = {
        "fields": {
            "project": {"key": _project_key()},
            "summary": summary,
            "description": _doc_text(description),
            "issuetype": {"name": resolved_type},
            "priority": {"name": resolved_priority},
            "labels": [label],
        }
    }
    try:
        r = httpx.post(f"{_base()}/rest/api/3/issue", json=payload, auth=_auth(), timeout=15)
        if not r.is_success:
            return {"error": f"Jira API error {r.status_code}", "details": r.text}
        key = r.json().get("key")
        return {
            "ticket_id": key,
            "ticket_url": f"{_base()}/browse/{key}",
            "ticket_type": resolved_type,
            "summary": summary,
            "priority": resolved_priority,
            "team": team,
        }
    except Exception as exc:
        return {"error": f"Jira request failed: {exc}"}


def _sync_close_ticket(issue_key: str, transition_name: str = "Done") -> Dict[str, Any]:
    err = _check_config()
    if err:
        return {"error": err}

    tid = _sync_resolve_transition(issue_key, transition_name)
    if not tid:
        return {"error": f"Transition '{transition_name}' not found for {issue_key}."}
    try:
        r = httpx.post(f"{_base()}/rest/api/3/issue/{issue_key}/transitions",
                       json={"transition": {"id": tid}}, auth=_auth(), timeout=15)
        if not r.is_success:
            return {"error": f"Jira API error {r.status_code}", "details": r.text}
        return {"status": "ok", "issue_key": issue_key, "transition": transition_name}
    except Exception as exc:
        return {"error": f"Jira request failed: {exc}"}


def _sync_add_comment(issue_key: str, comment: str) -> Dict[str, Any]:
    err = _check_config()
    if err:
        return {"error": err}
    try:
        r = httpx.post(f"{_base()}/rest/api/3/issue/{issue_key}/comment",
                       json={"body": _doc_text(comment)}, auth=_auth(), timeout=15)
        if not r.is_success:
            return {"error": f"Jira API error {r.status_code}", "details": r.text}
        return {"status": "ok", "comment_id": r.json().get("id")}
    except Exception as exc:
        return {"error": f"Jira request failed: {exc}"}


def _sync_update_fields(
    issue_key: str,
    priority: Optional[str] = None,
    assignee: Optional[str] = None,
    team: Optional[str] = None,
    labels: Optional[List[str]] = None,
    summary: Optional[str] = None,
) -> Dict[str, Any]:
    err = _check_config()
    if err:
        return {"error": err}

    fields: Dict[str, Any] = {}
    if summary:
        fields["summary"] = summary
    if priority:
        fields["priority"] = {"name": _sync_resolve_priority(priority)}
    if assignee:
        account_id = _sync_resolve_assignee(assignee)
        if not account_id:
            return {"error": f"Assignee '{assignee}' not found in Jira."}
        fields["assignee"] = {"id": account_id}

    combined: List[str] = list(labels or [])
    if team:
        combined.append(team)
    if combined:
        existing = _sync_get_labels(issue_key)
        seen: set[str] = set()
        merged: List[str] = []
        for lbl in existing + [_normalize_label(v) for v in combined]:
            n = _normalize_label(lbl)
            if n and n not in seen:
                merged.append(n)
                seen.add(n)
        fields["labels"] = merged

    if not fields:
        return {"error": "No update fields provided."}
    try:
        r = httpx.put(f"{_base()}/rest/api/3/issue/{issue_key}",
                      json={"fields": fields}, auth=_auth(), timeout=15)
        if not r.is_success:
            return {"error": f"Jira API error {r.status_code}", "details": r.text}
        return {"status": "ok", "issue_key": issue_key, "updated_fields": list(fields.keys())}
    except Exception as exc:
        return {"error": f"Jira request failed: {exc}"}


# ---------------------------------------------------------------------------
# Async wrappers (run sync calls in thread executor)
# ---------------------------------------------------------------------------

_EXECUTOR_TIMEOUT = 30.0


async def _run(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, partial(fn, *args, **kwargs)),
            timeout=_EXECUTOR_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return {"error": f"Jira request timed out after {_EXECUTOR_TIMEOUT:.0f}s."}


async def create_ticket(ticket_type: str, summary: str, description: str,
                        team: str, priority: str) -> Dict[str, Any]:
    return await _run(_sync_create_ticket, ticket_type, summary, description, team, priority)

async def close_ticket(issue_key: str, transition_name: str = "Done") -> Dict[str, Any]:
    return await _run(_sync_close_ticket, issue_key, transition_name)

async def add_ticket_comment(issue_key: str, comment: str) -> Dict[str, Any]:
    return await _run(_sync_add_comment, issue_key, comment)

async def update_ticket_fields(issue_key: str, priority: Optional[str] = None,
                               assignee: Optional[str] = None, team: Optional[str] = None,
                               labels: Optional[List[str]] = None,
                               summary: Optional[str] = None) -> Dict[str, Any]:
    return await _run(_sync_update_fields, issue_key, priority, assignee, team, labels, summary)


# ---------------------------------------------------------------------------
# Tool registry (same format as device adapters)
# ---------------------------------------------------------------------------

TOOLS: Dict[str, Any] = {
    "create_ticket": create_ticket,
    "close_ticket": close_ticket,
    "add_ticket_comment": add_ticket_comment,
    "update_ticket_fields": update_ticket_fields,
}

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "jira_create_ticket",
            "description": "Create a Jira ticket for a network incident or task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_type": {"type": "string", "description": "Issue type, e.g. 'Task', 'Bug', 'Incident'."},
                    "summary": {"type": "string", "description": "One-line ticket title."},
                    "description": {"type": "string", "description": "Detailed ticket body. ALWAYS include: device name, issue summary, severity (Critical/Major/Minor), investigation findings from tool output, playbook evidence (state, last_error, flaps, etc.), and recommended next steps. Do not leave this as a one-liner."},
                    "team": {"type": "string", "description": "Team label to apply."},
                    "priority": {"type": "string", "description": "Priority: High, Medium, Low."},
                },
                "required": ["ticket_type", "summary", "description", "team", "priority"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "jira_close_ticket",
            "description": "Close or transition a Jira ticket to a new state.",
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Jira issue key, e.g. 'NOC-123'."},
                    "transition_name": {"type": "string", "description": "Transition name, default 'Done'."},
                },
                "required": ["issue_key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "jira_add_ticket_comment",
            "description": "Add a comment to an existing Jira ticket.",
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Jira issue key, e.g. 'NOC-123'."},
                    "comment": {"type": "string", "description": "Comment text to add."},
                },
                "required": ["issue_key", "comment"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "jira_update_ticket_fields",
            "description": "Update fields on an existing Jira ticket (priority, assignee, labels, summary).",
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Jira issue key."},
                    "priority": {"type": "string", "description": "New priority (optional)."},
                    "assignee": {"type": "string", "description": "Assignee name or email (optional)."},
                    "team": {"type": "string", "description": "Team label to add (optional)."},
                    "labels": {"type": "array", "items": {"type": "string"}, "description": "Labels to add (optional)."},
                    "summary": {"type": "string", "description": "New summary text (optional)."},
                },
                "required": ["issue_key"],
            },
        },
    },
]
