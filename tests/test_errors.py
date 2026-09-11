"""Structured exception capture: keep the evidence, and group it by cause.

Before this, every catch site reduced its exception to `str(exc)[:300]`. That
says an error happened and nothing about where — no file, no line, no frame —
so debugging a fleet failure meant guessing which of several call paths
produced a message like "opencode exited 1:".
"""
import os
import tempfile
import unittest

from helpers import capture_events  # noqa: F401  (sys.path)

import config
import errors


class ErrorCase(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.mktemp(suffix=".db")
        self._orig = config.DB_PATH
        config.DB_PATH = self.db
        errors.reset_for_tests()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        errors.reset_for_tests()
        config.DB_PATH = self._orig
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db + suffix)
            except OSError:
                pass

    def _raise(self, exc):
        try:
            raise exc
        except Exception as e:  # noqa: BLE001 - capturing is the point
            return e


class TheTracebackSurvives(ErrorCase):
    def test_the_frames_are_kept_not_just_the_message(self):
        def inner():
            raise ValueError("boom")

        try:
            inner()
        except Exception as e:
            errors.capture(e, task="t1")
        tb = errors.recent()[0]["traceback"]
        self.assertIn("inner", tb)
        self.assertIn("ValueError", tb)

    def test_it_records_where_the_error_came_from(self):
        def inner():
            raise ValueError("boom")

        try:
            inner()
        except Exception as e:
            errors.capture(e)
        self.assertIn("inner", errors.recent()[0]["where_"] or "")

    def test_context_the_caller_knows_is_kept(self):
        errors.capture(self._raise(ValueError("x")), task="t1", model="GLM-5.3",
                       node="implement_t1", attempt=3)
        row = errors.recent()[0]
        self.assertEqual((row["task"], row["model"], row["node"]),
                         ("t1", "GLM-5.3", "implement_t1"))
        self.assertIn("3", row["context"])

    def test_every_capture_carries_the_run_id(self):
        errors.capture(self._raise(ValueError("x")))
        self.assertEqual(errors.recent()[0]["run_id"], errors.run_id())


class GroupingByCause(ErrorCase):
    """A hundred occurrences of one bug are one bug.

    Fingerprinting on the raw message would defeat this: the messages in this
    fleet embed worktree paths, task ids and durations, so every occurrence
    looks unique and the triage list becomes the error log again.
    """

    def _fail_the_same_way(self, detail):
        def inner():
            raise ValueError(f"worktree {detail} failed")

        try:
            inner()
        except Exception as e:
            return errors.capture(e, task=detail)

    def test_the_same_defect_groups_despite_differing_messages(self):
        for d in ("/home/a/abc123", "/home/b/def456", "/home/c/999"):
            self._fail_the_same_way(d)
        groups = errors.groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["count"], 3)

    def test_a_different_exception_type_is_a_different_defect(self):
        self._fail_the_same_way("/x/1")
        errors.capture(self._raise(KeyError("other")))
        self.assertEqual(len(errors.groups()), 2)

    def test_a_group_lists_the_tasks_it_hit(self):
        for d in ("/a/1", "/b/2"):
            self._fail_the_same_way(d)
        self.assertEqual(sorted(errors.groups()[0]["tasks"]), ["/a/1", "/b/2"])

    def test_groups_are_ordered_worst_first(self):
        for d in ("/a/1", "/b/2", "/c/3"):
            self._fail_the_same_way(d)
        errors.capture(self._raise(KeyError("rare")))
        self.assertEqual(errors.groups()[0]["count"], 3)

    def test_line_numbers_do_not_split_a_group(self):
        # Line numbers shift on every edit. If they were part of the
        # fingerprint, an unrelated edit above a bug would reset its history.
        a = errors.fingerprint(self._raise(ValueError("x")))
        b = errors.fingerprint(self._raise(ValueError("y")))
        self.assertEqual(a, b, "same site, same defect")

    def test_normalisation_strips_per_occurrence_noise(self):
        n = errors.normalise("worktree /home/x/ab12cd34 failed after 12.5s (pid 99182)")
        for token in ("/home/x", "ab12cd34", "12.5s", "99182"):
            self.assertNotIn(token, n)

    def test_an_exception_with_no_frames_still_groups(self):
        # Re-created from a string, or raised before entering our code.
        fp = errors.fingerprint(ValueError("detached"))
        self.assertTrue(fp)
        self.assertEqual(fp, errors.fingerprint(ValueError("detached")))


class CaptureNeverRaises(ErrorCase):
    """It runs inside `except` and `finally`. An instrumentation layer that can
    turn a handled error into an unhandled one is worse than none."""

    def test_an_unwritable_database_does_not_raise(self):
        errors.reset_for_tests()
        config.DB_PATH = "/nonexistent-dir/nope.db"
        self.assertEqual(errors.capture(self._raise(ValueError("x"))), "uncaptured")

    def test_unserialisable_context_does_not_raise(self):
        class Awkward:
            def __repr__(self):
                raise RuntimeError("even repr fails")

        fp = errors.capture(self._raise(ValueError("x")), thing=Awkward())
        self.assertTrue(fp)


class Retention(ErrorCase):
    def test_old_errors_can_be_pruned(self):
        errors.capture(self._raise(ValueError("x")))
        self.assertEqual(errors.prune(older_than_s=-1), 1)
        self.assertEqual(errors.recent(), [])

    def test_pruning_keeps_recent_ones(self):
        errors.capture(self._raise(ValueError("x")))
        self.assertEqual(errors.prune(older_than_s=3600), 0)
        self.assertEqual(len(errors.recent()), 1)


class TheDailyAudit(unittest.TestCase):
    """A report nobody reads twice is one that says '14 warnings' and stops.

    Every finding carries a severity and a concrete next action, and the exit
    code is the alarm — a scheduled audit that always exits 0 is a scheduled
    audit nobody hears.
    """

    def test_a_defect_seen_many_times_and_still_firing_is_critical(self):
        import audit
        import errors as e
        e.reset_for_tests()
        db = tempfile.mktemp(suffix=".db")
        orig = config.DB_PATH
        config.DB_PATH = db
        try:
            for i in range(6):
                try:
                    raise RuntimeError(f"repeated failure {i}")
                except Exception as exc:
                    e.capture(exc, task=f"t{i}")
            findings = audit.triage_errors()
            self.assertTrue(findings)
            self.assertEqual(findings[0]["severity"], "critical")
            self.assertIn("RuntimeError", findings[0]["what"])
            self.assertIn("fingerprint", findings[0]["action"])
        finally:
            config.DB_PATH = orig
            e.reset_for_tests()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(db + suffix)
                except OSError:
                    pass

    def test_no_errors_yields_no_defect_findings(self):
        import audit
        import errors as e
        e.reset_for_tests()
        db = tempfile.mktemp(suffix=".db")
        orig = config.DB_PATH
        config.DB_PATH = db
        try:
            self.assertEqual(audit.triage_errors(), [])
        finally:
            config.DB_PATH = orig
            e.reset_for_tests()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(db + suffix)
                except OSError:
                    pass

    def test_every_finding_carries_a_severity_and_an_action(self):
        import audit
        report = audit.run(store=None, with_health=False)
        for f in report["findings"]:
            self.assertIn(f["severity"], audit.SEV)
            self.assertTrue(f["what"])
            if f["severity"] in ("critical", "warning"):
                self.assertTrue(f["action"], f"no action given for: {f['what']}")

    def test_the_report_renders_without_a_store(self):
        import audit
        text = audit.render(audit.run(store=None, with_health=False))
        self.assertIn("ARC audit", text)

    def test_counts_match_the_findings(self):
        import audit
        r = audit.run(store=None, with_health=False)
        for sev in audit.SEV:
            self.assertEqual(
                r["counts"][sev],
                sum(1 for f in r["findings"] if f["severity"] == sev))

    def test_findings_are_ordered_worst_first(self):
        import audit
        order = {s: i for i, s in enumerate(audit.SEV)}
        seen = [order[f["severity"]] for f in
                audit.run(store=None, with_health=False)["findings"]]
        self.assertEqual(seen, sorted(seen))
