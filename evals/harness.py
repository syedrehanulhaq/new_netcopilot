"""
Replay evaluation harness for NetCopilot's answers.

Each case (evals/cases/*.yaml) is a recorded incident: a question, the device
inventory, and the tool outputs the devices would return. The harness runs the
real agent loop (real LLM, recorded tools — no devices touched) and grades the
reply: required tool calls, facts that must / must not appear, forbidden tools.

Use it before changing the model, the system prompt or tool descriptions:

    .venv/bin/python -m evals.run_evals            # uses the configured LLM
    .venv/bin/python -m evals.run_evals --case bgp-hold-timer --verbose
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import yaml

CASES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases")


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    reply: str
    tools_called: list[str]
    failures: list[str] = field(default_factory=list)
    seconds: float = 0.0
    usage: dict = field(default_factory=dict)


def load_cases(only: Optional[list[str]] = None) -> list[dict]:
    cases = []
    for name in sorted(os.listdir(CASES_DIR)):
        if not name.endswith((".yaml", ".yml")):
            continue
        with open(os.path.join(CASES_DIR, name), "r", encoding="utf-8") as f:
            case = yaml.safe_load(f)
        if not only or case["id"] in only:
            cases.append(case)
    return cases


class RecordedDispatcher:
    """
    Serves recorded tool outputs and offers the real tool schemas to the LLM.
    run_playbook and check_link run their real logic on top of the recordings,
    so the playbook engine and link analysis are evaluated too.
    """

    def __init__(self, real_dispatcher, responses: dict[str, Any]):
        self._real = real_dispatcher
        self._responses = responses
        self.calls: list[tuple[str, dict]] = []

    def get_all_tools(self, role: str) -> list[dict]:
        return self._real.get_all_tools(role)

    async def call_tool(self, name: str, args: dict) -> Any:
        self.calls.append((name, args))
        if name == "run_playbook":
            from playbooks.runner import get_runner

            params = {k: v for k, v in args.items() if k != "intent" and v is not None}
            result = await get_runner().run(args.get("intent", ""), params, self._unprefixed)
            return {"playbook": result.playbook_name, "severity": result.severity,
                    "evidence": result.evidence, "summary": result.summary_text}
        if name == "check_link":
            from adapters.devices.dispatcher import ToolDispatcher

            helper = ToolDispatcher()
            helper.call_tool_unprefixed = self._unprefixed
            return await helper._check_link(args)
        return self._recorded(name, args)

    async def _unprefixed(self, tool: str, args: dict) -> Any:
        from adapters.devices.registry import get_vendor

        vendor = get_vendor(str(args.get("device", ""))) if args.get("device") else None
        name = f"{vendor}_{tool}" if vendor else tool
        self.calls.append((name, args))
        if name not in self._responses:
            return {"error": f"Tool '{tool}' not found in '{vendor}' adapter", "unsupported": True}
        return self._recorded(name, args)

    def _recorded(self, name: str, args: dict) -> Any:
        recorded = self._responses.get(name)
        if isinstance(recorded, list):  # [{match: {...}, result: {...}}, ...]
            for option in recorded:
                match = option.get("match", {})
                if all(str(args.get(k)) == str(v) for k, v in match.items()):
                    return option["result"]
            recorded = None
        if recorded is None:
            recorded = self._responses.get("default", {"error": f"No recorded output for {name}"})
        return recorded


def grade(case: dict, reply: str, calls: list[tuple[str, dict]]) -> list[str]:
    expect = case.get("expect", {})
    called = [name for name, _ in calls]
    failures = []
    for tool in expect.get("must_call", []):
        if not any(re.fullmatch(tool, name) for name in called):
            failures.append(f"did not call {tool}")
    for tool in expect.get("forbidden_tools", []):
        if any(re.fullmatch(tool, name) for name in called):
            failures.append(f"called forbidden tool {tool}")
    for pattern in expect.get("must_mention", []):
        if not re.search(pattern, reply):
            failures.append(f"reply missing /{pattern}/")
    for pattern in expect.get("must_not_mention", []):
        if re.search(pattern, reply):
            failures.append(f"reply contains forbidden /{pattern}/")
    for rule in expect.get("forbidden_calls", []):
        for name, args in calls:
            if re.fullmatch(rule["tool"], name) and re.search(rule.get("args_regex", ".*"), json.dumps(args)):
                failures.append(f"forbidden call {name} {json.dumps(args)}")
    return failures


async def run_case(case: dict, app_module) -> CaseResult:
    """Run one case through app._agentic_loop with recorded tools."""
    from adapters.devices import registry

    registry.set_external_devices({
        name: {"host": "192.0.2.1", "port": None, "vendor": vendor, "platform": vendor,
               "site": "", "netconf_port": None, "source": "eval"}
        for name, vendor in case.get("devices", {}).items()
    })
    registry.reload_registry()

    real = app_module._dispatcher or app_module.ToolDispatcher()
    recorded = RecordedDispatcher(real, case.get("tool_responses", {}))
    previous = app_module._dispatcher
    app_module._dispatcher = recorded
    started = time.time()
    reply, usage = "", {}
    try:
        role = case.get("role", "admin")
        user = f"eval:{case['id']}"
        app_module._clear_memory(user)  # every case starts without conversation history
        messages, tools = app_module._build_chat_messages(case["question"], user, role)
        model = app_module._llm_model(app_module._get_llm_settings())
        async for event in app_module._agentic_loop(f"eval-{case['id']}", messages, tools, model,
                                                    f"eval:{case['id']}", role):
            if event["type"] == "reply":
                reply = event["content"]
            elif event["type"] == "usage":
                usage = {k: v for k, v in event.items() if k != "type"}
            elif event["type"] == "error":
                reply = f"[ERROR] {event['text']}"
    finally:
        app_module._dispatcher = previous

    failures = grade(case, reply, recorded.calls)
    return CaseResult(
        case_id=case["id"],
        passed=not failures,
        reply=reply,
        tools_called=[name for name, _ in recorded.calls],
        failures=failures,
        seconds=round(time.time() - started, 1),
        usage=usage,
    )


def summarize(results: list[CaseResult]) -> dict:
    passed = sum(r.passed for r in results)
    return {
        "passed": passed,
        "total": len(results),
        "score": round(passed / len(results), 3) if results else 0.0,
        "prompt_tokens": sum(r.usage.get("prompt_tokens", 0) for r in results),
        "completion_tokens": sum(r.usage.get("completion_tokens", 0) for r in results),
        "cases": [
            {"id": r.case_id, "passed": r.passed, "failures": r.failures,
             "tools": r.tools_called, "seconds": r.seconds}
            for r in results
        ],
    }


def to_json(results: list[CaseResult]) -> str:
    return json.dumps(summarize(results), indent=2)
