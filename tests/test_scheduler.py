import asyncio
import unittest

from helpers import capture_events  # noqa: F401  (sys.path + event redirect)

import config
from pool import ArcPool
from scheduler import Supervisor
from store import Store


def _make_supervisor(**kw):
    pool = ArcPool(dry_run=True)
    store = Store(":memory:")
    return Supervisor(pool, store, **kw), pool, store


class SchedulerSupervisorRuns(unittest.TestCase):
    """The supervisor must stop on its own when it has done its work, or it
    keeps burning model budget; max_rounds is the only bound that is tested."""

    def setUp(self):
        self._cooldown = config.ROUND_COOLDOWN
        self._questions = config.QUESTIONS_PER_ROUND
        config.ROUND_COOLDOWN = 0
        config.QUESTIONS_PER_ROUND = 1

    def tearDown(self):
        config.ROUND_COOLDOWN = self._cooldown
        config.QUESTIONS_PER_ROUND = self._questions

    def _store_cleanup(self, store):
        self.addCleanup(store.conn.close)

    def test_run_with_max_rounds_one_runs_exactly_one_round_and_stops(self):
        """A max_rounds=1 supervisor must launch one round and return, never
        looping forever and never touching the network (dry_run)."""
        sup, _pool, store = _make_supervisor(max_rounds=1)
        self._store_cleanup(store)
        with capture_events():
            asyncio.run(sup.run())
        self.assertEqual(sup.completed, 1)
        self.assertEqual(sup.failed, 0)
        rounds = store.stats()["rounds"]
        self.assertEqual(rounds.get("ok"), 1)
        self.assertEqual(sum(rounds.values()), 1)

    def test_pipeline_defaults_to_config_pipeline_rounds_when_not_given(self):
        """The pipeline controls how many rounds run concurrently; an omitted
        value must fall back to the configured default so the fleet is never
        over- or under-subscribed by accident."""
        sup, _pool, store = _make_supervisor()
        self._store_cleanup(store)
        self.assertEqual(sup.pipeline, config.PIPELINE_ROUNDS)

    def test_pipeline_honours_an_explicit_value(self):
        """An explicitly supplied pipeline must win over the config default,
        so an operator can tune concurrency per run."""
        sup, _pool, store = _make_supervisor(pipeline=5)
        self._store_cleanup(store)
        self.assertEqual(sup.pipeline, 5)

    def test_consecutive_failures_raises_backoff_delay_and_resets_after_success(self):
        """After a run of failures the supervisor must back off (exponential
        delay, capped at 300s) to give ARC breathing room; a success must reset
        the counter so the next failure starts the ladder over, not on top of it."""
        for failures, expected in ((1, 10), (2, 20)):
            sup, _pool, store = _make_supervisor(max_rounds=1)
            self._store_cleanup(store)
            recorded = []

            async def fake_sleep(delay, stop):
                recorded.append(delay)

            async def noop_stats(stop):
                return

            sup._sleep_or_stop = fake_sleep
            sup._stats_loop = noop_stats
            sup.consecutive_failures = failures
            with capture_events():
                asyncio.run(sup.run())
            self.assertEqual(recorded[0], expected)
            self.assertEqual(sup.consecutive_failures, 0)


class SchedulerFailStaleRounds(unittest.TestCase):
    """A round left 'running' from a crashed/interrupted run must be marked
    failed on restart, or it would look live forever and never be counted."""

    def test_fail_stale_rounds_marks_leftover_running_round_as_failed(self):
        pool = ArcPool(dry_run=True)
        store = Store(":memory:")
        self.addCleanup(store.conn.close)
        store.start_round("leftover")
        done_id = store.start_round("done")
        store.finish_round(done_id, "ok")
        n = store.fail_stale_rounds()
        self.assertEqual(n, 1)
        rounds = store.stats()["rounds"]
        self.assertEqual(rounds.get("failed"), 1)
        self.assertEqual(rounds.get("ok"), 1)
        self.assertNotIn("running", rounds)


if __name__ == "__main__":
    unittest.main()
