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

    def test_merges_over_an_untracked_directory_in_the_blessed_repo(self):
        """The bug that blocked every merge once tasks started reaching one.

        `git status --porcelain` collapses a wholly untracked directory into
        one entry ("?? logs/"), so a branch adding "logs/x.jsonl" matched
        nothing in the dirty set, nothing was stashed, and git refused with
        "untracked working tree files would be overwritten by merge".
        """
        wt = self.alloc("t1")
        (wt / "logs").mkdir()
        (wt / "logs" / "run.jsonl").write_text("from the task\n")
        asyncio.run(gitstore.publish(wt, "task(t1): add logs/run.jsonl"))
        # the operator's copy has that whole directory untracked
        (self.repo / "logs").mkdir(exist_ok=True)
        (self.repo / "logs" / "run.jsonl").write_text("local, uncommitted\n")
        st = git(self.repo, "status", "--porcelain")
        self.assertIn("?? logs/", st, "fixture must reproduce the collapsed entry")

        with capture_events() as ev:
            asyncio.run(gitstore.merge_to_main(self.repo, "t1"))
        self.assertIn("logs/run.jsonl",
                      git(self.repo, "ls-tree", "-r", "--name-only", "HEAD"))
        # the operator's colliding file could not be restored over the merged
        # copy — that is reported, NOT treated as a failed merge
        retained = ev.of("merge.stash_retained")
        if retained:
            self.assertIn("logs/run.jsonl", retained[0]["paths"])

    def test_a_landed_merge_is_never_reported_as_a_conflict(self):
        """A stash-pop collision must not fail work that actually merged."""
        wt = self.alloc("t1")
        (wt / "shared.txt").write_text("from the task\n")
        asyncio.run(gitstore.publish(wt, "task(t1): add shared.txt"))
        (self.repo / "shared.txt").write_text("local, uncommitted\n")
        with capture_events():
            asyncio.run(gitstore.merge_to_main(self.repo, "t1"))  # must not raise
        self.assertIn("shared.txt",
                      git(self.repo, "ls-tree", "-r", "--name-only", "HEAD"))

    def test_dirty_paths_lists_files_not_directories(self):
        (self.repo / "nested").mkdir()
        (self.repo / "nested" / "a.txt").write_text("a\n")
        (self.repo / "nested" / "b.txt").write_text("b\n")
        paths = asyncio.run(gitstore._dirty_paths(self.repo))
        self.assertIn("nested/a.txt", paths)
        self.assertIn("nested/b.txt", paths)
        self.assertNotIn("nested/", paths)

    def test_branch_ahead_reflects_unmerged_commits(self):
        wt = self.alloc("t1")
        self.assertFalse(asyncio.run(gitstore.branch_ahead(self.repo, "t1")))
        (wt / "new.py").write_text("x = 1\n")
        asyncio.run(gitstore.publish(wt, "task(t1): work"))
        self.assertTrue(asyncio.run(gitstore.branch_ahead(self.repo, "t1")))


if __name__ == "__main__":
    unittest.main()


class BaseRefIsLocal(unittest.TestCase):
    """Tasks must branch from the LOCAL base, even once a remote exists.

    alloc() used to prefer origin/<base> whenever any remote was configured.
    merge_to_main merges into the LOCAL branch and the push is best-effort, so
    the first time a push is skipped or fails, local main is ahead and every
    new task would branch from a stale origin — silently reverting merged work
    the next time it published. Adding a GitHub remote would have armed that.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        base = Path(self._dir.name)
        self.repo = base / "proj"; self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "config", "user.name", "t")
        (self.repo / "a.txt").write_text("one\n")
        git(self.repo, "add", "-A"); git(self.repo, "commit", "-qm", "init")
        # a bare "remote" that is deliberately BEHIND local
        self.remote = base / "origin.git"
        git(self.repo, "clone", "--bare", "-q", str(self.repo), str(self.remote))
        git(self.repo, "remote", "add", "origin", str(self.remote))
        git(self.repo, "fetch", "-q", "origin")
        (self.repo / "b.txt").write_text("two\n")     # local moves ahead
        git(self.repo, "add", "-A"); git(self.repo, "commit", "-qm", "local only")
        self._orig_root = config.WORKTREE_ROOT
        config.WORKTREE_ROOT = str(base / "worktrees")

    def tearDown(self):
        config.WORKTREE_ROOT = self._orig_root
        self._dir.cleanup()

    def test_base_ref_is_the_local_branch(self):
        self.assertEqual(asyncio.run(gitstore._base_ref(self.repo)), "main")

    def test_a_new_worktree_contains_unpushed_local_commits(self):
        wt = asyncio.run(gitstore.alloc(self.repo, "t1"))
        self.assertTrue((wt / "b.txt").exists(),
                        "task branched from stale origin and lost local work")
