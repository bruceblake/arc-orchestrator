"""Concurrent-run behaviour: several runs acting on shared state at once.

The fleet runs multiple code-run processes (terminal queue + dashboard
launches) plus threaded graph fanout, all sharing one sqlite file and one
graph engine. A race here once meant exceeded driver caps, clobbered task
rows, and cancelled sibling work — the failure modes that cost the most
debugging time. These tests pin that shared state stays consistent under
threads and across connections, without real subprocesses.
"""
import asyncio
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from helpers import capture_events  # noqa: F401  (sys.path + event log redirect)
from helpers import ENTRY, ENTRY_REVIEWER, STRONGEST  # noqa: E402,F401
import config  # noqa: E402

from graph import Graph
from store import Store

TTL = 1800


class TempDB(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = str(Path(self._dir.name) / "t.db")
        self.store = Store(self.db)

    def tearDown(self):
        self.store.conn.close()  # no ResourceWarning GC noise in gate output
        self._dir.cleanup()


class ConcurrencyLeaseCap(TempDB):
    """The driver cap is the last guard between the fleet and the per-account
    API limit: if N threads racing for leases could each observe "one slot
    free" and all take it, harnesses would pile past the account cap and ARC
    starts rejecting sessions. Acquisition is check-then-insert, so this only
    holds if the store serializes every attempt under its lock."""

    def test_cap_is_never_exceeded_no_matter_the_interleaving(self):
        cap, nthreads, rounds = 2, 6, 10
        pid = os.getpid()
        barrier = threading.Barrier(nthreads)
        lock = threading.Lock()
        state = {"holds": 0, "peak": 0, "acquired": 0, "rows_peak": 0}

        def worker(w):
            barrier.wait()
            for r in range(rounds):
                task = f"w{w}-{r}"
                while True:
                    got = self.store.acquire_driver_lease("glm", pid, task, cap, TTL)
                    if got is None:
                        break
                    time.sleep(0.001)  # at cap: wait, like drivers._lease_acquire
                with lock:
                    state["holds"] += 1
                    state["acquired"] += 1
                    state["peak"] = max(state["peak"], state["holds"])
                    state["rows_peak"] = max(state["rows_peak"],
                                             len(self.store.driver_lease_rows()))
                time.sleep(0.002)  # hold the lease briefly so races overlap
                with lock:
                    state["holds"] -= 1
                self.store.release_driver_lease("glm", pid, task)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(nthreads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(state["acquired"], nthreads * rounds)  # nobody starved
        self.assertEqual(state["peak"], cap)  # contention really happened...
        self.assertLessEqual(state["rows_peak"], cap)  # ...yet the cap held
        self.assertEqual(self.store.driver_lease_rows(), [])  # nothing leaked


class ConcurrencyLeaseVisibility(unittest.TestCase):
    """Separate run processes open separate Store objects on the same DB
    file, each with its own connection and its own in-process lock. Lease
    accounting only closes the cross-process hole if a lease written (and
    committed) by one connection is visible to the other's cap check."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = str(Path(self._dir.name) / "t.db")
        self._stores = []

    def tearDown(self):
        for s in self._stores:
            s.conn.close()
        self._dir.cleanup()

    def _store(self):
        s = Store(self.db)
        self._stores.append(s)
        return s

    def test_lease_taken_on_one_connection_counts_against_the_other(self):
        a, b = self._store(), self._store()
        pid = os.getpid()
        self.assertIsNone(a.acquire_driver_lease("glm", pid, "run-a", 1, TTL))
        # b's acquire must see a's committed row and refuse at cap=1.
        self.assertEqual(b.acquire_driver_lease("glm", pid, "run-b", 1, TTL), 1)
        self.assertEqual([r["task"] for r in b.driver_lease_rows()], ["run-a"])
        # Releasing on a frees the slot for b — slots are shared, not per-connection.
        a.release_driver_lease("glm", pid, "run-a")
        self.assertIsNone(b.acquire_driver_lease("glm", pid, "run-b", 1, TTL))

    def test_a_dead_pids_lease_is_reaped_by_another_connection(self):
        """A killed run process never calls release; its lease rows must not
        pin the model at cap for the next process. Reaping happens lazily on
        acquire (dead owner is dropped), so the next Store to ask must see the
        slot free — otherwise every crash deadlocks the fleet for the TTL."""
        a, b = self._store(), self._store()
        dead_pid = 4_000_000  # never a live process id on this host
        self.assertIsNone(a.acquire_driver_lease("glm", dead_pid, "crashed", 1, TTL))
        self.assertIsNone(b.acquire_driver_lease("glm", os.getpid(), "live", 1, TTL))
        self.assertEqual([r["task"] for r in b.driver_lease_rows()], ["live"])

    def test_release_leases_for_pid_drops_only_that_processes_rows(self):
        """Shutdown cleanup releases leases by OWNING pid, not by model: two
        run processes can each hold a lease on the same model, and one exiting
        must not free the other's slot or the cap is silently violated."""
        a, b = self._store(), self._store()
        pid_a, pid_b = os.getpid(), os.getppid()
        self.assertIsNone(a.acquire_driver_lease("glm", pid_a, "run-a", 2, TTL))
        self.assertIsNone(b.acquire_driver_lease("glm", pid_b, "run-b", 2, TTL))
        self.assertEqual(a.release_leases_for_pid(pid_a), 1)
        rows = a.driver_lease_rows()
        self.assertEqual([(r["pid"], r["task"]) for r in rows],
                         [(pid_b, "run-b")])

    def test_scoped_stale_reset_ignores_other_taskfiles_running_rows(self):
        """A new `code run A.json` resets A.json's stale 'running' rows via
        reset_stale_code_tasks(taskfile=...) while another process is mid-run
        on B.json, re-upserting its rows as 'running'. If the reset leaked
        past its taskfile, the live B run's rows would flip to failed and a
        later resume would see a phantom crash (reconcile's dead-process reset
        goes through the same code path)."""
        a, b = self._store(), self._store()
        a.upsert_code_task("A.json", "a1", "a1", "GLM-5.3", ENTRY_REVIEWER, "running")
        b.upsert_code_task("B.json", "b1", "b1", STRONGEST, "glm", "running")
        stop = threading.Event()

        def live_run_b():  # the other process, still heartbeating its row
            while not stop.is_set():
                b.upsert_code_task("B.json", "b1", "b1", STRONGEST, "glm", "running")
                time.sleep(0.002)

        t = threading.Thread(target=live_run_b)
        t.start()
        try:
            for _ in range(10):
                a.upsert_code_task("A.json", "a1", "a1", "GLM-5.3", ENTRY_REVIEWER, "running")
                self.assertEqual(
                    a.reset_stale_code_tasks(taskfile="A.json", reason="interrupted"), 1)
        finally:
            stop.set()
            t.join()
        row_a = a.code_tasks_for("A.json")[0]
        self.assertEqual(row_a["status"], "failed")
        self.assertEqual(row_a["error"], "interrupted")
        row_b = a.code_tasks_for("B.json")[0]  # read back across connections
        self.assertEqual(row_b["status"], "running")
        self.assertIsNone(row_b["error"])


class ConcurrencyUpsertSameTask(TempDB):
    """Escalation re-upserts a task at a stronger tier using the same
    (taskfile, id) key, and retries/threads can upsert the same key
    concurrently. Two things must survive that: the row count stays at one
    (no INSERT slipping past the UNIQUE key into a duplicate), and the final
    row carries the last model/reviewer written — dropping the update
    stranded tasks on resume at a tier they had already outgrown, and a torn
    pair would report a model with the wrong reviewer."""

    # Escalation walks the LIVE tier pairs, model and reviewer together: on
    # the 2026-09-12 two-model roster each model's reviewer is the other
    # family. Written from the roster so it does not encode a past fleet.
    TIERS = [(m, config.cross_family_reviewer(m)) for m in config.ESCALATION_PATH]

    def test_one_row_survives_and_the_last_write_wins(self):
        self.store.upsert_code_task("f.json", "t1", "t1", *self.TIERS[0], "running")
        barrier = threading.Barrier(len(self.TIERS))

        def worker(combo):
            barrier.wait()
            for _ in range(15):
                self.store.upsert_code_task("f.json", "t1", "t1", *combo, "running")

        threads = [threading.Thread(target=worker, args=(c,)) for c in self.TIERS]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        rows = self.store.code_tasks_for("f.json")
        self.assertEqual(len(rows), 1)  # upsert, not duplicate
        # model and reviewer are written in one statement: the surviving row
        # must be one complete pair somebody wrote, never a torn mix.
        self.assertIn((rows[0]["model"], rows[0]["reviewer"]), self.TIERS)
        # Deterministically pin last-write-wins: a final (escalation) upsert
        # must replace the recorded tier, not be dropped on conflict.
        top_pair = (STRONGEST, config.cross_family_reviewer(STRONGEST))
        self.store.upsert_code_task("f.json", "t1", "t1", *top_pair, "running")
        row = self.store.code_tasks_for("f.json")[0]
        self.assertEqual((row["model"], row["reviewer"]), top_pair)


class ConcurrencyGraph(unittest.TestCase):
    """Independent code tasks are wired as separate start nodes so the fleet
    can overlap them; if they serialized, wall-clock per run would multiply
    by task count. And when one start node fails, siblings already minutes
    into an implement/review must finish (drain) — cancelling them used to
    throw away work that would have merged, leaving orphaned worktrees, DB
    rows and driver leases behind."""

    def test_independent_start_nodes_run_concurrently(self):
        starts, ends = {}, {}

        def mk(name):
            async def fn(ctx):
                starts[name] = time.monotonic()
                await asyncio.sleep(0.15)
                ends[name] = time.monotonic()
                return name
            return fn

        g = Graph("t")
        for name in ("a", "b", "c"):
            g.node(name, mk(name))
            g.start(name)
        with capture_events():
            t0 = time.monotonic()
            asyncio.run(g.run({}))
            elapsed = time.monotonic() - t0
        # Every node started before the first one finished: real overlap...
        self.assertLess(max(starts.values()), min(ends.values()))
        # ...and well below the ~0.45s a serial run of three would take.
        self.assertLess(elapsed, 0.4)

    def test_a_failed_node_drains_in_flight_siblings_instead_of_cancelling(self):
        finished = []

        async def boom(ctx):
            await asyncio.sleep(0.05)
            raise RuntimeError("boom")

        async def slow(ctx):
            await asyncio.sleep(0.3)
            finished.append("slow")
            return "slow"

        g = Graph("t", drain_timeout=10)
        g.node("boom", boom)
        g.node("slow", slow)
        g.start("boom")
        g.start("slow")
        with capture_events() as cap:
            with self.assertRaises(RuntimeError):
                asyncio.run(g.run({}))
        # The failure propagates, but only after the sibling finished normally.
        self.assertIn("slow", finished)
        self.assertIsNotNone(cap.first("graph.draining"))


if __name__ == "__main__":
    unittest.main()
