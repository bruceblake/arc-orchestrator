// Summary tier — answer "is everything OK?" at one glance, then let the
// operator drill down. Reads the same shared state the panels below render
// from (SLOTS <- /api/queue, GH <- /api/github, PROJECTS <- /api/projects);
// no new endpoint, no extra fetches. Each figure summarizes a panel and
// scrolls to + unfolds it on click; every panel the operator has not asked
// to see starts folded unless it needs attention (attention auto-unfolds,
// a manual toggle is remembered in localStorage).

const PANELS = ["health-panel", "slots-panel", "agents-panel", "gh-panel", "projects-panel", "topo-panel"];
const PANEL_PREF = "arc-panels";

function summaryData() {
  const t = (SLOTS && SLOTS.totals) || {};
  const hs = (SLOTS && SLOTS.harnesses) || [];
  let hRun = 0, hCap = 0;
  const hBlocked = [], hFull = [];
  for (const h of hs) {
    hRun += h.running || 0;
    hCap += h.cap || 0;
    if ((h.waiting || 0) > 0) hBlocked.push(h.harness);
    else if ((h.cap || 0) > 0 && (h.running || 0) >= (h.cap || 0)) hFull.push(h.harness);
  }
  let open = null, stranded = 0;
  if (GH && GH.ready) {
    open = Array.isArray(GH.prs) ? GH.prs.filter(p => p.state === "OPEN").length : 0;
    stranded = GH.stranded || 0;
  }
  // "conflict or failed TODAY": last_activity (ISO, UTC) is the only
  // timestamp /api/projects carries, so it is the available day-boundary —
  // a project untouched since an earlier day is an abandoned failure, not
  // this morning's news. A missing or unparseable timestamp still counts:
  // bad data must not silence the alarm.
  const isToday = iso => {
    if (!iso) return true;
    const t = new Date(iso);
    return isNaN(t) || t.toDateString() === new Date().toDateString();
  };
  let failed = 0, conflict = 0;
  const bad = [];
  for (const p of PROJECTS || []) {
    const s = p.statuses || {};
    if (((s.failed || 0) + (s.conflict || 0) > 0) && isToday(p.last_activity)) {
      failed += s.failed || 0;
      conflict += s.conflict || 0;
      bad.push(p.title || p.file || "?");
    }
  }
  return {
    running: t.running != null ? t.running : 0,
    waiting: t.waiting != null ? t.waiting : 0,
    reviewers: t.reviewers_waiting != null ? t.reviewers_waiting : 0,
    open: open, stranded: stranded, failed: failed, conflict: conflict, bad: bad,
    hRun: hRun, hCap: hCap, hBlocked: hBlocked, hFull: hFull,
  };
}

function panelAttention(d) {
  return {
    "health-panel": !!(d && (d.waiting > 0 || d.hBlocked.length > 0)),
    "slots-panel": !!(d && d.waiting > 0),
    "agents-panel": false,
    "gh-panel": !!(d && d.stranded > 0),
    "projects-panel": !!(d && d.failed + d.conflict > 0),
    "topo-panel": false,
  };
}

function fig(cls, glyph, value, label, detail, goto, title) {
  return '<div class="sumfig ' + cls + '" data-goto="' + goto + '" role="button" tabindex="0" title="'
    + attr(title || label) + '"><b>' + glyph + " " + value + "</b>"
    + '<span class="k">' + label + "</span>"
    + (detail ? '<span class="d">' + detail + "</span>" : "")
    + "</div>";
}

function renderSummary() {
  const line = $("#summary-line"), grid = $("#summary");
  if (!line || !grid) return;
  if (!SLOTS) {
    line.className = "hint";
    line.innerHTML = "waiting for fleet data…";
    grid.innerHTML = "";
    applyDisclosure(summaryData());
    return;
  }
  const d = summaryData();
  const figs = [];
  figs.push(fig(d.running > 0 ? "run" : "dim", "▶", d.running, "running",
    d.running > 0 ? d.hRun + " of " + d.hCap + " harness seats" : "fleet idle",
    "slots-panel", "harness sessions running right now — click to open model slots"));
  const qd = d.reviewers > 0 ? ' · <span class="rev">' + d.reviewers + " waiting on reviews</span>" : "";
  figs.push(fig(d.waiting > 0 ? "warn" : "dim", "◇", d.waiting, "queued",
    (d.waiting > 0 ? d.waiting + " waiting" : "queue empty") + qd,
    "slots-panel", "tasks waiting for a model slot — reviewers waiting means reviews are the bottleneck"));
  let pc = "dim", pv = "…", pd = GH ? "no remote" : "waiting on github…";
  if (d.open != null) {
    pv = d.open;
    if (d.stranded > 0) { pc = "warn"; pd = d.stranded + " stranded"; }
    else if (d.open > 0) { pc = "run"; pd = "awaiting review"; }
    else { pd = "none open"; }
  }
  figs.push(fig(pc, "⎇", pv, "prs", pd, "gh-panel",
    "pull requests open against the base branch; stranded means merged PRs never promoted"));
  const fc = d.failed + d.conflict;
  figs.push(fig(fc > 0 ? "err" : "ok", fc > 0 ? "✗" : "✓", fc, "failed · conflict",
    fc > 0 ? d.failed + " failed · " + d.conflict + " conflict" : "nothing failed or conflicting",
    "projects-panel", fc > 0 ? "attention: " + d.bad.join(", ") : "no failures, no conflicts"));
  const hcls = d.hBlocked.length ? "err" : d.hFull.length ? "warn" : "ok";
  figs.push(fig(hcls, "⛨", d.hCap ? d.hRun + "/" + d.hCap : "–", "harness",
    d.hBlocked.length ? d.hBlocked.map(esc).join(", ") + " blocked"
      : d.hFull.length ? d.hFull.map(esc).join(", ") + " at cap" : "capacity free",
    "slots-panel", "harness processes vs their caps — blocked means queued tasks are waiting on a full harness"));
  grid.innerHTML = figs.join("");

  const probs = [];
  if (fc > 0) probs.push(fc + " failed/conflict" + (d.bad.length ? " (" + d.bad.map(esc).join(", ") + ")" : ""));
  if (d.waiting > 0) probs.push(d.waiting + " queued" + (d.reviewers > 0 ? " (" + d.reviewers + " waiting on reviews)" : ""));
  if (d.stranded > 0) probs.push(d.stranded + " stranded PR" + (d.stranded > 1 ? "s" : ""));
  if (d.hBlocked.length > 0) probs.push(d.hBlocked.map(esc).join(", ") + " harness blocked");
  if (probs.length) {
    line.className = "err";
    line.innerHTML = probs.join(" · ");
  } else {
    line.className = "okmsg";
    line.innerHTML = "✓ everything looks healthy — no failures, no queue, no stranded PRs";
  }
  applyDisclosure(d);
}

// ---- progressive disclosure ----------------------------------------------

function readPrefs() {
  try {
    const v = JSON.parse(localStorage.getItem(PANEL_PREF) || "null");
    return v && typeof v === "object" ? v : {};
  } catch (e) { return {}; }
}

function writePrefs(p) {
  try { localStorage.setItem(PANEL_PREF, JSON.stringify(p)); } catch (e) {}
}

function fold(el, want) {
  if (!el) return;
  const has = (" " + el.className + " ").includes(" folded ");
  if (want && !has) el.className = (el.className + " folded").trim();
  else if (!want && has) el.className = el.className.replace(/\s*\bfolded\b/, "").trim();
}

function applyDisclosure(d) {
  const prefs = readPrefs();
  const att = panelAttention(d);
  for (const id of PANELS) {
    const el = $("#" + id);
    if (!el) continue;
    if (!(" " + el.className + " ").includes(" foldable ")) el.className = (el.className + " foldable").trim();
    const open = prefs[id] ? prefs[id] === "open" : att[id];
    fold(el, !open);
  }
}

function toggleFold(panel) {
  const isFolded = (" " + panel.className + " ").includes(" folded ");
  fold(panel, !isFolded);
  const prefs = readPrefs();
  prefs[panel.id] = isFolded ? "open" : "folded";
  writePrefs(prefs);
}

function gotoPanel(figEl) {
  const id = figEl.getAttribute("data-goto");
  const el = id ? $("#" + id) : null;
  if (!el) return;
  fold(el, false);
  // Record the open state like toggleFold does: renderSummary re-runs
  // applyDisclosure every poll, and an unremembered unfold would be folded
  // back shut within 3 s — the drill-down must survive the next tick.
  const prefs = readPrefs();
  prefs[id] = "open";
  writePrefs(prefs);
  if (el.scrollIntoView) el.scrollIntoView({ behavior: "smooth", block: "start" });
}

// Delegated at document level so the test harness (whose element stubs have
// no addEventListener) never touches these, and so panels loaded before this
// file need no wiring of their own.
document.addEventListener("click", (e) => {
  const t = e.target;
  if (!t || !t.closest) return;
  if (t.closest("button, a, select, input, textarea")) return;
  const h = t.closest(".panel.foldable > h2");
  if (h) {
    const p = h.closest(".panel.foldable");
    if (p && p.id) toggleFold(p);
    return;
  }
  const f = t.closest("#summary [data-goto]");
  if (f) gotoPanel(f);
});

document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const t = e.target;
  if (!t || !t.closest) return;
  const f = t.closest("#summary [data-goto]");
  if (f) { e.preventDefault(); gotoPanel(f); }
});

renderSummary();
setInterval(renderSummary, 3000);