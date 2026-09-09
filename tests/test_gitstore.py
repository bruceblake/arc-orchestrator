"""Worktree/diff/merge behaviour against real temporary git repos."""
import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import capture_events  # noqa: F401  (sys.path)

import config
import gitstore


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


class RepoFixture(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        base = Path(self._dir.name)
        self.repo = base / "proj"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "config", "user.name", "t")
        (self.repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "init")
        self._orig_root = config.WORKTREE_ROOT
        config.WORKTREE_ROOT = str(base / "worktrees")

    def tearDown(self):
        config.WORKTREE_ROOT = self._orig_root
        self._dir.cleanup()

    def alloc(self, tid):
        return asyncio.run(gitstore.alloc(self.repo, tid))

    def diff(self, wt):
        return asyncio.run(gitstore.diff_full(wt, "main"))


class ReviewDiff(RepoFixture):
    def test_shows_this_tasks_own_changes(self):
        wt = self.alloc("t1")
        (wt / "calc.py").write_text("def add(a, b):\n    return a + b\n\n"
                                    "def multiply(a, b):\n    return a * b\n")
        d = self.diff(wt)
        self.assertIn("multiply", d)

    def test_includes_untracked_new_files(self):
        wt = self.alloc("t1")
        (wt / "brand_new.py").write_text("x = 1\n")
        self.assertIn("brand_new.py", self.diff(wt))

    def test_a_sibling_merge_does_not_pollute_this_tasks_diff(self):
        """The regression that made parallelism self-defeating.

        Merges are serialized but tasks run in parallel, so main moves forward
        while a task is still working. Diffing the worktree against the live
        main ref showed every file the sibling had merged as DELETED by this
        task; reviewers rejected the scope violation, and the task burned its
        whole fix budget and both escalations on work that was correct.
        """
        wt = self.alloc("t1")
        (wt / "calc.py").write_text("def add(a, b):\n    return a + b\n\n"
                                    "def multiply(a, b):\n    return a * b\n")
        # meanwhile a sibling task lands a brand-new file on main
        sib = self.alloc("t2")
        (sib / "mathx.py").write_text("def divide(a, b):\n    return a / b\n")
        asyncio.run(gitstore.publish(sib, "task(t2): divide"))
        asyncio.run(gitstore.merge_to_main(self.repo, "t2"))
        self.assertIn("mathx.py", git(self.repo, "ls-tree", "--name-only", "HEAD"))

        d = self.diff(wt)
        self.assertIn("multiply", d, "the task's own change vanished")
        self.assertNotIn("mathx.py", d,
                         "a sibling's merged file leaked into this task's review diff")

    def test_empty_worktree_reports_an_empty_diff(self):
        self.assertEqual(self.diff(self.alloc("t1")), "(empty diff)")


class Alloc(RepoFixture):
    def test_realloc_resets_the_branch_and_discards_rejected_work(self):
        wt = self.alloc("t1")
        (wt / "junk.py").write_text("rejected\n")
        asyncio.run(gitstore.publish(wt, "task(t1): rejected attempt"))
        wt2 = self.alloc("t1")
        self.assertFalse((wt2 / "junk.py").exists(),
                         "a rejected attempt leaked into the retry")


class MergeToMain(RepoFixture):
    def test_merges_a_task_branch(self):
        wt = self.alloc("t1")
        (wt / "new.py").write_text("x = 1\n")
        asyncio.run(gitstore.publish(wt, "task(t1): add new.py"))
        asyncio.run(gitstore.merge_to_main(self.repo, "t1"))
        self.assertIn("new.py", git(self.repo, "ls-tree", "--name-only", "HEAD"))

    def test_tolerates_an_unrelated_dirty_file_in_the_blessed_repo(self):
        """The blessed clone doubles as the operator's working copy."""
        wt = self.alloc("t1")
        (wt / "new.py").write_text("x = 1\n")
        asyncio.run(gitstore.publish(wt, "task(t1): add new.py"))
        (self.repo / "calc.py").write_text("# operator was mid-edit\n")
        asyncio.run(gitstore.merge_to_main(self.repo, "t1"))
        self.assertIn("new.py", git(self.repo, "ls-tree", "--name-only", "HEAD"))
        self.assertIn("operator was mid-edit", (self.repo / "calc.py").read_text(),
                      "the operator's uncommitted edit was lost")

    def test_branch_ahead_reflects_unmerged_commits(self):
        wt = self.alloc("t1")
        self.assertFalse(asyncio.run(gitstore.branch_ahead(self.repo, "t1")))
        (wt / "new.py").write_text("x = 1\n")
        asyncio.run(gitstore.publish(wt, "task(t1): work"))
        self.assertTrue(asyncio.run(gitstore.branch_ahead(self.repo, "t1")))


if __name__ == "__main__":
    unittest.main()
