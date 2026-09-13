"use strict";
// ---- plan-with-the-fleet-planner chat panel ----
// A conversational front to `main.py chat` over the orchestration repo.
// Chat with the planner; a turn that produced a taskfile shows an action card
// that can open the project or run the generated taskfile directly.
// Backend is read-mostly in this panel: it only POSTs /api/chat/start and
// /api/projects/run, exactly what the dashboard already exposes.

// Speech is optional and must be aliased — `new window.SpeechRecognition()`
// is the only valid construction, and the constructor may not exist at all.
function chatSR() {
  const W = (typeof window !== "undefined") ? window : null;
  return W ? (W.SpeechRecognition || W.webkitSpeechRecognition) : null;
}
function chatSupportsSpeech() { return !!(chatSR()); }

// Session ids are `plan-` + a kebab slug of the repo path's last segment,
// always matching the backend's ^[a-z0-9][a-z0-9-]{0,39}$ rule.
function chatSlug(s) {
  return String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
}
function chatSessionFor(path) {
  const parts = String(path || "").split("/").filter(Boolean);
  let slug = chatSlug(parts[parts.length - 1] || "");
  slug = slug.slice(0, 34).replace(/-+$/g, "");
  return "plan-" + (slug || "repo");
}

// ---- state ----
let CHAT_REPO = "";        // selected repo path (the picker value)
let CHAT_REPOS = [];       // all repos from /api/repos
let CHAT_SESSION = "";     // active session id
let CHAT_TURNS = [];       // turns rendered so far
let CHAT_LAST = 0;         // how many turns already drawn (poll since=)
let CHAT_RUNNING = false;  // whether a chat turn is in flight
let CHAT_POLL = null;      // setInterval for live polling
let CHAT_RECOG = null;     // live SpeechRecognition instance
let CHAT_LISTENING = false;
let CHAT_SPEECH_FINAL = ""; // accumulated final speech transcripts

function chatSetSend(busy) {
  const s = $("#c-send");
  if (!s) return;
  s.disabled = !!busy;
  s.textContent = busy ? "…" : "send";
}

function chatStartSession() {
  CHAT_SESSION = chatSessionFor(CHAT_REPO);
  const sess = $("#c-session");
  if (sess) sess.textContent = CHAT_SESSION;
  CHAT_TURNS = [];
  CHAT_LAST = 0;
  CHAT_RUNNING = false;
  chatSetSend(false);
  $("#c-log").innerHTML = chatEmptyState();
}

function chatUpdateMic() {
  const mic = $("#c-mic");
  if (!mic) return;
  mic.style.display = chatSupportsSpeech() ? "" : "none";
  mic.classList.toggle("listening", CHAT_LISTENING);
}

// ---- transcript rendering ----
function chatTaskcardHTML(file) {
  if (!file) return "";
  return `<div class="chat-taskcard"><div class="ctc">plan ready: <b>${esc(file)}</b></div>`
    + `<button class="act" data-ct-open="${attr(file)}">open project</button>`
    + `<button class="act primary" data-ct-run="${attr(file)}">run</button></div>`;
}

function chatTurnHTML(turn) {
  const who = turn.role === "user" ? "user" : "assistant";
  const parts = [];
  if (turn.text) parts.push(`<div class="chat-bubble">${esc(turn.text)}</div>`);
  if (turn.error) parts.push(`<div class="chat-err">${esc(turn.error)}</div>`);
  const card = chatTaskcardHTML(turn.taskfile);
  if (card) parts.push(card);
  const ts = turn.ts ? `<span class="chat-ts">${AGO(turn.ts)}</span>` : "";
  if (!parts.length) return `<div class="chat-turn ${who}"><div class="chat-bubble dim">…</div>${ts}</div>`;
  return `<div class="chat-turn ${who}">${parts.join("")}${ts}</div>`;
}

function chatThinkingHTML() {
  return `<div class="chat-turn assistant"><div class="chat-bubble dim chat-thinking"><span class="chat-spin"></span> ${esc(PLANNER_SHORT)} is thinking…</div></div>`;
}

function chatRender() {
  const log = $("#c-log");
  if (!log) return;
  const body = CHAT_TURNS.length ? CHAT_TURNS.map(chatTurnHTML).join("") : chatEmptyState();
  log.innerHTML = body + (CHAT_RUNNING ? chatThinkingHTML() : "");
  log.scrollTop = log.scrollHeight;
}

function chatEmptyState() {
  return `<div class="chat-empty">Describe what you want built. ${esc(PLANNER_SHORT)} will turn it into a governed project — tasks, model routing, verify gates, cross-review — and hand it back ready to run.</div>`;
}

// ---- network ----
const CHAT_NEW = "__newrepo__";  // picker value that means "create a repo here"
async function chatLoadRepos() {
  let repos = [];
  try { const data = await jget("/api/repos"); repos = (data && data.repos) || []; }
  catch (e) { repos = []; }
  CHAT_REPOS = repos;
  const sel = $("#c-repo");
  if (!sel) return;
  sel.innerHTML = repos.map(r => `<option value="${attr(r.path)}">${esc(r.name)}</option>`).join("")
    + `<option value="${CHAT_NEW}">new repo…</option>`;
  if (!CHAT_REPO || !repos.some(r => r.path === CHAT_REPO)) {
    CHAT_REPO = repos.length ? repos[0].path : "";
  }
  sel.value = CHAT_REPO;
}

function chatRepoSelect() {
  const sel = $("#c-repo");
  if (sel) sel.value = CHAT_REPO;
}

async function chatNewRepo() {
  const name = prompt("New repo name (kebab-case, e.g. my-feature):");
  if (!name) { chatRepoSelect(); return; }
  const slug = chatSlug(name);
  if (!slug) { alert("Enter a repo name."); chatRepoSelect(); return; }
  const msg = $("#c-msg");
  const { code, body } = await jpost("/api/repos/create", { name: slug });
  if (code === 200) {
    CHAT_REPO = body.path;
    await chatLoadRepos();
    if (msg) { msg.className = ""; msg.textContent = ""; }
    chatStartSession();
    chatPoll();
  } else {
    if (msg) { msg.className = "err"; msg.textContent = (body.error || "failed to create repo"); }
    chatRepoSelect();
  }
}

async function chatPoll() {
  if (!CHAT_SESSION) return;
  let data = null;
  try {
    data = await jget("/api/chat/poll?session=" + encodeURIComponent(CHAT_SESSION) + "&since=" + CHAT_LAST);
  } catch (e) {
    CHAT_RUNNING = false;
    chatSetSend(false);
    chatRender();
    return;
  }
  const turns = (data && data.turns) || [];
  if (turns.length) {
    CHAT_TURNS = CHAT_TURNS.concat(turns);
    CHAT_LAST += turns.length;
  }
  CHAT_RUNNING = !!(data && data.running);
  chatSetSend(CHAT_RUNNING);
  chatRender();
}

async function chatSend() {
  const input = $("#c-text");
  const msg = $("#c-msg");
  if (!input) return;
  const text = input.value;
  if (!text.trim() || CHAT_RUNNING) return;
  const body = { session: CHAT_SESSION, repo: CHAT_REPO, message: text };
  const { code, body: resp } = await jpost("/api/chat/start", body);
  if (code === 200) {
    input.value = "";
    CHAT_SPEECH_FINAL = "";
    CHAT_RUNNING = true;
    chatSetSend(true);
    chatRender();
    chatPoll();
  } else if (code === 409) {
    if (msg) { msg.className = "err"; msg.textContent = (resp.error || "a chat turn is already running"); }
    chatPoll();
  } else {
    if (msg) { msg.className = "err"; msg.textContent = (resp.error || "failed to send"); }
  }
}

async function chatRun(file) {
  if (!file) return;
  if (!confirm(`Start a REAL run for ${file}? This spends model tokens and mutates repo branches.`)) return;
  const { code, body } = await jpost("/api/projects/run", { file, dry_run: false });
  alert(code === 200 ? `started (pid ${body.pid})\nlog: logs/${body.log}` : `not started: ${body.error}`);
}

function chatCardClick(ev) {
  const t = ev.target;
  const data = t && t.dataset;
  if (!data) return;
  if (data.ctOpen) { openDetail(data.ctOpen); chatClose(); }
  else if (data.ctRun) chatRun(data.ctRun);
}

// ---- speech ----
function chatStopMic() {
  if (CHAT_RECOG) { try { CHAT_RECOG.stop(); } catch (e) {} CHAT_RECOG = null; }
  CHAT_LISTENING = false;
  CHAT_SPEECH_FINAL = "";
  const inter = $("#c-interim");
  if (inter) { inter.textContent = ""; inter.style.display = "none"; }
  chatUpdateMic();
}

function chatMic() {
  const C = chatSR();
  if (!C) return;
  if (CHAT_LISTENING) { chatStopMic(); return; }
  try {
    const rec = new C();
    rec.lang = "en-US";
    rec.continuous = true;
    rec.interimResults = true;
    rec.onresult = ev => {
      const input = $("#c-text");
      const inter = $("#c-interim");
      if (!input) return;
      let interim = "";
      const from = (typeof ev.resultIndex === "number" && ev.resultIndex >= 0)
        ? ev.resultIndex : 0;
      for (let i = 0; i < ev.results.length; i++) {
        const r = ev.results[i];
        const t = r && r[0] && r[0].transcript ? r[0].transcript : "";
        if (r.isFinal) {
          if (i >= from) {
            if (CHAT_SPEECH_FINAL) CHAT_SPEECH_FINAL += " ";
            CHAT_SPEECH_FINAL += t;
          }
        } else {
          interim += t;
        }
      }
      input.value = CHAT_SPEECH_FINAL;
      if (inter) { inter.textContent = interim; inter.style.display = interim ? "block" : "none"; }
    };
    const done = () => {
      CHAT_LISTENING = false;
      CHAT_SPEECH_FINAL = "";
      const inter = $("#c-interim");
      if (inter) { inter.textContent = ""; inter.style.display = "none"; }
      chatUpdateMic();
    };
    rec.onend = done;
    rec.onerror = done;
    CHAT_RECOG = rec;
    const input0 = $("#c-text");
    CHAT_SPEECH_FINAL = input0 && input0.value ? input0.value : "";
    CHAT_LISTENING = true;
    chatUpdateMic();
    rec.start();
  } catch (e) {
    CHAT_LISTENING = false;
    chatUpdateMic();
  }
}

// ---- open / close ----
async function chatOpen() {
  const modal = $("#chatmodal");
  if (modal) modal.classList.add("open");
  const msg = $("#c-msg");
  if (msg) { msg.className = ""; msg.textContent = ""; }
  await chatLoadRepos();
  chatStartSession();
  chatUpdateMic();
  if (CHAT_POLL) clearInterval(CHAT_POLL);
  CHAT_POLL = setInterval(chatPoll, 3000);
  chatPoll();
}

function chatClose() {
  const modal = $("#chatmodal");
  if (modal) modal.classList.remove("open");
  if (CHAT_POLL) { clearInterval(CHAT_POLL); CHAT_POLL = null; }
  chatStopMic();
}

// ---- wire up ----
(function chatInit() {
  const modal = $("#chatmodal");
  const btn = $("#btn-chat");
  if (btn) btn.onclick = chatOpen;
  const close = $("#c-close");
  if (close) close.onclick = chatClose;
  const send = $("#c-send");
  if (send) send.onclick = chatSend;
  const mic = $("#c-mic");
  if (mic) mic.onclick = chatMic;
  const repo = $("#c-repo");
  if (repo) repo.onchange = () => {
    if (repo.value === CHAT_NEW) { chatNewRepo(); return; }
    CHAT_REPO = repo.value; chatStartSession(); chatPoll();
  };
  const log = $("#c-log");
  if (log) log.onclick = chatCardClick;
  if (modal) modal.onclick = ev => { if (ev.target === modal) chatClose(); };
})();
