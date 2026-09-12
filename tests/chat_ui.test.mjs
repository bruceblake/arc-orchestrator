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
  + " if ('TURNS' in o) CHAT_TURNS=o.TURNS;"
  + " if ('LAST' in o) CHAT_LAST=o.LAST;"
  + " if ('RUNNING' in o) CHAT_RUNNING=o.RUNNING;"
  + " if ('LISTENING' in o) CHAT_LISTENING=o.LISTENING;"
  + " if ('FINAL' in o) CHAT_SPEECH_FINAL=o.FINAL;"
  + " };"
  + "\nglobalThis.__getChat = () => ({"
  + " REPO: CHAT_REPO, REPOS: CHAT_REPOS, SESSION: CHAT_SESSION,"
  + " TURNS: CHAT_TURNS, LAST: CHAT_LAST, RUNNING: CHAT_RUNNING,"
  + " LISTENING: CHAT_LISTENING, FINAL: CHAT_SPEECH_FINAL"
  + " });"
  + "\nreturn {chatSend, chatPoll, chatRender, chatTurnHTML, chatTaskcardHTML,"
  + " chatEmptyState, chatSupportsSpeech, chatUpdateMic, chatMic, chatStopMic,"
  + " chatLoadRepos, chatNewRepo, chatStartSession, chatOpen};");
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
ok(empty.includes("Kimi-K3 will turn it into a governed project")
   && empty.includes("and hand it back ready to run"),
   "empty state uses the exact spec wording");

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
ok(document.querySelector("#c-log").innerHTML.includes("Kimi-K3 is thinking…"),
   "render: thinking line shown while running");
__setChat({ TURNS: [], RUNNING: false });
c.chatRender();
ok(!document.querySelector("#c-log").innerHTML.includes("Kimi-K3 is thinking…"),
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

if (failures.length) {
  console.error(`chat_ui: FAIL — ${failures.length}/${n} checks failed:`);
  for (const f of failures) console.error("  - " + f);
  process.exit(1);
}
console.log(`chat_ui: PASS — ${n} checks`);
