"""
SSH host-key verification for device connections.

SSH_STRICT_HOST_KEY_CHECKING=true makes every device connection verify the
device's host key against a known_hosts file and refuse unknown or changed keys
(protects device credentials against man-in-the-middle). Off by default so labs
work out of the box; the security posture report flags it when off.

  Netmiko (Cisco, Nokia, Aruba): SSH_KNOWN_HOSTS_FILE (default ~/.ssh/known_hosts)
  PyEZ / ncclient (Junos):       always ~/.ssh/known_hosts (ncclient's default path)
"""

from __future__ import annotations

import os

from settings import get_setting, get_setting_bool

DEFAULT_KNOWN_HOSTS = "~/.ssh/known_hosts"


def strict_host_key_checking() -> bool:
    return get_setting_bool("SSH_STRICT_HOST_KEY_CHECKING", False)


def known_hosts_file() -> str:
    return os.path.expanduser(get_setting("SSH_KNOWN_HOSTS_FILE", DEFAULT_KNOWN_HOSTS) or DEFAULT_KNOWN_HOSTS)


def netmiko_host_key_params() -> dict:
    """Extra ConnectHandler kwargs; unknown host keys are rejected when strict."""
    if not strict_host_key_checking():
        return {}
    return {
        "ssh_strict": True,
        "system_host_keys": False,
        "alt_host_keys": True,
        "alt_key_file": known_hosts_file(),
    }


def junos_host_key_params() -> dict:
    """Extra jnpr.junos.Device kwargs (ncclient verifies against ~/.ssh/known_hosts)."""
    if not strict_host_key_checking():
        return {}
    return {"hostkey_verify": True}
