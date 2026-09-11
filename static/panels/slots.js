"use strict";
// ---- live model slots: who holds capacity, who is queued behind them ----
// The reviewer queue is the fleet's real bottleneck (PR_REVIEWERS scarce
// cross-family models per PR round), so reviewers are given their own colour,
// their own count, and their own filter rather than being averaged into a
// single "N waiting" number that hides which queue is actually stuck.

function slotCard(m) {
  const cls = m.waiting ? "queued" : m.running >= m.cap ? "full" : m.running ? "busy" : "";
  const pips = Array.from({length: m.cap},
    (_, i) => `<i class="pip${i < m.running ? " on" : ""}"></i>`).join("");
  const q = Array.from({length: Math.min(m.waiting, 12)}, (_, i) =>
    `<i class="qpip${i < m.reviewers_waiting ? " rev" : ""}"></i>`).join("");
  const revs = m.reviewers_waiting
    ? ` · <span class="rev">${m.reviewers_waiting} review${m.reviewers_waiting > 1 ? "s" : ""}</span>` : "";
  return `<div class="slot ${cls}${m.isHarness ? " harness" : ""}">
    <div class="slot-h"><b>${esc(m.pretty)}</b>${m.isHarness ? '<span class="tagh">harness</span>' : ""}<span class="n">${m.running}/${m.cap}</span></div>
    <div class="pips">${pips}</div>
    ${m.waiting ? `<div class="qbar">${q}</div>
      <div class="slot-q">${m.waiting} queued${revs}</div>`
      : `<div class="slot-q">${m.free} free</div>`}
  </div>`;
}

function slotRow(r, running) {
  const rev = r.role === "pr_reviewer";
  const why = running ? "holding a slot"
    : r.scope === "harness"
      ? `${r.harness || "harness"} pool full${r.cap ? ` (cap ${r.cap})` : ""}`
      : r.scope === "fleet"
        ? `fleet at cap${r.cap ? ` (${r.in_use}/${r.cap})` : ""}`
        : "waiting for a slot in its own run";
  return `<div class="qrow${rev ? " rev" : ""}${running ? " running" : ""}">
    <span>${running ? "▶" : "⏳"}</span>
    <span class="who">${esc(r.task)}</span>
    <span class="role">${esc(r.role_label || "working")}</span>
    <span class="why">${esc(r.pretty)} · ${why}</span>
    <span class="wait">${tick(r.seconds)}</span>
  </div>`;
}

function renderSlots(q) {
  SLOTS = q;
  const t = q.totals || {};
  const revWait = t.reviewers_waiting || 0;
  $("#slots-meta").innerHTML = t.running || t.waiting
    ? `${t.running} running · ${t.waiting} queued`
      + (revWait ? ` · <span style="color:var(--purple)">${revWait} reviewer${revWait > 1 ? "s" : ""} blocked</span>` : "")
      + ` · ${t.capacity} slots total`
    : "idle";
  // Harness rows first: opencode's own pool is usually the binding limit, and
  // when it is saturated every model looks comfortably under its cap while
  // nothing moves.
  const hs = (q.harnesses || []).map(h => slotCard({
    pretty: h.harness, cap: h.cap, running: h.running, waiting: h.waiting,
    free: h.free, reviewers_waiting: 0, isHarness: true,
  })).join("");
  $("#slots").innerHTML = hs + (q.models || []).map(slotCard).join("");
  const revOnly = SLOT_VIEW === "rev";
  const keep = r => !revOnly || r.role === "pr_reviewer";
  const rows = (q.waiting || []).filter(keep).map(r => slotRow(r, false))
    .concat((q.running || []).filter(keep).map(r => slotRow(r, true)));
  $("#slot-rows").innerHTML = rows.join("");
  $("#slots-empty").style.display = rows.length ? "none" : "";
  $("#slots-empty").textContent = revOnly && (t.running || t.waiting)
    ? "No PR reviewers running or queued right now."
    : "Nothing is running or queued — every model slot is free.";
}

async function pollSlots() {
  try { renderSlots(await jget("/api/queue")); markFail("queue", false); }
  catch (e) { markFail("queue", true); }
}

for (const b of document.querySelectorAll("[data-sl]")) {
  b.onclick = () => {
    SLOT_VIEW = b.dataset.sl;
    for (const o of document.querySelectorAll("[data-sl]")) o.classList.toggle("on", o === b);
    if (SLOTS) renderSlots(SLOTS);
  };
}

