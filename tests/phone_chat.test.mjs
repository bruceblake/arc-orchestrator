// Behavioural unit tests for the phone page (static/phone.html) Plan chat
// panel: speech input, session ids, chat start/poll, transcript + taskcard
// rendering, and the per-project "▶ run" action. Same approach as
// phone_render.test.mjs: stub the DOM, eval the page scripts, then drive
// the plan functions against fixtures shaped like the endpoints.
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
async function arun(fn, name) {
  n++;
  try { await fn(); } catch (e) { failures.push(name); console.log("FAIL " + name + " — " + (e && e.message)); }
}

// A rejected promise inside the page's async poll() must fail the run,
// not silently vanish after the summary line.
process.on("unhandledRejection", e => {
  console.error("unhandled rejection from the page script:", e);
  process.exit(1);
});

const realSetTimeout = globalThis.setTimeout.bind(globalThis);
function tick(ms) { return new Promise(r => realSetTimeout(r, ms || 20)); }

// ---- shared, per-test-mutable globals ------------------------------------
let fetchLog = [];
let impl = { status: 200, json: async () => ({}) };
let qsaMap = {};
let alertLog = [];
let confirmVal = true;

globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.fetch = async (url, opts) => { fetchLog.push({ url, opts }); return impl; };
globalThis.alert = m => { alertLog.push(m); };
globalThis.confirm = () => confirmVal;
globalThis.prompt = () => null;
globalThis.setInterval = () => 0;
globalThis.clearInterval = () => {};
globalThis.setTimeout = () => 0;
globalThis.clearTimeout = () => {};

function mk(id) {
  let html = "", value = "", placeholder = "", disabled = false;
  const subs = new Map();
  const classes = new Set();
  return {
    id, className: "", style: {}, onclick: null, scrollTop: 0,
    get value() { return value; }, set value(v) { value = String(v); },
    get placeholder() { return placeholder; }, set placeholder(v) { placeholder = String(v); },
    get disabled() { return disabled; }, set disabled(v) { disabled = !!v; },
    set innerHTML(v) { html = String(v); subs.clear(); },
    get innerHTML() { return html; },
    get textContent() { return html.replace(/<[^>]*>/g, ""); },
    set textContent(v) { html = String(v); },
    classList: {
      add: c => classes.add(c),
      remove: c => classes.delete(c),
      toggle: c => classes.has(c) ? (classes.delete(c), false) : (classes.add(c), true),
      contains: c => classes.has(c),
    },
    setAttribute() {}, removeAttribute() {}, getAttribute: () => "",
    appendChild() {},
    querySelectorAll: () => [],
    querySelector: s => { if (!subs.has(s)) subs.set(s, mk(id + "/" + s)); return subs.get(s); },
  };
}

// ---- load the page and its external scripts --------------------------------
const src = fs.readFileSync("static/phone.html", "utf8");
const EXTERNALS = [...src.matchAll(/<script[^>]+src="([^"]+)"/g)]
  .map(m => (m[1][0] === "/" ? "static" + m[1] : m[1]))
  .filter(p => fs.existsSync(p))
  .map(p => fs.readFileSync(p, "utf8"))
  .join("\n");
const JS = src.slice(src.indexOf("<script>") + 8, src.lastIndexOf("</script>"));
const INPUT_PLACEHOLDER = (src.match(/id="plan-input"[^>]*placeholder="([^"]*)"/) || [])[1] || "";

function loadPage(withSR) {
  fetchLog = [];
  alertLog = [];
  impl = { status: 200, json: async () => ({}) };
  qsaMap = {};
  confirmVal = true;
  const els = new Map();
  function getEl(id) { if (!els.has(id)) { const e = mk(id); if (id === "plan-input") e.placeholder = INPUT_PLACEHOLDER; els.set(id, e); } return els.get(id); }
  const document = {
    querySelector: s => (s && s[0] === "#" ? getEl(s.slice(1)) : mk(s)),
    querySelectorAll: sel => qsaMap[sel] || [],
    getElementById: getEl,
    createElement: t => mk(t),
    addEventListener() {}, removeEventListener() {},
    hidden: false, body: mk("body"), documentElement: mk("html"),
  };
  const location = { hash: "", search: "", href: "http://localhost:8787/phone.html", reload() {} };
  const window = {
    addEventListener() {}, removeEventListener() {},
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    location,
  };
  if (withSR) {
    window.SpeechRecognition = class {
      constructor() { this.continuous = false; this.interimResults = false; this.lang = ""; globalThis.__lastRec = this; }
      start() {} stop() {}
    };
    window.webkitSpeechRecognition = window.SpeechRecognition;
  }
  globalThis.document = document;
  globalThis.window = window;
  globalThis.location = location;
  const api = new Function(EXTERNALS + "\n" + JS + "\n" +
    "return {S, j, jpost, handleSpeechResult, setMicLive, getPlanSession," +
    " sendPlanMessage, pollPlan, renderPlanTranscript, renderPlanTaskcard," +
    " loadPlanHistory, loadPlanRepos, onPlanRepoChange, wireRun, runProject, init};")();
  const el = id => document.getElementById(id);
  return {
    api, el,
    setImpl: o => { impl = o; },
    setQsa: m => { qsaMap = m; },
    setConfirm: v => { confirmVal = v; },
    fetchLog: () => fetchLog,
    alertLog: () => alertLog,
  };
}

// ---- 1. microphone visibility depends on SpeechRecognition support --------
ok(loadPage(false).el("plan-mic").style.display === "none", "mic hidden when SpeechRecognition is unsupported");
ok(loadPage(true).el("plan-mic").style.display === "inline-block", "mic shown when SpeechRecognition is supported");

// ---- 2. speech result handling ---------------------------------------------
{
  const p = loadPage(true);
  const input = p.el("plan-input");
  input.value = "";
  run(() => p.api.handleSpeechResult({ resultIndex: 0, results: [{ 0: { transcript: "hello world" }, isFinal: true }] }), "handleSpeechResult() final");
  ok(input.value === "hello world", "final transcript lands in the input value");
  ok(p.el("plan-interim").textContent === "", "interim element is empty when there is no interim speech");
  run(() => p.api.handleSpeechResult({ resultIndex: 0, results: [{ 0: { transcript: "more" }, isFinal: true }] }), "handleSpeechResult() second final");
  ok(input.value === "hello world more", "a later final appends to the existing value");
  run(() => p.api.handleSpeechResult({ resultIndex: 0, results: [{ 0: { transcript: "partial" }, isFinal: false }] }), "handleSpeechResult() interim");
  ok(input.value === "hello world more", "interim text stays out of the committed value");
  ok(p.el("plan-interim").textContent.includes("partial"), "interim text goes to the dedicated interim element");
  ok(input.placeholder === "Ask the planner", "interim speech never pollutes the input placeholder");
  input.value = "  x";
  run(() => p.api.handleSpeechResult({ resultIndex: 0, results: [{ 0: { transcript: "y" }, isFinal: true }] }), "handleSpeechResult() final after manual edit");
  ok(input.value === "  x y", "a final appends to, not overwrites, manually typed text");
}

// ---- 3. mic live indicator ---------------------------------------------------
{
  const p = loadPage(true);
  const mic = p.el("plan-mic");
  run(() => p.api.setMicLive(true), "setMicLive(true)");
  ok(mic.textContent === "🔴", "mic live shows the red dot");
  ok(mic.classList.contains("mic-live"), "mic live gets the .mic-live class");
  const input = p.el("plan-input");
  input.value = "keep me";
  run(() => p.api.setMicLive(false), "setMicLive(false)");
  ok(mic.textContent === "🎤", "mic idle returns to the mic glyph");
  ok(!mic.classList.contains("mic-live"), "mic idle drops the .mic-live class");
  ok(input.value === "keep me", "returning to idle does not discard typed text");
  ok(p.el("plan-input").placeholder === "Ask the planner", "mic idle leaves the placeholder alone");
  run(() => { p.el("plan-interim").textContent = "partial"; p.api.setMicLive(false); }, "setMicLive(false) clears the interim element");
  ok(p.el("plan-interim").textContent === "", "mic idle clears the interim element");
}

// ---- 4. session id from the repo path ----------------------------------------
{
  const p = loadPage(false);
  const repo = p.el("plan-repo");
  repo.value = "/home/x/tasks/demo.json";
  ok(p.api.getPlanSession() === "plan-demo-json", "getPlanSession() slugs the repo path");
  repo.value = "/a/b/other.json";
  ok(p.api.getPlanSession() === "plan-other-json", "getPlanSession() uses the last path segment");
  repo.value = "__new__";
  ok(p.api.getPlanSession() === null, "getPlanSession() rejects the new-repo sentinel");
  repo.value = "";
  ok(p.api.getPlanSession() === null, "getPlanSession() rejects an empty repo");
}

// ---- 5. sendPlanMessage happy path ------------------------------------------
{
  const p = loadPage(false);
  const input = p.el("plan-input");
  const repo = p.el("plan-repo");
  repo.value = "/home/x/tasks/demo.json";
  input.value = "  build the thing  ";
  p.setImpl({ status: 200, json: async () => ({ turns: [], running: false }) });
  await arun(async () => { p.api.sendPlanMessage(); await tick(20); }, "sendPlanMessage() happy path");
  const calls = p.fetchLog().filter(f => f.url === "/api/chat/start");
  ok(calls.length === 1, "sendPlanMessage() posts to /api/chat/start once");
  const body = JSON.parse(calls[0].opts.body);
  ok(body.session === "plan-demo-json", "chat start body carries the plan session");
  ok(body.repo === "/home/x/tasks/demo.json", "chat start body carries the repo path");
  ok(body.message === "build the thing", "chat start body carries the trimmed message");
  ok(input.value === "", "sendPlanMessage() clears the input");
  ok(p.el("plan-send").disabled === false, "send button re-enabled after the poll settles");
}

// ---- 6. sendPlanMessage guards ----------------------------------------------
{
  const p = loadPage(false);
  const input = p.el("plan-input");
  const repo = p.el("plan-repo");
  repo.value = "/home/x/tasks/demo.json";
  input.value = "   ";
  p.api.sendPlanMessage();
  await tick(10);
  ok(p.fetchLog().filter(f => f.url === "/api/chat/start").length === 0, "whitespace-only message is a no-op");
  input.value = "go";
  repo.value = "__new__";
  p.api.sendPlanMessage();
  await tick(10);
  ok(p.alertLog().includes("Select repo"), "new-repo sentinel blocks with an alert");
  ok(p.el("plan-send").disabled === false, "blocked send never disables the button");
  repo.value = "/home/x/tasks/demo.json";
  p.setImpl({ status: 409, json: async () => ({ turns: [{ role: "user", text: "already running" }], running: true }) });
  input.value = "go";
  p.api.sendPlanMessage();
  await tick(10);
  ok(!p.alertLog().includes("Error starting chat"), "409 chat start attaches instead of alerting");
  ok(p.fetchLog().filter(f => f.url.includes("/api/chat/poll")).length >= 1, "409 chat start polls the already-running turn");
  ok(p.el("plan-send").disabled === true, "409 attach keeps the button busy for a running turn");

  p.setImpl({ status: 200, json: async () => ({ turns: [], running: false }) });
  const before = p.fetchLog().filter(f => f.url === "/api/chat/start").length;
  p.el("plan-send").disabled = true;
  input.value = "go";
  p.api.sendPlanMessage();
  await tick(10);
  ok(p.fetchLog().filter(f => f.url === "/api/chat/start").length === before, "busy guard: a disabled send never posts");
}

// ---- 7. pollPlan renders turns, taskcard and escaping ------------------------
{
  const p = loadPage(false);
  p.el("plan-repo").value = "/home/x/tasks/demo.json";
  p.el("plan-input").value = "go";
  p.setImpl({ status: 200, json: async () => ({
    turns: [
      { role: "user", ts: 1750000000, text: "build the app" },
      { role: "assistant", ts: 1750000005, text: "Here is the plan.", taskfile: "demo.json" },
    ], running: false,
  }) });
  await arun(async () => { p.api.sendPlanMessage(); await tick(20); }, "sendPlanMessage() then pollPlan()");
  const tx = p.el("plan-transcript");
  ok(tx.className === "plan-tx", "transcript container enters plan-tx mode");
  ok(tx.innerHTML.includes("build the app"), "transcript renders the user turn");
  ok(tx.innerHTML.includes("Here is the plan."), "transcript renders the assistant turn");
  ok(tx.innerHTML.includes("plan-turn user") && tx.innerHTML.includes("plan-turn assistant"), "turns carry their role classes");
  ok(tx.innerHTML.includes(new Date(1750000000 * 1000).toLocaleTimeString()), "transcript renders epoch-seconds timestamps");
  ok(!tx.innerHTML.includes("Invalid Date"), "transcript shows a real timestamp, not invalid");
   ok(p.el("plan-taskcard").innerHTML.includes('data-run-file="demo.json"'), "taskcard renders the run button for the latest plan");
  ok(p.el("plan-send").disabled === false, "poll resets busy when not running");

  p.setImpl({ status: 200, json: async () => ({
    turns: [
      { role: "user", ts: "", text: EVIL },
      { role: "assistant", ts: "", text: "fine", taskfile: "x.json" },
    ], running: false,
  }) });
  await arun(async () => { p.api.pollPlan(); await tick(20); }, "pollPlan() with hostile text");
  ok(clean(tx.innerHTML), "transcript escapes hostile turn text");

  p.setImpl({ status: 200, json: async () => ({
    turns: [{ role: "assistant", ts: "", text: "task failed", error: EVIL }], running: false,
  }) });
  await arun(async () => { p.api.pollPlan(); await tick(20); }, "pollPlan() with an error turn");
  ok(tx.innerHTML.includes("task failed"), "error turn renders its message");
  ok(tx.innerHTML.includes("plan-turn err"), "error turn carries the err class");
  ok(clean(tx.innerHTML), "transcript escapes the error detail text");

  const p3 = loadPage(false);
  p3.el("plan-repo").value = "/home/x/tasks/demo.json";
  p3.setImpl({ status: 200, json: async () => ({ turns: [{ role: "user", text: "hi" }], running: false }) });
  await arun(async () => { p3.api.pollPlan(); await tick(20); }, "pollPlan() with no plan");
  ok(p3.el("plan-taskcard").innerHTML === "", "taskcard empty when no assistant plan");

  const p4 = loadPage(false);
  p4.el("plan-repo").value = "/home/x/tasks/demo.json";
  p4.setImpl({ status: 500, json: async () => { throw new Error("net"); } });
  await arun(async () => { p4.api.pollPlan(); await tick(20); }, "pollPlan() fetch error");
  ok(p4.el("plan-send").disabled === false, "poll fetch error un-busies the send button");
  ok(p4.el("plan-transcript").className !== "plan-tx", "poll fetch error does not wedge into plan-tx mode");
}

// ---- 8. loadPlanHistory empty state ------------------------------------------
{
  const p = loadPage(false);
  p.el("plan-repo").value = "/home/x/tasks/demo.json";
  p.setImpl({ status: 200, json: async () => ({ turns: [], running: false }) });
  await arun(async () => { p.api.loadPlanHistory(); await tick(20); }, "loadPlanHistory() empty");
  ok(p.el("plan-transcript").className === "empty", "empty transcript enters the empty state");
  ok(p.el("plan-transcript").innerHTML === "no chat yet", "empty transcript shows the placeholder");
}

// ---- 9. runProject -------------------------------------------------------------
{
  const p = loadPage(false);
  p.setConfirm(true);
  p.setImpl({ status: 200, json: async () => ({ ok: true }) });
  await arun(async () => { p.api.runProject("demo.json"); await tick(20); }, "runProject() confirmed");
  const calls = p.fetchLog().filter(f => f.url === "/api/projects/run");
  ok(calls.length === 1, "runProject() posts /api/projects/run");
  ok(JSON.parse(calls[0].opts.body).file === "demo.json", "run body carries the file");
  ok(p.alertLog().includes('{"ok":true}'), "runProject() alerts the JSON body");

  p.setConfirm(false);
  p.setImpl({ status: 200, json: async () => ({ ok: true }) });
  await arun(async () => { p.api.runProject("demo.json"); await tick(10); }, "runProject() declined");
  ok(p.fetchLog().filter(f => f.url === "/api/projects/run").length === 1, "runProject() declined does not post");

  p.setConfirm(true);
  p.setImpl({ status: 500, json: async () => ({}) });
  await arun(async () => { p.api.runProject("demo.json"); await tick(10); }, "runProject() server error");
  ok(p.alertLog().includes("Project run failed"), "runProject() non-200 alerts the failure");
}

// ---- 10. wireRun wires the run button ------------------------------------------
{
  const p = loadPage(false);
  const b1 = { getAttribute: a => (a === "data-run-file" ? "demo.json" : "") };
  const b2 = { getAttribute: a => (a === "data-run-file" ? "other.json" : "") };
  p.setQsa({ "#projects [data-run-file]": [b1, b2] });
  p.setConfirm(true);
  p.setImpl({ status: 200, json: async () => ({ ok: true }) });
  await arun(async () => {
    p.api.wireRun("#projects");
    b1.onclick();
    await tick(20);
  }, "wireRun() wires the run button");
  const calls = p.fetchLog().filter(f => f.url === "/api/projects/run");
  ok(calls.length === 1, "wireRun() button posts /api/projects/run");
  ok(JSON.parse(calls[0].opts.body).file === "demo.json", "wired run posts the file from data-run-file");
}

// ---- 11. repo switch in the plan view ----------------------------------------
{
  const p = loadPage(false);
  p.setImpl({ status: 200, json: async () => ({ repos: [{ name: "demo", path: "/r/demo.json" }], turns: [], running: false }) });
  await arun(async () => { p.api.loadPlanRepos(); await tick(20); }, "loadPlanRepos()");
  const repo = p.el("plan-repo");
  ok(repo.innerHTML.includes('value="/r/demo.json"'), "loadPlanRepos() populates repo options");
  ok(repo.value === "/r/demo.json", "loadPlanRepos() selects the first repo by default");
  repo.value = "/r/other.json";
  const before = p.fetchLog().filter(f => f.url.includes("/api/chat/poll")).length;
  repo.onchange();
  await tick(20);
  const after = p.fetchLog().filter(f => f.url.includes("/api/chat/poll")).length;
  ok(after > before, "repo switch reloads the plan history");
  ok(!p.alertLog().includes("Repo exists"), "switching to a real repo does not prompt");
  ok(!p.alertLog().includes("Select repo"), "repo switch does not block on a select alert");
}

// ---- 12. repo creation slugs the name and catches failures --------------------
{
  const p = loadPage(false);
  const repo = p.el("plan-repo");
  repo.innerHTML = '<option value="__new__">new repo…</option>';
  repo.value = "__new__";
  globalThis.prompt = () => "My New Repo";
  p.setImpl({ status: 200, json: async () => ({ name: "my-new-repo", path: "/r/my-new-repo.json" }) });
  await arun(async () => { p.api.onPlanRepoChange(); await tick(20); }, "onPlanRepoChange() create");
  globalThis.prompt = () => null;
  const calls = p.fetchLog().filter(f => f.url === "/api/repos/create");
  ok(calls.length === 1, "create posts /api/repos/create");
  ok(JSON.parse(calls[0].opts.body).name === "my-new-repo", "create body carries the kebab-slugged name");
  ok(!p.fetchLog().some(f => f.url === "/api/repos/create" && JSON.parse(f.opts.body).name === "My New Repo"), "create never posts the raw un-slugged name");

  const p2 = loadPage(false);
  const repo2 = p2.el("plan-repo");
  repo2.innerHTML = '<option value="__new__">new repo…</option>';
  repo2.value = "__new__";
  globalThis.prompt = () => "My New Repo";
  p2.setImpl({ status: 409, json: async () => ({ error: "exists" }) });
  await arun(async () => { p2.api.onPlanRepoChange(); await tick(20); }, "onPlanRepoChange() create non-200");
  globalThis.prompt = () => null;
  ok(p2.alertLog().includes("Repo exists"), "create non-200 alerts Repo exists");

  const p3 = loadPage(false);
  const repo3 = p3.el("plan-repo");
  repo3.innerHTML = '<option value="__new__">new repo…</option>';
  repo3.value = "__new__";
  globalThis.prompt = () => "My New Repo";
  p3.setImpl({ status: 200, json: async () => { throw new Error("boom"); } });
  await arun(async () => { p3.api.onPlanRepoChange(); await tick(20); }, "onPlanRepoChange() create rejected");
  globalThis.prompt = () => null;
  ok(p3.alertLog().includes("Repo exists"), "create network/json failure alerts Repo exists");
}

// ---- done ---------------------------------------------------------------------
await tick(30);
console.log(failures.length ? failures.length + " phone-chat check(s) failed" : "phone chat: " + n + " checks passed");
process.exit(failures.length ? 1 : 0);
