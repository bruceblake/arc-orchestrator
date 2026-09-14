// Behavioural unit tests for the plan-with-kimi chat panel. No framework, no
// server: `node tests/chat_ui.test.mjs`; exits non-zero on any failure.
// Same harness as tests/ui_render.test.mjs — DOM stub + concatenated
// static/common.js + each page's inline+external script (index.html) evaluated
// with new Function. The chat panel's DOM stub needs classList.toggle, which
// the ui_render version omits, so it is defined here.
import fs from "node:fs";
const els = new Map();
const mk = id => {
  let html = "";
  const classes = new Set();
  return {
    id, className: "", title: "", value: "", options: [], dataset: {}, checked: false,
    style: {}, disabled: false, scrollTop: 0, scrollHeight: 0,
    classList: {
      add(c){classes.add(c);}, remove(c){classes.delete(c);},
      contains:c=>classes.has(c),
      toggle(c,on){ if (on === undefined) { on = !classes.has(c); } on ? classes.add(c) : classes.delete(c); },
    },
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
globalThis.confirm = () => false; globalThis.alert = () => {}; globalThis.prompt = () => null;
globalThis.fetch = async () => ({ json: async () => ({}), status: 200 });
globalThis.setInterval = () => 0; globalThis.setTimeout = () => 0; globalThis.clearInterval = () => 0;

const src = fs.readFileSync(new URL("../static/index.html", import.meta.url), "utf8");
const js = src.slice(src.indexOf("<script>") + 8, src.lastIndexOf("</script>"));
const externals = [...src.matchAll(/<script[^>]+src="([^"]+)"/g)]
  .map(m => "static/" + m[1].replace(/^\//, ""))
  .filter(f => fs.existsSync(f))
  .map(f => fs.readFileSync(f, "utf8"))
  .join("\n");
// chat.js keeps module-scope `let` state; expose setters from inside so tests
// can drive chatRender/chatUpdateMic/chatPoll against known state.
const mod = new Function(externals + "\n" + js
  + "\nglobalThis.__setChat = o => {"
  + " if ('REPO' in o) CHAT_REPO=o.REPO;"
  + " if ('REPOS' in o) CHAT_REPOS=o.REPOS;"
  + " if ('SESSION' in o) CHAT_SESSION=o.SESSION;"
  + " if ('SESSIONS' in o) CHAT_SESSIONS=o.SESSIONS;"
  + " if ('TURNS' in o) CHAT_TURNS=o.TURNS;"
  + " if ('LAST' in o) CHAT_LAST=o.LAST;"
  + " if ('RUNNING' in o) CHAT_RUNNING=o.RUNNING;"
  + " if ('LISTENING' in o) CHAT_LISTENING=o.LISTENING;"
  + " if ('FINAL' in o) CHAT_SPEECH_FINAL=o.FINAL;"
  + " };"
  + "\nglobalThis.__getChat = () => ({"
  + " REPO: CHAT_REPO, REPOS: CHAT_REPOS, SESSION: CHAT_SESSION,"
  + " SESSIONS: CHAT_SESSIONS,"
  + " TURNS: CHAT_TURNS, LAST: CHAT_LAST, RUNNING: CHAT_RUNNING,"
  + " LISTENING: CHAT_LISTENING, FINAL: CHAT_SPEECH_FINAL"
  + " });"
  + "\nreturn {chatSend, chatPoll, chatRender, chatTurnHTML, chatTaskcardHTML,"
  + " chatEmptyState, chatSupportsSpeech, chatUpdateMic, chatMic, chatStopMic,"
  + " chatLoadRepos, chatNewRepo, chatStartSession, chatOpen,"
  + " chatLoadSessions, chatSelectSession, chatNewSession, chatFreshSession};");
const c = mod();

let n = 0;
const failures = [];
function ok(cond, name) {
  n++;
  if (!cond) failures.push(name);
}
const EVIL = '<img src=x onerror=alert(1)>';
const clean = s => !s.includes("<img src=x");

// ---- functions exist -------------------------------------------------------
ok(typeof c.chatSend === "function", "chatSend defined");
ok(typeof c.chatPoll === "function", "chatPoll defined");
ok(typeof c.chatRender === "function", "chatRender defined");
ok(typeof c.chatEmptyState === "function", "chatEmptyState defined");

// ---- empty-state text (exact spec string) -----------------------------------
const empty = c.chatEmptyState();
// The planner is named by the page constant, not a retired model: assert the
// RULE (the live planner is named, plus the spec wording), not a model id.
ok(/\S+ will turn it into a governed project/.test(empty)
   && empty.includes("and hand it back ready to run")
   && !empty.includes("Kimi"),
   "empty state names a live planner and keeps the spec wording");

// ---- mic renders iff SpeechRecognition exists ------------------------------
window.SpeechRecognition = function(){};
ok(c.chatSupportsSpeech() === true, "supports speech when SpeechRecognition present");
window.webkitSpeechRecognition = undefined;
window.SpeechRecognition = undefined;
// Reset __setChat to a known state; chatUpdateMic uses the module CHAT_LISTENING.
__setChat({ LISTENING: false });
c.chatUpdateMic();
ok(document.querySelector("#c-mic").style.display === "none", "mic hidden when SR absent");

window.SpeechRecognition = function(){};
__setChat({ LISTENING: false });
c.chatUpdateMic();
ok(document.querySelector("#c-mic").style.display === "", "mic shown when SR present");
window.SpeechRecognition = undefined;

// ---- speech: finals appended exactly once over cumulative results ----------
// ev.results is the CUMULATIVE list for the whole session (continuous=true),
// so each isFinal transcript must be appended once, not re-appended on every
// subsequent onresult event. Two events share one cumulative results array.
let micInst = null;
window.SpeechRecognition = function() {
  micInst = this;
  this.start = () => {};
  this.stop = () => {};
};
__setChat({ LISTENING: false, FINAL: "" });
const textInput = document.querySelector("#c-text");
textInput.value = "";
c.chatMic();
ok(micInst && typeof micInst.onresult === "function",
   "speech: chatMic constructs a recognizer");
const cumulative = [];
function fire(resultIndex) { micInst.onresult({ resultIndex, results: cumulative }); }
cumulative.push({ isFinal: true, 0: { transcript: "build a tool" }, length: 1 });
fire(0);
cumulative.push({ isFinal: true, 0: { transcript: "for parsing logs" }, length: 1 });
fire(1);
ok(textInput.value === "build a tool for parsing logs",
   "speech: finals appended exactly once across cumulative results");
c.chatStopMic();
window.SpeechRecognition = undefined;

// ---- send resets the speech buffer (mic may be live while sending) ----------
// Regression: chatSend cleared only the compose input, leaving
// CHAT_SPEECH_FINAL holding the just-sent text. With continuous dictation the
// next live onresult wrote that text back into the compose box, so a later
// Send transmitted '<msg1> <msg2>'. A successful send must clear the
// accumulator too.
__setChat({ FINAL: "build a tool", RUNNING: false, SESSION: "plan-x" });
const sendInput = document.querySelector("#c-text");
sendInput.value = "build a tool";
await c.chatSend();
ok(__getChat().FINAL === "", "send: successful send clears the speech buffer");
sendInput.value = "";

// ---- escaping: turn text must be neutralised -------------------------------
ok(clean(c.chatTurnHTML({ role: "user", text: EVIL, ts: null })),
   "escaping: user turn text escaped");
ok(clean(c.chatTurnHTML({ role: "assistant", text: EVIL, error: null, taskfile: null })),
   "escaping: assistant turn text escaped");
ok(c.chatTurnHTML({ role: "user", text: EVIL, ts: null }).includes("&lt;img"),
   "escaping: hostile text rendered escaped, not dropped");
ok(clean(c.chatTurnHTML({ role: "assistant", text: "hi", error: EVIL, taskfile: null })),
   "escaping: turn error escaped");

// ---- taskcard: both buttons only when a taskfile is present -----------------
ok(c.chatTaskcardHTML(null) === "", "taskcard: no card when no taskfile");
ok(c.chatTaskcardHTML("") === "", "taskcard: no card for empty taskfile");
const card = c.chatTaskcardHTML("plan.json");
ok(card.includes("plan ready:") && card.includes("plan.json"), "taskcard: file label rendered");
ok(card.includes("open project") && card.includes(">run<"), "taskcard: open + run buttons present");
ok(card.includes("data-ct-open=") && card.includes("data-ct-run="), "taskcard: buttons carry data attrs");

// ---- thinking spinner renders while a turn runs ----------------------------
__setChat({ TURNS: [], RUNNING: true });
c.chatRender();
const thinking = document.querySelector("#c-log").innerHTML;
ok(/\S+ is thinking…/.test(thinking) && !thinking.includes("Kimi"),
   "render: thinking line shown while running");
__setChat({ TURNS: [], RUNNING: false });
c.chatRender();
const idle = document.querySelector("#c-log").innerHTML;
ok(!idle.includes("is thinking…"),
   "render: thinking line hidden when idle");

// ---- repo picker gains a 'new repo…' option --------------------------------
globalThis.fetch = async () => ({ json: async () => ({ repos: [
  { name: "alpha", path: "/x/alpha" }, { name: "beta", path: "/x/beta" } ] }), status: 200 });
await c.chatLoadRepos();
const repoHTML = document.querySelector("#c-repo").innerHTML;
ok(repoHTML.includes("new repo…"), "repo picker offers a 'new repo…' option");
ok(repoHTML.includes('value="/x/alpha"') && repoHTML.includes('value="/x/beta"'),
   "repo picker lists fetched repos");
globalThis.fetch = async () => ({ json: async () => ({}), status: 200 });

// ---- session picker: lists what the backend serves -------------------------
const srv = { "/api/chat/sessions": { sessions: [
  { name: "plan-beta", turns: 3, mtime: 300 },
  { name: "plan-alpha", turns: 1, mtime: 100 } ] } };
globalThis.fetch = async url => ({
  json: async () => (srv[String(url).split("?")[0]] || {}), status: 200 });
__setChat({ SESSION: "plan-beta", SESSIONS: [], TURNS: [] });
await c.chatLoadSessions();
const sessHTML = document.querySelector("#c-sessions").innerHTML;
ok(sessHTML.includes('value="plan-beta"') && sessHTML.includes('value="plan-alpha"'),
   "session picker lists the sessions the backend returned");
ok(__getChat().SESSIONS.length === 2, "session picker keeps the listing in state");
ok(document.querySelector("#c-sessions").value === "plan-beta",
   "session picker selects the active session");
ok(document.querySelector("#c-session").textContent === "plan-beta",
   "header shows the active session name");

// A session with no turns yet (a brand-new one) is still selectable.
__setChat({ SESSION: "plan-fresh", SESSIONS: [], TURNS: [] });
await c.chatLoadSessions();
ok(document.querySelector("#c-sessions").innerHTML.includes('value="plan-fresh"'),
   "the active session is offered even before the backend lists it");

// A failing listing degrades to an empty picker, it does not throw.
globalThis.fetch = async () => { throw new Error("down"); };
__setChat({ SESSION: "plan-x", TURNS: [] });
await c.chatLoadSessions();
ok(__getChat().SESSIONS.length === 0, "a failing listing leaves an empty session list");
ok(document.querySelector("#c-sessions").innerHTML.includes("plan-x"),
   "a failing listing still keeps the active session selectable");
globalThis.fetch = async () => ({ json: async () => ({}), status: 200 });

// ---- new chat: a fresh id, an empty panel ----------------------------------
ok(c.chatFreshSession("plan-alpha", []) === "plan-alpha",
   "fresh session: the base id when nothing holds it");
ok(c.chatFreshSession("plan-alpha", ["plan-alpha"]) === "plan-alpha-2",
   "fresh session: -2 when the base id is taken");
ok(c.chatFreshSession("plan-alpha", ["plan-alpha", "plan-alpha-2"]) === "plan-alpha-3",
   "fresh session: the next free suffix");
const longBase = "plan-" + "x".repeat(40);
ok(c.chatFreshSession(longBase, [longBase]).length <= 40,
   "fresh session: the id stays inside the backend's 40-char session rule");

globalThis.fetch = async url => ({
  json: async () => (srv[String(url).split("?")[0]] || {}), status: 200 });
// /x/gamma has no session on disk, so its base id is free.
__setChat({ REPO: "/x/gamma", SESSION: "plan-beta", SESSIONS: [], TURNS: [], LAST: 0 });
await c.chatNewSession();
const fresh = __getChat().SESSION;
ok(fresh === "plan-gamma", "new chat: uses the repo's session when it is free");
ok(__getChat().TURNS.length === 0, "new chat: clears the rendered transcript");
ok(__getChat().LAST === 0, "new chat: restarts the poll cursor");
ok(document.querySelector("#c-session").textContent === fresh,
   "new chat: header shows the new session name");
ok(!document.querySelector("#c-log").innerHTML.includes("chat-taskcard"),
   "new chat: the panel is emptied");

// With the repo's base id already on disk, New chat must not reuse it.
__setChat({ REPO: "/x/beta", SESSION: "plan-beta", SESSIONS: [], TURNS: [] });
await c.chatNewSession();
const fresh2 = __getChat().SESSION;
ok(fresh2 !== "plan-beta" && fresh2.startsWith("plan-beta-"),
   "new chat: steps aside from an existing session of the same repo");

// ---- switching sessions rebuilds the transcript from zero ------------------
__setChat({ SESSION: "plan-alpha", TURNS: [{ role: "user", text: "old" }], LAST: 9 });
const seen = [];
globalThis.fetch = async url => {
  seen.push(String(url));
  return { json: async () => (String(url).startsWith("/api/chat/poll")
    ? { turns: [{ role: "user", text: "from beta" }], running: false }
    : srv[String(url).split("?")[0]] || {}), status: 200 };
};
await c.chatSelectSession("plan-beta");
const after = __getChat();
ok(after.SESSION === "plan-beta", "session switch: activates the picked session");
ok(after.LAST === 1, "session switch: poll cursor restarts at the new session's turns");
ok(after.TURNS.length === 1 && after.TURNS[0].text === "from beta",
   "session switch: the transcript is rebuilt from the picked session alone");
ok(seen.some(u => u.includes("session=plan-beta")), "session switch: polls the picked session");
ok(!after.TURNS.some(t => t.text === "old"),
   "session switch: never splices the previous session's turns in");

globalThis.fetch = async () => ({ json: async () => ({}), status: 200 });

if (failures.length) {
  console.error(`chat_ui: FAIL — ${failures.length}/${n} checks failed:`);
  for (const f of failures) console.error("  - " + f);
  process.exit(1);
}
console.log(`chat_ui: PASS — ${n} checks`);
