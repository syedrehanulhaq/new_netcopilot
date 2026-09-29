"""Jira ticket creation against project quirks (no network: httpx is faked)."""

import copy

import httpx
import pytest

from adapters.integrations import jira

# Jira Cloud's reply when a team-managed project has no Priority field
PRIORITY_REJECTED = {"errorMessages": [], "errors": {
    "priority": "Field 'priority' cannot be set. It is not on the appropriate screen, or unknown."}}


@pytest.fixture
def jira_env(monkeypatch):
    for k, v in {"JIRA_BASE_URL": "https://example.atlassian.net", "JIRA_EMAIL": "noc@example.com",
                 "JIRA_API_TOKEN": "t", "JIRA_PROJECT_KEY": "KAN"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(jira.httpx, "get", lambda *a, **k: httpx.Response(404, json={}))
    posts = []

    def fake_post(url, json=None, **kwargs):
        posts.append(copy.deepcopy(json))  # the adapter edits its payload between attempts
        return replies.pop(0)

    replies = []
    monkeypatch.setattr(jira.httpx, "post", fake_post)
    return posts, replies


def test_ticket_is_created_without_priority_when_the_project_rejects_it(jira_env):
    posts, replies = jira_env
    replies += [httpx.Response(400, json=PRIORITY_REJECTED), httpx.Response(201, json={"key": "KAN-1"})]
    result = jira._sync_create_ticket("Task", "BGP down on pe1", "details", "NOC", "High")

    assert result["ticket_id"] == "KAN-1" and result["ticket_url"] == "https://example.atlassian.net/browse/KAN-1"
    assert "priority" in posts[0]["fields"] and "priority" not in posts[1]["fields"]
    assert posts[1]["fields"]["labels"] == posts[0]["fields"]["labels"]  # only the rejected field is dropped
    assert result["priority"] is None and "does not accept priority" in result["note"]


def test_other_jira_errors_are_not_retried(jira_env):
    posts, replies = jira_env
    replies += [httpx.Response(400, json={"errors": {"summary": "Summary must be less than 255 characters."}})]
    result = jira._sync_create_ticket("Task", "x" * 300, "details", "NOC", "High")
    assert len(posts) == 1 and "Jira API error 400" in result["error"]


def test_description_keeps_paragraphs_and_line_breaks():
    doc = jira._doc_text("Alert: link down\nDevice: pe1\n\nFindings:\n- far end admin down")
    assert [p["type"] for p in doc["content"]] == ["paragraph", "paragraph"]
    assert doc["content"][0]["content"] == [{"type": "text", "text": "Alert: link down"}, {"type": "hardBreak"},
                                            {"type": "text", "text": "Device: pe1"}]
    assert doc["content"][1]["content"][-1] == {"type": "text", "text": "- far end admin down"}
