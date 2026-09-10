"""HTTP contract tests for the core read endpoints.

These endpoints drive the operator's dashboard — the one live screen watched
mid-run. Pinning the status code and the JSON keys the console actually reads
catches a silent contract break: if a key disappears, or `progress` and
`task_progress` collapse back into one field, the operator sees a blank or
misleading screen even though nothing raised. Driving dashboard.Handler
directly keeps the checks off a real socket and off the operator's data.
"""
import json
import tempfile
import unittest
from pathlib import Path

from helpers import capture_events  # noqa: F401  (sys.path + event redirect)

import config
import dashboard
from store import Store


class _FakeHandler(dashboard.Handler):
    """A socket-less Handler that captures status/headers/body in memory.

    BaseHTTPRequestHandler.__init__ requires a live socket; we skip it and set
    only what do_GET/do_POST read, then override the write path so nothing is
    sent over the network.
    """

    def __init__(self):
        self.status = None
        self.response_headers = {}
        self.body = b""
        self.wfile = self
        self.path = "/"

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass

    def write(self, data):
        self.body += data


class HttpReadEndpoints(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)
        self._orig_tasks = config.TASKS_DIR
        self._orig_log = config.EVENTS_LOG
        config.TASKS_DIR = str(self.tmp / "tasks")
        config.EVENTS_LOG = str(self.tmp / "events.jsonl")
        dashboard.Handler.store = Store(":memory:")

    def tearDown(self):
        config.TASKS_DIR = self._orig_tasks
        config.EVENTS_LOG = self._orig_log
        store = dashboard.Handler.store
        dashboard.Handler.store = None
        if store is not None:
            store.conn.close()
        self._dir.cleanup()

    def _get(self, path):
        handler = _FakeHandler()
        handler.path = path
        handler.do_GET()
        return handler.status, json.loads(handler.body.decode("utf-8"))

    def _write_events(self, *events):
        Path(config.EVENTS_LOG).write_text(
            "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")

    def _seed_project(self):
        """Write one taskfile and two code-task rows so /api/projects sees it.

        t1 is merged (counts toward the rollup) while t2 is running, so the
        `progress` rollup and the `task_progress` per-task map must disagree in
        shape — exactly the situation where a shared key silently collapsed.
        """
        tdir = Path(config.TASKS_DIR)
        tdir.mkdir()
        f = tdir / "proj.json"
        f.write_text(json.dumps({"project": {"title": "P", "tasks": [
            {"id": "t1", "title": "one"}, {"id": "t2", "title": "two"}]}}),
            encoding="utf-8")
        st = dashboard.Handler.store
        st.upsert_code_task(str(f), "t1", "one", "GLM-5.3", "kimi", "merged")
        st.upsert_code_task(str(f), "t2", "two", "GLM-5.3", "kimi", "running")
        self._write_events({"type": "node_start", "node": "implement_t2", "ts": 0.0})

    def test_health_returns_200_json_object(self):
        """The health endpoint is a JSON object, so the console can parse it.

        A bare list or a non-JSON body would make the Projects page health
        strip throw on render instead of showing the fleet.
        """
        status, body = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertIsInstance(body, dict)

    def test_health_includes_fleet_keys(self):
        """The Projects page health strip reads models/agents/runs/problems.

        Those four keys are what the live fleet-health summary renders; losing
        one shows a half-empty health state even when the fleet is fine.
        """
        status, body = self._get("/api/health")
        self.assertEqual(status, 200)
        for key in ("models", "agents", "runs", "problems"):
            self.assertIn(key, body)

    def test_projects_returns_list_of_projects(self):
        """/api/projects wraps its payload in a dict with a `projects` list.

        The console iterates `body.projects`; an object here (or a missing
        `projects` key) would make the Projects page render nothing.
        """
        self._seed_project()
        status, body = self._get("/api/projects")
        self.assertEqual(status, 200)
        self.assertIsInstance(body, dict)
        self.assertIn("projects", body)
        self.assertIsInstance(body["projects"], list)
        self.assertEqual(len(body["projects"]), 1)

    def test_projects_item_carries_read_keys(self):
        """Each project item carries the keys the console reads.

        `file`, `title`, `phase`, `statuses` and `archived` are the navigation
        skeleton of the Projects page; dropping one blanks a column silently.
        """
        self._seed_project()
        status, body = self._get("/api/projects")
        self.assertEqual(status, 200)
        item = body["projects"][0]
        for key in ("file", "title", "phase", "progress", "task_progress",
                    "statuses", "archived"):
            self.assertIn(key, item)

    def test_projects_keeps_progress_rollup_distinct_from_task_map(self):
        """A project item must keep `progress` ({done,total}) AND a per-task
        `task_progress` map.

        These were once the same key; one silently replaced the other, leaving
        the console unable to tell how far a multi-task project had got. The
        rollup and the per-task map are different shapes and both must exist.
        """
        self._seed_project()
        status, body = self._get("/api/projects")
        self.assertEqual(status, 200)
        item = body["projects"][0]
        self.assertEqual(item["progress"], {"done": 1, "total": 2})
        self.assertIsInstance(item["task_progress"], dict)
        self.assertEqual(set(item["task_progress"]), {"t2"})

    def test_agents_returns_live_agents_and_recent_runs(self):
        """The agents panel reads `agents` (in flight) and `recent` (finished).

        Dropping `recent` would make every completed run vanish from the panel
        the moment it finished, so "did that review pass" becomes unanswerable.
        """
        status, body = self._get("/api/agents")
        self.assertEqual(status, 200)
        self.assertIsInstance(body, dict)
        for key in ("agents", "recent"):
            self.assertIn(key, body)

    def test_metrics_returns_model_rollups_and_task_totals(self):
        """The usage page reads `models` (per-model runs/cost) and `tasks`.

        Both halves of the rollup must come back; a missing `tasks` makes the
        outcome counts invisible even though the model numbers are correct.
        """
        status, body = self._get("/api/metrics")
        self.assertEqual(status, 200)
        self.assertIsInstance(body, dict)
        for key in ("models", "tasks"):
            self.assertIn(key, body)
        self.assertIsInstance(body["models"], list)
        self.assertIsInstance(body["tasks"], dict)

    def test_summary_returns_console_facts(self):
        """The top-of-page summary returns a JSON object, never a bare list.

        The console reads several facts off this response; a non-object shape
        here would break every one of them.
        """
        status, body = self._get("/api/summary")
        self.assertEqual(status, 200)
        self.assertIsInstance(body, dict)

    def test_unknown_path_returns_404_json_error(self):
        """An unknown path must 404 with a JSON error body.

        A stray path that falls through to an HTML 404 (or any non-JSON body)
        breaks console error handling, which expects a parseable `{"error"}`.
        """
        status, body = self._get("/api/does-not-exist")
        self.assertEqual(status, 404)
        self.assertIsInstance(body, dict)
        self.assertIn("error", body)
