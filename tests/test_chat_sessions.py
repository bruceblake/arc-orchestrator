"""HTTP contract tests for GET /api/chat/sessions, and per-session isolation.

The chat UI used to be locked to the one session its repo implies. The
dashboard can show every session it can read, so it grew a listing route:
GET /api/chat/sessions returns {sessions: [{name, turns, mtime}]} newest
first, skipping unreadable and corrupt files — a half-written jsonl or a
stale directory entry must never turn the picker into a 500. The listing is
read-only and takes NO parameters, so unlike /api/chat/start it cannot widen
the unauthenticated surface documented in Rule 6b.

Driven socket-less, like test_dashboard_chat.py: a fake Handler captures
status/body in memory and _spawn_logged is patched, so no model is ever
called and no `main.py chat` process ever starts.
"""
import time

from helpers import capture_events  # noqa: F401  (sys.path + event/DB redirect)
import json  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest import mock  # noqa: E402

import config  # noqa: E402
import dashboard  # noqa: E402
import orchchat  # noqa: E402


class _FakeRequest(dashboard.Handler):
    """A socket-less Handler that captures status/headers/body in memory."""

    def __init__(self, path, body=b""):
        self.status = None
        self.response_headers = {}
        self.body = b""
        self.wfile = self
        self.rfile = self
        self.path = path
        self._pending = body
        self.headers = {"Content-Length": str(len(body)),
                        "Content-Type": "application/json"}

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass

    def read(self, n):
        data, self._pending = self._pending[:n], self._pending[n:]
        return data

    def write(self, data):
        self.body += data


class ChatSessions(unittest.TestCase):
    """Per-test temp chat dir, cleared registry, stubbed spawn."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)
        self.chat_dir = self.tmp / "chat"
        self.chat_dir.mkdir(parents=True)
        self._env = {k: os.environ.get(k)
                     for k in ("ARC_CHAT_DIR", "ARC_REPOS_DIR")}
        os.environ["ARC_CHAT_DIR"] = str(self.chat_dir)
        os.environ["ARC_REPOS_DIR"] = str(self.tmp / "repos")
        self._orig_registry = dict(dashboard._launch_registry)
        dashboard._launch_registry.clear()
        self.spawn_calls = []
        self._spawn_patch = mock.patch.object(
            dashboard, "_spawn_logged", side_effect=self._fake_spawn)
        self._spawn_patch.start()

    def tearDown(self):
        self._spawn_patch.stop()
        dashboard._launch_registry.clear()
        dashboard._launch_registry.update(self._orig_registry)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._dir.cleanup()

    def _fake_spawn(self, argv, log_name):
        self.spawn_calls.append((list(argv), log_name))
        proc = mock.Mock(pid=4242)
        proc.wait.side_effect = subprocess.TimeoutExpired(argv, 1.5)
        return proc, log_name

    def _get(self, path):
        req = _FakeRequest(path)
        req.do_GET()
        return req.status, json.loads(req.body.decode("utf-8"))

    def _post_json(self, path, obj):
        req = _FakeRequest(path, json.dumps(obj).encode("utf-8"))
        req.do_POST()
        return req.status, json.loads(req.body.decode("utf-8"))

    def _session(self, name, turns, mtime=None):
        """Write a session file with `turns` user turns; return its path."""
        path = self.chat_dir / (name + ".jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for i in range(turns):
                f.write(json.dumps({"role": "user", "ts": 1750000000 + i,
                                    "text": f"turn {i}"}) + "\n")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    # ---- the listing -------------------------------------------------------

    def test_empty_dir_lists_nothing_and_does_not_500(self):
        status, resp = self._get("/api/chat/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(resp, {"sessions": []})

    def test_missing_dir_lists_nothing_and_does_not_500(self):
        os.environ["ARC_CHAT_DIR"] = str(self.tmp / "never-created")
        status, resp = self._get("/api/chat/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(resp["sessions"], [])

    def test_several_sessions_newest_first_with_turn_counts(self):
        now = time.time()
        self._session("plan-alpha", 2, mtime=now - 300)
        self._session("plan-beta", 1, mtime=now - 10)
        self._session("plan-gamma", 3, mtime=now - 100)
        status, resp = self._get("/api/chat/sessions")
        self.assertEqual(status, 200)
        self.assertEqual([s["name"] for s in resp["sessions"]],
                         ["plan-beta", "plan-gamma", "plan-alpha"])
        by_name = {s["name"]: s for s in resp["sessions"]}
        self.assertEqual([by_name[n]["turns"] for n in
                          ("plan-alpha", "plan-beta", "plan-gamma")], [2, 1, 3])
        for s in resp["sessions"]:
            self.assertEqual(set(s), {"name", "turns", "mtime"})
            self.assertIsInstance(s["turns"], int)
            self.assertIsInstance(s["mtime"], (int, float))

    def test_corrupt_file_is_skipped_not_fatal(self):
        self._session("plan-good", 1)
        (self.chat_dir / "plan-junk.jsonl").write_bytes(b"\x00not json at all{{{")
        (self.chat_dir / "plan-half.jsonl").write_text('{"role": "user"',
                                                      encoding="utf-8")
        status, resp = self._get("/api/chat/sessions")
        self.assertEqual(status, 200)
        names = [s["name"] for s in resp["sessions"]]
        self.assertEqual(names, ["plan-good"])
        # and the good session is still fully described
        self.assertEqual(resp["sessions"][0]["turns"], 1)

    def test_partly_corrupt_file_keeps_its_readable_turns(self):
        path = self.chat_dir / "plan-mixed.jsonl"
        path.write_text(json.dumps({"role": "user", "ts": 1, "text": "ok"})
                        + "\n{ not json\n"
                        + json.dumps({"role": "assistant", "ts": 2, "text": "hi"})
                        + "\n", encoding="utf-8")
        status, resp = self._get("/api/chat/sessions")
        self.assertEqual(status, 200)
        self.assertEqual([(s["name"], s["turns"]) for s in resp["sessions"]],
                         [("plan-mixed", 2)])

    def test_non_session_files_and_dirs_are_ignored(self):
        self._session("plan-keep", 1)
        (self.chat_dir / "notes.txt").write_text("hello", encoding="utf-8")
        (self.chat_dir / "plan-keep.jsonl.bak").write_text("{}", encoding="utf-8")
        (self.chat_dir / "Plan-Upper.jsonl").write_text("{}", encoding="utf-8")
        (self.chat_dir / "plan-dir.jsonl").mkdir()
        status, resp = self._get("/api/chat/sessions")
        self.assertEqual(status, 200)
        self.assertEqual([s["name"] for s in resp["sessions"]], ["plan-keep"])

    def test_a_broken_entry_never_makes_the_route_a_500(self):
        """Any single unreadable entry must not take the whole listing down."""
        self._session("plan-real", 1)
        with mock.patch.object(orchchat, "_read_turns",
                               side_effect=PermissionError("nope")):
            status, resp = self._get("/api/chat/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(resp["sessions"], [])

    # ---- start still creates exactly one session jsonl ----------------------

    def test_start_creates_the_session_jsonl_and_a_fixed_argv(self):
        repo = str(config.ROOT)
        status, resp = self._post_json("/api/chat/start", {
            "session": "plan-new-one", "repo": repo, "message": "build it"})
        self.assertEqual(status, 200)
        self.assertIn("pid", resp)
        path = self.chat_dir / "plan-new-one.jsonl"
        self.assertTrue(path.exists())
        turns = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l]
        self.assertEqual([t["role"] for t in turns], ["user"])
        self.assertEqual(turns[0]["text"], "build it")
        # the argv stays byte-identical to what Rule 6b documents
        argv = self.spawn_calls[0][0]
        self.assertEqual(argv[1:5], ["main.py", "chat", "--session", "plan-new-one"])
        self.assertEqual(argv[5:7], ["--repo", repo])
        # and the new session is what the picker now lists
        status, listing = self._get("/api/chat/sessions")
        self.assertEqual(status, 200)
        self.assertEqual([s["name"] for s in listing["sessions"]], ["plan-new-one"])

    # ---- polling is per-session --------------------------------------------

    def test_poll_is_per_session(self):
        self._session("plan-one", 2)
        self._session("plan-two", 1)
        status, one = self._get("/api/chat/poll?session=plan-one&since=0")
        self.assertEqual(status, 200)
        self.assertEqual([t["text"] for t in one["turns"]], ["turn 0", "turn 1"])
        status, two = self._get("/api/chat/poll?session=plan-two&since=0")
        self.assertEqual(status, 200)
        self.assertEqual([t["text"] for t in two["turns"]], ["turn 0"])
        status, tail = self._get("/api/chat/poll?session=plan-one&since=1")
        self.assertEqual(status, 200)
        self.assertEqual([t["text"] for t in tail["turns"]], ["turn 1"])

    def test_poll_of_a_corrupt_session_is_empty_not_a_500(self):
        (self.chat_dir / "plan-bad.jsonl").write_bytes(b"\x00\x01garbage")
        status, resp = self._get("/api/chat/poll?session=plan-bad&since=0")
        self.assertEqual(status, 200)
        self.assertEqual(resp["turns"], [])


if __name__ == "__main__":
    unittest.main()
