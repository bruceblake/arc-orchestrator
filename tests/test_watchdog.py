"""Watchdog tests: /api/health must tell IDLE from STALLED and name the blocker.

A fleet wedged at its concurrency cap emits heartbeats (driver.cap_wait)
forever, so every surface that counts "activity" stays green while nothing
completes. These tests pin the watchdog contract: `progress` ignores
heartbeats and only advances on real work (node_end / task.gate /
task.merged / driver.done); a threshold breach with work in flight is
STALLED and carries an evidence-based diagnosis naming the binding
resource; and an empty fleet is IDLE — calling idle a stall is the false
alarm that gets a watchdog muted.
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from helpers import capture_events  # noqa: F401  (sys.path + event redirect)

import config
import dashboard
from store import Store


class _Store:
    """Minimal store double: only what _queue/_watchdog read."""

    def __init__(self, leases=(), running=()):
        self._leases = list(leases)
        self._running = list(running)

    def driver_lease_rows(self):
        return self._leases

    def running_code_tasks(self):
        return self._running


class _FakeHandler(dashboard.Handler):
    """A socket-less Handler that captures status/headers/body in memory.

    BaseHTTPRequestHandler.__init__ requires a live socket; we skip it and
    set only what do_GET reads, then override the write path so nothing is
    sent over the network.
    """

    def __init__(self):
        self.status = None
        self.response_headers = {}
        self.body = b""
        self.wfile = self
        self.path = "/"

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass

    def write(self, data):
        self.body += data


class WatchdogCase(unittest.TestCase):
    """Temp event log + 60s stall threshold; each test builds its own fleet."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)
        self._orig_log = config.EVENTS_LOG
        config.EVENTS_LOG = str(self.tmp / "events.jsonl")
        dashboard._lines_cache["key"] = None
        self._orig_threshold = dashboard.WATCHDOG_STALL_S
        dashboard.WATCHDOG_STALL_S = 60.0
        self.addCleanup(self._restore)

    def _restore(self):
        config.EVENTS_LOG = self._orig_log
        dashboard._lines_cache["key"] = None
        dashboard.WATCHDOG_STALL_S = self._orig_threshold
        self._dir.cleanup()

    def _write(self, *events_):
        with open(config.EVENTS_LOG, "a", encoding="utf-8") as f:
            f.write("".join(json.dumps(e) + "\n" for e in events_))
        # the (size, mtime) cache must not serve pre-write lines
        dashboard._lines_cache["key"] = None

    def _ev(self, type_, task="t1", model="GLM-5.3", age=5, **kw):
        e = {"type": type_, "task": task, "model": model, "attempt": 1,
             "pid": os.getpid(), "ts": time.time() - age}
        e.update(kw)
        return e

    def _lease(self, model, task, age=5, pid=None):
        return {"id": 1, "model": model, "task": task,
                "pid": pid or os.getpid(),
                "acquired_at": time.time() - age}

    def _wd(self, store, live=None):
        return dashboard._watchdog(store, live or [])


class RecentProgressIsNotStalled(WatchdogCase):
    """A long implement node is normal work, not a stall.

    The threshold sits above DRIVER_TIMEOUT precisely so an in-flight driver
    never trips it; if recent completions still read as stalled, the alarm
    fires on healthy runs and gets ignored.
    """

    def test_recent_node_end_keeps_fleet_moving(self):
        self._write(self._ev("node_end", task="t0", age=10))
        store = _Store(leases=[self._lease("GLM-5.3", "h0")])
        w = self._wd(store)
        self.assertFalse(w["stalled"])
        self.assertEqual(w["state"], "moving")
        self.assertTrue(w["progress"])
        self.assertGreaterEqual(w["stalled_for_s"], 5)
        self.assertLess(w["stalled_for_s"], 60)


class HeartbeatsAloneAreAStall(WatchdogCase):
    """cap_wait re-fires roughly once a minute forever.

    If heartbeats counted as progress, a fleet wedged at its cap would read
    healthy indefinitely — this is the exact failure the watchdog exists for,
    so a log containing ONLY heartbeats must read as stalled, with the
    diagnosis naming the cap.
    """

    def test_cap_wait_only_log_reads_stalled_at_cap(self):
        cap = config.driver_limit("GLM-5.3")
        leases = [self._lease("GLM-5.3", f"h{i}") for i in range(cap)]
        self._write(self._ev("driver.cap_wait", task="t1", model="GLM-5.3",
                             age=300, in_use=cap, cap=cap))
        w = self._wd(_Store(leases=leases))
        self.assertTrue(w["stalled"])
        self.assertEqual(w["state"], "stalled")
        self.assertIn("at its cap", w["diagnosis"])


class EmptyFleetIsIdleNotStalled(WatchdogCase):
    """A fleet with no work in flight is healthy.

    Reporting idle as stalled is the false alarm that gets a watchdog muted,
    so no events and no work must read IDLE — including a stale wait left
    behind by a killed run, which _queue drops once its pid is gone.
    """

    def test_no_events_no_work_is_idle(self):
        w = self._wd(_Store())
        self.assertEqual(w["state"], "idle")
        self.assertFalse(w["stalled"])
        self.assertIsNone(w["stalled_for_s"])
        self.assertIn("nothing to do", w["diagnosis"])

    def test_stale_wait_from_dead_process_is_idle(self):
        self._write(self._ev("driver.cap_wait", task="t1", age=9999,
                             pid=999999))
        w = self._wd(_Store())
        self.assertEqual(w["state"], "idle")
        self.assertFalse(w["stalled"])


class SaturatedHarnessNamed(WatchdogCase):
    """The shared harness pool saturates while model caps still have room.

    That is the one ceiling operators forget (AGENTS.md Rule 6): opencode's
    models each sit under their own cap while the single local process pool
    is full. The diagnosis must name the harness, not a model.
    """

    def test_harness_saturation_named(self):
        hcap = config.harness_limit("opencode")
        leases = [self._lease("harness:opencode", f"hh{i}")
                  for i in range(hcap)]
        self._write(self._ev("driver.cap_wait", task="t1", model="GLM-5.3",
                             age=300, scope="harness", harness="opencode"))
        w = self._wd(_Store(leases=leases))
        self.assertTrue(w["stalled"])
        self.assertIn("opencode", w["diagnosis"])
        self.assertIn("saturated", w["diagnosis"])


class DeadRunDiagnosed(WatchdogCase):
    """A killed run process leaves task rows marked running forever.

    No cap change fixes that — the only remedy is resuming the taskfile —
    so when nothing runs, nothing waits, no process is alive, but rows say
    running, the diagnosis must say the run process is dead.
    """

    def test_running_rows_without_live_process_is_dead(self):
        self._write(self._ev("node_end", task="t0", age=300))
        store = _Store(running=[{"task": "t1", "status": "running"}])
        w = self._wd(store, live=[])
        self.assertTrue(w["stalled"])
        self.assertIn("dead", w["diagnosis"])
        self.assertIn("1 task", w["diagnosis"])


class QueuedButNoneStarted(WatchdogCase):
    """Process-scope semaphores can queue drivers with no cap full.

    No lease is held and no cap is saturated, so neither the model rows nor
    the harness row explains the silence — the diagnosis must say the
    drivers are queued and none has started.
    """

    def test_all_queued_none_started(self):
        self._write(self._ev("driver.slot_wait", task="t1", model="GLM-5.3",
                             age=300),
                    self._ev("driver.slot_wait", task="t2", model="Kimi-K3",
                             age=290))
        live = [{"pid": os.getpid(), "taskfile": "/tmp/x.json"}]
        w = self._wd(_Store(), live=live)
        self.assertTrue(w["stalled"])
        self.assertIn("none has started", w["diagnosis"])


class HealthEndpointCarriesWatchdog(WatchdogCase):
    """The watchdog is only useful on the screen the operator watches.

    /api/health is what the Projects page polls, so the verdict must ride
    along as a nested object with a stable key set — a missing key renders
    a blank strip, which is indistinguishable from healthy.
    """

    def setUp(self):
        super().setUp()
        self._orig_handler_store = dashboard.Handler.store
        dashboard.Handler.store = Store(":memory:")
        self.addCleanup(self._restore_handler)

    def _restore_handler(self):
        dashboard.Handler.store.conn.close()
        dashboard.Handler.store = self._orig_handler_store

    def test_health_has_watchdog_block(self):
        handler = _FakeHandler()
        handler.path = "/api/health"
        handler.do_GET()
        self.assertEqual(handler.status, 200)
        body = json.loads(handler.body.decode("utf-8"))
        self.assertIn("watchdog", body)
        w = body["watchdog"]
        for key in ("progress", "stalled_for_s", "stalled", "diagnosis"):
            self.assertIn(key, w)
        # live_runs() reads real /proc, so `state` depends on whatever runs on
        # this machine right now — value semantics are pinned by the unit
        # tests above. `stalled` is deterministic here: the redirected log is
        # empty, so there is no progress ts to breach any threshold.
        self.assertFalse(w["stalled"])
        self.assertIn(w["state"], ("idle", "moving", "stalled"))
        self.assertIsInstance(w["diagnosis"], str)


if __name__ == "__main__":
    unittest.main()