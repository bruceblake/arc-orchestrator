"""Per-role TOTAL budgets for a harness invocation — unlimited by default.

These tests originally pinned per-role wall-clock budgets (planner 5400,
reviewer 3600, implementer 2700 — PR #50) so a slow planner and a mechanical
implementer each got their own room. On 2026-09-14 the operator revoked total
budgets entirely ("just get rid of timeouts") after the wall clock — not a
fault — killed GLM-5.3 seven times in one night on the same task, every kill
landing mid-read with zero edits written. Total budgets now default to ``0``
= unlimited; the idle tripwire (``config.idle_timeout_for``) is the only
harness kill, and the lease invariant falls back to a fixed bound.

Nothing here spawns a harness: the deadline is a computation
(``t0 + budget if budget > 0 else float("inf")``) and is asserted as one.
"""
import os
import pathlib
import subprocess
import sys
import unittest

from helpers import capture_events  # noqa: F401  (sys.path via helpers import)

import config  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


class RoleBudgets(unittest.TestCase):
    """Roles keep SEPARATE env knobs; by default every one of them is off."""

    def test_each_role_has_its_own_total_budget(self):
        # Unlimited by default: the wall clock never fires. A role budget is a
        # knob you can still turn (env override), not a deadline that exists.
        self.assertTrue(config.ROLE_TIMEOUT, "ROLE_TIMEOUT itself went missing")
        for role in ("planner", "reviewer", "implementer"):
            self.assertIn(role, config.ROLE_TIMEOUT,
                          f"{role} lost its (now unlimited) budget knob")
            self.assertEqual(config.total_timeout_for(role), 0,
                             f"{role}'s total budget should default to unlimited")

    def test_an_unlisted_role_gets_the_global_default(self):
        for role in ("pr_reviewer", "anything-else", ""):
            self.assertEqual(config.total_timeout_for(role),
                             config.DRIVER_TIMEOUT,
                             f"{role!r} must fall back to DRIVER_TIMEOUT")

    def test_the_budget_is_per_role_not_per_harness(self):
        # Same role, any harness: the budget follows the ROLE.
        self.assertEqual(config.total_timeout_for("implementer"),
                         config.ROLE_TIMEOUT["implementer"])

    def test_each_role_budget_is_env_overridable(self):
        # Read at import time, so it must be a fresh interpreter.
        env = dict(os.environ,
                   ARC_PLANNER_TIMEOUT="111",
                   ARC_REVIEWER_TIMEOUT="222",
                   ARC_IMPLEMENTER_TIMEOUT="333")
        out = subprocess.run(
            [sys.executable, "-c",
             "import config;"
             "print(config.total_timeout_for('planner'),"
             "      config.total_timeout_for('reviewer'),"
             "      config.total_timeout_for('implementer'))"],
            cwd=str(ROOT), env=env, capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.split(), ["111.0", "222.0", "333.0"])

    def test_driver_timeout_is_still_the_unknown_role_budget(self):
        env = dict(os.environ, ARC_DRIVER_TIMEOUT="1234")
        out = subprocess.run(
            [sys.executable, "-c",
             "import config; print(config.total_timeout_for('nobody'))"],
            cwd=str(ROOT), env=env, capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "1234.0")


class TheLeaseOutlastsTheLongestRole(unittest.TestCase):
    """A lease reaped while its driver still runs breaks the model's ARC cap."""

    def test_the_longest_budget_helper_covers_every_role(self):
        longest = config.longest_total_timeout()
        self.assertGreaterEqual(longest, config.DRIVER_TIMEOUT)
        for role in config.ROLE_TIMEOUT:
            self.assertGreaterEqual(longest, config.total_timeout_for(role))
        self.assertEqual(longest, max([config.DRIVER_TIMEOUT]
                                      + list(config.ROLE_TIMEOUT.values())))

    def test_the_helper_takes_the_roles_it_is_asked_about(self):
        self.assertEqual(
            config.longest_total_timeout(("implementer",)),
            max(config.DRIVER_TIMEOUT, config.total_timeout_for("implementer")))

    def test_the_lease_outlasts_the_longest_role_budget(self):
        longest_hold = (config.longest_total_timeout()
                        + config.DRIVER_CAPACITY_BACKOFF_CAP)
        self.assertGreater(
            config.DRIVER_LEASE_TTL, longest_hold,
            "a lease could be reaped while a planner is still running")

    def test_unlimited_budgets_use_the_fixed_lease_fallback(self):
        # With every total at 0 there is no "longest run" to derive the lease
        # from, so the TTL must fall back to a fixed, generous bound instead of
        # collapsing to (0 + backoff + 300).
        if config.longest_total_timeout() > 0:
            self.skipTest("budgets are configured; the derived invariant applies")
        self.assertGreaterEqual(
            config.DRIVER_LEASE_TTL, 86400,
            "unlimited budgets need a generous fixed lease TTL")

    def test_status_reports_the_lease_against_the_longest_role(self):
        src = (ROOT / "main.py").read_text()
        self.assertIn("config.longest_total_timeout()", src)
        self.assertNotIn("config.DRIVER_LEASE_TTL > config.DRIVER_TIMEOUT", src)


class TheDeadlineHonoursTheRole(unittest.TestCase):
    """Both drivers must compute the deadline from the role, not the global."""

    def test_an_unlimited_budget_means_no_deadline(self):
        t0 = 1000.0
        for role in list(config.ROLE_TIMEOUT) + ["nobody"]:
            budget = config.total_timeout_for(role)
            deadline = t0 + budget if budget > 0 else float("inf")
            self.assertEqual(deadline, float("inf"),
                             f"{role!r} still gets a finite deadline by default")

    def test_both_drivers_read_the_role_budget_for_the_deadline(self):
        src = (ROOT / "drivers.py").read_text()
        self.assertNotIn("t0 + config.DRIVER_TIMEOUT", src,
                         "a driver still hard-codes the global total budget")
        # One deadline per pump: opencode's and reasonix's.
        self.assertEqual(
            src.count("total_budget = config.total_timeout_for(self.role)"), 2)
        self.assertEqual(
            src.count('deadline = t0 + total_budget if total_budget > 0 else float("inf")'),
            2)

    def test_the_timeout_event_reports_the_budget_that_actually_fired(self):
        src = (ROOT / "drivers.py").read_text()
        self.assertNotIn('{config.DRIVER_TIMEOUT}s total', src)
        self.assertEqual(src.count('{config.total_timeout_for(self.role)}s total'), 2)

    def test_the_stall_tripwire_stays_role_independent(self):
        # The total budget is per role; the idle tripwire is NOT touched here,
        # because a reviewer and an implementer both go quiet the same way.
        self.assertEqual(config.idle_timeout_for("reviewer"),
                         config.DRIVER_IDLE_TIMEOUT)
        self.assertEqual(config.idle_timeout_for("implementer"),
                         config.DRIVER_IDLE_TIMEOUT)
        src = (ROOT / "drivers.py").read_text()
        self.assertEqual(src.count("idle_budget = config.idle_timeout_for(self.role)"),
                         2)


if __name__ == "__main__":
    unittest.main()
