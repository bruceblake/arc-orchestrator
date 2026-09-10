// Render the dashboard's own JavaScript against real API payloads in a minimal
// DOM stub. `node --check` only proves the file parses; this proves the render
// path actually runs on live data and that user-controlled strings stay escaped.
// It caught an agent's rewrite renaming functions the page still called.
//
//   node tests/render_check.mjs <health.json> <projects.json> <project.json>
import fs from "node:fs";
// Minimal DOM stub: every $("#id") returns a recording element.
const els = new Map();
const mk = id => ({ id, innerHTML: "", textContent: "", className: "", title: "", value: "", options: [], dataset: {}, checked: false,
                    style: {}, disabled: false, classList: {add(){},remove(){},contains:()=>false},
                    querySelectorAll: () => [], appendChild(){}, onclick: null });
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
// The page loads its shared helpers (esc, short, fmtK, ...) from common.js
// before the inline script; do the same so they are defined here too.
const common = fs.readFileSync(new URL("../static/common.js", import.meta.url), "utf8");
const js = src.slice(src.indexOf("<script>") + 8, src.lastIndexOf("</script>"));
const mod = new Function(common + "\n" + js + "\nreturn {renderHealth, card, renderTasks, renderDag, renderFeed, taskDag, friendly, esc, renderProjects};");
const api = mod();

const health = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
api.renderHealth(health);
const hm = document.querySelector("#health-models").innerHTML;
console.log("health models rendered:", (hm.match(/class="mcap/g) || []).length, "chips");
console.log("header summary:", document.querySelector("#health-sum").textContent);
console.log("meta:", document.querySelector("#health-meta").textContent);
console.log("problems rendered:", (document.querySelector("#health-problems").innerHTML.match(/<div>/g) || []).length);

const projs = JSON.parse(fs.readFileSync(process.argv[3], "utf8")).projects;
let cards = projs.map((p, i) => api.card(p, i)).join("");
console.log("cards rendered:", projs.length, "| card html length:", cards.length);
console.log("cards showing a failure note:", (cards.match(/class="cardnote"/g) || []).length);

const d = JSON.parse(fs.readFileSync(process.argv[4], "utf8"));
api.renderDag(d); api.renderFeed(d);
console.log("dag svg:", document.querySelector("#dag").innerHTML.slice(0, 40) + "...");
console.log("feed entries:", (document.querySelector("#feed").innerHTML.match(/border-bottom/g) || []).length);

// XSS / escaping probe: a task title containing markup must not become markup.
const evil = { file: "x.json", title: '<img src=x onerror=alert(1)>', statuses: {}, n_tasks: 1,
               progress: {done:0,total:1}, dag: {nodes:[],edges:[]}, tokens: 0, seconds: 0,
               models: [], reviewers: [], errors: [{id:"<b>bad</b>", error:"<script>x</script>"}],
               last_activity: null };
const out = api.card(evil, 0);


if (!out.includes("<img src=x") && !out.includes("<script>x")) {
  console.log("render_check: PASS");
} else {
  console.error("render_check: FAIL — unescaped user content reached the DOM");
  process.exit(1);
}
