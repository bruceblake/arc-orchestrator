// Render assertions for the #N issue links. The API tests check JSON fields;
// these call the panel functions and require the markup a reader sees:
// the DAG tooltip and anchor, the timeline drawer, the Messages channel,
// and the live task-map row.
import fs from "node:fs";
import assert from "node:assert/strict";

const elements = new Map();
function make(id) {
  let html = "";
  const el = {
    id, value: "", scrollTop: 0, scrollHeight: 0, clientHeight: 0,
    style: {}, dataset: {}, hidden: false, className: "", title: "",
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    querySelectorAll: () => [],
    addEventListener() {},
    setAttribute() {},
    closest: () => null,
  };
  Object.defineProperty(el, "innerHTML", {
    get() { return html; },
    set(v) { html = String(v == null ? "" : v); },
  });
  Object.defineProperty(el, "textContent", {
    get() { return html.replace(/<[^>]*>/g, ""); },
    set(v) { html = String(v == null ? "" : v); },
  });
  Object.defineProperty(el, "options", { get() {
    return [...html.matchAll(/<option value="([^"]*)"/g)].map(m => ({ value: m[1] }));
  } });
  return el;
}
const $ = sel => {
  const id = String(sel).replace(/^#/, "");
  if (!elements.has(id)) elements.set(id, make(id));
  return elements.get(id);
};
globalThis.document = {
  querySelector: $,
  getElementById: id => $(id),
  querySelectorAll: () => [],
  addEventListener() {},
  createElement: () => make("new"),
};
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.location = { host: "localhost", hash: "", search: "", pathname: "/" };
globalThis.history = { replaceState() {} };
globalThis.setInterval = () => 0;
globalThis.clearInterval = () => {};

const files = [
  "static/common.js",
  "static/panels/state.js",
  "static/panels/projects.js",
  "static/panels/timeline.js",
  "static/panels/board.js",
  "static/panels/work_status.js",
];
const src = files.map(f => fs.readFileSync(f, "utf8")).join("\n");
const api = new Function(
  src + "\nreturn {taskDag, renderTimeline, boardPaint, workTaskRow};",
)();

const url = "https://github.com/acme/widgets/issues/4";
const node = {
  id: "doors", title: "Doors", status: "running",
  model: "GLM-5.3", reviewer: "deepseek", issue: 4, issue_url: url,
};
const full = api.taskDag({ nodes: [node], edges: [] }, { size: "full", file: "demo.json" });
assert.match(full, /<title>[\s\S]*#4 https:\/\/github.com\/acme\/widgets\/issues\/4/);
assert.match(full, /<a href="https:\/\/github.com\/acme\/widgets\/issues\/4"[^>]*>[\s\S]*#4/);

const bare = api.taskDag({
  nodes: [{ id: "doors", status: "running", model: "GLM-5.3", reviewer: "deepseek" }],
  edges: [],
}, { size: "full" });
assert.doesNotMatch(bare, /issues\/4/);
assert.doesNotMatch(bare, />#4</);

api.renderTimeline({
  id: "doors", issue: 4, issue_url: url, counts: {}, entries: [],
});
assert.match(
  $("#drawer-sub").innerHTML,
  /<a href="https:\/\/github.com\/acme\/widgets\/issues\/4"[^>]*>#4<\/a>/,
);

api.boardPaint({
  project: "widgets",
  channel: "task:doors",
  projects: [{ project: "widgets" }],
  channels: [{ channel: "task:doors", status: "running", issue: 4, issue_url: url }],
  messages: [],
  claims: [],
});
assert.match(
  $("#bd-rail").innerHTML,
  /task:doors[\s\S]*<a href="https:\/\/github.com\/acme\/widgets\/issues\/4"[^>]*>#4<\/a>/,
);

const row = api.workTaskRow({
  id: "doors", title: "Doors", status: "running", activity: "working",
  issue: 4, issue_url: url,
}, "demo.json");
assert.match(row, /<a href="https:\/\/github.com\/acme\/widgets\/issues\/4"[^>]*>#4<\/a>/);

console.log("issue link render ok");
