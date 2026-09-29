import yaml
from fastapi.testclient import TestClient

from conftest import DEVICES_YAML


def test_aruba_devices_are_routable():
    from adapters.devices.registry import get_vendor, resolve_host

    assert get_vendor("sw1") == "aruba"
    assert resolve_host("sw1") == ("10.0.0.4", 2222)


def test_inventory_sources_are_merged_and_yaml_wins(monkeypatch):
    import config

    def fake_load(**kwargs):
        vendor = kwargs["registry_key"].replace("_devices", "")
        return {"cisco": {"nb-xr9": "192.0.2.9", "xr1": "192.0.2.99"}}.get(vendor, {})

    monkeypatch.setattr(config, "load_inventory_devices", fake_load)
    monkeypatch.setenv("NETBOX_URL", "https://netbox.example")
    counts = config.reload_inventory_devices()

    from adapters.devices.registry import get_vendor, resolve_host
    assert get_vendor("nb-xr9") == "cisco"             # discovered device is now routable
    assert resolve_host("xr1") == ("10.0.0.2", None)   # devices.yaml entry wins
    assert counts["cisco"] == 2


def test_admin_inventory_lists_all_vendors():
    import app as appmod

    with TestClient(appmod.app) as client:
        data = client.get("/admin/inventory").json()
    assert data["counts"] == {"junos": 1, "cisco": 1, "nokia": 1, "aruba": 1}
    assert {d["name"] for d in data["devices"]} == {"pe1", "xr1", "sr1", "sw1"}


def test_csv_import_merges_without_deleting_existing_devices():
    import app as appmod
    from adapters.devices.registry import get_vendor

    csv_body = (
        "device,vendor,ip,port,platform,site\n"
        "new-xr,cisco,10.9.9.9,,iosxr,lab\n"
        "new-sw,aruba,10.9.9.10,22,aoscx,\n"
        "xr1,cisco,10.0.0.22,,,\n"
        "bad name!,cisco,10.1.1.1,,,\n"
    )
    with TestClient(appmod.app) as client:
        r = client.post("/admin/inventory/import-csv", files={"file": ("d.csv", csv_body, "text/csv")})
    body = r.json()
    assert r.status_code == 200, body
    assert (body["added"], body["updated"], body["skipped"]) == (2, 1, 1)

    saved = yaml.safe_load(open(DEVICES_YAML))["devices"]
    assert set(saved) == {"pe1", "xr1", "sr1", "sw1", "new-xr", "new-sw"}  # nothing lost
    assert saved["xr1"]["host"] == "10.0.0.22"
    assert saved["new-xr"] == {"host": "10.9.9.9", "vendor": "cisco", "platform": "iosxr", "site": "lab"}
    # live registry updated without a restart
    assert get_vendor("new-sw") == "aruba"


def test_find_device_never_guesses_between_devices_sharing_an_address():
    from adapters.devices import registry
    from conftest import write_devices

    write_devices({"devices": {
        "pe1": {"host": "192.0.2.10", "port": 33045, "vendor": "junos"},
        "p5": {"host": "192.0.2.10", "port": 33033, "vendor": "junos"},
        "xr1": {"host": "192.0.2.20", "vendor": "cisco"},
    }})
    registry.reload_registry()
    assert registry.find_device("192.0.2.20:9116") == "xr1"      # unique address
    assert registry.find_device("192.0.2.10:9116") is None       # ambiguous → no guess
    assert registry.find_device("192.0.2.10:33033") == "p5"      # port disambiguates
    assert registry.find_device("PE1.lab.example.net") == "pe1"  # FQDN → short name


def test_junos_uses_the_devices_netconf_port(monkeypatch):
    from jnpr.junos import Device

    from adapters.devices import junos, registry
    from conftest import write_devices

    write_devices({"devices": {
        "vmx": {"host": "192.0.2.30", "port": 37002, "netconf_port": 37000, "vendor": "junos"},
        "mx": {"host": "192.0.2.31", "port": 33033, "vendor": "junos"},
        "srx": {"host": "192.0.2.32", "vendor": "junos"},
    }})
    registry.reload_registry()
    monkeypatch.setenv("JUNIPER_USER", "u")
    monkeypatch.setenv("JUNIPER_PASS", "p")
    ports = []

    def _record(self, *args, **kwargs):
        ports.append(self._port)
        raise RuntimeError("stop before connecting")

    monkeypatch.setattr(Device, "open", _record)
    for name in ("vmx", "mx", "srx"):
        try:
            with junos._connect(name):
                pass
        except Exception:
            pass
    assert ports == [37000, 33033, 830]  # netconf_port, else port, else NETCONF_PORT default
