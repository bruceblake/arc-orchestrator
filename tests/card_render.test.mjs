// Behavioral test for the card-slimming rework (task card-slimming):
// the project card carries ONE line of signal — repo, title, LIVE badge,
// merged/failed chips, relative activity — and drops the redundant
// encodings of the same facts:
//   1. per-task status dots (the chips already count failures, and the
//      mini DAG already shows node status for multi-node projects)
//   2. the token stats span (tokens move to the detail view's meta line,
//      sourced client-side from the /api/projects snapshot — /api/project
//      needs no change)
// Runs the page's real inline script in a minimal DOM stub (same harness
// pattern as tests/projects_ui.test.mjs) against embedded fixtures — no
// dashboard required. Exits 1 if any check fails.
import fs from "node:fs";

// Minimal DOM stub: every $("#id") returns a recording element.
const els = new Map();
const mk = id => {
  // innerHTML and textContent are LINKED in a real DOM: setting markup
  // updates the text, and setting text replaces the markup.
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
globalThis.location = { hash: "", search: "", href: "http://localhost:8787/", pathname: "/" };
globalThis.CSS = { escape: s => String(s).replace(/[^a-zA-Z0-9_-]/g, c => "\\" + c) };
globalThis.history = { replaceState() {}, pushState(){} };
Object.defineProperty(globalThis, "navigator", {value: {clipboard: {writeText: async () => {}}}, configurable: true});
globalThis.confirm = () => false; globalThis.alert = () => {};
globalThis.setInterval = () => 0; globalThis.setTimeout = () => 0; globalThis.clearInterval = () => 0;

const src = fs.readFileSync(new URL("../static/index.html", import.meta.url), "utf8");
const js = src.slice(src.indexOf("<script>") + 8, src.lastIndexOf("</script>"));
const externals = [...src.matchAll(/<script[^>]+src="([^"]+)"/g)]
  .map(m => "static/" + m[1].replace(/^\//, ""))
  .filter(f => fs.existsSync(f))
  .map(f => fs.readFileSync(f, "utf8"))
  .join("\n");
const mod = new Function(externals + "\n" + js + "\nreturn {card, pollProjects, openDetail, closeDetail};");
const api = mod();

let good = 0;
const bad = [];
const ok = (name, cond) => { if (cond) good++; else bad.push(name); };
// The page's own handlers are async fire-and-forget; one macrotask tick
// settles every await in the chain (all stubbed fetches resolve immediately).
const drain = () => new Promise(r => setImmediate(r));

// ---- fixtures: a healthy running project (2 tasks, LIVE) and a flaky one
// (4 tasks: merged + 2 failed + 1 conflict) whose failure signal must fold
// into the chips; token values distinct enough to tell card from detail ----
const NOW = new Date().toISOString();
const FIX = { projects: [
  { file: "ui.json", repo: "acme/ui", title: "game UI polish", phase: "running", archived: false,
    models: ["GLM-5.3"], run_pid: 4242, statuses: {running: 1, merged: 1},
    progress: {done: 1, total: 2}, n_tasks: 2, tokens: 48890, seconds: 0,
    last_activity: NOW, errors: [],
    dag: {nodes: [{id: "t-clean", status: "merged"}, {id: "t-tests", status: "running", live: true}],
          edges: [{src: "t-clean", dst: "t-tests"}]} },
  { file: "flaky.json", repo: "acme/other-repo", title: "flaky tests", phase: "attention", archived: false,
    models: ["DeepSeek-V4.1-Flash-thinking-max"], statuses: {merged: 1, failed: 2, conflict: 1},
    progress: {done: 1, total: 4}, n_tasks: 4, tokens: 9000, seconds: 0,
    last_activity: NOW, errors: [],
    dag: {nodes: [{id: "f-a", status: "merged"}, {id: "f-b", status: "failed"},
                  {id: "f-c", status: "failed"}, {id: "f-d", status: "conflict"}],
          edges: []} },
]};
// /api/project payload for ui.json — deliberately WITHOUT a tokens field:
// the detail meta line must source tokens from PROJECTS (the /api/projects
// snapshot), not from this response.
const DET = { file: "ui.json", title: "game UI polish", repo: "acme/arc-orchestrator",
  tasks: [{id: "t-clean", title: "cleanup", model: "GLM-5.3", reviewer: "deepseek", deps: [], verify_cmd: "./check.sh"},
          {id: "t-tests", title: "tests", model: "GLM-5.3", reviewer: "deepseek", deps: ["t-clean"], verify_cmd: "node t.js"}],
  rows: [{id: "t-clean", status: "merged", attempts: 1}, {id: "t-tests", status: "running", attempts: 2}],
  runs: [{task_id: "t-tests", model: "GLM-5.3", role: "implementer", harness: "opencode",
          attempt: 2, exit_code: 0, seconds: 12, verdict: "", transcript: "logs/harness/t-tests-x2.jsonl"}],
  task_progress: {}, git: {branch: "task/t-tests", dirty: [], worktrees: []} };
const GRAPHS = {
  round: { name: "research round", starts: ["q1", "q2"],
    nodes: [{name: "q1"}, {name: "q2"}, {name: "synth", gather: true}, {name: "verify"}],
    edges: [{src: "q1", dst: "synth"}, {src: "q2", dst: "synth"}, {src: "synth", dst: "verify", conditional: true}] },
};

globalThis.fetch = async u => ({ status: 200, json: async () =>
  String(u).includes("/api/project?") ? DET : String(u).includes("/api/projects") ? FIX
  : String(u).includes("/api/graphs") ? GRAPHS : {} });

// ==== 1. the card is one line of signal ====
const cardUi = api.card(FIX.projects[0], 0);
ok("no per-task status dots on the card", !cardUi.includes('class="dot'));
ok("no token stats on the card", !cardUi.includes("⛁"));
ok("card keeps repo + title", cardUi.includes("acme/ui") && cardUi.includes("game UI polish"));
ok("card keeps LIVE badge for a running project", cardUi.includes(">LIVE<"));
ok("card keeps merged chip", cardUi.includes("1/2 merged"));
ok("card keeps relative activity", cardUi.includes("ago"));
ok("card keeps archive toggle", cardUi.includes('data-arch="ui.json"'));
ok("card keeps chevron + button semantics",
   cardUi.includes('class="chev"') && cardUi.includes('role="button"') && cardUi.includes('aria-expanded="false"'));

// ==== 2. failure signal folds conflict into the failed chip ====
const cardFlaky = api.card(FIX.projects[1], 1);
ok("failed chip counts failed + conflict", cardFlaky.includes('class="chip failed"') && cardFlaky.includes("3 failed"));
ok("flaky card keeps merged chip", cardFlaky.includes("1/4 merged"));
ok("healthy card has no failed chip", !cardUi.includes('class="chip failed"'));

// ==== 3. the mini DAG survives the slimming (3+ node projects only) ====
ok("3+ node project keeps the mini DAG", cardFlaky.includes("<svg"));
ok("2-node project skips the mini DAG", !cardUi.includes("<svg"));

// ==== 4. tokens moved to the detail meta line ====
await api.pollProjects();
await api.openDetail("ui.json");
await drain();
const meta = document.querySelector("#d-meta").innerHTML;
ok("detail meta shows tokens from the projects snapshot", meta.includes("⛁") && meta.includes("48.9k"));
ok("detail meta keeps repo/file/tasks/runs",
   meta.includes("acme/arc-orchestrator") && meta.includes("ui.json")
   && meta.includes("<b>2</b> tasks") && meta.includes("<b>1</b> harness runs"));
api.closeDetail();

console.log(bad.length ? "FAILURES:\n  " + bad.join("\n  ") : `all ${good} checks passed`);
process.exit(bad.length ? 1 : 0);