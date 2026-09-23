"""Subscription usage windows: uncapped plan seats, and waiting out a reset.

Operator directive 2026-09-22: the studio's plan-backed models (Claude Code,
Codex/GPT-6) run with no local session limit, and when the PLAN refuses on its
usage window the driver parks until the window resets instead of failing the
task. Before this, "usage limit reached" was filed as a crash: retried every
60 s for ~20 minutes, then fix rounds and escalations were spent on a model
that was only out of quota.
"""
import asyncio
import datetime
import json
import unittest
from pathlib import Path
from unittest import mock

from helpers import capture_events  # noqa: F401  (sys.path)
from test_drivers import TempLeaseDB
from test_studio import in_studio

import config
import drivers
from drivers import Driver, DriverError, DriverResult


NOW = 1_790_000_000.0


class Detection(unittest.TestCase):
    def test_plan_refusals_are_usage_limits(self):
        for text in ("Claude AI usage limit reached|1790150400",
                     "You've hit your limit · resets 3pm (Europe/London)",
                     "5-hour limit reached ∙ resets 11am",
                     "Weekly limit reached",
                     "You've hit your usage limit. Upgrade to Pro or try again in 2 hours",
                     '{"type":"error","code":"usage_limit_reached"}'):
            self.assertTrue(drivers.is_usage_limit(text), text)

    def test_arc_capacity_is_not_a_usage_limit(self):
        # ARC session limits clear in seconds; parking for hours on them would
        # stall the whole local fleet.
        for text in ('400 {"detail": "concurrent session limit reached"}',
                     "backend queue is full", "429 rate limit", "real crash", ""):
            self.assertFalse(drivers.is_usage_limit(text), text)


class ResetParsing(unittest.TestCase):
    def test_epoch_suffix(self):
        self.assertEqual(drivers.usage_reset_at(
            "Claude AI usage limit reached|1790150400", NOW), 1790150400)

    def test_rejected_rate_limit_event(self):
        ev = json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "resetsAt": 1790012345, "rateLimitType": "five_hour"}})
        self.assertEqual(drivers.usage_reset_at("noise\n" + ev, NOW), 1790012345)

    def test_allowed_rate_limit_event_is_ignored(self):
        ev = json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed", "resetsAt": 1790012345}})
        self.assertIsNone(drivers.usage_reset_at(ev, NOW))

    def test_relative_try_again(self):
        self.assertEqual(drivers.usage_reset_at(
            "You've hit your usage limit ... try again in 2 hours 13 minutes.", NOW),
            NOW + 2 * 3600 + 13 * 60)

    def test_codex_seconds_only_count_when_the_limit_was_reached(self):
        # Codex reports every window's reset on every turn; a window that is
        # not exhausted must not decide how long we wait.
        self.assertIsNone(drivers.usage_reset_at('{"resets_in_seconds": 9000}', NOW))
        self.assertEqual(drivers.usage_reset_at(
            '{"code":"usage_limit_reached","resets_in_seconds": 9000}', NOW), NOW + 9000)

    def test_wall_clock_reset_with_zone_is_the_next_occurrence(self):
        at =drivers.usage_reset_at("You've hit your limit · resets 3pm (Etc/UTC)", NOW)
        dt = datetime.datetime.fromtimestamp(at, datetime.timezone.utc)
        self.assertEqual((dt.hour, dt.minute), (15, 0))
        self.assertGreater(at, NOW)
        self.assertLessEqual(at - NOW, 86400)

    def test_unstated_reset_is_none(self):
        self.assertIsNone(drivers.usage_reset_at("Claude AI usage limit reached", NOW))


class ScriptedDriver(Driver):
    harness = "fake-plan"
    model = config.ESCALATION_PATH[0]
    role = "implementer"

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def argv(self, prompt, session_id):
        return ["true"]

    async def _once(self, prompt, worktree, session_id, task_id, attempt):
        self.calls += 1
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return DriverResult(self.harness, self.model, self.role, 0, text=step)


class WaitsForTheReset(unittest.TestCase):
    """Driver.run parks on a spent plan and resumes when the window reopens."""

    def setUp(self):
        drivers._usage_blocked_until.clear()
        self.addCleanup(drivers._usage_blocked_until.clear)
        # These tests pin the park-until-reset path. Swapping is on by default
        # and would hand the attempt to another harness instead of sleeping.
        self._swap = config.USAGE_SWAP
        config.USAGE_SWAP = False
        self.addCleanup(setattr, config, "USAGE_SWAP", self._swap)

    def _run(self, drv):
        clock = [NOW]
        slept = []

        async def fake_sleep(d):
            slept.append(d)
            clock[0] += d

        with TempLeaseDB(), capture_events() as ev, \
                mock.patch.object(drivers.time, "time", lambda: clock[0]), \
                mock.patch.object(drivers.asyncio, "sleep", fake_sleep):
            drivers._semaphores.pop(drv.model, None)
            try:
                result = asyncio.run(drv.run("p", Path("."), task_id="t1"))
            except DriverError as exc:
                result = exc
        return result, clock[0], slept, ev

    def test_waits_until_the_named_reset_then_succeeds(self):
        reset = NOW + 3 * 3600
        drv = ScriptedDriver([
            DriverError("claude exited 1: Claude AI usage limit reached",
                        usage_limit=True, resets_at=reset),
            "done"])
        result, end, slept, ev = self._run(drv)
        self.assertEqual(result.text, "done")
        self.assertGreaterEqual(end, reset, "retried before the window reopened")
        self.assertLessEqual(end, reset + config.USAGE_LIMIT_MARGIN + 1)
        self.assertEqual(drv.calls, 2)
        self.assertEqual(len(ev.of("driver.usage_limit")), 1)
        self.assertTrue(ev.of("driver.usage_wait"), "a parked task must stay visible")
        self.assertFalse(ev.of("driver.error"), "a plan refusal is not a crash")

    def test_the_wait_does_not_spend_retry_attempts(self):
        # More refusals than MAX_RETRIES: a crash would give up, a plan
        # window must not.
        n = config.MAX_RETRIES + 5
        drv = ScriptedDriver([DriverError("You've hit your usage limit")] * n + ["ok"])
        result, _, _, _ = self._run(drv)
        self.assertEqual(result.text, "ok")
        self.assertEqual(drv.calls, n + 1)

    def test_unstated_reset_polls(self):
        drv = ScriptedDriver([DriverError("Claude AI usage limit reached"), "ok"])
        _, end, _, _ = self._run(drv)
        self.assertAlmostEqual(end - NOW, config.USAGE_LIMIT_POLL, delta=1)

    def test_gives_up_after_the_max_wait(self):
        with mock.patch.object(config, "USAGE_LIMIT_MAX_WAIT", 3600.0):
            drv = ScriptedDriver([DriverError("usage limit", usage_limit=True,
                                              resets_at=NOW + 7200)] * 3)
            result, end, _, _ = self._run(drv)
        self.assertIsInstance(result, DriverError)
        self.assertLessEqual(end - NOW, 3600 + 1)

    def test_siblings_wait_for_a_known_reset_without_spending_a_refusal(self):
        drivers._usage_blocked_until["fake-plan"] = NOW + 1800
        drv = ScriptedDriver(["ok"])
        result, end, _, _ = self._run(drv)
        self.assertEqual(result.text, "ok")
        self.assertEqual(drv.calls, 1)
        self.assertGreaterEqual(end, NOW + 1800)


class SwapsOffASpentPlan(unittest.TestCase):
    """A spent window moves the attempt to a free harness before it waits."""

    def setUp(self):
        drivers._usage_blocked_until.clear()
        self.addCleanup(drivers._usage_blocked_until.clear)

    ROSTER = {  # model: (harness, family, tier, roles)
        "Sol": ("codex", "openai", "hard", {"implementer", "reviewer"}),
        "Luna": ("codex", "openai", "hard", {"implementer"}),
        "Opus": ("claude", "anthropic", "hard", {"implementer", "reviewer", "planner"}),
        "Cursor-Grok-4.7": ("cursor", "cursor", "hard", {"implementer", "reviewer"}),
        "Antigravity-Gemini": ("agy", "google", "hard", {"implementer", "reviewer"}),
        "Zen-Big-Pickle": ("opencode", "zen-big_pickle", "medium", {"implementer"}),
        "GLM-5.3": ("opencode", "glm", "hard", {"implementer", "reviewer"}),
    }

    def _roster(self, **drop):
        r = {m: v for m, v in self.ROSTER.items() if m not in drop}
        return (mock.patch.object(config, "MODEL_HARNESS", {m: v[0] for m, v in r.items()}),
                mock.patch.object(config, "MODEL_FAMILY", {m: v[1] for m, v in r.items()}),
                mock.patch.object(config, "MODEL_TIER", {m: v[2] for m, v in r.items()}),
                mock.patch.object(config, "MODEL_ROLES", {m: v[3] for m, v in r.items()}),
                mock.patch.object(config, "USAGE_SWAP", True),
                mock.patch.object(drivers.time, "time", lambda: NOW))

    def _sub(self, *a, **kw):
        patches = self._roster()
        for p in patches:
            p.start()
        try:
            return drivers.usage_substitute(*a, **kw)
        finally:
            for p in reversed(patches):
                p.stop()

    def test_cursor_is_tried_before_claude_and_a_blocked_seat_is_skipped(self):
        self.assertEqual(self._sub("Sol", "codex"), "Cursor-Grok-4.7")
        drivers._usage_blocked_until["cursor"] = NOW + 1000
        self.assertEqual(self._sub("Sol", "codex"), "Antigravity-Gemini")
        drivers._usage_blocked_until["agy"] = NOW + 1000
        self.assertEqual(self._sub("Sol", "codex"), "Opus")
        drivers._usage_blocked_until["claude"] = NOW + 1000
        # Another model on the spent harness is the same plan; Zen is medium.
        self.assertEqual(self._sub("Sol", "codex"), "GLM-5.3")

    def test_only_implementation_is_swapped(self):
        """A swapped reviewer could land in the implementer's family (Rule 2)."""
        for role in ("reviewer", "pr_reviewer", "planner"):
            self.assertIsNone(self._sub("Opus", "claude", role))

    def test_the_reviewers_family_is_never_the_substitute(self):
        """Codex implements, Cursor reviews: the swap must not pick Cursor."""
        self.assertEqual(self._sub("Sol", "codex", avoid_families={"cursor"}),
                         "Antigravity-Gemini")

    def test_a_hard_task_never_drops_to_a_medium_model(self):
        """Rule 1: the tier floor holds through a swap."""
        for h in ("cursor", "agy", "claude"):
            drivers._usage_blocked_until[h] = NOW + 1000
        self.assertEqual(self._sub("Sol", "codex", avoid_families={"glm"}), None)
        # Upward is fine: a medium task may move to a hard seat.
        self.assertIn(self._sub("Zen-Big-Pickle", "fake", avoid_families={"glm"}),
                      ("Sol", "Luna"))

    def test_a_refusal_reruns_on_the_substitute_without_waiting(self):
        clock = [NOW]

        async def fake_sleep(d):
            clock[0] += d

        other = ScriptedDriver(["from-cursor"])
        other.harness = "cursor"
        # A live roster model: the lease gate looks the name up. The swap
        # event still records the substitute usage_substitute returned.
        other.model = "GLM-5.3"
        drv = ScriptedDriver([
            DriverError("usage limit", usage_limit=True, resets_at=NOW + 3 * 3600)])
        with TempLeaseDB(), capture_events() as ev, \
                mock.patch.object(config, "USAGE_SWAP", True), \
                mock.patch.object(drivers, "usage_substitute",
                                  return_value="Cursor-Grok-4.7"), \
                mock.patch.object(drivers, "driver_for", return_value=other), \
                mock.patch.object(drivers.time, "time", lambda: clock[0]), \
                mock.patch.object(drivers.asyncio, "sleep", fake_sleep):
            drivers._semaphores.pop(drv.model, None)
            result = asyncio.run(drv.run("p", Path("."), task_id="t1"))
        self.assertEqual(result.text, "from-cursor")
        self.assertEqual(result.model, "GLM-5.3",
                         "the result names the model that RAN, for the records")
        self.assertEqual(clock[0], NOW, "swapping must not park for the reset")
        self.assertEqual(len(ev.of("driver.usage_swap")), 1)
        self.assertEqual(ev.of("driver.usage_swap")[0]["to_model"],
                         "Cursor-Grok-4.7")
        self.assertEqual(drv.calls, 1)

    def test_swap_off_returns_no_substitute(self):
        with mock.patch.object(config, "USAGE_SWAP", False):
            self.assertIsNone(drivers.usage_substitute("Sol", "codex"))


class ExitClassification(unittest.TestCase):
    """_once classifies from the FULL output, not the 300-char message tail."""

    def test_reset_time_survives_a_long_transcript(self):
        drv = drivers.ClaudeCodeDriver.__new__(drivers.ClaudeCodeDriver)
        drv.model, drv.role, drv.interactive = config.ESCALATION_PATH[0], "implementer", False
        script = ("import sys; print('x' * 5000); "
                  "print('Claude AI usage limit reached|1790150400'); sys.exit(1)")
        with mock.patch.object(drv, "argv", lambda p, s: ["python3", "-c", script]), \
                capture_events():
            with self.assertRaises(DriverError) as cm:
                asyncio.run(drv._once("p", Path("."), None, "usage-x1", 1))
        self.assertTrue(cm.exception.usage_limit)
        self.assertEqual(cm.exception.resets_at, 1790150400)

    def test_a_rejected_window_event_alone_is_a_usage_limit(self):
        drv = drivers.ClaudeCodeDriver.__new__(drivers.ClaudeCodeDriver)
        drv.model, drv.role, drv.interactive = config.ESCALATION_PATH[0], "implementer", False
        ev = json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "resetsAt": 1790012345, "rateLimitType": "five_hour"}})
        script = f"import sys; print({ev!r}); print('something went wrong'); sys.exit(1)"
        with mock.patch.object(drv, "argv", lambda p, s: ["python3", "-c", script]), \
                capture_events():
            with self.assertRaises(DriverError) as cm:
                asyncio.run(drv._once("p", Path("."), None, "usage-x2", 1))
        self.assertTrue(cm.exception.usage_limit)
        self.assertEqual(cm.exception.resets_at, 1790012345)


class SubscriptionSeatsAreUncapped(unittest.TestCase):
    PROBE = ("import config, json; print(json.dumps({"
             "'claude': config.harness_limit('claude'),"
             "'codex': config.harness_limit('codex'),"
             "'claude_driver': config.driver_limit('Claude-Opus-5.5', True),"
             "'openai_driver': config.driver_limit(config.STUDIO_OPENAI_MODEL, True)}))")

    def test_default_is_the_subscription_cap(self):
        caps = json.loads(in_studio(self.PROBE))
        for k, v in caps.items():
            self.assertGreaterEqual(v, 16, f"{k} is still capped: {caps}")

    def test_operator_can_restore_a_single_seat(self):
        import os
        with mock.patch.dict(os.environ, {"ARC_SUBSCRIPTION_SESSION_CAP": "1"}):
            caps = json.loads(in_studio(self.PROBE))
        self.assertEqual(caps, {"claude": 1, "codex": 1,
                                "claude_driver": 1, "openai_driver": 1})

    def test_api_profile_is_bound_by_the_opencode_pool_not_the_model(self):
        caps = json.loads(in_studio(
            "import config, json; print(json.dumps({m: config.driver_limit(m, True)"
            " for m in ('Claude-Opus-5.5', config.STUDIO_OPENAI_MODEL)}))",
            fleet="studio-api"))
        for m, v in caps.items():
            self.assertGreaterEqual(v, config.harness_limit("opencode"), (m, v))


if __name__ == "__main__":
    unittest.main()
