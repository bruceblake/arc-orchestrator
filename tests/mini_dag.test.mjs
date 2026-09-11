// Behavioral test for the mini-DAG in project cards (task mini-dag-fix).
// A UI audit measured mini-DAGs from 21px to 420px tall inside cards, with
// wide graphs scaling 9px labels down to ~3.5px — illegible. The contract:
//   1. projects with fewer than 3 tasks render NO dag (a single box teaches
//      nothing): taskDag(mini) returns "" so the card stays compact.
//   2. rendered height is capped (~120px): extra rows collapse into a
//      "+N more" text node instead of stretching the viewBox.
//   3. labels never fall below ~7px effective once the viewBox scales into
//      the ~336px card: node width/font come from the column count, and
//      labels drop entirely (coloured boxes stay) when they would be smaller.
// Status colours and fix-loop self-arcs must survive unchanged.
// Runs the page's real scripts in a minimal DOM stub (same harness pattern
// as tests/projects_ui.test.mjs) — no dashboard needed. Exits 1 on failure.
import fs from "node:fs";

const els = new Map();
const mk = id => {
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
  el.querySelector = () => null;
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
globalThis.history = { replaceState(){}, pushState(){} };
Object.defineProperty(globalThis, "navigator", {value: {clipboard: {writeText: async () => {}}}, configurable: true});
globalThis.confirm = () => false; globalThis.alert = () => {};
globalThis.setInterval = () => 0; globalThis.setTimeout = () => 0; globalThis.clearInterval = () => 0;
globalThis.fetch = async () => ({ status: 200, json: async () => ({}) });

const src = fs.readFileSync(new URL("../static/index.html", import.meta.url), "utf8");
const js = src.slice(src.indexOf("<script>") + 8, src.lastIndexOf("</script>"));
const externals = [...src.matchAll(/<script[^>]+src="([^"]+)"/g)]
  .map(m => "static/" + m[1].replace(/^\//, ""))
  .filter(f => fs.existsSync(f))
  .map(f => fs.readFileSync(f, "utf8"))
  .join("\n");
const mod = new Function(externals + "\n" + js + "\nreturn {taskDag, card};");
const api = mod();

let good = 0;
const bad = [];
const ok = (name, cond) => { if (cond) good++; else bad.push(name); };

// ---- fixtures ----
const dag = (ids, edges, extra) => ({
  nodes: ids.map(id => Object.assign({id, status: "pending"}, ((extra || {})[id]) || {})),
  edges: edges || [] });
const chain = ids => ids.slice(1).map((id, i) => ({src: ids[i], dst: id}));
const mini = d => api.taskDag(d, {size: "mini", file: "p.json"});
const box = svg => { const m = /viewBox="0 0 ([\d.]+) ([\d.]+)"/.exec(svg || ""); return m && {w: +m[1], h: +m[2]}; };
const CARDW = 336; // card width the viewBox scales into (from the audit)

// ==== 1. fewer than 3 tasks: no picture at all ====
ok("no dag for a 1-task project", mini(dag(["a"])) === "");
ok("no dag for a 2-task project", mini(dag(["a", "b"], chain(["a", "b"]))) === "");
const trio = mini(dag(["a", "b", "c"], chain(["a", "b", "c"])));
ok("3 tasks still render", trio.startsWith("<svg"));

// ==== 2. height cap ~120px with a "+N more" node ====
const tall = mini(dag(["t1", "t2", "t3", "t4", "t5", "t6"]));
const tb = box(tall);
ok("tall graph height capped near 120px", !!tb && tb.h <= 128);
ok("tall graph says '+4 more'", /\+4 more/.test(tall));
ok("first rows still drawn, overflow rows are not",
   tall.includes('data-t="t1"') && tall.includes('data-t="t2"') && !tall.includes('data-t="t6"'));

// ==== 3. effective label size >= ~7px ====
// 3 columns: labels stay, and must survive the card scale.
const eff = svg => { const b = box(svg); const f = Math.min(...[...svg.matchAll(/<text[^>]+font-size="([\d.]+)"/g)].map(m => +m[1]));
  return f * CARDW / b.w; };
ok("3-column graph keeps labels at >= ~7px effective",
   trio.includes(">a") && eff(trio) >= 6.95);
// 5 columns: no node box could hold a 7px-effective label -> text dropped,
// coloured status boxes kept.
const wide = mini(dag(["w1", "w2", "w3", "w4", "w5"], chain(["w1", "w2", "w3", "w4", "w5"]),
                      {w2: {status: "merged"}}));
ok("5-column graph drops illegible labels", !wide.includes("<text"));
ok("coloured boxes survive when labels drop",
   wide.includes('stroke="#3fb950"') && wide.includes('stroke="#484f58"'));

// ==== guards: status colours and fix-loop self-arcs unchanged ====
const col = mini(dag(["m1", "m2", "m3"], chain(["m1", "m2", "m3"]),
                     {m1: {status: "merged"}, m2: {status: "failed"}, m3: {status: "conflict"}}));
ok("status colours kept",
   col.includes('stroke="#3fb950"') && col.includes('stroke="#f85149"') && col.includes('stroke="#f0883e"'));
const loop = mini(dag(["l1", "l2", "l3"], chain(["l1", "l2", "l3"]), {l2: {attempts: 2, escalations: 1}}));
ok("fix-loop self-arc kept",
   loop.includes('stroke="#d29922"') && loop.includes(">x2 ⬆1<"));

// ==== 4. card() integrates the mini dag ====
const mkProj = (ids, edges) => ({ file: "p.json", title: "proj", repo: "acme/p",
  archived: false, models: [], statuses: {}, progress: {done: 0, total: ids.length},
  n_tasks: ids.length, dag: dag(ids, edges), tokens: 0, seconds: 0, last_activity: null, errors: [] });
ok("card of a 2-task project has no svg (compact)",
   !api.card(mkProj(["a", "b"], chain(["a", "b"])), 0).includes("<svg"));
ok("card of a 3+ task project shows the mini dag",
   api.card(mkProj(["a", "b", "c", "d"], chain(["a", "b", "c", "d"])), 0).includes("<svg"));

console.log(`mini_dag: ${good} passed, ${bad.length} failed`);
if (bad.length) {
  console.error("mini_dag: FAIL — " + bad.join("; "));
  process.exit(1);
}
console.log("mini_dag: PASS");
