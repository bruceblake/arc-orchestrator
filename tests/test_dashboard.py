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

import config
import dashboard


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
                           "model": "Kimi-K3", "role": "implementer",
                           "task": "t1", "attempt": 1})
        rows, _ = dashboard._collect_inflight(now, None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "driver:kimi")

    def test_a_settled_driver_counts_as_none(self):
        now = time.time()
        base = {"harness": "kimi", "model": "Kimi-K3", "role": "implementer",
                "task": "t1", "attempt": 1}
        self.write_events({"ts": now - 30, "type": "driver.start", **base},
                          {"ts": now - 5, "type": "driver.done", **base})
        rows, _ = dashboard._collect_inflight(now, None)
        self.assertEqual(rows, [])

    def test_a_cancelled_driver_settles_too(self):
        """Without driver.cancelled this lingered as a phantom for ~19 min."""
        now = time.time()
        base = {"harness": "kimi", "model": "Kimi-K3", "role": "implementer",
                "task": "t1", "attempt": 1}
        self.write_events({"ts": now - 30, "type": "driver.start", **base},
                          {"ts": now - 5, "type": "driver.cancelled", **base})
        rows, _ = dashboard._collect_inflight(now, None)
        self.assertEqual(rows, [])

    def test_a_stale_driver_start_is_pruned(self):
        now = time.time()
        self.write_events({"ts": now - dashboard.DRIVER_STALE_S - 60,
                           "type": "driver.start", "harness": "kimi",
                           "model": "Kimi-K3", "role": "implementer",
                           "task": "t1", "attempt": 1})
        rows, _ = dashboard._collect_inflight(now, None)
        self.assertEqual(rows, [], "a killed run's start event must not count forever")


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
        lines = [{"type": "llm.request", "model": "Kimi-K3",
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
        self._write(self._ev("driver.queued", "t1", "Kimi-K3"))
        q = dashboard._queue(self._store())
        self.assertEqual(q["totals"]["waiting"], 1)
        self.assertEqual(q["waiting"][0]["task"], "t1")

    def test_driver_start_settles_the_wait(self):
        self._write(self._ev("driver.queued", "t1", "Kimi-K3", age=9),
                    self._ev("driver.start", "t1", "Kimi-K3", age=8))
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
        e = self._ev("driver.queued", "t1", "Kimi-K3")
        e["pid"] = 2 ** 22  # never a live pid
        self._write(e)
        self.assertEqual(dashboard._queue(self._store())["totals"]["waiting"], 0)

    def test_a_wait_that_has_gone_quiet_is_not_shown(self):
        self._write(self._ev("driver.queued", "t1", "Kimi-K3",
                             age=dashboard.WAIT_STALE_S + 60))
        self.assertEqual(dashboard._queue(self._store())["totals"]["waiting"], 0)

    def test_it_separates_the_process_queue_from_the_fleet_queue(self):
        self._write(self._ev("driver.slot_wait", "t1", "GLM-5.3"),
                    self._ev("driver.cap_wait", "t2", "GLM-5.3", in_use=4, cap=4))
        scopes = {w["task"]: w["scope"] for w in dashboard._queue(self._store())["waiting"]}
        self.assertEqual(scopes, {"t1": "process", "t2": "fleet"})

    def test_pr_reviewers_are_counted_separately_from_implementers(self):
        self._write(self._ev("driver.queued", "t1", "Kimi-K3", role="pr_reviewer"),
                    self._ev("driver.queued", "t2", "Kimi-K3", role="implementer"))
        q = dashboard._queue(self._store())
        self.assertEqual(q["totals"]["waiting"], 2)
        self.assertEqual(q["totals"]["reviewers_waiting"], 1)
        kimi = next(m for m in q["models"] if m["model"] == "Kimi-K3")
        self.assertEqual(kimi["reviewers_waiting"], 1)

    def test_running_comes_from_live_leases_and_reports_free_slots(self):
        self._write(self._ev("driver.start", "t1", "Kimi-K3", role="pr_reviewer"))
        q = dashboard._queue(self._store([
            {"id": 1, "model": "Kimi-K3", "pid": os.getpid(), "task": "t1",
             "acquired_at": time.time() - 30}]))
        self.assertEqual(q["totals"]["running"], 1)
        self.assertEqual(q["running"][0]["role_label"], "PR review")
        kimi = next(m for m in q["models"] if m["model"] == "Kimi-K3")
        self.assertEqual((kimi["running"], kimi["free"]), (1, kimi["cap"] - 1))

    def test_a_lease_whose_run_died_does_not_pin_a_slot(self):
        q = dashboard._queue(self._store([
            {"id": 1, "model": "Kimi-K3", "pid": 2 ** 22, "task": "t1",
             "acquired_at": time.time() - 30}]))
        self.assertEqual(q["totals"]["running"], 0)

    def test_every_known_model_appears_even_when_idle(self):
        self._write()
        models = {m["model"] for m in dashboard._queue(self._store())["models"]}
        self.assertIn("Kimi-K3", models)
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
