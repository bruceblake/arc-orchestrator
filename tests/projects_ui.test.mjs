// Behavioral test for the projects-list UI rework (task projects-ui-cleanup):
//   1. compact single-row project cards that expand inline — one at a time,
//      persisted in the URL hash
//   2. a combined filter bar (repo / status / model / free text) that
//      composes, round-trips through the URL hash and drives an "N of M"
//      counter
//   3. workload topologies from /api/graphs rendered by the SAME taskDag
//      renderer the project DAGs use (cycles, conditional edges included)
// Runs the page's real inline script in a minimal DOM stub (same harness
// pattern as tests/render_check.mjs) against embedded fixtures — no
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
// saveHash() persists filters/expanded state via history.replaceState —
// record the URLs it writes so hash round-trips can be asserted.
const REPLACED = [];
globalThis.history = { replaceState: (a, b, url) => { REPLACED.push(String(url)); }, pushState(){} };
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
const mod = new Function(externals + "\n" + js + "\nreturn {card, taskDag, renderProjects, projectMatches, visibleProjects, loadHash, saveHash, pollProjects, pollTopos, openDetail, closeDetail};");
const api = mod();

let good = 0;
const bad = [];
const ok = (name, cond) => { if (cond) good++; else bad.push(name); };

// ---- fixtures: 4 projects across phases, 2 repos, 3 models ----
const mkProj = (file, title, phase, extra) => Object.assign({ file, title, phase,
  repo: "acme/" + file.replace(/\.json$/, ""), archived: false, models: [], statuses: {},
  progress: {done: 0, total: 2}, n_tasks: 2, dag: {nodes: [], edges: []},
  tokens: 0, seconds: 0, last_activity: null, errors: [] }, extra);
const NOW = new Date().toISOString();
const FIX = { projects: [
  mkProj("ui.json", "game UI polish", "running", { models: ["GLM-5.3"], run_pid: 4242,
    statuses: {running: 1, merged: 1}, progress: {done: 1, total: 2},
    dag: {nodes: [{id: "t-clean", status: "merged"}, {id: "t-tests", status: "running", live: true}],
          edges: [{src: "t-clean", dst: "t-tests"}]}, tokens: 4200,
    last_activity: NOW }),
  mkProj("web.json", "webapp build", "in_review", { models: ["DeepSeek-V4-Flash"],
    statuses: {in_review: 1, pending: 1},
    dag: {nodes: [{id: "w-a", status: "pending"}, {id: "w-b", status: "pending"}], edges: []} }),
  mkProj("flaky.json", "flaky tests", "attention", { models: ["gpt-oss-120b"], repo: "acme/other-repo",
    statuses: {merged: 1, failed: 1, conflict: 1}, progress: {done: 1, total: 3}, n_tasks: 3,
    dag: {nodes: [{id: "f-a", status: "merged"}, {id: "f-b", status: "failed"}, {id: "f-c", status: "conflict"}],
          edges: []}, tokens: 90000, last_activity: NOW }),
  mkProj("bench.json", "orchestration bench", "done", { models: ["gpt-oss-120b"],
    statuses: {merged: 2}, progress: {done: 2, total: 2},
    dag: {nodes: [{id: "b-one", status: "merged"}, {id: "b-two", status: "merged"}], edges: []} }),
]};
const DET = { file: "ui.json", title: "game UI polish", repo: "acme/arc-orchestrator",
  tasks: [{id: "t-clean", title: "cleanup", model: "GLM-5.3", reviewer: "kimi", deps: [], verify_cmd: "./check.sh"},
          {id: "t-tests", title: "tests", model: "GLM-5.3", reviewer: "kimi", deps: ["t-clean"], verify_cmd: "node t.js"}],
  rows: [{id: "t-clean", status: "merged", attempts: 1}, {id: "t-tests", status: "running", attempts: 2}],
  runs: [{task_id: "t-tests", model: "GLM-5.3", role: "implementer", harness: "opencode",
          attempt: 2, exit_code: 0, seconds: 12, verdict: "", transcript: "logs/harness/t-tests-x2.jsonl"}],
  task_progress: {}, git: {branch: "task/t-tests", dirty: [], worktrees: []} };
// /api/graphs shape (dashboard._build_graph_topologies): nodes carry {name,
// gather}, edges {src, dst, conditional}. The "fix loop" graph is cyclic.
const GRAPHS = {
  round: { name: "research round", starts: ["q1", "q2"],
    nodes: [{name: "q1"}, {name: "q2"}, {name: "synth", gather: true}, {name: "verify"}],
    edges: [{src: "q1", dst: "synth"}, {src: "q2", dst: "synth"}, {src: "synth", dst: "verify", conditional: true}] },
  build: { name: "build graph", starts: ["plan"],
    nodes: [{name: "plan"}, {name: "assemble", gather: true}], edges: [{src: "plan", dst: "assemble"}] },
  cycle: { name: "fix loop graph", starts: ["work"],
    nodes: [{name: "work"}, {name: "gate"}, {name: "fix"}],
    edges: [{src: "work", dst: "gate"}, {src: "gate", dst: "fix"}, {src: "fix", dst: "work", conditional: true}] },
};

const FETCHED = [];
globalThis.fetch = async u => { FETCHED.push(String(u)); return { status: 200, json: async () =>
  String(u).includes("/api/project?") ? DET : String(u).includes("/api/projects") ? FIX
  : String(u).includes("/api/graphs") ? GRAPHS : {} } };

const pr = () => document.querySelector("#projects").innerHTML;
const hint = () => document.querySelector("#f-hint").textContent;
const lastHash = () => REPLACED[REPLACED.length - 1];
// The page's own handlers are async fire-and-forget; one macrotask tick
// settles every await in the chain (all stubbed fetches resolve immediately).
const drain = () => new Promise(r => setImmediate(r));
const filter = hash => { location.hash = hash; api.loadHash(); api.renderProjects(); };
const files = () => api.visibleProjects().map(p => p.file).sort();

// ==== 1. compact single-row cards with inline expand ====
await api.pollProjects();
ok("all projects visible by default", files().length === 4 && hint() === "4 of 4 projects");
ok("one compact row per visible project",
   (pr().match(/class="prow"/g) || []).length === 3 && (pr().match(/class="chev"/g) || []).length === 3);
ok("repo + short repo shown", pr().includes("acme/ui") && pr().includes("acme/other-repo"));
const cardUi = api.card(FIX.projects[0], 0);
ok("one status dot per task, colored by status",
   (cardUi.match(/class="dot /g) || []).length === 2 && cardUi.includes("background:#3fb950")
   && cardUi.includes("background:#58a6ff") && cardUi.includes('class="dot live"'));
const cardFlaky = api.card(FIX.projects[2], 2);
ok("failed/conflict dot colors", cardFlaky.includes("background:#f85149") && cardFlaky.includes("background:#f0883e"));
ok("merged/failed count chips", cardFlaky.includes("1/3 merged") && cardFlaky.includes("2 failed"));
ok("tokens + relative time on the row", cardUi.includes("⛁") && cardUi.includes("ago"));
ok("row is a keyboard-reachable button",
   cardUi.includes('role="button"') && cardUi.includes('aria-expanded="false"')
   && cardUi.includes('aria-label="open project game UI polish"'));

// Click a row: the delegated #projects handler must expand it inline.
const projectsEl = document.querySelector("#projects");
const rowClick = i => projectsEl.onclick({ target: { closest: () => ({ dataset: { i: String(i) } }) } });
rowClick(0); await drain();
ok("click expands the row inline",
   pr().includes('class="pwrap open" data-file="ui.json"') && document.querySelector("#view-detail").style.display === "");
ok("expanded state persisted to the hash", lastHash() === "#x=ui.json");
rowClick(1); await drain();
ok("only one row expanded at a time",
   (pr().match(/class="pwrap open"/g) || []).length === 1 && pr().includes('class="pwrap open" data-file="web.json"'));
ok("previous row collapsed", /class="pwrap" data-file="ui.json"/.test(pr()));
rowClick(1); await drain();
ok("click again collapses",
   !pr().includes("pwrap open") && document.querySelector("#view-detail").style.display === "none");
ok("hash cleared on close", !lastHash().includes("#"));
// Reload with #x=… in the URL: the detail panel re-opens for that project.
location.hash = "#x=web.json"; api.loadHash();
await api.pollProjects(); await drain();
ok("hash re-opens the detail after reload",
   pr().includes('class="pwrap open" data-file="web.json"') && document.querySelector("#view-detail").style.display === "");
api.closeDetail(); await drain(); location.hash = "";

// ==== 2. filter bar: repo / status / model / text, composed + persisted ====
filter("#q=game");
ok("text search matches the title", JSON.stringify(files()) === '["ui.json"]' && hint() === "1 of 4 projects");
filter("#q=flaky");
ok("text search matches the task file", JSON.stringify(files()) === '["flaky.json"]');
filter("#m=DeepSeek-V4-Flash");
ok("model filter", JSON.stringify(files()) === '["web.json"]');
filter("#r=acme%2Fother-repo");
ok("repo filter", JSON.stringify(files()) === '["flaky.json"]');
filter("#r=acme%2Fui&q=game");
ok("filters compose", JSON.stringify(files()) === '["ui.json"]');
filter("#s=running");
ok("status filter: has running", JSON.stringify(files()) === '["ui.json"]');
filter("#s=failed");
ok("status filter: has failed (incl. conflict)", JSON.stringify(files()) === '["flaky.json"]');
filter("#s=merged");
ok("status filter: all merged", JSON.stringify(files()) === '["bench.json"]');
filter("#q=zzz");
ok("no-match empty state", pr().includes("No projects match this filter.") && hint() === "0 of 4 projects");
filter("#r=acme%2Fui&q=game");
api.saveHash();
ok("filters round-trip through the hash", lastHash() === "#r=acme%2Fui&q=game");
filter("");
ok("repo picker offered when >1 repo",
   document.querySelector("#f-repo").style.display === "" && document.querySelector("#f-repo").innerHTML.includes('value="acme/other-repo"')
   && document.querySelector("#f-repo").innerHTML.includes("All repos"));
const modelOpts = document.querySelector("#f-model").innerHTML;
ok("model picker lists the fleet",
   ["gpt-oss-120b", "DeepSeek-V4-Flash", "GLM-5.3", "Kimi-K3"].every(m => modelOpts.includes(`value="${m}"`)));
const fstat = src.slice(src.indexOf('id="f-status"'), src.indexOf('id="f-model"'));
ok("status picker offers running/failed/merged",
   ['value="running"', 'value="failed"', 'value="merged"'].every(v => fstat.includes(v)));
ok("graphs panel sits below the projects list", src.indexOf('id="topo-panel"') > src.indexOf('id="projects"'));

// ==== 3. bottom graphs: /api/graphs through the shared taskDag renderer ====
FETCHED.length = 0;
await api.pollTopos();
ok("topologies fetched from /api/graphs", FETCHED.includes("/api/graphs"));
const tp = document.querySelector("#topo").innerHTML;
ok("one headed section per workload graph",
   tp.includes("research round") && tp.includes("build graph") && tp.includes("fix loop graph")
   && tp.includes("4 nodes · 3 edges") && tp.includes("2 nodes · 1 edges"));
const gOf = t => ({ starts: t.starts || [],
  nodes: (t.nodes || []).map(n => ({id: n.name, gather: !!n.gather})),
  edges: (t.edges || []).map(e => ({src: e.src, dst: e.dst, conditional: !!e.conditional})) });
// The topology markup must be byte-identical to what the ONE shared renderer
// emits for the same graph — not a re-implementation living next to it.
ok("topologies rendered by the shared taskDag",
   tp.includes(api.taskDag(gOf(GRAPHS.round), {size: "full", topo: true}))
   && tp.includes(api.taskDag(gOf(GRAPHS.cycle), {size: "full", topo: true})));
const pd = api.taskDag(FIX.projects[0].dag, {size: "full", file: "ui.json"});
ok("project dags use that same renderer",
   pd.startsWith("<svg") && pd.includes('data-t="t-clean"') && pd.includes('stroke="#3fb950"') && pd.includes('stroke="#58a6ff"'));
ok("conditional edges dashed", tp.includes('stroke-dasharray="4 3"'));
ok("start/gather nodes colored", tp.includes('stroke="#58a6ff"') && tp.includes('stroke="#bc8cff"'));
ok("no stale field names leak into the svg", !tp.includes(">undefined") && !tp.includes("NaN"));
// A cyclic topology must render its back edge as a visible loop-back instead
// of pushing nodes outside the viewBox.
const cyc = api.taskDag(gOf(GRAPHS.cycle), {size: "full", topo: true});
const vb = /viewBox="0 0 (\d+) (\d+)"/.exec(cyc);
let inside = !!vb;
if (vb) for (const m of cyc.matchAll(/<rect x="(\d+(?:\.\d+)?)"[^>]*width="(\d+)"/g))
  inside = inside && (+m[1] + +m[2] <= +vb[1] + 0.5);
ok("cyclic graph: loop-back drawn, nodes stay in the viewBox",
   inside && cyc.includes('stroke="#d29922"'));

console.log(`projects_ui: ${good} passed, ${bad.length} failed`);
if (bad.length) {
  console.error("projects_ui: FAIL — " + bad.join("; "));
  process.exit(1);
}
console.log("projects_ui: PASS");