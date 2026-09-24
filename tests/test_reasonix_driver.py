"""ReasonixDriver: the DeepSeek harness since 2026-09-13.

Nothing here spawns reasonix. The stream shapes are captured verbatim from
reasonix 1.38.7 against ARC on 2026-09-13, so the parsers are tested against
what the binary actually prints, and the routing/argv/env tests pin the
contract the driver was verified with (a real run: edit + shell verify +
tokens, 15.5 s).
"""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import code_tasks
import config
import drivers

DS = "DeepSeek-V4.1-Flash-thinking-max"

STREAM = "\n".join([
    '{"kind":"turn_started","messageId":"01A"}',
    '{"kind":"turn_phase","text":"checking","phase":"checking"}',
    '{"kind":"tool_dispatch","tool":{"name":"read_file","args":"{\\"path\\":\\"calc.py\\"}"}}',
    '{"kind":"turn_phase","text":"working","phase":"working"}',
    '{"kind":"text","messageId":"01B","text":"fix"}',
    '{"kind":"message","messageId":"01B","text":"fixed"}',
    '{"kind":"usage","usage":{"promptTokens":6881,"completionTokens":124,"totalTokens":7005,'
    '"cacheHitTokens":6720,"cacheMissTokens":161,"source":"executor"}}',
    '{"kind":"usage","usage":{"promptTokens":7083,"completionTokens":3,"totalTokens":7086,'
    '"cacheHitTokens":6848,"cacheMissTokens":235,"source":"executor"}}',
    '{"kind":"completion_summary","completion":{"verdict":"uncertain"}}',
    '{"type":"result","subtype":"success","is_error":false,"duration_ms":11955,"num_turns":1,'
    '"result":"fixed","session_id":"20260913-220318.834403677-DeepSeek-V4.1-Flash-thinking-max",'
    '"usage":{"input_tokens":0,"output_tokens":0}}',
])

ERROR_STREAM = (
    '{"kind":"turn_started","messageId":"01C"}\n'
    '{"type":"result","subtype":"error_during_execution","is_error":true,"duration_ms":300,'
    '"num_turns":0,"result":"provider request failed: 400 {\\"detail\\":\\"concurrent session '
    'limit reached for model \'GLM-5.3\'\\"}","session_id":"20260913-220511-GLM-5.3"}\n'
)


class Routing(unittest.TestCase):
    def test_deepseek_rides_reasonix_by_roster(self):
        self.assertEqual(config.MODEL_HARNESS[DS], "reasonix")
        d = drivers.driver_for(DS, "implementer")
        self.assertIsInstance(d, drivers.ReasonixDriver)
        self.assertEqual(d.harness, "reasonix")

    def test_code_tasks_driver_follows_and_honours_a_policy_pin(self):
        self.assertIsInstance(code_tasks._driver(DS, "reviewer", None), drivers.ReasonixDriver)
        pinned = code_tasks._driver(DS, "reviewer", {"harness": {DS: "reasonix"}})
        self.assertIsInstance(pinned, drivers.ReasonixDriver)
        old = code_tasks._driver(DS, "reviewer", {"harness": {DS: "dsh"}})
        self.assertIsInstance(old, drivers.DeepseekDriver, "dsh stays pinnable for benches")

    def test_roles_still_come_from_the_roster(self):
        with self.assertRaises(ValueError):
            drivers.ReasonixDriver(DS, "planner")       # DeepSeek never plans
        with self.assertRaises(ValueError):
            drivers.ReasonixDriver("Kimi-K3", "implementer")   # retired
        drivers.ReasonixDriver(DS, "planner", bench=True)   # a bench may override

    def test_harness_caps_and_factor_are_registered(self):
        self.assertIn("reasonix", config._HARNESS_CAP)
        self.assertIn("reasonix", config._SESSIONS_PER_PROCESS)
        self.assertGreater(config.harness_limit("reasonix"), 0)
        self.assertGreater(config.driver_limit(DS), 0)
        self.assertTrue(config.harness_bin("reasonix").endswith("reasonix"))


class Argv(unittest.TestCase):
    def test_headless_stream_json_with_bypass_permissions(self):
        d = drivers.ReasonixDriver(DS, "implementer")
        a = d.argv("do the thing", None)
        self.assertTrue(a[0].endswith("reasonix"))
        self.assertEqual(a[1], "run")
        self.assertEqual(drivers._reasonix_provider(DS),
                         "arc-deepseek-v4-1-flash-thinking-max")
        self.assertEqual(a[a.index("--model") + 1],
                         f"{drivers._reasonix_provider(DS)}/{DS}")
        self.assertEqual(a[a.index("--permission-mode") + 1], "bypassPermissions")
        self.assertEqual(a[a.index("--output-format") + 1], "stream-json")
        self.assertEqual(a[-1], "do the thing")
        self.assertNotIn("-c", a)

    def test_a_session_id_continues_the_workspace_session(self):
        a = drivers.ReasonixDriver(DS, "implementer").argv("go on", "sess")
        self.assertEqual(a[-2:], ["-c", "go on"])


class FleetHome(unittest.TestCase):
    """reasonix reads keys only from <REASONIX_HOME>/.env — never the shell."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rxhome-"))
        self._saved = (config.REASONIX_FLEET_HOME, config.API_KEY, config.BASE_URL)
        config.REASONIX_FLEET_HOME = self.tmp / "home"
        config.API_KEY = "sk-test-123"
        config.BASE_URL = "https://llm-api.example/api/v1"

    def tearDown(self):
        config.REASONIX_FLEET_HOME, config.API_KEY, config.BASE_URL = self._saved

    def test_home_is_generated_with_the_arc_provider_key_and_no_sandbox(self):
        home = Path(drivers.reasonix_fleet_home())
        cfg = (home / "config.toml").read_text()
        self.assertIn('kind           = "openai"', cfg)
        self.assertIn('base_url       = "https://llm-api.example/api/v1"', cfg)
        self.assertIn(f'models         = ["{DS}"]', cfg)
        self.assertIn('api_key_env    = "ARC_API_KEY"', cfg)
        self.assertIn('bash = "off"', cfg, "no bubblewrap here: the shell tool must not refuse")
        self.assertIn('mode = "allow"', cfg)
        env = home / ".env"
        self.assertEqual(env.read_text(), "ARC_API_KEY=sk-test-123\n")
        self.assertEqual(stat.S_IMODE(env.stat().st_mode), 0o600)

    def test_one_provider_per_model_with_its_real_context_window(self):
        # context_window is PER-PROVIDER and windows differ 4x across the
        # roster, so models may not share one entry: at a shared window
        # DeepSeek compacted at ~52K of its 512K and the loop-guard refused
        # the model's writes mid-task (fleet-ops, 2026-09-13).
        cfg = (Path(drivers.reasonix_fleet_home()) / "config.toml").read_text()
        ds, glm = drivers._reasonix_provider(DS), drivers._reasonix_provider("GLM-5.3")
        self.assertNotEqual(ds, glm)
        self.assertIn(f'name           = "{ds}"', cfg)
        self.assertIn(f'name           = "{glm}"', cfg)
        self.assertIn(f'default_model = "{ds}/{DS}"', cfg)
        self.assertIn(f"context_window = {config.reasonix_context(DS)}", cfg)
        self.assertIn(f"context_window = {config.reasonix_context('GLM-5.3')}", cfg)
        self.assertEqual(config.reasonix_context(DS), 524288,
                         "ARC docs 2026-09-12: every V4.1-Flash variant is 512K")
        self.assertEqual(config.reasonix_context("DeepSeek-V4.1-Flash-thinking-low"),
                         524288, "the prefix covers every thinking variant")
        self.assertEqual(config.reasonix_context("GLM-5.3"), 131072)
        self.assertIn(f"bash_timeout_seconds = {int(config.GATE_TIMEOUT)}", cfg,
                      "an implementer's own ./check.sh must survive the harness bash")

    def test_regenerated_only_when_inputs_change(self):
        home = Path(drivers.reasonix_fleet_home())
        cfg = home / "config.toml"
        m1 = cfg.stat().st_mtime_ns
        drivers.reasonix_fleet_home()
        self.assertEqual(cfg.stat().st_mtime_ns, m1, "an unchanged home is not rewritten")
        config.BASE_URL = "https://other/api/v1"
        drivers.reasonix_fleet_home()
        self.assertIn("https://other/api/v1", cfg.read_text())

    def test_driver_env_points_reasonix_at_that_home(self):
        d = drivers.ReasonixDriver(DS, "implementer")
        env = d.extra_env("/x/wt")
        self.assertEqual(env["REASONIX_HOME"], str(config.REASONIX_FLEET_HOME))
        self.assertEqual(env["REASONIX_TELEMETRY"], "off")
        self.assertEqual(env["REASONIX_WORKSPACE_ROOT"], "/x/wt")
        self.assertEqual(drivers.OpencodeDriver("GLM-5.3", "implementer").extra_env("/x"), {})


class Transcript(unittest.TestCase):
    def test_answer_is_the_result_object_not_the_phase_chatter(self):
        sid, text = drivers.parse_transcript(STREAM)
        self.assertEqual(text, "fixed")
        self.assertEqual(sid, "20260913-220318.834403677-DeepSeek-V4.1-Flash-thinking-max")
        self.assertNotIn("checking", text)

    def test_a_reviewer_verdict_in_the_result_parses(self):
        stream = STREAM.replace('"result":"fixed"', '"result":"{\\"pass\\": true}"')
        _, text = drivers.parse_transcript(stream)
        self.assertEqual(code_tasks._parse_verdict(text), {"pass": True, "issues": []})

    def test_tokens_come_from_the_usage_receipts(self):
        total, prompt, completion = drivers.transcript_tokens(STREAM)
        self.assertEqual((total, prompt, completion), (7005 + 7086, 6881 + 7083, 124 + 3))

    def test_opencode_step_finish_still_counts(self):
        raw = '{"type":"step_finish","part":{"tokens":{"total":10,"input":6,"output":3,"reasoning":1,"cache":{"read":0,"write":0}}}}'
        self.assertEqual(drivers.transcript_tokens(raw), (10, 6, 4))

    def test_partial_stream_without_result_falls_back_to_the_dig(self):
        partial = "\n".join(STREAM.splitlines()[:6])
        sid, text = drivers.parse_transcript(partial)
        self.assertIsNone(sid)
        self.assertIn("fixed", text)

    def test_an_arc_capacity_error_is_recognised_from_the_result(self):
        _, text = drivers.parse_transcript(ERROR_STREAM)
        self.assertIn("concurrent session limit", text)
        self.assertTrue(drivers.Driver.is_capacity_error(text))


class Doctor(unittest.TestCase):
    def test_harness_bin_resolves_every_live_harness(self):
        for h in set(config.MODEL_HARNESS.values()):
            self.assertTrue(config.harness_bin(h))


if __name__ == "__main__":
    unittest.main()


class Activity(unittest.TestCase):
    def test_stall_evidence_names_the_tool_from_a_reasonix_record(self):
        rec = json.loads('{"kind":"tool_dispatch","tool":{"name":"edit_file","args":"{}"}}')
        self.assertEqual(drivers._describe_record(rec), "tool_dispatch:edit_file")
        self.assertEqual(drivers._describe_record({"kind": "usage", "usage": {}}), "usage")
        tail = drivers.activity_tail(STREAM, n=8)
        self.assertIn("tool_dispatch:read_file", tail)
        self.assertEqual(tail[-1], "result")


class DeepSeekSeats(unittest.TestCase):
    def test_all_ten_deepseek_sessions_are_usable(self):
        """Operator directive 2026-09-24: the account's 10 DeepSeek sessions all run."""
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in ("ARC_HARNESS_LIMIT_REASONIX", "ARC_DRIVER_LIMIT_DEEPSEEK"):
                os.environ.pop(k, None)
            self.assertEqual(config.harness_limit("reasonix"), 10)
            self.assertEqual(config._SESSIONS_PER_PROCESS["reasonix"], 1)
