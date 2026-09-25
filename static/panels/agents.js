"use strict";
// ---- agents panel ----
const attemptOf = t => { const m = /-(\d+)\.jsonl$/.exec(t || ""); return m ? m[1] : null; };
// The stable board ID (<task>/<role>): the same across fix rounds, usage swaps
// and escalations, and the address to DM (dm:<id>) from the Messages tab.
const agentId = a => {
  const id = a.agent || (a.task ? a.task + "/" + (a.role || "") : "");
  return id ? `<span class="agent-id" title="agent ID — DM it as dm:${attr(id)}"><b>${esc(id)}</b></span>` : "";
};
async function pollAgents() {
  try {
    const rev = await jgetRev("/api/agents", "agents");
    if (rev.unchanged) { markFail("agents", false); return; }
    const d = rev.data;
    AGENTS = (d.agents || []).filter(a => a.task);
    RECENT = d.recent || [];
    markFail("agents", false);
  } catch (e) { markFail("agents", true); }
  renderAgentChips(); renderAgents();
}
function renderAgentChips() {
  const by = {};
  AGENTS.forEach(a => { const k = a.pretty || short(a.model); by[k] = (by[k] || 0) + 1; });
  $("#agent-chips").innerHTML = Object.entries(by).sort((a, b) => b[1] - a[1])
    .map(([k, n]) => `<span class="achip">${esc(k)} x${n}</span>`).join("");
}
let AG_TAB = "live";
document.querySelectorAll("[data-ag]").forEach(b => b.onclick = () => {
  AG_TAB = b.dataset.ag;
  document.querySelectorAll("[data-ag]").forEach(x => x.classList.toggle("on", x === b));
  $("#agents").style.display = AG_TAB === "live" ? "" : "none";
  $("#agents-done").style.display = AG_TAB === "done" ? "" : "none";
  renderAgents();
});

function renderRecent() {
  const rows = FILT.m ? RECENT.filter(a => a.model === FILT.m || a.pretty === FILT.m) : RECENT;
  $("#agents-done").innerHTML = rows.length ? rows.map(a => {
    const v = a.verdict;
    const badge = v ? (v.pass ? '<span class="good">review pass</span>'
                              : `<span class="bad">review fail${v.issues ? " · " + v.issues + " issue" + (v.issues === 1 ? "" : "s") : ""}</span>`)
                    : (a.ok ? '<span class="hint">ok</span>' : '<span class="bad">exit ' + esc(a.exit_code) + "</span>");
    return `<div class="donerow ${a.ok && (!v || v.pass) ? "" : "bad"} ${a.transcript ? "has-t" : ""}"
                 data-t="${attr(a.transcript || "")}" data-m="${attr(a.pretty)}" data-r="${attr(a.role || "")}" data-task="${attr(a.task || "")}"${a.transcript ? ' role="button" tabindex="0" aria-label="open transcript"' : ""}>
      ${agentId(a)}
      <span class="who">${esc(a.pretty)}${a.harness ? " · " + esc(a.harness) : ""}</span>
      <span class="hint">${esc(a.task || "")}</span>
      <span class="hint">att ${esc(a.attempt)}</span>
      <span class="hint">${a.seconds ? tick(a.seconds) : ""}</span>
      <span style="flex:1"></span>${badge}
      <span class="hint">${a.ts ? fmtT(new Date(a.ts * 1000).toISOString()) : ""}</span>
    </div>`;
  }).join("") : '<div class="empty">No finished agent runs recorded yet.</div>';
  document.querySelectorAll("#agents-done .donerow.has-t").forEach(el => el.onclick = () =>
    openTranscript(el.dataset.t, `${el.dataset.m} ${el.dataset.r}`, el.dataset.task));
}

function renderAgents() {
  if (AG_TAB === "done") { renderRecent(); }
  const rows = FILT.m ? AGENTS.filter(a => a.model === FILT.m || a.pretty === FILT.m) : AGENTS;
  $("#agents-meta").textContent = `(${rows.length} running${FILT.m ? " of " + AGENTS.length : ""} — click for live transcript)`;
  $("#agents-empty").style.display = rows.length ? "none" : "";
  const html = rows.map(a => {
    const att = attemptOf(a.transcript);
    const quiet = a.idle_s != null && a.stuck
      ? `<span class="quiet">quiet <span data-since="${sinceStamp(a.idle_s)}" data-suffix="">…</span>${a.state ? ` · ${esc(a.state)}` : ""}${(a.cpu_delta_s != null && a.cpu_delta_s < 0.5) ? " · no CPU" : ""}</span>`
      : (a.idle_s != null ? `<span class="hint">quiet <span data-since="${sinceStamp(a.idle_s)}" data-suffix="">…</span></span>` : "");
    const hb = a.last_event_s != null
      ? `<span data-since="${sinceStamp(a.last_event_s)}" data-suffix=" ago">…</span>`
      : "starting";
    return `<div class="agentrow ${a.stuck ? "stuck" : ""} ${a.transcript ? "has-t" : ""}" data-t="${attr(a.transcript || "")}" data-m="${attr(a.pretty || short(a.model))}" data-r="${attr(a.role || "")}" data-task="${attr(a.task || "")}"${a.transcript ? ' role="button" tabindex="0" aria-label="open live transcript"' : ""}>
      ${agentId(a)}
      <span class="who">${esc(a.pretty || short(a.model))}${a.harness ? " · " + esc(a.harness) : ""}</span>
      ${a.session_id ? `<span class="hint" title="harness session">${esc(String(a.session_id).slice(0, 14))}</span>` : ""}
      <span class="hint">${esc(a.task)}</span>
      ${att ? `<span class="hint">attempt ${att}</span>` : ""}
      ${a.stalled ? '<span class="bad">STALLED</span>' : ""}
      <span class="hint" data-since="${a.started || ""}">…</span>
      <span class="hint">hb ${hb}</span>
      ${a.bytes ? `<span class="hint">${fmtK(a.bytes)}B out</span>` : ""}
      ${quiet}</div>`;
  }).join("");
  if (paint($("#agents"), html)) {
    document.querySelectorAll("#agents .agentrow.has-t").forEach(el => el.onclick = () => {
      if (el.dataset.t) openTranscript(el.dataset.t, `${el.dataset.m} ${el.dataset.r}`, el.dataset.task);
    });
  }
  rebuildFilterOptions();
}

