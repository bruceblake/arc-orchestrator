"""The watchdog resumes dead runs, respects a Stop, and parks what keeps dying."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (redirects the event log and DB first)
import config
import fleetwatch
from store import Store


class WatchdogTick(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self._old_dir = fleetwatch.STATE_DIR
        fleetwatch.STATE_DIR = root / "watchdog"
        self.addCleanup(setattr, fleetwatch, "STATE_DIR", self._old_dir)
        self._old_log = config.EVENTS_LOG
        config.EVENTS_LOG = str(root / "events.jsonl")
        self.addCleanup(setattr, config, "EVENTS_LOG", self._old_log)
        self.store = Store(str(root / "t.db"))
        self.tf = str(root / "proj.json")
        Path(self.tf).write_text(json.dumps({"project": {
            "name": "proj", "repo": str(root),
            "tasks": [{"id": "a"}, {"id": "b"}]}}))
        self.launches = []

        def fake_launch(tf, rec):
            self.launches.append(tf)
            return 4242, root / "run.log"
        p = mock.patch.object(fleetwatch, "_launch", fake_launch)
        p.start()
        self.addCleanup(p.stop)
        self.live = []
        p = mock.patch.object(fleetwatch.reconcile, "live_runs", lambda: self.live)
        p.start()
        self.addCleanup(p.stop)

    def _record(self, **over):
        rec = {"argv": ["python", "main.py", "code", "run", self.tf], "cwd": "/",
               "env": {}, "pid": 99, "started": 0, "last_seen": 10_000,
               "quick_fails": 0, "exited": 0}
        rec.update(over)
        fleetwatch._save("runs.json", {self.tf: rec})

    def _row(self, tid, status):
        self.store.upsert_code_task(self.tf, tid, tid, "GLM-5.3", "deepseek", status)

    def test_a_dead_run_with_unfinished_work_is_resumed(self):
        self._record()
        self._row("a", "merged")
        self._row("b", "failed")
        fleetwatch.tick(self.store)          # notices the exit, starts backoff
        runs = fleetwatch._load("runs.json", {})
        runs[self.tf]["exited"] = 0          # backoff elapsed
        fleetwatch._save("runs.json", runs)
        fleetwatch.tick(self.store)
        self.assertEqual(self.launches, [self.tf])

    def test_a_finished_taskfile_is_dropped_not_relaunched(self):
        self._record()
        self._row("a", "merged")
        self._row("b", "skipped")
        st = fleetwatch.tick(self.store)
        self.assertEqual(self.launches, [])
        self.assertEqual(st["done"], [self.tf])
        self.assertNotIn(self.tf, fleetwatch._load("runs.json", {}))

    def test_an_operator_stop_is_never_overruled(self):
        self._record(started=100)
        Path(config.EVENTS_LOG).write_text("\n" + json.dumps(
            {"ts": 200, "type": "run.stopped", "taskfile": self.tf}) + "\n")
        fleetwatch.tick(self.store)
        self.assertEqual(self.launches, [])
        self.assertNotIn(self.tf, fleetwatch._load("runs.json", {}))

    def test_a_run_that_keeps_dying_fast_is_parked(self):
        self._record(started=0, last_seen=5,
                     quick_fails=fleetwatch.MAX_QUICK_FAILS - 1)
        st = fleetwatch.tick(self.store)
        self.assertEqual(self.launches, [])
        self.assertIn(self.tf, st["parked"])

    def test_a_blocked_chain_waits_instead_of_launching(self):
        up = str(Path(self.tf).with_name("up.json"))
        Path(up).write_text(json.dumps({"project": {"tasks": [{"id": "u"}]}}))
        self.store.upsert_code_task(up, "u", "u", "GLM-5.3", "deepseek", "conflict")
        data = json.loads(Path(self.tf).read_text())
        data["project"]["after"] = [up]
        Path(self.tf).write_text(json.dumps(data))
        self._record(pid=None, exited=0)
        st = fleetwatch.tick(self.store)
        self.assertEqual(self.launches, [])
        self.assertTrue(st["waiting"][self.tf].startswith("chain:"))

    def test_force_is_never_replayed(self):
        self.live = [{"pid": 7, "taskfile": self.tf}]
        with mock.patch.object(fleetwatch, "_proc_launch", lambda pid: (
                ["python", "main.py", "code", "run", "--force", self.tf], "/", {})):
            fleetwatch.tick(self.store)
        rec = fleetwatch._load("runs.json", {})[self.tf]
        self.assertNotIn("--force", rec["argv"])


if __name__ == "__main__":
    unittest.main()
