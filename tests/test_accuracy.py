import asyncio

from fastapi.testclient import TestClient

from adapters.devices import parsers
from adapters.devices.dispatcher import ToolDispatcher


# ---------------------------------------------------------------------------
# check_link: both ends of a link
# ---------------------------------------------------------------------------

def _dispatcher_with(responses, calls):
    d = ToolDispatcher()

    async def fake_unprefixed(tool, args):
        calls.append((tool, args))
        key = (tool, args.get("device"))
        result = responses.get(key, {"error": "not found", "unsupported": True})
        if isinstance(result, Exception):
            raise result
        return result

    d.call_tool_unprefixed = fake_unprefixed
    return d


def test_check_link_finds_far_end_via_lldp_fqdn_and_flags_asymmetry():
    calls = []
    d = _dispatcher_with({
        ("get_interface_health", "pe1"): {"admin_status": "up", "oper_status": "down", "crc_errors": 0},
        ("get_lldp_neighbor", "pe1"): {"neighbor_device": "XR1.lab.example.net", "neighbor_interface": "Gi0/0/0/1"},
        ("get_interface_health", "xr1"): {"admin_status": "up", "oper_status": "up", "crc_errors": 42},
    }, calls)
    result = asyncio.run(d._check_link({"device": "pe1", "interface": "ge-0/0/1"}))
    assert result["far_end"]["inventory_device"] == "xr1"
    assert result["far_end"]["identified_by"] == "lldp"
    text = " ".join(result["assessment"])
    assert "Asymmetric: local down, far end up" in text
    assert "CRC errors on the xr1 (42)" in text
    assert ("get_interface_health", {"device": "xr1", "interface": "Gi0/0/0/1"}) in calls


def test_check_link_uses_description_when_lldp_is_gone():
    calls = []
    d = _dispatcher_with({
        ("get_interface_health", "pe1"): {"admin_status": "up", "oper_status": "down",
                                          "description": "Core link to sr1 1/1/3"},
        ("get_lldp_neighbor", "pe1"): {"error": "No LLDP neighbor found on this interface."},
        ("get_interface_health", "sr1"): {"admin_status": "up", "oper_status": "down"},
    }, calls)
    result = asyncio.run(d._check_link({"device": "pe1", "interface": "ge-0/0/1"}))
    assert result["far_end"] == {"lldp_name": None, "inventory_device": "sr1", "interface": "1/1/3",
                                 "identified_by": "interface description"}
    assert any("Both ends are down" in note for note in result["assessment"])


def test_check_link_validates_untrusted_lldp_values():
    # A malicious LLDP port-id must not reach the far-end device CLI.
    d = ToolDispatcher()
    called = []

    async def fake_adapter(tool_name, arguments, module_map):
        called.append((tool_name, arguments))
        if tool_name == "junos_get_lldp_neighbor":
            return {"neighbor_device": "xr1", "neighbor_interface": "Gi0/0/0/1\nreload"}
        return {"admin_status": "up", "oper_status": "down"}

    d._call_adapter_tool = fake_adapter
    result = asyncio.run(d._check_link({"device": "pe1", "interface": "ge-0/0/1"}))
    assert "Rejected unsafe input" in result["remote"]["error"]
    assert not any(name.startswith("cisco_") for name, _ in called)


# ---------------------------------------------------------------------------
# Text parsers (Nokia SR OS, Aruba AOS-CX)
# ---------------------------------------------------------------------------

NOKIA_PORT = """
===============================================================================
Ethernet Interface
===============================================================================
Description        : 10-Gig Ethernet to pe1
Interface          : 1/1/1                      Oper Speed       : 10 Gbps
Link-level         : Ethernet                   Config Speed     : 10 Gbps
Admin State        : up                         Oper Duplex      : full
Oper State         : down                       Config Duplex    : full
Physical Link      : No                         MTU              : 9212
Last State Change  : 09/26/2026 10:11:12        Hold time up     : 0 seconds
"""

NOKIA_BGP = """
Peer  : 10.0.0.2
Group : IBGP
-------------------------------------------------------------------------------
Peer AS              : 65000            Peer Port            : 0
State                : Active           Last State           : Established
Last Event           : holdTimerExpired
Last Error           : Hold Timer Expire
"""

ARUBA_IF = """
Interface 1/1/1 is down
 Admin state is up
 State information: Waiting for link
 Link transitions: 7
 Description: uplink to core
 MTU 1500
 Statistic            RX                   TX                   Total
 ---------------- -------------------- -------------------- --------------------
 Packets                          1234                 5678                 6912
 Errors                              3                    0                    3
 CRC/FCS                             3                  n/a                    3
"""

ARUBA_BGP = """
VRF : default
BGP Summary
-----------
 Local AS               : 65001        BGP Router Identifier  : 10.1.1.1
 Peers                  : 2            Log Neighbor Changes   : No
 Cfg. Hold Time         : 180          Cfg. Keep Alive        : 60

 Address-family : IPv4 Unicast
 -----------------------------
 Neighbor                             Remote-AS MsgRcvd MsgSent   Up/Down Time State        AdminStatus
 10.0.0.1                             65000       120     118     01h:02m:03s  Established  Up
 10.0.0.9                             65009       0       3       00h:00m:10s  Active       Up
"""


def test_nokia_parsers():
    port = parsers.parse_nokia_port_detail(NOKIA_PORT)
    assert port["admin_status"] == "up" and port["oper_status"] == "down"
    assert port["last_flapped"] == "09/26/2026 10:11:12"
    assert port["speed"] == "10 Gbps"
    bgp = parsers.parse_nokia_bgp_neighbor(NOKIA_BGP)
    assert bgp["state"] == "Active" and bgp["last_state"] == "Established"
    assert bgp["last_error"] == "Hold Timer Expire" and bgp["peer_as"] == "65000"


def test_aruba_parsers():
    iface = parsers.parse_aruba_interface(ARUBA_IF)
    assert iface == {"oper_status": "down", "admin_status": "up", "carrier_transitions": 7,
                     "description": "uplink to core", "mtu": 1500,
                     "input_errors": 3, "output_errors": 0, "crc_errors": 3}
    peers = parsers.parse_aruba_bgp_summary(ARUBA_BGP)
    assert [(p["address"], p["state"]) for p in peers] == [("10.0.0.1", "Established"), ("10.0.0.9", "Active")]


def test_parsers_never_invent_fields_from_unrelated_output():
    assert parsers.parse_nokia_port_detail("MINOR: CLI Invalid port.") == {}
    assert parsers.parse_aruba_interface("% Invalid input") == {}


def test_nokia_adapter_exposes_parsed_fields(monkeypatch):
    from adapters.devices import nokia

    monkeypatch.setattr(nokia, "_cli", lambda device, cmd: NOKIA_PORT if cmd.startswith("show port") else "")
    result = nokia._sync_get_interface_health("sr1", "1/1/1")
    assert result["admin_status"] == "up" and result["oper_status"] == "down"
    assert "Oper State" in result["port"]["output"]  # raw output kept


def test_aruba_bgp_neighbor_state_comes_from_summary(monkeypatch):
    from adapters.devices import aruba

    monkeypatch.setattr(aruba, "_cli", lambda device, cmd: ARUBA_BGP if cmd.endswith("summary") else "detail")
    assert aruba._sync_get_bgp_neighbor_status("sw1", "10.0.0.9")["state"] == "Active"
    assert aruba._sync_get_bgp_summary("sw1")["summary"] == {"total": 2, "established": 1, "down": 1}


# ---------------------------------------------------------------------------
# Change correlation (IOS XR / Junos)
# ---------------------------------------------------------------------------

XR_COMMITS = """
SNo. Label/ID              User      Line                Client      Time Stamp
~~~~ ~~~~~~~~              ~~~~      ~~~~                ~~~~~~      ~~~~~~~~~~
1    1000000226            alice     vty0:node0_RP0_CPU0 CLI         Sat Sep 26 09:58:01 2026
2    1000000225            bob       vty1:node0_RP0_CPU0 CLI         Fri Sep 25 17:01:02 2026
"""


def test_xr_recent_changes_and_diff(monkeypatch):
    from adapters.devices import cisco

    sent = []

    def fake_cli(device, cmd):
        sent.append(cmd)
        return XR_COMMITS if cmd == "show configuration commit list" else "+ router bgp 65000 neighbor 10.0.0.2 shutdown"

    monkeypatch.setattr(cisco, "_cli", fake_cli)
    changes = cisco._sync_get_recent_changes("xr1", count=1)
    assert changes["last_change"] == {"commit_id": "1000000226", "user": "alice", "client": "CLI",
                                      "line": "vty0:node0_RP0_CPU0", "time": "Sat Sep 26 09:58:01 2026"}
    assert len(changes["changes"]) == 1
    diff = cisco._sync_get_config_diff("xr1", "1000000226")
    assert sent[-1] == "show configuration commit changes 1000000226" and "shutdown" in diff["diff"]
    assert "error" in cisco._sync_get_config_diff("xr1", "1; reload")


def test_junos_recent_changes_and_diff(monkeypatch):
    from contextlib import contextmanager

    from lxml import etree

    from adapters.devices import junos

    commits = etree.fromstring(
        "<commit-information>"
        "<commit-history><sequence-number>0</sequence-number><user>alice</user><client>cli</client>"
        "<date-time>2026-09-26 09:58:01 UTC</date-time><log>bgp maintenance</log></commit-history>"
        "<commit-history><sequence-number>1</sequence-number><user>bob</user><client>netconf</client>"
        "<date-time>2026-09-25 17:01:02 UTC</date-time></commit-history>"
        "</commit-information>"
    )
    diff = etree.fromstring(
        "<configuration-information><configuration-output>[edit protocols bgp group IBGP]\n"
        "-    neighbor 10.0.0.2;</configuration-output></configuration-information>"
    )
    seen = {}

    class _Rpc:
        def get_commit_information(self):
            return commits

        def get_configuration(self, options=None):
            seen["options"] = options
            return diff

    class _Dev:
        rpc = _Rpc()

    @contextmanager
    def fake_connect(device):
        yield _Dev()

    monkeypatch.setattr(junos, "_connect", fake_connect)
    changes = junos._sync_get_recent_changes("pe1")
    assert changes["last_change"]["user"] == "alice" and changes["last_change"]["comment"] == "bgp maintenance"
    result = junos._sync_get_config_diff("pe1", 2)
    assert seen["options"] == {"compare": "rollback", "rollback": "2", "format": "text"}
    assert "neighbor 10.0.0.2" in result["diff"]
    assert "error" in junos._sync_get_config_diff("pe1", 99)


# ---------------------------------------------------------------------------
# Loki syslog backend
# ---------------------------------------------------------------------------

class _LokiResponse:
    status_code = 200

    def __init__(self, streams):
        self._streams = streams
        self.text = ""

    def json(self):
        return {"status": "success", "data": {"resultType": "streams", "result": self._streams}}


def test_loki_backend_builds_logql_and_sorts_lines(monkeypatch):
    from adapters.integrations import syslog

    monkeypatch.setenv("SYSLOG_URL", "http://loki:3100")
    captured = []
    streams = [
        {"stream": {"host": "pe1", "job": "syslog", "filename": "/var/log/x"},
         "values": [["1700000000000000000", "older line"], ["1700000100000000000", "%LINK-3-UPDOWN ge-0/0/1 down"]]},
    ]

    def fake_get(url, params=None, headers=None, timeout=None):
        captured.append((url, params))
        return _LokiResponse(streams)

    monkeypatch.setattr(syslog.httpx, "get", fake_get)
    result = syslog._sync_syslog_search_interface("pe1", 'ge-0/0/1"', limit=10)
    url, params = captured[-1]
    assert url == "http://loki:3100/loki/api/v1/query_range"
    assert params["query"] == '{host="pe1"} |= "ge-0/0/1\\""'
    assert params["direction"] == "backward"
    assert [l["line"] for l in result["lines"]] == ["%LINK-3-UPDOWN ge-0/0/1 down", "older line"]
    assert result["lines"][0]["labels"] == {"host": "pe1", "job": "syslog"}


def test_loki_device_search_falls_back_to_line_match(monkeypatch):
    from adapters.integrations import syslog

    monkeypatch.setenv("SYSLOG_URL", "http://loki.internal/")
    queries = []

    def fake_get(url, params=None, headers=None, timeout=None):
        queries.append(params["query"])
        return _LokiResponse([] if len(queries) == 1 else [{"stream": {}, "values": [["1", "pe1 BGP down"]]}])

    monkeypatch.setattr(syslog.httpx, "get", fake_get)
    result = syslog._sync_syslog_search_device("pe1")
    assert queries == ['{host="pe1"}', '{job=~".+"} |= "pe1"']
    assert result["count"] == 1


# ---------------------------------------------------------------------------
# Token usage + Prometheus metrics
# ---------------------------------------------------------------------------

def test_token_usage_is_metered_and_audited(monkeypatch):
    import app as appmod
    from test_chat import FakeLLM, _Msg

    class UsageLLM(FakeLLM):
        def chat(self, **kwargs):
            resp = super().chat(**kwargs)
            resp.usage = type("U", (), {"prompt_tokens": 1200, "completion_tokens": 80})()
            return resp

    monkeypatch.setattr(appmod, "get_llm_adapter", lambda: UsageLLM([_Msg("all good")]))
    with TestClient(appmod.app) as client:
        before = client.get("/metrics", params={"format": "json"}).json()["llm_prompt_tokens_total"]
        client.post("/chat", json={"message": "how is pe1?", "user": "metrics-user"})
        after = client.get("/metrics", params={"format": "json"}).json()
        text = client.get("/metrics").text
        events = client.get("/admin/audit", params={"event": "chat_end", "user": "metrics-user"}).json()["events"]
    assert after["llm_prompt_tokens_total"] - before == 1200
    assert 'netcopilot_llm_tokens_total{type="completion"}' in text
    assert "# TYPE netcopilot_tool_calls_total counter" in text
    assert events[0]["detail"]["prompt_tokens"] == 1200 and events[0]["detail"]["completion_tokens"] == 80


def test_metrics_token(monkeypatch):
    import app as appmod

    monkeypatch.setenv("METRICS_TOKEN", "m-tok")
    with TestClient(appmod.app) as client:
        assert client.get("/metrics").status_code == 401
        assert client.get("/metrics", headers={"Authorization": "Bearer m-tok"}).status_code == 200
