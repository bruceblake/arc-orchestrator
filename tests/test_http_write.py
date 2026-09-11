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

    def __init__(self, path, body=b"", length=None):
        self.status = None
        self.response_headers = {}
        self.body = b""
        self.wfile = self
        self.rfile = self
        self.path = path
        self._pending = body
        self.reads = []
        self.headers = {
            "Content-Length": str(len(body) if length is None else length)}

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
        return mock.Mock(pid=4242), log_name

    def _post(self, path, body, length=None):
        """POST raw bytes; returns (status, parsed JSON body)."""
        req = _FakeRequest(path, body, length=length)
        req.do_POST()
        return req.status, json.loads(req.body.decode("utf-8"))

    def _post_json(self, path, obj):
        return self._post(path, json.dumps(obj).encode("utf-8"))

    def _post_raw(self, path, body, length=None):
        """Like _post but also hands back the request (for read tracking)."""
        req = _FakeRequest(path, body, length=length)
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
        self.assertEqual(t1["model"], "DeepSeek-V4-Flash")
        self.assertEqual(t1["reviewer"], "kimi")
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
            str(path), "t1", "one", "DeepSeek-V4-Flash", "kimi", "failed")
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
        store.upsert_code_task(str(path), "t1", "one", "DeepSeek-V4-Flash",
                               "kimi", "failed")
        store.upsert_code_task(str(path), "t2", "two", "DeepSeek-V4-Flash",
                               "kimi", "merged")
        status, resp = self._post_json("/api/projects/retry-task",
                                       {"file": "proj.json", "task": "t1"})
        self.assertEqual(status, 200)
        self.assertEqual(resp, {"file": "proj.json", "task": "t1",
                                "status": "pending"})
        rows = {r["id"]: r["status"] for r in store.code_tasks_all()
                if r["taskfile"] == str(path)}
        self.assertEqual(rows, {"t1": "pending", "t2": "merged"})

    def test_retry_unknown_task_id_returns_404(self):
        path = self._write_taskfile()
        dashboard.Handler.store.upsert_code_task(
            str(path), "t1", "one", "DeepSeek-V4-Flash", "kimi", "failed")
        status, resp = self._post_json("/api/projects/retry-task",
                                       {"file": "proj.json", "task": "ghost"})
        self.assertEqual(status, 404)
        self.assertIn("task id not found", resp["error"])

    def test_retry_conflicts_while_project_is_running(self):
        path = self._write_taskfile()
        # the row must exist: the 404 lookup runs before the live check
        dashboard.Handler.store.upsert_code_task(
            str(path), "t1", "one", "DeepSeek-V4-Flash", "kimi", "failed")
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