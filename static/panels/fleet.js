"use strict";
// ---- fleet health (caps + problems) ----
const PROBLEM_LABEL = {
  "driver.error": "harness error", "driver.stalled": "stalled (no output)",
  "driver.timeout": "timed out", "driver.stale": "phantom agent pruned",
  "driver.cancelled": "cancelled", "driver.cap_wait": "waiting for a slot",
  "inflight.over_cap": "OVER account cap", "task.failed": "task failed",
  "task.conflict": "merge conflict", "graph.draining": "run draining after a failure",
  "run.interrupted": "run interrupted",
};
let _isRestarting = false;

async function restartDashboard(force = false) {
  if (_isRestarting) return;
  _isRestarting = true;
  const btn = $("#btn-restart-dashboard");
  const msg = $("#stale-banner-msg");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Restarting…";
  }
  if (msg) {
    msg.textContent = "⏳ Restarting dashboard… reloading in a moment";
  }
  try {
    const { code, body } = await jpost("/api/restart", { force: !!force });
    if (code === 409) {
      const confirmForce = confirm("An interactive chat/captain turn is in progress. Force restart anyway?");
      if (confirmForce) {
        _isRestarting = false;
        return restartDashboard(true);
      }
      _isRestarting = false;
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Restart dashboard";
      }
      if (msg) {
        msg.textContent = "Restart cancelled: interactive session in progress.";
      }
      return;
    }
    if (code !== 200) {
      if (msg) msg.textContent = `Restart failed: ${body && body.error ? body.error : "unknown error"}`;
      _isRestarting = false;
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Restart dashboard";
      }
      return;
    }
  } catch (err) {
    // Process re-exec may close the connection abruptly; proceed to poll
  }
  let attempts = 0;
  const pollInterval = setInterval(async () => {
    attempts++;
    try {
      const res = await fetch("/api/fleet", { cache: "no-store" });
      if (res.ok) {
        clearInterval(pollInterval);
        window.location.reload();
      }
    } catch (e) {
      // Server is restarting, continue waiting
    }
    if (attempts > 60) {
      clearInterval(pollInterval);
      if (msg) msg.textContent = "Restart took longer than expected. Please refresh manually.";
      _isRestarting = false;
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Restart dashboard";
      }
    }
  }, 800);
}

const restartBtn = $("#btn-restart-dashboard");
if (restartBtn) {
  restartBtn.onclick = () => restartDashboard(false);
}

function renderHealth(h) {
  // The fleet edits this server's own source. A running process keeps serving
  // what it started with, so a merged route can 404 and take the whole page
  // down with it — that is exactly how the console went blank.
  // VPN first: when ARC is unreachable every driver is waiting, so the fleet
  // shows zeros everywhere and looks idle. That is the worst possible way to
  // find out the VPN expired — say it in red at the top.
  const arc = h.arc || {};
  const vpn = $("#vpn-banner");
  if (vpn) {
    const down = arc.reachable === false;
    vpn.style.display = down ? "" : "none";
    vpn.innerHTML = down
      ? `⛔ <b>ARC is unreachable</b> — the fleet is paused, not idle. ${esc(arc.detail || "")}
         <span class="hint">Reconnect the Cisco VPN; drivers resume on their own within ~30s.</span>`
      : "";
  }
  const stale = h.stale_source || [];
  const banner = $("#stale-banner");
  const msg = $("#stale-banner-msg");
  const btn = $("#btn-restart-dashboard");
  if (banner) {
    if (_isRestarting) {
      banner.style.display = "";
    } else if (stale.length) {
      banner.style.display = "";
      if (msg) {
        msg.innerHTML = `⚠ the dashboard is running older code than the repo (${stale.map(esc).join(", ")} changed since it started) — click restart to reload with latest code`;
      }
      if (btn) {
        btn.style.display = "";
        btn.disabled = false;
        btn.textContent = "Restart dashboard";
      }
    } else {
      banner.style.display = "none";
    }
  }
  const models = h.models || [];
  const drivers = models.reduce((n, m) => n + m.drivers, 0);
  const account = models.reduce((n, m) => n + m.account, 0);
  const over = models.filter(m => m.over_cap), at = models.filter(m => m.at_cap && !m.over_cap);
  $("#health-sum").textContent = `${drivers} agent${drivers === 1 ? "" : "s"}`;
  $("#health-pill").className = "pill" + (over.length ? " on" : "");
  const runs = (h.runs || []).length;
  const interactive = account - drivers;
  $("#health-meta").textContent =
    `(${drivers} fleet agent${drivers === 1 ? "" : "s"} · ${runs} run${runs === 1 ? "" : "s"}` +
    `${interactive > 0 ? ` · ${interactive} interactive session${interactive === 1 ? "" : "s"} sharing the account` : ""}` +
    `${over.length ? " · OVER CAP: " + over.map(m => m.pretty).join(", ") : ""}` +
    `${!over.length && at.length ? " · at cap: " + at.map(m => m.pretty).join(", ") : ""})`;
  $("#health-models").innerHTML = models.map(m => {
    const cap = m.driver_cap || 0;
    const pct = cap ? Math.min(100, 100 * m.drivers / cap) : 0;
    const cls = m.over_cap ? "over" : m.at_cap ? "atcap" : m.drivers ? "busy" : "";
    const extra = m.account > m.drivers ? ` +${m.account - m.drivers}` : "";
    const age = m.account && m.oldest_s ? ` · oldest ${tick(m.oldest_s)}` : "";
    const tip = `${attr(m.model)}: ${m.drivers}/${cap} fleet drivers` +
      `, ${m.account}/${m.account_cap || "?"} on the ARC account${extra ? " (the +N are interactive sessions sharing your account)" : ""}${age}`;
    return `<span class="mcap ${cls}" title="${tip}" role="img" aria-label="${tip}">
      ${esc(m.pretty)} <b>${m.drivers}/${cap || "?"}</b>${extra ? `<span class="hint">${extra}</span>` : ""}
      <span class="gauge" aria-hidden="true"><i style="width:${pct}%"></i></span><span aria-hidden="true">${m.over_cap ? " !" : m.at_cap ? " !" : m.drivers ? " …" : " ✓"}</span></span>`;
  }).join("") || '<span class="hint">no models reporting</span>';
  const probs = (h.problems || []);
  $("#btn-problems").textContent = `recent problems (${probs.length})`;
  $("#health-problems").innerHTML = probs.length ? probs.map(e => {
    const what = PROBLEM_LABEL[e.type] || e.type;
    const who = [e.model && short(e.model), e.task].filter(Boolean).map(esc).join(" · ");
    let detail = e.error || e.reason || e.note ||
      (e.in_use != null ? `${e.in_use}/${e.cap} slots in use` : "") ||
      (e.inflight != null ? `${e.inflight}/${e.limit} in flight` : "");
    if (e.wire && e.wire.awaiting_api)
      detail = `API never answered — ${esc(e.wire.model || "request")} outstanding ` +
               `${e.wire.waiting_s}s (${fmtK(e.bytes || 0)}B written first)`;
    else if (e.blocked)
      detail = `blocked in state ${esc(e.state || "?")}, no CPU burn — ${detail}`;
    const cls = e.type === "driver.cap_wait" ? "warn" : "bad";
    return `<div><span class="hint">${AGO(e.ts)} ago</span>
      <span class="${cls}">${esc(what)}</span> ${who}
      <span class="hint">${esc(String(detail).slice(0, 160))}</span></div>`;
  }).join("") : '<div class="hint">nothing to report</div>';
}
async function pollHealth() {
  try { renderHealth(await jget("/api/health")); markFail("health", false); }
  catch (e) { markFail("health", true); }
}
$("#btn-problems").onclick = () => { const el = $("#health-problems");
  el.style.display = el.style.display === "none" ? "" : "none"; };

// ---- header totals from /api/fleet ----
async function pollFleet() {
  try {
    FLEET = await jget("/api/fleet");
    const t = FLEET.totals || {};
    $("#tot-tokens").innerHTML = `⛁ <b>${fmtK(t.tokens)}</b>`;
    $("#tot-tokens").title = `${fmtK(t.requests)} requests · ${fmtK(t.ok)} ok · ${fmtK(t.errors)} errors`;
    $("#tot-split").innerHTML = `↑<b>${fmtK(t.prompt_tokens)}</b> ↓<b>${fmtK(t.completion_tokens)}</b>`;
    markFail("fleet", false);
  } catch (e) { markFail("fleet", true); }
}

// ---- defect triage: distinct bugs, not a wall of occurrences ----
// A flat error log answers "what happened", which the feed already does. This
// answers "what should I fix" — every occurrence of one defect collapsed into
// one row with its count, its span, and the traceback that used to be thrown
// away entirely (every catch site kept only str(exc)[:300]).
let ERR_RANGE = "24h";

function errRow(g) {
  const when = g.active ? '<span class="bad">still firing</span>'
                        : `<span class="hint">last ${tick(g.age_s)} ago</span>`;
  const tasks = (g.tasks || []).length
    ? `<span class="hint">${(g.tasks || []).slice(0, 4).map(esc).join(", ")}</span>` : "";
  return `<div class="errrow${g.active ? " hot" : ""}">
    <div class="errhead">
      <b>${esc(g.kind)}</b> <span class="n">x${esc(g.count)}</span>
      <code>${esc(g.where || "unknown")}</code> ${when} ${tasks}
    </div>
    <div class="errmsg">${esc((g.message || "").slice(0, 300))}</div>
    ${g.traceback ? `<details class="errtb"><summary>traceback · ${esc(g.fingerprint)}</summary><pre>${esc(g.traceback)}</pre></details>` : ""}
  </div>`;
}

function renderErrors(d) {
  const panel = $("#errs-panel");
  const groups = (d && d.groups) || [];
  // Hidden entirely when there is nothing wrong. An empty panel that is always
  // present trains the eye to skip it, and then it is skipped when it matters.
  panel.style.display = groups.length ? "" : "none";
  if (!groups.length) return;
  const hot = groups.filter(g => g.active).length;
  $("#errs-meta").innerHTML =
    `${groups.length} distinct · ${d.total} occurrence${d.total === 1 ? "" : "s"}`
    + (hot ? ` · <span class="bad">${hot} still firing</span>` : "");
  $("#errs").innerHTML = groups.map(errRow).join("");
}

async function pollErrors() {
  try { renderErrors(await jget("/api/errors?range=" + ERR_RANGE)); markFail("errors", false); }
  catch (e) { markFail("errors", true); }
}

for (const b of document.querySelectorAll("[data-er]")) {
  b.onclick = () => {
    ERR_RANGE = b.dataset.er;
    for (const o of document.querySelectorAll("[data-er]")) o.classList.toggle("on", o === b);
    pollErrors();
  };
}


// ---- today's merged/failed via the incremental /api/events cursor ----
const dayStart = () => { const d = new Date(); d.setHours(0, 0, 0, 0); return d.getTime() / 1000; };
let EV_NEXT = 0, EV_DAY = dayStart(), TODAY = {merged: 0, failed: 0, conflict: 0};
function countEv(e) {
  if (!e.ts || e.ts < EV_DAY) return;
  if (e.type === "task.merged") TODAY.merged++;
  else if (e.type === "task.failed") TODAY.failed++;
  else if (e.type === "task.conflict") TODAY.conflict++;
}
function renderToday() {
  $("#tot-today").innerHTML = `✓<b>${TODAY.merged}</b> ✗<b>${TODAY.failed}</b>${TODAY.conflict ? ` ⚠<b>${TODAY.conflict}</b>` : ""}`;
  $("#tot-today").title = `today: ${TODAY.merged} merged · ${TODAY.failed} failed · ${TODAY.conflict} conflicts`;
}
async function initEvents() {
  try {
    let after = 0, chunks = 0;
    while (chunks++ < 60) {
      // Seed from today rather than line 0: the counters below only care
      // about today, and the log holds every event the fleet has ever emitted.
      const d = await jget("/api/events?after=" + after
                           + (after === 0 ? "&since=" + Math.floor(EV_DAY) : ""));
      const evs = d.events || [];
      evs.forEach(countEv);
      if (!evs.length || d.next === after) { after = d.next; break; }
      after = d.next;
    }
    EV_NEXT = after;
    markFail("events", false);
  } catch (e) { markFail("events", true); }
  renderToday();
}
// ---- fleet activity: the lifecycle moments, newest first ----
// The event log records everything, but "what is the fleet doing" was only
// answerable by grepping logs/events.jsonl. This reads the server's curated
// window (/api/activity) — deliberately NOT the raw log, which is mostly
// driver.heartbeat and would swamp the panel.
//
// One badge family per colour, and the colour is the ONLY thing that must not
// be learned twice: review / merge / fail / escalate / degraded / stall.
const ACTIVITY_KIND = {
  "task.reviewed": {k: "review",   w: "review"},
  "task.pr_reviewed": {k: "review", w: "PR review"},
  "task.pr_opened": {k: "review",  w: "PR opened"},
  "task.merged": {k: "merge",      w: "merged"},
  "task.failed": {k: "fail",       w: "failed"},
  "task.escalated": {k: "escalate", w: "escalated"},
  "task.review_degraded": {k: "degraded", w: "degraded"},
  "task.resynced": {k: "degraded", w: "resynced"},
  "driver.stalled": {k: "stall",   w: "stalled"},
  "chain.wait": {k: "stall",       w: "chain wait"},
  "chain.ready": {k: "merge",      w: "chain ready"},
  "chain.blocked": {k: "fail",     w: "chain blocked"},
};
// Events whose DETAIL line is the alarming part, so it is tinted red. Keyed by
// event type and not by badge kind: chain.wait shares the "stall" colour —
// waiting on an upstream project is routine and should be visible, not an
// alarm — while a driver that stopped producing output is the anomaly worth
// flagging.
const ACTIVITY_BAD = new Set(["task.failed", "chain.blocked", "driver.stalled"]);
// Badge kind -> colour. One entry per badge family, keyed by the KIND (not
// the event type) so every event in a family is the same colour and the six
// families are distinguishable at a glance.
const ACTIVITY_COLOR = {
  review: "#58a6ff", merge: "#3fb950", fail: "#f85149",
  escalate: "#bc8cff", degraded: "#d29922", stall: "#f0883e",
};
const activityColor = k => ACTIVITY_COLOR[k] || "#8b949e";
let ACTIVITY_LIMIT = 50;

// One human sentence per event: what happened, to whom, and — for failures —
// WHY. The detail line is where the enriched fields the server now attaches
// (n_issues, round, reason, detail, models) actually reach the operator.
function activityWhat(e) {
  const kind = ACTIVITY_KIND[e.type] || {w: e.type};
  const bits = [];
  switch (e.type) {
    case "task.reviewed":
      // `model` is a resolved model name; `reviewer` is the FAMILY TOKEN the
      // taskfile carries ("glm"), which short() would pass through verbatim.
      // revShort() maps both, so an older event reads "GLM 5.3" not "glm".
      bits.push(`${e.passed ? "passed" : "rejected"} by ` +
                revShort(e.model || e.reviewer));
      if (e.round) bits.push(`round ${e.round}`);
      if (e.n_issues) bits.push(`${e.n_issues} issue${e.n_issues === 1 ? "" : "s"}`);
      break;
    case "task.pr_reviewed": {
      // A historical event predates the `models` field (the pool used to
      // record only family tokens), so fall back to `reviewers` rather than
      // rendering an empty "by " — real logs are full of these.
      const who = (e.models || []).map(short).join(", ")
        || (e.reviewers || []).map(revShort).join(", ") || "no reviewer";
      // `approved` is false for BOTH a rejection and an inconclusive round —
      // a reviewer crashed, nobody read the diff, the round is retried. Folding
      // the two together reports a rejection of code nobody reviewed.
      if (e.inconclusive) {
        bits.push(`no verdict (reviewer crashed), retrying`);
      } else {
        bits.push(`${e.approved ? "approved" : "changes requested"} by ${who}`);
      }
      bits.push(`PR #${e.pr}`);
      if (e.round) bits.push(`round ${e.round}`);
      if (e.n_issues) bits.push(`${e.n_issues} issue${e.n_issues === 1 ? "" : "s"}`);
      break;
    }
    case "task.pr_opened":
      bits.push(`PR #${e.number}`); break;
    case "task.merged":
      bits.push(e.pr ? `PR #${e.pr}` : "no PR"); break;
    case "task.failed":
      bits.push(`on ${short(e.model)}`);
      if (e.escalations) bits.push(`${e.escalations} escalation${e.escalations === 1 ? "" : "s"}`);
      break;
    case "task.escalated":
      bits.push(`${short(e.from_model)} → ${short(e.to_model)}`); break;
    case "task.review_degraded":
      bits.push(`${e.got} of ${e.wanted} reviewers`); break;
    case "task.resynced":
      // Only the pr_merge resync path carries a PR number; the publish-path
      // resync (the common one — 51 of 51 in the live log) has base/note only,
      // so a bare `PR #${e.pr}` would print "PR #undefined".
      bits.push(e.pr ? `PR #${e.pr} merged with ${e.base}`
                     : `merged ${e.base} into the branch`); break;
    case "driver.stalled":
      bits.push(short(e.model));
      if (e.idle_s) bits.push(`no output for ${tick(e.idle_s)}`);
      break;
    case "chain.wait": case "chain.ready": case "chain.blocked":
      bits.push((e.taskfile || "").split("/").pop());
      if (e.waited_s) bits.push(`waited ${tick(e.waited_s)}`);
      break;
  }
  return `${kind.w}${bits.length ? " · " + bits.join(" · ") : ""}`;
}

function activityDetail(e) {
  // WHY the task died and WHAT was wrong with it are two different facts and
  // a failed task carries both: `reason` is the verdict ("exhausted
  // escalation: 2 escalation(s), ended on GLM-5.3"), `detail` is the evidence
  // (the gate tail naming the failing test, or the blocking review issues).
  // Showing only the second throws away the sentence that makes the row
  // readable; showing only the first hides what to fix. Failures show both.
  const bits = [];
  if (e.reason) bits.push(String(e.reason));
  if (e.detail && e.detail !== e.reason) bits.push(String(e.detail));
  if (!bits.length && e.note) bits.push(String(e.note));
  if (!bits.length && e.last_activity) bits.push(String(e.last_activity).slice(-200));
  return bits.length ? esc(bits.join("\n").slice(0, 400)) : "";
}

// Which project a row opens, or "" when it opens nothing.
//
// Driver events carry the HARNESS's task id, not a project id: an attempt is
// `<tid>-xN` and a PR reviewer is `<tid>-prN` (code_tasks.py hands both to
// driver.run), while the page matches on base ids. The server already strips
// those before it ships `file`; the PROJECTS fallback below has to strip them
// too, or a stalled harness resolves on one path and not the other.
const activityBase = t => String(t || "").replace(/-x\d+$/, "").replace(/-pr\d+$/, "");
function activityTarget(e) {
  if (e.file) return e.file;
  if (!e.task) return "";
  // Exact id first, so a task whose real id ends in a driver-ish suffix still
  // opens its own project; the base id is the fallback that makes a stalled
  // harness resolvable at all.
  const owner = findProject(e.task) || findProject(activityBase(e.task));
  return (owner && owner.file) || "";
}
function findProject(id) {
  return (PROJECTS || []).find(p => (p.tasks || []).some(t => t.id === id));
}
function activityRow(e) {
  const kind = ACTIVITY_KIND[e.type] || {k: "", w: e.type};
  const detail = activityDetail(e);
  const hot = ACTIVITY_BAD.has(e.type) ? " bad" : "";
  const color = activityColor(kind.k);
  // A click is a shortcut into the project's detail panel, and the row is
  // marked interactive ONLY when it has a project to open: a task id the page
  // cannot resolve to a name is not a clickable row. Rendering role="button"
  // off `e.task` alone promised an action that no-oped (a stalled harness is
  // exactly such a row), so the affordance and the click decide together.
  const target = activityTarget(e);
  return `<div class="arow${target ? " has-task" : ""}${hot}"` +
    (target ? ` role="button" tabindex="0" data-task="${attr(e.task)}"` +
      ` data-file="${attr(target)}"` +
      ` aria-label="open task ${attr(e.task)} in its project"` : "") + `>
    <span class="when">${esc(AGO(e.ts))} ago</span>
    <span class="abadge" style="color:${color}" title="${attr(e.type)}">${esc(kind.w)}</span>
    ${e.task ? `<span class="atask">${esc(e.task)}</span>` : ""}
    <span class="awhat">${esc(activityWhat(e))}</span>
    ${detail ? `<span class="adetail">${detail}</span>` : ""}
  </div>`;
}

function renderActivity(d) {
  const evs = (d && d.events) || [];
  // The empty state is #activity-empty's sentence, not an inline copy of it:
  // rendering both printed the same line twice.
  $("#activity").innerHTML = evs.length ? evs.map(activityRow).join("") : "";
  $("#activity-empty").style.display = evs.length ? "none" : "";
  $("#activity-meta").innerHTML = evs.length
    ? `last ${evs.length}${d && d.total ? ` of ${fmtK(d.total)} log line${d.total === 1 ? "" : "s"}` : ""}`
    : "nothing yet";
}

async function pollActivity() {
  try {
    renderActivity(await jget("/api/activity?limit=" + ACTIVITY_LIMIT));
    markFail("activity", false);
  } catch (e) { markFail("activity", true); }
}

for (const b of document.querySelectorAll("[data-ac]")) {
  b.onclick = () => {
    ACTIVITY_LIMIT = +b.dataset.ac;
    for (const o of document.querySelectorAll("[data-ac]")) o.classList.toggle("on", o === b);
    pollActivity();
  };
}

// Click (or Enter/Space on) an event with a task id → open its project. The
// task id alone does not name a taskfile: the server resolves it from the
// code_tasks rows and ships it as `file` on the event, so a click works for
// history the page's current filter (or an archive) has hidden.
function activityOpen(taskId, file) {
  const target = activityTarget({task: taskId, file: file});
  if (target) openDetail(target);
}
function activityKey(ev) {
  const row = ev.target && ev.target.closest && ev.target.closest("[data-task]");
  if (!row) return;
  activityOpen(row.dataset.task, row.dataset.file);
}
const activityEl = $("#activity");
if (activityEl) {
  activityEl.onclick = activityKey;
  activityEl.onkeydown = ev => {
    if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); activityKey(ev); }
  };
}

async function pollEvents() {
  try {
    const today = dayStart();
    if (today > EV_DAY) { EV_DAY = today; TODAY = {merged: 0, failed: 0, conflict: 0}; EV_NEXT = 0; }
    const d = await jget("/api/events?after=" + EV_NEXT);
    if (d.reset) { TODAY = {merged: 0, failed: 0, conflict: 0}; EV_NEXT = 0; }
    (d.events || []).forEach(countEv);
    EV_NEXT = d.next;
    markFail("events", false);
    renderToday();
  } catch (e) { markFail("events", true); }
}

