# NetCopilot

An AI assistant for network operations. Ask in plain English ("why is BGP to 10.0.0.2
down on pe1?") from the web UI, Slack or Teams, or let it investigate incoming alerts.
NetCopilot runs **read-only** diagnostics on Juniper Junos, Cisco IOS XR, Nokia SR OS and
Aruba AOS-CX devices, checks Prometheus, syslog (Loki) and recent configuration changes,
and answers with the evidence it collected. It never changes device configuration.

## What it can do

- **Diagnose:** interface, BGP, IS-IS/OSPF, routing, CPU/memory, chassis and optics checks
  per vendor, plus a read-only `show` command fallback.
- **Check both ends of a link:** `check_link` finds the far end via LLDP or the interface
  description and says which side is at fault.
- **Correlate with changes:** commit history and config diffs (Junos, IOS XR). Recent
  changes are the most common root cause.
- **Playbooks:** deterministic checks with severity rules (link down/flap, CRC, BGP, IS-IS,
  routing, CPU, hardware, config change), editable in the Admin UI.
- **Alerts:** Alertmanager / generic webhooks → Slack alert with *Investigate* and
  *Create ticket* buttons, optional automatic investigation posted in the alert's thread.
- **Tickets:** Jira ticket creation with the investigation findings.
- **History and audit:** every answer with the tools that ran and their (redacted) evidence;
  a full audit trail with CSV export.

## Quick start (local)

Requires Python 3.10+.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env        # set an LLM key (OPENAI_API_KEY or ANTHROPIC_API_KEY) and device credentials
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8001
```

Open http://127.0.0.1:8001. Without authentication configured, NetCopilot only serves
requests from this machine. Devices are listed in `devices.yaml`, or imported from
NetBox/Nautobot or a CSV file (Admin → Inventory).

```yaml
# devices.yaml
devices:
  pe1:   {host: 10.0.0.1, vendor: junos}
  xr1:   {host: 10.0.0.2, vendor: cisco, platform: iosxr}
  sr1:   {host: 10.0.0.3, vendor: nokia}
  sw1:   {host: 10.0.0.4, vendor: aruba, platform: aoscx}
```

## Access

| Mode | Configure | Use for |
|---|---|---|
| SSO (recommended) | `AUTH0_DOMAIN`, `AUTH0_CLIENT_ID`, `AUTH0_CLIENT_SECRET`, role/group claims | named users |
| API keys | `NETCOPILOT_API_KEYS="key1:admin,key2:noc,key3:noc-ticket"` | integrations, small teams (the UI asks for the key) |
| Local only | nothing | a laptop / jump host |

Roles: `noc` (diagnostics), `noc-ticket` (+ Jira), `admin` (+ settings, inventory,
playbooks, audit, security posture).

## Integrations

- **Slack:** create the app from `deploy/slack/app-manifest.yaml` (minimum scopes: `chat:write`,
  `commands`) and set `SLACK_BOT_TOKEN` and `SLACK_ALERT_CHANNEL`. Then either
  - **Socket Mode (recommended):** set `SLACK_APP_TOKEN` (app-level token, scope `connections:write`).
    NetCopilot connects out to Slack; no public URL, tunnel or inbound firewall rule. `/health` shows
    `"slack": {"mode": "socket", "connected": true}`.
  - **HTTP:** slash command → `POST /slack/command`, interactivity → `POST /slack/actions`, verified with
    `SLACK_SIGNING_SECRET`; NetCopilot must be reachable from Slack.
- **Teams:** outgoing webhook → `POST /teams/webhook`; set `TEAMS_HMAC_SECRET` (the security
  token Teams shows) and `TEAMS_RESULT_WEBHOOK_URL` for answers that take more than 5 s.
- **Alertmanager:**

  ```yaml
  receivers:
    - name: netcopilot
      webhook_configs:
        - url: https://netcopilot.example.com/alerts/alertmanager
          send_resolved: true
          http_config:
            authorization:
              credentials: <ALERT_INGEST_TOKEN>
  ```

  Devices are matched from the `device`, `hostname`, `host` or `instance` label (name or IP),
  and interfaces from `interface`, `ifName` or `ifDescr`. Set `AUTO_INVESTIGATE=true` to
  investigate automatically, capped by `AUTO_INVESTIGATE_MAX_PER_HOUR`.
- **Jira:** `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PROJECT_KEY`.
- **Prometheus / Loki:** `PROMETHEUS_URL`; `SYSLOG_URL` (Loki is detected automatically).
- **LLM:** `LLM_PROVIDER=openai|anthropic|local` (`local` = any OpenAI-compatible endpoint,
  e.g. LiteLLM → Bedrock; see `docs/bedrock_litellm.md`).

## Safety

Read-only enforcement, secret redaction before the LLM, encrypted secrets at rest, signed
webhooks, CSP/CSRF protection, audit trail and more are described in
**[docs/SECURITY.md](docs/SECURITY.md)**. Admin → Posture shows which guardrails are active
in a running deployment.

## Operating it

```bash
.venv/bin/python -m pytest -q tests          # unit/integration tests (no devices, no LLM)
.venv/bin/python -m evals.run_evals          # replay evals against your configured LLM (costs tokens)
curl -s localhost:8001/metrics               # Prometheus metrics incl. LLM token usage
```

Deployment: `docker-compose.yml`, `deploy/k8s/prod/` and `deploy/helm/`. Keep one replica,
because state is SQLite in `NETCOPILOT_DATA_DIR`.
