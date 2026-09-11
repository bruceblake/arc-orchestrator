"use strict";
// ---- GitHub panel ----------------------------------------------------------
async function pollGithub() {
  try { GH = await jget("/api/github"); } catch (e) { return; }
  renderGithub();
}
function renderGithub() {
  const d = GH || {};
  const panel = $("#gh-panel");
  if (!d.ready) {
    $("#gh-meta").textContent = `(${d.reason || "unavailable"})`;
    $("#gh-prs").innerHTML = '<div class="empty">Add a remote and run <code>gh auth login</code> to turn on the pull-request flow.</div>';
    return;
  }
  const open = (d.prs || []).filter(p => p.state === "OPEN");
  // Hide the promote button when there is nothing to promote into: offering
  // it implies a gate that a one-branch configuration does not have.
  const promo = $("#gh-promote");
  if (promo) promo.style.display = (d.base && d.base === d.prod) ? "none" : "";
  $("#gh-meta").innerHTML =
    `(${open.length} open · ${esc(d.base)} → ${esc(d.prod)}` +
    `${d.unpromoted ? ` · ${d.unpromoted} commit${d.unpromoted === 1 ? "" : "s"} unpromoted` : " · in sync"})` +
    (d.stranded ? ` <span class="warn">· ${d.stranded} with no run</span>` : "");
  const repo = $("#gh-repo");
  if (repo && d.repo_url) repo.href = d.repo_url;
  $("#gh-prs").innerHTML = (d.prs || []).slice(0, 10).map(p => {
    const rounds = p.rounds || [];
    const last = rounds[rounds.length - 1];
    const verdict = !rounds.length
      ? '<span class="hint">no fleet review recorded</span>'
      : last.approved
        ? `<span class="good">✓ ${(last.approvals || []).map(esc).join(" + ")}</span>`
        : last.inconclusive
          ? `<span class="warn">⟳ no verdict — ${(last.crashed || []).map(short).map(esc).join(", ") || "a reviewer"} could not run</span>`
          : `<span class="bad">✗ changes requested${last.issues ? ` · ${last.issues} issue${last.issues === 1 ? "" : "s"}` : ""}</span>`;
    // What the reviewers actually said. A count told an operator nothing about
    // whether the objection was real; the text lived only on GitHub.
    const detail = (last && (last.detail || []).length)
      ? `<details class="prissues"><summary>why it was sent back</summary>${
          (last.detail || []).map(i => `<div>${esc(i)}</div>`).join("")}</details>`
      : "";
    const roundNote = rounds.length > 1 ? `<span class="tag">round ${rounds.length}</span>` : "";
    // A PR nobody is working on looks identical to a healthy open one. Seven
    // were stranded at once here — branch pushed, PR open, no process ever
    // coming back — and nothing in the UI said so.
    const stranded = p.stranded
      ? '<span class="warn" title="No run is currently working on this PR — either its project is queued behind others, or its run ended before the review finished. Re-run the project to pick it back up.">◌ no run</span>'
      : "";
    return `<div class="prrow${p.stranded ? " stranded" : ""}">
      <a class="num" href="${attr(p.url)}" target="_blank" rel="noopener">#${esc(p.number)}</a>
      <span class="prstate ${esc(p.state)}">${esc(p.state.toLowerCase())}</span>
      <span class="ttl">${esc(p.title)}</span>
      <span class="hint">${esc(p.headRefName)} → ${esc(p.baseRefName)}</span>
      <span class="hint">+${esc(p.additions)}/-${esc(p.deletions)}</span>
      ${roundNote}${stranded}${verdict}${detail}
    </div>`;
  }).join("") || '<div class="empty">No pull requests yet.</div>';
}
$("#gh-promote").onclick = async () => {
  if (!confirm(`Open a ${(GH || {}).base} → ${(GH || {}).prod} pull request?\n\nIt is NOT merged — you review and merge it on GitHub.`)) return;
  const {code, body} = await jpost("/api/promote", {});
  alert(code === 200 ? (body.url ? `Promotion PR opened:\n${body.url}` : body.note || "nothing to promote")
                     : `failed: ${body.error || "unknown"}`);
  pollGithub();
};


// The filters answer operator questions: which repo, which model, what
// happened, and free text over titles/task files. Everything composes.
