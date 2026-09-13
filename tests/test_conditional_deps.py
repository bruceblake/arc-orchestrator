"""Conditional deps: task.when, probe_cmd, verdicts, and the skip branch.

Until now the graph between tasks was unconditional — every task written ran.
`when` lets a task run only if a dependency's probe verdict says so, which is
what a one-taskfile router needs. These tests pin the contract end to end:
the loader's rules, the predicate, how the edges are wired, what happens at
runtime when the condition fails (the branch and everything downstream are
recorded skipped), and that a resumed run re-reads the same verdict.
"""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helpers import FakeStore, capture_events, ENTRY, ENTRY_REVIEWER  # noqa: E402

import code_tasks  # noqa: E402
import config  # noqa: E402
import graph_shapes  # noqa: E402
from store import Store  # noqa: E402


def taskfile(tasks, repo="/tmp", pattern="router"):
    """Write a taskfile to a temp path and return it."""
    fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"project": {"repo": repo, "title": "t", "pattern": pattern, "tasks": tasks}}, fh)
    fh.close()
    return fh.name


def T(i, deps=(), **kw):
    return {"id": i, "title": i, "prompt": "p", "model": ENTRY, "reviewer": ENTRY_REVIEWER,
            "deps": list(deps), **kw}


ROUTER = [
    T("probe", verify_cmd="true", probe_cmd="echo '{\"area\": \"frontend\", \"n\": 2}'"),
    T("fix-fe", ["probe"], when={"dep": "probe", "key": "area", "equals": "frontend"}),
    T("fix-be", ["probe"], when={"dep": "probe", "key": "area", "equals": "backend"}),
    T("be-docs", ["fix-be"]),
]


class Loader(unittest.TestCase):
    def test_when_and_probe_cmd_are_loaded(self):
        ts = code_tasks.load_taskfile(taskfile(ROUTER))
        self.assertEqual(ts["tasks"]["probe"]["probe_cmd"], "echo '{\"area\": \"frontend\", \"n\": 2}'")
        self.assertEqual(ts["tasks"]["fix-fe"]["when"],
                         {"dep": "probe", "key": "area", "op": "equals", "value": "frontend"})
        self.assertIsNone(ts["tasks"]["be-docs"]["when"])

    def test_when_dep_must_be_a_dep(self):
        bad = [T("probe", probe_cmd="echo {}"), T("x", when={"dep": "probe", "key": "k", "equals": 1})]
        with self.assertRaises(ValueError) as cm:
            code_tasks.load_taskfile(taskfile(bad))
        self.assertIn("must also be listed in deps", str(cm.exception))

    def test_when_dep_must_have_a_probe(self):
        bad = [T("probe"), T("x", ["probe"], when={"dep": "probe", "key": "k", "equals": 1})]
        with self.assertRaises(ValueError) as cm:
            code_tasks.load_taskfile(taskfile(bad))
        self.assertIn("no probe_cmd", str(cm.exception))

    def test_when_needs_exactly_one_operator(self):
        for w in ({"dep": "probe", "key": "k"},
                  {"dep": "probe", "key": "k", "equals": 1, "in": [1]},
                  {"dep": "probe", "key": "k", "in": "notalist"},
                  {"dep": "probe", "key": "k", "truthy": "yes"},
                  {"dep": "", "key": "k", "equals": 1},
                  "probe.k == 1"):
            with self.subTest(when=w):
                with self.assertRaises(ValueError):
                    code_tasks.load_taskfile(taskfile(
                        [T("probe", probe_cmd="echo {}"), T("x", ["probe"], when=w)]))

    def test_describe_shows_the_condition_and_probe(self):
        text = code_tasks.describe(code_tasks.load_taskfile(taskfile(ROUTER)))
        self.assertIn('when=probe.area == "frontend"', text)
        self.assertIn("probe=echo", text)
        self.assertIn("graph: router", text)


class Predicate(unittest.TestCase):
    def w(self, **kw):
        return code_tasks._load_when("t", {"dep": "p", "key": kw.pop("key", "k"), **kw})

    def test_operators(self):
        H = code_tasks.when_holds
        self.assertTrue(H(self.w(equals="a"), {"k": "a"}))
        self.assertFalse(H(self.w(equals="a"), {"k": "b"}))
        self.assertFalse(H(self.w(equals="a"), {}))
        self.assertFalse(H(self.w(equals="a"), None))
        self.assertTrue(H(self.w(not_equals="a"), {"k": "b"}))
        self.assertTrue(H(self.w(not_equals="a"), {}))       # absent != a
        self.assertTrue(H(self.w(**{"in": [1, 2]}), {"k": 2}))
        self.assertFalse(H(self.w(**{"in": [1, 2]}), {"k": 3}))
        self.assertTrue(H(self.w(truthy=True), {"k": [1]}))
        self.assertFalse(H(self.w(truthy=True), {"k": 0}))
        self.assertTrue(H(self.w(truthy=False), {}))
        self.assertTrue(H(self.w(exists=True), {"k": None}))
        self.assertFalse(H(self.w(exists=True), {}))
        self.assertTrue(H(self.w(exists=False), {"other": 1}))

    def test_dotted_keys_reach_into_nested_verdicts(self):
        w = self.w(key="found.file", equals="a.py")
        self.assertTrue(code_tasks.when_holds(w, {"found": {"file": "a.py"}}))
        self.assertFalse(code_tasks.when_holds(w, {"found": "a.py"}))

    def test_text(self):
        self.assertEqual(code_tasks.when_text(self.w(equals="x")), 'p.k == "x"')
        self.assertEqual(code_tasks.when_text(self.w(**{"in": [1]})), "p.k in [1]")
        self.assertEqual(code_tasks.when_text(self.w(truthy=False)), "p.k is not truthy")


class Wiring(unittest.TestCase):
    def _graph(self, tasks, prior=()):
        ts = code_tasks.load_taskfile(taskfile(tasks))
        with capture_events():
            return code_tasks.build_code_graph(FakeStore(list(prior)), ts, taskfile="tf.json")

    def test_conditional_release_and_skip_edges(self):
        g = self._graph(ROUTER)
        for tid in ("fix-fe", "fix-be"):
            self.assertIn(f"skip_{tid}", g.nodes)
            out = {e.dst: e for e in g.edges if e.src == "pr_merge_probe" and e.dst.endswith(tid)}
            self.assertEqual(sorted(out), [f"alloc_{tid}", f"skip_{tid}"])
            self.assertTrue(all(e.when is not None for e in out.values()))
        # an unconditional dependent keeps its unconditional edge
        plain = [e for e in g.edges if e.src == "pr_merge_fix-be" and e.dst == "alloc_be-docs"]
        self.assertEqual(len(plain), 1)
        self.assertIsNone(plain[0].when)
        self.assertNotIn("skip_be-docs", g.nodes)

    def test_the_release_edge_reads_the_verdict_off_pr_merge(self):
        g = self._graph(ROUTER)
        rel = next(e for e in g.edges if e.src == "pr_merge_probe" and e.dst == "alloc_fix-fe")
        skip = next(e for e in g.edges if e.src == "pr_merge_probe" and e.dst == "skip_fix-fe")
        self.assertTrue(rel.when({"merged": True, "verdict": {"area": "frontend"}}, {}))
        self.assertFalse(skip.when({"merged": True, "verdict": {"area": "frontend"}}, {}))
        self.assertFalse(rel.when({"merged": True, "verdict": {"area": "backend"}}, {}))
        self.assertTrue(skip.when({"merged": True, "verdict": {"area": "backend"}}, {}))

    def test_a_join_carries_every_deps_verdict(self):
        tasks = [T("p1", probe_cmd="echo {}"), T("p2", probe_cmd="echo {}"),
                 T("x", ["p1", "p2"], when={"dep": "p2", "key": "go", "truthy": True})]
        g = self._graph(tasks)
        self.assertIn("join_x", g.nodes)
        rel = next(e for e in g.edges if e.src == "join_x" and e.dst == "alloc_x")
        self.assertTrue(rel.when({"verdicts": {"p1": None, "p2": {"go": 1}}}, {}))
        self.assertFalse(rel.when({"verdicts": {"p1": {"go": 1}, "p2": {"go": 0}}}, {}))
        res = asyncio.run(g.nodes["join_x"].fn({"results": {
            "pr_merge_p1": {"merged": True, "verdict": {"a": 1}},
            "pr_merge_p2": {"merged": True, "verdict": {"go": 1}}}}))
        self.assertEqual(res["verdicts"], {"p1": {"a": 1}, "p2": {"go": 1}})

    def test_a_resumed_merged_probe_replays_its_stored_verdict(self):
        prior = [{"id": "probe", "status": "merged", "model": ENTRY, "error": None,
                  "verdict": json.dumps({"area": "backend"})}]
        g = self._graph(ROUTER, prior)
        res = asyncio.run(g.nodes["pr_merge_probe"].fn({}))
        self.assertEqual(res, {"merged": True, "skipped": True, "verdict": {"area": "backend"}})

    def test_skip_marks_the_branch_and_everything_downstream(self):
        ts = code_tasks.load_taskfile(taskfile(ROUTER))
        store = FakeStore()
        with capture_events() as ev:
            g = code_tasks.build_code_graph(store, ts, taskfile="tf.json")
            res = asyncio.run(g.nodes["skip_fix-be"].fn({}))
        self.assertEqual(res["downstream"], ["be-docs"])
        rows = {u["id"]: u for u in store.upserts}
        self.assertEqual(rows["fix-be"]["status"], "skipped")
        self.assertIn('when probe.area == "backend" did not hold', rows["fix-be"]["error"])
        self.assertEqual(rows["be-docs"]["status"], "skipped")
        self.assertIn("depends on skipped fix-be", rows["be-docs"]["error"])
        kinds = [(f.get("task"), f.get("because")) for f in ev.of("task.skipped")]
        self.assertEqual(kinds, [("fix-be", None), ("be-docs", "fix-be")])


class Runtime(unittest.TestCase):
    """The engine takes exactly one branch: with a frontend verdict, alloc_fix-fe
    fires and skip_fix-be fires; nothing downstream of fix-be runs."""

    def test_only_the_matching_branch_runs(self):
        from graph import Graph
        ts = code_tasks.load_taskfile(taskfile(ROUTER))
        fired = []
        g = Graph("router-test")

        async def merged_probe(ctx):
            return {"merged": True, "verdict": {"area": "frontend"}}

        async def rec(name):
            async def fn(ctx):
                fired.append(name)
                return {"merged": True}
            return fn
        g.node("pr_merge_probe", merged_probe)
        for n in ("alloc_fix-fe", "alloc_fix-be", "alloc_be-docs", "pr_merge_fix-be"):
            g.node(n, asyncio.run(rec(n)))
        # wire exactly as build_code_graph does, using the same predicate
        for tid in ("fix-fe", "fix-be"):
            w = ts["tasks"][tid]["when"]
            g.edge("pr_merge_probe", f"alloc_{tid}",
                   when=lambda r, c, w=w: code_tasks.when_holds(w, r.get("verdict")))

            async def skip(ctx, tid=tid):
                fired.append(f"skip_{tid}"); return {"skipped": True}
            g.node(f"skip_{tid}", skip)
            g.edge("pr_merge_probe", f"skip_{tid}",
                   when=lambda r, c, w=w: not code_tasks.when_holds(w, r.get("verdict")))
        g.edge("alloc_fix-be", "pr_merge_fix-be")
        g.edge("pr_merge_fix-be", "alloc_be-docs")
        g.start("pr_merge_probe")
        asyncio.run(g.run({}))
        self.assertEqual(sorted(fired), ["alloc_fix-fe", "skip_fix-be"])


class Probe(unittest.TestCase):
    def test_last_json_object_in_stdout_is_the_verdict(self):
        v, err = asyncio.run(code_tasks._run_probe(
            "echo noise; echo '{\"first\": 1}'; echo 'tail {\"area\": \"x\", \"n\": {\"k\": 2}}'", "/tmp"))
        self.assertIsNone(err)
        self.assertEqual(v, {"area": "x", "n": {"k": 2}})

    def test_failures_are_reasons_not_verdicts(self):
        for cmd, frag in (("echo nojson", "no JSON object"),
                          ("echo '{\"a\": 1}'; exit 3", "exited 3"),
                          ("echo '[1, 2]'", "no JSON object")):
            with self.subTest(cmd=cmd):
                v, err = asyncio.run(code_tasks._run_probe(cmd, "/tmp"))
                self.assertIsNone(v)
                self.assertIn(frag, err)


class StoreColumn(unittest.TestCase):
    def test_verdict_round_trips_and_old_databases_are_migrated(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "o.db"
            import sqlite3
            # an "old" database: the table without the column
            con = sqlite3.connect(path)
            con.executescript("""CREATE TABLE code_tasks(id TEXT NOT NULL, taskfile TEXT NOT NULL,
                title TEXT, model TEXT NOT NULL, reviewer TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                branch TEXT, worktree TEXT, error TEXT, created_at TEXT NOT NULL, finished_at TEXT,
                PRIMARY KEY (taskfile, id));""")
            con.commit(); con.close()
            st = Store(str(path))
            st.upsert_code_task("tf", "probe", "P", ENTRY, ENTRY_REVIEWER, "merged", finished=True)
            st.set_code_task_verdict("tf", "probe", {"area": "frontend"})
            row = st.code_tasks_for("tf")[0]
            self.assertEqual(json.loads(row["verdict"]), {"area": "frontend"})
            self.assertIn("verdict", st.code_tasks_all()[0])
            st.conn.close()


class Shapes(unittest.TestCase):
    def test_conditional_deps_classify_as_router_and_the_engine_reports_it(self):
        c = graph_shapes.classify({"tasks": ROUTER, "pattern": "router"})
        self.assertEqual(c["shape"], "router")
        self.assertFalse(c["mismatch"])
        self.assertIn("routed by probe", c["reason"])
        names = {h["name"] for h in graph_shapes._engine()["has"]}
        self.assertIn("conditional deps between tasks", names)
        self.assertNotIn("conditional deps between tasks",
                         {m["name"] for m in graph_shapes._engine()["missing"]})

    def test_planner_is_told_how_to_write_a_condition(self):
        p = graph_shapes.planner_prose()
        self.assertIn('"probe_cmd"', p)
        self.assertIn('"when"', p)
        self.assertIn('"probe_cmd"', code_tasks.PLAN_SCHEMA_HINT)


if __name__ == "__main__":
    unittest.main()
