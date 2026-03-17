"""
Jira MCP Server

Exposes Jira ticket management tools over MCP/SSE.
Reads configuration from environment variables.

Start:  python -m mcp_servers.jira.server
Port:   MCP_JIRA_PORT (default 8104)
"""

import logging
import os
import re
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

logger = logging.getLogger("netcopilot.mcp.jira")

MCP_PORT = int(os.environ.get("MCP_JIRA_PORT", 8104))

JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")
JIRA_PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY", "")
JIRA_DEFAULT_ISSUE_TYPE = os.environ.get("JIRA_DEFAULT_ISSUE_TYPE", "Task")
JIRA_DEFAULT_PRIORITY = os.environ.get("JIRA_DEFAULT_PRIORITY", "Medium")

mcp = FastMCP("jira", port=MCP_PORT)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _auth() -> tuple[str, str]:
    return (JIRA_EMAIL, JIRA_API_TOKEN)


def _base() -> str:
    return JIRA_BASE_URL.rstrip("/")


def _check_config() -> Optional[str]:
    missing = [
        v for v, val in [
            ("JIRA_BASE_URL", JIRA_BASE_URL),
            ("JIRA_EMAIL", JIRA_EMAIL),
            ("JIRA_API_TOKEN", JIRA_API_TOKEN),
            ("JIRA_PROJECT_KEY", JIRA_PROJECT_KEY),
        ] if not val
    ]
    return f"Missing Jira config: {', '.join(missing)}" if missing else None


def _resolve_issue_type(ticket_type: str) -> str:
    try:
        project_resp = httpx.get(
            f"{_base()}/rest/api/3/project/{JIRA_PROJECT_KEY}", auth=_auth(), timeout=10
        )
        project_id = project_resp.json().get("id") if project_resp.is_success else None
        if project_id:
            types_resp = httpx.get(
                f"{_base()}/rest/api/3/issuetype/project",
                params={"projectId": project_id},
                auth=_auth(),
                timeout=10,
            )
            if types_resp.is_success:
                types = types_resp.json()
                target = ticket_type.strip().lower()
                for t in types:
                    if str(t.get("name", "")).strip().lower() == target:
                        return t["name"]
                if types:
                    return types[0]["name"]
    except Exception:
        pass
    return JIRA_DEFAULT_ISSUE_TYPE


def _resolve_priority(priority: str) -> str:
    try:
        resp = httpx.get(f"{_base()}/rest/api/3/priority", auth=_auth(), timeout=10)
        if resp.is_success:
            target = priority.strip().lower()
            for p in resp.json():
                if str(p.get("name", "")).strip().lower() == target:
                    return p["name"]
    except Exception:
        pass
    return priority or JIRA_DEFAULT_PRIORITY


def _resolve_transition_id(issue_key: str, transition_name: str) -> str:
    try:
        resp = httpx.get(
            f"{_base()}/rest/api/3/issue/{issue_key}/transitions",
            auth=_auth(),
            timeout=10,
        )
        if resp.is_success:
            target = transition_name.strip().lower()
            for t in resp.json().get("transitions", []):
                if str(t.get("name", "")).strip().lower() == target:
                    return str(t["id"])
    except Exception:
        pass
    return ""


def _resolve_assignee(query: str) -> str:
    if not query:
        return ""
    try:
        resp = httpx.get(
            f"{_base()}/rest/api/3/user/search",
            params={"query": query},
            auth=_auth(),
            timeout=10,
        )
        if resp.is_success:
            users = resp.json()
            q = query.strip().lower()
            for u in users:
                if q in str(u.get("emailAddress", "")).lower() or q == str(
                    u.get("displayName", "")
                ).lower():
                    return str(u.get("accountId", ""))
            if users:
                return str(users[0].get("accountId", ""))
    except Exception:
        pass
    return ""


def _normalize_label(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", value.strip().lower())
    return re.sub(r"_{2,}", "_", cleaned).strip("_") or "team"


def _get_issue_labels(issue_key: str) -> List[str]:
    try:
        resp = httpx.get(
            f"{_base()}/rest/api/3/issue/{issue_key}",
            params={"fields": "labels"},
            auth=_auth(),
            timeout=10,
        )
        if resp.is_success:
            return [str(l) for l in resp.json().get("fields", {}).get("labels", [])]
    except Exception:
        pass
    return []


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def create_ticket(
    ticket_type: str,
    summary: str,
    description: str,
    team: str,
    priority: str,
) -> Dict[str, Any]:
    """
    Create a Jira ticket.

    Args:
        ticket_type: Issue type name, e.g. 'Task', 'Bug', 'Incident'.
        summary: One-line ticket title.
        description: Detailed ticket body.
        team: Team label to apply to the ticket.
        priority: Priority name, e.g. 'High', 'Medium', 'Low'.
    """
    err = _check_config()
    if err:
        return {"error": err}

    resolved_type = _resolve_issue_type(ticket_type)
    resolved_priority = _resolve_priority(priority)
    label = _normalize_label(team)

    payload = {
        "fields": {
            "project": {"key": JIRA_PROJECT_KEY},
            "summary": summary,
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": description}],
                    }
                ],
            },
            "issuetype": {"name": resolved_type},
            "priority": {"name": resolved_priority},
            "labels": [label],
        }
    }

    try:
        resp = httpx.post(
            f"{_base()}/rest/api/3/issue", json=payload, auth=_auth(), timeout=15
        )
        if not resp.is_success:
            return {"error": f"Jira API error {resp.status_code}", "details": resp.text}
        data = resp.json()
        key = data.get("key")
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


@mcp.tool()
def close_ticket(issue_key: str, transition_name: str = "Done") -> Dict[str, Any]:
    """
    Close or transition a Jira ticket.

    Args:
        issue_key: Jira issue key, e.g. 'NOC-123'.
        transition_name: Transition name to apply, default 'Done'.
    """
    err = _check_config()
    if err:
        return {"error": err}

    transition_id = _resolve_transition_id(issue_key, transition_name)
    if not transition_id:
        return {"error": f"Transition '{transition_name}' not found for {issue_key}."}

    try:
        resp = httpx.post(
            f"{_base()}/rest/api/3/issue/{issue_key}/transitions",
            json={"transition": {"id": transition_id}},
            auth=_auth(),
            timeout=15,
        )
        if not resp.is_success:
            return {"error": f"Jira API error {resp.status_code}", "details": resp.text}
        return {"status": "ok", "issue_key": issue_key, "transition": transition_name}
    except Exception as exc:
        return {"error": f"Jira request failed: {exc}"}


@mcp.tool()
def add_ticket_comment(issue_key: str, comment: str) -> Dict[str, Any]:
    """
    Add a comment to an existing Jira ticket.

    Args:
        issue_key: Jira issue key, e.g. 'NOC-123'.
        comment: Comment text to add.
    """
    err = _check_config()
    if err:
        return {"error": err}

    payload = {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": comment}]}
            ],
        }
    }
    try:
        resp = httpx.post(
            f"{_base()}/rest/api/3/issue/{issue_key}/comment",
            json=payload,
            auth=_auth(),
            timeout=15,
        )
        if not resp.is_success:
            return {"error": f"Jira API error {resp.status_code}", "details": resp.text}
        return {"status": "ok", "comment_id": resp.json().get("id")}
    except Exception as exc:
        return {"error": f"Jira request failed: {exc}"}


@mcp.tool()
def update_ticket_fields(
    issue_key: str,
    priority: Optional[str] = None,
    assignee: Optional[str] = None,
    team: Optional[str] = None,
    labels: Optional[List[str]] = None,
    summary: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Update fields on an existing Jira ticket.

    Args:
        issue_key: Jira issue key, e.g. 'NOC-123'.
        priority: New priority name (optional).
        assignee: Assignee name or email to search and assign (optional).
        team: Team label to add (optional).
        labels: List of labels to add (optional).
        summary: New one-line summary (optional).
    """
    err = _check_config()
    if err:
        return {"error": err}

    fields: Dict[str, Any] = {}

    if summary:
        fields["summary"] = summary
    if priority:
        fields["priority"] = {"name": _resolve_priority(priority)}
    if assignee:
        account_id = _resolve_assignee(assignee)
        if not account_id:
            return {"error": f"Assignee '{assignee}' not found in Jira."}
        fields["assignee"] = {"id": account_id}

    combined: List[str] = list(labels or [])
    if team:
        combined.append(team)
    if combined:
        existing = _get_issue_labels(issue_key)
        merged = []
        seen: set[str] = set()
        for lbl in existing + [_normalize_label(v) for v in combined]:
            n = _normalize_label(lbl)
            if n and n not in seen:
                merged.append(n)
                seen.add(n)
        fields["labels"] = merged

    if not fields:
        return {"error": "No update fields provided."}

    try:
        resp = httpx.put(
            f"{_base()}/rest/api/3/issue/{issue_key}",
            json={"fields": fields},
            auth=_auth(),
            timeout=15,
        )
        if not resp.is_success:
            return {"error": f"Jira API error {resp.status_code}", "details": resp.text}
        return {"status": "ok", "issue_key": issue_key, "updated_fields": list(fields.keys())}
    except Exception as exc:
        return {"error": f"Jira request failed: {exc}"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logger.info("Starting Jira MCP server on port %d", MCP_PORT)
    mcp.run(transport="sse")
