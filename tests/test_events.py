"""Tests for the events module.

These tests verify that the public API behaves correctly and that failures in
writing the event log never raise exceptions. The helper `capture_events`
captures calls to `events.emit` without touching the filesystem, while the
temporary file setup in `setUp`/`tearDown` ensures the real log is not
modified.
"""

import json
import os
import stat
import tempfile
import unittest

from helpers import capture_events  # noqa: F401  (sys.path + event log redirect)

import config
import events


class TestEventsEmit(unittest.TestCase):
    """Verify that `emit` writes a proper JSON line and never crashes.

    The event log is an append‑only JSONL file. Each line must contain a
    timestamp (`ts`) and a `type` field, plus any supplied context and custom
    fields. The orchestrator expects `emit` to swallow I/O errors – a failure
    to write must not abort the workload.
    """

    def setUp(self):
        # Save original path and point to a fresh temporary file.
        self._orig_log = config.EVENTS_LOG
        self._tmp_file = tempfile.NamedTemporaryFile(delete=False)
        self._tmp_file.close()
        config.EVENTS_LOG = self._tmp_file.name
        # Ensure file is empty.
        open(config.EVENTS_LOG, "w").close()

    def tearDown(self):
        # Restore original configuration and clean up.
        config.EVENTS_LOG = self._orig_log
        try:
            os.unlink(self._tmp_file.name)
        except OSError:
            pass

    def test_emit_writes_json_line_with_ts_and_type(self):
        events.emit("my_event", foo="bar")
        with open(config.EVENTS_LOG, "r", encoding="utf-8") as f:
            line = f.readline().strip()
        self.assertTrue(line, "log file should contain a line")
        data = json.loads(line)
        self.assertIn("ts", data)
        self.assertIsInstance(data["ts"], (int, float))
        self.assertEqual(data["type"], "my_event")
        self.assertEqual(data["foo"], "bar")

    def test_emit_appends_one_json_line_per_call(self):
        events.emit("first_event", n=1)
        events.emit("second_event", n=2)
        with open(config.EVENTS_LOG, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
        self.assertEqual(len(lines), 2, "each emit call should append exactly one line")
        first = json.loads(lines[0])
        second = json.loads(lines[1])
        self.assertEqual(first["type"], "first_event")
        self.assertEqual(second["type"], "second_event")

    def test_context_round_trip_and_event_carries_context(self):
        # Set a known context.
        events.set_context(workload="test", round=1, iteration=2, module="mod")
        # Verify context() returns the same mapping.
        ctx = events.context()
        # The contextvars are what this test is about. Asserting the exact dict
        # made it fail the moment a correlation id was added alongside them,
        # which is metadata, not context the caller set.
        self.assertEqual(
            {k: ctx[k] for k in ("workload", "round", "iteration", "module")},
            {"workload": "test", "round": 1, "iteration": 2, "module": "mod"})
        # Emit an event using the real implementation.
        events.emit("ctx_event")
        # Read the emitted line from the temporary log file.
        with open(config.EVENTS_LOG, "r", encoding="utf-8") as f:
            line = f.readline().strip()
        data = json.loads(line)
        self.assertEqual(data.get("workload"), "test")
        self.assertEqual(data.get("round"), 1)
        self.assertEqual(data.get("iteration"), 2)
        self.assertEqual(data.get("module"), "mod")

    def test_every_event_carries_a_run_id(self):
        """Without it there is no way to reassemble one run after the fact.

        A task's events are spread across the run process, its drivers and
        whatever reads them later; a shared per-process id is what turns a flat
        log back into "everything that happened in THAT run".
        """
        events.emit("a")
        events.emit("b")
        with open(config.EVENTS_LOG, encoding="utf-8") as f:
            rows = [json.loads(ln) for ln in f if ln.strip()]
        ids = {r.get("run_id") for r in rows}
        self.assertEqual(len(ids), 1, "events from one process must share a run_id")
        self.assertTrue(next(iter(ids)))

    def test_emit_never_raises_when_log_path_unwritable(self):
        # Create a read‑only directory.
        readonly_dir = tempfile.mkdtemp()
        os.chmod(readonly_dir, stat.S_IREAD | stat.S_IEXEC)
        # Point EVENTS_LOG inside the read‑only directory.
        bad_path = os.path.join(readonly_dir, "events.jsonl")
        config.EVENTS_LOG = bad_path
        try:
            # This should silently ignore the OSError.
            events.emit("unwritable_event")
        except Exception as e:
            self.fail(f"emit raised an exception on unwritable path: {e}")
        finally:
            # Clean up permissions so the directory can be removed.
            os.chmod(readonly_dir, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            try:
                os.rmdir(readonly_dir)
            except OSError:
                pass

    def test_emit_handles_non_json_serializable_values(self):
        class Dummy:
            def __str__(self):
                return "<Dummy>"
        dummy = Dummy()
        try:
            events.emit("non_serializable", obj=dummy)
        except Exception as e:
            self.fail(f"emit raised when given non‑serializable value: {e}")
        # Verify that the line was written and contains the string representation.
        with open(config.EVENTS_LOG, "r", encoding="utf-8") as f:
            data = json.loads(f.readline())
        self.assertEqual(data["obj"], str(dummy))


if __name__ == "__main__":
    unittest.main()
