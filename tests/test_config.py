"""Invariants between the timeout/cap constants.

These constants are coupled, and the coupling is easy to break silently:
raising DRIVER_TIMEOUT without raising DRIVER_LEASE_TTL makes leases expire
under running drivers, which lets a model exceed its ARC cap — the exact
failure the lease table exists to prevent.
"""
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
