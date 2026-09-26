"""
Tool Dispatcher — routes tool calls to the right backend.

  Device tools      (cisco_*, junos_*, nokia_*) → direct Python adapters
  Integration tools (jira_*, prometheus_*, syslog_*) → direct Python adapters
                    (jiravnd_*)                      → MCPClient (mcp-atlassian)
  Playbook tool     (run_playbook)                  → PlaybookRunner

Device adapters and integration adapters are all in-process.
Vendor MCP servers (mcp-atlassian etc.) run as Docker sidecars; the
MCPClient connects to them over SSE/streamable-http at startup.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import re

from adapters.devices.registry import correct_tool_vendor, find_device, get_vendor
from adapters.devices.safety import UnsafeInputError, validate_tool_args

logger = logging.getLogger("netcopilot.dispatcher")

# Vendor prefix → module path (direct adapters)
_VENDOR_MODULES: dict[str, str] = {
    "cisco": "adapters.devices.cisco",
    "junos":  "adapters.devices.junos",
    "nokia":  "adapters.devices.nokia",
    "aruba":  "adapters.devices.aruba",
}

# Integration prefix → module path (direct adapters)
_INTEGRATION_MODULES: dict[str, str] = {
    "jira":       "adapters.integrations.jira",
    "prometheus": "adapters.integrations.prometheus",
    "syslog":     "adapters.integrations.syslog",
}

_DEVICE_PREFIXES      = tuple(f"{v}_" for v in _VENDOR_MODULES)
_INTEGRATION_PREFIXES = tuple(f"{v}_" for v in _INTEGRATION_MODULES)

# Unprefixed playbook tool names that are served by an integration, not a device
_PLAYBOOK_INTEGRATION_ALIASES: dict[str, str] = {
    "get_top_flapping_links": "prometheus_get_top_flapping_links",
}


def _is_device_tool(tool_name: str) -> bool:
    return any(tool_name.startswith(p) for p in _DEVICE_PREFIXES)


def _is_integration_tool(tool_name: str) -> bool:
    return any(tool_name.startswith(p) for p in _INTEGRATION_PREFIXES)


def _load_module(mod_name: str):
    import importlib
    return importlib.import_module(mod_name)


# Role access for integration adapters
_INTEGRATION_ROLES: dict[str, set[str]] = {
    "jira":       {"admin", "noc-ticket"},
    "prometheus": {"admin", "noc-ticket", "noc"},
    "syslog":     {"admin", "noc-ticket", "noc"},
}


class ToolDispatcher:
    """
    Central dispatcher called by _agentic_loop in app.py.

    Usage:
        dispatcher = ToolDispatcher(mcp_client=_mcp)
        result = await dispatcher.call_tool("cisco_get_cpu_memory", {"device": "pe1"})
        result = await dispatcher.call_tool("run_playbook", {"intent": "bgp_down", "device": "pe1"})
    """

    def __init__(self, mcp_client=None):
        self._mcp = mcp_client   # MCPClient for vendor MCP servers (mcp-atlassian etc.)

    # ------------------------------------------------------------------
    # Tool list (called by _run_chat / _agentic_loop)
    # ------------------------------------------------------------------

    def get_all_tools(self, role: str) -> list[dict]:
        """
        Return combined tool list for the given role:
          - Device adapter tools (all roles)
          - Integration adapter tools (role-filtered)
          - Vendor MCP server tools (role-filtered via MCPClient)
          - run_playbook meta-tool (all roles)
        """
        tools: list[dict] = []

        # Device adapter tools
        for vendor, mod_name in _VENDOR_MODULES.items():
            try:
                mod = _load_module(mod_name)
                tools.extend(getattr(mod, "TOOL_SCHEMAS", []))
            except Exception as exc:
                logger.warning("Could not load tool schemas for %s: %s", vendor, exc)

        # Direct integration adapter tools (role-filtered)
        # Skip any prefix that a vendor MCP server already covers to avoid duplicates
        mcp_prefixes: set[str] = set()
        if self._mcp:
            for tool_name in self._mcp.tool_to_server:
                prefix = tool_name.split("_", 1)[0]
                mcp_prefixes.add(prefix)

        for prefix, mod_name in _INTEGRATION_MODULES.items():
            allowed_roles = _INTEGRATION_ROLES.get(prefix, {"admin", "noc-ticket", "noc"})
            if role not in allowed_roles:
                continue
            if prefix in mcp_prefixes:
                # Vendor MCP server covers this prefix — skip direct adapter schemas
                # (direct adapter still handles Slack handler's legacy tool names)
                continue
            try:
                mod = _load_module(mod_name)
                tools.extend(getattr(mod, "TOOL_SCHEMAS", []))
            except Exception as exc:
                logger.warning("Could not load tool schemas for %s: %s", prefix, exc)

        # Vendor MCP server tools (mcp-atlassian etc.)
        if self._mcp:
            try:
                tools.extend(self._mcp.get_tools(role))
            except Exception as exc:
                logger.warning("Could not get MCP tools: %s", exc)

        # Meta-tools (all roles)
        tools.append(_PLAYBOOK_SCHEMA)
        tools.append(_CHECK_LINK_SCHEMA)

        return tools

    # ------------------------------------------------------------------
    # Tool dispatch
    # ------------------------------------------------------------------

    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        """Route a tool call to the correct backend."""

        # Meta-tools
        if tool_name == "run_playbook":
            return await self._run_playbook(arguments)
        if tool_name == "check_link":
            return await self._check_link(arguments)

        # Vendor correction for device tools
        device = arguments.get("device", "")
        if device and _is_device_tool(tool_name):
            tool_name = correct_tool_vendor(tool_name, device)

        if _is_device_tool(tool_name):
            try:
                validate_tool_args(arguments)
            except UnsafeInputError as exc:
                logger.warning("Rejected unsafe arguments for %s: %s", tool_name, exc)
                return {"error": f"Rejected unsafe input: {exc}", "device": device}
            return await self._call_adapter_tool(tool_name, arguments, _VENDOR_MODULES)

        # Integration tools: check vendor MCP first (has richer tool set),
        # then fall back to direct adapter (keeps legacy Slack handler working)
        if _is_integration_tool(tool_name):
            if self._mcp and tool_name in self._mcp.tool_to_server:
                return await self._call_mcp_tool(tool_name, arguments)
            return await self._call_adapter_tool(tool_name, arguments, _INTEGRATION_MODULES)

        # Unknown prefix — try vendor MCP servers (e.g. jiravnd_*)
        if self._mcp and tool_name in self._mcp.tool_to_server:
            return await self._call_mcp_tool(tool_name, arguments)

        logger.warning("Unknown tool '%s'", tool_name)
        return {"error": f"Unknown tool '{tool_name}'"}

    async def call_tool_unprefixed(self, tool_name: str, arguments: dict) -> Any:
        """
        Playbook runner entry point — tool_name has NO vendor prefix.
        Looks up the device's vendor from the registry and prefixes automatically.

        Example: call_tool_unprefixed("get_bgp_summary", {"device": "pe1"})
                 → internally calls call_tool("junos_get_bgp_summary", {"device": "pe1"})
        """
        if tool_name == "check_link":
            return await self._check_link(arguments)
        if tool_name in _PLAYBOOK_INTEGRATION_ALIASES:
            return await self.call_tool(_PLAYBOOK_INTEGRATION_ALIASES[tool_name], arguments)
        if _is_integration_tool(tool_name):
            return await self.call_tool(tool_name, arguments)

        device = arguments.get("device", "")
        if device:
            vendor = get_vendor(device)
            if vendor:
                return await self.call_tool(f"{vendor}_{tool_name}", arguments)
            return {"error": f"Unknown device '{device}' — not in the device inventory."}
        return await self.call_tool(tool_name, arguments)

    # ------------------------------------------------------------------
    # Internal dispatch helpers
    # ------------------------------------------------------------------

    async def _call_adapter_tool(
        self,
        tool_name: str,
        arguments: dict,
        module_map: dict[str, str],
    ) -> Any:
        prefix, _, base_name = tool_name.partition("_")
        if not base_name:
            return {"error": f"Malformed tool name '{tool_name}'"}

        mod_name = module_map.get(prefix)
        if not mod_name:
            return {"error": f"No adapter module for prefix '{prefix}'"}

        try:
            mod = _load_module(mod_name)
        except Exception as exc:
            return {"error": f"Failed to load adapter '{prefix}': {exc}"}

        tools_map: dict[str, Callable] = getattr(mod, "TOOLS", {})
        func = tools_map.get(base_name)
        if not func:
            return {"error": f"Tool '{base_name}' not found in '{prefix}' adapter", "unsupported": True}

        try:
            return await func(**arguments)
        except TypeError as exc:
            logger.error("Tool %s called with wrong args %s: %s", tool_name, arguments, exc)
            return {"error": f"Invalid arguments for '{tool_name}': {exc}"}
        except Exception as exc:
            logger.exception("Tool %s failed: %s", tool_name, exc)
            return {"error": f"Tool execution failed: {exc}"}

    async def _call_mcp_tool(self, tool_name: str, arguments: dict) -> Any:
        try:
            return await self._mcp.call_tool(tool_name, arguments)
        except Exception as exc:
            logger.exception("MCP tool %s failed: %s", tool_name, exc)
            return {"error": f"MCP tool execution failed: {exc}"}

    async def _run_playbook(self, arguments: dict) -> dict:
        """Execute a structured diagnostic playbook and return severity + evidence."""
        from playbooks.runner import get_runner

        intent = arguments.get("intent", "")
        if not intent:
            return {"error": "run_playbook requires 'intent' argument"}

        # All non-intent args become playbook params (device, interface, neighbor, prefix…)
        params = {k: v for k, v in arguments.items() if k != "intent" and v is not None}

        try:
            runner = get_runner()
            result = await runner.run(
                intent=intent,
                params=params,
                tool_caller=self.call_tool_unprefixed,
            )
        except Exception as exc:
            logger.exception("Playbook runner failed for intent=%s: %s", intent, exc)
            return {"error": f"Playbook execution failed: {exc}"}

        return {
            "playbook": result.playbook_name,
            "severity": result.severity,
            "evidence": result.evidence,
            "summary": result.summary_text,
            "steps_run": len(result.steps),
            "elapsed_ms": round(result.elapsed_ms),
            **({"error": result.error} if result.error else {}),
        }

    async def _check_link(self, arguments: dict) -> dict:
        """
        Check both ends of a link: local interface health, identify the far end
        (LLDP, or the interface description when LLDP is gone because the link
        is down), then the far end's interface health, plus a combined assessment.
        """
        device = str(arguments.get("device", ""))
        interface = str(arguments.get("interface", ""))
        if not device or not interface:
            return {"error": "check_link requires 'device' and 'interface'."}

        local = await self.call_tool_unprefixed("get_interface_health", {"device": device, "interface": interface})
        if not isinstance(local, dict) or local.get("error"):
            return {"device": device, "interface": interface, "local": local,
                    "error": "Could not read the local interface.", "assessment": []}

        lldp = await self.call_tool_unprefixed("get_lldp_neighbor", {"device": device, "interface": interface})
        lldp = lldp if isinstance(lldp, dict) else {}
        far_name = lldp.get("neighbor_device") or ""
        far_if = lldp.get("neighbor_interface") or ""
        source = "lldp"
        remote_device = _match_inventory_device(far_name) if far_name else None
        if not remote_device:
            remote_device, desc_if = _peer_from_description(str(local.get("description") or ""), exclude=device)
            if remote_device:
                source = "interface description"
                far_if = far_if or desc_if or ""

        remote = None
        if remote_device and far_if:
            remote = await self.call_tool_unprefixed(
                "get_interface_health", {"device": remote_device, "interface": far_if}
            )

        return {
            "device": device,
            "interface": interface,
            "local": _link_fields(local),
            "far_end": {
                "lldp_name": far_name or None,
                "inventory_device": remote_device,
                "interface": far_if or None,
                "identified_by": source if remote_device else None,
            },
            "remote": _link_fields(remote) if isinstance(remote, dict) else None,
            "assessment": _assess_link(local, remote if isinstance(remote, dict) else None, remote_device, far_if),
        }

    # ------------------------------------------------------------------
    # Status (for /health endpoint)
    # ------------------------------------------------------------------

    def server_status(self) -> list[dict]:
        """Return status for all backends."""
        status: list[dict] = []

        for vendor, mod_name in _VENDOR_MODULES.items():
            try:
                mod = _load_module(mod_name)
                status.append({"name": vendor, "type": "device_adapter",
                                "connected": True, "tools": len(getattr(mod, "TOOLS", {}))})
            except Exception:
                status.append({"name": vendor, "type": "device_adapter",
                                "connected": False, "tools": 0})

        for prefix, mod_name in _INTEGRATION_MODULES.items():
            try:
                mod = _load_module(mod_name)
                status.append({"name": prefix, "type": "integration_adapter",
                                "connected": True, "tools": len(getattr(mod, "TOOLS", {}))})
            except Exception:
                status.append({"name": prefix, "type": "integration_adapter",
                                "connected": False, "tools": 0})

        if self._mcp:
            for srv in self._mcp.server_status():
                srv["type"] = "vendor_mcp"
                status.append(srv)

        return status


# ------------------------------------------------------------------
# run_playbook tool schema (injected into get_all_tools for all roles)
# ------------------------------------------------------------------

_PLAYBOOK_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "run_playbook",
        "description": (
            "Run a structured diagnostic playbook for a known issue pattern. "
            "Executes a fixed sequence of diagnostic tools, evaluates severity rules, "
            "and returns a severity assessment (Critical/Warning/Info) with evidence. "
            "Use this when you identify a specific issue type on a device."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": [
                        "link_down", "link_flap", "crc_errors",
                        "bgp_down", "bgp_active",
                        "isis_down", "route_missing",
                        "cpu_high", "hardware_fault",
                    ],
                    "description": "The issue pattern to investigate.",
                },
                "device": {
                    "type": "string",
                    "description": "Primary device to investigate.",
                },
                "interface": {
                    "type": "string",
                    "description": "Interface name — required for link_down, link_flap, crc_errors.",
                },
                "neighbor": {
                    "type": "string",
                    "description": "BGP neighbor IP — required for bgp_down, bgp_active.",
                },
                "prefix": {
                    "type": "string",
                    "description": "Route prefix — required for route_missing.",
                },
            },
            "required": ["intent", "device"],
        },
    },
}


# ------------------------------------------------------------------
# check_link helpers
# ------------------------------------------------------------------

_CHECK_LINK_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "check_link",
        "description": (
            "Check BOTH ends of a link: local interface state/errors, the far-end device and "
            "interface (via LLDP or the interface description), the far end's state/errors, and "
            "an assessment (e.g. local down / remote up → suspect local optic or unidirectional fibre). "
            "Use for interface down, flapping or error investigations."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "device": {"type": "string", "description": "Device name from inventory"},
                "interface": {"type": "string", "description": "Local interface name"},
            },
            "required": ["device", "interface"],
        },
    },
}

_LINK_FIELDS = (
    "admin_status", "oper_status", "description", "last_flapped", "last_link_flapped",
    "carrier_transitions", "input_errors", "output_errors", "crc_errors",
    "rx_power_dbm", "tx_power_dbm", "error",
)


def _link_fields(result: dict | None) -> dict | None:
    if not isinstance(result, dict):
        return None
    return {k: result[k] for k in _LINK_FIELDS if result.get(k) not in (None, "")}


def _match_inventory_device(name: str) -> str | None:
    """Map an LLDP system name (possibly an FQDN) to an inventory device name."""
    return find_device(name)


_IFACE_TOKEN = re.compile(r"\b([a-z]{2,4}-\d+/\d+/\d+(?:[.:]\d+)?|[A-Za-z]+(?:Ethernet)?\d+(?:/\d+){1,3}(?:\.\d+)?|\d+/\d+/\d+)\b")


def _peer_from_description(description: str, exclude: str) -> tuple[str | None, str | None]:
    """Find an inventory device (and interface) mentioned in e.g. 'to pe2 xe-0/0/1'."""
    if not description:
        return None, None
    tokens = re.split(r"[\s,;:()\[\]]+", description)
    for token in tokens:
        match = _match_inventory_device(token)
        if match and match != exclude:
            iface = _IFACE_TOKEN.search(description)
            return match, iface.group(1) if iface else None
    return None, None


def _num(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _assess_link(local: dict, remote: dict | None, remote_device: str | None, far_if: str) -> list[str]:
    notes: list[str] = []
    l_admin, l_oper = str(local.get("admin_status", "")).lower(), str(local.get("oper_status", "")).lower()
    if l_admin == "down":
        notes.append("Local end is administratively down (shutdown/disable in config).")
    elif l_oper == "down":
        notes.append("Local end is admin up but operationally down.")
    elif l_oper == "up":
        notes.append("Local end is up.")

    if not remote_device:
        notes.append("Far end not identified (no LLDP neighbor and no inventory device in the description).")
    elif remote is None:
        notes.append(f"Far end {remote_device} identified but its interface is unknown; check it manually.")
    elif remote.get("error"):
        notes.append(f"Could not read far end {remote_device} {far_if}: {remote.get('error')}")
    else:
        r_admin, r_oper = str(remote.get("admin_status", "")).lower(), str(remote.get("oper_status", "")).lower()
        if r_admin == "down":
            notes.append(f"Far end {remote_device} {far_if} is administratively down — check the peer's configuration.")
        if l_oper == "down" and r_oper == "down" and l_admin != "down" and r_admin != "down":
            notes.append("Both ends are down while admin up: physical layer (fibre, patching, optics) or a transport fault is likely.")
        elif l_oper == "down" and r_oper == "up":
            notes.append("Asymmetric: local down, far end up — suspect the local receive path (optic, Rx power, unidirectional fibre).")
        elif l_oper == "up" and r_oper == "down":
            notes.append("Asymmetric: local up, far end down — suspect the far end's receive path.")
        elif l_oper == "up" and r_oper == "up":
            notes.append("Both ends are up.")
        for data, name in ((local, "local end"), (remote, remote_device)):
            crc = _num(data.get("crc_errors"))
            if crc > 0:
                notes.append(f"CRC errors on the {name} ({crc}): corruption arriving at its receiver — check the "
                             f"cable/optic feeding it and the transmitter on the other side.")
    return notes
