"""HTTP contract tests for the POST (write) endpoints.

These five endpoints are the operator's only write controls — create a
project, launch/stop a run, archive, retry one task. They are driven from
the browser against a live dashboard, so a broken status code or a changed
JSON shape fails silently in the console. Worse, three of them spawn real
`main.py` processes and one sends signals, so a regression does not just
blank a screen — it can double-run a task file (two processes sharing
worktrees and branches) or archive a project out from under a live run.

Driving dashboard.Handler directly (no socket, no server) keeps the tests
inside the request-handling layer: _spawn_logged and reconcile.live_runs
are patched, so nothing here spawns a process, scans /proc or talks to
git over the network.
"""
import json
import os
import shutil
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import capture_events  # noqa: F401  (sys.path + event redirect)
from helpers import ENTRY, STRONGEST  # noqa: E402,F401

import config
import dashboard
from store import Store

MAX_BODY = 256 * 1024


class _FakeRequest(dashboard.Handler):
    """A socket-less Handler that captures status/headers/body in memory.

    BaseHTTPRequestHandler.__init__ requires a live socket; we skip it and
    set only what do_POST reads. `read` records every length asked of rfile,
    so tests can prove the 256KB cap rejects a request BEFORE draining it —
    on a real socket, reading first is what hangs the connection.
    """

    def __init__(self, path, body=b"", length=None, headers=None):
        self.status = None
        self.response_headers = {}
        self.body = b""
        self.wfile = self
        self.rfile = self
        self.path = path
        self._pending = body
        self.reads = []
        # What the dashboard's own jpost() sends. A request without the
        # Content-Type is refused before the body is read (see the
        # cross-origin tests), so every other test states it.
        self.headers = {
            "Content-Length": str(len(body) if length is None else length),
            "Content-Type": "application/json"}
        if headers:
            self.headers.update(headers)
            for k in [k for k, v in headers.items() if v is None]:
                del self.headers[k]

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass

    def read(self, n):
        self.reads.append(n)
        data, self._pending = self._pending[:n], self._pending[n:]
        return data

    def write(self, data):
        self.body += data


class _PostCase(unittest.TestCase):
    """Shared scaffolding for POST-endpoint tests.

    TASKS_DIR and EVENTS_LOG point at a per-test temp dir, the store is an
    in-memory sqlite, and the two side-effect boundaries — _spawn_logged
    (subprocess.Popen) and reconcile.live_runs (/proc scan) — are patched,
    so a test asserts on the request handling itself, never on processes it
    cannot control.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)
        self._orig_tasks = config.TASKS_DIR
        self._orig_log = config.EVENTS_LOG
        self._orig_repo_root = config.REPO_ROOT
        config.TASKS_DIR = str(self.tmp / "tasks")
        config.EVENTS_LOG = str(self.tmp / "events.jsonl")
        # The fence the dashboard accepts repos from. Pointing it at the
        # per-test dir is what lets these tests run on any machine: it was
        # the operator's literal home directory, and the suite could not
        # pass anywhere else — CI included.
        config.REPO_ROOT = str(self.tmp / "repos")
        Path(config.REPO_ROOT).mkdir(parents=True)
        Path(config.TASKS_DIR).mkdir(parents=True)
        self._orig_registry = dict(dashboard._launch_registry)
        dashboard._launch_registry.clear()
        dashboard.Handler.store = Store(":memory:")
        self.spawn_calls = []
        self.spawn_exit = None   # set to an int to fake a process that exits at once
        self._spawn_patch = mock.patch.object(
            dashboard, "_spawn_logged", side_effect=self._fake_spawn)
        self._spawn_patch.start()
        self._live_patch = mock.patch("reconcile.live_runs", return_value=[])
        self.live_runs = self._live_patch.start()

    def tearDown(self):
        self._live_patch.stop()
        self._spawn_patch.stop()
        dashboard._launch_registry.clear()
        dashboard._launch_registry.update(self._orig_registry)
        store = dashboard.Handler.store
        dashboard.Handler.store = None
        if store is not None:
            store.conn.close()
        config.TASKS_DIR = self._orig_tasks
        config.EVENTS_LOG = self._orig_log
        config.REPO_ROOT = self._orig_repo_root
        self._dir.cleanup()

    def _fake_spawn(self, argv, log_name):
        self.spawn_calls.append((list(argv), log_name))
        # A run that is still going: /api/projects/run waits briefly on the
        # process and reports "started" only if it has NOT exited. A bare
        # Mock's wait() returns a Mock — truthy, "it exited" — so make the
        # stub behave like a live process unless a test says otherwise.
        proc = mock.Mock(pid=4242)
        proc.wait.side_effect = subprocess.TimeoutExpired(argv, 1.5)
        proc.wait.return_value = None
        if self.spawn_exit is not None:
            proc.wait.side_effect = None
            proc.wait.return_value = self.spawn_exit
        return proc, log_name

    def _post(self, path, body, length=None, headers=None):
        """POST raw bytes; returns (status, parsed JSON body)."""
        req = _FakeRequest(path, body, length=length, headers=headers)
        req.do_POST()
        return req.status, json.loads(req.body.decode("utf-8"))

    def _post_json(self, path, obj):
        return self._post(path, json.dumps(obj).encode("utf-8"))

    def _post_raw(self, path, body, length=None, headers=None):
        """Like _post but also hands back the request (for read tracking)."""
        req = _FakeRequest(path, body, length=length, headers=headers)
        req.do_POST()
        return req.status, json.loads(req.body.decode("utf-8")), req

    def _assert_rejected(self, path, body, length=None, needle=None):
        status, resp = self._post(path, body, length=length)
        self.assertEqual(status, 400)
        self.assertIn("error", resp)
        if needle:
            self.assertIn(needle, resp["error"])
        return resp

    def _make_repo(self, commits=True):
        """A throwaway repo under config.REPO_ROOT — the only prefix the
        dashboard's _valid_repo accepts, so repo validation is exercised
        for real (local git only, never a network remote)."""
        repo = Path(tempfile.mkdtemp(prefix="arc-qa-http-", dir=config.REPO_ROOT))
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        if commits:
            (repo / "README.md").write_text("qa\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "-c", "user.name=qa",
                            "-c", "user.email=qa@example.com",
                            "commit", "-q", "-m", "init"],
                           cwd=repo, check=True)
        return repo

    def _write_taskfile(self, fname="proj.json", repo=None):
        """One valid taskfile in the temp TASKS_DIR; returns its path."""
        repo = self._make_repo() if repo is None else repo
        path = Path(config.TASKS_DIR) / fname
        path.write_text(json.dumps({"project": {
            "repo": str(repo), "title": "P",
            "tasks": [{"id": "t1", "title": "one", "prompt": "do"}]}}),
            encoding="utf-8")
        return path


class HttpWriteCreateEndpoint(_PostCase):
    """/api/projects/create — the only way new work enters the queue.

    create validates at POST time on purpose: a bad repo (not under
    REPO_ROOT, not a git repo, no commits on main) would otherwise be
    discovered minutes later inside gitstore.alloc — after Kimi-K3 has
    already spent a full planning run on it. These tests pin that the
    checks fire HERE with actionable messages, and that both modes hand
    back the fields the UI needs to track the spawn.
    """

    def test_goal_mode_spawns_planner_and_registers_it(self):
        repo = self._make_repo()
        status, resp = self._post_json("/api/projects/create", {
            "repo": str(repo), "goal": "build a widget"})
        self.assertEqual(status, 200)
        self.assertEqual(resp["mode"], "plan")
        self.assertEqual(resp["taskfile"], "build-a-widget.json")
        self.assertEqual(resp["pid"], 4242)
        self.assertTrue(resp["log"].startswith("plan-build-a-widget.log"))
        argv, log_name = self.spawn_calls[0]
        self.assertEqual(len(argv), 6)
        self.assertEqual(argv[2:], ["code", "plan", "build a widget", str(repo)])
        self.assertEqual(log_name, "plan-build-a-widget.log")
        # the registry is what makes /api/projects/stop and re-run work;
        # an unregistered spawn is an unkillable one
        rec = dashboard._launch_registry[
            str(Path(config.TASKS_DIR) / "build-a-widget.json")]
        self.assertEqual(rec["pid"], 4242)
        self.assertEqual(rec["kind"], "plan")

    def test_tasks_mode_writes_a_loadable_taskfile(self):
        repo = self._make_repo()
        status, resp = self._post_json("/api/projects/create", {
            "repo": str(repo), "title": "My Tool",
            "tasks": [
                {"id": "t1", "title": "one", "prompt": "do it"},
                {"id": "t2", "title": "two", "prompt": "again", "deps": ["t1"]}]})
        self.assertEqual(status, 200)
        self.assertEqual(resp, {"mode": "tasks", "file": "my-tool.json",
                                "n_tasks": 2})
        doc = json.loads((Path(config.TASKS_DIR) / "my-tool.json")
                         .read_text(encoding="utf-8"))
        self.assertEqual(doc["project"]["repo"], str(repo))
        t1, t2 = doc["project"]["tasks"]
        # code_tasks.load_taskfile rejects tasks with no model or an unknown
        # reviewer, so create must always fill both in
        self.assertEqual(t1["model"], ENTRY)
        # the cross-family default for the entry model, whatever the roster says
        self.assertEqual(t1["reviewer"], config.cross_family_reviewer(ENTRY))
        self.assertEqual(t2["deps"], ["t1"])
        self.assertEqual(self.spawn_calls, [])

    def test_tasks_mode_rejects_unknown_dep_before_writing(self):
        # a taskfile referencing a missing dep is unloadable — the DAG would
        # silently drop the task, so it must die at create time
        repo = self._make_repo()
        self._assert_rejected("/api/projects/create", json.dumps({
            "repo": str(repo), "title": "T",
            "tasks": [{"id": "t1", "title": "one", "prompt": "x",
                       "deps": ["ghost"]}]}).encode("utf-8"),
            needle="deps references unknown id")
        self.assertEqual(list(Path(config.TASKS_DIR).iterdir()), [])

    def test_second_create_conflicts_until_overwrite(self):
        # 409 (not silent overwrite) protects a taskfile an operator may be
        # hand-editing; overwrite=true is the explicit escape hatch
        body = {"repo": str(self._make_repo()), "title": "Twice",
                "tasks": [{"id": "t1", "title": "one", "prompt": "x"}]}
        status, _ = self._post_json("/api/projects/create", body)
        self.assertEqual(status, 200)
        status, resp = self._post_json("/api/projects/create", body)
        self.assertEqual(status, 409)
        self.assertTrue(resp["exists"])
        self.assertEqual(resp["file"], "twice.json")
        body["overwrite"] = True
        status, _ = self._post_json("/api/projects/create", body)
        self.assertEqual(status, 200)

    def test_rejects_repo_outside_the_home_prefix(self):
        # only config.REPO_ROOT/... is accepted, so a path the dashboard can
        # never manage is refused instead of failing later in alloc. The
        # fence is a PATH, not a string prefix: a sibling directory whose
        # name merely starts with the root's, a traversal back out of it,
        # and the root itself are all outside.
        outside = Path(tempfile.mkdtemp(prefix="arc-qa-outside-"))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        sibling = Path(config.REPO_ROOT + "-evil")
        sibling.mkdir()
        self.addCleanup(shutil.rmtree, sibling, ignore_errors=True)
        for bad_repo in (None, str(outside), "relative/repo", str(sibling),
                         config.REPO_ROOT,
                         str(Path(config.REPO_ROOT) / ".." / outside.name)):
            with self.subTest(repo=bad_repo):
                self._assert_rejected("/api/projects/create", json.dumps({
                    "repo": bad_repo, "title": "T",
                    "tasks": [{"id": "t1", "title": "one", "prompt": "x"}]
                }).encode("utf-8"),
                    needle=f"repo must be an existing absolute path under {config.REPO_ROOT}")

    def test_rejects_repo_that_is_not_a_git_repository(self):
        plain = Path(tempfile.mkdtemp(prefix="arc-qa-http-", dir=config.REPO_ROOT))
        self.addCleanup(shutil.rmtree, plain, ignore_errors=True)
        resp = self._assert_rejected("/api/projects/create", json.dumps({
            "repo": str(plain), "title": "T",
            "tasks": [{"id": "t1", "title": "one", "prompt": "x"}]}).encode("utf-8"))
        # the message must tell the operator HOW to fix it, not just "no"
        self.assertIn("is not a git repository", resp["error"])
        self.assertIn("git init", resp["error"])

    def test_rejects_repo_without_commits_on_base_branch(self):
        # branches cut from 'main' with no commits → every task fails in
        # gitstore.alloc; refuse at the door, naming the branch
        repo = self._make_repo(commits=False)
        resp = self._assert_rejected("/api/projects/create", json.dumps({
            "repo": str(repo), "title": "T",
            "tasks": [{"id": "t1", "title": "one", "prompt": "x"}]}).encode("utf-8"))
        self.assertIn("has no 'main' branch with any commits", resp["error"])
        self.assertIn("git commit", resp["error"])

    def test_rejects_non_json_body(self):
        self._assert_rejected("/api/projects/create", b"{nope",
                              needle="invalid JSON")

    def test_rejects_empty_body(self):
        self._assert_rejected("/api/projects/create", b"",
                              needle="body size must be 1 byte..256KB")

    def test_rejects_array_body(self):
        self._assert_rejected("/api/projects/create", b"[1, 2]",
                              needle="JSON body required")

    def test_rejects_oversized_body_without_reading_it(self):
        # >256KB must 400 BEFORE rfile.read() — on a real socket, draining
        # first is what hangs the connection for the full body transfer
        status, resp, req = self._post_raw(
            "/api/projects/create", b"{}", length=MAX_BODY + 1)
        self.assertEqual(status, 400)
        self.assertIn("body size", resp["error"])
        self.assertEqual(req.reads, [])


class HttpWriteRunEndpoint(_PostCase):
    """/api/projects/run — double-start protection lives HERE, not in main.py.

    Two processes on one task file share task ids, worktrees and branches;
    the corruption is silent until a merge lands the wrong code. The
    registry only knows runs THIS dashboard launched, so a second guard
    scans live processes — both must 409 before any spawn happens.
    """

    def test_run_spawns_run_process_and_registers_it(self):
        path = self._write_taskfile()
        status, resp = self._post_json("/api/projects/run",
                                       {"file": "proj.json"})
        self.assertEqual(status, 200)
        self.assertEqual(resp["pid"], 4242)
        self.assertFalse(resp["dry_run"])
        self.assertTrue(resp["log"].startswith("run-proj-"))
        argv, _log = self.spawn_calls[0]
        self.assertEqual(argv[2:], ["code", "run", str(path)])
        rec = dashboard._launch_registry[str(path)]
        self.assertEqual(rec["pid"], 4242)
        self.assertEqual(rec["kind"], "run")

    def test_a_run_that_dies_at_once_is_reported_not_started(self):
        # The 09-12 case: `code run` crashed in its first second (a nested
        # asyncio.run), the dashboard said "started (pid N)", and the project
        # never moved. "started" must mean the process is still alive after
        # the spawn — otherwise it is an error carrying the log's tail.
        path = self._write_taskfile()
        self.spawn_exit = 1
        (Path(config.ROOT) / "logs").mkdir(exist_ok=True)
        status, resp = self._post_json("/api/projects/run", {"file": "proj.json"})
        self.assertEqual(status, 500)
        self.assertIn("exited immediately", resp["error"])
        self.assertEqual(resp["exit"], 1)
        self.assertTrue(resp["log"].startswith("run-proj-"))
        self.assertIsInstance(resp["tail"], list)
        self.assertNotIn(str(path), dashboard._launch_registry)

    def test_a_dry_run_that_finishes_cleanly_is_reported_finished(self):
        self._write_taskfile()
        self.spawn_exit = 0
        status, resp = self._post_json("/api/projects/run",
                                       {"file": "proj.json", "dry_run": True})
        self.assertEqual(status, 200)
        self.assertTrue(resp["finished"])
        self.assertEqual(dashboard._launch_registry, {})

    def test_dry_run_flag_reaches_argv_and_response(self):
        # the UI's only pre-flight check: a dry run must be visible in the
        # process argv (the run itself must know) and in the response
        self._write_taskfile()
        status, resp = self._post_json("/api/projects/run",
                                       {"file": "proj.json", "dry_run": True})
        self.assertEqual(status, 200)
        self.assertTrue(resp["dry_run"])
        argv, _log = self.spawn_calls[0]
        self.assertEqual(argv[-1], "--dry-run")
        self.assertTrue(dashboard._launch_registry[
            str(Path(config.TASKS_DIR) / "proj.json")]["dry_run"])

    def test_run_conflicts_when_this_dashboard_already_launched_it(self):
        path = self._write_taskfile()
        # a LIVE pid — _prune_registry drops dead ones before checking
        dashboard._launch_registry[str(path)] = {
            "pid": os.getpid(), "log": "x.log", "started": 1.0, "kind": "run"}
        status, resp = self._post_json("/api/projects/run",
                                       {"file": "proj.json"})
        self.assertEqual(status, 409)
        self.assertIn("already has a running process", resp["error"])
        self.assertEqual(resp["pid"], os.getpid())
        self.assertEqual(self.spawn_calls, [])

    def test_run_conflicts_when_started_outside_the_dashboard(self):
        # the registry is blind to terminal/queue launches; the /proc scan
        # is the only guard against those, and it must fire first-thing
        path = self._write_taskfile()
        self.live_runs.return_value = [{"pid": 555, "taskfile": str(path)}]
        status, resp = self._post_json("/api/projects/run",
                                       {"file": "proj.json"})
        self.assertEqual(status, 409)
        self.assertIn("outside this dashboard", resp["error"])
        self.assertEqual(resp["pid"], 555)
        self.assertEqual(self.spawn_calls, [])

    def test_run_rejects_zero_byte_taskfile(self):
        # a crash mid-write used to leave 0-byte task files that then failed
        # every later run — the endpoint must 400 cleanly, not 500
        (Path(config.TASKS_DIR) / "empty.json").write_bytes(b"")
        self._assert_rejected("/api/projects/run", b'{"file": "empty.json"}',
                              needle="invalid task file")

    def test_run_rejects_taskfile_with_unusable_repo(self):
        # taskfiles are hand-editable; run re-validates the repo rather
        # than trusting whatever create wrote
        (Path(config.TASKS_DIR) / "badrepo.json").write_text(
            json.dumps({"project": {"repo": "/tmp/nope", "tasks": []}}),
            encoding="utf-8")
        self._assert_rejected("/api/projects/run", b'{"file": "badrepo.json"}',
                              needle="unusable repo")

    def test_run_unknown_file_returns_404(self):
        status, resp = self._post_json("/api/projects/run",
                                       {"file": "ghost.json"})
        self.assertEqual(status, 404)
        self.assertIn("error", resp)

    def test_run_rejects_bad_file_name(self):
        # the regex is the only thing keeping "../" and "/" out of paths
        # built from user input — a traversal here reads any .json on disk
        for fname in ("", "nope", "../evil.json", "sub/dir.json", "a.json.txt"):
            with self.subTest(fname=fname):
                self._assert_rejected(
                    "/api/projects/run",
                    json.dumps({"file": fname}).encode("utf-8"),
                    needle="bad file name")

    def test_rejects_non_json_body(self):
        self._assert_rejected("/api/projects/run", b"{nope",
                              needle="invalid JSON")

    def test_rejects_empty_body(self):
        self._assert_rejected("/api/projects/run", b"",
                              needle="body size must be 1 byte..256KB")

    def test_rejects_array_body(self):
        self._assert_rejected("/api/projects/run", b"[1]",
                              needle="JSON body required")

    def test_rejects_oversized_body_without_reading_it(self):
        status, resp, req = self._post_raw(
            "/api/projects/run", b"{}", length=MAX_BODY + 1)
        self.assertEqual(status, 400)
        self.assertIn("body size", resp["error"])
        self.assertEqual(req.reads, [])


class HttpWriteStopEndpoint(_PostCase):
    """/api/projects/stop — the operator's only kill switch for a run.

    It must SIGTERM exactly the processes owning that task file: the run's
    SIGTERM handler cancels the graph, reaps its harness children and
    releases driver leases — a hard KILL leaves the orphan fleet that
    `code reconcile` exists to clean up. And signalling a wrong pid (or a
    dead one) is worse than not stopping at all, so "no live run" is a
    409, not a silent no-op 200.
    """

    def test_stop_sigterms_only_the_runs_owning_the_file(self):
        path = self._write_taskfile()
        other = Path(config.TASKS_DIR) / "other.json"
        self.live_runs.return_value = [
            {"pid": 111, "taskfile": str(path)},
            {"pid": 222, "taskfile": str(path)},
            {"pid": 333, "taskfile": str(other)},
        ]
        dashboard._launch_registry[str(path)] = {
            "pid": os.getpid(), "log": "x.log", "started": 1.0, "kind": "run"}
        with mock.patch("os.kill") as kill, capture_events() as ev:
            status, resp = self._post_json("/api/projects/stop",
                                           {"file": "proj.json"})
        self.assertEqual(status, 200)
        self.assertEqual(resp["stopped"], [111, 222])
        kill.assert_any_call(111, signal.SIGTERM)
        kill.assert_any_call(222, signal.SIGTERM)
        # the run on other.json must survive a stop aimed at proj.json
        self.assertEqual(kill.call_count, 2)
        self.assertNotIn(str(path), dashboard._launch_registry)
        self.assertEqual(ev.first("run.stop_requested")["pids"], [111, 222])

    def test_stop_with_no_live_run_returns_409(self):
        # includes a file that does not exist — stop is about processes,
        # not files, and a silent 200 would hide "your run already died"
        status, resp = self._post_json("/api/projects/stop",
                                       {"file": "ghost.json"})
        self.assertEqual(status, 409)
        self.assertIn("no run process is active", resp["error"])
        self.assertEqual(resp["stopped"], [])

    def test_stop_rejects_bad_file_name(self):
        for fname in ("", "nope", "../evil.json", "sub/dir.json"):
            with self.subTest(fname=fname):
                self._assert_rejected(
                    "/api/projects/stop",
                    json.dumps({"file": fname}).encode("utf-8"),
                    needle="bad file name")

    def test_rejects_non_json_body(self):
        self._assert_rejected("/api/projects/stop", b"{nope",
                              needle="invalid JSON")

    def test_rejects_empty_body(self):
        self._assert_rejected("/api/projects/stop", b"",
                              needle="body size must be 1 byte..256KB")

    def test_rejects_array_body(self):
        self._assert_rejected("/api/projects/stop", b"[1]",
                              needle="JSON body required")

    def test_rejects_oversized_body_without_reading_it(self):
        status, resp, req = self._post_raw(
            "/api/projects/stop", b"{}", length=MAX_BODY + 1)
        self.assertEqual(status, 400)
        self.assertIn("body size", resp["error"])
        self.assertEqual(req.reads, [])


class HttpWriteArchiveEndpoint(_PostCase):
    """/api/projects/archive — tidy the project list without losing history.

    Archiving hides a project from the active list but never touches the
    task file or its rows; it must refuse while the project is running
    (hiding a live run invites a second run on the same file), and it must
    warn when the project ended with failed/conflict tasks so the operator
    does not bury a broken fleet.
    """

    def test_archive_marks_project_archived(self):
        path = self._write_taskfile()
        with capture_events() as ev:
            status, resp = self._post_json("/api/projects/archive",
                                           {"file": "proj.json"})
        self.assertEqual(status, 200)
        self.assertEqual(resp["file"], "proj.json")
        self.assertTrue(resp["archived"])
        self.assertIn(str(path), dashboard.Handler.store.archived_projects())
        self.assertIsNotNone(ev.first("project.archived"))
        # archiving hides the project, it never deletes the file
        self.assertTrue(path.is_file())

    def test_archive_restores_project(self):
        path = self._write_taskfile()
        dashboard.Handler.store.set_project_archived(str(path), True)
        with capture_events() as ev:
            status, resp = self._post_json(
                "/api/projects/archive",
                {"file": "proj.json", "archived": False})
        self.assertEqual(status, 200)
        self.assertFalse(resp["archived"])
        self.assertNotIn(str(path),
                         dashboard.Handler.store.archived_projects())
        self.assertIsNotNone(ev.first("project.restored"))

    def test_archive_warns_when_tasks_ended_failed_or_conflict(self):
        path = self._write_taskfile()
        dashboard.Handler.store.upsert_code_task(
            str(path), "t1", "one", ENTRY, "kimi", "failed")
        status, resp = self._post_json("/api/projects/archive",
                                       {"file": "proj.json"})
        self.assertEqual(status, 200)
        self.assertIn("1 task(s) ended failed/conflict", resp["warning"])

    def test_archive_conflicts_while_project_is_running(self):
        path = self._write_taskfile()
        self.live_runs.return_value = [{"pid": 9, "taskfile": str(path)}]
        status, resp = self._post_json("/api/projects/archive",
                                       {"file": "proj.json"})
        self.assertEqual(status, 409)
        self.assertIn("stop it before archiving", resp["error"])
        self.assertNotIn(str(path),
                         dashboard.Handler.store.archived_projects())

    def test_archive_unknown_file_returns_404(self):
        status, resp = self._post_json("/api/projects/archive",
                                       {"file": "ghost.json"})
        self.assertEqual(status, 404)
        self.assertIn("error", resp)

    def test_archive_rejects_bad_file_name(self):
        for fname in ("", "nope", "../evil.json", "sub/dir.json"):
            with self.subTest(fname=fname):
                self._assert_rejected(
                    "/api/projects/archive",
                    json.dumps({"file": fname}).encode("utf-8"),
                    needle="bad file name")

    def test_rejects_non_json_body(self):
        self._assert_rejected("/api/projects/archive", b"{nope",
                              needle="invalid JSON")

    def test_rejects_empty_body(self):
        self._assert_rejected("/api/projects/archive", b"",
                              needle="body size must be 1 byte..256KB")

    def test_rejects_array_body(self):
        self._assert_rejected("/api/projects/archive", b"[1]",
                              needle="JSON body required")

    def test_rejects_oversized_body_without_reading_it(self):
        status, resp, req = self._post_raw(
            "/api/projects/archive", b"{}", length=MAX_BODY + 1)
        self.assertEqual(status, 400)
        self.assertIn("body size", resp["error"])
        self.assertEqual(req.reads, [])


class HttpWriteRetryTaskEndpoint(_PostCase):
    """/api/projects/retry-task — surgical re-run of ONE task.

    Resume (`code run` on the same file) skips merged tasks, so a failed
    task is retried by resetting exactly that row to pending; touching any
    other row would re-run already-merged work. The endpoint must 404 on
    an unknown task id rather than silently no-op, and refuse while the
    project is running.
    """

    def test_retry_resets_exactly_one_task_to_pending(self):
        path = self._write_taskfile()
        store = dashboard.Handler.store
        store.upsert_code_task(str(path), "t1", "one", ENTRY,
                               "kimi", "failed")
        store.upsert_code_task(str(path), "t2", "two", ENTRY,
                               "kimi", "merged")
        status, resp = self._post_json("/api/projects/retry-task",
                                       {"file": "proj.json", "task": "t1"})
        self.assertEqual(status, 200)
        self.assertEqual({k: resp[k] for k in ("file", "task", "status")},
                         {"file": "proj.json", "task": "t1", "status": "pending"})
        # A retry that only flipped the status was not a retry: nothing
        # scheduled "the next code run" it relied on, and a task sat pending
        # for nine hours. With no run holding the file, retry must start one.
        # (_spawn_logged is stubbed by this suite, so the pid is the stub's.)
        self.assertIsNotNone(resp.get("launched_pid"),
                             "retry on an idle project must launch a run")
        rows = {r["id"]: r["status"] for r in store.code_tasks_all()
                if r["taskfile"] == str(path)}
        self.assertEqual(rows, {"t1": "pending", "t2": "merged"})

    def test_retry_unknown_task_id_returns_404(self):
        path = self._write_taskfile()
        dashboard.Handler.store.upsert_code_task(
            str(path), "t1", "one", ENTRY, "kimi", "failed")
        status, resp = self._post_json("/api/projects/retry-task",
                                       {"file": "proj.json", "task": "ghost"})
        self.assertEqual(status, 404)
        self.assertIn("task id not found", resp["error"])

    def test_retry_conflicts_while_project_is_running(self):
        path = self._write_taskfile()
        # the row must exist: the 404 lookup runs before the live check
        dashboard.Handler.store.upsert_code_task(
            str(path), "t1", "one", ENTRY, "kimi", "failed")
        self.live_runs.return_value = [{"pid": 7, "taskfile": str(path)}]
        status, resp = self._post_json("/api/projects/retry-task",
                                       {"file": "proj.json", "task": "t1"})
        self.assertEqual(status, 409)
        self.assertIn("stop it before retrying", resp["error"])

    def test_retry_unknown_file_returns_404(self):
        status, resp = self._post_json("/api/projects/retry-task",
                                       {"file": "ghost.json", "task": "t1"})
        self.assertEqual(status, 404)
        self.assertIn("error", resp)

    def test_retry_rejects_bad_file_name(self):
        for fname in ("", "nope", "../evil.json", "sub/dir.json"):
            with self.subTest(fname=fname):
                self._assert_rejected(
                    "/api/projects/retry-task",
                    json.dumps({"file": fname, "task": "t1"}).encode("utf-8"),
                    needle="bad file name")

    def test_rejects_non_json_body(self):
        self._assert_rejected("/api/projects/retry-task", b"{nope",
                              needle="invalid JSON")

    def test_rejects_empty_body(self):
        self._assert_rejected("/api/projects/retry-task", b"",
                              needle="body size must be 1 byte..256KB")

    def test_rejects_array_body(self):
        self._assert_rejected("/api/projects/retry-task", b"[1]",
                              needle="JSON body required")

    def test_rejects_oversized_body_without_reading_it(self):
        status, resp, req = self._post_raw(
            "/api/projects/retry-task", b"{}", length=MAX_BODY + 1)
        self.assertEqual(status, 400)
        self.assertIn("body size", resp["error"])
        self.assertEqual(req.reads, [])


class HttpWriteRequestGuard(_PostCase):
    """The distance between "can reach the port" and "can start a run".

    The dashboard listens on every interface with no login, and every POST
    changes state — /api/projects/create even takes a verify_cmd the gate
    later runs as a shell command. Two things were true before this guard:
    a web page on ANY site could POST here from the operator's browser (a
    text/plain body is a CORS "simple request", sent without asking, and the
    handler parsed it as JSON regardless), and any device on the wifi could
    do the same directly. These tests pin the three checks that close that:
    the Content-Type that forces a preflight, the Origin that must be this
    server, and the optional token that gates every action.

    Every refusal must happen BEFORE the body is read or acted on: a refused
    request performs no spawn, writes no file.
    """

    VALID = {"file": "proj.json"}

    def _valid_run(self, **kw):
        """A POST that the run endpoint would otherwise accept."""
        self._write_taskfile()
        return self._post_raw("/api/projects/run", json.dumps(self.VALID).encode("utf-8"), **kw)

    def _assert_untouched(self, req):
        self.assertEqual(req.reads, [], "a refused request must not read the body")
        self.assertEqual(self.spawn_calls, [], "a refused request must not spawn")

    # -- 1. Content-Type ----------------------------------------------------

    def test_missing_content_type_is_refused_before_reading_the_body(self):
        status, resp, req = self._valid_run(headers={"Content-Type": None})
        self.assertEqual(status, 415)
        self.assertIn("application/json", resp["error"])
        self._assert_untouched(req)

    def test_text_plain_body_is_refused_even_when_it_is_json(self):
        # the CSRF shape: a cross-site form/fetch that the browser sends
        # without a preflight — the body is perfectly good JSON
        status, resp, req = self._valid_run(headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self._assert_untouched(req)

    def test_content_type_parameters_and_case_are_tolerated(self):
        for ctype in ("application/json; charset=utf-8", "Application/JSON"):
            with self.subTest(ctype=ctype):
                status, _, _ = self._valid_run(headers={"Content-Type": ctype})
                self.assertEqual(status, 200)

    # -- 2. Origin ----------------------------------------------------------

    def test_foreign_origin_is_refused(self):
        # Host and port are compared; the scheme is not. A page cannot be
        # served from THIS host:port by anyone but this server, so a scheme
        # mismatch is not an attack — and a TLS proxy in front (Tailscale
        # Serve, Caddy) legitimately produces an https Origin for this
        # plain-http backend.
        for origin in ("http://evil.example", "http://10.0.0.153:9999",
                       "http://10.0.0.153", "null"):
            with self.subTest(origin=origin):
                self.spawn_calls.clear()
                status, resp, req = self._valid_run(headers={
                    "Host": "10.0.0.153:8787", "Origin": origin})
                self.assertEqual(status, 403, origin)
                self.assertIn("cross-origin", resp["error"])
                self._assert_untouched(req)

    def test_own_origin_is_accepted(self):
        # what the dashboard's pages send: the origin they were served from
        for host, origin in (("10.0.0.153:8787", "http://10.0.0.153:8787"),
                             ("localhost:8787", "http://localhost:8787"),
                             ("LOCALHOST:8787", "http://localhost:8787")):
            with self.subTest(origin=origin):
                status, _, _ = self._valid_run(headers={"Host": host, "Origin": origin})
                self.assertEqual(status, 200)

    def test_origin_without_a_host_to_compare_against_is_refused(self):
        status, _, req = self._valid_run(headers={"Origin": "http://localhost:8787"})
        self.assertEqual(status, 403)
        self._assert_untouched(req)

    def test_no_origin_header_is_fine(self):
        # curl and scripts send none; that is not a cross-origin request
        status, _, _ = self._valid_run(headers={"Host": "localhost:8787"})
        self.assertEqual(status, 200)

    # -- 3. Token -----------------------------------------------------------

    def _with_token(self, token):
        orig = config.DASHBOARD_TOKEN
        config.DASHBOARD_TOKEN = token
        self.addCleanup(setattr, config, "DASHBOARD_TOKEN", orig)

    def test_no_token_configured_means_no_token_required(self):
        self._with_token("")
        status, _, _ = self._valid_run()
        self.assertEqual(status, 200)

    def test_token_configured_refuses_a_request_without_it(self):
        self._with_token("s3cret")
        status, resp, req = self._valid_run()
        self.assertEqual(status, 401)
        self.assertIn("ARC_DASHBOARD_TOKEN", resp["error"])
        self._assert_untouched(req)

    def test_wrong_or_malformed_token_is_refused(self):
        self._with_token("s3cret")
        for auth in ("Bearer wrong", "Bearer ", "s3cret", "Basic s3cret",
                     "Bearer s3cret-but-longer"):
            with self.subTest(auth=auth):
                status, _, req = self._valid_run(headers={"Authorization": auth})
                self.assertEqual(status, 401, auth)
                self._assert_untouched(req)

    def test_right_token_is_accepted_case_insensitive_scheme(self):
        self._with_token("s3cret")
        for auth in ("Bearer s3cret", "bearer s3cret", "Bearer  s3cret "):
            with self.subTest(auth=auth):
                status, _, _ = self._valid_run(headers={"Authorization": auth})
                self.assertEqual(status, 200, auth)

    def test_every_write_endpoint_is_behind_the_guard(self):
        # the guard runs before routing, so an endpoint added later cannot
        # forget it — pin that for each one that exists today
        self._with_token("s3cret")
        for path in ("/api/projects/create", "/api/projects/run",
                     "/api/projects/stop", "/api/promote",
                     "/api/projects/archive", "/api/projects/retry-task",
                     "/api/does-not-exist"):
            with self.subTest(path=path):
                status, _, req = self._post_raw(path, b"{}")
                self.assertEqual(status, 401, path)
                self._assert_untouched(req)

    def test_get_is_not_gated_by_the_token(self):
        # the pages are meant to be glanced at from a phone without a login
        # step; only actions need the token
        self._with_token("s3cret")
        req = _FakeRequest("/api/health")
        req.do_GET()
        self.assertEqual(req.status, 200)
