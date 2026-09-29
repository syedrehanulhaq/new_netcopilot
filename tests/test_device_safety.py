import asyncio

import pytest

from adapters.devices.safety import UnsafeInputError, validate_cli_command, validate_tool_args


@pytest.mark.parametrize("command", [
    "show bgp summary",
    "show interfaces ge-0/0/1 extensive | match error",
    "show configuration | display set",
    "show configuration | compare rollback 1",
    "ping 10.0.0.1 count 5",
    "traceroute 10.0.0.1",
])
def test_read_only_commands_are_allowed(command):
    assert validate_cli_command("junos", command) == command


@pytest.mark.parametrize("vendor,command", [
    ("cisco", "clear bgp ipv4 unicast *"),
    ("cisco", "reload location all noprompt"),
    ("cisco", "configure terminal"),
    ("junos", "request system reboot"),
    ("junos", "restart routing"),
    ("junos", "show configuration | save /var/tmp/x"),
    ("cisco", "show running-config | file disk0:/x"),
    ("cisco", "show version > disk0:/x"),
    ("nokia", "show port\nadmin reboot now"),
    ("nokia", "debug router isis"),
    ("cisco", "ping 10.0.0.1 count 100000"),
    ("aruba", ""),
])
def test_disruptive_or_injected_commands_are_rejected(vendor, command):
    with pytest.raises(UnsafeInputError):
        validate_cli_command(vendor, command)


def test_nokia_admin_show_is_allowed():
    assert validate_cli_command("nokia", "admin show configuration")


@pytest.mark.parametrize("args", [
    {"device": "pe1", "interface": "ge-0/0/1.0"},
    {"device": "xr1", "interface": "GigabitEthernet0/0/0/0"},
    {"device": "sr1", "interface": "1/1/c1/1:100"},
    {"device": "xr1", "prefix": "2001:db8::/32"},
    {"device": "xr1", "destination": "8.8.8.8", "count": 5},
])
def test_normal_tool_args_pass(args):
    validate_tool_args(args)


@pytest.mark.parametrize("args", [
    {"device": "xr1", "destination": "8.8.8.8\nreload location all noprompt"},
    {"device": "xr1", "interface": "Gi0/0/0/0 | file disk0:x"},
    {"device": "xr1", "prefix": "10.0.0.0/8; reload"},
    {"device": "xr1", "destination": "8.8.8.8", "count": 1_000_000},
])
def test_injection_in_tool_args_is_rejected(args):
    with pytest.raises(UnsafeInputError):
        validate_tool_args(args)


def test_cisco_run_command_never_reaches_device_for_disruptive_command(fake_netmiko):
    from adapters.devices import cisco

    result = asyncio.run(cisco.run_command(device="xr1", command="clear bgp ipv4 unicast *"))
    assert "error" in result
    assert fake_netmiko.sent == []


def test_dispatcher_blocks_newline_injection_before_device(fake_netmiko):
    from adapters.devices.dispatcher import ToolDispatcher

    result = asyncio.run(ToolDispatcher().call_tool(
        "cisco_get_ping", {"device": "xr1", "destination": "8.8.8.8\nreload location all noprompt"},
    ))
    assert "Rejected unsafe input" in result["error"]
    assert fake_netmiko.sent == []


@pytest.mark.parametrize("module,device", [("cisco", "xr1"), ("nokia", "sr1"), ("aruba", "sw1")])
def test_adapters_reject_newlines_even_when_called_directly(fake_netmiko, module, device):
    import importlib

    adapter = importlib.import_module(f"adapters.devices.{module}")
    result = asyncio.run(adapter.get_ping(device=device, destination="8.8.8.8\nreload"))
    assert "Rejected unsafe input" in result["error"]
    assert fake_netmiko.sent == []


def test_nokia_run_command_rejects_embedded_newline(fake_netmiko):
    from adapters.devices import nokia

    result = asyncio.run(nokia.run_command(device="sr1", command="show port\nadmin reboot now"))
    assert "reject" in result["error"].lower()
    assert fake_netmiko.sent == []


def test_parallel_calls_never_share_an_ssh_session(fake_netmiko):
    from adapters.devices import cisco

    async def main():
        await cisco.get_bgp_summary(device="xr1")  # warm the session cache
        await asyncio.gather(
            cisco.get_bgp_summary(device="xr1"),
            cisco.get_isis_neighbors(device="xr1"),
            cisco.get_ospf_neighbors(device="xr1"),
        )

    asyncio.run(main())
    assert fake_netmiko.overlaps == 0
    assert len(fake_netmiko.sent) == 4


def test_failed_session_is_not_returned_to_the_cache():
    from adapters.devices.ssh_cache import SSHSessionCache

    cache = SSHSessionCache("t")
    conn = object.__new__(type("C", (), {"disconnect": lambda self: None}))
    with pytest.raises(RuntimeError):
        with cache.session("d", lambda: conn):
            raise RuntimeError("boom")
    assert cache.idle_count("d") == 0

    with cache.session("d", lambda: conn):
        pass
    assert cache.idle_count("d") == 1
    assert cache.checkout("d") is conn
    assert cache.idle_count("d") == 0


def test_junos_cpu_fully_busy_is_reported_as_100_percent(monkeypatch):
    from contextlib import contextmanager

    from lxml import etree

    from adapters.devices import junos

    xml = etree.fromstring(
        "<route-engine-information><route-engine>"
        "<cpu-idle>0</cpu-idle><memory-system-total-used-percent>40</memory-system-total-used-percent>"
        "</route-engine></route-engine-information>"
    )

    class _Dev:
        class rpc:
            @staticmethod
            def get_route_engine_information():
                return xml

    @contextmanager
    def _fake_connect(device):
        yield _Dev()

    monkeypatch.setattr(junos, "_connect", _fake_connect)
    result = junos._sync_get_cpu_memory("pe1")
    assert result["cpu_percent"] == 100


def _closed_local_port():
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here now → connection refused immediately
    return port


def test_unreachable_device_fails_fast_with_clear_message(monkeypatch):
    import time

    from adapters.devices import cisco, registry
    from conftest import write_devices

    port = _closed_local_port()
    write_devices({"devices": {"lab-xr": {"host": "127.0.0.1", "port": port, "vendor": "cisco"}}})
    registry.reload_registry()
    monkeypatch.setenv("DEVICE_CONNECT_CHECK_SECONDS", "3")
    started = time.time()
    result = asyncio.run(cisco.get_cpu_memory(device="lab-xr"))
    assert f"Cannot reach 127.0.0.1:{port}" in result["error"]
    assert time.time() - started < 3


def test_failed_fresh_connection_is_not_retried(monkeypatch):
    from adapters.devices import cisco
    from adapters.devices.ssh_cache import SSHSessionCache

    attempts = []

    def failing_connect(device):
        attempts.append(device)
        raise ConnectionError("Cannot reach 192.0.2.1:22")

    monkeypatch.setattr(cisco, "_SESSIONS", SSHSessionCache("cisco"))
    monkeypatch.setattr(cisco, "_connect", failing_connect)
    with pytest.raises(ConnectionError):
        cisco._cli("xr1", "show version")
    assert attempts == ["xr1"]  # one attempt, not two
