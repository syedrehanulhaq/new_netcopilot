# NetCopilot — One‑Page Executive Summary

## What it is
NetCopilot is an AI‑powered network operations copilot that turns natural‑language questions into evidence‑backed troubleshooting results. It integrates directly with network devices and operational systems to reduce MTTR and improve NOC consistency.

## Why it matters
- NOC teams spend hours on manual CLI checks.
- Alerts lack context; tickets lack evidence.
- Multi‑vendor networks increase troubleshooting time.
- Security and compliance block public AI tools.

## What it does
- Runs real tools (CLI/NETCONF, inventory, telemetry, ticketing)
- Returns raw evidence + structured diagnosis
- Uses role‑based access and audit logs
- Deploys on‑prem, private cloud, or hybrid

## Key use cases
- Interface down / flapping
- BGP idle or flapping
- ISIS/OSPF neighbors down
- CRC errors, drops, MTU mismatch
- Routing missing
- High CPU / memory
- Create or update a ticket with evidence

## Architecture (summary)
Channels → NetCopilot API → LLM → Tools → Devices/Systems → Answer  
Channels: Web, Slack, Teams  
Tools: IOS XR, Junos, NetBox, Prometheus, Jira  
Storage: Memory + audit logs

## Deployment options
1) On‑prem with local or customer‑approved LLM  
2) Private cloud (Azure OpenAI / Bedrock / Vertex)  
3) Hybrid (LLM outside, tools inside)

## Business impact
- Faster MTTR  
- Reduced manual CLI work  
- Consistent troubleshooting quality  
- Lower training burden  
- Audit/compliance ready

## Next steps (POC)
1) Integrate 3–5 devices  
2) Define top 10 playbooks  
3) Connect inventory + telemetry  
4) Run pilot with NOC team
