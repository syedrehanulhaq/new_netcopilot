"""
Read-only enforcement for device tools.

NetCopilot is read-only by design, but the LLM chooses tool arguments — and
those arguments can be steered by untrusted text (alert payloads, syslog lines,
interface descriptions). Every string that ends up on a device CLI therefore
goes through this module first:

  validate_tool_args(args)          structured args (interface, prefix, ...)
  validate_cli_command(vendor, cmd) free-form commands for run_command / custom_cli

Both raise UnsafeInputError; callers turn that into a tool error so the LLM can
see why the call was refused.
"""

from __future__ import annotations

import re
from typing import Any

MAX_PING_COUNT = 100
_MAX_ARG_LEN = 256
_MAX_COMMAND_LEN = 512


class UnsafeInputError(ValueError):
    """Raised when a tool argument or CLI command is not safe to send to a device."""


# Newlines would let one argument smuggle a second command onto the device.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# Per-argument character sets. Interface names are the only values that may
# legitimately contain spaces ("GigabitEthernet 0/1" on some platforms).
_SAFE_ARG = re.compile(r"^[A-Za-z0-9_.:/@+\-]+$")
_SAFE_INTERFACE = re.compile(r"^[A-Za-z0-9_.:/+\- ]+$")

_STRUCTURED_ARGS = ("device", "interface", "neighbor", "prefix", "destination", "acl_name", "vlan_id")


def validate_tool_args(args: dict[str, Any]) -> None:
    """Reject structured tool arguments that could inject CLI syntax."""
    for key in _STRUCTURED_ARGS:
        value = args.get(key)
        if value is None or value == "":
            continue
        text = str(value)
        if len(text) > _MAX_ARG_LEN:
            raise UnsafeInputError(f"Argument '{key}' is too long.")
        if _CONTROL_CHARS.search(text):
            raise UnsafeInputError(f"Argument '{key}' contains control characters (e.g. newlines).")
        pattern = _SAFE_INTERFACE if key == "interface" else _SAFE_ARG
        if not pattern.match(text):
            raise UnsafeInputError(f"Argument '{key}' contains characters that are not allowed: {text!r}")

    if "count" in args and args["count"] is not None:
        try:
            count = int(args["count"])
        except (TypeError, ValueError):
            raise UnsafeInputError("Argument 'count' must be an integer.")
        if not 1 <= count <= MAX_PING_COUNT:
            raise UnsafeInputError(f"Argument 'count' must be between 1 and {MAX_PING_COUNT}.")


def reject_control_chars(*values: Any) -> None:
    """Last line of defence inside CLI adapters: no argument may carry a newline."""
    for value in values:
        if isinstance(value, str) and _CONTROL_CHARS.search(value):
            raise UnsafeInputError("Arguments must not contain newlines or control characters.")


# ---------------------------------------------------------------------------
# Free-form CLI commands
# ---------------------------------------------------------------------------

# First word(s) a read-only command may start with, per vendor.
_READ_ONLY_PREFIXES: dict[str, tuple[str, ...]] = {
    "cisco": ("show", "ping", "traceroute"),
    "junos": ("show", "ping", "traceroute"),
    "nokia": ("show", "admin show", "admin display-config", "ping", "traceroute"),
    "aruba": ("show", "ping", "traceroute"),
}

# Output modifiers that only filter or format output. Anything else after a
# pipe (save, file, tee, append, redirect, request, ...) is refused because
# several of them write files on the device.
_SAFE_PIPE_MODIFIERS = {
    "match", "except", "include", "exclude", "count", "last", "begin",
    "find", "trim", "no-more", "display", "section", "resolve", "compare",
}

_REPEAT_OPTION = re.compile(r"\b(?:count|repeat|repetitions|rapid)\s+(\d+)", re.IGNORECASE)


def validate_cli_command(vendor: str, command: str) -> str:
    """
    Return the normalized command if it is read-only, otherwise raise.

    Rules: single line, starts with a read-only verb for the vendor, output
    pipes limited to filter/format modifiers, no redirection, and ping repeat
    counts capped at MAX_PING_COUNT.
    """
    if not isinstance(command, str) or not command.strip():
        raise UnsafeInputError("Command is empty.")
    if len(command) > _MAX_COMMAND_LEN:
        raise UnsafeInputError("Command is too long.")
    if _CONTROL_CHARS.search(command):
        raise UnsafeInputError("Command must be a single line (no newlines or control characters).")

    normalized = " ".join(command.split())
    lowered = normalized.lower()

    prefixes = _READ_ONLY_PREFIXES.get(vendor, ("show",))
    if not any(lowered == p or lowered.startswith(p + " ") for p in prefixes):
        raise UnsafeInputError(
            f"Only read-only commands are allowed on {vendor} devices "
            f"(must start with: {', '.join(prefixes)})."
        )

    if ">" in normalized or ";" in normalized:
        raise UnsafeInputError("Output redirection and command chaining are not allowed.")

    for segment in normalized.split("|")[1:]:
        modifier = segment.strip().split(" ", 1)[0].lower()
        if modifier not in _SAFE_PIPE_MODIFIERS:
            raise UnsafeInputError(
                f"Pipe modifier '{modifier}' is not allowed. "
                f"Allowed: {', '.join(sorted(_SAFE_PIPE_MODIFIERS))}."
            )

    for match in _REPEAT_OPTION.finditer(normalized):
        if int(match.group(1)) > MAX_PING_COUNT:
            raise UnsafeInputError(f"Repeat counts above {MAX_PING_COUNT} are not allowed.")

    return normalized
