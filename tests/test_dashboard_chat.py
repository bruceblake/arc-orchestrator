"""HTTP contract tests for the orchestrator-chat and repo endpoints.

GET /api/repos, POST /api/repos/create, POST /api/repos/remote,
POST /api/chat/start and GET /api/chat/poll are how the dashboard's chat UI
talks to the planner. Three of them mutate: one creates git repos, one adds
their GitHub remote, one appends to a session file and spawns `main.py chat`.
They are unauthenticated (Rule 6b), so the allowlist discipline is the entire
security boundary — /api/chat/start and /api/repos/remote must refuse any
repo path that is not byte-identical to a /api/repos entry.

Driven socket-less, like test_http_write.py: _spawn_logged is patched so no
model is ever called and no `main.py chat` process ever starts — the tests
assert on argv and the launch registry instead. Repo creation DOES run local
git (init + one commit) against per-test temp dirs; remote creation runs
through the patched _ensure_remote hook, so no test ever talks to GitHub.
"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="arc-qa-chat-mod-")
# Redirect the session task dirs BEFORE config/dashboard import — dashboard
# reads ARC_CHAT_DIR / ARC_REPOS_DIR at call time, config reads ARC_TASKS_DIR
# at import time, and the real operator dirs must never see a test write.
os.environ.setdefault("ARC_CHAT_DIR", str(Path(_TMP) / "chat"))
os.environ.setdefault("ARC_TASKS_DIR", str(Path(_TMP) / "tasks"))
os.environ.setdefault("ARC_REPOS_DIR", str(Path(_TMP) / "repos"))

import config  # noqa: E402
import dashboard  # noqa: E402


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
        # Content-Type matters now: the dashboard refuses a POST that does not
        # declare JSON (415), which is what stops a cross-origin text/plain
        # form post from reaching a handler. These tests exercise the handlers
        # THEMSELVES, so they send a well-formed request and let the checks
        # they are actually about — traversal, validation — decide the status.
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


class _ChatCase(unittest.TestCase):
    """Per-test temp ARC_CHAT_DIR/ARC_REPOS_DIR, cleared registry, stubbed spawn."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)
        self.chat_dir = self.tmp / "chat"
        self.repos_dir = self.tmp / "repos"
        self.repos_dir.mkdir()
        self._env = {k: os.environ.get(k) for k in ("ARC_CHAT_DIR", "ARC_REPOS_DIR")}
        os.environ["ARC_CHAT_DIR"] = str(self.chat_dir)
        os.environ["ARC_REPOS_DIR"] = str(self.repos_dir)
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
        return mock.Mock(pid=4242), log_name

    def _get(self, path):
        req = _FakeRequest(path)
        req.do_GET()
        return req.status, json.loads(req.body.decode("utf-8"))

    def _post_json(self, path, obj):
        req = _FakeRequest(path, json.dumps(obj).encode("utf-8"))
        req.do_POST()
        return req.status, json.loads(req.body.decode("utf-8"))

    def _init_repo(self, name):
        repo = self.repos_dir / name
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        return repo


class TestReposApi(_ChatCase):
    """GET /api/repos — the allowlist every chat/start repo is judged by."""

    def test_arc_orchestrator_is_first_then_scan_sorted(self):
        self._init_repo("b-repo")
        self._init_repo("a-repo")
        (self.repos_dir / "plain").mkdir()          # no .git — must be skipped
        status, resp = self._get("/api/repos")
        self.assertEqual(status, 200)
        repos = resp["repos"]
        self.assertEqual(repos[0]["name"], "arc-orchestrator")
        self.assertEqual(repos[0]["path"], str(config.ROOT))
        self.assertEqual([r["name"] for r in repos[1:]], ["a-repo", "b-repo"])
        self.assertNotIn("plain", [r["name"] for r in repos])
        for r in repos:
            self.assertEqual(set(r), {"name", "path", "branch", "remote",
                                      "remote_url", "last_commit",
                                      "last_subject", "projects"})
            self.assertIsInstance(r["branch"], str)
            self.assertIsInstance(r["remote"], bool)
            self.assertTrue(r["remote_url"] is None
                            or isinstance(r["remote_url"], str))
            self.assertTrue(r["last_commit"] is None
                            or isinstance(r["last_commit"], int))
            self.assertTrue(r["last_subject"] is None
                            or isinstance(r["last_subject"], str))
            self.assertIsInstance(r["projects"], int)
        self.assertFalse(repos[1]["remote"])  # freshly inited: no remote
        self.assertIsNone(repos[1]["remote_url"])
        self.assertIsNone(repos[1]["last_commit"])  # and no commits yet
        self.assertIsNone(repos[1]["last_subject"])
        self.assertEqual(repos[1]["projects"], 0)   # no taskfiles target it
        # the live repo has a history and taskfiles the picker can show
        self.assertIsInstance(repos[0]["last_commit"], int)
        self.assertIsInstance(repos[0]["last_subject"], str)

    def test_missing_repos_dir_still_lists_arc_orchestrator(self):
        os.environ["ARC_REPOS_DIR"] = str(self.tmp / "no-such-dir")
        status, resp = self._get("/api/repos")
        self.assertEqual(status, 200)
        self.assertEqual([r["name"] for r in resp["repos"]], ["arc-orchestrator"])


class TestReposCreateApi(_ChatCase):
    """POST /api/repos/create — local git init, then a GitHub remote via the
    patchable _ensure_remote hook (no test ever runs real gh)."""

    def setUp(self):
        super().setUp()
        self.remote_calls = []
        self.remote_result = (True, "https://github.com/x/good-repo")

        def fake_ensure(path, name=None, private=True):
            self.remote_calls.append((str(path), name, private))
            return self.remote_result

        patcher = mock.patch.object(dashboard, "_ensure_remote", fake_ensure)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_create_really_git_inits_the_repo(self):
        status, resp = self._post_json("/api/repos/create", {"name": "good-repo"})
        self.assertEqual(status, 200)
        self.assertEqual(resp["name"], "good-repo")
        path = Path(resp["path"])
        self.assertEqual(path, self.repos_dir / "good-repo")
        self.assertTrue((path / ".git").is_dir())
        head = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                              capture_output=True, text=True)
        self.assertEqual(head.returncode, 0, "needs exactly one initial commit")
        self.assertIn("good-repo", (path / "README.md").read_text())
        names = [r["name"] for r in self._get("/api/repos")[1]["repos"]]
        self.assertIn("good-repo", names)

    def test_create_also_ensures_a_remote(self):
        status, resp = self._post_json("/api/repos/create", {"name": "good-repo"})
        self.assertEqual(status, 200)
        self.assertEqual(resp["remote_url"], "https://github.com/x/good-repo")
        self.assertEqual(resp["remote_note"], "created github.com/x/good-repo")
        self.assertEqual(self.remote_calls,
                         [(str(self.repos_dir / "good-repo"), "good-repo", True)])

    def test_private_flag_reaches_the_hook(self):
        self._post_json("/api/repos/create", {"name": "pub-repo", "private": False})
        self.assertEqual(self.remote_calls[0][2], False)

    def test_remote_false_means_local_only(self):
        status, resp = self._post_json(
            "/api/repos/create", {"name": "local-only", "remote": False})
        self.assertEqual(status, 200)
        self.assertIsNone(resp["remote_url"])
        self.assertEqual(self.remote_calls, [])

    def test_remote_failure_is_a_note_not_a_500(self):
        # gh missing or unauthenticated must not bury the local repo: the
        # 09-12 minecraft-test loss was a missing remote, not a broken repo.
        self.remote_result = (False, "gh not authenticated")
        status, resp = self._post_json("/api/repos/create", {"name": "good-repo"})
        self.assertEqual(status, 200)
        self.assertIsNone(resp["remote_url"])
        self.assertIn("gh not authenticated", resp["remote_note"])
        self.assertIn("local only", resp["remote_note"])
        self.assertTrue((self.repos_dir / "good-repo" / ".git").is_dir())

    def test_rejects_bad_private_and_remote_flags(self):
        for flags in ({"private": "yes"}, {"remote": 1}, {"private": None}):
            with self.subTest(flags=flags):
                status, resp = self._post_json(
                    "/api/repos/create", {"name": "flagged", **flags})
                self.assertEqual(status, 400)
                self.assertIn("error", resp)
        self.assertEqual(self.remote_calls, [])

    def test_rejects_traversal_and_bad_names(self):
        for bad in ("../evil", "a/b", "No-Upper", "_bad", "", None, "x" * 42):
            with self.subTest(name=bad):
                status, resp = self._post_json("/api/repos/create", {"name": bad})
                self.assertEqual(status, 400)
                self.assertIn("error", resp)
        self.assertEqual(list(self.repos_dir.iterdir()), [])
        self.assertFalse((self.repos_dir / ".." / "evil").exists())

    def test_existing_name_conflicts_with_409(self):
        self._post_json("/api/repos/create", {"name": "twice"})
        status, resp = self._post_json("/api/repos/create", {"name": "twice"})
        self.assertEqual(status, 409)
        self.assertTrue(resp["exists"])
        self.assertIn("error", resp)


class TestReposRemoteApi(_ChatCase):
    """POST /api/repos/remote — gh-backed remote creation for an EXISTING
    checkout, gated by the same /api/repos allowlist as /api/chat/start."""

    def setUp(self):
        super().setUp()
        self.remote_calls = []

        def fake_ensure(path, name=None, private=True):
            self.remote_calls.append((str(path), name, private))
            return True, "https://github.com/x/listed"

        patcher = mock.patch.object(dashboard, "_ensure_remote", fake_ensure)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_rejects_a_repo_not_on_the_allowlist(self):
        # Rule 6b: never an arbitrary path — even a REAL git repo that is
        # not on the /api/repos list is refused before gh is consulted.
        rogue = self._init_repo("rogue")
        (rogue / ".git").rename(self.repos_dir / "rogue-git")  # delist it
        for bad in (str(self.tmp / "outside"), str(rogue), "/etc", "", None):
            with self.subTest(repo=bad):
                status, resp = self._post_json("/api/repos/remote", {"repo": bad})
                self.assertIn(status, (400, 403))
                self.assertIn("error", resp)
        self.assertEqual(self.remote_calls, [])

    def test_ensures_remote_for_a_listed_repo(self):
        repo = self._init_repo("listed")
        status, resp = self._post_json("/api/repos/remote", {"repo": str(repo)})
        self.assertEqual(status, 200)
        self.assertEqual(resp["remote_url"], "https://github.com/x/listed")
        self.assertEqual(resp["note"], "created github.com/x/listed")
        self.assertEqual(self.remote_calls, [(str(repo), "listed", True)])

    def test_failure_is_a_note_with_the_reason(self):
        repo = self._init_repo("listed")
        with mock.patch.object(dashboard, "_ensure_remote",
                               return_value=(False, "gh not authenticated")):
            status, resp = self._post_json("/api/repos/remote", {"repo": str(repo)})
        self.assertEqual(status, 200)
        self.assertIsNone(resp["remote_url"])
        self.assertEqual(resp["note"], "gh not authenticated")

    def test_rejects_bad_private_flag(self):
        repo = self._init_repo("listed")
        status, resp = self._post_json(
            "/api/repos/remote", {"repo": str(repo), "private": "yes"})
        self.assertEqual(status, 400)
        self.assertIn("error", resp)
        self.assertEqual(self.remote_calls, [])


class TestChatApi(_ChatCase):
    """POST /api/chat/start — appends the user turn, spawns the chat process."""

    def test_rejects_a_repo_not_on_the_allowlist(self):
        # The one byte of Rule 6b this endpoint lives or dies by: an arbitrary
        # path must never reach a spawn. Even a REAL git repo that is not on
        # the /api/repos list is refused.
        rogue = self._init_repo("rogue")
        (rogue / ".git").rename(self.repos_dir / "rogue-git")  # delist it
        for bad in (str(self.tmp / "outside"), str(rogue), "/etc", "", None):
            with self.subTest(repo=bad):
                status, resp = self._post_json("/api/chat/start", {
                    "session": "s1", "repo": bad, "message": "hi"})
                self.assertIn(status, (400, 403))
                self.assertIn("error", resp)
        self.assertEqual(self.spawn_calls, [])
        self.assertFalse(self.chat_dir.exists())

    def test_rejects_bad_session_and_message(self):
        repo = str(config.ROOT)
        for session in ("../x", "Bad", "x" * 41, "", None):
            with self.subTest(session=session):
                status, _ = self._post_json("/api/chat/start", {
                    "session": session, "repo": repo, "message": "hi"})
                self.assertEqual(status, 400)
        for message in ("", None, 5, "x" * 8001):
            with self.subTest(message=str(message)[:20]):
                status, _ = self._post_json("/api/chat/start", {
                    "session": "s1", "repo": repo, "message": message})
                self.assertEqual(status, 400)
        self.assertEqual(self.spawn_calls, [])

    def test_valid_start_appends_user_turn_and_registers_chat(self):
        repo = str(config.ROOT)
        status, resp = self._post_json("/api/chat/start", {
            "session": "s1", "repo": repo, "message": "build me a widget"})
        self.assertEqual(status, 200)
        self.assertEqual(resp["pid"], 4242)
        lines = (self.chat_dir / "s1.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        turn = json.loads(lines[0])
        self.assertEqual(turn["role"], "user")
        self.assertEqual(turn["text"], "build me a widget")
        argv, log_name = self.spawn_calls[0]
        self.assertEqual(argv[0],
                         str(Path(config.ROOT) / ".venv" / "bin" / "python"))
        self.assertEqual(argv[1:], ["main.py", "chat", "--session", "s1",
                                    "--repo", repo])
        self.assertEqual(log_name, "chat-s1.log")
        rec = dashboard._launch_registry["chat:s1"]
        self.assertEqual(rec["kind"], "chat")
        self.assertEqual(rec["pid"], 4242)

    def test_second_start_while_running_conflicts(self):
        repo = str(config.ROOT)
        self._post_json("/api/chat/start", {
            "session": "s1", "repo": repo, "message": "first"})
        # The stub spawn's pid 4242 is already dead; make the registry hold a
        # LIVE pid (this test process), the condition _prune_registry keeps.
        dashboard._launch_registry["chat:s1"]["pid"] = os.getpid()
        status, resp = self._post_json("/api/chat/start", {
            "session": "s1", "repo": repo, "message": "second"})
        self.assertEqual(status, 409)
        self.assertEqual(resp["error"],
                         "a chat turn is already running for this session")
        self.assertEqual(len(self.spawn_calls), 1)
        # a rejected start must not mutate the session file
        lines = (self.chat_dir / "s1.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["text"], "first")
        rec = dashboard._launch_registry["chat:s1"]
        self.assertEqual(rec["pid"], os.getpid())

    def test_agents_splits_chats_out_of_runs(self):
        # Regression: chat entries must not inflate /api/agents `runs` (the
        # dashboard counts that list as "harness runs today").
        live = os.getpid()  # _agents prunes dead pids before building lists
        dashboard._launch_registry["chat:s1"] = {
            "pid": live, "log": "chat-s1.log", "started": 1.0, "kind": "chat"}
        dashboard._launch_registry["proj.json"] = {
            "pid": live, "log": "proj.log", "started": 1.0}
        data = dashboard._agents(None)
        self.assertEqual([r["taskfile"] for r in data["runs"]], ["proj.json"])
        self.assertEqual([c["session"] for c in data["chats"]], ["chat:s1"])


class TestChatApiPoll(_ChatCase):
    """GET /api/chat/poll — the UI's incremental read of a session file."""

    def _write_session(self, session, turns):
        self.chat_dir.mkdir(parents=True, exist_ok=True)
        (self.chat_dir / f"{session}.jsonl").write_text(
            "".join(json.dumps(t) + "\n" for t in turns), encoding="utf-8")

    def test_poll_returns_turns_from_the_since_index(self):
        turns = [
            {"role": "user", "ts": 1.0, "text": "hi"},
            {"role": "assistant", "ts": 2.0, "text": "hello", "taskfile": "w.json"},
            {"role": "user", "ts": 3.0, "text": "thanks"},
        ]
        self._write_session("s1", turns)
        status, resp = self._get("/api/chat/poll?session=s1&since=0")
        self.assertEqual(status, 200)
        self.assertEqual(resp["turns"], turns)
        self.assertEqual(resp["taskfile"], "w.json")
        status, resp = self._get("/api/chat/poll?session=s1&since=2")
        self.assertEqual(resp["turns"], turns[2:])
        status, resp = self._get("/api/chat/poll?session=s1&since=99")
        self.assertEqual(resp["turns"], [])

    def test_unknown_session_polls_empty(self):
        status, resp = self._get("/api/chat/poll?session=ghost&since=0")
        self.assertEqual(status, 200)
        self.assertEqual(resp, {"turns": [], "running": False, "taskfile": None})

    def test_poll_reports_a_running_chat(self):
        self._write_session("s1", [{"role": "user", "ts": 1.0, "text": "hi"}])
        dashboard._launch_registry["chat:s1"] = {
            "pid": os.getpid(), "log": "x.log", "started": 1.0, "kind": "chat"}
        status, resp = self._get("/api/chat/poll?session=s1&since=1")
        self.assertEqual(status, 200)
        self.assertTrue(resp["running"])
        self.assertIsNone(resp["taskfile"])

    def test_rejects_bad_session_id(self):
        status, _ = self._get("/api/chat/poll?session=../evil&since=0")
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
