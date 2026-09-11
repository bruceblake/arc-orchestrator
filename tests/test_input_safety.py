"""Adversarial pass over input that becomes a path, git ref, or filename."""
import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import capture_events

import code_tasks
import config
import dashboard
import gitstore
import reconcile


def taskfile(tasks, repo="/tmp"):
    doc = {"project": {"repo": repo, "title": "t", "tasks": tasks}}
    fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(doc, fh)
    fh.close()
    return fh.name


BASIC = {"id": "t1", "title": "T1", "prompt": "do it",
         "model": "gpt-oss-120b", "reviewer": "kimi"}


def rejected(result):
    obj, code = result
    return ("error" in obj) and code in (400, 404)


class InputSafetyDashboardFilenames(unittest.TestCase):
    """The dashboard serves files by operator-supplied names; every regex
    gate must reject traversal instead of resolving it, or any phone on the
    LAN can read files outside logs/ and ~/tasks."""

    def test_transcript_rejects_dotdot(self):
        self.assertTrue(rejected(dashboard._transcript_tail("../events.jsonl", 10)))

    def test_transcript_rejects_absolute_path(self):
        self.assertTrue(rejected(dashboard._transcript_tail("/etc/passwd", 10)))

    def test_transcript_rejects_url_encoded_traversal(self):
        self.assertTrue(rejected(dashboard._transcript_tail("..%2fevents.jsonl", 10)))

    def test_transcript_rejects_null_byte(self):
        self.assertTrue(rejected(dashboard._transcript_tail("a\x00.jsonl", 10)))

    def test_transcript_very_long_name_never_resolves(self):
        self.assertTrue(rejected(dashboard._transcript_tail("a" * 5000 + ".jsonl", 10)))

    def test_project_detail_rejects_dotdot(self):
        # Same gate pattern guards ~/<TASKS_DIR> filenames.
        self.assertTrue(rejected(dashboard._project_detail(None, "../x.json")))


class InputSafetyTaskfileIds(unittest.TestCase):
    """A task id becomes a git branch, a worktree path, and a transcript
    filename. An id that escapes this shape can write outside the worktree
    root or corrupt the ref namespace."""

    def test_valid_id_still_loads(self):
        ts = code_tasks.load_taskfile(taskfile([BASIC]))
        self.assertIn("t1", ts["tasks"])

    def test_rejects_dotdot_id(self):
        with self.assertRaises(ValueError):
            code_tasks.load_taskfile(taskfile([{**BASIC, "id": "../escape"}]))

    def test_rejects_shell_metachar_id(self):
        with self.assertRaises(ValueError):
            code_tasks.load_taskfile(taskfile([{**BASIC, "id": "t1;rm -rf /"}]))

    def test_rejects_newline_id(self):
        with self.assertRaises(ValueError):
            code_tasks.load_taskfile(taskfile([{**BASIC, "id": "t1\nrm"}]))

    def test_rejects_encoded_traversal_id(self):
        with self.assertRaises(ValueError):
            code_tasks.load_taskfile(taskfile([{**BASIC, "id": "..%2fetc"}]))

    def test_rejects_leading_dash_id(self):
        # A `task/<id>` argv that begins with '-' can be parsed as a git flag.
        with self.assertRaises(ValueError):
            code_tasks.load_taskfile(taskfile([{**BASIC, "id": "-x"}]))


class InputSafetyGitFlagInjection(unittest.TestCase):
    """_task_deliverable feeds a task id into git argv; a crafted id must
    never become an option or change what's read."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arc-git-")
        r = subprocess.run(["git", "init", "-q", self.tmp], capture_output=True)
        if r.returncode != 0:
            self.skipTest("git unavailable")

    def test_dotdot_id_is_rejected(self):
        out = dashboard._task_deliverable(Path(self.tmp), "../x")
        self.assertIn("error", out)

    def test_newline_id_is_rejected(self):
        out = dashboard._task_deliverable(Path(self.tmp), "t\n--all")
        self.assertIn("error", out)

    def test_leading_dash_id_cannot_be_a_flag(self):
        pwn = Path(self.tmp).parent / "arc-pwn-flag"
        out = dashboard._task_deliverable(Path(self.tmp), "-x")
        self.assertFalse(out.get("found"))
        self.assertFalse(pwn.exists())


class InputSafetyWorktreeContainment(unittest.TestCase):
    """alloc() and reconcile place directories on disk from task ids; a
    hostile id must never put a worktree outside config.WORKTREE_ROOT."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arc-wt-")
        prev = config.WORKTREE_ROOT
        config.WORKTREE_ROOT = self.tmp
        self.addCleanup(setattr, config, "WORKTREE_ROOT", prev)

    def test_alloc_refuses_traversing_id_and_creates_nothing(self):
        repo = Path(self.tmp) / "repo"
        with capture_events() as seen:
            with self.assertRaises(ValueError):
                asyncio.run(gitstore.alloc(repo, "../escape"))
        self.assertEqual(seen.of("worktree.alloc"), [])  # refused, not half-run
        self.assertFalse((Path(self.tmp) / "escape").exists())

    def test_find_repo_refuses_traversal_names(self):
        for bad in ("", "..", "../../etc", str(Path(self.tmp)), "a/b"):
            self.assertIsNone(reconcile._find_repo(bad))

    def test_find_repo_rejects_name_that_traverses_to_a_real_repo(self):
        # Without the guard, ~/repos/../../target resolves to a real clone.
        home = Path(self.tmp) / "home"
        (home / "repos").mkdir(parents=True)
        (Path(self.tmp) / "target" / ".git").mkdir(parents=True)
        with mock.patch.object(reconcile.Path, "home", return_value=home):
            self.assertIsNone(reconcile._find_repo("../../target"))


if __name__ == "__main__":
    unittest.main()
