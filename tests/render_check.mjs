// Render the dashboard's own JavaScript against real API payloads in a minimal
// DOM stub. `node --check` only proves the file parses; this proves the render
// path actually runs on live data and that user-controlled strings stay escaped.
// It caught an agent's rewrite renaming functions the page still called.
//
//   node tests/render_check.mjs <health.json> <projects.json> <project.json>
import fs from "node:fs";
// Minimal DOM stub: every $("#id") returns a recording element.
const els = new Map();
// innerHTML and textContent are LINKED in a real DOM: setting markup updates
// the text, and setting text replaces the markup. Two independent fields made
// an element written with .innerHTML read as empty through .textContent, which
// failed a legitimate assertion for a reason that exists nowhere but here.
const mk = id => {
  let html = "";
  return {
    id, className: "", title: "", value: "", options: [], dataset: {}, checked: false,
    style: {}, disabled: false, classList: {add(){},remove(){},contains:()=>false},
    querySelectorAll: () => [], appendChild(){}, onclick: null,
    get innerHTML() { return html; },
    set innerHTML(v) { html = String(v == null ? "" : v); },
    get textContent() { return html.replace(/<[^>]*>/g, ""); },
    set textContent(v) { html = String(v == null ? "" : v); },
  };
};
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
const mod = new Function(externals + "\n" + js + "\nreturn {renderHealth, card, renderTasks, renderDag, renderFeed, taskDag, friendly, esc, renderProjects, renderSlots, renderGithub, setGH: v => { GH = v; }};");
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


if (!out.includes("<img src=x") && !out.includes("<script>x")) {
  console.log("render_check: PASS");
} else {
  console.error("render_check: FAIL — unescaped user content reached the DOM");
  process.exit(1);
}
