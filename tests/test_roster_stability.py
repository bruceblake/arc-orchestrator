"""The suite's roster fixtures must survive the OPERATOR'S routing env.

Measured 2026-09-15 evening: every `main.py code run` launched under the Rule 2
capacity hatch exports ARC_ESCALATION_PATH=<the surviving model>, the gate node
runs each task's verify_cmd in a fresh subprocess that INHERITS it, and
./check.sh runs this suite inside it. With a one-model env path,
config.ESCALATION_PATH collapsed to that single model, helpers.ENTRY became
helpers.STRONGEST, and the suite reported 10 failures + 5 errors on a tree with
NO work applied — five gate failures across three task worktrees, fix rounds
burned on infrastructure noise. tests/helpers.py now drops that variable before
`import config` binds it; these two tests pin that, so it cannot come back
silently.
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

from helpers import ENTRY, STRONGEST  # noqa: F401  (sys.path)

import config


class TheSuiteIsTwoTierRegardlessOfOperatorEnv(unittest.TestCase):
    def test_fixtures_are_two_tier(self):
        # ENTRY/STRONGEST are the positional aliases roughly a dozen tests
        # lean on; equal values silently turn those tests into tautologies —
        # or, in test_workqueue's lease test, into false failures.
        self.assertNotEqual(ENTRY, STRONGEST)
        self.assertGreaterEqual(len(config.ESCALATION_PATH), 2)
        self.assertEqual(ENTRY, config.ESCALATION_PATH[0])
        self.assertEqual(STRONGEST, config.ESCALATION_PATH[-1])
        self.assertNotIn("glm", config.REVIEW_FAMILIES)
        self.assertIn("deepseek", config.REVIEW_FAMILIES)
        # GLM-5.3 is NOT a review family since 2026-09-28 (implement-only).
        # This suite runs on the DEFAULT local fleet, where deepseek is
        # therefore the only review family; the studio profile is where the
        # multi-family fixtures get their second family (it has five).
        self.assertEqual(sorted(config.REVIEW_FAMILIES), ["deepseek"])
        self.assertNotIn("glm", config.REVIEW_FAMILIES,
                         "GLM-5.3 implements only and must not review")

    def test_poisoned_env_does_not_collapse_the_path(self):
        # The real shape of the 2026-09-15 breakage: a gate subprocess that
        # inherits the hatch's env. A child importing helpers the way the
        # suite does must still see a two-tier fleet.
        code = (
            "import helpers, config\n"
            "assert helpers.ENTRY == config.ESCALATION_PATH[0], (\n"
            "    'ARC_ESCALATION_PATH collapsed the fleet: '\n"
            "    + repr(helpers.ENTRY))\n"
            "assert 'glm' not in config.REVIEW_FAMILIES\n"
            "print('ok')\n"
        )
        env = {**os.environ, "ARC_ESCALATION_PATH": STRONGEST}
        proc = subprocess.run([sys.executable, "-c", code],
                              cwd=str(Path(__file__).resolve().parent),
                              env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=120)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout.decode(errors="replace"))
