# Nautobot CSV Import (Scripted)

This script imports devices into Nautobot from a CSV file and sets a primary IP via a `mgmt0` interface.

## 1) Prepare Nautobot objects

Create these in Nautobot UI first:
- Sites (e.g., `Lab`)
- Device Roles (e.g., `Core`)
- Manufacturers (e.g., `Cisco`, `Juniper`, `Nokia`)
- Device Types (e.g., `VMX`, `IOSXRv`, `SR-OS`) under the correct manufacturer

## 2) Prepare CSV

Use the sample file as a template:
`docs/nautobot_devices_sample.csv`

Required columns:
- `name`
- `site`
- `role`
- `manufacturer`
- `device_type`
- `primary_ip4`

## 3) Run import

```
python3 scripts/nautobot_import_csv.py \
  --url http://127.0.0.1:8090 \
  --token YOUR_NAUTOBOT_TOKEN \
  --csv docs/nautobot_devices_sample.csv
```

## 4) Reload NetCopilot inventory

In Admin Settings → **Reload inventory**

## Notes
- The script creates a `mgmt0` interface and assigns the primary IP there.
- If the device already exists, it will be reused.
- If a required object is missing, the script exits with an error.

