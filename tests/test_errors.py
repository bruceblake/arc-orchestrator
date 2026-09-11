"""Structured exception capture: keep the evidence, and group it by cause.

Before this, every catch site reduced its exception to `str(exc)[:300]`. That
says an error happened and nothing about where — no file, no line, no frame —
so debugging a fleet failure meant guessing which of several call paths
produced a message like "opencode exited 1:".
"""
import json
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


class FileClashDetection(unittest.TestCase):
    """Would launching this taskfile collide with work already in flight?

    The subtlety that broke the first version: a taskfile's OTHER tasks may
    have merged hours ago, and their files are then free. Aggregating every
    task in any taskfile that had any live task reported a clash on a file
    whose owner had long since merged, and would have held back a launch for
    no reason.
    """

    def setUp(self):
        import sqlite3
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "t.db")
        self.tasks = os.path.join(self.dir, "tasks")
        os.makedirs(self.tasks)
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE code_tasks(id TEXT, status TEXT, taskfile TEXT)")
        con.commit()
        self.con = con
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        self.con.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _taskfile(self, stem, tid_or_tasks, files=None):
        tasks = ([{"id": tid_or_tasks, "files_hint": files or []}]
                 if isinstance(tid_or_tasks, str) else tid_or_tasks)
        path = os.path.join(self.tasks, f"{stem}.json")
        with open(path, "w") as fh:
            json.dump({"project": {"repo": "/x", "tasks": tasks}}, fh)
        return path

    def _live(self, task_id, status, taskfile):
        self.con.execute("INSERT INTO code_tasks VALUES(?,?,?)",
                         (task_id, status, taskfile))
        self.con.commit()

    def test_a_live_task_owns_its_files(self):
        import tools_file_clash as fc
        tf = self._taskfile("busy", [{"id": "a", "files_hint": ["shared.py"]}])
        self._live("a", "in_review", tf)
        self._taskfile("new", [{"id": "b", "files_hint": ["shared.py"]}])
        clashes, _, _ = fc.check("new", db_path=self.db, tasks_dir=self.tasks)
        self.assertIn("shared.py", clashes)

    def test_a_merged_siblings_files_are_free(self):
        # The false positive this exists to prevent.
        import tools_file_clash as fc
        tf = self._taskfile("busy", [
            {"id": "a", "files_hint": ["still-mine.py"]},
            {"id": "sibling", "files_hint": ["shared.py"]}])
        self._live("a", "in_review", tf)          # only 'a' is live
        self._taskfile("new", [{"id": "b", "files_hint": ["shared.py"]}])
        clashes, _, _ = fc.check("new", db_path=self.db, tasks_dir=self.tasks)
        self.assertEqual(clashes, {}, "a merged sibling's files must be free")

    def test_disjoint_files_are_safe(self):
        import tools_file_clash as fc
        tf = self._taskfile("busy", [{"id": "a", "files_hint": ["one.py"]}])
        self._live("a", "running", tf)
        self._taskfile("new", [{"id": "b", "files_hint": ["two.py"]}])
        clashes, _, _ = fc.check("new", db_path=self.db, tasks_dir=self.tasks)
        self.assertEqual(clashes, {})

    def test_relaunching_a_taskfile_is_not_a_clash_with_itself(self):
        # Re-running a taskfile to resume it is the normal recovery path:
        # resync, conflict repair, retry after a failed merge. Refusing because
        # the task being resumed holds its own files blocks the exact operation
        # that fixes things.
        import tools_file_clash as fc
        tf = self._taskfile("proj", "a", ["shared.py"])
        self._live("a", "conflict", tf)
        clashes, _, _ = fc.check("proj", db_path=self.db, tasks_dir=self.tasks)
        self.assertEqual(clashes, {})

    def test_a_merged_task_in_the_launched_file_wants_nothing(self):
        # On resume it collapses to a skip stub and edits nothing.
        import tools_file_clash as fc
        other = self._taskfile("busy", "live", ["shared.py"])
        self._live("live", "running", other)
        self._taskfile("mine", "done", ["shared.py"])
        self._live("done", "merged", os.path.join(self.tasks, "mine.json"))
        clashes, _, _ = fc.check("mine", db_path=self.db, tasks_dir=self.tasks)
        self.assertEqual(clashes, {})

    def test_a_terminal_task_owns_nothing(self):
        import tools_file_clash as fc
        tf = self._taskfile("busy", [{"id": "a", "files_hint": ["shared.py"]}])
        self._live("a", "merged", tf)
        self._taskfile("new", [{"id": "b", "files_hint": ["shared.py"]}])
        clashes, _, _ = fc.check("new", db_path=self.db, tasks_dir=self.tasks)
        self.assertEqual(clashes, {})


class TheAuditMustNotCryWolf(unittest.TestCase):
    """An audit that raises alarms during normal operation is one nobody reads.

    Both of these fired on the audit's first real use: it reported "8 task
    worktrees — CRITICAL" while four of them were in active use, and told the
    operator to "re-run their project to resume" two tasks that were running at
    that moment.
    """

    class _Store:
        def __init__(self, rows):
            self._rows = rows

        def code_tasks_all(self):
            return self._rows

    @staticmethod
    def _fake_git(worktrees):
        """audit_git shells out four times; answer each one for what it asked.

        A stub that returns the same text for every call made `git status
        --porcelain` return the worktree listing, so the repo read as dirty and
        the test failed for a reason that existed only in the test.
        """
        def run(*args, **kwargs):
            argv = list(args)
            if "worktree" in argv:
                return 0, worktrees, ""
            if "status" in argv:
                return 0, "", ""
            if "branch" in argv:
                return 0, "", ""
            return 0, "", ""
        return run

    def test_a_worktree_belonging_to_a_live_task_is_not_orphaned(self):
        import audit
        store = self._Store([{"id": "alive", "status": "running", "taskfile": "/t/a.json"}])
        orig, orig_root = audit._sh, config.WORKTREE_ROOT
        config.WORKTREE_ROOT = "/wt"
        audit._sh = self._fake_git(
            "worktree /repo\nworktree /wt/repo/alive\nworktree /wt/repo/dead\n")
        try:
            findings = audit.audit_git(repo="/repo", store=store)
        finally:
            audit._sh, config.WORKTREE_ROOT = orig, orig_root
        wt = [f for f in findings if "orphan" in f["what"]]
        self.assertEqual(len(wt), 1)
        self.assertIn("dead", wt[0]["detail"])
        self.assertNotIn("alive", wt[0]["detail"])

    def test_no_orphans_is_not_a_warning(self):
        import audit
        store = self._Store([{"id": "alive", "status": "running", "taskfile": "/t/a.json"}])
        orig, orig_root = audit._sh, config.WORKTREE_ROOT
        config.WORKTREE_ROOT = "/wt"
        audit._sh = self._fake_git("worktree /repo\nworktree /wt/repo/alive\n")
        try:
            findings = audit.audit_git(repo="/repo", store=store)
        finally:
            audit._sh, config.WORKTREE_ROOT = orig, orig_root
        self.assertTrue(all(f["severity"] == "info" for f in findings),
                        "a fleet working normally must raise nothing above info")

    def test_a_task_with_a_live_run_is_in_flight_not_stranded(self):
        import audit
        import reconcile
        store = self._Store([
            {"id": "busy", "status": "in_review", "taskfile": "/t/live.json"},
            {"id": "abandoned", "status": "in_review", "taskfile": "/t/dead.json"}])
        orig = reconcile.live_runs
        reconcile.live_runs = lambda: [{"taskfile": "/t/live.json", "pid": 1}]
        try:
            findings = audit.triage_tasks(store)
        finally:
            reconcile.live_runs = orig
        stranded = [f for f in findings if "stranded" in f["what"]]
        self.assertEqual(len(stranded), 1)
        self.assertIn("abandoned", stranded[0]["detail"])
        self.assertNotIn("busy", stranded[0]["detail"])
        self.assertTrue(any("in flight" in f["what"] for f in findings))

    def test_everything_in_flight_raises_nothing_above_info(self):
        import audit
        import reconcile
        store = self._Store([
            {"id": "a", "status": "running", "taskfile": "/t/live.json"},
            {"id": "b", "status": "in_review", "taskfile": "/t/live.json"}])
        orig = reconcile.live_runs
        reconcile.live_runs = lambda: [{"taskfile": "/t/live.json", "pid": 1}]
        try:
            findings = audit.triage_tasks(store)
        finally:
            reconcile.live_runs = orig
        self.assertTrue(all(f["severity"] == "info" for f in findings))


class WeakGateDetection(unittest.TestCase):
    """A verify_cmd that greps for a string already in the file proves nothing.

    AGENTS.md Rule 4 requires a gate that FAILS when the work is wrong. A gate
    built from `grep -q '<string>' <file>` fails that rule whenever the string
    is already present: the implementer sees nothing to prove, writes nothing,
    and the task dies as "no changes to publish". archived-attention failed
    exactly that way, twice.
    """

    class _Store:
        def __init__(self, merged):
            self.merged = merged

        def code_tasks_all(self):
            return [{"id": i, "status": "merged"} for i in self.merged]

    def setUp(self):
        import shutil
        self.dir = tempfile.mkdtemp()
        self.repo = os.path.join(self.dir, "arc-orchestrator")
        self.tasks = os.path.join(self.dir, "tasks")
        os.makedirs(self.repo)
        os.makedirs(self.tasks)
        with open(os.path.join(self.repo, "app.py"), "w") as fh:
            fh.write("def already_here():\n    return 1\n")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _taskfile(self, stem, tid, verify):
        with open(os.path.join(self.tasks, f"{stem}.json"), "w") as fh:
            json.dump({"project": {"repo": self.repo, "tasks": [
                {"id": tid, "verify_cmd": verify}]}}, fh)

    def _run(self, merged=()):
        import audit
        return audit.audit_gates(self._Store(merged), tasks_dir=self.tasks,
                                 repo=self.repo)

    def test_a_grep_that_already_matches_is_flagged(self):
        self._taskfile("p", "t1", "grep -q 'already_here' app.py")
        f = self._run()
        self.assertEqual(len(f), 1)
        self.assertIn("passes without the work", f[0]["what"])

    def test_a_grep_that_does_not_match_yet_is_fine(self):
        self._taskfile("p", "t1", "grep -q 'not_written_yet' app.py")
        self.assertEqual(self._run(), [])

    def test_a_merged_tasks_gate_is_not_flagged(self):
        # Its assertions SHOULD pass now. Flagging them anyway turned 4 real
        # findings into 43 meaningless ones.
        self._taskfile("p", "t1", "grep -q 'already_here' app.py")
        self.assertEqual(self._run(merged={"t1"}), [])

    def test_a_gate_that_runs_tests_is_not_flagged(self):
        self._taskfile("p", "t1", "./check.sh")
        self.assertEqual(self._run(), [])

    def test_one_unmatched_grep_is_enough_to_make_the_gate_real(self):
        self._taskfile("p", "t1",
                       "grep -q 'already_here' app.py && grep -q 'future' app.py")
        self.assertEqual(self._run(), [])

    def test_a_grep_against_a_missing_file_is_not_judged(self):
        self._taskfile("p", "t1", "grep -q 'x' does_not_exist.py")
        self.assertEqual(self._run(), [])


class TaskfileSnapshots(unittest.TestCase):
    """Taskfiles are the design of every project and nothing versions them.

    They live outside the repo, untracked. A taskfile holds the prompt, the
    decomposition, the model routing and the verify gate, and editing one
    leaves no record of what it said before.
    """

    def setUp(self):
        import shutil
        self.dir = tempfile.mkdtemp()
        self.tasks = os.path.join(self.dir, "tasks")
        os.makedirs(self.tasks)
        with open(os.path.join(self.tasks, "p.json"), "w") as fh:
            json.dump({"project": {"repo": "/x", "tasks": []}}, fh)
        self._orig_root = config.ROOT
        config.ROOT = self.dir
        self.addCleanup(self._restore, shutil)

    def _restore(self, shutil):
        config.ROOT = self._orig_root
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_never_snapshotted_is_a_warning(self):
        import audit
        f = audit.audit_tasks_backup(tasks_dir=self.tasks)
        self.assertEqual(f[0]["severity"], "warning")
        self.assertIn("never been snapshotted", f[0]["what"])

    def test_snapshotting_copies_every_taskfile(self):
        import audit
        audit.audit_tasks_backup(tasks_dir=self.tasks, snapshot=True)
        import glob
        copied = glob.glob(os.path.join(self.dir, "logs", "task-snapshots", "*", "*.json"))
        self.assertEqual(len(copied), 1)

    def test_a_fresh_snapshot_silences_the_warning(self):
        import audit
        audit.audit_tasks_backup(tasks_dir=self.tasks, snapshot=True)
        self.assertEqual(audit.audit_tasks_backup(tasks_dir=self.tasks), [])

    def test_an_empty_tasks_dir_reports_nothing(self):
        import audit
        empty = os.path.join(self.dir, "empty")
        os.makedirs(empty)
        self.assertEqual(audit.audit_tasks_backup(tasks_dir=empty), [])

    def test_the_snapshot_goes_to_logs_not_into_the_repo(self):
        # This repo is public. Operator task prompts are not ours to publish.
        import audit
        r = audit.audit_tasks_backup(tasks_dir=self.tasks, snapshot=True)
        self.assertIn("logs", r[0]["detail"])


class ReapingMustNotDestroyWork(unittest.TestCase):
    """A terminal task row does NOT mean its branch is disposable.

    pause-when-hidden is `failed` AND three commits ahead behind OPEN pull
    request #9. The first version of this audit called it orphaned on the
    strength of the status column alone, and acting on that would have
    destroyed reviewable work.
    """

    class _Store:
        def __init__(self, rows):
            self._rows = rows

        def code_tasks_all(self):
            return self._rows

    @staticmethod
    def _git(worktrees, ahead=0, prs="[]", dirty=""):
        def run(*args, **kwargs):
            argv = list(args)
            if "worktree" in argv:
                return 0, worktrees, ""
            if "rev-list" in argv:
                return 0, str(ahead), ""
            if argv and argv[0] == "gh":
                return 0, prs, ""
            if "status" in argv:
                return 0, dirty, ""
            return 0, "", ""
        return run

    def _findings(self, **kw):
        import audit
        orig_sh, orig_root = audit._sh, config.WORKTREE_ROOT
        config.WORKTREE_ROOT = "/wt"
        audit._sh = self._git(
            "worktree /repo\nworktree /wt/repo/spent\n", **kw)
        try:
            return audit.audit_git(repo="/repo", store=self._Store([]))
        finally:
            audit._sh, config.WORKTREE_ROOT = orig_sh, orig_root

    def test_unmerged_commits_block_reaping(self):
        f = self._findings(ahead=3)
        self.assertTrue(any("holds work" in x["what"] for x in f))
        self.assertFalse(any("orphaned" in x["what"] for x in f))

    def test_an_open_pull_request_blocks_reaping(self):
        f = self._findings(prs='[{"number": 9}]')
        held = [x for x in f if "holds work" in x["what"]]
        self.assertTrue(held)
        self.assertIn("#9", held[0]["detail"])

    def test_uncommitted_changes_block_reaping(self):
        f = self._findings(dirty=" M static/index.html\n")
        self.assertTrue(any("holds work" in x["what"] for x in f))

    def test_a_genuinely_spent_worktree_is_reported_orphaned(self):
        f = self._findings()
        self.assertTrue(any("orphaned" in x["what"] for x in f))

    def test_opencode_scratch_worktrees_are_not_ours_to_touch(self):
        # opencode makes detached worktrees under /tmp/opencode for snapshots.
        # They are not tasks and reaping one could break a live harness.
        import audit
        orig_sh, orig_root = audit._sh, config.WORKTREE_ROOT
        config.WORKTREE_ROOT = "/wt"
        audit._sh = self._git("worktree /repo\nworktree /tmp/opencode/pr-review\n")
        try:
            f = audit.audit_git(repo="/repo", store=self._Store([]))
        finally:
            audit._sh, config.WORKTREE_ROOT = orig_sh, orig_root
        self.assertFalse(any("pr-review" in str(x.get("detail")) for x in f))


class ForeseeablePRConflicts(unittest.TestCase):
    """An open PR whose files a live task is rewriting will conflict.

    Nothing warned about this: you found out when the merge was refused, after
    the reviewers had already been spent. PR #9 carried changes to
    static/phone.html and static/usage.html while phone-shell and
    usage-informative were rewriting exactly those files.
    """

    class _Store:
        def __init__(self, rows):
            self._rows = rows

        def code_tasks_all(self):
            return self._rows

    def setUp(self):
        import shutil
        self.dir = tempfile.mkdtemp()
        self.tf = os.path.join(self.dir, "live.json")
        with open(self.tf, "w") as fh:
            json.dump({"project": {"repo": "/x", "tasks": [
                {"id": "rewriter", "files_hint": ["static/phone.html"]}]}}, fh)
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _run(self, pr_files, pr_branch="task/other", live_status="running"):
        import audit
        import reconcile
        orig_sh, orig_live = audit._sh, reconcile.live_runs
        reconcile.live_runs = lambda: [{"taskfile": self.tf, "pid": 1}]

        def sh(*args, **kw):
            argv = list(args)
            if argv and argv[0] == "gh":
                return 0, json.dumps([{"number": 9, "headRefName": pr_branch}]), ""
            if "diff" in argv:
                return 0, "\n".join(pr_files), ""
            return 0, "", ""
        audit._sh = sh
        try:
            return audit.audit_pr_collisions(
                self._Store([{"id": "rewriter", "status": live_status}]), repo="/repo")
        finally:
            audit._sh, reconcile.live_runs = orig_sh, orig_live

    def test_an_overlapping_file_is_flagged(self):
        f = self._run(["static/phone.html"])
        self.assertEqual(len(f), 1)
        self.assertIn("#9", f[0]["what"])
        self.assertIn("rewriter", f[0]["detail"])

    def test_disjoint_files_are_not_flagged(self):
        self.assertEqual(self._run(["docs/readme.md"]), [])

    def test_a_pr_from_the_very_task_doing_the_rewrite_is_not_a_conflict(self):
        # Its own branch is where that rewrite is happening.
        self.assertEqual(
            self._run(["static/phone.html"], pr_branch="task/rewriter"), [])

    def test_a_task_that_is_not_live_does_not_trigger_it(self):
        self.assertEqual(
            self._run(["static/phone.html"], live_status="merged"), [])


class UnknownIsNotAllClear(unittest.TestCase):
    """A check that cannot run must say so, not report nothing.

    An exception reading the task list left `live` as an empty set, which makes
    every worktree look not-in-use — including one allocated seconds ago — and
    with --fix that is a reap of work in progress. "We could not tell" and
    "there is nothing wrong" are different answers.
    """

    class _BrokenStore:
        def code_tasks_all(self):
            raise RuntimeError("database is locked")

    def test_an_unreadable_task_list_skips_the_orphan_check(self):
        import audit
        orig_sh, orig_root = audit._sh, config.WORKTREE_ROOT
        config.WORKTREE_ROOT = "/wt"

        def sh(*args, **kw):
            # Answer each subcommand for what it asked. A stub returning one
            # blob for everything made `gh` look like it found a PR, so the
            # worktree read as "holds work" and the assertion below could not
            # fail even with the guard removed — mutation testing caught it.
            argv = list(args)
            if "worktree" in argv:
                return 0, "worktree /repo\nworktree /wt/repo/x\n", ""
            if "rev-list" in argv:
                return 0, "0", ""
            if argv and argv[0] == "gh":
                return 0, "[]", ""
            return 0, "", ""
        audit._sh = sh
        try:
            f = audit.audit_git(repo="/repo", store=self._BrokenStore())
        finally:
            audit._sh, config.WORKTREE_ROOT = orig_sh, orig_root
        self.assertTrue(any("cannot tell" in x["what"] for x in f))
        self.assertFalse(any("orphan" in x["what"] for x in f),
                         "must not call anything orphaned when it cannot tell")

    def test_a_failing_gh_does_not_report_zero_collisions(self):
        import audit
        import reconcile
        orig_sh, orig_live = audit._sh, reconcile.live_runs
        reconcile.live_runs = lambda: []
        audit._sh = lambda *a, **k: (1, "", "gh: not authenticated")
        try:
            f = audit.audit_pr_collisions(store=None, repo="/repo")
        finally:
            audit._sh, reconcile.live_runs = orig_sh, orig_live
        self.assertTrue(f, "a failed gh must be reported, not silently empty")
        self.assertIn("could not list", f[0]["what"])


class InvariantsCheckedAgainstReality(unittest.TestCase):
    """Claims the database makes that git, the process table or GitHub can refute.

    Every bug found today was a disagreement of exactly this shape — a status
    column believed over the world it describes. Checking the disagreements
    directly beats discovering them by their consequences.
    """

    class _Store:
        def __init__(self, rows):
            self._rows = rows

        def code_tasks_all(self):
            return self._rows

    def _run(self, rows, prs="[]", worktrees="worktree /repo\n", live=()):
        import audit
        import reconcile
        orig_sh, orig_live = audit._sh, reconcile.live_runs
        reconcile.live_runs = lambda: [{"taskfile": t} for t in live]

        def sh(*args, **kw):
            argv = list(args)
            if argv and argv[0] == "gh":
                return 0, prs, ""
            if "worktree" in argv:
                return 0, worktrees, ""
            return 0, "", ""
        audit._sh = sh
        try:
            return audit.audit_invariants(self._Store(rows), repo="/repo")
        finally:
            audit._sh, reconcile.live_runs = orig_sh, orig_live

    def test_running_with_no_live_run_is_flagged(self):
        f = self._run([{"id": "t1", "status": "running", "taskfile": "/t/a.json"}])
        self.assertTrue(any("no run is alive" in x["what"] for x in f))

    def test_running_with_a_live_run_is_fine(self):
        f = self._run([{"id": "t1", "status": "running", "taskfile": "/t/a.json"}],
                      live=["/t/a.json"])
        self.assertEqual(f, [])

    def test_in_review_with_no_open_pr_is_flagged(self):
        f = self._run([{"id": "t1", "status": "in_review", "taskfile": "/t/a.json"}])
        self.assertTrue(any("no PR is open" in x["what"] for x in f))

    def test_in_review_with_its_pr_open_is_fine(self):
        f = self._run([{"id": "t1", "status": "in_review", "taskfile": "/t/a.json"}],
                      prs='[{"number":4,"headRefName":"task/t1"}]')
        self.assertEqual(f, [])

    def test_merged_with_a_still_open_pr_is_flagged(self):
        f = self._run([{"id": "t1", "status": "merged"}],
                      prs='[{"number":4,"headRefName":"task/t1"}]')
        self.assertTrue(any("still open" in x["what"] for x in f))

    def test_merged_with_a_leftover_worktree_is_noted(self):
        f = self._run([{"id": "t1", "status": "merged"}],
                      worktrees="worktree /repo\nworktree /wt/t1\n")
        self.assertTrue(any("worktree remains" in x["what"] for x in f))

    def test_a_pr_for_an_unknown_task_is_flagged(self):
        f = self._run([], prs='[{"number":7,"headRefName":"task/ghost"}]')
        self.assertTrue(any("does not know" in x["what"] for x in f))

    def test_a_failing_gh_does_not_produce_false_pr_findings(self):
        # Unknown is not all-clear, and it is not "everything is broken" either.
        import audit
        import reconcile
        orig_sh, orig_live = audit._sh, reconcile.live_runs
        reconcile.live_runs = lambda: [{"taskfile": "/t/a.json"}]
        audit._sh = lambda *a, **k: ((1, "", "gh down") if a and a[0] == "gh"
                                     else (0, "worktree /repo\n", ""))
        try:
            f = audit.audit_invariants(
                self._Store([{"id": "t1", "status": "in_review",
                              "taskfile": "/t/a.json"}]), repo="/repo")
        finally:
            audit._sh, reconcile.live_runs = orig_sh, orig_live
        self.assertFalse(any("no PR is open" in x["what"] for x in f))

    def test_two_launched_taskfiles_that_collide_are_reported(self):
        # check() only compares a batch against work ALREADY in flight, which
        # silently answers "safe" for a wave whose own members collide.
        # dashboard-modularise beside dashboard-ux was exactly that: both own
        # static/index.html and nothing was in flight to reveal it.
        import tools_file_clash as fc
        self._taskfile("a", "t1", ["shared.py"])
        self._taskfile("b", "t2", ["shared.py"])
        within = fc.internal("a", "b", tasks_dir=self.tasks, db_path=self.db)
        self.assertIn("shared.py", within)
        self.assertEqual(within["shared.py"], ["a", "b"])

    def test_disjoint_taskfiles_report_no_internal_clash(self):
        import tools_file_clash as fc
        self._taskfile("a", "t1", ["one.py"])
        self._taskfile("b", "t2", ["two.py"])
        self.assertEqual(fc.internal("a", "b", tasks_dir=self.tasks, db_path=self.db), {})

    def test_a_merged_task_does_not_create_a_phantom_internal_clash(self):
        import tools_file_clash as fc
        self._taskfile("a", "done", ["shared.py"])
        self._live("done", "merged", os.path.join(self.tasks, "a.json"))
        self._taskfile("b", "t2", ["shared.py"])
        self.assertEqual(fc.internal("a", "b", tasks_dir=self.tasks, db_path=self.db), {})

    def test_one_taskfile_alone_never_collides_with_itself(self):
        import tools_file_clash as fc
        self._taskfile("a", "t1", ["shared.py"])
        self.assertEqual(fc.internal("a", tasks_dir=self.tasks, db_path=self.db), {})
