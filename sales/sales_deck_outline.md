# NetCopilot Sales Deck (8-10 slides)

## Slide 1 — Title
NetCopilot  
AI-Powered Network Operations Copilot

Subtitle: Automate troubleshooting, accelerate incident response, and reduce MTTR.


## Slide 2 — The Problem
- NOC teams spend hours on repetitive CLI checks.
- Alerts lack context; tickets lack evidence.
- Multi-vendor environments slow response.
- Security/compliance blocks public AI tools.


## Slide 3 — The Solution
NetCopilot turns natural-language questions into trusted, evidence-based answers.
- Runs approved tools (CLI, inventory, telemetry, ticketing).
- Returns raw evidence + structured diagnosis.
- Deployable on-prem or private cloud.


## Slide 4 — Architecture (Diagram)
Flow: User channels → NetCopilot API → LLM → Tools → Devices/Systems → LLM → Answer
- Channels: Web, Slack, Teams
- Orchestrator: FastAPI + tool registry
- LLM: OpenAI / Azure / Bedrock / Local
- Tools: IOS XR, Junos, NetBox, Prometheus, Jira
- Storage: Memory + audit logs


## Slide 5 — Key Use Cases
- Interface down / flapping
- BGP idle/active
- ISIS/OSPF neighbor down
- CRC errors / drops / MTU mismatch
- Routing missing
- High CPU / memory
- Raise or update a ticket with evidence


## Slide 6 — Demo Workflow
1) Ask: "Check interface health on sandbox-iosxr-1 Gi0/0/0/0"
2) Tool runs show commands
3) NetCopilot returns status, errors, counters, recommended actions
4) Optionally creates a Jira ticket


## Slide 7 — Why It’s Different
- Evidence-based answers (not just text).
- Multi-vendor tools with guardrails.
- Security-first: on-prem or private LLM.
- Integrations: Slack/Teams/Jira/NetBox/Prometheus.
- RBAC + audit logging.


## Slide 8 — Deployment Options
- On-prem (local LLM or customer-approved model).
- Private cloud (Azure OpenAI, Bedrock, Vertex).
- Hybrid (LLM outside, tools inside).


## Slide 9 — Business Impact
- Faster MTTR.
- Reduced manual CLI work.
- Consistent troubleshooting quality.
- Lower training burden.
- Audit/compliance ready.


## Slide 10 — Next Steps
- 2-week POC with 3–5 devices.
- Integrate inventory + telemetry.
- Define top 10 playbooks.
- Review pricing and rollout plan.
