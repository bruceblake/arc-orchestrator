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
  if (banner) {
    banner.style.display = stale.length ? "" : "none";
    banner.innerHTML = stale.length
      ? `⚠ the dashboard is running older code than the repo (${stale.map(esc).join(", ")} changed since it started) — run <code>./stop.sh &amp;&amp; ./start.sh</code> to pick it up`
      : "";
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

