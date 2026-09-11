// Render the dashboard's own JavaScript against real API payloads in a minimal
// DOM stub. `node --check` only proves the file parses; this proves the render
// path actually runs on live data and that user-controlled strings stay escaped.
// It caught an agent's rewrite renaming functions the page still called.
//
//   node tests/render_check.mjs <health.json> <projects.json> <project.json>
import fs from "node:fs";
// Minimal DOM stub: every $("#id") returns a recording element.
const els = new Map();
const mk = id => {
  // innerHTML and textContent are LINKED in a real DOM: setting markup updates
  // the text, and setting text replaces the markup. Two independent fields made
  // an element written with .innerHTML read as empty through .textContent, which
  // failed a legitimate assertion for a reason that exists nowhere but here.
  let html = "";
  const el = { id, className: "", title: "", value: "", dataset: {}, checked: false,
                style: {}, disabled: false, tabIndex: 0,
                classList: {add(){},remove(){},contains:()=>false},
                querySelectorAll: () => [], appendChild(){}, after(){}, setAttribute(){}, onclick: null };
  Object.defineProperty(el, "innerHTML", { get() { return html; },
    set(v) { html = String(v == null ? "" : v); } });
  Object.defineProperty(el, "textContent", { get() { return html.replace(/<[^>]*>/g, ""); },
    set(v) { html = String(v == null ? "" : v); } });
  // A real <select> derives .options from its markup; setOptions() relies on
  // that to keep the chosen filter selected across a rebuild.
  Object.defineProperty(el, "options", { get() {
    return [...el.innerHTML.matchAll(/<option value="([^"]*)"/g)].map(m => ({value: m[1]})); } });
  // Just enough querySelector for the detail-panel lookup: the page asks for
  // [data-file="…"] with a CSS.escape-escaped name, so unescape before matching.
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
globalThis.location = { hash: "", search: "", href: "http://localhost:8787/" };
globalThis.CSS = { escape: s => String(s).replace(/[^a-zA-Z0-9_-]/g, c => "\\" + c) };
globalThis.history = { replaceState(){}, pushState(){} };
Object.defineProperty(globalThis, "navigator", {value: {clipboard: {writeText: async () => {}}}, configurable: true});
globalThis.confirm = () => false; globalThis.alert = () => {};
globalThis.fetch = async () => ({ json: async () => ({}), status: 200 });
globalThis.setInterval = () => 0; globalThis.setTimeout = () => 0; globalThis.clearInterval = () => 0;

const src = fs.readFileSync(new URL("../static/index.html", import.meta.url), "utf8");
const js = src.slice(src.indexOf("<script>") + 8, src.lastIndexOf("</script>"));
// Pages now load shared helpers from <script src="/common.js">. Without
// prepending those, the harness evaluates a page missing esc/short/tick and
// reports a break that is its own — and, worse, would MISS a real break in
// the shared file, which every page depends on.
const externals = [...src.matchAll(/<script[^>]+src="([^"]+)"/g)]
  .map(m => "static/" + m[1].replace(/^\//, ""))
  .filter(f => fs.existsSync(f))
  .map(f => fs.readFileSync(f, "utf8"))
  .join("\n");
// setGH: GH is module-scoped inside this evaluated function, so the harness
// needs a closure to assign it — a global would not reach it.
const mod = new Function(externals + "\n" + js + "\nreturn {renderHealth, card, renderTasks, renderDag, renderFeed, taskDag, friendly, esc, renderProjects, projectMatches, visibleProjects, loadHash, pollProjects, pollTopos, openDetail, closeDetail, renderSlots, renderGithub, renderErrors, setGH: v => { GH = v; }};");
const api = mod();

const health = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
api.renderHealth(health);
const hm = document.querySelector("#health-models").innerHTML;
console.log("health models rendered:", (hm.match(/class="mcap/g) || []).length, "chips");
console.log("header summary:", document.querySelector("#health-sum").textContent);
console.log("meta:", document.querySelector("#health-meta").textContent);
console.log("problems rendered:", (document.querySelector("#health-problems").innerHTML.match(/<div>/g) || []).length);

const projs = JSON.parse(fs.readFileSync(process.argv[3], "utf8")).projects;
let cards = projs.map((p, i) => api.card(p, i)).join("");
console.log("cards rendered:", projs.length, "| card html length:", cards.length);
console.log("cards showing a failure note:", (cards.match(/class="cardnote"/g) || []).length);

const d = JSON.parse(fs.readFileSync(process.argv[4], "utf8"));
api.renderDag(d); api.renderFeed(d);
console.log("dag svg:", document.querySelector("#dag").innerHTML.slice(0, 40) + "...");
console.log("feed entries:", (document.querySelector("#feed").innerHTML.match(/border-bottom/g) || []).length);

// Every event type the fleet emits must render as something a human reads,
// not fall through to the raw type name. The three biggest signals in the PR
// flow (gate result, review verdict, PR verdict) all did exactly that.
const feedProbe = [
  {type: "task.gate", task: "t1", passed: false, tail: "3 tests failed"},
  {type: "task.gate", task: "t1", passed: true},
  {type: "task.reviewed", task: "t1", passed: false, reviewer: "kimi", n_issues: 2},
  {type: "task.pr_reviewed", task: "t1", pr: 9, approved: false, n_issues: 4, round: 1},
  {type: "task.pr_reviewed", task: "t1", pr: 6, approved: true, approvals: ["Kimi-K3", "GLM-5.3"]},
  {type: "task.resumed", task: "t1", prior_status: "in_review"},
  {type: "task.pr_reattached", task: "t1", pr: 5},
  {type: "task.branch_reset", task: "t1", branch: "task/t1", commits_discarded: 3},
  {type: "driver.slot_wait", task: "t1", model: "GLM-5.3", scope: "harness"},
  {type: "task.resynced", task: "t1", pr: 4, base: "development", resyncs: 1},
  {type: "task.conflict", task: "t1", pr: 4, reason: "2 conflicting file(s)",
   files: ["config.py", "dashboard.py"]},
];
for (const e of feedProbe) {
  const out = api.friendly(e);
  if (out.includes(e.type)) {
    console.error(`render_check: FAIL — ${e.type} renders as its raw type name`);
    process.exit(1);
  }
}
console.log("feed event types rendered:", feedProbe.length);

// A stranded PR — open with no run working on it — must be visibly different
// from a healthy one. Seven were stranded at once and the UI showed them as
// ordinary open pull requests.
api.setGH({ready: true, base: "development", prod: "main", unpromoted: 2,
  stranded: 1,
  prs: [
    {number: 7, state: "OPEN", title: "t", url: "u", headRefName: "task/a",
     baseRefName: "development", additions: 1, deletions: 0, stranded: true,
     rounds: [{round: 1, approved: false, approvals: [], issues: 1,
               detail: ["[GLM-5.3] <img src=x> ships no tests"]}]},
    {number: 8, state: "OPEN", title: "t", url: "u", headRefName: "task/b",
     baseRefName: "development", additions: 1, deletions: 0, rounds: [], stranded: false},
  ]});
api.renderGithub();
const gh = document.querySelector("#gh-prs").innerHTML;
if ((gh.match(/prrow stranded/g) || []).length !== 1) {
  console.error("render_check: FAIL — stranded PRs are not marked"); process.exit(1);
}
if (!document.querySelector("#gh-meta").innerHTML.includes("1 with no run")) {
  console.error("render_check: FAIL — the no-run count is not in the header"); process.exit(1);
}
if (!gh.includes("why it was sent back")) {
  console.error("render_check: FAIL — review issues are not shown"); process.exit(1);
}
if (gh.includes("<img src=x>")) {
  console.error("render_check: FAIL — a reviewer's text reached the DOM unescaped");
  process.exit(1);
}
console.log("github rows rendered:", (gh.match(/class="prrow/g) || []).length);

// Defect triage. A traceback is attacker-shaped input twice over: it is
// generated from an exception whose message can contain anything a model wrote.
api.renderErrors({ready: true, total: 4, groups: [
  {fingerprint: "abc123", count: 3, kind: "ValueError", where: "code_tasks.py:publish",
   age_s: 30, span_s: 600, active: true, tasks: ["t1", "<img src=x>"],
   message: "boom <script>alert(1)</script>", traceback: "Traceback...\n  <img src=x onerror=1>"},
]});
const errHtml = document.querySelector("#errs").innerHTML;
if (!/errrow hot/.test(errHtml)) {
  console.error("render_check: FAIL — a still-firing defect is not highlighted"); process.exit(1);
}
if (errHtml.includes("<script>alert") || errHtml.includes("<img src=x")) {
  console.error("render_check: FAIL — a traceback reached the DOM unescaped"); process.exit(1);
}
api.renderErrors({ready: true, total: 0, groups: []});
if (document.querySelector("#errs-panel").style.display !== "none") {
  console.error("render_check: FAIL — the defects panel shows when there are none");
  process.exit(1);
}
console.log("defect rows rendered + escaped + hidden when empty");

// Model slots. Driven by a synthetic BUSY payload rather than the live one:
// the fleet is usually idle when check.sh runs, and an all-zeros payload would
// exercise none of the queue rendering this panel exists for.
const queue = {
  totals: {running: 2, waiting: 3, reviewers_waiting: 2, capacity: 17},
  models: [
    {model: "Kimi-K3", pretty: "Kimi K3", cap: 3, running: 3, waiting: 2, free: 0, reviewers_waiting: 2},
    {model: "GLM-5.3", pretty: "GLM 5.3", cap: 4, running: 1, waiting: 1, free: 3, reviewers_waiting: 0},
    // saturated but nobody blocked behind it — amber, not red
    {model: "gpt-oss-120b", pretty: "gpt-oss 120B", cap: 5, running: 5, waiting: 0, free: 0, reviewers_waiting: 0},
    {model: "DeepSeek-V4-Flash", pretty: "DeepSeek V4 Flash", cap: 5, running: 0, waiting: 0, free: 5, reviewers_waiting: 0},
  ],
  harnesses: [{harness: "opencode", cap: 5, running: 5, waiting: 2, free: 0},
              {harness: "kimi", cap: 3, running: 1, waiting: 0, free: 2}],
  running: [{task: "qa-http-core", model: "Kimi-K3", pretty: "Kimi K3", role: "pr_reviewer",
             role_label: "PR review", pid: 1234, seconds: 91}],
  waiting: [{task: '<img src=x onerror=alert(1)>', model: "Kimi-K3", pretty: "Kimi K3",
             role: "pr_reviewer", role_label: "PR review", scope: "fleet",
             seconds: 240, in_use: 3, cap: 3}],
};
api.renderSlots(queue);
const slotHtml = document.querySelector("#slots").innerHTML;
const rowHtml = document.querySelector("#slot-rows").innerHTML;
if (!/slot .*harness/.test(slotHtml)) {
  console.error("render_check: FAIL — harness capacity is not rendered"); process.exit(1);
}
console.log("slot cards:", (slotHtml.match(/class="slot /g) || []).length,
            "| queue rows:", (rowHtml.match(/class="qrow/g) || []).length);
console.log("slots meta:", document.querySelector("#slots-meta").innerHTML.replace(/<[^>]+>/g, ""));
if (!/class="slot full"/.test(slotHtml)) {
  console.error("render_check: FAIL — a saturated model is not marked full"); process.exit(1);
}
if (!/class="slot queued"/.test(slotHtml)) {
  console.error("render_check: FAIL — a model with a queue behind it is not marked queued"); process.exit(1);
}
if (!/qrow rev/.test(rowHtml)) {
  console.error("render_check: FAIL — a queued PR reviewer is not highlighted"); process.exit(1);
}
if (rowHtml.includes("<img src=x")) {
  console.error("render_check: FAIL — a task id reached the slot panel unescaped"); process.exit(1);
}

// XSS / escaping probe: a task title containing markup must not become markup.
const evil = { file: "x.json", title: '<img src=x onerror=alert(1)>', statuses: {}, n_tasks: 1,
               progress: {done:0,total:1}, dag: {nodes:[],edges:[]}, tokens: 0, seconds: 0,
               models: [], reviewers: [], errors: [{id:"<b>bad</b>", error:"<script>x</script>"}],
               last_activity: null };
const out = api.card(evil, 0);

// ---- new UI: phase-grouped list, filter bar, inline detail, graph topologies ----
// The checks above prove the render paths run on live payloads; these shape
// the project list so filters, collapsing, and the inline detail panel can be
// asserted deterministically (no dashboard required).
let good = 0;
const bad = [];
const ok = (name, cond) => { if (cond) good++; else bad.push(name); };

const mkProj = (file, title, phase, extra) => Object.assign({ file, title, phase,
  repo: "acme/" + file.replace(/\.json$/, ""), archived: false, models: [], statuses: {},
  progress: {done: 0, total: 2}, n_tasks: 2, dag: {nodes: [], edges: []},
  tokens: 0, seconds: 0, last_activity: null, errors: [] }, extra);
const FIX = { projects: [
  mkProj("ui.json", "game UI polish", "running", { models: ["GLM-5.3"], run_pid: 4242,
    statuses: {running: 1, merged: 1}, progress: {done: 1, total: 2},
    dag: {nodes: [{id: "t-clean", status: "merged"}, {id: "t-tests", status: "running", live: true}],
          edges: [{src: "t-clean", dst: "t-tests"}]}, tokens: 4200,
    last_activity: new Date().toISOString() }),
  mkProj("bench.json", "orchestration bench", "done", { models: ["gpt-oss-120b"],
    statuses: {merged: 2}, progress: {done: 2, total: 2},
    dag: {nodes: [{id: "b-one", status: "merged"}, {id: "b-two", status: "merged"}], edges: []} }),
  mkProj("web.json", "webapp build", "in_review", { models: ["DeepSeek-V4-Flash"],
    statuses: {in_review: 1, pending: 1},
    dag: {nodes: [{id: "w-a", status: "pending"}, {id: "w-b", status: "pending"}], edges: []} }),
]};
const DET = { file: "ui.json", title: "game UI polish", repo: "acme/arc-orchestrator",
  tasks: [{id: "t-clean", title: "cleanup", model: "GLM-5.3", reviewer: "kimi", deps: [], verify_cmd: "./check.sh"},
          {id: "t-tests", title: "tests", model: "GLM-5.3", reviewer: "kimi", deps: ["t-clean"], verify_cmd: "node t.js"}],
  rows: [{id: "t-clean", status: "merged", attempts: 1}, {id: "t-tests", status: "running", attempts: 2}],
  runs: [{task_id: "t-tests", model: "GLM-5.3", role: "implementer", harness: "opencode",
          attempt: 2, exit_code: 0, seconds: 12, verdict: "", transcript: "logs/harness/t-tests-x2.jsonl"}],
  task_progress: {}, git: {branch: "task/t-tests", dirty: [], worktrees: []} };
const GRAPHS = { round: { name: "research round", starts: ["q1", "q2"],
    nodes: [{name: "q1"}, {name: "q2"}, {name: "synth", gather: true}, {name: "verify"}],
    edges: [{src: "q1", dst: "synth"}, {src: "q2", dst: "synth"}, {src: "synth", dst: "verify", conditional: true}] },
  build: { name: "build graph", starts: ["plan"],
    nodes: [{name: "plan"}, {name: "assemble", gather: true}], edges: [{src: "plan", dst: "assemble"}] } };

// Route the page's pollers at the fixtures; unknown URLs get {} like the
// old stub, so guarded pollers (health, fleet) stay on their empty path.
globalThis.fetch = async u => ({ status: 200, json: async () =>
  u.includes("/api/project?") ? DET : u.includes("/api/projects") ? FIX
  : u.includes("/api/graphs") ? GRAPHS : {} });

const pr = () => document.querySelector("#projects").innerHTML;
await api.pollProjects();
ok("project list renders 3", pr().includes('data-file="ui.json"') && pr().includes('data-file="web.json"'));
ok("hint counts", document.querySelector("#f-hint").textContent === "3 of 3 projects");
ok("meta counts", document.querySelector("#proj-meta").textContent === "(3)");
ok("phase groups", pr().includes("phasehead running") && pr().includes("phasehead in_review") && pr().includes("phasehead done"));
ok("done collapsed by default", !pr().includes('data-file="bench.json"') && pr().includes('data-toggle="done"'));
ok("card dots", (pr().match(/class="dot /g) || []).length === 4);
ok("live dot + LIVE badge", pr().includes('class="dot live"') && pr().includes(">LIVE<"));
ok("tokens formatted", pr().includes("⛁"));

const filter = hash => { location.hash = hash; api.loadHash(); api.renderProjects(); };
filter("#q=game");
ok("text filter", document.querySelector("#f-hint").textContent === "1 of 3 projects" && pr().includes('data-file="ui.json"'));
filter("#m=DeepSeek-V4-Flash");
ok("model filter", pr().includes('data-file="web.json"') && !pr().includes('data-file="ui.json"'));
filter("#r=acme%2Fbench");
ok("repo filter", document.querySelector("#f-repo").value === "acme/bench" && document.querySelector("#f-hint").textContent === "1 of 3 projects");
filter("#s=failed");
ok("status=failed empty state", pr().includes("No projects match this filter."));
filter("#s=running");
ok("status=running filter", pr().includes('data-file="ui.json"') && !pr().includes('data-file="web.json"'));
filter("#s=merged");
ok("status=merged filter", document.querySelector("#f-hint").textContent === "1 of 3 projects" && pr().includes("phasehead done"));
filter("");

await api.openDetail("ui.json");
ok("detail opens inline", document.querySelector("#view-detail").style.display === "" && pr().includes("pwrap open"));
ok("detail title", document.querySelector("#d-title").textContent === "game UI polish");
ok("detail meta", document.querySelector("#d-meta").innerHTML.includes("<b>2</b> tasks") && document.querySelector("#d-meta").innerHTML.includes("<b>1</b> harness runs"));
ok("detail dag svg", document.querySelector("#dag").innerHTML.startsWith("<svg"));
api.closeDetail();
ok("detail closes", document.querySelector("#view-detail").style.display === "none" && !pr().includes("pwrap open"));

await api.pollTopos();
const tp = document.querySelector("#topo").innerHTML;
ok("topology section", tp.includes("topohead") && tp.includes("research round") && tp.includes("build graph"));
ok("topology meta", document.querySelector("#topo-meta").textContent === "2 workload graphs · dashed = conditional edge");
ok("topology counts", tp.includes("4 nodes · 3 edges") && tp.includes("2 nodes · 1 edges"));
ok("conditional edge dashed", tp.includes('stroke-dasharray="4 3"'));
ok("topo node class", tp.includes('class="node topo"'));
const tsvg = api.taskDag({starts: ["a"], nodes: [{id: "a"}, {id: "b", gather: true}],
  edges: [{src: "a", dst: "b", conditional: true}]}, {size: "full", topo: true});
ok("taskDag topo mode", tsvg.startsWith("<svg") && tsvg.includes('class="node topo"') && tsvg.includes('stroke-dasharray="4 3"'));

console.log(`ui checks: ${good} passed, ${bad.length} failed`);
if (!out.includes("<img src=x") && !out.includes("<script>x") && !bad.length) {
  console.log("render_check: PASS");
} else {
  if (out.includes("<img src=x") || out.includes("<script>x")) bad.push("unescaped user content reached the DOM");
  console.error("render_check: FAIL — " + bad.join("; "));
  process.exit(1);
}
