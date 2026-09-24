"use strict";
// ---- task timeline drawer ------------------------------------------------
// One place to see everything that happened to a task. Debugging a task used
// to mean grepping events.jsonl, the harness transcripts, error_events, the
// gate output and logs/evidence by hand; the server gathers all four behind
// GET /api/tasks/<id>/timeline and this renders them in time order.
//
// The server BOUNDS its scan (TIMELINE_SCAN_LINES) and reports `truncated`
// when it hit the bound. The page must say so: a cut-off history shown as if
// it were complete is the same lie as a silently missing entry.

let timelineFile = null;        // the taskfile the open timeline belongs to
let timelineTask = null;        // and the task id, for polling

// Entry kinds the eye must not miss, mapped to their row class. An error is
// the reason the drawer was opened at all, and evidence is the only entry
// carrying pixels.
const TL_HIGHLIGHT = { error: "error", evidence: "evidence" };

function tlWhen(ts) {
  if (ts == null) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString() + " " + String(d.getDate()).padStart(2, "0") +
    "/" + String(d.getMonth() + 1).padStart(2, "0");
}

// A one-line gist per entry type, so the drawer reads as a story and not as a
// wall of keys. Anything not named here still renders — the type is shown and
// the body is picked by the server — but the common ones get their meaning
// spelled out rather than a bare event name.
function tlGist(e) {
  const secs = e.seconds != null ? tick(e.seconds) : "";
  switch (e.type) {
    case "harness.run":
      return `${short(e.model)} ${e.role || ""} · attempt ${e.attempt}` +
        `${e.exit_code ? ` · exit ${e.exit_code}` : ""}${secs ? ` · ${secs}` : ""}`;
    case "task.gate":
      return e.passed === false ? "gate FAILED" : "gate passed";
    case "task.reviewed":
      return e.passed === false ? `review rejected${(e.issues || []).length ? ` (${e.issues.length} issues)` : ""}`
        : "review passed";
    case "task.escalated":
      return `${short(e.from_model)} → ${short(e.to_model)}${e.n ? ` (#${e.n})` : ""}`;
    case "task.merged":
      return `merged${e.sha ? ` ${String(e.sha).slice(0, 8)}` : ""}`;
    case "task.failed":
      return "FAILED";
    case "task.skipped":
      return `skipped${e.because ? ` (${e.because})` : ""}`;
    case "driver.start":
      return `${short(e.model)} started`;
    case "driver.done":
      return `${short(e.model)} done${e.verdict ? ` · ${e.verdict}` : ""}`;
    case "driver.error":
      return `driver error${e.model ? ` · ${short(e.model)}` : ""}`;
    case "driver.cap_wait":
      return `waiting for capacity${e.in_use != null ? ` (${e.in_use}/${e.cap})` : ""}`;
    case "evidence.manifest":
      return `${e.project}/${e.attempt} · ${(e.shots || []).length} shot(s)` +
        `${(e.videos && Object.keys(e.videos).length) ? " + video" : ""}`;
    case "error":
      return `${e.error_kind || "error"}: ${String(e.message || "").slice(0, 160)}`;
    default:
      return e.reason ? String(e.reason).slice(0, 160) : "";
  }
}

function tlBody(e) {
  // `body` is chosen by the server from the event's own fields (tail, output,
  // issues, reason…): the WHY of the entry. A traceback is shown for an error,
  // bounded, because that is what the fingerprint points at.
  let out = "";
  if (e.traceback) out += esc(String(e.traceback).slice(-1500));
  else if (e.body) out += esc(Array.isArray(e.body) ? e.body.join("\n")
    : String(e.body));
  return out;
}

function tlEvidence(e) {
  const shots = (e.shots || []).slice(0, 12);
  let s = "";
  if (shots.length) {
    s += `<div class="tl-shots">` + shots.map(sh => {
      const url = attr(sh.url);
      // A video has no still to show; the link is what a reviewer wants
      // anyway, so it is a labelled link rather than a broken thumbnail.
      if (/\.(mp4|gif)$/i.test(sh.name)) {
        return `<a class="tl-shot" href="${url}" target="_blank" rel="noopener">▶ ${esc(sh.name)}</a>`;
      }
      return `<a class="tl-shot" href="${url}" target="_blank" rel="noopener">` +
        `<img src="${url}" alt="evidence ${attr(sh.name)}" loading="lazy">` +
        `<span>${esc(sh.name)}</span></a>`;
    }).join("") + `</div>`;
  }
  const bits = [];
  if (e.coverage) {
    const bad = Object.entries(e.coverage)
      .filter(([, v]) => (v && v.status && v.status !== "captured"))
      .map(([k, v]) => `${k}:${v.status}${v.reason ? ` (${v.reason})` : ""}`);
    bits.push(bad.length ? `<div class="warn">coverage gaps: ${esc(bad.join("; "))}</div>`
      : `<div class="hint">coverage: every kind captured</div>`);
  }
  if (e.no_visible_change)
    bits.push(`<div class="warn">⚠ no visible change for a gameplay diff — evidence may not have captured it</div>`);
  for (const g of (e.godot_errors || []).slice(0, 5))
    bits.push(`<div class="warn">godot: ${esc(String(g).slice(0, 200))}</div>`);
  for (const w of (e.warnings || []).slice(0, 5))
    bits.push(`<div class="hint">⚠ ${esc(String(w).slice(0, 200))}</div>`);
  return s + bits.join("");
}

function tlEntry(e) {
  const cls = "tl-row" + (TL_HIGHLIGHT[e.kind] ? ` ${TL_HIGHLIGHT[e.kind]}` : "");
  const gist = tlGist(e);
  const body = tlBody(e);
  const extra = e.kind === "evidence" ? tlEvidence(e) : "";
  // The transcript a run row points at opens in this same drawer (the page's
  // existing transcript view), so a run entry is a door rather than a label.
  const open = e.file
    ? `<button class="act seg" data-tl-run="${attr(e.file)}" data-tl-task="${attr(e.task || "")}">transcript</button>`
    : "";
  return `<div class="${cls}">
    <div class="tl-head">
      <span class="hint">${esc(tlWhen(e.ts))}</span>
      <span class="tl-type">${esc(e.type)}</span>
      ${e.fingerprint ? `<span class="tag" title="error fingerprint — Rule 7b">${esc(e.fingerprint)}</span>` : ""}
      <span style="flex:1"></span>${open}
    </div>
    ${gist ? `<div class="tl-gist">${esc(gist)}</div>` : ""}
    ${body ? `<div class="tl-body">${body}</div>` : ""}
    ${extra}
  </div>`;
}

function renderTimeline(d) {
  const body = $("#drawer-body");
  const entries = (d && d.entries) || [];
  const c = (d && d.counts) || {};
  $("#drawer-sub").textContent =
    `${entries.length} entr${entries.length === 1 ? "y" : "ies"}` +
    ` · ${c.events || 0} events · ${c.runs || 0} runs · ${c.errors || 0} errors` +
    ` · ${c.evidence || 0} evidence`;
  if (!entries.length) {
    body.innerHTML = `<div class="empty">Nothing recorded for ${esc(d.id || "")} yet — ` +
      `no events, harness runs, errors or evidence.</div>`;
    return;
  }
  // `truncated` is the server telling the truth about its own bound: say so
  // rather than presenting a partial history as the whole one.
  const cut = d.truncated
    ? `<div class="warn tl-cut">⚠ only the newest ${d.scanned} log line(s) were scanned ` +
      `(limit ${d.scan_limit}) — older entries are not shown.</div>` : "";
  body.innerHTML = cut + entries.map(tlEntry).join("");
  body.querySelectorAll("[data-tl-run]").forEach(b => b.onclick = ev => {
    ev.stopPropagation();
    openTranscript(b.dataset.tlRun, b.dataset.tlRun, b.dataset.tlTask || "");
  });
}

async function pollTimeline() {
  if (!timelineTask) return;
  const body = $("#drawer-body");
  const atBottom = body.scrollTop + body.clientHeight >= body.scrollHeight - 40;
  const url = `/api/tasks/${encodeURIComponent(timelineTask)}/timeline` +
    (timelineFile ? `?taskfile=${encodeURIComponent(timelineFile)}` : "");
  let d;
  try { d = await jget(url); }
  catch (e) { return; }            // keep the last good view; next tick retries
  if (d.error) { body.textContent = d.error; return; }
  renderTimeline(d);
  if (atBottom) body.scrollTop = body.scrollHeight;
}

// A task node in a DAG opens its timeline. The transcript drawer stays
// reachable: every run entry in the timeline has its own transcript button.
function openTaskTimeline(file, taskId) {
  $("#drawer").classList.add("open");
  $("#drawer-title").textContent = `${taskId} — timeline`;
  $("#drawer-sub").textContent = "loading…";
  $("#drawer-body").innerHTML = '<div class="hint">loading…</div>';
  $("#drawer-mode").style.display = "none";
  drawerFile = null;               // not a transcript: do not poll that view
  timelineFile = file || null;
  timelineTask = taskId;
  pollTimeline();
}

function closeTimeline() { timelineTask = null; timelineFile = null; }

setInterval(() => {
  if (timelineTask && $("#drawer").classList.contains("open")) pollTimeline();
}, 5000);
