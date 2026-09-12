"""archived-attention: archiving must never hide unresolved work.

dag-chaining.json was observed archived:true AND phase:'attention' — a project
with unresolved failures vanished from the console entirely. Two guards pin
the contract:

1. dashboard._archive_project must WARN (never block) when the archived
   project still has failed/conflict task rows.
2. static/panels/projects.js must keep archived 'attention' projects visible
   in the active list (SHOW_ARCHIVED false) with a clear 'archived' marker,
   and must surface the API's warning to the operator instead of swallowing it.

The panel test runs the page's REAL JavaScript in node against a minimal DOM
stub (same harness pattern as tests/projects_ui.test.mjs) — greps proved
nothing here: the previous gate matched a string the untouched file already
contained, the implementer had nothing to prove, and the task died as
'no changes to publish'.
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import capture_events  # noqa: F401  (sys.path)
from helpers import ENTRY, STRONGEST  # noqa: E402,F401

import config
import dashboard
from store import Store

ROOT = Path(__file__).resolve().parent.parent

NODE_PANEL_SCRIPT = r"""
import fs from "node:fs";
const ROOT = process.argv[2];

// --- minimal DOM stub (same pattern as tests/projects_ui.test.mjs) ---
const els = new Map();
const mk = id => {
  let html = "";
  const el = { id, className: "", title: "", value: "", dataset: {}, checked: false,
                style: {}, disabled: false, tabIndex: 0, _qcache: new Map(),
                classList: {add(){},remove(){},contains:()=>false},
                querySelectorAll: sel => {
                  if (sel === "[data-arch]") {
                    if (!el._qcache.has(sel)) el._qcache.set(sel,
                      [...el.innerHTML.matchAll(/data-arch="([^"]+)" data-on="(\d)"/g)]
                        .map(m => ({dataset: {arch: m[1], on: m[2]}, onclick: null})));
                    return el._qcache.get(sel);
                  }
                  return [];
                },
                appendChild(){}, after(){}, setAttribute(){}, onclick: null };
  Object.defineProperty(el, "innerHTML", { get() { return html; },
    set(v) { html = String(v == null ? "" : v); el._qcache = new Map(); } });
  Object.defineProperty(el, "textContent", { get() { return html.replace(/<[^>]*>/g, ""); },
    set(v) { html = String(v == null ? "" : v); } });
  Object.defineProperty(el, "options", { get() {
    return [...el.innerHTML.matchAll(/<option value="([^"]*)"/g)].map(m => ({value: m[1]})); } });
  el.querySelector = sel => { const m = /data-file="(.+?)"/.exec(sel || ""); if (!m) return null;
    const f = m[1].replace(/\\(.)/g, "$1");
    return el.innerHTML.includes(`data-file="${f}"`) ? mk("row:" + f) : null; };
  return el; };
globalThis.document = {
  querySelector: sel => { const id = sel.replace(/^#/, ""); if (!els.has(id)) els.set(id, mk(id)); return els.get(id); },
  // renderProjects binds buttons via document.querySelectorAll("#projects [data-arch]")
  querySelectorAll: sel => { const m = /^(#\S+) (\[data-arch\])$/.exec(sel);
    return m ? globalThis.document.querySelector(m[1]).querySelectorAll(m[2]) : []; },
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
globalThis.confirm = () => true;
globalThis.setInterval = () => 0; globalThis.setTimeout = () => 0; globalThis.clearInterval = () => 0;

const ALERTS = [], POSTS = [];
globalThis.alert = m => ALERTS.push(String(m));

// --- fixtures: the exact regression shape — archived:true AND phase:'attention' ---
const NOW = new Date().toISOString();
const mkProj = (file, title, phase, extra) => Object.assign({ file, title, phase,
  repo: "acme/arc-orchestrator", archived: false, models: [], statuses: {},
  progress: {done: 0, total: 2}, n_tasks: 2, dag: {nodes: [], edges: []},
  tokens: 0, seconds: 0, last_activity: NOW, errors: [] }, extra);
const FIX = { projects: [
  mkProj("dag-chaining.json", "dag chaining", "attention", { archived: true,
    statuses: {merged: 2, failed: 1}, progress: {done: 2, total: 3}, n_tasks: 3,
    dag: {nodes: [{id: "c-a", status: "merged"}, {id: "c-b", status: "failed"}],
          edges: [{src: "c-a", dst: "c-b"}]} }),
  mkProj("old-done.json", "finished long ago", "done", { archived: true,
    statuses: {merged: 2}, progress: {done: 2, total: 2} }),
  mkProj("live.json", "broken but active", "attention", { statuses: {failed: 1, merged: 1},
    dag: {nodes: [{id: "l-a", status: "failed"}], edges: []} }),
]};
globalThis.fetch = async (u, opts) => {
  if (opts && opts.method === "POST") { POSTS.push(JSON.parse(opts.body));
    return { status: 200, json: async () => ({ file: "live.json", archived: true,
      warning: "1 task(s) ended failed/conflict — archiving hides this project from the active list" }) }; }
  const s = String(u);
  return { status: 200, json: async () => s.includes("/api/projects") ? FIX : {} };
};

const src = fs.readFileSync(ROOT + "/static/index.html", "utf8");
const js = src.slice(src.indexOf("<script>") + 8, src.lastIndexOf("</script>"));
const externals = [...src.matchAll(/<script[^>]+src="([^"]+)"/g)]
  .map(m => ROOT + "/static/" + m[1].replace(/^\//, ""))
  .filter(f => fs.existsSync(f))
  .map(f => fs.readFileSync(f, "utf8"))
  .join("\n");
const mod = new Function(externals + "\n" + js + "\nreturn { card, renderProjects, pollProjects, "
  + "visibleProjects: typeof visibleProjects === 'function' ? visibleProjects : null };");
const api = mod();

let good = 0;
const bad = [];
const ok = (name, cond) => { if (cond) good++; else bad.push(name); };

// SHOW_ARCHIVED defaults to false — the active console view.
await api.pollProjects();
const html = () => document.querySelector("#projects").innerHTML;
const vis = api.visibleProjects ? api.visibleProjects().map(p => p.file) : null;

ok("archived 'attention' project stays visible when archived view is off",
   vis === null ? html().includes('data-file="dag-chaining.json"') : vis.includes("dag-chaining.json"));
ok("archived 'attention' project renders in the needs-attention group",
   html().includes('<div class="phasehead attention">') && html().includes("dag chaining"));
ok("its card carries a clear archived marker",
   html().includes(">archived</span>"));
ok("archived-and-DONE stays hidden (only unresolved work leaks through)",
   vis === null ? !html().includes("old-done.json") : !vis.includes("old-done.json"));
const pf = document.querySelector("#phase-filter").innerHTML;
ok("the needs-attention count includes archived-but-unresolved projects",
   pf.includes("needs attention 2"));

// The archive action must surface the API warning, not swallow it.
const btn = document.querySelector("#projects").querySelectorAll("[data-arch]")
  .find(b => b.dataset.arch === "live.json");
ok("an archive button rendered for the active project", !!btn);
if (btn) {
  await btn.onclick({ stopPropagation(){} });
  ok("archive POST went out", POSTS.length === 1 && POSTS[0].file === "live.json" && POSTS[0].archived === true);
  ok("the operator SEES the warning", ALERTS.some(a => a.includes("ended failed/conflict")));
}

console.log(`archived_attention_panel: ${good} passed, ${bad.length} failed`);
if (bad.length) { console.error("FAIL — " + bad.join("; ")); process.exit(1); }
"""


class ArchivedAttentionPhase(unittest.TestCase):
    """The JS predicate is p.phase === "attention"; the server's _project_phase
    must put failed AND conflict projects there, or the panel fix cannot see them."""

    def test_failed_tasks_mean_attention(self):
        self.assertEqual(
            dashboard._project_phase({"merged": 2, "failed": 1}, ["a", "b", "c"], None),
            "attention")

    def test_conflict_tasks_mean_attention(self):
        self.assertEqual(dashboard._project_phase({"conflict": 1}, ["a"], None), "attention")

    def test_fully_merged_is_done_not_attention(self):
        self.assertEqual(dashboard._project_phase({"merged": 2}, ["a", "b"], None), "done")


class ArchivedAttentionArchiveApi(unittest.TestCase):
    """_archive_project warns — but never blocks — on unresolved work."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)
        self._orig_tasks = config.TASKS_DIR
        config.TASKS_DIR = str(self.tmp / "tasks")
        Path(config.TASKS_DIR).mkdir(parents=True)
        self._orig_store = dashboard.Handler.store
        dashboard.Handler.store = Store(":memory:")
        self._live_patch = mock.patch("reconcile.live_runs", return_value=[])
        self._live_patch.start()

    def tearDown(self):
        self._live_patch.stop()
        st = dashboard.Handler.store
        dashboard.Handler.store = self._orig_store
        st.conn.close()
        config.TASKS_DIR = self._orig_tasks
        self._dir.cleanup()

    def _taskfile(self, name="proj.json"):
        path = Path(config.TASKS_DIR) / name
        path.write_text(json.dumps({"project": {"repo": "acme/x", "title": "t",
                                                "tasks": [{"id": "t1"}]}}))
        return path

    def test_warns_but_still_archives_a_project_with_failed_tasks(self):
        path = self._taskfile()
        dashboard.Handler.store.upsert_code_task(
            str(path), "t1", "one", ENTRY, "kimi", "failed")
        resp, code = dashboard._archive_project({"file": "proj.json", "archived": True})
        self.assertEqual(code, 200)
        self.assertIn("warning", resp)
        self.assertIn("1 task(s) ended failed/conflict", resp["warning"])
        # A warning must not block: the operator may archive deliberately.
        self.assertTrue(resp["archived"])
        self.assertIn(str(path), dashboard.Handler.store.archived_projects())

    def test_warns_for_conflict_tasks_too(self):
        path = self._taskfile()
        dashboard.Handler.store.upsert_code_task(
            str(path), "t1", "one", "GLM-5.3", "kimi", "conflict")
        dashboard.Handler.store.upsert_code_task(
            str(path), "t2", "two", "GLM-5.3", "kimi", "failed")
        dashboard.Handler.store.upsert_code_task(
            str(path), "t3", "three", "GLM-5.3", "kimi", "merged")
        resp, code = dashboard._archive_project({"file": "proj.json", "archived": True})
        self.assertEqual(code, 200)
        self.assertIn("2 task(s) ended failed/conflict", resp["warning"])

    def test_no_warning_when_nothing_failed(self):
        path = self._taskfile()
        dashboard.Handler.store.upsert_code_task(
            str(path), "t1", "one", "GLM-5.3", "kimi", "merged")
        resp, code = dashboard._archive_project({"file": "proj.json", "archived": True})
        self.assertEqual(code, 200)
        self.assertNotIn("warning", resp)

    def test_restoring_never_warns(self):
        path = self._taskfile()
        dashboard.Handler.store.upsert_code_task(
            str(path), "t1", "one", "GLM-5.3", "kimi", "failed")
        dashboard.Handler.store.set_project_archived(str(path), True)
        resp, code = dashboard._archive_project({"file": "proj.json", "archived": False})
        self.assertEqual(code, 200)
        self.assertNotIn("warning", resp)


class ArchivedAttentionPanel(unittest.TestCase):
    """The console must show an archived-but-unresolved project — with a marker —
    and must surface the archive warning. Runs the shipped JS, not greps."""

    def test_panel_surfaces_archived_attention_projects_and_warnings(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed (same skip as check.sh's JS checks)")
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "panel_check.mjs"
            script.write_text(NODE_PANEL_SCRIPT, encoding="utf-8")
            proc = subprocess.run([node, str(script), str(ROOT)],
                                  capture_output=True, text=True, timeout=120,
                                  cwd=str(ROOT))
        self.assertEqual(proc.returncode, 0,
                         "panel behavior check failed:\n" + proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
