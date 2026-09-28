"""Tests for the captain autopilot (captain_autopilot.py, AGENTS.md Rule 11).

No model is ever called and no process ever starts: the LLM is
`captain_autopilot._llm_call` (patched), the board is a fake injected into
`captain_autopilot._BOARD`, and run/resume go through a patched
`captain.execute_actions`.

What this pins:

- every detection rule fires on a synthetic snapshot, and stays quiet when
  its condition does not hold;
- playbooks turn findings into the right bounded actions;
- per-target cooldowns and the per-tick cap bound what one tick may do;
- the pause file and --dry-run stop the captain from acting;
- a model-proposed action outside the closed set, or one that fails
  validation, is dropped;
- plan changes touch only unstarted or failed tasks;
- a whole tick runs end to end on a fake snapshot.
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (sys.path + event/DB redirect)
import captain  # noqa: E402
import captain_autopilot as ap  # noqa: E402
import config  # noqa: E402
from store import Store  # noqa: E402

NOW = 1_800_000_000.0


class FakeBoard:
    def __init__(self, inbox=(), questions=(), claims=(), thread=()):
        self.posts = []
        self._inbox, self._q = list(inbox), list(questions)
        self._claims, self._thread = list(claims), list(thread)

    def post(self, project, **kw):
        self.posts.append(dict(kw, project=project))
        return f"m{len(self.posts)}"

    def inbox(self, project, agent, since_ts=None, model=""):
        return [m for m in self._inbox
                if since_ts is None or m["ts"] > since_ts]

    def thread(self, project, channel=None, since_ts=None, limit=100, kinds=None):
        return self._q if kinds == ["question"] else self._thread

    def claims(self, project, include_expired=False):
        return self._claims

    @staticmethod
    def paths_overlap(a, b):
        return a == b or a.startswith(b.rstrip("/") + "/") \
            or b.startswith(a.rstrip("/") + "/")


def task(tid="t1", status="running", **kw):
    t = {"id": tid, "taskfile": "/tasks/p.json", "project": "p",
         "status": status, "model": "GLM-5.3", "since": NOW - 60,
         "age_s": 60, "fix_round": 1, "escalations": 0, "last_gate": None,
         "recent_gates": [], "last_review": None, "last_driver_ts": NOW - 60,
         "infra_events": 0, "error": ""}
    t.update(kw)
    return t


def snap(**kw):
    s = {"ts": NOW, "fleet": {}, "tasks": [], "stopped": [],
         "projects": {"p": {"taskfiles": ["/tasks/p.json"], "repo": ""}},
         "active_projects": [], "prs": [], "watchdog": {}, "seats": {},
         "board": {}, "chains": []}
    s.update(kw)
    return s


def rules(s):
    return [f["rule"] for f in ap.detect(s)]


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="arc-autopilot-"))
        self._env = os.environ.get("ARC_CAPTAIN_DIR")
        os.environ["ARC_CAPTAIN_DIR"] = str(self.tmp / "captain")
        self.board = FakeBoard()
        ap._BOARD = self.board

    def tearDown(self):
        ap._BOARD = None
        if self._env is None:
            os.environ.pop("ARC_CAPTAIN_DIR", None)
        else:
            os.environ["ARC_CAPTAIN_DIR"] = self._env


class DetectRules(unittest.TestCase):
    def test_stuck_implementing_needs_silence(self):
        quiet = task(since=NOW - 3 * 3600, last_driver_ts=NOW - 100 * 60)
        self.assertEqual(rules(snap(tasks=[quiet])), ["stuck_implementing"])
        busy = task(since=NOW - 3 * 3600, last_driver_ts=NOW - 60)
        self.assertEqual(rules(snap(tasks=[busy])), [])

    def test_in_review_over_an_hour(self):
        self.assertEqual(rules(snap(tasks=[task(status="in_review",
                                                since=NOW - 61 * 60)])),
                         ["stuck_in_review"])
        self.assertEqual(rules(snap(tasks=[task(status="in_review",
                                                since=NOW - 30 * 60)])), [])

    def test_conflict_at_all_is_critical(self):
        f = ap.detect(snap(tasks=[task(status="conflict")]))
        self.assertEqual([(x["rule"], x["severity"]) for x in f],
                         [("conflict", "critical")])

    def test_repeated_gate_failure_ignores_numbers(self):
        g = [{"ts": NOW - i, "passed": False,
              "tail": f"FAILED test_x in {i}.2s at /tmp/wt{i}/a.py"} for i in range(3)]
        self.assertIn("repeated_gate_failure",
                      rules(snap(tasks=[task(recent_gates=g)])))
        g2 = g[:2] + [{"ts": NOW, "passed": False, "tail": "SyntaxError"}]
        self.assertNotIn("repeated_gate_failure",
                         rules(snap(tasks=[task(recent_gates=g2)])))
        g3 = g[:2] + [dict(g[2], passed=True)]
        self.assertNotIn("repeated_gate_failure",
                         rules(snap(tasks=[task(recent_gates=g3)])))

    def test_idle_model_while_others_cap_wait(self):
        seats = {"A": {"in_use": 4, "cap": 4, "cap_waits": 3,
                       "usage_limited_until": None},
                 "B": {"in_use": 0, "cap": 10, "cap_waits": 0,
                       "usage_limited_until": None},
                 "C": {"in_use": 0, "cap": 4, "cap_waits": 0,
                       "usage_limited_until": NOW + 600}}
        f = ap.detect(snap(seats=seats))
        self.assertEqual([(x["rule"], x["model"]) for x in f], [("idle_model", "B")])
        seats["A"]["cap_waits"] = 0
        self.assertEqual(rules(snap(seats=seats)), [])

    def test_parked_and_stopped_runs(self):
        s = snap(watchdog={"parked": {"/tasks/a.json": "6 quick exits"}},
                 stopped=["/tasks/b.json"])
        self.assertEqual(sorted(rules(s)), ["run_parked", "run_stopped"])

    def test_board_rules(self):
        q = {"id": "q1", "ts": NOW - 31 * 60, "author": "x/implementer",
             "channel": "task:x", "body": "which api?", "kind": "question",
             "state": "open"}
        mention = {"id": "m1", "ts": NOW - 5, "author": "y/implementer",
                   "channel": "project", "body": "@captain help"}
        conflict = {"a": "x/implementer", "b": "y/implementer", "a_task": "x",
                    "b_task": "y", "paths": ["app.py"]}
        s = snap(board={"p": {"open_questions": [q], "mentions": [mention],
                              "claim_conflicts": [conflict]}})
        self.assertEqual(sorted(rules(s)), ["captain_mention", "claim_conflict",
                                            "unanswered_question"])

    def test_stale_pr_needs_age_and_silence(self):
        pr = {"project": "p", "number": 7, "title": "x", "task": "t1",
              "age_s": 3 * 3600, "created": NOW - 3 * 3600,
              "updated": NOW - 2.5 * 3600}
        self.assertEqual(rules(snap(prs=[pr])), ["stale_pr"])
        self.assertEqual(rules(snap(prs=[dict(pr, updated=NOW - 60)])), [])

    def test_infra_failure_and_chain_blocked(self):
        t = task(status="failed", error="push failed: GraphQL rate limit exceeded")
        self.assertEqual(rules(snap(tasks=[t])), ["infra_failure"])
        self.assertEqual(rules(snap(tasks=[task(status="failed",
                                                error="exhausted escalation")])), [])
        ch = {"taskfile": "/tasks/down.json", "ok": False,
              "blocked": ["/tasks/up.json"], "waiting": []}
        f = ap.detect(snap(chains=[ch]))
        self.assertEqual([(x["rule"], x["severity"]) for x in f],
                         [("chain_blocked", "critical")])


class Playbooks(unittest.TestCase):
    def _one(self, f, s=None):
        return [ap.validate_action(dict(a, reason="r"), s or snap())
                for a in ap.playbook(f, s or snap())]

    def test_stuck_task_with_dead_run_is_resumed(self):
        with mock.patch.object(config, "TASKS_DIR", "/tasks"):
            s = snap(tasks=[task()])
            f = dict(ap.detect(snap(tasks=[task(since=NOW - 3 * 3600,
                                                last_driver_ts=0)]))[0])
            acts = self._one(f, s)
        self.assertEqual([a["kind"] for a in acts], ["resume"])
        self.assertEqual(acts[0]["taskfile"], "/tasks/p.json")

    def test_stuck_task_with_live_run_gets_a_ping(self):
        s = snap(tasks=[task(since=NOW - 3 * 3600, last_driver_ts=0)],
                 watchdog={"live": {"/tasks/p.json": 123}})
        acts = self._one(ap.detect(s)[0], s)
        self.assertEqual(acts[0]["kind"], "board_post")
        self.assertEqual(acts[0]["channel"], "task:t1")
        self.assertEqual(acts[0]["msg_kind"], "ping")
        self.assertIn("t1", acts[0]["mentions"])

    def test_conflict_escalates_and_claims_ping_both(self):
        s = snap(tasks=[task(status="conflict")])
        self.assertEqual([a["kind"] for a in self._one(ap.detect(s)[0], s)],
                         ["escalate_to_operator"])
        c = {"a": "x/implementer", "b": "y/implementer", "a_task": "x",
             "b_task": "y", "paths": ["app.py"]}
        s = snap(board={"p": {"claim_conflicts": [c]}})
        acts = self._one(ap.detect(s)[0], s)
        self.assertEqual(acts[0]["kind"], "board_post")
        self.assertEqual(sorted(acts[0]["mentions"]), ["x", "y"])

    def test_standup_once_per_period(self):
        s = snap(active_projects=["p"], tasks=[task(), task("t2", status="merged")])
        d = ap.decide([], s, state={}, now=NOW, llm=False)
        self.assertEqual([a["kind"] for a in d["actions"]], ["standup"])
        self.assertIn("done: t2", d["actions"][0]["body"])
        self.assertIn("in progress: t1", d["actions"][0]["body"])
        d = ap.decide([], s, state={"last_standup": {"p": NOW - 60}}, now=NOW,
                      llm=False)
        self.assertEqual(d["actions"], [])


class Guardrails(unittest.TestCase):
    def test_per_tick_cap(self):
        ts = [task(f"t{i}", status="conflict") for i in range(8)]
        s = snap(tasks=ts)
        d = ap.decide(ap.detect(s), s, state={}, now=NOW, llm=False)
        self.assertEqual(len(d["actions"]), ap.MAX_ACTIONS)
        self.assertEqual([a["skipped"] for a in d["skipped"]],
                         ["per-tick cap"] * (8 - ap.MAX_ACTIONS))

    def test_cooldown_never_nags_twice_in_30_min(self):
        s = snap(tasks=[task(status="conflict")])
        target = ap.detect(s)[0]["target"]
        d = ap.decide(ap.detect(s), s, state={"cooldowns": {target: NOW - 600}},
                      now=NOW, llm=False)
        self.assertEqual(d["actions"], [])
        self.assertEqual(d["skipped"][0]["skipped"], "cooldown")
        d = ap.decide(ap.detect(s), s,
                      state={"cooldowns": {target: NOW - ap.COOLDOWN_S - 1}},
                      now=NOW, llm=False)
        self.assertEqual(len(d["actions"]), 1)


class LlmActions(unittest.TestCase):
    def test_invalid_llm_actions_are_dropped(self):
        q = {"id": "q1", "ts": NOW - 31 * 60, "author": "x/implementer",
             "channel": "task:x", "body": "which api?", "state": "open"}
        s = snap(board={"p": {"open_questions": [q]}})
        findings = ap.detect(s)
        reply = "```captain\n" + json.dumps({"actions": [
            {"kind": "rm-rf", "path": "/"},
            {"kind": "resume", "taskfile": "p.json"},
            {"kind": "board_post", "project": "p", "channel": "../etc",
             "body": "x", "finding_target": "question:q1"},
            {"kind": "board_post", "project": "nope", "channel": "project",
             "body": "x", "finding_target": "question:q1"},
            {"kind": "board_post", "project": "p", "channel": "task:x",
             "msg_kind": "answer", "reply_to": "q1", "body": "use toggle(id)",
             "finding_target": "question:q1", "reason": "api is toggle"},
        ]}) + "\n```"
        with helpers.capture_events() as ev, \
                mock.patch.object(ap, "_llm_call", return_value=reply) as llm:
            d = ap.decide(findings, s, state={}, now=NOW)
        llm.assert_called_once()
        self.assertEqual(len(ev.of("captain.auto.dropped")), 4)
        self.assertEqual(len(d["actions"]), 1)
        a = d["actions"][0]
        self.assertEqual((a["kind"], a["msg_kind"], a["reply_to"], a["by"]),
                         ("board_post", "answer", "q1", "llm"))
        # The model covered the finding, so the playbook's escalation is not added.
        self.assertNotIn("escalate_to_operator", [x["kind"] for x in d["actions"]])

    def test_llm_failure_falls_back_to_playbook(self):
        q = {"id": "q1", "ts": NOW - 31 * 60, "author": "x/implementer",
             "channel": "task:x", "body": "?", "state": "open"}
        s = snap(board={"p": {"open_questions": [q]}})
        with mock.patch.object(ap, "_llm_call", side_effect=RuntimeError("usage")):
            d = ap.decide(ap.detect(s), s, state={}, now=NOW)
        self.assertEqual([a["kind"] for a in d["actions"]], ["escalate_to_operator"])

    def test_no_llm_call_without_judgment(self):
        s = snap(tasks=[task(status="conflict")])
        with mock.patch.object(ap, "_llm_call") as llm:
            ap.decide(ap.detect(s), s, state={}, now=NOW)
        llm.assert_not_called()

    def test_captain_may_swap_planner_seat_on_usage_limit(self):
        """A spent CAPTAIN_MODEL window may still move to another planner seat.

        `planner_swap` is the captain's opt-in: a turn is advisory, not a
        plan, so it moves rather than parking the tick. This is the outage
        path — it is not a promise that the captain never waits.
        """
        import drivers
        self.assertIsNone(drivers.usage_substitute(
            config.CAPTAIN_MODEL, config.MODEL_HARNESS[config.CAPTAIN_MODEL],
            "planner"))
        sub = drivers.usage_substitute(
            config.CAPTAIN_MODEL, config.MODEL_HARNESS[config.CAPTAIN_MODEL],
            "planner", allow_planner=True)
        if sub is not None:
            self.assertTrue(config.model_may(sub, "planner"))
            self.assertNotEqual(sub, config.CAPTAIN_MODEL)

    def test_a_spent_claude_plan_does_not_swap_the_captain(self):
        """The bug this directive exists for, in the profile that had it.

        On the studio profile PLANNER_MODEL is Claude-Opus-5.5, and the
        always-on captain swapping off Claude's spent weekly window every ~10
        minutes (while DeepSeek had capacity) is what the operator directive
        fixed. Runs in a subprocess under ARC_FLEET=studio, because the studio
        roster is not what this test process imported.
        """
        import subprocess
        import sys
        # The REAL call path is exercised (`usage_substitute`, then the seat the
        # autopilot asks for), so reverting _llm_call to PLANNER_MODEL fails
        # here: on studio that model is Claude and IS blocked.
        snippet = (
            "import json, time, config, drivers\n"
            "drivers._usage_blocked_until.clear()\n"
            "drivers._usage_blocked_until['claude'] = time.time() + 3600\n"
            "sub = drivers.usage_substitute(\n"
            "    config.CAPTAIN_MODEL, config.MODEL_HARNESS[config.CAPTAIN_MODEL],\n"
            "    'planner', allow_planner=True)\n"
            "planner_sub = drivers.usage_substitute(\n"
            "    config.PLANNER_MODEL, config.MODEL_HARNESS[config.PLANNER_MODEL],\n"
            "    'planner', allow_planner=True)\n"
            "print(json.dumps({'captain': config.CAPTAIN_MODEL,\n"
            "  'planner': config.PLANNER_MODEL,\n"
            "  'captain_family': config.MODEL_FAMILY.get(config.CAPTAIN_MODEL),\n"
            "  'captain_sub': sub,\n"
            "  'sub_family': config.MODEL_FAMILY.get(sub) if sub else None,\n"
            "  'planner_sub': planner_sub}))\n"
        )
        root = Path(__file__).resolve().parent.parent
        env = dict(os.environ, ARC_FLEET="studio", PYTHONPATH=str(root))
        env.pop("ARC_CAPTAIN_MODEL", None)
        p = subprocess.run([sys.executable, "-c", snippet], capture_output=True,
                           text=True, env=env, cwd=str(root), timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        d = json.loads(p.stdout)
        self.assertEqual(d["planner"], "Claude-Opus-5.5",
                         "the studio planner is Claude — the seat the captain left")
        self.assertEqual(d["captain"], "DeepSeek-V4.1-Flash-thinking-max")
        self.assertEqual(d["captain_family"], "deepseek")
        # THE binding assertion: a spent Claude window must not move the
        # captain AT ALL — no swap, because its own seat is not blocked.
        self.assertIsNone(d["captain_sub"],
                          "the captain swapped off a spent Claude plan it no "
                          "longer uses — the bug this directive fixed")
        self.assertNotEqual(d["sub_family"], "anthropic")
        # ...and the contrast that gives the assertion teeth: the PLANNER (still
        # Claude on studio) is exactly what would have been swapped.
        self.assertIsNotNone(d["planner_sub"],
                             "the studio planner should need a substitute here, "
                             "or this test cannot detect the regression")

    def test_the_captain_turn_asks_for_its_own_seat(self):
        """_llm_call must name CAPTAIN_MODEL — in the profile where that differs.

        This process is the local profile, where CAPTAIN_MODEL == PLANNER_MODEL,
        so asserting `asked["model"] == config.CAPTAIN_MODEL` here passes even if
        _llm_call were reverted to PLANNER_MODEL. So this test runs under
        ARC_FLEET=studio inside a subprocess, where PLANNER_MODEL is
        Claude-Opus-5.5 and the two differ: the driver must be asked for
        DeepSeek. No model call is made (`drivers.driver_for` is stubbed).
        """
        import subprocess
        import sys
        snippet = (
            "import config, drivers\n"
            "asked = {}\n"
            "class FakeDriver:\n"
            "    planner_swap = False\n"
            "    async def run(self, prompt, work, task_id=None):\n"
            "        return _R('', '')\n"
            "class _R:\n"
            "    def __init__(self, text, transcript_path):\n"
            "        self.text, self.transcript_path = text, transcript_path\n"
            "def fake_driver_for(model, role, **kw):\n"
            "    asked['model'], asked['role'] = model, role\n"
            "    return FakeDriver()\n"
            "drivers.driver_for = fake_driver_for\n"
            "import captain_autopilot as ap\n"
            "ap._llm_call('prompt')\n"
            "import json; print(json.dumps({'asked': asked,\n"
            "  'captain': config.CAPTAIN_MODEL, 'planner': config.PLANNER_MODEL}))\n"
        )
        root = Path(__file__).resolve().parent.parent
        env = dict(os.environ, ARC_FLEET="studio", PYTHONPATH=str(root))
        env.pop("ARC_CAPTAIN_MODEL", None)
        p = subprocess.run([sys.executable, "-c", snippet], capture_output=True,
                           text=True, env=env, cwd=str(root), timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        d = json.loads(p.stdout)
        self.assertNotEqual(d["captain"], d["planner"],
                            "this test is only meaningful where the seats differ")
        self.assertEqual(d["asked"]["model"], d["captain"],
                         "_llm_call must ask for the captain's own seat")
        self.assertEqual(d["asked"]["role"], "planner")

    def test_the_guard_and_the_authors_follow_the_captain_seat(self):
        """`ask_llm` tests, and the board credits, the seat that did the turn.

        The old code used PLANNER_MODEL for both, so on the studio profile the
        availability guard checked a model the captain did not use and the
        board credited Claude for DeepSeek's words.
        """
        finding = [{"target": "t", "rule": "r", "severity": "info"}]
        with mock.patch.object(ap, "_llm_call", return_value="```captain\n"
                              '{"actions": []}\n```'):
            self.assertEqual(ap.ask_llm(finding, snap()), [])
        with mock.patch.object(config, "CAPTAIN_MODEL", None):
            with mock.patch.object(ap, "_llm_call") as llm:
                self.assertEqual(ap.ask_llm(finding, snap()), [])
            llm.assert_not_called()      # no seat -> no call at all
        # ...and the attribution a board post carries is the same seat. The
        # assertion reads the post: patching PLANNER_MODEL must NOT change it.
        ap._BOARD = FakeBoard()
        with mock.patch.object(config, "PLANNER_MODEL", "Some-Other-Model"):
            ap._exec({"kind": "board_post", "project": "p", "channel": "project",
                      "msg_kind": "note", "body": "hi", "mentions": [],
                      "target": "t", "severity": "info"}, None, None)
        self.assertEqual(len(ap._BOARD.posts), 1)
        self.assertEqual(ap._BOARD.posts[-1]["author_model"],
                         config.CAPTAIN_MODEL,
                         "the board must credit the captain's own seat")
        self.assertNotEqual(ap._BOARD.posts[-1]["author_model"],
                            "Some-Other-Model")
    def test_the_captain_persona_describes_today_s_roster(self):
        """CAPTAIN_PERSONA must agree with IMPLEMENT_TIERS, not a dated note.

        Pre-merge review 2026-09: the persona hardcoded "GLM-5.3, hard tier
        and the planner; DeepSeek-V4.1-Flash-thinking-max, the fast medium-tier
        workhorse", so after the 2026-09-25 flip it described the fleet
        backwards — and it named PLANNER_MODEL as the scarcest seat when GLM's
        driver cap (4) is now the smallest, not DeepSeek's (10).
        """
        text = captain.CAPTAIN_PERSONA
        for tier, models in config.IMPLEMENT_TIERS.items():
            for model in models:
                self.assertIn(model, text, f"{model} ({tier}) missing from persona")
        scarce, cap = config.scarcest_seat()
        self.assertIsNotNone(scarce)
        self.assertIn(f"{scarce}'s per-account", text,
                      "the persona must name the model whose cap is smallest")
        self.assertIn(f"{cap} at once", text)
        # The dated, now-wrong sentence is gone.
        self.assertNotIn("medium-tier workhorse", text)
        self.assertNotIn("two-model fleet (GLM-5.3", text)

    def test_the_captain_persona_calls_the_planner_the_hard_tier(self):
        """Locally the planner IS the hard tier; the studio planner is not.

        Stated as the rule (who may plan, derived from model_may) so a roster
        move does not need this test rewritten.
        """
        planners = [m for m in sorted(config.IMPLEMENTER_MODELS)
                    if config.model_may(m, "planner")]
        self.assertTrue(planners, "no planner-capable implementer on the roster")
        for model in planners:
            self.assertIn(model, captain.CAPTAIN_PERSONA)
        for model in sorted(config.IMPLEMENTER_MODELS):
            if config.model_may(model, "planner"):
                continue
            # A model that may NOT plan must not be claimed as one that does.
            self.assertNotIn(f"{model} may plan", captain.CAPTAIN_PERSONA)


class PlanChanges(Tmp):
    def setUp(self):
        super().setUp()
        self.tasks_dir = self.tmp / "tasks"
        self.tasks_dir.mkdir()
        self.tf = self.tasks_dir / "p.json"
        self.tf.write_text(json.dumps({"project": {"repo": str(self.tmp), "tasks": [
            {"id": i, "title": i, "prompt": "p", "model": "GLM-5.3",
             "reviewer": "deepseek", "verify_cmd": "true"}
            for i in ("merged1", "failed1", "fresh1")]}}))
        self.store = Store(str(self.tmp / "t.db"))
        self.store.upsert_code_task(str(self.tf), "merged1", "m", "GLM-5.3",
                                    "deepseek", "merged")
        self.store.upsert_code_task(str(self.tf), "failed1", "f", "GLM-5.3",
                                    "deepseek", "failed")
        self.snap = snap(tasks=[task("merged1", taskfile=str(self.tf))])

    def _propose(self, tid):
        a = ap.validate_action({"kind": "propose_plan_change", "project": "p",
                                "taskfile": str(self.tf), "body": "tighten gate",
                                "amend": {"kind": "change_verify", "task": tid,
                                          "verify_cmd": f"echo {tid}"}}, self.snap)
        self.assertIsNotNone(a)
        import code_tasks
        with mock.patch.object(code_tasks, "_amendment_validator",
                               return_value=lambda data: None):
            return ap._exec(a, self.store, None)

    def test_only_unstarted_or_failed_tasks_change(self):
        with mock.patch.object(config, "TASKS_DIR", str(self.tasks_dir)):
            merged = self._propose("merged1")
            failed = self._propose("failed1")
            fresh = self._propose("fresh1")
        self.assertFalse(merged["ok"])
        self.assertIn("merged", merged["error"])
        self.assertTrue(failed["ok"])
        self.assertTrue(fresh["ok"])
        cmds = {t["id"]: t["verify_cmd"] for t in
                json.loads(self.tf.read_text())["project"]["tasks"]}
        self.assertEqual(cmds, {"merged1": "true", "failed1": "echo failed1",
                                "fresh1": "echo fresh1"})
        # One proposal message per accepted change, none for the refused one.
        self.assertEqual([p["kind"] for p in self.board.posts],
                         ["proposal", "proposal"])

    def test_validator_rejection_posts_nothing(self):
        def refuse(data):
            raise ValueError("illegal plan")
        import code_tasks
        a = ap.validate_action({"kind": "propose_plan_change", "project": "p",
                                "taskfile": str(self.tf), "body": "tighten gate",
                                "amend": {"kind": "change_verify", "task": "failed1",
                                          "verify_cmd": "echo nope"}}, self.snap)
        with mock.patch.object(config, "TASKS_DIR", str(self.tasks_dir)), \
                mock.patch.object(code_tasks, "_amendment_validator",
                                  return_value=refuse):
            res = ap._exec(a, self.store, None)
        self.assertFalse(res["ok"])
        self.assertEqual(res.get("rejected"), 1)
        self.assertEqual(self.board.posts, [])
        cmds = {t["id"]: t["verify_cmd"] for t in
                json.loads(self.tf.read_text())["project"]["tasks"]}
        self.assertEqual(cmds["failed1"], "true")


class Ticks(Tmp):
    def _snap(self):
        return snap(tasks=[task("c1", status="conflict"),
                           task("s1", since=NOW - 3 * 3600, last_driver_ts=0)],
                    watchdog={"live": {"/tasks/p.json": 1}},
                    active_projects=["p"])

    def _tick(self, **kw):
        with mock.patch.object(ap, "observe", return_value=self._snap()), \
                mock.patch.object(ap, "Store"):
            return ap.tick(now=NOW, llm=False, **kw)

    def test_full_tick_end_to_end(self):
        with helpers.capture_events() as ev:
            res = self._tick()
        kinds = sorted(a["kind"] for a in res["actions"])
        self.assertEqual(kinds, ["board_post", "escalate_to_operator", "standup"])
        channels = [p["channel"] for p in self.board.posts]
        self.assertIn("operator", channels)      # the escalation
        self.assertIn("task:s1", channels)       # the nudge
        self.assertIn("project", channels)       # the standup
        self.assertEqual(channels.count("captain"), 2)   # reasoning, per action
        self.assertEqual(len(ev.of("captain.auto.action")), 3)
        self.assertEqual(len(ev.of("captain.escalation")), 1)
        self.assertEqual(len(ap.escalations()), 1)
        lines = ap.log_path().read_text().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(json.loads(x)["reason"] for x in lines))
        v = ap.view()
        self.assertEqual(v["last_tick"], NOW)
        self.assertEqual(len(v["findings"]), 2)
        # The next tick does not nag the same targets (cooldown) or re-standup.
        self.board.posts.clear()
        with helpers.capture_events() as ev:
            res = self._tick()
        self.assertEqual(res["actions"], [])
        self.assertEqual(self.board.posts, [])
        self.assertEqual(len(ev.of("captain.auto.skipped")), 2)
        # Acknowledge clears the dashboard badge list.
        esc_id = ap.escalations()[0]["id"]
        self.assertTrue(ap.ack_escalation(esc_id))
        self.assertEqual(ap.escalations(), [])
        self.assertFalse(ap.ack_escalation("deadbeef00"))

    def test_dry_run_logs_but_does_not_act(self):
        with helpers.capture_events() as ev:
            res = self._tick(dry_run=True)
        self.assertEqual(len(res["actions"]), 3)
        self.assertEqual(self.board.posts, [])
        self.assertEqual(ap.escalations(), [])
        self.assertEqual(len(ev.of("captain.auto.dry_run")), 3)
        self.assertEqual(ev.of("captain.auto.action"), [])
        self.assertEqual(len(ap.log_path().read_text().splitlines()), 3)
        self.assertEqual(ap.load_state().get("cooldowns", {}), {})

    def test_pause_switch(self):
        ap.set_paused(True)
        with helpers.capture_events() as ev:
            res = self._tick()
        self.assertTrue(res["paused"])
        self.assertEqual(self.board.posts, [])
        self.assertEqual(len(ev.of("captain.auto.paused")), 1)
        self.assertTrue(ap.view()["paused"])
        ap.set_paused(False)
        self.assertFalse(ap.is_paused())
        self.assertEqual(len(self._tick()["actions"]), 3)

    def test_resume_goes_through_captain_capacity_gate(self):
        s = snap(tasks=[task("s1", since=NOW - 3 * 3600, last_driver_ts=0)])
        with mock.patch.object(config, "TASKS_DIR", "/tasks"), \
                mock.patch.object(ap, "observe", return_value=s), \
                mock.patch.object(ap, "Store"), \
                mock.patch.object(captain, "execute_actions",
                                  return_value=[{"kind": "resume", "ok": False,
                                                 "queued": True}]) as ex:
            res = ap.tick(now=NOW, llm=False)
        ex.assert_called_once()
        self.assertEqual(ex.call_args[0][0], [{"kind": "resume",
                                               "taskfile": "p.json"}])
        self.assertTrue(res["actions"][0]["result"]["queued"])


class Observe(Tmp):
    def test_snapshot_digests_events_and_rows(self):
        db = str(self.tmp / "o.db")
        store = Store(db)
        wt = self.tmp / "wt" / "proj" / "t1"
        store.upsert_code_task("/tasks/p.json", "t1", "t", "GLM-5.3", "deepseek",
                               "running", worktree=str(wt))
        evp = self.tmp / "events.jsonl"
        evs = [{"ts": NOW - 500, "type": "task.gate", "task": "t1", "attempt": 2,
                "passed": False, "tail": "boom"},
               {"ts": NOW - 400, "type": "task.escalated", "task": "t1"},
               {"ts": NOW - 300, "type": "driver.start", "task": "t1-x3",
                "model": "GLM-5.3"},
               {"ts": NOW - 200, "type": "driver.cap_wait", "task": "t2",
                "model": "GLM-5.3"}]
        evp.write_text("\n".join(json.dumps(e) for e in evs) + "\n")
        with mock.patch.object(ap, "_watchdog_status", return_value={}):
            s = ap.observe(store=store, db_path=db, now=NOW, events_path=evp)
        t = s["tasks"][0]
        self.assertEqual((t["project"], t["fix_round"], t["escalations"]),
                         ("proj", 2, 1))
        self.assertEqual(t["last_gate"]["passed"], False)
        self.assertEqual(t["last_driver_ts"], NOW - 300)
        self.assertEqual(s["active_projects"], ["proj"])
        self.assertIn("proj", s["board"])
        self.assertEqual(s["seats"]["GLM-5.3"]["cap_waits"], 1)

    def test_status_clock_ignores_events_inside_the_status(self):
        """in_review stays dated from pr_opened; a later gate is not driver work."""
        db = str(self.tmp / "clock.db")
        store = Store(db)
        wt = self.tmp / "wt" / "proj"
        store.upsert_code_task("/tasks/p.json", "rev", "r", "GLM-5.3", "deepseek",
                               "in_review", worktree=str(wt / "rev"))
        store.upsert_code_task("/tasks/p.json", "impl", "i", "GLM-5.3", "deepseek",
                               "running", worktree=str(wt / "impl"))
        evs = [
            {"ts": NOW - 7200, "type": "task.pr_opened", "task": "rev"},
            {"ts": NOW - 60, "type": "task.pr_reviewed", "task": "rev",
             "approved": True},
            {"ts": NOW - 30, "type": "task.resynced", "task": "rev"},
            {"ts": NOW - 100 * 60, "type": "driver.start", "task": "impl",
             "model": "GLM-5.3"},
            {"ts": NOW - 20, "type": "task.gate", "task": "impl", "attempt": 4,
             "passed": False, "tail": "still failing"},
        ]
        evp = self.tmp / "clock.jsonl"
        evp.write_text("\n".join(json.dumps(e) for e in evs) + "\n")
        with mock.patch.object(ap, "_watchdog_status", return_value={}):
            s = ap.observe(store=store, db_path=db, now=NOW, events_path=evp)
        by = {t["id"]: t for t in s["tasks"]}
        self.assertEqual(by["rev"]["since"], NOW - 7200)
        self.assertEqual(by["impl"]["last_driver_ts"], NOW - 100 * 60)
        self.assertIn("stuck_in_review", rules(s))
        self.assertIn("stuck_implementing", rules(s))

    def test_blocked_chain_with_no_rows_is_observed(self):
        tasks = self.tmp / "tasks"
        tasks.mkdir()
        up = tasks / "up.json"
        down = tasks / "down.json"
        def spec(ids, after=()):
            proj = {"repo": str(self.tmp), "tasks": [
                {"id": i, "title": i, "prompt": "p", "model": "GLM-5.3",
                 "reviewer": "deepseek", "verify_cmd": "true"} for i in ids]}
            if after:
                proj["after"] = list(after)
            return {"project": proj}
        up_key = str(up.resolve())
        up.write_text(json.dumps(spec(["u1"])))
        down.write_text(json.dumps(spec(["d1"], after=[up_key])))
        db = str(self.tmp / "chain.db")
        store = Store(db)
        store.upsert_code_task(up_key, "u1", "u", "GLM-5.3", "deepseek", "failed")
        with mock.patch.object(config, "TASKS_DIR", tasks), \
                mock.patch.object(ap, "_watchdog_status", return_value={}):
            s = ap.observe(store=store, db_path=db, now=NOW,
                           events_path=self.tmp / "missing.jsonl")
        chains = [c for c in s["chains"] if Path(c["taskfile"]).name == "down.json"]
        self.assertEqual(len(chains), 1)
        self.assertIn(up_key, chains[0]["blocked"])
        self.assertEqual(rules(s), ["chain_blocked"])


if __name__ == "__main__":
    unittest.main()
