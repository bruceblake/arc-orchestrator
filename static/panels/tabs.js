"use strict";
// ---- tabs --------------------------------------------------------------------
// The list view used to be ten panels stacked in one long scroll, with a
// 50-line activity log (often days old) above the projects and live agents an
// operator actually came to see. Tabs group the same panels by the question
// they answer; nothing is removed, and every panel keeps its element id.
//
//   Overview  what is running right now, and does anything need me?
//   Studio    the game being built: phases, gates, tasks, renders, verdicts
//   Projects  every taskfile, pull requests, the pipeline diagram
//   Activity  the full event log
const TABS = {
  overview: ["summary-panel", "errs-panel", "agents-panel", "slots-panel", "health-panel"],
  studio:   ["studio-panel"],
  projects: ["projects-panel", "gh-panel", "topo-panel"],
  activity: ["activity-panel"],
};
// The panel each tab exists to show. The summary tier's auto-fold must never
// close it, or switching to a tab would reveal a collapsed header and nothing
// else — the original clutter problem in a new place.
const TAB_PRIMARY = new Set(["agents-panel", "studio-panel", "projects-panel", "activity-panel"]);
const TAB_KEY = "arc.tab";
let TAB = "";

function tabOf(panelId) {
  for (const [t, ids] of Object.entries(TABS)) if (ids.includes(panelId)) return t;
  return "";
}

// Class edits go through className strings, not classList, and elements are
// found with $ (state.js) — the same convention as summary.js's fold(), so the
// node test harness's element stubs (no classList, no getElementById) are
// never touched in a way they cannot model.
function setHidden(el, hidden) {
  const cls = " " + (el.className || "") + " ";
  const has = cls.includes(" tab-hidden ");
  if (hidden && !has) el.className = ((el.className || "") + " tab-hidden").trim();
  else if (!hidden && has) el.className = cls.replace(" tab-hidden ", " ").trim();
}

function showTab(name, remember) {
  if (!TABS[name]) name = "overview";
  TAB = name;
  for (const [t, ids] of Object.entries(TABS)) {
    for (const id of ids) {
      const el = $("#" + id);
      // errs-panel manages its own visibility (it hides when there is nothing
      // to show); this only adds or removes the tab class on top of that.
      if (el) setHidden(el, t !== name);
    }
  }
  for (const t of Object.keys(TABS)) {
    const b = $('[data-tab="' + t + '"]');
    if (b && b.setAttribute) b.setAttribute("aria-selected", t === name ? "true" : "false");
  }
  if (remember) { try { localStorage.setItem(TAB_KEY, name); } catch (e) {} }
  if (name === "studio" && typeof pollStudio === "function") pollStudio();
}

// Used by summary.js: clicking a summary figure goes to its panel's tab first.
function showTabFor(panelId) {
  const t = tabOf(panelId);
  if (t && t !== TAB) showTab(t, true);
}

function initTabs(hasStudio) {
  let want = "";
  try { want = localStorage.getItem(TAB_KEY) || ""; } catch (e) {}
  // First visit: land on the game if there is one being built.
  showTab(want || (hasStudio ? "studio" : "overview"), false);
}

document.addEventListener("click", (e) => {
  const b = e.target && e.target.closest && e.target.closest("[data-tab]");
  if (b) showTab(b.getAttribute("data-tab"), true);
});
