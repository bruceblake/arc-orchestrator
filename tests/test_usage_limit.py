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

    def test_codex_try_again_at_wall_clock(self):
        at = drivers.usage_reset_at(
            "You've hit your usage limit. try again at 3:39 PM.", NOW)
        self.assertIsNotNone(at)
        self.assertGreater(at, NOW)
        self.assertLessEqual(at - NOW, 86400)


class ActivePlanWindows(unittest.TestCase):
    def test_a_future_reset_stays_up_after_the_swap(self):
        now = 1_000_000.0
        rows = [
            {"type": "driver.usage_limit", "harness": "claude", "model": "Claude-Opus-5.5",
             "ts": now - 100, "resets_at": now + 3600, "task": "t1"},
            {"type": "driver.usage_swap", "harness": "claude", "model": "Claude-Opus-5.5",
             "ts": now - 90, "to_model": "Cursor-Grok-4.7"},
            {"type": "driver.done", "harness": "cursor", "ts": now - 10},
        ]
        got = drivers.active_plan_windows(rows, now)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["label"], "Claude")
        self.assertEqual(got[0]["swapped_to"], "Cursor-Grok-4.7")
        self.assertEqual(got[0]["resets_at"], now + 3600)

    def test_a_later_success_on_that_harness_clears_it(self):
        now = 1_000_000.0
        rows = [
            {"type": "driver.usage_limit", "harness": "codex", "model": "GPT-6-Sol",
             "ts": now - 100, "resets_at": now + 3600},
            {"type": "driver.done", "harness": "codex", "ts": now - 10},
        ]
        self.assertEqual(drivers.active_plan_windows(rows, now), [])

    def test_an_unstated_refusal_stays_up_for_the_hold(self):
        now = 1_000_000.0
        rows = [{"type": "driver.usage_limit", "harness": "codex",
                 "model": "GPT-6-Sol", "ts": now - 60, "resets_at": None,
                 "error": "usage limit reached"}]
        got = drivers.active_plan_windows(rows, now)
        self.assertEqual(got[0]["harness"], "codex")
        self.assertIsNone(got[0]["resets_at"])
        old = [{"type": "driver.usage_limit", "harness": "codex",
                "model": "GPT-6-Sol", "ts": now - drivers._PLAN_UNKNOWN_HOLD_S - 1,
                "resets_at": None, "error": "usage limit reached"}]
        self.assertEqual(drivers.active_plan_windows(old, now), [])


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
        "Cursor-Grok-4.7": ("cursor", "cursor", "hard",
                            {"implementer", "reviewer", "pr_reviewer"}),
        "Antigravity-Gemini": ("agy", "google", "hard",
                               {"implementer", "reviewer", "pr_reviewer"}),
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
        # GLM-5.3 on ARC has no plan window: it is spent before Claude's
        # small plan (operator directive 2026-09-24).
        self.assertEqual(self._sub("Sol", "codex"), "GLM-5.3")
        self.assertEqual(self._sub("Sol", "codex", avoid_families={"glm"}), "Opus")
        drivers._usage_blocked_until["claude"] = NOW + 1000
        # Another model on the spent harness is the same plan; Zen is medium.
        self.assertIsNone(self._sub("Sol", "codex", avoid_families={"glm"}))

    def test_reviews_swap_but_not_onto_the_implementer_family(self):
        """A spent review seat moves, and never into the family that wrote the code."""
        drivers._usage_blocked_until["claude"] = NOW + 1000
        self.assertEqual(
            self._sub("Opus", "claude", "reviewer", avoid_families={"cursor"}),
            "Antigravity-Gemini")
        self.assertEqual(
            self._sub("Opus", "claude", "pr_reviewer", avoid_families={"cursor"}),
            "Antigravity-Gemini")
        self.assertNotEqual(
            self._sub("Opus", "claude", "reviewer", avoid_families={"cursor"}),
            "Cursor-Grok-4.7")

    def test_a_planner_is_not_swapped(self):
        self.assertIsNone(self._sub("Opus", "claude", "planner"))

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


class SubscriptionSeatCaps(unittest.TestCase):
    PROBE = ("import config, json; print(json.dumps({"
             "'claude': config.harness_limit('claude'),"
             "'codex': config.harness_limit('codex'),"
             "'claude_driver': config.driver_limit('Claude-Opus-5.5', True),"
             "'openai_driver': config.driver_limit(config.STUDIO_OPENAI_MODEL, True)}))")

    def test_defaults_are_the_per_seat_caps(self):
        caps = json.loads(in_studio(self.PROBE))
        self.assertEqual(caps, {"claude": 2, "codex": 4,
                                "claude_driver": 2, "openai_driver": 4})

    def test_driver_limit_env_overrides_one_family(self):
        caps = json.loads(in_studio(
            self.PROBE, ARC_DRIVER_LIMIT_ANTHROPIC="1", ARC_DRIVER_LIMIT_OPENAI="1"))
        self.assertEqual(caps["claude_driver"], 1)
        self.assertEqual(caps["openai_driver"], 1)
        self.assertEqual(caps["claude"], 2)
        self.assertEqual(caps["codex"], 4)

    def test_api_profile_is_bound_by_the_opencode_pool_not_the_model(self):
        caps = json.loads(in_studio(
            "import config, json; print(json.dumps({m: config.driver_limit(m, True)"
            " for m in ('Claude-Opus-5.5', config.STUDIO_OPENAI_MODEL)}))",
            fleet="studio-api"))
        for m, v in caps.items():
            self.assertGreaterEqual(v, config.harness_limit("opencode"), (m, v))


if __name__ == "__main__":
    unittest.main()


class SwapsOffAFullCap(unittest.TestCase):
    """A queued attempt moves to an IDLE seat of the same tier or above.

    2026-09-25, studio fleet: GLM-5.3 logged 5254 cap-waits in 24 h while
    Cursor-Grok-4.7 and Antigravity-Gemini (the same hard tier) sat free for
    ~30 seat-hours each. The capacity swap keeps the usage swap's rules."""

    ROSTER = SwapsOffASpentPlan.ROSTER

    def setUp(self):
        drivers._usage_blocked_until.clear()
        self.addCleanup(drivers._usage_blocked_until.clear)
        r = self.ROSTER
        self.patches = [
            mock.patch.object(config, "MODEL_HARNESS", {m: v[0] for m, v in r.items()}),
            mock.patch.object(config, "MODEL_FAMILY", {m: v[1] for m, v in r.items()}),
            mock.patch.object(config, "MODEL_TIER", {m: v[2] for m, v in r.items()}),
            mock.patch.object(config, "MODEL_ROLES", {m: v[3] for m, v in r.items()}),
            mock.patch.object(config, "CAP_SWAP_AFTER", 600.0),
            mock.patch.object(config, "driver_limit", lambda m, *a, **k: 2),
            mock.patch.object(config, "harness_limit", lambda h: 3),
            mock.patch.object(drivers.time, "time", lambda: NOW),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def _sub(self, model, role="implementer", usage=None, **kw):
        return drivers.cap_substitute(model, self.ROSTER[model][0], role,
                                      usage=usage or {}, **kw)

    def test_the_first_idle_same_tier_seat_is_chosen(self):
        self.assertEqual(self._sub("GLM-5.3"), "Cursor-Grok-4.7")

    def test_a_full_seat_or_a_full_harness_pool_is_skipped(self):
        usage = {"Cursor-Grok-4.7": 2}                 # model cap reached
        self.assertEqual(self._sub("GLM-5.3", usage=usage), "Antigravity-Gemini")
        usage["harness:agy"] = 3                       # its harness pool is full
        self.assertNotIn(self._sub("GLM-5.3", usage=usage),
                         ("Cursor-Grok-4.7", "Antigravity-Gemini"))

    def test_rule_1_a_hard_attempt_never_moves_to_a_medium_seat(self):
        full = {m: 2 for m, v in self.ROSTER.items() if v[2] == "hard"}
        self.assertIsNone(self._sub("GLM-5.3", usage=full),
                          "only the medium Zen seat is free; a hard task must wait")
        # Upward is legal.
        self.assertEqual(self._sub("Zen-Big-Pickle"), "Cursor-Grok-4.7")

    def test_rule_2_the_avoided_family_is_never_the_substitute(self):
        self.assertEqual(self._sub("GLM-5.3", avoid_families={"cursor"}),
                         "Antigravity-Gemini")
        self.assertEqual(self._sub("Opus", "reviewer",
                                   avoid_families={"cursor", "google", "glm"}), "Sol")

    def test_planner_and_disabled_swap_stay_put(self):
        self.assertIsNone(self._sub("Opus", "planner"))
        with mock.patch.object(config, "CAP_SWAP_AFTER", 0.0):
            self.assertIsNone(self._sub("GLM-5.3"))

    def test_a_spent_plan_is_not_idle(self):
        drivers._usage_blocked_until["cursor"] = NOW + 1000
        self.assertEqual(self._sub("GLM-5.3"), "Antigravity-Gemini")


class CapSwapInTheLeaseWait(unittest.TestCase):
    def test_the_wait_raises_capswap_only_after_the_threshold(self):
        clock = [1000.0]
        ctx = drivers._cap_swap_ctx.set(("GLM-5.3", "opencode", "implementer",
                                         frozenset(), frozenset({"deepseek"})))
        self.addCleanup(drivers._cap_swap_ctx.reset, ctx)
        with mock.patch.object(config, "CAP_SWAP_AFTER", 600.0), \
                mock.patch.object(drivers, "cap_substitute",
                                  return_value="Cursor-Grok-4.7") as sub, \
                mock.patch.object(drivers.time, "monotonic", lambda: clock[0]):
            drivers._maybe_cap_swap("GLM-5.3", 1000.0, 0)      # 0 s waited
            sub.assert_not_called()
            clock[0] += 601
            drivers._maybe_cap_swap("GLM-5.3", 1000.0, 1)      # not a check tick
            sub.assert_not_called()
            with self.assertRaises(drivers.CapSwap) as cm:
                drivers._maybe_cap_swap("GLM-5.3", 1000.0, 3)
        self.assertEqual(cm.exception.to_model, "Cursor-Grok-4.7")
        self.assertEqual(sub.call_args.kwargs["avoid_families"], frozenset({"deepseek"}))

    def test_no_context_means_no_swap(self):
        with mock.patch.object(drivers, "cap_substitute") as sub:
            drivers._maybe_cap_swap("GLM-5.3", 0.0, 3)
        sub.assert_not_called()

    def test_run_hands_the_queued_attempt_to_the_substitute(self):
        other = ScriptedDriver(["from-cursor"])
        other.harness = "cursor"
        other.model = "GLM-5.3"
        drv = ScriptedDriver(["never"])

        async def full(*a, **k):
            raise drivers.CapSwap("Cursor-Grok-4.7", 700.0, drv.model)

        with TempLeaseDB(), capture_events() as ev, \
                mock.patch.object(drv, "_guarded_once", full), \
                mock.patch.object(drivers, "driver_for", return_value=other) as dfor:
            result = asyncio.run(drv.run("p", Path("."), task_id="t1",
                                         avoid_families={"glm"}))
        self.assertEqual(result.text, "from-cursor")
        self.assertEqual(dfor.call_args.args[:2], ("Cursor-Grok-4.7", "implementer"))
        swaps = ev.of("driver.cap_swap")
        self.assertEqual(len(swaps), 1)
        self.assertEqual(swaps[0]["to_model"], "Cursor-Grok-4.7")
        self.assertEqual(drv.calls, 0, "no slot was held, so nothing ran here")
        self.assertIsNone(drivers._cap_swap_ctx.get(), "context must be reset")
