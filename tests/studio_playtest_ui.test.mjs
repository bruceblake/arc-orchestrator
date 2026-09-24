// Behavioural tests for the Studio panel's human-playtest view
// (static/panels/studio.js: studioPlaytestView + the delegated click handler).
// No framework, no server: `node tests/studio_playtest_ui.test.mjs`; exits
// non-zero on any failure. Same loader as tests/ui_render.test.mjs — a DOM
// stub, then every <script src> of index.html plus its inline script evaluated
// with new Function.
import fs from "node:fs";

const els = new Map();
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
// Capture document listeners so the delegated click handler can be driven.
const listeners = {};
globalThis.document = {
  querySelector: sel => { const id = sel.replace(/^#/, ""); if (!els.has(id)) els.set(id, mk(id)); return els.get(id); },
  querySelectorAll: () => [],
  createElement: () => mk("new"),
  addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
  removeEventListener: () => {},
  body: mk("body"), documentElement: mk("html"),
  activeElement: null,
};
globalThis.window = { addEventListener: () => {}, removeEventListener: () => {},
                      matchMedia: () => ({matches:false, addEventListener(){}}),
                      location: {hash: "", search: ""} };
const lsBacking = new Map();
globalThis.localStorage = { getItem: k => (lsBacking.has(k) ? lsBacking.get(k) : null),
                            setItem: (k, v) => lsBacking.set(k, String(v)),
                            removeItem: k => lsBacking.delete(k) };
globalThis.location = { hash: "", search: "", href: "http://localhost:8787/" };
globalThis.history = { replaceState(){}, pushState(){} };
Object.defineProperty(globalThis, "navigator", {value: {clipboard: {writeText: async () => {}}}, configurable: true});
globalThis.confirm = () => false;
const alerts = [];
globalThis.alert = m => alerts.push(String(m));
let promptAnswer = "";
const prompts = [];
globalThis.prompt = m => { prompts.push(String(m)); return promptAnswer; };
// fetch stub: records every POST; GET /api/studio returns the current snapshot.
const posts = [];
let postStatus = 200, postBody = {ok: true};
let SNAP = null;
globalThis.fetch = async (u, opt) => {
  if (opt && opt.method === "POST") {
    posts.push({url: u, body: JSON.parse(opt.body)});
    return { status: postStatus, json: async () => postBody };
  }
  if (String(u).startsWith("/api/studio")) return { status: 200, json: async () => SNAP };
  return { status: 200, json: async () => ({}) };
};
globalThis.setInterval = () => 0; globalThis.setTimeout = () => 0; globalThis.clearInterval = () => 0;

const src = fs.readFileSync(new URL("../static/index.html", import.meta.url), "utf8");
const js = src.slice(src.indexOf("<script>") + 8, src.lastIndexOf("</script>"));
const externals = [...src.matchAll(/<script[^>]+src="([^"]+)"/g)]
  .map(m => "static/" + m[1].replace(/^\//, ""))
  .filter(f => fs.existsSync(f))
  .map(f => fs.readFileSync(f, "utf8"))
  .join("\n");
const mod = new Function(externals + "\n" + js
  + "\nglobalThis.__setStudio = (v, view, open) => { STUDIO = v; STUDIO_VIEW = view; STUDIO_OPEN = open; };"
  + "\nglobalThis.__ptFilter = v => { if (v !== undefined) PT_FILTER = v; return PT_FILTER; };"
  + "\nreturn {studioPlaytestView, studioBoard, renderStudio, pollStudio, STUDIO_VIEWS};");
const api = mod();

let n = 0;
const failures = [];
function ok(cond, name) { n++; if (!cond) failures.push(name); }
const count = (s, sub) => s.split(sub).length - 1;
const EVIL = '<img src=x onerror=alert(1)>';

// ---- fixture ---------------------------------------------------------------
const shot = (s, f) => `/api/studio/playtest/shot?project=demo&session=${s}&file=${f}`;
const finding = (id, state, extra = {}) => ({
  id, session: "20260923-101500-ab12", build: "main", sha: "0123456789abcdef", source: "game",
  ts: "2026-09-23T10:20:00", category: "bug", severity: 2, note: "note " + id, scene: "res://main.tscn",
  screenshot: null, screenshot_url: null, state, triage_note: "", link: "", fixed_in: null, history: [], ...extra,
});
const playtest = (over = {}) => ({
  godot: true, display: ":0",
  builds: [
    {id: "main", ref: "refs/heads/main", sha: "aaaaaaa111111", subject: "main head", ts: 1790000000, snapshot: true},
    {id: "task/hud-v2", ref: "refs/heads/task/hud-v2", sha: "bbbbbbb222222", subject: "HUD <v2>", ts: 1790000100, snapshot: false},
  ],
  sessions: [
    {id: "20260923-110000-cd34", build: "task/hud-v2", sha: "bbbbbbb222222", started: "2026-09-23T11:00:00",
     ended: null, pid: 4242, status: "running", survey: null, findings: 0},
    {id: "20260923-101500-ab12", build: "main", sha: "aaaaaaa111111", started: "2026-09-23T10:15:00",
     ended: "2026-09-23T10:40:00", pid: 4100, status: "ended", survey: null, findings: 3},
  ],
  findings: [
    finding("f-00000001", "new", {screenshot: "shot-1.png", screenshot_url: shot("20260923-101500-ab12", "shot-1.png") + '&x="><script>'}),
    finding("f-00000002", "accepted", {severity: 1}),
    finding("f-00000003", "fixed", {fixed_in: "cccccccddd"}),
    finding("f-00000004", "verified"),
    finding("f-00000005", "wontfix", {triage_note: EVIL, note: EVIL}),
    finding("f-00000006", "duplicate"),
    finding("f-00000007", "reopened", {severity: 4}),
  ],
  counts: {new: 1, accepted: 1, fixed: 1, verified: 1, wontfix: 1, duplicate: 1, reopened: 1, open: 3},
  ...over,
});
const project = pt => ({name: "demo", repo: "/home/x/repos/demo", phase_label: "Graybox", phases: [], boards: [], playtest: pt});

// ---- the view is registered --------------------------------------------------
ok(api.STUDIO_VIEWS.some(([v, l]) => v === "playtest" && l === "Playtest"), "STUDIO_VIEWS has the Playtest tab");

// ---- builds / Play buttons -------------------------------------------------
__setStudio({projects: [project(playtest())]}, "playtest", "demo");
__ptFilter("all");
let html = api.studioPlaytestView(project(playtest()));
ok(html.includes('data-pt-launch="main"') && html.includes('data-pt-launch="task/hud-v2"'), "Play buttons carry build ids");
ok(!/data-pt-launch="[^"]*"[^>]*disabled/.test(html), "Play enabled with godot + display");
ok(html.includes("HUD &lt;v2&gt;") && !html.includes("HUD <v2>"), "build subject escaped");
ok(html.includes("aaaaaaa") && !html.includes("aaaaaaa1"), "build sha shortened to 7");
ok(html.includes("F8 in game"), "help line present");

const noGodot = api.studioPlaytestView(project(playtest({godot: false})));
ok(count(noGodot, "disabled") >= 2 && /data-pt-launch="main"[^>]*disabled/.test(noGodot), "Play disabled when godot=false");
ok(/Cannot launch/.test(noGodot) && /Godot/.test(noGodot), "no-godot reason shown");
const noDisplay = api.studioPlaytestView(project(playtest({display: ""})));
ok(/data-pt-launch="task\/hud-v2"[^>]*disabled/.test(noDisplay), "Play disabled when display is empty");
ok(/display/.test(noDisplay) && /Cannot launch/.test(noDisplay), "no-display reason shown");

// ---- missing / error -------------------------------------------------------
let threw = false;
try {
  const miss = api.studioPlaytestView(project(undefined));
  ok(/empty/.test(miss), "missing playtest key renders an empty state");
  const err = api.studioPlaytestView(project({error: EVIL}));
  ok(err.includes("unavailable") && !err.includes("<img src=x"), "error key renders escaped");
} catch (e) { threw = true; }
ok(!threw, "missing/error playtest never throws");

// ---- sessions --------------------------------------------------------------
ok(html.includes('data-pt-stop="20260923-110000-cd34"'), "running session has Stop");
ok(!html.includes('data-pt-stop="20260923-101500-ab12"'), "ended session has no Stop");
ok(html.includes('data-pt-survey="20260923-101500-ab12"'), "ended session without survey shows the survey");
ok(!html.includes('data-pt-survey="20260923-110000-cd34"'), "running session shows no survey");
ok(/name="fun"/.test(html) && /name="clarity"/.test(html) && /name="difficulty"/.test(html), "survey has three selects");
ok(html.includes("3 findings"), "session findings count shown");
const surveyed = playtest();
surveyed.sessions[1].survey = {fun: 4, clarity: 3, difficulty: 2, note: "ok", ts: "x"};
ok(!api.studioPlaytestView(project(surveyed)).includes("data-pt-survey="), "surveyed session shows no survey form");

// ---- triage buttons: legal ones only ---------------------------------------
const LEGAL = {new: ["accepted", "wontfix", "duplicate", "fixed"], accepted: ["fixed", "wontfix", "duplicate"],
               fixed: ["verified", "reopened"], verified: ["reopened"], wontfix: ["reopened"],
               duplicate: ["reopened"], reopened: ["accepted", "fixed", "wontfix"]};
const ALL = ["accepted", "wontfix", "duplicate", "fixed", "verified", "reopened"];
for (const f of playtest().findings) {
  for (const st of ALL) {
    const has = html.includes(`data-pt-triage="${f.id}" data-pt-state="${st}"`);
    ok(has === LEGAL[f.state].includes(st), `${f.state} → ${st} button ${LEGAL[f.state].includes(st) ? "present" : "absent"}`);
  }
}

// ---- screenshots -----------------------------------------------------------
ok(html.includes('src="/api/studio/playtest/shot?project=demo&amp;session=20260923-101500-ab12&amp;file=shot-1.png&amp;x=&quot;&gt;&lt;script&gt;"'),
   "thumbnail src is the escaped screenshot_url");
ok(!html.includes('"><script>'), "screenshot_url cannot break out of the attribute");
ok(count(html, "pt-thumb") === 1, "only findings with a screenshot get a thumbnail");

// ---- escaping --------------------------------------------------------------
ok(!html.includes("<img src=x"), "malicious note / triage note escaped");
ok(html.includes("&lt;img src=x onerror=alert(1)&gt;"), "escaped note is shown as text");

// ---- filter ----------------------------------------------------------------
__ptFilter("open");
const openHtml = api.studioPlaytestView(project(playtest()));
ok(openHtml.includes('data-pt-triage="f-00000001"') && openHtml.includes('data-pt-triage="f-00000002"')
   && openHtml.includes('data-pt-triage="f-00000007"'), "open filter shows new/accepted/reopened");
ok(!openHtml.includes('data-pt-triage="f-00000004"') && !openHtml.includes('data-pt-triage="f-00000005"')
   && !openHtml.includes('data-pt-triage="f-00000003"'), "open filter hides verified/wontfix/fixed");
ok(/data-pt-filter="open" aria-pressed="true"/.test(openHtml), "open chip is pressed");
ok(/pt-sev s1/.test(openHtml) && /pt-sev s4/.test(openHtml), "severity badges rendered");

// ---- click handler ---------------------------------------------------------
const click = async target => { for (const fn of listeners.click || []) await fn({target}); };
// A fake clicked element: closest() returns itself for any selector whose
// data-attribute it carries, and `form` for [data-pt-form].
const btn = (attrs, form = null) => ({
  disabled: !!attrs.disabled,
  getAttribute: k => (k in attrs ? attrs[k] : null),
  closest: sel => {
    const m = sel.match(/^\[([\w-]+)\]$/);
    if (m && m[1] in attrs) return btnSelf;
    if (sel === "[data-pt-form]") return form;
    return null;
  },
});
let btnSelf = null;
const hit = async (attrs, form) => { btnSelf = btn(attrs, form); await click(btnSelf); };
SNAP = {projects: [project(playtest())]};
__setStudio(SNAP, "playtest", "demo");

await hit({"data-pt-filter": "verified"});
ok(__ptFilter() === "verified" && lsBacking.get("arc.studio.pt.filter") === "verified", "filter chip persists to localStorage");
__ptFilter("open");

posts.length = 0;
await hit({"data-pt-launch": "task/hud-v2"});
ok(posts.length === 1 && posts[0].url === "/api/studio/playtest/launch"
   && posts[0].body.project === "demo" && posts[0].body.build === "task/hud-v2", "Play posts {project, build}");

posts.length = 0;
await hit({"data-pt-launch": "main", disabled: true});
ok(posts.length === 0, "disabled Play posts nothing");

posts.length = 0;
await hit({"data-pt-stop": "20260923-110000-cd34"});
ok(posts.length === 1 && posts[0].url === "/api/studio/playtest/stop" && posts[0].body.session === "20260923-110000-cd34", "Stop posts {project, session}");

posts.length = 0; prompts.length = 0; promptAnswer = "same as f-1";
await hit({"data-pt-triage": "f-00000001", "data-pt-state": "duplicate"});
ok(prompts.length === 1 && posts.length === 1 && posts[0].url === "/api/studio/playtest/triage"
   && posts[0].body.finding === "f-00000001" && posts[0].body.state === "duplicate" && posts[0].body.note === "same as f-1",
   "duplicate prompts for a note and posts it");

posts.length = 0; promptAnswer = null;
await hit({"data-pt-triage": "f-00000001", "data-pt-state": "wontfix"});
ok(posts.length === 0, "cancelled prompt posts nothing");

posts.length = 0; promptAnswer = "task-42";
await hit({"data-pt-triage": "f-00000001", "data-pt-state": "accepted"});
ok(posts.length === 1 && posts[0].body.link === "task-42" && posts[0].body.state === "accepted", "accept prompts for an optional link");

posts.length = 0; prompts.length = 0;
await hit({"data-pt-triage": "f-00000003", "data-pt-state": "verified"});
ok(prompts.length === 0 && posts.length === 1 && posts[0].body.state === "verified", "verified needs no prompt");

// Survey reads the selects/inputs of its own form.
const fakeForm = vals => ({ querySelector: sel => { const m = sel.match(/name="(\w+)"/); return m && m[1] in vals ? {value: vals[m[1]]} : null; } });
posts.length = 0;
await hit({"data-pt-survey": "20260923-101500-ab12"}, fakeForm({fun: "4", clarity: "5", difficulty: "2", note: "fun!"}));
ok(posts.length === 1 && posts[0].url === "/api/studio/playtest/survey"
   && posts[0].body.session === "20260923-101500-ab12" && posts[0].body.fun === 4 && posts[0].body.clarity === 5
   && posts[0].body.difficulty === 2 && posts[0].body.note === "fun!", "survey posts the form's values as numbers");
posts.length = 0; alerts.length = 0;
await hit({"data-pt-survey": "20260923-101500-ab12"}, fakeForm({fun: "", clarity: "5", difficulty: "2", note: ""}));
ok(posts.length === 0 && alerts.length === 1, "incomplete survey is refused locally");

posts.length = 0;
await hit({"data-pt-add": "1"}, fakeForm({category: "feel", severity: "2", note: "  jump is floaty "}));
ok(posts.length === 1 && posts[0].url === "/api/studio/playtest/finding" && posts[0].body.category === "feel"
   && posts[0].body.severity === 2 && posts[0].body.note === "jump is floaty", "add-finding posts category/severity/note");

// Non-200 → alert with the server's error text.
posts.length = 0; alerts.length = 0; postStatus = 409; postBody = {error: "no display configured"};
await hit({"data-pt-launch": "main"});
ok(alerts.length === 1 && alerts[0].includes("no display configured"), "non-200 alerts the error text");
postStatus = 200; postBody = {ok: true};

// ---- typed text survives the poll ------------------------------------------
// Drafts: an input event on a keyed field is written back by the next render.
const inputEv = (key, value) => ({target: {value, getAttribute: k => (k === "data-pt-key" ? key : null)}});
for (const fn of listeners.input || []) fn(inputEv("add:note", "half-typed <b>"));
const redraw = api.studioPlaytestView(project(playtest()));
ok(redraw.includes('value="half-typed &lt;b&gt;"'), "drafted note is restored (escaped) on re-render");
for (const fn of listeners.change || []) fn(inputEv("survey:20260923-101500-ab12:fun", "5"));
ok(/name="fun"[^>]*>(?:(?!<\/select>).)*<option value="5" selected>/s.test(api.studioPlaytestView(project(playtest()))),
   "drafted survey select is restored on re-render");
// Focus guard: while a playtest field has focus, the poll does not re-render.
const el = document.querySelector("#studio");
el.innerHTML = "SENTINEL";
document.activeElement = {tagName: "INPUT", closest: s => (s === "[data-pt-form]" ? {} : null)};
await api.pollStudio();
ok(el.innerHTML === "SENTINEL", "poll skips re-render while typing in a playtest form");
document.activeElement = null;
await api.pollStudio();
ok(el.innerHTML.includes("data-pt-launch"), "poll renders the playtest view once focus leaves");

// ---- agent thread on the Board ---------------------------------------------
const EVIL_THREAD = '<img src=x onerror=alert(1)>';
const boardHtml = api.studioBoard({
  kanban: {backlog: [], planned: [], building: [], review: [], done: [], blocked: []},
  thread: [{task: EVIL_THREAD, role: "implementer", model: "m <x>", harness: "cursor",
            kind: "note", body: EVIL_THREAD + " & more", timestamp: 1.5,
            session_owner: "cursor"}],
});
for (const col of ["backlog", "planned", "building", "review", "done", "blocked"])
  ok(boardHtml.includes(`kb-${col}`), `kanban column ${col} stays`);
ok(boardHtml.includes("Agent thread"), "board shows the agent thread");
ok(boardHtml.includes("&lt;img src=x onerror=alert(1)&gt;") && !boardHtml.includes(EVIL_THREAD),
   "agent-authored thread strings are escaped");
ok(boardHtml.includes(" &amp; more"), "ampersand in a post body is escaped");
ok(boardHtml.includes("session on cursor") && !boardHtml.includes("session_id"),
   "session ownership names the harness, not an id");
ok(api.studioBoard({kanban: {}}).includes("No agent handoffs"), "empty thread is explicit");

// ---- report ----------------------------------------------------------------
if (failures.length) {
  console.error(`studio_playtest_ui: ${failures.length}/${n} FAILED`);
  for (const f of failures) console.error("  ✗ " + f);
  process.exit(1);
}
console.log(`studio_playtest_ui: ${n} checks passed`);
