// Behavioural unit tests for the dashboard render functions. No framework,
// no server: `node tests/ui_render.test.mjs`; exits non-zero on any failure.
// Same harness as tests/render_check.mjs — DOM stub + concatenated
// static/common.js + each page's inline script (index.html, usage.html)
// evaluated with new Function.
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
// Backed by a Map so persistence (localStorage prefs) can be asserted; a
// plain no-op stub could never prove a fold was remembered.
const lsBacking = new Map();
globalThis.localStorage = { getItem: k => (lsBacking.has(k) ? lsBacking.get(k) : null),
                            setItem: (k, v) => lsBacking.set(k, String(v)),
                            removeItem: k => lsBacking.delete(k) };
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
  + "\nglobalThis.__setSlots = v => { SLOTS = v; };"
  + "\nglobalThis.__setProjects = v => { PROJECTS = v; };"
  + "\nreturn {card, pipelineLane, progressLines, renderGithub, liveBadge, friendly, esc, renderSummary, gotoPanel};");
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

// ---- renderSummary: the one-glance tier --------------------------------
const sumEl = document.querySelector("#summary");
const sumLine = document.querySelector("#summary-line");
const cnt = (s, sub) => s.split(sub).length - 1;

__setSlots({ totals: { running: 2, waiting: 1, reviewers_waiting: 1, capacity: 8 },
             harnesses: [ { harness: "opencode", cap: 5, running: 5, waiting: 1, free: 0 },
                          { harness: "kimi", cap: 3, running: 2, waiting: 0, free: 1 } ] });
__setGH({ now: 1, ready: true, base: "development", prod: "main", repo_url: "http://x",
          prs: [ { state: "OPEN", number: 1, title: "a" }, { state: "MERGED", number: 2 } ], stranded: 1 });
__setProjects([ { file: "a.json", title: "alpha", statuses: { merged: 2, failed: 1, conflict: 1 } },
                { file: "b.json", title: "beta", statuses: { merged: 3 } } ]);
api.renderSummary();
ok(cnt(sumEl.innerHTML, 'class="sumfig') === 5, "summary: five figures, no more");
ok(sumEl.innerHTML.includes('data-goto="slots-panel"') && sumEl.innerHTML.includes('data-goto="gh-panel"')
   && sumEl.innerHTML.includes('data-goto="projects-panel"'), "summary: figures point at their panels");
ok(sumEl.innerHTML.includes("waiting on reviews"), "summary: reviewers called out as the bottleneck");
ok(sumLine.innerHTML.includes("failed") && sumLine.innerHTML.includes("alpha"), "summary: alarm line names the problem");
ok(sumLine.className === "err", "summary: alarm line is red");
ok(!document.querySelector("#projects-panel").className.includes("folded")
   && document.querySelector("#projects-panel").className.includes("foldable"),
   "summary: attention panel unfolded");
ok(document.querySelector("#topo-panel").className.includes("folded"), "summary: quiet panel folded");
ok(sumEl.innerHTML.includes('class="sumfig err"'), "summary: failed figure is red");

// all-healthy fleet -> the healthy line, nothing red
__setSlots({ totals: { running: 1, waiting: 0, reviewers_waiting: 0, capacity: 8 },
             harnesses: [ { harness: "opencode", cap: 5, running: 2, waiting: 0, free: 3 } ] });
__setGH({ now: 1, ready: true, base: "development", prod: "main", repo_url: "http://x",
          prs: [ { state: "OPEN", number: 7, title: "z" } ], stranded: 0 });
__setProjects([ { file: "c.json", title: "gamma", statuses: { merged: 4 } } ]);
api.renderSummary();
ok(sumLine.className === "okmsg" && sumLine.innerHTML.includes("healthy"),
   "summary: healthy fleet shows the healthy line");
ok(!sumLine.innerHTML.includes("failed") && !sumEl.innerHTML.includes('class="sumfig err"')
   && !sumEl.innerHTML.includes('class="sumfig warn"'), "summary: nothing red or amber when healthy");

// conflict fixture -> a visible alarm
__setProjects([ { file: "d.json", title: "delta", statuses: { conflict: 2 } } ]);
api.renderSummary();
ok(sumLine.innerHTML.includes("conflict") && sumLine.className === "err",
   "summary: conflict raises the alarm");
ok(sumEl.innerHTML.includes('class="sumfig err"'), "summary: conflict figure is red");

// a hostile project title must not reach the DOM as markup
__setProjects([ { file: "e.json", title: EVIL, statuses: { failed: 1 } } ]);
api.renderSummary();
ok(clean(sumLine.innerHTML), "escaping: summary line escapes project titles");
ok(sumLine.innerHTML.includes("&lt;img"), "escaping: hostile title rendered escaped, not dropped");
ok(clean(sumEl.innerHTML), "escaping: summary figures escape project titles");

// a hostile HARNESS name reaches the figure detail too — same esc() rule
__setSlots({ totals: { running: 5, waiting: 1, reviewers_waiting: 0, capacity: 8 },
             harnesses: [ { harness: EVIL, cap: 5, running: 5, waiting: 1, free: 0 } ] });
api.renderSummary();
ok(clean(sumEl.innerHTML), "escaping: summary figures escape harness names");
ok(sumEl.innerHTML.includes("&lt;img"), "escaping: hostile harness name rendered escaped, not dropped");

// failed/conflict is a TODAY signal: an abandoned failure from an earlier
// day is yesterday's news, not this morning's alarm
const daysAgo = n => new Date(Date.now() - n * 864e5).toISOString();
__setSlots({ totals: { running: 1, waiting: 0, reviewers_waiting: 0, capacity: 8 },
             harnesses: [ { harness: "opencode", cap: 5, running: 2, waiting: 0, free: 3 } ] });
__setProjects([ { file: "old.json", title: "stale-project", statuses: { failed: 2 }, last_activity: daysAgo(9) } ]);
api.renderSummary();
ok(sumLine.className === "okmsg", "summary: failure from an earlier day is not today's alarm");
__setProjects([ { file: "new.json", title: "fresh-project", statuses: { failed: 1 }, last_activity: new Date().toISOString() },
                { file: "old.json", title: "stale-project", statuses: { conflict: 1 }, last_activity: daysAgo(9) } ]);
api.renderSummary();
ok(sumLine.className === "err" && sumLine.innerHTML.includes("fresh-project")
   && !sumLine.innerHTML.includes("stale-project"),
   "summary: today's failure alarms, an earlier day's does not");

// click-to-expand must persist: renderSummary re-runs applyDisclosure on
// every 3 s poll, so an unfold that is not remembered is folded back shut
// within one tick — the drill-down has to survive the next disclosure pass
lsBacking.clear();
api.gotoPanel({ getAttribute: () => "slots-panel" });
ok(!document.querySelector("#slots-panel").className.includes("folded"),
   "gotoPanel: unfolds the target panel");
ok(JSON.parse(globalThis.localStorage.getItem("arc-panels") || "{}")["slots-panel"] === "open",
   "gotoPanel: records the open state, as toggleFold does");
api.renderSummary();
ok(!document.querySelector("#slots-panel").className.includes("folded"),
   "gotoPanel: drill-down survives the disclosure pass that follows a poll");

// ---- usage.html: success rate, waste, direction, alarming sort -------------
// Same DOM stub; usage.html's fillTotal() reads module-scope `let DAILY` for
// the direction arrows, so expose a setter from inside the module (the
// __setGH trick again).
const usrc = fs.readFileSync(new URL("../static/usage.html", import.meta.url), "utf8");
const ujs = usrc.slice(usrc.indexOf("<script>") + 8, usrc.lastIndexOf("</script>"));
const uext = [...usrc.matchAll(/<script[^>]+src="([^"]+)"/g)]
  .map(m => "static/" + m[1].replace(/^\//, ""))
  .filter(f => fs.existsSync(f))
  .map(f => fs.readFileSync(f, "utf8"))
  .join("\n");
const umod = new Function(uext + "\n" + ujs
  + "\nglobalThis.__setDaily = v => { DAILY = v; };"
  + "\nreturn {updateUI, fillTotal, updateModels, updateDaily, updateDriverEvents};");
const uapi = umod();

// Fixture: one model at 60% (ok 3 / err 1 / failed 1 of 5 requests), one at
// 100%; totals ok 12 / err 1 / failed 1 of 14 → 85.7% and waste ≈ 4000 tok.
const usage = {
  range: "24h",
  totals: { requests: 14, ok: 12, errors: 1, failed_attempts: 1, tokens: 48000,
            prompt_tokens: 30000, completion_tokens: 18000 },
  models: [
    { model: "steady-1", pretty: "Steady", family: "glm", source: "arc-pool",
      requests: 9, ok: 9, errors: 0, failed_attempts: 0, tokens: 30000,
      prompt_tokens: 20000, completion_tokens: 10000, avg_latency_ms: 1500, last_ts: null },
    { model: "wobbly-1", pretty: "Wobbly", family: "kimi", source: "driver:opencode",
      requests: 5, ok: 3, errors: 1, failed_attempts: 1, tokens: 18000,
      prompt_tokens: 10000, completion_tokens: 8000, avg_latency_ms: 900, last_ts: null },
  ],
  families: [
    { family: "glm", limit: 4, requests: 9, ok: 9, errors: 0, failed_attempts: 0, tokens: 30000, inflight: 1 },
    { family: "kimi", limit: 3, requests: 5, ok: 3, errors: 1, failed_attempts: 1, tokens: 18000, inflight: 2 },
  ],
  recent_driver_events: [ { type: "driver.error", model: "wobbly-1", task: "t1", error: "boom", ts: null } ],
};
__setDaily([
  { date: "2026-09-09", requests: 100, tokens: 1000, task_runs: 10 },
  { date: "2026-09-10", requests: 50, tokens: 2000, task_runs: 5 },
]);
uapi.updateUI(usage, { caps: {}, totals: null });

const grid = () => document.querySelector("#modelgrid").innerHTML;
ok(grid().includes("60%") && grid().includes("100%"), "usage: per-model success rate rendered");
ok(grid().includes('v-failed">1</b>'), "usage: failure count sits beside the rate");
ok(grid().includes("waste") && grid().includes("≈6.0k tok"), "usage: per-model waste figure rendered");
ok(count(grid(), "waste") === 1, "usage: no waste row for a clean model");
ok(grid().indexOf("Wobbly") < grid().indexOf("Steady"), "usage: alarm sort puts the 60% model first");
ok(document.querySelector("#sort-note").textContent.includes("success rate"), "usage: sort note explains alarm order");
ok(document.querySelector("#total-srate").textContent === "85.7%", "usage: total success rate rendered");
ok(document.querySelector("#total-srate-sub").textContent === "12 ok · 1 err · 1 failed", "usage: total rate breakdown rendered");
ok(document.querySelector("#total-waste").textContent === "1 failed · ≈4.0k tok", "usage: total waste figure rendered");
ok(document.querySelector("#total-tok").textContent === "48.0k", "usage: total tokens still rendered");
ok(document.querySelector("#split-prompt").style.width === "62.5%", "usage: prompt/completion split still rendered");
ok(document.querySelector("#range-label").textContent === "24H", "usage: range label rendered");
ok(document.querySelector("#dir-tok").textContent === "▲ +100%", "usage: token direction vs yesterday up");
ok(document.querySelector("#dir-req").textContent === "▼ -50%", "usage: request direction vs yesterday down");
ok(document.querySelector("#dir-tok").className === "dir up" && document.querySelector("#dir-req").className === "dir down",
   "usage: direction arrows carry up/down colour class");
const drows = document.querySelector("#daily-body").innerHTML;
ok(drows.includes("2026-09-10") && drows.indexOf("2026-09-10") < drows.indexOf("2026-09-09"),
   "usage: daily breakdown rendered, newest first");
ok(document.querySelector("#fam-body").innerHTML.includes("kimi"), "usage: family chips still rendered");
ok(document.querySelector("#drv-events").innerHTML.includes("boom"), "usage: driver events feed still rendered");
uapi.updateDaily([]);
ok(document.querySelector("#daily-box").style.display === "none", "usage: daily box hidden when no days");

// A range with no comparable preceding window shows no arrow, not a fake one.
uapi.fillTotal({ range: "1h", totals: usage.totals });
ok(document.querySelector("#dir-tok").textContent === "" && document.querySelector("#dir-req").textContent === "",
   "usage: 1h range has no direction arrow");

// Nothing below 90% → back to token order.
uapi.updateModels([
  { model: "small-1", pretty: "Small", family: "glm", source: "arc-pool",
    requests: 10, ok: 10, errors: 0, failed_attempts: 0, tokens: 9000,
    prompt_tokens: 6000, completion_tokens: 3000, avg_latency_ms: 100, last_ts: null },
  { model: "big-1", pretty: "Big", family: "kimi", source: "arc-pool",
    requests: 2, ok: 2, errors: 0, failed_attempts: 0, tokens: 50000,
    prompt_tokens: 30000, completion_tokens: 20000, avg_latency_ms: 100, last_ts: null },
], {}, 59000);
ok(grid().indexOf("Big") < grid().indexOf("Small"), "usage: healthy fleet sorts by tokens");
ok(document.querySelector("#sort-note").textContent.includes("tokens"), "usage: sort note explains token order");

// Escaping: model and driver-event fields are event-log data.
uapi.updateModels([
  { model: EVIL, pretty: EVIL, family: EVIL, source: EVIL,
    requests: 1, ok: 1, errors: 0, failed_attempts: 0, tokens: 100,
    prompt_tokens: 50, completion_tokens: 50, avg_latency_ms: null, last_ts: null },
], {}, 100);
ok(clean(grid()), "usage: model name markup does not reach the DOM as markup");
ok(grid().includes("&lt;img"), "usage: hostile model name rendered escaped, not dropped");
uapi.updateDriverEvents([ { type: EVIL, model: EVIL, role: EVIL, error: EVIL, ts: null } ]);
ok(clean(document.querySelector("#drv-events").innerHTML), "usage: driver-event fields escaped");

if (failures.length) {
  console.error(`ui_render: FAIL — ${failures.length}/${n} checks failed:`);
  for (const f of failures) console.error("  - " + f);
  process.exit(1);
}
console.log(`ui_render: PASS — ${n} checks`);
