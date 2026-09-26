# NetCopilot — Security Review Guide

Audience: security, risk and platform teams assessing NetCopilot for production use.
Every control below is implemented in code and covered by the automated test suite
(`tests/`). The live configuration of a deployment can be checked at
**Admin → Posture** (`GET /admin/security-posture`).

## 1. What NetCopilot does

A web / Slack / Teams assistant that answers network operations questions by running
**read-only** diagnostics on routers and switches (Juniper Junos, Cisco IOS XR, Nokia
SR OS, Aruba AOS-CX), querying Prometheus and syslog (Loki), and optionally creating
Jira tickets. An LLM decides which diagnostics to run and summarises the evidence.

## 2. Data flows

| Flow | Direction | What is sent | Notes |
|---|---|---|---|
| Users → NetCopilot | inbound HTTPS | questions, admin settings | Auth0 SSO or API keys (§3.1) |
| Slack / Teams / Alertmanager → NetCopilot | inbound webhooks | slash commands, button clicks, alerts | signed / token-authenticated (§3.6) |
| NetCopilot → LLM provider | outbound | system prompt, question, last 12 turns of that user's conversation, **redacted** tool output (≤ 24 000 chars per tool call) | device inventory **names/vendors only** (no IPs); **device credentials are never sent** |
| NetCopilot → devices | outbound SSH (Netmiko) / NETCONF (PyEZ) | read-only commands | enforced in code (§3.2) |
| NetCopilot → Jira | outbound HTTPS | ticket create / comment / update / transition | roles `noc-ticket` and `admin` only |
| NetCopilot → Prometheus / Loki | outbound HTTPS | read queries | |
| NetCopilot → Slack / Teams | outbound HTTPS | answers, alert messages | |

To keep all data inside your network or cloud tenancy, set `LLM_PROVIDER=local` and point
`LOCAL_LLM_BASE_URL` at an internal OpenAI-compatible endpoint (e.g. LiteLLM in front of
Amazon Bedrock or Azure OpenAI, see `docs/bedrock_litellm.md`).

### Data at rest (`NETCOPILOT_DATA_DIR`, SQLite)

| File | Contents | Retention |
|---|---|---|
| `settings.db` | Admin UI settings; **secret values encrypted** (Fernet) | until changed |
| `memory.db` | per-user conversation context (incl. truncated, redacted tool output) | `CHAT_RETENTION_DAYS` (14) and last 12 turns |
| `history.db` | investigations: question, answer, tools run, redacted evidence | `HISTORY_RETENTION_DAYS` (90) |
| `audit.db` | audit trail (§3.8) | `AUDIT_RETENTION_DAYS` (90; 365 in the K8s prod manifests) |
| `alerts.db` | received alerts and latest analysis per channel | `ALERT_RETENTION_DAYS` (7) |
| `playbooks.db` | diagnostic playbooks | until changed |
| `.settings.key`, `.session_secret` | generated only when not supplied by env (mode 0600) | — |

## 3. Controls

### 3.1 Authentication and authorisation
- **Modes:** Auth0 SSO (OIDC; roles from ID-token claims/group map), or API keys
  (`NETCOPILOT_API_KEYS="key:role,..."`, compared in constant time).
- **No anonymous access by default.** With neither configured, only requests from the
  host itself (127.0.0.1, not relayed through a proxy) are served. `ALLOW_ANONYMOUS_ACCESS=true`
  exists for isolated labs and is reported as a **fail** in the posture report.
- **Roles:** `noc` (diagnostics), `noc-ticket` (+ Jira), `admin` (+ settings, inventory,
  playbooks, audit). The role-filtered tool list is enforced **at execution time** — a
  model that names a tool outside the caller's role is refused.
- Settings (API keys, tokens) are read per request: rotation takes effect immediately.

### 3.2 Read-only device access (not configurable)
`adapters/devices/safety.py`, applied in the dispatcher and again inside each adapter:
- free-form commands must start with `show` / `ping` / `traceroute` (SR OS also
  `admin show`, `admin display-config`); `debug`, `clear`, `request`, `configure`, `reload`
  etc. are rejected;
- pipes limited to filter/format modifiers (`match`, `except`, `count`, `display`,
  `compare`, …); `save`, `file`, `tee`, redirection `>` and `;` are rejected;
- **no newlines/control characters** in any argument (blocks smuggling a second command);
  structured arguments (interface, prefix, neighbour, destination, ACL) are
  character-set validated; ping counts capped at 100;
- values learned from devices (e.g. LLDP neighbour names) pass the same validation
  before being used against another device.

Individual tools can additionally be disabled with `TOOL_DENYLIST`
(e.g. `cisco_run_command,junos_run_command,nokia_run_command,aruba_run_command`)
or restricted with `TOOL_ALLOWLIST`.

### 3.3 Secret redaction before the LLM
`redaction.py` masks passwords, secrets, keys (incl. IOS type-7 / Junos `$9$` reversible
forms and crypt hashes), SNMP communities, PSKs and PEM private keys in **all** tool output
before it reaches the LLM, conversation memory or history. The model is told how many values
were hidden. BGP communities are deliberately kept. Controlled by `REDACT_DEVICE_SECRETS`
(default on; reported as **fail** when off).

### 3.4 Prompt-injection resilience
Device output, syslog lines, alert text and ticket fields are untrusted. The system prompt
instructs the model to treat tool output as data, and — more importantly — the tool layer
cannot perform changes regardless of what the model is persuaded to do (§3.2). The replay
eval suite includes an injection case (`evals/cases/prompt-injection-description.yaml`).

### 3.5 Secrets management
- All credentials can come from environment / Kubernetes Secrets or the Admin UI; device
  and Jira credentials can also come from HashiCorp Vault (`VAULT_ADDR`, token or AppRole,
  KV v2), which then takes precedence.
- Admin-UI secrets are encrypted at rest with Fernet (AES-128-CBC + HMAC-SHA256); key from
  `NETCOPILOT_ENCRYPTION_KEY`, else a generated 0600 key file (posture **warn**).
- Secrets are never returned by the API (the Admin UI shows only "Saved").
- Error messages from device libraries are scrubbed of credential-looking values.

### 3.6 Inbound webhooks
- **Slack:** signing secret (HMAC-SHA256, 5-minute replay window). Unsigned requests are
  refused unless anonymous access is allowed.
- **Teams:** outgoing-webhook HMAC (`TEAMS_HMAC_SECRET`); long answers are posted to a
  channel webhook instead of the 5-second synchronous reply.
- **Alerts / Alertmanager:** `ALERT_INGEST_TOKEN` via `X-Alert-Token` or `Authorization: Bearer`.
- **Nautobot inventory webhook:** `INVENTORY_WEBHOOK_TOKEN` (constant-time compare).

### 3.7 Browser and session security
- Strict Content-Security-Policy (`script-src 'self'`, no third-party scripts or fonts,
  `frame-ancestors 'none'`), `X-Content-Type-Options`, `Referrer-Policy`, `Cache-Control: no-store`
  on API responses, HSTS when `SESSION_COOKIE_SECURE=true`.
- Session cookies: SameSite=Lax, optional Secure flag, bounded lifetime
  (`SESSION_MAX_AGE_HOURS`, 12h), signed with a configured or generated secret.
- CSRF: cookie-authenticated writes must come from the same origin (`ALLOWED_ORIGINS` for extras).
- All model / device text is rendered as text or through an escaping Markdown renderer.
- `/docs` (Swagger) can be disabled with `ENABLE_API_DOCS=false` (disabled in the prod manifests).

### 3.8 Audit trail
Recorded to stdout (JSON) and `audit.db`, viewable/exportable at **Admin → Audit**
(`GET /admin/audit`, `/admin/audit.csv`): logins/logouts, every chat (user, role, tools used,
token counts), **every tool call with its arguments**, admin setting changes (key names only),
denied setting changes, CSRF blocks, alerts received, and every write request or denied/failed
request with the caller identity. Secret-named fields are redacted from audit records.

### 3.9 Guardrail lock
`NETCOPILOT_LOCK_GUARDRAILS=true` makes security settings (auth, redaction, host-key checking,
tool allow/deny lists, LLM endpoint, retention, …) come **only** from the deployment
environment; the Admin UI cannot change them. Enabled in the Kubernetes prod manifests.

### 3.10 Network device transport
- `SSH_STRICT_HOST_KEY_CHECKING=true` rejects unknown or changed device host keys
  (Netmiko: `SSH_KNOWN_HOSTS_FILE`; Junos/NETCONF: `~/.ssh/known_hosts` of the service user).
- Per-device concurrency limit, circuit breaker, 60 s timeouts; one SSH session per call.

### 3.11 Abuse and cost controls
Per-client rate limit (`RATE_LIMIT_PER_MIN`), 10 tool rounds per question, 5 parallel tool
calls, automatic alert investigations deduplicated and capped per hour
(`AUTO_INVESTIGATE_MAX_PER_HOUR`) and run with the read-only `noc` role. LLM token usage is
recorded per request and exported to Prometheus (`/metrics`, optional `METRICS_TOKEN`).

### 3.12 Container and Kubernetes
Non-root user (UID 10001), read-only root filesystem, all capabilities dropped,
no privilege escalation, RuntimeDefault seccomp, no service-account token, NetworkPolicy,
single replica with `Recreate` rollouts (SQLite state).

## 4. Production checklist

1. Authentication: Auth0 SSO (preferred) or `NETCOPILOT_API_KEYS`; never `ALLOW_ANONYMOUS_ACCESS`.
2. TLS at the ingress; `SESSION_COOKIE_SECURE=true`.
3. `NETCOPILOT_ENCRYPTION_KEY` and `SESSION_SECRET` from your secret manager.
4. `NETCOPILOT_LOCK_GUARDRAILS=true`.
5. Device accounts with **read-only privileges on the devices themselves** (defence in depth:
   a Junos login class / IOS XR task group / SR OS profile without configuration rights).
6. `SSH_STRICT_HOST_KEY_CHECKING=true` with a managed known_hosts file.
7. Decide the LLM data boundary (`LLM_PROVIDER=local` for in-tenancy processing).
8. Webhook secrets: `SLACK_SIGNING_SECRET`, `TEAMS_HMAC_SECRET`, `ALERT_INGEST_TOKEN`.
9. Ship stdout logs (JSON, including the audit stream) to your SIEM; set `AUDIT_RETENTION_DAYS`.
10. Review **Admin → Posture** — it should show no `fail` and only accepted `warn`s.

## 5. Residual risks and limitations

- **Analysis quality:** prompt injection or model error can produce a wrong *conclusion*
  (never a device change). Answers show the evidence and tool calls behind them; the replay
  evals (`evals/`) measure accuracy before model or prompt changes.
- **Redaction is pattern-based.** Unusual secret formats could pass through. For strict
  environments also deny the free-form `*_run_command` tools and/or keep the LLM in-tenancy.
- **Non-secret sensitive data** (topology, interface descriptions, customer names in configs)
  is sent to the LLM as part of normal diagnostics.
- **API keys are static shared secrets** without expiry; prefer SSO for named accountability.
  MFA is delegated to the identity provider.
- **SSO is Auth0-specific** today (OIDC via Auth0 discovery); other IdPs need a small change.
- **Local anonymous mode** trusts anyone with access to the host itself.
- **Single replica** (SQLite); no high availability until state moves to an external database.
- Jira write actions are available to `noc-ticket` / `admin` roles by design.
- Rate limiting is in-process (per replica).
