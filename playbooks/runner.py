"""
Playbook Runner — deterministic, structured investigation engine.

Loads playbooks from playbooks.db (seeded from playbooks/playbooks.yaml via
store.py; the YAML file is the fallback). Given an intent string and a dict of
params, runs the playbook's steps in sequence, evaluates severity rules, and
returns a PlaybookResult.

Playbooks come in two shapes and both are accepted:
  YAML  : {name, intent, tools: [...], evidence_fields, severity_rules}
  store : {title, intent, vendor, enabled, steps: [{type, ...}], checks: {evidence_fields, severity_rules}}

Step types (store / Admin UI):
  {"type": "tool", "tool": "get_bgp_summary", "vendor": "junos"}   vendor optional
  {"type": "custom_cli", "command": "show interfaces {{interface}}"}
  {"type": "prometheus_query", "query": "up{instance=\"{{device}}\"}"}
  {"type": "syslog_search", "query": "{{device}} {{interface}}", "limit": 50}

Rule syntax (severity_rules / evidence_fields):
  "state=Idle"                                      any result record where state == Idle
  "admin_status=up & oper_status=down"              both conditions on the SAME record
  "get_bgp_neighbor_status: state!=Established"     only records from that tool's step
  evidence "get_bgp_neighbor_status: state"         first value of state from that step

Usage:
    from playbooks.runner import get_runner

    runner = get_runner()
    result = await runner.run(
        intent="bgp_down",
        params={"device": "pe1", "neighbor": "10.0.0.1"},
        tool_caller=mcp_call,   # async def call(name: str, args: dict) -> dict
    )
    print(result.severity)      # "Critical"
    print(result.summary_text)  # "[Critical] BGP neighbor down on pe1 -- state: Idle ..."
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional

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
    skipped: Optional[str] = None   # reason the step did not run (missing input, unsupported)


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
# Playbook normalization (YAML and store formats)
# ---------------------------------------------------------------------------

# UI/store vendor names → device registry vendor prefixes
_VENDOR_NAMES = {
    "iosxr": "cisco", "ios-xr": "cisco", "cisco": "cisco",
    "junos": "junos", "juniper": "junos",
    "nokia": "nokia", "sros": "nokia",
    "aruba": "aruba", "aoscx": "aruba",
}


def _norm_vendor(value: Any) -> str:
    """Return a registry vendor prefix, or '' meaning 'any vendor'."""
    return _VENDOR_NAMES.get(str(value or "").strip().lower(), "")


def _normalize_playbook(pb: dict) -> dict:
    checks = pb.get("checks") or {}
    steps = pb.get("steps")
    if not steps:
        steps = [{"type": "tool", "tool": t} for t in (pb.get("tools") or [])]
    return {
        "name": pb.get("name") or pb.get("title") or pb.get("intent") or "Investigation",
        "intent": pb.get("intent") or "",
        "vendor": _norm_vendor(pb.get("vendor")),
        "enabled": pb.get("enabled", True) is not False,
        "steps": [s for s in steps if isinstance(s, dict)],
        "evidence_fields": pb.get("evidence_fields") or checks.get("evidence_fields") or [],
        "severity_rules": pb.get("severity_rules") or checks.get("severity_rules") or {},
    }


# ---------------------------------------------------------------------------
# Rule parsing and evaluation
# ---------------------------------------------------------------------------

def _to_num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _num_op(fn):
    def _cmp(a: Any, b: str) -> bool:
        x, y = _to_num(a), _to_num(b)
        return x is not None and y is not None and fn(x, y)
    return _cmp


_OP_TABLE = {
    "=": lambda a, b: str(a).lower() == b.lower(),
    "!=": lambda a, b: str(a).lower() != b.lower(),
    ">": _num_op(lambda x, y: x > y),
    "<": _num_op(lambda x, y: x < y),
    ">=": _num_op(lambda x, y: x >= y),
    "<=": _num_op(lambda x, y: x <= y),
}

_SCOPE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.+)$")


def _split_scope(text: str) -> tuple[Optional[str], str]:
    """'get_bgp_summary: state=Idle' -> ('get_bgp_summary', 'state=Idle')."""
    m = _SCOPE_RE.match(text)
    if m and not any(op in m.group(1) for op in _OP_TABLE):
        return m.group(1), m.group(2).strip()
    return None, text.strip()


def _parse_condition(cond: str) -> tuple[str, str, str]:
    """
    Parse "state=Idle" -> ("state", "=", "Idle")
    Parse "cpu_percent>80" -> ("cpu_percent", ">", "80")
    """
    for op in (">=", "<=", "!=", ">", "<", "="):
        if op in cond:
            field, val = cond.split(op, 1)
            return field.strip(), op, val.strip()
    raise ValueError(f"Cannot parse severity rule: {cond!r}")


def _parse_rule(rule: str) -> tuple[Optional[str], list[tuple[str, str, str]]]:
    scope, body = _split_scope(rule)
    return scope, [_parse_condition(c) for c in body.split("&") if c.strip()]


def _iter_records(steps: list[StepResult], scope: Optional[str] = None) -> Iterator[dict]:
    """Yield every dict found in step results (recursively), optionally for one tool only."""
    def _walk(obj: Any) -> Iterator[dict]:
        if isinstance(obj, dict):
            yield obj
            for v in obj.values():
                yield from _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                yield from _walk(item)

    for step in steps:
        if step.skipped or (scope and step.tool_name != scope):
            continue
        yield from _walk(step.result)


def _record_matches(record: dict, conditions: list[tuple[str, str, str]]) -> bool:
    for field, op, threshold in conditions:
        if field not in record or record[field] is None:
            return False
        try:
            if not _OP_TABLE[op](record[field], threshold):
                return False
        except Exception:
            return False
    return True


def _evaluate_severity(severity_rules: dict[str, list[str]], steps: list[StepResult]) -> str:
    """
    A level is triggered when any of its rules matches a result record.
    Conditions joined with '&' must all hold on the same record.
    Critical takes precedence over Warning over Info.
    """
    if not severity_rules:
        return "Info"

    for level in ("critical", "warning", "info"):
        for rule in severity_rules.get(level, []) or []:
            try:
                scope, conditions = _parse_rule(str(rule))
            except ValueError as exc:
                logger.warning("Bad severity rule %r: %s", rule, exc)
                continue
            if any(_record_matches(r, conditions) for r in _iter_records(steps, scope)):
                return level.capitalize()

    return "Info"


# ---------------------------------------------------------------------------
# Evidence extraction
# ---------------------------------------------------------------------------

def _extract_evidence(evidence_fields: list[str], steps: list[StepResult]) -> dict:
    """First non-null value of each field, walking steps in playbook order."""
    evidence: dict[str, Any] = {}
    for spec in evidence_fields:
        scope, field = _split_scope(str(spec))
        if field in evidence:
            continue
        for record in _iter_records(steps, scope):
            value = record.get(field)
            if value is not None and value != "":
                evidence[field] = value
                break
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

    device = params.get("device", "")
    parts.append(f"[{severity}] {playbook['name']}")
    if device:
        parts.append(f"on {device}")
    parts.append("--")

    for field_name, val in evidence.items():
        if isinstance(val, list):
            if val and isinstance(val[0], dict):
                parts.append(f"{field_name}: {len(val)} item(s)")
            else:
                parts.append(f"{field_name}: {val}")
        else:
            parts.append(f"{field_name}: {val}")

    ran = [s for s in steps if not s.skipped]
    if not evidence:
        for step in ran:
            summary = step.result.get("summary") or step.result.get("raw_output", "")
            if summary and isinstance(summary, str):
                parts.append(summary[:500])
                break
        else:
            parts.append("no structured evidence extracted; review step outputs.")

    errors = [f"{s.tool_name}: {s.error}" for s in ran if s.error]
    if errors:
        parts.append(f"Errors: {'; '.join(errors[:3])}")
    skipped = [f"{s.tool_name} ({s.skipped})" for s in steps if s.skipped]
    if skipped:
        parts.append(f"Skipped: {', '.join(skipped)}")

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Step → tool call
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
    # Integration (Prometheus) — no device argument
    "get_top_flapping_links": [],
    # Change history and link meta-tool
    "get_recent_changes": ["device"],
    "get_config_diff": ["device"],
    "check_link": ["device", "interface"],
}

# When a playbook param is absent, these params may stand in for it
# (e.g. ping / route-lookup the BGP neighbor address).
_ARG_FALLBACKS: dict[str, tuple[str, ...]] = {
    "destination": ("neighbor",),
    "prefix": ("neighbor",),
}

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def _param(params: dict, key: str) -> Any:
    if params.get(key) not in (None, ""):
        return params[key]
    for alt in _ARG_FALLBACKS.get(key, ()):
        if params.get(alt) not in (None, ""):
            return params[alt]
    return None


def _build_args(tool_name: str, params: dict) -> tuple[dict, list[str]]:
    """Return (args, missing_required) for a tool step."""
    allowed = _TOOL_ARG_MAPS.get(tool_name)
    if allowed is None:
        # Unknown tool — pass all params and let the tool sort it out
        return dict(params), []
    args: dict = {}
    missing: list[str] = []
    for key in allowed:
        value = _param(params, key)
        if value is None:
            missing.append(key)
        else:
            args[key] = value
    return args, missing


def _render(template: str, params: dict) -> tuple[str, list[str]]:
    missing: list[str] = []

    def _sub(m: re.Match) -> str:
        value = _param(params, m.group(1))
        if value is None:
            missing.append(m.group(1))
            return m.group(0)
        return str(value)

    return _PLACEHOLDER_RE.sub(_sub, template or ""), missing


def plan_step(step: dict, params: dict, device_vendor: str) -> tuple[str, dict, Optional[str]]:
    """Return (tool_name, args, skip_reason) for one playbook step."""
    step_type = step.get("type") or "tool"
    step_vendor = _norm_vendor(step.get("vendor"))
    label = step.get("tool") or step_type

    if step_vendor and device_vendor and step_vendor != device_vendor:
        return label, {}, f"step is for {step_vendor} devices"

    if step_type == "tool":
        tool = step.get("tool") or ""
        if not tool:
            return label, {}, "no tool selected"
        args, missing = _build_args(tool, params)
        if missing:
            return tool, args, f"missing input: {', '.join(missing)}"
        return tool, args, None

    if step_type == "custom_cli":
        command, missing = _render(step.get("command", ""), params)
        if missing:
            return "run_command", {}, f"missing input: {', '.join(missing)}"
        if not params.get("device"):
            return "run_command", {}, "missing input: device"
        return "run_command", {"device": params["device"], "command": command}, None

    if step_type == "prometheus_query":
        query, missing = _render(step.get("query", ""), params)
        if missing:
            return "prometheus_query", {}, f"missing input: {', '.join(missing)}"
        return "prometheus_prometheus_query", {"query": query}, None

    if step_type == "syslog_search":
        query, missing = _render(step.get("query", ""), params)
        if missing:
            return "syslog_search", {}, f"missing input: {', '.join(missing)}"
        return "syslog_syslog_search", {"query": query, "limit": int(step.get("limit") or 50)}, None

    return label, {}, f"unknown step type '{step_type}'"


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


def _find_playbook(intent: str, playbooks: list[dict], vendor: str = "") -> Optional[dict]:
    """
    Match intent to an enabled, normalized playbook.
    Strategy:
      1. Exact match on 'intent' (a vendor-specific playbook beats a generic one)
      2. Substring match on 'intent' or 'name'
    """
    intent_lower = intent.strip().lower()
    if not intent_lower:
        return None
    candidates = [
        pb for pb in playbooks
        if pb["enabled"] and (not pb["vendor"] or not vendor or pb["vendor"] == vendor)
    ]
    candidates.sort(key=lambda pb: 0 if pb["vendor"] and pb["vendor"] == vendor else 1)

    for pb in candidates:
        if pb["intent"].lower() == intent_lower:
            return pb

    for pb in candidates:
        pb_intent = pb["intent"].lower()
        pb_name = pb["name"].lower()
        if intent_lower in pb_intent or intent_lower in pb_name:
            return pb
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
        raw: list[dict] = []
        try:
            from playbooks.store import list_playbooks
            raw = list_playbooks()
        except Exception as exc:
            logger.debug("store.list_playbooks failed, falling back to YAML: %s", exc)
        if not raw:
            raw = _load_playbooks_yaml()
        return [_normalize_playbook(pb) for pb in raw if isinstance(pb, dict)]

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
                         (unprefixed device tool names, or full integration tool names)

        Returns:
            PlaybookResult with steps, severity, evidence, and summary_text.
        """
        run_start = time.monotonic()

        device_vendor = ""
        if params.get("device"):
            try:
                from adapters.devices.registry import get_vendor
                device_vendor = get_vendor(str(params["device"])) or ""
            except Exception:
                device_vendor = ""

        playbook = _find_playbook(intent, self._get_playbooks(), device_vendor)
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

        logger.info(
            "Running playbook %r for intent %r with params %s",
            playbook["name"], intent, list(params.keys()),
        )

        steps: list[StepResult] = []
        for step in playbook["steps"]:
            tool_name, args, skip_reason = plan_step(step, params, device_vendor)
            if skip_reason:
                steps.append(StepResult(tool_name, args, {}, 0.0, skipped=skip_reason))
                continue

            step_start = time.monotonic()
            step_error: Optional[str] = None
            skipped: Optional[str] = None
            try:
                result = await tool_caller(tool_name, args)
                if isinstance(result, list):
                    result = {"items": result}
                elif not isinstance(result, dict):
                    result = {"raw_output": str(result)}
                if result.get("unsupported"):
                    skipped = f"not supported on {device_vendor or 'this device'}"
                else:
                    step_error = result.get("error")
            except Exception as exc:
                step_error = str(exc)
                result = {"error": step_error, "tool": tool_name}
                logger.warning("Step %r failed: %s", tool_name, exc)

            steps.append(StepResult(
                tool_name=tool_name,
                args=args,
                result=result,
                elapsed_ms=(time.monotonic() - step_start) * 1000,
                error=step_error,
                skipped=skipped,
            ))

        severity = _evaluate_severity(playbook["severity_rules"], steps)
        if not any(s for s in steps if not s.skipped and not s.error):
            severity = "Unknown"   # nothing ran successfully — never report a false "Info"
        evidence = _extract_evidence(playbook["evidence_fields"], steps)
        summary_text = _build_summary(playbook, params, severity, evidence, steps)

        total_elapsed = (time.monotonic() - run_start) * 1000
        logger.info(
            "Playbook %r done: severity=%s steps=%d elapsed=%.0fms",
            playbook["name"], severity, len(steps), total_elapsed,
        )

        return PlaybookResult(
            intent=intent,
            playbook_name=playbook["name"],
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
