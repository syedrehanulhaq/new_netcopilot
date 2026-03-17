# NetCopilot Architecture (Detailed)

## 1) Entry points (how users interact)
- **Web UI** (`/`): The browser UI posts chat requests to the API.
- **Slack / ChatOps**: Slash commands call the API for investigations.
- **Direct API**: `/chat` for natural language requests; other endpoints for alerts and inventory.

## 2) Core API (FastAPI)
The FastAPI service is the “brain” that orchestrates everything:
- **Auth + RBAC**: Determines user role (admin/noc) and which tools are allowed.
- **Rate limits**: Prevents overload.
- **Audit logging**: Every request/tool call is logged.
- **Settings + inventory**: Admin UI writes configuration to a local DB.

## 3) LLM Router (OpenAI / Bedrock / Local)
NetCopilot uses a provider‑agnostic adapter:
- **LLM provider can switch** (OpenAI, Bedrock, local models).
- The model is only asked to **decide** and **summarize**.
- It uses **tool calling**, so the model can request “real checks” instead of guessing.

## 4) Tools + Adapters (real evidence)
Tools are the actions the LLM can request. Each tool calls an adapter:
- **Device adapters**: Cisco IOS XR, Junos, Nokia SR OS.
- **Inventory adapters**: YAML, NetBox, Nautobot.
- **Ticketing**: Jira.
- **Monitoring**: Prometheus.
- **Chat**: Slack / Teams.

Adapters abstract vendor differences so the rest of the system stays consistent.

## 5) Data storage
- **settings.db**: Admin settings (LLM provider, tokens, etc.).
- **memory.db**: Optional short chat memory.
- **alerts.db**: Alert ingestion for Slack investigations.
- **logs/**: Structured JSON logs + audit.

## 6) Read‑only policy (safety)
The system prompt forbids configuration changes. If a user asks for config, NetCopilot refuses and suggests a ticket.

## 7) End‑to‑end flow (example)
User: “Check link health on sandbox‑junos‑1 ge‑0/0/3.”

1) API receives `/chat` request.
2) LLM requests `get_link_health(device, interface)`.
3) Junos adapter runs live device checks.
4) Tool returns structured data.
5) LLM summarizes with evidence + next steps.

## 8) Security posture
- **RBAC**: Admin vs noc controls.
- **Tool allowlist/denylist**.
- **Audit logs** with redaction.
- **Rate limits**.

