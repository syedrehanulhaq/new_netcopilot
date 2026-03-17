# NetCopilot Customer Install Pack

## Overview
NetCopilot is delivered as a containerized application and deployed via Helm on Kubernetes.
No Python or system dependencies are required on the customer host beyond standard Kubernetes tooling.

## What You Receive
1) **Container image** (from your registry)
2) **Helm chart** (`deploy/helm/netcopilot`)
3) **values-prod.yaml** template
4) **This install guide**

## System Requirements
- Kubernetes 1.24+
- Helm 3+
- Ingress controller (nginx or equivalent)
- TLS certificate (K8s secret)
- StorageClass for PVCs

## Network Requirements
Outbound access from NetCopilot pods to:
- Device management networks (SSH + Netconf ports)
- LLM endpoint (OpenAI or LiteLLM/Bedrock)
- Jira/ServiceNow/Slack/Prometheus (if enabled)

## Dependencies (Customer)
**No runtime dependencies to install on hosts**. All runtime libraries are inside the container image.
Only the following tools are required on the admin workstation:
- `kubectl`
- `helm`

## Configuration Inputs Required
### Device Inventory
Provide device list in `values-prod.yaml`:
```yaml
inventory:
  cisco_devices:
    sandbox-iosxr-1: sandbox-iosxr-1.cisco.com
  juniper_devices:
    pe1: "66.129.234.213:33045"
  nokia_devices: {}
```

### Credentials / Secrets
Fill in `secrets` block in `values-prod.yaml`:
```yaml
secrets:
  CISCO_USER: "..."
  CISCO_PASS: "..."
  JUNIPER_USER: "..."
  JUNIPER_PASS: "..."
  SESSION_SECRET: "..."
```

### LLM Provider
OpenAI example:
```yaml
config:
  LLM_PROVIDER: openai
  OPENAI_MODEL: gpt-4.1-mini
secrets:
  OPENAI_API_KEY: "..."
```

LiteLLM example:
```yaml
config:
  LLM_PROVIDER: local
  LOCAL_LLM_BASE_URL: http://litellm:4000/v1
  LOCAL_LLM_MODEL: claude-haiku
```

## Installation Steps
1) Pull image from registry
```
docker pull <registry>/netcopilot:prod
```

2) Install with Helm
```
helm upgrade --install netcopilot deploy/helm/netcopilot \
  --namespace netcopilot --create-namespace \
  -f values-prod.yaml
```

3) Verify
```
kubectl -n netcopilot get pods
curl https://<your-host>/health
```

## Operations
- Reload inventory after device changes:
```
curl -X POST https://<your-host>/admin/reload-inventory
```
- Health checks:
  - `/health`
  - `/health/llm`
- Logs:
  - container stdout/stderr (ship to SIEM)

## Support
For support, provide:
- `/health` output
- `/health/llm` output
- pod logs for the NetCopilot deployment
