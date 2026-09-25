"""ocserve.py: server lifecycle, prompt round-trip, error classification.

Hermetic: the lifecycle tests point ARC_OPENCODE_SERVE_BIN at a tiny generated
stub script, and the client tests talk to tests/fake_ocserve.py over loopback.
The real `opencode` binary is never spawned here.
"""
import asyncio
import gc
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import capture_events  # noqa: F401  (also puts ROOT on sys.path)

import config
import ocserve
from fake_ocserve import FakeOcserve


def _gone(pid, wait_s=5.0):
    """True once `pid` no longer exists (a zombie still exists — give init a
    moment to reap what the killed parent left behind)."""
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            with open(f"/proc/{pid}/stat") as fh:
                if fh.read().rsplit(")", 1)[1].split()[0] == "Z":
                    return True
        except OSError:
            return True
        time.sleep(0.05)
    return False


_STUB_BODY = '''#!{python}
"""Stub `opencode serve` for ocserve tests. Mode: {mode}."""
import http.server, os, sys, threading, time

PIDFILE = {pidfile!r}
MODE = {mode!r}


def _bail(code):
    open(PIDFILE, "w").write(str(os.getpid()))
    sys.exit(code)


port = 0
for i, arg in enumerate(sys.argv):
    if arg == "--port":
        port = int(sys.argv[i + 1])

if MODE == "exit":
    _bail(3)
if MODE == "never-ready":
    open(PIDFILE, "w").write(str(os.getpid()))
    while True:
        time.sleep(60)


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b'{{"healthy":true}}'
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
srv.daemon_threads = True
open(PIDFILE, "w").write(str(os.getpid()))
srv.serve_forever()
'''


def _write_stub(directory, mode):
    pidfile = Path(directory) / f"stub-{mode}.pid"
    path = Path(directory) / f"opencode-stub-{mode}"
    path.write_text(_STUB_BODY.format(python=sys.executable, mode=mode,
                                      pidfile=str(pidfile)))
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(path), pidfile


def _wait_pidfile(pidfile, wait_s=10.0):
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            return int(pidfile.read_text().strip())
        except (OSError, ValueError):
            time.sleep(0.05)
    raise AssertionError(f"stub never wrote its pid to {pidfile}")


def _reset_shared(saved_bin):
    """Test cleanup: drop the process-wide server, restore the knob."""
    shared = ocserve._shared
    ocserve._shared = None
    config.OPENCODE_SERVE_BIN = saved_bin
    if shared is not None:
        shared.stop()


class ServerLifecycle(unittest.TestCase):
    """start_server / stop, driven by a stub binary — never the real one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ocserve-stub-")
        self.addCleanup(self.tmp.cleanup)

    def test_start_is_ready_and_stop_kills_the_process(self):
        binary, pidfile = _write_stub(self.tmp.name, "ok")
        handle = ocserve.start_server(binary=binary, startup_timeout=20)
        try:
            self.assertTrue(handle.base_url.startswith("http://127.0.0.1:"))
            self.assertTrue(ocserve._health_ok(handle.base_url))
            self.assertEqual(handle.argv[0], binary)
            self.assertIn("serve", handle.argv)
            pid = handle.proc.pid
            self.assertFalse(_gone(pid, 0.0))
        finally:
            handle.stop()
        self.assertTrue(handle.stopped)
        self.assertTrue(_gone(pid), f"stub {pid} survived stop()")
        handle.stop()          # idempotent: a second stop is a no-op
        self.assertTrue(handle.stopped)

    def test_startup_timeout_cleans_up_the_process(self):
        binary, pidfile = _write_stub(self.tmp.name, "never-ready")
        with self.assertRaises(ocserve.StartupError) as ctx:
            ocserve.start_server(binary=binary, startup_timeout=1.5)
        self.assertIn("not ready within", str(ctx.exception))
        pid = _wait_pidfile(pidfile)
        self.assertTrue(_gone(pid), f"stub {pid} outlived the startup timeout")

    def test_startup_failure_when_the_binary_exits(self):
        binary, pidfile = _write_stub(self.tmp.name, "exit")
        with self.assertRaises(ocserve.StartupError) as ctx:
            ocserve.start_server(binary=binary, startup_timeout=20)
        self.assertIn("exited during startup", str(ctx.exception))
        self.assertTrue(_gone(_wait_pidfile(pidfile)))

    def test_missing_binary_is_a_startup_error(self):
        with self.assertRaises(ocserve.StartupError):
            ocserve.start_server(binary=str(Path(self.tmp.name) / "nope"),
                                 startup_timeout=2)

    def test_default_binary_is_resolved_at_spawn_not_import_time(self):
        """OPENCODE_SERVE_BIN is fixed at import; start_server must re-resolve."""
        resolved = str(Path(self.tmp.name) / "resolved-opencode")
        Path(resolved).write_text(f"#!{sys.executable}\n", encoding="utf-8")
        Path(resolved).chmod(0o755)
        saved = config.OPENCODE_SERVE_BIN
        config.OPENCODE_SERVE_BIN = "opencode"
        self.addCleanup(setattr, config, "OPENCODE_SERVE_BIN", saved)

        mock_proc = mock.MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 424242

        with mock.patch.object(config, "opencode_serve_bin", return_value=resolved):
            with mock.patch("ocserve.subprocess.Popen", return_value=mock_proc) as popen:
                with mock.patch("ocserve._health_ok", return_value=True):
                    handle = ocserve.start_server(startup_timeout=5)
                    try:
                        argv = popen.call_args[0][0]
                        self.assertEqual(argv[0], resolved)
                        self.assertEqual(config.OPENCODE_SERVE_BIN, "opencode")
                    finally:
                        handle.stop()

    def test_shared_server_is_one_process_and_restarts_after_stop(self):
        binary, _ = _write_stub(self.tmp.name, "ok")
        saved = config.OPENCODE_SERVE_BIN
        ocserve._shared = None
        self.addCleanup(_reset_shared, saved)
        with mock.patch.object(config, "opencode_serve_bin", return_value=binary):
            first = ocserve.get_shared_server()
            self.assertIs(first, ocserve.get_shared_server())   # ONE per process
            first.stop()
            second = ocserve.get_shared_server()                # died: replaced
            self.assertIsNot(first, second)
            self.assertTrue(ocserve._health_ok(second.base_url))


class ClientAgainstFake(unittest.TestCase):
    """OcserveClient against tests/fake_ocserve.py over loopback."""

    def setUp(self):
        self.fake = FakeOcserve().start()
        self.addCleanup(self.fake.stop)
        self.handle = ocserve.ServeHandle(self.fake.base_url, proc=None)

    def _create(self, worktree="/tmp/ocserve-test-worktree"):
        return asyncio.run(ocserve.OcserveClient.create(
            self.handle, worktree=worktree))

    def test_happy_path_extracts_text_and_tokens(self):
        client = self._create()
        result = asyncio.run(client.prompt("hi"))
        self.assertIn("Hello from the fake server.", result.text)
        self.assertEqual(result.tokens["input"], 11)
        self.assertEqual(result.tokens["output"], 22)
        self.assertEqual(result.tokens["reasoning"], 3)
        self.assertEqual(result.tokens["cache"], {"read": 20, "write": 1})
        self.assertEqual(result.tokens["total"], 57)   # the server's own total

    def test_the_prompt_echo_is_not_part_of_the_answer(self):
        """The stream carries the USER's text too — it must not be collected.

        Measured live on 1.18.29: the user's own prompt comes back as a
        message.part.updated text part before the answer. Collecting it put the
        prompt at the head of result.text, where downstream verdict parsing and
        transcript_tokens would read it as the model's output.
        """
        client = self._create()
        prompt = "Use the bash tool to run `ls /tmp` and then reply FINISHED."
        result = asyncio.run(client.prompt(prompt))
        self.assertEqual(result.text, "echo: " + prompt)
        self.assertFalse(result.text.startswith(prompt),
                         "the user's text part was collected as the answer")
        # Distinctive marker proves the USER frame specifically was skipped:
        # the echo of the user's turn is the only frame carrying it.
        self.fake.frames_for = lambda text: [
            {"type": "message.updated", "properties": {"info": {
                "id": "msg_u", "role": "user"}}},
            {"type": "message.part.updated", "properties": {"part": {
                "type": "text", "messageID": "msg_u", "text": "USER_MARKER"}}},
            {"type": "message.updated", "properties": {"info": {
                "id": "msg_a", "role": "assistant"}}},
            {"type": "message.part.updated", "properties": {"part": {
                "type": "text", "messageID": "msg_a", "text": "ANSWER"}}},
            {"type": "session.idle", "properties": {}},
        ]
        result = asyncio.run(client.prompt("hi"))
        self.assertEqual(result.text, "ANSWER")
        self.assertNotIn("USER_MARKER", result.text)

    def test_unknown_role_text_is_still_collected(self):
        """A stream that never announced a role must not silently drop text."""
        client = self._create()
        self.fake.frames_for = lambda text: [
            {"type": "message.part.updated",
             "properties": {"part": {"type": "text", "text": "unattributed"}}},
            {"type": "session.idle", "properties": {}},
        ]
        result = asyncio.run(client.prompt("hi"))
        self.assertEqual(result.text, "unattributed")

    def test_model_payload_uses_the_key_each_route_demands(self):
        """`id` on /session, `modelID` on prompt_async — verified live.

        The fake rejects the wrong key with the real server's 400, so sending
        one shared shape (the rejected attempt did) fails here rather than
        against the binary.
        """
        client = asyncio.run(ocserve.OcserveClient.create(
            self.handle, worktree="/tmp/ocserve-model-keys",
            model="ARC/GLM-5.3"))
        self.assertEqual(self.fake.session_models[client.session_id],
                         {"providerID": "ARC", "id": "GLM-5.3"})
        asyncio.run(client.prompt("hi", model="ARC/GLM-5.3"))
        self.assertEqual(self.fake.prompt_models[-1],
                         {"providerID": "ARC", "modelID": "GLM-5.3"})

    def test_model_dict_is_accepted_in_either_spelling(self):
        client = asyncio.run(ocserve.OcserveClient.create(
            self.handle, worktree="/tmp/ocserve-model-dict",
            model={"providerID": "ARC", "modelID": "GLM-5.3"}))
        self.assertEqual(self.fake.session_models[client.session_id],
                         {"providerID": "ARC", "id": "GLM-5.3"})
        self.assertIsNone(ocserve._model_for_session({"modelID": "no-provider"}))

    def test_prompt_frees_its_reader_thread_and_socket(self):
        """prompt() must not leak the SSE reader — one per prompt adds up.

        Measured on the rejected attempt: 11 prompts left 11 live ocserve-sse
        threads and 11 extra fds, because `conn.close()` does not release a
        reader parked in `resp.fp.readline()`.
        """
        client = self._create()

        def sse_threads():
            return [t for t in threading.enumerate()
                    if t.name == "ocserve-sse" and t.is_alive()]

        def open_fds():
            # Force collection first: a deferred finalizer closing a socket on
            # the NEXT return would otherwise be counted, and the process-wide
            # count moves for reasons unrelated to prompt() (measured in CI:
            # +2 over five prompts with no leak). The leak this guards against
            # is ONE fd PER PROMPT, so the assertion below is on the slope, not
            # on a hand-tuned absolute tolerance that a busy host can trip.
            gc.collect()
            return len(os.listdir("/proc/self/fd"))

        asyncio.run(client.prompt("hi"))          # warm any first-use handles
        before_threads, before_fds = len(sse_threads()), open_fds()
        # n is large on purpose: the leak is ONE fd per prompt, so it grows
        # with n while incidental process-wide churn does not. With n=5 and a
        # tolerance of 3, fds opened by other suites' background threads on a
        # busy host failed this intermittently (2026-09-24: 4 and 5 extra fds
        # under a full check.sh with a live fleet, 0 when run alone).
        n = 20
        for _ in range(n):
            asyncio.run(client.prompt("hi"))
        self.assertEqual(sse_threads(), [])
        self.assertEqual(len(sse_threads()), before_threads)
        # No per-prompt leak: growth must stay well below the prompt count.
        # The rejected attempt grew by exactly n (11 prompts, 11 fds).
        self.assertLess(open_fds() - before_fds, n // 2)

    def test_tokens_fold_when_total_is_absent(self):
        self.assertEqual(ocserve._token_total(
            {"input": 5, "output": 7, "reasoning": 0,
             "cache": {"read": 0, "write": 0}}), 12)

    def test_capacity_refusal_is_not_a_generic_error(self):
        client = self._create()
        with self.assertRaises(ocserve.CapacityFull) as ctx:
            asyncio.run(client.prompt("capacity!"))
        self.assertIn("concurrent session limit", str(ctx.exception))
        self.assertIsInstance(ctx.exception, ocserve.OcserveError)

    def test_other_session_error_carries_its_message(self):
        client = self._create()
        with self.assertRaises(ocserve.OcserveError) as ctx:
            asyncio.run(client.prompt("boom"))
        self.assertNotIsInstance(ctx.exception, ocserve.CapacityFull)
        self.assertIn("Model not found", str(ctx.exception))

    def test_dispose_posts_the_instance_route_for_this_directory(self):
        worktree = "/tmp/ocserve-dispose-me"
        client = self._create(worktree=worktree)
        asyncio.run(client.dispose())
        self.assertIn(worktree, self.fake.disposed)
        self.assertIn("instance.dispose", self.fake.hits)

    def test_directory_header_reaches_the_server_on_every_route(self):
        worktree = "/tmp/ocserve-header-check"
        client = self._create(worktree=worktree)
        asyncio.run(client.prompt("hello there"))
        asyncio.run(client.dispose())
        for route in ("session.create", "event", "prompt_async",
                      "instance.dispose"):
            self.assertIn(route, self.fake.directories, f"{route} never hit")
            self.assertEqual(self.fake.directories[route], worktree,
                             f"{route} lost x-opencode-directory")
        self.assertEqual(self.fake.sessions[client.session_id], worktree)

    def test_prompt_reaches_the_async_route_with_the_text(self):
        client = self._create()
        result = asyncio.run(client.prompt("write me a poem"))
        self.assertIn("echo: write me a poem", result.text)
        sent = self.fake.prompts[-1]
        self.assertEqual(sent["session"], client.session_id)
        self.assertEqual(sent["body"]["parts"][0]["text"], "write me a poem")

    def test_stream_that_ends_without_an_idle_is_an_error(self):
        client = self._create()
        # Frames arrive, then nothing more and no session.idle: the client must
        # not report a successful empty answer, it must fail loudly.
        self.fake.frames_for = lambda text: [
            {"type": "message.part.updated",
             "properties": {"part": {"type": "text", "text": "partial"}}}]
        with self.assertRaises(ocserve.OcserveError) as ctx:
            asyncio.run(client.prompt("hi", timeout=1.0))
        self.assertIn("timed out", str(ctx.exception))

    def test_a_siblings_traffic_does_not_reset_the_stall_clock(self):
        """`/event` is one stream shared by every session (ocserve.py:28-31).

        A silent session must stall even while a sibling keeps emitting frames
        on the SAME stream — otherwise a wedged reviewer never trips its idle
        budget (the live Union-Alpha hang: 20+ min past 840 s, no
        driver.stalled event, because the sibling's traffic kept the clock
        alive).
        """
        client = self._create()
        # This session sends only its prompt echo, then goes silent: no
        # session.idle for OUR session.
        self.fake.frames_for = lambda text: [
            {"type": "message.part.updated",
             "properties": {"part": {"type": "text", "text": "thinking..."}}}]
        sibling_frame = {"type": "message.part.updated",
                         "properties": {"part": {"type": "text", "text": "sib"}}}
        stop = asyncio.Event()

        async def chatty_sibling():
            # A frame every 0.2s — well inside the 0.5s stall window, so before
            # the fix every one of them re-armed OUR idle clock and the prompt
            # never stalled (it would instead sit until the 10s total budget).
            while not stop.is_set():
                self.fake.emit_sibling_frame(sibling_frame)
                try:
                    await asyncio.wait_for(stop.wait(), 0.2)
                except asyncio.TimeoutError:
                    pass

        async def run():
            sib = asyncio.ensure_future(chatty_sibling())
            try:
                return await client.prompt("stallme", timeout=10.0,
                                           stall_timeout=0.5)
            finally:
                stop.set()
                await sib

        with self.assertRaises(ocserve.StreamStalled) as ctx:
            asyncio.run(run())
        self.assertIn("stalled", str(ctx.exception))

    def test_our_own_frames_still_reset_the_stall_clock(self):
        """The complement: frames for OUR session are progress, so a stream
        that trickles its own parts must NOT spuriously stall."""
        client = self._create()
        self.fake.frames_for = lambda text: [
            {"type": "message.part.updated",
             "properties": {"part": {"type": "text", "text": "part1"}}}]

        async def run():
            task = asyncio.ensure_future(
                client.prompt("hi", timeout=10.0, stall_timeout=0.5))
            for _ in range(4):
                await asyncio.sleep(0.2)
                self.fake.emit_sibling_frame(
                    {"type": "message.part.updated",
                     "properties": {"part": {"type": "text", "text": "ours"}}},
                    session_id=client.session_id)
            self.fake.emit_sibling_frame(
                {"type": "session.idle", "properties": {}},
                session_id=client.session_id)
            return await task

        result = asyncio.run(run())
        self.assertIn("ours", result.text)

    def test_server_heartbeats_do_not_reset_the_stall_clock(self):
        """The session-less keepalive must not count as progress.

        Live on opencode 1.18.29 the `/event` stream carries
        `server.heartbeat` frames every ~60s with `properties: {}` (no
        sessionID). Counting them as progress is exactly how a wedged session
        (the live textkit-tokens GLM reviewer: silent from 15:38, no
        driver.stalled for 50+ min) escaped its 840s idle kill — so the clock
        must rest ONLY on frames whose sessionID is OUR session.
        """
        client = self._create()
        self.fake.frames_for = lambda text: [
            {"type": "message.part.updated",
             "properties": {"part": {"type": "text", "text": "thinking..."}}}]
        stop = asyncio.Event()

        async def heartbeats():
            while not stop.is_set():
                self.fake.emit_server_heartbeat()
                try:
                    await asyncio.wait_for(stop.wait(), 0.2)   # < 0.5s stall
                except asyncio.TimeoutError:
                    pass

        async def run():
            hb = asyncio.ensure_future(heartbeats())
            try:
                return await client.prompt("stallme", timeout=10.0,
                                           stall_timeout=0.5)
            finally:
                stop.set()
                await hb

        # The heartbeats arrive well inside the stall window; before the fix
        # each one re-armed the clock and the prompt never stalled (it sat to
        # the 10s total budget). Now it must raise StreamStalled.
        with self.assertRaises(ocserve.StreamStalled) as ctx:
            asyncio.run(run())
        self.assertIn("stalled", str(ctx.exception))

    def test_stream_closed_before_idle_is_an_error(self):
        client = self._create()

        async def run():
            task = asyncio.ensure_future(client.prompt("stallme", timeout=10.0))
            await asyncio.sleep(0.4)
            self.fake.release_all_streams()   # server closes the stream
            return await task

        self.fake.frames_for = lambda text: [
            {"type": "message.part.updated",
             "properties": {"part": {"type": "text", "text": "partial"}}}]
        with self.assertRaises(ocserve.OcserveError) as ctx:
            asyncio.run(run())
        self.assertIn("ended before the prompt finished", str(ctx.exception))

    def test_unknown_session_is_rejected_at_prompt_time(self):
        client = ocserve.OcserveClient(self.handle, "/tmp/ocserve-nosession",
                                       session_id="ses_unknown")
        with self.assertRaises(ocserve.OcserveError) as ctx:
            asyncio.run(client.prompt("hi"))
        self.assertIn("prompt_async failed: HTTP 404", str(ctx.exception))

    def test_prompt_without_a_session_is_rejected(self):
        client = ocserve.OcserveClient(self.handle, "/tmp/ocserve-nosession")
        with self.assertRaises(ocserve.OcserveError):
            asyncio.run(client.prompt("hi"))


if __name__ == "__main__":
    unittest.main()
