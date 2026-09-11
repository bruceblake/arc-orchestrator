"""Resumable graph state: persisted node results survive a killed run."""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from helpers import capture_events  # noqa: F401  (sys.path)
import graph
from graph import Graph, Persist
from store import Store


def run(g, ctx=None):
    return asyncio.run(g.run(ctx or {}))


class ResumableState(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self._dir.name) / "state.db"))

    def tearDown(self):
        self._dir.cleanup()

    def make_graph(self, persist, b_fn=None, c_fn=None, a_to_b_when=None):
        g = Graph("t", persist=persist)

        async def a(ctx):
            return {"step": "a"}

        async def b(ctx):
            if b_fn is not None:
                return await b_fn(ctx)
            return {"verdict": "pass"}

        async def c(ctx):
            if c_fn is not None:
                return await c_fn(ctx)
            return {"step": "c"}

        g.node("a", a)
        g.node("b", b)
        g.node("c", c)
        g.start("a")
        g.edge("a", "b", when=a_to_b_when)
        g.edge("b", "c")
        return g

    def kill_after_b(self, persist, **kw):
        """a->b->c where c dies, as a killed run would: b's result is the
        work worth resuming from."""
        async def boom(ctx):
            raise RuntimeError("killed")

        with self.assertRaises(RuntimeError):
            run(self.make_graph(persist, c_fn=boom, **kw))

    def rows(self):
        return {r["node"]: r for r in self.store.graph_state_rows("t")}

    def test_result_survives_a_new_execution(self):
        p = Persist(self.store, ["b"])
        self.kill_after_b(p)
        rows = self.rows()
        self.assertEqual(json.loads(rows["b"]["result"]), {"verdict": "pass"})
        self.assertEqual(rows["b"]["runs"], 1)
        with capture_events() as ev:
            ex = graph._Execution(self.make_graph(p), {})
        self.assertEqual(ex.ctx["results"]["b"], {"verdict": "pass"})
        self.assertEqual(ex.ctx["runs"]["b"], 1)
        self.assertEqual(ev.first("graph.resume")["nodes"], ["b"])

    def test_seeded_node_fires_downstream_without_rerunning(self):
        fired = []

        async def b(ctx):
            fired.append(1)
            return {"verdict": "pass"}

        p = Persist(self.store, ["b"])
        self.kill_after_b(p, b_fn=b)
        # Close a->b on resume: b already has a persisted verdict, so the
        # graph routes AROUND it (the way code_tasks stubs merged tasks).
        final = run(self.make_graph(p, b_fn=b, a_to_b_when=lambda r, ctx: False))
        self.assertEqual(len(fired), 1)  # b itself never re-ran
        self.assertEqual(final["results"]["b"], {"verdict": "pass"})
        self.assertEqual(final["results"]["c"], {"step": "c"})

    def test_runs_resume_from_the_persisted_count(self):
        p = Persist(self.store, ["b"])
        self.kill_after_b(p)
        # Resume WITHOUT closing a->b, so b re-runs on top of its seeded
        # count. c fires twice here (seed + re-run) — the documented
        # double-fire; only b's count is asserted.
        final = run(self.make_graph(p))
        self.assertEqual(final["runs"]["b"], 2)

    def test_unserialisable_result_is_not_persisted(self):
        p = Persist(self.store, ["a", "b"])

        async def b(ctx):
            return {"path": Path("/tmp/x")}

        self.kill_after_b(p, b_fn=b)
        self.assertEqual(set(self.rows()), {"a"})

    def test_stale_rows_past_the_ttl_are_ignored(self):
        p = Persist(self.store, ["a", "b"], ttl=60)
        self.kill_after_b(p)
        with self.store.lock:
            self.store.conn.execute(
                "UPDATE graph_state SET updated_at=? WHERE graph='t'",
                (time.time() - 3600,))
            self.store.conn.commit()
        ex = graph._Execution(self.make_graph(p), {})
        self.assertNotIn("b", ex.ctx["results"])
        self.assertEqual(ex.seeded, [])

    def test_caller_provided_ctx_wins_over_seeded_rows(self):
        p = Persist(self.store, ["b"])
        self.kill_after_b(p)
        ex = graph._Execution(
            self.make_graph(p), {"results": {"b": {"verdict": "reject"}}})
        self.assertEqual(ex.ctx["results"]["b"], {"verdict": "reject"})
        self.assertEqual(ex.seeded, [])

    def test_no_persist_handle_leaves_state_untouched(self):
        self.kill_after_b(None)
        self.assertEqual(self.rows(), {})
        self.store.save_graph_state("t", "b", '{"verdict": "pass"}', 1)
        ex = graph._Execution(self.make_graph(None), {})
        self.assertEqual(ex.seeded, [])
        self.assertNotIn("b", ex.ctx.get("results", {}))

    def test_success_clears_persisted_state(self):
        p = Persist(self.store, ["b"])
        self.kill_after_b(p)
        self.assertIn("b", self.rows())
        run(self.make_graph(p))
        self.assertEqual(self.rows(), {})


if __name__ == "__main__":
    unittest.main()