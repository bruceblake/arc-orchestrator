"use strict";
// ---- project detail view ----
// The detail panel renders inline under its project row (the list stays
// visible); EXPANDED (URL hash x=) tracks which row owns it.
async function openDetail(file) {
  EXPANDED = file; saveHash();
  renderProjects();
  if (!$("#projects").querySelector(`.pwrap[data-file="${CSS.escape(file)}"]`)) return;
  CUR = file;
  await refreshDetail();
  if (detailTimer) clearInterval(detailTimer);
  detailTimer = setInterval(refreshDetail, 5000);
}
function closeDetail() {
  if (detailTimer) { clearInterval(detailTimer); detailTimer = null; }
  EXPANDED = ""; saveHash();
  CUR = null; CUR_DATA = null;
  renderProjects();
  pollProjects(); pollHealth();
}
$("#btn-back").onclick = closeDetail;

async function refreshDetail() {
  if (!CUR) return;
  let d;
  try { d = await jget("/api/project?file=" + encodeURIComponent(CUR)); markFail("detail", false); }
  catch (e) { markFail("detail", true); return; }
  if (d.error) { $("#d-title").textContent = "error: " + d.error; return; }
  CUR_DATA = d;
  $("#clock").textContent = new Date().toLocaleTimeString();
  $("#d-title").textContent = d.title;
  const running = (d.rows || []).some(r => r.status === "running");
  CUR_PID = d.run_pid || null;
  $("#d-live").innerHTML = running ? '<span class="live">LIVE</span>' : "";
  $("#btn-stop").style.display = CUR_PID ? "" : "none";
  $("#btn-stop").title = CUR_PID ? `stop run process ${CUR_PID}` : "";
  $("#btn-run").disabled = !!CUR_PID;
  $("#btn-run").textContent = CUR_PID ? "● running" : "▶ Run";
  $("#btn-run").title = CUR_PID ? `run process ${CUR_PID} is live` : "start a real run";
  // Tokens come from the /api/projects snapshot (PROJECTS), not this
  // response: /api/project has no tokens field, and the card no longer
  // shows them — this line is where they live now.
  const snap = (PROJECTS || []).find(p => p.file === CUR);
  $("#d-meta").innerHTML = `<span>repo <b>${esc(d.repo)}</b></span><span>file <b>${esc(d.file)}</b></span>
    <span><b>${(d.tasks || []).length}</b> tasks</span><span><b>${(d.runs || []).length}</b> harness runs</span>
    ${snap && snap.tokens ? `<span>⛁ <b>${fmtK(snap.tokens)}</b></span>` : ""}
    ${CUR_PID ? `<span>run pid <b>${CUR_PID}</b></span>` : ""}`;
  const g = d.git || {};
  $("#d-git").innerHTML = g.error ? `<span class="hint">${esc(g.error)}</span>` : `<div class="kv">
      <span>branch <b>${esc(g.branch || "—")}</b></span><span>dirty files <b class="${(g.dirty || []).length ? "warn" : ""}">${(g.dirty || []).length}</b></span>
      <span>worktrees <b>${(g.worktrees || []).length}</b></span></div>
    ${g.ready !== undefined ? `<div class="hint" style="margin-top:3px">PRs <b class="${g.ready ? "" : "warn"}">${esc(g.ready ? "on: " + (g.remote || "") : "off: " + (g.reason || "unavailable"))}</b></div>` : ""}
    ${(g.log || []).length ? `<div class="hint" style="margin-top:5px">${g.log.map(l => "&nbsp;&nbsp;" + esc(l)).join("<br>")}</div>` : ""}
    ${(g.worktrees || []).length > 1 ? `<div class="hint" style="margin-top:3px">${g.worktrees.map(esc).join("<br>")}</div>` : ""}`;
  const pm = $("#d-progress");
  if (pm) pm.innerHTML = progressLines(d.task_progress);
  renderChain(d); renderDag(d); renderFeed(d); renderTasks(d);
}

function renderChain(d) {
  const box = $("#d-chain"); if (!box) return;
  const c = d.chain;
  if (!c) { box.innerHTML = ""; box.style.display = "none"; return; }
  box.style.display = "";
  const dep = x => {
    const prog = x.n_tasks != null ? `${x.merged}/${x.n_tasks} merged` : "not planned yet";
    const cls = x.state === "failed" ? "bad" : x.state === "ready" ? "good" : "warn";
    return `<li><a href="#" data-open="${attr(x.file)}"><b>${esc(x.title)}</b></a> <span class="${cls}">${esc(x.state)}</span> <span class="hint">${esc(prog)}${x.failed && x.failed.length ? ` · failed: ${esc(x.failed.join(", "))}` : ""}</span></li>`;
  };
  const g = c.gate;
  const gateLine = !g ? "" :
    g.state === "waiting" ? `<div class="hint">⛓ a run is holding at the chain gate since ${esc(fmtT(new Date(g.ts * 1000).toISOString()))} — it allocates no worktree until every upstream task is merged</div>` :
    g.state === "ready" ? `<div class="hint good">⛓ chain gate opened after ${esc(tick(g.waited_s || 0))}</div>` :
    `<div class="hint bad">⛓ chain blocked: ${esc(g.reason || "an upstream task failed")}</div>`;
  box.innerHTML = `<div class="kv"><span>chain</span></div>
    ${(c.deps || []).length ? `<div class="hint">runs only after ${c.deps.length === 1 ? "this project is" : "these projects are"} fully merged:</div><ul class="chainlist">${c.deps.map(dep).join("")}</ul>` : ""}
    ${(c.blocks || []).length ? `<div class="hint">waiting on this one:</div><ul class="chainlist">${c.blocks.map(b => `<li><a href="#" data-open="${attr(b.file)}">${esc(b.title)}</a></li>`).join("")}</ul>` : ""}
    ${gateLine}`;
  box.onclick = ev => {
    const a = ev.target.closest && ev.target.closest("[data-open]");
    if (!a) return;
    ev.preventDefault(); openDetail(a.dataset.open);
  };
}

function renderDag(d) {
  const tasks = d.tasks || [];
  if (!tasks.length) { $("#dag").innerHTML = '<div class="empty">no tasks in this file</div>'; return; }
  const rowsById = {}; (d.rows || []).forEach(r => rowsById[r.id] = r);
  const nodes = tasks.map(t => {
    const row = rowsById[t.id] || {};
    return {id: t.id, title: t.title || t.id, model: t.model, reviewer: t.reviewer,
            status: row.status || "pending", live: row.status === "running",
            attempts: row.attempts || 0, escalations: row.escalations || 0,
            last_verdict: row.last_verdict || null, verify_cmd: t.verify_cmd || ""};
  });
  const edges = [];
  for (const t of tasks) for (const dep of (t.deps || t.depends || []))
    if (rowsById[dep] !== undefined || tasks.some(x => x.id === dep)) edges.push({src: dep, dst: t.id});
  // The chain gate, drawn where it really sits: in front of every head.
  const c = d.chain;
  if (c && (c.deps || []).length) {
    const ids = new Set(tasks.map(t => t.id));
    const heads = tasks.filter(t => !(t.deps || t.depends || []).some(x => ids.has(x))).map(t => t.id);
    const g = c.gate || {};
    for (const x of c.deps) {
      let status = x.state === "failed" ? "failed" : x.state === "ready" ? "merged" : g.state === "waiting" ? "running" : "pending";
      if (g.state === "blocked" && x.state !== "ready") status = "failed";
      const id = "after:" + x.file;
      nodes.unshift({id, kind: "chain", file: x.file, title: "after " + x.title, status, merged: x.merged, n_tasks: x.n_tasks});
      for (const h of heads) edges.push({src: id, dst: h, kind: "chain"});
    }
  }
  $("#d-dagmeta").textContent = `(left → right = dependency order; colored border = status; hover = title + verify; click = transcript${c && (c.deps || []).length ? "; dashed ⛓ = chain gate, click = that project" : ""})`;
  $("#dag").innerHTML = taskDag({nodes, edges}, {size: "full", file: CUR});
  bindDagClicks("#dag", d.runs || []);
}

function friendly(e) {
  // Every interpolation here is escaped: these fields carry raw harness
  // stderr and task ids straight out of the event log.
  const m = esc(short(e.model)), task = esc(e.task || e.module || "");
  const cut = (v, n) => esc(String(v == null ? "" : v).slice(0, n));
  switch (e.type) {
    case "driver.start": return `${m} <b>${esc(e.role)}</b> started · ${task} (attempt ${esc(e.attempt)})`;
    case "driver.done": return `${m} <b>${esc(e.role)}</b> finished · ${task} in ${esc(e.seconds)}s${e.tokens ? ` · ${esc(e.tokens)} tok` : ""}`;
    case "driver.error": return `<span class="bad">${m} ${e.capacity ? "at capacity" : "error"} · ${task}: ${cut(e.error, 140)}</span>`;
    case "driver.stalled": return `<span class="bad">${m} stalled (no output for ${esc(e.idle_s)}s) · ${task}</span>`;
    case "driver.timeout": return `<span class="bad">${m} timed out · ${task}</span>`;
    case "driver.cancelled": return `<span class="warn">${m} cancelled · ${task}</span>`;
    case "driver.cap_wait": return `<span class="warn">⏳ ${m} waiting for a slot · ${esc(e.in_use)}/${esc(e.cap)} in use</span>`;
    case "worktree.alloc": return `worktree ready · <span class="hint">${esc(e.branch || e.path || "")}</span>`;
    case "worktree.free": return `worktree released · <span class="hint">${esc(e.branch || "")}</span>`;
    case "task.start": return `task started · ${task}`;
    case "task.end": return `task ended · ${task} → ${esc(e.status || "")}`;
    case "task.escalated": return `<span class="warn">⬆ ${task} escalated ${esc(short(e.from_model))} → ${esc(short(e.to_model))}</span>`;
    case "task.conflict": return `<span class="warn">⚠ merge conflict · ${task}${e.reason || e.error ? ": " + cut(e.reason || e.error, 120) : ""}${(e.files || []).length ? ` <span class="hint">(${(e.files || []).map(esc).join(", ").slice(0, 120)})</span>` : ""}</span>`;
    case "task.failed": return `<span class="bad">✗ ${task} failed${e.reason ? ": " + cut(e.reason, 120) : ""}</span>`;
    case "task.merged": return `<span class="good">⬢ ${task} merged</span>`;
    case "task.budget": {
      const a = e.implement_attempts || 0, x = e.escalations || 0, k = e.total_tokens || 0;
      return `<span class="hint">⛁ ${task} cost ${a} attempt${a === 1 ? "" : "s"}, ${x} escalation${x === 1 ? "" : "s"}, ${tick(e.total_driver_seconds)}, ${k >= 1000 ? Math.round(k / 1000) + "k" : k} tok</span>`;
    }
    case "task.gate": return e.passed
      ? `<span class="good">✓ verify gate passed · ${task}</span>`
      : `<span class="bad">✗ verify gate failed · ${task}${e.tail ? `: ${cut(e.tail, 160)}` : ""}</span>`;
    case "task.reviewed": return e.passed
      ? `<span class="good">✓ review passed · ${task} <span class="hint">by ${esc(e.reviewer)}</span></span>`
      : `<span class="warn">↩ review sent it back · ${task}${e.n_issues ? ` (${esc(e.n_issues)} issue${e.n_issues === 1 ? "" : "s"})` : ""} <span class="hint">by ${esc(e.reviewer)}</span></span>`;
    case "task.pr_reviewed": {
      const who = (e.approvals || []).map(short).map(esc).join(" + ");
      return e.approved
        ? `<span class="good">✓ PR #${esc(e.pr)} approved · ${task} <span class="hint">by ${who}</span></span>`
        : `<span class="warn">↩ PR #${esc(e.pr)} sent back · ${task} — ${esc(e.n_issues)} issue${e.n_issues === 1 ? "" : "s"} (round ${esc(e.round)})</span>`;
    }
    case "task.resumed": return `<span class="hint">↻ resumed ${task} <span class="hint">(was ${esc(e.prior_status || "?")})</span></span>`;
    case "task.pr_reattached": return `<span class="hint">↻ reattached to PR #${esc(e.pr)} · ${task}</span>`;
    case "task.resynced": return `<span class="warn">⟳ ${task}: PR #${esc(e.pr)} resynced with ${esc(e.base)} — needs re-review</span>`;
    case "task.branch_reset": return `<span class="bad">⚠ ${task}: reset ${esc(e.branch)} — ${esc(e.commits_discarded)} commit(s) discarded</span>`;
    case "driver.slot_wait": return `<span class="warn">⏳ ${m} queued · ${task} <span class="hint">(${esc(e.scope)} pool full)</span></span>`;
    case "task.pr_opened": return `<span class="good">⇪ PR opened · ${task} ${cut(e.url, 80)}</span>`;
    case "task.pr_skipped": return `<span class="hint">PR skipped · ${task}: ${cut(e.reason, 100)}</span>`;
    case "run.resume": return `↻ resumed: skipped ${(e.skipped_merged || []).length} merged, retried ${(e.retried || []).length}`;
    case "run.interrupted": return `<span class="warn">↯ run interrupted — ${(e.tasks || []).length} task(s) marked failed</span>`;
    case "run.stopped": return `<span class="warn">■ run stopped by operator</span>`;
    case "graph.draining": return `<span class="warn">⧗ draining ${esc(e.in_flight)} in-flight node(s) after a failure</span>`;
    case "node_start": return `<span class="hint">node ${esc(e.node)} · ${task}</span>`;
    case "node_end": return `<span class="hint">node ${esc(e.node)} done · ${task}</span>`;
    default: return `<span class="hint">${esc(e.type)} ${task}</span>`;
  }
}
function renderFeed(d) {
  const evs = (d.events || []).slice(-30).reverse();
  $("#feed").innerHTML = evs.length ? evs.map(e => {
    const t = e.ts ? new Date(e.ts * 1000).toLocaleTimeString() : "";
    return `<div style="padding:3px 0;border-bottom:1px solid var(--border)"><span class="hint">${t}</span> ${friendly(e)}</div>`;
  }).join("") : '<div class="empty">No events recorded for this project yet.</div>';
}

// What a finished task actually produced. A merged task used to show a green
// chip and nothing else — no way to tell whether it wrote the thing you asked
// for. gitstore.publish tags every commit `task(<id>): <title>`, so the answer
// was always one git call away.
async function loadDeliverable(tid, host) {
  host.innerHTML = '<span class="hint">loading…</span>';
  const d = await jget(`/api/task-diff?file=${encodeURIComponent(CUR)}&task=${encodeURIComponent(tid)}`);
  if (d.error) { host.innerHTML = `<span class="bad">${esc(d.error)}</span>`; return; }
  if (!d.found) { host.innerHTML = `<span class="hint">${esc(d.reason)}</span>`; return; }
  host.innerHTML = `<div class="deliv">
    <span class="good">✓ ${esc(d.summary)}</span>
    <span class="hint">${esc(d.sha)} · ${fmtT(d.when)}</span>
    <div>${d.files.map(f => `<span class="tag">${esc(f.path)} <span class="hint">${esc(f.churn)}</span></span>`).join("")}</div>
    <button class="act seg" data-patch="${attr(tid)}">view diff</button>
  </div>`;
  host.querySelectorAll("[data-patch]").forEach(b => b.onclick = async ev => {
    ev.stopPropagation();
    const full = await jget(`/api/task-diff?file=${encodeURIComponent(CUR)}&task=${encodeURIComponent(tid)}&patch=1`);
    $("#drawer").classList.add("open");
    $("#drawer-title").textContent = `${tid} — what changed`;
    $("#drawer-sub").textContent = full.summary || "";
    drawerFile = null;                       // static content, do not poll
    $("#drawer-body").textContent =
      (full.patch || "(empty)") + (full.truncated ? "\n\n… diff truncated" : "");
  });
}

function renderTasks(d) {
  const tb = $("#tasks tbody"); tb.innerHTML = "";
  const rowsById = {}; (d.rows || []).forEach(r => rowsById[r.id] = r);
  const runsByTask = {}; (d.runs || []).forEach(r => (runsByTask[r.task_id] = runsByTask[r.task_id] || []).push(r));
  for (const t of (d.tasks || [])) {
    const row = rowsById[t.id] || {};
    const st = row.status || "pending";
    const runs = runsByTask[t.id] || [];
    const secs = runs.length && runs[0].seconds ? tick(runs[0].seconds) : (row.finished_at ? "" : "—");
    const verdict = runs.length ? (runs[0].verdict || "") : "";
    const loops = (row.attempts || 0) > 1 || (row.escalations || 0) > 0
      ? ` <span class="tag" title="fix-loop attempts / escalations">x${row.attempts}${row.escalations ? " ⬆" + row.escalations : ""}</span>` : "";
    const retryBtn = (st === "failed" || st === "conflict")
      ? ` <button class="act seg" data-retry="${attr(t.id)}" title="re-run this task">retry</button>` : "";
    const laneHost = (d.task_progress || {})[t.id];
    const laneRow = pipelineLane({status: st, task_progress: laneHost});
    const tr = document.createElement("tr"); tr.className = "clickable";
    tr.setAttribute("role", "button"); tr.tabIndex = 0;
    tr.setAttribute("aria-label", `${t.id}: toggle run history`);
    tr.innerHTML = `<td>${esc(t.id)}</td><td class="hint">${esc(t.title || "")}</td><td>${esc(short(t.model))}</td>
      <td>${esc(revShort(t.reviewer))}</td><td>${chip(st, "")}${loops}${retryBtn}</td><td>${runs.length || "—"}</td>
      <td>${secs || "—"}</td><td class="${verdict.includes("pass") ? "good" : verdict ? "bad" : ""}">${esc((verdict || "—").slice(0, 40))}</td>`;
    const lr = document.createElement("tr");
    lr.innerHTML = `<td colspan="8" style="padding-top:0">${laneRow}</td>`;
    const sr = document.createElement("tr"); sr.className = "subrow"; sr.style.display = "none";
    const why = row.error ? `<div class="errbox">${esc(row.error)}</div>` : "";
    sr.innerHTML = `<td colspan="8">${why}${t.verify_cmd ? `<div class="hint" style="margin:4px 0">verify: <b>${esc(t.verify_cmd)}</b></div>` : ""}${runs.length ? runs.map(r =>
      `<div style="padding:3px 0"><span class="tag">${esc(r.role)}</span> <span class="tag">${esc(r.harness)}</span>
        attempt ${r.attempt} · exit ${r.exit_code} · ${r.seconds ? tick(r.seconds) : "?"} · ${esc(r.verdict || "")}
        ${r.transcript ? `<button class="act" data-t="${attr(String(r.transcript).split("/").pop())}" data-m="${attr(short(r.model))}" data-r="${attr(r.role)}" data-task="${attr(t.id)}">transcript</button>` : ""}</div>`).join("")
      : '<span class="hint">no runs yet</span>'} <div class="hint" style="margin-top:4px">prompt: ${esc((t.prompt || "").slice(0, 260))}…</div></td>`;
    tr.onclick = () => sr.style.display = sr.style.display === "none" ? "" : "none";
    tb.appendChild(tr); tb.appendChild(lr); tb.appendChild(sr);
  }
  tb.querySelectorAll("button[data-t]").forEach(b => b.onclick = ev => { ev.stopPropagation();
    openTranscript(b.dataset.t, `${b.dataset.m} ${b.dataset.r}`, b.dataset.task); });
  tb.querySelectorAll("button[data-retry]").forEach(b => b.onclick = async ev => { ev.stopPropagation();
    const {code, body} = await jpost("/api/projects/retry-task", {file: CUR, task: b.dataset.retry});
    if (code !== 200) alert(`retry failed: ${body.error || "unknown error"}`);
    refreshDetail();
  });
}


// ---- run buttons ----
// No confirm()/alert() here. Native dialogs are the one thing on this page
// the browser may silently suppress ("prevent this page from creating
// additional dialogs"), after which a Run click does nothing and says
// nothing. Everything the operator needs to know — are you sure, starting,
// started with which pid, or why not — is rendered inline under the header,
// and "started" is only shown once the server has seen the process survive
// its first moments and the next poll has found it alive.
let RUN_ARMED = null;   // "run" | "dry" while the inline confirm strip is up
function runMsg(cls, html) {
  const el = $("#d-runmsg"); if (!el) return;
  el.className = "runmsg " + (cls || ""); el.innerHTML = html || "";
}
function runLogLink(log) {
  return log ? ` <a href="#" data-runlog="${attr(log)}">view log</a>` : "";
}
function armRun(dry) {
  if (!CUR || CUR_PID) return;
  RUN_ARMED = dry ? "dry" : "run";
  const what = dry ? "a <b>dry run</b> — resolves the DAG, no models, no git changes"
                   : "a <b>real run</b> — spends model tokens and mutates repo branches";
  runMsg("ask", `Start ${what} for <b>${esc(CUR)}</b>? ` +
    `<button class="act primary" id="run-confirm">▶ start</button> ` +
    `<button class="act" id="run-cancel">cancel</button>`);
  const box = $("#d-runmsg");   // the buttons live inside the strip, not the page
  box.querySelector("#run-confirm").onclick = () => doRun(dry);
  box.querySelector("#run-cancel").onclick = () => { RUN_ARMED = null; runMsg("", ""); };
  box.querySelector("#run-confirm").focus();
}
async function doRun(dry) {
  if (!CUR) return;
  RUN_ARMED = null;
  const file = CUR, btn = $("#btn-run"), dryb = $("#btn-dry");
  btn.disabled = true; dryb.disabled = true;
  runMsg("wait", `⏳ starting ${dry ? "dry run" : "run"} of <b>${esc(file)}</b>…`);
  let code, body;
  try { ({code, body} = await jpost("/api/projects/run", {file, dry_run: dry})); }
  catch (e) { code = 0; body = {error: "no answer from the dashboard — is it running?"}; }
  dryb.disabled = false;
  if (code !== 200) {
    const tail = (body.tail || []).length ? `<pre class="runtail">${esc(body.tail.join("\n"))}</pre>` : "";
    runMsg("err", `✖ not started: ${esc(body.error || "unknown error")}${runLogLink(body.log)}${tail}`);
    btn.disabled = !!CUR_PID;
    return;
  }
  if (body.finished) {   // a dry run that already ran to completion
    runMsg("ok", `✔ dry run finished — ${esc(file)} resolves cleanly.${runLogLink(body.log)}`);
    btn.disabled = false;
    return;
  }
  runMsg("ok", `✔ ${dry ? "dry run" : "run"} started · pid <b>${body.pid}</b> · logs/${esc(body.log)}${runLogLink(body.log)} — confirming it is alive…`);
  await refreshDetail();
  // The server saw it survive 1.5 s; now confirm the poll sees it too.
  setTimeout(async () => {
    if (CUR !== file) return;
    await refreshDetail();
    const live = CUR_PID || (CUR_DATA && (CUR_DATA.rows || []).some(r => r.status === "running"));
    if (live) runMsg("ok", `✔ ${dry ? "dry run" : "run"} is live · pid <b>${body.pid}</b> · logs/${esc(body.log)}${runLogLink(body.log)}`);
    else runMsg("err", `✖ the run started (pid ${body.pid}) but is already gone — read the log.${runLogLink(body.log)}`);
  }, 3000);
}
$("#btn-run").onclick = () => armRun(false);
$("#btn-dry").onclick = () => armRun(true);
document.addEventListener("click", ev => {
  const a = ev.target.closest && ev.target.closest("[data-runlog]");
  if (!a) return;
  ev.preventDefault(); showRunLog(a.dataset.runlog);
});
async function showRunLog(fn) {
  const d = await jget(`/api/run-log?file=${encodeURIComponent(fn)}`);
  $("#drawer").classList.add("open");
  $("#drawer-title").textContent = "run log";
  $("#drawer-sub").textContent = d.error ? "" : `logs/${fn} · last ${(d.lines || []).length} lines`;
  drawerFile = null;
  $("#drawer-body").textContent = d.error || (d.lines || []).join("\n") || "(empty)";
}
$("#btn-stop").onclick = async () => {
  if (!CUR) return;
  if (RUN_ARMED !== "stop") {
    RUN_ARMED = "stop";
    runMsg("ask", `Stop the run for <b>${esc(CUR)}</b>? In-flight agents are cancelled, unfinished tasks are marked failed, driver slots are released; merged work is kept. ` +
      `<button class="act danger" id="stop-confirm">■ stop</button> <button class="act" id="stop-cancel">cancel</button>`);
    const box = $("#d-runmsg");
    box.querySelector("#stop-confirm").onclick = () => $("#btn-stop").onclick();
    box.querySelector("#stop-cancel").onclick = () => { RUN_ARMED = null; runMsg("", ""); };
    return;
  }
  RUN_ARMED = null;
  runMsg("wait", "⏳ stopping…");
  const {code, body} = await jpost("/api/projects/stop", {file: CUR});
  if (code === 200) runMsg("ok", `■ stop signal sent to pid ${esc((body.stopped || []).join(", "))} ${esc(body.note || "")}`);
  else runMsg("err", `✖ not stopped: ${esc(body.error || "unknown error")}`);
  refreshDetail(); pollHealth();
};

