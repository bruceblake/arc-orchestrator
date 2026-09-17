"""Serve is the ONLY OpencodeDriver path (task opencode-serve-only).

Two things this suite pins, both about the switch being COMPLETE:

  * config exposes no mode knob any more — the rollback story is `git revert`,
    not an env var;
  * `OpencodeDriver._once` always goes to the shared `opencode serve` server
    and never spawns an `opencode run` process.

Hermetic: the server is tests/fake_ocserve.py, the spawn boundary is a mock.
No real opencode binary is ever started.
"""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import capture_events  # noqa: F401  (sys.path)

import config
import drivers
import ocserve
from fake_ocserve import FakeOcserve


class NoModeKnobRemains(unittest.TestCase):
    def test_config_exposes_no_mode_knob(self):
        self.assertFalse(hasattr(config, "opencode_mode"),
                         "ARC_OPENCODE_MODE's helper must be gone")
        self.assertFalse(hasattr(config, "OPENCODE_MODE_DEFAULT"),
                         "the mode default must be gone")

    def test_the_old_env_var_is_ignored(self):
        """Setting ARC_OPENCODE_MODE must change nothing — there is no reader."""
        with mock.patch.dict(os.environ, {"ARC_OPENCODE_MODE": "oneshot"}):
            self.assertFalse(hasattr(config, "opencode_mode"))


class TheDriverOnlyEverUsesTheServer(unittest.TestCase):
    """A fake server receives the prompt; no process is ever spawned."""

    def setUp(self):
        self.fake = FakeOcserve().start()
        self.addCleanup(self.fake.stop)
        self.worktree = tempfile.mkdtemp(prefix="ocserve-only-")
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self.worktree, ignore_errors=True))
        self.tdir = tempfile.mkdtemp(prefix="ocserve-only-tx-")
        self._orig_tdir = drivers.TRANSCRIPT_DIR
        drivers.TRANSCRIPT_DIR = Path(self.tdir)
        self.addCleanup(lambda: setattr(drivers, "TRANSCRIPT_DIR", self._orig_tdir))
        self._shared = mock.patch.object(
            ocserve, "get_shared_server",
            side_effect=lambda **kw: ocserve.ServeHandle(self.fake.base_url,
                                                         proc=None))
        self._shared.start()
        self.addCleanup(self._shared.stop)
        self._db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._db.close()
        self.addCleanup(lambda: os.unlink(self._db.name))
        self.addCleanup(lambda: setattr(drivers, "_lease_store", None))
        import store
        drivers._lease_store = store.Store(self._db.name)

    def _driver(self):
        d = drivers.OpencodeDriver("GLM-5.3", "implementer")
        drivers._semaphores.pop(d.model, None)
        return d

    def test_the_server_receives_the_prompt_and_spawn_is_never_called(self):
        d = self._driver()

        async def no_spawn(*a, **kw):
            raise AssertionError("spawn() called: a process was launched")

        with mock.patch.object(drivers, "spawn", no_spawn):
            res = asyncio.run(d.run("hi", worktree=self.worktree,
                                    task_id="t-only"))
        self.assertIn("Hello from the fake server.", res.text)
        self.assertTrue(self.fake.prompts, "the server saw no prompt")
        self.assertTrue(self.fake.sessions, "no session was created")

    def test_the_prompt_body_carries_the_model_and_directory(self):
        d = self._driver()
        asyncio.run(d.run("hi", worktree=self.worktree, task_id="t-body"))
        self.assertTrue(self.fake.prompt_models)
        self.assertEqual(self.fake.prompt_models[-1],
                         {"providerID": "ARC", "modelID": "GLM-5.3"})
        # x-opencode-directory binds the session to the run's worktree.
        self.assertEqual(self.fake.directories.get("prompt_async"),
                         str(self.worktree))


if __name__ == "__main__":
    unittest.main()
