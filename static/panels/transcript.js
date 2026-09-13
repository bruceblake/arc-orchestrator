"use strict";
// ---- transcript drawer ----
function openTranscript(file, title, sub) {
  $("#drawer").classList.add("open");
  $("#drawer-title").textContent = title;
  $("#drawer-sub").textContent = sub;
  $("#drawer-body").textContent = "loading…";
  drawerFile = file;
  pollTranscript();
}
async function openTaskTranscript(file, taskId, cachedRuns) {
  let runs = cachedRuns;
  if (!runs) {
    $("#drawer").classList.add("open");
    $("#drawer-title").textContent = taskId;
    $("#drawer-sub").textContent = "looking up runs…";
    $("#drawer-body").textContent = "loading…";
    drawerFile = null;
    try { const d = await jget("/api/project?file=" + encodeURIComponent(file)); runs = d.runs || []; }
    catch (e) { runs = []; }
  }
  const mine = runs.filter(r => r.task_id === taskId || String(r.task_id || "").indexOf(taskId + "-x") === 0);
  const withT = mine.filter(r => r.transcript);
  const last = withT[0]; // /api/project runs come newest-first (harness_runs_for, ORDER BY id DESC)
  if (last) openTranscript(String(last.transcript).split("/").pop(),
    `${short(last.model)} ${last.role || ""}`, `${taskId} · attempt ${last.attempt}`);
  else {
    $("#drawer").classList.add("open");
    $("#drawer-title").textContent = taskId;
    $("#drawer-sub").textContent = "no transcript";
    $("#drawer-body").textContent = `No harness transcript recorded yet for ${taskId}.`;
    drawerFile = null;
  }
}
async function pollTranscript() {
  if (!drawerFile) return;
  const pre = $("#drawer-body");
  const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 40;
  let d;
  try { d = await jget("/api/transcript?file=" + encodeURIComponent(drawerFile) + "&tail=200"); }
  catch (e) { return; } // keep last good content; next tick retries
  if (d.error) { pre.textContent = d.error; return; }
  $("#drawer-sub").textContent = $("#drawer-sub").textContent.split(" · ")[0] + ` · ${d.total_lines} lines`;
  pre.textContent = d.lines.map(renderTranscriptLine).join("\n");
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}
// The harnesses log completely different shapes and only one was ever handled:
// opencode emits {type: "step_finish"|"text"|"tool_use"}, and the retired kimi
// CLI emitted OpenAI-style {role: "assistant"|"tool", tool_calls, content}. A
// kimi transcript therefore rendered `o.type` — undefined — on every single
// line, so the drawer showed a column of "undefined" and looked broken. The
// kimi branch stays: old transcripts are still opened from history.
function renderTranscriptLine(l) {
  let o;
  try { o = JSON.parse(l); } catch { return l.slice(0, 300); }

  if (typeof o.type === "string") {                    // opencode
    const p = o.part || {};
    if (o.type === "step_finish") return `— step (${(p.tokens && p.tokens.total) || "?"} tok)`;
    if (o.type === "text") return (p.text || "").slice(0, 400);
    if (o.type === "tool_use") return `🔧 ${p.tool || "tool"}`;
    if (o.role === "meta") return `— ${o.type}${o.version ? " " + o.version : ""}`;
    return `— ${o.type}`;
  }

  const text = c =>                                    // kimi content: string or parts
    typeof c === "string" ? c
      : Array.isArray(c) ? c.map(x => (x && x.text) || "").join("")
      : "";

  if (o.role === "assistant") {
    const calls = (o.tool_calls || []).map(c => {
      const fn = (c && c.function) || {};
      let arg = "";
      try {
        const a = JSON.parse(fn.arguments || "{}");
        arg = a.file_path || a.path || a.pattern || a.command || a.description || "";
      } catch { arg = String(fn.arguments || "").slice(0, 80); }
      return `🔧 ${fn.name || "tool"}${arg ? " " + String(arg).slice(0, 90) : ""}`;
    });
    const said = text(o.content).trim();
    return [said ? said.slice(0, 500) : null, ...calls].filter(Boolean).join("\n") || "— (empty turn)";
  }
  if (o.role === "tool") {
    const body = text(o.content).replace(/\s+/g, " ").trim();
    return `   ↳ ${body.slice(0, 200)}${body.length > 200 ? ` … (${body.length} chars)` : ""}`;
  }
  if (o.role === "user") return `▸ ${text(o.content).slice(0, 400)}`;
  if (o.role === "meta") return `— ${o.type || "meta"}`;
  return l.slice(0, 200);
}

function closeDrawer() { $("#drawer").classList.remove("open"); drawerFile = null; }
$("#drawer-close").onclick = closeDrawer;
document.addEventListener("keydown", e => {
  if (e.key === "Escape") { closeDrawer(); $("#modal").classList.remove("open"); }
});
setInterval(() => { if (drawerFile && $("#drawer").classList.contains("open")) pollTranscript(); }, 3000);
setInterval(() => { // live elapsed ticking for agent rows
  const now = Date.now() / 1000;
  document.querySelectorAll("[data-el]").forEach(el => {
    const st = +el.dataset.el; if (st) el.textContent = tick(now - st);
  });
}, 1000);

