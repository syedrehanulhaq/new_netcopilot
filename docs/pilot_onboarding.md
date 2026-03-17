# NetCopilot Pilot Onboarding

This guide gets a pilot environment running quickly with a repeatable demo flow.

## 1) Prerequisites
- macOS/Linux with Python 3.10+
- Network access to target devices
- Slack app (optional for ChatOps)
- Jira project (optional for ticketing)
- Prometheus (optional for real flaps)
- LiteLLM + Bedrock (optional for on‑prem/cloud LLM)

## 2) Project setup
```bash
cd /Users/syedhaq/netcopilot
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## 3) Core env variables
Create `.env` (or update Settings UI):
```
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4.1-mini
LLM_PROVIDER=openai
CISCO_USER=...
CISCO_PASS=...
JUNIPER_USER=...
JUNIPER_PASS=...
SESSION_SECRET=change-me
```

## 4) Device inventory
Edit `devices.yaml`:
```
cisco_devices:
  sandbox-iosxr-1: sandbox-iosxr-1.cisco.com
juniper_devices:
  sandbox-junos-1: "66.129.234.203:45003"
  sandbox-junos-2: "66.129.234.203:45007"
nokia_devices: {}
```
After edits, go to Settings and click **Reload inventory**.

## 5) Run the API + UI
```bash
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8001
```
Open: `http://127.0.0.1:8001`

## 6) Optional: Slack
Set in `.env` or Settings UI:
```
SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...
SLACK_VERIFICATION_TOKEN=...
```
Ngrok (if needed):
```bash
ngrok http 8001
```
Slash command URL:
```
https://<ngrok-host>/slack/command
```

## 7) Optional: Jira
```
JIRA_BASE_URL=https://<your-domain>.atlassian.net
JIRA_EMAIL=...
JIRA_API_TOKEN=...
JIRA_PROJECT_KEY=SUP
JIRA_DEFAULT_ISSUE_TYPE=Task
JIRA_DEFAULT_PRIORITY=Medium
JIRA_DEFAULT_TEAM=Network_Operations
```

## 8) Optional: LiteLLM + Bedrock
Run LiteLLM:
```bash
.venv/bin/litellm --config litellm.yaml --port 4000
```
Set NetCopilot to use it:
```
LLM_PROVIDER=local
LOCAL_LLM_BASE_URL=http://127.0.0.1:4000/v1
LOCAL_LLM_MODEL=claude-haiku
```
Check:
```bash
curl -sS http://127.0.0.1:8001/health/llm
```

## 9) Demo flow (suggested)
1. Interface health:
   - “check interface health on sandbox-iosxr-1 Gi0/0/0/1”
2. Link health (LLDP + remote):
   - “check link health on sandbox-junos-1 ge-0/0/3”
3. BGP status:
   - “show bgp neighbor 192.0.2.1 on sandbox-iosxr-1”
4. Ticket creation:
   - “create a ticket for interface down on ge-0/0/3”
5. Slack alert ingest:
   - POST `/alerts/ingest` and run `/netcopilot investigate`

## 10) Troubleshooting
- `/health` → API status
- `/health/llm` → LLM connectivity
- `logs/netcopilot.log` → runtime logs
- `logs/audit.log` → tool calls + admin actions

## 11) Clean reset
```bash
rm -f memory.db alerts.db lldp_cache.db
```
