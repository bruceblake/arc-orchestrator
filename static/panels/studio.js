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
// Which view of the open project: its sub-tabs. Remembered per browser.
let STUDIO_VIEW = (() => { try { return localStorage.getItem("arc.studio.view") || "overview"; } catch (e) { return "overview"; } })();
const STUDIO_VIEWS = [["overview", "Overview"], ["board", "Board"], ["changelog", "Changelog"],
                      ["evidence", "Evidence"], ["workbench", "Workbench"], ["playtest", "Playtest"]];

// The gauntlet a task has been through, as compact badges: every angle it was
// checked from (fix rounds, the gate's measurements, the independent critic,
// PR review rounds, escalations, the manual gate).
function gauntletBadges(g) {
  if (!g) return "";
  const b = [];
  if (g.attempts > 1) b.push(`<span class="gb" title="implementation rounds">↺${g.attempts}</span>`);
  if (g.gate_pass || g.gate_fail) b.push(`<span class="gb" title="verify gate: passed / failed">gate <b class="good">${g.gate_pass}✓</b>${g.gate_fail ? ` <b class="bad">${g.gate_fail}✗</b>` : ""}</span>`);
  if (g.review_pass || g.review_fail) b.push(`<span class="gb" title="independent cross-family critic">critic <b class="good">${g.review_pass}✓</b>${g.review_fail ? ` <b class="bad">${g.review_fail}✗</b>` : ""}</span>`);
  if (g.pr_rounds) b.push(`<span class="gb" title="pull-request review rounds (rejected)">PR r${g.pr_rounds}${g.pr_rejects ? ` <b class="bad">${g.pr_rejects}✗</b>` : ""}</span>`);
  if (g.escalations) b.push(`<span class="gb warn" title="escalated to a stronger tier">⬆${g.escalations}</span>`);
  if (g.manual) b.push(`<span class="gb ${g.manual === "approved" ? "good" : g.manual === "awaiting" ? "warn" : "bad"}" title="manual review gate">you: ${esc(g.manual)}</span>`);
  return `<span class="gbs">${b.join("")}</span>`;
}

async function pollStudio() {
  try { STUDIO = await jget("/api/studio"); markFail("studio", false); }
  catch (e) { markFail("studio", true); return; }
  // The panel is rebuilt from markup every poll. While the operator is typing
  // into a playtest form, rebuilding would drop focus and the caret, so the
  // render is deferred until the field loses focus (the data is kept; the next
  // poll draws it). Typed values also survive a later render: they are drafted
  // into PT_DRAFT and written back into the markup.
  if (studioEditing()) return;
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
        ${t.feature ? `<span class="tag">◆ ${esc(t.feature)}</span>` : ""}
        ${gauntletBadges(t.gauntlet)}
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

const KANBAN_LABEL = {backlog: "Backlog", planned: "Planned", building: "Building",
                      review: "In review", done: "Done", blocked: "Blocked"};

function studioBoard(p) {
  const k = p.kanban || {};
  const col = c => {
    const cards = k[c] || [];
    return `<div class="kb-col kb-${c}"><div class="kb-h">${KANBAN_LABEL[c]} <span class="hint">${cards.length}</span></div>
      ${cards.map(x => `<div class="kb-card ${x.kind}">
        <div class="kb-t">${x.live ? '<span class="good">● </span>' : ""}${esc(x.title)}</div>
        <div class="hint">${x.kind === "feature" ? "roadmap feature" : esc(x.id)}${x.phase ? " · " + esc(String(x.phase).replace(/^PHASE_\d_/, "").toLowerCase().replace(/_/g, " ")) : ""}</div>
        ${x.kind === "task" ? `<div class="hint">${esc(short(x.model))}${x.live_role ? " · " + esc(x.live_role.replace("_", " ")) : ""}${x.feature ? " · ◆ " + esc(x.feature) : ""}</div>${gauntletBadges(x.gauntlet)}` : ""}
        ${x.error ? `<div class="bad kb-err">${esc(x.error.slice(0, 140))}</div>` : ""}
      </div>`).join("") || '<div class="hint kb-empty">—</div>'}</div>`;
  };
  return `<div class="kb">${["backlog", "planned", "building", "review", "done", "blocked"].map(col).join("")}</div>
    <div class="hint">Backlog is <code>studio_roadmap.json</code> in the game repo: features not yet in any plan. A planned task that names a feature moves it onto the board.</div>
    ${studioThread(p)}`;
}

function studioThread(p) {
  const rows = p.thread || [];
  const head = `<h3>Agent thread</h3>`;
  if (!rows.length) return head + `<div class="empty">No agent handoffs on this project's board yet.</div>`;
  const items = rows.map(r => `<div class="st-task">
      <span class="tag">${esc(r.kind)}</span>
      <span class="id">${esc(r.task)}</span>
      <span class="hint">${esc(r.role)} · ${esc(short(r.model))} via ${esc(r.harness)}${r.session_owner ? ` · session on ${esc(r.session_owner)}` : ""}</span>
      <span class="hint">${esc(r.timestamp)}</span>
      <div class="st-err">${esc(r.body)}</div>
    </div>`).join("");
  return `${head}<div class="st-board">${items}</div>`;
}

function studioChangelog(p) {
  const log = p.changelog || [];
  if (!log.length) return '<div class="empty">Nothing has shipped to <code>main</code> yet.</div>';
  return `<div class="cl">${log.map(e => `<div class="cl-row">
      <span class="hint cl-d">${esc((e.date || "").slice(0, 16).replace("T", " "))}</span>
      ${e.pr ? `<a class="tag" href="${attr(e.pr_url)}" target="_blank" rel="noopener">#${e.pr}</a>` : '<span class="tag">—</span>'}
      <span class="cl-t">${esc(e.title)}</span>
      ${e.task ? `<span class="hint">${esc(e.task)}</span>` : ""}
      ${e.model ? `<span class="hint">built by ${esc(short(e.model))}${e.reviewer ? `, critic ${esc(e.reviewer)}` : ""}</span>` : ""}
      <span class="hint">${esc(e.sha)}</span></div>`).join("")}</div>`;
}

function studioEvidence(p) {
  const ev = p.evidence || {};
  let out = "";
  const pt = ev.playtest;
  out += `<h3>Scripted playtest <span class="hint">measure + look</span></h3>`;
  out += pt ? `<div class="${pt.passed ? "good" : "bad"}"><b>${pt.passed ? "✓ passed" : "✗ failed"}</b>
      <span class="hint">${pt.checks.length} checks · ${pt.screenshots} screenshots${pt.seconds ? ` · ${(+pt.seconds).toFixed(1)}s` : ""}</span></div>
      <table class="st-metrics"><thead><tr><th>check</th><th>value</th><th>expected</th><th></th></tr></thead><tbody>${
        pt.checks.map(c => `<tr><td>${esc(c.name)}</td><td class="num">${esc(JSON.stringify(c.value))}</td><td class="num">${esc(JSON.stringify(c.expected))}</td><td>${c.passed ? '<span class="good">✓</span>' : '<span class="bad">✗</span>'}</td></tr>`).join("")}</tbody></table>`
    : `<div class="empty">No playtest report on <code>main</code> yet. <code>tools/playtest.gd</code> writes one; <code>studio gate</code> and <code>studio playtest</code> run it.</div>`;
  const pf = ev.perf;
  out += `<h3>Performance</h3>`;
  out += pf ? `<div class="kv">
      <span>fps p5 <b class="${pf.fps_p5 >= pf.min_fps ? "good" : "bad"}">${esc(pf.fps_p5)}</b> <span class="hint">(≥ ${esc(pf.min_fps)})</span></span>
      <span>avg <b>${esc(pf.fps_avg)}</b></span><span>frame p95 <b>${esc(pf.frame_ms_p95)}ms</b></span>
      <span>draw calls <b>${esc(pf.draw_calls)}</b></span>
      <span>shadow lights <b class="${pf.shadow_lights > pf.max_shadow_lights ? "bad" : "good"}">${esc(pf.shadow_lights)}</b> <span class="hint">(≤ ${esc(pf.max_shadow_lights)})</span></span>
      <span class="hint">${esc(pf.renderer || "")}</span></div>`
    : '<div class="empty">No perf report yet (gated from phase 3; <code>tools/perf.gd</code> needs a display).</div>';
  const pal = ev.palette;
  out += `<h3>Colour bible</h3>`;
  out += pal ? `<div class="kv">${pal.shares.map(x => `<span>${esc(x.name)} <b class="${x.share >= pal.min ? "good" : "bad"}">${Math.round(x.share * 100)}%</b></span>`).join("")}
      <span class="hint">round ${esc(pal.round)} · needs ${Math.round(pal.min * 100)}% on-palette</span></div>`
    : '<div class="empty">No renders checked against the palette yet.</div>';
  return out + studioExtras(p);
}

function studioWorkbenchView(p) {
  const w = p.workbench || {};
  const mesh = (w.meshes || []).map(m => `<tr>
      <td>${esc(m.asset)}</td>
      <td class="num ${m.over ? "bad" : ""}">${esc(m.triangles)}${m.budget ? ` / ${esc(m.budget)}` : ""}</td>
      <td class="num ${m.non_manifold ? "bad" : ""}">${esc(m.non_manifold)}</td>
      <td class="num">${m.bones == null ? "—" : esc(m.bones)}</td><td class="num">${esc(m.actions)}</td>
      <td><span class="chip ${m.approval === "approved" ? "merged" : m.approval === "rejected" ? "failed" : "pending"}" title="${attr(m.approval_note || "")}">${esc(m.approval)}</span></td>
      <td><button class="pill" data-approve="${attr(m.asset)}" data-state="approved">approve</button>
          <button class="pill" data-approve="${attr(m.asset)}" data-state="rejected">reject…</button></td></tr>`).join("");
  const shots = (w.renders || []).map(s => `<a class="st-shot" href="${attr(s.url)}" target="_blank" rel="noopener"><img loading="lazy" src="${attr(s.url)}" alt="${attr(s.name)}"><span>${esc(s.name)}</span></a>`).join("");
  return `<div class="hint">Build each asset on its own, look at it, and lock it in before anything is assembled. The phase-2 gate requires every measured asset to be approved.</div>
    ${shots ? `<h3>Asset previews</h3><div class="st-shots">${shots}</div>` : ""}
    <h3>Assets</h3>${mesh ? `<table class="st-metrics"><thead><tr><th>asset</th><th>tris / budget</th><th>non-manifold</th><th>bones</th><th>clips</th><th>sign-off</th><th></th></tr></thead><tbody>${mesh}</tbody></table>`
      : '<div class="empty">No measured assets yet — assets appear here once phase-2 tasks export and measure them.</div>'}
    <h3>Renders & judge</h3>${studioRounds(p)}`;
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

// ---- Human playtest -----------------------------------------------------------
// Builds to launch on the desktop, sessions (stop / post-session survey) and the
// findings a human logged (F8 in game, or the "+ Finding" form), with triage.
// Everything here comes from p.playtest (studio/status.py); actions POST to
// /api/studio/playtest/* and re-poll.
const PT_FILTERS = [["open", "Open"], ["new", "New"], ["accepted", "Accepted"], ["reopened", "Reopened"],
                    ["fixed", "Fixed"], ["verified", "Verified"], ["wontfix", "Won't fix"],
                    ["duplicate", "Duplicate"], ["all", "All"]];
const PT_OPEN = ["new", "accepted", "reopened"];
let PT_FILTER = (() => { try { return localStorage.getItem("arc.studio.pt.filter") || "open"; } catch (e) { return "open"; } })();
// Legal next states per state. The server decides; this only offers them.
const PT_TRIAGE = {new: ["accepted", "wontfix", "duplicate", "fixed"],
                   accepted: ["fixed", "wontfix", "duplicate"],
                   fixed: ["verified", "reopened"], verified: ["reopened"],
                   wontfix: ["reopened"], duplicate: ["reopened"],
                   reopened: ["accepted", "fixed", "wontfix"]};
const PT_TRIAGE_LABEL = {accepted: "Accept", wontfix: "Won't fix", duplicate: "Duplicate",
                         fixed: "Fixed", verified: "Verified", reopened: "Reopen"};
const PT_SEV = {1: "blocker", 2: "major", 3: "minor", 4: "polish"};
const PT_CATS = ["bug", "feel", "balance", "ux", "visual", "perf", "other"];
// Unsent form values, keyed "<project>|<field key>": every render writes them
// back into the markup, so a 5 s poll never wipes what was typed.
const PT_DRAFT = {};

// True while focus sits in a playtest form field (pollStudio defers then).
function studioEditing() {
  const a = document.activeElement;
  return !!(a && a.closest && a.closest("[data-pt-form]") && /^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName || ""));
}
function ptDraft(key, dflt) {
  const v = PT_DRAFT[STUDIO_OPEN + "|" + key];
  return v == null ? dflt : v;
}
// Timestamps may be epoch seconds or ISO strings.
function ptWhen(ts) {
  if (ts == null || ts === "") return "";
  return typeof ts === "number" ? AGO(ts) + " ago" : fmtT(ts);
}
function ptSelect(key, name, label, opts, dflt) {
  const cur = String(ptDraft(key, dflt));
  return `<select name="${attr(name)}" data-pt-key="${attr(key)}" aria-label="${attr(label)}">${opts.map(([v, l]) =>
    `<option value="${attr(v)}"${String(v) === cur ? " selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
}

function ptSurvey(s) {
  const k = f => `survey:${s.id}:${f}`;
  const five = [["", "—"], ["1", "1"], ["2", "2"], ["3", "3"], ["4", "4"], ["5", "5"]];
  return `<div class="pt-survey" data-pt-form="survey">
    <span class="hint">survey</span>
    <label>fun ${ptSelect(k("fun"), "fun", "fun, 1 to 5", five, "")}</label>
    <label>clarity ${ptSelect(k("clarity"), "clarity", "clarity, 1 to 5", five, "")}</label>
    <label>difficulty ${ptSelect(k("difficulty"), "difficulty", "difficulty, 1 to 5", five, "")}</label>
    <input type="text" name="note" data-pt-key="${attr(k("note"))}" aria-label="survey note" placeholder="note (optional)" maxlength="2000" value="${attr(ptDraft(k("note"), ""))}">
    <button class="pill" data-pt-survey="${attr(s.id)}">Save</button>
  </div>`;
}

function ptFinding(f) {
  const sev = PT_SEV[f.severity] ? +f.severity : 4;
  // The server says which moves are legal (f.next); the table is the fallback.
  const moves = Array.isArray(f.next) ? f.next : (PT_TRIAGE[f.state] || []);
  const buttons = moves.filter(st => PT_TRIAGE_LABEL[st]).map(st =>
    `<button class="pill" data-pt-triage="${attr(f.id)}" data-pt-state="${st}">${PT_TRIAGE_LABEL[st]}</button>`).join("");
  const shot = f.screenshot_url
    ? `<a class="pt-thumb" href="${attr(f.screenshot_url)}" target="_blank" rel="noopener"><img loading="lazy" src="${attr(f.screenshot_url)}" alt="${attr("screenshot " + (f.screenshot || ""))}"></a>` : "";
  const link = f.link ? (/^https?:\/\//.test(f.link)
    ? `<a class="tag" href="${attr(f.link)}" target="_blank" rel="noopener">${esc(f.link)}</a>`
    : `<span class="tag">${esc(f.link)}</span>`) : "";
  return `<div class="pt-finding ${attr(f.state)}">
    ${shot}
    <div class="pt-body">
      <div class="pt-meta">
        <span class="pt-sev s${sev}" title="severity ${sev}">${sev} ${PT_SEV[sev]}</span>
        <span class="tag">${esc(f.category || "other")}</span>
        <span class="chip pt-state ${attr(f.state)}">${esc(f.state)}</span>
        <span class="hint">${esc(f.build || "")}${f.sha ? " @ " + esc(String(f.sha).slice(0, 7)) : ""}${f.scene ? " · " + esc(f.scene) : ""} · ${esc(f.source || "")} ${esc(ptWhen(f.ts))}</span>
        ${link}
      </div>
      <div class="pt-note">${esc(f.note || "")}</div>
      ${f.triage_note ? `<div class="hint">triage: ${esc(f.triage_note)}</div>` : ""}
      ${f.fixed_in ? `<div class="hint">fixed in ${esc(String(f.fixed_in).slice(0, 7))}</div>` : ""}
      ${buttons ? `<div class="pt-actions">${buttons}</div>` : ""}
    </div>
  </div>`;
}

function studioPlaytestView(p) {
  const pt = p.playtest;
  if (!pt) return '<div class="empty">No playtest data for this project (the dashboard server predates human playtesting).</div>';
  if (pt.error) return `<div class="empty bad">Playtest data unavailable: ${esc(pt.error)}</div>`;
  const reason = !pt.godot ? "Godot is not installed on this machine"
    : !pt.display ? "no display is configured (STUDIO_DISPLAY)" : "";
  const head = `<div class="pt-head">
    <span class="pill ${pt.godot ? "good" : "bad"}">godot <b>${pt.godot ? "yes" : "no"}</b></span>
    <span class="pill ${pt.display ? "good" : "bad"}">display <b>${esc(pt.display || "none")}</b></span>
    ${reason ? `<span class="bad">Cannot launch: ${esc(reason)}.</span>` : ""}
  </div>
  <div class="hint">F8 in game = log a finding with screenshot. Accepted findings are handed to the planner on the next <code>studio plan</code>.</div>`;

  const builds = (pt.builds || []).map(b => `<tr>
      <td>${esc(b.id)}</td><td><code>${esc(String(b.sha || "").slice(0, 7))}</code></td>
      <td>${esc(b.subject || "")}</td><td class="hint">${esc(ptWhen(b.ts))}</td>
      <td><button class="pill" data-pt-launch="${attr(b.id)}"${reason ? ` disabled title="${attr(reason)}"` : ""}>▶ Play</button></td></tr>`).join("");
  const buildsHtml = builds
    ? `<table class="st-metrics"><thead><tr><th>build</th><th>sha</th><th>subject</th><th>age</th><th></th></tr></thead><tbody>${builds}</tbody></table>`
    : '<div class="empty">No playable builds (no <code>main</code> or <code>task/*</code> branch found).</div>';

  const sessions = (pt.sessions || []).map(s => `<div class="pt-session ${attr(s.status)}">
      <div class="pt-meta">
        <b>${esc(s.id)}</b> <span class="tag">${esc(s.build || "")}</span>
        <code>${esc(String(s.sha || "").slice(0, 7))}</code>
        <span class="chip ${s.status === "running" || s.status === "preparing" ? "running" : s.status === "failed" ? "failed" : "merged"}">${esc(s.status)}</span>
        ${s.status === "preparing" ? `<span class="hint">first play of this build: cloning + importing, Godot opens when ready</span>` : ""}
        ${s.error ? `<span class="hint">${esc(s.error)}</span>` : ""}
        <span class="hint">started ${esc(ptWhen(s.started))} · ${esc(s.findings || 0)} finding${s.findings === 1 ? "" : "s"}</span>
        ${s.status === "running" ? `<button class="pill" data-pt-stop="${attr(s.id)}">■ Stop</button>` : ""}
        ${s.survey ? `<span class="hint">fun ${esc(s.survey.fun)} · clarity ${esc(s.survey.clarity)} · difficulty ${esc(s.survey.difficulty)}${s.survey.note ? " — " + esc(s.survey.note) : ""}</span>` : ""}
      </div>
      ${s.status !== "running" && s.status !== "preparing" && !s.survey ? ptSurvey(s) : ""}
    </div>`).join("");

  const all = pt.findings || [];
  const counts = pt.counts || {};
  const inFilter = (f, v) => v === "all" || (v === "open" ? PT_OPEN.includes(f.state) : f.state === v);
  const shown = all.filter(f => inFilter(f, PT_FILTER));
  const chips = PT_FILTERS.map(([v, l]) => {
    const n = v !== "all" && counts[v] != null ? counts[v] : all.filter(f => inFilter(f, v)).length;
    return `<button class="st-view" data-pt-filter="${v}" aria-pressed="${PT_FILTER === v}">${l} <span class="hint">${esc(n)}</span></button>`;
  }).join("");

  const add = `<details class="pt-add"${ptDraft("add:open", "") ? " open" : ""}><summary>+ Finding</summary>
    <div class="pt-form" data-pt-form="add">
      ${ptSelect("add:category", "category", "category", PT_CATS.map(c => [c, c]), "bug")}
      ${ptSelect("add:severity", "severity", "severity", [1, 2, 3, 4].map(n => [String(n), `${n} ${PT_SEV[n]}`]), "3")}
      <input type="text" name="note" data-pt-key="add:note" aria-label="finding note" placeholder="what happened?" maxlength="2000" value="${attr(ptDraft("add:note", ""))}">
      <button class="pill" data-pt-add="1">Add</button>
    </div></details>`;

  return `${head}
    <h3>Builds</h3>${buildsHtml}
    <h3>Sessions</h3>${sessions || '<div class="empty">No playtest sessions yet — press ▶ Play on a build.</div>'}
    <h3>Findings</h3>
    <div class="st-views pt-filters">${chips}</div>
    ${add}
    ${shown.length ? `<div class="pt-findings">${shown.map(ptFinding).join("")}</div>` : '<div class="empty">No findings in this filter.</div>'}`;
}

// Draft every keystroke / selection so a later render can restore it.
function ptRemember(e) {
  const t = e.target;
  const k = t && t.getAttribute && t.getAttribute("data-pt-key");
  if (k) PT_DRAFT[STUDIO_OPEN + "|" + k] = t.value;
}
document.addEventListener("input", ptRemember);
document.addEventListener("change", ptRemember);
// <details> toggle does not bubble; capture it so the "+ Finding" form stays open.
document.addEventListener("toggle", e => {
  const t = e.target;
  if (t && t.classList && t.classList.contains("pt-add")) PT_DRAFT[STUDIO_OPEN + "|add:open"] = t.open ? "1" : "";
}, true);
function ptClear(prefix) {
  for (const k of Object.keys(PT_DRAFT)) if (k.startsWith(STUDIO_OPEN + "|" + prefix)) delete PT_DRAFT[k];
}
async function ptPost(path, body) {
  let res;
  try { res = await jpost("/api/studio/playtest/" + path, {project: STUDIO_OPEN, ...body}); }
  catch (err) { alert(`playtest ${path} failed: ${err}`); return false; }
  if (res.code !== 200) { alert(`playtest ${path} failed: ${(res.body && res.body.error) || res.code}`); return false; }
  return true;
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
      <div class="st-views" role="tablist">${STUDIO_VIEWS.map(([v, label]) => {
        const n = v === "board" ? ((p.kanban || {}).blocked || []).length : 0;
        return `<button class="st-view" role="tab" data-studio-view="${v}" aria-selected="${STUDIO_VIEW === v}">${label}${n ? ` <span class="bad">${n}</span>` : ""}</button>`;
      }).join("")}</div>
      ${STUDIO_VIEW === "board" ? studioBoard(p)
        : STUDIO_VIEW === "changelog" ? studioChangelog(p)
        : STUDIO_VIEW === "evidence" ? studioEvidence(p)
        : STUDIO_VIEW === "workbench" ? studioWorkbenchView(p)
        : STUDIO_VIEW === "playtest" ? studioPlaytestView(p)
        : `<div class="st-grid">
            <div class="st-col"><h3>Phase gate</h3>${studioGate(p)}</div>
            <div class="st-col"><h3>Phase tasks</h3>${studioBoards(p)}</div>
          </div>
          <h3>Latest renders & judge</h3>${studioRounds(p)}`}` : ""}
    </section>`;
  }).join("");
}

// Delegated, like summary.js, so the test harness's element stubs are untouched.
document.addEventListener("click", async (e) => {
  const el = e.target && e.target.closest;
  if (!el) return;
  const t = e.target.closest("[data-studio-project]");
  if (t) { STUDIO_OPEN = t.getAttribute("data-studio-project"); renderStudio(); return; }
  const v = e.target.closest("[data-studio-view]");
  if (v) {
    STUDIO_VIEW = v.getAttribute("data-studio-view");
    try { localStorage.setItem("arc.studio.view", STUDIO_VIEW); } catch (err) {}
    renderStudio(); return;
  }
  const a = e.target.closest("[data-approve]");
  if (a) {
    const state = a.getAttribute("data-state");
    let note = "";
    if (state === "rejected") { note = prompt("What must change about this asset?") || ""; if (!note.trim()) return; }
    const {code, body} = await jpost("/api/studio/approve",
      {project: STUDIO_OPEN, asset: a.getAttribute("data-approve"), state, note});
    if (code !== 200) alert(`not recorded: ${body.error || code}`);
    pollStudio();
    return;
  }
  const pf = e.target.closest("[data-pt-filter]");
  if (pf) {
    PT_FILTER = pf.getAttribute("data-pt-filter");
    try { localStorage.setItem("arc.studio.pt.filter", PT_FILTER); } catch (err) {}
    renderStudio(); return;
  }
  const pl = e.target.closest("[data-pt-launch]");
  if (pl) {
    if (pl.disabled) return;
    await ptPost("launch", {build: pl.getAttribute("data-pt-launch")});
    pollStudio(); return;
  }
  const ps = e.target.closest("[data-pt-stop]");
  if (ps) {
    await ptPost("stop", {session: ps.getAttribute("data-pt-stop")});
    pollStudio(); return;
  }
  const tr = e.target.closest("[data-pt-triage]");
  if (tr) {
    const state = tr.getAttribute("data-pt-state");
    const body = {finding: tr.getAttribute("data-pt-triage"), state};
    if (state === "wontfix" || state === "duplicate" || state === "reopened") {
      const note = prompt(state === "duplicate" ? "Duplicate of which finding?"
        : state === "reopened" ? "Why is this reopened?" : "Why won't this be fixed?", "");
      if (note == null) return;          // cancelled: record nothing
      body.note = note;
    } else if (state === "accepted") {
      const link = prompt("Link a task id or PR url (optional):", "");
      if (link == null) return;
      if (link.trim()) body.link = link.trim();
    }
    await ptPost("triage", body);
    pollStudio(); return;
  }
  const sv = e.target.closest("[data-pt-survey]");
  if (sv) {
    const sid = sv.getAttribute("data-pt-survey");
    const form = sv.closest("[data-pt-form]");
    const val = n => { const x = form && form.querySelector(`[name="${n}"]`); return x ? x.value : ""; };
    const fun = +val("fun"), clarity = +val("clarity"), difficulty = +val("difficulty");
    if (!fun || !clarity || !difficulty) { alert("Rate fun, clarity and difficulty (1–5) first."); return; }
    if (await ptPost("survey", {session: sid, fun, clarity, difficulty, note: val("note")})) ptClear(`survey:${sid}:`);
    pollStudio(); return;
  }
  const ad = e.target.closest("[data-pt-add]");
  if (ad) {
    const form = ad.closest("[data-pt-form]");
    const val = n => { const x = form && form.querySelector(`[name="${n}"]`); return x ? x.value : ""; };
    const note = val("note").trim();
    if (!note) { alert("Describe the finding first."); return; }
    if (await ptPost("finding", {category: val("category") || "other", severity: +val("severity") || 3, note})) ptClear("add:");
    pollStudio(); return;
  }
});
