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
        # These fixtures build main-only repos. branch_ahead now defaults to
        # config.BASE_BRANCH (development), so the base must be named here or
        # there is no ref to compare against — and with no ref it correctly
        # reports "ahead", because a check that cannot vouch for a branch must
        # not let reconcile delete its worktree.
        self._orig_base = config.BASE_BRANCH
        config.BASE_BRANCH = "main"
        self.addCleanup(setattr, config, "BASE_BRANCH", self._orig_base)
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
        # what a merged pull request leaves behind: main has advanced
        git(self.repo, "merge", "--no-ff", "-q", "-m", "merge task/t2", "task/t2")
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


class BranchAhead(RepoFixture):
    def test_branch_ahead_reflects_unmerged_commits(self):
        wt = self.alloc("t1")
        self.assertFalse(asyncio.run(gitstore.branch_ahead(self.repo, "t1")))
        (wt / "new.py").write_text("x = 1\n")
        asyncio.run(gitstore.publish(wt, "task(t1): work"))
        self.assertTrue(asyncio.run(gitstore.branch_ahead(self.repo, "t1")))



class BaseRefIsLocal(unittest.TestCase):
    """Tasks must branch from the LOCAL base, even once a remote exists.

    alloc() used to prefer origin/<base> whenever any remote was configured.
    The local base is what pr_merge fast-forwards after each merge and it can
    legitimately be ahead of origin (an operator's unpushed commit, a failed
    push), so the first time that happened every new task would have branched
    from a stale origin — silently reverting merged work the next time it
    published. Adding a GitHub remote would have armed that.
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


class BranchAheadUsesTheIntegrationBranch(unittest.TestCase):
    """Comparing against main disabled reconcile's cleanup entirely.

    The fleet merges into development; main is promoted to separately and lags
    it — 52 commits behind when this was written. A branch fully merged into
    development still had commits main lacked, so reconcile classified it
    "ahead", kept its worktree, and the cleanup it exists to perform never
    happened. Measured: task/graph-admission-control, 0 ahead of development
    and 30 ahead of main.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.repo = Path(self.dir) / "repo"
        self.repo.mkdir()
        self._orig_base = config.BASE_BRANCH
        self._orig_root = config.WORKTREE_ROOT
        config.WORKTREE_ROOT = str(Path(self.dir) / "wt")
        self.addCleanup(self._restore)
        for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"],
                    ["config", "user.name", "t"]):
            subprocess.run(["git", *cmd], cwd=self.repo, check=True, capture_output=True)
        (self.repo / "f.txt").write_text("one\n")
        self._commit("init")
        subprocess.run(["git", "branch", "development"], cwd=self.repo,
                       check=True, capture_output=True)

    def _restore(self):
        config.BASE_BRANCH = self._orig_base
        config.WORKTREE_ROOT = self._orig_root

    def _commit(self, msg):
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", msg], cwd=self.repo, check=True,
                       capture_output=True)

    def _advance(self, branch, text):
        subprocess.run(["git", "checkout", "-q", branch], cwd=self.repo, check=True,
                       capture_output=True)
        (self.repo / "f.txt").write_text(text)
        self._commit(f"advance {branch}")
        subprocess.run(["git", "checkout", "-q", "main"], cwd=self.repo, check=True,
                       capture_output=True)

    def test_a_branch_level_with_development_is_not_ahead(self):
        config.BASE_BRANCH = "development"
        self._advance("development", "dev moved on\n")
        subprocess.run(["git", "branch", "task/t1", "development"], cwd=self.repo,
                       check=True, capture_output=True)
        self.assertFalse(asyncio.run(gitstore.branch_ahead(self.repo, "t1")))

    def test_the_same_branch_looks_ahead_of_a_lagging_main(self):
        # The bug, stated directly: main lags, so the identical branch reads
        # as unmerged and its worktree is kept forever.
        config.BASE_BRANCH = "development"
        self._advance("development", "dev moved on\n")
        subprocess.run(["git", "branch", "task/t1", "development"], cwd=self.repo,
                       check=True, capture_output=True)
        self.assertTrue(asyncio.run(gitstore.branch_ahead(self.repo, "t1", base="main")))

    def test_real_unmerged_work_is_still_ahead(self):
        config.BASE_BRANCH = "development"
        subprocess.run(["git", "branch", "task/t1", "development"], cwd=self.repo,
                       check=True, capture_output=True)
        self._advance("task/t1", "task work\n")
        self.assertTrue(asyncio.run(gitstore.branch_ahead(self.repo, "t1")))

    def test_a_missing_branch_is_not_ahead(self):
        config.BASE_BRANCH = "development"
        self.assertFalse(asyncio.run(gitstore.branch_ahead(self.repo, "nope")))


class PromotionWithOneBranch(unittest.TestCase):
    """main -> main is not a pull request GitHub will accept.

    Returning its error ("No commits between main and main") reads like a bug
    rather than a configuration choice, so the one-branch case is refused up
    front with a reason that explains itself.
    """

    def setUp(self):
        self._base, self._prod = config.BASE_BRANCH, config.PROD_BRANCH
        self.addCleanup(self._restore)

    def _restore(self):
        config.BASE_BRANCH, config.PROD_BRANCH = self._base, self._prod

    def test_it_refuses_and_says_why(self):
        config.BASE_BRANCH = config.PROD_BRANCH = "main"
        n, url, note = asyncio.run(gitstore.open_promotion_pr("/nonexistent"))
        self.assertIsNone(n)
        self.assertIsNone(url)
        self.assertIn("not configured", note)
        self.assertIn("main", note)

    def test_it_does_not_refuse_when_two_branches_are_configured(self):
        config.BASE_BRANCH, config.PROD_BRANCH = "development", "main"
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        repo = Path(d) / "r"
        repo.mkdir()
        for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"],
                    ["config", "user.name", "t"]):
            subprocess.run(["git", *cmd], cwd=repo, check=True, capture_output=True)
        (repo / "f").write_text("x\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "i"], cwd=repo, check=True,
                       capture_output=True)
        subprocess.run(["git", "branch", "development"], cwd=repo, check=True,
                       capture_output=True)
        # No remote and nothing ahead, so it declines for a DIFFERENT reason.
        # What matters is that the one-branch guard did not short-circuit it.
        _, _, note = asyncio.run(gitstore.open_promotion_pr(repo))
        self.assertNotIn("not configured", note or "")

    def test_config_agrees(self):
        config.BASE_BRANCH = config.PROD_BRANCH = "main"
        self.assertFalse(config.promotion_configured())
        config.BASE_BRANCH = "development"
        self.assertTrue(config.promotion_configured())


class DriftConflictsArePreventable(unittest.TestCase):
    """The conflict class that syncing at publish removes.

    A task branches from base and opens its PR a median of 101 minutes later —
    13.5 hours at the extreme, measured over this fleet. Other tasks merge into
    base throughout. If nothing reconciles the two until GitHub refuses the
    merge, a task whose changes never overlapped anyone's still fails, after
    two reviewers have read a diff against a base that no longer exists.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.repo = Path(self.dir) / "repo"
        self.repo.mkdir()
        self._orig_root, self._orig_base = config.WORKTREE_ROOT, config.BASE_BRANCH
        config.WORKTREE_ROOT = str(Path(self.dir) / "wt")
        config.BASE_BRANCH = "main"
        self.addCleanup(self._restore)
        for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"],
                    ["config", "user.name", "t"]):
            subprocess.run(["git", *cmd], cwd=self.repo, check=True, capture_output=True)
        (self.repo / "theirs.txt").write_text("base line\n")
        (self.repo / "mine.txt").write_text("original\n")
        self._commit("init")

    def _restore(self):
        config.WORKTREE_ROOT, config.BASE_BRANCH = self._orig_root, self._orig_base

    def _commit(self, msg, cwd=None):
        cwd = cwd or self.repo
        subprocess.run(["git", "add", "-A"], cwd=cwd, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", msg], cwd=cwd, check=True,
                       capture_output=True)

    def test_a_task_that_never_overlapped_merges_cleanly_after_a_sync(self):
        wt = asyncio.run(gitstore.alloc(self.repo, "t1", base="main"))
        # base moves on in a file the task never touches
        (self.repo / "theirs.txt").write_text("base line\nsomeone else\n")
        self._commit("base moved on")
        # the task edits its own file and commits
        (wt / "mine.txt").write_text("my work\n")
        self._commit("task work", cwd=wt)
        ok, conflicts, _ = asyncio.run(gitstore.sync_with_base(wt, "main"))
        self.assertTrue(ok)
        self.assertEqual(conflicts, [])
        # both changes are present: drift resolved without anyone's help
        self.assertIn("someone else", (wt / "theirs.txt").read_text())
        self.assertIn("my work", (wt / "mine.txt").read_text())

    def test_a_genuine_overlap_is_still_reported_not_hidden(self):
        wt = asyncio.run(gitstore.alloc(self.repo, "t1", base="main"))
        (self.repo / "mine.txt").write_text("base rewrote it\n")
        self._commit("base touched the same file")
        (wt / "mine.txt").write_text("task rewrote it\n")
        self._commit("task work", cwd=wt)
        ok, conflicts, _ = asyncio.run(
            gitstore.sync_with_base(wt, "main", keep_conflicts=True))
        self.assertFalse(ok)
        self.assertEqual(conflicts, ["mine.txt"])

    def test_head_moves_when_the_sync_creates_a_merge_commit(self):
        # publish pushes `head`; if the sync's merge commit did not update it,
        # the reconciled branch would never leave the machine.
        wt = asyncio.run(gitstore.alloc(self.repo, "t1", base="main"))
        (self.repo / "theirs.txt").write_text("moved\n")
        self._commit("base moved on")
        (wt / "mine.txt").write_text("my work\n")
        self._commit("task work", cwd=wt)
        before = asyncio.run(gitstore.head(wt))
        asyncio.run(gitstore.sync_with_base(wt, "main"))
        self.assertNotEqual(asyncio.run(gitstore.head(wt)), before)

    def test_head_is_readable_and_stable_when_nothing_changed(self):
        wt = asyncio.run(gitstore.alloc(self.repo, "t1", base="main"))
        before = asyncio.run(gitstore.head(wt))
        self.assertTrue(before)
        asyncio.run(gitstore.sync_with_base(wt, "main"))
        self.assertEqual(asyncio.run(gitstore.head(wt)), before)


if __name__ == "__main__":
    unittest.main()
