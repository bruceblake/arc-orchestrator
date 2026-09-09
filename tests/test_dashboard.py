"""Dashboard accounting: in-flight attribution and cap arithmetic.

These numbers govern operator decisions — whether the fleet looks wedged,
whether to throttle — so over-counting is not a cosmetic bug.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path

from helpers import capture_events  # noqa: F401  (sys.path)

import config
import dashboard


class SessionAttribution(unittest.TestCase):
    def test_reads_the_task_id_out_of_a_kimi_session_path(self):
        p = Path("/home/u/.kimi-code/sessions/wd_index-graph-polish_ea5e72361606"
                 "/session_4b6b088e/agents/main/wire.jsonl")
        self.assertEqual(dashboard._session_task(p), "index-graph-polish")

    def test_handles_a_task_id_containing_underscores(self):
        p = Path("/x/sessions/wd_my_task_name_deadbeef/session_1/agents/main/wire.jsonl")
        self.assertEqual(dashboard._session_task(p), "my_task_name")

    def test_returns_none_for_an_unrecognised_path(self):
        self.assertIsNone(dashboard._session_task(Path("/tmp/nope/wire.jsonl")))


class InflightAttribution(unittest.TestCase):
    """A fleet driver must be counted once, not once per accounting layer.

    The fleet spawns the kimi CLI, which writes its own kimi-code wire log. The
    driver was therefore counted both from its driver.start event AND from that
    wire log, so one driver read as several agents against the ARC account cap
    — and a retried task inflated it further, because each killed attempt
    leaves an unanswered llm.request that looks live for 10 minutes.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self._orig_log = config.EVENTS_LOG
        config.EVENTS_LOG = str(self.root / "events.jsonl")
        dashboard._lines_cache["key"] = None
        dashboard._kimi_cache.clear()
        dashboard._fleet_names_cache.update(key=0.0, names=frozenset())

    def tearDown(self):
        config.EVENTS_LOG = self._orig_log
        dashboard._lines_cache["key"] = None
        dashboard._fleet_names_cache.update(key=0.0, names=frozenset())
        self._dir.cleanup()

    def write_events(self, *events):
        Path(config.EVENTS_LOG).write_text(
            "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")

    def test_an_unmatched_driver_start_counts_as_one_agent(self):
        now = time.time()
        self.write_events({"ts": now - 30, "type": "driver.start", "harness": "kimi",
                           "model": "Kimi-K3", "role": "implementer",
                           "task": "t1", "attempt": 1})
        rows, _ = dashboard._collect_inflight(now, None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "driver:kimi")

    def test_a_settled_driver_counts_as_none(self):
        now = time.time()
        base = {"harness": "kimi", "model": "Kimi-K3", "role": "implementer",
                "task": "t1", "attempt": 1}
        self.write_events({"ts": now - 30, "type": "driver.start", **base},
                          {"ts": now - 5, "type": "driver.done", **base})
        rows, _ = dashboard._collect_inflight(now, None)
        self.assertEqual(rows, [])

    def test_a_cancelled_driver_settles_too(self):
        """Without driver.cancelled this lingered as a phantom for ~19 min."""
        now = time.time()
        base = {"harness": "kimi", "model": "Kimi-K3", "role": "implementer",
                "task": "t1", "attempt": 1}
        self.write_events({"ts": now - 30, "type": "driver.start", **base},
                          {"ts": now - 5, "type": "driver.cancelled", **base})
        rows, _ = dashboard._collect_inflight(now, None)
        self.assertEqual(rows, [])

    def test_a_stale_driver_start_is_pruned(self):
        now = time.time()
        self.write_events({"ts": now - dashboard.DRIVER_STALE_S - 60,
                           "type": "driver.start", "harness": "kimi",
                           "model": "Kimi-K3", "role": "implementer",
                           "task": "t1", "attempt": 1})
        rows, _ = dashboard._collect_inflight(now, None)
        self.assertEqual(rows, [], "a killed run's start event must not count forever")


class KimiSessionExclusion(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        dashboard._kimi_cache.clear()

    def tearDown(self):
        self._dir.cleanup()

    def _session(self, task, answered):
        """Build a wire.jsonl for one kimi-code session."""
        d = (Path(self._dir.name) / f"wd_{task}_abc123" / "session_1"
             / "agents" / "main")
        d.mkdir(parents=True)
        now_ms = time.time() * 1000
        lines = [{"type": "llm.request", "model": "Kimi-K3",
                  "modelAlias": "arc/kimi-k3", "agentId": "main", "time": now_ms}]
        if answered:
            lines.append({"type": "usage.record", "model": "arc/kimi-k3",
                          "usageScope": "turn", "time": now_ms + 1000,
                          "usage": {"inputOther": 10, "output": 5}})
        (d / "wire.jsonl").write_text(
            "".join(json.dumps(l) + "\n" for l in lines), encoding="utf-8")
        return d / "wire.jsonl"

    def test_an_unanswered_session_is_in_flight(self):
        parsed = dashboard._parse_kimi_wire(self._session("interactive-work", False))
        self.assertIsNotNone(parsed["last_req"])
        self.assertGreater(parsed["last_req"][0], parsed["last_done"])

    def test_an_answered_session_is_not(self):
        parsed = dashboard._parse_kimi_wire(self._session("interactive-work", True))
        self.assertLessEqual(parsed["last_req"][0], parsed["last_done"])

    def test_token_totals_are_kept_for_every_session(self):
        """kimi's stream-json carries no usage, so wire logs are the only
        source — excluding fleet sessions from in-flight must not lose them."""
        parsed = dashboard._parse_kimi_wire(self._session("fleet-task", True))
        self.assertEqual(parsed["file_totals"]["prompt"], 10)
        self.assertEqual(parsed["file_totals"]["completion"], 5)


if __name__ == "__main__":
    unittest.main()
