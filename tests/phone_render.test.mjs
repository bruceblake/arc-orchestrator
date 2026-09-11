// Behavioural unit tests for the phone page (static/phone.html).
// Same approach as ui_render.test.mjs: stub the DOM, eval the page's
// scripts, then drive the render/toggle functions against fixture
// payloads shaped like the five JSON endpoints. Also parses the page
// CSS to enforce the 44x44px minimum tap-target floor.
import fs from "node:fs";

const EVIL = '<img src=x onerror=alert(1)>';
const clean = s => !s.includes("<img");

const failures = [];
let n = 0;
function ok(cond, name) {
  n++;
  if (cond) return;
  failures.push(name);
  console.log("FAIL " + name);
}
function run(fn, name) {
  n++;
  try { fn(); } catch (e) { failures.push(name); console.log("FAIL " + name + " — " + (e && e.message)); }
}

// A rejected promise inside the page's async poll() must fail the run,
// not silently vanish after the summary line.
process.on("unhandledRejection", e => {
  console.error("unhandled rejection from the page script:", e);
  process.exit(1);
});

// ---- minimal DOM stub ---------------------------------------------------
const els = new Map();
function mk(id) {
  let html = "";
  return {
    id: id, className: "", style: {}, onclick: null,
    set innerHTML(v) { html = String(v); }, get innerHTML() { return html; },
    get textContent() { return html.replace(/<[^>]*>/g, ""); },
    set textContent(v) { html = String(v); },
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    setAttribute() {}, removeAttribute() {}, getAttribute: () => "",
    appendChild() {}, querySelectorAll: () => [],
  };
}
function getEl(id) {
  if (!els.has(id)) els.set(id, mk(id));
  return els.get(id);
}
const document = {
  querySelector: s => (s && s[0] === "#" ? getEl(s.slice(1)) : mk(s)),
  querySelectorAll: () => [],
  getElementById: getEl,
  createElement: t => mk(t),
  addEventListener() {}, removeEventListener() {},
  hidden: false, body: mk("body"), documentElement: mk("html"),
};
const location = { hash: "", search: "", href: "http://localhost:8787/phone.html", reload() {} };
const window = {
  addEventListener() {}, removeEventListener() {},
  matchMedia: () => ({ matches: false, addEventListener() {} }),
  location: location,
};
const realSetTimeout = globalThis.setTimeout.bind(globalThis);
globalThis.document = document;
globalThis.window = window;
globalThis.location = location;
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.fetch = async () => ({ json: async () => ({}) });
globalThis.setInterval = () => 0;
globalThis.clearInterval = () => {};
globalThis.setTimeout = () => 0;
globalThis.clearTimeout = () => {};

// ---- load the page and its external scripts -----------------------------
const src = fs.readFileSync("static/phone.html", "utf8");
const externals = [...src.matchAll(/<script[^>]+src="([^"]+)"/g)]
  .map(m => (m[1][0] === "/" ? "static" + m[1] : m[1])) // web root maps to static/
  .filter(p => fs.existsSync(p))
  .map(p => fs.readFileSync(p, "utf8"))
  .join("\n");
const js = src.slice(src.indexOf("<script>") + 8, src.lastIndexOf("</script>"));
const api = new Function(externals + "\n" + js + "\n" +
  "return {S: S, agentCard: agentCard, projectCard: projectCard, prCard: prCard," +
  " modelCard: modelCard, harnessCard: harnessCard, limitsCard: limitsCard," +
  " renderNow: renderNow, renderProjects: renderProjects, renderPRs: renderPRs," +
  " renderFleet: renderFleet, renderMeta: renderMeta, renderAll: renderAll," +
  " toggle: toggle, showView: showView, familyOf: familyOf};")();
const el = id => document.getElementById(id);

// ---- fixtures shaped like the live endpoints -----------------------------
const GH_NOW = 1757500000; // seconds, as /api/github reports
const iso = s => new Date((GH_NOW - s) * 1000).toISOString();
const REQ1 = "driver/opencode:GLM-5.3:implementer:t1";
const FIX = {
  summary: { ts: GH_NOW, limits: { "GLM-5.3": 4, "Kimi-K3": 3 } },
  agents: {
    now: GH_NOW,
    agents: [
      { req_id: REQ1, family: "glm", model: "GLM-5.3", role: "implementer", task: "t1", harness: "opencode", elapsed_s: 1250, last_event_s: 30, stalled: false, transcript: "t1-implementer-1.jsonl" },
      { req_id: "driver/kimi:Kimi-K3:reviewer:t2", family: "kimi", model: "Kimi-K3", role: "reviewer", task: "t2", harness: "kimi", elapsed_s: 90, last_event_s: 400, stalled: true, transcript: "t2-reviewer-1.jsonl" },
    ],
    runs: [{ taskfile: "/home/x/tasks/a.json", pid: 4242 }, { taskfile: "/home/x/tasks/b.json", pid: 4243 }],
  },
  projects: [{
    file: "demo.json", title: "Demo project", n_tasks: 2, task_ids: ["t1", "t2"],
    models: ["GLM-5.3", "gpt-oss-120b"], reviewers: ["kimi"],
    statuses: { merged: 1, running: 1 },
    dag: {
      nodes: [
        { id: "t1", status: "merged", tokens: 1234, seconds: 65, attempts: 1 },
        { id: "t2", status: "running", live: true, tokens: 500, seconds: 30, attempts: 2 },
      ],
      edges: [{ src: "t1", dst: "t2" }],
    },
    progress: { done: 1, total: 2 }, tokens: 1734, seconds: 95,
    done_tokens: 1734, done_seconds: 95, live_tokens: 0, live_seconds: 0,
    errors: [{ id: "t2", status: "failed", error: "gate timeout" }], archived: false, phase: "implement", task_progress: {},
    run_pid: 4242, active: true, last_activity: iso(30), orphan_rows: 0,
  }],
  gh: {
    now: GH_NOW, ready: true, base: "development", prod: "main", stranded: 0,
    prs: [
      {
        number: 7, title: "task(t1): do the thing", state: "OPEN",
        headRefName: "task/t1", baseRefName: "development",
        additions: 120, deletions: 12, url: "http://example.com/7",
        isDraft: false, mergedAt: null, task: "t1",
        approvals: ["glm"], reviewers: ["glm"],
        rounds: [
          { round: 1, approved: false, approvals: [], reviewers: ["glm"], issues: 2, detail: ["missing test", "naming"], inconclusive: false, crashed: null },
          { round: 2, approved: true, approvals: ["glm"], reviewers: ["glm"], issues: 0, detail: [], inconclusive: false, crashed: null },
        ],
      },
      {
        number: 8, title: "task(t0): earlier work", state: "MERGED",
        headRefName: "task/t0", baseRefName: "development",
        additions: 40, deletions: 4, url: "http://example.com/8",
        isDraft: false, mergedAt: iso(600), task: "t0",
        approvals: ["glm"], reviewers: ["glm"], rounds: [],
      },
    ],
  },
  queue: {
    running: [], waiting: [],
    models: [
      { model: "GLM-5.3", pretty: "GLM 5.3", cap: 4, running: 1, waiting: 0, free: 3, reviewers_waiting: 0 },
      { model: "Kimi-K3", pretty: "Kimi K3", cap: 3, running: 0, waiting: 1, free: 3, reviewers_waiting: 1 },
    ],
    harnesses: [
      { harness: "opencode", cap: 5, running: 1, waiting: 0, free: 4 },
      { harness: "kimi", cap: 3, running: 0, waiting: 0, free: 3 },
    ],
    totals: { running: 1, waiting: 1, reviewers_waiting: 1, capacity: 22 },
  },
};

// ---- 1. fixture render ----------------------------------------------------
Object.assign(api.S, { view: "now", stale: false, open: { now: "", projects: "", prs: "", fleet: "" } }, FIX);
run(() => api.renderAll(), "renderAll() on fixture payloads");
ok(el("figs").innerHTML.split('class="fig"').length - 1 === 4, "figs row renders four figures");
ok(el("agents").innerHTML.includes("GLM 5.3 · implementer"), "agent card shows model and role");
ok(el("agents").innerHTML.includes("t2 · STALLED"), "stalled agent is flagged");
ok(el("nowsub").textContent.includes("2 harness runs today"), "harness-run count in the sub line");
ok(el("alert").innerHTML.includes("Kimi K3 stalled on t2"), "stall surfaces in the alert line");
ok(el("alert").style.display === "block", "alert is visible while red lines exist");
ok(el("projects").innerHTML.includes("Demo project"), "project card shows title");
ok(el("projects").innerHTML.includes("demo.json · 1/2 · running"), "project sub shows file, progress, running");
ok(el("prssub").textContent === "2 tracked · base development", "pr sub line summarizes tracked PRs");
ok(el("prs").innerHTML.includes("#7") && el("prs").innerHTML.includes("OPEN"), "pr card shows number and state");
ok(el("prs").innerHTML.includes("ok 1"), "pr card shows approval count");
ok(el("fleet").innerHTML.includes("GLM 5.3"), "fleet lists model");
ok(el("fleet").innerHTML.includes("1 running · 0 waiting · cap 4"), "model card shows queue numbers");
ok(el("fleet").innerHTML.includes("opencode harness"), "fleet lists harness");
ok(el("fleet").innerHTML.includes("Account API caps"), "fleet lists the account caps card");
ok(el("fleetsub").textContent === "capacity 22 · 1 running · 1 waiting", "fleet sub line shows totals");
ok(el("stale").style.display === "none", "stale badge hidden while fresh");

// ---- 2. progressive disclosure ---------------------------------------------
run(() => api.toggle("projects", "demo.json"), "toggle() opens a project");
ok(el("projects").innerHTML.includes('class="card open"'), "open project gets .open");
ok(el("projects").innerHTML.includes('class="pbar"'), "open project shows the progress bar");
ok(el("projects").innerHTML.includes("harness time"), "open project shows harness time");
ok(el("projects").innerHTML.includes("gate timeout"), "error entries render their error text");
ok(!el("projects").innerHTML.includes("[object Object]"), "error dicts never stringify to [object Object]");
ok(el("projects").innerHTML.includes("merged") && el("projects").innerHTML.includes("live"), "open project lists dag nodes");
run(() => api.toggle("projects", "demo.json"), "toggle() closes the project again");
ok(!el("projects").innerHTML.includes('class="card open"'), "closed project loses .open");
run(() => api.toggle("now", REQ1), "toggle() opens an agent");
ok(el("agents").innerHTML.includes("heartbeat"), "open agent shows heartbeat");
ok(el("agents").innerHTML.includes("t1-implementer-1.jsonl"), "open agent names its transcript file");
ok(el("agents").innerHTML.includes('class="tx"'), "open agent renders an inline transcript pane");
ok(!el("agents").innerHTML.includes('href="t1-implementer'), "transcript is never linked as a bare filename");
run(() => api.showView("fleet"), "showView() switches view");
ok(el("view-fleet").className === "view on" && el("nav-fleet").className === "tap on", "fleet view and nav light up");
ok(el("view-now").className === "view" && el("nav-now").className === "tap", "previous view and nav dim");

// ---- 3. age formatting (nowOf() must use /api/github's seconds) ------------
const oldP = JSON.parse(JSON.stringify(FIX.projects[0]));
oldP.active = false;
oldP.last_activity = iso(120);
ok(api.projectCard(oldP).includes("2m ago"), "idle project shows age since last activity");
run(() => api.toggle("prs", "8"), "toggle() opens a merged PR");
ok(el("prs").innerHTML.includes("10m ago"), "merged PR shows merge age");
run(() => api.toggle("prs", "7"), "toggle() switches to the open PR");
ok(el("prs").innerHTML.includes("round 2") && el("prs").innerHTML.includes("approved"), "pr rounds render with verdicts");
ok(el("prs").innerHTML.includes("changes requested"), "rejected round is named");
ok(el("prs").innerHTML.includes("2 issues"), "rejected round shows its issue count");
ok(el("prs").innerHTML.includes('href="http://example.com/7"'), "open PR links to github");

// ---- 4. escaping ------------------------------------------------------------
const evilP = JSON.parse(JSON.stringify(FIX.projects[0]));
evilP.title = EVIL;
evilP.dag.nodes[0].id = EVIL;
ok(clean(api.projectCard(evilP)), "project card escapes title and node ids");
const evilPr = JSON.parse(JSON.stringify(FIX.gh.prs[0]));
evilPr.title = EVIL;
api.S.open.prs = "7";
ok(clean(api.prCard(evilPr)), "pr card escapes title");
const evilA = JSON.parse(JSON.stringify(FIX.agents.agents[0]));
evilA.task = EVIL;
evilA.req_id = EVIL;
ok(clean(api.agentCard(evilA)), "agent card escapes task and req_id");
const stalledEvil = JSON.parse(JSON.stringify(FIX.agents.agents[1]));
stalledEvil.model = EVIL;
api.S.agents = { now: GH_NOW, agents: [stalledEvil], runs: [] };
run(() => api.renderNow(), "renderNow() with a hostile stalled agent");
ok(clean(el("alert").innerHTML), "alert line escapes hostile model names");

// ---- 5. empty payloads --------------------------------------------------------
Object.assign(api.S, { summary: {}, agents: {}, projects: [], gh: {}, queue: {}, stale: false });
run(() => api.renderAll(), "renderAll() on empty payloads");
ok(el("projects").innerHTML.includes("no projects yet"), "empty projects placeholder");
ok(el("prssub").textContent === "github status unavailable", "github-down sub line");
ok(el("agents").innerHTML.includes("no agents running"), "empty agents placeholder");
ok(el("alert").style.display === "none", "alert hidden without red lines");
ok(el("fleet").innerHTML.includes("queue status unavailable"), "empty fleet placeholder");

// ---- 6. CSS tap-target floor (>= 44x44px) -------------------------------------
const css = src.slice(src.indexOf("<style>") + 7, src.indexOf("</style>")).replace(/\/\*[\s\S]*?\*\//g, " ");
const rules = [];
css.split("}").forEach(frag => {
  const brace = frag.lastIndexOf("{");
  if (brace < 0) return;
  let sel = frag.slice(0, brace);
  if (sel.indexOf("{") >= 0) sel = sel.slice(sel.lastIndexOf("{") + 1); // unwrap @media
  const decls = frag.slice(brace + 1);
  sel.split(",").forEach(s => {
    s = s.trim();
    if (s) rules.push({ sel: s, decls: decls });
  });
});
const subject = sel => sel.split(/[\s>+~]+/).filter(Boolean).pop() || "";
const isTap = r =>
  /^\.tap($|\.|:)/.test(subject(r.sel)) ||
  /^\.card-head($|\.|:)/.test(subject(r.sel)) ||
  (/^button/.test(subject(r.sel)) && /\.bnav/.test(r.sel));
const minPx = (decls, prop) => {
  const m = decls.match(new RegExp(prop + ":\\s*([\\d.]+)px"));
  return m ? [parseFloat(m[1])] : [];
};
const tapRules = rules.filter(isTap);
ok(tapRules.length >= 3, "tap-target rules exist");
tapRules.forEach(r => {
  minPx(r.decls, "min-width").concat(minPx(r.decls, "min-height")).forEach(v => {
    ok(v >= 44, r.sel + " keeps >= 44px tap targets");
  });
});
[
  [".tap", r => /^\.tap($|\.|:)/.test(subject(r.sel))],
  [".bnav button", r => /^button/.test(subject(r.sel)) && /\.bnav/.test(r.sel)],
  [".card-head", r => /^\.card-head($|\.|:)/.test(subject(r.sel))],
].forEach(([name, match]) => {
  const rs = tapRules.filter(match);
  ok(rs.length > 0 &&
    rs.some(r => (minPx(r.decls, "min-width")[0] || 0) >= 44 && (minPx(r.decls, "min-height")[0] || 0) >= 44),
    name + " declares a >= 44px square tap target");
});

// ---- done ---------------------------------------------------------------------
// Let poll()'s continuation (over the stubbed empty payloads) settle; a
// throw there trips the unhandledRejection handler above.
await new Promise(r => realSetTimeout(r, 25));
console.log(failures.length ? failures.length + " phone-page check(s) failed" : "phone page: " + n + " checks passed");
process.exit(failures.length ? 1 : 0);