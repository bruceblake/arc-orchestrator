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
import re
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


def in_studio(snippet, fleet="studio", **env_extra):
    """Run `snippet` under a studio fleet profile and return its stdout."""
    env = dict(os.environ, ARC_FLEET=fleet, PYTHONPATH=str(ROOT))
    env.pop("ARC_ZEN_FREE", None)
    env.update(env_extra)
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
                      "Gemini-3.8-Flash", "Cursor-Grok-4.7",
                      "Antigravity-Gemini"):
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

        # Subscription: plan CLIs, and NO provider aliases — a plan-backed
        # model is reached by its own CLI. Zen (the only aliased rows on this
        # profile) is opt-in, so the default has none.
        self.assertEqual(sub["planner"], "Claude-Opus-5.5")
        self.assertEqual(sub["aliases"], {})
        for harness in ("claude", "codex", "cursor", "agy"):
            self.assertIn(harness, sub["harnesses"])
        self.assertIn("cursor", sub["fams"])
        self.assertIn("google", sub["fams"])
        self.assertNotIn("cursor", api["fams"])
        self.assertNotIn("xai", sub["fams"], "no Grok without a subscription CLI")
        # The old Gemini CLI is still not a subscription harness. Google on
        # this profile is Antigravity (`agy`). Gemini-3.8-Flash stays the
        # studio-api judge and is not an implementer.
        self.assertNotIn("gemini", sub["harnesses"])
        self.assertNotIn("agy", api["harnesses"])

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
            "plan = [m for m in config.EXTERNAL_MODELS if not m.startswith('Zen-')];"
            "print(json.dumps([config.provider_model_alias(m)"
            " for m in sorted(plan)]))")
        aliases = json.loads(out)
        self.assertTrue(aliases)
        self.assertTrue(all(a is None for a in aliases), aliases)

    ZEN_PROBE = ("import config, json;"
                 "zen = sorted(m for m in config.IMPLEMENTER_MODELS if m.startswith('Zen-'));"
                 "print(json.dumps({'n': len(zen), 'aliases': len(config._ZEN_ALIASES),"
                 " 'esc': config.ESCALATION_PATH,"
                 " 'sample': config.provider_model_alias(zen[0]) if zen else None}))")

    def test_zen_is_opt_in_and_stays_off_the_escalation_path(self):
        """Free Zen slugs may train on the game's source, and 11 of them in the
        escalation path made a task walk 17 stages before it could fail."""
        d = json.loads(in_studio(self.ZEN_PROBE))
        self.assertEqual((d["n"], d["aliases"]), (0, 0))
        self.assertFalse([m for m in d["esc"] if m.startswith("Zen-")])
        self.assertLessEqual(len(d["esc"]), 6, d["esc"])

    def test_zen_free_models_on_studio_roster(self):
        d = json.loads(in_studio(self.ZEN_PROBE, ARC_ZEN_FREE="1"))
        self.assertEqual(d["n"], len(config.ZEN_OPENCODE_SLUGS))
        self.assertEqual(d["aliases"], len(config.ZEN_OPENCODE_SLUGS))
        self.assertTrue(d["sample"], d["sample"])
        self.assertTrue(d["sample"].startswith("opencode/"))

    def test_subscription_harnesses_follow_the_subscription_cap(self):
        # Operator directive 2026-09-22: the plan seats are not capped low;
        # the plan's usage window is the limit, and Driver.run waits it out
        # (tests/test_usage_limit.py).
        out = in_studio(
            "import config, json;"
            "print(json.dumps([config.SUBSCRIPTION_SESSION_CAP,"
            " {h: config.harness_limit(h) for h in "
            "('claude', 'codex', 'cursor', 'agy')}]))")
        cap, caps = json.loads(out)
        self.assertEqual(caps, {"claude": cap, "codex": cap, "cursor": cap,
                                "agy": cap})

    IMAGE_PROBE = """
import json, config, drivers
out = {}
for key, d in (("gemini", drivers.GeminiDriver("any", "reviewer", bench=True)),
               ("codex", drivers.driver_for(config.STUDIO_OPENAI_MODEL, "reviewer")),
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

    def test_cursor_cli_runs_headless_in_the_worktree(self):
        out = in_studio(
            "import drivers, json\n"
            "d = drivers.driver_for('Cursor-Grok-4.7', 'implementer')\n"
            "print(json.dumps({'fresh': d.argv('fix the door', None),\n"
            " 'resume': d.argv('fix the door', 'chat-9')}))")
        argv = json.loads(out)
        fresh = " ".join(argv["fresh"])
        self.assertIn("--print", fresh)
        self.assertIn("stream-json", fresh)
        self.assertIn("--force", fresh)
        self.assertIn("--trust", fresh)
        self.assertIn("--model grok-4.7-high", fresh)
        self.assertNotIn("--resume", fresh)
        resumed = " ".join(argv["resume"])
        self.assertIn("--resume chat-9", resumed)

    def test_antigravity_cli_runs_headless_in_the_worktree(self):
        out = in_studio(
            "import drivers, json\n"
            "d = drivers.driver_for('Antigravity-Gemini', 'implementer')\n"
            "print(json.dumps({'fresh': d.argv('fix the door', None),\n"
            " 'resume': d.argv('fix the door', 'conv-9')}))")
        argv = json.loads(out)
        fresh = argv["fresh"]
        # `--print` consumes the next argument. Flags before it, prompt after.
        self.assertLess(fresh.index("--output-format"), fresh.index("--print"))
        self.assertEqual(fresh[fresh.index("--print") + 1], "fix the door")
        self.assertIn("--dangerously-skip-permissions", fresh)
        self.assertNotIn("--sandbox", fresh)
        self.assertNotIn("--model", fresh)
        resumed = argv["resume"]
        self.assertEqual(resumed[resumed.index("--print") + 1], "fix the door")
        self.assertLess(resumed.index("--conversation"), resumed.index("--print"))
        self.assertEqual(resumed[resumed.index("--conversation") + 1], "conv-9")

    ROLE_PROBE = """
import drivers
try:
    import config
    drivers.driver_for(config.STUDIO_OPENAI_MODEL, "planner")
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


AGY_STREAM = "\n".join([
    '{"event":"init","conversation_id":"9ec58bfd","init":{"cwd":"/tmp"}}',
    '{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"{\\"pass\\": true}"}}',
    '{"event":"result","result":{"conversation_id":"9ec58bfd","status":"SUCCESS","response":"{\\"pass\\": true}"}}',
])


class TestSubscriptionStreams(unittest.TestCase):
    """The subscription CLIs' real output reaches the pipeline intact."""

    def test_agy_answer_is_the_result_response_not_the_delta(self):
        import code_tasks, drivers
        sid, text = drivers.parse_transcript(AGY_STREAM)
        self.assertEqual(sid, "9ec58bfd")
        self.assertEqual(text, '{"pass": true}')
        self.assertEqual(code_tasks._parse_verdict(text),
                         {"pass": True, "issues": []})

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
        self.assertEqual(d["model"], "GPT-6-Sol", "operator decision 2026-09-22")
        i = d["argv"].index("-m")
        self.assertEqual(d["argv"][i + 1], "gpt-6-sol")

    def test_codex_runs_at_high_effort_not_the_model_default(self):
        """gpt-6-sol defaults to medium; the operator chose high."""
        argv = json.loads(in_studio(self.MODEL_PROBE))["argv"]
        self.assertIn('model_reasoning_effort="high"', argv)
        self.assertEqual(argv[argv.index('model_reasoning_effort="high"') - 1], "-c")

    RESUME_PROBE = """
import config, drivers, json
d = drivers.driver_for(config.STUDIO_OPENAI_MODEL, "implementer")
print(json.dumps(d.argv("P", "sess-1")))
"""

    def test_a_resumed_session_keeps_model_and_effort(self):
        argv = json.loads(in_studio(self.RESUME_PROBE))
        self.assertEqual(argv[1:4], ["exec", "resume", "sess-1"])
        self.assertIn("gpt-6-sol", argv)
        self.assertIn('model_reasoning_effort="high"', argv)

    def test_codex_argv_never_uses_the_s_flag(self):
        """`codex exec resume` rejects -s; the sandbox must be a -c override."""
        for sid in (None, "sess-1"):
            probe = ("import config, drivers, json;"
                     "d = drivers.driver_for(config.STUDIO_OPENAI_MODEL, 'implementer');"
                     f"print(json.dumps(d.argv('P', {sid!r})))")
            argv = json.loads(in_studio(probe))
            self.assertNotIn("-s", argv, sid)
            self.assertIn('sandbox_mode="workspace-write"', argv, sid)

    CLI_FLAGS_PROBE = """
import config, drivers, json
d = drivers.driver_for(config.STUDIO_OPENAI_MODEL, "implementer")
d.images = ["/tmp/x.png"]
print(json.dumps({"fresh": d.argv("P", None), "resume": d.argv("P", "sess-1")}))
"""

    @unittest.skipUnless(Path(config.codex_bin()).exists(), "codex CLI not installed")
    def test_every_codex_flag_is_accepted_by_the_installed_cli(self):
        """Pin the argv to the REAL CLI, so a Codex update cannot silently break it.

        The flag a subcommand rejects fails the whole attempt with exit 2 and
        no model call at all — indistinguishable, in the fix loop, from a task
        the model could not do. This asks the installed Codex which flags each
        subcommand takes and checks every one the driver passes.
        """
        argvs = json.loads(in_studio(self.CLI_FLAGS_PROBE))
        for kind, cmd in (("fresh", ["exec"]), ("resume", ["exec", "resume"])):
            help_text = subprocess.run([config.codex_bin(), *cmd, "--help"],
                                       capture_output=True, text=True, timeout=60).stdout
            accepted = set(re.findall(r"(?m)^\s+(?:(-\w), )?(--[\w-]+)", help_text))
            flags = {f for pair in accepted for f in pair if f}
            used = [a for a in argvs[kind] if a.startswith("-") and a != "-"]
            for flag in used:
                self.assertIn(flag, flags, f"{kind}: codex {' '.join(cmd)} does not accept {flag}")

    def test_ultra_effort_is_refused(self):
        """ultra delegates to sub-agents, multiplying sessions past the cap."""
        env = dict(os.environ, ARC_FLEET="studio", ARC_CODEX_REASONING="ultra",
                   PYTHONPATH=str(ROOT))
        p = subprocess.run([sys.executable, "-c", "import config"], env=env,
                           capture_output=True, text=True, cwd=str(ROOT), timeout=60)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("ultra", p.stderr)

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


class TestDbPath(unittest.TestCase):
    """`main.py` must record into config.DB_PATH, i.e. honour ARC_DB_PATH."""

    PROBE = """
import argparse, json, main
a = argparse.Namespace(db=None)
print(json.dumps([main.db_path(a, False), main.db_path(a, True)]))
"""

    def test_run_db_honours_arc_db_path(self):
        env = dict(os.environ, ARC_DB_PATH="/tmp/elsewhere.db", PYTHONPATH=str(ROOT))
        p = subprocess.run([sys.executable, "-c", self.PROBE], capture_output=True,
                           text=True, env=env, cwd=str(ROOT), timeout=60)
        real, dry = json.loads(p.stdout)
        self.assertEqual(real, "/tmp/elsewhere.db")
        self.assertTrue(dry.endswith("dry-run.db"), "dry runs keep their own file")

    def test_default_is_unchanged(self):
        env = {k: v for k, v in os.environ.items() if k != "ARC_DB_PATH"}
        env["PYTHONPATH"] = str(ROOT)
        p = subprocess.run([sys.executable, "-c", self.PROBE], capture_output=True,
                           text=True, env=env, cwd=str(ROOT), timeout=60)
        real, _ = json.loads(p.stdout)
        self.assertEqual(real, str(ROOT / "orchestrator.db"))


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

    def test_compiled_taskfile_records_its_fleet(self):
        with tempfile.TemporaryDirectory() as d:
            doc = gt.compile_taskfile(name="p", repo=d, tasks=[self._task()])
        self.assertEqual(doc["project"]["fleet"], config.FLEET)

    def test_loader_refuses_a_taskfile_from_another_fleet(self):
        """It used to say 'model X must be an implementer', naming the wrong problem."""
        import code_tasks
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "tf.json"
            path.write_text(json.dumps({"project": {
                "name": "p", "repo": d, "fleet": "studio",
                "tasks": [{"id": "a", "prompt": "x", "model": "GPT-6-Sol",
                           "reviewer": "glm", "verify_cmd": "true"}]}}))
            with self.assertRaises(ValueError) as cm:
                code_tasks.load_taskfile(path)
        self.assertIn("ARC_FLEET=studio", str(cm.exception))

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


class TestStudioRouting(unittest.TestCase):
    """Every studio worker resolves to a live, fitting model on each profile."""

    PROBE = ("import json; from studio.schemas import task as T;"
             "from studio.evaluation import judge_loop as J;"
             "print(json.dumps({'workers': {w: T.worker_model(w) for w in T.WORKERS},"
             " 'judges': J.available_judges()}))")

    def test_subscription_profile(self):
        d = json.loads(in_studio(self.PROBE))
        self.assertEqual(d["workers"]["grok_feature_driver"], "Cursor-Grok-4.7",
                         "Grok work fell through to GPT-6-Sol")
        self.assertEqual(d["workers"]["gemini_visual_judge"], "Antigravity-Gemini")
        self.assertIn("Antigravity-Gemini", d["judges"])
        self.assertGreaterEqual(len(d["judges"]), 3,
                                "a two-model judge panel cannot rotate away from its own taste")

    def test_api_profile(self):
        d = json.loads(in_studio(self.PROBE, fleet="studio-api"))
        self.assertEqual(d["workers"]["grok_feature_driver"], "Grok-4.7")
        self.assertEqual(d["workers"]["gemini_visual_judge"], "Gemini-3.8-Flash")


class TestPlannerFullReply(unittest.TestCase):
    """A long plan must reach the parser uncut (a 34k-char plan once did not)."""

    def _res(self, lines, text):
        import types
        d = tempfile.mkdtemp()
        path = Path(d, "plan.jsonl")
        path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        return types.SimpleNamespace(text=text, transcript_path=str(path))

    def test_the_uncut_result_record_wins_over_the_truncated_text(self):
        from studio import planner
        plan = json.dumps({"tasks": [{"id": f"t{i}", "prompt": "x" * 3000}
                                     for i in range(12)]})
        res = self._res([{"type": "system"}, {"type": "result", "result": plan}],
                        text=plan[-3000:])
        self.assertEqual(planner._full_reply(res), plan)

    def test_codex_and_antigravity_finals_are_read(self):
        from studio import planner
        long = "y" * 5000
        codex = self._res([{"type": "item.completed",
                            "item": {"type": "agent_message", "text": long}}], "y")
        agy = self._res([{"event": "result", "result": {"response": long}}], "y")
        self.assertEqual(planner._full_reply(codex), long)
        self.assertEqual(planner._full_reply(agy), long)

    def test_missing_transcript_falls_back_to_text(self):
        import types
        from studio import planner
        res = types.SimpleNamespace(text="short", transcript_path="/nonexistent/x")
        self.assertEqual(planner._full_reply(res), "short")


class TestPlannerPrompt(unittest.TestCase):
    """The planner prompt renders on every studio profile.

    It once read a WORKERS key that no longer existed, and nothing rendered
    the prompt, so the first live `studio plan` died with a KeyError.
    """

    PROBE = """
from studio import planner
from studio.schemas.task import PHASE_1_GRAYBOX_PROTOTYPING as P
p = planner.system_prompt(P, "/tmp/game")
import json, config
print(json.dumps({"len": len(p), "has_planner": config.PLANNER_MODEL in p,
                  "workers": [w for w in ("opus_architect", "gpt_6_astra_operator",
                                          "deepseek_qa_swarm") if w in p]}))
"""

    def test_prompt_renders_on_both_studio_profiles(self):
        for fleet in ("studio", "studio-api"):
            d = json.loads(in_studio(self.PROBE, fleet=fleet))
            self.assertGreater(d["len"], 3000, fleet)
            self.assertEqual(len(d["workers"]), 3, fleet)


class TestStudioStatus(unittest.TestCase):
    """The dashboard's Studio snapshot: read-only, contained, and accurate."""

    def setUp(self):
        self._dirs = [tempfile.TemporaryDirectory() for _ in range(3)]
        self.studio, self.tasks, self.repo = (Path(d.name) for d in self._dirs)
        self._old = (config.STUDIO_DIR, config.TASKS_DIR)
        config.STUDIO_DIR, config.TASKS_DIR = self.studio, str(self.tasks)
        (self.repo / "studio_target.json").write_text(json.dumps(
            {"bucket_a": {"corridor_width_m": 2.4, "vent_bore_m": 0.7},
             "bucket_b": {"mood": "dusk"}}))
        (self.repo / "studio_metrics.json").write_text(json.dumps({"corridor_width_m": 2.41}))
        stage_manager.promote("game", str(self.repo), force=True)
        (self.tasks / "game-phase_1_graybox_prototyping.json").write_text(json.dumps(
            {"project": {"name": "game", "repo": str(self.repo), "tasks": [
                {"id": "a", "model": "GLM-5.3", "reviewer": "deepseek", "deps": []},
                {"id": "b", "model": "GLM-5.3", "reviewer": "deepseek", "deps": ["a"]}]}}))

    def tearDown(self):
        config.STUDIO_DIR, config.TASKS_DIR = self._old
        for d in self._dirs:
            d.cleanup()

    def test_snapshot_reports_phase_gate_metrics_and_board(self):
        from studio import status
        snap = status.snapshot(None, live_tasks={"a": "reviewer"})
        (p,) = snap["projects"]
        self.assertEqual(p["phase"], "PHASE_1_GRAYBOX_PROTOTYPING")
        self.assertEqual([x["state"] for x in p["phases"]][:3], ["done", "current", "todo"])
        m = {r["key"]: r for r in p["metrics"]}
        self.assertTrue(m["corridor_width_m"]["ok"])
        self.assertIsNone(m["vent_bore_m"]["measured"])
        tasks = {t["id"]: t for t in p["boards"][0]["tasks"]}
        self.assertEqual(tasks["a"]["status"], "in_review",
                         "a task under review must not read as 'running'")
        self.assertEqual(tasks["b"]["status"], "pending")

    def test_snapshot_does_not_emit_gate_events(self):
        """The view polls every few seconds; looking is not an event."""
        from studio import status
        with capture_events() as evs:
            status.snapshot(None)
        self.assertEqual(evs.of("studio.gate"), [])

    def test_image_path_is_contained(self):
        from studio import status
        rd = config.studio_run_dir("game", create=True) / "round_1"
        rd.mkdir(parents=True)
        (rd / "shot.png").write_bytes(b"\x89PNG")
        (rd / "meta.json").write_text("{}")
        self.assertIsNotNone(status.image_path("game", "round_1/shot.png"))
        for bad in ("../../etc/passwd", "round_1/meta.json", "/etc/hosts",
                    "round_1/../../x.png"):
            self.assertIsNone(status.image_path("game", bad), bad)
        for bad_project in ("../game", "", "a/b", "x" * 200):
            self.assertIsNone(status.image_path(bad_project, "round_1/shot.png"))

    def _with_board(self):
        old = config.BOARD_DIR
        config.BOARD_DIR = self.studio / "boards"
        return old

    def test_thread_is_empty_when_there_is_no_board(self):
        from studio import status
        old = self._with_board()
        try:
            (p,) = status.snapshot(None)["projects"]
        finally:
            config.BOARD_DIR = old
        self.assertEqual(p["thread"], [])
        self.assertEqual(list(p["kanban"]),
                         ["backlog", "planned", "building", "review", "done", "blocked"])

    def test_thread_lists_recent_posts_without_session_ids(self):
        import board
        from studio import status
        old = self._with_board()
        try:
            name = Path(self.repo).name
            wt = self.studio / "wt"
            wt.mkdir()
            board.post(wt, task="doors", role="implementer", model="GLM-5.3",
                       harness="opencode", kind="handoff",
                       body="toggle(id) is the door api",
                       session_id="secret-session", project=name)
            board.post(wt, task="hud", role="reviewer", model="DeepSeek",
                       harness="reasonix", kind="note", body="looks fine",
                       project=name)
            (p,) = status.snapshot(None)["projects"]
        finally:
            config.BOARD_DIR = old
        self.assertEqual(len(p["thread"]), 2)
        first, second = p["thread"]
        self.assertEqual(first["task"], "doors")
        self.assertEqual(first["role"], "implementer")
        self.assertEqual(first["model"], "GLM-5.3")
        self.assertEqual(first["harness"], "opencode")
        self.assertEqual(first["kind"], "handoff")
        self.assertEqual(first["body"], "toggle(id) is the door api")
        self.assertIsInstance(first["timestamp"], float)
        self.assertEqual(first["session_owner"], "opencode")
        self.assertNotIn("session_id", first)
        self.assertNotIn("secret-session", json.dumps(p["thread"]))
        self.assertNotIn("session_owner", second)
        self.assertEqual(list(p["kanban"]),
                         ["backlog", "planned", "building", "review", "done", "blocked"])

    def test_thread_keeps_agent_markup_for_the_panel_to_escape(self):
        import board
        from studio import status
        old = self._with_board()
        body = '<img src=x onerror=alert(1)> & <b>'
        try:
            wt = self.studio / "wt"
            wt.mkdir()
            board.post(wt, task="<script>", role="implementer", model="m",
                       harness="cursor", kind="note", body=body,
                       session_id="sid-99", project=Path(self.repo).name)
            (p,) = status.snapshot(None)["projects"]
        finally:
            config.BOARD_DIR = old
        (row,) = p["thread"]
        self.assertEqual(row["body"], body)
        self.assertEqual(row["task"], "<script>")
        self.assertEqual(row["session_owner"], "cursor")
        self.assertNotIn("sid-99", json.dumps(row))


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


GROUNDED_B = {"mood": "dusk", "palette_hex": ["#c9ccd1", "#5a6069", "#8b5a3c"]}
ART = " ".join(["Concrete and painted steel, worn at hand height."] * 12)


class TestStageGates(StudioDirTest):
    def _project(self, art=False, **target):
        d = tempfile.mkdtemp()
        (Path(d) / "studio_target.json").write_text(json.dumps(target))
        if art:
            (Path(d) / "studio_art_direction.md").write_text(ART)
        return d

    def test_phase_0_demands_numeric_bucket_a(self):
        d = self._project(bucket_a={"note": "cells are small"},
                          bucket_b={"mood": "dusk"})
        r = stage_manager.check("p", d, gt.PHASE_0_TARGET_GROUNDING)
        self.assertFalse(r["passed"])
        self.assertTrue(any("NUMERIC" in f for f in r["failures"]))

    def test_phase_0_passes_when_fully_grounded(self):
        d = self._project(art=True, bucket_a={"corridor_width_m": 2.4},
                          bucket_b=GROUNDED_B)
        r = stage_manager.check("p", d, gt.PHASE_0_TARGET_GROUNDING)
        self.assertTrue(r["passed"], r["failures"])

    def test_phase_0_demands_a_colour_bible_and_art_direction(self):
        """'Show the AI what you mean' — the videos' first principle."""
        d = self._project(bucket_a={"corridor_width_m": 2.4}, bucket_b={"mood": "dusk"})
        fails = " ".join(stage_manager.check("p", d, gt.PHASE_0_TARGET_GROUNDING)["failures"])
        self.assertIn("palette_hex", fails)
        self.assertIn("studio_art_direction.md", fails)

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
        d = self._project(art=True, bucket_a={"corridor_width_m": 2.4}, bucket_b=GROUNDED_B)
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

    CLEAN_FUZZ = {"project": "p", "bots": 3, "messages_sent": 120,
                  "replies_seen": 120, "authority_violations": 0,
                  "desyncs": 0, "crashes": 0}

    def _fuzz(self, d, report):
        path = config.studio_run_dir("p", create=True) / "fuzz-1.json"
        path.write_text(json.dumps(report))
        return stage_manager.check("p", d, gt.PHASE_4_NETWORKED_QA)

    def test_phase_4_rejects_unreachable_or_empty_swarm(self):
        d = self._project(bucket_a={"x": 1}, bucket_b={"m": "x"})
        unreachable = {**self.CLEAN_FUZZ, "messages_sent": 0, "replies_seen": 0,
                       "server_unreachable": True}
        failures = self._fuzz(d, unreachable)["failures"]
        self.assertTrue(any("unreachable" in f for f in failures), failures)
        self.assertTrue(any("messages_sent" in f for f in failures), failures)
        # A reachable server with a real swarm IS evidence, and passes.
        self.assertTrue(self._fuzz(d, self.CLEAN_FUZZ)["passed"])

    def test_phase_4_needs_explicit_counts_for_the_same_project(self):
        d = self._project(bucket_a={"x": 1}, bucket_b={"m": "x"})
        for key in ("bots", "messages_sent", "replies_seen",
                    "authority_violations", "desyncs", "crashes"):
            missing = {k: v for k, v in self.CLEAN_FUZZ.items() if k != key}
            failures = self._fuzz(d, missing)["failures"]
            self.assertTrue(any(key in f for f in failures), (key, failures))
        for key in ("authority_violations", "desyncs", "crashes"):
            r = self._fuzz(d, {**self.CLEAN_FUZZ, key: "0"})
            self.assertFalse(r["passed"], (key, r["failures"]))
        r = self._fuzz(d, {**self.CLEAN_FUZZ, "project": "some-other-game"})
        self.assertFalse(r["passed"])
        self.assertTrue(any("project" in f for f in r["failures"]), r["failures"])

    def test_phase_4_keeps_genuine_failures_failing(self):
        d = self._project(bucket_a={"x": 1}, bucket_b={"m": "x"})
        for key, needle in (("authority_violations", "authoritative"),
                            ("desyncs", "desync"),
                            ("crashes", "crashed")):
            failures = self._fuzz(d, {**self.CLEAN_FUZZ, key: 2})["failures"]
            self.assertTrue(any(needle in f for f in failures), (key, failures))


class TestVideoChecks(StudioDirTest):
    """The checks the published workflows use: measure, look, the colour
    bible, the frame-rate budget and the workbench sign-off."""

    def _repo(self):
        d = tempfile.mkdtemp()
        (Path(d) / "studio_target.json").write_text(json.dumps(
            {"bucket_a": {"corridor_width_m": 2.4}, "bucket_b": GROUNDED_B}))
        (Path(d) / "studio_metrics.json").write_text(json.dumps({"corridor_width_m": 2.4}))
        return d

    def test_phase_1_requires_a_scripted_playtest(self):
        d = self._repo()
        fails = " ".join(stage_manager.check("p", d, gt.PHASE_1_GRAYBOX_PROTOTYPING)["failures"])
        self.assertIn("no scripted playtest", fails)

    def test_a_failing_playtest_check_blocks_the_phase(self):
        d = self._repo()
        Path(d, "tools").mkdir()
        Path(d, "tools", "playtest.gd").write_text("extends SceneTree")
        Path(d, "studio_playtest.json").write_text(json.dumps({"passed": False, "checks": [
            {"name": "reaches_yard", "passed": False, "value": 3.1, "expected": 0}]}))
        fails = stage_manager.playtest_status(d)[0]
        self.assertTrue(any("reaches_yard" in f for f in fails))
        Path(d, "studio_playtest.json").write_text(json.dumps({"passed": True, "checks": [
            {"name": n, "passed": True, "value": 0, "expected": 0}
            for n in ("reaches_yard", "route_time", "unseen")]}))
        self.assertEqual(stage_manager.playtest_status(d)[0], [])

    def test_a_playtest_needs_more_than_one_check(self):
        d = self._repo()
        Path(d, "tools").mkdir()
        Path(d, "tools", "playtest.gd").write_text("extends SceneTree")
        one = {"name": "ok", "passed": True, "value": 1, "expected": 1}
        Path(d, "studio_playtest.json").write_text(json.dumps({"passed": True, "checks": [one]}))
        self.assertTrue(any("at least" in f for f in stage_manager.playtest_status(d)[0]))
        Path(d, "studio_playtest.json").write_text(json.dumps(
            {"passed": True, "checks": [dict(one, name=f"c{i}") for i in range(3)]}))
        self.assertEqual(stage_manager.playtest_status(d)[0], [])

    def test_a_playtest_with_no_checks_proves_nothing(self):
        d = self._repo()
        Path(d, "tools").mkdir()
        Path(d, "tools", "playtest.gd").write_text("extends SceneTree")
        Path(d, "studio_playtest.json").write_text(json.dumps({"passed": True, "checks": []}))
        self.assertTrue(stage_manager.playtest_status(d)[0])

    def test_perf_gate_enforces_fps_and_shadow_casters(self):
        d = self._repo()
        Path(d, "studio_perf.json").write_text(json.dumps({"fps_p5": 20, "shadow_lights": 40}))
        fails = " ".join(stage_manager.perf_status(d)[0])
        self.assertIn("fps", fails)
        self.assertIn("shadow-casting", fails)
        Path(d, "studio_perf.json").write_text(json.dumps(
            {"fps_p5": config.STUDIO_MIN_FPS + 5, "shadow_lights": 2}))
        self.assertEqual(stage_manager.perf_status(d)[0], [])

    def test_workbench_signoff_gates_phase_2(self):
        from studio import approvals
        d = self._repo()
        rd = config.studio_run_dir("p", create=True)
        (rd / "mesh-bunk.json").write_text(json.dumps(
            {"model_path": "/x/bunk.glb", "triangles": 800, "max_triangle_count": 2000}))
        Path(d, "assets").mkdir()
        Path(d, "assets", "bunk.glb").write_bytes(b"x")
        fails = " ".join(stage_manager.check("p", d, gt.PHASE_2_3D_ASSET_AND_ANIMATION)["failures"])
        self.assertIn("not yet approved", fails)
        approvals.decide("p", "bunk.glb", "approved")
        fails = " ".join(stage_manager.check("p", d, gt.PHASE_2_3D_ASSET_AND_ANIMATION)["failures"])
        self.assertNotIn("not yet approved", fails)
        approvals.decide("p", "bunk.glb", "rejected", note="legs too thin")
        fails = " ".join(stage_manager.check("p", d, gt.PHASE_2_3D_ASSET_AND_ANIMATION)["failures"])
        self.assertIn("rejected assets", fails)

    def test_approvals_refuse_bad_input(self):
        from studio import approvals
        with self.assertRaises(ValueError):
            approvals.decide("p", "../../etc/passwd", "approved")
        with self.assertRaises(ValueError):
            approvals.decide("p", "bunk.glb", "maybe")


def _png(path, color, w=40, h=24, filt=0):
    import struct
    import zlib
    rows = b""
    for y in range(h):
        line = bytes(color) * w
        if filt == 1:  # "Sub" filter: store differences, the decoder must undo them
            raw = bytearray(line)
            line = bytes(raw[:3]) + bytes((raw[i] - raw[i - 3]) & 255 for i in range(3, len(raw)))
        rows += bytes([filt]) + line
    ch = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                           + ch(b"IDAT", zlib.compress(rows)) + ch(b"IEND", b""))


class TestPalette(unittest.TestCase):
    def test_hex_parsing(self):
        from studio.evaluation import palette
        self.assertEqual(palette.parse_hex("#c9ccd1"), (0xc9, 0xcc, 0xd1))
        self.assertEqual(palette.parse_hex("fff"), (255, 255, 255))
        with self.assertRaises(palette.PaletteError):
            palette.parse_hex("#12")

    def test_on_and_off_palette_renders(self):
        from studio.evaluation import palette
        with tempfile.TemporaryDirectory() as d:
            _png(f"{d}/on.png", (0x8a, 0x7f, 0x74), filt=1)
            _png(f"{d}/off.png", (0, 0xb0, 0xb0))
            pal = [palette.parse_hex(c) for c in ("#8b7d73", "#c9ccd1")]
            self.assertEqual(palette.conformance(f"{d}/on.png", pal, tolerance=40), 1.0)
            self.assertEqual(palette.conformance(f"{d}/off.png", pal, tolerance=40), 0.0)
            target = {"bucket_b": {"palette_hex": ["#8b7d73", "#c9ccd1"]}}
            _res, fails = palette.check_images([{"name": "off", "path": f"{d}/off.png"}],
                                               target, minimum=0.6)
            self.assertTrue(fails and "off" in fails[0])

    def test_non_png_is_refused_not_guessed(self):
        from studio.evaluation import palette
        with tempfile.TemporaryDirectory() as d:
            Path(d, "x.png").write_bytes(b"not a png")
            with self.assertRaises(palette.PaletteError):
                palette.read_png(Path(d, "x.png"))


class TestManualReviewGate(unittest.TestCase):
    """Gemini (Antigravity) or Cursor, driven by hand, get the last word."""

    def _run(self, responses):
        import asyncio
        import code_tasks
        import gitstore
        calls = []

        async def fake_gh(args, cwd, timeout=180):
            calls.append(args)
            if args[:2] == ["pr", "view"]:
                doc = responses.pop(0) if len(responses) > 1 else responses[0]
                return 0, json.dumps(doc), ""
            return 0, "", ""
        old_gh, old_poll = gitstore._gh, config.PR_MANUAL_POLL
        gitstore._gh, config.PR_MANUAL_POLL = fake_gh, 0
        try:
            with capture_events():
                out = asyncio.run(code_tasks._await_manual_review("/repo", "t", 7, 1))
        finally:
            gitstore._gh, config.PR_MANUAL_POLL = old_gh, old_poll
        return out, calls

    def test_approved_label_merges(self):
        out, _ = self._run([{"labels": [], "comments": []},
                            {"labels": [{"name": "manual-approved"}], "comments": []}])
        self.assertEqual(out["decision"], "approved")

    def test_rejection_carries_the_humans_comment_not_the_fleets(self):
        import datetime as dt
        now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        out, calls = self._run([{"labels": [{"name": "manual-rejected"}], "comments": [
            {"body": "**GLM-5.3** (round 1) — approved.", "createdAt": now},
            {"body": "The door slides through the wall at x=1.2; clamp it.", "createdAt": now}]}])
        self.assertEqual(out["decision"], "rejected")
        self.assertEqual(out["issues"], ["The door slides through the wall at x=1.2; clamp it."])
        self.assertTrue(any("--remove-label" in c for c in calls),
                        "the label must be cleared so the next round waits afresh")

    def test_timeout_never_turns_into_a_merge(self):
        old = config.PR_MANUAL_TIMEOUT
        config.PR_MANUAL_TIMEOUT = 0.001
        try:
            out, _ = self._run([{"labels": [], "comments": []}])
        finally:
            config.PR_MANUAL_TIMEOUT = old
        self.assertEqual(out["decision"], "rejected")

    def test_fleet_comments_are_recognised(self):
        import code_tasks
        self.assertTrue(code_tasks._is_fleet_comment("**DeepSeek** (round 2) — changes requested:"))
        self.assertTrue(code_tasks._is_fleet_comment("**Changes requested** (round 1) —"))
        self.assertFalse(code_tasks._is_fleet_comment("please clamp the door"))


class TestFeatureTracking(StudioDirTest):
    def test_kanban_places_tasks_and_unplanned_features(self):
        from studio import status
        boards = [{"phase": "PHASE_1_GRAYBOX_PROTOTYPING", "tasks": [
            {"id": "a", "title": "A", "status": "merged", "model": "m", "feature": "routine"},
            {"id": "b", "title": "B", "status": "in_review", "model": "m"},
            {"id": "c", "title": "C", "status": "failed", "model": "m"},
            {"id": "d", "title": "D", "status": "pending", "model": "m"}]}]
        feats = [{"id": "routine", "title": "Routine"}, {"id": "suspicion", "title": "Suspicion"}]
        k = status.kanban(boards, feats)
        self.assertEqual([c["id"] for c in k["done"]], ["a"])
        self.assertEqual([c["id"] for c in k["review"]], ["b"])
        self.assertEqual([c["id"] for c in k["blocked"]], ["c"])
        self.assertEqual([c["id"] for c in k["planned"]], ["d"])
        self.assertEqual([c["id"] for c in k["backlog"]], ["suspicion"],
                         "a feature a task already serves is not backlog")

    def test_changelog_reads_merged_task_commits(self):
        from studio import status
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
            run = lambda *a: subprocess.run(["git", "-C", d, *a], env=env, check=True,
                                            capture_output=True)
            run("init", "-q", "-b", "main")
            Path(d, "f").write_text("1")
            run("add", "f")
            run("commit", "-qm", "task(doors): Sliding doors (#4)\n\nModel: GPT-6-Sol\nReviewer: glm")
            (entry,) = status.changelog(d)
        self.assertEqual((entry["task"], entry["pr"], entry["model"], entry["reviewer"]),
                         ("doors", 4, "GPT-6-Sol", "glm"))

    def test_gauntlet_counts_rounds_not_spawn_retries(self):
        from studio import status
        log = Path(tempfile.mkdtemp()) / "events.jsonl"
        ev = [{"type": "driver.start", "role": "implementer", "task": "t-x1"}] * 5 + [
            {"type": "driver.start", "role": "implementer", "task": "t-x2"},
            {"type": "task.gate", "task": "t", "passed": False},
            {"type": "task.gate", "task": "t", "passed": True},
            {"type": "task.pr_reviewed", "task": "t", "approved": True}]
        log.write_text("\n".join(json.dumps(e) for e in ev) + "\n")
        old = config.EVENTS_LOG
        config.EVENTS_LOG = str(log)
        try:
            g = status.gauntlet(["t"])["t"]
        finally:
            config.EVENTS_LOG = old
        self.assertEqual((g["attempts"], g["gate_pass"], g["gate_fail"], g["pr_rounds"]),
                         (2, 1, 1, 1))


class TestApproveRoute(StudioDirTest):
    def test_route_only_signs_off_measured_assets_of_known_projects(self):
        import dashboard
        (config.studio_run_dir("game", create=True) / "stage.json").write_text("{}")
        (config.studio_run_dir("game") / "mesh-bunk.json").write_text(
            json.dumps({"model_path": "/x/bunk.glb"}))
        ok = lambda b: dashboard._studio_approve(b)[1]
        self.assertEqual(ok({"project": "nope", "asset": "bunk.glb", "state": "approved"}), 404)
        self.assertEqual(ok({"project": "game", "asset": "other.glb", "state": "approved"}), 404)
        self.assertEqual(ok({"project": "game", "asset": "bunk.glb", "state": "weird"}), 400)
        self.assertEqual(ok({"project": "game", "asset": "bunk.glb", "state": "rejected"}), 400,
                         "a rejection must say what to change")
        self.assertEqual(ok({"project": "game", "asset": "bunk.glb", "state": "approved"}), 200)


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


@unittest.skipUnless(godot.available(), "godot is not installed")
class TestScaffoldUnderGodot(unittest.TestCase):
    """Run the scaffold's GDScript in a REAL Godot and check what it measures.

    The first real run found two classes of bug that no Python test could:
    `:=` type inference that Godot 4 refuses on untyped values (the scripts
    did not parse at all), and a level whose vent shaft ran THROUGH the
    corridor ceiling, which the raycast measurer reported as 0.38m of crawl
    clearance against a 0.7m target. Skipped where Godot is absent (CI).
    """

    def test_scaffold_parses_measures_and_meets_its_own_target(self):
        from studio import scaffold
        with tempfile.TemporaryDirectory() as d:
            scaffold.create(d)
            exe = godot.godot_bin()
            subprocess.run([exe, "--headless", "--path", d, "--import", "--quit"],
                           capture_output=True, text=True, timeout=300)
            out = subprocess.run(
                [exe, "--headless", "--path", d, "--script", "res://tools/measure.gd"],
                capture_output=True, text=True, timeout=300)
            text = out.stdout + out.stderr
            self.assertEqual(godot.output_errors(text), [], text[-2000:])
            self.assertIn("STUDIO_METRICS_OK", text)
            gate = subprocess.run([sys.executable, "tools/assert_metrics.py"],
                                  cwd=d, capture_output=True, text=True, timeout=60)
            self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)
            pt = subprocess.run(
                [exe, "--headless", "--path", d, "--script", "res://tools/playtest.gd"],
                capture_output=True, text=True, timeout=300)
            self.assertIn("STUDIO_PLAYTEST PASS", pt.stdout + pt.stderr,
                          (pt.stdout + pt.stderr)[-2000:])
            report = json.loads(Path(d, "studio_playtest.json").read_text())
            self.assertTrue(report["passed"])
            self.assertGreaterEqual(len(report["checks"]), 3)
            metrics = json.loads(Path(d, "studio_metrics.json").read_text())
            self.assertAlmostEqual(metrics["crouch_clearance_m"], 0.7, places=2)
            self.assertAlmostEqual(metrics["ceiling_height_m"], 3.0, places=2)
            self.assertAlmostEqual(metrics["wall_height_m"], 6.0, places=2)


def _can_render():
    import os
    return godot.available() and bool(config.STUDIO_DISPLAY or os.environ.get("DISPLAY")
                                      or os.path.exists("/tmp/.X11-unix/X0"))


@unittest.skipUnless(_can_render(), "needs godot and a display")
class TestRenderUnderGodot(unittest.TestCase):
    """A real render: lit, aimed, and not blank.

    The first live render hung for 400s+ (the harness was launched as a scene,
    not with --script); the second produced ~2KB solid-black frames (no light
    in a graybox, and look_at() called before the tree started).
    """

    def test_graybox_renders_real_frames(self):
        import os
        from studio import scaffold
        with tempfile.TemporaryDirectory() as d:
            scaffold.create(d)
            subprocess.run([godot.godot_bin(), "--headless", "--path", d, "--import",
                            "--quit"], capture_output=True, timeout=300)
            old = config.STUDIO_DISPLAY
            config.STUDIO_DISPLAY = old or os.environ.get("DISPLAY") or ":0"
            try:
                shots = godot.render(d, camera_system.to_json(
                    camera_system.cameras_for_round("t", 1)), Path(d) / "out",
                    scene="res://scenes/world.tscn", resolution="480x270", timeout=300)
            finally:
                config.STUDIO_DISPLAY = old
            self.assertEqual(len(shots), 4)
            for s in shots:
                # A lit, aimed graybox frame compresses to tens of KB; the
                # blank frames it replaced were ~2KB of one or two colours.
                self.assertGreater(Path(s).stat().st_size, 10_000, s)


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
