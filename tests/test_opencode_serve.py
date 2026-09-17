"""OpencodeDriver on the serve path — now the ONLY path.

Hermetic: every test drives the driver against tests/fake_ocserve.py over
loopback and never spawns the real opencode binary. The point of the suite is
that the serve path is indistinguishable from the retired one-shot path to
everything downstream — same DriverResult fields, same transcript lines, same
capacity/escalation semantics — and that no `opencode run` process is spawned.
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
        # Serve is the ONLY path now (task opencode-serve-only): a shared-server
        # stub pointing at the fake receives every prompt.
        self._shared = mock.patch.object(
            ocserve, "get_shared_server",
            side_effect=lambda **kw: _FakeSharedServer(self.fake.base_url))
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


class ServeIsTheOnlyPath(unittest.TestCase):
    """Serve is unconditional now: no mode knob, no one-shot spawn."""

    def test_config_exposes_no_mode_knob(self):
        self.assertFalse(hasattr(config, "opencode_mode"))
        self.assertFalse(hasattr(config, "OPENCODE_MODE_DEFAULT"))

    def test_the_driver_never_spawns_a_process(self):
        """_once must go to the server, never to spawn(). Reaching spawn — the
        boundary the retired one-shot path used — is the failure."""
        d = drivers.OpencodeDriver("GLM-5.3", "implementer")

        def no_server(*a, **kw):
            raise AssertionError("get_shared_server: serve path not taken")

        async def no_spawn(*a, **kw):
            raise AssertionError("reached spawn: _once took a one-shot path")

        with mock.patch.object(drivers, "spawn", no_spawn):
            with mock.patch.object(ocserve, "get_shared_server", no_server):
                with self.assertRaises(AssertionError):
                    asyncio.run(d._once("hi", Path("."), None, "t", 1))


if __name__ == "__main__":
    unittest.main()
