// Behavioural unit tests for the dashboard render functions. No framework,
// no server: `node tests/ui_render.test.mjs`; exits non-zero on any failure.
// Same harness as tests/render_check.mjs — DOM stub + concatenated
// static/common.js + index.html's inline script evaluated with new Function.
import fs from "node:fs";
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
const externals = [...src.matchAll(/<script[^>]+src="([^"]+)"/g)]
  .map(m => "static/" + m[1].replace(/^\//, ""))
  .filter(f => fs.existsSync(f))
  .map(f => fs.readFileSync(f, "utf8"))
  .join("\n");
// renderGithub() reads module-scope `let GH`; expose a setter from inside.
const mod = new Function(externals + "\n" + js
  + "\nglobalThis.__setGH = v => { GH = v; };"
  + "\nreturn {card, pipelineLane, progressLines, renderGithub, liveBadge, friendly, esc};");
const api = mod();

let n = 0;
const failures = [];
function ok(cond, name) {
  n++;
  if (!cond) failures.push(name);
}
const count = (s, sub) => s.split(sub).length - 1;
const EVIL = '<img src=x onerror=alert(1)>';
const clean = s => !s.includes("<img src=x");

// ---- pipelineLane ----------------------------------------------------------
const merged = api.pipelineLane({ status: "merged" });
ok(count(merged, 'class="st done"') === 7, "merged: all 7 stages done");
ok(!merged.includes('class="st bad"') && !merged.includes('class="st now"') && !merged.includes('class="st wait"'),
   "merged: no bad/now/wait stages");

const running = api.pipelineLane({ status: "running", task_progress: { step: "gate" } });
ok(count(running, 'class="st done"') === 2, "running: stages before gate done");
ok(running.includes('class="st now" title="gate"'), "running: gate stage is now");
ok(!running.includes('class="st bad"'), "running: nothing red");

const inReview = api.pipelineLane({ status: "in_review" });
ok(count(inReview, 'class="st done"') === 5, "in_review: five stages done");
ok(inReview.includes('class="st wait" title="pr_review"'), "in_review: pr_review waits");
ok(inReview.includes('class="st " title="pr_merge"'), "in_review: merged stage neutral");

const failedKnown = api.pipelineLane({ status: "failed", task_progress: { step: "implement" } });
ok(failedKnown.includes('class="st done" title="alloc"'), "failed known: worktree stage done");
ok(failedKnown.includes('class="st bad" title="implement"'), "failed known: implement red");
ok(!failedKnown.includes("stage unknown"), "failed known: no unknown-stage tail");

const failedUnknown = api.pipelineLane({ status: "failed" });
ok(count(failedUnknown, 'class="st bad"') === 1, "failed unknown: exactly one red stage");
ok(failedUnknown.includes('class="st bad" title="stage unknown"'), "failed unknown: red is the tail badge");
ok(failedUnknown.includes('class="st " title="alloc"'), "failed unknown: first stage NOT painted red");

// ---- liveBadge -------------------------------------------------------------
ok(api.liveBadge({ run_pid: 1234 }).includes("LIVE"), "liveBadge: run_pid shows LIVE");
ok(api.liveBadge({ active: true }).includes("stale"), "liveBadge: active without run_pid shows stale");
ok(api.liveBadge({}) === "", "liveBadge: empty project shows nothing");
ok(api.liveBadge({ active: false, archived: true }) === "", "liveBadge: inactive archived shows nothing");

// ---- progressLines ---------------------------------------------------------
ok(api.progressLines({}) === "", "progressLines: empty progress renders nothing");
const moving = api.progressLines({ t1: { label: "writing", moving: 1, idle_s: 3, bytes: 2048 } });
ok(moving.includes("▸") && moving.includes("quiet"), "progressLines: moving task shows ▸ and quiet time");
const looping = api.progressLines({ t1: { label: "fixing", last_gate_failed: 1, attempt: 3, gate_log: "t1.log" } });
ok(looping.includes("↻") && looping.includes("verify gate rejected") && looping.includes("retry 3"),
   "progressLines: failed gate shows loop marker, reason and retry count");

// ---- card ------------------------------------------------------------------
const proj = { file: "p.json", title: "proj", n_tasks: 2, statuses: { merged: 1, failed: 1 },
               progress: { done: 1, total: 2 }, dag: { nodes: [], edges: [] }, tokens: 1500,
               seconds: 65, models: ["Kimi-K3"], reviewers: ["glm"], errors: [], last_activity: null };
const c = api.card(proj, 0);
ok(c.includes("1/2 merged"), "card: shows merged counts");
ok(c.includes("1 failed"), "card: shows failed count");
ok(c.includes('class="chip merged"') && c.includes('class="chip failed"'), "card: renders status chips");

// ---- renderGithub ----------------------------------------------------------
__setGH({ ready: false, reason: "no remote" });
api.renderGithub();
ok(document.querySelector("#gh-meta").textContent.includes("no remote"), "renderGithub: not-ready shows reason");
ok(document.querySelector("#gh-prs").innerHTML.includes("empty"), "renderGithub: not-ready shows empty panel");
__setGH({ ready: true, base: "development", prod: "main", repo_url: "http://x",
          prs: [{ state: "OPEN", number: 1, title: "t", url: "http://x/1", headRefName: "task/t1",
                  baseRefName: "development", additions: 1, deletions: 2, rounds: [] }] });
api.renderGithub();
ok(document.querySelector("#gh-meta").textContent.includes("1 open"), "renderGithub: meta counts open PRs");
ok(document.querySelector("#gh-prs").innerHTML.includes("task/t1"), "renderGithub: renders PR row");

// ---- escaping: every function must neutralise markup -----------------------
const evilProj = { file: "x.json", title: EVIL, statuses: {}, n_tasks: 1, progress: { done: 0, total: 1 },
                   dag: { nodes: [], edges: [] }, tokens: 0, seconds: 0, models: [], reviewers: [],
                   errors: [], last_activity: null };
ok(clean(api.card(evilProj, 0)), "escaping: card escapes title");
ok(clean(api.pipelineLane({ status: "failed", task_progress: { step: EVIL } })), "escaping: pipelineLane escapes step");
ok(clean(api.pipelineLane({ status: EVIL, task_progress: null })), "escaping: pipelineLane escapes status");
ok(clean(api.progressLines({ t1: { label: EVIL, gate_log: EVIL, last_gate_failed: 1 } })), "escaping: progressLines escapes label");
ok(clean(api.liveBadge({ run_pid: EVIL })), "escaping: liveBadge escapes run_pid title");
__setGH({ ready: true, base: "development", prod: "main",
          prs: [{ state: "OPEN", number: 2, title: EVIL, url: "http://x/2", headRefName: EVIL,
                  baseRefName: "development", additions: 0, deletions: 0, rounds: [] }] });
api.renderGithub();
ok(clean(document.querySelector("#gh-prs").innerHTML), "escaping: renderGithub escapes PR title/branch");
ok(clean(api.friendly({ type: "task.failed", task: "t1", reason: EVIL, model: EVIL })), "escaping: friendly escapes fields");

// ---- friendly: a readable line per event type ------------------------------
const events = [
  { type: "driver.start", model: "Kimi-K3", role: "implementer", task: "t1", attempt: 1 },
  { type: "driver.done", model: "GLM-5.3", role: "reviewer", task: "t1", seconds: 12, tokens: 500 },
  { type: "driver.error", model: "gpt-oss-120b", task: "t1", error: "boom" },
  { type: "driver.error", model: "GLM-5.3", task: "t1", capacity: true, error: "at cap" },
  { type: "driver.stalled", model: "Kimi-K3", task: "t1", idle_s: 130 },
  { type: "driver.timeout", model: "Kimi-K3", task: "t1" },
  { type: "driver.cancelled", model: "Kimi-K3", task: "t1" },
  { type: "driver.cap_wait", model: "GLM-5.3", in_use: 3, cap: 4 },
  { type: "worktree.alloc", branch: "task/t1" },
  { type: "worktree.free", branch: "task/t1" },
  { type: "task.start", task: "t1" },
  { type: "task.end", task: "t1", status: "merged" },
  { type: "task.escalated", task: "t1", from_model: "DeepSeek-V4-Flash", to_model: "GLM-5.3" },
  { type: "task.conflict", task: "t1", reason: "overlap" },
  { type: "task.failed", task: "t1", reason: "gate failed" },
  { type: "task.merged", task: "t1" },
  { type: "task.budget", task: "t1", implement_attempts: 2, escalations: 1, total_tokens: 1500, total_driver_seconds: 70 },
  { type: "task.pr_opened", task: "t1", url: "http://x/pr/1" },
  { type: "task.pr_skipped", task: "t1", reason: "no remote" },
  { type: "run.resume", skipped_merged: ["t0"], retried: ["t2"] },
  { type: "run.interrupted", tasks: ["t1"] },
  { type: "run.stopped" },
  { type: "graph.draining", in_flight: 2 },
  { type: "node_start", node: "gate_t1", task: "t1" },
  { type: "node_end", node: "gate_t1", task: "t1" },
  { type: "some.future.event", task: "t1" },
];
for (const e of events) {
  const out = api.friendly(e);
  ok(typeof out === "string" && out.length > 0 && !out.includes("undefined"),
     `friendly(${e.type}): readable line without 'undefined'`);
}

if (failures.length) {
  console.error(`ui_render: FAIL — ${failures.length}/${n} checks failed:`);
  for (const f of failures) console.error("  - " + f);
  process.exit(1);
}
console.log(`ui_render: PASS — ${n} checks`);
