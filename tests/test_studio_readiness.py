"""Production readiness must not report success from failed prerequisites."""
import argparse
import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from helpers import capture_events  # sets repository import path
import config
from studio import cli, provision
from studio.engine import godot


class TestReadiness(unittest.TestCase):
    def test_only_active_subscription_harnesses_are_required(self):
        roster = [("Claude", "anthropic", "claude"), ("GPT", "openai", "codex")]
        with patch.object(config, "ROSTER", roster):
            self.assertEqual(set(provision.cli_status()), {"claude", "codex"})

    def test_failed_codex_status_is_not_authenticated(self):
        with tempfile.NamedTemporaryFile() as binary:
            for rc, output in [(1, "network failed"), (1, "Logged in"), (0, ""),
                               (0, "Not logged in"), (0, "Logged in using ChatGPT")]:
                with self.subTest(rc=rc, output=output), \
                     patch.object(config, "ROSTER", [("GPT", "openai", "codex")]), \
                     patch.object(config, "codex_bin", return_value=binary.name), \
                     patch("subprocess.run", return_value=subprocess.CompletedProcess([], rc, output, "")):
                    self.assertEqual(provision.cli_status()["codex"]["logged_in"],
                                     rc == 0 and output == "Logged in using ChatGPT")

    def test_failed_measurement_cannot_gate_or_promote(self):
        args = argparse.Namespace(project="p", repo="/tmp/game", phase="", force=False, reason="")
        with patch.object(Path, "exists", return_value=True), \
             patch.object(godot, "available", return_value=True), \
             patch.object(godot, "measure", side_effect=godot.GodotError("broken measurement")), \
             patch("studio.engine.stage_manager.check") as check, \
             patch("studio.engine.stage_manager.promote") as promote, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_gate(args), 1)
            self.assertEqual(cli.cmd_promote(args), 1)
            check.assert_not_called()
            promote.assert_not_called()

    def test_missing_godot_cannot_gate_when_measurer_exists(self):
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / godot.MEASURER
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text("placeholder")
            args = argparse.Namespace(project="p", repo=d, phase="")
            with patch.object(godot, "available", return_value=False), \
                 patch("studio.engine.stage_manager.check") as check, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.cmd_gate(args), 1)
                check.assert_not_called()

    def test_successful_measurement_reaches_gate(self):
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / godot.MEASURER
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text("placeholder")
            args = argparse.Namespace(project="p", repo=d, phase="graybox")
            result = {"phase": "graybox", "passed": True, "failures": []}
            with patch.object(godot, "available", return_value=True), \
                 patch.object(godot, "measure", return_value={"width": 12}) as measure, \
                 patch("studio.engine.stage_manager.check", return_value=result) as check, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.cmd_gate(args), 0)
                measure.assert_called_once_with(d)
                check.assert_called_once_with("p", d, "graybox")

    def test_nonzero_measurer_cannot_pass_with_success_marker(self):
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / godot.MEASURER
            script.parent.mkdir()
            script.write_text("placeholder")
            with patch.object(godot, "import_assets"), \
                 patch.object(godot, "_run", return_value=(1, "STUDIO_METRICS_OK")):
                with self.assertRaisesRegex(godot.GodotError, "measurer failed"):
                    godot.measure(d)

    def test_failed_provider_probe_blocks_readiness(self):
        g = {"godot_bin": "/godot", "display": ":0"}
        with patch.object(config, "STUDIO", True), patch.object(config, "STUDIO_API", True), \
             patch.object(provision.godot, "doctor", return_value=g), \
             patch.object(provision.astra_operator, "doctor", return_value={"missing": []}), \
             patch.object(provision, "opencode_plan", return_value={"missing": {}}), \
             patch.object(provision.budget, "summary", return_value={}), \
             patch.object(provision.openrouter, "api_key", return_value="test"), \
             patch.object(provision, "probe_models", return_value={"ok": False, "error": "offline"}):
            report = provision.doctor()
            self.assertFalse(report["ready"])
            self.assertIn("OpenRouter model probe failed: offline", report["problems"])


class TestGodotOnly(unittest.TestCase):
    def test_cli_lists_studio_and_rejects_retired_builder(self):
        root = Path(__file__).resolve().parent.parent
        help_run = subprocess.run([sys.executable, str(root / "main.py"), "--help"],
                                  text=True, capture_output=True)
        self.assertEqual(help_run.returncode, 0)
        self.assertIn("studio", help_run.stdout)
        retired = subprocess.run([sys.executable, str(root / "main.py"), "build"],
                                 text=True, capture_output=True)
        self.assertEqual(retired.returncode, 2)
        self.assertIn("invalid choice", retired.stderr)

    def test_research_graph_still_runs_without_minecraft(self):
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run([sys.executable, str(root / "main.py"), "graph"],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("minecraft", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()
