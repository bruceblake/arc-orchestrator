"""Invariants between the timeout/cap constants.

These constants are coupled, and the coupling is easy to break silently:
raising DRIVER_TIMEOUT without raising DRIVER_LEASE_TTL makes leases expire
under running drivers, which lets a model exceed its ARC cap — the exact
failure the lease table exists to prevent.
"""
import pathlib
import unittest

from helpers import capture_events  # noqa: F401  (sys.path)

import config


class TimeoutInvariants(unittest.TestCase):
    def test_lease_outlasts_the_longest_an_attempt_can_hold_it(self):
        longest_hold = config.DRIVER_TIMEOUT + config.DRIVER_CAPACITY_BACKOFF_CAP
        self.assertGreater(
            config.DRIVER_LEASE_TTL, longest_hold,
            "a lease can be reaped while its driver still runs; another driver "
            "would take the slot and the model would go over its ARC cap")

    def test_idle_timeout_is_the_binding_stall_detector(self):
        self.assertLess(config.DRIVER_IDLE_TIMEOUT, config.DRIVER_TIMEOUT,
                        "the wall clock must be a backstop, not the stall detector")

    def test_capacity_backoff_exceeds_the_crash_backoff(self):
        crash_max = min(30, 2 ** config.MAX_RETRIES)
        self.assertGreater(config.DRIVER_CAPACITY_BACKOFF, crash_max,
                           "a capacity rejection must wait longer than a crash")
        self.assertGreaterEqual(config.DRIVER_CAPACITY_BACKOFF_CAP,
                                config.DRIVER_CAPACITY_BACKOFF)


class RoutingInvariants(unittest.TestCase):
    def test_every_escalation_tier_is_a_known_implementer(self):
        for model in config.ESCALATION_PATH:
            self.assertIn(model, config.IMPLEMENTER_MODELS, model)

    def test_every_implementer_has_a_family_and_a_driver_cap(self):
        for model in config.IMPLEMENTER_MODELS:
            self.assertIn(model, config.MODEL_FAMILY, model)
            self.assertGreater(config.driver_limit(model), 0, model)

    def test_escalation_path_runs_weakest_to_strongest(self):
        """Escalation only makes sense if later tiers are scarcer/stronger."""
        caps = [config.driver_limit(m) for m in config.ESCALATION_PATH]
        self.assertEqual(caps, sorted(caps, reverse=True),
                         f"escalation path {config.ESCALATION_PATH} should move "
                         f"toward scarcer models, got caps {caps}")

    def test_driver_caps_leave_headroom_under_the_account_cap(self):
        """Driver caps must reserve room for interactive use of the account."""
        for model, family in config.MODEL_FAMILY.items():
            self.assertLessEqual(config.driver_limit(model),
                                 config.family_limit(family), model)


if __name__ == "__main__":
    unittest.main()


class HarnessContextBudget(unittest.TestCase):
    """The fleet declares a context budget to its harnesses.

    Lowering it below the harness default was tried and reverted: it made
    compaction fire earlier exactly as intended, and kimi-code's compaction
    never completes against this provider (20 full_compaction.begin across the
    whole session history, 0 full_compaction.end). Reaching a broken path
    sooner is strictly worse, so the budget stays at the default unless
    compaction is fixed upstream.
    """

    def test_budget_does_not_force_compaction_earlier_than_the_default(self):
        self.assertGreaterEqual(
            config.HARNESS_CONTEXT, 131072,
            "lowering this makes the fleet hit kimi-code's broken compaction "
            "sooner; the working lever is not growing context (see "
            "code_tasks._impl_prompt), not compacting it")

    def test_kimi_gets_an_alias_only_when_the_config_defines_it(self):
        """A missing alias must degrade to the default model, not fail the run
        with 'model not found'."""
        real = config.harness_model("Kimi-K3", "kimi")
        self.assertIn(real, (None, "arc/kimi-k3-fleet"))
        orig = config.KIMI_CONFIG
        config.KIMI_CONFIG = pathlib.Path("/nonexistent/config.toml")
        try:
            self.assertIsNone(config.harness_model("Kimi-K3", "kimi"))
        finally:
            config.KIMI_CONFIG = orig

    def test_opencode_never_gets_a_model_alias(self):
        """opencode sends the model KEY to the API, so a renamed alias comes
        back 'Model not found' — its budget comes from OPENCODE_CONFIG."""
        for m in config.IMPLEMENTER_MODELS:
            self.assertIsNone(config.harness_model(m, "opencode"))

    def test_the_switch_disables_every_alias(self):
        orig = config.USE_FLEET_ALIASES
        config.USE_FLEET_ALIASES = False
        try:
            self.assertIsNone(config.harness_model("Kimi-K3", "kimi"))
        finally:
            config.USE_FLEET_ALIASES = orig
