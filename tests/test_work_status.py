"""The live DAG API must explain work and waits without reviving old events."""

import json
import time
import unittest
from unittest import mock

from helpers import capture_events  # noqa: F401  (repo import path)

import dashboard


class WorkStatusTests(unittest.TestCase):
    def setUp(self):
        dashboard._work_status_cache.update(at=0.0, value=None)
        self.now = time.time()
        self.pid = 424242

    def tearDown(self):
        dashboard._work_status_cache.update(at=0.0, value=None)

    def project(self, *, status="running", run_pid=None, deps=(), chain=None):
        nodes = [{"id": "alpha", "title": "Alpha", "model": "GLM-5.3",
                  "status": status}]
        edges = []
        if deps:
            nodes.insert(0, {"id": "foundation", "title": "Foundation",
                             "model": "GLM-5.3", "status": "pending"})
            edges.append({"src": "foundation", "dst": "alpha"})
        return {"file": "p.json", "title": "P", "run_pid": run_pid,
                "chain": chain, "errors": [],
                "dag": {"nodes": nodes, "edges": edges}}

    def event(self, kind, *, age=1, **fields):
        return {"type": kind, "ts": self.now - age,
                "run_id": f"123-{self.pid}", **fields}

    def status(self, project, *events, agents=()):
        lines = [json.dumps(e) for e in events]
        with (mock.patch.object(dashboard, "_projects", return_value=[project]),
              mock.patch.object(dashboard, "_collect_inflight", return_value=(list(agents), [])),
              mock.patch.object(dashboard, "_load_event_lines", return_value=lines)):
            return dashboard._work_status(None)["projects"][0]["tasks"]

    def test_multiword_pr_reviewer_stage_matches_full_task_suffix(self):
        tasks = self.status(
            self.project(status="in_review", run_pid=self.pid),
            self.event("node_start", node="pr_reviewer_alpha"),
            self.event("driver.start", task="alpha-pr1", model="GLM-5.3",
                       role="pr_reviewer"))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["stage"], "pr_reviewer")
        self.assertEqual(tasks[0]["activity"], "working")

    def test_usage_wait_reports_reset_time_only_while_current_run_waits(self):
        reset = self.now + 3600
        tasks = self.status(
            self.project(run_pid=self.pid),
            self.event("node_start", node="implement_alpha", age=20),
            self.event("driver.usage_limit", task="alpha-x1", model="GLM-5.3",
                       role="implementer", resets_at=reset, age=10),
            self.event("driver.usage_wait", task="alpha-x1", model="GLM-5.3",
                       resets_at=reset, remaining_s=3500, age=1))
        self.assertEqual(tasks[0]["stage"], "implement")
        self.assertEqual(tasks[0]["activity"], "usage_wait")
        self.assertEqual(tasks[0]["usage_resets_at"], reset)

    def test_fresh_usage_wait_outweighs_an_old_agent_idle_sample(self):
        reset = self.now + 3600
        tasks = self.status(
            self.project(run_pid=self.pid),
            self.event("node_start", node="implement_alpha", age=20),
            self.event("driver.usage_wait", task="alpha-x1", resets_at=reset),
            agents=[{"task": "alpha-x1", "source": "driver:opencode",
                     "pid": self.pid, "started": self.now - 19,
                     "stalled": True, "last_event_s": 700}])
        self.assertEqual(tasks[0]["activity"], "usage_wait")

    def test_old_usage_wait_is_not_a_live_pause(self):
        tasks = self.status(
            self.project(run_pid=self.pid),
            self.event("node_start", node="implement_alpha", age=700),
            self.event("driver.usage_wait", task="alpha-x1", model="GLM-5.3",
                       resets_at=self.now + 3600, age=600))
        self.assertNotEqual(tasks[0]["activity"], "usage_wait")
        self.assertIsNone(tasks[0]["usage_resets_at"])

    def test_recent_cap_wait_is_queued_and_old_cap_wait_is_not(self):
        project = self.project(run_pid=self.pid)
        fresh = self.status(
            project,
            self.event("node_start", node="review_alpha", age=30),
            self.event("driver.cap_wait", task="alpha-x1", model="GLM-5.3",
                       role="reviewer", scope="fleet", in_use=4, cap=4, age=1))
        self.assertEqual(fresh[0]["activity"], "queued")
        dashboard._work_status_cache.update(at=0.0, value=None)
        old = self.status(
            project,
            self.event("node_start", node="review_alpha", age=700),
            self.event("driver.cap_wait", task="alpha-x1", model="GLM-5.3",
                       role="reviewer", scope="fleet", in_use=4, cap=4, age=600))
        self.assertNotEqual(old[0]["activity"], "queued")

    def test_running_row_without_owner_is_stale_not_working(self):
        tasks = self.status(
            self.project(run_pid=None),
            self.event("node_start", node="implement_alpha", age=1),
            self.event("driver.start", task="alpha-x1", model="GLM-5.3", age=1))
        self.assertEqual(tasks[0]["activity"], "stalled")
        self.assertIsNone(tasks[0]["stage"])
        self.assertIn("not live", tasks[0]["reason"])

    def test_old_run_events_do_not_claim_the_current_run_stage(self):
        old = self.event("node_start", node="pr_merge_alpha", age=1)
        old["run_id"] = "122-987654"
        tasks = self.status(self.project(status="in_review", run_pid=self.pid), old)
        self.assertNotEqual(tasks[0]["stage"], "pr_merge")
        self.assertNotEqual(tasks[0]["activity"], "working")

    def test_driver_start_settles_usage_and_capacity_waits(self):
        for wait_kind in ("driver.usage_wait", "driver.cap_wait"):
            with self.subTest(wait_kind=wait_kind):
                dashboard._work_status_cache.update(at=0.0, value=None)
                tasks = self.status(
                    self.project(run_pid=self.pid),
                    self.event("node_start", node="implement_alpha", age=20),
                    self.event(wait_kind, task="alpha-x1", model="GLM-5.3",
                               resets_at=self.now + 3600, age=10),
                    self.event("driver.start", task="alpha-x1", model="GLM-5.3",
                               role="implementer", age=1))
                self.assertEqual(tasks[0]["activity"], "working")
                self.assertIsNone(tasks[0]["usage_resets_at"])

    def test_node_end_closes_a_pipeline_stage(self):
        tasks = self.status(
            self.project(status="in_review", run_pid=self.pid),
            self.event("node_start", node="pr_merge_alpha", age=10),
            self.event("node_end", node="pr_merge_alpha", age=1))
        self.assertIsNone(tasks[0]["stage"])
        self.assertNotEqual(tasks[0]["activity"], "working")

    def test_duplicate_task_ids_do_not_cross_project_runs(self):
        first = self.project(run_pid=self.pid)
        first["file"] = "first.json"
        second = self.project(run_pid=self.pid + 1)
        second["file"] = "second.json"
        agent = {"task": "alpha-x1", "pid": self.pid, "source": "driver:opencode",
                 "model": "GLM-5.3", "role": "implementer", "harness": "opencode",
                 "started": self.now - 3, "last_event_s": 1}
        lines = [json.dumps(self.event("node_start", node="implement_alpha", age=4)),
                 json.dumps(self.event("driver.start", task="alpha-x1", age=3))]
        with (mock.patch.object(dashboard, "_projects", return_value=[first, second]),
              mock.patch.object(dashboard, "_collect_inflight", return_value=([agent], [])),
              mock.patch.object(dashboard, "_load_event_lines", return_value=lines)):
            projects = dashboard._work_status(None)["projects"]
        self.assertEqual(projects[0]["tasks"][0]["activity"], "working")
        self.assertEqual(len(projects[0]["tasks"][0]["agents"]), 1)
        self.assertIsNone(projects[1]["tasks"][0]["stage"])
        self.assertEqual(projects[1]["tasks"][0]["agents"], [])

    def test_fanout_keeps_both_concurrent_pr_reviewers_visible(self):
        project = self.project(status="in_review", run_pid=self.pid)
        reviewers = [
            {"task": "alpha-pr1", "pid": self.pid, "source": "driver:opencode",
             "model": "GLM-5.3", "role": "pr_reviewer", "harness": "opencode",
             "started": self.now - 8, "last_event_s": 1},
            {"task": "alpha-pr1", "pid": self.pid, "source": "driver:reasonix",
             "model": "DeepSeek-V4.1-Flash-thinking-max", "role": "pr_reviewer",
             "harness": "reasonix", "started": self.now - 7, "last_event_s": 1},
        ]
        tasks = self.status(
            project,
            self.event("node_start", node="pr_reviewer_alpha", age=10),
            self.event("node_start", node="pr_reviewer_alpha", age=9),
            self.event("driver.start", task="alpha-pr1", age=8),
            self.event("driver.start", task="alpha-pr1", age=7),
            agents=reviewers)
        self.assertEqual(tasks[0]["stage"], "pr_reviewer")
        self.assertEqual(len(tasks[0]["agents"]), 2)
        self.assertEqual({a["model"] for a in tasks[0]["agents"]},
                         {a["model"] for a in reviewers})

    def test_first_fanout_completion_does_not_close_second_reviewer(self):
        tasks = self.status(
            self.project(status="in_review", run_pid=self.pid),
            self.event("node_start", node="pr_reviewer_alpha", age=10),
            self.event("node_start", node="pr_reviewer_alpha", age=9),
            self.event("node_end", node="pr_reviewer_alpha", age=1))
        self.assertEqual(tasks[0]["stage"], "pr_reviewer")

    def test_dependency_and_project_chain_name_the_blocker(self):
        tasks = self.status(self.project(status="pending", run_pid=self.pid,
                                         deps=("foundation",)))
        alpha = next(t for t in tasks if t["id"] == "alpha")
        self.assertEqual(alpha["activity"], "waiting")
        self.assertEqual(alpha["blocked_by"], ["foundation"])

        dashboard._work_status_cache.update(at=0.0, value=None)
        chained = self.status(self.project(status="pending", run_pid=None,
                                           chain={"ready": False}))
        self.assertEqual(chained[0]["activity"], "waiting")
        self.assertIn("upstream project", chained[0]["reason"])

    def test_pending_task_is_not_called_capacity_queued_without_a_wait_event(self):
        tasks = self.status(self.project(status="pending", run_pid=self.pid))
        self.assertEqual(tasks[0]["activity"], "waiting")
        self.assertIn("scheduler", tasks[0]["reason"])


class WorkStatusHttpTests(unittest.TestCase):
    def test_route_returns_the_live_dag_payload(self):
        from tests.test_http_read import _FakeHandler

        handler = _FakeHandler()
        handler.path = "/api/work-status"
        with mock.patch.object(dashboard, "_work_status", return_value={
                "now": 1, "projects": [{"file": "p.json", "tasks": []}],
                "agents": []}):
            handler.do_GET()
        self.assertEqual(handler.status, 200)
        payload = json.loads(handler.body)
        self.assertEqual(payload["projects"][0]["file"], "p.json")
        self.assertIn("agents", payload)


if __name__ == "__main__":
    unittest.main()
