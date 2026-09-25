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
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from helpers import ENTRY, capture_events  # noqa: F401  (sys.path)

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

    def _activity_body(self, name, lines, query=""):
        harness = self.tmp / "logs" / "harness"
        (harness / name).write_text("".join(line + "\n" for line in lines), encoding="utf-8")
        dashboard._activity_cache.clear()
        req = self.get(f"/api/transcript?file={name}&view=activity" + query)
        self.assertEqual(req.status, 200)
        return req.json()

    def test_activity_view_reduces_a_reasonix_transcript(self):
        """view=activity answers folded blocks plus the still-open group as
        `pending` — the raw tail of the same file would be useless scraps."""
        body = self._activity_body("rx.jsonl", [
            json.dumps({"kind": "user_message", "messageId": "u1", "text": "do the thing"}),
            json.dumps({"kind": "reasoning", "messageId": "r1", "attemptId": "a1", "text": "let me "}),
            json.dumps({"kind": "reasoning", "messageId": "r1", "attemptId": "a1", "text": "think"}),
            json.dumps({"kind": "tool_dispatch", "tool": {
                "id": "c1", "name": "bash", "args": json.dumps({"command": "ls"})}}),
            json.dumps({"kind": "tool_result", "tool": {
                "runState": "completed", "id": "c1", "name": "bash", "output": "ok"}}),
            json.dumps({"kind": "reasoning", "messageId": "r2", "attemptId": "a1", "text": "pondering"}),
        ])
        self.assertEqual(body["mode"], "activity")
        self.assertEqual(body["total_blocks"], len(body["blocks"]))
        self.assertEqual(body["blocks"], ["▸ do the thing", "💭 let me think",
                                          "🔧 bash ls", "   ↳ ok"])
        self.assertIn("thinking", body["pending"])   # the r2 group is still open
        self.assertIn("pondering", body["pending"])

    def test_activity_view_catches_up_incrementally(self):
        """A second poll after the file grew must see only the new records —
        not re-render what it already served, and never corrupt on a partial
        trailing line."""
        first = self._activity_body("grow.jsonl", [
            json.dumps({"kind": "reasoning", "messageId": "r1", "attemptId": "a1", "text": "one"}),
            json.dumps({"kind": "notice", "text": "n1"}),
        ])
        self.assertEqual(first["blocks"], ["💭 one", "— n1"])
        path = self.tmp / "logs" / "harness" / "grow.jsonl"
        with open(path, "a", encoding="utf-8") as fh:   # the run appends a partial…
            fh.write(json.dumps({"kind": "notice", "text": "n2"})[:20])
        req = self.get("/api/transcript?file=grow.jsonl&view=activity")
        self.assertEqual(req.status, 200)
        self.assertEqual(req.json()["blocks"], ["💭 one", "— n1"])   # …held, not mangled
        with open(path, "a", encoding="utf-8") as fh:   # …then completes it
            fh.write(json.dumps({"kind": "notice", "text": "n2"})[20:] + "\n")
        req = self.get("/api/transcript?file=grow.jsonl&view=activity")
        self.assertEqual(req.status, 200)
        self.assertEqual(req.json()["blocks"], ["💭 one", "— n1", "— n2"])

    def test_activity_view_falls_back_to_raw_for_kimi_shaped_files(self):
        """A kimi-cli transcript is not reducible: the answer is today's raw
        tail, so the drawer renders it exactly as before."""
        body = self._activity_body("kimi.jsonl", [
            json.dumps({"role": "assistant", "content": "hi", "tool_calls": []}),
            json.dumps({"role": "tool", "content": "out"}),
        ])
        self.assertNotIn("mode", body)
        self.assertEqual(body["total_lines"], 2)
        self.assertIn('"role": "assistant"', body["lines"][0])

    def test_activity_view_marks_a_stalled_pending_line(self):
        """An open delta group in a file older than 60 s is not "thinking" —
        the handler annotates it as a possibly stalled/dead stream. A fresh
        file with the same open group gets no annotation (pending() itself
        stays time-pure; only the handler knows the mtime)."""
        harness = self.tmp / "logs" / "harness"
        path = harness / "stalled.jsonl"
        path.write_text(
            json.dumps({"kind": "reasoning", "messageId": "r1", "attemptId": "a1",
                        "text": "deep in thought"}) + "\n", encoding="utf-8")
        old = time.time() - 300
        os.utime(path, (old, old))
        dashboard._activity_cache.clear()
        req = self.get("/api/transcript?file=stalled.jsonl&view=activity")
        self.assertEqual(req.status, 200)
        pending = req.json()["pending"]
        self.assertIn("deep in thought", pending)
        self.assertIn("no new output for 5m", pending)
        self.assertIn("stream may be stalled/dead", pending)
        # Same content, but the file is still being written to: no warning.
        now = time.time()
        os.utime(path, (now, now))
        dashboard._activity_cache.clear()
        req = self.get("/api/transcript?file=stalled.jsonl&view=activity")
        self.assertEqual(req.status, 200)
        pending = req.json()["pending"]
        self.assertIn("deep in thought", pending)
        self.assertNotIn("no new output", pending)

    def test_activity_view_keeps_the_same_guards_as_the_raw_tail(self):
        """The filename guard is the raw path's; view=activity changes nothing."""
        req = self.get("/api/transcript?file=bad:name.jsonl&view=activity")
        self.assert_error(req, 400)
        req = self.get("/api/transcript?file=ghost.jsonl&view=activity")
        self.assert_error(req, 404)

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
        for r in ("1h", "3h", "6h", "today", "24h", "7d", "all"):
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


class HttpParamsPlanProposals(EndpointCase):
    """/api/plan-proposals is the read-only window into the plan_proposals
    table (plan_amend.py's trail of who wanted to change the plan). file= is
    a bare taskfile basename under TASKS_DIR — same allowlist discipline as
    /api/task-diff (AGENTS.md Rule 6b) — and limit= is clamped, never fatal."""

    def setUp(self):
        super().setUp()
        self.st = dashboard.Handler.store

    def _mk(self, taskfile, i=0, action="noted"):
        self.st.save_plan_proposal(taskfile, f"t{i}", "t9", "implementer",
                                   ENTRY, "note", action, "a reason",
                                   '{"kind":"note"}')

    def test_no_file_param_lists_everything(self):
        """The fleet-wide view: every proposal, newest first."""
        self._mk("a.json")
        self._mk("b.json")
        req = self.get("/api/plan-proposals")
        self.assertEqual(req.status, 200)
        self.assertEqual(len(req.json()["proposals"]), 2)

    def test_file_filter_matches_only_that_taskfile(self):
        """The drawer asks for one plan's trail; the filter is exact."""
        a = str((Path(config.TASKS_DIR) / "a.json").resolve())
        self._mk(a)
        self._mk(str((Path(config.TASKS_DIR) / "b.json").resolve()))
        rows = self.get("/api/plan-proposals?file=a.json").json()["proposals"]
        self.assertEqual([r["taskfile"] for r in rows], [a])

    def test_bad_file_name_is_a_400_json_error(self):
        """A wrong file param degrades to machine-readable JSON, never 500."""
        self.assert_error(self.get("/api/plan-proposals?file=noext"), 400)
        self.assert_error(self.get("/api/plan-proposals?file=.."), 400)

    def test_traversal_names_are_rejected(self):
        """file= is one path segment, never a path (Rule 6b)."""
        for name in TRAVERSALS:
            with self.subTest(name=name):
                self.assert_error(self.get(f"/api/plan-proposals?file={name}"),
                                  400)

    def test_limit_clamped_never_fatal(self):
        """Huge, zero and non-numeric limits clamp instead of erroring."""
        for i in range(3):
            self._mk("a.json", i)
        q = "/api/plan-proposals?limit="
        self.assertEqual(len(self.get(q + "2").json()["proposals"]), 2)
        self.assertEqual(len(self.get(q + "1").json()["proposals"]), 1)
        self.assertEqual(len(self.get(q + "0").json()["proposals"]), 1)   # →1
        self.assertEqual(len(self.get(q + "99999").json()["proposals"]), 3)  # →500
        self.assertEqual(len(self.get(q + "bogus").json()["proposals"]), 3)  # →100


class BoardRequest(dashboard.Handler):
    """Socket-less POST/GET for the board routes. do_POST reads Content-Type
    and, when ARC_DASHBOARD_TOKEN is set, Authorization, before the body."""

    def __init__(self, path, body=b"", headers=None):
        self.path = path
        self.status = None
        self.chunks = []
        self.wfile = self
        self.rfile = self
        self._pending = body if isinstance(body, bytes) else body.encode()
        self.headers = {
            "Content-Length": str(len(self._pending)),
            "Content-Type": "application/json",
            "Host": "localhost:8787",
        }
        if headers:
            self.headers.update(headers)

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, name, value):
        pass

    def end_headers(self):
        pass

    def read(self, n):
        data, self._pending = self._pending[:n], self._pending[n:]
        return data

    def write(self, chunk):
        self.chunks.append(chunk)

    def json(self):
        return json.loads(b"".join(self.chunks).decode("utf-8"))


class HttpParamsBoard(EndpointCase):
    """The Messages tab reads and posts through /api/board/*. Names are
    validated and never opened as paths; the operator is the only author a
    POST may record; the token guard on every POST still applies."""

    def setUp(self):
        super().setUp()
        import agentboard
        self.agentboard = agentboard
        self._db = self.tmp / "board.db"
        self._saved_db = config.DB_PATH
        config.DB_PATH = str(self._db)
        agentboard._conns.pop(str(self._db), None)

    def tearDown(self):
        conn = self.agentboard._conns.pop(str(self._db), None)
        if conn is not None:
            conn.close()
        config.DB_PATH = self._saved_db
        super().tearDown()

    def get(self, path):
        req = BoardRequest(path)
        req.do_GET()
        return req

    def post(self, obj, headers=None, path="/api/board/post"):
        req = BoardRequest(path, json.dumps(obj).encode(), headers)
        req.do_POST()
        return req

    def test_bad_project_and_traversal_are_rejected(self):
        self.assert_error(self.get("/api/board/thread"), 400)
        self.assert_error(self.get("/api/board/thread?project=bad name"), 400)
        for name in TRAVERSALS:
            with self.subTest(name=name):
                self.assert_error(self.get(f"/api/board/channels?project={name}"), 400)
                self.assert_error(self.get(f"/api/board/thread?project=ok&channel={name}"), 400)
                req = self.post({"project": name, "channel": "project", "kind": "note", "body": "x"})
                self.assert_error(req, 400)
                self.assertNotIn(b"root:", b"".join(req.chunks))

    def test_kind_must_be_a_board_kind(self):
        req = self.post({"project": "demo", "channel": "project", "kind": "shell", "body": "rm -rf /"})
        self.assert_error(req, 400)
        self.assertEqual(self.agentboard.thread("demo"), [])

    def test_token_guard_blocks_a_post(self):
        orig = config.DASHBOARD_TOKEN
        config.DASHBOARD_TOKEN = "s3cret"
        self.addCleanup(setattr, config, "DASHBOARD_TOKEN", orig)
        req = self.post({"project": "demo", "channel": "project", "kind": "note", "body": "secret"})
        self.assert_error(req, 401)
        self.assertIn("ARC_DASHBOARD_TOKEN", req.json()["error"])
        self.assertEqual(self.agentboard.thread("demo"), [])
        ok = self.post(
            {"project": "demo", "channel": "project", "kind": "note", "body": "hello"},
            headers={"Authorization": "Bearer s3cret"})
        self.assertEqual(ok.status, 200)
        self.assertEqual(self.agentboard.thread("demo")[0]["body"], "hello")

    def test_post_persists_as_operator_and_ignores_path_command_refs(self):
        long_body = "x" * (config.BOARD_BODY_MAX + 25)
        req = self.post({
            "project": "demo", "channel": "task:locks", "kind": "question",
            "body": long_body, "mentions": ["locks", "all"], "reply_to": None,
            "author": "root", "path": "/etc/passwd", "command": "id",
            "refs": {"files": ["/etc/passwd"]}, "verify_cmd": "echo pwned",
        })
        self.assertEqual(req.status, 200)
        self.assertEqual(req.json()["author"], "operator")
        rows = self.agentboard.thread("demo", "task:locks")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["author"], "operator")
        self.assertEqual(rows[0]["kind"], "question")
        self.assertEqual(len(rows[0]["body"]), config.BOARD_BODY_MAX)
        self.assertEqual(rows[0]["mentions"], ["locks", "all"])
        self.assertEqual(rows[0]["refs"], {})
        self.assertFalse((self.tmp / "passwd").exists())
        listed = self.get("/api/board/projects").json()["projects"]
        self.assertEqual(listed[0]["project"], "demo")
        chans = self.get("/api/board/channels?project=demo").json()["channels"]
        self.assertIn("task:locks", [c["channel"] for c in chans])

    def test_read_drops_unread_to_zero(self):
        """Opening a channel marks it read for the operator, so the badge
        counts messages newer than that cursor, not every message ever."""
        self.agentboard.post("demo", author="locks/implementer", channel="project",
                             kind="note", body="please look")
        def unread():
            chans = self.get("/api/board/channels?project=demo").json()["channels"]
            return next(c["unread"] for c in chans if c["channel"] == "project")
        self.assertGreater(unread(), 0)
        ts = self.agentboard.thread("demo", "project")[-1]["ts"]
        bad = self.post({"project": "../../etc/passwd", "channel": "project", "ts": ts},
                        path="/api/board/read")
        self.assert_error(bad, 400)
        self.assertGreater(unread(), 0)
        ok = self.post({"project": "demo", "channel": "project", "ts": ts,
                        "path": "/etc/passwd", "command": "id"},
                       path="/api/board/read")
        self.assertEqual(ok.status, 200)
        self.assertEqual(ok.json()["reader"], "operator")
        self.assertEqual(unread(), 0)


class HttpParamsTimeline(EndpointCase):
    """/api/tasks/<id>/timeline is the one place the events, harness runs,
    errors and evidence of a task are gathered. The id comes straight from
    the URL path and becomes a directory name under logs/evidence and a LIKE
    prefix against harness_runs, so it must be validated before it is used —
    and a malformed id, a taskfile param carrying a separator or a bad path
    on /api/evidence-file must all answer with machine-readable JSON."""

    def setUp(self):
        super().setUp()
        self.evlog = Path(config.EVENTS_LOG)
        self.evlog.parent.mkdir(parents=True, exist_ok=True)
        self.evidence = self.tmp / "logs" / "evidence"
        self._saved_evidence = config.EVIDENCE_DIR
        config.EVIDENCE_DIR = str(self.evidence)
        # error_events lives in the shared temp db helpers.py redirects to, so
        # a row written by one test would otherwise show up in the next one's
        # timeline — the assertions here are about what THIS test recorded.
        import errors
        errors.reset_for_tests()
        errors._db().execute("DELETE FROM error_events")
        self.addCleanup(errors.reset_for_tests)
        (self.evidence / "proj" / "t1" / "x2").mkdir(parents=True)
        (self.evidence / "proj" / "t1" / "x2" / "shot.png").write_bytes(
            b"\x89PNG\r\n\x1a\n")
        (self.evidence / "proj" / "t1" / "x2" / "manifest.json").write_text(
            json.dumps({"seconds": 3, "shots": [
                str(self.evidence / "proj" / "t1" / "x2" / "shot.png"),
                "/etc/passwd"]}), encoding="utf-8")
        (self.evidence / "proj" / "other" / "x1").mkdir(parents=True)
        (self.evidence / "proj" / "other" / "x1" / "manifest.json").write_text(
            json.dumps({"shots": []}), encoding="utf-8")

    def tearDown(self):
        config.EVIDENCE_DIR = self._saved_evidence
        dashboard._timeline_cache.update(key=None, value=None)
        dashboard._lines_cache["key"] = None
        super().tearDown()

    def _events(self, events):
        """Write the log in append order, as the real writer does."""
        self.evlog.write_text("".join(json.dumps(e) + "\n" for e in events),
                              encoding="utf-8")
        dashboard._timeline_cache.update(key=None, value=None)

    def _entries(self, tid="t1"):
        """The timeline entries for one task, through the real route."""
        req = self.get(f"/api/tasks/{tid}/timeline")
        self.assertEqual(req.status, 200)
        return req.json()["entries"]

    def test_timeline_returns_the_task_entries_in_time_order(self):
        """The whole point: one list, oldest first, whatever the source."""
        self._events([
            {"type": "driver.start", "task": "t1", "ts": 100.0, "model": "GLM-5.3"},
            {"type": "task.gate", "task": "t1", "ts": 120.0, "tail": "boom"},
            {"type": "driver.done", "task": "t1-x2", "ts": 140.0, "verdict": "pass"},
        ])
        req = self.get("/api/tasks/t1/timeline")
        self.assertEqual(req.status, 200)
        body = req.json()
        self.assertEqual(body["id"], "t1")
        self.assertEqual([e["type"] for e in body["entries"]
                          if e["kind"] == "event"],
                         ["driver.start", "task.gate", "driver.done"])
        # The contract is time order across ALL sources, not just the events:
        # the evidence manifest and any harness run must be interleaved too.
        stamps = [e["ts"] for e in body["entries"] if e["ts"] is not None]
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(body["counts"]["events"], 3)
        self.assertEqual(body["counts"]["evidence"], 1)

    def test_events_of_other_tasks_are_excluded(self):
        """A sibling task, a task id this one is a PREFIX of, and a
        non-lifecycle event type all stay off this timeline."""
        self._events([
            {"type": "driver.start", "task": "t1", "ts": 100.0},
            {"type": "driver.start", "task": "t10", "ts": 110.0},   # prefix, not an attempt
            {"type": "driver.start", "task": "other", "ts": 120.0},
            {"type": "heartbeat", "task": "t1", "ts": 130.0},       # not a family
            {"type": "chain.wait", "task": "t1", "ts": 140.0},      # chain.* IS a family
        ])
        tasks = [e["task"] for e in
                 self.get("/api/tasks/t1/timeline").json()["entries"]]
        self.assertNotIn("t10", tasks)
        self.assertNotIn("other", tasks)
        types = [e["type"] for e in
                 self.get("/api/tasks/t1/timeline").json()["entries"]]
        self.assertNotIn("heartbeat", types)
        self.assertIn("chain.wait", types)

    def test_gate_output_tail_and_review_issues_are_inlined(self):
        """The entry carries the WHY — the gate's tail, the review's issues —
        so the page does not have to render a raw JSON blob."""
        self._events([
            {"type": "task.gate", "task": "t1", "ts": 100.0, "passed": False,
             "tail": "FAIL: unit tests"},
            {"type": "task.reviewed", "task": "t1", "ts": 110.0, "passed": False,
             "issues": ["no test for the new route"]},
        ])
        entries = self.get("/api/tasks/t1/timeline").json()["entries"]
        gate, rev = entries[0], entries[1]
        self.assertEqual(gate["tail"], "FAIL: unit tests")
        self.assertEqual(gate["body"], "FAIL: unit tests")
        self.assertEqual(rev["issues"], ["no test for the new route"])
        self.assertEqual(rev["body"], ["no test for the new route"])

    def test_harness_runs_and_transcripts_are_listed(self):
        """A run row reaches the timeline with the transcript basename the
        drawer opens — and an attempt row (`<id>-x2`) belongs to the task."""
        self._events([])
        store_ = dashboard.Handler.store
        store_.save_harness_run("t1-x2", "opencode", "GLM-5.3", "implementer",
                                2, 0, str(self.tmp / "logs" / "harness" /
                                          "t1-x2-implementer.jsonl"), 12.5,
                                "pass")
        store_.save_harness_run("other", "opencode", "GLM-5.3", "implementer",
                                1, 0, "/tmp/nope.jsonl", 1.0)
        entries = self.get("/api/tasks/t1/timeline").json()["entries"]
        runs = [e for e in entries if e["kind"] == "run"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["file"], "t1-x2-implementer.jsonl")
        self.assertEqual(runs[0]["attempt"], 2)
        self.assertEqual(runs[0]["verdict"], "pass")

    def test_a_harness_run_is_timestamped_and_interleaves_with_events(self):
        """A run row must carry its real time.

        It did not: `harness_runs_prefix` did not SELECT `created_at`, so
        every run arrived with `ts: None`, rendered as '—', and sorted to the
        HEAD of the timeline as an undated entry instead of appearing where it
        happened between the events around it."""
        self._events([
            {"type": "driver.start", "task": "t1", "ts": 100.0},
            {"type": "task.merged", "task": "t1", "ts": 9e9},   # far future
        ])
        store_ = dashboard.Handler.store
        store_.save_harness_run("t1-x2", "opencode", "GLM-5.3", "implementer",
                                2, 0, str(self.tmp / "logs" / "harness" /
                                          "t1-x2.jsonl"), 12.5, "pass")
        entries = self.get("/api/tasks/t1/timeline").json()["entries"]
        run = [e for e in entries if e["kind"] == "run"][0]
        self.assertIsNotNone(run["ts"], "a harness run must have a timestamp")
        # Its epoch is 'now', which lies between the 1970 event and the far
        # future one — so it must sort BETWEEN them, not at the head. (The
        # setUp fixture's evidence manifest is stamped 'now' too, hence the
        # position check rather than an exact list.)
        kinds = [e["kind"] for e in entries]
        self.assertNotEqual(kinds[0], "run",
                            "an undated run sorted to the head of the timeline")
        self.assertLess(kinds.index("run"), kinds.index("event", 1),
                        "the run must sort before the far-future event")
        stamps = [e["ts"] for e in entries]
        self.assertEqual(stamps, sorted(stamps))

    def test_a_sibling_task_on_the_same_prefix_is_excluded(self):
        """`t1` and `t1-sibling` are DIFFERENT tasks: ids are hyphenated
        English, so an earlier any-hyphen-suffix match leaked a sibling's
        events and runs onto this timeline."""
        self._events([
            {"type": "driver.start", "task": "t1", "ts": 100.0},
            {"type": "driver.start", "task": "t1-sibling", "ts": 110.0},
            {"type": "task.merged", "task": "t1-sibling", "ts": 120.0},
            {"type": "driver.start", "task": "evidence-scene-stats", "ts": 130.0},
        ])
        store_ = dashboard.Handler.store
        store_.save_harness_run("t1-sibling-x2", "opencode", "GLM-5.3",
                                "implementer", 2, 0, "/tmp/sib.jsonl", 3.0)
        store_.save_harness_run("t1-x2", "opencode", "GLM-5.3", "implementer",
                                2, 0, "/tmp/mine.jsonl", 3.0)
        entries = self.get("/api/tasks/t1/timeline").json()["entries"]
        self.assertEqual([e["task"] for e in entries if e["kind"] == "event"],
                         ["t1"])
        self.assertEqual([e["file"] for e in entries if e["kind"] == "run"],
                         ["mine.jsonl"])

    def test_error_events_carry_their_fingerprint(self):
        """error_events rows are the triage view (Rule 7b): the fingerprint
        must survive, because it is the identity of the defect."""
        self._events([])
        import errors
        errors.reset_for_tests()
        try:
            raise ValueError("synthetic timeline defect")
        except ValueError as exc:
            errors.capture(exc, task="t1-x3", node="gate_t1")
        entries = self.get("/api/tasks/t1/timeline").json()["entries"]
        errs = [e for e in entries if e["kind"] == "error"]
        self.assertEqual(len(errs), 1)
        self.assertTrue(errs[0]["fingerprint"])
        self.assertEqual(errs[0]["message"], "synthetic timeline defect")
        self.assertEqual(errs[0]["node"], "gate_t1")

    def test_evidence_manifests_are_listed_with_shot_urls(self):
        """A manifest's shots become /api/evidence-file URLs — and a path
        outside the evidence root (here /etc/passwd) gets none."""
        self._events([])
        entries = self.get("/api/tasks/t1/timeline").json()["entries"]
        ev = [e for e in entries if e["kind"] == "evidence"]
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["attempt"], "x2")
        self.assertEqual(ev[0]["project"], "proj")
        urls = [s["url"] for s in ev[0]["shots"]]
        self.assertEqual(len(urls), 1)             # /etc/passwd is not served
        self.assertTrue(urls[0].startswith("/api/evidence-file?path="))
        self.assertNotIn("passwd", urls[0])

    def test_evidence_is_found_in_a_project_past_the_old_64_dir_cap(self):
        """Every project directory is searched, not the first 64 by name.

        The walk used to stop at 64 directories, so a task whose project sorts
        later (here `zzz-late`) had no manifests — and an absent manifest is
        indistinguishable from a run that captured none, so the drawer said
        "no evidence" for a task that had some."""
        self._events([])
        for i in range(70):                    # all sort before zzz-late
            (self.evidence / f"aaa-{i:03d}").mkdir(parents=True, exist_ok=True)
        late = self.evidence / "zzz-late" / "t1" / "x7"
        late.mkdir(parents=True, exist_ok=True)
        (late / "late.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (late / "manifest.json").write_text(
            json.dumps({"shots": [str(late / "late.png")]}), encoding="utf-8")
        entries = self.get("/api/tasks/t1/timeline").json()["entries"]
        ev = [e for e in entries if e["kind"] == "evidence"]
        self.assertIn("zzz-late", [e["project"] for e in ev],
                      "a project sorting past 64 other directories was skipped")
        late_shots = [s for e in ev if e["project"] == "zzz-late"
                      for s in e["shots"]]
        self.assertEqual([s["name"] for s in late_shots], ["late.png"])

    def test_evidence_file_serves_a_shot(self):
        """The thumbnail the drawer renders is one GET away."""
        req = self.get("/api/evidence-file?path=proj/t1/x2/shot.png")
        self.assertEqual(req.status, 200)
        self.assertTrue(b"".join(req.chunks).startswith(b"\x89PNG"))

    def test_evidence_file_rejects_traversal_and_outside_paths(self):
        """'..', an absolute path and a root escape are refused before any
        byte is read."""
        for bad in ("../../etc/passwd", "/etc/passwd", "proj/../../../etc/hosts",
                    "..%2f..%2fetc%2fpasswd"):
            with self.subTest(path=bad):
                req = self.get(f"/api/evidence-file?path={bad}")
                self.assertIn(req.status, (400, 404))
                self.assertNotIn(b"root:", b"".join(req.chunks))
                self.assertIsInstance(req.json().get("error"), str)

    def test_evidence_file_needs_a_path(self):
        """No ?path= is a 400, not a directory listing."""
        self.assert_error(self.get("/api/evidence-file"), 400)
        self.assert_error(self.get("/api/evidence-file?path="), 400)

    def test_bad_task_id_is_a_400_json_error(self):
        """The id becomes a path segment and a LIKE prefix, so it is
        validated: a traversal, a glob, a wildcard or a space is refused
        before it is used for anything."""
        for bad in ("..", "..%2f..", "t%25", "t*", "a b", "%20", ".", "..."):
            with self.subTest(id=bad):
                self.assert_error(self.get(f"/api/tasks/{bad}/timeline"), 400)

    def test_a_separator_in_the_id_never_reaches_a_path(self):
        """`/` in the path does not match the route at all — it is a 404 with
        a JSON body, and no evidence file is read on the way."""
        req = self.get("/api/tasks/a/b/timeline")
        self.assertEqual(req.status, 404)
        self.assertIsInstance(req.json().get("error"), str)

    def test_a_traversal_id_never_reads_outside_the_evidence_root(self):
        """A rejected id must not have been used in a path at all."""
        (self.tmp / "secret").mkdir(exist_ok=True)
        (self.tmp / "secret" / "manifest.json").write_text(
            json.dumps({"shots": ["/etc/passwd"]}), encoding="utf-8")
        req = self.get("/api/tasks/..%2f..%2fsecret/timeline")
        self.assert_error(req, 400)
        self.assertNotIn(b"passwd", b"".join(req.chunks))

    def test_bad_taskfile_param_is_a_400_json_error(self):
        """?taskfile= is a bare .json basename under TASKS_DIR, like the other
        read endpoints — never a path."""
        for name in TRAVERSALS:
            with self.subTest(name=name):
                self.assert_error(
                    self.get(f"/api/tasks/t1/timeline?taskfile={name}"), 400)
        self.assert_error(
            self.get("/api/tasks/t1/timeline?taskfile=noext"), 400)

    def test_missing_task_is_an_empty_timeline_not_an_error(self):
        """A task with nothing recorded answers with an empty list: the
        drawer must open, not show a traceback."""
        self._events([])
        body = self.get("/api/tasks/never-ran/timeline").json()
        self.assertEqual(body["entries"], [])
        self.assertEqual(body["counts"]["events"], 0)
        self.assertEqual(body["counts"]["runs"], 0)

    def test_the_scan_is_bounded_and_says_so(self):
        """A 100 MiB log must not stall the server: older entries beyond the
        scan budget are not examined, and `truncated` reports that the view is
        partial instead of silently passing off a cut-off history."""
        self._events([{"type": "driver.start", "task": "t1", "ts": float(i)}
                      for i in range(400)])
        req = self.get("/api/tasks/t1/timeline")
        self.assertEqual(req.status, 200)
        body = req.json()
        self.assertLessEqual(body["scanned"], 20000)
        self.assertEqual(body["scanned"], 400)
        self.assertFalse(body["truncated"])

    def test_the_event_kept_bound_is_a_scan_bound(self):
        """The backward scan stops once it holds `max_events` matches — it
        does not walk the whole file to answer one task."""
        self._events([{"type": "driver.start", "task": "t1", "ts": float(i)}
                      for i in range(300)])
        saved = dashboard.TIMELINE_MAX_EVENTS
        dashboard.TIMELINE_MAX_EVENTS = 10
        dashboard._timeline_cache.update(key=None, value=None)
        try:
            out = dashboard._timeline_events("t1")
        finally:
            dashboard.TIMELINE_MAX_EVENTS = saved
            dashboard._timeline_cache.update(key=None, value=None)
        self.assertEqual(len(out["events"]), 10)
        self.assertTrue(out["truncated"])
        self.assertLessEqual(out["scanned"], 11)
        # ...and the newest are the ones kept, since the scan walks backwards
        self.assertEqual(out["events"][-1]["ts"], 299.0)

    def test_events_are_read_from_a_tail_window_not_the_whole_file(self):
        """The reader seeks to a bounded window: a huge log yields a partial
        view and says `truncated`, and the scan stays within the budget."""
        self._events([{"type": "driver.start", "task": "t1", "ts": float(i)}
                      for i in range(5000)])
        out = dashboard._timeline_events("t1", scan_lines=50)
        self.assertLessEqual(out["scanned"], 50)
        self.assertTrue(out["truncated"])
        self.assertGreater(len(out["events"]), 0)

    def test_the_cache_pair_is_read_and_written_under_a_lock(self):
        """key and value must be read/written as ONE snapshot.

        Dashboard.Handler runs on a ThreadingHTTPServer, so an unlocked
        check-then-read let two overlapping GETs interleave as "key matches
        for task A, value already replaced by task B" — one task's timeline
        came back holding another task's events.

        Asserted by recording the lock state at every cache access rather
        than by racing threads: a timing-based version of this test PASSED
        against the unlocked code, so it proved nothing."""
        self._events([{"type": "driver.start", "task": "ta", "ts": 1.0, "note": "A"}])
        accesses = []
        real = dashboard._timeline_cache

        class Watched(dict):
            def __getitem__(self, k):
                accesses.append(("read", dashboard._timeline_cache_lock.locked()))
                return super().__getitem__(k)

            def __setitem__(self, k, v):
                accesses.append(("write", dashboard._timeline_cache_lock.locked()))
                super().__setitem__(k, v)

        watched = Watched(real)
        dashboard._timeline_cache = watched
        try:
            dashboard._timeline_events("ta")      # miss: scans, then publishes
            dashboard._timeline_events("ta")      # hit: reads the pair back
        finally:
            dashboard._timeline_cache = real
        self.assertTrue(accesses, "the cache was never touched")
        unlocked = [op for op, held in accesses if not held]
        self.assertEqual(
            unlocked, [],
            f"cache {unlocked} happened outside the lock — key/value is not atomic")

    def test_a_cache_hit_returns_this_tasks_value_not_the_last_one(self):
        """A hit is answered from the value belonging to the key it matched."""
        self._events([{"type": "driver.start", "task": "ta", "ts": 1.0, "note": "A"}])
        first = dashboard._timeline_events("ta")
        self.assertEqual([e["note"] for e in first["events"]], ["A"])
        hit = dashboard._timeline_events("ta")
        self.assertEqual([e["note"] for e in hit["events"]], ["A"])
        self.assertIs(hit, first, "the hit must be the value stored for this key")
        # A DIFFERENT task must never be answered from the previous key's value.
        self._events([{"type": "driver.start", "task": "tb", "ts": 1.0, "note": "B"}])
        self.assertEqual([e["note"] for e in
                          dashboard._timeline_events("tb")["events"]], ["B"])

    def test_the_cache_holds_key_and_value_from_the_same_scan(self):
        """A hit returns the value belonging to the key it matched."""
        self._events([{"type": "driver.start", "task": "ta", "ts": 1.0, "note": "A"}])
        first = dashboard._timeline_events("ta")
        self.assertEqual(first["events"][0]["note"], "A")
        hit = dashboard._timeline_events("ta")            # served from cache
        self.assertEqual(hit["events"][0]["note"], "A")
        self.assertIs(hit, first, "the cached value must be the one stored for this key")
