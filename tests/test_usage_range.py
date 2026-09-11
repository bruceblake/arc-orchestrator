"""/api/usage?range= must actually filter the aggregation window.

Before this, `_usage` validated the range_key and then walked the WHOLE event
log regardless of it, so 1h/24h/7d/all returned byte-identical totals and the
usage page's range picker was decorative. These tests pin the windowing down so
the picker reflects reality: a narrow window shows only recent traffic, an
in-flight agent is never cut off, and an empty window is zeros, not a crash.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path

from helpers import capture_events  # noqa: F401  (sys.path)

import config
import dashboard


class UsageRangeBase(unittest.TestCase):
    """Redirects the event log to a temp file and stubs kimi-code sessions.

    _kimi_code_usage reads the real ~/.kimi-code/sessions; without stubbing it
    the tests count whatever the operator happens to be running and fail at
    random. Same contract as InflightAttribution in test_dashboard.py.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self._orig_log = config.EVENTS_LOG
        config.EVENTS_LOG = str(self.root / "events.jsonl")
        dashboard._lines_cache["key"] = None
        dashboard._kimi_cache.clear()
        dashboard._fleet_names_cache.update(key=0.0, names=frozenset())
        self._orig_kimi = dashboard._kimi_code_usage
        dashboard._kimi_code_usage = lambda now, fleet_names=frozenset(): {
            "models": [], "inflight": [], "points": []}

    def tearDown(self):
        dashboard._kimi_code_usage = self._orig_kimi
        config.EVENTS_LOG = self._orig_log
        dashboard._lines_cache["key"] = None
        dashboard._fleet_names_cache.update(key=0.0, names=frozenset())
        self._dir.cleanup()

    def write_events(self, *events):
        Path(config.EVENTS_LOG).write_text(
            "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")

    def done(self, ts, model="gpt-oss-120b", tokens=100):
        return {"ts": ts, "type": "driver.done", "harness": "opencode",
                "model": model, "role": "implementer", "task": "t1", "attempt": 1,
                "tokens": tokens, "prompt_tokens": tokens // 2,
                "completion_tokens": tokens // 2, "seconds": 1.0}


class UsageRangeWindow(UsageRangeBase):
    def test_one_hour_excludes_a_two_hour_old_event(self):
        """A 1h window must not count traffic from an hour ago — otherwise the
        picker's default hides nothing and overstates the current load."""
        now = time.time()
        # Oldest first: the log is append-ordered and the window seek depends on
        # that, so a test writing the recent event before the old one would
        # confuse the backward scan into dropping both.
        self.write_events(self.done(now - 7200), self.done(now - 300))
        res = dashboard._usage(None, "1h")
        self.assertEqual(res["totals"]["requests"], 1)
        self.assertEqual(res["totals"]["tokens"], 100)

    def test_twenty_four_hour_window_includes_a_two_hour_old_event(self):
        """Widening to 24h must pull in what 1h dropped."""
        now = time.time()
        self.write_events(self.done(now - 7200))
        res = dashboard._usage(None, "24h")
        self.assertEqual(res["totals"]["requests"], 1)
        self.assertEqual(res["totals"]["tokens"], 100)

    def test_all_window_includes_even_a_ten_day_old_event(self):
        """`all` is the historical view: nothing is dropped."""
        now = time.time()
        self.write_events(self.done(now - 10 * 86400))
        res = dashboard._usage(None, "all")
        self.assertEqual(res["totals"]["requests"], 1)

    def test_totals_shrink_monotonically_as_the_window_narrows(self):
        """Each range must be a strict subset of the next wider one, not a
        constant: the whole point of the picker is that a narrower window
        shows genuinely less."
        """
        now = time.time()
        self.write_events(
            self.done(now - 10 * 86400),
            self.done(now - 3 * 86400),
            self.done(now - 2 * 3600),
            self.done(now - 300),
        )
        requests = {r: dashboard._usage(None, r)["totals"]["requests"]
                    for r in ("1h", "24h", "7d", "all")}
        self.assertEqual(requests["1h"], 1)
        self.assertEqual(requests["24h"], 2)
        self.assertEqual(requests["7d"], 3)
        self.assertEqual(requests["all"], 4)
        self.assertLessEqual(requests["1h"], requests["24h"])
        self.assertLessEqual(requests["24h"], requests["7d"])
        self.assertLessEqual(requests["7d"], requests["all"])

    def test_models_only_include_models_active_within_the_window(self):
        """A model that has no traffic inside a window must not appear at all,
        not appear with stale totals still attached."""
        now = time.time()
        self.write_events(self.done(now - 7200, model="gpt-oss-120b"))
        self.assertEqual(dashboard._usage(None, "1h")["models"], [])
        self.assertEqual(len(dashboard._usage(None, "24h")["models"]), 1)

    def test_an_empty_window_returns_zeros_instead_of_raising(self):
        """A window with no matching events (quiet hour) is zeros, never a 500,
        and never leaks the out-of-window traffic in."""
        now = time.time()
        self.write_events(self.done(now - 2 * 3600))
        res = dashboard._usage(None, "1h")
        self.assertEqual(res["totals"]["requests"], 0)
        self.assertEqual(res["totals"]["tokens"], 0)


class UsageRangeInflight(UsageRangeBase):
    """The range window bounds aggregate HISTORY, never live agents."""

    def test_an_inflight_entry_survives_every_range(self):
        """A live (unsettled) agent is a fact about right now, not about the
        window: cutting it from the 1h view would hide a real account-cap
        breach while still showing 24h worth of dead history."""
        now = time.time()
        base = {"harness": "kimi", "model": "Kimi-K3", "role": "implementer",
                "task": "t1", "attempt": 1}
        self.write_events({"ts": now - 30, "type": "driver.start", **base})
        for r in ("1h", "24h", "7d", "all"):
            with self.subTest(range=r):
                res = dashboard._usage(None, r)
                self.assertEqual(len(res["inflight"]), 1)
                self.assertEqual(res["inflight"][0]["source"], "driver:kimi")

    def test_inflight_is_not_cut_even_when_the_start_is_outside_the_window(self):
        """A run that started more than an hour ago but is still live must stay
        in the 1h inflight list — the cutoff trims the totals, never the
        live-agent count. A driver.start can't prove this: the stale-prune
        threshold (DRIVER_TIMEOUT + 240s, ~49 min) is SHORTER than an hour, so
        a driver started before the 1h cutoff is already pruned as stale. A
        pool request is the right vehicle — its stale threshold is
        POOL_STALE_S (7200s), comfortably longer than an hour.
        """
        now = time.time()
        age = (dashboard.POOL_STALE_S + 3600) // 2
        self.assertLess(3600, age,
                        "test needs a start outside the 1h cutoff")
        self.assertLess(age, dashboard.POOL_STALE_S,
                        "test needs a start that is not yet pruned as stale")
        self.write_events({"ts": now - age, "type": "request_start",
                           "req_id": "req-1", "family": "gpt-oss",
                           "model": "gpt-oss-120b"})
        res = dashboard._usage(None, "1h")
        self.assertEqual(len(res["inflight"]), 1)
        self.assertEqual(res["inflight"][0]["source"], "arc-pool")
