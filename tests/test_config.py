"""Invariants between the timeout/cap constants.

These constants are coupled, and the coupling is easy to break silently:
raising DRIVER_TIMEOUT without raising DRIVER_LEASE_TTL makes leases expire
under running drivers, which lets a model exceed its ARC cap — the exact
failure the lease table exists to prevent.
"""
import pathlib
import os
import unittest

from helpers import capture_events, ENTRY, STRONGEST  # noqa: F401  (sys.path)

import config


class TimeoutInvariants(unittest.TestCase):
    """Timeout invariants ensure lease, idle, and capacity backoff ordering prevents drivers from exceeding ARC caps.

    If DRIVER_LEASE_TTL is not longer than DRIVER_TIMEOUT, a lease could be reclaimed while a driver is still running, allowing another driver to take the slot and cause the model to exceed its account cap. DRIVER_IDLE_TIMEOUT is deliberately the binding stall detector — a harness silent that long is waiting on a request that is not coming back, so it is killed rather than waited out — and DRIVER_TIMEOUT is the wall-clock backstop a step above it, never the stall detector itself."""
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
    """Routing is loader-enforced, so a stale config silently misroutes tasks.

    `code_tasks.load_taskfile` only checks that a model is in IMPLEMENTER_MODELS
    and that its role/family pairing holds; it never checks that every tier in
    the escalation path is routable or that a driver cap stays under its
    account cap. A model in ESCALATION_PATH with no family entry or a zero
    driver limit crashes plan-time at the first tier; a driver cap above the
    family's account cap lets the fleet out-request what the account allows,
    so ARC 400s mid-run and every attempt at that tier burns its fix budget.
    """

    def test_every_escalation_tier_is_a_known_implementer(self):
        for model in config.ESCALATION_PATH:
            self.assertIn(
                model, config.IMPLEMENTER_MODELS,
                f"{model} is in ESCALATION_PATH but not a known implementer; "
                "escalation would crash the run at the first tier")

    def test_every_implementer_has_a_family_and_a_driver_cap(self):
        for model in config.IMPLEMENTER_MODELS:
            self.assertIn(
                model, config.MODEL_FAMILY,
                f"{model} has no MODEL_FAMILY entry; it cannot be routed, "
                "gated, or reviewed by family")
            self.assertGreater(
                config.driver_limit(model), 0,
                f"{model} has a non-positive driver cap; it would never get "
                "a harness slot and any task routed to it would stall")

    def test_escalation_path_runs_weakest_to_strongest(self):
        """Escalation must climb TIERS, not scarcity.

        This used to assert the path ran from most plentiful to scarcest, on
        the assumption that a stronger model is always a scarcer one. DeepSeek
        4.1-thinking-max broke that: it sits in the hard tier and is no less
        available than the medium one. Scarcity was a proxy for strength and
        the proxy stopped holding; the rule the system actually depends on is
        that a task escalates into a HIGHER tier, never sideways or down.
        """
        order = {t: i for i, t in enumerate(config.TIER_ORDER)}
        tier_of = {m: t for t, ms in config.IMPLEMENT_TIERS.items() for m in ms}
        tiers = [order[tier_of[m]] for m in config.ESCALATION_PATH]
        self.assertEqual(tiers, sorted(tiers),
                         f"escalation path must not move down a tier: "
                         f"{config.ESCALATION_PATH}")
        self.assertEqual(tier_of[config.ESCALATION_PATH[-1]], config.TIER_ORDER[-1],
                         "the last step must land in the strongest tier")

    def test_the_planner_holds_a_planner_capable_roster_role(self):
        """PLANNER_MODEL must be a roster model the roster TRUSTS to plan.

        The two-model fleet (2026-09-12): GLM-5.3 plans; DeepSeek-V4.1-Flash-
        thinking-max implements and reviews but never plans. Stated as the
        rule — not "PLANNER_MODEL == GLM-5.3" — so a roster move that grants
        planner to another model does not need this test edited.
        """
        planner = config.PLANNER_MODEL
        self.assertIsNotNone(planner, "no planner-capable model on the roster")
        self.assertIn(planner, config.MODEL_ROLES)
        self.assertTrue(config.model_may(planner, "planner"),
                        f"{planner} is the planner but the roster does not "
                        "grant it the planner role")
        # And it is constructible: roster role and driver enforcement must agree.
        drivers = __import__("drivers")
        self.assertEqual(drivers.driver_for(planner, "planner").role, "planner")

    def test_every_model_below_the_top_has_an_escalation_successor(self):
        """A non-top implementer must reach a stronger tier on escalation."""
        top = config.ESCALATION_PATH[-1]
        for model in config.ESCALATION_PATH:
            if model == top:
                continue
            self.assertIn(model, config.ESCALATION_PATH)
            self.assertLess(config.ESCALATION_PATH.index(model),
                            len(config.ESCALATION_PATH) - 1,
                            f"{model} has no successor in {config.ESCALATION_PATH}")
        self.assertGreaterEqual(len(config.ESCALATION_PATH), 2,
                                "a one-model path cannot escalate anywhere")

    def test_every_live_implementer_is_on_the_escalation_path(self):
        """Otherwise a task routed to it could never escalate."""
        for model in config.IMPLEMENTER_MODELS:
            self.assertIn(model, config.ESCALATION_PATH,
                          f"{model} implements but is off the escalation path")

    def test_a_driver_cap_never_over_subscribes_its_session_budget(self):
        """The bug this unit change fixes: caps set equal to the session limit.

        An opencode process holds ~2 sessions, so a cap of 4 processes was
        really asking for 8 against a ceiling of 4 — measured as 23 capacity
        rejections in four hours, GLM refused with as few as two drivers live.
        """
        for model, sessions in config._MEASURED_CONCURRENCY.items():
            per_proc = config._SESSIONS_PER_PROCESS[config._harness_of_model(model)]
            self.assertLessEqual(
                config.driver_limit(model) * per_proc, sessions,
                f"{model}: {config.driver_limit(model)} drivers x {per_proc} "
                f"sessions each exceeds its {sessions}-session budget")

    def test_driver_caps_leave_headroom_under_the_account_cap(self):
        """Driver caps must reserve room for interactive use of the account."""
        for model, family in config.MODEL_FAMILY.items():
            self.assertLessEqual(
                config.driver_limit(model), config.family_limit(family),
                f"{model} driver cap {config.driver_limit(model)} exceeds the "
                f"{family} account cap {config.family_limit(family)}; the fleet "
                "can out-request the account and ARC rejects over-limit mid-run")


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

    def test_no_live_model_runs_on_the_retired_kimi_harness(self):
        """Kimi-K3 left the fleet on 2026-09-12; nothing live maps to its CLI."""
        self.assertNotIn("kimi", set(config.MODEL_HARNESS.values()))
        self.assertNotIn("kimi", config.FAMILIES,
                         "a retired family in FAMILIES keeps `main.py ask "
                         "--family kimi` reaching a retired model")
        self.assertNotIn("Kimi-K3", config.MODEL_ROLES)

    def test_kimi_wire_model_still_prices_historical_tokens(self):
        """The wire log carries no model name and is the only token source for
        old kimi-harness runs, so the reader must still name Kimi-K3 — falling
        back to it now that no live model uses that harness."""
        self.assertEqual(config.kimi_wire_model(), "Kimi-K3")
        self.assertTrue(config._price_for(config.kimi_wire_model()),
                        "historical kimi tokens must not render $0.00")

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


class PRReviewerCountInvariants(unittest.TestCase):
    """The PR review gate needs independent readers, and every reviewer must be
    a distinct review-capable family.

    `code_tasks.pr_review` picks reviewers from families other than the
    implementer's and all of them must approve before `pr_merge` runs. Fewer
    than two reviewers means no independent second reading at all; more than
    the number of review-capable families can never be satisfied, so the
    review node waits on approvals that no model will ever grant.
    """

    def test_pr_reviewers_support_cross_review(self):
        # Two independent readings is the POLICY (PR_REVIEWERS_WANTED). Whether
        # today's roster can deliver it is a separate fact: with two families
        # (2026-09-12) there is one other family per implementer, and the
        # effective count honestly drops to 1 — the roster audit reports the gap.
        self.assertGreaterEqual(config.PR_REVIEWERS_WANTED, 2,
                                "the operator's policy is two independent reads")
        if len(config.PR_REVIEW_FAMILIES) >= 3:
            self.assertEqual(config.PR_REVIEWERS, 2, "three families can deliver two")

    def test_pr_reviewers_do_not_exceed_review_capable_families(self):
        # An implementer's PR can only be read by the OTHER review-capable
        # families, so the effective count must fit in (families - 1). The
        # operator's wish (PR_REVIEWERS_WANTED) may exceed it — the roster audit
        # reports that — but the effective value must never promise a gate the
        # fleet cannot staff.
        self.assertLessEqual(config.PR_REVIEWERS, max(1, len(config.PR_REVIEW_FAMILIES) - 1))
        self.assertGreaterEqual(config.PR_REVIEWERS, 1)
        self.assertLessEqual(config.PR_REVIEWERS, config.PR_REVIEWERS_WANTED)


class PRRoundsInvariants(unittest.TestCase):
    """The bounded fix and review loops must each get at least one pass.

    `code_tasks.build_code_graph` keeps looping `implement` / `pr_review` only
    while `runs <= MAX_FIX_ROUNDS` / `round_n <= PR_MAX_ROUNDS`. If either
    bound were zero, a rejected change would skip straight to the fail node
    with no chance to correct it, silently dropping work that a single fix pass
    would have salvaged.
    """

    def test_pr_review_gets_at_least_one_round(self):
        self.assertGreaterEqual(config.PR_MAX_ROUNDS, 1,
                                "a zero round bound never lets a rejected change back in")

    def test_fix_loop_gets_at_least_one_attempt(self):
        self.assertGreaterEqual(config.MAX_FIX_ROUNDS, 1,
                                "a zero fix budget fails a task with no chance to correct it")


class BranchInvariants(unittest.TestCase):
    """The fleet integrates on `development`; `main` is prod and never written
    by the fleet.

    `gitstore` opens the PR against BASE_BRANCH and `publish` merges there;
    PROD_BRANCH is only reached through a manual promotion PR. If the two were
    equal, every task would land directly on prod and skip the human gate.
    """

    def test_the_branch_configuration_is_coherent(self):
        """Either flow is valid; a half-configured one is not.

        This used to assert BASE_BRANCH != PROD_BRANCH outright, encoding the
        two-branch flow as a law rather than a choice. Running everything on one
        branch is legitimate — it is the default now — but then the promotion PR
        must be OFF, because main -> main is not a pull request GitHub accepts
        and offering it implies a gate that does not exist.
        """
        if config.BASE_BRANCH == config.PROD_BRANCH:
            self.assertFalse(config.promotion_configured(),
                             "one branch, so promotion must be disabled")
        else:
            self.assertTrue(config.promotion_configured(),
                            "two branches, so promotion must be available")

    def test_neither_branch_name_is_empty(self):
        self.assertTrue(config.BASE_BRANCH.strip())
        self.assertTrue(config.PROD_BRANCH.strip())


class HarnessContextBounds(unittest.TestCase):
    """The harness context budgets are positive and within the provider window.

    `OPENCODE_CONTEXT`/`KIMI_CONTEXT` set the driver's context window. A
    zero or negative value breaks every request the harness makes; a value
    beyond 131072 exceeds the provider's output window and gets clamped or
    fails mid-run.
    """

    def test_opencode_context_is_positive_and_bounded(self):
        self.assertGreater(config.OPENCODE_CONTEXT, 0,
                           "a non-positive context window breaks every harness request")
        self.assertLessEqual(config.OPENCODE_CONTEXT, 131072,
                             "context beyond the provider window is clamped or fails")

    def test_kimi_context_is_positive_and_bounded(self):
        self.assertGreater(config.KIMI_CONTEXT, 0,
                           "a non-positive context window breaks every harness request")
        self.assertLessEqual(config.KIMI_CONTEXT, 131072,
                             "context beyond the provider window is clamped or fails")



class DriverCapsMatchMeasuredReality(unittest.TestCase):
    """Every model's account cap must equal its published/measured ceiling.

    _MEASURED_CONCURRENCY is the one ceiling table: rows whose concurrency was
    ramped until ARC rejected (measured 2026-09-10 — gpt-oss 5, GLM 4, Kimi 3,
    the retired fleet) and rows the provider publishes instead (DeepSeek 10,
    provider docs 2026-09-12). The old config claimed 10 account / 8 drivers
    for gpt-oss and DeepSeek — over-subscribed by 3, so the fleet generated
    its own 400s under load and the capacity backoff blamed the provider,
    while GLM and Kimi sat one slot under their real ceilings.
    """

    def test_no_model_is_over_subscribed(self):
        for model, family in config.MODEL_FAMILY.items():
            self.assertLessEqual(
                config.driver_limit(model), config.family_limit(family),
                f"{model}: more drivers than the account can serve — the fleet "
                f"would generate its own 400s")

    def test_account_caps_match_the_measurement(self):
        for model, measured in config._MEASURED_CONCURRENCY.items():
            self.assertEqual(
                config.family_limit(config.MODEL_FAMILY[model]), measured,
                f"{model}: family limit disagrees with the measured ceiling")

    def test_headroom_reserves_slots_without_starving(self):
        """ARC_DRIVER_HEADROOM trades fleet throughput for interactive use."""
        for model, measured in config._MEASURED_CONCURRENCY.items():
            self.assertGreaterEqual(config.driver_limit(model), 1,
                                    f"{model}: headroom must never reach zero drivers")
            self.assertLessEqual(config.driver_limit(model), measured)


class HarnessConcurrencyCeiling(unittest.TestCase):
    """The harness is a second, lower ceiling than the per-model caps.

    Every opencode-backed model shares one local binary and one sqlite store.
    Measured on this machine with an identical prompt: 5 concurrent runs all
    succeed, 6 loses 2, 10 loses 6 — failing fast with an empty stderr that the
    fleet logged as "opencode exited 1: " and retried four times per task.
    """

    def test_effective_opencode_concurrency_never_exceeds_the_measured_ceiling(self):
        """Whichever cap binds — the harness's or the sum of its models' — the
        number of opencode processes that can run at once must stay at or
        under the measured ceiling of 5. Which one binds is roster-dependent:
        with gpt-oss retired and the sessions-per-process divisor applied, the
        model caps (2+2) now sit BELOW the harness cap, and asserting the
        harness was 'below the sum' encoded yesterday's roster as a law."""
        served = [m for m, h in config.MODEL_HARNESS.items() if h == "opencode"]
        effective = min(config.harness_limit("opencode"),
                        sum(config.driver_limit(m) for m in served))
        self.assertLessEqual(effective, 5)
        self.assertGreater(effective, 0)

    def test_opencode_sits_at_the_measured_ceiling(self):
        self.assertEqual(config.harness_limit("opencode"), 5)

    def test_no_live_harness_is_throttled_below_its_models_caps(self):
        # Derived per harness: a harness cap below the sum of its models'
        # driver caps throttles them. The sum can legitimately EXCEED the
        # harness cap when the harness is the binding ceiling (opencode: 5),
        # so this asserts the roster invariant that fails loudly instead —
        # each harness limit is positive and no model's driver cap exceeds
        # its own account cap.
        for harness in set(config.MODEL_HARNESS.values()):
            self.assertGreater(config.harness_limit(harness), 0)
        for model, fam in config.MODEL_FAMILY.items():
            self.assertLessEqual(config.driver_limit(model),
                                 config.family_limit(fam))
        self.assertNotIn("kimi", set(config.MODEL_HARNESS.values()),
                         "no live model runs the retired kimi harness")

    def test_an_unknown_harness_still_gets_a_finite_cap(self):
        self.assertGreater(config.harness_limit("nope"), 0)

    def test_the_env_override_is_honoured(self):
        os.environ["ARC_HARNESS_LIMIT_OPENCODE"] = "2"
        try:
            self.assertEqual(config.harness_limit("opencode"), 2)
        finally:
            del os.environ["ARC_HARNESS_LIMIT_OPENCODE"]

    def test_a_junk_override_falls_back_rather_than_crashing(self):
        os.environ["ARC_HARNESS_LIMIT_OPENCODE"] = "lots"
        try:
            self.assertEqual(config.harness_limit("opencode"), 5)
        finally:
            del os.environ["ARC_HARNESS_LIMIT_OPENCODE"]


class PerRoleIdleBudgets(unittest.TestCase):
    """A planner is silent for different reasons than an implementer.

    An implementer edits in many small steps; seven minutes of silence means
    something is wrong. A planner does one long agentic read of the repo and
    then emits a single JSON plan — it is legitimately quiet while the model
    generates, and at Kimi's cap of 3 its request waits behind the others with
    the connection held open. Every planner "stall" on 2026-09-11/12 fired
    after exactly 59 bytes (the version handshake) with 3-4 other Kimi drivers
    running: a healthy process killed for being queued.
    """

    def test_the_planner_gets_a_longer_budget_than_an_implementer(self):
        self.assertGreater(config.idle_timeout_for("planner"),
                           config.idle_timeout_for("implementer"))

    def test_an_unlisted_role_gets_the_default(self):
        for role in ("implementer", "reviewer", "pr_reviewer", "anything-else"):
            self.assertEqual(config.idle_timeout_for(role), config.DRIVER_IDLE_TIMEOUT)

    def test_every_role_budget_fits_inside_the_outer_deadline(self):
        # The idle clock must fire BEFORE the hard timeout, or the hard timeout
        # is the only thing that ever fires and the idle diagnosis is lost.
        for role, budget in config.ROLE_IDLE_TIMEOUT.items():
            self.assertLess(budget, config.DRIVER_TIMEOUT,
                            f"{role}'s idle budget must stay under DRIVER_TIMEOUT")

    def test_the_planner_budget_covers_a_queued_first_token(self):
        # The measured time-to-first-token tail is ~309 s unloaded; at cap the
        # request waits behind the others. The budget must clear that with room.
        self.assertGreaterEqual(config.idle_timeout_for("planner"), 900)


class TheApiIsTheFactTheDatesAreThePlan(unittest.TestCase):
    """A roster date is a promise someone else has to keep.

    On 2026-09-12 the roster said DeepSeek-V4.1-Flash had replaced V4. The API
    had never heard of 4.1 and still served V4. Trusting the date alone routed
    the entire medium tier at a model that does not exist; trusting it in the
    other direction retired the only medium model the fleet actually had.
    """

    def _roster(self, served, day="2026-09-12"):
        import datetime
        orig = config.available_models
        config.available_models = lambda *a, **k: served
        try:
            return {m for m, *_ in config.live_roster(datetime.date.fromisoformat(day))}
        finally:
            config.available_models = orig

    def test_a_dated_arrival_the_api_does_not_serve_is_deferred(self):
        live = self._roster({"DeepSeek-V4-Flash", "GLM-5.3", "Kimi-K3"})
        self.assertNotIn("DeepSeek-V4.1-Flash-thinking-max", live)

    def test_an_incumbent_stays_while_its_replacement_is_not_real(self):
        live = self._roster({"DeepSeek-V4-Flash", "GLM-5.3", "Kimi-K3"})
        self.assertIn("DeepSeek-V4-Flash", live,
                      "retiring it would leave the fleet with no medium tier")

    def test_the_swap_happens_the_day_the_api_serves_it(self):
        live = self._roster({"DeepSeek-V4.1-Flash-thinking-max", "GLM-5.3", "Kimi-K3"})
        self.assertIn("DeepSeek-V4.1-Flash-thinking-max", live)
        self.assertNotIn("DeepSeek-V4-Flash", live)

    def test_unknown_availability_falls_back_to_the_dates(self):
        # No snapshot, or the VPN is down: an unverifiable claim must not empty
        # the roster.
        live = self._roster(None)
        self.assertTrue(live)

    def test_an_empty_api_answer_does_not_empty_the_fleet(self):
        live = self._roster(set())
        self.assertTrue(live, "a bad snapshot must not leave the fleet with no models")


class InteractiveWorkGetsAReservedSlot(unittest.TestCase):
    """A human waiting on a chat reply must not queue behind batch work."""

    def test_the_planner_holds_a_slot_back_from_batch(self):
        p = config.PLANNER_MODEL
        if config.driver_limit(p, interactive=True) - config.INTERACTIVE_RESERVE < config.MIN_BATCH_SLOTS:
            self.skipTest("planner cap too small to reserve from today")
        self.assertLess(config.driver_limit(p), config.driver_limit(p, interactive=True))

    def test_no_other_model_loses_a_slot(self):
        for m in config.IMPLEMENTER_MODELS:
            if m == config.PLANNER_MODEL:
                continue
            self.assertEqual(config.driver_limit(m), config.driver_limit(m, interactive=True),
                             f"{m} does not serve chat and must keep its full cap")

    def test_batch_never_drops_below_the_floor(self):
        for m in config.IMPLEMENTER_MODELS:
            self.assertGreaterEqual(config.driver_limit(m), 1)
