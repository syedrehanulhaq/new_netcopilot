Adapters isolate vendor/integration logic from NetCopilot's core.

Current adapters:
- devices/ (Cisco IOS XR, Junos, Nokia SR OS)
- ticketing/ (Jira)
- inventory/ (NetBox + Nautobot + YAML)
- chat/ (Slack)
- llm/ (OpenAI/LiteLLM)

Next candidates:
- inventory/ (NetBox, YAML)
- chat/ (Slack, Teams)
- llm/ (OpenAI, LiteLLM/Bedrock)
