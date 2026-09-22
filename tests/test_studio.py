"""The studio fleet: profile isolation, schema governance, and each module.

The most important test in this file is the FIRST one. The studio profile is
worth having only if it cannot disturb the fleet that is already working, so
`TestProfileIsolation` pins the local roster against the studio rows being
present in the same file.

Roster-dependent assertions run in a SUBPROCESS with ARC_FLEET=studio, because
config derives IMPLEMENTER_MODELS, REVIEW_FAMILIES, PLANNER_MODEL and the
escalation path at import time. Reloading config in-process would hand every
later test in the suite a different fleet.
"""
import json
import os
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from helpers import capture_events  # noqa: F401  (sets sys.path)

import config  # noqa: E402
from studio import budget  # noqa: E402
from studio.engine import godot, stage_manager  # noqa: E402
from studio.engine.operators import astra_operator  # noqa: E402
from studio.evaluation import arbitrator, camera_system, judge_loop  # noqa: E402
from studio.memory import compactor  # noqa: E402
from studio.qa import deepseek_fuzzer  # noqa: E402
from studio.schemas import task as gt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def in_studio(snippet, fleet="studio"):
    """Run `snippet` under a studio fleet profile and return its stdout."""
    env = dict(os.environ, ARC_FLEET=fleet, PYTHONPATH=str(ROOT))
    p = subprocess.run([sys.executable, "-c", snippet], capture_output=True,
                       text=True, env=env, cwd=str(ROOT), timeout=120)
    if p.returncode != 0:
        raise AssertionError(f"studio subprocess failed:\n{p.stdout}\n{p.stderr}")
    return p.stdout.strip()


class TestProfileIsolation(unittest.TestCase):
    """The studio rows must be invisible to the local fleet."""

    def test_local_is_the_two_model_fleet(self):
        self.assertEqual(config.FLEET, "local")
        self.assertFalse(config.STUDIO)
        self.assertEqual(sorted(config.FAMILIES), ["deepseek", "glm"])
        self.assertEqual(config.PLANNER_MODEL, "GLM-5.3")
        self.assertEqual(config.EXTERNAL_MODELS, set())
        self.assertEqual(config.MODEL_HARNESS_ALIAS, {})

    def test_no_studio_model_is_routable_locally(self):
        for model in ("Claude-Opus-5.5", "GPT-6-Astra", "Grok-4.7",
                      "Gemini-3.8-Flash"):
            self.assertNotIn(model, config.IMPLEMENTER_MODELS)
            self.assertNotIn(model, config.MODEL_ROLES)
            self.assertNotIn(model, config.ESCALATION_PATH)

    def test_bad_fleet_name_is_fatal(self):
        env = dict(os.environ, ARC_FLEET="studioo", PYTHONPATH=str(ROOT))
        p = subprocess.run([sys.executable, "-c", "import config"],
                           capture_output=True, text=True, env=env,
                           cwd=str(ROOT), timeout=60)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("ARC_FLEET", p.stderr)

    PROFILE_PROBE = """
import config, json
print(json.dumps({
    "fams": sorted(config.FAMILIES),
    "planner": config.PLANNER_MODEL,
    "impl": sorted(config.IMPLEMENTER_MODELS),
    "harnesses": sorted(set(config.MODEL_HARNESS.values())),
    "aliases": config.MODEL_HARNESS_ALIAS,
    "pr": config.PR_REVIEWERS,
}))
"""

    def test_both_studio_profiles_load(self):
        """Subscription and API are different fleets with the same shape."""
        sub = json.loads(in_studio(self.PROFILE_PROBE))
        api = json.loads(in_studio(self.PROFILE_PROBE, fleet="studio-api"))

        # Subscription: the three CLI harnesses, and NO provider aliases — a
        # plan-backed model is reached by its own CLI, never through
        # OpenRouter. An alias here would silently bill an empty account.
        self.assertEqual(sub["planner"], "Claude-Opus-5.5")
        self.assertEqual(sub["aliases"], {})
        for harness in ("claude", "codex"):
            self.assertIn(harness, sub["harnesses"])
        self.assertNotIn("xai", sub["fams"], "no Grok without a subscription CLI")
        # Gemini is only usable inside Antigravity on the operator's plan, so
        # the subscription roster has no google family and no gemini harness.
        self.assertNotIn("google", sub["fams"])
        self.assertNotIn("gemini", sub["harnesses"])

        # API: everything through opencode/openrouter, with aliases.
        self.assertEqual(api["planner"], "Claude-Opus-5.5")
        self.assertEqual(sorted(api["harnesses"]), ["opencode", "reasonix"])
        self.assertEqual(len(api["aliases"]), 4)
        self.assertIn("xai", api["fams"])

        for d in (sub, api):
            self.assertEqual(d["pr"], 2,
                             "a multi-family studio should field two PR reviewers")

    def test_subscription_models_never_get_a_provider_alias(self):
        """The bug this guards: routing a plan-backed model through OpenRouter."""
        out = in_studio(
            "import config, json;"
            "print(json.dumps([config.provider_model_alias(m)"
            " for m in sorted(config.EXTERNAL_MODELS)]))")
        aliases = json.loads(out)
        self.assertTrue(aliases)
        self.assertTrue(all(a is None for a in aliases), aliases)

    def test_subscription_harnesses_are_capped_for_a_human_plan(self):
        out = in_studio(
            "import config, json;"
            "print(json.dumps({h: config.harness_limit(h)"
            " for h in ('claude', 'codex', 'gemini')}))")
        caps = json.loads(out)
        self.assertEqual(caps["claude"], 1,
                         "the operator's own Claude session shares this plan")
        for h, cap in caps.items():
            self.assertLessEqual(cap, 2, f"{h} must not fan out on a consumer plan")

    IMAGE_PROBE = """
import json, drivers
out = {}
for key, d in (("gemini", drivers.GeminiDriver("any", "reviewer", bench=True)),
               ("codex", drivers.driver_for("GPT-6-Astra", "reviewer")),
               ("claude", drivers.driver_for("Claude-Opus-5.5", "reviewer"))):
    d.images = ["/renders/a.png"]
    out[key] = " ".join(d.argv("SCORE THIS", None))
print(json.dumps(out))
"""

    def test_each_cli_attaches_images_its_own_way(self):
        """The visual judge is useless if the renders never reach the model."""
        argv = json.loads(in_studio(self.IMAGE_PROBE))
        self.assertIn("@/renders/a.png", argv["gemini"])
        self.assertIn("-i /renders/a.png", argv["codex"])
        self.assertIn("/renders/a.png", argv["claude"])

    ROLE_PROBE = """
import drivers
try:
    drivers.driver_for("GPT-6-Astra", "planner")
    print("NO ERROR")
except ValueError:
    print("refused")
"""

    def test_drivers_refuse_a_role_the_roster_withholds(self):
        self.assertEqual(in_studio(self.ROLE_PROBE), "refused")

    def test_judge_never_becomes_an_implementer(self):
        """The role filter on IMPLEMENT_TIERS and the escalation path."""
        out = in_studio(
            "import config, json;"
            "print(json.dumps({'impl': sorted(config.IMPLEMENTER_MODELS),"
            "'tiers': config.IMPLEMENT_TIERS,"
            "'esc': config.ESCALATION_PATH}))")
        d = json.loads(out)
        self.assertNotIn("Gemini-3.8-Flash", d["impl"])
        self.assertNotIn("Gemini-3.8-Flash", d["esc"])
        for tier, models in d["tiers"].items():
            self.assertNotIn("Gemini-3.8-Flash", models, f"tier {tier}")

    def test_studio_fleet_config_path_is_separate(self):
        out = in_studio("import config; print(config.OPENCODE_FLEET_CONFIG.name)")
        self.assertNotEqual(out, config.OPENCODE_FLEET_CONFIG.name)


# Real headless streams, captured 2026-09-22 (trimmed). They pin the parsers
# to what the CLIs ACTUALLY emit rather than to what their docs describe.
CODEX_STREAM = "\n".join([
    '{"type":"thread.started","thread_id":"01a0cac5-5b94-7e51-b39c-bc1315ad50d3"}',
    '{"type":"turn.started"}',
    '{"type":"item.completed","item":{"id":"item_r","type":"reasoning","text":"the user wants a verdict"}}',
    '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"{\\"pass\\": true}"}}',
    '{"type":"turn.completed","usage":{"input_tokens":13731,"cached_input_tokens":11776,'
    '"cache_write_input_tokens":0,"output_tokens":9,"reasoning_output_tokens":0}}',
])
CLAUDE_STREAM = "\n".join([
    '{"type":"system","subtype":"init","session_id":"55feada1","model":"claude-opus-5-5","apiKeySource":"none"}',
    '{"type":"assistant","message":{"content":[{"type":"text","text":"thinking about it"}]},"session_id":"55feada1"}',
    '{"type":"result","subtype":"success","is_error":false,"result":"{\\"pass\\": true}",'
    '"session_id":"55feada1","usage":{"input_tokens":2,"cache_creation_input_tokens":8175,'
    '"cache_read_input_tokens":8141,"output_tokens":9}}',
])


class TestSubscriptionStreams(unittest.TestCase):
    """The subscription CLIs' real output reaches the pipeline intact."""

    def test_codex_answer_is_the_agent_message_not_the_reasoning(self):
        import drivers
        sid, text = drivers.parse_transcript(CODEX_STREAM)
        self.assertEqual(sid, "01a0cac5-5b94-7e51-b39c-bc1315ad50d3")
        self.assertEqual(text, '{"pass": true}')
        self.assertNotIn("the user wants", text)

    def test_codex_verdict_parses_for_review(self):
        import code_tasks, drivers
        self.assertEqual(code_tasks._parse_verdict(drivers.parse_transcript(CODEX_STREAM)[1]),
                         {"pass": True, "issues": []})

    def test_codex_tokens_do_not_double_count_the_cache(self):
        import drivers
        self.assertEqual(drivers.transcript_tokens(CODEX_STREAM), (13740, 13731, 9))

    def test_claude_answer_and_tokens(self):
        import code_tasks, drivers
        sid, text = drivers.parse_transcript(CLAUDE_STREAM)
        self.assertEqual(sid, "55feada1")
        self.assertEqual(code_tasks._parse_verdict(text), {"pass": True, "issues": []})
        # cache reads and writes are prompt tokens: 2 + 8175 + 8141
        self.assertEqual(drivers.transcript_tokens(CLAUDE_STREAM), (16327, 16318, 9))

    MODEL_PROBE = """
import config, drivers, json
d = drivers.driver_for(config.STUDIO_OPENAI_MODEL, "implementer")
print(json.dumps({"model": config.STUDIO_OPENAI_MODEL, "argv": d.argv("P", None)}))
"""

    def test_codex_runs_the_model_the_roster_names(self):
        d = json.loads(in_studio(self.MODEL_PROBE))
        self.assertEqual(d["model"], "GPT-6-Astra",
                         "the subscription default is Astra: no per-token cost")
        i = d["argv"].index("-m")
        self.assertEqual(d["argv"][i + 1], "gpt-6-astra")

    def test_api_profile_defaults_to_sol(self):
        out = in_studio("import config; print(config.STUDIO_OPENAI_MODEL)",
                        fleet="studio-api")
        self.assertEqual(out, "GPT-6-Sol")

    def test_single_claude_slot_is_not_the_default_reviewer(self):
        """Your own Claude session shares the plan; review must not queue on it."""
        out = in_studio(
            "import config, json;"
            "from studio.schemas.task import implementing_workers, worker_model,"
            " preferred_reviewer;"
            "print(json.dumps([preferred_reviewer(worker_model(w)) or"
            " config.cross_family_reviewer(worker_model(w))"
            " for w in implementing_workers()"
            " if worker_model(w) != 'Claude-Opus-5.5']))")
        self.assertNotIn("anthropic", json.loads(out))


class TestGameTaskGovernance(unittest.TestCase):
    """A game task may add fields; it may never drop a governance one."""

    def _task(self, **over):
        base = dict(task_id="block-out-cells", phase=gt.PHASE_1_GRAYBOX_PROTOTYPING,
                    assigned_worker="glm_content_swarm",
                    prompt="Block out the cell wing.",
                    verify_cmd="./check.sh")
        base.update(over)
        return gt.GameTask.from_dict(base)

    def test_verify_cmd_is_mandatory(self):
        with self.assertRaises(ValueError) as cm:
            self._task(verify_cmd="").validate()
        self.assertIn("Rule 4", str(cm.exception))

    def test_task_id_must_be_a_safe_ref(self):
        for bad in ("Block Out", "../escape", "x" * 80, ""):
            with self.assertRaises(ValueError):
                self._task(task_id=bad).validate()

    def test_unknown_phase_and_worker_are_rejected(self):
        with self.assertRaises(ValueError):
            self._task(phase="PHASE_9_SHIPPING").validate()
        with self.assertRaises(ValueError):
            self._task(assigned_worker="nobody").validate()

    def test_animation_requirements_belong_to_phase_2(self):
        with self.assertRaises(ValueError) as cm:
            self._task(animation_requirements={"clip_name": "cuff",
                                               "max_triangle_count": 100}).validate()
        self.assertIn(gt.PHASE_2_3D_ASSET_AND_ANIMATION, str(cm.exception))

    def test_computer_use_is_astra_only(self):
        with self.assertRaises(ValueError):
            self._task(computer_use_enabled=True).validate()

    def test_sync_target_needs_a_clip(self):
        with self.assertRaises(ValueError):
            gt.AnimationRequirements(sync_target_clip="x").validate("t")

    def test_compiles_to_a_real_taskfile_the_loader_accepts(self):
        """The whole contract: a GameTask becomes an ordinary governed task."""
        import code_tasks
        with tempfile.TemporaryDirectory() as d:
            doc = gt.compile_taskfile(
                name="prison-escape", repo=d,
                tasks=[self._task(),
                       self._task(task_id="yard-walls", deps=["block-out-cells"])])
            path = Path(d) / "tf.json"
            path.write_text(json.dumps(doc))
            tasks, _repo, _after = code_tasks.load_taskfile(path)[:3] \
                if isinstance(code_tasks.load_taskfile(path), tuple) \
                else (code_tasks.load_taskfile(path), None, None)
        self.assertTrue(tasks)

    def test_unknown_dep_is_rejected_at_compile(self):
        with self.assertRaises(ValueError):
            gt.compile_taskfile(name="p", repo="/tmp",
                                tasks=[self._task(deps=["nope"])])

    def test_review_preference_never_widens_config(self):
        own = config.MODEL_FAMILY.get("GLM-5.3")
        pref = gt.preferred_reviewer("GLM-5.3")
        if pref is not None:
            self.assertIn(pref, config.REVIEW_FAMILIES)
            self.assertNotEqual(pref, own)

    def test_studio_pairings_are_cross_family_and_spread(self):
        out = in_studio(
            "import config, json;"
            "from studio.schemas.task import implementing_workers, worker_model,"
            " preferred_reviewer;"
            "print(json.dumps({w: [worker_model(w),"
            " preferred_reviewer(worker_model(w))]"
            " for w in implementing_workers()}))")
        pairs = json.loads(out)
        for worker, (model, reviewer) in pairs.items():
            self.assertIsNotNone(reviewer, worker)
        reviewers = {r for _m, r in pairs.values()}
        self.assertGreater(len(reviewers), 1,
                           "review load must not funnel into one family")


class TestCameras(unittest.TestCase):
    def test_anchors_only_before_the_adversarial_round(self):
        cams = camera_system.cameras_for_round("p", 1)
        self.assertEqual(len(cams), 4)
        self.assertTrue(all(c.kind == camera_system.ANCHOR for c in cams))

    def test_adversarial_cameras_appear_and_are_deterministic(self):
        a = camera_system.cameras_for_round("p", config.STUDIO_ADVERSARIAL_ROUND)
        b = camera_system.cameras_for_round("p", config.STUDIO_ADVERSARIAL_ROUND)
        self.assertGreater(len(a), 4)
        self.assertEqual([c.to_dict() for c in a], [c.to_dict() for c in b])

    def test_adversarial_cameras_differ_between_rounds(self):
        r = config.STUDIO_ADVERSARIAL_ROUND
        self.assertNotEqual([c.to_dict() for c in camera_system.cameras_for_round("p", r)],
                            [c.to_dict() for c in camera_system.cameras_for_round("p", r + 1)])

    def test_project_may_override_anchors(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / camera_system.CAMERA_FILE).write_text(json.dumps(
                {"anchors": [{"name": "only", "position": [1, 2, 3],
                              "look_at": [0, 0, 0]}]}))
            cams = camera_system.cameras_for_round("p", 1, project_dir=d)
        self.assertEqual([c.name for c in cams], ["only"])

    def test_round_zero_is_rejected(self):
        with self.assertRaises(ValueError):
            camera_system.cameras_for_round("p", 0)


class TestArbitrator(unittest.TestCase):
    def test_oscillation_halts(self):
        a = arbitrator.assess([
            (1, ["brighten the cell corridor lighting"]),
            (2, ["darken the cell corridor, it is blown out"]),
            (3, ["brighten the corridor again, too murky"])])
        self.assertTrue(a.halt)
        self.assertIn("brightness", a.reason)

    def test_progress_does_not_halt(self):
        a = arbitrator.assess([
            (1, ["brighten the cell corridor lighting"]),
            (2, ["add grime to the yard walls"]),
            (3, ["soften the tower shadow edges"])])
        self.assertFalse(a.halt)

    def test_two_rounds_are_a_change_of_mind_not_an_argument(self):
        a = arbitrator.assess([(1, ["brighten the corridor"]),
                               (2, ["darken the corridor"])])
        self.assertFalse(a.halt)

    def test_different_subjects_do_not_conflict(self):
        a = arbitrator.assess([(1, ["more grime on the yard wall"]),
                               (2, ["less light in the vent shaft"]),
                               (3, ["more grime on the yard wall"])])
        self.assertFalse(a.halt)

    def test_overlapping_word_pairs_share_one_axis(self):
        """The bug this lexicon was rewritten for: brighten/lighten/darken."""
        self.assertTrue(arbitrator.conflicts(
            arbitrator.parse_directive("lighten the corridor"),
            arbitrator.parse_directive("darken the corridor")))


class StudioDirTest(unittest.TestCase):
    """Base class: every studio artefact goes to a temp dir, never logs/studio."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._old = config.STUDIO_DIR
        config.STUDIO_DIR = Path(self._dir.name)

    def tearDown(self):
        config.STUDIO_DIR = self._old
        self._dir.cleanup()

    def _shots(self, cams, where):
        out = []
        for c in cams:
            p = Path(where) / f"{c.name}.png"
            p.write_bytes(b"\x89PNG fake")
            out.append(p)
        return out


class TestCompactor(StudioDirTest):
    def test_context_is_bounded_to_baseline_anchors_plus_latest(self):
        with tempfile.TemporaryDirectory() as src:
            for r in (1, 2, 3):
                cams = camera_system.cameras_for_round("p", r)
                compactor.archive("p", r, self._shots(cams, src), cameras=cams)
        compactor.reset_baseline("p", phase="PHASE_1", round_n=1)
        ctx = compactor.context_images("p", 3)
        roles = [c["role"] for c in ctx]
        self.assertEqual(roles.count("baseline"), 4)
        self.assertTrue(all(c["kind"] == "anchor"
                            for c in ctx if c["role"] == "baseline"))
        self.assertEqual({c["round"] for c in ctx if c["role"] == "current"}, {3})
        self.assertLess(len(ctx), compactor.stats("p")["images_on_disk"])

    def test_missing_render_is_an_error_not_a_silent_skip(self):
        with self.assertRaises(FileNotFoundError):
            compactor.archive("p", 1, ["/nonexistent/x.png"])


class TestStageGates(StudioDirTest):
    def _project(self, **target):
        d = tempfile.mkdtemp()
        (Path(d) / "studio_target.json").write_text(json.dumps(target))
        return d

    def test_phase_0_demands_numeric_bucket_a(self):
        d = self._project(bucket_a={"note": "cells are small"},
                          bucket_b={"mood": "dusk"})
        r = stage_manager.check("p", d, gt.PHASE_0_TARGET_GROUNDING)
        self.assertFalse(r["passed"])
        self.assertTrue(any("NUMERIC" in f for f in r["failures"]))

    def test_phase_0_passes_with_numbers(self):
        d = self._project(bucket_a={"corridor_width_m": 2.4},
                          bucket_b={"mood": "dusk"})
        self.assertTrue(stage_manager.check("p", d, gt.PHASE_0_TARGET_GROUNDING)["passed"])

    def test_graybox_rejects_mesh_assets(self):
        d = self._project(bucket_a={"corridor_width_m": 2.4}, bucket_b={"m": "x"})
        Path(d, "assets").mkdir()
        Path(d, "assets", "bunk.glb").write_bytes(b"x")
        r = stage_manager.check("p", d, gt.PHASE_1_GRAYBOX_PROTOTYPING)
        self.assertFalse(r["passed"])
        self.assertTrue(any("primitives only" in f for f in r["failures"]))

    def test_unmeasured_bucket_a_target_is_a_failure(self):
        d = self._project(bucket_a={"corridor_width_m": 2.4, "vent_bore_m": 0.7},
                          bucket_b={"m": "x"})
        Path(d, "studio_metrics.json").write_text(json.dumps({"corridor_width_m": 2.4}))
        failures, checked = stage_manager.check_bucket_a(d)
        self.assertEqual(checked, 2)
        self.assertTrue(any("vent_bore_m" in f and "no measurement" in f
                            for f in failures))

    def test_bucket_a_tolerance(self):
        d = self._project(bucket_a={"corridor_width_m": 2.4}, bucket_b={"m": "x"})
        Path(d, "studio_metrics.json").write_text(json.dumps({"corridor_width_m": 2.41}))
        self.assertEqual(stage_manager.check_bucket_a(d)[0], [])
        Path(d, "studio_metrics.json").write_text(json.dumps({"corridor_width_m": 1.8}))
        self.assertTrue(stage_manager.check_bucket_a(d)[0])

    def test_promotion_requires_the_gate_and_resets_the_baseline(self):
        d = self._project(bucket_a={"corridor_width_m": 2.4}, bucket_b={"m": "x"})
        self.assertEqual(stage_manager.current_phase("p"), gt.PHASE_0_TARGET_GROUNDING)
        res = stage_manager.promote("p", d)
        self.assertTrue(res["promoted"])
        self.assertEqual(stage_manager.current_phase("p"),
                         gt.PHASE_1_GRAYBOX_PROTOTYPING)
        self.assertEqual(compactor.baseline("p")["phase"],
                         gt.PHASE_1_GRAYBOX_PROTOTYPING)

    def test_failing_gate_blocks_promotion_but_force_records_it(self):
        d = self._project(bucket_a={}, bucket_b={})
        self.assertFalse(stage_manager.promote("p", d)["promoted"])
        self.assertEqual(stage_manager.current_phase("p"), gt.PHASE_0_TARGET_GROUNDING)
        forced = stage_manager.promote("p", d, force=True, reason="operator")
        self.assertTrue(forced["promoted"])
        hist = stage_manager.state("p")["history"][-1]
        self.assertTrue(hist["forced"])
        self.assertTrue(hist["failures_at_promotion"])

    def test_phase_4_needs_a_fuzz_report(self):
        d = self._project(bucket_a={"x": 1}, bucket_b={"m": "x"})
        r = stage_manager.check("p", d, gt.PHASE_4_NETWORKED_QA)
        self.assertFalse(r["passed"])
        self.assertTrue(any("fuzz report" in f for f in r["failures"]))


class TestJudgeContract(StudioDirTest):
    def test_crashed_verdict_is_not_a_score(self):
        self.assertIsNone(judge_loop._normalise(None))
        self.assertIsNone(judge_loop._normalise({"artifacts": []}))

    def test_overall_score_is_salvaged_from_the_buckets(self):
        v = judge_loop._normalise({"bucket_a_score": 80, "bucket_b_score": 60})
        self.assertEqual(v["score"], 70.0)

    def test_pass_defaults_to_the_configured_threshold(self):
        low = judge_loop._normalise({"score": config.STUDIO_JUDGE_PASS - 1})
        high = judge_loop._normalise({"score": config.STUDIO_JUDGE_PASS + 1})
        self.assertFalse(low["pass"])
        self.assertTrue(high["pass"])

    def test_target_file_is_required(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError):
                judge_loop.load_target(d)

    def test_no_judges_on_the_local_fleet(self):
        self.assertEqual(judge_loop.available_judges(), [])
        with self.assertRaises(ValueError):
            judge_loop.judge_for_round(1)

    def test_rotation_cycles_in_studio(self):
        out = in_studio(
            "import json;"
            "from studio.evaluation import judge_loop as j;"
            "n=len(j.available_judges());"
            "print(json.dumps({'n': n,"
            " 'picks': [j.judge_for_round(r) for r in range(1, 2 * n + 1)]}))")
        d = json.loads(out)
        n, picks = d["n"], d["picks"]
        self.assertGreater(n, 1, "one model judging every round is not a rotation")
        self.assertEqual(picks[:n], picks[n:], "the rotation must cycle")
        self.assertEqual(len(set(picks[:n])), n, "every judge takes a turn")


class TestBudget(StudioDirTest):
    def test_guard_refuses_once_the_ceiling_is_crossed(self):
        old = config.STUDIO_BUDGET_USD
        try:
            config.STUDIO_BUDGET_USD = 1.0
            self.assertIsNotNone(budget.guard("GLM-5.3", task="t"))
            budget.record("GLM-5.3", {"cost_usd": 1.5}, task="t")
            with self.assertRaises(budget.BudgetExceeded):
                budget.guard("GLM-5.3", task="t")
        finally:
            config.STUDIO_BUDGET_USD = old

    def test_zero_ceiling_disables_the_guard(self):
        old = config.STUDIO_BUDGET_USD
        try:
            config.STUDIO_BUDGET_USD = 0.0
            budget.record("GLM-5.3", {"cost_usd": 999.0})
            self.assertIsNone(budget.guard("GLM-5.3"))
        finally:
            config.STUDIO_BUDGET_USD = old

    def test_torn_ledger_line_never_blocks_a_run(self):
        budget.record("GLM-5.3", {"cost_usd": 0.5})
        with budget._ledger_path().open("a") as fh:
            fh.write("{not json\n")
        self.assertEqual(budget.spent(), 0.5)


class TestToolchainHonesty(unittest.TestCase):
    """Absent binaries must report, never crash or pretend."""

    def test_godot_doctor_survives_a_missing_binary(self):
        d = godot.doctor()
        self.assertIn("can_render", d)
        if not d["godot_bin"]:
            self.assertFalse(d["can_render"])
            self.assertTrue(d["why_not_render"])

    def test_astra_doctor_lists_what_is_missing(self):
        d = astra_operator.doctor()
        self.assertIn("missing", d)
        if not d["blender"]:
            self.assertFalse(d["can_model"])
            self.assertIn("blender", d["missing"])

    def test_render_without_a_display_explains_itself(self):
        old = config.STUDIO_DISPLAY
        try:
            config.STUDIO_DISPLAY = ""
            env_display = os.environ.pop("DISPLAY", None)
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaises(godot.GodotError) as cm:
                    godot.render(d, [], Path(d) / "out")
            self.assertIn("headless", str(cm.exception).lower())
        finally:
            config.STUDIO_DISPLAY = old
            if env_display is not None:
                os.environ["DISPLAY"] = env_display

    def test_godot_output_errors_are_found_despite_exit_zero(self):
        self.assertTrue(godot.output_errors(
            "SCRIPT ERROR: Parse Error: Identifier 'foo' not declared"))
        self.assertFalse(godot.output_errors("Godot Engine v4.4 - loaded fine"))


class TestRenderInspectLoop(unittest.TestCase):
    """The operator must be able to SEE what it built, not just build it.

    Published accounts of GPT-6 Astra's Blender workflow put render -> inspect
    -> revise at the centre of it. An operator that only runs scripts writes
    geometry blind, so these pin that the loop exists and is mandatory.
    """

    def test_render_preview_is_a_tool_the_model_can_call(self):
        names = [t["function"]["name"] for t in astra_operator.TOOLS]
        self.assertIn("render_preview", names)
        # Rendering must be offered BEFORE the screen and mouse tools: the
        # prompt tells the model to prefer scripted, observable work.
        self.assertLess(names.index("render_preview"),
                        names.index("mouse_click"))

    def test_system_prompt_makes_the_loop_mandatory(self):
        prompt = astra_operator.SYSTEM_PROMPT
        for step in ("BUILD", "RENDER", "COMPARE", "REVISE ONE THING", "MEASURE"):
            self.assertIn(step, prompt)

    def test_system_prompt_states_the_organic_limit(self):
        self.assertIn("Organic forms", astra_operator.SYSTEM_PROMPT)
        self.assertIn("base mesh", astra_operator.SYSTEM_PROMPT)

    def test_render_without_blender_explains_itself(self):
        old = config.BLENDER_BIN
        try:
            config.BLENDER_BIN = "/nonexistent/blender"
            with self.assertRaises(astra_operator.OperatorError) as cm:
                astra_operator.render_preview("/tmp/x.blend")
            self.assertIn("blender", str(cm.exception).lower())
        finally:
            config.BLENDER_BIN = old

    def test_render_probe_ships_alongside_the_operator(self):
        self.assertTrue(astra_operator.RENDER_PROBE.exists())
        self.assertTrue(astra_operator.PROBE.exists())

    def test_shell_entrypoint_exposes_render(self):
        """The subscription path reaches the loop through a shell command."""
        p = subprocess.run(
            [sys.executable, "-m", "studio.engine.operators.astra_operator",
             "render", "--help"],
            capture_output=True, text=True, cwd=str(ROOT), timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("blend_file", p.stdout)


# --- the fuzz swarm, against real servers ------------------------------------
BOUNDS = {"x": [-60, 60], "y": [0, 20], "z": [-60, 60]}


def _clamp(v, lo, hi):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return lo
    if v != v or v in (float("inf"), float("-inf")):
        return lo
    return max(lo, min(hi, v))


def _server(trusting):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            name = "?"
            for line in self.rfile:
                try:
                    msg = json.loads(line)
                except ValueError:
                    self.wfile.write(b'{"ok":false}\n')
                    continue
                if msg.get("type") == "join":
                    name = msg.get("name", "?")
                    self.wfile.write(b'{"ok":true}\n')
                elif msg.get("type") == "move":
                    pos = msg.get("pos", [0, 0, 0])
                    if trusting:
                        out = pos
                    else:
                        out = [_clamp(pos[i] if len(pos) > i else 0, *BOUNDS[ax])
                               for i, ax in enumerate("xyz")]
                    self.wfile.write(
                        (json.dumps({"players": {name: {"pos": out}}}) + "\n").encode())
                else:
                    self.wfile.write((json.dumps({"ok": bool(trusting)}) + "\n").encode())

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    srv = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class TestFuzzSwarm(StudioDirTest):
    def _proto_dir(self, port):
        d = tempfile.mkdtemp()
        (Path(d) / "studio_protocol.json").write_text(json.dumps({
            "transport": "tcp", "host": "127.0.0.1", "port": port,
            "encoding": "json", "handshake": {"type": "join", "name": "$BOT"},
            "state_field": "players", "position_field": "pos",
            "move": {"type": "move", "pos": [0, 0, 0]},
            "privileged": [{"type": "open_door", "door": "gate"}],
            "bounds": BOUNDS, "max_speed_mps": 6.5}))
        return d

    def test_a_trusting_server_is_caught(self):
        srv = _server(trusting=True)
        try:
            report = deepseek_fuzzer.fuzz("p", self._proto_dir(srv.server_address[1]),
                                          bots=4, seconds=1.5)
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertGreater(report["authority_violations"], 0)
        self.assertGreater(report["messages_sent"], 0)

    def test_an_authoritative_server_passes(self):
        srv = _server(trusting=False)
        try:
            report = deepseek_fuzzer.fuzz("p", self._proto_dir(srv.server_address[1]),
                                          bots=4, seconds=1.5)
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(report["authority_violations"], 0)
        self.assertEqual(report["crashes"], 0)

    def test_an_unreachable_server_is_labelled_not_scored(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead_port = s.getsockname()[1]
        report = deepseek_fuzzer.fuzz("p", self._proto_dir(dead_port),
                                      bots=3, seconds=1.0)
        self.assertTrue(report.get("server_unreachable"))
        self.assertEqual(report["authority_violations"], 0)

    def test_missing_protocol_explains_the_contract(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(deepseek_fuzzer.ProtocolError) as cm:
                deepseek_fuzzer.load_protocol(d)
        self.assertIn("studio_protocol.json", str(cm.exception))

    def test_enet_is_refused_with_a_reason(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "studio_protocol.json").write_text(
                json.dumps({"transport": "enet", "port": 1}))
            with self.assertRaises(deepseek_fuzzer.ProtocolError) as cm:
                deepseek_fuzzer.load_protocol(d)
        self.assertIn("WebSocketMultiplayerPeer", str(cm.exception))


class TestScaffold(unittest.TestCase):
    def test_scaffold_passes_its_own_phase_0_gate(self):
        from studio import scaffold
        with tempfile.TemporaryDirectory() as d, \
                tempfile.TemporaryDirectory() as studio_dir:
            written = scaffold.create(d)
            self.assertIn("project.godot", written)
            self.assertIn("studio_target.json", written)
            old = config.STUDIO_DIR
            config.STUDIO_DIR = Path(studio_dir)
            try:
                r = stage_manager.check("p", d, gt.PHASE_0_TARGET_GROUNDING)
            finally:
                config.STUDIO_DIR = old
        self.assertTrue(r["passed"], r["failures"])

    def test_scaffold_is_graybox_clean(self):
        from studio import scaffold
        with tempfile.TemporaryDirectory() as d:
            scaffold.create(d)
            self.assertEqual(stage_manager.find_mesh_assets(d), [])

    def test_scaffold_does_not_overwrite_without_force(self):
        from studio import scaffold
        with tempfile.TemporaryDirectory() as d:
            scaffold.create(d)
            marker = Path(d) / "studio_target.json"
            marker.write_text("{}")
            self.assertEqual(scaffold.create(d), [])
            self.assertEqual(marker.read_text(), "{}")


if __name__ == "__main__":
    unittest.main()
