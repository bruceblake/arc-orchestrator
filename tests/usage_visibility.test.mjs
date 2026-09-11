// usage.html must stop polling a tab nobody is looking at.
//
// A background tab kept hitting /api/usage every five seconds forever, keeping
// the machine awake for a page nobody is reading. PR #9 attempted this fix and
// was rejected four times; its structural mistake was pasting the whole tick
// callback into the visibilitychange handler, so the same six lines existed
// twice and could drift. These checks pin both the behaviour and that shape.
import fs from "node:fs";

const src = fs.readFileSync(new URL("../static/usage.html", import.meta.url), "utf8");
const js = src.slice(src.indexOf("<script>") + 8, src.lastIndexOf("</script>"));

let failures = 0;
const ok = (cond, what) => {
  if (!cond) { console.error(`usage_visibility: FAIL — ${what}`); failures++; }
};

// ---- shape: one callback, not two copies -----------------------------------
const tickBodies = (js.match(/const left = Math\.max\(0, Math\.ceil\(\(nextAt/g) || []).length;
ok(tickBodies === 1,
   `the tick body appears ${tickBodies} times; it must be written once and ` +
   `referenced, not duplicated into the visibilitychange handler`);
ok(/document\.addEventListener\("visibilitychange"/.test(js),
   "no visibilitychange handler is registered");
ok(/document\.hidden/.test(js), "document.hidden is never consulted");

// ---- behaviour: run the real code against a controllable stub ---------------
let now = 1_000_000;
let nextId = 1;
const timers = new Map();            // id -> ms
const listeners = {};
const el = () => ({ innerHTML: "", textContent: "", className: "", style: {},
                    dataset: {}, classList: {add(){}, remove(){}, toggle(){}},
                    querySelectorAll: () => [], appendChild(){}, onclick: null });
const els = new Map();

globalThis.document = {
  hidden: false,
  querySelector: sel => { const k = sel; if (!els.has(k)) els.set(k, el()); return els.get(k); },
  querySelectorAll: () => [],
  createElement: el,
  addEventListener: (name, fn) => { (listeners[name] ||= []).push(fn); },
  removeEventListener: () => {},
  body: el(), documentElement: el(),
};
globalThis.window = { addEventListener: () => {}, removeEventListener: () => {},
                      matchMedia: () => ({ matches: false, addEventListener(){} }),
                      location: { hash: "", search: "" } };
globalThis.location = { hash: "", search: "", href: "http://x/" };
globalThis.history = { replaceState(){}, pushState(){} };
globalThis.localStorage = { getItem: () => null, setItem(){}, removeItem(){} };
globalThis.fetch = async () => ({ json: async () => ({}), status: 200 });
globalThis.setInterval = (fn, ms) => { const id = nextId++; timers.set(id, ms); return id; };
globalThis.clearInterval = id => { timers.delete(id); };
globalThis.setTimeout = () => 0;
globalThis.Date = class extends Date { static now() { return now; } };

const externals = [...src.matchAll(/<script[^>]+src="([^"]+)"/g)]
  .map(m => "static/" + m[1].replace(/^\//, ""))
  .filter(f => fs.existsSync(f))
  .map(f => fs.readFileSync(f, "utf8")).join("\n");
new Function(externals + "\n" + js)();

const fire = () => (listeners.visibilitychange || []).forEach(fn => fn());

ok(timers.size === 1, `a visible page should tick once, saw ${timers.size} timer(s)`);

document.hidden = true; fire();
ok(timers.size === 0, `a hidden tab must stop polling, ${timers.size} timer(s) still armed`);

document.hidden = false; fire();
ok(timers.size === 1, `returning must resume polling, saw ${timers.size} timer(s)`);

// The bug that turns this fix into a runaway: visibilitychange can fire more
// than once for one transition, and an unguarded start stacks intervals.
fire(); fire(); fire();
ok(timers.size === 1,
   `repeated visibilitychange stacked ${timers.size} intervals; the page would ` +
   `poll faster than before the fix`);

if (failures) process.exit(1);
console.log("usage_visibility: PASS — 7 checks");
