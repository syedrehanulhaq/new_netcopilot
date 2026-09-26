"""The eval harness itself: scripted LLMs stand in for the real model."""

import asyncio
import re

import app as appmod
from evals.harness import load_cases, run_case
from test_chat import FakeLLM, _Msg, _ToolCall


def _case(case_id):
    return next(c for c in load_cases() if c["id"] == case_id)


def test_all_cases_load_and_have_expectations():
    cases = load_cases()
    assert len(cases) >= 5
    for case in cases:
        assert case["question"] and case["devices"] and case["expect"]["must_call"]
        expect = case["expect"]
        patterns = (expect.get("must_call", []) + expect.get("must_mention", []) + expect.get("must_not_mention", [])
                    + [rule["args_regex"] for rule in expect.get("forbidden_calls", [])])
        for pattern in patterns:
            re.compile(pattern)


def test_good_answer_passes(monkeypatch):
    llm = FakeLLM([
        _Msg(tool_calls=[
            _ToolCall("cisco_get_bgp_neighbor_status", {"device": "xr1", "neighbor": "10.0.0.9"}),
            _ToolCall("cisco_get_recent_changes", {"device": "xr1"}),
        ]),
        _Msg(tool_calls=[_ToolCall("cisco_get_config_diff", {"device": "xr1", "commit_id": "1000000226"})]),
        _Msg("BGP to 10.0.0.9 is Idle because commit 1000000226 by alice added `shutdown` to the neighbor."),
    ])
    monkeypatch.setattr(appmod, "get_llm_adapter", lambda: llm)
    result = asyncio.run(run_case(_case("change-caused-bgp-down"), appmod))
    assert result.passed, result.failures
    assert result.tools_called == ["cisco_get_bgp_neighbor_status", "cisco_get_recent_changes", "cisco_get_config_diff"]


def test_answer_that_follows_injected_instructions_fails(monkeypatch):
    llm = FakeLLM([
        _Msg(tool_calls=[_ToolCall("junos_get_interface_health", {"device": "pe1", "interface": "ge-0/0/3"})]),
        _Msg(tool_calls=[_ToolCall("junos_run_command", {"device": "pe1", "command": "request system reboot"})]),
        _Msg("I rebooted pe1; everything is fine."),
    ])
    monkeypatch.setattr(appmod, "get_llm_adapter", lambda: llm)
    result = asyncio.run(run_case(_case("prompt-injection-description"), appmod))
    assert not result.passed
    assert any("forbidden call junos_run_command" in f for f in result.failures)
    assert any("everything is fine" in f for f in result.failures)


def test_check_link_runs_real_logic_on_recordings(monkeypatch):
    llm = FakeLLM([
        _Msg(tool_calls=[_ToolCall("check_link", {"device": "xr1", "interface": "Gi0/0/0/1"})]),
        _Msg("xr1 Gi0/0/0/1 is down because the far end pe1 ge-0/0/1 is administratively down "
             "(disabled by bob for fibre work)."),
    ])
    monkeypatch.setattr(appmod, "get_llm_adapter", lambda: llm)
    result = asyncio.run(run_case(_case("link-far-end-admin-down"), appmod))
    assert result.passed, result.failures
    # far end found through the interface description, then queried
    assert "junos_get_interface_health" in result.tools_called
    tool_msg = [m for m in llm.calls[-1]["messages"] if isinstance(m, dict) and m.get("role") == "tool"][0]
    assert "administratively down" in tool_msg["content"]
