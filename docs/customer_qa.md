# Customer Q&A (NetCopilot)

## What problem does this solve?
It reduces mean time to resolution by automating the “collect evidence” step and summarizing findings in plain English.

## Is it safe? Will it change configurations?
No. It is **read‑only** by design and refuses to provide or execute configuration commands.

## Where does it run?
Inside the customer’s environment (on‑prem or in their cloud/VPC). Data does not need to leave their environment.

## Does it need internet access?
Only for the selected LLM provider (or none if using a local/on‑prem model).

## How does it get data?
It uses standard vendor access methods (SSH/Netconf) and existing tools (Prometheus, Slack, Jira, etc.).

## Is it auditable?
Yes. Requests and tool actions are logged. Results are evidence‑based and traceable to outputs.

## What vendors are supported?
Currently: Cisco IOS XR and Juniper Junos. Nokia SR OS is planned.

## What common scenarios does it cover?
Interface health, link flaps, BGP/ISIS/OSPF neighbor issues, missing routes, CPU/memory, and hardware health.

## How does it avoid hallucinations?
It only answers using tool output and refuses to guess when evidence is missing.

## Can it integrate with our monitoring tools?
Yes. Prometheus/Alertmanager and Slack are supported; adapters for other tools can be added.

## Can it create or update tickets?
Yes. Jira ticket creation, comments, and field updates are supported.

## How long does a pilot take?
Typically 1–2 days to install and validate, then 1–2 weeks of testing in a lab or shadow‑mode.

## What does the customer need to provide?
- Device access (read‑only)  
- Inventory list (CSV or YAML)  
- LLM credentials (or on‑prem model endpoint)  
- Slack and Jira tokens (optional, for integrations)

## What data is stored?
Configuration settings and optional chat memory in a local DB. Credentials are stored securely.

