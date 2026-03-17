# Nautobot -> NetCopilot Inventory Webhook

NetCopilot can reload inventory automatically when devices change in Nautobot.

This avoids restarting NetCopilot whenever devices are added/updated in Nautobot.

## NetCopilot endpoint

- URL: `POST /webhooks/nautobot/inventory`
- Auth header: `X-Webhook-Token: <INVENTORY_WEBHOOK_TOKEN>`

Set these in NetCopilot Admin Settings:

- `INVENTORY_WEBHOOK_TOKEN` (required)
- `INVENTORY_WEBHOOK_DEBOUNCE_SECONDS` (optional, default 5)

## Nautobot setup (high-level)

1. In Nautobot, create a Webhook that triggers on Device create/update/delete.
2. Set the webhook target URL to your NetCopilot base URL plus:
   - `/webhooks/nautobot/inventory`
3. Add the HTTP header:
   - `X-Webhook-Token: <your token>`

When Nautobot triggers the webhook, NetCopilot will call `reload_inventory_devices()` and pick up:

- New/updated devices from Nautobot
- Devices from `devices.yaml` (if you also use YAML inventory)

