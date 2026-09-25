"use strict";
// ---- Needs you: the human checkpoint queue ------------------------------------
// Every pull request the fleet approved that is now waiting for a person
// (AGENTS.md Rule 5, manual review; taskfile `human_review`). Each card shows
// what a decision needs in one place: the evidence the gate captured
// (before/after per camera, screenshots, flythrough and playtest videos), the
// fleet reviewers' verdicts, a button that opens the build on this machine's
// display, and Approve / Request changes. A rejection's comment becomes the
// implementer's feedback. Data: GET /api/reviews (review_routes.py).
let REVIEWS = null;
// Unsent comments and scene picks, keyed "<task>|<pr>|<round>", so a poll
// re-render never wipes what was typed.
const RV_DRAFT = {};
const rvKey = r => `${r.task}|${r.pr}|${r.round}`;
const rvGh = u => typeof u === "string" && /^https:\/\/github\.com\//.test(u);

function rvEditing() {
  const a = document.activeElement;
  return !!(a && a.closest && a.closest("[data-rv-form]") && /^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName || ""));
}

function rvVerdicts(r) {
  const rows = (r.reviewers || []).map(v => {
    const st = v.crashed ? "crashed" : v.approve ? "approved" : "changes requested";
    const cls = v.crashed ? "conflict" : v.approve ? "merged" : "failed";
    const issues = (v.issues || []).map(i => `<li>${esc(i)}</li>`).join("");
    const fu = (v.follow_ups || []).map(i => `<li class="hint">${esc(i)}</li>`).join("");
    return `<div class="rv-verdict"><b>${esc(v.model || "?")}</b> <span class="chip ${cls}">${st}</span>
      ${issues || fu ? `<ul>${issues}${fu}</ul>` : ""}</div>`;
  }).join("");
  return rows || '<div class="hint">no fleet verdicts recorded</div>';
}

function rvEvidence(ev) {
  if (!ev) return '<div class="empty">No evidence captured for this task (not a Godot project, or capture was unavailable).</div>';
  const img = (u, alt) => u ? `<a class="rv-shot" href="${attr(u)}" target="_blank" rel="noopener"><img loading="lazy" src="${attr(u)}" alt="${attr(alt)}"></a>` : "";
  const cmp = (ev.compare || []).map(c => `<figure class="rv-cmp">${img(c.url, "before | after | difference: " + (c.name || ""))}
      <figcaption>${esc(c.name || "")} <span class="hint">${c.changed == null ? "" : (100 * c.changed).toFixed(1) + "% of pixels changed"}</span></figcaption></figure>`).join("");
  const vids = Object.entries(ev.videos || {}).map(([name, v]) => `<figure class="rv-cmp">
      ${v.gif ? img(v.gif, name + " video") : ""}
      <figcaption>${esc(name)} ${v.mp4 ? `<a href="${attr(v.mp4)}" target="_blank" rel="noopener">mp4</a>` : ""}</figcaption></figure>`).join("");
  const shots = (ev.shots || []).map(s => img(s.url, s.name || "screenshot")).join("");
  const warn = (ev.warnings || []).map(w => `<li>${esc(w)}</li>`).join("");
  return `<div class="hint">evidence from attempt ${esc(ev.attempt || "?")}</div>
    ${warn ? `<ul class="rv-warn">${warn}</ul>` : ""}
    ${cmp ? `<h4>Before | after | difference</h4><div class="rv-grid">${cmp}</div>` : ""}
    ${vids ? `<h4>Videos</h4><div class="rv-grid">${vids}</div>` : ""}
    ${!cmp && ev.contact_sheet ? `<h4>Screenshots</h4>${img(ev.contact_sheet, "contact sheet")}` : ""}
    ${shots ? `<details><summary>all ${(ev.shots || []).length} images</summary><div class="rv-thumbs">${shots}</div></details>` : ""}`;
}

function rvPlay(r) {
  const p = r.play || {};
  const k = rvKey(r);
  if (!p.available) {
    return `<div class="rv-play"><button class="pill" disabled title="${attr(p.reason || "")}">▶ Play this build</button>
      <span class="hint">${esc(p.reason || "cannot play here")}</span></div>`;
  }
  const scenes = p.scenes || [];
  const cur = RV_DRAFT[k + "|scene"] || "";
  const pick = scenes.length > 1 ? `<select data-rv-scene="${attr(k)}" aria-label="scene to open">
      ${scenes.map(s => `<option value="${attr(s === p.main_scene ? "" : s)}"${(s === p.main_scene ? "" : s) === cur ? " selected" : ""}>${esc(s)}${s === p.main_scene ? " (main scene)" : ""}</option>`).join("")}</select>` : "";
  const base = p.base ? `<button class="pill" data-rv-play="${attr(p.base.build)}" data-rv-project="${attr(p.project)}" data-rv-key="${attr(k)}">▶ Play ${esc(p.base.build)}</button>` : "";
  return `<div class="rv-play" data-rv-form="play">
    <button class="pill good" data-rv-play="${attr(p.build)}" data-rv-project="${attr(p.project)}" data-rv-key="${attr(k)}">▶ Play this build</button>
    ${pick} ${base}
    <span class="hint">opens on this PC's display · F8 in game logs a finding</span></div>`;
}

function rvCard(r) {
  const k = rvKey(r);
  const pr = rvGh(r.url) ? `<a href="${attr(r.url)}" target="_blank" rel="noopener">PR #${esc(r.pr)}</a>` : `PR #${esc(r.pr)}`;
  const live = r.live ? '<span class="chip running">run waiting</span>'
    : '<span class="chip conflict" title="the run that asked is not alive; your decision is kept and applied when it resumes">run not live</span>';
  return `<section class="rv-card" data-rv="${attr(k)}">
    <div class="rv-h"><b>${esc(r.title || r.task)}</b> <span class="tag">${esc(r.task)}</span> ${pr}
      <span class="hint">round ${esc(r.round)} · ${esc(r.project || "")} · waiting ${esc(AGO(r.requested_at))}</span> ${live}</div>
    <div class="rv-cols">
      <div class="rv-ev">${rvEvidence(r.evidence)}</div>
      <div class="rv-side">
        <h4>Play it</h4>${rvPlay(r)}
        <h4>Fleet reviewers</h4>${rvVerdicts(r)}
        <h4>Your decision</h4>
        <div class="rv-form" data-rv-form="decide">
          <textarea data-rv-comment="${attr(k)}" rows="3" maxlength="4000" aria-label="comment for the implementer"
            placeholder="What must change? (required to request changes; optional note on approve)">${esc(RV_DRAFT[k] || "")}</textarea>
          <div class="rv-btns">
            <button class="act rv-ok" data-rv-decide="approve" data-rv-key="${attr(k)}">✓ Approve &amp; merge</button>
            <button class="act rv-no" data-rv-decide="reject" data-rv-key="${attr(k)}">✗ Request changes</button>
          </div>
        </div>
      </div>
    </div>
  </section>`;
}

function renderReviews() {
  const el = $("#reviews"), badge = $("#rv-count");
  const d = REVIEWS || {};
  const waiting = d.waiting || [];
  if (badge) {
    badge.textContent = waiting.length ? String(waiting.length) : "";
    badge.className = waiting.length ? "rv-badge on" : "rv-badge";
  }
  if (!el) return;
  const mode = d.global_default ? "on for every task (ARC_PR_MANUAL_REVIEW=1)"
    : "off by default: only tasks whose taskfile sets human_review wait here";
  const recent = (d.recent || []).slice(0, 10).map(r => `<div class="rv-recent">
      <span class="chip ${r.status === "approved" ? "merged" : "failed"}">${esc(r.status)}</span>
      <b>${esc(r.task)}</b> PR #${esc(r.pr)} r${esc(r.round)} <span class="hint">${esc(r.decided_by || "")} ${esc(AGO(r.decided_at))} ago${r.comment ? " — " + esc(String(r.comment).slice(0, 160)) : ""}</span></div>`).join("");
  el.innerHTML = `<div class="hint">Human checkpoint ${esc(mode)}. Play any other build (main or any task branch) under Studio → Playtest.</div>
    ${waiting.length ? waiting.map(rvCard).join("") : '<div class="empty">Nothing is waiting for you. A PR lands here after the fleet approves it, when its task wants a human checkpoint.</div>'}
    ${recent ? `<h3>Recent decisions</h3>${recent}` : ""}`;
}

async function pollReviews() {
  try { REVIEWS = await jget("/api/reviews"); markFail("reviews", false); }
  catch (e) { markFail("reviews", true); return; }
  if (!rvEditing()) renderReviews();
  else renderReviewsBadgeOnly();
}
function renderReviewsBadgeOnly() {
  const badge = $("#rv-count"), n = ((REVIEWS || {}).waiting || []).length;
  if (badge) { badge.textContent = n ? String(n) : ""; badge.className = n ? "rv-badge on" : "rv-badge"; }
}

function rvFind(k) { return ((REVIEWS || {}).waiting || []).find(r => rvKey(r) === k); }

async function rvDecide(k, decision) {
  const r = rvFind(k);
  if (!r) return;
  const comment = (RV_DRAFT[k] || "").trim();
  if (decision === "reject" && !comment) { alert("Say what must change: the comment is what the implementer gets."); return; }
  if (decision === "approve" && !confirm(`Approve ${r.task} (PR #${r.pr}) and let the fleet merge it?`)) return;
  let res;
  try { res = await jpost("/api/reviews/decide", {task: r.task, pr: r.pr, round: r.round, decision, comment}); }
  catch (e) { alert("decision failed: " + e); return; }
  if (res.code !== 200) { alert("decision failed: " + ((res.body && res.body.error) || res.code)); return; }
  delete RV_DRAFT[k];
  pollReviews();
}

async function rvLaunch(project, build, k) {
  const body = {project, build};
  const scene = k && build.startsWith("task/") ? RV_DRAFT[k + "|scene"] : "";
  if (scene) body.scene = scene;
  let res;
  try { res = await jpost("/api/studio/playtest/launch", body); }
  catch (e) { alert("launch failed: " + e); return; }
  if (res.code !== 200) { alert("launch failed: " + ((res.body && res.body.error) || res.code)); return; }
  const s = (res.body || {}).session || {};
  alert(s.status === "preparing"
    ? `Preparing ${build} (first play of this build: clone + import). Godot opens on the PC's display when ready.`
    : `${build} is starting on the PC's display. F8 in game logs a finding.`);
}

document.addEventListener("input", e => {
  const t = e.target;
  const k = t && t.getAttribute && t.getAttribute("data-rv-comment");
  if (k) RV_DRAFT[k] = t.value;
});
document.addEventListener("change", e => {
  const t = e.target;
  const k = t && t.getAttribute && t.getAttribute("data-rv-scene");
  if (k) RV_DRAFT[k + "|scene"] = t.value;
});
document.addEventListener("click", e => {
  const t = e.target && e.target.closest ? e.target : null;
  if (!t) return;
  const d = t.closest("[data-rv-decide]");
  if (d) { rvDecide(d.getAttribute("data-rv-key"), d.getAttribute("data-rv-decide")); return; }
  const p = t.closest("[data-rv-play]");
  if (p && !p.disabled) rvLaunch(p.getAttribute("data-rv-project"), p.getAttribute("data-rv-play"), p.getAttribute("data-rv-key"));
});
