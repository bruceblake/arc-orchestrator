"use strict";
// One live snapshot drives this board. The database's broad `running` status
// does not say whether a node is executing, queued, usage paused or stranded.
let WORK_STATUS = null;
const WORK_ACTIVITY = {
  working: "● working", usage_wait: "◷ usage paused", queued: "◷ queued",
  stalled: "! stalled", waiting: "○ waiting", blocked: "! blocked",
  done: "✓ done", unknown: "? unknown"
};
const WORK_STAGE = {
  alloc: "worktree", implement: "implement", gate: "verify gate",
  review: "cross review", publish: "publish PR", pr_fanout: "assign PR review",
  pr_reviewer: "PR reviewer", pr_join: "collect PR verdicts",
  pr_review: "PR review", pr_merge: "merge PR", escalate: "escalate",
  chain_wait: "project chain", dependency_wait: "dependencies",
  skip: "skip", fail: "failure"
};
function workStage(stage) { return WORK_STAGE[stage] || stage || "not started"; }
function workActivity(task) {
  const a = task.activity || "unknown";
  return WORK_ACTIVITY[a] || WORK_ACTIVITY.unknown;
}
function workTaskReason(t) {
  let reason = t.reason || "";
  if (!reason && (t.blocked_by || []).length) reason = "Waiting for " + t.blocked_by.join(", ");
  if (!reason && t.activity === "working") reason = "Agent or pipeline node is active";
  if (!reason && t.activity === "done") reason = t.status || "finished";
  if (!reason && t.activity === "waiting") reason = "Waiting for its turn";
  return reason || "No recent activity recorded";
}
function workCount(projects) {
  const counts = {working:0, waiting:0, attention:0, done:0};
  for (const p of projects) for (const t of (p.tasks || [])) {
    if (["stalled", "blocked"].includes(t.activity) || ["failed", "conflict"].includes(t.status)) counts.attention++;
    else if (t.activity === "working") counts.working++;
    else if (t.activity === "done" || ["merged", "skipped"].includes(t.status)) counts.done++;
    else counts.waiting++;
  }
  return counts;
}
function workMatch(t, filter) {
  if (filter === "attention") return ["stalled", "blocked"].includes(t.activity) || ["failed", "conflict"].includes(t.status);
  if (filter === "active") return t.activity !== "done" && !["merged", "skipped"].includes(t.status);
  return true;
}
function workTaskRow(t, file) {
  const activity = t.activity || "unknown";
  const reason = workTaskReason(t);
  const detail = [workActivity(t), workStage(t.stage), reason].join(" · ");
  const idle = t.idle_s != null && ["working", "stalled"].includes(activity);
  const age = idle
    ? ` · no output <span data-since="${sinceStamp(t.idle_s)}">…</span>` : "";
  const agents = (t.agents || []).map(a => `${a.role || "agent"}: ${short(a.model || "?")}${a.activity ? " · " + a.activity : ""}`);
  const agentLine = agents.length ? agents.join("; ") : (t.role ? `${t.role}${t.model ? " · " + short(t.model) : ""}` : "");
  return `<button class="work-task ${attr(activity)}" type="button" data-work-file="${attr(file)}" data-work-task="${attr(t.id)}" aria-label="${attr(`${t.id}: ${detail}${idle ? " · no output" : ""}. Open project detail`)}">
    <span><b>${esc(t.id)}</b><span class="title">${esc(t.title || "")}</span></span>
    <span>${esc(workActivity(t))}</span>
    <span>${esc(workStage(t.stage))}${agentLine ? `<span class="title">${esc(agentLine)}</span>` : ""}</span>
    <span class="reason">${esc(reason)}${age}</span>
  </button>`;
}
function workProject(p, filter) {
  const tasks = p.tasks || [];
  const shown = tasks.filter(t => workMatch(t, filter));
  if (!shown.length) return "";
  const hasAttention = tasks.some(t => workMatch(t, "attention"));
  const paused = tasks.some(t => t.activity === "usage_wait" || t.activity === "queued");
  const klass = hasAttention ? "attention" : paused ? "paused" : "";
  const deps = (p.chain && p.chain.deps) || [];
  const chain = deps.length ? `<div class="work-chain">⛓ Starts after ${deps.map(d =>
    `<button type="button" data-work-project="${attr(d.file)}" title="Open upstream project">${esc(d.title || d.file)} (${esc(d.state || "waiting")}${d.n_tasks != null ? `, ${esc(d.merged || 0)}/${esc(d.n_tasks)} merged` : ""})</button>`).join(" + ")}</div>` : "";
  const graphNodes = ((p.dag || {}).nodes || []).map(n => {
    const t = tasks.find(x => x.id === n.id) || {};
    return {...n, title: t.title || n.title, status: t.status || n.status,
      live: t.activity === "working", stage: t.stage, activity: t.activity, reason: t.reason};
  });
  const graph = graphNodes.length ? taskDag({nodes:graphNodes, edges:((p.dag || {}).edges || [])}, {size:"full", file:p.file}) : "";
  return `<section class="work-project ${klass}" data-work-section="${attr(p.file)}" aria-label="${attr(p.title || p.file)} task graph">
    <div class="work-head"><button class="act" type="button" data-work-project="${attr(p.file)}" aria-label="Open project ${attr(p.title || p.file)}"><b>${esc(p.title || p.file)}</b></button>
      <span class="hint">${esc(shown.length)} of ${esc(tasks.length)} tasks · ${esc(p.phase || "unknown")}</span></div>
    ${chain}
    <div class="work-dag" aria-label="Dependency graph for ${attr(p.title || p.file)}">${graph}</div>
    <div class="work-tasks">${shown.map(t => workTaskRow(t, p.file)).join("")}</div>
  </section>`;
}
function renderWorkStatus(data) {
  const projects = (data && data.projects) || [];
  const filter = $("#work-filter").value || "all";
  const c = workCount(projects);
  $("#work-totals").innerHTML = `<span class="chip running">● ${c.working} working</span> <span class="chip chainwait">◷ ${c.waiting} waiting</span> <span class="chip failed">! ${c.attention} needs attention</span> <span class="chip merged">✓ ${c.done} done</span>`;
  const agents = projects.reduce((n, p) => n + (p.tasks || []).reduce((m, t) => m + (t.agents || []).length, 0), 0);
  $("#work-meta").textContent = `${projects.length} projects · ${agents} active agents · refreshed ${data && data.now ? new Date(data.now * 1000).toLocaleTimeString() : "now"}`;
  const priority = p => (p.tasks || []).some(t => workMatch(t, "attention")) ? 0
    : (p.tasks || []).some(t => t.activity === "working") ? 1
    : (p.tasks || []).some(t => workMatch(t, "active")) ? 2 : 3;
  const html = [...projects].sort((a,b) => priority(a)-priority(b)).map(p => workProject(p, filter)).filter(Boolean);
  const map = $("#work-map");
  const next = html.join("") || `<div class="work-empty">${projects.length ? "No tasks match this filter." : "No projects are planned yet."}</div>`;
  if (map._paint !== next) {
    // Polling every five seconds must not throw away a keyboard user's focus
    // or reset a wide DAG while someone is panning it. The clock rewrites
    // [data-since] text, so compare the template, not the live innerHTML.
    const active = typeof document !== "undefined" ? document.activeElement : null;
    const focused = active && map.contains && map.contains(active) ? {
      file: active.dataset.workFile || active.dataset.workProject || active.dataset.f || active.dataset.file,
      task: active.dataset.workTask || active.dataset.t || ""
    } : null;
    const scroll = map.querySelectorAll ? [...map.querySelectorAll("[data-work-section]")].map(section =>
      [section.dataset.workSection, (section.querySelector(".work-dag") || {}).scrollLeft || 0]) : [];
    map._paint = next;
    map.innerHTML = next;
    if (map.querySelectorAll) {
      for (const section of map.querySelectorAll("[data-work-section]")) {
        const prior = scroll.find(([file]) => file === section.dataset.workSection);
        const dag = section.querySelector(".work-dag");
        if (prior && dag) dag.scrollLeft = prior[1];
      }
      if (focused) {
        const target = [...map.querySelectorAll("[data-work-task], [data-work-project], g.node")].find(el =>
          (el.dataset.workFile || el.dataset.workProject || el.dataset.f || el.dataset.file) === focused.file &&
          (el.dataset.workTask || el.dataset.t || "") === focused.task);
        if (target && target.focus) target.focus({preventScroll:true});
      }
    }
  }
}
async function openWorkProject(file) {
  if (!file) return;
  // The detail view is inside the Projects tab and respects the global list
  // filters. Clear those filters so a clicked task can always be found.
  FILT = {r:"", s:"", m:"", q:""}; PHASE_FILT = "";
  SHOW_ARCHIVED = !!PROJECTS.find(p => p.file === file && p.archived);
  if (typeof showTab === "function") showTab("projects", true);
  await openDetail(file);
  const panel = $("#view-detail");
  if (panel && panel.scrollIntoView) panel.scrollIntoView({block:"start", behavior:"smooth"});
}
$("#work-filter").onchange = () => renderWorkStatus(WORK_STATUS);
function activateWorkTarget(ev) {
  const el = ev.target.closest && ev.target.closest("[data-work-task], [data-work-project], g.node");
  if (!el) return;
  const file = el.dataset.workFile || el.dataset.workProject || el.dataset.f || el.dataset.file;
  const task = el.dataset.workTask || el.dataset.t;
  if (!file) return;
  openWorkProject(file).then(() => { if (task) openTaskTranscript(file, task); });
}
$("#work-map").onclick = activateWorkTarget;
$("#work-map").onkeydown = ev => {
  if ((ev.key === "Enter" || ev.key === " ") && ev.target.matches && ev.target.matches("g.node")) {
    ev.preventDefault(); activateWorkTarget(ev);
  }
};
async function pollWorkStatus() {
  try {
    const rev = await jgetRev("/api/work-status", "work");
    if (rev.unchanged) { markFail("work-status", false); return; }
    const data = rev.data;
    if (!data || data.error) {
      forgetEtag("work");
      throw new Error((data && data.error) || "empty work status");
    }
    WORK_STATUS = data;
    // This snapshot already contains the full /api/projects payload. Reuse it
    // for the Projects tab instead of making a second expensive scan each poll.
    PROJECTS = data.projects || [];
    if (typeof renderProjects === "function") renderProjects();
    renderWorkStatus(data);
    markFail("work-status", false);
  } catch (e) {
    markFail("work-status", true);
    if (typeof pollProjects === "function") pollProjects();
    if (!WORK_STATUS) $("#work-map").innerHTML = '<div class="work-empty">Live task map unavailable; retrying…</div>';
  }
}
