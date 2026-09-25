"""The dashboard serves with the SAME token however it is started, never
hot-loops on a busy port, and answers /api/projects fast under polling.

2026-09-24/25: a copy started outside systemd (start.sh's nohup fallback,
reached through restart.sh after a 401 from its token-less .env lookup) held
port 8787 with actions UNLOCKED while arc-dashboard.service crash-looped 2700+
times on "port already in use"; after the next reboot the unit won and the
operator was suddenly asked for a token. Separately, /api/projects took ~10 s
under several pollers because every call re-read the 50 MB event log.
"""
import errno
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import capture_events  # noqa: F401  (sys.path + event redirect)

import config
import dashboard


class _Req(dashboard.Handler):
    def __init__(self, path, headers=None):
        self.path = path
        self.headers = headers or {}
        self.status, self.body, self.wfile = None, b"", self
        self.response_headers = {}

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, k, v):
        self.response_headers[k] = v

    def end_headers(self):
        pass

    def write(self, data):
        self.body += data


class TokenFromTheEnvFile(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.file = Path(self._dir.name) / "arc-dashboard.env"
        for name in ("DASHBOARD_TOKEN", "DASHBOARD_ENV_FILE"):
            self.addCleanup(setattr, config, name, getattr(config, name))
        config.DASHBOARD_ENV_FILE = str(self.file)
        config.DASHBOARD_TOKEN = ""
        env = {k: v for k, v in os.environ.items()
               if k != "ARC_DASHBOARD_TOKEN"}
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def test_a_copy_started_without_the_unit_env_loads_the_units_token(self):
        self.file.write_text("# systemd EnvironmentFile\nARC_DASHBOARD_TOKEN='s3cret'\n")
        self.assertIsNone(dashboard._ensure_token())
        self.assertEqual(config.DASHBOARD_TOKEN, "s3cret")
        self.assertEqual(os.environ["ARC_DASHBOARD_TOKEN"], "s3cret",
                         "exported so a graceful re-exec keeps it")

    def test_an_env_file_without_a_token_refuses_to_serve_unlocked(self):
        self.file.write_text("ARC_DASHBOARD_TOKEN=\n")
        why = dashboard._ensure_token()
        self.assertIn("refusing to serve with actions unlocked", why)
        with mock.patch.object(config, "DASHBOARD_ALLOW_OPEN", True):
            self.assertIsNone(dashboard._ensure_token())

    def test_an_unreadable_env_file_refuses(self):
        self.file.write_text("ARC_DASHBOARD_TOKEN=x\n")
        with mock.patch.object(Path, "read_text", side_effect=PermissionError):
            self.assertIn("unreadable", dashboard._ensure_token())

    def test_no_env_file_and_no_token_is_the_operators_choice(self):
        self.assertIsNone(dashboard._ensure_token())
        self.assertEqual(config.DASHBOARD_TOKEN, "")

    def test_serve_exits_78_rather_than_serving_unlocked(self):
        self.file.write_text("OTHER=1\n")
        with self.assertRaises(SystemExit) as cm, mock.patch("builtins.print"):
            dashboard.serve(port=1)
        self.assertEqual(cm.exception.code, dashboard.EXIT_TOKEN_UNREADABLE)

    def test_graceful_reexec_passes_the_environment(self):
        os.environ["ARC_DASHBOARD_TOKEN"] = "s3cret"
        with mock.patch.object(dashboard.os, "execve") as ex, \
                mock.patch.object(dashboard.logging, "shutdown"):
            dashboard._graceful_reexec()
        env = ex.call_args.args[2]
        self.assertEqual(env["ARC_DASHBOARD_TOKEN"], "s3cret")


class AuthRoute(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, config, "DASHBOARD_TOKEN", config.DASHBOARD_TOKEN)

    def _get(self, headers=None):
        r = _Req("/api/auth", headers)
        r.do_GET()
        return r.status, json.loads(r.body)

    def test_reports_lock_state_without_echoing_the_token(self):
        config.DASHBOARD_TOKEN = ""
        self.assertEqual(self._get()[1], {"required": False, "ok": True})
        config.DASHBOARD_TOKEN = "s3cret"
        self.assertEqual(self._get()[1], {"required": True, "ok": False})
        st, body = self._get({"Authorization": "Bearer wrong"})
        self.assertEqual(body["ok"], False)
        st, body = self._get({"Authorization": "Bearer s3cret"})
        self.assertEqual((st, body), (200, {"required": True, "ok": True}))


class PortConflict(unittest.TestCase):
    def _busy(self):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        self.addCleanup(s.close)
        return s.getsockname()[1]

    def test_a_busy_port_exits_98_and_names_the_holder(self):
        port = self._busy()
        with mock.patch.object(config, "DASHBOARD_BIND_WAIT", 0.0), \
                mock.patch("builtins.print") as out, \
                self.assertRaises(SystemExit) as cm:
            dashboard._bind("127.0.0.1", port)
        self.assertEqual(cm.exception.code, dashboard.EXIT_PORT_IN_USE)
        said = " ".join(str(c.args[0]) for c in out.call_args_list)
        self.assertIn(f"pid {os.getpid()}", said, "the holder is identified")

    def test_holder_lookup_finds_this_process(self):
        port = self._busy()
        pid, argv, cwd = dashboard._port_holder(port)
        self.assertEqual(pid, os.getpid())

    def test_a_brief_overlap_is_waited_out(self):
        port = self._busy()
        calls = []

        def fake(addr, handler):
            calls.append(addr)
            if len(calls) < 3:
                raise OSError(errno.EADDRINUSE, "busy")
            return "server"
        with mock.patch.object(dashboard, "ThreadingHTTPServer", fake), \
                mock.patch.object(dashboard.time, "sleep"), \
                mock.patch.object(config, "DASHBOARD_BIND_WAIT", 30.0):
            self.assertEqual(dashboard._bind("127.0.0.1", port), "server")
        self.assertEqual(len(calls), 3)

    def test_the_unit_takes_over_only_a_stray_copy(self):
        stray = (4242, ["/x/.venv/bin/python", "main.py", "serve", "--port", "8787"], "/x")
        other = (4243, ["nginx"], "/")
        with mock.patch.object(dashboard, "_in_unit", return_value=False), \
                mock.patch.object(dashboard.os, "stat") as st, \
                mock.patch.object(dashboard.os, "getuid", return_value=1000):
            st.return_value.st_uid = 1000
            self.assertTrue(dashboard._is_stray_dashboard(stray))
            self.assertFalse(dashboard._is_stray_dashboard(other))
        with mock.patch.object(dashboard, "_in_unit", return_value=True):
            self.assertFalse(dashboard._is_stray_dashboard(stray),
                             "the unit's own process is never a stray")

        attempts = []

        def fake(addr, handler):
            attempts.append(addr)
            if len(attempts) == 1:
                raise OSError(errno.EADDRINUSE, "busy")
            return "server"
        env = {"INVOCATION_ID": "abc"}
        with mock.patch.object(dashboard, "ThreadingHTTPServer", fake), \
                mock.patch.object(dashboard, "_port_holder", return_value=stray), \
                mock.patch.object(dashboard, "_is_stray_dashboard", return_value=True), \
                mock.patch.object(dashboard.os, "kill") as kill, \
                mock.patch.object(dashboard.time, "sleep"), \
                mock.patch("builtins.print"), \
                mock.patch.object(config, "DASHBOARD_TAKEOVER", True), \
                mock.patch.object(config, "DASHBOARD_BIND_WAIT", 0.0), \
                mock.patch.dict(os.environ, env):
            self.assertEqual(dashboard._bind("0.0.0.0", 8787), "server")
        kill.assert_called_once_with(4242, dashboard.signal.SIGTERM)

    def test_without_the_unit_nothing_is_killed(self):
        port = self._busy()
        env_now = {k: v for k, v in os.environ.items() if k != "INVOCATION_ID"}
        with mock.patch.dict(os.environ, env_now, clear=True), \
                mock.patch.object(config, "DASHBOARD_TAKEOVER", True), \
                mock.patch.object(config, "DASHBOARD_BIND_WAIT", 0.0), \
                mock.patch.object(dashboard.os, "kill") as kill, \
                mock.patch("builtins.print"), self.assertRaises(SystemExit):
            dashboard._bind("127.0.0.1", port)
        kill.assert_not_called()


class ClientsThatLeave(unittest.TestCase):
    def test_a_reader_that_timed_out_is_not_a_handler_error(self):
        r = _Req("/api/health")
        with mock.patch.object(dashboard, "_health", side_effect=TimeoutError(110, "t")), \
                mock.patch.object(dashboard.log, "exception") as logged:
            r.do_GET()
        logged.assert_not_called()


class IncrementalEventLog(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.addCleanup(setattr, config, "EVENTS_LOG", config.EVENTS_LOG)
        config.EVENTS_LOG = str(Path(self._dir.name) / "events.jsonl")
        dashboard._lines_cache["key"] = None
        self.addCleanup(dashboard._lines_cache.__setitem__, "key", None)

    def _append(self, text):
        with open(config.EVENTS_LOG, "a", encoding="utf-8") as fh:
            fh.write(text)
        # a distinct mtime even on a coarse clock
        st = os.stat(config.EVENTS_LOG)
        os.utime(config.EVENTS_LOG, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    def test_append_reads_only_new_bytes_and_keeps_order(self):
        self._append('{"type":"a"}\n{"type":"b"}\n')
        self.assertEqual(len(dashboard._load_event_lines()), 2)
        gen = dashboard._lines_cache["gen"]
        self._append('{"type":"c"}\n')
        lines = dashboard._load_event_lines()
        self.assertEqual([json.loads(x)["type"] for x in lines], ["a", "b", "c"])
        self.assertEqual(dashboard._lines_cache["gen"], gen, "an append is not a reload")

    def test_a_half_written_line_is_returned_but_not_committed(self):
        self._append('{"type":"a"}\n{"type":')
        lines = dashboard._load_event_lines()
        self.assertEqual(len(lines), 2)
        _l, n_complete, _g = dashboard._event_lines_state()
        self.assertEqual(n_complete, 1)
        self._append('"b"}\n')
        self.assertEqual([json.loads(x)["type"] for x in dashboard._load_event_lines()],
                         ["a", "b"])

    def test_rotation_or_rewrite_reloads(self):
        self._append('{"type":"a"}\n{"type":"b"}\n')
        dashboard._load_event_lines()
        gen = dashboard._lines_cache["gen"]
        os.replace(config.EVENTS_LOG, config.EVENTS_LOG + ".1")    # rotation
        self._append('{"type":"z"}\n')
        self.assertEqual([json.loads(x)["type"] for x in dashboard._load_event_lines()], ["z"])
        self.assertNotEqual(dashboard._lines_cache["gen"], gen)

    def test_the_inflight_fold_matches_a_full_rescan(self):
        ev = lambda t, **k: json.dumps({"type": t, "ts": time.time(), "harness": "h",
                                        "model": "M", "role": "implementer",
                                        "task": "t1", "attempt": 1, **k}) + "\n"
        self._append(ev("driver.start"))
        st = dashboard._INFLIGHT_FOLD.get()
        self.assertEqual(len(st["driver_starts"]), 1)
        self._append(ev("driver.heartbeat") + ev("driver.done"))
        st = dashboard._INFLIGHT_FOLD.get()
        self.assertEqual(st["driver_starts"], {})
        self.assertEqual(st["driver_last"], {})


class ProjectsSingleFlight(unittest.TestCase):
    def setUp(self):
        dashboard._projects_invalidate()
        self.addCleanup(dashboard._projects_invalidate)

    def test_concurrent_pollers_share_one_computation(self):
        calls = []
        gate = threading.Event()

        def slow(store):
            calls.append(1)
            gate.wait(2)
            return [{"file": "p.json"}]
        store = object()
        out = []
        with mock.patch.object(dashboard, "_projects", slow):
            ts = [threading.Thread(target=lambda: out.append(
                dashboard._projects_cached(store, ttl=60))) for _ in range(5)]
            for t in ts:
                t.start()
            time.sleep(0.2)
            gate.set()
            for t in ts:
                t.join()
            self.assertEqual(len(calls), 1, "five pollers, one walk of the log")
            self.assertEqual(out, [[{"file": "p.json"}]] * 5)
            dashboard._projects_invalidate()          # a POST changed a project
            dashboard._projects_cached(store, ttl=60)
            self.assertEqual(len(calls), 2)

    def test_the_ttl_expires(self):
        n = []
        with mock.patch.object(dashboard, "_projects", lambda s: n.append(1) or []):
            dashboard._projects_cached("s", ttl=0)
            dashboard._projects_cached("s", ttl=0)
        self.assertEqual(len(n), 2)


if __name__ == "__main__":
    unittest.main()
