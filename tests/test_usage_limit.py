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
import os
import unittest
from pathlib import Path
from unittest import mock

from helpers import capture_events  # noqa: F401  (sys.path)
from helpers import second_review_family  # noqa: E402,F401
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

    def test_codex_absolute_date_is_america_new_york(self):
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
        expected = datetime.datetime(2026, 9, 29, 16, 17, tzinfo=tz).timestamp()
        now = datetime.datetime(2026, 9, 28, 12, 0, tzinfo=tz).timestamp()
        self.assertLess(now, expected)
        self.assertEqual(drivers.usage_reset_at(
            "try again at Sep 29th, 2026 4:17 PM", now), expected)
        self.assertEqual(drivers.usage_reset_at(
            "try again at September 29, 2026 4:17 PM", now), expected)
        chicago = ZoneInfo("America/Chicago")
        self.assertEqual(drivers.usage_reset_at(
            "try again at Sep 29th, 2026 4:17 PM America/Chicago", now),
            datetime.datetime(2026, 9, 29, 16, 17, tzinfo=chicago).timestamp())
        earlier = ("cache resets May 1st, 2020 12:00 AM. "
                   "See America/Los_Angeles docs. "
                   "try again at Sep 29th, 2026 4:17 PM")
        self.assertEqual(drivers.usage_reset_at(earlier, now), expected)


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


class RefreshUsageBlocks(unittest.TestCase):
    def test_a_codex_dated_refusal_blocks_until_that_instant(self):
        import tempfile
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
        now = datetime.datetime(2026, 9, 28, 12, 0, tzinfo=tz).timestamp()
        drivers._usage_blocked_until.clear()
        self.addCleanup(drivers._usage_blocked_until.clear)
        line = json.dumps({
            "type": "driver.usage_limit", "harness": "codex",
            "model": "GPT-6-Sol", "ts": now - 60,
            "error": "You've hit your usage limit. try again at Sep 29th, 2026 4:17 PM",
        })
        noise = json.dumps({"type": "driver.start", "task": "not-a-plan-window"})
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "events.jsonl"
            path.write_text(noise + "\n" + line + "\n", encoding="utf-8")
            with mock.patch.object(config, "EVENTS_LOG", str(path)), \
                    mock.patch.object(drivers.time, "time", lambda: now):
                first = drivers.refresh_usage_blocks()
                again = drivers.refresh_usage_blocks()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["harness"], "codex")
        self.assertGreater(drivers._usage_blocked_until["codex"], now)
        self.assertEqual(again[0]["resets_at"], first[0]["resets_at"])
        self.assertEqual(drivers._usage_blocked_until["codex"], first[0]["resets_at"])

    def test_a_later_done_clears_a_block_this_process_still_held(self):
        import tempfile
        now = 1_000_000.0
        drivers._usage_blocked_until.clear()
        self.addCleanup(drivers._usage_blocked_until.clear)
        drivers._usage_blocked_until["codex"] = now + 5000
        rows = [
            {"type": "driver.usage_limit", "harness": "codex", "model": "GPT-6-Sol",
             "ts": now - 100, "resets_at": now + 3600},
            {"type": "driver.done", "harness": "codex", "ts": now - 10},
        ]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "events.jsonl"
            path.write_text("".join(json.dumps(r) + "\n" for r in rows),
                            encoding="utf-8")
            with mock.patch.object(config, "EVENTS_LOG", str(path)), \
                    mock.patch.object(drivers.time, "time", lambda: now):
                drivers.refresh_usage_blocks()
        self.assertNotIn("codex", drivers._usage_blocked_until)


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
        # Claude and GPT stay off ordinary implementation, so nothing is left.
        self.assertIsNone(self._sub("Sol", "codex", avoid_families={"glm"}))
        drivers._usage_blocked_until["claude"] = NOW + 1000
        # Another model on the spent harness is the same plan; Zen is medium.
        self.assertIsNone(self._sub("Sol", "codex", avoid_families={"glm"}))

    def test_reviews_swap_but_not_onto_the_implementer_family(self):
        """A spent review seat moves, and never into the family that wrote the code."""
        drivers._usage_blocked_until["claude"] = NOW + 1000
        # A review moves to GPT-6 before the implementation seats.
        self.assertEqual(
            self._sub("Opus", "claude", "reviewer", avoid_families={"cursor"}),
            "Sol")
        # Sol has no pr_reviewer role in this roster, so that swap stays
        # on the next implementation seat.
        self.assertEqual(
            self._sub("Opus", "claude", "pr_reviewer", avoid_families={"cursor"}),
            "Antigravity-Gemini")
        self.assertNotEqual(
            self._sub("Opus", "claude", "reviewer", avoid_families={"cursor"}),
            "Cursor-Grok-4.7")

    def test_a_planner_is_not_swapped(self):
        self.assertIsNone(self._sub("Opus", "claude", "planner"))

    def test_claude_implementation_waits_instead_of_moving(self):
        self.assertIsNone(self._sub("Opus", "claude", "implementer"))

    def test_the_reviewers_family_is_never_the_substitute(self):
        """Codex implements, Cursor reviews: the swap must not pick Cursor."""
        self.assertEqual(self._sub("Sol", "codex", avoid_families={"cursor"}),
                         "Antigravity-Gemini")

    def test_a_hard_task_never_drops_to_a_medium_model(self):
        """Rule 1: the tier floor holds through a swap."""
        for h in ("cursor", "agy", "claude"):
            drivers._usage_blocked_until[h] = NOW + 1000
        self.assertEqual(self._sub("Sol", "codex", avoid_families={"glm"}), None)
        # Upward is fine once Cursor is open. GPT is not that seat.
        del drivers._usage_blocked_until["cursor"]
        self.assertEqual(
            self._sub("Zen-Big-Pickle", "fake", avoid_families={"glm"}),
            "Cursor-Grok-4.7")

    def test_a_refusal_reruns_on_the_substitute_without_waiting(self):
        clock = [NOW]

        async def fake_sleep(d):
            clock[0] += d

        other = ScriptedDriver(["from-cursor"])
        other.harness = "cursor"
        # A live roster model: the lease gate looks the name up. The swap
        # event still records the substitute usage_substitute returned.
        other.model = config.ESCALATION_PATH[0]
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
        self.assertEqual(result.model, config.ESCALATION_PATH[0],
                         "the result names the model that RAN, for the records")
        self.assertEqual(clock[0], NOW, "swapping must not park for the reset")
        self.assertEqual(len(ev.of("driver.usage_swap")), 1)
        self.assertEqual(ev.of("driver.usage_swap")[0]["to_model"],
                         "Cursor-Grok-4.7")
        self.assertEqual(drv.calls, 1)

    def test_the_substitute_reviewer_gets_the_evidence_images(self):
        """A swapped review must still see the screenshots the gate attached."""
        other = ScriptedDriver(['{"pass": true}'])
        other.harness = "cursor"
        other.model = config.ESCALATION_PATH[0]
        drv = ScriptedDriver([
            DriverError("usage limit", usage_limit=True, resets_at=NOW + 3 * 3600)])
        drv.images = ("/ev/index-desktop-light.png", "/ev/usage-phone-dark.png")
        with TempLeaseDB(), capture_events(), \
                mock.patch.object(config, "USAGE_SWAP", True), \
                mock.patch.object(drivers, "usage_substitute",
                                  return_value="Cursor-Grok-4.7"), \
                mock.patch.object(drivers, "driver_for", return_value=other), \
                mock.patch.object(drivers.time, "time", lambda: NOW):
            drivers._semaphores.pop(drv.model, None)
            asyncio.run(drv.run("p", Path("."), task_id="t1"))
        self.assertEqual(other.images, drv.images)

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
             "'claude_driver': config.driver_limit('Claude-Sonnet-5.5', True),"
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
            " for m in ('Claude-Sonnet-5.5', config.STUDIO_OPENAI_MODEL)}))",
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
    def _bind(self):
        ctx = drivers._cap_swap_ctx.set(("GLM-5.3", "opencode", "implementer",
                                         frozenset(), frozenset({"deepseek"})))
        self.addCleanup(drivers._cap_swap_ctx.reset, ctx)

    def test_the_first_failed_acquire_raises_capswap(self):
        self._bind()
        clock = [1000.0]
        with mock.patch.object(config, "CAP_SWAP_AFTER", 600.0), \
                mock.patch.object(drivers, "cap_substitute",
                                  return_value="Cursor-Grok-4.7") as sub, \
                mock.patch.object(drivers.time, "monotonic", lambda: clock[0]):
            drivers._maybe_cap_swap("GLM-5.3", 1000.0, 1)      # not a check tick
            sub.assert_not_called()
            with self.assertRaises(drivers.CapSwap) as cm:
                drivers._maybe_cap_swap("GLM-5.3", 1000.0, 0)  # 0 s waited
        self.assertEqual(cm.exception.to_model, "Cursor-Grok-4.7")
        self.assertEqual(cm.exception.waited_s, 0)
        self.assertEqual(sub.call_args.kwargs["avoid_families"], frozenset({"deepseek"}))

    def test_cap_swap_after_zero_never_swaps(self):
        self._bind()
        with mock.patch.object(config, "CAP_SWAP_AFTER", 0.0), \
                mock.patch.object(drivers, "cap_substitute",
                                  return_value="Cursor-Grok-4.7") as sub:
            drivers._maybe_cap_swap("GLM-5.3", 1000.0, 0)
            drivers._maybe_cap_swap("GLM-5.3", 0.0, 3)
        sub.assert_not_called()

    def test_no_context_means_no_swap(self):
        with mock.patch.object(drivers, "cap_substitute") as sub:
            drivers._maybe_cap_swap("GLM-5.3", 0.0, 3)
        sub.assert_not_called()

    def test_run_hands_the_queued_attempt_to_the_substitute(self):
        other = ScriptedDriver(["from-cursor"])
        other.harness = "cursor"
        other.model = config.ESCALATION_PATH[0]
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


class GlmDoesNotRunAlone(unittest.TestCase):
    """A FREE GLM-5.3 slot is kept only while another model is in flight.

    Operator decision 2026-09-28: GLM-5.3 is the fleet's slowest model and the
    fleet keeps it as PARALLEL implementation capacity — it must not be the
    single agent a task waits on while Cursor-Grok-4.7, Antigravity-Gemini or
    DeepSeek-V4.1-Flash-thinking-max are free. This is NOT the cap swap:
    nothing is full here and nothing waited, so `CAP_SWAP_AFTER` is irrelevant
    (it is left at its default) and the event is `driver.glm_yield`.
    """

    # The real roster names, patched: the rule keys off model + harness.
    GLM = "GLM-5.3"
    DS = "DeepSeek-V4.1-Flash-thinking-max"
    CURSOR = "Cursor-Grok-4.7"
    AGY = "Antigravity-Gemini"
    ROSTER = {
        GLM: ("opencode", "glm", "medium", 4, {"implementer"}),
        DS: ("reasonix", "deepseek", "hard", 10, {"implementer", "planner"}),
        CURSOR: ("cursor", "cursor", "hard", 3, {"implementer", "reviewer"}),
        AGY: ("agy", "google", "hard", 3, {"implementer", "reviewer"}),
    }

    def setUp(self):
        self.patches = [
            mock.patch.object(config, "MODEL_HARNESS",
                              {m: v[0] for m, v in self.ROSTER.items()}),
            mock.patch.object(config, "MODEL_FAMILY",
                              {m: v[1] for m, v in self.ROSTER.items()}),
            mock.patch.object(config, "MODEL_TIER",
                              {m: v[2] for m, v in self.ROSTER.items()}),
            mock.patch.object(config, "MODEL_ROLES",
                              {m: v[4] for m, v in self.ROSTER.items()}),
            # Both ceilings come from the lease table's view, so they must
            # cover the patched roster rather than the real one.
            mock.patch.object(config, "driver_limit",
                              lambda m, *a, **k: self.ROSTER[m][3]),
            mock.patch.object(config, "harness_limit",
                              lambda h: 3),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        ctx = drivers._cap_swap_ctx.set(
            (self.GLM, "opencode", "implementer", frozenset(), frozenset()))
        self.addCleanup(drivers._cap_swap_ctx.reset, ctx)

    def _emit_ctx(self, role="implementer"):
        return {"harness": "opencode", "role": role, "attempt": 1, "pid": 1}

    def _check(self, usage, glm_leased=False):
        """Run the check with the lease table reporting `usage`; return the
        raised GlmYield or None.

        `glm_leased` acquires the GLM lease FIRST, which is the state
        `_lease_wait_loop` is in when it calls the check — it has just taken
        the free slot. Without it the release assertion is vacuous: releasing
        a lease nobody held leaves the count at 0 whether or not
        `_lease_release` is called at all.
        """
        with TempLeaseDB() as store, capture_events() as ev:
            if glm_leased:
                store.acquire_driver_lease(self.GLM, os.getpid(), "t1", 99, 300)
            for model, n in usage.items():
                for _ in range(n):
                    store.acquire_driver_lease(model, os.getpid(), f"t-{model}",
                                               99, 300)
            try:
                drivers._glm_yield_alone(self.GLM, "t1", self._emit_ctx())
            except drivers.GlmYield as y:
                out = (y, ev)
            else:
                out = (None, ev)
            # Read the counts INSIDE the temp store. Capturing them after the
            # `with` exits always reported 0, which made every
            # "the lease was released" assertion vacuous — it passed whether
            # or not `_lease_release` ran.
            self.held = store.lease_usage().get(self.GLM, 0)
            self.mine = sum(1 for r in store.driver_lease_rows()
                            if r.get("model") == self.GLM
                            and r.get("task") == "t1")
            return out

    def _held(self):
        return self.held

    def _my_lease_held(self):
        """Whether THIS attempt's own lease (task t1) survived."""
        return self.mine

    def test_yields_to_cursor_when_no_other_model_is_in_flight(self):
        # glm_leased=True is the real state: the check runs in
        # _lease_wait_loop's success branch, holding the slot it just took.
        # Without the lease the release assertion below would pass even if
        # `_lease_release` were deleted.
        y, ev = self._check({}, glm_leased=True)
        self.assertIsNotNone(y, "a lone GLM attempt must yield")
        self.assertEqual(y.to_model, self.CURSOR)
        self.assertEqual(y.reason, "alone")
        events = ev.of("driver.glm_yield")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["reason"], "alone")
        self.assertEqual(events[0]["to_harness"], "cursor")
        self.assertNotIn("cap_wait", [t for t, _ in ev.seen],
                         "nothing was full; this is not a cap warning")
        # The lease it just took is released, not held while the attempt moves.
        # (`glm_leased=True` above is what makes this non-vacuous.)
        self.assertEqual(self._held(), 0, "the GLM lease must be released")
        self.assertEqual(self._my_lease_held(), 0,
                         "the lease this attempt took must be released")

    def test_the_lease_loop_hook_actually_fires(self):
        """SPEC ITEM 3, wired: `_lease_wait_loop` must call the check.

        Every other test here calls `_glm_yield_alone` DIRECTLY, so deleting
        the call from `_lease_wait_loop` (drivers.py:865) left the whole suite
        green — the hook was untested. This drives the real loop with an empty
        lease table, so the free GLM slot is taken by the loop itself and the
        yield must come out of it.
        """
        with TempLeaseDB() as store:
            ctx = drivers._cap_swap_ctx.set(
                (self.GLM, "opencode", "implementer", frozenset(),
                 frozenset({"deepseek"})))
            self.addCleanup(drivers._cap_swap_ctx.reset, ctx)
            with capture_events() as ev:
                with self.assertRaises(drivers.GlmYield) as cm:
                    # deadline far away: the yield must come from the free-slot
                    # branch, not from a wait timeout.
                    asyncio.run(drivers._lease_wait_loop(
                        self.GLM, "t1", self._emit_ctx(), None, self.GLM,
                        1e18, None))
            self.assertEqual(cm.exception.to_model, self.CURSOR)
            self.assertEqual(cm.exception.reason, "alone")
            self.assertEqual(ev.of("driver.glm_yield")[0]["reason"], "alone")
            # The loop took the slot, so the release has to have happened here.
            self.assertEqual(store.lease_usage().get(self.GLM, 0), 0,
                             "the loop's own lease must be released")

    def test_the_capswap_handler_moves_the_attempt_and_claims_no_full_cap(self):
        """SPEC ITEM 3, handler+log: Driver.run must move the attempt.

        Modelled on `CapSwapInTheLeaseWait.test_run_hands_the_queued_attempt
        _to_the_substitute`, but with `GlmYield` and `reason == "alone"`: the
        driver must hand the attempt to the substitute, record the reason on
        the event AND on the returned result (which `code_tasks` words its
        issue comment from), and must NOT describe this as a full cap — the
        log line and the board handoff are what a human reads.
        """
        other = ScriptedDriver(["from-cursor"])
        other.harness = "cursor"
        other.model = self.CURSOR
        drv = ScriptedDriver(["never"])
        posts = []

        async def yield_now(*a, **k):
            raise drivers.GlmYield(self.CURSOR, self.GLM)

        with TempLeaseDB(), capture_events() as ev, \
                mock.patch.object(drv, "_guarded_once", yield_now), \
                mock.patch.object(drivers, "driver_for", return_value=other), \
                mock.patch.object(drivers, "post_handoff",
                                  lambda *a, **k: posts.append(a[4])), \
                mock.patch.object(drivers.log, "warning") as warn:
            result = asyncio.run(drv.run("p", Path("."), task_id="t1",
                                         avoid_families={"glm"}))
        self.assertEqual(result.text, "from-cursor", "the attempt moved")
        self.assertEqual(getattr(result, "swap_reason", None), "alone",
                         "the result must carry the reason for code_tasks")
        swaps = ev.of("driver.cap_swap")
        self.assertEqual(len(swaps), 1)
        self.assertEqual(swaps[0]["reason"], "alone")
        self.assertEqual(swaps[0]["to_model"], self.CURSOR)
        # NOT the "no slot" warning: nothing was full and nothing waited.
        said = " ".join(posts) + " " + " ".join(
            str(c) for c in warn.call_args_list)
        self.assertNotIn("concurrency cap", said)
        self.assertNotIn("no slot", said)
        self.assertIn("only agent working", said)

    def test_a_deepseek_lease_keeps_glm(self):
        y, _ev = self._check({self.DS: 1})
        self.assertIsNone(y, "another model is working: GLM is parallel capacity")

    def test_another_glm_attempt_is_not_company(self):
        """Two GLM tasks are still GLM working alone.

        The docstring has to be ASSERTED, not implied: the check counts
        OTHER MODELS, so a live GLM lease is not company and the attempt
        still yields to a faster seat — and the lease it took is released
        rather than held while the attempt moves.
        """
        y, ev = self._check({self.GLM: 1}, glm_leased=True)
        self.assertIsNotNone(y, "another GLM lease is not 'another model'")
        self.assertEqual(y.to_model, self.CURSOR)
        self.assertEqual(ev.of("driver.glm_yield")[0]["reason"], "alone")
        # Our lease is gone, and only ours: a sibling GLM lease from another
        # task may legitimately remain, since this check releases the slot it
        # took and nothing else.
        self.assertEqual(self._my_lease_held(), 0,
                         "the lease this attempt took must be released")
        self.assertEqual(self._held(), 1, "the sibling GLM lease is untouched")

    def test_the_planned_reviewer_avoid_set_does_not_hide_the_seat(self):
        """REGRESSION: `Driver.run` sets `avoid_families` to the planned
        reviewer's family, which is `deepseek` on EVERY GLM-5.3 task. Passing
        that to the seat lookup hid a seat that family reviews for, so a lone
        GLM attempt kept its lease while the seat sat idle. The review node
        re-pairs from `wrote_the_code`, so the yield must not apply it.

        The seat has to be REVIEWABLE for this to be a yield at all — a
        candidate nobody can review is skipped by design (the local-profile
        bug the review of 2026-09-28 found), so a second review family is
        patched in to make DeepSeek a legal destination.
        """
        ctx = drivers._cap_swap_ctx.set(
            (self.GLM, "opencode", "implementer", frozenset(),
             frozenset({"deepseek"})))
        self.addCleanup(drivers._cap_swap_ctx.reset, ctx)
        roster = {self.GLM: "opencode", self.DS: "reasonix"}
        # DeepSeek keeps its own family, so the avoid set is the only thing
        # standing between the lookup and this seat.
        with mock.patch.object(config, "MODEL_HARNESS", roster), \
                second_review_family():
            y, _ev = self._check({})
        self.assertIsNotNone(y, "the avoid set must not swallow the only seat")
        self.assertEqual(y.to_model, self.DS)

    def test_an_unreviewable_seat_is_not_a_yield(self):
        """A seat with NO possible reviewer is not a destination.

        `cross_family_reviewer` returns None only when there is no review
        family AT ALL: since the one-family fallback ("a missing review is
        worse than a same-family one") a lone family now reviews its own
        work, so the local profile — one hefty family, deepseek — no longer
        produces None and DeepSeek work IS reviewable there. What remains is
        the genuinely unreviewable configuration: zero review families, where
        the review node's `_reviewer_for` raises its ValueError and burns
        MAX_REVIEW_CRASHES retries on a task that dies UNREVIEWED. Yielding
        into it would move a healthy GLM attempt onto a seat that cannot be
        reviewed, so the seat is skipped and GLM keeps its slot — "a task
        with nowhere else to go still runs".

        setUp patches a roster whose fast seats a second family can review,
        which is what makes the OTHER tests' yields legal; this one pins the
        no-reviewer configuration.
        """
        real_local = {self.GLM: "opencode", self.DS: "reasonix"}
        with mock.patch.object(config, "MODEL_HARNESS", real_local), \
                mock.patch.object(config, "REVIEW_FAMILIES", {}), \
                mock.patch.object(config, "PR_REVIEW_FAMILIES", set()):
            # The premise: with no review family, nothing can be reviewed.
            self.assertIsNone(config.cross_family_reviewer(self.DS))
            y, _ev = self._check({}, glm_leased=True)
            self.assertIsNone(y, "an unreviewable seat is not a destination")
            # Kept, not released: the attempt stays on GLM and runs.
            self.assertEqual(self._held(), 1, "GLM keeps its slot and runs")

    def test_the_capacity_hatch_restores_the_unreviewable_yield(self):
        """With `ARC_ALLOW_SAME_FAMILY_REVIEW` the whole point is that
        same-family review is allowed, so the filter above must stand down —
        otherwise the documented hatch cannot rescue a one-family fleet."""
        # Same pin as the test above: no review family at all is the only
        # configuration `cross_family_reviewer` now answers None for.
        real_local = {self.GLM: "opencode", self.DS: "reasonix"}
        with mock.patch.object(config, "MODEL_HARNESS", real_local), \
                mock.patch.object(config, "REVIEW_FAMILIES", {}), \
                mock.patch.object(config, "PR_REVIEW_FAMILIES", set()), \
                mock.patch.object(config, "ALLOW_SAME_FAMILY_REVIEW", True):
            self.assertIsNone(config.cross_family_reviewer(self.DS))
            y, _ev = self._check({})
        self.assertIsNotNone(y, "the hatch allows the otherwise-unreviewable seat")
        self.assertEqual(y.to_model, self.DS)

    def test_cap_swap_after_zero_does_not_disable_the_yield(self):
        """REGRESSION: `cap_substitute` returns None when CAP_SWAP_AFTER <= 0,
        and an operator setting that to 0 must not re-enable lone-GLM work.
        This path runs when nothing was full, so the kill switch is the cap
        swap's and only the cap swap's."""
        with mock.patch.object(config, "CAP_SWAP_AFTER", 0.0):
            y, _ev = self._check({})
        self.assertIsNotNone(y, "ARC_CAP_SWAP_AFTER=0 must not keep GLM alone")
        self.assertEqual(y.to_model, self.CURSOR)

    def test_a_non_fast_candidate_does_not_abandon_the_search(self):
        """REGRESSION: `cap_substitute` returned the FIRST idle candidate.
        On studio-api that is Claude-Opus-5.5 (opencode, same swap rank as
        reasonix, earlier name), and the old `to_harness not in
        _FAST_YIELD_HARNESSES` check then KEPT GLM even though DeepSeek was
        free. A non-fast candidate must be skipped, not treated as the end of
        the search."""
        zen = "Zen-First-Candidate"
        with mock.patch.dict(config.MODEL_HARNESS, {zen: "opencode"}), \
                mock.patch.dict(config.MODEL_ROLES, {zen: {"implementer"}}), \
                mock.patch.dict(config.MODEL_FAMILY, {zen: "zen"}), \
                mock.patch.dict(config.MODEL_TIER, {zen: "hard"}):
            y, _ev = self._check({})
        self.assertIsNotNone(y, "an opencode seat ahead of the fast ones "
                                "must not abandon the yield")
        self.assertIn(config.MODEL_HARNESS[y.to_model], ("cursor", "agy",
                                                         "reasonix"))

    def test_no_faster_seat_free_means_glm_runs(self):
        # A faster seat that is FULL is also a seat that is WORKING — its lease
        # is the "somebody else is in flight" signal, so GLM keeps its slot and
        # runs. (A full seat with no lease at all would be the backend holding
        # a slot we do not own; that is the cap swap's problem, not this one.)
        for usage in ({self.CURSOR: 3}, {self.AGY: 3}, {self.DS: 1},
                      {self.CURSOR: 3, self.AGY: 3}):
            with self.subTest(usage=usage):
                y, _ev = self._check(usage)
                self.assertIsNone(y, "a task with nowhere else to go still runs")

    def test_no_faster_seat_exists_means_glm_runs(self):
        # "Absent": no cursor / agy / reasonix row at all, so there is nothing
        # to yield to and a lone GLM attempt runs rather than refusing work.
        only_glm = {self.GLM: ("opencode", "glm", "medium", 4,
                               {"implementer"})}
        with mock.patch.object(config, "MODEL_HARNESS",
                               {self.GLM: "opencode"}), \
                mock.patch.object(config, "MODEL_ROLES",
                                  {self.GLM: {"implementer"}}), \
                mock.patch.object(config, "MODEL_TIER", {self.GLM: "medium"}), \
                mock.patch.object(config, "MODEL_FAMILY", {self.GLM: "glm"}), \
                mock.patch.object(config, "driver_limit",
                                  lambda m, *a, **k: only_glm[m][3]):
            y, ev = self._check({})
        self.assertIsNone(y, "nothing faster exists: GLM runs")
        self.assertEqual(ev.of("driver.glm_yield"), [])

    def test_a_review_attempt_never_yields(self):
        with TempLeaseDB(), capture_events():
            try:
                drivers._glm_yield_alone(self.GLM, "t1",
                                         self._emit_ctx(role="reviewer"))
                raised = None
            except drivers.GlmYield as y:
                raised = y
        self.assertIsNone(raised, "review never reaches the yield branch")

    def test_a_claude_or_codex_seat_is_not_faster(self):
        # Only cursor / agy / reasonix count. A free Codex seat must not take
        # a GLM attempt: `_swap_candidates` keeps ordinary implementation off
        # Claude and Codex for a reason.
        roster = {**self.ROSTER,
                  "GPT-6-Sol": ("codex", "openai", "hard", 4, {"implementer"})}
        usage = {m: v[0] for m, v in roster.items()}
        with mock.patch.object(config, "MODEL_HARNESS", usage):
            y, _ev = self._check({})
        self.assertIn(y.to_model, (self.CURSOR, self.AGY),
                      "the cursor/agy seat is chosen, never codex")
        self.assertNotEqual(config.MODEL_HARNESS[y.to_model], "codex")
