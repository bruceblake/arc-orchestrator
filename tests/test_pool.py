"""Unit tests for pool.ArcPool in dry-run mode (never touches the ARC API).

The research workload routes every request through ArcPool, so the model
resolver, the dry-run fake responses, the snapshot counters, and the
session-limit detector are what keep 24/7 rounds from silently misbehaving.
"""
import asyncio
import types
import pathlib
import re
import unittest

from helpers import capture_events  # noqa: F401  (fixes sys.path, redirects the event log)

import config
import pool as pool_module
from openai import BadRequestError, RateLimitError
from pool import ArcPool, _is_session_limit


class PoolResolveModel(unittest.TestCase):
    """resolve_model maps a family + effort/websearch flag to one model id.

    Family routing is what keeps research rounds on the model the family's
    concurrency cap was sized for; a wrong default or a cross-family id would
    silently break the per-family limits that family_limit() enforces.
    """

    def setUp(self):
        self.pool = ArcPool(dry_run=True)

    def test_returns_the_family_default_model(self):
        # Every live family, not one named literally: this asserted gpt-oss's
        # default and went uninformative the day gpt-oss left the registry.
        for fam in config.FAMILY_ORDER:
            self.assertEqual(self.pool.resolve_model(fam),
                             config.FAMILIES[fam].models["default"])

    def test_a_removed_family_is_unreachable(self):
        # Retiring a model from ROSTER is not enough: while it stayed in
        # FAMILIES, `main.py ask --family gpt-oss` still routed to it.
        with self.assertRaises(ValueError):
            self.pool.resolve_model("gpt-oss")

    def test_returns_an_explicit_effort_variant(self):
        self.assertEqual(
            self.pool.resolve_model("deepseek", effort="max"),
            "DeepSeek-V4.1-Flash-thinking-max",
        )

    def test_returns_the_websearch_variant(self):
        self.assertEqual(
            self.pool.resolve_model("kimi", websearch=True),
            "Kimi-K3-thinking-max-legacy-tool-calling",
        )

    def test_rejects_an_unknown_family(self):
        # Same error type as chat(), and never a silent fall-through to some
        # other family's model. It used to be a bare KeyError from indexing
        # FAMILIES, which reached the CLI as a traceback.
        with self.assertRaises(ValueError):
            self.pool.resolve_model("no-such-family")

    def test_rejects_websearch_for_a_family_without_a_websearch_variant(self):
        orig = pool_module.FAMILIES
        pool_module.FAMILIES = dict(orig)
        pool_module.FAMILIES["plain"] = config.Family(
            name="plain", limit=1, models={"default": "plain-model"}
        )
        try:
            with self.assertRaises(ValueError):
                self.pool.resolve_model("plain", websearch=True)
        finally:
            pool_module.FAMILIES = orig


class PoolDryRunChat(unittest.IsolatedAsyncioTestCase):
    """chat() in dry-run must answer every purpose and record usage metadata.

    The production pool has no live API in dry-run mode, so these tests are
    what prove the request path (purpose -> fake response -> meta) is wired up
    without spending real budget.
    """

    def setUp(self):
        self.pool = ArcPool(dry_run=True)

    @staticmethod
    def _purposes_used_in_the_codebase():
        """Every purpose= the code actually passes to pool.chat().

        Derived, not hand-listed. The hand-written list here named eight and
        the codebase used thirteen, so `bench` (used four times in bench.py)
        had no dry-run response at all and nothing noticed: --dry-run returned
        a generic string where the caller expected fenced code.
        """
        found = set()
        for f in sorted(pathlib.Path(__file__).resolve().parent.parent.glob("*.py")):
            for m in re.finditer(r'purpose\s*=\s*"([a-z_]+)"', f.read_text()):
                found.add(m.group(1))
        return sorted(found)

    async def test_every_purpose_has_its_own_dry_run_response(self):
        """Not merely non-empty — non-GENERIC.

        The fallthrough is `[dry-run <family>] <prompt>`, which is non-empty,
        so an assertion that the text is truthy passes for a purpose that has
        no case at all. `bench` was missing for exactly that reason: bench.py
        asks for it four times and got an echo of its own prompt where it
        expected a fenced code block.
        """
        purposes = self._purposes_used_in_the_codebase()
        self.assertGreaterEqual(len(purposes), 8, "purpose scan found too few")
        for purpose in purposes:
            if purpose == "chat":
                continue  # the echo IS chat's intended response
            with self.subTest(purpose=purpose):
                text = await self.pool.chat(
                    "deepseek", [{"role": "user", "content": "hello"}],
                    purpose=purpose,
                )
                self.assertTrue(text.strip(),
                                f"dry-run has no response for purpose={purpose!r}")
                self.assertFalse(
                    text.startswith("[dry-run deepseek] hello"),
                    f"purpose={purpose!r} fell through to the generic echo — "
                    f"add a case for it in pool._dry_run_text")

    async def test_the_generic_fallthrough_still_exists_for_unknown_purposes(self):
        text = await self.pool.chat(
            "deepseek", [{"role": "user", "content": "hello"}],
            purpose="not-a-real-purpose")
        self.assertTrue(text.strip())

    async def test_populates_meta_with_model_tokens_and_latency(self):
        meta = {}
        await self.pool.chat(
            "deepseek", [{"role": "user", "content": "hello"}],
            purpose="answer", meta=meta,
        )
        self.assertEqual(meta["model"], "dry-run")
        self.assertGreaterEqual(meta["tokens"], 1)
        self.assertGreaterEqual(meta["latency_ms"], 0)
        self.assertIn("attempts", meta)

    async def test_rejects_an_unknown_family(self):
        with self.assertRaises(ValueError):
            await self.pool.chat(
                "no-such-family", [{"role": "user", "content": "hello"}]
            )


class PoolSnapshot(unittest.TestCase):
    """snapshot() reports the per-family counters the dashboard drains.

    The research workload renders these into per-family usage; if a counter is
    missing or never increments, the dashboard shows an idle family while
    requests are actually flying.
    """

    def setUp(self):
        self.pool = ArcPool(dry_run=True)

    def test_reports_requests_tokens_errors_inflight_and_capacity(self):
        snap = self.pool.snapshot()
        for key in ("requests", "tokens", "errors", "inflight", "capacity"):
            self.assertIn(key, snap)

    def test_request_counter_increases_after_a_chat(self):
        before = sum(self.pool.snapshot()["requests"].values())
        asyncio.run(
            self.pool.chat("deepseek", [{"role": "user", "content": "hi"}], purpose="answer")
        )
        after = sum(self.pool.snapshot()["requests"].values())
        self.assertGreater(after, before)
        self.assertEqual(self.pool.snapshot()["inflight"]["deepseek"], 0)

    def test_capacity_reflects_the_family_limits(self):
        snap = self.pool.snapshot()
        for fam in config.FAMILY_ORDER:
            self.assertEqual(snap["capacity"][fam], config.family_limit(fam))


class PoolSessionLimitDetection(unittest.TestCase):
    """_is_session_limit catches ARC-concurrency 400s and nothing else.

    ARC rejects an over-cap request with HTTP 400 "concurrent session limit
    reached"; treating that as a generic bad request would drop it straight to
    the error counter and end the round instead of backing off and retrying.
    """

    @staticmethod
    def _bad_request(cls, message):
        response = types.SimpleNamespace(
            request=types.SimpleNamespace(), headers={}, status_code=400
        )
        return cls(message, response=response, body=None)

    def test_recognises_arcs_concurrency_400(self):
        self.assertTrue(
            _is_session_limit(self._bad_request(
                BadRequestError, '400 {"detail": "concurrent session limit reached"}'))
        )

    def test_recognises_the_session_limit_phrase(self):
        self.assertTrue(
            _is_session_limit(self._bad_request(BadRequestError, "Error: session limit exceeded"))
        )

    def test_does_not_match_an_unrelated_bad_request(self):
        self.assertFalse(
            _is_session_limit(self._bad_request(BadRequestError, "400 invalid parameter"))
        )

    def test_does_not_match_other_error_types(self):
        self.assertFalse(
            _is_session_limit(self._bad_request(RateLimitError, "429 rate limited"))
        )


if __name__ == "__main__":
    unittest.main()
