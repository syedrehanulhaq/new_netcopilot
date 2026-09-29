import asyncio

import pytest

from playbooks.runner import get_runner


def run(intent, params, responses, calls=None):
    """Run a playbook with a fake tool caller. responses: tool name -> result dict."""
    calls = calls if calls is not None else []

    async def tool_caller(name, args):
        calls.append((name, args))
        if name in responses:
            return responses[name]
        return {"error": f"Tool '{name}' not found in 'x' adapter", "unsupported": True}

    return asyncio.run(get_runner().run(intent, params, tool_caller)), calls


def test_store_backed_playbooks_actually_run_their_steps():
    # Regression: runner read YAML keys ('tools') from DB-format playbooks ('steps')
    # and executed zero steps, always returning "Info".
    result, calls = run(
        "bgp_down",
        {"device": "pe1", "neighbor": "10.0.0.2"},
        {
            "get_bgp_neighbor_status": {"state": "Idle", "last_error": "Hold Timer Expired"},
            "get_bgp_summary": {"peers": [{"address": "10.0.0.2", "state": "Idle"}]},
            "get_ping": {"packet_loss_pct": 100.0},
        },
    )
    assert [c[0] for c in calls] == ["get_bgp_neighbor_status", "get_bgp_summary", "get_ping", "get_recent_changes"]
    assert result.severity == "Critical"
    assert result.evidence["state"] == "Idle"
    assert result.evidence["last_error"] == "Hold Timer Expired"


def test_bgp_severity_is_scoped_to_the_requested_neighbor():
    # Another peer being Idle in the summary must not make an Established neighbor Critical,
    # and evidence must report the requested neighbor's state.
    result, calls = run(
        "bgp_down",
        {"device": "pe1", "neighbor": "10.0.0.2"},
        {
            "get_bgp_neighbor_status": {"state": "Established"},
            "get_bgp_summary": {"peers": [
                {"address": "10.0.0.9", "state": "Idle"},
                {"address": "10.0.0.2", "state": "Established"},
            ]},
            "get_ping": {"packet_loss_pct": 0.0},
        },
    )
    assert result.severity == "Info"
    assert result.evidence["state"] == "Established"
    # the BGP neighbor address is used as the ping destination
    assert ("get_ping", {"device": "pe1", "destination": "10.0.0.2"}) in calls


@pytest.mark.parametrize("admin,oper,expected", [
    ("up", "up", "Info"),        # healthy — used to be reported Critical
    ("up", "down", "Critical"),
    ("down", "down", "Warning"),  # administratively down
])
def test_link_down_severity(admin, oper, expected):
    result, _ = run(
        "link_down",
        {"device": "pe1", "interface": "ge-0/0/1"},
        {
            "get_interface_health": {"admin_status": admin, "oper_status": oper},
            "get_lldp_neighbor": {"neighbor_device": "pe2"},
        },
    )
    assert result.severity == expected


def test_steps_missing_inputs_or_unsupported_are_skipped_not_errors():
    result, calls = run(
        "crc_errors",
        {"device": "xr1", "interface": "Gi0/0/0/0"},
        {
            "get_interface_health": {"crc_errors": 12},
            "get_interface_counters": {"crc_errors": 12},
            # get_optics: not implemented for Cisco → unsupported
        },
    )
    assert result.severity == "Warning"
    optics = [s for s in result.steps if s.tool_name == "get_optics"][0]
    assert optics.skipped and not optics.error
    assert "Errors:" not in result.summary_text

    result, calls = run("isis_down", {"device": "pe1"}, {"get_isis_neighbors": {"neighbors": []}})
    skipped = {s.tool_name: s.skipped for s in result.steps if s.skipped}
    assert "missing input: interface" in skipped["get_mtu_info"]


def test_nothing_succeeding_reports_unknown_not_info():
    result, _ = run("cpu_high", {"device": "pe1"}, {"get_cpu_memory": {"error": "timeout"}})
    assert result.severity == "Unknown"


def test_cpu_rules_use_vendor_neutral_fields():
    result, _ = run("cpu_high", {"device": "xr1"}, {"get_cpu_memory": {"cpu_percent": 93, "memory_used_percent": 40}})
    assert result.severity == "Warning"


def test_hardware_fault_major_alarm_is_critical():
    result, _ = run(
        "hardware_fault",
        {"device": "pe1"},
        {"get_chassis_health": {
            "has_alarms": True,
            "chassis_alarms": [{"class": "Major", "description": "PEM 0 Not OK"}],
            "fpcs": [{"slot": "0", "state": "Online"}],
        }},
    )
    assert result.severity == "Critical"


def test_link_flap_calls_prometheus_without_device_argument():
    result, calls = run(
        "link_flap",
        {"device": "pe1", "interface": "ge-0/0/1"},
        {
            "get_interface_health": {"carrier_transitions": 9},
            "get_interface_counters": {},
            "get_top_flapping_links": [{"device": "pe1", "interface": "ge-0/0/1", "flap_count": 9}],
        },
    )
    assert ("get_top_flapping_links", {}) in calls
    assert result.severity == "Warning"


def test_ui_step_types_are_executed(monkeypatch):
    from playbooks import store

    store.upsert_playbooks([{
        "id": "custom_iface",
        "title": "Custom interface check",
        "intent": "custom_iface",
        "vendor": "any",
        "steps": [
            {"type": "custom_cli", "command": "show interfaces {{interface}} extensive"},
            {"type": "prometheus_query", "query": 'up{instance="{{device}}"}'},
            {"type": "syslog_search", "query": "{{device}} {{interface}}", "limit": 20},
            {"type": "tool", "tool": "get_bgp_summary", "vendor": "iosxr"},  # pe1 is junos → skipped
        ],
        "checks": {"severity_rules": {"warning": ["output=ok"]}, "evidence_fields": []},
    }])
    result, calls = run(
        "custom_iface",
        {"device": "pe1", "interface": "ge-0/0/1"},
        {
            "run_command": {"output": "ok"},
            "prometheus_prometheus_query": {"status": "success"},
            "syslog_syslog_search": {"lines": []},
        },
    )
    assert calls == [
        ("run_command", {"device": "pe1", "command": "show interfaces ge-0/0/1 extensive"}),
        ("prometheus_prometheus_query", {"query": 'up{instance="pe1"}'}),
        ("syslog_syslog_search", {"query": "pe1 ge-0/0/1", "limit": 20}),
    ]
    assert result.steps[3].skipped == "step is for cisco devices"
    assert result.severity == "Warning"
    store.delete_playbook("custom_iface")


def test_yaml_changes_are_reseeded_into_existing_db(tmp_path, monkeypatch):
    from playbooks import store

    yaml_path = tmp_path / "playbooks.yaml"
    yaml_path.write_text("- name: A\n  intent: a_intent\n  tools: [get_bgp_summary]\n")
    monkeypatch.setattr(store, "PLAYBOOK_YAML", str(yaml_path))
    monkeypatch.setattr(store, "PLAYBOOK_DB", str(tmp_path / "pb.db"))

    assert [p["title"] for p in store.list_playbooks()] == ["A"]
    yaml_path.write_text("- name: A (fixed)\n  intent: a_intent\n  tools: [get_bgp_summary]\n")
    assert [p["title"] for p in store.list_playbooks()] == ["A (fixed)"]


def test_run_playbook_reports_failure_when_every_step_failed(monkeypatch):
    from adapters.devices.dispatcher import ToolDispatcher

    d = ToolDispatcher()

    async def failing(tool, args):
        return {"error": "Cannot reach sandbox:22 (timed out after 5s)"}

    d.call_tool_unprefixed = failing
    result = asyncio.run(d._run_playbook({"intent": "cpu_high", "device": "xr1"}))
    assert result["severity"] == "Unknown"
    assert result["error"].startswith("All playbook steps failed: Cannot reach sandbox:22")


def test_playbook_steps_run_together_and_keep_their_order():
    import time as _time

    started = []

    async def slow_caller(tool, args):
        started.append(tool)
        await asyncio.sleep(0.3)
        return {"admin_status": "up", "oper_status": "down"} if tool == "get_interface_health" else {}

    t0 = _time.monotonic()
    result = asyncio.run(get_runner().run("link_down", {"device": "pe1", "interface": "ge-0/0/1"}, slow_caller))
    assert _time.monotonic() - t0 < 0.6                       # 3 steps × 0.3 s, run together
    assert [s.tool_name for s in result.steps] == ["get_interface_health", "check_link", "get_recent_changes"]
    assert result.severity == "Critical"


def test_identical_device_calls_are_shared_within_a_request(monkeypatch):
    from adapters.devices import dispatcher as dmod

    calls = []

    async def fake_adapter_call(self, tool_name, arguments, modules):
        calls.append(tool_name)
        await asyncio.sleep(0.05)
        return {"oper_status": "down"}

    monkeypatch.setattr(dmod.ToolDispatcher, "_call_adapter_tool", fake_adapter_call)
    d = dmod.ToolDispatcher()
    args = {"device": "pe1", "interface": "ge-0/0/1"}

    async def one_request():
        dmod.start_shared_calls()
        try:
            a, b = await asyncio.gather(d.call_tool("junos_get_interface_health", dict(args)),
                                        d.call_tool_unprefixed("get_interface_health", dict(args)))
            a["mutated"] = True                                   # each caller gets its own copy
            return a, b
        finally:
            dmod.end_shared_calls()

    a, b = asyncio.run(one_request())
    assert calls == ["junos_get_interface_health"] and "mutated" not in b
    asyncio.run(one_request())
    assert len(calls) == 2                                        # a new request asks the device again
    asyncio.run(d.call_tool("junos_get_interface_health", dict(args)))
    assert len(calls) == 3                                        # outside a request: never shared
