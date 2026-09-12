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

    # ---- /api/run-log -----------------------------------------------------
    # What "view log" opens after a Run click: the stdout of a run/plan process
    # the dashboard launched. Only logs/run-*.log and logs/plan-*.log — the
    # name comes from the browser, so it must not reach any other file.
    def test_run_log_returns_the_tail_of_a_run_log(self):
        log_dir = Path(config.ROOT) / "logs"
        log_dir.mkdir(exist_ok=True)
        name = "run-test-http-read-0.log"
        (log_dir / name).write_text("".join(f"line {i}\n" for i in range(30)),
                                    encoding="utf-8")
        try:
            status, body = self._get(f"/api/run-log?file={name}&lines=5")
            self.assertEqual(status, 200)
            self.assertEqual(body["file"], name)
            self.assertEqual(body["lines"], [f"line {i}" for i in range(25, 30)])
        finally:
            (log_dir / name).unlink()

    def test_run_log_refuses_any_other_file(self):
        for bad in ("server.log", "../orchestrator.db", "gates/x.log",
                    "run-..%2F..%2Fetc.log", ""):
            with self.subTest(file=bad):
                status, body = self._get(f"/api/run-log?file={bad}")
                self.assertIn(status, (400, 404))
                self.assertIn("error", body)
        status, body = self._get("/api/run-log?file=run-nope-1.log")
        self.assertEqual(status, 404)

    # ---- project chains (project.after) --------------------------------
    # The runner has gated on `after` since Rule 9 landed; the page never
    # read it. /api/projects and /api/project must carry the chain in both
    # directions plus the gate node drawn into the DAG.
    def _seed_chain(self):
        tdir = Path(config.TASKS_DIR)
        tdir.mkdir(exist_ok=True)
        up = tdir / "up.json"
        up.write_text(json.dumps({"project": {"title": "Upstream", "tasks": [
            {"id": "u1", "title": "one"}, {"id": "u2", "title": "two"}]}}),
            encoding="utf-8")
        down = tdir / "down.json"
        down.write_text(json.dumps({"project": {"title": "Downstream",
            "after": [str(up)], "tasks": [
                {"id": "d1", "title": "head"},
                {"id": "d2", "title": "tail", "deps": ["d1"]}]}}), encoding="utf-8")
        st = dashboard.Handler.store
        st.upsert_code_task(str(up), "u1", "one", "GLM-5.3", "kimi", "merged")
        return up, down

    def test_projects_carry_the_chain_in_both_directions(self):
        self._seed_chain()
        status, body = self._get("/api/projects")
        self.assertEqual(status, 200)
        by = {p["file"]: p for p in body["projects"]}
        down, up = by["down.json"], by["up.json"]
        self.assertEqual(down["chain"]["after"], ["up.json"])
        self.assertFalse(down["chain"]["ready"])
        dep = down["chain"]["deps"][0]
        self.assertEqual((dep["file"], dep["title"], dep["merged"], dep["n_tasks"], dep["state"]),
                         ("up.json", "Upstream", 1, 2, "waiting"))
        # a project that cannot start is not "never run"
        self.assertEqual(down["phase"], "chained")
        self.assertEqual([b["file"] for b in up["chain"]["blocks"]], ["down.json"])
        self.assertTrue(up["chain"]["ready"])
        # the gate node feeds the head task only, and does not count as a task
        gate = [n for n in down["dag"]["nodes"] if n.get("kind") == "chain"]
        self.assertEqual(len(gate), 1)
        self.assertEqual(gate[0]["file"], "up.json")
        self.assertEqual([(e["src"], e["dst"]) for e in down["dag"]["edges"] if e.get("kind") == "chain"],
                         [("after:up.json", "d1")])
        self.assertEqual(down["progress"], {"done": 0, "total": 2})
        self.assertEqual(down["n_tasks"], 2)

    def test_chain_becomes_ready_when_every_upstream_task_is_merged(self):
        up, down = self._seed_chain()
        dashboard.Handler.store.upsert_code_task(str(up), "u2", "two", "GLM-5.3", "kimi", "merged")
        status, body = self._get("/api/projects")
        d = next(p for p in body["projects"] if p["file"] == "down.json")
        self.assertTrue(d["chain"]["ready"])
        self.assertEqual(d["chain"]["deps"][0]["state"], "ready")
        self.assertEqual(d["phase"], "new")

    def test_project_detail_carries_chain_and_gate_state(self):
        self._seed_chain()
        self._write_events({"type": "chain.wait", "ts": 5.0,
                            "taskfile": str(Path(config.TASKS_DIR) / "down.json"),
                            "deps": ["x"]})
        status, body = self._get("/api/project?file=down.json")
        self.assertEqual(status, 200)
        self.assertEqual(body["chain"]["after"], ["up.json"])
        self.assertEqual(body["chain"]["gate"]["state"], "waiting")
        status, body = self._get("/api/project?file=up.json")
        self.assertEqual([b["file"] for b in body["chain"]["blocks"]], ["down.json"])
        self.assertIsNone(body["chain"]["gate"])

    def test_unchained_project_has_no_chain(self):
        self._seed_project()
        status, body = self._get("/api/projects")
        self.assertIsNone(body["projects"][0]["chain"])

    # ---- /api/graph-shapes ----------------------------------------------
    def test_graph_shapes_serves_catalogue_projects_and_engine(self):
        self._seed_project()
        status, body = self._get("/api/graph-shapes")
        self.assertEqual(status, 200)
        for key in ("patterns", "projects", "engine", "decisions", "caps"):
            self.assertIn(key, body)
        self.assertEqual(body["projects"][0]["file"], "proj.json")
        self.assertEqual(body["projects"][0]["detected"]["shape"], "fanout")
        ids = [p["id"] for p in body["patterns"]]
        for want in ("chain", "fanout", "diamond", "router"):
            self.assertIn(want, ids)
