"""Worktree/diff/merge behaviour against real temporary git repos."""
import asyncio
import shutil
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


class FindingAnExistingWorktree(unittest.TestCase):
    """Resuming a task with an open PR must reuse the branch that PR came from.

    publish() used to bail with "no worktree" whenever it was the start node,
    which fired the fallthrough edge into alloc — and alloc resets task/<id> to
    the base branch. Every conflict-repair and in_review resume therefore threw
    away the work it was resuming and re-implemented from scratch.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.repo = Path(self.dir) / "repo"
        self.repo.mkdir()
        self._orig_root = config.WORKTREE_ROOT
        config.WORKTREE_ROOT = str(Path(self.dir) / "wt")
        self.addCleanup(setattr, config, "WORKTREE_ROOT", self._orig_root)
        for cmd in (["init", "-q", "-b", "main"],
                    ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            subprocess.run(["git", *cmd], cwd=self.repo, check=True,
                           capture_output=True)
        (self.repo / "f.txt").write_text("x\n")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.repo, check=True,
                       capture_output=True)

    def test_path_is_derived_without_touching_the_disk(self):
        p = gitstore.worktree_for(self.repo, "t1")
        self.assertEqual(p.name, "t1")
        self.assertFalse(p.exists())

    def test_none_when_the_task_has_no_worktree(self):
        self.assertIsNone(asyncio.run(gitstore.existing_worktree(self.repo, "t1")))

    def test_finds_the_worktree_alloc_created(self):
        wt = asyncio.run(gitstore.alloc(self.repo, "t1", base="main"))
        found = asyncio.run(gitstore.existing_worktree(self.repo, "t1"))
        self.assertEqual(found, wt)

    def test_none_once_the_worktree_is_removed(self):
        asyncio.run(gitstore.alloc(self.repo, "t1", base="main"))
        subprocess.run(["git", "worktree", "remove", "--force",
                        str(gitstore.worktree_for(self.repo, "t1"))],
                       cwd=self.repo, check=True, capture_output=True)
        self.assertIsNone(asyncio.run(gitstore.existing_worktree(self.repo, "t1")))

    def test_a_bare_directory_git_does_not_track_is_not_a_worktree(self):
        stray = gitstore.worktree_for(self.repo, "t1")
        stray.mkdir(parents=True)
        (stray / ".git").write_text("gitdir: /nowhere\n")
        self.assertIsNone(asyncio.run(gitstore.existing_worktree(self.repo, "t1")))

    def test_alloc_reports_when_it_discards_committed_work(self):
        wt = asyncio.run(gitstore.alloc(self.repo, "t1", base="main"))
        (wt / "new.txt").write_text("work\n")
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "reviewed work"], cwd=wt,
                       check=True, capture_output=True)
        with capture_events() as ev:
            asyncio.run(gitstore.alloc(self.repo, "t1", base="main"))
        reset = [f for t, f in ev.seen if t == "task.branch_reset"]
        self.assertEqual(len(reset), 1)
        self.assertEqual(reset[0]["commits_discarded"], 1)

    def test_a_fresh_alloc_reports_nothing(self):
        with capture_events() as ev:
            asyncio.run(gitstore.alloc(self.repo, "t1", base="main"))
        self.assertEqual([t for t, _ in ev.seen if t == "task.branch_reset"], [])


class SyncingATaskBranchWithItsBase(unittest.TestCase):
    """A PR that conflicts with the base was a dead end: marked conflict,
    finished, left for a human. With every task merging into one integration
    branch that is the normal cost of parallelism, and most of it is not a real
    disagreement — just a base that moved on under a long-running task.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.repo = Path(self.dir) / "repo"
        self.repo.mkdir()
        self._orig = config.WORKTREE_ROOT
        config.WORKTREE_ROOT = str(Path(self.dir) / "wt")
        self.addCleanup(setattr, config, "WORKTREE_ROOT", self._orig)
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")
        (self.repo / "shared.txt").write_text("line one\n")
        (self.repo / "other.txt").write_text("untouched\n")
        self._git("add", "-A"); self._git("commit", "-qm", "init")

    def _git(self, *a, cwd=None):
        return subprocess.run(["git", *a], cwd=cwd or self.repo, check=True,
                              capture_output=True, text=True)

    def _branch_with(self, path, text):
        wt = asyncio.run(gitstore.alloc(self.repo, "t1", base="main"))
        (wt / path).write_text(text)
        self._git("add", "-A", cwd=wt)
        self._git("commit", "-qm", "task work", cwd=wt)
        return wt

    def _advance_main(self, path, text):
        (self.repo / path).write_text(text)
        self._git("add", "-A"); self._git("commit", "-qm", "main moved on")

    def test_a_base_that_moved_elsewhere_merges_with_no_model(self):
        wt = self._branch_with("other.txt", "task changed this\n")
        self._advance_main("shared.txt", "line one\nline two\n")
        ok, conflicts, note = asyncio.run(gitstore.sync_with_base(wt, "main"))
        self.assertTrue(ok, note)
        self.assertEqual(conflicts, [])
        self.assertIn("line two", (wt / "shared.txt").read_text())
        self.assertIn("task changed this", (wt / "other.txt").read_text())

    def test_a_genuine_overlap_reports_the_conflicting_paths(self):
        wt = self._branch_with("shared.txt", "task rewrote this\n")
        self._advance_main("shared.txt", "main rewrote this\n")
        ok, conflicts, note = asyncio.run(gitstore.sync_with_base(wt, "main"))
        self.assertFalse(ok)
        self.assertEqual(conflicts, ["shared.txt"])

    def test_a_failed_sync_leaves_the_worktree_clean(self):
        # Aborting matters: a half-merged worktree would be committed by the
        # next publish, pushing conflict markers into an open pull request.
        wt = self._branch_with("shared.txt", "task rewrote this\n")
        self._advance_main("shared.txt", "main rewrote this\n")
        asyncio.run(gitstore.sync_with_base(wt, "main"))
        st = subprocess.run(["git", "status", "--porcelain"], cwd=wt,
                            capture_output=True, text=True).stdout
        self.assertEqual(st.strip(), "")
        self.assertNotIn("<<<<<<<", (wt / "shared.txt").read_text())

    def test_syncing_an_already_current_branch_is_a_no_op(self):
        wt = self._branch_with("other.txt", "task changed this\n")
        ok, conflicts, _ = asyncio.run(gitstore.sync_with_base(wt, "main"))
        self.assertTrue(ok)
        self.assertEqual(conflicts, [])

    def test_conflicts_can_be_left_in_place_for_an_agent_to_resolve(self):
        wt = self._branch_with("shared.txt", "task rewrote this\n")
        self._advance_main("shared.txt", "main rewrote this\n")
        ok, conflicts, _ = asyncio.run(
            gitstore.sync_with_base(wt, "main", keep_conflicts=True))
        self.assertFalse(ok)
        self.assertEqual(conflicts, ["shared.txt"])
        self.assertIn("<<<<<<<", (wt / "shared.txt").read_text())
        in_merge, unmerged = asyncio.run(gitstore.merge_in_progress(wt))
        self.assertTrue(in_merge)
        self.assertEqual(unmerged, ["shared.txt"])

    def test_a_resolved_merge_reports_ready_to_commit(self):
        wt = self._branch_with("shared.txt", "task rewrote this\n")
        self._advance_main("shared.txt", "main rewrote this\n")
        asyncio.run(gitstore.sync_with_base(wt, "main", keep_conflicts=True))
        (wt / "shared.txt").write_text("both changes, reconciled\n")
        subprocess.run(["git", "add", "shared.txt"], cwd=wt, check=True,
                       capture_output=True)
        ok, conflicts, note = asyncio.run(gitstore.sync_with_base(wt, "main"))
        self.assertTrue(ok, note)
        self.assertEqual(conflicts, [])

    def test_an_unresolved_merge_is_not_mistaken_for_a_clean_one(self):
        wt = self._branch_with("shared.txt", "task rewrote this\n")
        self._advance_main("shared.txt", "main rewrote this\n")
        asyncio.run(gitstore.sync_with_base(wt, "main", keep_conflicts=True))
        ok, conflicts, _ = asyncio.run(gitstore.sync_with_base(wt, "main"))
        self.assertFalse(ok)
        self.assertEqual(conflicts, ["shared.txt"])

    def test_a_clean_worktree_is_not_mid_merge(self):
        wt = asyncio.run(gitstore.alloc(self.repo, "t1", base="main"))
        self.assertEqual(asyncio.run(gitstore.merge_in_progress(wt)), (False, []))


class AdvancingTheLocalBaseBranch(unittest.TestCase):
    """`update-ref` on the CHECKED-OUT branch corrupts the working tree.

    It moves the branch pointer without touching the index or working tree, so
    every file in the repo then reads as massively modified or deleted. That
    was invisible while the operator's checkout was always `main` and the base
    was always `development` — and becomes a foot-gun the moment anyone works
    ON the integration branch, which the branch model actively encourages.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.remote = Path(self.dir) / "remote.git"
        self.repo = Path(self.dir) / "repo"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "development",
                        str(self.remote)], check=True, capture_output=True)
        subprocess.run(["git", "clone", "-q", str(self.remote), str(self.repo)],
                       check=True, capture_output=True)
        self._git("config", "user.email", "t@t"); self._git("config", "user.name", "t")
        self._git("checkout", "-qB", "development")
        (self.repo / "f.txt").write_text("one\n")
        self._git("add", "-A"); self._git("commit", "-qm", "init")
        self._git("push", "-q", "-u", "origin", "development")
        # someone else advances origin/development
        self.other = Path(self.dir) / "other"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(self.other)],
                       check=True, capture_output=True)
        self._git("config", "user.email", "o@o", cwd=self.other)
        self._git("config", "user.name", "o", cwd=self.other)
        (self.other / "f.txt").write_text("one\ntwo\n")
        self._git("add", "-A", cwd=self.other)
        self._git("commit", "-qm", "advance", cwd=self.other)
        self._git("push", "-q", "origin", "development", cwd=self.other)

    def _git(self, *a, cwd=None):
        return subprocess.run(["git", *a], cwd=cwd or self.repo, check=True,
                              capture_output=True, text=True)

    def _dirty(self):
        return subprocess.run(["git", "status", "--porcelain"], cwd=self.repo,
                              capture_output=True, text=True).stdout.strip()

    def test_it_advances_the_base_when_that_base_is_checked_out(self):
        ok, note = asyncio.run(gitstore.fast_forward_base(self.repo, "development"))
        self.assertTrue(ok, note)
        self.assertEqual((self.repo / "f.txt").read_text(), "one\ntwo\n")

    def test_the_working_tree_stays_clean(self):
        # The whole point: update-ref would leave every file looking modified.
        asyncio.run(gitstore.fast_forward_base(self.repo, "development"))
        self.assertEqual(self._dirty(), "")

    def test_it_advances_a_base_that_is_not_checked_out(self):
        self._git("checkout", "-qb", "side")
        ok, _ = asyncio.run(gitstore.fast_forward_base(self.repo, "development"))
        self.assertTrue(ok)
        head = subprocess.run(["git", "rev-parse", "development"], cwd=self.repo,
                              capture_output=True, text=True).stdout.strip()
        origin = subprocess.run(["git", "rev-parse", "origin/development"],
                                cwd=self.repo, capture_output=True,
                                text=True).stdout.strip()
        self.assertEqual(head, origin)

    def test_it_refuses_rather_than_clobbering_local_commits(self):
        (self.repo / "mine.txt").write_text("local work\n")
        self._git("add", "-A"); self._git("commit", "-qm", "local only")
        ok, note = asyncio.run(gitstore.fast_forward_base(self.repo, "development"))
        self.assertFalse(ok)
        self.assertIn("fast-forward", note.lower())
        self.assertTrue((self.repo / "mine.txt").exists())
