"""HTTP contract tests for GET /api/activity — the fleet activity feed.

The event log already records everything, but nothing surfaced it in one
place. These checks pin the three properties the feed depends on: it reads
through the EXISTING tolerant event-log reader (a corrupt line is skipped,
never a 500), it is FILTERED to a curated set of task-lifecycle/driver/chain
types (raw `driver.heartbeat` would swamp the feed — several per second), and
`?limit=` is honoured within a hard cap.

Driving dashboard.Handler directly keeps the checks off a real socket and off
the operator's data.
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from helpers import capture_events, FakeStore  # noqa: F401  (sys.path + redirect)

import code_tasks
import config
import dashboard
from store import Store


class _FakeHandler(dashboard.Handler):
    """A socket-less Handler that captures status/headers/body in memory.

    BaseHTTPRequestHandler.__init__ requires a live socket; we skip it and set
    only what do_GET reads, then override the write path so nothing is sent
    over the network.
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


class ActivityFeed(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)
        self._orig_log = config.EVENTS_LOG
        config.EVENTS_LOG = str(self.tmp / "events.jsonl")
        # _load_event_lines caches by (size, mtime_ns); a fresh temp file per
        # test must never be answered from the previous test's cache.
        dashboard._lines_cache["key"] = None
        dashboard._lines_cache["lines"] = []
        dashboard.Handler.store = Store(":memory:")

    def tearDown(self):
        config.EVENTS_LOG = self._orig_log
        store = dashboard.Handler.store
        dashboard.Handler.store = None
        if store is not None:
            store.conn.close()
        self._dir.cleanup()
        dashboard._lines_cache["key"] = None
        dashboard._lines_cache["lines"] = []

    # -- helpers ---------------------------------------------------------
    def _write_raw(self, text):
        Path(config.EVENTS_LOG).write_text(text, encoding="utf-8")

    def _write(self, *events):
        self._write_raw("".join(json.dumps(e) + "\n" for e in events))

    def _get(self, path="/api/activity"):
        handler = _FakeHandler()
        handler.path = path
        handler.do_GET()
        return handler.status, json.loads(handler.body.decode("utf-8"))

    def _ev(self, etype, ts, **fields):
        return dict({"type": etype, "ts": ts, "workload": "code",
                     "run_id": "r-1", **fields})

    # -- the curated set -------------------------------------------------
    def test_curated_lifecycle_events_are_returned(self):
        """Every type the panel colours must survive the filter."""
        curated = ["task.reviewed", "task.failed", "task.escalated",
                   "task.merged", "task.pr_opened", "task.pr_reviewed",
                   "task.resynced", "task.review_degraded", "driver.stalled",
                   "chain.wait", "chain.ready", "chain.blocked"]
        self._write(*[self._ev(t, 100.0 + i, task=f"t{i}") for i, t in
                      enumerate(curated)])
        status, body = self._get()
        self.assertEqual(status, 200)
        self.assertEqual({e["type"] for e in body["events"]}, set(curated))

    def test_heartbeats_and_noise_are_excluded(self):
        """A raw driver.heartbeat would swamp the feed — it must be dropped."""
        self._write(
            self._ev("driver.heartbeat", 10.0, model="GLM-5.3"),
            self._ev("driver.heartbeat", 11.0, model="GLM-5.3"),
            self._ev("driver.done", 12.0, task="t1"),
            self._ev("task.merged", 13.0, task="t1"),
        )
        status, body = self._get()
        self.assertEqual(status, 200)
        self.assertEqual([e["type"] for e in body["events"]], ["task.merged"])

    def test_each_entry_carries_the_panel_contract(self):
        """ts/type/task/run_id/context — the five keys the UI renders from."""
        self._write(self._ev("task.reviewed", 42.0, task="t1", reviewer="glm",
                             model="GLM-5.3", round=2, n_issues=3))
        _, body = self._get()
        e = body["events"][0]
        for key in ("ts", "type", "task", "run_id", "context"):
            self.assertIn(key, e)
        self.assertEqual(e["task"], "t1")
        self.assertEqual(e["run_id"], "r-1")
        self.assertEqual(e["context"]["workload"], "code")
        # The review-round enrichment rides through to the panel.
        self.assertEqual(e["n_issues"], 3)
        self.assertEqual(e["round"], 2)

    def test_the_normalised_contract_keys_win(self):
        """A raw field may not shadow a normalised one.

        events.emit() flattens context into the record (workload/run_id sit at
        the top level), so the route rebuilds `context` from those fields. The
        panel is promised THOSE keys; a leftover raw field of the same name
        must not clobber them.
        """
        self._write_raw(json.dumps(
            {"type": "task.merged", "ts": 5.0, "task": "t1", "run_id": "r-9",
             "context": "stale-raw-value"}) + "\n")
        _, body = self._get()
        e = body["events"][0]
        self.assertIsInstance(e["context"], dict)
        self.assertEqual(e["context"]["run_id"], "r-9")
        self.assertEqual(e["run_id"], "r-9")

    def test_a_task_files_are_resolved_for_the_click_through(self):
        """The panel opens a PROJECT, so every row needs its taskfile name."""
        st = dashboard.Handler.store
        st.upsert_code_task("/home/x/tasks/panel.json", "t1", "one", "GLM-5.3",
                            "deepseek", "merged")
        self._write(self._ev("task.merged", 1.0, task="t1"))
        _, body = self._get()
        self.assertEqual(body["events"][0]["file"], "panel.json")

    def test_a_driver_event_id_resolves_to_its_project(self):
        """Driver events carry the HARNESS id, not a code_tasks row key.

        code_tasks hands driver.run `<tid>-xN` (an attempt) and `<tid>-prN` (a
        PR reviewer), and drivers.py emits whatever it was given. The rows are
        keyed by the base id, so an exact-match lookup dropped `file` for every
        driver event — and the panel then offered a button that opened nothing.
        A stalled harness is precisely the row an operator wants to open.
        """
        st = dashboard.Handler.store
        st.upsert_code_task("/home/x/tasks/panel.json", "t1", "one", "GLM-5.3",
                            "deepseek", "merged")
        self._write(
            self._ev("driver.stalled", 1.0, task="t1-x2", model="GLM-5.3",
                     idle_s=900),
            self._ev("driver.stalled", 2.0, task="t1-pr3", model="GLM-5.3",
                     idle_s=900),
            self._ev("task.merged", 3.0, task="t1"),
        )
        _, body = self._get()
        files = {e["task"]: e["file"] for e in body["events"]}
        self.assertEqual(files["t1-x2"], "panel.json")
        self.assertEqual(files["t1-pr3"], "panel.json")
        self.assertEqual(files["t1"], "panel.json")
        # The id the panel SHOWS is the one the event carried, unstripped.
        self.assertEqual([e["task"] for e in body["events"]],
                         ["t1", "t1-pr3", "t1-x2"])

    def test_an_exact_id_wins_over_the_suffix_stripped_fallback(self):
        """A task really called `release-pr2` must open ITS project.

        Stripping the suffix first would resolve such an id to a different
        taskfile (or none), so the exact row key is tried before the fallback.
        """
        st = dashboard.Handler.store
        st.upsert_code_task("/home/x/tasks/real.json", "release-pr2", "the real one",
                            "GLM-5.3", "deepseek", "merged")
        st.upsert_code_task("/home/x/tasks/other.json", "release", "the fallback",
                            "GLM-5.3", "deepseek", "merged")
        self._write(self._ev("task.merged", 1.0, task="release-pr2"))
        _, body = self._get()
        self.assertEqual(body["events"][0]["file"], "real.json")

    def test_an_unknown_task_resolves_to_no_file_but_still_renders(self):
        """History outlives the row it came from — that must not drop the event."""
        self._write(self._ev("task.merged", 1.0, task="long-gone"))
        status, body = self._get()
        self.assertEqual(status, 200)
        self.assertEqual(len(body["events"]), 1)
        self.assertIsNone(body["events"][0]["file"])

    def test_newest_first(self):
        self._write(*[self._ev("task.merged", 100.0 + i, task=f"t{i}")
                      for i in range(5)])
        _, body = self._get()
        self.assertEqual([e["task"] for e in body["events"]],
                         ["t4", "t3", "t2", "t1", "t0"])

    # -- limit -----------------------------------------------------------
    def test_default_limit_is_50(self):
        self._write(*[self._ev("task.merged", 100.0 + i, task=f"t{i}")
                      for i in range(80)])
        _, body = self._get()
        self.assertEqual(len(body["events"]), 50)
        self.assertEqual(body["limit"], 50)
        self.assertEqual(body["total"], 80)

    def test_limit_is_honoured(self):
        self._write(*[self._ev("task.merged", 100.0 + i, task=f"t{i}")
                      for i in range(30)])
        _, body = self._get("/api/activity?limit=5")
        self.assertEqual(len(body["events"]), 5)
        self.assertEqual([e["task"] for e in body["events"]],
                         ["t29", "t28", "t27", "t26", "t25"])

    def test_limit_is_capped_at_500(self):
        """A huge ?limit= must not turn the endpoint into a log download."""
        self._write(*[self._ev("task.merged", 100.0 + i, task=f"t{i}")
                      for i in range(600)])
        _, body = self._get("/api/activity?limit=100000")
        self.assertEqual(len(body["events"]), 500)
        self.assertEqual(body["limit"], 500)

    def test_a_bad_limit_degrades_instead_of_500(self):
        """A typo'd link (`?limit=abc`, `?limit=`) must not break the panel."""
        self._write(self._ev("task.merged", 1.0, task="t1"))
        for q in ("?limit=abc", "?limit=", "?limit=-4", "?limit=0"):
            status, body = self._get("/api/activity" + q)
            self.assertEqual(status, 200, q)
            self.assertTrue(body["events"], q)

    # -- tolerance -------------------------------------------------------
    def test_corrupt_lines_are_skipped_without_a_500(self):
        """One truncated/garbage line (a killed writer) must not kill the feed."""
        # Append order, like a real log: a truncated write (a killed writer
        # mid-append), a blank line and plain garbage between two good lines.
        self._write_raw(
            '{"type": "task.merged", "ts": 1.0, "task": "t1"}\n'
            '{"type": "task.merged", "ts": 2.0, "task":\n'
            'not json at all\n'
            '\n'
            '[1, 2, 3]\n'
            '"a bare string is valid JSON too"\n'
            '{"type": "task.merged", "ts": 3.0, "task": "t3", "context": 7}\n'
        )
        status, body = self._get()
        self.assertEqual(status, 200)
        self.assertEqual([e["task"] for e in body["events"]], ["t3", "t1"])

    def test_a_missing_event_log_is_an_empty_feed(self):
        status, body = self._get()
        self.assertEqual(status, 200)
        self.assertEqual(body["events"], [])


def _taskfile(tasks, repo="/tmp", title="t"):
    project = {"repo": repo, "title": title, "tasks": tasks}
    fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"project": project}, fh)
    fh.close()
    return Path(fh.name)


_BASIC = {"id": "t1", "title": "T1", "prompt": "do it",
          "model": config.ESCALATION_PATH[0],
          "reviewer": config.cross_family_reviewer(config.ESCALATION_PATH[0])}


class EnrichedFailureEvents(unittest.TestCase):
    """`task.failed` must say what was actually wrong, not just that it failed.

    Eleven real task.failed events sit in the live log reading "pre-merge
    review still rejecting after 1 attempt(s)": the sentence says a reviewer
    objected and says nothing about WHAT it objected to. The feed can only
    show what the event carries, so the reason alone is not enough — the gate
    output tail (which names the failing tests) or the blocking review issues
    must ride beside it as `detail`.
    """

    def _fail_node(self):
        ts = code_tasks.load_taskfile(_taskfile([_BASIC]))
        with capture_events():
            g = code_tasks.build_code_graph(FakeStore([]), ts, taskfile="tf.json")
        return g.nodes["fail_t1"].fn

    def _run(self, results, runs=None):
        ctx = {"results": results, "runs": runs or {}}
        with capture_events() as ev:
            asyncio.run(self._fail_node()(ctx))
        return (ev.of("task.failed") or [{}])[0]

    def test_a_gate_failure_carries_the_gate_output_tail(self):
        e = self._run({"gate_t1": {"passed": False, "output":
                                   "FAIL: test_the_tail_is_kept\n1 test failed"}})
        self.assertEqual(e["reason"],
                         "verify gate still failing after 0 attempt(s) on "
                         + config.ESCALATION_PATH[0])
        self.assertIn("FAIL: test_the_tail_is_kept", e["detail"])

    def test_a_review_failure_carries_the_blocking_issues(self):
        e = self._run({"gate_t1": {"passed": True},
                       "review_t1": {"pass": False,
                                     "issues": ["the null check is missing"]}})
        self.assertIn("pre-merge review still rejecting", e["reason"])
        self.assertIn("the null check is missing", e["detail"])

    def test_a_pr_failure_carries_the_unresolved_issues(self):
        e = self._run({"pr_review_t1": {"approved": False, "pr": 9,
                                        "issues": ["renamed a public helper"]}})
        self.assertIn("PR #9 rejected", e["reason"])
        self.assertIn("renamed a public helper", e["detail"])

    def test_the_exhausted_escalation_wording_is_preserved(self):
        """The message the runbook documents must survive the enrichment."""
        e = self._run({}, runs={"implement_t1": 3, "escalate_t1": 2})
        self.assertEqual(
            e["reason"],
            "exhausted escalation: 2 escalation(s), ended on "
            + config.ESCALATION_PATH[0])

    def test_every_failure_emits_a_non_empty_detail(self):
        """Whatever path reached `fail`, the feed has something to show."""
        for results in ({}, {"gate_t1": {"passed": False, "output": "boom"}},
                        {"review_t1": {"pass": False, "issues": ["x"]}}):
            self.assertTrue(self._run(results).get("detail"), results)


if __name__ == "__main__":
    unittest.main()
