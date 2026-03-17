# NetCopilot — Complete E2E Technical Reference

> Everything you need to explain, understand, and present NetCopilot to anyone.

---

## Table of Contents

1. [What problem does NetCopilot solve?](#1-what-problem-does-netcopilot-solve)
2. [File structure and what each file owns](#2-file-structure-and-what-each-file-owns)
3. [Startup sequence](#3-startup-sequence)
4. [Settings and credentials](#4-settings-and-credentials)
5. [Device registry](#5-device-registry)
6. [The system prompt](#6-the-system-prompt)
7. [The Agentic AI Loop](#7-the-agentic-ai-loop)
8. [The Tool Dispatcher](#8-the-tool-dispatcher)
9. [Device adapters](#9-device-adapters)
10. [The MCP Client](#10-the-mcp-client)
11. [The Playbook Runner](#11-the-playbook-runner)
12. [Slack integration](#12-the-slack-integration)
13. [Roles and access control](#13-the-three-roles-and-what-they-control)
14. [Alert pipeline](#14-the-alert-pipeline)
15. [How the LLM knows what tools exist](#15-how-the-llm-knows-what-tools-exist)
16. [Deployment — Docker Compose](#16-deployment--docker-compose)
17. [Read-only safety guarantee](#17-the-read-only-safety-guarantee)
18. [2-minute summary](#18-2-minute-summary-for-anyone)

---

## 1. What problem does NetCopilot solve?

NOC (Network Operations Centre) engineers spend their days SSHing into routers, running `show` commands, cross-referencing Prometheus alerts, searching syslogs, and opening Jira tickets — all manually, one device at a time. It is slow, repetitive, and requires deep command-line knowledge for every vendor.

NetCopilot replaces that workflow. An engineer asks a plain-English question like *"why is BGP down on pe1?"* and the system logs into the device, pulls data, evaluates severity, and writes back a structured diagnosis — no manual SSH, no copy-pasting, no command memorisation.

---

## 2. File structure and what each file owns

```
netcopilot_claude/
│
├── app.py                     ← The entire web server. Every HTTP endpoint lives here.
├── config.py                  ← Reads all settings (credentials, URLs, features) at startup.
├── settings.py                ← The settings storage engine (SQLite + env var fallback).
├── credentials.py             ← How secrets are fetched at call time (env / DB / Vault).
├── devices.yaml               ← The device registry: name → vendor, host, port.
│
├── llm/
│   └── prompts.py             ← The system prompt: rules the LLM follows every request.
│
├── adapters/
│   ├── llm.py                 ← LLM provider abstraction (OpenAI, Anthropic, local Ollama).
│   ├── chat.py                ← Slack adapter (post messages, format payloads).
│   ├── devices/
│   │   ├── registry.py        ← Device registry loader: name → {vendor, host, port}.
│   │   ├── dispatcher.py      ← THE ROUTER. Every single tool call passes through here.
│   │   ├── pool.py            ← SSH connection pool + circuit breaker (60 s TTL).
│   │   ├── cisco.py           ← 18 Cisco IOS XR tools (Netmiko SSH).
│   │   ├── junos.py           ← 19 Juniper Junos tools (PyEZ NETCONF).
│   │   └── nokia.py           ← 18 Nokia SR OS tools (Netmiko SSH).
│   ├── integrations/
│   │   ├── jira.py            ← 4 Jira tools (direct httpx). Used by Slack handler.
│   │   ├── prometheus.py      ← 5 Prometheus tools (direct httpx, PromQL queries).
│   │   └── syslog.py          ← 5 Loki/Syslog tools (direct httpx, LogQL queries).
│   └── inventory/             ← NetBox / Nautobot device discovery adapters.
│
├── mcp_client/
│   └── client.py              ← Connects to vendor MCP servers at startup. Jira = 48 tools.
│
├── playbooks/
│   ├── playbooks.yaml         ← 9 diagnostic recipes (bgp_down, link_flap, cpu_high …).
│   ├── runner.py              ← Executes playbooks, scores severity, extracts evidence.
│   └── store.py               ← Playbook CRUD in SQLite (admin can edit via API).
│
├── mcp_servers.yaml           ← Which vendor MCP servers to connect to at startup.
├── docker-compose.yml         ← Production deployment (app container + jira-mcp sidecar).
└── Dockerfile.prod            ← How to build the production container image.
```

---

## 3. Startup sequence

When `uvicorn app:app` starts, the `lifespan` context manager in `app.py` runs five steps before the server begins accepting requests.

### Step 1 — Init memory DB
Creates `memory.db` (SQLite) if it does not exist. This is where conversation history is stored per user so the LLM remembers the last 12 exchanges.

### Step 2 — Init alerts DB
Creates `alerts.db` (SQLite). When Prometheus or PagerDuty fires an alert via `/alerts/ingest`, it is stored here so the Slack handler can pick it up with `/noc investigate`.

### Step 3 — Reload device registry
Calls `reload_registry()` which reads `devices.yaml` and builds the in-memory `_cache` dict. From this point on, any code can call `get_vendor("pe1")` and instantly get `"junos"` without re-reading the file.

### Step 4 — Start the MCP client
Creates `MCPClient`, reads `mcp_servers.yaml`, and connects to every `enabled: true` server over SSE. Currently only `jira` (mcp-atlassian on port 8104) is enabled. The client asks the server "what tools do you have?" and stores 48 Jira tool definitions in memory.

### Step 5 — Create the Tool Dispatcher
Passes the connected MCP client in. The dispatcher is the routing brain that holds references to all backends and is used by every subsequent tool call.

On shutdown, `_mcp_client.stop()` gracefully disconnects from all MCP servers.

---

## 4. Settings and credentials

### The problem this solves
Credentials and configuration should be changeable at runtime without restarting the app.

### How `settings.py` works
Every `get_setting(key, default)` call follows this priority chain:

```
settings.db (SQLite) → environment variable → default value
```

1. Look in `settings.db` — editable by admins via the web UI at `/admin/settings`
2. If not found, look at the environment variable with the same name
3. If still not found, return the default value

This means:
- **Development**: put values in a `.env` file — they are picked up automatically
- **Production**: set Docker environment variables in `docker-compose.yml`
- **Runtime changes**: an admin changes a setting via the UI — the next request picks it up immediately because `get_setting()` queries SQLite on every call, no restart needed

### How `config.py` relates
`config.py` runs at import time and sets module-level constants (`JIRA_BASE_URL`, `SLACK_BOT_TOKEN`, etc.). These are read once at startup and used in code that runs before the first request (middleware setup, lifespan hooks).

`settings.py` is for per-request dynamic lookups — LLM provider, device credentials, feature flags — things that should change without a restart.

### Credentials at call time
Device credentials (`JUNIPER_USER`, `JUNIPER_PASS`, etc.) are fetched by `credentials.get_secret()` inside each tool function, not at startup. This means changing a password in the UI takes effect on the next SSH connection without restart.

---

## 5. Device registry

### `devices.yaml` — the source of truth

```yaml
pe1:
  host: 66.129.234.213
  port: 33045
  vendor: junos          # ← critical: deterministic vendor routing
  platform: junos
  site: london-dc1       # optional, informational
```

The `vendor` field is the key that makes routing work without the LLM guessing. Every routing decision in the dispatcher is based on this field.

### `adapters/devices/registry.py` — the in-memory cache

Key public functions:

| Function | What it does |
|---|---|
| `get_vendor("pe1")` | Returns `"junos"` |
| `resolve_host("pe1")` | Returns `("66.129.234.213", 33045)` |
| `correct_tool_vendor("nokia_get_bgp_summary", "pe1")` | Returns `"junos_get_bgp_summary"` — fixes LLM mistakes |
| `device_inventory_summary()` | Returns a formatted string injected into the LLM system prompt |
| `list_devices()` | Returns all registered devices as a list (no credentials) |

### Why not let the LLM figure out the vendor?
LLMs hallucinate. If the LLM guesses wrong and calls a Cisco command on a Juniper device, the SSH connection will fail or return garbage. The registry makes vendor resolution 100% deterministic — the LLM reads the inventory in its system prompt and always knows.

### Alternative sources: NetBox and Nautobot
`config.py` calls `reload_inventory_devices()` which can query NetBox or Nautobot APIs and merge the results into the same registry structure. In production with a real IPAM, `devices.yaml` does not need to be manually maintained — it is auto-populated. The `/admin/reload-inventory` endpoint and the `/webhooks/nautobot/inventory` webhook can trigger this refresh without restart.

---

## 6. The system prompt

**File:** `llm/prompts.py`

The system prompt is what the LLM reads at the start of every conversation. It is injected as the first message with `role: "system"`. It is built dynamically on every request by `build_system_prompt()`.

### What it contains

**1. Identity and scope**
"You are NetCopilot, an AI assistant for network operations engineers."

**2. Read-only constraint**
Explicitly forbids configuration changes. The LLM will decline requests like "shut down this interface" and offer to create a change ticket instead. This is a safety guardrail baked into the LLM's instructions.

**3. Device inventory** — injected dynamically every request:
```
sandbox-iosxr-1: vendor=cisco platform=iosxr [devnet-sandbox]
pe1: vendor=junos platform=junos
sw1: vendor=junos platform=junos
```
This is why the LLM calls `junos_get_bgp_summary` for pe1 and `cisco_get_bgp_summary` for sandbox-iosxr-1 — it read the inventory in its instructions.

**4. Vendor → tool mapping rule**
"Juniper devices → use junos_ prefix tools. Do NOT ask the user which vendor a device is."

**5. Playbook summary**
Lists available playbooks and when to use them.

**6. Answer format rules**
How to structure a response: Status → Evidence → Likely cause → Next steps.

### Why is it built dynamically?
Because the device inventory can change (new devices added via `/admin/reload-inventory`). If it were hardcoded, a restart would be required every time a device is added or removed.

---

## 7. The Agentic AI Loop

**File:** `app.py`, function `_agentic_loop()` (~line 671)

This is the heart of the system.

### What "agentic" means
A normal LLM call is one-shot — you ask, it answers. An agentic loop is a while-loop where the LLM can say "I need more data" by emitting a tool call instead of a text reply. The loop runs again with the tool result appended, and the LLM decides again. This repeats until the LLM writes plain text.

### The loop — step by step

```
┌────────────────────────────────────────────────────────────────────┐
│                     AGENTIC LOOP (max 10 iterations)               │
│                                                                    │
│  Build messages:                                                   │
│  [ system prompt ] + [ conversation history ] + [ user message ]  │
│  + [ all tool results collected so far ]                           │
│         │                                                          │
│         ▼                                                          │
│  Call LLM with messages + tool schemas + tool_choice="auto"        │
│         │                                                          │
│         ├── LLM returns plain text → yield reply, STOP            │
│         │                                                          │
│         └── LLM returns tool_calls → execute them                 │
│                  │                                                 │
│                  ▼                                                 │
│  asyncio.gather(*[exec_one(tc) for tc in tool_calls])             │
│  (all tool calls in parallel)                                      │
│                  │                                                 │
│                  ▼                                                 │
│  Append tool results to messages as role:"tool" entries            │
│  Loop back to top ──────────────────────────────────────────────► │
└────────────────────────────────────────────────────────────────────┘
```

### Parallel tool execution
If the LLM requests multiple tools in one turn (e.g. "get BGP summary AND check CPU"), they run simultaneously with `asyncio.gather()`, not one after another. Multiple checks take the same wall-clock time as one.

### Why append tool results as messages?
The OpenAI API protocol requires it. The conversation history must contain the tool call (from the assistant) followed by the tool result (with `role: "tool"`). This lets the LLM "see" what came back before deciding next steps.

### SSE streaming
The `/chat/stream` endpoint yields SSE events in real time:
- `{"type": "tool_start", "name": "junos_get_bgp_summary", "args": {...}}` — so the UI can show "calling..."
- `{"type": "tool_end", "name": "...", "ms": 412}` — timing info
- `{"type": "reply", "content": "BGP is down because..."}` — the final answer

The `/chat` endpoint runs the same loop but waits for the final reply and returns JSON.

### Audit logging
Every tool call is written to `audit.log` with: request ID, tool name, user, role, execution time, and whether it errored. Every HTTP request is also audit-logged. All sensitive fields are redacted.

---

## 8. The Tool Dispatcher

**File:** `adapters/devices/dispatcher.py`

The dispatcher is the routing brain. Nothing calls a device adapter or integration adapter directly — everything goes through here.

### Why have a dispatcher at all?
Without it, `app.py` would need a 200-line `if/elif` chain. Every new vendor or integration would require editing `app.py`. With the dispatcher, adding a new vendor means adding one entry to `_VENDOR_MODULES`. Adding a new integration means one entry in `_INTEGRATION_MODULES`.

### `get_all_tools(role)` — builds the LLM's tool list

Called fresh for every request:
1. Load `TOOL_SCHEMAS` from each device adapter (`cisco.py`, `junos.py`, `nokia.py`)
2. Load `TOOL_SCHEMAS` from integration adapters — but skip any prefix that the MCP client already covers (to avoid showing duplicate tools to the LLM)
3. Get tools from the MCP client, filtered by role
4. Append the `run_playbook` meta-tool schema

### `call_tool(tool_name, arguments)` — routing logic

```
tool_name == "run_playbook"
    └─→ PlaybookRunner._run_playbook()

tool_name starts with cisco_* / junos_* / nokia_*
    ├─→ correct_tool_vendor() — auto-fix wrong vendor prefix
    └─→ correct device adapter module (cisco.py / junos.py / nokia.py)

tool_name starts with jira_* / prometheus_* / syslog_*
    ├─→ if MCPClient has this tool → MCPClient (vendor MCP wins)
    └─→ else → direct adapter (prometheus.py / syslog.py / jira.py)

tool_name unknown prefix
    └─→ try MCPClient as fallback (catches jiravnd_* mcp-atlassian native names)
```

### `call_tool_unprefixed(tool_name, arguments)` — for PlaybookRunner

The playbook YAML says `get_bgp_summary`, not `junos_get_bgp_summary`. This method looks up the device in the registry, gets the vendor, and auto-prefixes. Playbooks are therefore vendor-agnostic.

```python
def call_tool_unprefixed(tool_name, args):
    vendor = get_vendor(args["device"])   # "junos"
    return call_tool(f"{vendor}_{tool_name}", args)
    # → call_tool("junos_get_bgp_summary", args)
```

---

## 9. Device adapters

**Files:** `adapters/devices/cisco.py`, `junos.py`, `nokia.py`

### What each adapter exports

Every adapter file exports two things:

- **`TOOLS`** — a dict mapping base-name → async function:
  ```python
  {"get_bgp_summary": get_bgp_summary, "get_cpu_memory": get_cpu_memory, ...}
  ```
- **`TOOL_SCHEMAS`** — a list of OpenAI function format dicts describing each tool to the LLM

### How a tool function works (e.g. `junos_get_bgp_summary`)

```
async def get_bgp_summary(device: str) -> dict:
    1. Look up host and port from registry.resolve_host(device)
    2. Get credentials from credentials.get_secret("JUNIPER_USER/PASS")
    3. Acquire connection from DevicePool (or create new one if none available)
    4. Run NETCONF RPC in a thread executor (never blocks the async event loop)
    5. Parse XML response into a clean Python dict
    6. Return the dict to the dispatcher
```

### Why thread executor?
SSH and NETCONF are blocking I/O. FastAPI runs on asyncio (non-blocking). Running blocking code directly in an async function would freeze the entire server — no other requests could be served while the SSH command runs. `run_in_executor()` hands the blocking call to a separate OS thread while the event loop serves other requests.

### Protocol choices by vendor

| Vendor | Protocol | Library | Why |
|--------|----------|---------|-----|
| Cisco IOS XR | SSH + CLI | Netmiko | Industry-standard Python SSH for network devices. Reliable CLI parsing. |
| Juniper Junos | NETCONF | PyEZ (junos-eznc) | Juniper's official Python library. NETCONF returns structured XML — more reliable than text-scraping SSH. Schema is versioned and stable. |
| Nokia SR OS | SSH + CLI | Netmiko | Same as Cisco — SSH + CLI text parsing. |

### Connection pool (`adapters/devices/pool.py`)
SSH connections are expensive to set up (~1–2 seconds for TLS handshake + auth). The pool keeps connections alive for 60 seconds and reuses them. A **circuit breaker** tracks consecutive failures — if a device fails 3 times, the circuit opens and stops hammering the device for a cooldown period before retrying.

---

## 10. The MCP Client

**File:** `mcp_client/client.py`

### What is MCP?
Model Context Protocol — an open standard for LLM tools. Instead of hardcoding tool schemas in Python, you run a separate server that publishes tools over HTTP (SSE or streamable-http). The client connects at startup, discovers tools dynamically, and routes calls.

### Why use MCP for Jira but not for devices?

**For Jira:** The community maintains `mcp-atlassian` which exposes 48 Jira tools. If direct httpx code were used for everything, all 48 would need to be coded and maintained in-house. With mcp-atlassian, the community does that work.

**For devices:** There is no official, production-grade MCP server for Cisco IOS XR or Nokia SR OS. The adapters are simple Python under our control. No benefit in the extra network hop to a separate process.

### How `MCPClient.start()` works

1. Read `mcp_servers.yaml`
2. For each `enabled: true` server, open a persistent SSE connection to its URL
3. Perform the MCP `initialize` handshake
4. Call `list_tools()` — the server returns all its tool definitions with schemas
5. Store them with a name prefix: `jira_create_issue`, `jira_search_issues`, etc.
6. Build `tool_to_server` lookup: `{"jira_create_issue": "jira", ...}`

### The prefix trick
mcp-atlassian calls its tool `create_issue`. We store it as `jira_create_issue`. This way the dispatcher can route by prefix, exactly like all other tools. The client strips the prefix back before forwarding to the server.

### Environment variable URL override
`JIRA_MCP_URL` env var overrides the YAML URL. This lets `mcp_servers.yaml` contain `http://localhost:8104/sse` (local dev) while Docker sets `JIRA_MCP_URL=http://jira-mcp:8104/sse` (Docker internal hostname). Zero YAML changes required between environments.

### Reconnection logic
If the SSE connection drops (server restarted, network blip), the client catches `ClosedResourceError`, automatically reconnects once, and retries the tool call. If that fails too, the server is removed from the connection pool and `/health` shows it as disconnected.

---

## 11. The Playbook Runner

**Files:** `playbooks/runner.py`, `playbooks/playbooks.yaml`

### What problem this solves
When the LLM sees a BGP alarm, left to its own devices it might call different tools each time, miss a step, or check things in an inconsistent order. Playbooks enforce a deterministic investigation sequence for known issue patterns.

### A playbook definition

```yaml
- name: BGP neighbor down
  intent: bgp_down
  tools:
    - get_bgp_summary           # step 1
    - get_bgp_neighbor_status   # step 2
    - get_ping                  # step 3
  evidence_fields:
    - state          # extracted from results for the summary
    - last_error
    - flaps
  severity_rules:
    critical:
      - state=Idle   # if any result has state="Idle" → Critical
      - state=Active # if any result has state="Active" → Critical
```

### How `runner.run()` executes a playbook

**Step 1 — Find the playbook**
Match the `intent` string (e.g. `"bgp_down"`) to a playbook. First tries exact match, then fuzzy substring match.

**Step 2 — Build arguments for each tool**
The runner knows that `get_bgp_neighbor_status` needs `device` + `neighbor`. This mapping is hardcoded in `_TOOL_ARG_MAPS`. It builds `{"device": "pe1", "neighbor": "10.0.0.2"}` from the params dict passed in.

**Step 3 — Call each tool**
Calls `tool_caller(tool_name, args)` for each step — this is the `call_tool_unprefixed` callback from the dispatcher. The playbook YAML uses unprefixed names (`get_bgp_summary`) — the dispatcher auto-adds the vendor prefix.

**Step 4 — Severity evaluation**
Walks all result dicts recursively using `_collect_all_values()`. Finds every `state` field across all results. Compares against severity rules. If any `state` value equals `"Idle"` or `"Active"`, severity = Critical. The rule engine supports `=`, `!=`, `>`, `<`, `>=`, `<=` operators.

**Step 5 — Evidence extraction**
Walks all results again and pulls out the first non-null value for each field listed in `evidence_fields`. Returns these as a dict for the LLM to cite.

**Step 6 — Return `PlaybookResult`**
```python
PlaybookResult(
    severity="Critical",
    evidence={"state": "Active", "last_error": "Hold timer expired", "flaps": 3},
    summary_text="[Critical] BGP neighbor down on pe1 -- state: Active last_error: Hold timer expired",
    steps=[...],
    elapsed_ms=1243,
)
```

The LLM receives this structured result and uses it as evidence to write its final diagnosis.

### Available playbooks (9 total)

| Intent | Name | Tools called |
|--------|------|-------------|
| `link_down` | Interface down/down | get_interface_health, get_link_health |
| `link_flap` | Interface flapping | get_top_flapping_links, get_interface_health, get_interface_counters |
| `crc_errors` | High errors / CRC | get_interface_health, get_interface_counters, get_optics |
| `bgp_down` | BGP neighbor down | get_bgp_summary, get_bgp_neighbor_status, get_ping |
| `bgp_active` | BGP neighbor Active/Idle | get_bgp_summary, get_bgp_neighbor_status, get_route_lookup, get_ping |
| `isis_down` | ISIS adjacency down | get_isis_health, get_isis_neighbors, get_mtu_info |
| `route_missing` | Routing missing | get_route_lookup, get_bgp_route, get_isis_neighbors, get_ospf_neighbors |
| `cpu_high` | High CPU / memory | get_cpu_memory |
| `hardware_fault` | Chassis/PIC/FPC issue | get_chassis_health |

---

## 12. The Slack integration

**File:** `app.py`, endpoint `/slack/command` (~line 1014)

The Slack integration handles three distinct command patterns.

### Pattern 1: `/noc ticket BGP down on pe1`
- `_parse_ticket_command()` detects the word "ticket" in the text
- Calls `_dispatcher.call_tool("jira_create_ticket", payload)` directly — **no LLM involved**
- Why no LLM? Speed. Slack requires a response within 3 seconds or it shows an error. LLM calls take 5–15 seconds.
- Solution: immediately return `{"text": "Creating ticket..."}` (HTTP 200 within 3 seconds), spawn a daemon thread that does the real work, post the result to Slack's `response_url` webhook when done.

### Pattern 2: `/noc investigate`
- Looks up the latest alert in `alerts.db` for this Slack channel
- Sends it to `_run_chat("Investigate alert: {alert_text}", ...)` — full LLM agentic loop
- Saves the analysis back to `alerts.db` so a subsequent `/noc ticket` can include the AI investigation in the ticket description

### Pattern 3: `/noc {any other question}`
- Goes straight to `_run_chat()` — full LLM agentic loop, same as the browser UI

### Slack request verification
Every incoming Slack request is verified using HMAC-SHA256 with the `SLACK_SIGNING_SECRET`. The check validates both the signature and that the request timestamp is within 5 minutes (replay attack prevention). Without this, anyone who knows your endpoint URL could forge Slack commands.

### The two Jira paths

| Path | Trigger | Route | Tools | Speed |
|------|---------|-------|-------|-------|
| A — LLM | AI-initiated during a chat | LLM → Dispatcher → MCPClient → mcp-atlassian | 48 tools | 5–15 s |
| B — Slack | `/noc ticket` command | Slack handler → Dispatcher → jira.py | 4 tools | < 3 s |

Path B bypasses the LLM entirely for maximum speed. Path A gives the LLM the full power of mcp-atlassian's 48 Jira tools when it needs to search, transition, or do complex ticket operations during a conversation.

---

## 13. The three roles and what they control

Roles are: `admin`, `noc-ticket`, `noc`

### Where roles come from

| Interface | How role is determined |
|-----------|------------------------|
| Browser | Auth0 JWT token → role claim extracted at login → stored in session cookie |
| API | `X-API-Key` header → looked up in `NETCOPILOT_API_KEYS` setting (`key:role` pairs) |
| Slack | Fixed `SLACK_ROLE` setting (default: `noc`) |
| Microsoft Teams | Fixed `TEAMS_ROLE` setting (default: `noc`) |
| No auth configured | Defaults to `admin` (development convenience) |

### What roles gate

**1. Tool visibility**
`get_all_tools(role)` returns only tools visible to that role. A `noc` user never sees Jira ticket creation tools in their tool list. The LLM literally cannot call them because they are not in its available tools.

**2. Admin endpoints**
`/admin/settings`, `/admin/inventory`, `/admin/playbooks`, `/admin/reload-inventory` all require `role == "admin"`. Any other role gets HTTP 403.

**3. MCP server tools**
Each server in `mcp_servers.yaml` has a `roles` list:
```yaml
- name: jira
  roles: [noc-ticket, admin]
```
A `noc` engineer cannot create Jira tickets through the AI — even if they ask for it, the LLM has no Jira tools in its list.

---

## 14. The alert pipeline

External monitoring systems can push alerts into NetCopilot:

### Ingest
```http
POST /alerts/ingest
X-Alert-Token: <secret>
Content-Type: application/json

{"text": "Interface ge-0/0/1 down", "device": "pe1", "severity": "critical", "channel": "C12345"}
```

1. Token validated against `ALERT_INGEST_TOKEN`
2. Alert saved to `alerts.db` keyed by Slack channel ID
3. Slack notification posted: `"ALERT: Interface ge-0/0/1 down on pe1. Run /netcopilot investigate to analyze."`

### Investigation
4. Engineer types `/noc investigate` in Slack
5. App fetches the latest alert from `alerts.db` for that channel
6. Passes it to the full LLM agentic loop: `"Investigate alert: device=pe1 interface=ge-0/0/1 severity=critical"`
7. LLM calls appropriate tools, PlaybookRunner scores severity, full diagnosis produced
8. Analysis saved back to `alerts.db` under `source="analysis"`

### Ticketing
9. Engineer types `/noc ticket` — app fetches latest alert AND latest analysis from `alerts.db`
10. Combines them: ticket summary from alert text, description includes device info + AI investigation findings
11. Jira ticket created via `jira.py` (fast path, no LLM)

This creates a full automated loop: alert → AI investigation → documented Jira ticket, with engineer oversight at every step.

---

## 15. How the LLM knows what tools exist

The LLM does not inherently know about your devices or tools. It is told in two places.

### 1. The system prompt (per-request)
Built by `build_system_prompt()` and injected as the first message:
- Full device inventory with vendor labels
- Rule: "use junos_ prefix for Juniper devices"
- Rule: "do NOT ask the user which vendor a device is"
- Playbook names and intents

### 2. The tool schema list (per-request)
```python
tools = _dispatcher.get_all_tools(role)
```

This returns a list of OpenAI function schemas:
```json
[
  {
    "type": "function",
    "function": {
      "name": "junos_get_bgp_summary",
      "description": "Get BGP summary for all neighbours on a Juniper device.",
      "parameters": {
        "type": "object",
        "properties": {
          "device": {"type": "string", "description": "Device name from registry"}
        },
        "required": ["device"]
      }
    }
  }
]
```

The LLM reads these schemas and understands: "I can call `junos_get_bgp_summary` with a `device` argument." It decides **when** and **whether** to call based on conversation context — it was not scripted.

### Tool call format
When the LLM wants to use a tool, it responds with:
```json
{
  "tool_calls": [{
    "id": "call_abc123",
    "function": {
      "name": "junos_get_bgp_summary",
      "arguments": "{\"device\": \"pe1\"}"
    }
  }]
}
```
Note: `arguments` is a JSON-encoded string, not a dict. The loop parses it with `json.loads()`.

---

## 16. Deployment — Docker Compose

### Starting production

```bash
cp .env.example .env   # fill in credentials
docker-compose up -d
docker-compose logs -f app
```

### Two containers

**`app`** — the FastAPI server
- Built from `Dockerfile.prod`
- Listens on port 8001 (configurable via `APP_PORT`)
- All credentials injected as environment variables

**`jira-mcp`** — mcp-atlassian sidecar
- Pulled from `ghcr.io/sooperset/mcp-atlassian:latest`
- Listens on port 8104 internally
- Maintained by the Atlassian community, not by us

### Dependency ordering
The app container has:
```yaml
depends_on:
  jira-mcp:
    condition: service_healthy
```
The app will not start until jira-mcp passes its healthcheck (`curl http://localhost:8104/sse`).

### Persistent storage (volumes)

| Volume | What is stored |
|--------|---------------|
| `netcopilot_data` | All SQLite DBs: settings.db, memory.db, alerts.db, playbooks.db |
| `netcopilot_logs` | Rotating log files: netcopilot.log, audit.log |

### Config files mounted from host
- `devices.yaml` — mounted read-only; edit on host and run `/admin/reload-inventory` to pick up changes
- `playbooks/playbooks.yaml` — mounted read-only; edit on host, changes picked up on next playbook run

### URL environment variable override
`JIRA_MCP_URL=http://jira-mcp:8104/sse` is set in the app container. This overrides the `mcp_servers.yaml` value (`http://localhost:8104/sse`) which is correct for local dev but wrong inside Docker where the hostname is `jira-mcp`.

---

## 17. The read-only safety guarantee

Three independent layers prevent NetCopilot from modifying devices:

### Layer 1 — System prompt instruction
The LLM is instructed in `prompts.py` that it performs read/diagnostic operations only. It will decline change requests and offer to open a change ticket instead.

### Layer 2 — `_is_config_request()` regex check
In `_run_chat()`, before the LLM is even called, the message is tested against a regex:
```python
pattern = re.compile(
    r"\b(configure|configuration|apply|commit|rollback|shutdown|no shutdown|
       enable|disable|set\s+|delete\s+|replace\s+|edit\s+|change\s+|modify\s+)\b"
)
```
If matched, the function returns a refusal immediately — the LLM and all device adapters are never invoked.

### Layer 3 — Tool schema design
None of the tool functions in `cisco.py`, `junos.py`, or `nokia.py` accept write commands. There is no `push_config()`, `commit_changes()`, or `apply_config()` function. Even if the LLM tried to call one, the tool does not exist.

All three layers must be independently bypassed for a configuration change to be possible. In practice this is not possible without modifying source code.

---

## 18. 2-minute summary for anyone

> **"NetCopilot is a FastAPI web server that connects your NOC engineers to an AI assistant. When an engineer asks a question — through a browser, Slack, or Teams — the message is sent to an LLM (GPT-4 or Claude) together with a list of available diagnostic tools: SSH commands for Cisco, Juniper, and Nokia devices; Prometheus metric queries; Loki log searches; and Jira ticket operations.**
>
> **The LLM decides which tools to call. The server runs them against real devices. The results go back to the LLM. This loop repeats until the LLM has enough data to write a structured diagnosis — with severity, evidence, and recommended next steps.**
>
> **A dispatcher routes every tool call to the right backend. Device calls go to in-process Python adapters using SSH and NETCONF. Jira goes to a vendor-maintained MCP sidecar (mcp-atlassian) that exposes 48 Jira tools without us writing a line of Jira code. Diagnostic playbooks enforce a deterministic investigation sequence for known issue types like BGP down or interface flapping.**
>
> **Everything is read-only — three independent safety layers prevent configuration changes. Role-based access control ensures NOC engineers can only see and call the tools appropriate to their role. Every action is audit-logged. Credentials never leave the server and are fetched fresh on every tool call so passwords can be rotated without restart."**

---

*Document generated from live source code. Last updated: 2026-02-27.*
