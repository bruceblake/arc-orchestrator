"use strict";
const $ = s => document.querySelector(s);
let SHOW_ARCHIVED = false, PHASE_FILT = "", OPEN_PHASES = {};
let PROJECTS = [], AGENTS = [], RECENT = [], FLEET = null;
let CUR = null, CUR_DATA = null, CUR_PID = null, detailTimer = null, drawerFile = null;
let FILT = {r: "", s: "", m: "", q: ""}, EXPANDED = "";
// Glyph cue that survives greyscale (colour alone must not carry status).
const STATUSG = {merged: " ✓", done: " ✓", running: " …", failed: " ✗", conflict: " !", pending: " ○"};

const fmtT = iso => { if (!iso) return "—"; const s = (Date.now() - new Date(iso).getTime())/1000;
  if (s < 60) return Math.round(s)+"s ago"; if (s < 3600) return Math.round(s/60)+"m ago";
  if (s < 86400) return (s/3600).toFixed(1)+"h ago"; return (s/86400).toFixed(1)+"d ago"; };
const AGO = ts => { if (!ts) return ""; const d = Date.now()/1000 - ts;
  return d < 60 ? Math.round(d)+"s" : d < 3600 ? Math.round(d/60)+"m" : (d/3600).toFixed(1)+"h"; };
// esc() (common.js) does not escape quotes; attr() is the attribute-safe variant
const attr = s => esc(s).replace(/"/g, "&quot;");
async function jget(u) { const r = await fetch(u, {cache:"no-store"}); return r.json(); }
async function jpost(u, body) { const r = await fetch(u, {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)}); return {code: r.status, body: await r.json()}; }
function chip(status, n) { return `<span class="chip ${status}">${status} ${n}</span>`; }
const repoShort = r => !r ? "(no repo)" : r.split("/").filter(Boolean).slice(-2).join("/");

// Reliability: a failing poll flips the stale badge but never blanks the UI.
const FAILS = new Set();
function markFail(name, failed) {
  if (failed) FAILS.add(name); else FAILS.delete(name);
  $("#stale").style.display = FAILS.size ? "" : "none";
}

// ---- filters (persisted in the URL hash) ----
function loadHash() {
  const h = new URLSearchParams(location.hash.slice(1));
  FILT.r = h.get("r") || ""; FILT.s = h.get("s") || ""; FILT.m = h.get("m") || ""; FILT.q = h.get("q") || "";
  EXPANDED = h.get("x") || "";
}
function saveHash() {
  const h = new URLSearchParams();
  if (FILT.r) h.set("r", FILT.r);
  if (FILT.s) h.set("s", FILT.s);
  if (FILT.m) h.set("m", FILT.m);
  if (FILT.q) h.set("q", FILT.q);
  if (EXPANDED) h.set("x", EXPANDED);
  history.replaceState(null, "", h.toString() ? "#" + h.toString() : location.pathname);
}
function syncFilterControls() {
  $("#f-repo").value = FILT.r; $("#f-status").value = FILT.s; $("#f-model").value = FILT.m;
  if ($("#f-q").value !== FILT.q) $("#f-q").value = FILT.q;
}
$("#f-repo").onchange = e => { FILT.r = e.target.value; saveHash(); renderProjects(); };
$("#f-status").onchange = e => { FILT.s = e.target.value; saveHash(); renderProjects(); };
$("#f-model").onchange = e => { FILT.m = e.target.value; saveHash(); renderProjects(); renderAgents(); };
$("#f-q").oninput = e => { FILT.q = e.target.value; saveHash(); renderProjects(); };
const FOUR_MODELS = ["gpt-oss-120b", "DeepSeek-V4-Flash", "GLM-5.3", "Kimi-K3"];
function rebuildFilterOptions() {
  const repos = [...new Set(PROJECTS.map(p => p.repo || ""))].sort();
  $("#f-repo").style.display = repos.length > 1 ? "" : "none";
  setOptions("#f-repo", [["", "All repos"], ...repos.map(r => [r, repoShort(r)])], FILT.r);
  const models = new Set(AGENTS.filter(a => a.task && a.model).map(a => a.model));
  (FLEET ? FLEET.models || [] : []).forEach(m => models.add(m.model));
  const list = [...FOUR_MODELS, ...[...models].filter(m => !FOUR_MODELS.includes(m)).sort()];
  setOptions("#f-model", [["", "All models"], ...list.map(m => [m, short(m)])], FILT.m);
}
function setOptions(sel, pairs, keep) {
  const el = $(sel), cur = el.value;
  el.innerHTML = pairs.map(([v, l]) => `<option value="${attr(v)}">${esc(l)}</option>`).join("");
  el.value = [...el.options].some(o => o.value === keep) ? keep : "";
  if (el.value !== keep && keep) { // selected filter value disappeared — drop it
    if (sel === "#f-repo") FILT.r = ""; if (sel === "#f-model") FILT.m = "";
    saveHash();
  } else if (cur !== el.value) { /* normalized */ }
}

const revShort = r => r === "kimi" ? "Kimi K3" : r === "glm" ? "GLM 5.3" : short(r);


let GH = null;

let SLOT_VIEW = "all", SLOTS = null;
