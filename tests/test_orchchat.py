"""Tests for the conversational planning engine (orchchat.py).

The Kimi-K3 driver is replaced with an in-process stub, so these tests
make no network calls and touch no real harness. Session and taskfile
paths are redirected to a process-private temp area set up before config
is imported (the helpers import writes that redirect into config).
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="arc-tests-orchchat-")
os.environ["ARC_CHAT_DIR"] = os.path.join(_TMP, "chat")
os.environ["ARC_TASKS_DIR"] = os.path.join(_TMP, "tasks")

from helpers import capture_events  # noqa: F401,E402  (env/DB redirect)
from helpers import ENTRY, STRONGEST, STRONGEST_FAMILY  # noqa: E402,F401
import config  # noqa: E402
import orchchat  # noqa: E402
from drivers import DriverResult  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _planner_cls(reply="", error=None):
    """A KimiDriver stand-in that records its call and returns a canned reply."""
    state = {"calls": 0, "prompt": None, "worktree": None, "task_id": None}

    class _Stub:
        def __init__(self, role, bench=False, interactive=False):
            assert role == "planner", "chat must construct the planner role"
            # Chat has a human waiting, so it must ask for the interactive slot.
            assert interactive, "chat planning must be interactive"
            state["role"] = role
            state["interactive"] = interactive

        async def run(self, prompt, worktree, session_id=None, task_id=None):
            state["calls"] += 1
            state["prompt"] = prompt
            state["worktree"] = worktree
            state["task_id"] = task_id
            if error is not None:
                raise error
            return DriverResult(harness="kimi", model=STRONGEST,
                                role="planner", exit_code=0, text=reply)

    return _Stub, state


class TestOrchChat(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="orchchat-t-")
        self.chat_dir = Path(self._dir.name) / "chat"
        self.tasks_dir = Path(self._dir.name) / "tasks"
        self.chat_dir.mkdir()
        self.tasks_dir.mkdir()
        self._old_chat = os.environ.get("ARC_CHAT_DIR")
        self._old_tasks = config.TASKS_DIR
        os.environ["ARC_CHAT_DIR"] = str(self.chat_dir)
        config.TASKS_DIR = str(self.tasks_dir)
        self._restore_paths = lambda: (
            self._setenv("ARC_CHAT_DIR", self._old_chat),
            setattr(config, "TASKS_DIR", self._old_tasks))
        self.addCleanup(self._restore_paths)
        self.addCleanup(self._dir.cleanup)

    def _setenv(self, key, value):
        # per-test ARC_CHAT_DIR restore (None removes it)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    def _make_repo(self):
        # under /home/proxyie with a plain .git dir: _repo_problem checks
        # only prefix + directory + .git existence, so no git init needed
        repo = Path(tempfile.mkdtemp(prefix="arc-qa-chat-", dir="/home/proxyie"))
        (repo / ".git").mkdir()
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        return repo

    def _user_turns(self, *texts):
        return [{"role": "user", "ts": 1700000000.0 + i, "text": t}
                for i, t in enumerate(texts)]

    def _write_turns(self, session, turns):
        spath = self.chat_dir / f"{session}.jsonl"
        with open(spath, "w", encoding="utf-8") as f:
            for t in turns:
                f.write(json.dumps(t) + "\n")
        return spath

    def _run(self, reply="", error=None, session="sess-1", repo=None,
             turns=None):
        if turns is None:
            turns = self._user_turns("please build the thing")
        self._write_turns(session, turns)
        cls, state = _planner_cls(reply, error)
        # orchchat._planner_driver() chooses the class from the roster (Kimi
        # today, GLM once Kimi is withdrawn), so patch the FACTORY — patching
        # KimiDriver alone stopped covering the path the day the roster moved.
        with mock.patch.object(orchchat, "_planner_driver",
                               lambda: cls("planner", interactive=True)):
            code = asyncio.run(orchchat.run_turn(session, str(repo)))
        return code, state, self.chat_dir / f"{session}.jsonl"

    def _read_turns(self, spath):
        return [json.loads(line) for line in
                spath.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _taskfiles(self):
        return sorted(p.name for p in self.tasks_dir.iterdir())

    @staticmethod
    def _plan(title="Build A Widget", repo="/home/proxyie/r",
              model=ENTRY, reviewer="glm", tid="make-widget"):
        return {
            "project": {"repo": repo, "title": title,
                        "tasks": [{"id": tid, "title": title,
                                   "prompt": "Build the widget.",
                                   "model": model, "reviewer": reviewer,
                                   "verify_cmd": "./check.sh",
                                   "files_hint": ["widget.py"]}]}}
        # (dict returned; caller json-dumps inside a fenced block)

    @staticmethod
    def _reply(taskfile):
        return ("Here is the plan.\n\n```taskfile\n"
                + json.dumps(taskfile) + "\n```\n")


class TestOrchChatHappy(TestOrchChat):
    def test_valid_taskfile_written(self):
        repo = self._make_repo()
        code, state, spath = self._run(
            reply=self._reply(self._plan(repo=str(repo))), repo=repo)
        self.assertEqual(code, 0)
        self.assertEqual(state["calls"], 1)
        self.assertEqual(self._taskfiles(), ["build-a-widget.json"])
        turns = self._read_turns(spath)
        self.assertEqual(len(turns), 2)
        last = turns[-1]
        self.assertEqual(last["role"], "assistant")
        self.assertEqual(last["taskfile"], "build-a-widget.json")
        self.assertNotIn("error", last)

    def test_cli_argument_repo_overrides_model_repo(self):
        repo = self._make_repo()
        plan = self._plan(repo="/home/proxyie/not-this-one")
        code, state, spath = self._run(reply=self._reply(plan), repo=repo)
        self.assertEqual(code, 0)
        self.assertEqual(self._taskfiles(), ["build-a-widget.json"])
        written = json.loads((self.tasks_dir / "build-a-widget.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(written["project"]["repo"], str(repo))

    def test_last_taskfile_block_wins(self):
        repo = self._make_repo()
        reply = (self._reply(self._plan(title="First Plan", tid="first-1",
                                        repo=str(repo)))
                 + "\n" + self._reply(self._plan(title="Second Plan",
                                                 tid="second-1",
                                                 repo=str(repo))))
        code, state, spath = self._run(reply=reply, repo=repo)
        self.assertEqual(code, 0)
        self.assertEqual(self._taskfiles(), ["second-plan.json"])

    def test_filename_collision_gets_suffix(self):
        repo = self._make_repo()
        (self.tasks_dir / "build-a-widget.json").write_text("{}")
        code, state, spath = self._run(
            reply=self._reply(self._plan(repo=str(repo))), repo=repo)
        self.assertEqual(code, 0)
        self.assertIn("build-a-widget-2.json", self._taskfiles())

    def test_prompt_contains_persona_repo_and_history(self):
        repo = self._make_repo()
        turns = self._user_turns("first turn", "second turn")
        code, state, spath = self._run(reply="ok", repo=repo, turns=turns)
        self.assertEqual(code, 0)
        prompt = state["prompt"]
        self.assertIn("planning orchestrator", prompt)
        self.assertIn(str(repo), prompt)
        self.assertIn("first turn", prompt)
        self.assertIn("second turn", prompt)
        self.assertEqual(state["worktree"], repo)
        self.assertEqual(state["task_id"], "chat-sess-1")

    def test_history_truncates_oldest(self):
        repo = self._make_repo()
        turns = self._user_turns(*[f"old-{i} " + "x" * 4000 for i in range(9)])
        turns.append({"role": "user", "ts": 1.0, "text": "the newest turn"})
        code, state, spath = self._run(reply="ok", repo=repo, turns=turns)
        self.assertEqual(code, 0)
        prompt = state["prompt"]
        self.assertIn("the newest turn", prompt)
        self.assertIn("old-8", prompt)
        self.assertNotIn("old-0 ", prompt)
        self.assertNotIn("old-1 ", prompt)
        self.assertLess(len(prompt),
                        len(orchchat.PLANNER_PERSONA) + 31000)


class TestOrchChatRejections(TestOrchChat):
    def test_same_harness_review_pairing_rejected(self):
        repo = self._make_repo()
        plan = self._plan(repo=str(repo), model=STRONGEST, reviewer=STRONGEST_FAMILY)
        code, state, spath = self._run(reply=self._reply(plan), repo=repo)
        self.assertEqual(code, 0)
        self.assertEqual(self._taskfiles(), [])
        last = self._read_turns(spath)[-1]
        self.assertIn("reviewer", last["error"])
        self.assertNotIn("taskfile", last)

    def test_unknown_model_rejected(self):
        repo = self._make_repo()
        plan = self._plan(repo=str(repo), model="GPT-4o")
        code, state, spath = self._run(reply=self._reply(plan), repo=repo)
        self.assertEqual(code, 0)
        self.assertEqual(self._taskfiles(), [])
        last = self._read_turns(spath)[-1]
        self.assertIn("GPT-4o", last["error"])

    def test_bad_json_block_rejected(self):
        repo = self._make_repo()
        reply = "```taskfile\n{not json at all\n```"
        code, state, spath = self._run(reply=reply, repo=repo)
        self.assertEqual(code, 0)
        self.assertEqual(self._taskfiles(), [])
        last = self._read_turns(spath)[-1]
        self.assertIn("JSON", last["error"])


class TestOrchChatFailures(TestOrchChat):
    def test_driver_failure_appends_turn_exit_zero(self):
        repo = self._make_repo()
        code, state, spath = self._run(
            error=RuntimeError("kimi exploded"), repo=repo)
        self.assertEqual(code, 0)
        self.assertEqual(state["calls"], 1)
        self.assertEqual(self._taskfiles(), [])
        last = self._read_turns(spath)[-1]
        self.assertEqual(last["role"], "assistant")
        self.assertIn("planner failed", last["text"])
        self.assertIn("kimi exploded", last["error"])

    def test_empty_reply_records_error(self):
        repo = self._make_repo()
        code, state, spath = self._run(reply="   ", repo=repo)
        self.assertEqual(code, 0)
        last = self._read_turns(spath)[-1]
        self.assertIn("empty reply", last["error"])

    def test_bad_repo_prefix_rejected_without_calling_driver(self):
        code, state, spath = self._run(repo="/tmp/not-ours")
        self.assertEqual(code, 0)
        self.assertEqual(state["calls"], 0)
        last = self._read_turns(spath)[-1]
        self.assertIn("/home/proxyie", last["error"])
        self.assertNotIn("taskfile", last)

    def test_non_git_repo_rejected(self):
        repo = Path(tempfile.mkdtemp(prefix="arc-qa-chat-", dir="/home/proxyie"))
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        code, state, spath = self._run(repo=repo)
        self.assertEqual(code, 0)
        self.assertEqual(state["calls"], 0)
        last = self._read_turns(spath)[-1]
        self.assertIn("git", last["error"])


class TestOrchChatSessionIO(unittest.TestCase):
    def test_invalid_session_ids_rejected(self):
        repo = Path("/home/proxyie/anything")
        for bad in ("Bad_Session", "../escape", "", "a" * 41):
            with self.subTest(bad=bad):
                code = asyncio.run(orchchat.run_turn(bad, str(repo)))
                self.assertEqual(code, 1)

    def test_unreadable_session_returns_one(self):
        blocked = Path(tempfile.mkdtemp(prefix="orchchat-f-")) / "not-a-dir"
        blocked.write_text("x")
        old = os.environ.get("ARC_CHAT_DIR")
        os.environ["ARC_CHAT_DIR"] = str(blocked)
        try:
            code = asyncio.run(orchchat.run_turn("sess-1",
                                                 "/home/proxyie/anything"))
            self.assertEqual(code, 1)
        finally:
            if old is None:
                os.environ.pop("ARC_CHAT_DIR", None)
            else:
                os.environ["ARC_CHAT_DIR"] = old
            shutil.rmtree(blocked.parent, ignore_errors=True)

    def test_session_roundtrip(self):
        with tempfile.TemporaryDirectory(prefix="orchchat-io-") as d:
            os.environ["ARC_CHAT_DIR"] = d
            try:
                spath = Path(d) / "s.jsonl"
                turn = {"role": "user", "ts": 1.5, "text": "hi"}
                orchchat.append_turn(spath, turn)
                orchchat.append_turn(spath, {"role": "assistant",
                                             "ts": 1.6, "text": "hello"})
                self.assertEqual(orchchat.load_session(spath),
                                 [turn, {"role": "assistant", "ts": 1.6,
                                         "text": "hello"}])
            finally:
                os.environ.pop("ARC_CHAT_DIR", None)


class TestOrchChatReplyText(unittest.TestCase):
    def test_reads_full_transcript_not_capped_text(self):
        with tempfile.TemporaryDirectory(prefix="orchchat-tr-") as d:
            tpath = Path(d) / "t.jsonl"
            big = "y" * 6000
            with open(tpath, "w", encoding="utf-8") as f:
                f.write(json.dumps({"role": "user", "content": "prompt"}) + "\n")
                f.write(json.dumps({"role": "assistant", "content": big}) + "\n")
            res = DriverResult(harness="kimi", model=STRONGEST, role="planner",
                               exit_code=0, text=big[-3000:],
                               transcript_path=str(tpath))
            self.assertEqual(orchchat._reply_text(res), big)

    def test_falls_back_to_capped_text(self):
        res = DriverResult(harness="kimi", model=STRONGEST, role="planner",
                           exit_code=0, text="short")
        self.assertEqual(orchchat._reply_text(res), "short")


class TestOrchChatCLI(unittest.TestCase):
    def test_help_smoke(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "main.py"), "chat", "--help"],
            capture_output=True, text=True, cwd=str(ROOT), timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--session", proc.stdout)
        self.assertIn("--repo", proc.stdout)


if __name__ == "__main__":
    unittest.main()