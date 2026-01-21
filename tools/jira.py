import re
from typing import Any, Dict

import httpx

from config import (
    JIRA_API_TOKEN,
    JIRA_BASE_URL,
    JIRA_DEFAULT_ISSUE_TYPE,
    JIRA_EMAIL,
    JIRA_PROJECT_KEY,
)


def _missing_jira_config() -> str:
    missing = []
    if not JIRA_BASE_URL:
        missing.append("JIRA_BASE_URL")
    if not JIRA_EMAIL:
        missing.append("JIRA_EMAIL")
    if not JIRA_API_TOKEN:
        missing.append("JIRA_API_TOKEN")
    if not JIRA_PROJECT_KEY:
        missing.append("JIRA_PROJECT_KEY")
    return ", ".join(missing)


def _resolve_issue_type(requested_type: str) -> str:
    url = f"{JIRA_BASE_URL.rstrip('/')}/rest/api/3/issuetype"
    auth = (JIRA_EMAIL, JIRA_API_TOKEN)
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(url, auth=auth)
        if resp.status_code >= 300:
            return JIRA_DEFAULT_ISSUE_TYPE
        types = resp.json()
    except Exception:
        return JIRA_DEFAULT_ISSUE_TYPE

    requested_lower = requested_type.strip().lower()
    for item in types:
        name = str(item.get("name", "")).strip()
        if name.lower() == requested_lower:
            return name
    return JIRA_DEFAULT_ISSUE_TYPE


def _resolve_issue_type_for_project(requested_type: str) -> str:
    auth = (JIRA_EMAIL, JIRA_API_TOKEN)
    base = JIRA_BASE_URL.rstrip("/")
    try:
        with httpx.Client(timeout=15) as client:
            project_resp = client.get(f"{base}/rest/api/3/project/{JIRA_PROJECT_KEY}", auth=auth)
        if project_resp.status_code >= 300:
            return _resolve_issue_type(requested_type)
        project_id = project_resp.json().get("id")
        if not project_id:
            return _resolve_issue_type(requested_type)
        with httpx.Client(timeout=15) as client:
            types_resp = client.get(
                f"{base}/rest/api/3/issuetype/project",
                params={"projectId": project_id},
                auth=auth,
            )
        if types_resp.status_code >= 300:
            return _resolve_issue_type(requested_type)
        types = types_resp.json()
    except Exception:
        return _resolve_issue_type(requested_type)

    requested_lower = requested_type.strip().lower()
    for item in types:
        name = str(item.get("name", "")).strip()
        if name.lower() == requested_lower:
            return name

    if types:
        return str(types[0].get("name", JIRA_DEFAULT_ISSUE_TYPE))
    return JIRA_DEFAULT_ISSUE_TYPE


def _resolve_priority(requested_priority: str) -> str:
    url = f"{JIRA_BASE_URL.rstrip('/')}/rest/api/3/priority"
    auth = (JIRA_EMAIL, JIRA_API_TOKEN)
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(url, auth=auth)
        if resp.status_code >= 300:
            return requested_priority
        priorities = resp.json()
    except Exception:
        return requested_priority

    requested_lower = requested_priority.strip().lower()
    for item in priorities:
        name = str(item.get("name", "")).strip()
        if name.lower() == requested_lower:
            return name

    if priorities:
        return str(priorities[0].get("name", requested_priority))
    return requested_priority


def _normalize_label(value: str) -> str:
    cleaned = value.strip().lower()
    cleaned = re.sub(r"[^a-z0-9]+", "_", cleaned)
    cleaned = re.sub(r"_{2,}", "_", cleaned).strip("_")
    return cleaned or "team"


def create_ticket(
    ticket_type: str,
    summary: str,
    description: str,
    team: str,
    priority: str,
) -> Dict[str, Any]:
    """
    Create a Jira ticket via REST API.
    """
    missing = _missing_jira_config()
    if missing:
        return {
            "error": f"Missing Jira configuration: {missing}",
            "notes": "Set env vars to enable Jira integration.",
        }

    resolved_type = _resolve_issue_type_for_project(ticket_type)

    resolved_priority = _resolve_priority(priority)

    label_value = _normalize_label(team)

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
            "labels": [label_value],
        }
    }

    url = f"{JIRA_BASE_URL.rstrip('/')}/rest/api/3/issue"
    auth = (JIRA_EMAIL, JIRA_API_TOKEN)

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(url, json=payload, auth=auth)
        if resp.status_code >= 300:
            return {
                "error": f"Jira API error {resp.status_code}",
                "details": resp.text,
            }
        data = resp.json()
        return {
            "ticket_type": resolved_type,
            "summary": summary,
            "description": description,
            "team": team,
            "priority": resolved_priority,
            "ticket_id": data.get("key"),
            "ticket_url": f"{JIRA_BASE_URL.rstrip('/')}/browse/{data.get('key')}",
            "status": "CREATED",
            "issue_type_requested": ticket_type,
            "priority_requested": priority,
            "label_used": label_value,
        }
    except Exception as exc:
        return {
            "error": f"Jira request failed: {exc}",
        }


def close_ticket(issue_key: str, transition_name: str = "Done") -> Dict[str, Any]:
    missing = _missing_jira_config()
    if missing:
        return {
            "error": f"Missing Jira configuration: {missing}",
            "notes": "Set env vars to enable Jira integration.",
        }

    base = JIRA_BASE_URL.rstrip("/")
    auth = (JIRA_EMAIL, JIRA_API_TOKEN)
    transitions_url = f"{base}/rest/api/3/issue/{issue_key}/transitions"
    try:
        with httpx.Client(timeout=15) as client:
            transitions_resp = client.get(transitions_url, auth=auth)
        if transitions_resp.status_code >= 300:
            return {
                "error": f"Jira API error {transitions_resp.status_code}",
                "details": transitions_resp.text,
            }
        transitions = transitions_resp.json().get("transitions", [])
        target = None
        for item in transitions:
            name = str(item.get("name", "")).strip()
            if name.lower() == transition_name.lower():
                target = item
                break
        if not target and transitions:
            target = transitions[-1]
        if not target:
            return {"error": "No transitions available for this issue."}

        payload = {"transition": {"id": target.get("id")}}
        with httpx.Client(timeout=15) as client:
            resp = client.post(transitions_url, json=payload, auth=auth)
        if resp.status_code >= 300:
            return {
                "error": f"Jira API error {resp.status_code}",
                "details": resp.text,
            }

        return {
            "issue_key": issue_key,
            "status": "CLOSED",
            "transition_used": target.get("name"),
        }
    except Exception as exc:
        return {"error": f"Jira request failed: {exc}"}


def add_ticket_comment(issue_key: str, comment: str) -> Dict[str, Any]:
    missing = _missing_jira_config()
    if missing:
        return {
            "error": f"Missing Jira configuration: {missing}",
            "notes": "Set env vars to enable Jira integration.",
        }

    base = JIRA_BASE_URL.rstrip("/")
    auth = (JIRA_EMAIL, JIRA_API_TOKEN)
    url = f"{base}/rest/api/3/issue/{issue_key}/comment"
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
        with httpx.Client(timeout=15) as client:
            resp = client.post(url, json=payload, auth=auth)
        if resp.status_code >= 300:
            return {
                "error": f"Jira API error {resp.status_code}",
                "details": resp.text,
            }
        data = resp.json()
        return {
            "issue_key": issue_key,
            "comment_id": data.get("id"),
            "status": "COMMENTED",
        }
    except Exception as exc:
        return {"error": f"Jira request failed: {exc}"}
