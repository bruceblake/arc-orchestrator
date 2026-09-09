"""Orphan reaping against a real temporary git repo."""
import asyncio
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

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
                                    "kimi", "running")
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


class LiveRunGuard(unittest.TestCase):
    def test_live_run_detection_ignores_unrelated_command_lines(self):
        """A shell wrapper mentioning main.py, code and run in scattered
        arguments must not be mistaken for an orchestrator run."""
        pids = reconcile.live_run_pids()
        self.assertNotIn(os.getpid(), pids)

    def test_refuses_to_reconcile_while_a_run_is_alive(self):
        orig = reconcile.live_run_pids
        reconcile.live_run_pids = lambda: [12345]
        try:
            with tempfile.TemporaryDirectory() as d:
                store = Store(str(Path(d) / "t.db"))
                rep = asyncio.run(reconcile.reconcile(store))
            self.assertTrue(rep["skipped"])
            self.assertIn("SKIPPED", reconcile.format_report(rep))
        finally:
            reconcile.live_run_pids = orig


if __name__ == "__main__":
    unittest.main()
