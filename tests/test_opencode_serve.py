"""OpencodeDriver on the serve path (ARC_OPENCODE_MODE=serve).

Hermetic: every test drives the driver against tests/fake_ocserve.py over
loopback and never spawns the real opencode binary. The point of the suite is
that the serve branch is indistinguishable from the one-shot branch to
everything downstream — same DriverResult fields, same transcript lines, same
capacity/escalation semantics — and that flipping the mode back to 'oneshot'
starts no server at all.
"""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import capture_events, ENTRY  # noqa: F401  (sys.path)

import config
import drivers
import ocserve
from fake_ocserve import FakeOcserve


class _FakeSharedServer:
    """Stands in for ocserve.get_shared_server(): returns a handle to the fake.

    The driver only needs `.base_url` from the handle (the client posts to it);
    the point is that NO real server is started and the fake's port is used.
    """

    def __init__(self, base_url):
        self.base_url = base_url


class ServeDriverTestBase(unittest.TestCase):
    """Wire OpencodeDriver onto a fake server in serve mode, on a tmp worktree."""

    def setUp(self):
        self.fake = FakeOcserve().start()
        self.addCleanup(self.fake.stop)
        self.worktree = tempfile.mkdtemp(prefix="ocserve-driver-")
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.worktree, ignore_errors=True))
        # Redirect the transcript dir so the suite never writes real logs.
        self.tdir = tempfile.mkdtemp(prefix="ocserve-transcripts-")
        self._orig_tdir = drivers.TRANSCRIPT_DIR
        drivers.TRANSCRIPT_DIR = Path(self.tdir)
        self.addCleanup(lambda: setattr(drivers, "TRANSCRIPT_DIR", self._orig_tdir))
        # Serve mode + a shared-server stub pointing at the fake.
        self._env = mock.patch.dict(os.environ, {"ARC_OPENCODE_MODE": "serve"})
        self._env.start()
        self.addCleanup(self._env.stop)
        self._shared = mock.patch.object(
            ocserve, "get_shared_server",
            return_value=_FakeSharedServer(self.fake.base_url))
        self._shared.start()
        self.addCleanup(self._shared.stop)
        # No real backoff sleeps on the retry ladder.
        self._orig_backoff = config.DRIVER_CAPACITY_BACKOFF
        config.DRIVER_CAPACITY_BACKOFF = 0.01
        self.addCleanup(lambda: setattr(
            config, "DRIVER_CAPACITY_BACKOFF", self._orig_backoff))
        self._orig_cap = config.DRIVER_CAPACITY_BACKOFF_CAP
        config.DRIVER_CAPACITY_BACKOFF_CAP = 0.05
        self.addCleanup(lambda: setattr(
            config, "DRIVER_CAPACITY_BACKOFF_CAP", self._orig_cap))
        # Leases go to a throwaway db.
        self._db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._db.close()
        self.addCleanup(lambda: os.unlink(self._db.name))
        self.addCleanup(lambda: setattr(drivers, "_lease_store", None))
        import store
        drivers._lease_store = store.Store(self._db.name)

    def _driver(self, role="implementer"):
        d = drivers.OpencodeDriver("GLM-5.3", role)
        self._quieten(d)
        return d

    def _quieten(self, driver):
        # The suite must not depend on the model having a free slot; the gates
        # are exercised elsewhere. Give this driver its own counter so a global
        # cap state cannot flake the test.
        drivers._semaphores.pop(driver.model, None)

    def _transcript(self, task_id, role="implementer", attempt=1):
        return Path(self.tdir) / f"{task_id}-{role}-{attempt}.jsonl"


class ServeHappyPath(ServeDriverTestBase):
    def test_result_fields_match_the_contract(self):
        d = self._driver()
        res = asyncio.run(d.run("hi", worktree=self.worktree, task_id="t-happy"))
        self.assertEqual(res.harness, "opencode")
        self.assertEqual(res.model, "GLM-5.3")
        self.assertEqual(res.role, "implementer")
        self.assertEqual(res.exit_code, 0)
        self.assertIn("Hello from the fake server.", res.text)
        self.assertTrue(res.session_id and res.session_id.startswith("ses_"))
        self.assertEqual(Path(res.transcript_path), self._transcript("t-happy"))
        # prompt = input + cache read + cache write; completion = output + reasoning
        self.assertEqual(res.prompt_tokens, 11 + 20 + 1)
        self.assertEqual(res.completion_tokens, 22 + 3)
        self.assertEqual(res.tokens, 57)

    def test_emits_the_same_driver_events_as_the_one_shot_path(self):
        d = self._driver()
        with capture_events() as ev:
            asyncio.run(d.run("hi", worktree=self.worktree, task_id="t-events"))
        self.assertTrue(ev.of("driver.queued"))
        self.assertTrue(ev.of("driver.start"))
        done = ev.first("driver.done")
        self.assertIsNotNone(done)
        self.assertEqual(done["model"], "GLM-5.3")
        self.assertEqual(done["tokens"], 57)

    def test_transcript_carries_one_shot_shaped_lines(self):
        d = self._driver()
        asyncio.run(d.run("hi", worktree=self.worktree, task_id="t-tx"))
        raw = self._transcript("t-tx").read_text()
        # The dashboard tails these; parse_transcript/transcript_tokens must
        # read them exactly as they read the one-shot stream.
        types = [json.loads(l).get("type") for l in raw.splitlines() if l.strip()]
        self.assertIn("text", types)
        self.assertIn("step_finish", types)
        sid, _ = drivers.parse_transcript(raw)
        toks, ptok, ctok = drivers.transcript_tokens(raw)
        self.assertEqual(toks, 57)
        self.assertEqual(ptok, 11 + 20 + 1)
        self.assertEqual(ctok, 22 + 3)

    def test_the_model_reaches_the_server_with_the_roster_alias(self):
        d = self._driver()
        asyncio.run(d.run("hi", worktree=self.worktree, task_id="t-alias"))
        # Both routes received the provider-qualified model, split correctly.
        self.assertTrue(self.fake.prompt_models)
        self.assertEqual(self.fake.prompt_models[-1],
                         {"providerID": "ARC", "modelID": "GLM-5.3"})
        sid = next(iter(self.fake.session_models))
        self.assertEqual(self.fake.session_models[sid],
                         {"providerID": "ARC", "id": "GLM-5.3"})

    def test_dispose_is_called_on_success(self):
        d = self._driver()
        asyncio.run(d.run("hi", worktree=self.worktree, task_id="t-dispose-ok"))
        self.assertIn(str(self.worktree), self.fake.disposed)


class ServeFailureSemantics(ServeDriverTestBase):
    def _always(self, frames):
        """Make every prompt produce `frames`, whatever its text.

        Driver.run retries a failed attempt with a DIFFERENT continuation
        prompt, so a fake keyed on the prompt text would succeed on the retry
        and hide the ladder under test.
        """
        self.fake.frames_for = lambda text: frames

    def test_capacity_refusal_is_retried_then_fails_without_escaping(self):
        """CapacityFull must retry on the capacity ladder and surface as a
        failed attempt (DriverError), not an exception out of the graph."""
        self._always(FakeOcserve().frames_for("capacity!"))
        d = self._driver()
        config.MAX_RETRIES = 1
        try:
            with capture_events() as ev:
                with self.assertRaises(drivers.DriverError) as ctx:
                    asyncio.run(d.run("capacity!", worktree=self.worktree,
                                      task_id="t-cap"))
        finally:
            config.MAX_RETRIES = 24
        self.assertIn("concurrency", str(ctx.exception))
        # Retried at least once before giving up, on the capacity ladder.
        errs = ev.of("driver.error")
        self.assertTrue(errs)
        self.assertTrue(all(e["capacity"] for e in errs),
                        "a capacity refusal must be classified as capacity")
        # And NOT captured as a defect (capacity is expected weather).
        self.assertTrue(all(e["fingerprint"] is None for e in errs))

    def test_dispose_is_called_on_error(self):
        self._always(FakeOcserve().frames_for("boom"))
        d = self._driver()
        # A non-capacity error backs off exponentially (up to 60s, MAX_RETRIES
        # times); cap the ladder so the test asserts dispose-on-error without
        # sleeping through it.
        config.MAX_RETRIES = 1
        try:
            with self.assertRaises(drivers.DriverError):
                asyncio.run(d.run("boom", worktree=self.worktree,
                                  task_id="t-dispose-err"))
        finally:
            config.MAX_RETRIES = 24
        self.assertIn(str(self.worktree), self.fake.disposed)

    def test_a_stalled_stream_disposes_and_retries(self):
        """An SSE stall is the idle-kill analogue: dispose, then retry."""
        self._always([])                 # connect, then silence forever
        d = self._driver()
        # A tiny stall window so the fake's silence trips it fast.
        orig_idle = config.ROLE_IDLE_TIMEOUT.get("implementer")
        config.ROLE_IDLE_TIMEOUT["implementer"] = 0.3
        try:
            with capture_events() as ev:
                with self.assertRaises(drivers.DriverError) as ctx:
                    asyncio.run(d.run("emit:[]", worktree=self.worktree,
                                      task_id="t-stall"))
        finally:
            if orig_idle is None:
                config.ROLE_IDLE_TIMEOUT.pop("implementer", None)
            else:
                config.ROLE_IDLE_TIMEOUT["implementer"] = orig_idle
        self.assertIn("stall", str(ctx.exception).lower())
        self.assertTrue(ev.of("driver.error"))
        self.assertIn(str(self.worktree), self.fake.disposed)


class DefaultModeStartsNoServer(unittest.TestCase):
    """The default must stay one-shot: no server, no session, no ocserve."""

    def test_default_mode_is_oneshot(self):
        self.assertEqual(config.OPENCODE_MODE_DEFAULT, "oneshot")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARC_OPENCODE_MODE", None)
            self.assertEqual(config.opencode_mode(), "oneshot")

    def test_a_bad_mode_value_is_rejected(self):
        with mock.patch.dict(os.environ, {"ARC_OPENCODE_MODE": "srve"}):
            with self.assertRaises(ValueError):
                config.opencode_mode()

    def test_oneshot_mode_never_touches_ocserve(self):
        with mock.patch.dict(os.environ, {"ARC_OPENCODE_MODE": "oneshot"}):
            d = drivers.OpencodeDriver("GLM-5.3", "implementer")

            async def boom(*a, **kw):
                raise AssertionError("oneshot path must not start a server")

            with mock.patch.object(ocserve, "get_shared_server", boom):
                # The one-shot _once would spawn a real binary, so stop it at
                # the spawn boundary: reaching spawn means it did NOT take the
                # serve branch, which is the assertion.
                with mock.patch.object(drivers, "spawn",
                                       side_effect=AssertionError("spawned one-shot")):
                    with self.assertRaises(AssertionError) as ctx:
                        asyncio.run(d._once("hi", Path("."), None, "t", 1))
            self.assertIn("one-shot", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
