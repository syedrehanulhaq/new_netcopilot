"""
Test isolation: every SQLite DB and devices.yaml lives in a throwaway directory,
so running the suite never touches the repo's runtime files.
The environment must be set before any NetCopilot module is imported.
"""

import os
import sys
import tempfile

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = tempfile.mkdtemp(prefix="netcopilot-tests-")
DEVICES_YAML = os.path.join(DATA_DIR, "devices.yaml")

os.environ["NETCOPILOT_DATA_DIR"] = DATA_DIR
os.environ["DEVICE_REGISTRY_PATH"] = DEVICES_YAML
os.environ["OPENAI_API_KEY"] = "sk-test"
os.environ["LLM_PROVIDER"] = "openai"
os.environ["JIRA_MCP_URL"] = "http://127.0.0.1:9/sse"  # nothing listens there — fails fast
# TestClient's peer is "testclient", not loopback; auth-specific tests turn this off.
os.environ["ALLOW_ANONYMOUS_ACCESS"] = "true"
for key in ("NETBOX_URL", "NAUTOBOT_URL", "NETCOPILOT_API_KEYS", "AUTH0_DOMAIN",
            "NETCOPILOT_LOCK_GUARDRAILS", "NETCOPILOT_ENCRYPTION_KEY", "SESSION_SECRET"):
    os.environ.pop(key, None)
for key, value in {
    "CISCO_USER": "u", "CISCO_PASS": "p",
    "NOKIA_USER": "u", "NOKIA_PASS": "p",
    "ARUBA_USER": "u", "ARUBA_PASS": "p",
}.items():
    os.environ[key] = value

os.chdir(ROOT)  # app.py serves ./static and reads ./mcp_servers.yaml
sys.path.insert(0, ROOT)

BASE_DEVICES = {
    "devices": {
        "pe1": {"host": "10.0.0.1", "vendor": "junos", "platform": "junos"},
        "xr1": {"host": "10.0.0.2", "vendor": "cisco", "platform": "iosxr"},
        "sr1": {"host": "10.0.0.3", "vendor": "nokia", "platform": "sros"},
        "sw1": {"host": "10.0.0.4", "port": 2222, "vendor": "aruba", "platform": "aoscx"},
    }
}


def write_devices(data: dict) -> None:
    with open(DEVICES_YAML, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)


@pytest.fixture(autouse=True)
def device_registry():
    """Fresh devices.yaml, registry and device pool for every test."""
    from adapters.devices import pool, registry

    write_devices(BASE_DEVICES)
    registry.set_external_devices({})
    registry.reload_registry()
    pool._pool = None  # asyncio primitives must not leak across event loops
    yield


class FakeConn:
    """Stands in for a Netmiko connection; records commands and detects concurrent use."""

    sent: list[str] = []
    overlaps = 0
    _active: dict[int, int] = {}

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def send_command(self, command):
        import threading
        import time

        with _fake_lock:
            FakeConn.sent.append(command)
            FakeConn._active[id(self)] = FakeConn._active.get(id(self), 0) + 1
            if FakeConn._active[id(self)] > 1:
                FakeConn.overlaps += 1
        time.sleep(0.05)
        with _fake_lock:
            FakeConn._active[id(self)] -= 1
        return "ok"

    def disconnect(self):
        pass


import threading as _threading  # noqa: E402

_fake_lock = _threading.Lock()


@pytest.fixture
def fake_netmiko(monkeypatch):
    import netmiko

    from adapters.devices import aruba, cisco, nokia
    from adapters.devices.ssh_cache import SSHSessionCache

    FakeConn.sent = []
    FakeConn.overlaps = 0
    FakeConn._active = {}
    monkeypatch.setattr(netmiko, "ConnectHandler", FakeConn)
    for mod, name in ((cisco, "cisco"), (nokia, "nokia"), (aruba, "aruba")):
        monkeypatch.setattr(mod, "_SESSIONS", SSHSessionCache(name))
    return FakeConn
