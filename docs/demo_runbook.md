# NetCopilot Demo Runbook (Lab/Pilot)

Use this runbook to drive a clean demo in a lab environment. It assumes the
FastAPI server, Slack app, and Jira integration are already configured.

## 0) Preflight (5 minutes)

1) Start services
```
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8001
```
```
.venv/bin/litellm --config litellm.yaml --port 4000
```

2) Health checks
```
curl -sS http://127.0.0.1:8001/health
curl -sS http://127.0.0.1:8001/health/llm
```
Expected: `status=ok` on API and a healthy LLM provider.

3) Auth check
```
curl -sS http://127.0.0.1:8001/auth/me
```
Expected: `email`, `name`, and `role` present after login.

## 1) UI Walkthrough (2 minutes)

- Open `http://127.0.0.1:8001/`.
- Confirm logo, sign-in/out, gear (settings) button.
- Verify reply panel and message box are visible.

## 2) Interface Health (Junos) (5 minutes)

Ask:
```
Check interface health on sandbox-junos-1 ge-0/0/3
```
Expected:
- Local admin/oper state
- Errors/CRC, flaps, and alarms
- Diagnosis + checklist
- No configuration commands

If LLDP is configured:
```
Check link health on sandbox-junos-1 ge-0/0/3
```
Expected:
- Local + remote validation
- Neighbor interface name and state

## 3) Interface Health (IOS XR) (3 minutes)

Ask:
```
Check interface health on sandbox-iosxr-1 Gi0/0/0/1
```
Expected:
- IOS XR status + controllers
- Errors and oper state

## 4) Flapping Links (Prometheus-backed) (3 minutes)

Ask:
```
Show top 5 flapping links in the last 24 hours
```
Expected:
- Data from Prometheus if configured
- If not configured, confirm it is labeled as mock in the response

## 5) Alert → Slack → Investigate (5 minutes)

Trigger alert:
```
curl -sS -X POST http://127.0.0.1:8001/alerts/ingest \
  -H "Content-Type: application/json" \
  -H "X-Alert-Token: YOUR_ALERT_TOKEN" \
  -d '{
    "text": "Interface down detected",
    "device": "sandbox-junos-1",
    "interface": "ge-0/0/4",
    "severity": "critical",
    "channel": "all-netcopilot"
  }'
```
Expected:
- Slack alert posted in `#all-netcopilot`
- Use `/netcopilot investigate` to trigger analysis
- Response includes status/evidence/likely cause/next steps

## 6) Ticket Creation (Jira) (3 minutes)

Ask:
```
Create a ticket for the interface down on sandbox-junos-1 ge-0/0/4
```
Expected:
- Ticket created with a real Jira key
- Summary and description populated with alert context

## 7) RBAC & Settings (2 minutes)

- Login as admin → open settings
- Verify you can save settings and reload inventory
- Login as noc → settings hidden or locked

## 8) Demo Wrap (1 minute)

Recap:
- Natural language → tool execution → evidence-based response
- Slack alert workflow
- Jira ticket creation
- Multi-vendor capability

## Common Issues & Fixes

- **LLM degraded**: check LiteLLM is running and credentials are set.
- **Slack errors**: ensure bot is in channel + scopes set.
- **Jira 400**: validate project key + issue type.
- **No LLDP neighbor**: enable LLDP on both ends or validate neighbor interface.

