// Test the pause-when-hidden behaviour: every dashboard page must stop polling
// when the tab is hidden and refresh immediately (then resume polling) when it
// becomes visible again. `node --check` proves the pages parse; this proves the
// visibilitychange handler exists, clears every polling interval on hide, and
// re-registers exactly the same number on show without leaking.
//
//   node tests/visibility_check.mjs
//
// It runs each page in a recording DOM stub (no live server): setInterval ids
// are recorded so we can assert that hide clears the polling timers and show
// restarts them; fetch() records every URL so we can assert that show actually
// fires the poll functions.
import fs from "node:fs";

const ROOT = new URL("../static/", import.meta.url);

// ---- recording DOM / runtime environment -----------------------------------
function makeEnv() {
  const els = new Map();
  const mkEl = id => ({
    id, innerHTML: "", textContent: "", className: "", title: "", value: "",
    options: [], dataset: {}, checked: false, disabled: false, tabIndex: 0,
    style: {}, hidden: false,
    classList: { add(){}, remove(){}, toggle(){}, contains: () => false },
    querySelector: () => mkEl(id + "-sub"),
    querySelectorAll: () => [],
    appendChild(){}, remove(){}, focus(){}, click(){}, scrollIntoView(){},
    setAttribute(){}, getAttribute: () => null, removeAttribute(){},
    onclick: null, onchange: null,
  });

  const listeners = {};
  const intervals = [];
  const cleared = new Set();
  const fetches = [];
  let seq = 0;

  const doc = {
    hidden: false, title: "",
    querySelector: sel => {
      const id = String(sel).replace(/^#/, "");
      if (!els.has(id)) els.set(id, mkEl(id));
      return els.get(id);
    },
    querySelectorAll: () => [],
    getElementById: id => {
      if (!els.has(id)) els.set(id, mkEl(id));
      return els.get(id);
    },
    createElement: () => mkEl("new"),
    addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); },
    removeEventListener: () => {},
    body: mkEl("body"), documentElement: mkEl("html"),
  };

  const win = {
    addEventListener(){}, removeEventListener(){},
    matchMedia: () => ({ matches: false, addEventListener(){} }),
    location: { hash: "", search: "" },
    scrollY: 0, scrollTo(){},
    requestAnimationFrame: () => 0, cancelAnimationFrame(){},
    open(){}, close(){},
  };

  const fetchStub = async url => { fetches.push(url); return { ok: true, status: 200, json: async () => ({}) }; };
  // setTimeout is a no-op (return 0): the pages' staggered "boot" polls must
  // never fire, so the boot interval set is deterministic.
  const setIntervalStub = (fn, ms) => { const id = ++seq; intervals.push({ id, fn, ms }); return id; };
  const clearIntervalStub = id => { cleared.add(id); };
  const setTimeoutStub = () => 0;
  const clearTimeoutStub = () => {};

  return {
    doc, win, listeners, intervals, cleared, fetches,
    install() {
      globalThis.document = doc;
      globalThis.window = win;
      globalThis.localStorage = { getItem: () => null, setItem(){}, removeItem(){} };
      globalThis.location = { hash: "", search: "", href: "http://localhost:8787/" };
      globalThis.history = { replaceState(){}, pushState(){} };
      Object.defineProperty(globalThis, "navigator", { value: { clipboard: { writeText: async () => {} } }, configurable: true });
      globalThis.confirm = () => false;
      globalThis.alert = () => {};
      globalThis.fetch = fetchStub;
      globalThis.setInterval = setIntervalStub;
      globalThis.clearInterval = clearIntervalStub;
      globalThis.setTimeout = setTimeoutStub;
      globalThis.clearTimeout = clearTimeoutStub;
    },
    active: () => intervals.filter(i => !cleared.has(i.id)),
    activeNamed: names => intervals.filter(i => !cleared.has(i.id) && names.includes(i.fn && i.fn.name)),
  };
}

const flush = () => new Promise(r => setImmediate(r));

// ---- per-page configuration ------------------------------------------------
const PAGES = [
  {
    file: "index.html",
    // index has stray intervals (drawer timer, live-elapsed tick) that are not
    // part of the polling set; only the six poll timers in startPolling() must
    // follow the visibility lifecycle.
    polls: ["pollFleet", "pollAgents", "pollHealth", "pollEvents", "pollProjects", "pollGithub"],
    fetchOnShow: ["/api/fleet", "/api/agents", "/api/health", "/api/events", "/api/projects", "/api/github"],
    bootActiveNamed: 2,       // pollProjects + pollGithub pushed at boot
    expectNamed: 6,
    onShowExtra: null,
    toString: cfg => `return { };`,
  },
  {
    file: "phone.html",
    polls: [],
    fetchOnShow: ["/api/usage", "/api/agents", "/api/projects"],
    bootActiveNamed: 2,       // pollTimer + transcriptTimer
    expectActive: 2,
    toString: cfg => `return { };`,
  },
  {
    file: "usage.html",
    polls: [],
    fetchOnShow: ["/api/usage", "/api/fleet"],
    bootActiveNamed: 1,       // tickTimer
    expectActive: 1,
    // expose nextAt so the test can simulate "hidden longer than AUTO" and
    // assert the resume does not double-fetch.
    toString: cfg => `return { set_nextAt: v => { nextAt = v; } };`,
  },
];

function extractPage(file) {
  const src = fs.readFileSync(new URL(file, ROOT), "utf8");
  const js = src.slice(src.indexOf("<script>") + 8, src.lastIndexOf("</script>"));
  const externals = [...src.matchAll(/<script[^>]+src="([^"]+)"/g)]
    .map(m => "static/" + m[1].replace(/^\//, ""))
    .filter(f => fs.existsSync(f))
    .map(f => fs.readFileSync(new URL("../" + f, ROOT), "utf8"))
    .join("\n");
  return externals + "\n" + js;
}

// ---- assertions -------------------------------------------------------------
function fail(cfg, msg) { console.error(`FAIL ${cfg.file}: ${msg}`); }

async function runIndex(cfg) {
  const env = makeEnv();
  env.install();
  const mod = new Function(extractPage(cfg.file) + "\n" + cfg.toString(cfg));
  mod();

  const hs = env.listeners.visibilitychange || [];
  if (hs.length !== 1) return fail(cfg, `expected 1 visibilitychange listener, got ${hs.length}`);
  const listener = hs[0];
  let ok = true;

  // Boot: only the two staggered interval pushes should be live.
  let n = env.activeNamed(cfg.polls).length;
  if (n !== cfg.bootActiveNamed) { fail(cfg, `boot named polls = ${n}, expected ${cfg.bootActiveNamed}`); ok = false; }

  const has = p => env.fetches.some(u => u.includes(p));

  // Hide: every named poll timer must be cleared.
  env.doc.hidden = true; listener();
  n = env.activeNamed(cfg.polls).length;
  if (n !== 0) { fail(cfg, `after hide, named polls = ${n}, expected 0`); ok = false; }

  // Show: poll functions fire and all six timers restart.
  const before = env.fetches.length;
  env.doc.hidden = false; listener();
  await flush();
  for (const p of cfg.fetchOnShow) if (!has(p)) { fail(cfg, `show did not fetch ${p}`); ok = false; }
  // any fetch happened at all on show
  if (env.fetches.length === before) { fail(cfg, "show did not call any poll function"); ok = false; }
  n = env.activeNamed(cfg.polls).length;
  if (n !== cfg.expectNamed) { fail(cfg, `after show, named polls = ${n}, expected ${cfg.expectNamed}`); ok = false; }

  // Hide -> Show -> Hide -> Show: no leak (count stays stable).
  env.doc.hidden = true; listener();
  if (env.activeNamed(cfg.polls).length !== 0) { fail(cfg, "second hide did not clear polls"); ok = false; }
  env.doc.hidden = false; listener();
  await flush();
  n = env.activeNamed(cfg.polls).length;
  if (n !== cfg.expectNamed) { fail(cfg, `after second show, named polls = ${n}, expected ${cfg.expectNamed} (leak)`); ok = false; }

  return ok;
}

async function runIntervalPages(cfg) {
  const env = makeEnv();
  env.install();
  const mod = new Function(extractPage(cfg.file) + "\n" + cfg.toString(cfg));
  mod();

  const hs = env.listeners.visibilitychange || [];
  if (hs.length !== 1) return fail(cfg, `expected 1 visibilitychange listener, got ${hs.length}`);
  const listener = hs[0];
  let ok = true;
  const has = p => env.fetches.some(u => u.includes(p));

  let n = env.active().length;
  if (n !== cfg.bootActiveNamed) { fail(cfg, `boot active = ${n}, expected ${cfg.bootActiveNamed}`); ok = false; }

  env.doc.hidden = true; listener();
  if (env.active().length !== 0) { fail(cfg, `after hide, active = ${env.active().length}, expected 0`); ok = false; }

  const before = env.fetches.length;
  env.doc.hidden = false; listener();
  await flush();
  for (const p of cfg.fetchOnShow) if (!has(p)) { fail(cfg, `show did not fetch ${p}`); ok = false; }
  if (env.fetches.length === before) { fail(cfg, "show did not call any poll function"); ok = false; }
  n = env.active().length;
  if (n !== cfg.expectActive) { fail(cfg, `after show, active = ${n}, expected ${cfg.expectActive}`); ok = false; }

  env.doc.hidden = true; listener();
  if (env.active().length !== 0) { fail(cfg, "second hide did not clear timers"); ok = false; }
  env.doc.hidden = false; listener();
  await flush();
  n = env.active().length;
  if (n !== cfg.expectActive) { fail(cfg, `after second show, active = ${n}, expected ${cfg.expectActive} (leak)`); ok = false; }

  return ok;
}

async function runUsageNextAt(cfg) {
  const env = makeEnv();
  env.install();
  const mod = new Function(extractPage(cfg.file) + "\n" + cfg.toString(cfg));
  const api = mod();
  const listener = (env.listeners.visibilitychange || [])[0];
  await flush(); // let the boot load() settle

  // nextAt must be advanced up front in refresh(); the tick must not fire a
  // second refresh on resume when the tab was hidden for longer than AUTO.
  // Expose nextAt so we can simulate that "stale on resume" condition.
  const stale = Date.now() - 10000;
  api.set_nextAt(stale);

  const before = env.fetches.length;
  env.doc.hidden = false; listener();       // show -> refresh() + restart tick
  // Invoke the restarted tick immediately, before the async load().finally
  // (in the unfixed page) gets a chance to advance nextAt.
  const tick = env.intervals.at(-1).fn;
  tick();
  await flush();

  // fetchFleet() also fetches "/api/usage?range=all", so count only the page's
  // own daily refresh (range=1h) — a second one means the restarted tick fired
  // an extra refresh() because nextAt was stale on resume.
  const usageDeltas = env.fetches.slice(before).filter(u => u.includes("/api/usage?range=1h")).length;
  if (usageDeltas !== 1) {
    fail(cfg, `resume triggered ${usageDeltas} refresh() calls, expected 1 (duplicate auto-refresh on show)`);
    return false;
  }
  return true;
}

// ---- main -------------------------------------------------------------------
let rc = 0;
for (const cfg of PAGES) {
  let ok;
  if (cfg.file === "index.html") ok = await runIndex(cfg);
  else if (cfg.file === "usage.html") {
    ok = await runIntervalPages(cfg);
    if (ok) ok = await runUsageNextAt(cfg);
  } else ok = await runIntervalPages(cfg);
  if (!ok) rc = 1;
}

if (rc === 0) console.log("visibility_check: PASS");
else console.error("visibility_check: FAIL");
process.exit(rc);
