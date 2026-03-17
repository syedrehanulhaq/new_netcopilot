# NetCopilot Executive Summary (1‑Page)

## What it is
NetCopilot is an AI‑powered network operations assistant that turns plain‑English questions into verified, evidence‑based troubleshooting answers. It connects to existing network devices and tools, collects the right diagnostic data, and explains the result clearly.

## Why it matters
- **Faster MTTR:** reduces manual CLI digging and speeds triage.
- **Consistency:** applies standard troubleshooting playbooks every time.
- **Evidence‑first answers:** outputs cite real device data, not guesses.
- **Safety:** read‑only by design; no configuration changes.

## How it works (high level)
1) Engineer asks a question.  
2) NetCopilot selects the correct diagnostic checks.  
3) It gathers evidence from devices and tools.  
4) It returns a clear diagnosis + next steps.

## Key capabilities (current MVP)
- Multi‑vendor device checks (IOS XR, Junos; Nokia planned).
- Interface health, counters, link health with LLDP neighbor validation.
- Routing and protocol checks (BGP/ISIS/OSPF, route lookup).
- Alert → Slack → investigate workflow.
- Jira ticket creation and updates.
- Admin settings and inventory management.

## Security posture
- Runs inside customer environment.
- Uses existing access controls and credentials.
- Logs interactions for auditability.
-
Read‑only mode prevents unsafe configuration changes.

## Pilot value
NetCopilot provides immediate ROI by accelerating investigations and reducing human error during triage, while keeping data and control within the customer’s environment.

