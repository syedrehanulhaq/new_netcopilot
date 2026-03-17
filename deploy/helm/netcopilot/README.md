# NetCopilot Helm Chart

## Install
```bash
helm upgrade --install netcopilot deploy/helm/netcopilot \
  --namespace netcopilot --create-namespace
```

## Configure
Edit values:
```bash
helm upgrade --install netcopilot deploy/helm/netcopilot \
  --namespace netcopilot --create-namespace \
  -f values-prod.yaml
```

## Example values file
```yaml
image:
  repository: netcopilot
  tag: prod

config:
  LLM_PROVIDER: local
  LOCAL_LLM_BASE_URL: http://litellm:4000/v1
  LOCAL_LLM_MODEL: claude-haiku

secrets:
  OPENAI_API_KEY: ""
  CISCO_USER: "admin"
  CISCO_PASS: "secret"
  JUNIPER_USER: "jcluser"
  JUNIPER_PASS: "secret"
  SESSION_SECRET: "change-me"

inventory:
  juniper_devices:
    pe1: "66.129.234.213:33045"
```
