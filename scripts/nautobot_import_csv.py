#!/usr/bin/env python3
import argparse
import csv
import sys
from typing import Any

import httpx


def _get_single(client: httpx.Client, base_url: str, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    resp = client.get(f"{base_url}{endpoint}", params=params)
    resp.raise_for_status()
    data = resp.json()
    results = data.get("results") or []
    return results[0] if results else {}


def _get_required(client: httpx.Client, base_url: str, endpoint: str, name: str) -> dict[str, Any]:
    item = _get_single(client, base_url, endpoint, {"name": name})
    if not item:
        raise RuntimeError(f"Missing required object '{name}' at {endpoint}")
    return item


def _get_or_create_interface(
    client: httpx.Client, base_url: str, device_id: str, name: str
) -> dict[str, Any]:
    existing = _get_single(
        client, base_url, "/api/dcim/interfaces/", {"device_id": device_id, "name": name}
    )
    if existing:
        return existing
    payload = {"device": device_id, "name": name, "type": "virtual"}
    resp = client.post(f"{base_url}/api/dcim/interfaces/", json=payload)
    resp.raise_for_status()
    return resp.json()


def _get_or_create_device(
    client: httpx.Client,
    base_url: str,
    name: str,
    site_id: str,
    role_id: str,
    device_type_id: str,
) -> dict[str, Any]:
    existing = _get_single(client, base_url, "/api/dcim/devices/", {"name": name})
    if existing:
        return existing
    payload = {
        "name": name,
        "status": "active",
        "site": site_id,
        "role": role_id,
        "device_type": device_type_id,
    }
    resp = client.post(f"{base_url}/api/dcim/devices/", json=payload)
    resp.raise_for_status()
    return resp.json()


def _set_primary_ip(
    client: httpx.Client,
    base_url: str,
    device: dict[str, Any],
    interface_id: str,
    address: str,
) -> None:
    if not address:
        return
    ip_payload = {
        "address": address,
        "status": "active",
        "assigned_object_type": "dcim.interface",
        "assigned_object_id": interface_id,
    }
    resp = client.post(f"{base_url}/api/ipam/ip-addresses/", json=ip_payload)
    resp.raise_for_status()
    ip_obj = resp.json()
    device_id = device.get("id")
    patch = {"primary_ip4": ip_obj.get("id")}
    resp = client.patch(f"{base_url}/api/dcim/devices/{device_id}/", json=patch)
    resp.raise_for_status()


def main() -> int:
    parser = argparse.ArgumentParser(description="Import Nautobot devices from CSV.")
    parser.add_argument("--url", required=True, help="Nautobot base URL, e.g. http://127.0.0.1:8090")
    parser.add_argument("--token", required=True, help="Nautobot API token")
    parser.add_argument("--csv", required=True, help="Path to CSV file")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")
    headers = {"Authorization": f"Token {args.token}"}

    with open(args.csv, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    if not rows:
        print("CSV is empty.", file=sys.stderr)
        return 1

    with httpx.Client(headers=headers, timeout=20) as client:
        for row in rows:
            name = (row.get("name") or row.get("device") or "").strip()
            site = (row.get("site") or "").strip()
            role = (row.get("role") or "").strip()
            device_type = (row.get("device_type") or row.get("type") or "").strip()
            manufacturer = (row.get("manufacturer") or "").strip()
            primary_ip4 = (row.get("primary_ip4") or row.get("ip") or "").strip()

            if not all([name, site, role, device_type, manufacturer]):
                raise RuntimeError(f"Missing required fields for device '{name}': {row}")

            site_obj = _get_required(client, base_url, "/api/dcim/sites/", site)
            role_obj = _get_required(client, base_url, "/api/dcim/device-roles/", role)
            manufacturer_obj = _get_required(
                client, base_url, "/api/dcim/manufacturers/", manufacturer
            )
            device_type_obj = _get_single(
                client,
                base_url,
                "/api/dcim/device-types/",
                {"manufacturer_id": manufacturer_obj["id"], "model": device_type},
            )
            if not device_type_obj:
                raise RuntimeError(
                    f"Device type '{device_type}' not found for manufacturer '{manufacturer}'"
                )

            device = _get_or_create_device(
                client,
                base_url,
                name=name,
                site_id=site_obj["id"],
                role_id=role_obj["id"],
                device_type_id=device_type_obj["id"],
            )
            iface = _get_or_create_interface(client, base_url, device["id"], "mgmt0")
            _set_primary_ip(client, base_url, device, iface["id"], primary_ip4)
            print(f"Imported {name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
