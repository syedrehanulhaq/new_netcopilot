# NetCopilot Startup (After Reboot)

## 1) Open a terminal and go to the project

```
cd /Users/syedhaq/new_netcopilot
```

First time only — Python 3.10+ is required (the `mcp`, `mcp-atlassian` and `anthropic` packages need it):
```
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

## 2) Start LiteLLM (if using Bedrock/OpenAI via proxy)

```
.venv/bin/litellm --config litellm.yaml --port 4000
```

Health check:
```
curl -sS http://127.0.0.1:4000/health
```

## 3) Start NetCopilot

```
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8001
```

Health checks:
```
curl -sS http://127.0.0.1:8001/health
curl -sS http://127.0.0.1:8001/health/llm
```

## 4) Open the UI

```
http://127.0.0.1:8001/
```

## 5) Optional: Expose locally via ngrok (Slack/Teams callbacks)

Start ngrok:
```
ngrok http 8001
```

Copy the HTTPS URL and use it for:
- Slack slash command request URL
- Slack event or interactive callback URLs (if enabled)
- Any external webhook tests

## 6) Optional: Prometheus demo flaps

If Prometheus is used:
```
curl -sS http://127.0.0.1:9090/-/healthy
```

---

# Tests

```
.venv/bin/python -m pytest -q tests
```

The suite uses a temporary data directory and fake devices/LLM — it never touches
real devices, your `devices.yaml`, or the `.db` files.

# Quick Notes

- If `/health/llm` shows `provider: local`, LiteLLM must be running.
- If `/health/llm` shows `provider: openai`, LiteLLM is not required.
- Update LLM settings in the Admin UI if you change LiteLLM models.
