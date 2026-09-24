"""Worktree/diff/merge behaviour against real temporary git repos."""
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
    def _channels(self, wt):
        channel = wt / ".arc"
        channel.mkdir()
        (channel / "board.jsonl").write_text('{"message":"runtime"}\n')
        (channel / "plan_proposals.jsonl").write_text('{"kind":"note"}\n')

    def _assert_channels_stay_out(self, wt):
        diff = self.diff(wt)
        self.assertIn("return a + b + 1", diff)
        self.assertNotIn("runtime", diff)
        self.assertNotIn('"kind":"note"', diff)
        head = asyncio.run(gitstore.publish(wt, "task(t1): fix"))
        files = git(wt, "show", "--name-only", "--format=", head).split()
        self.assertNotIn(".arc/board.jsonl", files)
        self.assertNotIn(".arc/plan_proposals.jsonl", files)
        return files

    def test_ignored_agent_channels_do_not_break_review_or_publish(self):
        """Git 2.55 rejects an explicit add pathspec for an ignored .arc file."""
        wt = self.alloc("t1")
        (wt / ".gitignore").write_text(".arc/\n")
        (wt / "calc.py").write_text("def add(a, b):\n    return a + b + 1\n")
        self._channels(wt)
        files = self._assert_channels_stay_out(wt)
        self.assertEqual(files, [".gitignore", "calc.py"])

    def test_unignored_agent_channels_stay_out_of_review_and_publish(self):
        """A project that does not ignore .arc still must not publish channels."""
        wt = self.alloc("t1")
        (wt / "calc.py").write_text("def add(a, b):\n    return a + b + 1\n")
        self._channels(wt)
        files = self._assert_channels_stay_out(wt)
        self.assertEqual(files, ["calc.py"])

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

    def test_alloc_replaces_a_hollow_directory_git_does_not_track(self):
        """Resume of prison-escape died on this shape.

        The directory was still on disk, .git was 0 bytes, and the admin
        gitdir was invalid. `worktree remove` said "not a working tree" and
        `worktree add --force` still refused with "already exists".
        """
        stray = gitstore.worktree_for(self.repo, "t1")
        stray.mkdir(parents=True)
        (stray / ".git").write_text("")
        (stray / "f.txt").write_text("")
        admin = self.repo / ".git" / "worktrees" / "t1"
        admin.mkdir(parents=True)
        (admin / "gitdir").write_text("")
        wt = asyncio.run(gitstore.alloc(self.repo, "t1", base="main"))
        self.assertEqual((wt / "f.txt").read_text(), "x\n")
        self.assertGreater((wt / ".git").stat().st_size, 0)

    def test_a_zero_byte_checkout_of_a_real_blob_is_not_intact(self):
        wt = asyncio.run(gitstore.alloc(self.repo, "t1", base="main"))
        (wt / "f.txt").write_text("")
        self.assertFalse(asyncio.run(
            gitstore._checkout_intact(self.repo, wt, "main")))

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


class CheckpointingWorktreeWork(RepoFixture):
    """A worktree is state. alloc resets task/<id> to base on every (re)alloc,
    which on 2026-09-24 threw away the reviewed work of three tasks in one
    reboot-resume (it survived only because those branches had been pushed).
    The checkpoint is the fix: the work is saved before anything discards it,
    and a resume restores it instead of starting over.
    """

    def setUp(self):
        super().setUp()
        self._orig_cp = config.CHECKPOINT_DIR
        self._cps = Path(self._dir.name) / "checkpoints"
        config.CHECKPOINT_DIR = self._cps
        self.addCleanup(setattr, config, "CHECKPOINT_DIR", self._orig_cp)
        (self.repo / ".gitignore").write_text(".arc/\n.reasonix/\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "ignore channels")

    def work(self, wt):
        """One attempt's output: a commit, an edit, an untracked file, channels."""
        (wt / "committed.txt").write_text("done\n")
        git(wt, "add", "-A", "--", ".")
        git(wt, "commit", "-qm", "attempt work")
        (wt / "calc.py").write_text("def add(a, b):\n    return a + b + 0\n")
        (wt / "untracked.txt").write_text("new file\n")
        (wt / ".arc").mkdir(exist_ok=True)
        (wt / ".arc" / "board.jsonl").write_text('{"kind":"note"}\n')
        (wt / ".arc" / "plan_proposals.jsonl").write_text('{"kind":"note"}\n')
        (wt / ".reasonix").mkdir(exist_ok=True)
        (wt / ".reasonix" / "state.json").write_text("{}\n")

    def test_captures_commits_uncommitted_and_untracked_work(self):
        wt = self.alloc("t1")
        self.work(wt)
        p = asyncio.run(gitstore.checkpoint(self.repo, "t1", wt, "x1",
                                            model="m", attempt=1))
        self.assertTrue(p.exists())
        meta = json.loads(p.with_suffix(".json").read_text())
        self.assertEqual(sorted(meta["files"]),
                         ["calc.py", "committed.txt", "untracked.txt"])
        self.assertEqual(meta["commits"], 1)
        self.assertEqual(meta["label"], "x1")
        self.assertEqual(meta["attempt"], 1)
        self.assertEqual(meta["model"], "m")
        self.assertTrue(meta["head"])
        self.assertEqual(meta["merge_base"], git(self.repo, "rev-parse", "main").strip())

    def test_excludes_the_channel_files_and_reasonix_state(self):
        wt = self.alloc("t1")
        self.work(wt)
        p = asyncio.run(gitstore.checkpoint(self.repo, "t1", wt, "x1"))
        patch = p.read_text()
        self.assertNotIn(".arc/", patch)
        self.assertNotIn("board.jsonl", patch)
        self.assertNotIn(".reasonix", patch)

    def test_non_ascii_paths_are_captured_not_silently_dropped(self):
        """PR review: `ls-files` C-quotes a non-ASCII path by default, so it
        yielded `"caf\\303\\251.txt"` — a string naming no file. Handed to
        `diff --no-index` git exited 1 ("Could not access …") with EMPTY stdout,
        and 1 is also the exit code for "the files differ", so the empty blob
        was appended and the quoted name recorded: checkpoint reported SUCCESS
        with a 0-byte patch and the file was gone. -z emits raw bytes, and a
        file whose per-file diff produced nothing is skipped, not recorded.
        """
        wt = self.alloc("t1")
        names = ["café.txt", "日本語.md", "sp ace.txt"]
        for n in names:
            (wt / n).write_text(f"content of {n}\n")
        p = asyncio.run(gitstore.checkpoint(self.repo, "t1", wt, "x1"))
        meta = json.loads(p.with_suffix(".json").read_text())
        self.assertEqual(sorted(meta["files"]), sorted(names))
        for f in meta["files"]:
            self.assertNotIn("\\303", f)
            self.assertFalse(f.startswith('"'), f)
        self.assertGreater(p.stat().st_size, 0)
        git(wt, "reset", "-q", "--hard", "HEAD")
        git(wt, "clean", "-qfd")
        res = asyncio.run(gitstore.restore_checkpoint(self.repo, "t1", wt))
        self.assertTrue(res["restored"], res)
        for n in names:
            self.assertEqual((wt / n).read_text(), f"content of {n}\n", n)

    def test_a_non_utf8_path_round_trips(self):
        """The same defect one layer down: `_git` decodes with errors="replace",
        which turns a name holding a raw non-UTF-8 byte into a string that names
        nowhere. fsdecode's surrogateescape round-trips it instead."""
        raw = b"lat\xe9n1.txt"          # 0xE9 is not valid UTF-8
        name = os.fsdecode(raw)
        wt = self.alloc("t1")
        # Do NOT chdir: the test process is shared, and leaving cwd inside a
        # torn-down temp worktree breaks every later test (it showed up as an
        # unrelated GitHubQuota error). A bytes path through builtin open()
        # reaches the same file without touching the process cwd.
        with open(os.path.join(os.fsencode(wt), raw), "wb") as fh:
            fh.write(b"latin1 name\n")
        p = asyncio.run(gitstore.checkpoint(self.repo, "t1", wt, "x1"))
        meta = json.loads(p.with_suffix(".json").read_text())
        self.assertEqual(meta["files"], [name])
        self.assertEqual(os.fsencode(meta["files"][0]), raw)
        git(wt, "reset", "-q", "--hard", "HEAD")
        git(wt, "clean", "-qfd")
        res = asyncio.run(gitstore.restore_checkpoint(self.repo, "t1", wt))
        self.assertTrue(res["restored"], res)
        self.assertEqual((wt / name).read_text(), "latin1 name\n")

    def test_a_modified_tracked_non_ascii_path_is_listed(self):
        """The tracked half uses `diff --name-only`, which C-quotes too — and it
        is that listing which feeds meta["files"] and the list an operator
        reads. A trace-only fix would leave the record wrong."""
        wt = self.alloc("t1")
        (wt / "café.txt").write_text("base\n")
        git(wt, "add", "-A", "--", ".")
        git(wt, "commit", "-qm", "add accented file")
        (wt / "café.txt").write_text("base\nmodified\n")
        p = asyncio.run(gitstore.checkpoint(self.repo, "t1", wt, "x1"))
        meta = json.loads(p.with_suffix(".json").read_text())
        self.assertEqual(meta["files"], ["café.txt"])
        # The commit is checkpointed too, so a reset that keeps HEAD would
        # apply the patch onto a tree that already contains it and conflict.
        # alloc resets to the BASE — reproduce that, which is the real case.
        git(wt, "reset", "-q", "--hard", meta["merge_base"])
        res = asyncio.run(gitstore.restore_checkpoint(self.repo, "t1", wt))
        self.assertTrue(res["restored"], res)
        self.assertEqual((wt / "café.txt").read_text(), "base\nmodified\n")

    def test_a_file_git_cannot_read_is_skipped_not_faked(self):
        """The guard behind both: a per-file diff that produced no patch must
        never be recorded as captured. A path that cannot be read is the honest
        way to drive that branch."""
        wt = self.alloc("t1")
        (wt / "real.txt").write_text("kept\n")
        orig = gitstore._git_bytes

        async def flaky(args, cwd, check=True):
            if "--no-index" in args and args[-1] == "ghost.txt":
                return 1, b"", "error: could not access 'ghost.txt'"
            return await orig(args, cwd, check=check)

        gitstore._git_bytes = flaky
        self.addCleanup(setattr, gitstore, "_git_bytes", orig)
        (wt / "ghost.txt").write_text("here at listing time\n")
        p = asyncio.run(gitstore.checkpoint(self.repo, "t1", wt, "x1"))
        meta = json.loads(p.with_suffix(".json").read_text())
        self.assertNotIn("ghost.txt", meta["files"])
        self.assertIn("real.txt", meta["files"])

    def test_restore_after_a_reset_recovers_the_files(self):
        wt = self.alloc("t1")
        self.work(wt)
        saved = asyncio.run(gitstore.checkpoint(self.repo, "t1", wt, "x1"))
        with capture_events() as ev:
            wt2 = self.alloc("t1")          # resets task/t1 to base
        # alloc checkpointed the work it was about to discard...
        self.assertTrue(any(t == "task.branch_reset" for t, _ in ev.seen))
        self.assertGreater(len(gitstore.checkpoint_files(self.repo, "t1")), 1)
        self.assertFalse((wt2 / "committed.txt").exists())
        res = asyncio.run(gitstore.restore_checkpoint(self.repo, "t1", wt2))
        self.assertTrue(res["restored"], res)
        self.assertEqual((wt2 / "committed.txt").read_text(), "done\n")
        self.assertEqual((wt2 / "untracked.txt").read_text(), "new file\n")
        self.assertIn("return a + b + 0", (wt2 / "calc.py").read_text())
        self.assertTrue(saved.exists())
        # Restored into the FILES, not the index: `git apply --3way` implies
        # --index, so the implementer would otherwise inherit a pre-staged diff.
        self.assertEqual(git(wt2, "diff", "--cached", "--name-only").strip(), "")
        self.assertIn("calc.py", git(wt2, "status", "--porcelain"))

    def test_a_non_utf8_file_round_trips_byte_for_byte(self):
        """A patch is DATA, not text.

        `_git` decodes stdout with errors="replace", and the patch used to be
        re-encoded with encode("utf-8", "replace") — so every byte that is not
        valid UTF-8 was silently rewritten (0xE9 -> EF BF BD) and the restore
        still reported success. `--binary` does NOT save this file: git only
        classifies a blob as binary when it contains a NUL byte, and a latin-1
        source file has none. The capture reads and writes raw bytes.
        """
        wt = self.alloc("t1")
        latin = b"caf\xe9 na\xefve\n"        # 0xE9, no NUL: git calls it TEXT
        (wt / "latin.txt").write_bytes(latin)
        p = asyncio.run(gitstore.checkpoint(self.repo, "t1", wt, "x1"))
        raw = p.read_bytes()
        self.assertIn(b"\xe9", raw, "the patch must carry the original byte")
        self.assertNotIn(b"\xef\xbf\xbd", raw, "…not the U+FFFD replacement")

        wt2 = self.alloc("t1")
        res = asyncio.run(gitstore.restore_checkpoint(self.repo, "t1", wt2))
        self.assertTrue(res["restored"], res)
        self.assertEqual((wt2 / "latin.txt").read_bytes(), latin)

    def test_a_non_utf8_untracked_file_round_trips_too(self):
        """The untracked half is a separate `git diff --no-index` per file."""
        wt = self.alloc("t1")
        blob = b"\xff\xfe\x00\x01mixed\n"    # this one IS binary to git
        latin = b"na\xefve\n"
        (wt / "blob.bin").write_bytes(blob)
        (wt / "latin.txt").write_bytes(latin)
        asyncio.run(gitstore.checkpoint(self.repo, "t1", wt, "x1"))
        wt2 = self.alloc("t1")
        res = asyncio.run(gitstore.restore_checkpoint(self.repo, "t1", wt2))
        self.assertTrue(res["restored"], res)
        self.assertEqual((wt2 / "blob.bin").read_bytes(), blob)
        self.assertEqual((wt2 / "latin.txt").read_bytes(), latin)

    def test_a_conflicting_restore_is_reported_not_half_applied(self):
        wt = self.alloc("t1")
        self.work(wt)
        p = asyncio.run(gitstore.checkpoint(self.repo, "t1", wt, "x1"))
        # The base moves under the checkpoint: the same file now says something
        # else, so the patch cannot apply cleanly.
        git(wt, "reset", "-q", "--hard", "HEAD~1")
        (wt / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        git(wt, "commit", "-qam", "someone else changed it")
        (wt / "untracked.txt").unlink(missing_ok=True)
        with capture_events() as ev:
            res = asyncio.run(
                gitstore.restore_checkpoint(self.repo, "t1", wt, str(p)))
        self.assertFalse(res["restored"])
        self.assertEqual(res["conflicts"], ["calc.py"])
        # Nothing half-applied: no conflict markers, no partial new file, and
        # the tree is exactly what HEAD says it is.
        self.assertNotIn("<<<<<<<", (wt / "calc.py").read_text())
        self.assertEqual(git(wt, "status", "--porcelain").strip(), "")
        self.assertEqual([f for t, f in ev.seen
                          if t == "task.checkpoint_conflict"][0]["conflicts"],
                         ["calc.py"])

    def test_retention_keeps_only_the_newest_per_task(self):
        orig = config.CHECKPOINT_KEEP
        config.CHECKPOINT_KEEP = 3
        self.addCleanup(setattr, config, "CHECKPOINT_KEEP", orig)
        wt = self.alloc("t1")
        for i in range(5):
            (wt / f"f{i}.txt").write_text(f"attempt {i}\n")
            asyncio.run(gitstore.checkpoint(self.repo, "t1", wt, f"x{i}"))
        rows = gitstore.checkpoint_files(self.repo, "t1")
        self.assertEqual(len(rows), 3)
        # The newest survive, and no orphaned JSON is left behind.
        self.assertEqual([m["label"] for _, m in rows], ["x4", "x3", "x2"])
        jsons = list((self._cps / "proj" / "t1").glob("*.json"))
        self.assertEqual(len(jsons), 3)

    def test_a_clean_tree_checkpoints_nothing(self):
        wt = self.alloc("t1")
        self.assertIsNone(
            asyncio.run(gitstore.checkpoint(self.repo, "t1", wt, "x1")))
        self.assertEqual(gitstore.checkpoint_files(self.repo, "t1"), [])

    def test_the_cancel_sweep_runs_synchronously(self):
        """The run's finally block calls this with no event loop left."""
        wt = self.alloc("t1")
        (wt / "half-written.txt").write_text("attempt was cut off\n")
        # No asyncio.run here on purpose: the caller is synchronous.
        n = gitstore.checkpoint_worktrees(self.repo, ["t1", "no-such-task"])
        self.assertEqual(n, 1)
        rows = gitstore.checkpoint_files(self.repo, "t1")
        self.assertEqual([m["label"] for _, m in rows], ["interrupted"])
        self.assertIn("half-written.txt", rows[0][1]["files"])

    def test_the_sweep_accepts_store_row_dicts(self):
        """What main.py actually has in hand at a cancel is
        store.running_code_tasks(...) — a list of ROW DICTS, not ids. The sweep
        used to do str(t) on whatever it was given, so each lookup became a
        directory named "{'id': 't1', …}": every task missed, nothing was saved,
        and because a missing worktree is an ordinary skip there was not even an
        error. Both forms must work, and a genuine miss must be reported."""
        wt = self.alloc("t1")
        (wt / "still-here.txt").write_text("interrupted mid-write\n")
        rows = [{"id": "t1", "taskfile": "tf.json", "model": "m", "status": "running"},
                {"id": "gone", "status": "running"}]
        with capture_events() as ev:
            n = asyncio.run(gitstore.checkpoint_stopping(self.repo, rows))
        self.assertEqual(n, 1, "a row dict must resolve to its task id")
        saved = gitstore.checkpoint_files(self.repo, "t1")
        self.assertIn("still-here.txt", saved[0][1]["files"])
        # And the miss is visible rather than silent.
        skipped = [f for t, f in ev.seen if t == "task.checkpoint_skipped"]
        self.assertEqual([f["task"] for f in skipped], ["gone"])
        # The id form still works too.
        self.assertEqual(asyncio.run(
            gitstore.checkpoint_stopping(self.repo, ["no-such-task"])), 0)


    def test_the_sweep_awaits_on_a_live_loop(self):
        """main.py's finally is INSIDE async def run(): asyncio.run there raises
        RuntimeError, which the caller's except would swallow into a log line —
        so a cancel wrote no checkpoint at all. checkpoint_stopping is the
        awaited form, and checkpoint_worktrees must refuse, not lie."""
        wt = self.alloc("t1")
        (wt / "stopped.txt").write_text("cut off\n")

        async def sweep():
            n = await gitstore.checkpoint_stopping(self.repo, ["t1"])
            # The sync wrapper is a last resort, not something a live loop may
            # call: failing loudly beats silently saving nothing.
            with self.assertRaises(RuntimeError):
                gitstore.checkpoint_worktrees(self.repo, ["t1"])
            return n

        self.assertEqual(asyncio.run(sweep()), 1)
        self.assertIn("stopped.txt",
                      gitstore.checkpoint_files(self.repo, "t1")[0][1]["files"])

    def test_a_checkpoint_leaves_the_index_and_worktree_untouched(self):
        """A drain sweeps while sibling agents are still implementing: `git add
        -N` would race index.lock and leave a staged copy of work the agent
        never staged. The capture is read-only instead."""
        wt = self.alloc("t1")
        (wt / "calc.py").write_text("def add(a, b):\n    return a + b + 9\n")
        (wt / "untracked.txt").write_text("brand new\n")
        before_status = git(wt, "status", "--porcelain")
        before_index = git(wt, "ls-files", "-s")
        p = asyncio.run(gitstore.checkpoint(self.repo, "t1", wt, "x1"))
        self.assertTrue(p.exists())
        self.assertIn("untracked.txt", p.read_text())
        self.assertEqual(git(wt, "status", "--porcelain"), before_status)
        self.assertEqual(git(wt, "ls-files", "-s"), before_index)

    def test_newest_wins_even_when_two_checkpoints_share_a_second(self):
        """The stamp used to be whole seconds, so two checkpoints in the same
        second sorted on their LABEL — "…-interrupted-1" before "…-interrupted",
        "pre-reset" before "x1". checkpoint_files reads newest-first and
        _prune_checkpoints drops the front, so a resume restored an OLDER patch
        than the one just written and could prune the newest away."""
        wt = self.alloc("t1")
        labels = ["pre-reset", "interrupted", "interrupted", "x1"]
        for i, label in enumerate(labels):
            (wt / "calc.py").write_text(f"def add(a, b):\n    return a + b + {i}\n")
            asyncio.run(gitstore.checkpoint(self.repo, "t1", wt, label))
        rows = gitstore.checkpoint_files(self.repo, "t1")
        self.assertEqual(len(rows), len(labels))
        # Newest first: the LAST written is the one restore_checkpoint applies.
        self.assertEqual(rows[0][1]["files"], ["calc.py"])
        self.assertIn("a + b + 3", rows[0][0].read_text())
        # And the order is the write order, whatever the labels were.
        by_mtime = sorted(rows, key=lambda r: r[0].stat().st_mtime_ns)
        self.assertIn("a + b + 3", by_mtime[-1][0].read_text())

    def test_retention_keeps_the_newest_when_stamps_collide(self):
        orig = config.CHECKPOINT_KEEP
        config.CHECKPOINT_KEEP = 2
        self.addCleanup(setattr, config, "CHECKPOINT_KEEP", orig)
        wt = self.alloc("t1")
        for i in range(4):
            (wt / "calc.py").write_text(f"def add(a, b):\n    return a + b + {i}\n")
            # Labels chosen so a NAME sort would keep the wrong pair.
            asyncio.run(gitstore.checkpoint(self.repo, "t1", wt,
                                            ["interrupted-1", "pre-reset"][i % 2]))
        rows = gitstore.checkpoint_files(self.repo, "t1")
        self.assertEqual(len(rows), 2)
        self.assertIn("a + b + 3", rows[0][0].read_text())
        self.assertIn("a + b + 2", rows[1][0].read_text())


    def test_never_raises_on_a_worktree_that_is_gone(self):
        self.assertIsNone(asyncio.run(
            gitstore.checkpoint(self.repo, "t1", Path("/nonexistent/wt"), "x1")))
        res = asyncio.run(gitstore.restore_checkpoint(
            self.repo, "t1", self.alloc("t1")))
        self.assertFalse(res["restored"])
        self.assertIn("no checkpoint", res["reason"])


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

    def test_untracked_copies_of_incoming_files_do_not_block_the_fast_forward(self):
        """The prison-escape failure.

        cell-wing-foundation merged on GitHub, which committed the .uid files
        Godot had already written into the blessed clone as untracked. The
        fast-forward aborted, local main stayed on the scaffold, and the
        three dependents branched without the foundation they were written
        against.
        """
        (self.other / "generated.uid").write_text("from-origin\n")
        self._git("add", "-A", cwd=self.other)
        self._git("commit", "-qm", "uid", cwd=self.other)
        self._git("push", "-q", "origin", "development", cwd=self.other)
        (self.repo / "generated.uid").write_text("local-godot\n")
        ok, note = asyncio.run(gitstore.fast_forward_base(self.repo, "development"))
        self.assertTrue(ok, note)
        self.assertEqual((self.repo / "generated.uid").read_text(), "from-origin\n")
        self.assertEqual((self.repo / "f.txt").read_text(), "one\ntwo\n")
        self.assertEqual(self._dirty(), "")

    def test_a_tracked_local_edit_is_not_discarded_to_fast_forward(self):
        (self.repo / "f.txt").write_text("local edit\n")
        ok, note = asyncio.run(gitstore.fast_forward_base(self.repo, "development"))
        self.assertFalse(ok, note)
        self.assertEqual((self.repo / "f.txt").read_text(), "local edit\n")


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


class EnsureRemote(unittest.TestCase):
    """Remote creation is gh-backed but never raises: a missing remote is the
    one refusal gh can cure on its own (the 09-12 minecraft-test run lost
    eight minutes of model work to it), so it must fail softly everywhere."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.repo = Path(self._dir.name) / "proj"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")

    def test_gh_absent_reports_a_reason_without_raising(self):
        # No gh on PATH: a plain reason, not an exception — the dashboard
        # shows it as a note and `code run` quotes it in its refusal.
        with mock.patch("shutil.which", return_value=None):
            ok, why = asyncio.run(gitstore.ensure_remote(self.repo))
        self.assertFalse(ok)
        self.assertIn("gh", why)

    def test_existing_origin_is_returned_untouched(self):
        # An origin that is already there wins before gh is ever consulted:
        # patching gh away proves it was never needed.
        git(self.repo, "remote", "add", "origin",
            "https://github.com/owner/proj.git")
        with mock.patch("shutil.which", return_value=None):
            ok, url = asyncio.run(gitstore.ensure_remote(self.repo))
        self.assertTrue(ok)
        self.assertEqual(url, "https://github.com/owner/proj.git")


if __name__ == "__main__":
    unittest.main()


class GitHubQuota(unittest.TestCase):
    """A spent GitHub quota waits or goes through REST; it never fails a task."""

    SPENT = "GraphQL: API rate limit already exceeded for user ID 64327054."

    def run_gh(self, replies, fn):
        calls = []

        async def fake_raw(args, cwd, timeout=180):
            calls.append(list(args))
            if args[:2] == ["api", "rate_limit"]:
                return 0, '{"resources": {"graphql": {"remaining": 0, "reset": 0}}}', ""
            return replies.pop(0)

        async def no_sleep(_s):
            return None
        with mock.patch.object(gitstore, "_gh_raw", fake_raw), \
                mock.patch.object(gitstore.asyncio, "sleep", no_sleep), \
                capture_events() as evs:
            result = asyncio.run(fn())
        return result, calls, evs

    def test_the_live_refusal_text_is_a_quota_refusal(self):
        self.assertTrue(gitstore.is_rate_limited(self.SPENT))
        self.assertFalse(gitstore.is_rate_limited("GraphQL: No commits between main and task/x"))

    def test_a_quota_refusal_waits_for_the_reset_and_retries(self):
        (rc, out, _), calls, evs = self.run_gh(
            [(1, "", self.SPENT), (0, "ok", "")],
            lambda: gitstore._gh(["pr", "view", "3"], cwd="."))
        self.assertEqual((rc, out), (0, "ok"))
        self.assertEqual([c for c in calls if c[0] == "pr"], [["pr", "view", "3"]] * 2)
        self.assertIn("git.quota_wait", [t for t, _ in evs.seen])

    def test_other_refusals_are_not_retried(self):
        (rc, _, _), calls, evs = self.run_gh(
            [(1, "", "HTTP 401: Bad credentials")],
            lambda: gitstore._gh(["pr", "view", "3"], cwd="."))
        self.assertEqual(rc, 1)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("git.quota_wait", [t for t, _ in evs.seen])

    def test_the_wait_is_bounded(self):
        with mock.patch.object(config, "GH_QUOTA_MAX_WAIT", 100):
            (rc, _, _), calls, _ = self.run_gh(
                [(1, "", self.SPENT)] * 50,
                lambda: gitstore._gh(["pr", "view", "3"], cwd="."))
        self.assertEqual(rc, 1)
        self.assertLess(len([c for c in calls if c[0] == "pr"]), 50)

    def test_open_pr_goes_through_rest_when_graphql_is_spent(self):
        replies = [
            (1, "", self.SPENT),              # gh pr list (GraphQL)
            (0, "[]", ""),                    # REST: no open PR yet
            (1, "", self.SPENT),              # gh pr create (GraphQL)
            (0, "https://github.com/o/r/pull/77\n", ""),  # REST create
        ]
        (number, url, note), calls, evs = self.run_gh(
            replies, lambda: gitstore.open_pr(".", "t1", "task(t1): x", "body", base="main"))
        self.assertEqual((number, note), (77, "opened"))
        self.assertEqual(calls[-1][:2], ["api", "repos/{owner}/{repo}/pulls"])
        self.assertIn("head=task/t1", calls[-1])
        self.assertIn("git.rest_fallback", [t for t, _ in evs.seen])
        self.assertNotIn(["api", "rate_limit"], calls, "REST path must not wait first")
