"""HTTP contract for the parameterised read endpoints.

GET /api/project, /api/task-diff, /api/gate-log and /api/transcript take a
file name straight from the query string and use it to open something on
disk — a taskfile under ~/tasks, a gate log or harness transcript under the
repo's logs/ — and /api/task-diff goes on to grep the project's repo. The
console renders whatever JSON comes back, so a malformed or missing
parameter must answer with a small JSON error and the right status code —
never a traceback page or a 500 — and a name carrying a path separator must
be refused before anything is opened. /api/usage's ?range= drives the usage
page's picker and must accept every documented value and fall back safely
on anything else.

The tests drive dashboard.Handler.do_GET directly on a socket-less request
so the whole handler path runs without a server.
"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import capture_events  # noqa: F401  (sys.path)

import config
import dashboard
import store

TRAVERSALS = ("../../etc/passwd", "..%2f..%2fetc%2fpasswd", "/etc/passwd")


class FakeRequest(dashboard.Handler):
    """A socket-less request: do_GET writes into memory instead of a socket."""

    def __init__(self, path):
        self.path = path
        self.status = None
        self.chunks = []
        self.wfile = self

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, name, value):
        pass

    def end_headers(self):
        pass

    def write(self, chunk):
        self.chunks.append(chunk)

    def json(self):
        return json.loads(b"".join(self.chunks).decode("utf-8"))


class EndpointCase(unittest.TestCase):
    """Points TASKS_DIR, EVENTS_LOG and ROOT at a temp dir and Handler.store
    at an in-memory db so no request here reads or writes operator data, and
    stubs the kimi session scan so usage counts do not depend on whatever
    happens to be running on the account."""

    endpoint = None

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.tmp = Path(self._dir.name)
        self._saved = (config.TASKS_DIR, config.EVENTS_LOG, config.ROOT)
        config.TASKS_DIR = str(self.tmp / "tasks")
        config.EVENTS_LOG = str(self.tmp / "events.jsonl")
        config.ROOT = self.tmp
        self._saved_store = dashboard.Handler.store
        dashboard.Handler.store = store.Store(":memory:")
        self._saved_kimi = dashboard._kimi_code_usage
        dashboard._kimi_code_usage = lambda now, fleet_names=frozenset(): {
            "models": [], "inflight": [], "points": []}
        dashboard._lines_cache["key"] = None
        dashboard._fleet_names_cache.update(key=0.0, names=frozenset())

    def tearDown(self):
        # Store has no close(); close the connection directly so 28
        # in-memory dbs per run do not pile up as ResourceWarnings.
        try:
            dashboard.Handler.store.conn.close()
        except Exception:
            pass
        dashboard._kimi_code_usage = self._saved_kimi
        dashboard.Handler.store = self._saved_store
        config.TASKS_DIR, config.EVENTS_LOG, config.ROOT = self._saved
        dashboard._lines_cache["key"] = None
        dashboard._fleet_names_cache.update(key=0.0, names=frozenset())

    def get(self, path):
        req = FakeRequest(path)
        req.do_GET()
        return req

    def assert_error(self, req, code):
        """A rejection must be a JSON error string at the expected status."""
        self.assertEqual(req.status, code)
        body = req.json()
        self.assertIsInstance(body.get("error"), str)
        self.assertTrue(body["error"])
        return body

    def assert_traversal_rejected(self):
        """The file param is a single path segment: every separator-carrying
        form must die at the name regex with a 400 and must not serve any
        byte of the file it names."""
        for name in TRAVERSALS:
            with self.subTest(name=name):
                req = self.get(f"{self.endpoint}?file={name}")
                self.assert_error(req, 400)
                self.assertNotIn(b"root:", b"".join(req.chunks))


class HttpParamsProject(EndpointCase):
    """/api/project?file= feeds the console's project drawer, and the name
    is used as a path segment under the taskfile dir — so it must be a bare
    .json basename and every failure must stay machine-readable JSON."""

    endpoint = "/api/project"

    def setUp(self):
        super().setUp()
        Path(config.TASKS_DIR).mkdir(parents=True)
        (Path(config.TASKS_DIR) / "proj.json").write_text(json.dumps(
            {"project": {"title": "Demo",
                         "tasks": [{"id": "t1", "title": "One"}]}}),
            encoding="utf-8")

    def test_existing_file_returns_the_project_payload(self):
        """The drawer renders title, task list, run rows and repo state."""
        req = self.get("/api/project?file=proj.json")
        self.assertEqual(req.status, 200)
        body = req.json()
        self.assertEqual(body["file"], "proj.json")
        self.assertEqual(body["title"], "Demo")
        self.assertEqual([t["id"] for t in body["tasks"]], ["t1"])
        self.assertEqual(body["rows"], [])
        self.assertIn("runs", body)
        self.assertEqual(body["events"], [])
        self.assertIsInstance(body["git"], dict)

    def test_missing_file_param_is_a_400_json_error(self):
        """No ?file= reaches the handler as '' — the regex must refuse it."""
        req = self.get("/api/project")
        self.assert_error(req, 400)

    def test_name_failing_the_regex_is_a_400_json_error(self):
        """":" is outside the name regex, so a bad name dies before any
        path join."""
        req = self.get("/api/project?file=bad:name.json")
        self.assert_error(req, 400)

    def test_absent_file_is_a_404_json_error(self):
        """A name that parses but does not exist is a plain 404, not a leak."""
        req = self.get("/api/project?file=ghost.json")
        self.assert_error(req, 404)

    def test_unparsable_file_is_a_400_json_error(self):
        """A corrupt taskfile must not take the drawer down with a 500."""
        (Path(config.TASKS_DIR) / "bad.json").write_text("{not json",
                                                         encoding="utf-8")
        req = self.get("/api/project?file=bad.json")
        self.assert_error(req, 400)

    def test_traversal_names_are_rejected_without_leaking_files(self):
        """A file param is one path segment, never a path."""
        self.assert_traversal_rejected()


class HttpParamsTaskDiff(EndpointCase):
    """/api/task-diff?file=&task= opens the project's repo and greps its
    history, so the file name must be a bare .json basename and the task id
    must stay a short token — both are attacker-controlled strings here."""

    endpoint = "/api/task-diff"

    def setUp(self):
        super().setUp()
        Path(config.TASKS_DIR).mkdir(parents=True)
        # _valid_repo only passes repos under config.REPO_ROOT, so the fence
        # is pointed at this test's temp dir and the throwaway repo lives
        # inside it, to exercise the real git lookup. (It used to live under
        # the operator's home and these tests skipped on every other machine.)
        orig_root = config.REPO_ROOT
        config.REPO_ROOT = str(self.tmp / "repos")
        Path(config.REPO_ROOT).mkdir(parents=True)
        self.addCleanup(setattr, config, "REPO_ROOT", orig_root)
        self.repo = Path(tempfile.mkdtemp(prefix="qa-http-params-repo-",
                                          dir=config.REPO_ROOT))
        (Path(config.TASKS_DIR) / "proj.json").write_text(json.dumps(
            {"project": {"repo": str(self.repo), "tasks": []}}),
            encoding="utf-8")

    def _git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                       capture_output=True, timeout=30)

    def _commit_task(self):
        self._git("init", "-q")
        self._git("config", "user.email", "qa@example.com")
        self._git("config", "user.name", "QA")
        (self.repo / "f.txt").write_text("hello\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "task(t1): add f.txt")

    def test_merged_task_returns_its_deliverable(self):
        """publish tags commits "task(<id>):" — the diff endpoint is that
        commit's files and summary."""
        self._commit_task()
        req = self.get("/api/task-diff?file=proj.json&task=t1")
        self.assertEqual(req.status, 200)
        body = req.json()
        self.assertTrue(body["found"])
        self.assertEqual(body["task"], "t1")
        self.assertEqual(body["subject"], "task(t1): add f.txt")
        self.assertEqual([f["path"] for f in body["files"]], ["f.txt"])
        self.assertNotIn("error", body)

    def test_task_without_a_commit_reports_not_found_shape(self):
        """A task that never reached publish answers found:false with a
        reason, not a 404 — the drawer shows the reason inline."""
        self._commit_task()
        req = self.get("/api/task-diff?file=proj.json&task=ghost")
        self.assertEqual(req.status, 200)
        body = req.json()
        self.assertFalse(body["found"])
        self.assertIn("reason", body)

    def test_missing_task_param_is_an_error_not_a_crash(self):
        """No ?task= reaches the lookup as '' — must stay JSON, not a 500."""
        self._commit_task()
        req = self.get("/api/task-diff?file=proj.json")
        self.assertNotEqual(req.status, 500)
        self.assertTrue(req.json().get("error"))

    def test_missing_file_param_is_a_400_json_error(self):
        """No ?file= reaches the handler as '' — the regex must refuse it."""
        req = self.get("/api/task-diff")
        self.assert_error(req, 400)

    def test_name_failing_the_regex_is_a_400_json_error(self):
        """":" is outside the name regex, so a bad name dies before any
        path join."""
        req = self.get("/api/task-diff?file=bad:name.json&task=t1")
        self.assert_error(req, 400)

    def test_absent_file_is_a_404_json_error(self):
        """A taskfile that parses but does not exist is a plain 404."""
        req = self.get("/api/task-diff?file=ghost.json&task=t1")
        self.assert_error(req, 404)

    def test_traversal_names_are_rejected_without_leaking_files(self):
        """A file param is one path segment, never a path."""
        self.assert_traversal_rejected()


class HttpParamsGateLog(EndpointCase):
    """/api/gate-log?file= tails logs/gates/<name> under the repo root into
    the drawer, so only a bare .log basename may pass and every failure must
    stay machine-readable JSON."""

    endpoint = "/api/gate-log"

    def setUp(self):
        super().setUp()
        gates = self.tmp / "logs" / "gates"
        gates.mkdir(parents=True)
        (gates / "g1.log").write_text("line-1\nline-2\nline-3\n",
                                      encoding="utf-8")

    def test_existing_log_returns_the_tail_payload(self):
        """The drawer renders the tail lines verbatim."""
        req = self.get("/api/gate-log?file=g1.log")
        self.assertEqual(req.status, 200)
        body = req.json()
        self.assertEqual(body["file"], "g1.log")
        self.assertEqual(body["total_lines"], 3)
        self.assertEqual(body["lines"], ["line-1", "line-2", "line-3"])

    def test_missing_file_param_is_a_400_json_error(self):
        """No ?file= reaches the handler as '' — the regex must refuse it."""
        req = self.get("/api/gate-log")
        self.assert_error(req, 400)

    def test_name_failing_the_regex_is_a_400_json_error(self):
        """":" is outside the name regex, so a bad name dies before any
        path join."""
        req = self.get("/api/gate-log?file=bad:name.log")
        self.assert_error(req, 400)

    def test_absent_log_is_a_404_json_error(self):
        """An attempt with no gate log is a plain 404 with a message that
        says why."""
        req = self.get("/api/gate-log?file=ghost.log")
        body = self.assert_error(req, 404)
        self.assertIn("no gate log", body["error"])

    def test_traversal_names_are_rejected_without_leaking_files(self):
        """A file param is one path segment, never a path."""
        self.assert_traversal_rejected()


class HttpParamsTranscript(EndpointCase):
    """/api/transcript?file= streams a harness transcript from
    logs/harness/<name> into the live drawer — a bare .jsonl basename only,
    and the tail cap must clamp, never error."""

    endpoint = "/api/transcript"

    def setUp(self):
        super().setUp()
        harness = self.tmp / "logs" / "harness"
        harness.mkdir(parents=True)
        self.lines = [json.dumps({"n": i}) for i in (1, 2, 3)]
        (harness / "t1-implementer-x1.jsonl").write_text(
            "".join(line + "\n" for line in self.lines), encoding="utf-8")

    def test_existing_transcript_returns_the_tail_lines(self):
        """The drawer renders the whole (bounded) transcript."""
        req = self.get("/api/transcript?file=t1-implementer-x1.jsonl")
        self.assertEqual(req.status, 200)
        body = req.json()
        self.assertEqual(body["file"], "t1-implementer-x1.jsonl")
        self.assertEqual(body["total_lines"], 3)
        self.assertEqual(body["lines"], self.lines)

    def test_tail_parameter_limits_the_lines_returned(self):
        """The drawer asks for the last N lines; the answer is that suffix."""
        req = self.get("/api/transcript?file=t1-implementer-x1.jsonl&tail=2")
        self.assertEqual(req.status, 200)
        self.assertEqual(req.json()["lines"], self.lines[-2:])

    def test_missing_file_param_is_a_400_json_error(self):
        """No ?file= reaches the handler as '' — the regex must refuse it."""
        req = self.get("/api/transcript")
        self.assert_error(req, 400)

    def test_name_failing_the_regex_is_a_400_json_error(self):
        """":" is outside the name regex, so a bad name dies before any
        path join."""
        req = self.get("/api/transcript?file=bad:name.jsonl")
        self.assert_error(req, 400)

    def test_absent_transcript_is_a_404_json_error(self):
        """A run with no transcript on disk is a plain 404."""
        req = self.get("/api/transcript?file=ghost.jsonl")
        self.assert_error(req, 404)

    def test_traversal_names_are_rejected_without_leaking_files(self):
        """A file param is one path segment, never a path."""
        self.assert_traversal_rejected()


class HttpParamsUsage(EndpointCase):
    """/api/usage?range= backs the usage page's range picker: every value
    the UI offers must be accepted, and anything else must fall back to the
    default window instead of erroring."""

    def test_missing_range_defaults_to_one_hour(self):
        """Bare /api/usage must answer the default window, not an error."""
        req = self.get("/api/usage")
        self.assertEqual(req.status, 200)
        self.assertEqual(req.json()["range"], "1h")

    def test_documented_ranges_are_accepted(self):
        """Every value the picker offers must round-trip."""
        for r in ("1h", "24h", "7d", "all"):
            with self.subTest(range=r):
                req = self.get(f"/api/usage?range={r}")
                self.assertEqual(req.status, 200)
                self.assertEqual(req.json()["range"], r)

    def test_unknown_range_falls_back_to_the_default(self):
        """A stray value (stale link, typo) must degrade to 1h, not a 500."""
        req = self.get("/api/usage?range=bogus")
        self.assertEqual(req.status, 200)
        self.assertEqual(req.json()["range"], "1h")

    def test_payload_keeps_the_shape_the_pages_render(self):
        """The usage pages index models/families/totals/inflight directly."""
        body = self.get("/api/usage?range=24h").json()
        self.assertIsInstance(body["models"], list)
        self.assertIsInstance(body["families"], list)
        self.assertIsInstance(body["totals"], dict)
        self.assertIn("inflight", body)