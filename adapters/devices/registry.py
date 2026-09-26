"""
Device Registry — single source of truth for device→vendor mapping.

All routing decisions are made here. The LLM is never asked to guess vendor.

Usage:
    from adapters.devices.registry import get_vendor, make_tool_name, resolve_host, list_devices

    vendor = get_vendor("sandbox-iosxr-1")   # → "cisco"
    tool   = make_tool_name("cisco", "get_cpu_memory")  # → "cisco_get_cpu_memory"
    host, port = resolve_host("sandbox-iosxr-1")   # → ("sandbox-iosxr-1.cisco.com", None)
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

import yaml

logger = logging.getLogger("netcopilot.device_registry")

_REGISTRY_PATH = os.environ.get(
    "DEVICE_REGISTRY_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "devices.yaml"),
)

_VENDOR_ALIASES = {
    "cisco": "cisco",
    "iosxr": "cisco",
    "ios-xr": "cisco",
    "junos": "junos",
    "juniper": "junos",
    "nokia": "nokia",
    "sros": "nokia",
    "sr-os": "nokia",
    "alcatel": "nokia",
    "aruba": "aruba",
    "aoscx": "aruba",
    "aos-cx": "aruba",
    "arubaos-cx": "aruba",
    "aosswitch": "aruba",
    "aos-switch": "aruba",
    "procurve": "aruba",
}


def normalize_vendor(value: str) -> str:
    """Map a vendor/platform alias (e.g. 'juniper', 'ios-xr') to its adapter prefix, or ''."""
    return _VENDOR_ALIASES.get(str(value or "").strip().lower(), "")


def _load() -> dict[str, dict]:
    """Load and normalize the devices registry."""
    path = _REGISTRY_PATH
    if not os.path.exists(path):
        logger.warning("devices.yaml not found at %s", path)
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except Exception as exc:
        logger.error("Failed to load devices.yaml: %s", exc)
        return {}

    devices: dict[str, dict] = {}

    # New format: flat devices dict with vendor field
    if "devices" in raw:
        for name, cfg in (raw["devices"] or {}).items():
            if not isinstance(cfg, dict):
                continue
            vendor = normalize_vendor(cfg.get("vendor", ""))
            if not vendor:
                logger.warning("Device '%s' has no vendor field — skipping.", name)
                continue
            devices[str(name)] = {
                "host": str(cfg.get("host", name)),
                "port": cfg.get("port"),
                "vendor": vendor,
                "platform": cfg.get("platform", vendor),
                "site": cfg.get("site", ""),
                "netconf_port": cfg.get("netconf_port"),
                "source": "devices.yaml",
            }

    # Legacy format: cisco_devices / juniper_devices / nokia_devices / aruba_devices
    for section, vendor in [
        ("cisco_devices", "cisco"),
        ("juniper_devices", "junos"),
        ("nokia_devices", "nokia"),
        ("aruba_devices", "aruba"),
    ]:
        for name, value in (raw.get(section) or {}).items():
            if str(name) in devices:
                continue  # new format wins
            host_port = str(value)
            host, _, port_str = host_port.partition(":")
            devices[str(name)] = {
                "host": host,
                "port": int(port_str) if port_str.isdigit() else None,
                "vendor": vendor,
                "platform": vendor,
                "site": "",
                "netconf_port": None,
                "source": "devices.yaml",
            }

    return devices


# Module-level cache — reloaded by reload_registry()
_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()

# Devices discovered from NetBox / Nautobot (set by config.reload_inventory_devices).
# devices.yaml entries win over these on name collisions.
_external: dict[str, dict] = {}


def set_external_devices(devices: dict[str, dict]) -> None:
    """Replace the source-of-truth (NetBox/Nautobot) device set merged into the registry."""
    global _external
    with _cache_lock:
        _external = dict(devices)


def reload_registry() -> None:
    """Reload device registry atomically (thread-safe)."""
    new_cache = _load()  # expensive I/O outside the lock
    with _cache_lock:
        global _cache
        merged = dict(_external)
        merged.update(new_cache)
        _cache = merged
    logger.info(
        "Device registry loaded: %d devices (%d from devices.yaml, %d from inventory sources)",
        len(merged), len(new_cache), len(merged) - len(new_cache),
    )


def registry_path() -> str:
    return _REGISTRY_PATH


def _registry() -> dict[str, dict]:
    with _cache_lock:
        if not _cache:
            # Release lock, load, then re-acquire to swap
            pass
        else:
            return _cache
    reload_registry()
    with _cache_lock:
        return _cache


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_vendor(device: str) -> Optional[str]:
    """Return the vendor string for a device, or None if unknown."""
    entry = _registry().get(device)
    return entry["vendor"] if entry else None


def resolve_host(device: str) -> tuple[str, Optional[int]]:
    """
    Return (hostname, port) for a device.
    Raises ValueError if device is not in registry.
    """
    entry = _registry().get(device)
    if not entry:
        raise ValueError(
            f"Unknown device '{device}'. Known: {sorted(_registry().keys())}"
        )
    return entry["host"], entry.get("port")


def get_device_info(device: str) -> Optional[dict]:
    """Return the full device entry dict, or None."""
    return _registry().get(device)


def list_devices() -> list[dict]:
    """Return all registered devices as a list of dicts (no credentials)."""
    return [
        {
            "name": name,
            "vendor": info["vendor"],
            "platform": info["platform"],
            "site": info.get("site", ""),
        }
        for name, info in _registry().items()
    ]


def list_devices_detailed() -> list[dict]:
    """Return all registered devices with connection targets (admin inventory view)."""
    return [
        {
            "name": name,
            "vendor": info["vendor"],
            "platform": info["platform"],
            "host": info["host"],
            "port": info.get("port"),
            "site": info.get("site", ""),
            "source": info.get("source", ""),
        }
        for name, info in sorted(_registry().items())
    ]


def find_device(identifier: str) -> Optional[str]:
    """
    Resolve an inventory device name from a name, FQDN, IP/host, or 'host:port'
    (as found in LLDP system names, alert labels, syslog hosts). Case-insensitive.
    """
    ident = str(identifier or "").strip().lower()
    if not ident:
        return None
    reg = _registry()
    names = {n.lower(): n for n in reg}
    candidates = [ident]
    if ident.count(":") == 1:          # host:port (not IPv6)
        candidates.append(ident.split(":", 1)[0])
    candidates += [c.split(".", 1)[0] for c in list(candidates) if not c.replace(".", "").isdigit()]
    for candidate in candidates:
        if candidate in names:
            return names[candidate]
    port = ident.split(":", 1)[1] if ident.count(":") == 1 else ""
    for candidate in candidates:
        matches = [
            name for name, info in reg.items()
            if str(info.get("host", "")).lower() in (candidate,)
            or str(info.get("host", "")).lower().split(".", 1)[0] == candidate
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            # Several devices behind one address (port-mapped lab / NAT): only a port
            # match is conclusive — never guess, a wrong device means a wrong diagnosis.
            by_port = [n for n in matches if port and str(reg[n].get("port") or "") == port]
            return by_port[0] if len(by_port) == 1 else None
    for candidate in candidates:       # inventory names that are FQDNs themselves
        for lname, real in names.items():
            if lname.split(".", 1)[0] == candidate:
                return real
    return None


def make_tool_name(vendor: str, base_tool: str) -> str:
    """Return the prefixed tool name, e.g. cisco_get_cpu_memory."""
    return f"{vendor}_{base_tool}"


def correct_tool_vendor(tool_name: str, device: str) -> str:
    """
    Validate that the tool's vendor prefix matches the device's registered vendor.
    If not, return the corrected tool name.

    Example:
        correct_tool_vendor("nokia_get_cpu_memory", "sandbox-iosxr-1")
        → "cisco_get_cpu_memory"
    """
    vendor = get_vendor(device)
    if not vendor:
        return tool_name  # unknown device, let it pass through

    parts = tool_name.split("_", 1)
    if len(parts) == 2 and parts[0] != vendor:
        corrected = f"{vendor}_{parts[1]}"
        logger.warning(
            "Tool vendor mismatch: '%s' called for device '%s' (vendor=%s) → correcting to '%s'",
            tool_name,
            device,
            vendor,
            corrected,
        )
        return corrected
    return tool_name


def device_inventory_summary() -> str:
    """Return a human-readable device list for the LLM system prompt."""
    reg = _registry()
    if not reg:
        return "No devices registered."
    lines = []
    for name, info in sorted(reg.items()):
        site = f" [{info['site']}]" if info.get("site") else ""
        lines.append(f"  {name}: vendor={info['vendor']} platform={info['platform']}{site}")
    return "\n".join(lines)
