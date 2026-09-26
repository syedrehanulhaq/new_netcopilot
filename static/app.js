// ---------------------------------------------------------------------------
// Authenticated fetch: adds the API key (if the user entered one) to every call.
// The key lives in sessionStorage only — it is gone when the tab closes.
// ---------------------------------------------------------------------------
const API_KEY_STORAGE = "netcopilot_api_key";
let authMode = "anonymous";

function storedApiKey() {
  try { return sessionStorage.getItem(API_KEY_STORAGE) || ""; } catch { return ""; }
}

async function apiFetch(url, options = {}) {
  const headers = new Headers(options.headers || {});
  const key = storedApiKey();
  if (key) headers.set("X-API-Key", key);
  const resp = await fetch(url, { ...options, headers, credentials: "same-origin" });
  if (resp.status === 401 && !String(url).startsWith("/auth/")) showSignIn();
  return resp;
}

const messageEl = document.getElementById("message");
const loginBtn = document.getElementById("login");
const logoutBtn = document.getElementById("logout");
const settingsToggleBtn = document.getElementById("settings-toggle");
const apiKeyForm = document.getElementById("apikey-form");
const apiKeyInput = document.getElementById("apikey-input");
const userBadge = document.getElementById("user");
const replyEl = document.getElementById("reply");
const sendBtn = document.getElementById("send");
const newChatBtn = document.getElementById("new-chat");
const settingsOverlay = document.getElementById("settings-overlay");
const settingsCard = document.getElementById("settings-card");
const closeSettingsBtn = document.getElementById("close-settings");
const settingsNav = document.getElementById("settings-nav");
const settingsSections = Array.from(document.querySelectorAll(".settings-section"));
const reloadInventoryBtn = document.getElementById("reload-inventory");
const inventoryFileEl = document.getElementById("inventory-file");
const importInventoryBtn = document.getElementById("import-inventory");
const inventoryPreviewEl = document.getElementById("inventory-preview");
const refreshInventoryPreviewBtn = document.getElementById("refresh-inventory-preview");
const addSettingBtn = document.getElementById("add-setting");
const customKeyEl = document.getElementById("custom-key");
const customValueEl = document.getElementById("custom-value");
const customSettingsList = document.getElementById("custom-settings-list");
const settingsStatus = document.getElementById("settings-status");
const playbooksList = document.getElementById("playbooks-list");
const addPlaybookBtn = document.getElementById("add-playbook");
const savePlaybooksBtn = document.getElementById("save-playbooks");
const playbooksStatus = document.getElementById("playbooks-status");
const TOOL_OPTIONS = [
  "get_interface_health",
  "get_lldp_neighbor",
  "get_interface_counters",
  "get_interface_status",
  "get_top_flapping_links",
  "get_bgp_summary",
  "get_bgp_neighbor_status",
  "get_ospf_neighbors",
  "get_isis_neighbors",
  "get_isis_interface",
  "get_optics",
  "get_route_lookup",
  "get_bgp_route",
  "get_acl_hits",
  "get_mtu_info",
  "get_ping",
  "get_traceroute",
  "get_cpu_memory",
  "get_chassis_health",
];
const TOOL_HELP = {
  get_interface_health:
    "Example: IOS XR show interface <if>, Junos show interfaces <if> (checks status/errors).",
  get_lldp_neighbor: "Example: LLDP neighbor on the interface (what is on the other end).",
  get_optics: "Example: optical Rx/Tx power (Junos only; skipped on other vendors).",
  get_interface_counters: "Example: interface counters/errors over a time window.",
  get_top_flapping_links: "Example: Prometheus/topk flaps or syslog flap events.",
  get_bgp_summary: "Example: show bgp summary (neighbors + state).",
  get_isis_neighbors: "Example: show isis adjacency / neighbors.",
  get_ospf_neighbors: "Example: show ospf neighbor.",
  get_cpu_memory: "Example: show processes / system resources.",
};
const CLI_TEMPLATES = {
  any: [
    { label: "Show interfaces (generic)", command: "show interfaces {{interface}}" },
    { label: "Show BGP summary", command: "show bgp summary" },
    { label: "Show LLDP neighbors", command: "show lldp neighbors" },
  ],
  iosxr: [
    { label: "Show interface (IOS XR)", command: "show interface {{interface}}" },
    { label: "Show BGP summary", command: "show bgp summary" },
    { label: "Show ISIS neighbors", command: "show isis neighbors" },
    { label: "Show LLDP neighbors", command: "show lldp neighbors" },
  ],
  junos: [
    { label: "Show interface (Junos)", command: "show interfaces {{interface}}" },
    { label: "Show interface extensive", command: "show interfaces {{interface}} extensive" },
    { label: "Show BGP summary", command: "show bgp summary" },
    { label: "Show ISIS adjacency", command: "show isis adjacency" },
    { label: "Show LLDP neighbors", command: "show lldp neighbors" },
  ],
  nokia: [
    { label: "Show port (SR OS)", command: "show port {{interface}}" },
    { label: "Show router bgp summary", command: "show router bgp summary" },
    { label: "Show isis adjacency", command: "show router isis adjacency" },
    { label: "Show lldp neighbors", command: "show system lldp neighbor" },
  ],
};
const TOOL_INPUTS = {
  get_interface_health: ["device", "interface"],
  get_lldp_neighbor: ["device", "interface"],
  get_interface_counters: ["device", "interface"],
  get_interface_status: ["device"],
  get_top_flapping_links: [],
  get_bgp_summary: ["device"],
  get_bgp_neighbor_status: ["device", "neighbor"],
  get_ospf_neighbors: ["device"],
  get_isis_neighbors: ["device"],
  get_isis_interface: ["device", "interface"],
  get_optics: ["device", "interface"],
  get_route_lookup: ["device", "prefix"],
  get_bgp_route: ["device", "prefix"],
  get_acl_hits: ["device", "acl_name"],
  get_mtu_info: ["device", "interface"],
  get_ping: ["device", "destination"],
  get_traceroute: ["device", "destination"],
  get_cpu_memory: ["device"],
  get_chassis_health: ["device"],
};
const COMMON_INPUTS = [
  "device",
  "interface",
  "neighbor",
  "prefix",
  "destination",
  "ip",
  "acl_name",
  "limit",
  "since_hours",
  "window_minutes",
  "severity",
  "channel",
  "team",
];
const settingsState = new Map();
const SETTINGS_SECTION_IDS = {
  LLM: "section-llm",
  Devices: "section-devices",
  Inventory: "section-inventory",
  Ticketing: "section-ticketing",
  Slack: "section-slack",
  Teams: "section-teams",
  Prometheus: "section-prometheus",
  Syslog: "section-syslog",
  Alerts: "section-alerts",
  Auth: "section-auth",
  Security: "section-security",
  Logging: "section-logging",
};
let isAdmin = false;
settingsOverlay.classList.remove("is-open");
settingsToggleBtn.hidden = true;

// Conversation memory is keyed by user. Signed-in users are identified by the
// server session; otherwise each browser gets its own anonymous id so that
// unauthenticated users don't share one history.
let signedInUser = "";
let anonymousId = "";
function chatUserId() {
  if (signedInUser) return signedInUser;
  if (!anonymousId) {
    try { anonymousId = localStorage.getItem("netcopilot_user_id") || ""; } catch {}
    if (!anonymousId) {
      const rand = (window.crypto && crypto.randomUUID)
        ? crypto.randomUUID().slice(0, 12)
        : Math.random().toString(36).slice(2, 14);
      anonymousId = `anon-${rand}`;
      try { localStorage.setItem("netcopilot_user_id", anonymousId); } catch {}
    }
  }
  return anonymousId;
}

// ---------------------------------------------------------------------------
// Safe Markdown: everything is HTML-escaped first; only our own tags are added.
// ---------------------------------------------------------------------------
function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function inlineMarkdown(escaped) {
  return escaped
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*(?!\*)/g, "$1<em>$2</em>");
}

function renderMarkdown(source) {
  const lines = String(source || "").replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let i = 0;
  const isTableRow = (l) => /^\s*\|.*\|\s*$/.test(l);
  const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => inlineMarkdown(escapeHtml(c.trim())));
  while (i < lines.length) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {
      const code = [];
      i += 1;
      while (i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i++]);
      i += 1;
      out.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
      continue;
    }
    if (isTableRow(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1])) {
      const head = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && isTableRow(lines[i])) rows.push(cells(lines[i++]));
      out.push(
        `<table><thead><tr>${head.map((c) => `<th>${c}</th>`).join("")}</tr></thead><tbody>` +
          rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("") +
          "</tbody></table>"
      );
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      const level = Math.min(heading[1].length + 2, 6);
      out.push(`<h${level}>${inlineMarkdown(escapeHtml(heading[2]))}</h${level}>`);
      i += 1;
      continue;
    }
    const listType = /^\s*[-*•]\s+/.test(line) ? "ul" : /^\s*\d+[.)]\s+/.test(line) ? "ol" : null;
    if (listType) {
      const items = [];
      const marker = listType === "ul" ? /^\s*[-*•]\s+/ : /^\s*\d+[.)]\s+/;
      while (i < lines.length && marker.test(lines[i])) {
        items.push(`<li>${inlineMarkdown(escapeHtml(lines[i].replace(marker, "")))}</li>`);
        i += 1;
      }
      out.push(`<${listType}>${items.join("")}</${listType}>`);
      continue;
    }
    if (!line.trim()) {
      i += 1;
      continue;
    }
    const para = [];
    while (
      i < lines.length && lines[i].trim() && !/^\s*```/.test(lines[i]) && !/^#{1,4}\s/.test(lines[i]) &&
      !/^\s*([-*•]|\d+[.)])\s+/.test(lines[i]) && !isTableRow(lines[i])
    ) {
      para.push(inlineMarkdown(escapeHtml(lines[i])));
      i += 1;
    }
    out.push(`<p>${para.join("<br>")}</p>`);
  }
  return out.join("");
}

// ---------------------------------------------------------------------------
// Conversation transcript
// ---------------------------------------------------------------------------
function clearTranscript(text) {
  const empty = document.createElement("div");
  empty.className = "transcript-empty";
  empty.textContent = text;
  replyEl.replaceChildren(empty);
}

function addBubble(role) {
  replyEl.querySelector(".transcript-empty")?.remove();
  const bubble = document.createElement("div");
  bubble.className = `bubble ${role}`;
  replyEl.appendChild(bubble);
  return bubble;
}

async function sendMessage() {
  const message = messageEl.value.trim();
  const user = chatUserId();
  if (!message || sendBtn.disabled) return;

  const userBubble = addBubble("user");
  userBubble.textContent = message;
  messageEl.value = "";

  const bubble = addBubble("assistant");
  const steps = document.createElement("details");
  steps.className = "steps";
  steps.hidden = true;
  const stepsSummary = document.createElement("summary");
  const stepsList = document.createElement("ul");
  steps.append(stepsSummary, stepsList);
  const content = document.createElement("div");
  content.className = "content thinking";
  content.textContent = "Thinking…";
  const footer = document.createElement("div");
  footer.className = "bubble-footer";
  bubble.append(steps, content, footer);
  bubble.scrollIntoView({ block: "end" });

  const started = performance.now();
  const stepItems = [];
  let toolCount = 0;
  const updateSummary = () => {
    const failed = stepItems.filter((li) => li.classList.contains("failed")).length;
    stepsSummary.textContent = `Evidence: ${toolCount} tool call${toolCount === 1 ? "" : "s"}` +
      (failed ? ` · ${failed} failed` : "");
  };

  sendBtn.disabled = true;
  try {
    const response = await apiFetch("/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, user }),
    });
    if (!response.ok || !response.body) {
      const data = await response.json().catch(() => ({}));
      content.classList.remove("thinking");
      content.classList.add("error-text");
      content.textContent = data.detail || "Request failed.";
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop();
      for (const part of parts) {
        const line = part.startsWith("data: ") ? part.slice(6) : part;
        if (!line) continue;
        let evt;
        try { evt = JSON.parse(line); } catch { continue; }

        if (evt.type === "tool_start") {
          toolCount += 1;
          steps.hidden = false;
          const li = document.createElement("li");
          li.dataset.tool = evt.name;
          li.textContent = `⚙ ${evt.name}  ${JSON.stringify(evt.args || {})}`;
          stepItems.push(li);
          stepsList.appendChild(li);
          content.textContent = `Running ${evt.name}…`;
          updateSummary();
        } else if (evt.type === "tool_end") {
          const li = stepItems.find((item) => item.dataset.tool === evt.name && !item.dataset.done);
          if (li) {
            li.dataset.done = "1";
            li.textContent = li.textContent.replace(/^⚙/, evt.error ? "✕" : "✓") + `  (${evt.ms} ms)`;
            if (evt.error) li.classList.add("failed");
          }
          updateSummary();
        } else if (evt.type === "reply") {
          content.classList.remove("thinking");
          content.innerHTML = renderMarkdown(evt.content);
        } else if (evt.type === "error") {
          content.classList.remove("thinking");
          content.classList.add("error-text");
          content.textContent = evt.text;
        } else if (evt.type === "done") {
          const seconds = ((performance.now() - started) / 1000).toFixed(1);
          footer.textContent = `${seconds}s`;
          if (evt.investigation_id) {
            const link = document.createElement("button");
            link.className = "link-button";
            link.textContent = "details";
            link.addEventListener("click", () => openHistory(evt.investigation_id));
            footer.append(" · ", link);
          }
        }
      }
    }
  } catch (err) {
    content.classList.remove("thinking");
    content.classList.add("error-text");
    content.textContent = `Error: ${err}`;
  } finally {
    sendBtn.disabled = false;
    bubble.scrollIntoView({ block: "end" });
    messageEl.focus();
  }
}

sendBtn.addEventListener("click", sendMessage);
newChatBtn.addEventListener("click", async () => {
  newChatBtn.disabled = true;
  try {
    const resp = await apiFetch("/chat/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user: chatUserId() }),
    });
    clearTranscript(resp.ok ? "New conversation — NetCopilot has forgotten the previous one." : "Could not reset the conversation.");
  } catch (err) {
    clearTranscript(`Error: ${err}`);
  } finally {
    newChatBtn.disabled = false;
    messageEl.focus();
  }
});
messageEl.addEventListener("keydown", (event) => {
  // Enter sends; Shift+Enter inserts a new line.
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    sendMessage();
  }
});

// ---------------------------------------------------------------------------
// Device shortcuts and example questions
// ---------------------------------------------------------------------------
function insertIntoMessage(text) {
  const { selectionStart: a, selectionEnd: b, value } = messageEl;
  const before = value.slice(0, a);
  const spacer = before && !/\s$/.test(before) ? " " : "";
  messageEl.value = before + spacer + text + value.slice(b);
  messageEl.focus();
}

function chip(label, onClick, title) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "chip";
  btn.textContent = label;
  if (title) btn.title = title;
  btn.addEventListener("click", onClick);
  return btn;
}

async function loadDeviceShortcuts() {
  const devicesEl = document.getElementById("device-chips");
  const examplesEl = document.getElementById("examples");
  let devices = [];
  try {
    const resp = await apiFetch("/devices");
    if (resp.ok) devices = (await resp.json()).devices || [];
  } catch {}
  devicesEl.replaceChildren(...devices.slice(0, 24).map((d) =>
    chip(d.name, () => insertIntoMessage(d.name), `${d.vendor}${d.site ? " · " + d.site : ""}`)));
  const first = devices[0] ? devices[0].name : "pe1";
  const examples = [
    `Is anything down on ${first}?`,
    `Check BGP on ${first}`,
    `What changed on ${first} recently?`,
    `Check both ends of the link <interface> on ${first}`,
  ];
  examplesEl.replaceChildren(...examples.map((text) => chip(text, () => {
    messageEl.value = text;
    messageEl.focus();
  })));
}

// ---------------------------------------------------------------------------
// Investigation history
// ---------------------------------------------------------------------------
const historyOverlay = document.getElementById("history-overlay");
const historyList = document.getElementById("history-list");
const historyDetail = document.getElementById("history-detail");
const historySearch = document.getElementById("history-search");
const historyAll = document.getElementById("history-all");
const historyAllLabel = document.getElementById("history-all-label");

function metaText(text) {
  const span = document.createElement("span");
  span.textContent = text;
  return span;
}

function badge(text, cls) {
  const b = document.createElement("span");
  b.className = `badge ${cls || ""}`;
  b.textContent = text;
  return b;
}

async function loadHistoryList() {
  historyList.textContent = "Loading…";
  const params = new URLSearchParams({ user: chatUserId(), q: historySearch.value.trim(), limit: "100" });
  if (isAdmin && historyAll.checked) params.set("scope", "all");
  try {
    const data = await (await apiFetch(`/investigations?${params}`)).json();
    const items = data.investigations || [];
    if (!items.length) {
      historyList.textContent = "No investigations yet.";
      return;
    }
    historyList.replaceChildren(...items.map((item) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "history-row";
      const q = document.createElement("div");
      q.className = "history-question";
      q.textContent = item.question;
      const meta = document.createElement("div");
      meta.className = "history-meta";
      meta.append(metaText(new Date(item.created_at * 1000).toLocaleString()), badge(item.source));
      if (isAdmin && historyAll.checked) meta.append(metaText(item.user));
      if (item.severity) meta.append(badge(item.severity, `sev-${item.severity.toLowerCase()}`));
      if (item.ticket) meta.append(badge("ticket", "sev-info"));
      if (item.status === "error") meta.append(badge("error", "sev-critical"));
      meta.append(metaText(`${item.tools.length} tool${item.tools.length === 1 ? "" : "s"}`));
      row.append(q, meta);
      row.addEventListener("click", () => showInvestigation(item.id));
      return row;
    }));
  } catch (err) {
    historyList.textContent = `Could not load history: ${err}`;
  }
}

async function showInvestigation(id) {
  historyDetail.textContent = "Loading…";
  try {
    const resp = await apiFetch(`/investigations/${encodeURIComponent(id)}?user=${encodeURIComponent(chatUserId())}`);
    if (!resp.ok) {
      historyDetail.textContent = "Investigation not found.";
      return;
    }
    const inv = await resp.json();
    const title = document.createElement("h3");
    title.textContent = inv.question;
    const meta = document.createElement("div");
    meta.className = "history-meta";
    meta.append(metaText(new Date(inv.created_at * 1000).toLocaleString()), badge(inv.source),
      metaText(`${(inv.duration_ms / 1000).toFixed(1)}s`));
    if (inv.usage && inv.usage.prompt_tokens) meta.append(metaText(`${inv.usage.prompt_tokens + inv.usage.completion_tokens} tokens`));
    if (inv.ticket) meta.append(badge(inv.ticket, "sev-info"));
    const answer = document.createElement("div");
    answer.className = "content";
    answer.innerHTML = renderMarkdown(inv.reply);
    const evidence = document.createElement("div");
    evidence.className = "history-evidence";
    (inv.steps || []).forEach((step) => {
      const d = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = `${step.error ? "✕" : "✓"} ${step.name} ${JSON.stringify(step.args || {})}`;
      if (step.error) summary.classList.add("error-text");
      const pre = document.createElement("pre");
      pre.textContent = step.result;
      d.append(summary, pre);
      evidence.appendChild(d);
    });
    historyDetail.replaceChildren(title, meta, answer, evidence);
  } catch (err) {
    historyDetail.textContent = `Could not load investigation: ${err}`;
  }
}

function openHistory(id) {
  historyAllLabel.hidden = !isAdmin;
  historyOverlay.hidden = false;
  historyOverlay.classList.add("is-open");
  loadHistoryList();
  if (id) showInvestigation(id);
  else historyDetail.textContent = "Select an investigation to see its answer and evidence.";
}

function closeHistory() {
  historyOverlay.classList.remove("is-open");
  historyOverlay.hidden = true;
}

document.getElementById("history-open").addEventListener("click", () => openHistory());
document.getElementById("history-close").addEventListener("click", closeHistory);
historyOverlay.addEventListener("click", (event) => {
  if (event.target === historyOverlay) closeHistory();
});
let historySearchTimer;
historyAll.addEventListener("change", loadHistoryList);
historySearch.addEventListener("input", () => {
  clearTimeout(historySearchTimer);
  historySearchTimer = setTimeout(loadHistoryList, 250);
});

function showSignIn() {
  if (authMode === "auth0") loginBtn.hidden = false;
  if (authMode === "api_key") apiKeyForm.hidden = false;
  userBadge.textContent = "Sign-in required";
}

async function refreshUser() {
  let data = {};
  try {
    data = await (await apiFetch("/auth/me")).json();
  } catch (err) {
    userBadge.textContent = "Auth check failed";
  }
  authMode = data.auth_mode || "anonymous";
  const role = data.role || "";
  isAdmin = role === "admin";
  signedInUser = data.user && data.user.email ? data.user.email : "";
  if (signedInUser) {
    userBadge.textContent = `${data.user.name || signedInUser} (${role})`;
  } else if (data.authenticated) {
    userBadge.textContent = authMode === "api_key" ? `API key (${role})` : `Local access (${role})`;
  } else {
    userBadge.textContent = "Not signed in";
  }
  loginBtn.hidden = !(authMode === "auth0" && !signedInUser);
  logoutBtn.hidden = !(signedInUser || (authMode === "api_key" && storedApiKey()));
  apiKeyForm.hidden = !(authMode === "api_key" && !data.authenticated);
  settingsToggleBtn.hidden = !isAdmin;
  if (!isAdmin) {
    settingsOverlay.classList.remove("is-open");
    settingsOverlay.hidden = true;
  }
}

apiKeyForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const key = apiKeyInput.value.trim();
  if (!key) return;
  try { sessionStorage.setItem(API_KEY_STORAGE, key); } catch {}
  apiKeyInput.value = "";
  await refreshUser();
  if (userBadge.textContent === "Not signed in") {
    try { sessionStorage.removeItem(API_KEY_STORAGE); } catch {}
    userBadge.textContent = "Invalid API key";
    apiKeyForm.hidden = false;
  }
});

function activateSettingsSection(sectionId) {
  settingsSections.forEach((section) => {
    section.classList.toggle("active", section.id === sectionId);
  });
  Array.from(settingsNav.querySelectorAll("button")).forEach((btn) => {
    btn.classList.toggle("active", `section-${btn.dataset.section}` === sectionId);
  });
}

settingsNav.addEventListener("click", (event) => {
  const btn = event.target.closest("button[data-section]");
  if (!btn) return;
  activateSettingsSection(`section-${btn.dataset.section}`);
  if (btn.dataset.section === "posture") loadPosture();
  if (btn.dataset.section === "audit") loadAudit();
});

const STATUS_LABEL = { pass: "✓ Pass", warn: "! Warn", fail: "✕ Fail", info: "i Info" };

async function loadPosture() {
  const list = document.getElementById("posture-list");
  const summary = document.getElementById("posture-summary");
  list.textContent = "Loading…";
  try {
    const resp = await apiFetch("/admin/security-posture");
    const data = await resp.json();
    summary.replaceChildren(...Object.entries(data.summary || {}).map(([status, count]) => {
      const pill = document.createElement("span");
      pill.className = `status-${status}`;
      pill.textContent = `${count} ${status}`;
      return pill;
    }));
    list.replaceChildren(...(data.checks || []).map((check) => {
      const item = document.createElement("div");
      item.className = "posture-item";
      const badge = document.createElement("span");
      badge.className = `posture-badge status-${check.status}`;
      badge.textContent = STATUS_LABEL[check.status] || check.status;
      const body = document.createElement("div");
      const title = document.createElement("h4");
      title.textContent = check.title;
      const detail = document.createElement("p");
      detail.textContent = check.detail;
      body.append(title, detail);
      if (check.remediation && check.status !== "pass") {
        const fix = document.createElement("p");
        fix.className = "remediation";
        fix.textContent = `Fix: ${check.remediation}`;
        body.appendChild(fix);
      }
      item.append(badge, body);
      return item;
    }));
  } catch (err) {
    list.textContent = `Could not load posture: ${err}`;
  }
}

function auditSummary(evt) {
  const d = evt.detail || {};
  if (evt.event === "tool_call") return `${d.name} ${JSON.stringify(d.args || {})}${d.error ? " (error)" : ""}`;
  if (evt.event === "http_request") return `${d.method} ${d.path} → ${d.status}`;
  const { event, user, role, ...rest } = d;
  return JSON.stringify(rest).slice(0, 300);
}

async function loadAudit() {
  const table = document.getElementById("audit-table");
  table.textContent = "Loading…";
  const params = new URLSearchParams({
    hours: document.getElementById("audit-hours").value,
    event: document.getElementById("audit-event").value.trim(),
    user: document.getElementById("audit-user").value.trim(),
    limit: "300",
  });
  try {
    const data = await (await apiFetch(`/admin/audit?${params}`)).json();
    const rows = data.events || [];
    if (!rows.length) {
      table.textContent = "No audit events for this filter.";
      return;
    }
    const tbl = document.createElement("table");
    const head = tbl.createTHead().insertRow();
    ["Time", "Event", "User", "Role", "Detail"].forEach((h) => {
      const th = document.createElement("th");
      th.textContent = h;
      head.appendChild(th);
    });
    const body = tbl.createTBody();
    rows.forEach((evt) => {
      const tr = body.insertRow();
      [new Date(evt.ts * 1000).toLocaleString(), evt.event, evt.user || "", evt.role || "", auditSummary(evt)]
        .forEach((value, i) => {
          const td = tr.insertCell();
          td.textContent = value;
          if (i === 4) td.className = "detail";
        });
    });
    table.replaceChildren(tbl);
  } catch (err) {
    table.textContent = `Could not load audit trail: ${err}`;
  }
}

document.getElementById("audit-refresh").addEventListener("click", loadAudit);
document.getElementById("audit-export").addEventListener("click", async () => {
  const resp = await apiFetch("/admin/audit.csv");
  if (!resp.ok) return;
  const url = URL.createObjectURL(await resp.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = "netcopilot-audit.csv";
  link.click();
  URL.revokeObjectURL(url);
});

function createStepRow(step = {}) {
  const row = document.createElement("div");
  row.className = "step-row";

  const typeSelect = document.createElement("select");
  const stepLabels = {
    tool: "Tool",
    prometheus_query: "Prometheus query",
    syslog_search: "Syslog search",
    custom_cli: "Custom CLI",
  };
  ["tool", "prometheus_query", "syslog_search", "custom_cli"].forEach((opt) => {
    const option = document.createElement("option");
    option.value = opt;
    option.textContent = stepLabels[opt] || opt.replace("_", " ");
    typeSelect.appendChild(option);
  });
  typeSelect.value = step.type || "tool";

  const vendorSelect = document.createElement("select");
  [
    { value: "inherit", label: "Inherit vendor" },
    { value: "any", label: "Any vendor" },
    { value: "iosxr", label: "IOS XR" },
    { value: "junos", label: "Junos" },
    { value: "nokia", label: "Nokia" },
    { value: "aruba", label: "Aruba" },
  ].forEach((item) => {
    const option = document.createElement("option");
    option.value = item.value;
    option.textContent = item.label;
    vendorSelect.appendChild(option);
  });
  vendorSelect.value = step.vendor || "inherit";

  const toolSelect = document.createElement("select");
  function setToolOptions(type) {
    toolSelect.innerHTML = "";
    if (type === "tool") {
      TOOL_OPTIONS.forEach((tool) => {
        const option = document.createElement("option");
        option.value = tool;
        option.textContent = tool;
        toolSelect.appendChild(option);
      });
      toolSelect.value = step.tool || TOOL_OPTIONS[0];
      return;
    }
    if (type === "custom_cli") {
      const templates =
        CLI_TEMPLATES[vendorSelect.value] || CLI_TEMPLATES.any || [];
      const empty = document.createElement("option");
      empty.value = "";
      empty.textContent = "Choose template";
      toolSelect.appendChild(empty);
      templates.forEach((tpl) => {
        const option = document.createElement("option");
        option.value = tpl.command;
        option.textContent = tpl.label;
        toolSelect.appendChild(option);
      });
      toolSelect.value = "";
      return;
    }
  }
  setToolOptions(typeSelect.value);

  const textInput = document.createElement("input");
  textInput.placeholder = "Query or command";
  textInput.className = "compact";
  textInput.value = step.command || step.query || "";

  const limitInput = document.createElement("input");
  limitInput.type = "number";
  limitInput.min = "1";
  limitInput.placeholder = "Limit";
  limitInput.className = "compact";
  limitInput.value = step.limit || "";

  const removeBtn = document.createElement("button");
  removeBtn.textContent = "Remove";
  removeBtn.className = "secondary";
  removeBtn.addEventListener("click", () => row.remove());

  const hint = document.createElement("div");
  hint.className = "step-hint";

  function toggleFields() {
    const type = typeSelect.value;
    const showTemplate = type === "custom_cli";
    toolSelect.style.display = type === "tool" || showTemplate ? "block" : "none";
    textInput.style.display = type === "tool" ? "none" : "block";
    limitInput.style.display = type === "syslog_search" ? "block" : "none";
    if (type === "custom_cli") {
      textInput.placeholder = "Command (e.g. show interfaces {{interface}})";
      hint.textContent =
        "Custom CLI runs on the target device. Use {{device}} and other variables like {{interface}}.";
    } else if (type === "prometheus_query") {
      textInput.placeholder = "Prometheus query";
      hint.textContent =
        "Prometheus query can include placeholders like {{device}} or {{interface}}.";
    } else if (type === "syslog_search") {
      textInput.placeholder = "Syslog/Loki query";
      hint.textContent =
        "Syslog/Loki query can include placeholders like {{device}} or {{interface}}.";
    } else {
      const toolName = toolSelect.value;
      const toolInputs = TOOL_INPUTS[toolName] || [];
      const help = TOOL_HELP[toolName];
      hint.textContent = help
        ? `${help}${toolInputs.length ? ` Inputs: ${toolInputs.join(", ")}.` : ""}`
        : toolInputs.length
          ? `Uses inputs: ${toolInputs.join(", ")}`
          : "Uses tool defaults.";
    }
  }

  typeSelect.addEventListener("change", () => {
    setToolOptions(typeSelect.value);
    toggleFields();
  });
  vendorSelect.addEventListener("change", () => {
    if (typeSelect.value === "custom_cli") {
      setToolOptions("custom_cli");
    }
    toggleFields();
  });
  toolSelect.addEventListener("change", () => {
    if (typeSelect.value === "custom_cli" && toolSelect.value) {
      textInput.value = toolSelect.value;
    }
    toggleFields();
  });
  toggleFields();

  row.appendChild(typeSelect);
  row.appendChild(vendorSelect);
  row.appendChild(toolSelect);
  row.appendChild(textInput);
  row.appendChild(limitInput);
  const actionsWrap = document.createElement("div");
  actionsWrap.style.display = "flex";
  actionsWrap.style.gap = "8px";
  const testBtn = document.createElement("button");
  testBtn.textContent = "Test";
  testBtn.className = "secondary";
  actionsWrap.appendChild(testBtn);
  actionsWrap.appendChild(removeBtn);
  row.appendChild(actionsWrap);
  row.appendChild(hint);
  row._testBtn = testBtn;
  return row;
}

function createPlaybookCard(playbook = {}) {
  const card = document.createElement("div");
  card.className = "playbook-card";
  card.dataset.id = playbook.id || "";
  card._checks = playbook.checks || {};

  const header = document.createElement("div");
  header.className = "playbook-header";

  const titleInput = document.createElement("input");
  titleInput.placeholder = "Playbook title";
  titleInput.value = playbook.title || "";

  const intentInput = document.createElement("input");
  intentInput.placeholder = "Intent (e.g. bgp_down)";
  intentInput.value = playbook.intent || "";

  const vendorSelect = document.createElement("select");
  ["any", "iosxr", "junos", "nokia", "aruba"].forEach((vendor) => {
    const option = document.createElement("option");
    option.value = vendor;
    option.textContent = vendor;
    vendorSelect.appendChild(option);
  });
  vendorSelect.value = playbook.vendor || "any";

  const enabledWrap = document.createElement("label");
  enabledWrap.style.display = "inline-flex";
  enabledWrap.style.alignItems = "center";
  enabledWrap.style.gap = "6px";
  const enabledInput = document.createElement("input");
  enabledInput.type = "checkbox";
  enabledInput.checked = playbook.enabled !== false;
  enabledWrap.appendChild(enabledInput);
  enabledWrap.append("Enabled");

  header.appendChild(titleInput);
  header.appendChild(intentInput);
  header.appendChild(vendorSelect);
  header.appendChild(enabledWrap);

  const meta = document.createElement("div");
  meta.className = "playbook-meta";

  const inputsWrap = document.createElement("div");
  inputsWrap.className = "inputs-row";
  const inputsLabel = document.createElement("strong");
  inputsLabel.textContent = "Inputs (auto + custom)";
  inputsLabel.style.fontSize = "13px";
  inputsWrap.appendChild(inputsLabel);
  const tagContainer = document.createElement("div");
  tagContainer.className = "input-tags";
  inputsWrap.appendChild(tagContainer);
  const inputsControls = document.createElement("div");
  inputsControls.className = "inputs-controls";
  const quickSelect = document.createElement("select");
  const quickDefault = document.createElement("option");
  quickDefault.value = "";
  quickDefault.textContent = "Quick add input";
  quickSelect.appendChild(quickDefault);
  COMMON_INPUTS.forEach((input) => {
    const option = document.createElement("option");
    option.value = input;
    option.textContent = input;
    quickSelect.appendChild(option);
  });
  const addInputField = document.createElement("input");
  addInputField.placeholder = "Custom input (e.g., vrf, site)";
  const addInputBtn = document.createElement("button");
  addInputBtn.textContent = "Add";
  addInputBtn.className = "secondary";
  inputsControls.appendChild(quickSelect);
  inputsControls.appendChild(addInputField);
  inputsControls.appendChild(addInputBtn);
  inputsWrap.appendChild(inputsControls);
  meta.appendChild(inputsWrap);

  const inputsHelp = document.createElement("div");
  inputsHelp.className = "inputs-help";
  inputsHelp.textContent =
    "Inputs are variables used by steps (e.g., {{device}}, {{interface}}). They are filled from the user request.";
  meta.appendChild(inputsHelp);

  const inputWarning = document.createElement("div");
  inputWarning.className = "input-warning";
  inputWarning.style.display = "none";
  meta.appendChild(inputWarning);

  const usageWrap = document.createElement("div");
  usageWrap.className = "input-usage";
  meta.appendChild(usageWrap);

  const testOutput = document.createElement("div");
  testOutput.className = "test-output";
  testOutput.style.display = "none";
  meta.appendChild(testOutput);

  const stepsWrap = document.createElement("div");
  stepsWrap.className = "playbook-steps";

  const manualInputs = new Set((playbook.inputs || []).map((item) => item.trim()).filter(Boolean));
  let currentInputs = Array.from(manualInputs);

  function extractPlaceholders(text) {
    if (!text) return [];
    const matches = text.matchAll(/{{\\s*([a-zA-Z0-9_]+)\\s*}}/g);
    return Array.from(matches).map((m) => m[1]);
  }

  function computeInputsFromSteps() {
    const inputs = new Set();
    const usage = new Map();
    const addUsage = (inputName, label) => {
      if (!inputName) return;
      inputs.add(inputName);
      if (!usage.has(inputName)) {
        usage.set(inputName, new Set());
      }
      usage.get(inputName).add(label);
    };
    const rows = stepsWrap.querySelectorAll(".step-row");
    rows.forEach((row) => {
      const selects = row.querySelectorAll("select");
      const inputFields = row.querySelectorAll("input");
      const typeSelect = selects[0];
      const toolSelect = selects[2];
      const textInput = inputFields[0];
      const type = typeSelect?.value || "tool";
      if (type === "tool") {
        const toolName = toolSelect.value;
        (TOOL_INPUTS[toolName] || []).forEach((item) => addUsage(item, `tool:${toolName}`));
      } else if (type === "custom_cli") {
        const placeholders = extractPlaceholders(textInput.value);
        placeholders.forEach((item) => addUsage(item, "custom_cli"));
        addUsage("device", "custom_cli");
      } else if (type === "prometheus_query" || type === "syslog_search") {
        const placeholders = extractPlaceholders(textInput.value);
        placeholders.forEach((item) => addUsage(item, type));
      }
    });
    return {
      inputs: Array.from(inputs),
      usage,
      warnings: [],
    };
  }

  function renderTags() {
    tagContainer.innerHTML = "";
    if (!currentInputs.length) {
      const empty = document.createElement("span");
      empty.textContent = "No inputs detected yet.";
      empty.style.color = "#687489";
      empty.style.fontSize = "12px";
      tagContainer.appendChild(empty);
      return;
    }
    currentInputs.forEach((input) => {
      const tag = document.createElement("div");
      tag.className = "input-tag";
      tag.textContent = input;
      if (card._usageMap && card._usageMap.has(input)) {
        tag.title = `Used by: ${Array.from(card._usageMap.get(input)).join(", ")}`;
      }
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "×";
      remove.addEventListener("click", () => {
        manualInputs.delete(input);
        syncInputs();
      });
      tag.appendChild(remove);
      tagContainer.appendChild(tag);
    });
  }

  function syncInputs() {
    const computed = computeInputsFromSteps();
    const merged = new Set([...computed.inputs, ...manualInputs]);
    currentInputs = Array.from(merged);
    currentInputs.sort();
    card._usageMap = computed.usage;
    renderTags();
    const warnings = computed.warnings || [];
    if (warnings.length) {
      inputWarning.textContent = warnings.join(" ");
      inputWarning.style.display = "block";
    } else {
      inputWarning.textContent = "";
      inputWarning.style.display = "none";
    }
    usageWrap.innerHTML = "";
    currentInputs.forEach((input) => {
      const row = document.createElement("div");
      const usage = card._usageMap && card._usageMap.get(input);
      row.textContent = `${input}: ${usage ? Array.from(usage).join(", ") : "manual"}`;
      usageWrap.appendChild(row);
    });
  }

  function collectStepFromRow(row) {
    const selects = row.querySelectorAll("select");
    const inputs = row.querySelectorAll("input");
    const typeSelect = selects[0];
    const vendorSelect = selects[1];
    const toolSelect = selects[2];
    const textInput = inputs[0];
    const limitInput = inputs[1];
    const type = typeSelect.value;
    const stepVendor = vendorSelect.value;
    const payload = { type };
    if (stepVendor && stepVendor !== "inherit") {
      payload.vendor = stepVendor;
    }
    if (type === "tool") {
      payload.tool = toolSelect.value;
      return payload;
    }
    if (type === "syslog_search") {
      payload.query = textInput.value.trim();
      payload.limit = limitInput.value ? Number(limitInput.value) : 50;
      return payload;
    }
    if (type === "custom_cli") {
      payload.command = textInput.value.trim();
      return payload;
    }
    payload.query = textInput.value.trim();
    return payload;
  }

  function requiredInputsForStep(step) {
    if (step.type === "tool") {
      return TOOL_INPUTS[step.tool] || [];
    }
    if (step.type === "custom_cli") {
      const placeholders = extractPlaceholders(step.command || "");
      const set = new Set(["device", ...placeholders]);
      return Array.from(set);
    }
    if (step.type === "prometheus_query" || step.type === "syslog_search") {
      return extractPlaceholders(step.query || "");
    }
    return [];
  }

  function attachTestHandler(row) {
    if (!row._testBtn) return;
    row._testBtn.addEventListener("click", async () => {
      const stepPayload = collectStepFromRow(row);
      const required = requiredInputsForStep(stepPayload);
      const inputs = {};
      for (const key of required) {
        const value = window.prompt(`Enter value for ${key}`);
        if (value === null) {
          return;
        }
        if (value.trim()) {
          inputs[key] = value.trim();
        }
      }
      testOutput.style.display = "block";
      testOutput.textContent = "Running test step...";
      try {
        const resp = await apiFetch("/admin/playbooks/test-step", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            step: stepPayload,
            inputs,
            playbook_vendor: vendorSelect.value,
          }),
        });
        const data = await resp.json();
        if (!resp.ok) {
          testOutput.textContent = data.detail || JSON.stringify(data);
        } else {
          testOutput.textContent = JSON.stringify(data.result, null, 2);
        }
      } catch (err) {
        testOutput.textContent = `Test failed: ${err}`;
      }
    });
  }

  addInputBtn.addEventListener("click", () => {
    const value = addInputField.value.trim();
    if (!value) return;
    manualInputs.add(value);
    addInputField.value = "";
    syncInputs();
  });

  quickSelect.addEventListener("change", () => {
    if (!quickSelect.value) return;
    manualInputs.add(quickSelect.value);
    quickSelect.value = "";
    syncInputs();
  });

  addInputField.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      addInputBtn.click();
    }
  });

  const steps = playbook.steps || [];
  if (steps.length === 0) {
    const row = createStepRow();
    stepsWrap.appendChild(row);
    attachTestHandler(row);
    syncInputs();
  } else {
    steps.forEach((step) => {
      const row = createStepRow(step);
      stepsWrap.appendChild(row);
      attachTestHandler(row);
    });
    syncInputs();
  }

  const stepActions = document.createElement("div");
  stepActions.className = "playbook-actions";
  const addStepBtn = document.createElement("button");
  addStepBtn.textContent = "Add step";
  addStepBtn.addEventListener("click", () => {
    const row = createStepRow();
    stepsWrap.appendChild(row);
    attachTestHandler(row);
    syncInputs();
  });
  const deleteBtn = document.createElement("button");
  deleteBtn.textContent = "Delete playbook";
  deleteBtn.className = "secondary";
  deleteBtn.addEventListener("click", async () => {
    const playbookId = card.dataset.id;
    if (playbookId) {
      try {
        await apiFetch(`/admin/playbooks/${playbookId}`, { method: "DELETE" });
      } catch (err) {
        playbooksStatus.textContent = "Delete failed.";
        return;
      }
    }
    card.remove();
  });
  stepActions.appendChild(addStepBtn);
  stepActions.appendChild(deleteBtn);

  card.appendChild(header);
  card.appendChild(meta);
  card.appendChild(stepsWrap);
  card.appendChild(stepActions);

  card._collect = () => {
    const stepRows = Array.from(stepsWrap.querySelectorAll(".step-row"));
    const collectedSteps = stepRows.map((row) => {
      const selects = row.querySelectorAll("select");
      const inputs = row.querySelectorAll("input");
      const typeSelect = selects[0];
      const vendorSelect = selects[1];
      const toolSelect = selects[2];
      const textInput = inputs[0];
      const limitInput = inputs[1];
      const type = typeSelect.value;
      const stepVendor = vendorSelect.value;
      if (type === "tool") {
        const payload = { type, tool: toolSelect.value };
        if (stepVendor && stepVendor !== "inherit") {
          payload.vendor = stepVendor;
        }
        return payload;
      }
      if (type === "syslog_search") {
        const payload = {
          type,
          query: textInput.value.trim(),
          limit: limitInput.value ? Number(limitInput.value) : 50,
        };
        if (stepVendor && stepVendor !== "inherit") {
          payload.vendor = stepVendor;
        }
        return payload;
      }
      if (type === "custom_cli") {
        const payload = { type, command: textInput.value.trim() };
        if (stepVendor && stepVendor !== "inherit") {
          payload.vendor = stepVendor;
        }
        return payload;
      }
      const payload = { type, query: textInput.value.trim() };
      if (stepVendor && stepVendor !== "inherit") {
        payload.vendor = stepVendor;
      }
      return payload;
    });
    const inputs = currentInputs;
    return {
      id: card.dataset.id || undefined,
      title: titleInput.value.trim(),
      intent: intentInput.value.trim(),
      vendor: vendorSelect.value,
      enabled: enabledInput.checked,
      inputs,
      steps: collectedSteps,
      checks: card._checks,
    };
  };

  stepsWrap.addEventListener("change", syncInputs);
  stepsWrap.addEventListener("keyup", syncInputs);
  syncInputs();

  return card;
}

async function loadPlaybooks() {
  if (!isAdmin) {
    playbooksList.innerHTML = "";
    return;
  }
  playbooksStatus.textContent = "Loading playbooks...";
  try {
    const resp = await apiFetch("/admin/playbooks");
    const data = await resp.json();
    playbooksList.innerHTML = "";
    (data.playbooks || []).forEach((playbook) => {
      playbooksList.appendChild(createPlaybookCard(playbook));
    });
    playbooksStatus.textContent = data.playbooks?.length
      ? `${data.playbooks.length} playbooks loaded.`
      : "No playbooks configured yet.";
  } catch (err) {
    playbooksStatus.textContent = "Failed to load playbooks.";
  }
}

function collectPlaybooks() {
  return Array.from(playbooksList.querySelectorAll(".playbook-card")).map(
    (card) => card._collect()
  );
}

function customSettingRow(key, value) {
  const row = document.createElement("div");
  row.className = "settings-row";
  const left = document.createElement("div");
  const name = document.createElement("strong");
  name.textContent = key;
  const tag = document.createElement("small");
  tag.textContent = "custom";
  left.append(name, tag);
  const right = document.createElement("div");
  const input = document.createElement("input");
  input.type = "text";
  input.dataset.key = key;
  input.value = value || "";
  right.appendChild(input);
  row.append(left, right);
  return row;
}

async function loadSettings() {
  try {
    const resp = await apiFetch("/admin/settings");
    const data = await resp.json();
    settingsState.clear();
    Object.values(SETTINGS_SECTION_IDS).forEach((sectionId) => {
      const section = document.getElementById(sectionId);
      if (!section) return;
      const target = section.querySelector(".settings-section-body") || section;
      target.querySelectorAll(".settings-group.dynamic").forEach((group) => group.remove());
      section.querySelectorAll(".section-status").forEach((el) => (el.textContent = ""));
    });
    if (customSettingsList) {
      customSettingsList.innerHTML = "";
    }

    const grouped = new Map();
    (data.settings || []).forEach((item) => {
      const category = item.category || "General";
      if (!grouped.has(category)) {
        grouped.set(category, []);
      }
      grouped.get(category).push(item);
    });

    grouped.forEach((items, category) => {
      const sectionId = SETTINGS_SECTION_IDS[category] || "section-system";
      const section = document.getElementById(sectionId);
      if (!section) return;
      const target = section.querySelector(".settings-section-body") || section;
      const group = document.createElement("div");
      group.className = "settings-group dynamic";
      const heading = document.createElement("h3");
  heading.textContent = category;
  group.appendChild(heading);
      const grid = document.createElement("div");
      grid.className = "settings-grid";
      group.appendChild(grid);
      items.forEach((item) => {
        const row = document.createElement("div");
        row.className = "settings-row";
        const left = document.createElement("div");
        const label = document.createElement("strong");
    label.textContent = item.label;
    const keyName = document.createElement("small");
    keyName.textContent = item.key;
    left.append(label, keyName);
        const right = document.createElement("div");
        const input = document.createElement("input");
        input.type = item.secret ? "password" : "text";
        input.placeholder = item.secret && item.has_value ? "••••••••" : "";
        input.value = item.secret ? "" : item.value || "";
        input.dataset.key = item.key;
        right.appendChild(input);
        if (item.secret) {
          const hint = document.createElement("div");
          hint.className = "secret-hint";
          hint.textContent = item.has_value ? "Saved" : "Not set";
          right.appendChild(hint);
        }
        row.appendChild(left);
        row.appendChild(right);
        grid.appendChild(row);
        settingsState.set(item.key, item);
      });
      target.appendChild(group);
    });

    const custom = data.custom_settings || {};
    Object.entries(custom).forEach(([key, value]) => {
      if (settingsState.has(key)) {
        return;
      }
      const row = document.createElement("div");
      row.className = "settings-row";
      row.innerHTML = `
        <div><strong>${key}</strong><small>custom</small></div>
        <div><input type="text" data-key="${key}" value="${value}" /></div>
      `;
      customSettingsList.appendChild(row);
      settingsState.set(key, { key, secret: false });
    });

    await loadPlaybooks();
    await loadInventoryPreview();
  } catch (err) {
    settingsStatus.textContent = "Failed to load settings.";
  }
}

async function loadInventoryPreview() {
  if (!inventoryPreviewEl) return;
  inventoryPreviewEl.textContent = "Loading devices...";
  try {
    const resp = await apiFetch("/admin/inventory");
    if (!resp.ok) {
      const text = await resp.text();
      inventoryPreviewEl.textContent = text || `Failed to load inventory (${resp.status}).`;
      return;
    }
    const data = await resp.json();
    const rows = (data.devices || []).map((d) => ({
      vendor: d.vendor || "",
      name: d.name || "",
      target: d.port ? `${d.host}:${d.port}` : d.host || "",
      source: d.source || "",
    }));

    if (!rows.length) {
      inventoryPreviewEl.textContent = "No devices loaded yet.";
      return;
    }

    const esc = (s) =>
      String(s || "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");

    inventoryPreviewEl.innerHTML = `
      <table>
        <thead><tr><th>Vendor</th><th>Name</th><th>Target</th><th>Source</th></tr></thead>
        <tbody>
          ${rows
            .map(
              (r) =>
                `<tr><td>${esc(r.vendor)}</td><td>${esc(r.name)}</td><td>${esc(r.target)}</td><td>${esc(r.source)}</td></tr>`
            )
            .join("")}
        </tbody>
      </table>
    `;
  } catch (err) {
    inventoryPreviewEl.textContent = `Inventory load error: ${err}`;
  }
}

settingsToggleBtn.addEventListener("click", async () => {
  settingsOverlay.hidden = false;
  settingsOverlay.classList.add("is-open");
  activateSettingsSection("section-llm");
  await loadSettings();
});

closeSettingsBtn.addEventListener("click", () => {
  settingsOverlay.classList.remove("is-open");
  settingsOverlay.hidden = true;
});

settingsOverlay.addEventListener("click", (event) => {
  if (event.target === settingsOverlay) {
    settingsOverlay.classList.remove("is-open");
    settingsOverlay.hidden = true;
  }
});

addSettingBtn.addEventListener("click", () => {
  const key = customKeyEl.value.trim();
  if (!key) {
    settingsStatus.textContent = "Enter a custom key.";
    return;
  }
  const row = customSettingRow(key, customValueEl.value.trim());
  customSettingsList.appendChild(row);
  settingsState.set(key, { key, secret: false });
  customKeyEl.value = "";
  customValueEl.value = "";
  settingsStatus.textContent = "Custom setting added.";
});

addPlaybookBtn.addEventListener("click", () => {
  playbooksList.appendChild(createPlaybookCard());
  playbooksStatus.textContent = "New playbook added.";
});

savePlaybooksBtn.addEventListener("click", async () => {
  playbooksStatus.textContent = "Saving playbooks...";
  const payload = collectPlaybooks();
  try {
    const resp = await apiFetch("/admin/playbooks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ playbooks: payload }),
    });
    const data = await resp.json();
    if (!resp.ok || data.status !== "ok") {
      playbooksStatus.textContent = data.detail || JSON.stringify(data);
      return;
    }
    playbooksStatus.textContent = `Saved ${data.count} playbooks.`;
    await loadPlaybooks();
  } catch (err) {
    playbooksStatus.textContent = `Save error: ${err}`;
  }
});

function sectionStatus(sectionEl) {
  return sectionEl.querySelector(".section-status") || settingsStatus;
}

function collectSectionPayload(sectionEl) {
  const payload = {};
  const inputs = sectionEl.querySelectorAll("input[data-key]");
  inputs.forEach((input) => {
    const key = input.dataset.key;
    const meta = settingsState.get(key) || {};
    const value = input.value;
    // Secret fields: only send when user typed a new value.
    if (meta.secret && !value) return;
    // If custom setting looks masked, don't overwrite.
    if (!meta.secret && value === "********" && /secret|token|pass|key/i.test(key)) return;
    payload[key] = value;
  });
  return payload;
}

async function saveSection(sectionEl) {
  const statusEl = sectionStatus(sectionEl);
  const payload = collectSectionPayload(sectionEl);
  const keys = Object.keys(payload);
  if (!keys.length) {
    statusEl.textContent = "No changes to save.";
    return;
  }
  statusEl.textContent = "Saving...";
  try {
    const resp = await apiFetch("/admin/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings: payload }),
    });
    let data = null;
    try {
      data = await resp.json();
    } catch (parseErr) {
      data = null;
    }
    if (!resp.ok || !data || data.status !== "ok") {
      statusEl.textContent =
        (data && (data.detail || JSON.stringify(data))) || `Save failed (${resp.status}).`;
      return;
    }
    const updated = Array.isArray(data.updated) ? data.updated : [];
    statusEl.textContent = updated.length
      ? `Saved (${updated.length}): ${updated.join(", ")}`
      : "Saved.";
    await loadSettings();
  } catch (err) {
    statusEl.textContent = `Error: ${err}`;
  }
}

settingsCard.addEventListener("click", async (event) => {
  const btn = event.target.closest(".save-section");
  if (!btn) return;
  const sectionId = btn.dataset.section;
  const sectionEl = document.getElementById(sectionId);
  if (!sectionEl) return;
  await saveSection(sectionEl);
});

reloadInventoryBtn.addEventListener("click", async () => {
  const inventorySection = document.getElementById("section-inventory");
  const statusEl = inventorySection ? sectionStatus(inventorySection) : settingsStatus;
  statusEl.textContent = "Reloading inventory...";
  try {
    const resp = await apiFetch("/admin/reload-inventory", { method: "POST" });
    if (!resp.ok) {
      const text = await resp.text();
      statusEl.textContent = `Reload failed: ${text}`;
      return;
    }
    const data = await resp.json();
    const counts = data.counts || {};
    statusEl.textContent = `Inventory reloaded (cisco=${counts.cisco || 0}, juniper=${counts.juniper || 0}, nokia=${counts.nokia || 0}).`;
    await loadInventoryPreview();
  } catch (err) {
    statusEl.textContent = `Reload error: ${err}`;
  }
});

if (refreshInventoryPreviewBtn) {
  refreshInventoryPreviewBtn.addEventListener("click", async () => {
    await loadInventoryPreview();
  });
}

importInventoryBtn.addEventListener("click", async () => {
  const inventorySection = document.getElementById("section-inventory");
  const statusEl = inventorySection ? sectionStatus(inventorySection) : settingsStatus;
  if (!inventoryFileEl.files.length) {
    statusEl.textContent = "Choose a CSV file first.";
    return;
  }
  statusEl.textContent = "Uploading CSV...";
  const formData = new FormData();
  formData.append("file", inventoryFileEl.files[0]);
  try {
    const resp = await apiFetch("/admin/inventory/import-csv", {
      method: "POST",
      body: formData,
    });
    let data = null;
    try {
      data = await resp.json();
    } catch (parseErr) {
      const text = await resp.text();
      statusEl.textContent = text || "Import failed.";
      return;
    }
    if (!resp.ok || data.status !== "ok") {
      statusEl.textContent = data.detail || JSON.stringify(data);
      return;
    }
    statusEl.textContent = `Imported: ${data.added} added, ${data.updated} updated, ${data.skipped} skipped.`;
    inventoryFileEl.value = "";
    await loadSettings();
  } catch (err) {
    statusEl.textContent = `Import error: ${err}`;
  }
});

loginBtn.addEventListener("click", () => {
  window.location.href = "/auth/login";
});

logoutBtn.addEventListener("click", async () => {
  if (storedApiKey()) {
    try { sessionStorage.removeItem(API_KEY_STORAGE); } catch {}
    await refreshUser();
    return;
  }
  window.location.href = "/auth/logout";
});

refreshUser().then(loadDeviceShortcuts);
