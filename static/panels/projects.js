"use strict";
// ---- task DAG renderer: layered left→right, status colors, hover, click-to-transcript ----
function taskDag(dag, opts) {
  opts = opts || {};
  const mini = opts.size !== "full";
  const topo = !!opts.topo; // static workload topology: no per-run state
  const nodes = (dag && dag.nodes) || [], edges = (dag && dag.edges) || [];
  if (!nodes.length) return "";
  // A box or two teaches nothing a status dot does not — keep the card
  // compact and skip the picture entirely.
  if (mini && nodes.length < 3) return "";
  const lvl = {}; nodes.forEach(n => lvl[n.id] = 0);
  // Some graphs are cyclic (topologies: verify→synthesize, wiring_fix→
  // integration_review). The level relaxation below cannot converge on a
  // cycle: its guard truncates with cycle nodes pushed to level ~100, whose
  // x = level*(NW+GX) lands far outside the viewBox (invisible nodes).
  // Detect back edges via DFS, layer over the remaining acyclic edges only,
  // and draw back edges separately as loop-backs.
  const adj = {}, back = new Set(), mark = {};
  edges.forEach(e => { if (e.src !== e.dst && lvl[e.src] !== undefined && lvl[e.dst] !== undefined) (adj[e.src] = adj[e.src] || []).push(e); });
  const dfs = u => { mark[u] = 1; (adj[u] || []).forEach(e => {
    if (mark[e.dst] === 1) back.add(e);
    else if (!mark[e.dst]) dfs(e.dst); }); mark[u] = 2; };
  nodes.forEach(n => { if (!mark[n.id]) dfs(n.id); });
  let ch = true, guard = 0;
  while (ch && guard++ < nodes.length + 2) { ch = false;
    for (const e of edges) if (e.src !== e.dst && !back.has(e) && lvl[e.src] !== undefined && lvl[e.dst] !== undefined && lvl[e.dst] < lvl[e.src] + 1) { lvl[e.dst] = lvl[e.src] + 1; ch = true; } }
  // normalize levels to dense column indices so x can never exceed the
  // viewBox even if the levels above came out sparse
  const colOf = {}; [...new Set(nodes.map(n => lvl[n.id]))].sort((a, b) => a - b).forEach((l, i) => colOf[l] = i);
  const cols = {};
  nodes.forEach(n => (cols[colOf[lvl[n.id]]] = cols[colOf[lvl[n.id]]] || []).push(n));
  const ncols = Object.keys(cols).length;
  let NW = 200, NH = 48, GX = 60, GY = 26, PAD = 14, labFs = 0;
  if (mini) {
    NH = 30; GX = 46; GY = 12; PAD = 6;
    // Labels must survive the scale into the ~336px card: effective px =
    // font size x CARDW/W. Solve for the widest node that keeps 9px text at
    // >= 7px effective; when even that box is too narrow to be worth a
    // label, drop the labels and keep just the coloured status boxes.
    const CARDW = 336, MINEFF = 7;
    NW = Math.min(132, Math.floor((CARDW * 9 / MINEFF - 2 * PAD - (ncols - 1) * GX) / ncols));
    labFs = NW >= 60 ? 9 : 0;
    if (!labFs) NW = 40;
  }
  // A tall column used to stretch the viewBox until scaling made everything
  // illegible. Past the ~120px cap, stop drawing rows and count the rest
  // into a "+N more" footer instead (2 rows of NH+GY + footer = 112px).
  const pos = {}; let dropped = 0;
  Object.entries(cols).forEach(([l, ns]) => ns.forEach((n, i) => {
    if (mini && i >= 2) { dropped++; return; }
    pos[n.id] = {x: PAD + (+l) * (NW + GX), y: PAD + 12 + i * (NH + GY)}; }));
  const W = PAD * 2 + ncols * NW + Math.max(0, ncols - 1) * GX;
  const maxY = Math.max(...Object.values(pos).map(p => p.y));
  const H = maxY + NH + PAD + (back.size ? 22 : 0) + (dropped ? 16 : 0);
  let s = "", bi = 0;
  for (const e of edges) { const a = pos[e.src], b = pos[e.dst]; if (!a || !b || e.src === e.dst) continue;
    if (back.has(e)) { // loop-back: swing below the graph so it stays visible
      const y = maxY + NH + 12 + Math.min(bi++, 2) * 7;
      s += `<path d="M${a.x + NW / 2},${a.y + NH} C${a.x + NW / 2},${y} ${b.x + NW / 2},${y} ${b.x + NW / 2},${b.y + NH}" stroke="#d29922" stroke-dasharray="4 3" fill="none"><title>loop back (cycle): ${esc(e.src)} → ${esc(e.dst)}</title></path>`;
      continue; }
    s += `<path d="M${a.x + NW},${a.y + NH / 2} C${a.x + NW + GX / 2},${a.y + NH / 2} ${b.x - GX / 2},${b.y + NH / 2} ${b.x},${b.y + NH / 2}" stroke="#30363d" fill="none"${e.conditional ? ' stroke-dasharray="4 3"' : ""}/>`; }
  for (const n of nodes) { const p = pos[n.id]; if (!p) continue;
    const c = topo ? ((dag.starts || []).includes(n.id) ? "#58a6ff" : n.gather ? "#bc8cff" : "#8b949e") : (STATUSC[n.status] || STATUSC.pending);
    const run = !topo && (n.status === "running" || n.live);
    const att = n.attempts || 0, escn = n.escalations || 0;
    const ttip = topo ? [n.id, n.gather ? "gather — joins parallel branches" : (dag.starts || []).includes(n.id) ? "start" : ""].filter(Boolean).join("\n")
      : [n.title || n.id,
      `status: ${n.status}${n.live ? " (live)" : ""}`,
      `impl: ${short(n.model)} · review: ${revShort(n.reviewer)}`,
      n.verify_cmd ? `verify: ${n.verify_cmd}` : ""].filter(Boolean).join("\n");
    s += topo
      ? `<g class="node topo" data-node="${attr(n.id)}" role="button" tabindex="0" ` +
        `aria-label="${attr(`${n.id} — click to read what this stage does`)}">` +
        `<title>${esc(ttip)}: click to read what it does</title>` +
        `<rect x="${p.x}" y="${p.y}" width="${NW}" height="${NH}" rx="7" fill="#0d1117" stroke="${c}" stroke-width="1.6"/>` +
        `<text x="${p.x + 10}" y="${p.y + 20}" font-size="11" fill="#c9d1d9">${esc(n.id.slice(0, 26))}</text>` +
        `<text x="${p.x + 10}" y="${p.y + 35}" font-size="9" fill="#8b949e">${(dag.starts || []).includes(n.id) ? "start" : n.gather ? "gather" : ""}</text>`
      : `<g class="node" data-t="${attr(n.id)}" data-f="${attr(opts.file || "")}" role="button" tabindex="0" aria-label="${attr(`task ${n.id}: ${n.status}${n.live ? " (live)" : ""}`)}">` +
         `<title>${esc(ttip)}</title>` +
         `<rect x="${p.x}" y="${p.y}" width="${NW}" height="${NH}" rx="${mini ? 5 : 7}" fill="#0d1117" stroke="${c}" stroke-width="1.6" class="${run ? "run" : ""}"/>` +
         (mini && !labFs ? "" :
         `<text x="${p.x + (mini ? 6 : 10)}" y="${p.y + (mini ? 12.5 : 17)}" font-size="${mini ? labFs : 11}" fill="#c9d1d9">${esc(n.id.slice(0, mini ? Math.max(6, Math.floor((NW - 12) / 6)) : 26))}${mini ? (STATUSG[n.status] || "") : ""}</text>` +
         `<text x="${p.x + (mini ? 6 : 10)}" y="${p.y + (mini ? 24.5 : 33)}" font-size="${mini ? labFs : 9}" fill="#8b949e">${esc(short(n.model))} → ${esc(revShort(n.reviewer))}${att > 1 ? ` · x${att}` : ""}${escn ? ` ⬆${escn}` : ""}</text>`);
    if (!topo && !mini)
      s += `<text x="${p.x + 10}" y="${p.y + 45}" font-size="9" fill="${c}">${esc(n.status)}${n.live ? " · live" : ""}</text>`;
    if (!topo && (att > 1 || escn > 0))
      s += `<path d="M${p.x + NW - 4},${p.y} C${p.x + NW - 4},${p.y - 12} ${p.x + 4},${p.y - 12} ${p.x + 4},${p.y}" stroke="#d29922" stroke-dasharray="3 2" fill="none"/>` +
           `<text x="${p.x + NW / 2}" y="${p.y - 4}" font-size="${mini ? 7 : 8.5}" fill="#d29922" text-anchor="middle">x${att}${escn ? " ⬆" + escn : ""}</text>`;
    if (!topo && n.last_verdict && n.last_verdict.pass === false && n.status !== "merged")
      s += `<circle cx="${p.x + NW - 5}" cy="${p.y + 5}" r="3" fill="#f85149"/>`;
    s += `</g>`; }
  if (dropped) s += `<text x="${PAD}" y="${H - 4}" font-size="9" fill="#8b949e">+${dropped} more — see the full DAG in the project detail</text>`;
  const wide = W > 1400;
  return `<svg viewBox="0 0 ${W} ${H}" style="display:block;${wide ? `width:${W}px;max-width:none` : "width:100%"}" preserveAspectRatio="xMinYMid meet">${s}</svg>`;
}
function bindDagClicks(sel, runsCache) {
  document.querySelectorAll(sel + " g.node").forEach(g => g.onclick = ev => {
    ev.stopPropagation();
    openTaskTranscript(g.dataset.f, g.dataset.t, runsCache || null);
  });
}

// ---- projects list ----
function card(p, i) {
  // One line of signal: repo, title, LIVE, merged/failed chips, activity,
  // archive. Per-task status dots and token stats are gone from the card —
  // the chips already count failures, the mini DAG colors nodes for 3+ task
  // projects, and tokens live in the detail meta line.
  const tot = (p.progress && p.progress.total) || p.n_tasks || 1;
  const done = (p.progress && p.progress.done) || 0;
  const failed = ((p.statuses && p.statuses.failed) || 0) + ((p.statuses && p.statuses.conflict) || 0);
  const open = EXPANDED === p.file;
  return `<div class="pwrap${open ? " open" : ""}" data-file="${attr(p.file)}">
    <div class="prow" data-i="${i}" role="button" tabindex="0" aria-expanded="${open}" aria-label="open project ${attr(p.title)}">
      <span class="chev">▶</span>
      <span class="pc" title="${attr(p.repo || "")}">${esc(repoShort(p.repo))}</span>
      <span class="pt"><b>${esc(p.title)}</b> ${liveBadge(p)}${p.archived ? ' <span class="chip pending" title="archived — hidden from the active list">archived</span>' : ""}</span>
      <span class="pc"><span class="chip merged">${done}/${tot} merged</span>${failed ? ` <span class="chip failed">${failed} failed</span>` : ""}</span>
      <span class="pc">${fmtT(p.last_activity)}</span>
      <span><button class="act seg" data-arch="${attr(p.file)}" data-on="${p.archived ? 1 : 0}">${p.archived ? "restore" : "archive"}</button></span>
    </div>
    ${taskDag(p.dag, {size: "mini", file: p.file})}
    ${(p.errors && p.errors.length) ? `<div class="cardnote" title="${attr(p.errors[0].error)}">⚠ ${esc(p.errors[0].id)}: ${esc(String(p.errors[0].error).slice(0, 90))}</div>` : ""}
  </div>`;
}
// "Is this actually progressing?" had no answer on the page: a task that had
// produced nothing for ten minutes looked identical to one mid-edit. This
// joins the step it is on (node_start/node_end) with whether its output is
// still growing (driver.progress).
function progressLines(progress) {
  const rows = Object.entries(progress || {});
  if (!rows.length) return "";
  return rows.map(([tid, g]) => {
    const quiet = g.idle_s == null ? "" :
      ` · quiet ${tick(g.idle_s)}`;
    const size = g.bytes ? ` · ${fmtK(g.bytes)}B written` : "";
    // A failing gate is the difference between "working" and "stuck in a
    // loop redoing work that keeps being rejected".
    const looping = g.last_gate_failed;
    const cls = looping ? "bad" : g.moving ? "good" : "warn";
    const mark = looping ? "↻" : g.moving ? "▸" : "⏸";
    const why = looping
      ? ` <span class="bad">· verify gate rejected the last attempt${g.attempt > 1 ? `, retry ${esc(g.attempt)}` : ""}</span>${g.gate_log ? ` <button class="act seg" data-gate="${attr(g.gate_log)}">why?</button>` : ""}`
      : "";
    return `<div class="hint" style="margin-top:3px">
      <span class="${cls}">${mark} ${esc(g.label)}</span>
      <span>${esc(tid)}</span>${g.attempt > 1 ? ` <span class="tag">attempt ${esc(g.attempt)}</span>` : ""}
      <span class="hint">${size}${quiet}</span>${why}
    </div>`;
  }).join("");
}

// One honest status instead of two overlapping ones.
//
// `active` means the DATABASE says a task is running (or an agent is in
// flight); `run_pid` means an OS PROCESS owns this task file. They answer
// different questions, and showing both as separate badges — "LIVE" plus
// "running (pid 28336)" — said the same thing twice when they agreed and
// explained nothing when they did not.
//
// Their DISAGREEMENT is the useful signal: rows marked running with no
// process are leftovers from a killed run, which is the single most common
// wrong state this fleet gets into. That now says so, and says what to do.
function liveBadge(p) {
  if (p.run_pid) return `<span class="live" title="process ${esc(p.run_pid)} is running this project">LIVE</span>`;
  if (p.active) return `<span class="chip failed" title="marked running in the database, but no process owns this task file — a run was killed. Run: main.py code reconcile">stale</span>`;
  return "";
}

async function showGateLog(fn) {
  const d = await jget(`/api/gate-log?file=${encodeURIComponent(fn)}`);
  $("#drawer").classList.add("open");
  $("#drawer-title").textContent = "verify gate output";
  $("#drawer-sub").textContent = d.error ? "" : `${fn} · ${d.total_lines} lines`;
  drawerFile = null;
  $("#drawer-body").textContent = d.error || (d.lines || []).join("\n");
}
document.addEventListener("click", ev => {
  const b = ev.target.closest && ev.target.closest("[data-gate]");
  if (b) { ev.stopPropagation(); showGateLog(b.dataset.gate); }
});

// ---- the pipeline lane -----------------------------------------------------
// Every task walks the same seven stages. Showing them as a lane answers "what
// is happening and what is left" at a glance, which a status word cannot:
// "running" covers writing code, waiting on a gate, and sitting in PR review,
// and those need completely different reactions from an operator.
const STAGES = [
  ["alloc",     "worktree"],
  ["implement", "code"],
  ["gate",      "gate"],
  ["review",    "review"],
  ["publish",   "PR"],
  ["pr_review", "2 approvals"],
  ["pr_merge",  "merged"],
];

function pipelineLane(node) {
  const st = node.status || "pending";
  const prog = node.task_progress || null;
  const cur = prog ? prog.step : null;
  // Where the task currently is. For a finished-badly task we usually do NOT
  // know which stage killed it, and guessing (painting stage 0 red, implying
  // it died creating a worktree) is worse than admitting it: the lane is left
  // neutral and a failed badge is appended instead.
  const known = {pending: -1, in_review: 5, merged: 6}[st];
  const idx = cur ? STAGES.findIndex(x => x[0] === cur)
                  : (known === undefined ? -1 : known);
  const bad = st === "failed" || st === "conflict";

  const lane = STAGES.map(([key, label], i) => {
    let cls = "";
    if (st === "merged") cls = "done";
    else if (bad && idx >= 0) cls = i < idx ? "done" : (i === idx ? "bad" : "");
    else if (!bad) {
      if (i < idx) cls = "done";
      else if (i === idx) cls = (prog && prog.last_gate_failed) ? "bad"
                              : (key === "pr_review" ? "wait" : "now");
    }
    return `<span class="st ${cls}" title="${attr(key)}">${esc(label)}</span>`
      + (i < STAGES.length - 1 ? '<span class="lanesep">›</span>' : "");
  }).join("");
  const tail = bad && idx < 0
    ? `<span class="lanesep">›</span><span class="st bad" title="stage unknown">${esc(st)}</span>`
    : "";
  return `<div class="lane">${lane}${tail}</div>`;
}


function projectMatches(p) {
  if (FILT.r && (p.repo || "") !== FILT.r) return false;
  if (FILT.m && !(p.models || []).includes(FILT.m)) return false;
  const q = FILT.q.trim().toLowerCase();
  if (q && !((p.title || "").toLowerCase().includes(q) || (p.file || "").toLowerCase().includes(q))) return false;
  const st = p.statuses || {};
  if (FILT.s === "running") return (st.running || 0) > 0 || !!p.run_pid;
  if (FILT.s === "failed") return ((st.failed || 0) + (st.conflict || 0)) > 0;
  if (FILT.s === "merged") {
    const tot = (p.progress && p.progress.total) || p.n_tasks || 0;
    return tot > 0 && ((p.progress && p.progress.done) || 0) === tot;
  }
  return true;
}
function visibleProjects() {
  return PROJECTS.filter(p => SHOW_ARCHIVED ? p.archived : (!p.archived || p.phase === "attention"))
                 .filter(p => !PHASE_FILT || p.phase === PHASE_FILT)
                 .filter(projectMatches);
}
function renderProjects() {
  rebuildFilterOptions(); syncFilterControls();
  const vis = visibleProjects();
  $("#proj-meta").textContent = `(${vis.length}${vis.length !== PROJECTS.length ? " of " + PROJECTS.length : ""})`;
  $("#f-hint").textContent = PROJECTS.length ? `${vis.length} of ${PROJECTS.length} projects` : "";
  $("#projects-empty").style.display = PROJECTS.length ? "none" : "";
  // Grouped by PHASE, not by repo. The operator's question is "what needs
  // me?", and eighteen cards sorted by repo answered a question nobody asked:
  // finished work buried the two projects that were actually running.
  const PHASES = [
    ["running",   "running now"],
    ["in_review", "pull requests awaiting review"],
    ["attention", "needs attention"],
    ["new",       "never run"],
    ["done",      "done"],
  ];
  const counts = {};
  PROJECTS.filter(p => !p.archived || p.phase === "attention").forEach(p => counts[p.phase] = (counts[p.phase] || 0) + 1);
  $("#phase-filter").innerHTML = PHASES.filter(([k]) => counts[k])
    .map(([k, label]) => `<button class="act seg ${PHASE_FILT === k ? "on" : ""}" data-ph="${k}">${label} ${counts[k]}</button>`)
    .join("") + (PHASE_FILT ? '<button class="act seg" data-ph="">all</button>' : "");

  const byPhase = {};
  vis.forEach(p => (byPhase[p.phase] = byPhase[p.phase] || []).push(p));

  // The detail panel renders inline UNDER its project row. Park it at the
  // end of <main> before the innerHTML wipe so it is never destroyed, then
  // re-attach it under the expanded row (if that row is visible). Unconditional
  // appendChild: a no-op when already parked, a rescue when under a row.
  const det = $("#view-detail");
  document.querySelector("main").appendChild(det);

  $("#projects").innerHTML = PHASES.map(([key, label]) => {
    const items = byPhase[key] || [];
    if (!items.length) return "";
    // `done` collapses by default: it is the largest group and the least
    // actionable, which is exactly what was causing the clutter.
    const collapsed = key === "done" && !PHASE_FILT && !SHOW_ARCHIVED && !OPEN_PHASES.done
      && !(EXPANDED && (byPhase.done || []).some(p => p.file === EXPANDED));
    return `<div class="phasehead ${key}">
        <h3>${label}</h3><span class="hint">${items.length}</span>
        ${key === "done" ? `<button class="act seg" data-toggle="done">${collapsed ? "show" : "hide"}</button>
          <button class="act seg" data-archive-all="1">archive all done</button>` : ""}
      </div>
      ${collapsed ? "" : `<div class="prows">${items.map(p => card(p, PROJECTS.indexOf(p))).join("")}</div>`}`;
  }).join("") || `<div class="empty">${SHOW_ARCHIVED ? "Nothing archived." : "No projects match this filter."}</div>`;

  const wrap = EXPANDED && $("#projects").querySelector(`.pwrap[data-file="${CSS.escape(EXPANDED)}"]`);
  if (wrap) { det.style.display = ""; wrap.after(det); }
  else det.style.display = "none";
  bindDagClicks("#projects");

  document.querySelectorAll("#phase-filter [data-ph]").forEach(b =>
    b.onclick = () => { PHASE_FILT = b.dataset.ph; renderProjects(); });
  document.querySelectorAll("#projects [data-toggle]").forEach(b =>
    b.onclick = () => { OPEN_PHASES.done = !OPEN_PHASES.done; renderProjects(); });
  document.querySelectorAll("#projects [data-archive-all]").forEach(b =>
    b.onclick = async () => {
      const done = PROJECTS.filter(p => p.phase === "done" && !p.archived);
      if (!done.length || !confirm(`Archive ${done.length} finished project(s)?\n\nTask files are not touched and they stay runnable.`)) return;
      for (const p of done) await jpost("/api/projects/archive", {file: p.file, archived: true});
      pollProjects(); pollGithub();
    });
  document.querySelectorAll("#projects [data-arch]").forEach(b =>
    b.onclick = async ev => { ev.stopPropagation();
      const {code, body} = await jpost("/api/projects/archive",
        {file: b.dataset.arch, archived: b.dataset.on !== "1"});
      if (code !== 200) alert(body.error || "failed");
      else if (body.warning) alert(body.warning);
      pollProjects();
    });
}
async function pollProjects() {
  try {
    const d = await jget("/api/projects");
    PROJECTS = d.projects || [];
    $("#clock").textContent = new Date().toLocaleTimeString();
    markFail("projects", false);
  } catch (e) { markFail("projects", true); }
  renderProjects();
  // Re-open the inline detail panel if the URL hash asks for it and it is
  // not open yet (page load with x=…, or the project just appeared).
  if (EXPANDED && EXPANDED !== CUR &&
      $("#projects").querySelector(`.pwrap[data-file="${CSS.escape(EXPANDED)}"]`)) openDetail(EXPANDED);
}


// ---- the pipeline shape (static /api/graphs) ----
// ONE diagram: the code-tasks pipeline every project runs through, generated
// by the server from the same code that builds the live graph. It used to be
// two diagrams of workloads nobody had run in days. Dashed edge = conditional
// (when= gate); amber dashed = a loop back — fix rounds, escalation, a PR sent
// back by its reviewers, a resync after the base moved, a crashed reviewer
// retrying. Those loops are the point: the happy path is obvious, the loops
// are not, and they are where a task actually spends its time.
let TOPOS = null;
async function pollTopos() {
  try {
    TOPOS = await jget("/api/graphs");
    markFail("graphs", false);
  } catch (e) { markFail("graphs", true); return; }
  renderTopos();
}
// ---- the pipeline explainer -------------------------------------------
// Clicking a stage tells you what it does, what it is FOR, what goes wrong
// with it, every edge that leaves it with the condition in plain English, and
// how it has actually behaved over the last week. The prose is written by
// hand; the edges and the numbers are generated, so the picture cannot claim a
// node retries three times while the code says ten.
let PIPE = null, PIPE_OPEN = null;

async function loadPipelineDoc() {
  if (PIPE) return PIPE;
  try { PIPE = await jget("/api/pipeline"); } catch (e) { PIPE = null; }
  return PIPE;
}

function pipeOverview(d) {
  const o = (d && d.overview) || {};
  return `<div class="pipe-overview">
    ${(o.paragraphs || []).map(t => `<p>${esc(t)}</p>`).join("")}
    <div class="pipe-key">${(o.reading || []).map(t => `<span>${esc(t)}</span>`).join("")}</div>
    <p class="hint">Click any stage above to read what it does.</p>
  </div>`;
}

function pipeNode(n) {
  const stat = n.stats
    ? `<div class="pipe-stats">
         <span><b>${n.stats.runs}</b> runs</span>
         <span><b>${n.stats.reliability}%</b> reached the next stage</span>
         ${n.stats.median_s != null ? `<span>median <b>${tick(n.stats.median_s)}</b></span>` : ""}
         ${n.stats.errors ? `<span class="bad"><b>${n.stats.errors}</b> errored</span>` : ""}
       </div>`
    : '<div class="pipe-stats"><span class="hint">no runs recorded in the last week</span></div>';
  const outs = (n.outgoing || []).map(e =>
    `<li><code>${esc(e.to)}</code>${e.loop ? ' <span class="tag loop">loops back</span>' : ""}
       <span class="hint">${esc(e.when)}</span></li>`).join("");
  const ins = (n.incoming || []).map(i => `<code>${esc(i)}</code>`).join(" ");
  return `<div class="pipe-detail">
    <h3>${esc(n.title)}${n.start ? ' <span class="tag">start</span>' : ""}${n.gather ? ' <span class="tag">join</span>' : ""}</h3>
    ${stat}
    ${n.what ? `<p><b>What it does.</b> ${esc(n.what)}</p>` : ""}
    ${n.why ? `<p><b>Why it exists.</b> ${esc(n.why)}</p>` : ""}
    ${n.watch ? `<p class="pipe-watch"><b>Worth knowing.</b> ${esc(n.watch)}</p>` : ""}
    ${ins ? `<p class="hint">Reached from: ${ins}</p>` : ""}
    ${outs ? `<div><b>Where it goes next</b><ul class="pipe-edges">${outs}</ul></div>` : ""}
    <button class="act" data-pipe-close="1">close</button>
  </div>`;
}

async function showPipelineNode(id) {
  const d = await loadPipelineDoc();
  const box = $("#topo-detail");
  if (!box) return;
  if (!d) { box.innerHTML = '<div class="empty">explanation unavailable</div>'; return; }
  if (id === PIPE_OPEN) { PIPE_OPEN = null; box.innerHTML = pipeOverview(d); return; }
  const n = (d.nodes || []).find(x => x.id === id);
  PIPE_OPEN = n ? id : null;
  box.innerHTML = n ? pipeNode(n) : pipeOverview(d);
  for (const g of document.querySelectorAll("#topo .node.topo"))
    g.classList.toggle("sel", g.dataset.node === PIPE_OPEN);
  box.onclick = ev => {
    if (ev.target && ev.target.dataset && ev.target.dataset.pipeClose)
      showPipelineNode(PIPE_OPEN);
  };
  box.scrollIntoView({block: "nearest", behavior: "smooth"});
}

function renderTopos() {
  const tops = Object.values(TOPOS || {}).filter(t => t && Array.isArray(t.nodes));
  if (!tops.length) { $("#topo").innerHTML = '<div class="empty">no graphs available</div>'; return; }
  // A loop is an edge that goes BACKWARD in pipeline order (or to itself).
  // Counting "any edge into a node that can be a loop target" was wrong: it
  // called alloc->implement a loop because implement is where fix rounds land.
  const ORDER = ["alloc", "implement", "gate", "review", "escalate", "publish",
                 "pr_fanout", "pr_reviewer", "pr_review", "pr_merge", "fail"];
  const pos = n => { const i = ORDER.indexOf(n); return i < 0 ? 99 : i; };
  const loops = tops.reduce((n, t) => n + (t.edges || [])
    .filter(e => e.src === e.dst || pos(e.dst) < pos(e.src)).length, 0);
  $("#topo-meta").textContent = `dashed = conditional · amber = loop back (${loops} of them)`;
  $("#topo-meta").title = "Generated from the code that builds the live graph — this cannot drift from what runs.";
  $("#topo").innerHTML = tops.map(t => {
    const g = {
      starts: t.starts || [],
      nodes: (t.nodes || []).map(n => ({id: n.name, gather: !!n.gather})),
      edges: (t.edges || []).map(e => ({src: e.src, dst: e.dst, conditional: !!e.conditional})),
    };
    return `<div class="topohead"><h3>${esc(t.name || "graph")}</h3><span class="hint">${g.nodes.length} nodes · ${g.edges.length} edges</span></div>
      <div style="overflow-x:auto">${taskDag(g, {size: "full", topo: true})}</div>`;
  }).join("");
  for (const g of document.querySelectorAll("#topo .node.topo")) {
    const open = () => showPipelineNode(g.dataset.node);
    g.onclick = open;
    g.onkeydown = ev => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); } };
  }
  loadPipelineDoc().then(d => {
    const box = $("#topo-detail");
    if (box && !PIPE_OPEN && d) box.innerHTML = pipeOverview(d);
  });
}


// ---- new project modal ----
const modal = $("#modal");
$("#btn-show-archived").onclick = () => {
  SHOW_ARCHIVED = !SHOW_ARCHIVED;
  $("#btn-show-archived").textContent = SHOW_ARCHIVED ? "show active" : "show archived";
  $("#btn-show-archived").classList.toggle("primary", SHOW_ARCHIVED);
  renderProjects();
};
$("#btn-new").onclick = () => { modal.classList.add("open"); $("#p-msg").textContent = ""; $("#j-msg").textContent = ""; };
$("#modal-close").onclick = () => modal.classList.remove("open");
modal.onclick = ev => { if (ev.target === modal) modal.classList.remove("open"); };
document.querySelectorAll(".tab").forEach(b => b.onclick = () => {
  document.querySelectorAll(".tab").forEach(x => x.classList.remove("on")); b.classList.add("on");
  $("#tab-plan").style.display = b.dataset.t === "plan" ? "" : "none";
  $("#tab-json").style.display = b.dataset.t === "json" ? "" : "none";
});
const repoSel = (sel, inp) => { const el = $(sel); return () => el.value === "__custom" ? $(inp).value.trim() : el.value; };
const planRepo = repoSel("#p-repo", "#p-repo-custom"), jsonRepo = repoSel("#j-repo", "#j-repo-custom");
$("#p-repo").onchange = e => $("#p-repo-custom").style.display = e.target.value === "__custom" ? "" : "none";
$("#j-repo").onchange = e => $("#j-repo-custom").style.display = e.target.value === "__custom" ? "" : "none";

async function doPlan(overwrite) {
  const goal = $("#p-goal").value.trim(), repo = planRepo();
  const msg = $("#p-msg"); msg.className = "";
  if (!goal || !repo) { msg.className = "err"; msg.textContent = "repo and goal are required"; return; }
  msg.textContent = "asking Kimi-K3 to draft the task file… (this takes a minute)";
  const body0 = {goal, repo};
  if (overwrite) body0.overwrite = true;
  const {code, body} = await jpost("/api/projects/create", body0);
  if (code === 409 && body.exists && !overwrite) {
    if (confirm(body.error + "\n\nReplan and overwrite it?")) return doPlan(true);
    msg.className = "err"; msg.textContent = body.error; return;
  }
  if (code !== 200) { msg.className = "err"; msg.textContent = body.error || "failed"; return; }
  msg.className = "okmsg";
  msg.textContent = `planning started (pid ${body.pid}, log ${body.log}) — waiting for ${body.taskfile}…`;
  const expect = body.taskfile, t0 = Date.now();
  const poll = setInterval(async () => {
    await pollProjects();
    if (PROJECTS.some(p => p.file === expect)) { clearInterval(poll);
      msg.textContent = "task file ready — opening it."; setTimeout(() => { modal.classList.remove("open"); openDetail(expect); }, 600); }
    else if (Date.now() - t0 > 240000) { clearInterval(poll);
      msg.className = "err"; msg.textContent = `still planning… see logs/${body.log} — close this and check back.`; }
  }, 5000);
}
$("#p-go").onclick = () => doPlan(false);

$("#j-go").onclick = async () => {
  const title = $("#j-title").value.trim(), repo = jsonRepo(), msg = $("#j-msg"); msg.className = "";
  let tasks;
  try { tasks = JSON.parse($("#j-tasks").value); } catch (e) { msg.className = "err"; msg.textContent = "invalid JSON: " + e.message; return; }
  if (!title || !repo) { msg.className = "err"; msg.textContent = "title and repo are required"; return; }
  const {code, body} = await jpost("/api/projects/create", {title, repo, tasks});
  if (code !== 200) { msg.className = "err"; msg.textContent = body.error || "failed"; return; }
  msg.className = "okmsg"; msg.textContent = `created ${body.file} (${body.n_tasks} tasks).`;
  await pollProjects();
  setTimeout(() => { modal.classList.remove("open"); openDetail(body.file); }, 500);
};


// ---- keyboard access: Enter/Space on a role="button" element does its click ----
document.addEventListener("keydown", e => {
  const t = e.target;
  if ((e.key === "Enter" || e.key === " ") && t.getAttribute && t.getAttribute("role") === "button" && t.tagName !== "BUTTON") {
    e.preventDefault(); t.click();
  }
});
$("#projects").onclick = ev => {
  const el = ev.target.closest("[data-i]");
  const p = el && PROJECTS[+el.dataset.i];
  if (p && p.file) { if (EXPANDED === p.file) closeDetail(); else openDetail(p.file); }
};

