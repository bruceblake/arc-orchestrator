"use strict";
// ---- captain panel ----
// The conversational supervisor over the governed fleet: talk to it, it reads
// LIVE fleet state (task counts, the three concurrency layers, recent events)
// and issues a bounded set of actions (plan / run / resume / status / amend)
// that the backend executes as fixed `main.py` argv. Backend routes:
//   GET  /api/captain/state    live snapshot + session list
//   GET  /api/captain/poll     turns for one session (since=)
//   GET  /api/captain/queue    work the captain queued for capacity
//   POST /api/captain/start    append the operator turn + spawn one turn
// It reuses the chat panel's repo/session helpers (chatLoadRepos,
// chatSessionFor, chatFreshSession) — one allowlist, one slug rule.

let CAP_REPO = "";
let CAP_SESSION = "";
let CAP_SESSIONS = [];
let CAP_TURNS = [];
let CAP_LAST = 0;
let CAP_RUNNING = false;
let CAP_POLL = null;
let CAP_STATE = null;
let CAP_THINKING = null;   // {pending, blocks} from the running turn's transcript

// Captain sessions are `captain-<repo slug>` so they share the chat panel's
// session id rules (^[a-z0-9][a-z0-9-]{0,39}$) but never collide with a
// planning chat, which is `plan-<repo slug>`.
function capSessionFor(path) {
  const slug = chatSessionFor(path).replace(/^plan-/, "").slice(0, 31);
  return "captain-" + (slug || "repo");
}

function capSetSend(busy) {
  const s = $("#k-send");
  if (!s) return;
  s.disabled = !!busy;
  s.textContent = busy ? "…" : "send";
}

function capSetSession(id) {
  CAP_SESSION = id || "";
  const head = $("#k-session");
  if (head) head.textContent = CAP_SESSION;
  const sel = $("#k-sessions");
  if (sel && CAP_SESSION) sel.value = CAP_SESSION;
}

async function capStartSession(id) {
  capSetSession(id || capSessionFor(CAP_REPO));
  CAP_TURNS = [];
  CAP_LAST = 0;
  CAP_RUNNING = false;
  capSetSend(false);
  const log = $("#k-log");
  if (log) log.innerHTML = capEmptyState();
  await capPoll();
}

// ---- state strip ----
function capCapacityHTML() {
  const cap = (CAP_STATE && CAP_STATE.capacity) || {};
  const models = Object.keys(cap);
  if (!models.length) return `<div class="cap-empty">no capacity data</div>`;
  return models.map(m => {
    const c = cap[m];
    const used = c.in_use || 0, dcap = c.driver_cap || 0;
    const full = dcap > 0 && used >= dcap;
    const pct = dcap > 0 ? Math.min(100, Math.round(used / dcap * 100)) : 0;
    const hc = c.harness_cap != null ? ` · ${esc(c.harness)} ${esc(String(c.harness_cap))}` : "";
    return `<div class="cap-row${full ? " full" : ""}">`
      + `<span class="cap-name">${esc(short(m))}</span>`
      + `<span class="cap-bar"><span style="width:${pct}%"></span></span>`
      + `<span class="cap-num">${used}/${dcap} slots${hc} · acct ${esc(String(c.account))}</span>`
      + `</div>`;
  }).join("");
}

function capAttentionHTML() {
  const att = (CAP_STATE && CAP_STATE.needs_attention) || [];
  if (!att.length) return `<div class="cap-ok">✓ nothing needs attention</div>`;
  return att.map(r => `<div class="cap-att">`
    + `<span class="cap-att-status ${esc(r.status || "")}">${esc(r.status || "?")}</span> `
    + `<b>${esc(r.id || "?")}</b> `
    + `<span class="hint">${esc((r.taskfile || "").split("/").pop() || "")}</span>`
    + (r.error ? ` <span class="cap-att-err">${esc(r.error)}</span>` : "")
    + `</div>`).join("");
}

function capCountsHTML() {
  const counts = (CAP_STATE && CAP_STATE.task_status_counts) || {};
  const keys = Object.keys(counts).sort();
  if (!keys.length) return "";
  return keys.map(k => chip(k, counts[k])).join(" ");
}

function capRenderState() {
  const el = $("#k-state");
  if (!el) return;
  el.innerHTML = `<div class="cap-row-head">`
    + `<span class="cap-title">fleet state</span>`
    + `<span class="hint">${esc((CAP_STATE && CAP_STATE.planner_model) || "")}</span></div>`
    + `<div class="cap-chips">${capCountsHTML()}</div>`
    + `<div class="cap-caps">${capCapacityHTML()}</div>`
    + `<div class="cap-att-wrap">${capAttentionHTML()}</div>`;
}

async function capLoadState() {
  try {
    CAP_STATE = await jget("/api/captain/state");
  } catch (e) { return; }
  capRenderState();
  const sessions = (CAP_STATE && CAP_STATE.sessions) || [];
  CAP_SESSIONS = sessions;
  const sel = $("#k-sessions");
  if (sel) {
    const known = sessions.slice();
    if (CAP_SESSION && !known.some(s => s.name === CAP_SESSION)) {
      known.unshift({ name: CAP_SESSION, turns: CAP_TURNS.length, mtime: 0 });
    }
    sel.innerHTML = known.map(s => `<option value="${attr(s.name)}">${esc(s.name)} (${s.turns})</option>`).join("");
    sel.value = CAP_SESSION;
  }
}

// ---- transcript ----
function capActionsHTML(actions) {
  if (!actions || !actions.length) return "";
  return actions.map(a => {
    const ok = a.ok !== false;
    const cls = ok ? "ok" : (a.queued ? "queued" : "bad");
    const label = a.kind + (a.taskfile ? " " + a.taskfile : "");
    const note = a.note || a.error || "";
    return `<div class="cap-act ${cls}">`
      + `<span class="cap-act-kind">${esc(label)}</span>`
      + (note ? `<span class="cap-act-note">${esc(note)}</span>` : "")
      + `</div>`;
  }).join("");
}

function capTurnHTML(turn) {
  const who = turn.role === "user" ? "user" : "assistant";
  const parts = [];
  if (turn.text) parts.push(`<div class="chat-bubble">${esc(turn.text)}</div>`);
  if (turn.actions && turn.actions.length) parts.push(capActionsHTML(turn.actions));
  if (turn.error) parts.push(`<div class="chat-err">${esc(turn.error)}</div>`);
  const ts = turn.ts ? `<span class="chat-ts">${AGO(turn.ts)}</span>` : "";
  if (!parts.length) return `<div class="chat-turn ${who}"><div class="chat-bubble dim">…</div>${ts}</div>`;
  return `<div class="chat-turn ${who}">${parts.join("")}${ts}</div>`;
}

function capThinkingHTML() {
  // While a turn runs, show WHAT IT IS DOING: the transcript reducer's live
  // "thinking" line plus the last few readable blocks (tool calls/results), so
  // a 2-4 minute GLM turn is legible from the first seconds instead of a
  // static spinner. Falls back to the spinner when the transcript is not yet
  // readable (first moment after send).
  const t = CAP_THINKING;
  const pending = (t && t.pending) || "";
  const blocks = (t && t.blocks) || [];
  const head = `<div class="chat-thinking-head"><span class="chat-spin"></span> captain is working…</div>`;
  const pend = pending
    ? `<div class="cap-think-pending">${esc(pending)}</div>`
    : "";
  const body = blocks.length
    ? `<div class="cap-think-blocks">` + blocks.slice(-6).map(b =>
        `<div class="cap-think-block">${esc(capBlockText(b))}</div>`).join("") + `</div>`
    : (pending ? "" : `<div class="cap-think-idle">captain is reviewing the fleet…</div>`);
  return `<div class="chat-turn assistant"><div class="chat-bubble dim chat-thinking">`
    + head + pend + body + `</div></div>`;
}

// A reducer block is either a string or an object; render one line of it.
function capBlockText(b) {
  if (typeof b === "string") return b;
  if (b && typeof b === "object") {
    return b.text || b.label || b.summary || (b.kind ? "[" + b.kind + "]" : JSON.stringify(b));
  }
  return String(b);
}

function capRender() {
  const log = $("#k-log");
  if (!log) return;
  const body = CAP_TURNS.length ? CAP_TURNS.map(capTurnHTML).join("") : capEmptyState();
  log.innerHTML = body + (CAP_RUNNING ? capThinkingHTML() : "");
  log.scrollTop = log.scrollHeight;
}

function capEmptyState() {
  return `<div class="chat-empty">Captain online. Ask it what the fleet is doing, tell it what to build, or ask it to keep the project on track — it reads live state and can plan, run, and resume work without you touching the CLI.</div>`;
}

async function capPoll() {
  if (!CAP_SESSION) return;
  let data = null;
  try {
    data = await jget("/api/captain/poll?session=" + encodeURIComponent(CAP_SESSION) + "&since=" + CAP_LAST);
  } catch (e) {
    CAP_RUNNING = false;
    CAP_THINKING = null;
    capSetSend(false);
    capRender();
    return;
  }
  const turns = (data && data.turns) || [];
  if (turns.length) {
    CAP_TURNS = CAP_TURNS.concat(turns);
    CAP_LAST += turns.length;
  }
  CAP_RUNNING = !!(data && data.running);
  CAP_THINKING = CAP_RUNNING ? ((data && data.thinking) || CAP_THINKING) : null;
  capSetSend(CAP_RUNNING);
  capRender();
}

async function capSend() {
  const input = $("#k-text");
  const msg = $("#k-msg");
  if (!input) return;
  const text = input.value;
  if (!text.trim() || CAP_RUNNING) return;
  const { code, body: resp } = await jpost("/api/captain/start",
    { session: CAP_SESSION, repo: CAP_REPO, message: text });
  if (code === 200) {
    input.value = "";
    CAP_RUNNING = true;
    capSetSend(true);
    capRender();
    capPoll();
  } else if (code === 409) {
    if (msg) { msg.className = "err"; msg.textContent = (resp.error || "a captain turn is already running"); }
    capPoll();
  } else {
    if (msg) { msg.className = "err"; msg.textContent = (resp.error || "failed to send"); }
  }
}

function capSelectSession(name) {
  if (!name || name === CAP_SESSION) return;
  stopCapPoll();
  capStartSession(name).then(startCapPoll);
}

async function capNewSession() {
  stopCapPoll();
  await capLoadState();
  await capStartSession(chatFreshSession(
    capSessionFor(CAP_REPO),
    CAP_SESSIONS.map(s => s.name)));
  await capLoadState();
  startCapPoll();
}

// ---- poll lifecycle ----
function startCapPoll() {
  if (CAP_POLL) clearInterval(CAP_POLL);
  CAP_POLL = setInterval(() => { capPoll(); capLoadState(); }, 4000);
}
function stopCapPoll() { if (CAP_POLL) { clearInterval(CAP_POLL); CAP_POLL = null; } }

// ---- open / close ----
async function capOpen() {
  const modal = $("#capmodal");
  if (modal) modal.classList.add("open");
  const msg = $("#k-msg");
  if (msg) { msg.className = ""; msg.textContent = ""; }
  await chatLoadRepos();
  CAP_REPO = CHAT_REPO;
  const sel = $("#k-repo");
  if (sel) {
    sel.innerHTML = CHAT_REPOS.map(r => `<option value="${attr(r.path)}">${esc(r.name)}</option>`).join("");
    sel.value = CAP_REPO;
  }
  await capLoadState();
  await capStartSession(capSessionFor(CAP_REPO));
  startCapPoll();
}

function capClose() {
  const modal = $("#capmodal");
  if (modal) modal.classList.remove("open");
  stopCapPoll();
}

// ---- wire up ----
(function capInit() {
  const modal = $("#capmodal");
  const btn = $("#btn-captain");
  if (btn) btn.onclick = capOpen;
  const close = $("#k-close");
  if (close) close.onclick = capClose;
  const send = $("#k-send");
  if (send) send.onclick = capSend;
  const repo = $("#k-repo");
  if (repo) repo.onchange = () => {
    CAP_REPO = repo.value;
    capStartSession(capSessionFor(CAP_REPO));
    capLoadState();
  };
  const sessions = $("#k-sessions");
  if (sessions) sessions.onchange = () => capSelectSession(sessions.value);
  const fresh = $("#k-new");
  if (fresh) fresh.onclick = capNewSession;
  const text = $("#k-text");
  if (text) text.onkeydown = ev => { if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); capSend(); } };
  if (modal) modal.onclick = ev => { if (ev.target === modal) capClose(); };
})();
