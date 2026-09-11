"""The durable work queue: idempotency, leases, and push notification.

Two things this replaces were neither durable nor push. run-queue.sh held its
pending list in the shell's argv, so killing the queue lost every task file that
had not started. drivers._lease_acquire polls every 20 s, so a slot freed one
second after a poll goes unnoticed for another nineteen — on the PR-reviewer
handoffs that are the fleet's scarcest resource.
"""
import asyncio
import os
import tempfile
import threading
import time
import unittest

from helpers import capture_events  # noqa: F401  (sys.path)

import config
import workqueue


class QueueCase(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.mktemp(suffix=".db")
        self.q = workqueue.Queue(self.db)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        try:
            self.q.conn.close()
        except Exception:
            pass
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db + suffix)
            except OSError:
                pass


class EnqueueingIsIdempotent(QueueCase):
    """Enqueuing the same live work twice must not create two items.

    Callers enqueue on retries, on restart, and from more than one place. If
    that produced duplicates, a restarted queue would run everything twice.
    """

    def test_the_same_key_returns_the_same_item(self):
        first, created = self.q.enqueue("t", "alpha", {"n": 1})
        again, created2 = self.q.enqueue("t", "alpha", {"n": 1})
        self.assertEqual(first, again)
        self.assertTrue(created)
        self.assertFalse(created2)

    def test_only_one_item_actually_exists(self):
        self.q.enqueue("t", "alpha")
        self.q.enqueue("t", "alpha")
        self.assertEqual(len(self.q.pending("t")), 1)

    def test_different_keys_are_different_items(self):
        a, _ = self.q.enqueue("t", "alpha")
        b, _ = self.q.enqueue("t", "beta")
        self.assertNotEqual(a, b)

    def test_the_same_key_in_another_topic_is_separate(self):
        a, _ = self.q.enqueue("one", "alpha")
        b, _ = self.q.enqueue("two", "alpha")
        self.assertNotEqual(a, b)

    def test_a_claimed_item_still_blocks_a_duplicate(self):
        self.q.enqueue("t", "alpha")
        self.q.claim("t")
        _, created = self.q.enqueue("t", "alpha")
        self.assertFalse(created, "in-flight work must not be enqueued twice")

    def test_a_finished_key_can_be_enqueued_again(self):
        # "run this task file again tomorrow" is legitimate. The unique index is
        # partial precisely so completion frees the key.
        first, _ = self.q.enqueue("t", "alpha")
        self.q.complete(self.q.claim("t")["id"])
        again, created = self.q.enqueue("t", "alpha")
        self.assertTrue(created)
        self.assertNotEqual(first, again)


class ClaimingIsALease(QueueCase):
    """A claim is a lease, not a handoff — that is what survives a dead worker."""

    def test_claiming_removes_it_from_pending(self):
        self.q.enqueue("t", "a")
        self.assertIsNotNone(self.q.claim("t"))
        self.assertIsNone(self.q.claim("t"))

    def test_an_empty_topic_claims_nothing(self):
        self.assertIsNone(self.q.claim("nothing-here"))

    def test_higher_priority_is_claimed_first(self):
        self.q.enqueue("t", "low", priority=0)
        self.q.enqueue("t", "high", priority=5)
        self.assertEqual(self.q.claim("t")["dedupe_key"], "high")

    def test_equal_priority_is_first_in_first_out(self):
        self.q.enqueue("t", "one")
        self.q.enqueue("t", "two")
        self.assertEqual(self.q.claim("t")["dedupe_key"], "one")

    def test_an_expired_lease_is_reclaimed(self):
        self.q.enqueue("t", "a")
        item = self.q.claim("t", lease_s=0.0)
        self.assertEqual(self.q.reclaim("t", now=time.time() + 1), 1)
        self.assertEqual(self.q.claim("t")["id"], item["id"])

    def test_a_live_lease_is_not_reclaimed(self):
        self.q.enqueue("t", "a")
        self.q.claim("t", lease_s=600)
        self.assertEqual(self.q.reclaim("t"), 0)

    def test_attempts_count_up_across_reclaims(self):
        self.q.enqueue("t", "a")
        self.q.claim("t", lease_s=0.0)
        self.q.reclaim("t", now=time.time() + 1)
        self.assertEqual(self.q.claim("t")["attempts"], 2)

    def test_a_second_completion_reports_that_it_did_nothing(self):
        # The race that matters: a reclaimed item finishes under a new worker,
        # then the original comes back. Silently accepting both would report
        # success for work that was superseded.
        self.q.enqueue("t", "a")
        item = self.q.claim("t")
        self.assertTrue(self.q.complete(item["id"]))
        self.assertFalse(self.q.complete(item["id"]))

    def test_a_failure_can_be_retried_instead_of_buried(self):
        self.q.enqueue("t", "a")
        item = self.q.claim("t")
        self.q.fail(item["id"], "network blip", retry=True)
        self.assertEqual(self.q.claim("t")["id"], item["id"])

    def test_a_terminal_failure_stays_failed(self):
        self.q.enqueue("t", "a")
        self.q.fail(self.q.claim("t")["id"], "broken")
        self.assertIsNone(self.q.claim("t"))
        self.assertEqual(self.q.stats("t")["t"]["failed"], 1)


class ConcurrentClaims(QueueCase):
    """One item must go to exactly one worker, under real contention."""

    def test_ten_threads_racing_for_five_items_each_get_one(self):
        for i in range(5):
            self.q.enqueue("t", f"k{i}")
        got, lock = [], threading.Lock()

        def worker():
            item = self.q.claim("t")
            if item:
                with lock:
                    got.append(item["id"])

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(got), sorted(set(got)), "an item was claimed twice")
        self.assertEqual(len(got), 5)


class PushNotification(QueueCase):
    """Push turns a 20-second wait into an immediate one.

    Deliberately NOT load-bearing: a datagram can be dropped and a subscriber
    can die without unregistering, so every wait takes a timeout and the
    caller's poll loop remains the source of truth.
    """

    def test_a_subscriber_is_woken(self):
        with self.q.subscribe("slots") as sub:
            threading.Timer(0.05, lambda: self.q.notify("slots")).start()
            self.assertTrue(sub.wait(5))

    def test_waiting_with_no_notify_times_out_rather_than_hanging(self):
        with self.q.subscribe("slots") as sub:
            t0 = time.monotonic()
            self.assertFalse(sub.wait(0.2))
            self.assertLess(time.monotonic() - t0, 3)

    def test_a_notify_on_another_topic_does_not_wake_it(self):
        with self.q.subscribe("slots") as sub:
            self.q.notify("something-else")
            self.assertFalse(sub.wait(0.2))

    def test_every_subscriber_to_a_topic_is_woken(self):
        with self.q.subscribe("slots") as a, self.q.subscribe("slots") as b:
            self.assertEqual(self.q.notify("slots"), 2)
            self.assertTrue(a.wait(5))
            self.assertTrue(b.wait(5))

    def test_repeated_notifies_coalesce_into_one_wake(self):
        with self.q.subscribe("slots") as sub:
            for _ in range(5):
                self.q.notify("slots")
            self.assertTrue(sub.wait(5))
            self.assertFalse(sub.wait(0.2), "the socket should have been drained")

    def test_a_dead_subscriber_is_pruned_not_accumulated(self):
        self.q.conn.execute(
            "INSERT INTO queue_subscribers(path, topic, pid, created_at)"
            " VALUES('/tmp/arcq-does-not-exist.sock','slots',999999,0)")
        self.assertEqual(self.q.notify("slots"), 0)
        left = self.q.conn.execute(
            "SELECT COUNT(*) c FROM queue_subscribers").fetchone()["c"]
        self.assertEqual(left, 0)

    def test_unsubscribing_removes_the_registration_and_the_socket(self):
        with self.q.subscribe("slots") as sub:
            path = sub.path
            self.assertTrue(os.path.exists(path))
        self.assertFalse(os.path.exists(path))
        left = self.q.conn.execute(
            "SELECT COUNT(*) c FROM queue_subscribers").fetchone()["c"]
        self.assertEqual(left, 0)

    def test_the_async_wait_wakes_too(self):
        async def go():
            with self.q.subscribe("slots") as sub:
                threading.Timer(0.05, lambda: self.q.notify("slots")).start()
                return await sub.wait_async(5)
        self.assertTrue(asyncio.run(go()))

    def test_the_async_wait_times_out_rather_than_hanging(self):
        async def go():
            with self.q.subscribe("slots") as sub:
                return await sub.wait_async(0.2)
        self.assertFalse(asyncio.run(go()))


class Durability(QueueCase):
    """The queue must survive the process that created it."""

    def test_pending_work_outlives_the_connection(self):
        self.q.enqueue("t", "survivor", {"n": 7})
        self.q.conn.close()
        reopened = workqueue.Queue(self.db)
        item = reopened.claim("t")
        self.assertEqual(item["dedupe_key"], "survivor")
        self.assertEqual(item["payload"], {"n": 7})
        self.q = reopened  # so cleanup closes the live one

    def test_stats_report_each_state(self):
        self.q.enqueue("t", "a")
        self.q.enqueue("t", "b")
        self.q.complete(self.q.claim("t")["id"])
        self.assertEqual(self.q.stats("t")["t"], {"done": 1, "pending": 1})


class TheLeaseWaitIsPushNotified(unittest.TestCase):
    """Releasing a driver slot must wake whoever is queued for it.

    The measurable claim: drivers._lease_acquire slept up to 20 s between
    attempts, so a slot freed just after a poll was not noticed for the rest of
    the tick. With PR reviewers the scarcest resource in the fleet, that delay
    landed on the handoffs that matter most.
    """

    def setUp(self):
        self.db = tempfile.mktemp(suffix=".db")
        self._orig = None
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db + suffix)
            except OSError:
                pass

    def test_release_notifies_the_exact_slot_key(self):
        import config
        import drivers
        orig_db, orig_store = config.DB_PATH, drivers._lease_store
        config.DB_PATH = self.db
        drivers._lease_store = None
        try:
            q = workqueue.Queue(self.db)
            with q.subscribe("slot:Kimi-K3") as sub:
                drivers._lease_release("Kimi-K3", "some-task")
                self.assertTrue(sub.wait(5), "a released slot did not wake its queue")
        finally:
            config.DB_PATH, drivers._lease_store = orig_db, orig_store

    def test_a_release_of_another_model_does_not_wake_this_one(self):
        import config
        import drivers
        orig_db, orig_store = config.DB_PATH, drivers._lease_store
        config.DB_PATH = self.db
        drivers._lease_store = None
        try:
            q = workqueue.Queue(self.db)
            with q.subscribe("slot:Kimi-K3") as sub:
                drivers._lease_release("GLM-5.3", "some-task")
                self.assertFalse(sub.wait(0.3))
        finally:
            config.DB_PATH, drivers._lease_store = orig_db, orig_store

    def test_a_release_still_succeeds_when_notification_is_impossible(self):
        # The fleet must not stop releasing slots because a socket could not be
        # opened. Releasing is correctness; notifying is speed.
        import config
        import drivers
        orig_db, orig_store = config.DB_PATH, drivers._lease_store
        config.DB_PATH = "/nonexistent-dir/nope.db"
        drivers._lease_store = None
        try:
            drivers._lease_release("Kimi-K3", "t")  # must not raise
        finally:
            config.DB_PATH, drivers._lease_store = orig_db, orig_store

    def test_an_unusable_database_yields_no_subscription_rather_than_raising(self):
        # Push is an optimisation over a working poll loop. A box that cannot
        # open a unix socket or the db should still run the fleet, more slowly.
        import config
        import drivers
        orig = config.DB_PATH
        config.DB_PATH = "/nonexistent-dir/nope.db"
        try:
            self.assertIsNone(drivers._slot_subscription("Kimi-K3"))
        finally:
            config.DB_PATH = orig

    def test_the_wait_loop_still_sleeps_when_there_is_no_subscription(self):
        import asyncio as aio
        import config
        import drivers
        orig_db, orig_store = config.DB_PATH, drivers._lease_store
        config.DB_PATH = self.db
        drivers._lease_store = None
        try:
            async def go():
                # cap=0 can never be satisfied, so the loop must reach its
                # deadline and raise rather than spin or hang.
                with self.assertRaises(drivers.DriverError):
                    await drivers._lease_wait_loop(
                        "Kimi-K3", "t", {}, 0, "Kimi-K3",
                        time.monotonic() + 0.2, None)
            aio.run(go())
        finally:
            config.DB_PATH, drivers._lease_store = orig_db, orig_store


class TheSlotQueueHandleIsCached(unittest.TestCase):
    """Constructing a Queue opens a connection AND re-runs the schema script.

    Doing that on every lease release measured 69x the cost of reusing one, on
    a path that runs on every driver attempt.
    """

    def setUp(self):
        import drivers
        self.db = tempfile.mktemp(suffix=".db")
        self._orig = config.DB_PATH
        drivers._slot_queue = None
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import drivers
        config.DB_PATH = self._orig
        drivers._slot_queue = None
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db + suffix)
            except OSError:
                pass

    def test_the_same_handle_is_reused(self):
        import drivers
        config.DB_PATH = self.db
        self.assertIs(drivers._slot_q(), drivers._slot_q())

    def test_repointing_the_database_invalidates_the_cache(self):
        # Tests and `--db` repoint config.DB_PATH. A handle cached against the
        # old path would quietly write to the wrong database.
        import drivers
        config.DB_PATH = self.db
        first = drivers._slot_q()
        other = tempfile.mktemp(suffix=".db")
        config.DB_PATH = other
        try:
            second = drivers._slot_q()
            self.assertIsNot(first, second)
            self.assertEqual(second.db_path, other)
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(other + suffix)
                except OSError:
                    pass

    def test_an_unopenable_database_yields_none_rather_than_raising(self):
        import drivers
        config.DB_PATH = "/nonexistent-dir/nope.db"
        drivers._slot_queue = None
        self.assertIsNone(drivers._slot_q())
