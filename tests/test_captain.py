"""Tests for the captain supervisor: state gathering, action parsing, and the
capacity-aware admission gate.

No model is ever called and no `main.py` process ever starts: the HTTP tests
patch `_spawn_logged`, and the action tests call `captain.execute_actions`
directly with `_spawn_detached` patched. The capacity tests drive `plan_pressure`
against a temp taskfile and stubbed lease counts.

The load-bearing behaviours this file pins:

- the "captain block" action schema is CLOSED — an unknown kind, a missing
  field, or a malformed JSON block degrades to a prose-only turn, never a
  surprise action;
- `run`/`resume` are capacity-gated: a taskfile whose implementer models have
  no free driver slots is QUEUED (an entry in queue.jsonl + a `captain.action`
  event), not launched into a wall of capacity 400s;
- a taskfile that does not exist is refused, never queued as if it did;
- the dashboard routes never 500 (an absent captain dir is empty state).
"""
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (sys.path + event/DB redirect)
import captain  # noqa: E402
import config  # noqa: E402
import dashboard  # noqa: E402


class CaptainActionParsing(unittest.TestCase):
    """parse_actions is the captain's whole trust boundary — pin it."""

    def test_last_block_wins_and_valid_actions_survive(self):
        text = (
            "thinking out loud\n```captain\n{\"actions\": [{\"kind\": \"status\"}]}\n```\n"
            "final:\n```captain\n{\"actions\": ["
            "{\"kind\": \"plan\", \"goal\": \"build a notes app\"},"
            "{\"kind\": \"run\", \"taskfile\": \"notes.json\"}]}\n```")
        acts = captain.parse_actions(text)
        self.assertEqual([a["kind"] for a in acts], ["plan", "run"])
        self.assertEqual(acts[0]["goal"], "build a notes app")
        self.assertEqual(acts[1]["taskfile"], "notes.json")

    def test_unknown_kind_is_dropped_not_executed(self):
        acts = captain.parse_actions(
            "```captain\n{\"actions\": [{\"kind\": \"rm-rf\", \"path\": \"/\"},"
            "{\"kind\": \"status\"}]}\n```")
        self.assertEqual([a["kind"] for a in acts], ["status"])

    def test_plan_without_goal_is_dropped(self):
        acts = captain.parse_actions(
            "```captain\n{\"actions\": [{\"kind\": \"plan\"},"
            "{\"kind\": \"plan\", \"goal\": \"ok goal\"}]}\n```")
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts[0]["goal"], "ok goal")

    def test_run_without_taskfile_is_dropped(self):
        acts = captain.parse_actions(
            "```captain\n{\"actions\": [{\"kind\": \"run\"}]}\n```")
        self.assertEqual(acts, [])

    def test_malformed_json_yields_no_actions(self):
        self.assertEqual(captain.parse_actions("```captain\nnot json{{{\n```"), [])

    def test_no_block_yields_no_actions(self):
        self.assertEqual(captain.parse_actions("just prose, no block"), [])

    def test_non_object_entries_are_skipped(self):
        acts = captain.parse_actions(
            "```captain\n{\"actions\": [\"status\", 42, null, {\"kind\": \"status\"}]}\n```")
        self.assertEqual(len(acts), 1)


class CaptainCapacity(unittest.TestCase):
    """plan_pressure: the capacity-aware admission gate."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)
        self._tasks = self.tmp / "tasks"
        self._tasks.mkdir()
        self._old_tasks = config.TASKS_DIR
        config.TASKS_DIR = str(self._tasks)
        self._cap_patch = mock.patch.object(
            captain, "capacity_snapshot", side_effect=self._cap)
        self._cap_patch.start()
        self.cap = {}

    def tearDown(self):
        self._cap_patch.stop()
        config.TASKS_DIR = self._old_tasks
        self._dir.cleanup()

    def _cap(self, db_path=None):
        return self.cap

    def _write_taskfile(self, name, models):
        doc = {"project": {"repo": str(self.tmp / "repo"), "title": "t",
                           "tasks": []}}
        for i, m in enumerate(models):
            doc["project"]["tasks"].append({
                "id": f"t{i}", "prompt": "do something",
                "model": m, "reviewer": config.cross_family_reviewer(m),
                "verify_cmd": "true"})
        (self._tasks / name).write_text(json.dumps(doc), encoding="utf-8")
        return name

    def test_admits_when_slots_are_free(self):
        m = sorted(config.IMPLEMENTER_MODELS)[0]
        name = self._write_taskfile("ok.json", [m])
        self.cap = {m: {"batch_headroom": 3}}
        r = captain.plan_pressure(name)
        self.assertTrue(r["admit"])
        self.assertEqual(r["models"], {m: 1})

    def test_queues_when_a_model_has_no_free_slot(self):
        m = sorted(config.IMPLEMENTER_MODELS)[0]
        name = self._write_taskfile("busy.json", [m, m])
        self.cap = {m: {"batch_headroom": 0}}
        r = captain.plan_pressure(name)
        self.assertFalse(r["admit"])
        self.assertEqual(r["deficit"], {m: 2})
        self.assertIn(m, r["reason"])

    def test_wider_than_the_cap_still_admits_when_a_slot_is_free(self):
        """Demanding a slot per task would queue any taskfile wider than the
        driver cap forever; the run's own leases pace the rest."""
        m = sorted(config.IMPLEMENTER_MODELS)[0]
        name = self._write_taskfile("wide.json", [m] * 6)
        self.cap = {m: {"batch_headroom": 1}}
        self.assertTrue(captain.plan_pressure(name)["admit"])

    def test_taskfile_outside_tasks_dir_is_refused(self):
        outside = self.tmp / "evil.json"
        outside.write_text("{}", encoding="utf-8")
        self.assertIsNone(captain._taskfile_path(str(outside)))
        self.assertIsNone(captain._taskfile_path("../evil.json"))
        self.assertIsNotNone(captain._taskfile_path("ok.json"))

    def test_recent_events_reads_only_the_tail(self):
        log = self.tmp / "events.jsonl"
        big = json.dumps({"type": "task.gate", "task": "old", "pad": "x" * 500})
        with open(log, "w", encoding="utf-8") as f:
            for _ in range(captain.EVENTS_TAIL_BYTES // len(big) + 50):
                f.write(big + "\n")
            f.write(json.dumps({"type": "task.merged", "task": "new"}) + "\n")
        evs = captain._recent_events(path=log)
        self.assertEqual(evs[-1]["task"], "new")
        self.assertLessEqual(len(evs), captain.EVENTS_TAIL)

    def test_missing_taskfile_admits_so_the_run_reports_it(self):
        r = captain.plan_pressure("nope.json")
        self.assertTrue(r["admit"])

    def test_unparseable_taskfile_admits(self):
        (self._tasks / "junk.json").write_text("{not json", encoding="utf-8")
        self.assertTrue(captain.plan_pressure("junk.json")["admit"])


class CaptainFleetState(unittest.TestCase):
    """fleet_state assembles a snapshot without a live db."""

    def test_state_has_the_expected_shape(self):
        st = captain.fleet_state(store=_FakeStore(), db_path=":memory:")
        self.assertIn("capacity", st)
        self.assertIn("task_status_counts", st)
        self.assertIn("needs_attention", st)
        self.assertEqual(st["planner_model"], config.PLANNER_MODEL)

    def test_capacity_covers_every_live_implementer(self):
        with mock.patch.object(captain, "_live_lease_counts", return_value={}):
            cap = captain.capacity_snapshot(db_path=":memory:")
        for m in config.IMPLEMENTER_MODELS:
            self.assertIn(m, cap)
            self.assertGreaterEqual(cap[m]["driver_cap"], 1)


class CaptainExecuteActions(unittest.TestCase):
    """execute_actions launches fixed argv and gates run/resume on capacity."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)
        self._tasks = self.tmp / "tasks"
        self._tasks.mkdir()
        self._capdir = self.tmp / "captain"
        self._old_tasks = config.TASKS_DIR
        self._old_cap = os.environ.get("ARC_CAPTAIN_DIR")
        config.TASKS_DIR = str(self._tasks)
        os.environ["ARC_CAPTAIN_DIR"] = str(self._capdir)
        self.spawned = []
        self._spawn_patch = mock.patch.object(
            captain, "_spawn_detached", side_effect=self._spawn)
        self._spawn_patch.start()

    def tearDown(self):
        self._spawn_patch.stop()
        config.TASKS_DIR = self._old_tasks
        if self._old_cap is None:
            os.environ.pop("ARC_CAPTAIN_DIR", None)
        else:
            os.environ["ARC_CAPTAIN_DIR"] = self._old_cap
        self._dir.cleanup()
        _clear_run_queue()

    def _spawn(self, argv, log_name):
        self.spawned.append((list(argv), log_name))
        return mock.Mock(pid=999)

    def _write_taskfile(self, name, model):
        doc = {"project": {"repo": str(self.tmp / "repo"), "title": "t",
                           "tasks": [{"id": "t0", "prompt": "do",
                                      "model": model,
                                      "reviewer": config.cross_family_reviewer(model),
                                      "verify_cmd": "true"}]}}
        (self._tasks / name).write_text(json.dumps(doc), encoding="utf-8")

    def test_status_is_a_noop(self):
        res = captain.execute_actions([{"kind": "status"}], "/repo")
        self.assertEqual(len(res), 1)
        self.assertTrue(res[0]["ok"])
        self.assertEqual(self.spawned, [])

    def test_run_launches_fixed_argv_when_admitted(self):
        m = sorted(config.IMPLEMENTER_MODELS)[0]
        self._write_taskfile("ok.json", m)
        with mock.patch.object(captain, "plan_pressure",
                               return_value={"admit": True, "reason": "free",
                                             "models": {}, "deficit": {}}):
            res = captain.execute_actions(
                [{"kind": "run", "taskfile": "ok.json"}], "/repo")
        self.assertTrue(res[0]["ok"])
        argv = self.spawned[0][0]
        self.assertEqual(argv[1:4], ["main.py", "code", "run"])
        self.assertTrue(argv[4].endswith("ok.json"))

    def test_run_is_queued_when_capacity_is_tight(self):
        m = sorted(config.IMPLEMENTER_MODELS)[0]
        self._write_taskfile("busy.json", m)
        with mock.patch.object(captain, "plan_pressure",
                               return_value={"admit": False,
                                             "reason": f"no free slot for {m} (+1)",
                                             "models": {m: 2}, "deficit": {m: 1}}):
            res = captain.execute_actions(
                [{"kind": "run", "taskfile": "busy.json"}], "/repo")
        self.assertFalse(res[0]["ok"])
        self.assertTrue(res[0]["queued"])
        self.assertEqual(self.spawned, [])          # nothing launched
        q = self._capdir / "queue.jsonl"
        self.assertTrue(q.is_file())
        entry = json.loads(q.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(entry["kind"], "run")
        self.assertIn("no free slot", entry["reason"])

    def test_run_of_missing_taskfile_is_refused_not_queued(self):
        res = captain.execute_actions(
            [{"kind": "run", "taskfile": "ghost.json"}], "/repo")
        self.assertFalse(res[0]["ok"])
        self.assertFalse(res[0].get("queued"))
        self.assertEqual(self.spawned, [])
        self.assertFalse((self._capdir / "queue.jsonl").exists())

    def test_plan_launches_the_planner(self):
        res = captain.execute_actions(
            [{"kind": "plan", "goal": "build a notes app"}], "/repo")
        self.assertTrue(res[0]["ok"])
        argv = self.spawned[0][0]
        self.assertEqual(argv[1:4], ["main.py", "code", "plan"])
        self.assertEqual(argv[4], "build a notes app")


class _FakeStore:
    def code_tasks_all(self, limit=500):
        return [{"id": "a", "taskfile": "/t/x.json", "status": "merged",
                 "model": "GLM-5.3", "error": None}]


class _FakeRequest(dashboard.Handler):
    def __init__(self, path, body=b""):
        self.status = None
        self.response_headers = {}
        self.body = b""
        self.wfile = self
        self.rfile = self
        self.path = path
        self._pending = body
        self.headers = {"Content-Length": str(len(body)),
                        "Content-Type": "application/json"}

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass

    def read(self, n):
        data, self._pending = self._pending[:n], self._pending[n:]
        return data

    def write(self, data):
        self.body += data


class CaptainRoutes(unittest.TestCase):
    """The captain HTTP surface never 500s and gates the same allowlist."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)
        self._capdir = self.tmp / "captain"
        self._env = {k: os.environ.get(k)
                     for k in ("ARC_CAPTAIN_DIR", "ARC_REPOS_DIR")}
        os.environ["ARC_CAPTAIN_DIR"] = str(self._capdir)
        os.environ["ARC_REPOS_DIR"] = str(self.tmp / "repos")
        self._orig = dict(dashboard._launch_registry)
        dashboard._launch_registry.clear()
        self._spawn_patch = mock.patch.object(
            dashboard, "_spawn_logged", side_effect=self._spawn)
        self._spawn_patch.start()

    def tearDown(self):
        self._spawn_patch.stop()
        dashboard._launch_registry.clear()
        dashboard._launch_registry.update(self._orig)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._dir.cleanup()

    def _spawn(self, argv, log_name):
        proc = mock.Mock(pid=4242)
        proc.wait.side_effect = subprocess.TimeoutExpired(argv, 1.5)
        return proc, log_name

    def _get(self, path):
        req = _FakeRequest(path)
        req.do_GET()
        return req.status, json.loads(req.body.decode("utf-8"))

    def _post(self, path, obj):
        req = _FakeRequest(path, json.dumps(obj).encode("utf-8"))
        req.do_POST()
        return req.status, json.loads(req.body.decode("utf-8"))

    def test_state_route_never_500s_with_no_dir(self):
        os.environ["ARC_CAPTAIN_DIR"] = str(self.tmp / "never")
        status, resp = self._get("/api/captain/state")
        self.assertEqual(status, 200)
        self.assertIn("state", resp)
        self.assertIn("sessions", resp)

    def test_sessions_route_empty_when_dir_missing(self):
        status, resp = self._get("/api/captain/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(resp, {"sessions": []})

    def test_queue_route_empty_when_missing(self):
        status, resp = self._get("/api/captain/queue")
        self.assertEqual(status, 200)
        self.assertEqual(resp, {"queued": []})

    def test_poll_rejects_bad_session(self):
        status, resp = self._get("/api/captain/poll?session=BAD_ID")
        self.assertEqual(status, 400)

    def test_start_rejects_repo_not_on_allowlist(self):
        status, resp = self._post("/api/captain/start",
                                  {"session": "captain-x", "repo": "/etc",
                                   "message": "hi"})
        self.assertEqual(status, 400)
        self.assertIn("/api/repos", resp["error"])

    def test_start_rejects_bad_session(self):
        status, resp = self._post("/api/captain/start",
                                  {"session": "../evil", "repo": "/etc",
                                   "message": "hi"})
        self.assertEqual(status, 400)

    def test_start_rejects_empty_message(self):
        status, resp = self._post("/api/captain/start",
                                  {"session": "captain-x", "repo": "/etc",
                                   "message": ""})
        self.assertEqual(status, 400)


class CaptainLiveThinking(unittest.TestCase):
    """The panel must show WHAT a running turn is doing, not a bare spinner.

    `_captain_poll` returns a `thinking` block (the transcript reducer's folded
    blocks + the live `pending` line) while the turn runs, and the reducer must
    accept a transcript whose FIRST record is larger than any fixed byte window
    (a captain prompt is one 13 KB+ record — the original 8 KB probe rejected
    such files wholesale, so the whole captain stream fell back to raw JSON).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.old_env = {k: os.environ.get(k)
                        for k in ("ARC_CAPTAIN_DIR",)}
        os.environ["ARC_CAPTAIN_DIR"] = str(self.dir / "captain")
        # A config.ROOT that holds the harness transcript we write.
        self.root = self.dir / "root"
        (self.root / "logs" / "harness").mkdir(parents=True)
        self._root_patch = mock.patch.object(config, "ROOT", self.root)
        self._root_patch.start()

    def tearDown(self):
        self._root_patch.stop()
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _write_transcript(self, session, records):
        d = self.root / "logs" / "harness"
        p = d / f"captain-{session}-planner-1.jsonl"
        p.write_text("\n".join(json.dumps(r) for r in records) + "\n",
                     encoding="utf-8")
        return p

    def test_probe_accepts_a_first_record_larger_than_the_window(self):
        # One 20 KB text record, then a normal one — must still be recognized.
        big = {"type": "text", "part": {"type": "text", "text": "x" * 20000}}
        nxt = {"type": "step_finish", "part": {"reason": "stop"}}
        data = json.dumps(big) + "\n" + json.dumps(nxt) + "\n"
        self.assertTrue(dashboard._probe_transcript(data))

    def test_poll_includes_thinking_while_running(self):
        self._write_transcript("captain-x", [
            {"type": "text", "part": {"type": "text", "text": "status: all good"}}])
        (self.dir / "captain").mkdir(parents=True, exist_ok=True)
        (self.dir / "captain" / "captain-x.jsonl").write_text(
            json.dumps({"role": "user", "ts": 1, "text": "hi"}) + "\n",
            encoding="utf-8")
        with mock.patch.object(dashboard, "_captain_running", return_value=True):
            resp = dashboard._captain_poll("captain-x", 0)
        self.assertTrue(resp["running"])
        self.assertIn("thinking", resp)
        self.assertIn("blocks", resp["thinking"])
        self.assertTrue(any("all good" in str(b) for b in resp["thinking"]["blocks"]))

    def test_poll_omits_thinking_when_not_running(self):
        with mock.patch.object(dashboard, "_captain_running", return_value=False):
            resp = dashboard._captain_poll("captain-x", 0)
        self.assertFalse(resp["running"])
        self.assertNotIn("thinking", resp)

    def test_poll_never_500s_with_no_transcript(self):
        with mock.patch.object(dashboard, "_captain_running", return_value=True):
            resp = dashboard._captain_poll("captain-none", 0)
        self.assertTrue(resp["running"])
        self.assertNotIn("thinking", resp)      # no file -> no thinking, still 200

    def test_echoed_persona_prompt_is_not_shown_as_thinking(self):
        # The harness echoes the captain's own prompt as the first text record;
        # it must be filtered so the panel shows real progress, not persona prose.
        d = self.root / "logs" / "harness"
        p = d / "captain-captain-x-planner-1.jsonl"
        p.write_text(
            json.dumps({"type": "text", "part": {
                "type": "text",
                "text": "You are the CAPTAIN of the ARC multi-model coding fleet. " + "z" * 5000}}) + "\n"
            + json.dumps({"type": "text", "part": {
                "type": "text", "text": "Status: 3 tasks failed"}}) + "\n",
            encoding="utf-8")
        t = dashboard._captain_thinking("captain-x")
        joined = " ".join(str(b) for b in t["blocks"])
        self.assertIn("3 tasks failed", joined)
        self.assertNotIn("You are the CAPTAIN", joined)

    def test_thinking_resolves_the_newest_attempt(self):
        self._write_transcript("captain-x", [{"type": "text", "part": {"type": "text", "text": "old"}}])
        p2 = self.root / "logs" / "harness" / "captain-captain-x-planner-2.jsonl"
        p2.write_text(json.dumps({"type": "text", "part": {"type": "text", "text": "new"}}) + "\n")
        os.utime(p2, (time.time() + 10, time.time() + 10))
        self.assertEqual(dashboard._captain_running_transcript("captain-x"),
                         "captain-captain-x-planner-2.jsonl")


def _clear_run_queue():
    try:
        q = captain._run_queue()
        with q.lock:
            q.conn.execute(
                "DELETE FROM queue_items WHERE topic=?", (captain.RUN_TOPIC,))
            q.conn.commit()
    except Exception:
        pass


def _blocked(model):
    return {"admit": False, "reason": f"no free slot for {model} (+1)",
            "models": {model: 1}, "deficit": {model: 1}}


def _free():
    return {"admit": True, "reason": "capacity free", "models": {}, "deficit": {}}


class CaptainQueueDrain(unittest.TestCase):
    """Saturation queues one durable run; a later drain launches it once."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)
        self._tasks = self.tmp / "tasks"
        self._tasks.mkdir()
        self._capdir = self.tmp / "captain"
        self._old_tasks = config.TASKS_DIR
        self._old_cap = os.environ.get("ARC_CAPTAIN_DIR")
        config.TASKS_DIR = str(self._tasks)
        os.environ["ARC_CAPTAIN_DIR"] = str(self._capdir)
        self.spawned = []
        self._spawn_patch = mock.patch.object(
            captain, "_spawn_detached", side_effect=self._spawn)
        self._spawn_patch.start()
        _clear_run_queue()

    def tearDown(self):
        self._spawn_patch.stop()
        _clear_run_queue()
        config.TASKS_DIR = self._old_tasks
        if self._old_cap is None:
            os.environ.pop("ARC_CAPTAIN_DIR", None)
        else:
            os.environ["ARC_CAPTAIN_DIR"] = self._old_cap
        self._dir.cleanup()

    def _spawn(self, argv, log_name):
        self.spawned.append((list(argv), log_name))
        return mock.Mock(pid=999)

    def _write_taskfile(self, name, model):
        doc = {"project": {"repo": str(self.tmp / "repo"), "title": "t",
                           "tasks": [{"id": "t0", "prompt": "do", "model": model,
                                      "reviewer": config.cross_family_reviewer(model),
                                      "verify_cmd": "true"}]}}
        (self._tasks / name).write_text(json.dumps(doc), encoding="utf-8")
        return self._tasks / name

    def _queue_run(self, name, model):
        with mock.patch.object(captain, "plan_pressure", return_value=_blocked(model)):
            return captain.execute_actions(
                [{"kind": "run", "taskfile": name}], "/repo")

    def test_saturation_queues_then_one_launch_on_release(self):
        m = sorted(config.IMPLEMENTER_MODELS)[0]
        self._write_taskfile("busy.json", m)
        with mock.patch.object(captain, "plan_pressure", return_value=_blocked(m)):
            res = captain.execute_actions(
                [{"kind": "run", "taskfile": "busy.json"}], "/repo")
            self.assertTrue(res[0]["queued"])
            self.assertEqual(self.spawned, [])
            held = captain.drain_once()
        self.assertEqual(held[0]["result"], "retained")
        self.assertEqual(self.spawned, [])
        self.assertEqual(len(captain._run_queue().pending(captain.RUN_TOPIC)), 1)
        with mock.patch.object(captain, "plan_pressure", return_value=_free()):
            launched = captain.drain_once()
            again = captain.drain_once()
        self.assertEqual([r["result"] for r in launched], ["launched"])
        self.assertEqual(again, [])
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.spawned[0][0][1:4], ["main.py", "code", "run"])
        self.assertEqual(captain.queue_view()["queued"], [])

    def test_duplicate_enqueue_launches_once(self):
        m = sorted(config.IMPLEMENTER_MODELS)[0]
        self._write_taskfile("twice.json", m)
        self._queue_run("twice.json", m)
        self._queue_run("twice.json", m)
        self.assertEqual(len(captain._run_queue().pending(captain.RUN_TOPIC)), 1)
        with mock.patch.object(captain, "plan_pressure", return_value=_free()):
            captain.drain_once()
        self.assertEqual(len(self.spawned), 1)

    def test_expired_claim_is_reclaimed_and_launched_once(self):
        m = sorted(config.IMPLEMENTER_MODELS)[0]
        self._write_taskfile("reclaim.json", m)
        self._queue_run("reclaim.json", m)
        abandoned = captain._run_queue().claim(captain.RUN_TOPIC, lease_s=-1)
        self.assertIsNotNone(abandoned)
        with mock.patch.object(captain, "plan_pressure", return_value=_free()):
            out = captain.drain_once()
            captain.drain_once()
        self.assertEqual([r["result"] for r in out], ["launched"])
        self.assertEqual(len(self.spawned), 1)

    def test_missing_taskfile_is_dropped(self):
        m = sorted(config.IMPLEMENTER_MODELS)[0]
        path = self._write_taskfile("gone.json", m)
        self._queue_run("gone.json", m)
        path.unlink()
        out = captain.drain_once()
        self.assertEqual(out[0]["result"], "missing")
        self.assertEqual(self.spawned, [])
        self.assertEqual(captain._run_queue().pending(captain.RUN_TOPIC), [])

    def test_live_run_is_not_launched_again(self):
        m = sorted(config.IMPLEMENTER_MODELS)[0]
        self._write_taskfile("live.json", m)
        self._queue_run("live.json", m)
        with mock.patch.object(captain, "plan_pressure", return_value=_free()), \
                mock.patch("reconcile.live_runs",
                           return_value=[{"pid": 42, "taskfile": "live.json"}]):
            out = captain.drain_once()
        self.assertEqual(out[0]["result"], "live")
        self.assertEqual(self.spawned, [])
        self.assertEqual(captain._run_queue().pending(captain.RUN_TOPIC), [])

    def test_legacy_jsonl_entry_still_shows(self):
        captain._enqueue({"ts": 1, "kind": "run", "taskfile": "old.json",
                          "reason": "historical"})
        queued = captain.queue_view()["queued"]
        self.assertEqual(queued[0]["taskfile"], "old.json")
        self.assertIn("historical", queued[0]["reason"])


if __name__ == "__main__":
    unittest.main()
