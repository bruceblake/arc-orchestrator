"""Orphan reaping against a real temporary git repo."""
import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import capture_events  # noqa: F401  (sys.path)

import config
import gitstore
import reconcile
from store import Store


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


class RepoFixture(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        base = Path(self._dir.name)
        self.repo = base / "proj"
        self.repo.mkdir()
        # A main-only repo. branch_ahead now defaults to config.BASE_BRANCH
        # (development), so the base must be named here or the comparison has
        # no ref to make — and with no ref it correctly reports "ahead" rather
        # than letting reconcile delete a worktree it cannot vouch for.
        self._orig_base = config.BASE_BRANCH
        config.BASE_BRANCH = "main"
        self.addCleanup(setattr, config, "BASE_BRANCH", self._orig_base)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "config", "user.name", "t")
        (self.repo / "README.md").write_text("hello\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "init")

        self.wt_root = base / "worktrees"
        self._orig_root = config.WORKTREE_ROOT
        config.WORKTREE_ROOT = str(self.wt_root)
        self.store = Store(str(base / "t.db"))
        # reconcile locates the blessed clone by directory name
        self._orig_find = reconcile._find_repo
        reconcile._find_repo = lambda name: self.repo if name == "proj" else None

    def tearDown(self):
        config.WORKTREE_ROOT = self._orig_root
        reconcile._find_repo = self._orig_find
        self._dir.cleanup()

    def alloc(self, tid):
        return asyncio.run(gitstore.alloc(self.repo, tid))

    def run_reconcile(self, **kw):
        return asyncio.run(reconcile.reconcile(self.store, force=True, **kw))


class WorktreeSweep(RepoFixture):
    def test_removes_a_clean_worktree_whose_branch_is_level_with_main(self):
        self.alloc("t1")
        rep = self.run_reconcile()
        self.assertEqual([w["task"] for w in rep["worktrees"]], ["t1"])
        self.assertFalse((self.wt_root / "proj" / "t1").exists())

    def test_keeps_a_worktree_holding_uncommitted_agent_edits(self):
        """The orchestrator only commits at publish, so an interrupted
        implement's whole output lives as uncommitted files in the worktree."""
        wt = self.alloc("t2")
        (wt / "agent_output.py").write_text("# hours of work\n")
        rep = self.run_reconcile()
        self.assertEqual([w["task"] for w in rep["kept"]], ["t2"])
        self.assertEqual(rep["worktrees"], [])
        self.assertTrue((wt / "agent_output.py").exists(), "agent work was deleted")

    def test_keeps_a_branch_with_unmerged_commits(self):
        wt = self.alloc("t3")
        (wt / "f.py").write_text("x = 1\n")
        git(wt, "add", "-A")
        git(wt, "commit", "-qm", "reviewed work")
        rep = self.run_reconcile()
        self.assertEqual([w["task"] for w in rep["kept"]], ["t3"])
        self.assertIn("unmerged", rep["kept"][0]["reason"])

    def test_dry_run_changes_nothing(self):
        self.alloc("t4")
        rep = self.run_reconcile(apply=False)
        self.assertEqual([w["task"] for w in rep["worktrees"]], ["t4"])
        self.assertTrue((self.wt_root / "proj" / "t4").exists())


class RowAndLeaseSweep(RepoFixture):
    def test_marks_stale_running_rows_failed_with_an_infrastructure_reason(self):
        self.store.upsert_code_task("f.json", "t1", "T1", "gpt-oss-120b",
                                    config.cross_family_reviewer("gpt-oss-120b"),
                                    "running")
        rep = self.run_reconcile()
        self.assertEqual([r["id"] for r in rep["rows"]], ["t1"])
        row = self.store.code_tasks_for("f.json")[0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], reconcile.INTERRUPTED_REASON)

    def test_the_reason_does_not_trigger_escalation(self):
        """Ties the reaper to the resume planner: reaping must not push the
        task onto a scarcer model next time."""
        import code_tasks
        self.assertFalse(
            code_tasks._is_capability_failure(reconcile.INTERRUPTED_REASON))

    def test_reaps_leases_owned_by_dead_processes(self):
        self.store.acquire_driver_lease("m", 2 ** 22, "t", 4, 1800)
        rep = self.run_reconcile()
        self.assertEqual(rep["leases"], 1)
        self.assertEqual(self.store.driver_lease_rows(), [])

    def test_force_does_not_reset_running_rows_of_a_live_taskfile(self):
        """Two taskfiles in parallel is normal. `--force` exists to clean up
        around a WEDGED process — it must not mark a healthy concurrent run's
        rows failed."""
        import reconcile as _rec
        self.store.upsert_code_task(
            "live.json", "t-live", "T", "gpt-oss-120b",
            config.cross_family_reviewer("gpt-oss-120b"), "running")
        self.store.upsert_code_task(
            "dead.json", "t-dead", "T", "gpt-oss-120b",
            config.cross_family_reviewer("gpt-oss-120b"), "running")
        orig = _rec.live_runs
        _rec.live_runs = lambda: [{"pid": 99999, "taskfile": "live.json"}]
        try:
            rep = self.run_reconcile()
        finally:
            _rec.live_runs = orig
        self.assertEqual([r["id"] for r in rep["rows"]], ["t-dead"])
        self.assertEqual([r["id"] for r in rep["rows_kept"]], ["t-live"])
        by_id = {r["id"]: r for r in self.store.code_tasks_all()}
        self.assertEqual(by_id["t-dead"]["status"], "failed")
        self.assertEqual(by_id["t-live"]["status"], "running")

    def test_settles_in_review_rows_whose_pr_is_already_merged(self):
        """The run that would have written 'merged' died first; a resume
        alone should not be required just to do bookkeeping."""
        self.alloc("t-merged")  # gives the row a worktree path _find_repo can use
        self.store.upsert_code_task(
            "f.json", "t-merged", "T", "gpt-oss-120b",
            config.cross_family_reviewer("gpt-oss-120b"), "in_review",
            branch="task/t-merged",
            worktree=str(self.wt_root / "proj" / "t-merged"))
        import gitstore as _gs
        orig = _gs.find_pr

        async def fake_find_pr(repo, tid, state="open"):
            if tid == "t-merged":
                return 42, "https://example/pr/42", "MERGED"
            return None, None, None

        _gs.find_pr = fake_find_pr
        try:
            rep = self.run_reconcile()
        finally:
            _gs.find_pr = orig
        self.assertEqual(rep["merged_settled"],
                         [{"id": "t-merged", "pr": 42,
                           "url": "https://example/pr/42"}])
        row = self.store.code_tasks_for("f.json")[0]
        self.assertEqual(row["status"], "merged")

    def test_settles_conflict_rows_whose_pr_is_already_merged(self):
        """A conflict row whose GitHub PR was already merged must be settled
        to 'merged' and its worktree cleaned up when no run is alive."""
        self.alloc("t-conflict-merged")
        self.store.upsert_code_task(
            "f.json", "t-conflict-merged", "T", "gpt-oss-120b",
            config.cross_family_reviewer("gpt-oss-120b"), "conflict",
            branch="task/t-conflict-merged",
            worktree=str(self.wt_root / "proj" / "t-conflict-merged"))
        import gitstore as _gs
        orig = _gs.find_pr

        async def fake_find_pr(repo, tid, state="open"):
            if tid == "t-conflict-merged":
                return 43, "https://example/pr/43", "MERGED"
            return None, None, None

        _gs.find_pr = fake_find_pr
        try:
            rep = self.run_reconcile()
        finally:
            _gs.find_pr = orig
        self.assertEqual(rep["merged_settled"],
                         [{"id": "t-conflict-merged", "pr": 43,
                           "url": "https://example/pr/43"}])
        row = self.store.code_tasks_for("f.json")[0]
        self.assertEqual(row["status"], "merged")
        self.assertFalse((self.wt_root / "proj" / "t-conflict-merged").exists())


class SafeReconcileWhileLive(RepoFixture):
    def test_skips_worktree_sweep_and_does_not_delete_worktrees_while_live(self):
        """While a run is alive, worktrees must not be deleted and the destructive
        sweep must be skipped."""
        self.alloc("t-clean")
        self.alloc("t-in-review-merged")
        self.store.upsert_code_task(
            "other.json", "t-in-review-merged", "T", "gpt-oss-120b",
            config.cross_family_reviewer("gpt-oss-120b"), "in_review",
            branch="task/t-in-review-merged",
            worktree=str(self.wt_root / "proj" / "t-in-review-merged"))

        orig_live = reconcile.live_runs
        reconcile.live_runs = lambda: [{"pid": 12345, "taskfile": "live.json"}]
        import gitstore as _gs
        orig_pr = _gs.find_pr

        async def fake_find_pr(repo, tid, state="open"):
            if tid == "t-in-review-merged":
                return 101, "https://example/pr/101", "MERGED"
            return None, None, None

        _gs.find_pr = fake_find_pr
        try:
            rep = asyncio.run(reconcile.reconcile(self.store, force=False))
        finally:
            reconcile.live_runs = orig_live
            _gs.find_pr = orig_pr

        self.assertTrue(rep["skipped"])
        self.assertEqual(rep["worktrees"], [])
        self.assertTrue((self.wt_root / "proj" / "t-clean").exists())
        self.assertTrue((self.wt_root / "proj" / "t-in-review-merged").exists())
        row = self.store.code_tasks_for("other.json")[0]
        self.assertEqual(row["status"], "merged")

    def test_running_rows_reset_only_when_taskfile_missing_on_disk(self):
        """While a run is alive, running rows are only reset if their taskfile
        is not the live run's taskfile AND does not exist on disk."""
        existing_tf = Path(self._dir.name) / "existing.json"
        existing_tf.write_text("{}")
        missing_tf = Path(self._dir.name) / "missing_deleted.json"
        if missing_tf.exists():
            missing_tf.unlink()

        self.store.upsert_code_task(
            "live.json", "t-live", "T", "gpt-oss-120b",
            config.cross_family_reviewer("gpt-oss-120b"), "running")
        self.store.upsert_code_task(
            str(existing_tf), "t-exist", "T", "gpt-oss-120b",
            config.cross_family_reviewer("gpt-oss-120b"), "running")
        self.store.upsert_code_task(
            str(missing_tf), "t-missing", "T", "gpt-oss-120b",
            config.cross_family_reviewer("gpt-oss-120b"), "running")

        orig_live = reconcile.live_runs
        reconcile.live_runs = lambda: [{"pid": 12345, "taskfile": "live.json"}]
        try:
            rep = asyncio.run(reconcile.reconcile(self.store, force=False))
        finally:
            reconcile.live_runs = orig_live

        self.assertTrue(rep["skipped"])
        self.assertEqual([r["id"] for r in rep["rows"]], ["t-missing"])
        self.assertEqual(set(r["id"] for r in rep["rows_kept"]), {"t-live", "t-exist"})

        by_id = {r["id"]: r for r in self.store.code_tasks_all()}
        self.assertEqual(by_id["t-missing"]["status"], "failed")
        self.assertEqual(by_id["t-missing"]["error"], reconcile.INTERRUPTED_REASON)
        self.assertEqual(by_id["t-exist"]["status"], "running")
        self.assertEqual(by_id["t-live"]["status"], "running")

    def test_merged_settlement_for_in_review_and_conflict_and_skips_open_pr(self):
        """While a run is alive, in_review and conflict rows whose PR is MERGED
        are settled to 'merged' without deleting worktrees, while OPEN or
        CONFLICTING PRs and live run taskfiles are not settled."""
        for tid in ("t-ir-merged", "t-cf-merged", "t-cf-open", "t-ir-conflicting", "t-live-merged"):
            self.alloc(tid)

        self.store.upsert_code_task(
            "other.json", "t-ir-merged", "T", "gpt-oss-120b",
            config.cross_family_reviewer("gpt-oss-120b"), "in_review",
            branch="task/t-ir-merged",
            worktree=str(self.wt_root / "proj" / "t-ir-merged"))
        self.store.upsert_code_task(
            "other.json", "t-cf-merged", "T", "gpt-oss-120b",
            config.cross_family_reviewer("gpt-oss-120b"), "conflict",
            branch="task/t-cf-merged",
            worktree=str(self.wt_root / "proj" / "t-cf-merged"))
        self.store.upsert_code_task(
            "other.json", "t-cf-open", "T", "gpt-oss-120b",
            config.cross_family_reviewer("gpt-oss-120b"), "conflict",
            branch="task/t-cf-open",
            worktree=str(self.wt_root / "proj" / "t-cf-open"))
        self.store.upsert_code_task(
            "other.json", "t-ir-conflicting", "T", "gpt-oss-120b",
            config.cross_family_reviewer("gpt-oss-120b"), "in_review",
            branch="task/t-ir-conflicting",
            worktree=str(self.wt_root / "proj" / "t-ir-conflicting"))
        self.store.upsert_code_task(
            "live.json", "t-live-merged", "T", "gpt-oss-120b",
            config.cross_family_reviewer("gpt-oss-120b"), "in_review",
            branch="task/t-live-merged",
            worktree=str(self.wt_root / "proj" / "t-live-merged"))

        orig_live = reconcile.live_runs
        reconcile.live_runs = lambda: [{"pid": 12345, "taskfile": "live.json"}]

        import gitstore as _gs
        orig_pr = _gs.find_pr

        async def fake_find_pr(repo, tid, state="open"):
            mapping = {
                "t-ir-merged": (201, "https://example/pr/201", "MERGED"),
                "t-cf-merged": (202, "https://example/pr/202", "MERGED"),
                "t-cf-open": (203, "https://example/pr/203", "OPEN"),
                "t-ir-conflicting": (204, "https://example/pr/204", "CONFLICTING"),
                "t-live-merged": (205, "https://example/pr/205", "MERGED"),
            }
            return mapping.get(tid, (None, None, None))

        _gs.find_pr = fake_find_pr
        try:
            rep = asyncio.run(reconcile.reconcile(self.store, force=False))
        finally:
            reconcile.live_runs = orig_live
            _gs.find_pr = orig_pr

        self.assertEqual(
            set(m["id"] for m in rep["merged_settled"]),
            {"t-ir-merged", "t-cf-merged"},
        )
        by_id = {r["id"]: r for r in self.store.code_tasks_all()}
        self.assertEqual(by_id["t-ir-merged"]["status"], "merged")
        self.assertEqual(by_id["t-cf-merged"]["status"], "merged")
        self.assertEqual(by_id["t-cf-open"]["status"], "conflict")
        self.assertEqual(by_id["t-ir-conflicting"]["status"], "in_review")
        self.assertEqual(by_id["t-live-merged"]["status"], "in_review")

        # Worktrees were not deleted
        for tid in ("t-ir-merged", "t-cf-merged", "t-cf-open", "t-ir-conflicting", "t-live-merged"):
            self.assertTrue((self.wt_root / "proj" / tid).exists())

    def test_reaps_dead_pid_leases_keeps_alive_pid_leases_while_live(self):
        """While a run is alive, dead-pid driver leases are reaped, but leases
        whose owner process is still alive are never dropped."""
        import time
        dead_pid = 2 ** 22
        alive_pid = os.getpid()
        self.store.acquire_driver_lease("m", alive_pid, "alive-task", 4, 1800)
        # Artificially age the alive lease beyond normal TTL
        with self.store.lock:
            self.store.conn.execute(
                "UPDATE driver_leases SET acquired_at=? WHERE pid=?",
                (0.0, alive_pid),
            )
            # Insert dead pid directly so acquire_driver_lease does not reap it early
            self.store.conn.execute(
                "INSERT INTO driver_leases(model, pid, task, acquired_at) VALUES (?,?,?,?)",
                ("m", dead_pid, "dead-task", time.time()),
            )
            self.store.conn.commit()

        orig_live = reconcile.live_runs
        reconcile.live_runs = lambda: [{"pid": 12345, "taskfile": "live.json"}]
        try:
            rep = asyncio.run(reconcile.reconcile(self.store, force=False))
        finally:
            reconcile.live_runs = orig_live

        self.assertEqual(rep["leases"], 1)
        remaining = self.store.driver_lease_rows()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["pid"], alive_pid)
        self.assertEqual(remaining[0]["task"], "alive-task")

    def test_live_run_with_deleted_tmp_taskfile_stays_running(self):
        """A live run whose taskfile was deleted while the process is still
        alive must not be reset. Process cwd is used to resolve relative argv
        tokens. A non-live row with a missing taskfile is reset."""
        temp_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(temp_dir, ignore_errors=True))

        live_tf_path = str((Path(temp_dir) / "foo.json").resolve())
        dead_tf_path = str((Path(temp_dir) / "other_dead.json").resolve())

        # Neither file exists on disk
        if Path(live_tf_path).exists():
            Path(live_tf_path).unlink()
        if Path(dead_tf_path).exists():
            Path(dead_tf_path).unlink()

        self.store.upsert_code_task(
            live_tf_path, "t-live-deleted", "T", "gpt-oss-120b",
            config.cross_family_reviewer("gpt-oss-120b"), "running")
        self.store.upsert_code_task(
            dead_tf_path, "t-dead-missing", "T", "gpt-oss-120b",
            config.cross_family_reviewer("gpt-oss-120b"), "running")

        orig_live = reconcile.live_runs
        reconcile.live_runs = lambda: [
            {"pid": 12345, "taskfile": "foo.json", "cwd": temp_dir}
        ]
        try:
            rep = asyncio.run(reconcile.reconcile(self.store, force=False))
        finally:
            reconcile.live_runs = orig_live

        self.assertEqual([r["id"] for r in rep["rows"]], ["t-dead-missing"])
        self.assertEqual([r["id"] for r in rep["rows_kept"]], ["t-live-deleted"])
        by_id = {r["id"]: r for r in self.store.code_tasks_all()}
        self.assertEqual(by_id["t-dead-missing"]["status"], "failed")
        self.assertEqual(by_id["t-dead-missing"]["error"], reconcile.INTERRUPTED_REASON)
        self.assertEqual(by_id["t-live-deleted"]["status"], "running")

    def test_unread_proc_cwd_does_not_reset_or_settle_taskfile(self):
        """When /proc/<pid>/cwd cannot be read, reconcile must not guess:
        rows matching that pid's taskfile token must not be reset or settled."""
        temp_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(temp_dir, ignore_errors=True))

        missing_tf = str((Path(temp_dir) / "proc_live.json").resolve())
        if Path(missing_tf).exists():
            Path(missing_tf).unlink()

        self.store.upsert_code_task(
            missing_tf, "t-unread-cwd", "T", "gpt-oss-120b",
            config.cross_family_reviewer("gpt-oss-120b"), "running")
        self.store.upsert_code_task(
            missing_tf, "t-unread-cf", "T", "gpt-oss-120b",
            config.cross_family_reviewer("gpt-oss-120b"), "conflict",
            branch="task/t-unread-cf",
            worktree=str(self.wt_root / "proj" / "t-unread-cf"))

        orig_live = reconcile.live_runs
        # No cwd provided and _proc_cwd will return None for non-existent pid 99998
        reconcile.live_runs = lambda: [
            {"pid": 99998, "taskfile": "proc_live.json"}
        ]
        import gitstore as _gs
        orig_pr = _gs.find_pr

        async def fake_find_pr(repo, tid, state="open"):
            return 999, "https://example/pr/999", "MERGED"

        _gs.find_pr = fake_find_pr
        try:
            rep = asyncio.run(reconcile.reconcile(self.store, force=False))
        finally:
            reconcile.live_runs = orig_live
            _gs.find_pr = orig_pr

        self.assertEqual(rep["rows"], [])
        self.assertIn("t-unread-cwd", [r["id"] for r in rep["rows_kept"]])
        self.assertEqual(rep["merged_settled"], [])
        by_id = {r["id"]: r for r in self.store.code_tasks_all()}
        self.assertEqual(by_id["t-unread-cwd"]["status"], "running")
        self.assertEqual(by_id["t-unread-cf"]["status"], "conflict")

    def test_format_report_does_not_claim_nothing_happened_when_actions_taken(self):
        """format_report must acknowledge safe bookkeeping when leases/rows
        were settled while runs were alive, rather than claiming skipped."""
        rep = {
            "live_runs": [12345],
            "rows": [{"id": "t1", "taskfile": "f.json", "model": "m"}],
            "rows_kept": [],
            "merged_settled": [{"id": "t2", "pr": 42}],
            "leases": 1,
            "worktrees": [],
            "kept": [],
            "skipped": True,
        }
        text = reconcile.format_report(rep)
        self.assertIn("SKIPPED", text)
        self.assertIn("safe bookkeeping completed", text)
        self.assertNotIn("stop them first", text)

        # But when truly nothing happened:
        empty_rep = {
            "live_runs": [12345],
            "rows": [],
            "rows_kept": [],
            "merged_settled": [],
            "leases": 0,
            "worktrees": [],
            "kept": [],
            "skipped": True,
        }
        empty_text = reconcile.format_report(empty_rep)
        self.assertIn("SKIPPED", empty_text)
        self.assertIn("stop them first", empty_text)


class LiveRunGuard(unittest.TestCase):
    def test_live_run_detection_ignores_unrelated_command_lines(self):
        """A shell wrapper mentioning main.py, code and run in scattered
        arguments must not be mistaken for an orchestrator run."""
        pids = reconcile.live_run_pids()
        self.assertNotIn(os.getpid(), pids)

    def test_live_runs_reports_the_taskfile_each_run_owns(self):
        """The duplicate-run guards key off this, so the taskfile must be the
        first non-flag argument after `code run`, not any argument."""
        runs = reconcile.live_runs()
        for r in runs:
            self.assertIn("pid", r)
            self.assertIn("taskfile", r)

    def test_refuses_to_reconcile_while_a_run_is_alive(self):
        orig = reconcile.live_run_pids
        reconcile.live_run_pids = lambda: [12345]
        try:
            with tempfile.TemporaryDirectory() as d:
                store = Store(str(Path(d) / "t.db"))
                wt_root = Path(d) / "worktrees"
                wt = wt_root / "proj" / "t1"
                wt.mkdir(parents=True)
                orig_root = config.WORKTREE_ROOT
                config.WORKTREE_ROOT = str(wt_root)
                try:
                    rep = asyncio.run(reconcile.reconcile(store))
                finally:
                    config.WORKTREE_ROOT = orig_root
                self.assertTrue(rep["skipped"])
                self.assertEqual(rep["worktrees"], [])
                self.assertTrue(wt.exists(), "worktree was deleted while run was alive")
                self.assertIn("SKIPPED", reconcile.format_report(rep))
        finally:
            reconcile.live_run_pids = orig


class LiveTaskfileMatcherTests(unittest.TestCase):
    def test_relative_argv_resolves_against_process_cwd(self):
        runs = [
            {"pid": 111, "taskfile": "wave.json", "cwd": "/tmp/proj1"},
            {"pid": 222, "taskfile": "/tmp/proj2/abs.json", "cwd": "/tmp/other"},
        ]
        matcher = reconcile.LiveTaskfileMatcher(runs)
        self.assertTrue(matcher.is_live("/tmp/proj1/wave.json"))
        self.assertTrue(matcher.is_live("wave.json"))
        self.assertFalse(matcher.is_live("/tmp/other/wave.json"))
        self.assertTrue(matcher.is_live("/tmp/proj2/abs.json"))
        self.assertEqual(matcher.matching_pids("/tmp/proj1/wave.json"), [111])
        self.assertEqual(matcher.matching_pids("wave.json"), [111])
        self.assertEqual(matcher.matching_pids("/tmp/other/wave.json"), [])
        self.assertEqual(matcher.matching_pids("/tmp/proj2/abs.json"), [222])
        self.assertEqual(len(matcher), 2)

    def test_unreadable_cwd_does_not_guess_and_protects_by_basename(self):
        runs = [
            {"pid": 333, "taskfile": "blind.json", "cwd": None},
        ]
        matcher = reconcile.LiveTaskfileMatcher(runs)
        self.assertTrue(matcher.is_live("/some/dir/blind.json"))
        self.assertTrue(matcher.is_live("blind.json"))
        self.assertEqual(matcher.matching_pids("/some/dir/blind.json"), [333])
        self.assertFalse(matcher.is_live("/some/dir/other.json"))
        self.assertEqual(matcher.matching_pids("/some/dir/other.json"), [])


class ResetStaleAndDuplicateRunCliTests(unittest.TestCase):
    def test_reset_stale_protects_live_run_with_relative_argv_and_process_cwd(self):
        import argparse
        import io
        from contextlib import redirect_stdout
        import main

        with tempfile.TemporaryDirectory() as d:
            db_path = str(Path(d) / "orchestrator.db")
            store = Store(db_path)
            live_cwd = str(Path(d) / "subrun")
            os.makedirs(live_cwd, exist_ok=True)
            live_tf = str(Path(live_cwd) / "foo.json")
            dead_tf = str(Path(d) / "dead.json")

            store.upsert_code_task(
                live_tf, "t-live", "T", "gpt-oss-120b",
                config.cross_family_reviewer("gpt-oss-120b"), "running")
            store.upsert_code_task(
                dead_tf, "t-dead", "T", "gpt-oss-120b",
                config.cross_family_reviewer("gpt-oss-120b"), "running")

            mock_runs = [{"pid": 5555, "taskfile": "foo.json", "cwd": live_cwd}]
            args = argparse.Namespace(code_cmd="status", reset_stale=True, db=db_path)
            with mock.patch("reconcile.live_runs", return_value=mock_runs), \
                    redirect_stdout(io.StringIO()) as out:
                main.cmd_code(args)

            by_id = {r["id"]: r for r in store.code_tasks_all()}
            self.assertEqual(by_id["t-live"]["status"], "running")
            self.assertEqual(by_id["t-dead"]["status"], "failed")
            self.assertIn("1 live taskfile(s) left alone", out.getvalue())

    def test_duplicate_run_guard_detects_relative_launch_against_live_process_cwd(self):
        import argparse
        import main

        with tempfile.TemporaryDirectory() as d:
            live_cwd = str(Path(d) / "active_dir")
            os.makedirs(live_cwd, exist_ok=True)
            tf_path = Path(live_cwd) / "run.json"
            tf_path.write_text(json.dumps({
                "project": {
                    "repo": d,
                    "title": "proj",
                    "tasks": []
                }
            }))

            mock_runs = [{"pid": 6666, "taskfile": "run.json", "cwd": live_cwd}]
            args = argparse.Namespace(
                code_cmd="run",
                taskfile=str(tf_path),
                repo=d,
                dry_run=False,
                force=False,
                no_wait=False,
                db=str(Path(d) / "t.db"),
            )
            with mock.patch("reconcile.live_runs", return_value=mock_runs):
                with self.assertRaises(SystemExit) as cm:
                    main.cmd_code(args)
                self.assertIn("run.json is already being run by pid 6666", str(cm.exception))


class AWrapperIsNotACompetingRun(unittest.TestCase):
    """`timeout 60 python main.py code run <file>` keeps the whole command in
    the wrapper's argv, so the wrapper matches the run pattern — and the run it
    launched then refuses to start, reporting that its own parent is already
    running the taskfile. Observed exactly that while relaunching
    minecraft-test.
    """

    def test_our_own_ancestry_is_excluded(self):
        import os
        mine = reconcile._ancestry(os.getpid())
        self.assertIn(os.getpid(), mine)
        self.assertIn(os.getppid(), mine)

    def test_ancestry_terminates_on_a_broken_chain(self):
        # A pid that does not exist must not loop or raise.
        self.assertEqual(reconcile._ancestry(2 ** 22), {2 ** 22})

    def test_ancestry_is_bounded(self):
        import os
        self.assertLessEqual(len(reconcile._ancestry(os.getpid(), limit=3)), 4)

    def test_live_runs_does_not_report_this_process_tree(self):
        # Whatever launched this test may well have "main.py code run" in its
        # argv; none of it is a live run of a taskfile.
        import os
        pids = {r["pid"] for r in reconcile.live_runs()}
        self.assertNotIn(os.getpid(), pids)
        self.assertNotIn(os.getppid(), pids)


if __name__ == "__main__":
    unittest.main()
