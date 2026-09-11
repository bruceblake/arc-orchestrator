"""Execution semantics of the graph engine."""
import asyncio
import unittest

from helpers import capture_events  # noqa: F401  (also fixes sys.path)

from graph import Graph, GraphError


def run(g, ctx=None):
    return asyncio.run(g.run(ctx or {}))


class LinearExecution(unittest.TestCase):
    def test_runs_nodes_in_dependency_order(self):
        order = []

        def mk(name):
            async def fn(ctx):
                order.append(name)
                return {"name": name}
            return fn

        g = Graph("t")
        for name in ("a", "b", "c"):
            g.node(name, mk(name))
        g.edge("a", "b")
        g.edge("b", "c")
        g.start("a")
        final = run(g)
        self.assertEqual(order, ["a", "b", "c"])
        self.assertEqual(final["results"]["c"], {"name": "c"})

    def test_conditional_edge_not_taken(self):
        async def a(ctx):
            return {"ok": False}

        async def b(ctx):
            raise AssertionError("must not fire")

        g = Graph("t")
        g.node("a", a)
        g.node("b", b)
        g.edge("a", "b", when=lambda r, c: r["ok"])
        g.start("a")
        run(g)  # completes without firing b

    def test_loop_is_bounded_by_max_steps(self):
        async def a(ctx):
            return {}

        g = Graph("t", max_steps=5)
        g.node("a", a)
        g.edge("a", "a")
        g.start("a")
        with self.assertRaises(GraphError) as cm:
            run(g)
        self.assertIn("max_steps", str(cm.exception))


class ValidateTopology(unittest.TestCase):
    def test_rejects_unreachable_node(self):
        async def fn(ctx):
            return {}

        g = Graph("t")
        g.node("a", fn)
        g.node("orphan", fn)
        g.start("a")
        with self.assertRaises(GraphError) as cm:
            run(g)
        self.assertIn("unreachable", str(cm.exception))

    def test_rejects_edge_to_unknown_node(self):
        async def fn(ctx):
            return {}

        g = Graph("t")
        g.node("a", fn)
        g.edge("a", "ghost")
        g.start("a")
        with self.assertRaises(GraphError):
            run(g)


class DrainOnError(unittest.TestCase):
    """A node crash must not cancel siblings that are mid-flight.

    In the code workload a sibling is a whole task that may be minutes into an
    implement/review it will successfully merge; cancelling it threw the work
    away and left its worktree, DB row and driver lease behind.
    """

    def test_inflight_sibling_finishes_before_the_error_propagates(self):
        finished = []

        async def slow(ctx):
            await asyncio.sleep(0.15)
            finished.append("slow")
            return {}

        async def boom(ctx):
            await asyncio.sleep(0.01)
            raise RuntimeError("kaboom")

        g = Graph("t")
        g.node("slow", slow)
        g.node("boom", boom)
        g.start("slow")
        g.start("boom")
        with self.assertRaises(RuntimeError):
            run(g)
        self.assertEqual(finished, ["slow"], "sibling was cancelled instead of drained")

    def test_no_new_work_is_scheduled_after_a_failure(self):
        started = []

        async def slow(ctx):
            await asyncio.sleep(0.15)
            started.append("slow")
            return {}

        async def after_slow(ctx):
            started.append("after_slow")
            return {}

        async def boom(ctx):
            raise RuntimeError("kaboom")

        g = Graph("t")
        g.node("slow", slow)
        g.node("after_slow", after_slow)
        g.node("boom", boom)
        g.edge("slow", "after_slow")
        g.start("slow")
        g.start("boom")
        with self.assertRaises(RuntimeError):
            run(g)
        self.assertIn("slow", started)
        self.assertNotIn("after_slow", started,
                         "a draining graph must not schedule downstream work")

    def test_first_error_wins(self):
        async def boom1(ctx):
            raise RuntimeError("first")

        async def boom2(ctx):
            await asyncio.sleep(0.05)
            raise RuntimeError("second")

        g = Graph("t")
        g.node("boom1", boom1)
        g.node("boom2", boom2)
        g.start("boom1")
        g.start("boom2")
        with self.assertRaises(RuntimeError) as cm:
            run(g)
        self.assertEqual(str(cm.exception), "first")


class GatherNodes(unittest.TestCase):
    def test_gather_waits_for_every_source(self):
        async def src(ctx):
            return {"v": 1}

        seen = {}

        async def sink(ctx):
            seen.update(ctx["results"])
            return {}

        g = Graph("t")
        g.node("a", src)
        g.node("b", src)
        g.node("sink", sink, gather=True)
        g.edge("a", "sink")
        g.edge("b", "sink")
        g.start("a")
        g.start("b")
        run(g)
        self.assertIn("a", seen)
        self.assertIn("b", seen)

    def test_unreachable_gather_source_fails_instead_of_hanging(self):
        """Previously this hung the process forever."""
        async def a(ctx):
            return {"ok": False}

        async def b(ctx):
            return {}

        async def sink(ctx):
            return {}

        g = Graph("t", drain_timeout=2)
        g.node("a", a)
        g.node("b", b)
        g.node("sink", sink, gather=True)
        g.edge("a", "sink")
        # b only fires on a condition that never holds, so sink waits forever
        g.edge("a", "b", when=lambda r, c: r["ok"])
        g.edge("b", "sink")
        g.start("a")
        with self.assertRaises(GraphError) as cm:
            asyncio.run(asyncio.wait_for(g.run({}), timeout=10))
        self.assertIn("still waiting", str(cm.exception))


if __name__ == "__main__":
    unittest.main()




class DrainingStillLandsFinishedWork(unittest.TestCase):
    """A sibling's failure must not orphan work that is already committed.

    The real symptom: task A fails, the graph drains, and task B — whose PR is
    already open on GitHub — never gets reviewed or merged, because draining
    stopped firing every edge, including the two that only had to land it.
    """

    def _run(self, on_drain):
        g = Graph("drain")
        seen = []
        publishing = asyncio.Event()
        sibling_died = asyncio.Event()

        async def boom(ctx):
            await publishing.wait()  # fail only once publish is in flight
            sibling_died.set()
            raise RuntimeError("sibling task died")

        async def publish(ctx):
            # In flight when the sibling dies — the branch is pushed and the PR
            # is open by the time the graph starts draining.
            publishing.set()
            await sibling_died.wait()
            seen.append("publish")
            return {"ok": True}

        g.node("boom", boom)
        g.node("publish", publish)
        for name in ("review", "merge", "rework"):
            async def fn(ctx, n=name):
                seen.append(n)
                return {"ok": True}
            g.node(name, fn)
        g.edge("publish", "review", on_drain=on_drain)
        g.edge("review", "merge", on_drain=on_drain)
        g.edge("review", "rework")  # fresh model work: never during a drain
        g.start("boom")
        g.start("publish")
        with self.assertRaises(RuntimeError):
            run(g)
        return seen

    def test_landing_edges_fire_while_draining(self):
        self.assertEqual(self._run(on_drain=True), ["publish", "review", "merge"])

    def test_draining_does_not_start_fresh_rework(self):
        self.assertNotIn("rework", self._run(on_drain=True))

    def test_without_the_flag_the_open_pr_is_orphaned(self):
        self.assertEqual(self._run(on_drain=False), ["publish"])

    def test_work_queued_BEFORE_the_drain_is_still_dropped(self):
        """The other half of the guard, and a different moment in time.

        Refusing to fire new edges after a failure cannot help an item that was
        already on a queue when the failure happened. That one is stopped when
        it is DEQUEUED. Both checks are needed and neither is redundant —
        verified by mutation: breaking either one alone leaves behaviour
        correct, breaking both lets fresh work start during a drain.
        """
        g = Graph("predrain")
        seen = []
        queued = asyncio.Event()

        async def slow(ctx):
            # Holds the worker so `fresh` sits on its queue, already put there,
            # while the failure below happens.
            await queued.wait()
            raise RuntimeError("sibling died")

        async def fan(ctx):
            return {"ok": True}

        async def fresh(ctx):
            seen.append("fresh")
            return {"ok": True}

        g.node("slow", slow)
        g.node("fan", fan)
        g.node("fresh", fresh)
        g.edge("fan", "fresh")          # queued before the drain, no on_drain
        g.start("slow")
        g.start("fan")

        async def go():
            queued.set()
            with self.assertRaises(RuntimeError):
                await g.run({})
        asyncio.run(go())
        # `fresh` may or may not have been dequeued before the error landed;
        # what must never happen is it running AFTER the drain began.
        self.assertLessEqual(len(seen), 1)


class ANodeThatRetriesItself(unittest.TestCase):
    """A self-edge must terminate on a counter carried through the context.

    pr_review retries itself when a round reaches no verdict (every reviewer
    crashed), bounded by a count it reads back out of its OWN previous result.
    If the graph did not carry that result forward to the retry, the counter
    would reset every time and the node would loop until max_steps.
    """

    def _run(self, limit, always_inconclusive=True):
        g = Graph("retry", max_steps=50)
        runs = []

        async def review(ctx):
            prior = ctx.get("results", {}).get("review", {})
            n = prior.get("n", 0) + 1
            runs.append(n)
            return {"inconclusive": always_inconclusive, "n": n}

        async def merge(ctx):
            runs.append("merged")
            return {}

        g.node("review", review)
        g.node("merge", merge)
        g.edge("review", "review",
               when=lambda r, c: r["inconclusive"] and r["n"] < limit)
        g.edge("review", "merge", when=lambda r, c: not r["inconclusive"])
        g.start("review")
        run(g)
        return runs

    def test_it_stops_at_the_limit(self):
        self.assertEqual(self._run(3), [1, 2, 3])

    def test_the_counter_survives_each_retry(self):
        # The bug this guards: a context that did not carry the previous result
        # forward would produce [1, 1, 1, ...] and never reach the limit.
        self.assertEqual(self._run(5), [1, 2, 3, 4, 5])

    def test_a_conclusive_first_round_never_retries(self):
        self.assertEqual(self._run(3, always_inconclusive=False), [1, "merged"])

    def test_it_cannot_run_away_to_max_steps(self):
        self.assertLess(len(self._run(3)), 50)


class RandomisedGraphStress(unittest.TestCase):
    """Drains, self-edges and gathers racing each other.

    Three features added on the same day interact: on_drain edges keep firing
    after a failure, a self-edge retries a node against a counter it reads from
    its own previous result, and a gather waits on sources that a drain may
    stop feeding. Each is tested alone; the failure mode that matters is a
    graph that never settles when they combine, and that only shows up under
    varied timing.
    """

    def _one(self, seed):
        import collections
        import random
        random.seed(seed)
        g = Graph(f"s{seed}", max_steps=400)
        ran = collections.Counter()

        async def flaky(ctx):
            await asyncio.sleep(random.random() * 0.002)
            ran["flaky"] += 1
            if random.random() < 0.4:
                raise RuntimeError("boom")
            return {"ok": True}

        async def land(ctx):
            ran["land"] += 1
            return {"ok": True}

        async def retry(ctx):
            n = (ctx.get("results", {}).get("retry", {}) or {}).get("n", 0) + 1
            ran["retry"] += 1
            return {"again": n < 3, "n": n}

        async def gathered(ctx):
            ran["gathered"] += 1
            return {"ok": True}

        g.node("flaky", flaky)
        g.node("land", land)
        g.node("retry", retry)
        g.node("gathered", gathered, gather=True)
        g.edge("flaky", "land", on_drain=True)
        g.edge("land", "gathered", on_drain=True)
        g.edge("retry", "retry", when=lambda r, c: r["again"])
        g.edge("retry", "gathered", when=lambda r, c: not r["again"], on_drain=True)
        g.start("flaky")
        g.start("retry")

        async def go():
            try:
                await asyncio.wait_for(g.run({}), timeout=10)
            except asyncio.TimeoutError:
                raise AssertionError(f"graph never settled (seed {seed})")
            except Exception:
                pass       # a failed node is an expected outcome here
        asyncio.run(go())
        return ran

    def test_it_always_settles(self):
        for seed in range(40):
            with self.subTest(seed=seed):
                self._one(seed)

    def test_the_self_edge_never_runs_away(self):
        for seed in range(40):
            ran = self._one(seed)
            self.assertLessEqual(ran["retry"], 3,
                                 f"self-edge exceeded its bound (seed {seed})")
