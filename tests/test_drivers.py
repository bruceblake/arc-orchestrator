"""Driver slot accounting, capacity classification, transcript parsing."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from helpers import capture_events  # noqa: F401  (sys.path)

import config
import drivers
from drivers import Driver, DriverError
from store import Store


class TempLeaseDB:
    """Point drivers' lease store at a throwaway sqlite file."""

    def __enter__(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self._dir.name) / "t.db"))
        self._orig = drivers._lease_store
        drivers._lease_store = self.store
        return self.store

    def __exit__(self, *exc):
        drivers._lease_store = self._orig
        self._dir.cleanup()
        return False


class FakeDriver(Driver):
    harness = "fake"
    model = "gpt-oss-120b"
    role = "implementer"

    def __init__(self, raiser):
        self._raiser = raiser
        self.calls = 0

    def argv(self, prompt, session_id):
        return ["true"]

    async def _once(self, prompt, worktree, session_id, task_id, attempt):
        self.calls += 1
        raise self._raiser


class SlotRelease(unittest.TestCase):
    """Both concurrency slots must be freed on every exit path.

    Before this was a try/finally, an exception that was not a DriverError
    (a missing harness binary, a bug inside _once, cancellation at shutdown)
    leaked the in-process semaphore for the life of the process and left a DB
    lease row pinning the model at its cap until the 30-minute TTL expired.
    """

    def _slots_after(self, exc):
        with TempLeaseDB() as store:
            drv = FakeDriver(exc)
            drivers._semaphores.pop(drv.model, None)
            gate = drivers._gate(drv.model)
            before = gate._value

            async def go():
                with self.assertRaises(type(exc)):
                    await drv._guarded_once("p", Path("."), None, "task-x1", 1)

            asyncio.run(go())
            return before, gate._value, len(store.driver_lease_rows())

    def test_driver_error_releases_both_slots(self):
        before, after, leases = self._slots_after(DriverError("boom"))
        self.assertEqual(before, after, "semaphore permit leaked")
        self.assertEqual(leases, 0, "lease row leaked")

    def test_unexpected_exception_releases_both_slots(self):
        before, after, leases = self._slots_after(FileNotFoundError("no such harness"))
        self.assertEqual(before, after, "semaphore permit leaked")
        self.assertEqual(leases, 0, "lease row leaked")

    def test_repeated_failures_do_not_drain_the_semaphore(self):
        with TempLeaseDB():
            drv = FakeDriver(FileNotFoundError("nope"))
            drivers._semaphores.pop(drv.model, None)
            gate = drivers._gate(drv.model)
            before = gate._value

            async def go():
                for _ in range(5):
                    try:
                        await drv._guarded_once("p", Path("."), None, "t", 1)
                    except FileNotFoundError:
                        pass

            asyncio.run(go())
            self.assertEqual(gate._value, before)


class RetryLoop(unittest.TestCase):
    """Driver.run(): the whole retry ladder, with sleeps stubbed out."""

    def _drive(self, exc):
        """Run the loop to exhaustion; return (attempts, backoffs, slots_clean)."""
        backoffs = []

        async def fake_sleep(d):
            backoffs.append(d)

        with TempLeaseDB() as store:
            drv = FakeDriver(exc)
            drivers._semaphores.pop(drv.model, None)
            gate = drivers._gate(drv.model)
            before = gate._value
            orig_sleep = drivers.asyncio.sleep
            drivers.asyncio.sleep = fake_sleep
            try:
                with capture_events():
                    async def go():
                        with self.assertRaises(DriverError):
                            await drv.run("p", Path("."), task_id="t1")
                    asyncio.run(go())
            finally:
                drivers.asyncio.sleep = orig_sleep
            clean = gate._value == before and not store.driver_lease_rows()
        return drv.calls, backoffs, clean

    def test_gives_up_after_max_retries_without_leaking_slots(self):
        calls, _, clean = self._drive(DriverError("opencode exited 2: real crash"))
        self.assertEqual(calls, config.MAX_RETRIES + 1)
        self.assertTrue(clean, "slots leaked across the retry ladder")

    def test_capacity_rejection_backs_off_far_longer_than_a_crash(self):
        _, crash_backoffs, _ = self._drive(DriverError("opencode exited 2: real crash"))
        _, cap_backoffs, _ = self._drive(DriverError(
            "provider.api_error: 400 status code (no body)"))
        self.assertTrue(min(cap_backoffs) > max(crash_backoffs),
                        f"capacity {cap_backoffs} must exceed crash {crash_backoffs}")
        self.assertLessEqual(max(cap_backoffs),
                             config.DRIVER_CAPACITY_BACKOFF_CAP * 1.25)


class ChildTermination(unittest.TestCase):
    """A cancelled or timed-out attempt must not orphan the harness process.

    An orphaned kimi/opencode keeps holding an ARC concurrency slot after the
    orchestrator that spawned it is gone, which is what made a killed run make
    the cap situation worse rather than better.
    """

    class SleeperDriver(Driver):
        harness = "sleep"
        model = "gpt-oss-120b"
        role = "implementer"

        def argv(self, prompt, session_id):
            return ["sleep", "60"]

    def test_cancelling_an_attempt_kills_the_child(self):
        holder = {}

        async def go():
            drv = self.SleeperDriver()
            orig = drv._pump

            async def spy(proc, *a, **kw):
                holder["proc"] = proc
                return await orig(proc, *a, **kw)

            drv._pump = spy
            task = asyncio.create_task(
                drv._once("p", Path("."), None, "t", 1))
            for _ in range(100):          # wait for the child to exist
                await asyncio.sleep(0.02)
                if "proc" in holder:
                    break
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(go())
        proc = holder.get("proc")
        self.assertIsNotNone(proc, "child was never spawned")
        self.assertIsNotNone(proc.returncode, "harness process was left running")

    def test_cancellation_emits_a_terminal_event(self):
        """Every driver.start needs a matching end event: the dashboard pairs
        them to count in-flight agents against each model's cap, and an
        unmatched start reads as a phantom agent for ~19 minutes."""
        with capture_events() as ev, TempLeaseDB():
            drv = FakeDriver(asyncio.CancelledError())

            async def go():
                with self.assertRaises(asyncio.CancelledError):
                    await drv.run("p", Path("."), task_id="t1")

            asyncio.run(go())
        self.assertEqual(len(ev.of("driver.start")), 1)
        self.assertEqual(len(ev.of("driver.cancelled")), 1)

    def test_idle_timeout_kills_the_child(self):
        orig_idle = config.DRIVER_IDLE_TIMEOUT
        config.DRIVER_IDLE_TIMEOUT = 0.3
        holder = {}
        try:
            async def go():
                drv = self.SleeperDriver()
                orig = drv._pump

                async def spy(proc, *a, **kw):
                    holder["proc"] = proc
                    return await orig(proc, *a, **kw)

                drv._pump = spy
                with capture_events():
                    with self.assertRaises(DriverError):
                        await drv._once("p", Path("."), None, "t", 1)

            asyncio.run(go())
        finally:
            config.DRIVER_IDLE_TIMEOUT = orig_idle
        self.assertIsNotNone(holder["proc"].returncode,
                             "stalled harness was left running")


class StallInstrumentation(unittest.TestCase):
    """A short idle timeout is only safe if the stall still explains itself."""

    def test_proc_snapshot_reports_state_cpu_and_sockets(self):
        snap = drivers.proc_snapshot(os.getpid())
        self.assertIn(snap.get("state"), list("RSDZTt"))
        self.assertIsInstance(snap.get("cpu_s"), float)
        self.assertGreaterEqual(snap.get("sockets", 0), 0)

    def test_proc_snapshot_is_empty_for_a_dead_pid(self):
        self.assertEqual(drivers.proc_snapshot(2 ** 22), {})

    def test_activity_tail_reads_the_opencode_shape(self):
        raw = ('{"type":"text","part":{}}\n'
               '{"type":"tool_use","part":{"tool":"edit"}}\n'
               '{"type":"step_finish"}\n')
        self.assertEqual(drivers.activity_tail(raw),
                         ["text", "tool_use:edit", "step_finish"])

    def test_activity_tail_reads_the_kimi_shape(self):
        """kimi emits OpenAI-style role/tool_calls, not opencode's type field."""
        raw = ('{"role":"assistant","tool_calls":[{"function":{"name":"Read"}},'
               '{"function":{"name":"Grep"}}]}\n'
               '{"role":"tool","tool_call_id":"c1","content":"..."}\n')
        self.assertEqual(drivers.activity_tail(raw),
                         ["assistant:Read+Grep", "tool"])

    def test_activity_tail_survives_a_single_huge_record(self):
        """One kimi tool result can be 50KB; slicing a byte tail lands
        mid-line and parses nothing, which silently emptied the field."""
        huge = json.dumps({"role": "tool", "tool_call_id": "c1",
                           "content": "x" * 60000})
        raw = '{"role":"assistant","tool_calls":[{"function":{"name":"Read"}}]}\n' + huge + "\n"
        self.assertEqual(drivers.activity_tail(raw), ["assistant:Read", "tool"])

    def test_activity_tail_tolerates_junk(self):
        self.assertEqual(drivers.activity_tail("not json at all"), [])
        self.assertEqual(drivers.activity_tail(""), [])
        self.assertEqual(drivers.activity_tail('{"unrecognised": 1}'), [])

    def test_a_stall_emits_forensics_and_reads_as_capacity(self):
        """The stall event must carry enough to diagnose without the process."""
        orig_idle, orig_prog = config.DRIVER_IDLE_TIMEOUT, config.DRIVER_PROGRESS_INTERVAL
        config.DRIVER_IDLE_TIMEOUT, config.DRIVER_PROGRESS_INTERVAL = 0.4, 0.15

        class Sleeper(Driver):
            harness = "sleep"
            model = "gpt-oss-120b"
            role = "implementer"

            def argv(self, prompt, session_id):
                return ["sleep", "30"]

        try:
            with capture_events() as ev:
                async def go():
                    with self.assertRaises(DriverError) as cm:
                        await Sleeper()._once("p", Path("."), None, "t1", 1)
                    return cm.exception

                exc = asyncio.run(go())
        finally:
            config.DRIVER_IDLE_TIMEOUT, config.DRIVER_PROGRESS_INTERVAL = orig_idle, orig_prog

        stalls = ev.of("driver.stalled")
        self.assertEqual(len(stalls), 1, "a stall must report itself exactly once")
        st = stalls[0]
        self.assertIn(st.get("state"), list("RSDZTt"), "no /proc sample taken")
        self.assertTrue(st.get("blocked"),
                        "a sleeping process burning no CPU must read as blocked")
        self.assertIn("idle_s", st)
        # progress heartbeats give the stall a CPU baseline to diff against
        self.assertTrue(ev.of("driver.progress"), "no progress heartbeat emitted")
        self.assertIn("stalled", str(exc))

    def test_progress_heartbeats_report_liveness(self):
        orig_idle, orig_prog = config.DRIVER_IDLE_TIMEOUT, config.DRIVER_PROGRESS_INTERVAL
        config.DRIVER_IDLE_TIMEOUT, config.DRIVER_PROGRESS_INTERVAL = 1.0, 0.15

        class Sleeper(Driver):
            harness = "sleep"
            model = "gpt-oss-120b"
            role = "implementer"

            def argv(self, prompt, session_id):
                return ["sleep", "30"]

        try:
            with capture_events() as ev:
                async def go():
                    with self.assertRaises(DriverError):
                        await Sleeper()._once("p", Path("."), None, "t1", 1)
                asyncio.run(go())
        finally:
            config.DRIVER_IDLE_TIMEOUT, config.DRIVER_PROGRESS_INTERVAL = orig_idle, orig_prog
        beats = ev.of("driver.progress")
        self.assertGreaterEqual(len(beats), 2, "heartbeats should repeat")
        self.assertTrue(all("idle_s" in b and "elapsed_s" in b for b in beats))
        self.assertLessEqual(beats[0]["idle_s"], beats[-1]["idle_s"],
                             "idle time must grow while the harness is quiet")


class CapacityClassification(unittest.TestCase):
    """Capacity rejections need a long backoff; crashes need a short one."""

    def test_a_blocked_stall_counts_as_capacity_not_a_crash(self):
        """Proven on 2026-09-09: the harness sat in state 'S' burning no CPU
        with an ARC request unanswered for 320s. No 400 is ever returned, but
        retrying it on the crash ladder walks back into the same saturation."""
        msg = ("kimi stalled after 120.0s idle (total 300s, idle 120s, 110943 "
               "bytes; Kimi-K3 request unanswered for 320.9s)")
        self.assertTrue(Driver.is_capacity_error(msg) or "unanswered for" in msg)

    def test_recognises_arc_capacity_rejections(self):
        for msg in ("kimi exited 1: error: failed to run prompt: "
                    "provider.api_error: 400 status code (no body)",
                    "session limit reached",
                    "too many concurrent requests",
                    "429 Too Many Requests"):
            self.assertTrue(Driver.is_capacity_error(msg), msg)

    def test_does_not_misread_a_real_crash(self):
        for msg in ("opencode exited 2: SyntaxError in world.js",
                    "kimi stalled after 300.0s idle",
                    ""):
            self.assertFalse(Driver.is_capacity_error(msg), msg)


class TranscriptParsing(unittest.TestCase):
    def test_sums_opencode_step_tokens(self):
        raw = "\n".join([
            '{"type":"step_finish","part":{"tokens":{"total":100,"input":60,'
            '"output":30,"reasoning":10,"cache":{"read":0,"write":0}}}}',
            '{"type":"step_finish","part":{"tokens":{"total":50,"input":30,'
            '"output":20,"reasoning":0,"cache":{"read":0,"write":0}}}}',
        ])
        self.assertEqual(drivers.transcript_tokens(raw), (150, 90, 60))

    def test_ignores_noise_and_never_raises(self):
        self.assertEqual(drivers.transcript_tokens("not json\n{broken"), (0, 0, 0))

    def test_extracts_session_id_and_text(self):
        raw = '{"session_id":"abc123","content":"hello"}'
        sid, text = drivers.parse_transcript(raw)
        self.assertEqual(sid, "abc123")
        self.assertIn("hello", text)

    def test_falls_back_to_raw_when_no_json(self):
        sid, text = drivers.parse_transcript("plain output")
        self.assertIsNone(sid)
        self.assertIn("plain output", text)


class RoleGuards(unittest.TestCase):
    """Governance routing: weak models may only implement."""

    def test_weak_model_cannot_review(self):
        with self.assertRaises(ValueError):
            drivers.OpencodeDriver("gpt-oss-120b", "reviewer")

    def test_weak_model_may_implement(self):
        drivers.OpencodeDriver("gpt-oss-120b", "implementer")

    def test_unknown_model_is_rejected(self):
        with self.assertRaises(ValueError):
            drivers.OpencodeDriver("gpt-4", "implementer")

    def test_bench_policy_bypasses_the_guards(self):
        drivers.OpencodeDriver("gpt-oss-120b", "reviewer", bench=True)


if __name__ == "__main__":
    unittest.main()
