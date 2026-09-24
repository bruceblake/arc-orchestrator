"use strict";
// Messages tab: the operator's live view of the agent board.
const BD = {
  project: "", channel: "project", projects: [], channels: [], tasks: [],
  messages: [], claims: [], expertise: {}, questions: [],
  since: 0, timer: 0, stick: true, kind: "", agent: "", q: "",
};
const BD_POST_KINDS = ["note", "question", "decision", "ping"];
const BD_FAM = {
  glm: "#58a6ff", deepseek: "#d29922", kimi: "#bc8cff", "gpt-oss": "#3fb950",
  claude: "#d4a574", grok: "#f0883e", gemini: "#3fb950", operator: "#bc8cff", unknown: "#8b949e",
};

function boardFamily(model, author) {
  if (author === "operator" || String(author || "").endsWith("/operator")) return "operator";
  const m = String(model || author || "").toLowerCase();
  if (m.includes("glm")) return "glm";
  if (m.includes("deepseek")) return "deepseek";
  if (m.includes("kimi")) return "kimi";
  if (m.includes("claude")) return "claude";
  if (m.includes("grok") || m.includes("cursor")) return "grok";
  if (m.includes("gemini")) return "gemini";
  if (m.includes("gpt")) return "gpt-oss";
  return "unknown";
}

function boardBody(text) {
  return esc(text).replace(/(^|[^A-Za-z0-9_])@([A-Za-z0-9][\w.\-/:]*)/g,
    (full, pre, tok) => pre + '<span class="bd-mention">@' + tok + "</span>");
}

function boardRefs(refs) {
  if (!refs || typeof refs !== "object") return "";
  const bits = [];
  for (const k of ["url", "pr", "pr_url"]) {
    const u = refs[k];
    if (typeof u === "string" && /^https:\/\/[^"'<>\s]+$/.test(u))
      bits.push('<a class="bd-ref" href="' + attr(u) + '" target="_blank" rel="noopener">' + esc(u) + "</a>");
  }
  const files = [].concat(refs.files || [], refs.paths || []);
  for (const f of files) if (typeof f === "string" && f) bits.push('<code class="bd-file">' + esc(f) + "</code>");
  return bits.length ? '<div class="bd-refs">' + bits.join(" ") + "</div>" : "";
}

function boardMsg(m, nested) {
  const fam = boardFamily(m.author_model, m.author);
  const color = (typeof COLORS !== "undefined" && COLORS[fam]) || BD_FAM[fam] || BD_FAM.unknown;
  const kind = String(m.kind || "note");
  const stand = (kind === "question" || kind === "blocker" || kind === "decision") ? " bd-kind-" + kind : "";
  const replies = m.replies || [];
  const nestedHtml = replies.length
    ? '<details class="bd-replies"' + (nested ? "" : " open") + "><summary>" + replies.length
      + ' repl' + (replies.length === 1 ? "y" : "ies") + "</summary>"
      + replies.map(r => boardMsg(r, true)).join("") + "</details>"
    : "";
  return '<div class="bd-msg" data-id="' + attr(m.id || "") + '">'
    + '<span class="bd-chip" style="color:' + color + ";border-color:" + color + '">' + esc(m.author || "?") + "</span> "
    + '<span class="bd-kind' + stand + '">' + esc(kind) + "</span> "
    + '<span class="hint">' + esc(AGO(m.ts)) + "</span>"
    + '<div class="bd-text">' + boardBody(m.body || "") + "</div>"
    + boardRefs(m.refs)
    + nestedHtml + "</div>";
}

function boardMatch(m) {
  if (BD.kind && m.kind !== BD.kind) return false;
  if (BD.agent && m.author !== BD.agent && !(m.mentions || []).includes(BD.agent)) return false;
  if (BD.q) {
    const blob = (m.body || "") + " " + (m.author || "") + " " + (m.kind || "");
    if (!blob.toLowerCase().includes(BD.q)) return false;
  }
  return true;
}

function boardVisible(m) {
  if (boardMatch(m)) return true;
  return (m.replies || []).some(boardVisible);
}

function boardPaint(snapshot) {
  if (snapshot) {
    for (const k of ["project", "channel", "since", "projects", "channels", "tasks", "messages", "claims", "expertise", "questions"])
      if (k in snapshot) BD[k] = snapshot[k];
  }
  const rail = $("#bd-rail");
  const thread = $("#bd-thread");
  const side = $("#bd-side");
  if (!rail || !thread || !side) return;
  const prev = thread.scrollTop || 0;
  const height = thread.scrollHeight || 0;
  const stick = BD.stick || !height || prev + (thread.clientHeight || height) >= height - 24;
  const projOpts = (BD.projects || []).map(p => {
    const name = p.project || p;
    return '<option value="' + attr(name) + '"' + (name === BD.project ? " selected" : "") + ">" + esc(name) + "</option>";
  }).join("");
  const chans = BD.channels || [];
  rail.innerHTML = '<label class="hint" for="bd-project">project</label>'
    + '<select id="bd-project" aria-label="project">' + projOpts + "</select>"
    + chans.map(c => {
      const st = c.status || "";
      const dot = (typeof STATUSC !== "undefined" && STATUSC[st]) || "#8b949e";
      const unread = c.unread ? ' <b class="bd-unread">' + esc(String(c.unread)) + "</b>" : "";
      const on = c.channel === BD.channel ? " on" : "";
      return '<div class="bd-ch' + on + '" data-ch="' + attr(c.channel) + '">'
        + (st ? '<i class="bd-dot" style="background:' + dot + '" title="' + attr(st) + '"></i>' : "")
        + esc(c.channel) + unread + "</div>";
    }).join("");
  const shown = (BD.messages || []).filter(boardVisible);
  thread.innerHTML = shown.length ? shown.map(m => boardMsg(m, false)).join("")
    : '<div class="empty">No messages in this window.</div>';
  if (stick) thread.scrollTop = thread.scrollHeight || 0;
  else thread.scrollTop = prev;
  const claims = BD.claims || [];
  const byTask = {};
  for (const c of claims) (byTask[c.task || "(no task)"] = byTask[c.task || "(no task)"] || []).push(c);
  const claimHtml = Object.keys(byTask).map(task => {
    const rows = byTask[task].map(c => {
      const age = AGO(c.ts);
      const remain = c.expires_at ? Math.max(0, c.expires_at - Date.now() / 1000) : 0;
      const exp = c.expires_at ? "exp " + esc(tick(remain)) : "";
      const clash = c.conflict || (c.overlaps && c.overlaps.length);
      const paths = (c.paths || []).map(esc).join(", ");
      return '<div class="' + (clash ? "bd-conflict" : "bd-claim") + '">'
        + esc(c.author || "?") + " · " + esc(paths) + " · " + esc(age)
        + (exp ? " · " + exp : "")
        + (clash ? " · overlap " + esc((c.overlaps || []).join(", ")) : "")
        + "</div>";
    }).join("");
    return "<h3>" + esc(task) + "</h3>" + rows;
  }).join("") || '<div class="empty">No live claims.</div>';
  const exp = BD.expertise || {};
  const expHtml = Object.keys(exp).map(who => {
    const e = exp[who] || {};
    return "<div><b>" + esc(who) + "</b> " + esc((e.paths || []).join(", "))
      + (e.topics && e.topics.length ? " · " + esc(e.topics.join(", ")) : "") + "</div>";
  }).join("") || '<div class="empty">No expertise yet.</div>';
  const qs = (BD.questions && BD.questions.length) ? BD.questions
    : (BD.messages || []).filter(m => m.kind === "question" && m.state === "open");
  const qHtml = qs.map(m => '<div class="bd-q">' + esc(AGO(m.ts)) + " · " + boardBody(m.body || "") + "</div>").join("")
    || '<div class="empty">No open questions.</div>';
  side.innerHTML = "<h3>Who is working on what</h3>" + claimHtml
    + "<h3>Who knows what</h3>" + expHtml
    + "<h3>Open questions</h3>" + qHtml;
  const kindSel = $("#bd-kind");
  if (kindSel && !kindSel.options.length) {
    const kinds = ["", "note", "question", "answer", "claim", "blocker", "decision", "ping", "status", "result", "handoff"];
    kindSel.innerHTML = kinds.map(k => '<option value="' + attr(k) + '">' + esc(k || "all kinds") + "</option>").join("");
  }
  const agentSel = $("#bd-agent");
  if (agentSel) {
    const authors = [];
    const walk = ms => { for (const m of ms || []) { if (m.author && authors.indexOf(m.author) < 0) authors.push(m.author); walk(m.replies); } };
    walk(BD.messages);
    agentSel.innerHTML = '<option value="">all agents</option>'
      + authors.map(a => '<option value="' + attr(a) + '"' + (a === BD.agent ? " selected" : "") + ">" + esc(a) + "</option>").join("");
  }
  const postKind = $("#bd-post-kind");
  if (postKind && !postKind.options.length)
    postKind.innerHTML = BD_POST_KINDS.map(k => '<option value="' + k + '">' + k + "</option>").join("");
}

function boardSuggest() {
  const box = $("#bd-suggest");
  const input = $("#bd-post-body");
  if (!box || !input) return;
  const text = input.value || "";
  const at = text.lastIndexOf("@");
  if (at < 0 || text.slice(at).includes(" ")) { box.hidden = true; box.innerHTML = ""; return; }
  const prefix = text.slice(at + 1).toLowerCase();
  const names = ["all"];
  for (const t of BD.tasks || []) if (t.id && names.indexOf(t.id) < 0) names.push(t.id);
  for (const t of BD.tasks || []) if (t.model && names.indexOf(t.model) < 0) names.push(t.model);
  const walk = ms => { for (const m of ms || []) { if (m.author && names.indexOf(m.author) < 0) names.push(m.author); walk(m.replies); } };
  walk(BD.messages);
  const hits = names.filter(n => n.toLowerCase().startsWith(prefix)).slice(0, 8);
  if (!hits.length) { box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;
  box.innerHTML = hits.map(n => '<li data-mention="' + attr(n) + '">@' + esc(n) + "</li>").join("");
}

function boardMerge(incoming) {
  const byId = {};
  const walk = ms => { for (const m of ms || []) { byId[m.id] = m; m.replies = m.replies || []; walk(m.replies); } };
  walk(BD.messages);
  for (const m of incoming) {
    if (m.id && byId[m.id]) continue;
    m.replies = m.replies || [];
    const parent = m.reply_to && byId[m.reply_to];
    if (parent) parent.replies.push(m);
    else BD.messages.push(m);
    if (m.id) byId[m.id] = m;
  }
}

async function boardMarkRead() {
  // A background cursor update must not prompt for the dashboard token.
  // Only the composer's Post (jpost) may ask. A 401 is ignored.
  if (typeof TAB !== "undefined" && TAB !== "messages") return 0;
  if (!BD.project || !BD.channel || !BD.since) return 0;
  try {
    const r = await fetch("/api/board/read", {
      method: "POST",
      headers: {"Content-Type": "application/json", ...authHeaders()},
      body: JSON.stringify({project: BD.project, channel: BD.channel, ts: BD.since}),
    });
    return r.status;
  } catch (e) {
    return 0;
  }
}

async function boardRefresh(full) {
  if (typeof TAB !== "undefined" && TAB !== "messages") return;
  if (!BD.project) return;
  const q = "project=" + encodeURIComponent(BD.project);
  const ch = "&channel=" + encodeURIComponent(BD.channel || "project");
  const since = !full && BD.since ? "&since=" + encodeURIComponent(BD.since) : "";
  try {
    const [thread, claims, expertise, channels, questions] = await Promise.all([
      jget("/api/board/thread?" + q + ch + since),
      jget("/api/board/claims?" + q),
      jget("/api/board/expertise?" + q),
      jget("/api/board/channels?" + q),
      jget("/api/board/thread?" + q + "&kinds=question"),
    ]);
    if (full || !since) BD.messages = thread.messages || [];
    else boardMerge(thread.messages || []);
    let maxTs = BD.since;
    const walk = ms => { for (const m of ms || []) { if (m.ts > maxTs) maxTs = m.ts; walk(m.replies); } };
    walk(BD.messages);
    BD.since = maxTs || BD.since;
    if ((await boardMarkRead()) === 200)
      for (const c of channels.channels || []) if (c.channel === BD.channel) c.unread = 0;
    BD.claims = claims.claims || [];
    BD.expertise = expertise.expertise || {};
    BD.channels = channels.channels || [];
    BD.tasks = channels.tasks || BD.tasks;
    BD.questions = (questions.messages || []).filter(m => m.state === "open");
    boardPaint();
  } catch (e) { /* a failed poll keeps the last window */ }
}

async function boardLoadProjects() {
  let data = {};
  try { data = await jget("/api/board/projects"); } catch (e) { return; }
  BD.projects = data.projects || [];
  if (!BD.project && BD.projects.length) BD.project = BD.projects[0].project;
  boardPaint();
  if (BD.project) boardRefresh(true);
}

function boardOnTab() {
  if (BD.timer) return;
  boardLoadProjects();
  BD.timer = setInterval(() => {
    if (typeof TAB === "undefined" || TAB === "messages") boardRefresh(false);
  }, 5000);
}

function boardLeaveTab() {
  if (!BD.timer) return;
  clearInterval(BD.timer);
  BD.timer = 0;
}

document.addEventListener("change", (e) => {
  const t = e.target;
  if (!t || !t.id) return;
  if (t.id === "bd-project") { BD.project = t.value; BD.channel = "project"; BD.since = 0; BD.messages = []; boardRefresh(true); }
  if (t.id === "bd-kind") { BD.kind = t.value; boardPaint(); }
  if (t.id === "bd-agent") { BD.agent = t.value; boardPaint(); }
});
document.addEventListener("input", (e) => {
  if (e.target && e.target.id === "bd-search") { BD.q = (e.target.value || "").trim().toLowerCase(); boardPaint(); }
  if (e.target && e.target.id === "bd-post-body") boardSuggest();
});
document.addEventListener("click", (e) => {
  const t = e.target;
  if (!t || !t.closest) return;
  const ch = t.closest("[data-ch]");
  if (ch && $("#bd-rail") && $("#bd-rail").contains(ch)) {
    BD.channel = ch.getAttribute("data-ch");
    BD.since = 0; BD.messages = [];
    boardRefresh(true);
  }
  const men = t.closest("[data-mention]");
  if (men) {
    const input = $("#bd-post-body");
    const text = input.value || "";
    const at = text.lastIndexOf("@");
    input.value = (at < 0 ? text : text.slice(0, at)) + "@" + men.getAttribute("data-mention") + " ";
    $("#bd-suggest").hidden = true;
  }
  if (t.id === "bd-send") boardSend();
});
document.addEventListener("scroll", (e) => {
  if (e.target && e.target.id === "bd-thread") {
    const el = e.target;
    BD.stick = (el.scrollTop || 0) + (el.clientHeight || 0) >= (el.scrollHeight || 0) - 24;
  }
}, true);

async function boardSend() {
  const body = ($("#bd-post-body").value || "").trim();
  if (!body || !BD.project) return;
  const mentions = [];
  const re = /(?:^|[^\w@])@([A-Za-z0-9][\w.\-/:]*)/g;
  let m;
  while ((m = re.exec(body))) if (mentions.indexOf(m[1]) < 0) mentions.push(m[1]);
  const res = await jpost("/api/board/post", {
    project: BD.project, channel: BD.channel || "project",
    kind: $("#bd-post-kind").value || "note", body, mentions,
  });
  if (res.code === 200) { $("#bd-post-body").value = ""; boardRefresh(false); }
}
