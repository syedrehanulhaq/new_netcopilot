"""
Playbook Runner — deterministic, structured investigation engine.

Loads playbooks from playbooks/playbooks.yaml (seeded into playbooks.db via
store.py).  Given an intent string and a dict of params, runs the playbook's
tool steps in sequence, evaluates severity rules, and returns a PlaybookResult.

Usage:
    from playbooks.runner import get_runner

    runner = get_runner()
    result = await runner.run(
        intent="bgp_down",
        params={"device": "pe1", "neighbor": "10.0.0.1"},
        tool_caller=mcp_call,   # async def call(name: str, args: dict) -> dict
    )
    print(result.severity)      # "Critical"
    print(result.summary_text)  # "BGP neighbor 10.0.0.1 is Idle. Last error: ..."
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import yaml

logger = logging.getLogger("netcopilot.playbook_runner")

_PLAYBOOKS_YAML = os.path.join(os.path.dirname(__file__), "playbooks.yaml")

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    tool_name: str
    args: dict
    result: dict
    elapsed_ms: float
    error: Optional[str] = None


@dataclass
class PlaybookResult:
    intent: str
    playbook_name: str
    steps: list[StepResult]
    severity: str          # "Critical", "Warning", "Info", "Unknown"
    evidence: dict         # field_name -> value extracted from step results
    summary_text: str
    elapsed_ms: float
    error: Optional[str] = None   # set if no matching playbook found


# ---------------------------------------------------------------------------
# Severity rule evaluation
# ---------------------------------------------------------------------------

_OP_TABLE = {
    "=": lambda a, b: str(a).lower() == b.lower(),
    ">": lambda a, b: _to_num(a) > _to_num(b),
    "<": lambda a, b: _to_num(a) < _to_num(b),
    ">=": lambda a, b: _to_num(a) >= _to_num(b),
    "<=": lambda a, b: _to_num(a) <= _to_num(b),
    "!=": lambda a, b: str(a).lower() != b.lower(),
}


def _to_num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _parse_rule(rule: str) -> tuple[str, str, str]:
    """
    Parse "state=Idle" -> ("state", "=", "Idle")
    Parse "cpu_percent>80" -> ("cpu_percent", ">", "80")
    """
    for op in (">=", "<=", "!=", ">", "<", "="):
        if op in rule:
            field, val = rule.split(op, 1)
            return field.strip(), op, val.strip()
    raise ValueError(f"Cannot parse severity rule: {rule!r}")


def _collect_all_values(steps: list[StepResult]) -> dict[str, list[Any]]:
    """
    Walk every step result dict (recursively for lists of dicts) and
    collect all values grouped by field name.
    """
    collected: dict[str, list[Any]] = {}

    def _walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                collected.setdefault(k, [])
                if v is not None:
                    collected[k].append(v)
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    for step in steps:
        _walk(step.result)

    return collected


def _evaluate_severity(
    severity_rules: dict[str, list[str]],
    steps: list[StepResult],
) -> str:
    """
    Evaluate severity_rules dict against collected step results.

    severity_rules:
        critical:
          - state=Idle
          - state=Active
        warning:
          - carrier_transitions>3

    A level is triggered if ANY of its rules matches ANY value in the
    collected result fields.  Critical takes precedence over Warning.
    """
    if not severity_rules:
        return "Info"

    all_values = _collect_all_values(steps)
    level_order = ["critical", "warning", "info"]

    for level in level_order:
        rules = severity_rules.get(level, [])
        for rule in rules:
            try:
                field_name, op, threshold = _parse_rule(rule)
            except ValueError as exc:
                logger.warning("Bad severity rule %r: %s", rule, exc)
                continue
            op_fn = _OP_TABLE.get(op)
            if op_fn is None:
                continue
            for val in all_values.get(field_name, []):
                try:
                    if op_fn(val, threshold):
                        return level.capitalize()
                except Exception:
                    continue

    return "Info"


# ---------------------------------------------------------------------------
# Evidence extraction
# ---------------------------------------------------------------------------

def _extract_evidence(
    evidence_fields: list[str],
    steps: list[StepResult],
) -> dict:
    """
    Walk all step results and pull out the first non-null value for each
    field listed in evidence_fields.
    """
    evidence: dict[str, Any] = {}

    def _walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in evidence_fields and k not in evidence and v is not None:
                    evidence[k] = v
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    for step in steps:
        _walk(step.result)
        if len(evidence) == len(evidence_fields):
            break  # all fields found

    return evidence


# ---------------------------------------------------------------------------
# Summary builder
# ---------------------------------------------------------------------------

def _build_summary(
    playbook: dict,
    params: dict,
    severity: str,
    evidence: dict,
    steps: list[StepResult],
) -> str:
    """
    Build a human-readable one-paragraph summary of the investigation.
    Combines playbook name, params, severity, and extracted evidence values.
    """
    parts: list[str] = []

    name = playbook.get("name", "Investigation")
    device = params.get("device", "")
    parts.append(f"[{severity}] {name}")
    if device:
        parts.append(f"on {device}")
    parts.append("--")

    # Emit each evidence field that has a value
    for field_name, val in evidence.items():
        # Pretty-print lists of dicts compactly
        if isinstance(val, list):
            if val and isinstance(val[0], dict):
                # e.g. routes: show count
                parts.append(f"{field_name}: {len(val)} item(s)")
            else:
                parts.append(f"{field_name}: {val}")
        else:
            parts.append(f"{field_name}: {val}")

    # If no evidence was extracted, fall back to step summaries
    if not evidence:
        for step in steps:
            summary = step.result.get("summary") or step.result.get("raw_output", "")
            if summary and isinstance(summary, str):
                parts.append(summary)
                break

    # Append errors from steps
    errors = [s.error for s in steps if s.error]
    if errors:
        parts.append(f"Errors: {'; '.join(errors[:3])}")

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Tool argument builder
# ---------------------------------------------------------------------------

_TOOL_ARG_MAPS: dict[str, list[str]] = {
    # Tools that need (device, interface)
    "get_interface_health": ["device", "interface"],
    "get_interface_counters": ["device", "interface"],
    "get_lldp_neighbor": ["device", "interface"],
    "get_mtu_info": ["device", "interface"],
    "get_isis_interface": ["device", "interface"],
    "get_optics": ["device", "interface"],
    # Tools that need (device, neighbor)
    "get_bgp_neighbor_status": ["device", "neighbor"],
    # Tools that need (device, prefix)
    "get_route_lookup": ["device", "prefix"],
    "get_bgp_route": ["device", "prefix"],
    # Tools that need (device, destination)
    "get_ping": ["device", "destination"],
    "get_traceroute": ["device", "destination"],
    # Tools that need (device, acl_name)
    "get_acl_hits": ["device", "acl_name"],
    # Device-only tools
    "get_interface_status": ["device"],
    "get_bgp_summary": ["device"],
    "get_ospf_neighbors": ["device"],
    "get_isis_neighbors": ["device"],
    "get_cpu_memory": ["device"],
    "get_chassis_health": ["device"],
    "get_top_flapping_links": ["device"],
    "get_link_health": ["device"],
    "get_isis_health": ["device"],
}


def _build_args(tool_name: str, params: dict) -> dict:
    """
    Build the args dict for a tool call from the playbook params.
    Falls back to passing all available params if tool is unknown.
    """
    allowed = _TOOL_ARG_MAPS.get(tool_name)
    if allowed is None:
        # Unknown tool — pass all params and let the tool sort it out
        return dict(params)
    return {k: params[k] for k in allowed if k in params}


# ---------------------------------------------------------------------------
# Playbook loader
# ---------------------------------------------------------------------------

def _load_playbooks_yaml() -> list[dict]:
    """Load raw playbook definitions from YAML file."""
    path = _PLAYBOOKS_YAML
    if not os.path.exists(path):
        logger.warning("playbooks.yaml not found at %s", path)
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or []
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.error("Failed to load playbooks.yaml: %s", exc)
        return []


def _find_playbook(intent: str, playbooks: list[dict]) -> Optional[dict]:
    """
    Match intent to a playbook.
    Strategy:
      1. Exact match on the 'intent' field (case-insensitive)
      2. Substring match on 'intent' or 'name'
    """
    intent_lower = intent.strip().lower()

    # Exact match
    for pb in playbooks:
        if pb.get("intent", "").lower() == intent_lower:
            return pb

    # Substring / fuzzy match
    for pb in playbooks:
        pb_intent = pb.get("intent", "").lower()
        pb_name = pb.get("name", "").lower()
        if intent_lower in pb_intent or intent_lower in pb_name:
            return pb
        # Also try if the playbook intent is a substring of the user intent
        if pb_intent and pb_intent in intent_lower:
            return pb

    return None


# ---------------------------------------------------------------------------
# PlaybookRunner
# ---------------------------------------------------------------------------

class PlaybookRunner:
    """
    Deterministic playbook execution engine.

    Instantiate once and reuse (thread-safe for reads; playbooks reloaded
    each call to pick up runtime edits).
    """

    def __init__(self, playbooks_path: Optional[str] = None) -> None:
        self._path = playbooks_path or _PLAYBOOKS_YAML

    def _get_playbooks(self) -> list[dict]:
        """Load playbooks, preferring live DB if available, else YAML."""
        try:
            from playbooks.store import list_playbooks
            pbs = list_playbooks()
            if pbs:
                return pbs
        except Exception as exc:
            logger.debug("store.list_playbooks failed, falling back to YAML: %s", exc)
        return _load_playbooks_yaml()

    async def run(
        self,
        intent: str,
        params: dict,
        tool_caller: Callable,
    ) -> PlaybookResult:
        """
        Execute a playbook matched by intent.

        Args:
            intent:      Intent string, e.g. "bgp_down", "link_flap"
            params:      Runtime parameters, e.g. {"device": "pe1", "neighbor": "10.0.0.1"}
            tool_caller: Async callable: async def call(name: str, args: dict) -> dict

        Returns:
            PlaybookResult with steps, severity, evidence, and summary_text.
        """
        run_start = time.monotonic()
        playbooks = self._get_playbooks()

        playbook = _find_playbook(intent, playbooks)
        if playbook is None:
            elapsed = (time.monotonic() - run_start) * 1000
            return PlaybookResult(
                intent=intent,
                playbook_name="(none)",
                steps=[],
                severity="Unknown",
                evidence={},
                summary_text=f"No playbook found for intent: {intent!r}",
                elapsed_ms=elapsed,
                error=f"No playbook matched intent {intent!r}",
            )

        pb_name = playbook.get("name", intent)
        tool_names: list[str] = playbook.get("tools", [])
        evidence_fields: list[str] = playbook.get("evidence_fields", [])
        severity_rules: dict = playbook.get("severity_rules", {})

        logger.info(
            "Running playbook %r for intent %r with params %s",
            pb_name, intent, list(params.keys()),
        )

        steps: list[StepResult] = []

        for tool_name in tool_names:
            args = _build_args(tool_name, params)
            step_start = time.monotonic()
            step_error: Optional[str] = None
            result: dict = {}

            try:
                result = await tool_caller(tool_name, args)
                if not isinstance(result, dict):
                    result = {"raw_output": str(result)}
                step_error = result.get("error")
            except Exception as exc:
                step_error = str(exc)
                result = {"error": step_error, "tool": tool_name}
                logger.warning("Step %r failed: %s", tool_name, exc)

            elapsed_ms = (time.monotonic() - step_start) * 1000
            steps.append(StepResult(
                tool_name=tool_name,
                args=args,
                result=result,
                elapsed_ms=elapsed_ms,
                error=step_error,
            ))

        # Severity evaluation
        severity = _evaluate_severity(severity_rules, steps)

        # Evidence extraction
        evidence = _extract_evidence(evidence_fields, steps)

        # Summary construction
        summary_text = _build_summary(playbook, params, severity, evidence, steps)

        total_elapsed = (time.monotonic() - run_start) * 1000

        logger.info(
            "Playbook %r done: severity=%s steps=%d elapsed=%.0fms",
            pb_name, severity, len(steps), total_elapsed,
        )

        return PlaybookResult(
            intent=intent,
            playbook_name=pb_name,
            steps=steps,
            severity=severity,
            evidence=evidence,
            summary_text=summary_text,
            elapsed_ms=total_elapsed,
        )


# ---------------------------------------------------------------------------
# Singleton factory
# ---------------------------------------------------------------------------

_runner: Optional[PlaybookRunner] = None


def get_runner(playbooks_path: Optional[str] = None) -> PlaybookRunner:
    """
    Return the shared PlaybookRunner singleton.

    Example:
        runner = get_runner()
        result = await runner.run("bgp_down", {"device": "pe1", "neighbor": "10.0.0.1"}, mcp_call)
    """
    global _runner
    if _runner is None:
        _runner = PlaybookRunner(playbooks_path=playbooks_path)
    return _runner
