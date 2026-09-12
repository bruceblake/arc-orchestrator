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


class BoundedAdmission(unittest.TestCase):
    """The graph used to start every ready node the instant it was queued.

    run-queue.sh bounds task FILES, nothing bounded tasks within one: the
    real ~/tasks files have up to 5 root tasks, all starting at once. Work
    past the per-model/per-harness ceilings never ran — it queued inside
    drivers._lease_acquire holding a worktree and a DB row, and could time
    out at ARC_DRIVER_LEASE_WAIT having done nothing. max_in_flight makes the
    graph itself admit only what the fleet can actually execute.
    """

    def _fan(self, limit, n=5, held=0.02):
        g = Graph("admit", max_in_flight=limit)
        state = {"cur": 0, "peak": 0}
        ran = []

        async def work(name):
            state["cur"] += 1
            state["peak"] = max(state["peak"], state["cur"])
            await asyncio.sleep(held)
            state["cur"] -= 1
            ran.append(name)
            return {"ok": True}

        for i in range(n):
            name = f"n{i}"

            async def fn(ctx, _name=name):
                return await work(_name)
            g.node(name, fn)
            g.start(name)
        run(g)
        return state, ran

    def test_at_most_the_limit_runs_concurrently(self):
        state, _ = self._fan(limit=2)
        self.assertEqual(state["peak"], 2)

    def test_every_ready_node_still_eventually_runs(self):
        _, ran = self._fan(limit=2)
        self.assertEqual(sorted(ran), ["n0", "n1", "n2", "n3", "n4"])

    def test_no_limit_means_no_admission_control(self):
        state, ran = self._fan(limit=None)
        self.assertEqual(state["peak"], 5)
        self.assertEqual(len(ran), 5)


class GatherAdmissionCannotDeadlock(unittest.TestCase):
    """The one way an admission limit can hang the graph: a gather holding a
    slot while the sources it waits on sit queued behind a full limit —
    nothing runnable, nothing finishing, no drain clock ticking. Admission
    therefore gates only the node fn: a WAITING gather holds no slot, so its
    sources are always admitted as earlier fns finish. If that regresses this
    test does not assert-fail — it hangs until the timeout below.
    """

    def test_gather_completes_with_five_sources_and_a_limit_of_two(self):
        g = Graph("g", max_in_flight=2)
        done = []

        def make_src(i):
            async def src(ctx):
                await asyncio.sleep(0.005)
                return {"v": i}
            return src

        async def sink(ctx):
            done.append(sorted(ctx["results"][f"s{i}"]["v"] for i in range(5)))
            return {"ok": True}

        for i in range(5):
            g.node(f"s{i}", make_src(i))
            g.edge(f"s{i}", "sink")
            g.start(f"s{i}")
        g.node("sink", sink, gather=True)
        final = asyncio.run(asyncio.wait_for(g.run({}), timeout=10))
        self.assertEqual(done, [[0, 1, 2, 3, 4]])
        self.assertEqual(final["results"]["sink"], {"ok": True})


class DrainLandingUnderTheLimit(unittest.TestCase):
    """An on_drain edge must be schedulable even at the admission limit.

    on_drain exists to land work already paid for — in the code workload, a
    pushed branch and an open PR. If the limit could starve a landing edge, a
    sibling's failure would orphan that PR again, this time with the limit as
    the excuse. And admission adds a new moment an item can meet the failure:
    dequeued BEFORE the drain began but admitted AFTER it — that item must
    drop, not start fresh work (the predrain test above, one queue later).
    """

    def test_landing_edges_fire_with_the_limit_saturated(self):
        g = Graph("dl", max_in_flight=2)
        seen = []
        publishing = asyncio.Event()
        sibling_died = asyncio.Event()

        async def boom(ctx):
            await publishing.wait()  # both slots are now held
            sibling_died.set()
            raise RuntimeError("sibling task died")

        async def publish(ctx):
            publishing.set()
            await sibling_died.wait()  # still in flight as the drain begins
            seen.append("publish")
            return {"ok": True}

        for name in ("review", "merge"):
            async def fn(ctx, n=name):
                seen.append(n)
                return {"ok": True}
            g.node(name, fn)
        g.node("boom", boom)
        g.node("publish", publish)
        g.edge("publish", "review", on_drain=True)
        g.edge("review", "merge", on_drain=True)
        g.start("boom")
        g.start("publish")
        with self.assertRaises(RuntimeError):
            run(g)
        self.assertEqual(seen, ["publish", "review", "merge"])

    def test_work_blocked_on_a_slot_drops_once_the_drain_begins(self):
        g = Graph("dq", max_in_flight=1)
        seen = []

        async def slow_then_fail(ctx):
            await asyncio.sleep(0.05)  # hold the only slot past the dequeue
            raise RuntimeError("boom")

        async def waiting(ctx):
            seen.append("waiting")  # dequeued pre-failure, admitted mid-drain
            return {}

        g.node("slow", slow_then_fail)
        g.node("waiting", waiting)
        g.start("slow")
        g.start("waiting")
        with self.assertRaises(RuntimeError):
            run(g)
        self.assertEqual(seen, [])


class DynamicFanOut(unittest.TestCase):
    """Spawn: width decided at runtime from data — LangGraph's Send(), Prefect's map.

    The PR reviewers fanned out INSIDE one node via asyncio.gather, which made
    them invisible to the graph: not in the diagram, not checkpointed, not
    individually retryable. A crashed reviewer surfaced only as a field on its
    parent's result.
    """

    def _run(self, items, fail_on=None):
        from graph import Spawn
        g = Graph("spawn")
        seen = []

        async def plan(ctx):
            return Spawn("worker", items, "join")

        async def worker(ctx):
            seen.append(ctx["spawn"])
            if ctx["spawn"] == fail_on:
                raise RuntimeError(f"worker {ctx['spawn']} died")
            return {"did": ctx["spawn"] * 10}

        joined = []

        async def join(ctx):
            joined.append(ctx["results"]["worker"])
            return {"n": len(ctx["results"]["worker"])}

        g.node("plan", plan); g.node("worker", worker); g.node("join", join)
        g.edge("plan", "worker"); g.edge("worker", "join")  # declared for validate()
        g.start("plan")
        out = run(g)
        return seen, joined, out

    def test_one_child_per_item_all_run(self):
        seen, _, _ = self._run([1, 2, 3])
        self.assertEqual(sorted(seen), [1, 2, 3])

    def test_the_join_fires_once_with_results_in_item_order(self):
        _, joined, _ = self._run([3, 1, 2])
        self.assertEqual(len(joined), 1)
        self.assertEqual(joined[0], [{"did": 30}, {"did": 10}, {"did": 20}])

    def test_the_join_does_not_fire_before_every_child_finishes(self):
        import asyncio as aio
        from graph import Spawn
        g = Graph("spawn-wait")
        release = aio.Event(); joined = []

        async def plan(ctx): return Spawn("w", ["fast", "slow"], "j")
        async def w(ctx):
            if ctx["spawn"] == "slow": await release.wait()
            return {"k": ctx["spawn"]}
        async def j(ctx): joined.append(ctx["results"]["w"]); return {}
        g.node("plan", plan); g.node("w", w); g.node("j", j)
        g.edge("plan", "w"); g.edge("w", "j"); g.start("plan")

        async def go():
            t = aio.create_task(g.run({}))
            await aio.sleep(0.05)
            self.assertEqual(joined, [], "join fired with a child still running")
            release.set(); await t
        aio.run(go())
        self.assertEqual(joined, [[{"k": "fast"}, {"k": "slow"}]])

    def test_zero_items_fires_the_join_immediately_with_an_empty_list(self):
        _, joined, _ = self._run([])
        self.assertEqual(joined, [[]])

    def test_a_failing_child_drains_like_any_other_node(self):
        with self.assertRaises(RuntimeError):
            self._run([1, 2], fail_on=2)

    def test_the_spawning_node_records_how_many_it_spawned(self):
        _, _, out = self._run([1, 2, 3])
        self.assertEqual(out["results"]["plan"], {"spawned": 3})

    def test_a_spawn_to_an_unknown_node_fails_loudly(self):
        from graph import Spawn
        g = Graph("bad")
        async def plan(ctx): return Spawn("nope", [1], "join")
        async def join(ctx): return {}
        g.node("plan", plan); g.node("join", join); g.edge("plan", "join"); g.start("plan")
        with self.assertRaises(GraphError):
            run(g)


class PerNodeRetry(unittest.TestCase):
    """Retries belonged to the driver layer; a transiently failing node drained the graph."""

    def _flaky(self, fail_times, retry, on=(RuntimeError,)):
        from graph import Retry
        g = Graph("retry"); calls = []

        async def node(ctx):
            calls.append(1)
            if len(calls) <= fail_times:
                raise RuntimeError("transient")
            return {"ok": True}
        g.node("n", node, retry=Retry(attempts=retry, backoff=0.001, on=on))
        g.start("n")
        return g, calls

    def test_a_transient_failure_is_retried_and_succeeds(self):
        g, calls = self._flaky(fail_times=2, retry=3)
        out = run(g)
        self.assertEqual(len(calls), 3)
        self.assertEqual(out["results"]["n"], {"ok": True})

    def test_exhausting_the_policy_fails_the_node(self):
        g, calls = self._flaky(fail_times=5, retry=3)
        with self.assertRaises(RuntimeError):
            run(g)
        self.assertEqual(len(calls), 3)

    def test_only_listed_exceptions_are_retried(self):
        # Retrying a programming error is how a bug gets to run three times.
        from graph import Retry
        g = Graph("retry-on"); calls = []

        async def node(ctx):
            calls.append(1); raise KeyError("bug")
        g.node("n", node, retry=Retry(attempts=5, backoff=0.001, on=(RuntimeError,)))
        g.start("n")
        with self.assertRaises(KeyError):
            run(g)
        self.assertEqual(len(calls), 1, "a KeyError must not be retried under on=(RuntimeError,)")

    def test_each_retry_is_announced(self):
        with capture_events() as ev:
            g, _ = self._flaky(fail_times=2, retry=3)
            run(g)
        retries = [f for t, f in ev.seen if t == "node_retry"]
        self.assertEqual([r["attempt"] for r in retries], [1, 2])

    def test_backoff_grows_and_is_capped(self):
        from graph import Retry
        r = Retry(attempts=10, backoff=1.0, max_backoff=5.0)
        self.assertEqual([r.delay(i) for i in (1, 2, 3, 4, 5)], [1.0, 2.0, 4.0, 5.0, 5.0])


class PerNodeTimeout(unittest.TestCase):
    def test_a_node_that_overruns_fails_with_a_readable_error(self):
        g = Graph("timeout")

        async def slow(ctx):
            await asyncio.sleep(5)
            return {}
        g.node("slow", slow, timeout=0.05); g.start("slow")
        with self.assertRaises(GraphError) as cm:
            run(g)
        self.assertIn("timed out", str(cm.exception))

    def test_a_timeout_can_be_retried(self):
        from graph import Retry
        g = Graph("timeout-retry"); calls = []

        async def sometimes_slow(ctx):
            calls.append(1)
            if len(calls) == 1:
                await asyncio.sleep(5)
            return {"ok": True}
        g.node("n", sometimes_slow, timeout=0.05,
               retry=Retry(attempts=2, backoff=0.001, on=(GraphError,)))
        g.start("n")
        self.assertEqual(run(g)["results"]["n"], {"ok": True})
        self.assertEqual(len(calls), 2)


class ErrorHandlerNodes(unittest.TestCase):
    """on_error routes a failure to a node instead of draining the graph."""

    def _graph(self, handler_name="recover"):
        g = Graph("on_error"); seen = []

        async def risky(ctx): raise RuntimeError("it broke")
        async def recover(ctx):
            seen.append(ctx["error"]); return {"recovered": True}
        async def after(ctx):
            seen.append("after"); return {}
        g.node("risky", risky, on_error=handler_name)
        g.node("recover", recover); g.node("after", after)
        g.edge("recover", "after"); g.start("risky")
        return g, seen

    def test_the_handler_runs_instead_of_the_graph_failing(self):
        g, seen = self._graph()
        out = run(g)  # must NOT raise
        self.assertEqual(out["results"]["recover"], {"recovered": True})

    def test_the_handler_is_told_what_failed_and_why(self):
        g, seen = self._graph()
        run(g)
        err = seen[0]
        self.assertEqual(err["node"], "risky")
        self.assertEqual(err["kind"], "RuntimeError")
        self.assertIn("it broke", err["message"])

    def test_downstream_of_the_handler_still_runs(self):
        g, seen = self._graph()
        run(g)
        self.assertIn("after", seen)

    def test_an_unknown_handler_is_rejected_at_validation(self):
        g, _ = self._graph(handler_name="does-not-exist")
        with self.assertRaises(GraphError):
            run(g)

    def test_the_handler_runs_after_retries_are_exhausted_not_before(self):
        from graph import Retry
        g = Graph("retry-then-handle"); calls = []; handled = []

        async def risky(ctx):
            calls.append(1); raise RuntimeError("still broken")
        async def recover(ctx):
            handled.append(1); return {}
        g.node("risky", risky, retry=Retry(attempts=3, backoff=0.001), on_error="recover")
        g.node("recover", recover); g.start("risky")
        run(g)
        self.assertEqual((len(calls), len(handled)), (3, 1))


class Subgraphs(unittest.TestCase):
    """A node that is itself a graph: one retryable, drainable unit."""

    def test_the_inner_graph_runs_and_its_results_are_recorded(self):
        inner = Graph("inner")
        async def a(ctx): return {"a": 1}
        async def b(ctx): return {"b": ctx["results"]["a"]["a"] + 1}
        inner.node("a", a); inner.node("b", b); inner.edge("a", "b"); inner.start("a")

        outer = Graph("outer")
        outer.subgraph("sub", inner)
        async def done(ctx): return {"saw": ctx["results"]["sub"]["results"]["b"]}
        outer.node("done", done); outer.edge("sub", "done"); outer.start("sub")
        out = run(outer)
        self.assertEqual(out["results"]["done"], {"saw": {"b": 2}})

    def test_an_inner_failure_is_the_outer_nodes_failure(self):
        inner = Graph("inner")
        async def boom(ctx): raise ValueError("inner died")
        inner.node("boom", boom); inner.start("boom")
        outer = Graph("outer"); outer.subgraph("sub", inner); outer.start("sub")
        with self.assertRaises(ValueError):
            run(outer)

    def test_the_outers_on_error_catches_an_inner_failure(self):
        inner = Graph("inner")
        async def boom(ctx): raise ValueError("inner died")
        inner.node("boom", boom); inner.start("boom")
        outer = Graph("outer"); handled = []
        async def recover(ctx): handled.append(ctx["error"]["kind"]); return {}
        outer.subgraph("sub", inner, on_error="recover"); outer.node("recover", recover)
        outer.start("sub")
        run(outer)
        self.assertEqual(handled, ["ValueError"])


class ARaisingEdgePredicateCannotHangTheGraph(unittest.TestCase):
    """Everything after the node fn ran outside the try/except.

    A `when=` lambda that hit a missing key escaped the worker coroutine, killed
    it silently, and left in_flight un-settled: the graph hung forever with no
    error and no event. This predates Spawn; Spawn merely made it reachable
    from a test. The failure must be a failure, not a hang.
    """

    def test_a_when_that_raises_fails_the_graph_rather_than_hanging(self):
        import asyncio as aio
        g = Graph("bad-when")
        async def a(ctx): return {}
        async def b(ctx): return {}
        g.node("a", a); g.node("b", b)
        g.edge("a", "b", when=lambda r, c: r["no_such_key"])  # KeyError at routing time
        g.start("a")

        async def go():
            with self.assertRaises(KeyError):
                await aio.wait_for(g.run({}), 5)
        aio.run(go())

    def test_the_routing_failure_is_captured_with_its_node(self):
        import asyncio as aio
        g = Graph("bad-when-2")
        async def a(ctx): return {}
        g.node("a", a); g.node("b", a)
        g.edge("a", "b", when=lambda r, c: 1 / 0)
        g.start("a")
        with capture_events() as ev:
            async def go():
                with self.assertRaises(ZeroDivisionError):
                    await aio.wait_for(g.run({}), 5)
            aio.run(go())
        errs = [f for t, f in ev.seen if t == "node_error"]
        self.assertEqual(len(errs), 1)
        self.assertIn("routing after a", errs[0]["error"])
