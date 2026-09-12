"""Dashboard accounting: in-flight attribution and cap arithmetic.

These numbers govern operator decisions — whether the fleet looks wedged,
whether to throttle — so over-counting is not a cosmetic bug.
"""
import json
import os
import pathlib
import tempfile
import time
import unittest
from pathlib import Path

from helpers import capture_events  # noqa: F401  (sys.path)
from helpers import needs_kimi, needs_deepseek_v4, needs_three_families, ENTRY, STRONGEST  # noqa: E402,F401
from helpers import STRONGEST_FAMILY, STRONGEST_REVIEWER  # noqa: E402,F401
from helpers import KIMI_HARNESS_MODEL, needs_kimi_harness, PRICE_PAIR, needs_two_rates  # noqa: E402,F401

import config
import code_tasks
import dashboard
from test_code_tasks import BASIC, taskfile  # noqa: E402


class SessionAttribution(unittest.TestCase):
    def test_reads_the_task_id_out_of_a_kimi_session_path(self):
        p = Path("/home/u/.kimi-code/sessions/wd_index-graph-polish_ea5e72361606"
                 "/session_4b6b088e/agents/main/wire.jsonl")
        self.assertEqual(dashboard._session_task(p), "index-graph-polish")

    def test_handles_a_task_id_containing_underscores(self):
        p = Path("/x/sessions/wd_my_task_name_deadbeef/session_1/agents/main/wire.jsonl")
        self.assertEqual(dashboard._session_task(p), "my_task_name")

    def test_returns_none_for_an_unrecognised_path(self):
        self.assertIsNone(dashboard._session_task(Path("/tmp/nope/wire.jsonl")))


class InflightAttribution(unittest.TestCase):
    """A fleet driver must be counted once, not once per accounting layer.

    The fleet spawns the kimi CLI, which writes its own kimi-code wire log. The
    driver was therefore counted both from its driver.start event AND from that
    wire log, so one driver read as several agents against the ARC account cap
    — and a retried task inflated it further, because each killed attempt
    leaves an unanswered llm.request that looks live for 10 minutes.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self._orig_log = config.EVENTS_LOG
        config.EVENTS_LOG = str(self.root / "events.jsonl")
        dashboard._lines_cache["key"] = None
        dashboard._kimi_cache.clear()
        dashboard._fleet_names_cache.update(key=0.0, names=frozenset())
        # _collect_inflight also merges kimi-code sessions read from the real
        # ~/.kimi-code/sessions. Without stubbing that, these tests count
        # whatever the operator happens to be running and fail at random —
        # which they did, the moment a planner agent was live.
        self._orig_kimi = dashboard._kimi_code_usage
        dashboard._kimi_code_usage = lambda now, fleet_names=frozenset(): {
            "models": [], "inflight": [], "points": []}

    def tearDown(self):
        dashboard._kimi_code_usage = self._orig_kimi
        config.EVENTS_LOG = self._orig_log
        dashboard._lines_cache["key"] = None
        dashboard._fleet_names_cache.update(key=0.0, names=frozenset())
        self._dir.cleanup()

    def write_events(self, *events):
        Path(config.EVENTS_LOG).write_text(
            "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")

    def test_an_unmatched_driver_start_counts_as_one_agent(self):
        now = time.time()
        self.write_events({"ts": now - 30, "type": "driver.start", "harness": "kimi",
                           "model": STRONGEST, "role": "implementer",
                           "task": "t1", "attempt": 1})
        rows, _ = dashboard._collect_inflight(now, None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "driver:kimi")

    def test_a_settled_driver_counts_as_none(self):
        now = time.time()
        base = {"harness": "kimi", "model": STRONGEST, "role": "implementer",
                "task": "t1", "attempt": 1}
        self.write_events({"ts": now - 30, "type": "driver.start", **base},
                          {"ts": now - 5, "type": "driver.done", **base})
        rows, _ = dashboard._collect_inflight(now, None)
        self.assertEqual(rows, [])

    def test_a_cancelled_driver_settles_too(self):
        """Without driver.cancelled this lingered as a phantom for ~19 min."""
        now = time.time()
        base = {"harness": "kimi", "model": STRONGEST, "role": "implementer",
                "task": "t1", "attempt": 1}
        self.write_events({"ts": now - 30, "type": "driver.start", **base},
                          {"ts": now - 5, "type": "driver.cancelled", **base})
        rows, _ = dashboard._collect_inflight(now, None)
        self.assertEqual(rows, [])

    def test_a_stale_driver_start_is_pruned(self):
        now = time.time()
        self.write_events({"ts": now - dashboard.DRIVER_STALE_S - 60,
                           "type": "driver.start", "harness": "kimi",
                           "model": STRONGEST, "role": "implementer",
                           "task": "t1", "attempt": 1})
        rows, _ = dashboard._collect_inflight(now, None)
        self.assertEqual(rows, [], "a killed run's start event must not count forever")

    def test_last_event_age_and_stall_flag_come_from_the_newest_ping(self):
        """last_event_s must read the newest liveness event, not the start —
        a live agent shows a fresh heartbeat age, not its total runtime."""
        now = time.time()
        base = {"harness": "kimi", "model": STRONGEST, "role": "implementer",
                "task": "t1", "attempt": 1}
        self.write_events({"ts": now - 90, "type": "driver.start", **base},
                          {"ts": now - 12, "type": "driver.heartbeat", **base})
        rows, _ = dashboard._collect_inflight(now, None)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["last_event_s"], 12.0, delta=0.5)
        self.assertFalse(rows[0]["stalled"])

    def test_a_stalled_driver_is_flagged(self):
        now = time.time()
        base = {"harness": "kimi", "model": STRONGEST, "role": "implementer",
                "task": "t1", "attempt": 1}
        self.write_events({"ts": now - 90, "type": "driver.start", **base},
                          {"ts": now - 12, "type": "driver.stalled", **base})
        rows, _ = dashboard._collect_inflight(now, None)
        self.assertEqual(len(rows), 1, "a stall report settles nothing")
        self.assertTrue(rows[0]["stalled"])

    def test_long_idle_progress_flags_stalled(self):
        """Idle time is carried forward from the newest progress sample; far
        past the 300s stall threshold it must flag the row even with no
        driver.stalled event."""
        now = time.time()
        base = {"harness": "kimi", "model": STRONGEST, "role": "implementer",
                "task": "t1", "attempt": 1}
        self.write_events({"ts": now - 90, "type": "driver.start", **base},
                          {"ts": now - 5, "type": "driver.progress",
                           "idle_s": 400, **base})
        rows, _ = dashboard._collect_inflight(now, None)
        self.assertTrue(rows[0]["stalled"])

    def test_a_heartbeat_does_not_outlive_its_driver(self):
        """driver.done must clear the liveness record with the start — a
        settled run must not keep reporting a heartbeat age."""
        now = time.time()
        base = {"harness": "kimi", "model": STRONGEST, "role": "implementer",
                "task": "t1", "attempt": 1}
        self.write_events({"ts": now - 90, "type": "driver.start", **base},
                          {"ts": now - 30, "type": "driver.heartbeat", **base},
                          {"ts": now - 5, "type": "driver.done", **base})
        rows, _ = dashboard._collect_inflight(now, None)
        self.assertEqual(rows, [])


class KimiSessionExclusion(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        dashboard._kimi_cache.clear()

    def tearDown(self):
        self._dir.cleanup()

    def _session(self, task, answered):
        """Build a wire.jsonl for one kimi-code session."""
        d = (Path(self._dir.name) / f"wd_{task}_abc123" / "session_1"
             / "agents" / "main")
        d.mkdir(parents=True)
        now_ms = time.time() * 1000
        lines = [{"type": "llm.request", "model": STRONGEST,
                  "modelAlias": "arc/kimi-k3", "agentId": "main", "time": now_ms}]
        if answered:
            lines.append({"type": "usage.record", "model": "arc/kimi-k3",
                          "usageScope": "turn", "time": now_ms + 1000,
                          "usage": {"inputOther": 10, "output": 5}})
        (d / "wire.jsonl").write_text(
            "".join(json.dumps(l) + "\n" for l in lines), encoding="utf-8")
        return d / "wire.jsonl"

    def test_an_unanswered_session_is_in_flight(self):
        parsed = dashboard._parse_kimi_wire(self._session("interactive-work", False))
        self.assertIsNotNone(parsed["last_req"])
        self.assertGreater(parsed["last_req"][0], parsed["last_done"])

    def test_an_answered_session_is_not(self):
        parsed = dashboard._parse_kimi_wire(self._session("interactive-work", True))
        self.assertLessEqual(parsed["last_req"][0], parsed["last_done"])

    def test_token_totals_are_kept_for_every_session(self):
        """kimi's stream-json carries no usage, so wire logs are the only
        source — excluding fleet sessions from in-flight must not lose them."""
        parsed = dashboard._parse_kimi_wire(self._session("fleet-task", True))
        self.assertEqual(parsed["file_totals"]["prompt"], 10)
        self.assertEqual(parsed["file_totals"]["completion"], 5)


if __name__ == "__main__":
    unittest.main()


class RepoValidation(unittest.TestCase):
    """A project must be rejected at CREATE time if its repo cannot host a run.

    Without this the task file is written happily and the failure surfaces
    minutes later inside gitstore.alloc as "fatal: not in a git directory" or
    "fatal: invalid reference: main" — by which point a worktree and a DB row
    already exist and the operator has no idea what went wrong.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def _git(self, repo, *args):
        import subprocess
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True, text=True)

    def test_plain_directory_is_rejected(self):
        d = self.root / "plain"; d.mkdir()
        msg = dashboard._repo_problem(d)
        self.assertIsNotNone(msg)
        self.assertIn("not a git repository", msg)

    def test_git_repo_without_a_base_commit_is_rejected(self):
        d = self.root / "empty"; d.mkdir()
        self._git(d, "init", "-q")
        msg = dashboard._repo_problem(d)
        self.assertIsNotNone(msg)
        self.assertIn("no 'main' branch", msg)

    def test_a_usable_repo_passes(self):
        d = self.root / "good"; d.mkdir()
        self._git(d, "init", "-q", "-b", "main")
        self._git(d, "config", "user.email", "t@t")
        self._git(d, "config", "user.name", "t")
        (d / "f.txt").write_text("hi\n")
        self._git(d, "add", "-A")
        self._git(d, "commit", "-qm", "init")
        self.assertIsNone(dashboard._repo_problem(d))

    def test_error_message_tells_the_operator_what_to_do(self):
        d = self.root / "plain2"; d.mkdir()
        msg = dashboard._repo_problem(d)
        self.assertIn("git init", msg)


class AtomicTaskfileWrite(unittest.TestCase):
    """A crash mid-write must not leave a task file that breaks everything.

    A plain write truncates first and fills after; a process killed in that
    window leaves a ZERO-BYTE task file, which then fails every later run of
    it and every check.sh gate. Observed for real on 2026-09-09.
    """

    def test_write_is_atomic_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "proj.json"
            doc = {"project": {"repo": "/tmp", "title": "T", "tasks": []}}
            dashboard._write_taskfile_atomically(f, doc)
            self.assertEqual(json.loads(f.read_text()), doc)
            self.assertEqual([p.name for p in Path(d).iterdir()], ["proj.json"])

    def test_overwriting_never_exposes_an_empty_file(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "proj.json"
            f.write_text('{"project": {"tasks": ["old"]}}')
            dashboard._write_taskfile_atomically(
                f, {"project": {"repo": "/tmp", "title": "new", "tasks": []}})
            self.assertEqual(json.loads(f.read_text())["project"]["title"], "new")
            self.assertGreater(f.stat().st_size, 0)


class ProjectPhase(unittest.TestCase):
    """One word for "what is this project doing", because a dict of five
    status counters does not answer the operator's actual question: what
    needs me? `attention` means finished executing with something unresolved."""

    def test_running_wins_over_everything(self):
        self.assertEqual(
            dashboard._project_phase({"merged": 2, "failed": 1}, ["a", "b", "c"], 123),
            "running")
        self.assertEqual(
            dashboard._project_phase({"running": 1, "failed": 1}, ["a", "b"], None),
            "running")

    def test_all_merged_is_done(self):
        self.assertEqual(
            dashboard._project_phase({"merged": 3}, ["a", "b", "c"], None), "done")

    def test_a_failure_needs_attention_even_when_the_rest_merged(self):
        self.assertEqual(
            dashboard._project_phase({"merged": 2, "failed": 1}, ["a", "b", "c"], None),
            "attention")

    def test_a_conflict_needs_attention(self):
        self.assertEqual(
            dashboard._project_phase({"conflict": 1}, ["a"], None), "attention")

    def test_never_run_is_new(self):
        self.assertEqual(dashboard._project_phase({}, ["a", "b"], None), "new")

    def test_partially_merged_but_idle_needs_attention(self):
        """Nothing running, not everything merged: it stopped short."""
        self.assertEqual(
            dashboard._project_phase({"merged": 1}, ["a", "b"], None), "attention")


class ProjectArchive(unittest.TestCase):
    """Archiving is a dashboard concern: the task file is never touched and
    the project stays runnable. It exists so finished work stops burying the
    projects that still need the operator."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = __import__("store").Store(str(Path(self._dir.name) / "t.db"))

    def tearDown(self):
        self._dir.cleanup()

    def test_archive_and_restore_round_trip(self):
        tf = "/tasks/x.json"
        self.assertEqual(self.store.archived_projects(), {})
        self.store.set_project_archived(tf, True)
        self.assertIn(tf, self.store.archived_projects())
        self.assertIsNotNone(self.store.archived_projects()[tf], "needs a timestamp")
        self.store.set_project_archived(tf, False)
        self.assertEqual(self.store.archived_projects(), {})

    def test_archiving_twice_is_idempotent(self):
        self.store.set_project_archived("/tasks/x.json", True)
        self.store.set_project_archived("/tasks/x.json", True)
        self.assertEqual(len(self.store.archived_projects()), 1)

    def test_projects_are_tracked_independently(self):
        self.store.set_project_archived("/tasks/a.json", True)
        self.store.set_project_archived("/tasks/b.json", True)
        self.store.set_project_archived("/tasks/a.json", False)
        self.assertEqual(list(self.store.archived_projects()), ["/tasks/b.json"])


class ProjectPayloadShape(unittest.TestCase):
    """The keys the console reads must keep meaning what it thinks they mean.

    `progress` has always been {done, total} and the compact project row
    renders `p.progress.done`. Adding a per-task progress map under the SAME
    key silently replaced it — Python keeps the last duplicate in a dict
    literal — so every row rendered "undefined/N". Nothing failed: the tests
    did not assert on payload shape and the render gate does not check values.
    """

    REQUIRED = {
        "file": str, "title": str, "n_tasks": int, "phase": str,
        "archived": bool, "progress": dict, "task_progress": dict,
        "statuses": dict, "dag": dict, "tokens": int, "seconds": float,
    }

    def test_no_duplicate_keys_in_the_project_dict_literal(self):
        """A duplicated key in a dict literal is legal Python and silently
        drops the earlier value — exactly how this broke."""
        import ast
        src = pathlib.Path("dashboard.py").read_text()
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Dict):
                continue
            names = [k.value for k in node.keys
                     if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            dupes = {n for n in names if names.count(n) > 1}
            self.assertFalse(dupes,
                             f"dict literal at line {node.lineno} repeats {dupes}")

    def test_progress_and_task_progress_are_different_things(self):
        self.assertIn("progress", self.REQUIRED)
        self.assertIn("task_progress", self.REQUIRED)
        src = pathlib.Path("dashboard.py").read_text()
        self.assertIn('"progress": {"done"', src,
                      "progress must stay the {done,total} rollup")


class LiveQueueView(unittest.TestCase):
    """What /api/queue reports as running vs waiting.

    These numbers tell the operator whether the fleet is busy or wedged, and
    which model the PR reviewers are stuck behind. A phantom queue entry — one
    left by a run that was killed — is worse than no queue view at all, so the
    liveness filtering is tested as carefully as the happy path.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.log = Path(self.dir) / "events.jsonl"
        self._orig = config.EVENTS_LOG
        config.EVENTS_LOG = str(self.log)
        dashboard._lines_cache["key"] = None
        self.addCleanup(self._restore)

    def _restore(self):
        config.EVENTS_LOG = self._orig
        dashboard._lines_cache["key"] = None

    def _write(self, *events_):
        self.log.write_text("".join(json.dumps(e) + "\n" for e in events_))
        dashboard._lines_cache["key"] = None

    def _ev(self, type, task, model, role="pr_reviewer", age=5, **kw):
        return dict(type=type, task=task, model=model, role=role, attempt=1,
                    pid=os.getpid(), ts=time.time() - age, **kw)

    def _store(self, leases=()):
        class S:
            def driver_lease_rows(self_):
                return list(leases)
        return S()

    def test_a_queued_attempt_with_no_start_is_waiting(self):
        self._write(self._ev("driver.queued", "t1", STRONGEST))
        q = dashboard._queue(self._store())
        self.assertEqual(q["totals"]["waiting"], 1)
        self.assertEqual(q["waiting"][0]["task"], "t1")

    def test_driver_start_settles_the_wait(self):
        self._write(self._ev("driver.queued", "t1", STRONGEST, age=9),
                    self._ev("driver.start", "t1", STRONGEST, age=8))
        self.assertEqual(dashboard._queue(self._store())["totals"]["waiting"], 0)

    def test_done_and_error_also_settle_it(self):
        for terminal in ("driver.done", "driver.error", "driver.cancelled",
                         "driver.timeout", "driver.cap_timeout"):
            with self.subTest(terminal=terminal):
                self._write(self._ev("driver.queued", "t1", "GLM-5.3", age=9),
                            self._ev(terminal, "t1", "GLM-5.3", age=8))
                self.assertEqual(
                    dashboard._queue(self._store())["totals"]["waiting"], 0)

    def test_a_wait_left_by_a_dead_run_is_not_shown(self):
        e = self._ev("driver.queued", "t1", STRONGEST)
        e["pid"] = 2 ** 22  # never a live pid
        self._write(e)
        self.assertEqual(dashboard._queue(self._store())["totals"]["waiting"], 0)

    def test_a_wait_that_has_gone_quiet_is_not_shown(self):
        self._write(self._ev("driver.queued", "t1", STRONGEST,
                             age=dashboard.WAIT_STALE_S + 60))
        self.assertEqual(dashboard._queue(self._store())["totals"]["waiting"], 0)

    def test_it_separates_the_process_queue_from_the_fleet_queue(self):
        self._write(self._ev("driver.slot_wait", "t1", "GLM-5.3"),
                    self._ev("driver.cap_wait", "t2", "GLM-5.3", in_use=4, cap=4))
        scopes = {w["task"]: w["scope"] for w in dashboard._queue(self._store())["waiting"]}
        self.assertEqual(scopes, {"t1": "process", "t2": "fleet"})

    def test_pr_reviewers_are_counted_separately_from_implementers(self):
        self._write(self._ev("driver.queued", "t1", STRONGEST, role="pr_reviewer"),
                    self._ev("driver.queued", "t2", STRONGEST, role="implementer"))
        q = dashboard._queue(self._store())
        self.assertEqual(q["totals"]["waiting"], 2)
        self.assertEqual(q["totals"]["reviewers_waiting"], 1)
        kimi = next(m for m in q["models"] if m["model"] == STRONGEST)
        self.assertEqual(kimi["reviewers_waiting"], 1)

    def test_running_comes_from_live_leases_and_reports_free_slots(self):
        self._write(self._ev("driver.start", "t1", STRONGEST, role="pr_reviewer"))
        q = dashboard._queue(self._store([
            {"id": 1, "model": STRONGEST, "pid": os.getpid(), "task": "t1",
             "acquired_at": time.time() - 30}]))
        self.assertEqual(q["totals"]["running"], 1)
        self.assertEqual(q["running"][0]["role_label"], "PR review")
        kimi = next(m for m in q["models"] if m["model"] == STRONGEST)
        self.assertEqual((kimi["running"], kimi["free"]), (1, kimi["cap"] - 1))

    def test_a_lease_whose_run_died_does_not_pin_a_slot(self):
        q = dashboard._queue(self._store([
            {"id": 1, "model": STRONGEST, "pid": 2 ** 22, "task": "t1",
             "acquired_at": time.time() - 30}]))
        self.assertEqual(q["totals"]["running"], 0)

    def test_every_known_model_appears_even_when_idle(self):
        self._write()
        models = {m["model"] for m in dashboard._queue(self._store())["models"]}
        self.assertIn(STRONGEST, models)
        self.assertIn("GLM-5.3", models)

    def test_a_harness_wait_is_shown_under_the_real_model(self):
        # drivers report the harness lease with report_as=<real model> so one
        # attempt does not split into two rows, one of them under a model name
        # ("harness:opencode") that does not exist.
        self._write(self._ev("driver.cap_wait", "t1", "GLM-5.3",
                             scope="harness", harness="opencode", cap=5))
        q = dashboard._queue(self._store())
        self.assertEqual(len(q["waiting"]), 1)
        row = q["waiting"][0]
        self.assertEqual(row["model"], "GLM-5.3")
        self.assertEqual(row["scope"], "harness")
        self.assertEqual(q["harnesses"][0]["waiting"], 1)

    def test_a_harness_lease_is_capacity_not_a_second_running_task(self):
        now = time.time()
        q = dashboard._queue(self._store([
            {"id": 1, "model": "GLM-5.3", "pid": os.getpid(), "task": "t1",
             "acquired_at": now - 10},
            {"id": 2, "model": "harness:opencode", "pid": os.getpid(),
             "task": "t1", "acquired_at": now - 10}]))
        self.assertEqual(q["totals"]["running"], 1)
        oc = next(h for h in q["harnesses"] if h["harness"] == "opencode")
        self.assertEqual((oc["running"], oc["free"]), (1, oc["cap"] - 1))

    def test_harness_rows_never_appear_as_models(self):
        q = dashboard._queue(self._store([
            {"id": 1, "model": "harness:opencode", "pid": os.getpid(),
             "task": "t1", "acquired_at": time.time()}]))
        self.assertEqual([m for m in q["models"]
                          if m["model"].startswith("harness:")], [])

    def test_a_task_queued_for_the_harness_is_not_also_reported_running(self):
        # It holds its model lease but not yet the harness lease nested inside
        # it. Reporting it as both running and queued double-counts one attempt.
        self._write(self._ev("driver.cap_wait", "t1", "GLM-5.3",
                             scope="harness", harness="opencode", cap=5))
        q = dashboard._queue(self._store([
            {"id": 1, "model": "GLM-5.3", "pid": os.getpid(), "task": "t1",
             "acquired_at": time.time() - 5}]))
        self.assertEqual(q["totals"]["running"], 0)
        self.assertEqual(q["totals"]["waiting"], 1)

    def test_a_task_holding_every_slot_is_reported_running(self):
        self._write(self._ev("driver.start", "t1", "GLM-5.3"))
        q = dashboard._queue(self._store([
            {"id": 1, "model": "GLM-5.3", "pid": os.getpid(), "task": "t1",
             "acquired_at": time.time() - 5}]))
        self.assertEqual((q["totals"]["running"], q["totals"]["waiting"]), (1, 0))

    def test_a_start_settles_a_cap_wait_from_the_same_attempt(self):
        # cap_wait and driver.start must key identically, or the wait is never
        # cleared and the panel shows a queue that has already been served.
        self._write(self._ev("driver.queued", "t1", STRONGEST, age=30),
                    self._ev("driver.cap_wait", "t1", STRONGEST, age=20,
                             in_use=3, cap=3),
                    self._ev("driver.start", "t1", STRONGEST, age=10))
        self.assertEqual(dashboard._queue(self._store())["totals"]["waiting"], 0)

    def test_a_cap_wait_with_no_start_is_still_a_wait(self):
        self._write(self._ev("driver.queued", "t1", STRONGEST, age=30),
                    self._ev("driver.cap_wait", "t1", STRONGEST, age=20,
                             in_use=3, cap=3))
        self.assertEqual(dashboard._queue(self._store())["totals"]["waiting"], 1)


class SeekingIntoTheEventLog(unittest.TestCase):
    """Finding where a time window starts, without walking the whole log.

    The dashboard only counts TODAY's merges and failures, but its client
    walked the event log from line 0 on every page load — the entire history
    the fleet has ever emitted, which rotates only at 100MB.
    """

    def _lines(self, *ts):
        return [json.dumps({"ts": t, "type": "x"}) for t in ts]

    def test_it_finds_the_first_event_at_the_cutoff(self):
        lines = self._lines(1, 2, 3, 4, 5)
        self.assertEqual(dashboard._first_event_at_or_after(lines, 3), 2)

    def test_an_exact_match_is_included_not_skipped(self):
        lines = self._lines(10, 20, 30)
        self.assertEqual(dashboard._first_event_at_or_after(lines, 20), 1)

    def test_a_cutoff_before_everything_returns_the_start(self):
        self.assertEqual(dashboard._first_event_at_or_after(self._lines(5, 6), 1), 0)

    def test_a_cutoff_after_everything_returns_the_end(self):
        lines = self._lines(5, 6)
        self.assertEqual(dashboard._first_event_at_or_after(lines, 99), len(lines))

    def test_an_empty_log_is_not_an_error(self):
        self.assertEqual(dashboard._first_event_at_or_after([], 5), 0)

    def test_malformed_and_ts_less_lines_are_stepped_over(self):
        # A bisect cannot do this, which is why the scan is linear.
        lines = [json.dumps({"ts": 1, "type": "x"}), "{not json",
                 json.dumps({"type": "no-ts"}),
                 json.dumps({"ts": 9, "type": "x"})]
        self.assertEqual(dashboard._first_event_at_or_after(lines, 9), 3)

    def test_it_seeks_rather_than_scanning_from_zero(self):
        lines = self._lines(*range(1, 501))
        self.assertEqual(dashboard._first_event_at_or_after(lines, 480), 479)


class StrandedPullRequests(unittest.TestCase):
    """A PR nobody is working on looks identical to a healthy open one.

    This is the failure that cost this repo the most: publish opened the PR,
    pr_review never ran, the run ended, and the branch sat on GitHub with no
    process ever coming back for it. Seven at once, and the UI showed seven
    ordinary open pull requests. The detector is deliberately conservative —
    calling a live PR abandoned is worse than staying quiet.
    """

    LIVE = "/t/live.json"
    IDLE = "/t/idle.json"

    def _owner(self, status="in_review", taskfile=None):
        return {"t1": {"id": "t1", "status": status,
                       "taskfile": taskfile or self.IDLE}}

    def _pr(self, **kw):
        base = {"state": "OPEN", "number": 1, "task": "t1"}
        base.update(kw)
        return base

    def test_an_open_pr_with_no_run_is_stranded(self):
        self.assertTrue(dashboard._pr_is_stranded(
            self._pr(), self._owner(), {self.LIVE}))

    def test_a_pr_whose_project_is_running_is_not(self):
        self.assertFalse(dashboard._pr_is_stranded(
            self._pr(), self._owner(taskfile=self.LIVE), {self.LIVE}))

    def test_a_closed_or_merged_pr_is_never_stranded(self):
        for state in ("MERGED", "CLOSED"):
            self.assertFalse(dashboard._pr_is_stranded(
                self._pr(state=state), self._owner(), {self.LIVE}))

    def test_a_finished_task_is_not_stranded(self):
        for status in ("merged", "failed"):
            self.assertFalse(dashboard._pr_is_stranded(
                self._pr(), self._owner(status=status), {self.LIVE}))

    def test_nothing_is_reported_when_the_run_list_is_unreadable(self):
        # live=None means we could not tell. Report nothing, not everything.
        self.assertFalse(dashboard._pr_is_stranded(self._pr(), self._owner(), None))

    def test_a_pr_the_fleet_does_not_own_is_left_alone(self):
        # Probably a human's branch; calling their PR abandoned is worse than
        # saying nothing.
        self.assertFalse(dashboard._pr_is_stranded(
            self._pr(task=None, headRefName="feature/mine"), self._owner(), set()))

    def test_the_task_is_recovered_from_the_branch_name(self):
        pr = self._pr(task=None, headRefName="task/t1")
        self.assertTrue(dashboard._pr_is_stranded(pr, self._owner(), set()))


class UsageRangesAndTimeline(unittest.TestCase):
    """Finer windows, a calendar day, and failures that show on the timeline."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.log = Path(self.dir) / "events.jsonl"
        self._orig = config.EVENTS_LOG
        config.EVENTS_LOG = str(self.log)
        dashboard._lines_cache["key"] = None
        # _usage also merges kimi-code CLI sessions read from the real
        # ~/.kimi-code/sessions, which made this fixture report 194 requests
        # instead of 1. Stubbed because these tests are about the EVENT-LOG
        # window; the kimi merge has its own range coverage in
        # tests/test_usage_range.py, and stubbing it THERE is what let a
        # windowing bug through once already.
        self._orig_kimi = dashboard._kimi_code_usage
        dashboard._kimi_code_usage = lambda *a, **k: {
            "models": [], "inflight": [], "points": [], "turns": []}
        self.addCleanup(self._restore)

    def _restore(self):
        config.EVENTS_LOG = self._orig
        dashboard._kimi_code_usage = self._orig_kimi
        dashboard._lines_cache["key"] = None

    def _write(self, *rows):
        self.log.write_text("".join(json.dumps(r) + "\n" for r in rows))
        dashboard._lines_cache["key"] = None

    def _done(self, age_s, tokens=100, model="GLM-5.3"):
        return {"type": "driver.done", "model": model, "harness": "opencode",
                "ts": time.time() - age_s, "tokens": tokens, "seconds": 1}

    def _err(self, age_s, model="GLM-5.3"):
        return {"type": "driver.error", "model": model, "harness": "opencode",
                "ts": time.time() - age_s, "error": "concurrent session limit"}

    def test_the_finer_windows_exist(self):
        for r in ("1h", "3h", "6h", "today", "24h", "7d", "all"):
            self.assertIn(r, dashboard.RANGES)

    def test_three_hours_excludes_a_four_hour_old_request(self):
        self._write(self._done(4 * 3600), self._done(60))
        self.assertEqual(dashboard._usage(None, "3h")["totals"]["requests"], 1)
        self.assertEqual(dashboard._usage(None, "6h")["totals"]["requests"], 2)

    def test_today_is_a_calendar_day_not_a_rolling_one(self):
        """At 09:00 a rolling day is mostly yesterday.

        'What has the fleet done today' is the question an operator asks, and
        a 24h window answers a different one.
        """
        now = time.time()
        start = dashboard._range_cutoff("today", now)
        lt = time.localtime(start)
        self.assertEqual((lt.tm_hour, lt.tm_min, lt.tm_sec), (0, 0, 0))
        self.assertLessEqual(start, now)
        self.assertGreaterEqual(start, now - 86400)

    def test_today_never_reaches_further_back_than_24h(self):
        self._write(self._done(30 * 3600), self._done(60))
        today = dashboard._usage(None, "today")["totals"]["requests"]
        day = dashboard._usage(None, "24h")["totals"]["requests"]
        self.assertLessEqual(today, day)

    def test_a_failed_driver_attempt_reaches_the_timeline(self):
        # It used to increment a counter and `continue`, so an hour of capacity
        # rejections rendered as a QUIET hour rather than a bad one.
        self._write(self._err(60), self._err(120), self._done(90))
        u = dashboard._usage(None, "1h", include_series=True)
        in_series = sum(p["errors"] for v in u["series"].values() for p in v)
        self.assertEqual(in_series, 2)
        self.assertEqual(u["totals"]["failed_attempts"], 2)

    def test_the_timeline_reconciles_with_the_totals(self):
        self._write(*[self._err(i * 30) for i in range(1, 8)])
        u = dashboard._usage(None, "1h", include_series=True)
        self.assertEqual(sum(p["errors"] for v in u["series"].values() for p in v),
                         u["totals"]["failed_attempts"])

    def test_every_series_bucket_has_an_errors_key(self):
        self._write(self._done(60))
        u = dashboard._usage(None, "1h", include_series=True)
        for fam, pts in u["series"].items():
            for p in pts:
                self.assertIn("errors", p, f"{fam} bucket missing errors")

    def test_an_unknown_range_still_falls_back_to_an_hour(self):
        self._write(self._done(4 * 3600), self._done(60))
        self.assertEqual(dashboard._usage(None, "nonsense")["range"], "1h")


class ProjectTaskModelPricing(unittest.TestCase):
    """Each model's tokens under one task id are priced at that model's own rate.

    A task id carries work from several models at once — a gpt-oss-120b
    implementer cross-reviewed by GLM-5.3, a task that escalates tiers. Pricing
    the whole bucket at the taskfile's implementer charges GLM's expensive
    reviewer tokens at gpt-oss's cheap rate, so the figure was not even an
    upper bound. The rollup must accumulate per-event at the event's own model.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self._env = {k: v for k, v in os.environ.items() if k.startswith("ARC_PRICE_")}
        for k in self._env:
            os.environ.pop(k)

        self._orig_tasks = config.TASKS_DIR
        config.TASKS_DIR = str(self.root / "tasks")
        Path(config.TASKS_DIR).mkdir()

        self._orig_log = config.EVENTS_LOG
        config.EVENTS_LOG = str(self.root / "events.jsonl")
        dashboard._lines_cache["key"] = None
        dashboard._kimi_task_tokens_cache.update(key=0.0, map={})

        self._orig_kimi = dashboard._kimi_tokens_by_task
        dashboard._kimi_tokens_by_task = lambda: {}
        self._orig_inflight = dashboard._collect_inflight
        dashboard._collect_inflight = lambda now, store=None: ([], {})
        self._orig_transcript = dashboard._transcript_toks
        dashboard._transcript_toks = lambda p: (0, 0, 0)

        import reconcile
        self._orig_live_runs = reconcile.live_runs
        reconcile.live_runs = lambda: []

        self.store = __import__("store").Store(str(self.root / "s.db"))

    def tearDown(self):
        import reconcile
        reconcile.live_runs = self._orig_live_runs
        dashboard._transcript_toks = self._orig_transcript
        dashboard._collect_inflight = self._orig_inflight
        dashboard._kimi_tokens_by_task = self._orig_kimi
        dashboard._kimi_task_tokens_cache.update(key=0.0, map={})
        dashboard._lines_cache["key"] = None
        config.EVENTS_LOG = self._orig_log
        config.TASKS_DIR = self._orig_tasks
        for k in list(os.environ):
            if k.startswith("ARC_PRICE_") and k not in self._env:
                os.environ.pop(k)
        os.environ.update(self._env)
        self._dir.cleanup()

    def _taskfile(self, model=config.ESCALATION_PATH[0], reviewer="glm"):
        tf = Path(config.TASKS_DIR) / "mixed.json"
        tf.write_text(json.dumps({"project": {"repo": "/x", "title": "mixed", "tasks": [
            {"id": "t1", "title": "t1", "model": model, "reviewer": reviewer,
             "verify_cmd": "true", "deps": []}]}}))
        return tf

    def _events(self, *events_):
        Path(config.EVENTS_LOG).write_text(
            "".join(json.dumps(e) + "\n" for e in events_))
        dashboard._lines_cache["key"] = None

    def _node(self):
        projects = dashboard._projects(self.store)
        self.assertEqual(len(projects), 1)
        for n in projects[0]["dag"]["nodes"]:
            if n["id"] == "t1":
                return n
        self.fail("t1 node not found")

    @needs_two_rates
    def test_each_models_tokens_are_priced_at_its_own_rate(self):
        """One task, two models, two rates -- not one blended rate.

        The pair must be picked BY PRICE: naming the implementer and the
        reviewer positionally (ESCALATION_PATH[0] and "GLM-5.3") made the test
        vacuous the day the roster reordered and both names resolved to the
        same model, at which point it could no longer fail.
        """
        impl, rev = PRICE_PAIR
        self._taskfile(model=impl, reviewer=config.MODEL_FAMILY[rev])
        self._events(
            {"type": "driver.done", "task": "t1", "harness": "opencode",
             "model": impl, "role": "implementer",
             "tokens": 1000, "prompt_tokens": 800, "completion_tokens": 200,
             "seconds": 60},
            {"type": "driver.done", "task": "t1", "harness": "opencode",
             "model": rev, "role": "reviewer",
             "tokens": 100, "prompt_tokens": 50, "completion_tokens": 50,
             "seconds": 30},
        )
        node = self._node()
        expected = config.cost_of(impl, 800, 200) + config.cost_of(rev, 50, 50)
        self.assertEqual(node["cost"], round(expected, 4))
        blended = config.cost_of(impl, 850, 250)
        self.assertNotEqual(node["cost"], round(blended, 4),
                            f"{rev} reviewer tokens must not be priced at "
                            f"{impl}'s rate")

    @needs_kimi_harness
    def test_kimi_wire_extra_is_priced_at_kimi_completion_rate(self):
        # The wire log belongs to the kimi HARNESS, so its tokens price at the
        # kimi-harness model's rate. This asserted STRONGEST's rate, which was
        # only ever right because Kimi-K3 happened to be the top tier.
        kimi = KIMI_HARNESS_MODEL
        self._taskfile(model=kimi, reviewer=config.cross_family_reviewer(kimi))
        dashboard._kimi_tokens_by_task = lambda: {"t1": 2000}
        # No driver.done for kimi: the wire log is the only source of its tokens,
        # and it carries no prompt/completion split.
        self._events()
        node = self._node()
        self.assertEqual(node["tokens"], 2000)
        self.assertEqual(node["tokens_source"], "kimi-wire")
        self.assertEqual(node["cost"], round(config.cost_of(kimi, 0, 2000), 4))


class TheServerKnowsWhenItIsStale(unittest.TestCase):
    """The fleet merges changes to this server's own source while it runs.

    A running process keeps serving what it loaded, so a merged route 404s and
    a renamed function takes the page blank — that is how the console went
    dark once. The UI banner for this has existed since c59a76f, reading a
    field the server never produced: six merges landed on 09-11 and the banner
    stayed hidden through every one of them.
    """

    def setUp(self):
        self._head, self._cache = dashboard._SERVED_HEAD, dict(dashboard._stale_cache)
        dashboard._stale_cache.update(key=0.0, files=[])
        self.addCleanup(self._restore)

    def _restore(self):
        dashboard._SERVED_HEAD = self._head
        dashboard._stale_cache.update(self._cache)

    def test_a_server_matching_the_repo_reports_nothing_stale(self):
        dashboard._SERVED_HEAD = dashboard._git_out("rev-parse", "HEAD").strip()
        dashboard._stale_cache.update(key=0.0)
        self.assertEqual(dashboard._stale_source(), [])

    def test_a_server_behind_the_repo_names_the_changed_source_files(self):
        # Pretend we started at the previous commit that touched dashboard.py.
        older = dashboard._git_out("log", "-2", "--format=%H", "--",
                                   "dashboard.py").split()
        if len(older) < 2:
            self.skipTest("repo has fewer than two commits touching dashboard.py")
        dashboard._SERVED_HEAD = older[1]
        dashboard._stale_cache.update(key=0.0)
        files = dashboard._stale_source()
        self.assertIn("dashboard.py", files)

    def test_only_source_the_server_executes_or_serves_counts(self):
        for f in dashboard._stale_source():
            self.assertTrue(f.startswith(dashboard._SOURCE_PATHS),
                            f"{f} is not something this process runs or serves")

    def test_no_git_means_nothing_to_be_stale_relative_to(self):
        dashboard._SERVED_HEAD = None
        dashboard._stale_cache.update(key=0.0)
        self.assertEqual(dashboard._stale_source(), [])

    def test_the_answer_is_cached_between_polls(self):
        dashboard._SERVED_HEAD = "0" * 40  # a sha that will never match HEAD
        dashboard._stale_cache.update(key=0.0)
        first = dashboard._stale_source(now=1000.0)
        dashboard._SERVED_HEAD = dashboard._git_out("rev-parse", "HEAD").strip()
        self.assertEqual(dashboard._stale_source(now=1005.0), first,
                         "within the cache window the old answer must be returned")

    def test_health_carries_the_field_the_banner_reads(self):
        h = dashboard._health(None)
        self.assertIn("stale_source", h)
        self.assertIsInstance(h["stale_source"], list)
        self.assertIn("served_head", h)


class RetryActuallyRuns(unittest.TestCase):
    """Resetting a task to `pending` is not a retry unless something runs it.

    The button relied on "the next `code run`", which nothing schedules. A
    retry clicked while the fleet was idle changed a label and did nothing
    else; graph-admission-control sat `pending` for nine hours that way.
    """

    def test_the_retry_path_launches_when_no_run_holds_the_file(self):
        src = pathlib.Path(dashboard.__file__).read_text()
        body = src[src.index("def _retry_task"):]
        body = body[:body.index("\ndef ", 10)]
        self.assertIn("_run_project(", body,
                      "retry must start a run, not only flip a status")
        self.assertIn("reconcile.live_runs()", body,
                      "and must not start a second run beside a live one")

    def test_the_response_says_what_happened(self):
        src = pathlib.Path(dashboard.__file__).read_text()
        body = src[src.index("def _retry_task"):]
        body = body[:body.index("\ndef ", 10)]
        for key in ('"launched_pid"', '"note"'):
            self.assertIn(key, body)


class ManualEscalation(unittest.TestCase):
    """The operator can move a task up a tier before the fix budget does.

    The fix budget escalates only after repeated failure. An operator watching
    a planner on gpt-oss produce thin task lists can see the problem well before
    three rounds prove it. The override is read by cur_model() at every node
    boundary, so a RUNNING task moves up at its very next step.
    """

    def setUp(self):
        import shutil
        import store as _store
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self._tasks = config.TASKS_DIR
        config.TASKS_DIR = self.dir
        self.addCleanup(setattr, config, "TASKS_DIR", self._tasks)
        self.tf = Path(self.dir) / "p.json"
        self.tf.write_text(json.dumps({"project": {"repo": "/x", "title": "p", "tasks": [
            {"id": "t1", "title": "T", "prompt": "p", "model": config.ESCALATION_PATH[0],
             "reviewer": "kimi", "verify_cmd": "", "files_hint": [], "deps": []}]}}))
        self._store = dashboard.Handler.store
        dashboard.Handler.store = _store.Store(":memory:")
        dashboard.Handler.store.upsert_code_task(str(self.tf), "t1", "T", config.ESCALATION_PATH[0],
                                                 "kimi", "running")
        self.addCleanup(setattr, dashboard.Handler, "store", self._store)
        import reconcile
        self._live = reconcile.live_runs
        reconcile.live_runs = lambda: []
        self.addCleanup(setattr, reconcile, "live_runs", self._live)

    def _task(self):
        return json.loads(self.tf.read_text())["project"]["tasks"][0]

    def test_default_escalates_one_tier_up(self):
        out, code = dashboard._escalate_task({"file": "p.json", "task": "t1"})
        self.assertEqual(code, 200, out)
        self.assertEqual((out["from"], out["to"]),
                         (config.ESCALATION_PATH[0], config.ESCALATION_PATH[1]))

    def test_the_taskfile_is_rewritten_so_a_fresh_run_starts_higher(self):
        dashboard._escalate_task({"file": "p.json", "task": "t1"})
        self.assertEqual(self._task()["model"], config.ESCALATION_PATH[1])

    def test_the_override_is_stored_for_the_running_graph(self):
        dashboard._escalate_task({"file": "p.json", "task": "t1", "to_model": STRONGEST})
        self.assertEqual(dashboard.Handler.store.get_model_override(str(self.tf), "t1"),
                         STRONGEST)

    def test_the_reviewer_follows_the_implementer_across_families(self):
        # The strongest model's work must be reviewed by another family, never
        # by itself — kimi->glm today, glm->deepseek after Kimi leaves.
        out, _ = dashboard._escalate_task({"file": "p.json", "task": "t1", "to_model": STRONGEST})
        self.assertEqual(out["reviewer"], STRONGEST_REVIEWER)
        self.assertNotEqual(out["reviewer"], STRONGEST_FAMILY)
        self.assertEqual(self._task()["reviewer"], STRONGEST_REVIEWER)

    def test_it_refuses_to_move_down(self):
        dashboard._escalate_task({"file": "p.json", "task": "t1", "to_model": STRONGEST})
        out, code = dashboard._escalate_task({"file": "p.json", "task": "t1",
                                              "to_model": ENTRY})
        self.assertEqual(code, 409)
        self.assertIn("only moves up", out["error"])

    def test_the_top_tier_cannot_go_higher(self):
        dashboard._escalate_task({"file": "p.json", "task": "t1", "to_model": STRONGEST})
        out, code = dashboard._escalate_task({"file": "p.json", "task": "t1"})
        self.assertEqual(code, 409)
        self.assertIn("top tier", out["error"])

    def test_an_unknown_model_is_rejected(self):
        out, code = dashboard._escalate_task({"file": "p.json", "task": "t1", "to_model": "GPT-9"})
        self.assertEqual(code, 400)

    def test_it_says_whether_a_live_run_will_pick_it_up(self):
        import reconcile
        reconcile.live_runs = lambda: [{"taskfile": str(self.tf), "pid": 1}]
        out, _ = dashboard._escalate_task({"file": "p.json", "task": "t1"})
        self.assertTrue(out["live"])
        self.assertIn("next step", out["note"])

    def test_the_event_is_marked_manual(self):
        with capture_events() as ev:
            dashboard._escalate_task({"file": "p.json", "task": "t1"})
        esc = [f for t, f in ev.seen if t == "task.escalated"]
        self.assertTrue(esc and esc[0].get("manual"))


class TheRunningGraphHonoursTheOverride(unittest.TestCase):
    """cur_model() must see a manual override at its next call — no restart."""

    def _graph_and_store(self):
        import store as _store
        st = _store.Store(":memory:")
        ts = code_tasks.load_taskfile(taskfile([BASIC]))
        with capture_events():
            g = code_tasks.build_code_graph(st, ts, taskfile="tf.json")
        return g, st

    def test_an_override_wins_over_the_declared_model(self):
        import code_tasks as ct
        g, st = self._graph_and_store()
        # reach cur_model through a node that exposes it: escalate's "from"
        st.set_model_override("tf.json", "t1", "GLM-5.3", "test")
        # implement records the model it is about to use via upsert; read it back
        import asyncio as aio
        orig = ct._driver
        seen = {}
        class FakeDrv:
            harness = "x"
            async def run(self_, *a, **k):
                class R: session_id=None; exit_code=0; transcript_path=""; seconds=0.0
                return R()
        ct._driver = lambda model, role, pol: seen.setdefault("model", model) and FakeDrv() or FakeDrv()
        try:
            aio.run(g.nodes["implement_t1"].fn(
                {"results": {"alloc_t1": {"worktree": "/tmp"}}, "runs": {}}))
        finally:
            ct._driver = orig
        self.assertEqual(seen.get("model"), "GLM-5.3")

    def test_the_higher_of_manual_and_automatic_wins(self):
        import code_tasks as ct, asyncio as aio
        g, st = self._graph_and_store()
        st.set_model_override("tf.json", "t1", ENTRY, "test")  # manual: medium
        orig = ct._driver; seen = {}
        class FakeDrv:
            harness = "x"
            async def run(self_, *a, **k):
                class R: session_id=None; exit_code=0; transcript_path=""; seconds=0.0
                return R()
        ct._driver = lambda model, role, pol: seen.setdefault("model", model) and FakeDrv() or FakeDrv()
        try:
            # the graph itself already auto-escalated to Kimi: that is higher
            aio.run(g.nodes["implement_t1"].fn(
                {"results": {"alloc_t1": {"worktree": "/tmp"},
                             "escalate_t1": {"to_model": STRONGEST}}, "runs": {}}))
        finally:
            ct._driver = orig
        self.assertEqual(seen.get("model"), STRONGEST)


class ThePipelineExplainer(unittest.TestCase):
    """Clicking a stage must explain it, and the explanation must not drift.

    The prose is hand-written — what a node is for, and what goes wrong with
    it, are things no introspection recovers. Everything else is generated from
    the graph the fleet actually builds, because a picture that claims a node
    retries three times while the code says ten is worse than no picture.
    """

    def _doc(self):
        import pipeline_doc
        return pipeline_doc.describe(dashboard._build_graph_topologies()["code"])

    def test_every_node_in_the_diagram_is_explained(self):
        d = self._doc()
        missing = [n["id"] for n in d["nodes"] if not n["what"]]
        self.assertEqual(missing, [], f"nodes with no explanation: {missing}")

    def test_the_prose_covers_every_node_the_graph_builds(self):
        # The guard against drift in the other direction: a node added to the
        # pipeline with no entry here would render as a blank panel.
        import pipeline_doc
        built = {n["name"] for n in dashboard._build_graph_topologies()["code"]["nodes"]}
        self.assertEqual(built - set(pipeline_doc.NODES), set(),
                         "pipeline nodes with no entry in pipeline_doc.NODES")

    def test_edges_come_from_the_real_graph_not_the_prose(self):
        d = self._doc()
        impl = next(n for n in d["nodes"] if n["id"] == "implement")
        # implement is reached from many stages; that count is generated
        self.assertGreaterEqual(len(impl["incoming"]), 4)
        self.assertTrue(all("to" in e and "when" in e for e in impl["outgoing"]))

    def test_every_edge_condition_reads_as_english(self):
        d = self._doc()
        vague = [(n["id"], e["to"]) for n in d["nodes"] for e in n["outgoing"]
                 if e["when"] == "conditional"]
        self.assertEqual(vague, [],
                         f"edges with no plain-English reading: {vague}")

    def test_loops_are_marked_as_loops(self):
        d = self._doc()
        rev = next(n for n in d["nodes"] if n["id"] == "pr_review")
        back = [e for e in rev["outgoing"] if e["loop"]]
        self.assertTrue(back, "pr_review's send-back and re-review are loops")

    def test_live_stats_are_computed_from_the_event_log(self):
        """Fed a synthetic log, not this machine's.

        The first version asserted that SOME node had runs, which passed here
        only because the developer's event log happened to be in place — it
        would have failed on a fresh clone, and it tested the machine rather
        than the code.
        """
        import pipeline_doc
        now = time.time()
        rows = [
            {"type": "node_start", "node": "gate_t1", "ts": now - 100},
            {"type": "node_end", "node": "gate_t1", "ts": now - 90},
            {"type": "node_start", "node": "gate_t2", "ts": now - 80},
            {"type": "node_end", "node": "gate_t2", "ts": now - 50},
            {"type": "node_error", "node": "gate_t3", "ts": now - 40},
        ]
        d = Path(tempfile.mkdtemp())
        log = d / "events.jsonl"
        log.write_text("".join(json.dumps(r) + "\n" for r in rows))
        orig = config.EVENTS_LOG
        config.EVENTS_LOG = str(log)
        try:
            stats = pipeline_doc._stats()
        finally:
            config.EVENTS_LOG = orig
        g = stats["gate"]
        self.assertEqual((g["runs"], g["errors"]), (2, 1))
        self.assertEqual(g["reliability"], 67)       # 2 of 3
        self.assertEqual(g["median_s"], 30.0)        # median of [10, 30]

    def test_stats_ignore_events_outside_the_window(self):
        import pipeline_doc
        now = time.time()
        rows = [{"type": "node_start", "node": "gate_old", "ts": now - 30 * 86400},
                {"type": "node_end", "node": "gate_old", "ts": now - 30 * 86400 + 5}]
        d = Path(tempfile.mkdtemp())
        log = d / "events.jsonl"
        log.write_text("".join(json.dumps(r) + "\n" for r in rows))
        orig = config.EVENTS_LOG
        config.EVENTS_LOG = str(log)
        try:
            self.assertEqual(pipeline_doc._stats(), {})
        finally:
            config.EVENTS_LOG = orig

    def test_the_overview_explains_the_whole_thing(self):
        o = self._doc()["overview"]
        self.assertGreaterEqual(len(o["paragraphs"]), 4)
        self.assertTrue(o["reading"], "the legend must say how to read the diagram")

    def test_a_node_with_no_runs_yet_still_describes_itself(self):
        import pipeline_doc
        orig = pipeline_doc._stats
        pipeline_doc._stats = lambda *a, **k: {}
        try:
            d = self._doc()
        finally:
            pipeline_doc._stats = orig
        self.assertTrue(all(n["what"] for n in d["nodes"]))
        self.assertTrue(all(n["stats"] is None for n in d["nodes"]))
