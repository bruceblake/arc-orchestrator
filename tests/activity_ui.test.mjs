// Behavioral test for the Fleet activity panel (task activity-feed):
//   1. newest first, one row per curated event
//   2. type badges with distinct colours (review / merge / fail / escalate /
//      degraded / stall)
//   3. relative timestamps
//   4. a click on an event that has a task id links to that project
// Runs the panel's real script in a minimal DOM stub (the same harness pattern
// as tests/projects_ui.test.mjs) against embedded fixtures — no dashboard
// required. Exits 1 if any check fails.
import fs from "node:fs";

// Minimal DOM stub: every $("#id") returns a recording element.
const els = new Map();
const mk = id => {
  let html = "";
  const el = { id, className: "", title: "", value: "", dataset: {}, checked: false,
                style: {}, disabled: false, tabIndex: 0,
                classList: {add(){},remove(){},contains:()=>false},
                querySelectorAll: () => [], appendChild(){}, after(){}, setAttribute(){}, onclick: null };
  Object.defineProperty(el, "innerHTML", { get() { return html; },
    set(v) { html = String(v == null ? "" : v); } });
  Object.defineProperty(el, "textContent", { get() { return html.replace(/<[^>]*>/g, ""); },
    set(v) { html = String(v == null ? "" : v); } });
  el.querySelector = sel => { const m = /data-file="(.+?)"/.exec(sel || ""); if (!m) return null;
    const f = m[1].replace(/\\(.)/g, "$1");
    return el.innerHTML.includes(`data-file="${f}"`) ? mk("row:" + f) : null; };
  return el; };
globalThis.document = {
  querySelector: sel => { const id = sel.replace(/^#/, ""); if (!els.has(id)) els.set(id, mk(id)); return els.get(id); },
  querySelectorAll: () => [],
  createElement: () => mk("new"),
  addEventListener: () => {}, removeEventListener: () => {},
  body: mk("body"), documentElement: mk("html"),
};
globalThis.window = { addEventListener: () => {}, removeEventListener: () => {},
                      matchMedia: () => ({matches:false, addEventListener(){}}),
                      location: {hash: "", search: ""} };
globalThis.localStorage = { getItem: () => null, setItem(){}, removeItem(){} };
globalThis.location = { hash: "", search: "", href: "http://localhost:8787/", pathname: "/" };
globalThis.CSS = { escape: s => String(s).replace(/[^a-zA-Z0-9_-]/g, c => "\\" + c) };
globalThis.confirm = () => false; globalThis.alert = () => {};
globalThis.setInterval = () => 0; globalThis.setTimeout = () => 0; globalThis.clearInterval = () => 0;

// The panel is a real file the page loads, alongside common.js and the state
// helpers it leans on ($, esc, AGO, jget, markFail).
const read = f => fs.readFileSync(new URL("../static/" + f, import.meta.url), "utf8");
// openDetail() lives in projects/detail.js — the panel calls it from a click
// handler, so the harness supplies the one function the click path needs and
// records what it was asked to open.
const OPENED = [];
const STUBS = "\nfunction openDetail(file) { globalThis.__opened.push(file); }\n";
globalThis.__opened = OPENED;
const EXTERNALS = STUBS + ["common.js", "panels/state.js", "panels/fleet.js"].map(read).join("\n");
const mod = new Function(EXTERNALS +
  "\nreturn {renderActivity, pollActivity, activityRow, activityTarget,"
  + " ACTIVITY_KIND, ACTIVITY_COLOR,"
  // PROJECTS is a `let` in state.js, i.e. in this Function's scope: the page
  // fills it from /api/projects, and the tests need the same lever.
  + " setProjects: ps => { PROJECTS = ps; }};");
const api = mod();

let good = 0;
const bad = [];
const ok = (name, cond) => { if (cond) good++; else bad.push(name); };

const NOW = Date.now() / 1000;
const EV = [
  { ts: NOW - 5, type: "task.reviewed", task: "t-rev", file: "panel.json",
    run_id: "r1", context: {workload: "code"}, reviewer: "glm", model: "GLM-5.3",
    n_issues: 2, round: 1, passed: false },
  { ts: NOW - 400, type: "task.merged", task: "t-merge", file: "panel.json",
    run_id: "r1", context: {workload: "code"}, pr: 12 },
  { ts: NOW - 900, type: "task.failed", task: "t-fail", file: "panel.json",
    run_id: "r1", context: {workload: "code"},
    reason: "exhausted escalation: 1 escalation(s), ended on GLM-5.3" },
  { ts: NOW - 1200, type: "task.escalated", task: "t-esc", file: "panel.json",
    run_id: "r1", context: {workload: "code"},
    from_model: "DeepSeek-V4.1-Flash-thinking-max", to_model: "GLM-5.3" },
  { ts: NOW - 2000, type: "task.review_degraded", task: "t-deg", file: "panel.json",
    run_id: "r1", context: {workload: "code"}, got: 1, wanted: 2,
    note: "thin review: 1 of 2 reviewer(s) the roster wanted" },
  // Real shape: drivers.py emits the task id it was HANDED, which code_tasks
  // passes as `<tid>-xN`/`<tid>-prN`, and `file` is whatever the server could
  // resolve for that id.
  { ts: NOW - 2500, type: "driver.stalled", task: "t-stall-x2", file: null,
    run_id: "r1", context: {workload: "code"}, model: "GLM-5.3", idle_s: 900 },
  { ts: NOW - 3000, type: "chain.wait", run_id: "r1", context: {workload: "code"},
    taskfile: "/home/x/tasks/panel.json" },
  // The PUBLISH-path resync — the shape 51 of 51 real ones have: base and
  // note, no PR number. It used to render "PR #undefined".
  { ts: NOW - 3500, type: "task.resynced", task: "t-resync", file: "panel.json",
    run_id: "r1", context: {workload: "code"}, base: "main",
    note: "merged main into task/t-resync" },
];

let FETCHED = [];
globalThis.fetch = async u => { FETCHED.push(String(u)); return { status: 200,
  json: async () => ({events: EV, limit: 50, total: EV.length}) }; };
const feed = () => document.querySelector("#activity").innerHTML;
// The loaded project list, as /api/projects returns it: BASE task ids only.
api.setProjects([{file: "panel.json", tasks: [{id: "t-rev"}, {id: "t-stall"},
                                              {id: "t-merge"}]}]);

await api.pollActivity();
ok("feed fetched from /api/activity", FETCHED.some(u => u.startsWith("/api/activity")));
ok("one row per event", (feed().match(/class="arow/g) || []).length === EV.length);
ok("newest first",
   feed().indexOf("t-rev") < feed().indexOf("t-merge")
   && feed().indexOf("t-merge") < feed().indexOf("t-fail"));
ok("relative timestamps rendered", /\d+s/.test(feed()) && /\d+m/.test(feed()));
ok("no undefined leaked into the markup",
   !feed().includes(">undefined") && !feed().includes('"undefined"'));

// ---- type badges, one distinct colour per family -----------------------
const BADGE = {review: "#58a6ff", merge: "#3fb950", fail: "#f85149",
               escalate: "#bc8cff", degraded: "#d29922", stall: "#f0883e"};
const kinds = ["review", "merge", "fail", "escalate", "degraded", "stall"];
ok("every badge family has its own colour",
   kinds.every(k => api.ACTIVITY_COLOR[k] === BADGE[k])
   && new Set(kinds.map(k => BADGE[k])).size === kinds.length);
ok("badge colours actually land in the markup",
   kinds.every(k => feed().includes(BADGE[k])));
ok("each event is labelled with its own type",
   feed().includes("review") && feed().includes("merged") && feed().includes("failed")
   && feed().includes("escalated") && feed().includes("degraded")
   && feed().includes("stalled"));
ok("failed events carry the real reason string",
   feed().includes("exhausted escalation: 1 escalation(s), ended on GLM-5.3"));
ok("review events carry the reviewer and the issue count",
   feed().includes("GLM 5.3") && feed().includes("2 issues"));
// A failure carries BOTH the verdict and the evidence: the reason names what
// happened, the detail says what was wrong. Rendering only one of the two
// either hides why it died or hides what to fix.
const bothHtml = api.activityRow({
  ts: NOW - 60, type: "task.failed", task: "t-both", reason: "verify gate failed",
  detail: "FAIL: test_the_tail_is_kept" });
ok("a failure shows its reason and its evidence",
   bothHtml.includes("verify gate failed") && bothHtml.includes("FAIL: test_the_tail_is_kept"));
// Historical events predate the enriched fields — the panel reads real
// 81k-line logs, most of which was written before this task existed. None of
// it may render as an empty "by " or a literal undefined.
const oldPr = api.activityRow({ ts: NOW - 90, type: "task.pr_reviewed", task: "t-old",
  pr: 5, round: 2, approved: false, reviewers: ["glm"], n_issues: 1 });
ok("an older PR-review event falls back to the family token",
   oldPr.includes("changes requested by GLM 5.3") && !oldPr.includes("by  ·"));
const oldRev = api.activityRow({ ts: NOW - 90, type: "task.reviewed", task: "t-old",
  passed: true, reviewer: "deepseek" });
// The token renders through revShort — the page's existing convention, shared
// with detail.js and projects.js — so it is never blank and never a literal
// undefined. A NEW event carries the resolved `model` and reads "GLM 5.3".
ok("an older pre-merge review event still names its reviewer",
   oldRev.includes("passed by deepseek") && !oldRev.includes("undefined")
   && !oldRev.includes("by  ·"));
ok("a new pre-merge review event names the resolved model, not the token",
   api.activityRow({ ts: NOW, type: "task.reviewed", task: "t",
     passed: false, reviewer: "glm", model: "DeepSeek-V4.1-Flash-thinking-max" })
     .includes("rejected by DeepSeek V4.1 Flash max"));
// Waiting on an upstream project is routine, not an incident: it shares the
// stall colour but must not be tinted red like a stalled harness.
const isBad = html => /class="arow[^"]*\bbad\b/.test(html);
ok("chain.wait is not tinted as an error while a stall is",
   !isBad(api.activityRow({ ts: NOW, type: "chain.wait", taskfile: "/x/a.json" }))
   && isBad(api.activityRow({ ts: NOW, type: "driver.stalled", task: "t" }))
   && isBad(api.activityRow({ ts: NOW, type: "task.failed", task: "t", reason: "x" })));

// ---- a resync has two shapes, and only one of them has a PR --------------
// Every one of the 51 task.resynced records in the live log comes from the
// publish path and carries base/note with NO pr; only the rarer pr_merge
// resync has one. Reading `PR #${e.pr}` unconditionally printed
// "PR #undefined" on every resync row.
const rsNoPr = api.activityRow({ ts: NOW, type: "task.resynced", task: "t-rs",
  base: "main", note: "merged main into task/t-rs" });
ok("a publish-path resync reads as a resync, not PR #undefined",
   rsNoPr.includes("merged main into the branch") && !rsNoPr.includes("undefined"));
ok("a pr_merge resync still names its PR",
   api.activityRow({ ts: NOW, type: "task.resynced", task: "t-rs", pr: 12,
     base: "main", resyncs: 1 }).includes("PR #12 merged with main"));
ok("the fixture resync row renders without an undefined", !/undefined/.test(feed()));

// ---- an inconclusive PR round is not a rejection ------------------------
// `approved` is false when a reviewer CRASHED and nobody objected (17 of the
// 97 real task.pr_reviewed events): the diff was never read, and the round is
// retried. Calling that "changes requested" reports a rejection of code no
// reviewer saw.
const incon = api.activityRow({ ts: NOW, type: "task.pr_reviewed", task: "t-in",
  pr: 4, round: 2, approved: false, inconclusive: true, reviewers: ["glm"],
  models: ["GLM-5.3"], crashed: ["GLM-5.3"], n_issues: 1,
  issues: ["reviewer GLM-5.3 session ended without a verdict"] });
ok("an inconclusive round says no verdict, retrying",
   incon.includes("no verdict (reviewer crashed), retrying") && incon.includes("PR #4"));
ok("a genuine rejection still reads as changes requested",
   api.activityRow({ ts: NOW, type: "task.pr_reviewed", task: "t-rj", pr: 4,
     round: 3, approved: false, reviewers: ["glm"], models: ["GLM-5.3"], n_issues: 2 })
     .includes("changes requested by GLM 5.3"));

// ---- click-through to the project --------------------------------------
ok("rows with a task id carry the click affordance",
   feed().includes('data-task="t-rev"') && feed().includes('role="button"'));
ok("the server-resolved taskfile rides on the row",
   feed().includes('data-file="panel.json"'));
ok("rows without a task id are not clickable",
   !/data-task=""|data-task="undefined"/.test(feed()));

// ---- a driver event's id is not a project id ----------------------------
// drivers.py:1332 emits `driver.stalled` with the task id code_tasks handed it
// — `<tid>-xN` for an attempt, `<tid>-prN` for a PR reviewer — while the page
// matches projects on BASE ids. Resolving only exact ids left `file` null and
// still rendered role="button", so a stalled harness (the row an operator most
// wants to open) was a focusable control that opened nothing.
ok("a suffixed driver id still resolves to its project",
   api.activityTarget({task: "t-stall-x2", file: null}) === "panel.json"
   && api.activityTarget({task: "t-rev-pr3", file: null}) === "panel.json");
// An id that IS a row key must not be rewritten: a project can legitimately
// be called `release-pr2`, and stripping first would open a different one.
ok("an exact task id wins over the suffix-stripped fallback",
   api.activityTarget({task: "t-stall-x2", file: null}) === "panel.json"
   && api.activityTarget({task: "t-merge", file: null}) === "panel.json");
ok("an unresolvable task id is not rendered as a button",
   !/role="button"/.test(api.activityRow({ts: NOW, type: "chain.wait",
     task: "nobody-knows-this-one-x7", file: null}))
   && !/has-task/.test(api.activityRow({ts: NOW, type: "chain.wait",
     task: "nobody-knows-this-one-x7", file: null}))
   // ...but its task id is still shown: the row is readable, just not a link.
   && api.activityRow({ts: NOW, type: "chain.wait", task: "gone-x7", file: null})
        .includes("gone-x7"));
// The click handler is the page's: it must route the event's own task/file
// into openDetail (which the harness stubs and records).
OPENED.length = 0;
document.querySelector("#activity").onclick({ target: { closest: () => ({
  dataset: { task: "t-rev", file: "panel.json" } }) } });
ok("clicking an event opens that project", OPENED.length === 1 && OPENED[0] === "panel.json");
// Enter/Space on a focused row is the keyboard equivalent of that click.
OPENED.length = 0;
document.querySelector("#activity").onkeydown({ key: "Enter", preventDefault() {},
  target: { closest: () => ({ dataset: { task: "t-rev", file: "panel.json" } }) } });
ok("Enter on a focused row opens it too",
   OPENED.length === 1 && OPENED[0] === "panel.json");

// ---- empty state -------------------------------------------------------
globalThis.fetch = async () => ({status: 200, json: async () => ({events: []})});
await api.pollActivity();
// The message is #activity-empty's, and it must be the ONLY copy: rendering an
// inline duplicate into #activity printed the same sentence twice.
ok("an empty feed says so instead of rendering nothing",
   !/arow/.test(feed())
   && document.querySelector("#activity-empty").style.display === ""
   && (read("index.html").match(/No fleet activity in the log yet/g) || []).length === 1
   && !feed().includes("No fleet activity"));

console.log(`activity_ui: ${good} passed, ${bad.length} failed`);
if (bad.length) {
  console.error("activity_ui: FAIL — " + bad.join("; "));
  process.exit(1);
}
console.log("activity_ui: PASS");
