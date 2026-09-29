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


def check_reachable(host: str, port: int) -> None:
    """
    Fail fast when a device's management port doesn't answer: a TCP connect with a
    short timeout (DEVICE_CONNECT_CHECK_SECONDS, default 5; 0 disables) instead of
    waiting for the SSH/NETCONF library's much longer timeouts.
    """
    import socket

    from settings import get_setting_int

    timeout = get_setting_int("DEVICE_CONNECT_CHECK_SECONDS", 5)
    if timeout <= 0:
        return
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            pass
    except OSError as exc:
        reason = "timed out" if isinstance(exc, TimeoutError) else (exc.strerror or type(exc).__name__)
        raise ConnectionError(
            f"Cannot reach {host}:{port} ({reason} after {timeout}s). Check the device address/port "
            f"in the inventory and that this server can reach the device's management network."
        ) from exc
