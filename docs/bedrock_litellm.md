# Bedrock (Claude) via LiteLLM

NetCopilot uses OpenAI-compatible tool calling. LiteLLM provides an OpenAI-style proxy for AWS Bedrock (Claude) or OpenAI.

## 1) Create a LiteLLM config (OpenAI now, Bedrock later)

OpenAI (default):

```yaml
model_list:
  - model_name: gpt-4.1-mini
    litellm_params:
      model: openai/gpt-4.1-mini
```

Bedrock (Claude):

Save this as `litellm.yaml` (or use the example in the repo).

```yaml
model_list:
  - model_name: claude-3-5-sonnet
    litellm_params:
      model: bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0
      aws_region_name: us-east-1
  - model_name: claude-3-5-haiku
    litellm_params:
      model: bedrock/anthropic.claude-3-5-haiku-20241022-v1:0
      aws_region_name: us-east-1
```

## 2) Set AWS credentials (example, Bedrock only)

You can use `~/.aws/credentials` or export environment variables:

```
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_REGION=us-east-1
```

## 3) Run LiteLLM

```
litellm --config litellm.yaml --port 4000
```

## 4) Point NetCopilot to LiteLLM

In `.env` or Admin Settings:

```
LLM_PROVIDER=local
LOCAL_LLM_BASE_URL=http://127.0.0.1:4000/v1
LOCAL_LLM_API_KEY=local
LOCAL_LLM_MODEL=claude-3-5-sonnet
```

Restart NetCopilot after the change.

## Switching to Bedrock later (no code change)

1. Replace your `litellm.yaml` with the Bedrock config above.
2. Set AWS credentials on the LiteLLM host.
3. In NetCopilot Settings, update `LOCAL_LLM_MODEL` to `claude-3-5-sonnet` (or haiku).
