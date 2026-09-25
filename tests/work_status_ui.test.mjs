import fs from "node:fs";
import assert from "node:assert/strict";

const elements = new Map();
const el = id => {
  if (!elements.has(id)) elements.set(id, {value:"", innerHTML:"", textContent:"", dataset:{}, onclick:null, onchange:null});
  return elements.get(id);
};
const $ = selector => el(selector);
const esc = s => String(s ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
const attr = s => esc(s).replaceAll('"', "&quot;");
const data = {now: 1780000000, projects:[
  {file:"base.json", title:"Base", phase:"running", tasks:[
    {id:"a", title:"make base", status:"merged", activity:"done", stage:"pr_merge"}],
   dag:{nodes:[{id:"a"}],edges:[]}},
  {file:"follow.json", title:"Follow <up>", phase:"running",
   chain:{deps:[{file:"base.json", title:"Base", state:"waiting", merged:1, n_tasks:2}]},
   tasks:[
     {id:"b", title:"build", status:"running", stage:"implement", activity:"usage_wait", reason:"Usage limit resets at 18:00", blocked_by:[]},
     {id:"c", title:"review", status:"running", stage:"pr_reviewer", activity:"working", reason:"DeepSeek is reviewing", role:"reviewer", model:"DeepSeek-V4.1-Flash-thinking-max", agents:[{role:"pr_reviewer", model:"DeepSeek-V4.1-Flash-thinking-max"},{role:"pr_reviewer", model:"GLM-5.3"}]},
     {id:"d", title:"next", status:"pending", stage:"dependency_wait", activity:"waiting", blocked_by:["b"]},
     {id:"e", title:"stalled", status:"running", stage:"implement", activity:"stalled", reason:"no recent agent heartbeat"}],
   dag:{nodes:[{id:"b"},{id:"c"},{id:"d"}],edges:[{src:"b",dst:"d"}]}},
]};
const js = fs.readFileSync("static/panels/work_status.js", "utf8");
let opened = "", transcript = "";
const api = new Function("$", "esc", "attr", "taskDag", "bindDagClicks", "short", "tick", "jget", "markFail", "showTab", "openDetail", "openTaskTranscript", "PROJECTS", "FILT", "PHASE_FILT", "SHOW_ARCHIVED", "data",
  js + "\nreturn {renderWorkStatus, workCount, workMatch, workProject, pollWorkStatus};")(
  $, esc, attr,
  dag => `<svg data-nodes="${dag.nodes.length}" data-edges="${dag.edges.length}"></svg>`,
  () => {}, x => x, x => `${x}s`, async () => data, () => {}, () => {}, async f => {opened=f;}, (f,t) => {transcript=f+":"+t;},
  data.projects, {}, "", false, data);

api.renderWorkStatus(data);
assert.deepEqual(api.workCount(data.projects), {working:1, waiting:2, attention:1, done:1});
const html = el("#work-map").innerHTML;
assert.match(html, /Usage limit resets at 18:00/);
assert.match(html, /PR reviewer/);
assert.match(html, /Waiting for b/);
assert.match(html, /no recent agent heartbeat/);
assert.match(html, /pr_reviewer: DeepSeek-V4.1-Flash-thinking-max; pr_reviewer: GLM-5.3/);
assert.match(html, /data-work-project="base.json"/);
assert.match(html, /data-edges="1"/);
assert.ok(!html.includes("Follow <up>"), "project title must be escaped");
assert.match(html, /Follow &lt;up&gt;/);
assert.match(el("#work-totals").innerHTML, /1 needs attention/);

el("#work-filter").value = "attention";
api.renderWorkStatus(data);
assert.match(el("#work-map").innerHTML, /no recent agent heartbeat/);
assert.ok(!el("#work-map").innerHTML.includes("Waiting for b"));
assert.ok(!el("#work-map").innerHTML.includes("Usage limit resets at 18:00"));
el("#work-filter").value = "active";
api.renderWorkStatus(data);
assert.ok(!el("#work-map").innerHTML.includes("make base"));
assert.match(el("#work-map").innerHTML, /Usage limit resets at 18:00/);

await api.pollWorkStatus();
assert.match(el("#work-map").innerHTML, /data-edges="1"/);
el("#work-map").onclick({target:{closest:() => ({dataset:{workFile:"follow.json", workTask:"c"}})}});
await new Promise(resolve => setImmediate(resolve));
assert.equal(opened, "follow.json");
assert.equal(transcript, "follow.json:c");
console.log("work status UI: passed");
