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
    """The budget is per harness, because they fail differently at it.

    opencode's compaction works — measured firing twice inside one GLM-5.3 run
    which then carried on to 621KB, against ~350KB at the default where it
    never compacted. A smaller budget is a win there.

    kimi's compaction never completes against this provider: 20
    `full_compaction.begin` across the whole session history, 0
    `full_compaction.end`. Lowering its budget only reaches that dead end
    sooner — tried, measured, reverted.
    """

    def test_opencode_budget_is_small_enough_to_force_compaction(self):
        self.assertLess(config.OPENCODE_CONTEXT, 131072,
                        "opencode never compacts at the default, and its "
                        "compaction is the one that works")
        self.assertGreaterEqual(config.OPENCODE_CONTEXT, 16000,
                                "too small to hold a real task's working set")

    def test_kimi_budget_does_not_force_its_broken_compaction(self):
        self.assertGreaterEqual(
            config.KIMI_CONTEXT, 131072,
            "kimi compaction never completes; firing it earlier is strictly "
            "worse than not firing it")

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


class IdleTimeoutClearsTheLatencyTail(unittest.TestCase):
    """The idle timeout must exceed how long ARC makes a healthy request wait.

    ARC queues rather than refuses: median time-to-first-token is ~1s at every
    context size, but the tail reaches 308.9s (measured over 1927 completed
    steps) before the response streams normally. Time-to-first-token is stdout
    silence, so an idle timeout below that tail kills work that was about to
    succeed — at 120s, ~8.7% of context-heavy tasks.
    """

    OBSERVED_TTFT_TAIL_S = 309

    def test_idle_timeout_exceeds_the_observed_tail(self):
        self.assertGreater(
            config.DRIVER_IDLE_TIMEOUT, self.OBSERVED_TTFT_TAIL_S,
            "slow is not dead: this kills requests ARC would have answered")

    def test_total_timeout_allows_several_slow_steps(self):
        self.assertGreaterEqual(
            config.DRIVER_TIMEOUT, config.DRIVER_IDLE_TIMEOUT * 4,
            "the wall clock must not become the binding limit again")


class KimiPlanMode(unittest.TestCase):
    """Plan mode silently disables the fleet: agents propose instead of edit.

    Leaving plan mode requires approving ExitPlanMode, and a headless run has
    nobody to approve it. Before this was found, 182 of 206 sessions entered
    plan mode and only 44 left — most agents wrote long transcripts and
    changed no files at all.
    """

    def _probe(self, body):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d) / "config.toml"
            f.write_text(body)
            orig = config.KIMI_CONFIG
            config.KIMI_CONFIG = f
            try:
                return config.kimi_plan_mode_on()
            finally:
                config.KIMI_CONFIG = orig

    def test_detects_plan_mode_on(self):
        self.assertTrue(self._probe('default_plan_mode = true\n'))

    def test_detects_plan_mode_off(self):
        self.assertFalse(self._probe('default_plan_mode = false\n'))

    def test_ignores_a_commented_out_setting(self):
        self.assertFalse(self._probe('# default_plan_mode = true\n'))

    def test_missing_config_is_not_treated_as_plan_mode(self):
        orig = config.KIMI_CONFIG
        config.KIMI_CONFIG = pathlib.Path("/nonexistent/config.toml")
        try:
            self.assertFalse(config.kimi_plan_mode_on())
        finally:
            config.KIMI_CONFIG = orig

    def test_this_box_is_configured_to_let_agents_edit(self):
        self.assertFalse(config.kimi_plan_mode_on(),
                         "kimi default_plan_mode is true — fleet agents will "
                         "plan instead of edit")
