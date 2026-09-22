"use strict";
// ---- Studio panel -----------------------------------------------------------
// The game-development "workbench": one view of a studio project while the
// fleet builds it. Phase progress, the current phase's gate (what still stands
// between this phase and the next), Bucket A measured against target, the
// phase's task board, and the render rounds with the blind judge's verdicts.
// Everything comes from GET /api/studio (studio/status.py), which is
// read-only; nothing here starts or changes work.
let STUDIO = null;
let STUDIO_OPEN = "";            // project whose section is expanded

async function pollStudio() {
  try { STUDIO = await jget("/api/studio"); markFail("studio", false); }
  catch (e) { markFail("studio", true); return; }
  renderStudio();
}

const STUDIO_TASK_G = {merged: "✓", done: "✓", running: "●", in_review: "◎",
                       failed: "✗", conflict: "!", pending: "○", skipped: "⊘"};

function studioPct(v) { return v == null ? "—" : Math.round(v * 100) + "%"; }

function studioHead(d) {
  const sp = d.spend || {};
  const cap = sp.ceiling_usd ? ` of $${sp.ceiling_usd.toFixed(0)}` : "";
  const plan = d.plan && d.plan.windows ? d.plan.windows : null;
  const five = plan && plan.five_hour ? plan.five_hour.used : null;
  const week = plan && plan.seven_day ? plan.seven_day.used : null;
  const planCls = five == null ? "" : five >= 0.85 ? "bad" : five >= 0.6 ? "warn" : "good";
  return `<div class="st-head">
    <span class="pill">fleet <b>${esc(d.fleet)}</b></span>
    <span class="pill">planner <b>${esc(short(d.planner))}</b></span>
    ${d.openai_model ? `<span class="pill">builder <b>${esc(short(d.openai_model))}</b>${d.codex_effort ? ` · ${esc(d.codex_effort)}` : ""}</span>` : ""}
    <span class="pill" title="direct studio model calls (judge, operator, planner via API); subscription CLI runs cost $0">spend <b>$${(sp.spent_usd || 0).toFixed(2)}</b>${cap}</span>
    ${five != null ? `<span class="pill ${planCls}" title="Claude plan: your own Claude Code shares these windows">Claude 5h <b>${studioPct(five)}</b> · 7d <b>${studioPct(week)}</b></span>` : ""}
  </div>`;
}

function studioPhases(p) {
  return `<div class="st-phases" role="list">${(p.phases || []).map(ph =>
    `<div class="st-phase ${ph.state}" role="listitem" title="${attr(ph.id)}">
       <span class="n">${ph.state === "done" ? "✓" : ph.n}</span><span>${esc(ph.label)}</span>
     </div>`).join('<span class="st-arrow">›</span>')}</div>`;
}

function studioGate(p) {
  const g = p.gate;
  if (!g) return `<div class="empty">No repo found for this project, so its gate cannot be checked.</div>`;
  const head = g.passed
    ? `<div class="good"><b>✓ gate passes</b> — ready to promote to the next phase</div>`
    : `<div class="warn"><b>${g.failures.length} thing${g.failures.length === 1 ? "" : "s"}</b> ${g.failures.length === 1 ? "stands" : "stand"} between this phase and the next</div>`;
  // Unmeasured Bucket A targets are already visible row by row in the table
  // below; listing each one again as a failure buried the failures that are
  // actually different (a missing verdict, a mesh in a graybox).
  const unmeasured = (g.failures || []).filter(f => /has no measurement in/.test(f));
  const other = (g.failures || []).filter(f => !/has no measurement in/.test(f));
  const items = (unmeasured.length ? [`<li>${unmeasured.length} Bucket A dimension${unmeasured.length === 1 ? " has" : "s have"} not been measured on <code>main</code> yet — <code>studio gate</code> measures them</li>`] : [])
    .concat(other.map(f => `<li>${esc(f)}</li>`)).join("");
  const rows = (p.metrics || []).map(m => `<tr>
      <td>${esc(m.key)}</td><td class="num">${esc(m.target)}</td>
      <td class="num">${m.measured == null ? '<span class="hint">not measured</span>' : esc(m.measured)}</td>
      <td>${m.ok == null ? "○" : m.ok ? '<span class="good">✓</span>' : '<span class="bad">✗</span>'}</td></tr>`).join("");
  return `${head}${items ? `<ul class="st-fails">${items}</ul>` : ""}
    ${rows ? `<table class="st-metrics"><thead><tr><th>Bucket A</th><th>target</th><th>measured</th><th></th></tr></thead><tbody>${rows}</tbody></table>` : ""}`;
}

function studioBoards(p) {
  const boards = p.boards || [];
  if (!boards.length) return `<div class="empty">No taskfile planned for this project yet — <code>main.py studio plan "&lt;goal&gt;" &lt;repo&gt;</code>.</div>`;
  return boards.map(b => {
    const pct = b.total ? Math.round(100 * b.done / b.total) : 0;
    const run = b.run_pid ? `<span class="tag good" title="a run process is working on this taskfile">run pid ${esc(b.run_pid)}</span>` : `<span class="tag">no run</span>`;
    const rows = b.tasks.map(t => `<div class="st-task ${esc(t.status)}">
        <span class="g" aria-hidden="true">${STUDIO_TASK_G[t.status] || "○"}</span>
        <span class="id">${esc(t.id)}</span>
        <span class="chip ${esc(t.status)}">${esc(t.status.replace("_", " "))}${t.live ? ` · ${esc((t.live_role || "live").replace("_", " "))}` : ""}</span>
        <span class="hint">${esc(short(t.model))} → rev ${esc(t.reviewer || "—")}</span>
        ${t.deps.length ? `<span class="hint">after ${t.deps.map(esc).join(", ")}</span>` : ""}
        ${t.error ? `<div class="bad st-err">${esc(t.error)}</div>` : ""}
      </div>`).join("");
    return `<div class="st-board">
      <div class="st-board-h"><b>${esc(b.file.replace(/\.json$/, ""))}</b> ${run}
        <span class="hint">${b.done}/${b.total} merged</span></div>
      <div class="st-bar"><span style="width:${pct}%"></span></div>
      ${rows}</div>`;
  }).join("");
}

function studioVerdict(v) {
  const score = v.score == null ? "—" : v.score;
  const cls = v.validation ? "" : v.pass ? "good" : "bad";
  const arts = (v.artifacts || []).map(a =>
    `<li><span class="tag ${a.severity === "high" ? "bad" : a.severity === "medium" ? "warn" : ""}">${esc(a.severity)}</span> ${esc(a.camera)} · ${esc(a.kind)} — ${esc(a.note)}</li>`).join("");
  const dirs = (v.directives || []).map(x => `<li>${esc(x)}</li>`).join("");
  return `<div class="st-verdict">
    <div><b class="${cls}">${score}</b> <span class="hint">/100</span>
      <span class="tag">${esc(short(v.model))}</span>
      ${v.validation ? '<span class="tag warn" title="free validation judge: never gates a phase">validation</span>' : ""}
      <span class="hint">A ${v.a == null ? "—" : v.a} · B ${v.b == null ? "—" : v.b}</span></div>
    ${v.summary ? `<div class="hint">${esc(v.summary)}</div>` : ""}
    ${arts ? `<details><summary>${v.artifacts.length} artifact${v.artifacts.length === 1 ? "" : "s"}</summary><ul>${arts}</ul></details>` : ""}
    ${dirs ? `<details open><summary>directives</summary><ul>${dirs}</ul></details>` : ""}
  </div>`;
}

function studioRounds(p) {
  const rounds = p.rounds || [];
  if (!rounds.length) return `<div class="empty">No renders yet. After tasks merge: <code>main.py studio render ${esc(p.name)} ${esc(p.repo || "&lt;repo&gt;")}</code>, then <code>studio judge</code>.</div>`;
  return rounds.map((r, i) => `<details class="st-round" ${i === 0 ? "open" : ""}>
    <summary><b>Round ${r.round}</b> <span class="hint">${esc(r.phase || "")} · ${r.images.length} camera${r.images.length === 1 ? "" : "s"} · ${r.verdicts.length} verdict${r.verdicts.length === 1 ? "" : "s"}</span></summary>
    <div class="st-shots">${r.images.map(im => `<a class="st-shot ${esc(im.kind)}" href="${attr(im.url)}" target="_blank" rel="noopener" title="${attr(im.note || im.name)}">
        <img loading="lazy" src="${attr(im.url)}" alt="${attr(im.name)}"><span>${esc(im.name)}${im.kind === "adversarial" ? " ⚑" : ""}</span></a>`).join("")}</div>
    ${r.verdicts.length ? `<div class="st-verdicts">${r.verdicts.map(studioVerdict).join("")}</div>` : '<div class="hint">not judged yet</div>'}
  </details>`).join("");
}

function studioWorkbench(p) {
  const w = p.workbench || {};
  const shots = (w.renders || []).map(s => `<a class="st-shot" href="${attr(s.url)}" target="_blank" rel="noopener"><img loading="lazy" src="${attr(s.url)}" alt="${attr(s.name)}"><span>${esc(s.name)}</span></a>`).join("");
  const mesh = (w.meshes || []).map(m => `<tr><td>${esc(m.asset)}</td>
      <td class="num ${m.over ? "bad" : ""}">${esc(m.triangles)}${m.budget ? ` / ${esc(m.budget)}` : ""}</td>
      <td class="num ${m.non_manifold ? "bad" : ""}">${esc(m.non_manifold)}</td>
      <td class="num">${esc(m.materials)}</td><td class="num">${m.bones == null ? "—" : esc(m.bones)}</td>
      <td class="num">${esc(m.actions)}</td></tr>`).join("");
  if (!shots && !mesh) return "";
  return `<h3>Asset workbench</h3>
    ${shots ? `<div class="st-shots">${shots}</div>` : ""}
    ${mesh ? `<table class="st-metrics"><thead><tr><th>asset</th><th>tris / budget</th><th>non-manifold</th><th>mats</th><th>bones</th><th>clips</th></tr></thead><tbody>${mesh}</tbody></table>` : ""}`;
}

function studioExtras(p) {
  let out = "";
  if (p.arbitration && p.arbitration.halt)
    out += `<div class="panel err st-halt"><b>Judges are oscillating — loop halted.</b> ${esc(p.arbitration.reason)}</div>`;
  const f = p.fuzz;
  if (f) out += `<h3>Fuzz swarm</h3><div class="kv">
      <span>bots <b>${esc(f.bots)}</b></span><span>sent <b>${esc(f.messages_sent)}</b></span>
      <span>tick p95 <b>${f.tick_p95_ms == null ? "—" : esc(f.tick_p95_ms) + "ms"}</b></span>
      <span>authority violations <b class="${f.authority_violations ? "bad" : "good"}">${esc(f.authority_violations)}</b></span>
      <span>crashes <b class="${f.crashes ? "bad" : "good"}">${esc(f.crashes)}</b></span>
      ${f.server_unreachable ? '<span class="bad">server unreachable</span>' : ""}</div>`;
  const hist = (p.history || []).slice().reverse();
  if (hist.length) out += `<h3>Promotions</h3>${hist.map(h => `<div class="hint">${esc((h.from || "").replace(/^PHASE_\d_/, ""))} → <b>${esc((h.to || "").replace(/^PHASE_\d_/, ""))}</b>
      ${h.forced ? '<span class="tag bad">forced over a failing gate</span>' : ""} ${h.ts ? AGO(h.ts) + " ago" : ""} ${h.reason ? "· " + esc(h.reason) : ""}</div>`).join("")}`;
  return out;
}

function renderStudio() {
  const el = $("#studio");
  if (!el) return;
  const d = STUDIO || {};
  const projects = d.projects || [];
  $("#studio-meta").textContent = d.studio_active
    ? `(${projects.length} project${projects.length === 1 ? "" : "s"})`
    : "(studio fleet not active on this dashboard)";
  if (!projects.length) {
    el.innerHTML = studioHead(d) + `<div class="empty">No studio projects yet. Start one:
      <code>main.py studio scaffold ~/repos/&lt;game&gt;</code> then <code>main.py studio gate &lt;game&gt; ~/repos/&lt;game&gt;</code>.</div>`;
    return;
  }
  if (!STUDIO_OPEN || !projects.some(p => p.name === STUDIO_OPEN)) STUDIO_OPEN = projects[0].name;
  el.innerHTML = studioHead(d) + projects.map(p => {
    const open = p.name === STUDIO_OPEN;
    const live = (p.boards || []).reduce((n, b) => n + b.tasks.filter(t => t.live).length, 0);
    return `<section class="st-proj ${open ? "open" : ""}">
      <div class="st-proj-h" data-studio-project="${attr(p.name)}">
        <b>${esc(p.name)}</b> <span class="tag">${esc(p.phase_label)}</span>
        ${live ? `<span class="tag good">● ${live} building now</span>` : ""}
        <span class="hint">${esc(p.repo || "")}</span>
      </div>
      ${open ? `${studioPhases(p)}
      <div class="st-grid">
        <div class="st-col"><h3>Phase gate</h3>${studioGate(p)}</div>
        <div class="st-col"><h3>Phase tasks</h3>${studioBoards(p)}</div>
      </div>
      <h3>Renders & judge</h3>${studioRounds(p)}
      ${studioWorkbench(p)}${studioExtras(p)}` : ""}
    </section>`;
  }).join("");
}

// Delegated, like summary.js, so the test harness's element stubs are untouched.
document.addEventListener("click", (e) => {
  const t = e.target && e.target.closest && e.target.closest("[data-studio-project]");
  if (!t) return;
  STUDIO_OPEN = t.getAttribute("data-studio-project");
  renderStudio();
});
