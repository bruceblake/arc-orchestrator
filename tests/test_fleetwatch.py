"""The watchdog resumes dead runs, respects a Stop, and parks what keeps dying."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (redirects the event log and DB first)
from helpers import capture_events  # noqa: E402

import audit  # noqa: E402
import config  # noqa: E402
import fleetwatch  # noqa: E402
from store import Store  # noqa: E402


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


class DurableTaskfileCopies(unittest.TestCase):
    """A taskfile that a reboot deleted must not end as 'taskfile unreadable'.

    On 2026-09-24 a WSL restart wiped /tmp, seven runs were parked by the
    watchdog with that message, and their code_tasks rows stayed `running`
    forever: nothing else on the fleet notices a plan that no longer exists.
    """

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
        # A taskfile that LOOKS like it lives under /tmp, without this test
        # depending on the real one being writable.
        self._old_tmp = fleetwatch.TMP_DIRS
        fleetwatch.TMP_DIRS = (str(root / "tmp"),)
        self.addCleanup(setattr, fleetwatch, "TMP_DIRS", self._old_tmp)
        (root / "tmp").mkdir()
        self.tf = str(root / "tmp" / "wave.json")
        Path(self.tf).write_text(json.dumps({"project": {"tasks": [{"id": "a"}]}}))
        self.store = Store(str(root / "t.db"))

    def test_a_live_runs_taskfile_is_copied_once(self):
        rec = {}
        copy = fleetwatch.keep_taskfile(self.tf, rec)
        self.assertTrue(Path(copy).is_file())
        self.assertEqual(Path(copy).read_text(), Path(self.tf).read_text())
        self.assertEqual(rec["taskfile_copy"], str(copy))
        # A second sighting with unchanged content must not rewrite it: resume
        # is driven by the file on disk, and re-copying could capture an edit
        # made mid-run.
        stamp = Path(copy).stat().st_mtime_ns
        self.assertEqual(fleetwatch.keep_taskfile(self.tf, rec), copy)
        self.assertEqual(Path(copy).stat().st_mtime_ns, stamp)

    def test_a_changed_taskfile_replaces_the_copy(self):
        rec = {}
        copy = fleetwatch.keep_taskfile(self.tf, rec)
        # Make the source strictly newer than the copy, which is what a real
        # edit does (the copy was just written, so same-second writes need the
        # explicit bump).
        Path(self.tf).write_text(json.dumps({"project": {"tasks": [{"id": "b"}]}}))
        os.utime(self.tf, ns=(Path(copy).stat().st_mtime_ns + 10**9,) * 2)
        fleetwatch.keep_taskfile(self.tf, rec)
        self.assertIn('"b"', Path(rec["taskfile_copy"]).read_text())

    def test_an_older_source_never_overwrites_a_newer_copy(self):
        """The rejection: differing bytes are not enough — an OLDER source loses.

        A rollback that preserved mtimes, a half-written taskfile, or a tick
        that read stale bytes all present as "different bytes, same path". If
        those replace the copy, the durable record of the plan is destroyed by
        exactly the accident the copy exists to survive — and once the original
        is gone, the copy is all that is left.
        """
        rec = {}
        copy = fleetwatch.keep_taskfile(self.tf, rec)
        kept = Path(copy).read_text()
        # The source now holds OLDER content, with an mtime before the copy's.
        Path(self.tf).write_text(json.dumps({"project": {"tasks": [{"id": "old"}]}}))
        os.utime(self.tf, ns=(Path(copy).stat().st_mtime_ns - 10**9,) * 2)
        self.assertEqual(fleetwatch.keep_taskfile(self.tf, rec), copy)
        self.assertEqual(Path(copy).read_text(), kept,
                         "an older source replaced a newer durable copy")

    def test_an_equal_mtime_does_not_replace_the_copy(self):
        """"Newer" is strict: a same-instant source is not evidence of an edit."""
        rec = {}
        copy = fleetwatch.keep_taskfile(self.tf, rec)
        kept = Path(copy).read_text()
        Path(self.tf).write_text(json.dumps({"project": {"tasks": [{"id": "same"}]}}))
        os.utime(self.tf, ns=(Path(copy).stat().st_mtime_ns,) * 2)
        fleetwatch.keep_taskfile(self.tf, rec)
        self.assertEqual(Path(copy).read_text(), kept)

    def test_a_copy_that_is_absent_is_always_written(self):
        """The absent-copy case IS the reboot recovery, so no mtime may gate it."""
        rec = {}
        copy = fleetwatch.keep_taskfile(self.tf, rec)
        Path(copy).unlink()
        Path(self.tf).write_text(json.dumps({"project": {"tasks": [{"id": "c"}]}}))
        again = fleetwatch.keep_taskfile(self.tf, rec)
        self.assertTrue(Path(again).is_file())
        self.assertIn('"c"', Path(again).read_text())

    def test_a_missing_original_is_restored_from_the_copy_and_resumed(self):
        rec = {"argv": ["python", "main.py", "code", "run", self.tf], "cwd": "/",
               "env": {}, "pid": None, "started": 0, "last_seen": 0,
               "quick_fails": 0, "exited": 0}
        fleetwatch.keep_taskfile(self.tf, rec)
        Path(self.tf).unlink()          # the reboot
        self.addCleanup(lambda: Path(self.tf).exists() and Path(self.tf).unlink())
        launches = []
        with mock.patch.object(fleetwatch, "_launch",
                               lambda tf, r: (launches.append(tf), (4242, "log"))[1]), \
                mock.patch.object(fleetwatch.reconcile, "live_runs", lambda: []):
            fleetwatch._save("runs.json", {self.tf: rec})
            st = fleetwatch.tick(self.store)
        self.assertTrue(Path(self.tf).is_file(), "the copy was not put back")
        self.assertEqual(launches, [self.tf])
        self.assertEqual(st["parked"], {})

    def test_a_taskfile_with_no_copy_still_parks(self):
        Path(self.tf).unlink()
        rec = {"argv": [], "cwd": "/", "env": {}, "pid": None, "started": 0,
               "last_seen": 0, "quick_fails": 0, "exited": 0}
        with mock.patch.object(fleetwatch.reconcile, "live_runs", lambda: []):
            fleetwatch._save("runs.json", {self.tf: rec})
            st = fleetwatch.tick(self.store)
        self.assertEqual(st["parked"][self.tf], "taskfile unreadable")

    def test_a_taskfile_under_tmp_is_warned_about(self):
        with mock.patch.object(fleetwatch.reconcile, "live_runs",
                               lambda: [{"pid": 7, "taskfile": self.tf}]), \
                mock.patch.object(fleetwatch, "_proc_launch",
                                  lambda pid: (["python", "main.py"], "/", {})):
            with capture_events() as seen:
                fleetwatch.tick(self.store)
        warnings = seen.of("watchdog.tmp_taskfile")
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["taskfile"], self.tf)
        self.assertTrue(warnings[0]["copy"], "the warning must name the durable copy")

    def test_a_copy_name_separates_two_identical_basenames(self):
        other = str(Path(self.tmp.name) / "wave.json")
        Path(other).write_text(Path(self.tf).read_text())
        self.assertNotEqual(fleetwatch._tf_copy_name(self.tf),
                            fleetwatch._tf_copy_name(other))

    def test_a_different_taskfile_never_overwrites_a_copy(self):
        """A basename is not an identity: `/a/wave.json` is not `/b/wave.json`.

        Two plans sharing a basename is the normal case here — every wave is
        `plan.json` in its own directory — so the path digest keeps their
        copies apart, and the recorded source path stops a second file from
        claiming the first one's copy.
        """
        rec = {}
        copy = fleetwatch.keep_taskfile(self.tf, rec)
        other = str(Path(self.tmp.name) / "elsewhere" / "wave.json")
        Path(other).parent.mkdir()
        Path(other).write_text(json.dumps({"project": {"tasks": [{"id": "z"}]}}))
        self.assertIsNone(fleetwatch.keep_taskfile(other, rec),
                          "a different path must not claim another plan's copy")
        self.assertEqual(rec["taskfile_copy"], str(copy))
        self.assertIn('"a"', Path(copy).read_text())


class NoWatchdogMeansNothingResumesWork(unittest.TestCase):
    """`main.py audit` is the only thing that can see a fleet with no watchdog.

    A watchdog that is not running looks exactly like a fleet with nothing to
    do, and a fleet that is up but has no watchdog is one crash away from work
    that nobody retries.
    """

    def setUp(self):
        self._orig = audit._sh
        self.addCleanup(setattr, audit, "_sh", self._orig)

    def test_a_dead_watchdog_is_a_warning_with_an_action(self):
        audit._sh = lambda *a, **k: (0, "Linger=yes\n", "")
        with mock.patch.object(audit, "_watchdog_pids", lambda: []):
            findings = [f for f in audit.audit_watchdog() if "fleetwatch" in f["what"]]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "warning")
        self.assertIn("arc-watchdog", findings[0]["action"])

    def test_a_live_watchdog_is_not_reported(self):
        audit._sh = lambda *a, **k: (0, "Linger=yes\n", "")
        with mock.patch.object(audit, "_watchdog_pids", lambda: [1234]):
            self.assertEqual(audit.audit_watchdog(), [])

    def test_linger_off_is_a_warning_naming_the_command(self):
        audit._sh = lambda *a, **k: (0, "Linger=no\n", "")
        with mock.patch.object(audit, "_watchdog_pids", lambda: [1234]), \
                mock.patch.dict(os.environ, {"USER": "proxyie"}):
            findings = audit.audit_watchdog()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "warning")
        self.assertIn("enable-linger proxyie", findings[0]["action"])

    def test_no_loginctl_is_skipped_quietly(self):
        """Containers and dev boxes have no loginctl; that is not a finding."""
        audit._sh = lambda *a, **k: (1, "", "loginctl: command not found")
        with mock.patch.object(audit, "_watchdog_pids", lambda: [1234]):
            self.assertEqual(audit.audit_watchdog(), [])

    def test_an_empty_linger_answer_is_not_a_warning(self):
        audit._sh = lambda *a, **k: (0, "", "")
        with mock.patch.object(audit, "_watchdog_pids", lambda: [1234]):
            self.assertEqual(audit.audit_watchdog(), [])

    def test_the_watchdog_is_only_found_by_argv_token(self):
        """A process that merely mentions the file is not the watchdog."""
        with mock.patch.object(Path, "iterdir") as it:
            entry = mock.MagicMock()
            entry.name = "1"
            entry.__truediv__ = lambda s, n: _FakeProc(
                b"grep\0fleetwatch.py\0" if n == "cmdline" else b"")
            it.return_value = [entry]
            self.assertEqual(audit._watchdog_pids(), [])


class _FakeProc:
    """A /proc/<pid> entry holding only a cmdline, for the token test."""

    def __init__(self, cmdline):
        self._cmdline = cmdline

    def read_bytes(self):
        return self._cmdline


if __name__ == "__main__":
    unittest.main()
