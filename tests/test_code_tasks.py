"""Taskfile validation, reviewer-verdict parsing, resume/escalation planning,
and project chaining (`after`)."""
import asyncio
import time
import shutil
import os
import subprocess
import json
import pathlib
import re
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from helpers import FakeStore, capture_events
from helpers import ENTRY, STRONGEST, DISTINCT_MODELS, MODEL_OF  # noqa: E402,F401
from helpers import STRONGEST_FAMILY, STRONGEST_REVIEWER  # noqa: E402,F401


import code_tasks
import config
import gitstore


def taskfile(tasks, repo="/tmp", title="t", after=None, pattern=None):
    """Write a taskfile to a temp path and return it."""
    project = {"repo": repo, "title": title, "tasks": tasks}
    if after is not None:
        project["after"] = after
    if pattern is not None:
        project["pattern"] = pattern
    doc = {"project": project}
    fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(doc, fh)
    fh.close()
    return Path(fh.name)


def _gone(pid, wait_s=3.0):
    """True once `pid` no longer exists (or is a zombie awaiting init)."""
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


def _reap(pid):
    """Cleanup for a test that FAILED: do not leave the sleeper behind."""
    try:
        os.kill(pid, 9)
    except OSError:
        pass


# The canonical valid task: entry-tier implementer, reviewed by the family the
# roster says reviews it. Derived, not pinned, so a transition does not leave a
# fixture whose reviewer is a family that has left (kimi left on 2026-09-12).
BASIC = {"id": "t1", "title": "T1", "prompt": "do it",
         "model": config.ESCALATION_PATH[0],
         "reviewer": config.cross_family_reviewer(config.ESCALATION_PATH[0])}


class LoadTaskfile(unittest.TestCase):
    def test_accepts_a_valid_cross_family_task(self):
        ts = code_tasks.load_taskfile(taskfile([BASIC]))
        self.assertEqual(list(ts["tasks"]), ["t1"])

    def test_every_implementer_has_a_cross_family_default_reviewer(self):
        """The RULE, over the whole roster: name any live implementer and the
        default reviewer's family differs from its own. Stated this way, a
        roster move cannot leave a model whose only reviewer is itself."""
        for model in config.IMPLEMENTER_MODELS:
            fam = config.MODEL_FAMILY[model]
            rev = config.cross_family_reviewer(model)
            self.assertIsNotNone(rev, f"{model} has no cross-family reviewer")
            self.assertNotEqual(rev, fam, f"{model} would self-review")
            self.assertIn(rev, config.REVIEW_FAMILIES)
            # And the loader accepts the pairing it implies.
            t = {**BASIC, "model": model, "reviewer": rev}
            loaded = code_tasks.load_taskfile(taskfile([t]))
            self.assertEqual(loaded["tasks"]["t1"]["reviewer"], rev)

    def test_rejects_a_non_implementer_model(self):
        bad = {**BASIC, "model": "gpt-4"}
        with self.assertRaises(ValueError) as cm:
            code_tasks.load_taskfile(taskfile([bad]))
        self.assertIn("must be an implementer", str(cm.exception))

    def test_rejects_same_family_review(self):
        """Cross-family review is the DEFAULT: with the capacity flag off,
        same-family review is rejected. (ARC_ALLOW_SAME_FAMILY_REVIEW is the
        emergency hatch for a hard-down reviewer backend — 2026-09-12..14
        and re-added 2026-09-15 — so pin it off here, whatever the operator's
        shell happens to export.)"""
        bad = {**BASIC, "model": STRONGEST, "reviewer": STRONGEST_FAMILY}
        saved = config.ALLOW_SAME_FAMILY_REVIEW
        config.ALLOW_SAME_FAMILY_REVIEW = False
        try:
            with self.assertRaises(ValueError) as cm:
                code_tasks.load_taskfile(taskfile([bad]))
        finally:
            config.ALLOW_SAME_FAMILY_REVIEW = saved
        self.assertIn("must not be the harness", str(cm.exception))

    def test_same_family_capacity_flag(self):
        """ARC_ALLOW_SAME_FAMILY_REVIEW=1 (the reviewer backend is down):
        the loader accepts a same-family pairing, escalation keeps it, and
        the PR pool inverts to ONLY the implementer's own family."""
        same = {**BASIC, "model": STRONGEST, "reviewer": STRONGEST_FAMILY}
        saved = config.ALLOW_SAME_FAMILY_REVIEW
        config.ALLOW_SAME_FAMILY_REVIEW = True
        try:
            ts = code_tasks.load_taskfile(taskfile([same]))
            self.assertEqual(ts["tasks"]["t1"]["reviewer"], STRONGEST_FAMILY)
            task = {"reviewer": STRONGEST_FAMILY}
            self.assertEqual(code_tasks._reviewer_for(task, STRONGEST),
                             STRONGEST_FAMILY)
            pool = code_tasks._eligible_pr_reviewers(STRONGEST_FAMILY, None)
            self.assertTrue(pool)
            for m in pool:
                self.assertEqual(config.MODEL_FAMILY[m], STRONGEST_FAMILY)
            # And the cross-family default is untouched underneath it:
            config.ALLOW_SAME_FAMILY_REVIEW = False
            pool = code_tasks._eligible_pr_reviewers(STRONGEST_FAMILY, None)
            for m in pool:
                self.assertNotEqual(config.MODEL_FAMILY[m], STRONGEST_FAMILY)
        finally:
            config.ALLOW_SAME_FAMILY_REVIEW = saved

    def test_rejects_duplicate_ids(self):
        with self.assertRaises(ValueError):
            code_tasks.load_taskfile(taskfile([BASIC, dict(BASIC)]))

    def test_rejects_unknown_dependency(self):
        bad = {**BASIC, "deps": ["ghost"]}
        with self.assertRaises(ValueError) as cm:
            code_tasks.load_taskfile(taskfile([bad]))
        self.assertIn("unknown dep", str(cm.exception))

    def test_rejects_dependency_cycle(self):
        a = {**BASIC, "id": "a", "deps": ["b"]}
        b = {**BASIC, "id": "b", "deps": ["a"]}
        with self.assertRaises(ValueError) as cm:
            code_tasks.load_taskfile(taskfile([a, b]))
        self.assertIn("cycle", str(cm.exception))

    def test_bench_policy_may_relax_self_review(self):
        same = {**BASIC, "model": STRONGEST, "reviewer": STRONGEST_FAMILY}
        ts = code_tasks.load_taskfile(taskfile([same]),
                                      policy={"allow_self_review": True})
        self.assertEqual(ts["tasks"]["t1"]["reviewer"], STRONGEST_FAMILY)

    def test_passes_through_project_pattern(self):
        ts = code_tasks.load_taskfile(taskfile([BASIC], pattern="chain"))
        self.assertEqual(ts["pattern"], "chain")

    def test_pattern_aliases_normalize_to_the_catalogue_id(self):
        # Planners wrote "fan-out-fan-in" and "orchestrator-workers" for the
        # same shape; the dashboard and describe() compare labels to detected
        # shapes, so the label must be canonical. Unknown names pass through.
        for raw, want in (("fan-out-fan-in", "fanout"), ("Orchestrator Workers", "fanout"),
                          ("evaluator-optimizer", "evaluator"), ("debate-vote", "debate"),
                          ("something-new", "something-new")):
            with self.subTest(raw=raw):
                ts = code_tasks.load_taskfile(taskfile([BASIC], pattern=raw))
                self.assertEqual(ts["pattern"], want)

    def test_pattern_defaults_to_empty_when_absent(self):
        ts = code_tasks.load_taskfile(taskfile([BASIC]))
        self.assertEqual(ts["pattern"], "")


class PlannerPatternLibrary(unittest.TestCase):
    """The planner designs the graph BETWEEN tasks: it is handed the catalogue
    (graph_shapes.PATTERNS), the decision table, today's fan-out caps, and
    the other projects for the repo it may chain `after`."""

    def test_schema_hint_carries_the_pattern_and_after_fields(self):
        self.assertIn('"pattern"', code_tasks.PLAN_SCHEMA_HINT)
        self.assertIn('"after"', code_tasks.PLAN_SCHEMA_HINT)
        for pid in ("chain", "fanout", "diamond", "router"):
            self.assertIn(pid, code_tasks.PLAN_SCHEMA_HINT)

    def test_planner_prompt_points_at_the_pattern_library(self):
        seen = {}

        class FakeDriver:
            async def run(self, prompt, cwd, task_id=None):
                seen["prompt"] = prompt
                return object()

        # The planner is whichever model the roster says may plan today —
        # GLM-5.3 on the 2026-09-12 two-model roster — resolved through
        # _driver. Stub THAT, not a specific driver class, or the test breaks
        # the day the roster moves.
        orig_driver = code_tasks._driver
        orig_extract = code_tasks._plan_json_from_run
        code_tasks._driver = lambda model, role, pol: FakeDriver()
        code_tasks._plan_json_from_run = (
            lambda res: '{"project": {"repo": "/tmp", "tasks": []}}')
        try:
            with tempfile.TemporaryDirectory() as d:
                asyncio.run(code_tasks.plan_tasks(
                    "g", "/tmp", out_path=Path(d) / "plan.json"))
        finally:
            code_tasks._driver = orig_driver
            code_tasks._plan_json_from_run = orig_extract
        import graph_shapes
        p = seen["prompt"]
        # The catalogue itself is in the prompt — a planner working on another
        # repo cannot read this repo's docs/graph-patterns.md.
        for pat in graph_shapes.PATTERNS:
            if pat["id"] not in ("evaluator", "escalate"):
                self.assertIn(f"{pat['id']}: {pat['gist']}", p)
        self.assertIn("Decision table", p)
        self.assertIn("Joins are real", p)
        self.assertIn('"after"', p)
        # The per-task pipeline is stated as fixed, so the planner designs
        # only the graph above it.
        self.assertIn("fixed per-task pipeline", p)
        self.assertIn('"pattern"', p)
        # Fan-out arithmetic quotes today's slots, not a typed number.
        for m in config.ESCALATION_PATH:
            self.assertIn(f"{m} {config.driver_limit(m)}", p)

    def test_planner_sees_existing_projects_for_the_repo_only(self):
        with tempfile.TemporaryDirectory() as d:
            orig = config.TASKS_DIR
            config.TASKS_DIR = d
            try:
                (Path(d) / "mine.json").write_text(json.dumps({"project": {
                    "repo": "/tmp/repo-a", "title": "Mine", "tasks": [BASIC]}}))
                (Path(d) / "other.json").write_text(json.dumps({"project": {
                    "repo": "/tmp/repo-b", "title": "Other", "tasks": [BASIC]}}))
                prose = code_tasks._existing_projects_prose("/tmp/repo-a")
            finally:
                config.TASKS_DIR = orig
        self.assertIn("mine.json: Mine (1 tasks", prose)
        self.assertNotIn("other.json", prose)
        self.assertIn('"after"', prose)


class ParseVerdict(unittest.TestCase):
    def test_plain_pass(self):
        self.assertTrue(code_tasks._parse_verdict('{"pass": true}')["pass"])

    def test_last_verdict_wins_over_an_earlier_example(self):
        text = 'Example: {"pass": true}\nMy verdict: {"pass": false, "issues": ["x"]}'
        v = code_tasks._parse_verdict(text)
        self.assertFalse(v["pass"])
        self.assertEqual(v["issues"], ["x"])

    def test_braces_inside_quoted_code_do_not_break_the_span(self):
        text = 'The constant {WORLD_X,WORLD_Z} is fine.\n{"pass": true}'
        self.assertTrue(code_tasks._parse_verdict(text)["pass"])

    def test_unparseable_reviewer_output_fails_closed(self):
        v = code_tasks._parse_verdict("looks good to me!")
        self.assertFalse(v["pass"], "a missing verdict must never count as a pass")
        self.assertTrue(v["issues"])

    def test_missing_verdict_is_marked_truncated(self):
        # A session that ends with a question instead of a verdict (observed:
        # a reviewer asked "Want me to continue?" after 47 minutes of analysis)
        # must be distinguishable from a real rejection.
        v = code_tasks._parse_verdict(
            "I checked all three files and found two minor doc issues. "
            "Want me to continue with the test run and finish the verdict?")
        self.assertFalse(v["pass"])
        self.assertTrue(v.get("truncated"))
        self.assertFalse(
            code_tasks._parse_verdict('{"pass": true}').get("truncated"))
        self.assertFalse(
            code_tasks._parse_verdict(
                '{"pass": false, "issues": ["x"]}').get("truncated"))


class CapabilityFailureClassification(unittest.TestCase):
    """Only a real capability failure may burn a stronger model tier.

    The strings here are the ones `fail()` and `publish` ACTUALLY store, copied
    from those call sites, not the words one would guess. The list once held
    "gate failed"/"review rejected" while fail() writes "verify gate still
    failing after N attempt(s)" — so every gate- and review-rejected task was
    read as infrastructure noise.
    """

    def test_exhausted_rounds_is_a_capability_failure(self):
        self.assertTrue(code_tasks._is_capability_failure("exhausted fix rounds"))
        self.assertTrue(code_tasks._is_capability_failure(
            f"exhausted escalation up to {config.PLANNER_MODEL}"))

    def test_the_gate_failure_fail_actually_writes(self):
        # fail(): f"verify gate still failing after {attempts} attempt(s) on {last}"
        self.assertTrue(code_tasks._is_capability_failure(
            "verify gate still failing after 16 attempt(s) on GLM-5.3"))

    def test_the_review_failure_fail_actually_writes(self):
        # fail(): f"pre-merge review still rejecting after {attempts} attempt(s)"
        self.assertTrue(code_tasks._is_capability_failure(
            "pre-merge review still rejecting after 16 attempt(s)"))

    def test_the_pr_rejection_fail_actually_writes(self):
        # fail(): f"PR #{pr} rejected after {PR_MAX_ROUNDS} review round(s); …"
        self.assertTrue(code_tasks._is_capability_failure(
            "PR #50 rejected after 16 review round(s); last had 2 unresolved "
            "issue(s)"))

    def test_every_string_fail_can_store_classifies_the_same_way(self):
        """A guard against this drifting again: build fail()'s reasons from the
        same f-strings and assert each one is recognised. A token list that
        stops matching the code is worse than no list — it silently reclassifies
        a real rejection as infrastructure noise."""
        attempts, last, rounds = 16, "GLM-5.3", config.PR_MAX_ROUNDS
        written_by_fail = [
            (f"PR #{50} rejected after {rounds} review round(s); last had "
             f"{2} unresolved issue(s)", True),
            (f"verify gate still failing after {attempts} attempt(s) on {last}",
             True),
            (f"pre-merge review still rejecting after {attempts} attempt(s)", True),
            (f"exhausted escalation: {2} escalation(s), ended on {last}", True),
        ]
        for text, want in written_by_fail:
            self.assertEqual(code_tasks._is_capability_failure(text), want, text)

    def test_killed_run_process_is_not(self):
        self.assertFalse(code_tasks._is_capability_failure(
            "reset-stale: owning run process died"))
        self.assertFalse(code_tasks._is_capability_failure(
            "interrupted: run process exited before the task finished"))

    def test_harness_crash_is_not(self):
        self.assertFalse(code_tasks._is_capability_failure(
            "run crashed: driver 400"))

    def test_infrastructure_failures_stay_out(self):
        """The other strings that reach a `failed` row without the model having
        had a fair chance. Restoring is right for these."""
        for text in ("push failed: could not read from remote repository",
                     "could not open PR: gh not authenticated",
                     "PR #7 conflicts with main: still conflicting after 12 "
                     "resync(s)"):
            self.assertFalse(code_tasks._is_capability_failure(text), text)


class ResumePlanning(unittest.TestCase):
    """build_code_graph reports its resume plan via the run.resume event."""

    def plan(self, prior):
        ts = code_tasks.load_taskfile(taskfile([BASIC]))
        with capture_events() as ev:
            code_tasks.build_code_graph(FakeStore(prior), ts, taskfile="tf.json")
        return ev.first("run.resume") or {}

    def test_merged_task_is_skipped(self):
        p = self.plan([{"id": "t1", "status": "merged", "model": config.ESCALATION_PATH[0],
                        "error": None}])
        self.assertEqual(p["skipped_merged"], ["t1"])
        self.assertEqual(p["retried"], [])

    def test_capability_failure_escalates_one_tier(self):
        p = self.plan([{"id": "t1", "status": "failed", "model": config.ESCALATION_PATH[0],
                        "error": "exhausted fix rounds"}])
        self.assertEqual(p["escalated_on_resume"],
                         {"t1": config.ESCALATION_PATH[1]},
                         "a capability failure at the entry tier moves up one")

    def test_killed_run_resumes_at_the_same_tier(self):
        """The regression that put four tasks on one scarce tier at once."""
        p = self.plan([{"id": "t1", "status": "failed", "model": config.ESCALATION_PATH[0],
                        "error": "reset-stale: owning run process died"}])
        self.assertEqual(p["escalated_on_resume"], {},
                         "an interrupted run is not evidence the model was too weak")

    def test_conflict_resumes_at_the_same_tier(self):
        p = self.plan([{"id": "t1", "status": "conflict", "model": "GLM-5.3",
                        "error": "merge failed"}])
        self.assertEqual(p["escalated_on_resume"], {})

    def test_escalation_stops_at_the_top_of_the_path(self):
        top = config.ESCALATION_PATH[-1]
        p = self.plan([{"id": "t1", "status": "failed", "model": top,
                        "error": "exhausted fix rounds"}])
        self.assertEqual(p["escalated_on_resume"], {})


class ResumeRestoresAnInterruptedAttempt(unittest.TestCase):
    """A worktree is state: an interrupted attempt's work is restored, not
    discarded. A capability failure starts CLEAN — a gate or a reviewer
    rejected that work, so re-applying it would re-submit what was refused.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        base = Path(self.dir)
        self.repo = base / "proj"
        self.repo.mkdir()
        for a in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"],
                  ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", str(self.repo), *a], check=True,
                           capture_output=True)
        (self.repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (self.repo / ".gitignore").write_text(".arc/\n.reasonix/\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "init"],
                       check=True, capture_output=True)
        self._orig = {k: getattr(config, k) for k in
                      ("WORKTREE_ROOT", "CHECKPOINT_DIR", "BASE_BRANCH")}
        config.WORKTREE_ROOT = str(base / "wts")
        config.CHECKPOINT_DIR = base / "cps"
        config.BASE_BRANCH = "main"
        for k, v in self._orig.items():
            self.addCleanup(setattr, config, k, v)

    def alloc_node(self, prior_rows):
        ts = code_tasks.load_taskfile(taskfile([{**BASIC, "id": "t1"}],
                                               repo=str(self.repo)))
        store = FakeStore(prior_rows)
        # Drive the node the way the engine does, with events captured around
        # the run itself (build_code_graph only plans; the node does the work).
        with capture_events() as ev:
            g = code_tasks.build_code_graph(store, ts, taskfile="tf.json")
            res = asyncio.run(g.nodes["alloc_t1"].fn({}))
        return Path(res["worktree"]), ev

    def _work(self, wt):
        (wt / "calc.py").write_text("def add(a, b):\n    return a + b + 1\n")
        (wt / "untracked.txt").write_text("attempt output\n")
        assert asyncio.run(gitstore.checkpoint(self.repo, "t1", wt, "x1"))

    def test_an_interrupted_row_restores_and_a_capability_failure_does_not(self):
        first = asyncio.run(gitstore.alloc(self.repo, "t1", "main"))
        self._work(first)
        # The run process was killed: that row says nothing about the model, and
        # the work it wrote is what the resume continues from.
        wt, ev = self.alloc_node([
            {"id": "t1", "status": "failed", "model": config.ESCALATION_PATH[0],
             "error": "interrupted: run process exited before the task finished"}])
        self.assertIn("return a + b + 1", (wt / "calc.py").read_text())
        self.assertEqual((wt / "untracked.txt").read_text(), "attempt output\n")
        restored = ev.first("task.checkpoint_restored")
        self.assertIsNotNone(restored, "the resume must say what it restored")
        self.assertEqual(restored["task"], "t1")
        self.assertEqual(restored["files"], 2)

    def test_a_capability_failure_starts_clean(self):
        first = asyncio.run(gitstore.alloc(self.repo, "t1", "main"))
        self._work(first)
        wt, ev = self.alloc_node([
            {"id": "t1", "status": "failed", "model": config.ESCALATION_PATH[0],
             "error": "exhausted fix rounds"}])
        self.assertFalse((wt / "untracked.txt").exists())
        self.assertEqual((wt / "calc.py").read_text(),
                         "def add(a, b):\n    return a + b\n")
        self.assertIsNone(ev.first("task.checkpoint_restored"))

    def test_no_rejected_attempt_is_ever_restored(self):
        """One table over EVERY string that can reach a failed row's error.

        A deny-everything case list rather than one test per string, because
        that is the mistake being guarded here: the restore test used to be a
        DENYLIST of capability words, so each new string publish() or fail()
        grew defaulted to RESTORING — three separate review rounds each found
        one more ("verify gate still failing…", "rework after PR rejection
        produced no changes", …). The strings below are built the same way
        their writers build them.
        """
        attempts, last, rounds = 16, config.ESCALATION_PATH[0], config.PR_MAX_ROUNDS
        rejected = [
            f"verify gate still failing after {attempts} attempt(s) on {last}",
            f"pre-merge review still rejecting after {attempts} attempt(s)",
            (f"PR #{50} rejected after {rounds} review round(s); last had 2 "
             f"unresolved issue(s)"),
            f"exhausted escalation: {2} escalation(s), ended on {last}",
            f"no path forward on {last} after {attempts} attempt(s) "
            f"(no escalation was taken)",
            # publish()
            "rework after PR rejection produced no changes",
            "implementer produced no changes",
            f"push failed: {'could not read from remote repository'}",
            f"could not open PR: {'gh not authenticated'}",
            # A reason nobody has written yet: the safe default is CLEAN.
            "some failure mode a future version of this code introduces",
        ]
        first = asyncio.run(gitstore.alloc(self.repo, "t1", "main"))
        self._work(first)
        for error in rejected:
            with self.subTest(error=error[:48]):
                wt, ev = self.alloc_node([
                    {"id": "t1", "status": "failed",
                     "model": config.ESCALATION_PATH[0], "error": error}])
                self.assertFalse((wt / "untracked.txt").exists(), error)
                self.assertIsNone(ev.first("task.checkpoint_restored"), error)

    def test_the_two_interruption_reasons_do_restore(self):
        """The allowlist, built from the module constants that write them."""
        import reconcile
        from store import Store
        # Exact equality, so the tuple cannot silently drift from the two
        # strings that actually write a row: an added-but-unwritten entry would
        # be dead weight, and a REWORDED writer would stop restoring.
        self.assertEqual(set(code_tasks._INTERRUPTION_REASONS),
                         {reconcile.INTERRUPTED_REASON, Store.STALE_REASON})
        for reason in (reconcile.INTERRUPTED_REASON, Store.STALE_REASON):
            with self.subTest(reason=reason[:40]):
                first = asyncio.run(gitstore.alloc(self.repo, "t1", "main"))
                self._work(first)
                wt, ev = self.alloc_node([
                    {"id": "t1", "status": "failed",
                     "model": config.ESCALATION_PATH[0], "error": reason}])
                self.assertTrue((wt / "untracked.txt").exists(), reason)
                self.assertIsNotNone(ev.first("task.checkpoint_restored"),
                                     reason)

    def test_a_merged_task_is_never_restored(self):
        """A merged task has no alloc node at all: it becomes a skip stub, so
        there is no reset to restore from and no chance of re-running it."""
        first = asyncio.run(gitstore.alloc(self.repo, "t1", "main"))
        self._work(first)
        ts = code_tasks.load_taskfile(taskfile([{**BASIC, "id": "t1"}],
                                               repo=str(self.repo)))
        with capture_events() as ev:
            g = code_tasks.build_code_graph(
                FakeStore([{"id": "t1", "status": "merged",
                            "model": config.ESCALATION_PATH[0], "error": None}]),
                ts, taskfile="tf.json")
        self.assertNotIn("alloc_t1", g.nodes)
        self.assertIsNone(ev.first("task.checkpoint_restored"))

    def test_the_tail_checkpoints_only_its_own_task(self):
        """A drain fires the tail while siblings are still being implemented.
        A sweep over EVERY worktree would read (and, before the capture became
        read-only, `git add -N`) a tree a live agent is writing to. One node per
        task, each reading only its own worktree."""
        ts = code_tasks.load_taskfile(taskfile(
            [{**BASIC, "id": "a"}, {**BASIC, "id": "b", "deps": ["a"]}],
            repo=str(self.repo)))
        with capture_events():
            g = code_tasks.build_code_graph(FakeStore([]), ts, taskfile="tf.json")
        self.assertIn("checkpoint_a", g.nodes)
        self.assertIn("checkpoint_b", g.nodes)
        # Each is fed by its OWN failure, never by a shared gather node.
        self.assertTrue(any(e.src == "fail_a" and e.dst == "checkpoint_a"
                            for e in g.edges))
        self.assertTrue(any(e.src == "fail_b" and e.dst == "checkpoint_b"
                            for e in g.edges))
        self.assertFalse(any(e.src == "fail_b" and e.dst == "checkpoint_a"
                             for e in g.edges))

    def test_the_tail_hangs_off_failure_not_the_merge(self):
        """pr_merge calls gitstore.cleanup before it returns — the worktree is
        already gone, so a tail node downstream of it would save nothing. The
        path where work actually survives is `fail`."""
        ts = code_tasks.load_taskfile(taskfile([{**BASIC, "id": "a"}],
                                               repo=str(self.repo)))
        with capture_events():
            g = code_tasks.build_code_graph(FakeStore([]), ts, taskfile="tf.json")
        self.assertTrue(any(e.src == "fail_a" and e.dst == "checkpoint_a"
                            for e in g.edges))
        self.assertFalse(any(e.src == "pr_merge_a" and e.dst == "checkpoint_a"
                             for e in g.edges))

    def test_the_tail_node_reads_the_worktree_it_names(self):
        """Driven directly: it checkpoints the alloc'd worktree and skips a task
        that never allocated, instead of sweeping the whole fleet."""
        ts = code_tasks.load_taskfile(taskfile(
            [{**BASIC, "id": "a"}, {**BASIC, "id": "b", "deps": ["a"]}],
            repo=str(self.repo)))
        with capture_events():
            g = code_tasks.build_code_graph(FakeStore([]), ts, taskfile="tf.json")
        wt = asyncio.run(gitstore.alloc(self.repo, "a", "main"))
        (wt / "half-done.txt").write_text("still here\n")
        # ctx as the engine builds it: only a's alloc ran. b was never reached,
        # and must not be read or written by a's tail node.
        ctx = {"results": {"alloc_a": {"worktree": str(wt)}}}
        res = asyncio.run(g.nodes["checkpoint_a"].fn(ctx))
        self.assertIn("half-done.txt", Path(res["saved"]).read_text())
        self.assertEqual(gitstore.checkpoint_files(self.repo, "b"), [])

    def test_a_missing_alloc_is_skipped_not_guessed(self):
        ts = code_tasks.load_taskfile(taskfile([{**BASIC, "id": "a"}],
                                               repo=str(self.repo)))
        with capture_events():
            g = code_tasks.build_code_graph(FakeStore([]), ts, taskfile="tf.json")
        res = asyncio.run(g.nodes["checkpoint_a"].fn({"results": {}}))
        self.assertEqual(res["saved"], None)

    def test_a_resume_that_never_allocated_still_finds_its_worktree(self):
        """A resume starting at publish never ran alloc in THIS graph, so the
        results hold no worktree — the node must ask git, like every other node
        does, instead of giving up and letting the next alloc reset the work."""
        ts = code_tasks.load_taskfile(taskfile([{**BASIC, "id": "a"}],
                                               repo=str(self.repo)))
        with capture_events():
            g = code_tasks.build_code_graph(FakeStore([]), ts, taskfile="tf.json")
        wt = asyncio.run(gitstore.alloc(self.repo, "a", "main"))
        (wt / "left-behind.txt").write_text("work survives\n")
        res = asyncio.run(g.nodes["checkpoint_a"].fn({"results": {}}))
        self.assertIn("left-behind.txt", Path(res["saved"]).read_text())

    def test_a_crashed_attempt_still_checkpoints_what_it_wrote(self):
        """The crash path RETURNS from its except, so a checkpoint placed after
        the try never ran for it: an attempt that wrote files and then died
        saved nothing, and a reboot — which runs no `finally` at all — lost the
        work outright. The checkpoint belongs in a `finally`."""
        ts = code_tasks.load_taskfile(taskfile([{**BASIC, "id": "a"}],
                                               repo=str(self.repo)))
        with capture_events():
            g = code_tasks.build_code_graph(FakeStore([]), ts, taskfile="tf.json")
        wt = asyncio.run(gitstore.alloc(self.repo, "a", "main"))
        (wt / "half-written.txt").write_text("wrote this, then died\n")

        class _Boom:
            model, harness, images = BASIC["model"], "fake", None

            async def run(self, prompt, cwd, task_id=None, **kw):
                raise code_tasks.DriverError("harness fell over")

        orig = code_tasks._driver
        code_tasks._driver = lambda model, role, pol: _Boom()
        try:
            out = asyncio.run(g.nodes["implement_a"].fn(
                {"results": {"alloc_a": {"worktree": str(wt)}}, "runs": {}}))
        finally:
            code_tasks._driver = orig
        self.assertTrue(out["crashed"])
        saved = gitstore.checkpoint_files(self.repo, "a")
        self.assertEqual([m["label"] for _, m in saved], ["x1"],
                         "a crashed attempt must still save what it wrote")
        self.assertIn("half-written.txt", saved[0][1]["files"])
        self.assertEqual(saved[0][1]["attempt"], 1)

    def test_a_successful_attempt_checkpoints_too(self):
        """The same finally covers the success path — one code path, not two."""
        ts = code_tasks.load_taskfile(taskfile([{**BASIC, "id": "a"}],
                                               repo=str(self.repo)))
        with capture_events():
            g = code_tasks.build_code_graph(FakeStore([]), ts, taskfile="tf.json")
        wt = asyncio.run(gitstore.alloc(self.repo, "a", "main"))

        class _Ok:
            model, harness, images = BASIC["model"], "fake", None

            async def run(self, prompt, cwd, task_id=None, **kw):
                (Path(cwd) / "done.txt").write_text("attempt output\n")
                return mock.Mock(session_id="s-1", text="ok", harness="fake",
                                 model=BASIC["model"], exit_code=0,
                                 transcript_path="", seconds=1.0)

        orig = code_tasks._driver
        code_tasks._driver = lambda model, role, pol: _Ok()
        try:
            out = asyncio.run(g.nodes["implement_a"].fn(
                {"results": {"alloc_a": {"worktree": str(wt)}}, "runs": {}}))
        finally:
            code_tasks._driver = orig
        self.assertEqual(out["session_id"], "s-1")
        saved = gitstore.checkpoint_files(self.repo, "a")
        self.assertEqual([m["label"] for _, m in saved], ["x1"])
        self.assertIn("done.txt", saved[0][1]["files"])


class ExtractPlanJson(unittest.TestCase):
    def test_finds_the_plan_among_prose_and_fences(self):
        doc = json.dumps({"project": {"repo": "/tmp", "tasks": []}})
        text = f"Here is the plan:\n```json\n{doc}\n```\nHope that helps."
        self.assertEqual(json.loads(code_tasks._extract_plan_json(text)),
                         json.loads(doc))

    def test_returns_none_when_absent(self):
        self.assertIsNone(code_tasks._extract_plan_json('{"not": "a plan"}'))


class GateFeedbackNamesTheFailures(unittest.TestCase):
    """The fix loop must hand the implementer WHAT failed, not THAT it failed.

    The gate feedback used to be the last 2000 characters of output — on a
    check.sh run that is the unittest summary and the shell's echo, which say
    nothing. empty-diff-publish failed its worktree gate on exactly one of
    1046 tests, the FAIL line sat a hundred lines above the cut, and the
    implementer was told only "unit tests failed" — twice.
    """

    def test_names_unittest_failures_above_the_tail_cut(self):
        out = ("ok\n" * 200
               + "FAIL: test_every_edge_condition_reads_as_english "
                 "(tests.test_x.X)\n"
               + "AssertionError: 'merged' != 'conflict'\n"
               + "ok\n" * 200
               + "FAILED (failures=1)\n")
        names = code_tasks._gate_failures(out)
        self.assertTrue(any(n.startswith("FAIL: test_every_edge_condition")
                            for n in names), names)
        self.assertTrue(any(n.startswith("AssertionError:") for n in names),
                        names)

    def test_a_clean_suite_names_nothing(self):
        self.assertEqual(code_tasks._gate_failures("ok\n" * 50 + "OK\n"), [])

    def test_dedups_and_caps(self):
        out = ("FAILED tests/test_a.py\n" * 30 + "not ok 1 smoke\n" * 3)
        names = code_tasks._gate_failures(out, limit=2)
        self.assertEqual(len(names), 2)
        self.assertEqual(names[0], "FAILED tests/test_a.py")

    def test_mid_line_mentions_do_not_count(self):
        # Anchored at line start: prose ABOUT a failure is not the failure.
        self.assertEqual(code_tasks._gate_failures(
            "rerun with FAILED verbosity for details\n"), [])

    def test_names_pytest_default_failures_section(self):
        # Default pytest output (no -rf): the check names live in the
        # FAILURES-section underlines and the E-prefixed exception lines —
        # without patterns for them a pytest-repo gate silently got the old
        # nameless feedback.
        out = (".....F..\n"
               + "=" * 32 + " FAILURES " + "=" * 32 + "\n"
               + "_" * 20 + " test_totals_handle_empty_days " + "_" * 20 + "\n"
               + "\n    def test_totals_handle_empty_days():\n"
               + ">       assert totals() == 0\n"
               + "E       AssertionError: assert {'n': 0} == 0\n"
               + "\ntests/test_usage.py:44: AssertionError\n"
               + "=" * 74 + "\n1 failed, 8 passed in 0.42s\n")
        names = code_tasks._gate_failures(out)
        self.assertTrue(any("test_totals_handle_empty_days" in n
                            for n in names), names)
        self.assertTrue(any(n.startswith("E") and "AssertionError" in n
                            for n in names), names)

    def test_names_block_is_failure_only(self):
        # A green run whose output merely LOOKS like a failure (an
        # expected-error test printing its caught traceback, a TAP '# TODO'
        # not-ok) must not get a "failing checks" header — the names block is
        # feedback, and pass-path output is only observability.
        src = pathlib.Path(code_tasks.__file__).read_text()
        self.assertIn("names if not passed else None", src)

    def test_gate_feedback_lists_names_before_the_tail(self):
        # Wiring, pinned by source slice like the other ownership tests. The
        # names block now lives in gate_feedback() (with the failing sections
        # and the log path), so the slice covers that function plus its call.
        src = pathlib.Path(code_tasks.__file__).read_text()
        body = src[src.index("async def gate(ctx):"):]
        body = body[:body.index("async def review(ctx):")]
        self.assertIn("_gate_failures(full)", body)
        self.assertIn("gate_feedback(full, log_path", body)
        fn = src[src.index("def gate_feedback("):]
        fn = fn[:fn.index("\ndef save_review_log(")]
        self.assertIn('"failing checks:\\n"', fn)
        # Sections before the tail: the order is the point of the function.
        self.assertLess(fn.index("failing sections:"), fn.index("gate output (tail):"))

    def test_the_full_fail_list_survives_the_log_tail_cut(self):
        """Measured 2026-09-15: a gate kept 1 of 15 failing test names.

        The FAIL: lines sat at the START of a >2000-char check.sh run, the
        kept window was the unittest summary and the shell's echo, and three
        worktrees spent a fix round hunting the rest. The full `FAIL:`/
        `ERROR:` list now rides beside the tail in the log file and in the
        event, so it is readable without opening a 20k-line log.
        """
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="arc-qa-gate-log-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        head = ("FAIL: test_alpha_fails (tests.test_x.X)\n"
                "FAIL: test_beta_fails (tests.test_x.X)\n")
        filler = "".join(f"pad line {i}\n" for i in range(400))
        (tmp / "gate-output.txt").write_text(head + filler
                                             + "FAILED (failures=2)\n")
        task = dict(BASIC, verify_cmd=f"cat {tmp / 'gate-output.txt'}; exit 1")
        ts = code_tasks.load_taskfile(taskfile([task]))
        with capture_events():
            g = code_tasks.build_code_graph(FakeStore(), ts, taskfile="tf.json")
        ctx = {"results": {"alloc_t1": {"worktree": str(tmp)},
                           "implement_t1": {"harness": "x"}}, "runs": {}}
        # config.ROOT decides where the gate log lands: point it at the temp
        # worktree so the run does not write into the repo's own logs/gates.
        with mock.patch.object(config, "ROOT", str(tmp)):
            with capture_events() as ev:
                res = asyncio.run(g.nodes["gate_t1"].fn(ctx))
        self.assertFalse(res["passed"])
        # The cut, precisely: the raw output's own last 2000 characters —
        # which is ALL the log file used to hold — reach neither FAIL: line.
        raw_tail = (head + filler + "FAILED (failures=2)\n")[-2000:]
        self.assertNotIn("FAIL: test_alpha_fails", raw_tail)
        self.assertNotIn("FAIL: test_beta_fails", raw_tail)
        log = pathlib.Path(res["log_path"]).read_text()
        self.assertIn(code_tasks._GATE_FULL_LIST_HEADER, log)
        # Both names survive in the log's own kept window.
        self.assertIn("FAIL: test_alpha_fails", log[-2000:])
        self.assertIn("FAIL: test_beta_fails", log[-2000:])
        gate_ev = ev.of("task.gate")[-1]
        self.assertIn(code_tasks._GATE_FULL_LIST_HEADER, gate_ev["tail"])
        self.assertIn("FAIL: test_beta_fails", gate_ev["tail"])


class TheGateLogIsKeptWholeAndCapped(unittest.TestCase):
    """Rule 4: the gate's FULL output lands on disk, bounded by a byte cap.

    The file used to hold `full` and nothing capped it; the feedback handed to
    the implementer was `full[-2000:]`, which on a long test run is the
    unittest summary and the shell's echo — the failure itself is above the
    cut. These tests pin the three things a fix round depends on: the file
    exists, it cannot grow without bound, and what the implementer reads is
    the failing SECTIONS rather than only the tail.
    """

    def _run_gate(self, output_text, verify_cmd=None):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="arc-gate-log-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "gate-output.txt").write_text(output_text)
        cmd = verify_cmd or f"cat {tmp / 'gate-output.txt'}; exit 1"
        task = dict(BASIC, verify_cmd=cmd)
        ts = code_tasks.load_taskfile(taskfile([task]))
        with capture_events():
            g = code_tasks.build_code_graph(FakeStore(), ts, taskfile="tf.json")
        ctx = {"results": {"alloc_t1": {"worktree": str(tmp)},
                           "implement_t1": {"harness": "x"}}, "runs": {}}
        with mock.patch.object(config, "ROOT", str(tmp)):
            with capture_events() as ev:
                res = asyncio.run(g.nodes["gate_t1"].fn(ctx))
        return tmp, res, ev

    def test_the_log_is_written_and_capped_at_the_configured_size(self):
        """A runaway gate cannot fill the disk, and says what it dropped."""
        # A distinctive head, then ~400 KB of filler, then a distinctive
        # tail. Head and tail are BOTH retained on purpose, so the marker for
        # what was dropped belongs at the cut, not near the start.
        head = "FIRST LINE OF THE RUN\n"
        tail = "FAILED (failures=1)\n"
        body = head + "pad\n" * 100_000 + tail
        self.assertGreater(len(body), 50_000)
        with mock.patch.object(config, "GATE_LOG_MAX_BYTES", 20_000):
            _, res, _ = self._run_gate(body)
        log = pathlib.Path(res["log_path"])
        self.assertTrue(log.is_file(), "the gate log must be written")
        raw = log.read_bytes()
        # The cap is on the FILE, marker included — 20 KB, not 20 KB + marker.
        self.assertLessEqual(len(raw), 20_000)
        text = raw.decode("utf-8", errors="replace")
        self.assertIn("bytes omitted", text)
        # Head AND tail survive: the first error is at the top, the summary at
        # the end, and a human opens this file to find either.
        self.assertIn(head.strip(), text)
        self.assertIn(tail.strip(), text)
        # What the cap bought: a fraction of the gate's own output.
        self.assertLess(len(raw), len(body.encode("utf-8")) // 10)

    def test_cap_log_keeps_head_and_tail_and_drops_only_the_middle(self):
        """The dropped span is the middle, named by its byte count."""
        text = "HEAD" + "M" * 5000 + "TAIL"
        out = code_tasks.cap_log(text, 400)
        self.assertLessEqual(len(out.encode("utf-8")), 400)
        self.assertTrue(out.startswith("HEAD"), out[:20])
        self.assertTrue(out.endswith("TAIL"), out[-20:])
        self.assertIn("bytes omitted", out)
        # The 5000-char run of M is broken in the middle: head and tail each
        # keep a stub, and NO unbroken run survives the drop. Counting "MMMM"
        # outright would fail — both stubs are M runs — so the run LENGTH is
        # what has to be bounded.
        self.assertLess(out.count("M"), 5000)
        self.assertLess(max(len(r) for r in re.findall(r"M+", out)), 250)

    def test_the_uncapped_log_is_byte_identical_to_the_gate_output(self):
        """Under the cap nothing is altered: no marker, no reordering."""
        body = "alpha\nbeta\nFAILED (failures=1)\n"
        _, res, _ = self._run_gate(body)
        self.assertEqual(pathlib.Path(res["log_path"]).read_text(), body)

    def test_the_implementer_gets_the_failing_block_not_just_the_tail(self):
        """The traceback under FAIL: is what a fix round needs, and it sat
        above the 2000-char cut."""
        block = ("FAIL: test_alpha (tests.test_x.X)\n"
                 + "-" * 70 + "\n"
                 "Traceback (most recent call last):\n"
                 '  File "/w/tests/test_x.py", line 12, in test_alpha\n'
                 "    self.assertEqual(1, 2)\n"
                 "AssertionError: 1 != 2\n\n")
        _, res, _ = self._run_gate(block + "pad line\n" * 1000
                                   + "FAILED (failures=1)\n")
        out = res["output"]
        self.assertFalse(res["passed"])
        # The assertion — the WHY — is in the feedback, ahead of the tail.
        self.assertIn("AssertionError: 1 != 2", out)
        self.assertIn("FAIL: test_alpha", out)
        self.assertLess(out.index("AssertionError: 1 != 2"),
                        out.index("gate output (tail)"))
        # The blind tail reaches none of it: that is the bug this fixed.
        self.assertNotIn("AssertionError",
                         (block + "pad line\n" * 1000 + "FAILED (failures=1)\n")[-2000:])
        # Bounded like the window it replaced.
        self.assertLessEqual(len(out.encode("utf-8")), 6000)
        # And the implementer is told where the rest is.
        self.assertIn(res["log_path"], out)

    def _full_head(self):
        """A gate log whose failing sections and names both hit their caps.

        Three 1500-byte sections are the worst case for the head, and a 40-name
        list is the worst case for the names block — together they exceed the
        whole budget, which is exactly what the rejected version mishandled.
        """
        def sec(i):
            return ((f"FAIL: test_s{i} (t.T)\n")
                    + "".join("  " + "y" * 198 + "\n" for _ in range(8)))
        out = ("\n".join(sec(i) for i in (1, 2, 3))
               + "\npad line\n" * 500 + "FAILED (failures=3)\n")
        names = [f"test_a_very_long_failing_check_name_{i:03d}" for i in range(40)]
        return out, names

    def test_the_log_path_survives_a_head_that_fills_the_budget(self):
        """The rejected version evicted the path FIRST — it was appended after
        the tail and the block was sliced from the front, so three capped
        sections produced 6000 bytes with no path at all. The path is the one
        line a reader cannot reconstruct, so it is reserved before the rest."""
        out, names = self._full_head()
        path = "/logs/gates/proj/t1/x1.log"
        self.assertEqual(len(code_tasks._failing_sections(out)), 3)
        fb = code_tasks.gate_feedback(out, path, names=names)
        self.assertIn(path, fb)

    def test_feedback_never_exceeds_its_budget(self):
        """Reserving the path must not push the block over the bound: the
        rejected version returned 6221 bytes with NEITHER path nor tail.
        Measured in bytes, like the log cap, since the budget is a prompt
        budget and the output is multi-byte."""
        out, names = self._full_head()
        path = "/logs/gates/proj/t1/x1.log"
        for label, fb in [
            ("sections+names", code_tasks.gate_feedback(out, path, names=names)),
            ("sections only", code_tasks.gate_feedback(out, path)),
            ("names only", code_tasks.gate_feedback("plain\n" * 3000, path,
                                                    names=names)),
            ("everything huge", code_tasks.gate_feedback(out + "z" * 100_000,
                                                         path, names=names)),
        ]:
            with self.subTest(label):
                self.assertLessEqual(len(fb.encode("utf-8")), 6000)
                self.assertIn(path, fb)
                self.assertIn("gate output (tail)", fb)

    def test_a_clipped_head_keeps_whole_names(self):
        """A cut mid-line would leave half a test name, which reads as a real
        one — the one thing the names block exists to prevent."""
        out, names = self._full_head()
        fb = code_tasks.gate_feedback(out, "/logs/x.log", names=names)
        for line in fb.splitlines():
            if line.startswith("  test_a_very_long"):
                self.assertIn(line.strip(), names)

    def test_a_godot_fail_line_is_extracted(self):
        """Godot's own gate output: SCRIPT ERROR plus its stack, and FAIL:."""
        godot = ("Godot Engine v4.4\n"
                 "SCRIPT ERROR: Invalid call. Nonexistent function 'foo'.\n"
                 "          at: push_error (core/variant/variant_utility.cpp:1090)\n"
                 "FAIL: [SceneTree] res://test_scene.gd:12 - expected 3 got 4\n"
                 + "pad line\n" * 1000 + "exit 1\n")
        _, res, _ = self._run_gate(godot)
        out = res["output"]
        self.assertFalse(res["passed"])
        self.assertIn("SCRIPT ERROR", out)
        self.assertIn("test_scene.gd:12", out)
        # The stack frame belongs to the error it explains.
        self.assertIn("push_error", out)

    def test_a_passing_gate_is_still_just_the_tail(self):
        """On a pass the output is observability, not feedback: no failing
        sections header over green output (a caught exception printed by an
        expected-error test would make it a lie)."""
        _, res, _ = self._run_gate("a caught AssertionError: fine\ndone\n",
                                   verify_cmd="true")
        self.assertTrue(res["passed"])
        self.assertNotIn("failing sections:", res["output"])

    def test_cap_log_is_utf8_safe_and_counts_bytes(self):
        """The cap is a DISK budget: a multi-byte char is 3 bytes, and a cut
        must never land mid-codepoint."""
        text = "中" * 1000
        out = code_tasks.cap_log(text, 300)
        self.assertLessEqual(len(out.encode("utf-8")), 300)
        out.encode("utf-8")            # a mid-codepoint cut would raise here
        self.assertIn("bytes omitted", out)

    def test_the_gate_event_names_the_log(self):
        """Rule 7: an operator reads the event to find the log."""
        _, res, ev = self._run_gate("FAILED (failures=1)\n")
        gate_ev = [e for e in ev.of("task.gate") if e.get("passed") is False][-1]
        self.assertEqual(gate_ev["log"], res["log_path"])
        self.assertTrue(gate_ev["log"])

    def test_the_log_sits_under_the_project_and_task(self):
        """The layout Rule 4 specifies: logs/gates/<project>/<task>/x<n>.log.

        Asserted by SHAPE, not by the sandbox's temp path: the gate log and
        the raw reviewer output share this directory so one task's whole
        evidence trail is one folder.
        """
        p = pathlib.Path(code_tasks.gate_log_path("my-proj", "my-task", 3))
        self.assertEqual(p.parts[-5:],
                         ("logs", "gates", "my-proj", "my-task", "x3.log"))

    def test_a_slash_in_an_id_cannot_escape_the_log_directory(self):
        """Task ids come from a taskfile: `..` must not climb out."""
        p = pathlib.Path(code_tasks.gate_log_path("../../etc", "..", 1))
        self.assertNotIn("..", p.parts)
        self.assertEqual(p.parts[-2], "x")


class TheRawReviewerOutputIsKept(unittest.TestCase):
    """A reviewer that never emitted a verdict used to leave no evidence.

    The review is retried (correctly — a crash is not a rejection), but the
    retry had nothing to go on: whatever the reviewer wrote INSTEAD of the
    verdict JSON was discarded with the session.
    """

    def _write(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="arc-review-log-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with mock.patch.object(config, "ROOT", str(tmp)):
            path = code_tasks.save_review_log(
                "arc-orchestrator", "t1", 3, "GLM-5.3",
                "I have a question before I can judge this diff: ...")
        return tmp, path

    def test_the_raw_output_is_written_under_the_project_and_task(self):
        tmp, path = self._write()
        self.assertIsNotNone(path)
        rel = pathlib.Path(path).relative_to(tmp)
        self.assertEqual(rel.as_posix(),
                         "logs/gates/arc-orchestrator/t1/review-x3.txt")
        body = pathlib.Path(path).read_text()
        self.assertIn("GLM-5.3", body)
        self.assertIn("I have a question", body)

    def test_an_id_cannot_escape_the_log_directory(self):
        """The id reaches a path: it is slugged, never joined raw."""
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="arc-review-log-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with mock.patch.object(config, "ROOT", str(tmp)):
            path = code_tasks.save_review_log("../../etc", "..", 1, "m", "x")
        self.assertTrue(pathlib.Path(path).resolve().is_relative_to(tmp.resolve()))

    def test_a_write_failure_never_raises(self):
        """Instrumentation must not turn a handled crash into an unhandled
        one — the review is retried either way."""
        with mock.patch.object(config, "ROOT", "/proc/nonexistent/nowhere"):
            self.assertIsNone(
                code_tasks.save_review_log("p", "t", 1, "m", "text"))

    def test_the_reviewer_paths_save_their_raw_output(self):
        """Wiring, pinned by source slice: BOTH no-verdict paths (pre-merge
        and PR) keep the file, and neither returns without naming it."""
        src = pathlib.Path(code_tasks.__file__).read_text()
        self.assertGreaterEqual(src.count("save_review_log("), 4)  # def + 3 uses
        self.assertIn('fingerprint="reviewer.no_verdict"', src)
        body = src[src.index("async def review(ctx):"):]
        body = body[:body.index("async def publish(ctx):")]
        self.assertIn("save_review_log", body)


class AFixRoundContinuesTheHarnessSession(unittest.TestCase):
    """A rework is the same model mending the same worktree: reuse its session.

    Re-reading the repo is the dominant cost of a hard task (60–85 minutes
    measured before GLM-5.3's first edit), and every fix round re-paid it from
    scratch. A tier change starts fresh — another model owns no part of the
    previous session, and one harness may not read another's session files.
    """

    def test_same_model_continues(self):
        results = {"implement_t1": {"session_id": "s-1", "model": "GLM-5.3",
                                    "harness": "opencode"}}
        self.assertEqual(
            code_tasks._resume_session(results, "t1", "GLM-5.3", "opencode"),
            "s-1")

    def test_other_harness_session_is_not_resumed(self):
        # The live failure: GPT-6-Sol's fix round ran `codex exec resume` on
        # a Cursor chat id and exited with "no rollout found".
        results = {"implement_t1": {"session_id": "ffc0a77b", "model": "GPT-6-Sol",
                                    "harness": "cursor"}}
        self.assertIsNone(
            code_tasks._resume_session(results, "t1", "GPT-6-Sol", "codex"))

    def test_session_without_a_harness_is_not_resumed(self):
        results = {"implement_t1": {"session_id": "s-1", "model": "GLM-5.3"}}
        self.assertIsNone(
            code_tasks._resume_session(results, "t1", "GLM-5.3", "opencode"))

    def test_escalation_starts_fresh(self):
        results = {"implement_t1": {"session_id": "s-1", "model": "GLM-5.3"}}
        other = next(m for m in config.ESCALATION_PATH if m != "GLM-5.3")
        self.assertIsNone(code_tasks._resume_session(results, "t1", other))

    def test_a_crash_starts_fresh(self):
        # The crash path returns no model/session_id, so the next attempt is
        # cold — continuing a dead harness's session re-enters its crash.
        results = {"implement_t1": {"crashed": True, "harness": "opencode"}}
        self.assertIsNone(code_tasks._resume_session(results, "t1", "GLM-5.3"))

    def test_first_attempt_and_missing_results_start_fresh(self):
        self.assertIsNone(code_tasks._resume_session({}, "t1", "GLM-5.3"))
        self.assertIsNone(code_tasks._resume_session(None, "t1", "GLM-5.3"))


class ReviewsAvoidTheModelThatWroteTheDiff(unittest.TestCase):
    """A usage swap records a different author than the assigned seat.

    Reviews that avoid only the assigned family can land on the harness
    that actually wrote the code.
    """

    def _pair(self):
        models = list(config.MODEL_FAMILY)
        self.assertGreaterEqual(len(models), 2)
        return models[0], models[1]

    def test_the_implement_result_wins_over_the_assigned_seat(self):
        wrote, assigned = self._pair()
        ctx = {"results": {"implement_t1": {"model": wrote}}}
        self.assertEqual(code_tasks.wrote_the_code(ctx, "t1", assigned), wrote)

    def test_a_resume_reads_the_last_successful_implementer_row(self):
        wrote, assigned = self._pair()
        class Store:
            def harness_runs_prefix(self, tid):
                self.tid = tid
                return [
                    {"task_id": tid, "role": "implementer", "exit_code": 1,
                     "model": assigned},
                    {"task_id": tid, "role": "implementer", "exit_code": 0,
                     "model": wrote},
                    {"task_id": tid, "role": "reviewer", "exit_code": 0,
                     "model": assigned},
                    {"task_id": tid + "0", "role": "implementer", "exit_code": 0,
                     "model": assigned},
                ]
        store = Store()
        self.assertEqual(
            code_tasks.wrote_the_code({"results": {}}, "t1", assigned, store), wrote)
        self.assertEqual(store.tid, "t1")

    def test_without_a_record_the_assigned_seat_stands(self):
        _wrote, assigned = self._pair()
        self.assertEqual(
            code_tasks.wrote_the_code({"results": {}}, "t1", assigned), assigned)

    def test_review_nodes_ask_who_wrote_the_diff(self):
        src = pathlib.Path(code_tasks.__file__).read_text()
        for fn in ("async def review(ctx):", "async def pr_fanout(ctx):",
                   "async def pr_reviewer(ctx):"):
            body = src[src.index(fn):]
            body = body[:body.index("\n        async def ")]
            self.assertIn("wrote_the_code(", body, fn)

    def test_implement_passes_the_session_through(self):
        src = pathlib.Path(code_tasks.__file__).read_text()
        body = src[src.index("async def implement(ctx):"):]
        body = body[:body.index("async def gate(ctx):")]
        self.assertIn("session_id=resume", body)
        self.assertIn('"session_id": res.session_id', body,
                      "the next round needs the id this one produced")


class GateTimeoutKillsTheWholeTree(unittest.TestCase):
    """A verify_cmd is a shell pipeline, and the shell is the least of it.

    `./check.sh && grep ...` runs a unittest suite, node and git under
    /bin/sh. On timeout the gate killed the shell alone; the test runner it
    had started kept running in the worktree, blocked forever on a stdout
    pipe nobody was reading, and nothing ever reported it.
    """

    def test_a_timed_out_gate_leaves_no_grandchild_behind(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="arc-qa-gate-pg-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        pidfile = tmp / "grandchild.pid"
        task = dict(BASIC, verify_cmd=f"sleep 60 & echo $! > {pidfile}; wait")
        ts = code_tasks.load_taskfile(taskfile([task]))
        with capture_events():
            g = code_tasks.build_code_graph(FakeStore(), ts, taskfile="tf.json")
        ctx = {"results": {"alloc_t1": {"worktree": str(tmp)},
                           "implement_t1": {"harness": "x"}}, "runs": {}}
        orig = config.GATE_TIMEOUT
        config.GATE_TIMEOUT = 0.5
        try:
            with capture_events():
                res = asyncio.run(g.nodes["gate_t1"].fn(ctx))
        finally:
            config.GATE_TIMEOUT = orig
        self.assertFalse(res["passed"])
        self.assertIn("timed out", res["output"])
        self.assertTrue(pidfile.is_file(), "the gate shell never started its child")
        pid = int(pidfile.read_text().strip())
        self.addCleanup(_reap, pid)
        self.assertTrue(_gone(pid), f"grandchild {pid} survived the gate kill")


if __name__ == "__main__":
    unittest.main()


class ImplementPromptDiscipline(unittest.TestCase):
    """The prompt carries the mitigation for the fleet's dominant failure.

    ARC terminates long-running requests. Measured on this fleet: tasks doing
    whole-file rewrites lost 33% of their requests to termination against an
    8.3% baseline, each costing ~5 minutes of retry. Response length is the
    variable the implementer actually controls.
    """

    def test_prompt_forbids_whole_file_rewrites(self):
        p = code_tasks._impl_prompt(
            {"id": "t", "title": "T", "prompt": "rewrite index.html",
             "files_hint": [], "model": "", "reviewer": ""}, "")
        self.assertIn("NEVER rewrite a whole file", p)

    def test_prompt_tells_the_agent_to_read_narrowly(self):
        # Without a code graph (tests/test_graft.py covers the graph case);
        # pinned so the assertion does not depend on whether this machine
        # has the graft binary.
        import graft
        saved = dict(graft._bin_cache)
        graft._bin_cache.update(checked=True, path=None)
        try:
            p = code_tasks._impl_prompt(
                {"id": "t", "title": "T", "prompt": "x", "files_hint": [],
                 "model": "", "reviewer": ""}, "")
        finally:
            graft._bin_cache.clear()
            graft._bin_cache.update(saved)
        for phrase in ("grep/search FIRST", "line ranges", "Do not re-read"):
            self.assertIn(phrase, p)

    def test_review_feedback_still_reaches_the_implementer(self):
        p = code_tasks._impl_prompt(
            {"id": "t", "title": "T", "prompt": "x", "files_hint": [],
             "model": "", "reviewer": ""}, "- missing the null check")
        self.assertIn("missing the null check", p)


class PlanExtraction(unittest.TestCase):
    """The planner's output must survive the trip back from the harness.

    DriverResult.text is capped at the last 3000 characters by
    drivers.parse_transcript. That is harmless for reviewer verdicts (small,
    and scanned backwards) but fatal for a plan: real ones run 6.5-11.5KB, so
    the opening {"project": is always cut off and no balanced span can match.
    The planner completed normally, exited 0, and had its work thrown away
    with "produced no usable JSON" — twice, unnoticed, for hours.
    """

    PLAN = {"project": {"repo": "/tmp", "title": "T", "tasks": [
        {"id": "a", "title": "A", "prompt": "x" * 200, "model": config.ESCALATION_PATH[0],
         "reviewer": config.cross_family_reviewer(config.ESCALATION_PATH[0])}]}}
    DECOY = {"project": {"repo": "/tmp", "title": "T", "tasks": [
        {"id": "a", "title": "A", "prompt": "...", "model": config.ESCALATION_PATH[0],
         "reviewer": config.cross_family_reviewer(config.ESCALATION_PATH[0])}]}}

    def _run(self, transcript_lines, text=""):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            p.write_text("\n".join(json.dumps(l) for l in transcript_lines))
            res = type("R", (), {"transcript_path": str(p), "text": text})()
            return code_tasks._plan_json_from_run(res)

    def test_reads_opencode_event_dialect_transcripts(self):
        """opencode session logs have NO "role" lines: assistant text lives in
        {"type":"text","part":{"text":...}} records. Without reading them a
        completed GLM-via-opencode plan parses as empty and is thrown away
        (first attempt killed exactly so, 2026-09-13)."""
        span = self._run([
            {"type": "step_start", "part": {"type": "step-start"}},
            {"type": "text", "part": {"type": "text", "text": "investigating the repo...\n"}},
            {"type": "text", "part": {"type": "text", "synthetic": True,
                                      "metadata": {"compaction_continue": True},
                                      "text": "Continue if you have next steps."}},
            {"type": "text", "part": {"type": "text", "text": "the plan:\n" + json.dumps(self.PLAN)}},
            {"type": "step_finish", "part": {"type": "step-finish", "reason": "stop"}},
        ])
        self.assertIsNotNone(span, "opencode-dialect plan lost")
        self.assertEqual(json.loads(span)["project"]["tasks"][0]["id"], "a")

    def test_opencode_synthetic_markers_are_not_messages(self):
        """Compaction 'continue' markers must not count as assistant output."""
        self.assertIsNone(self._run([
            {"type": "text", "part": {"type": "text", "synthetic": True,
                                      "text": "Continue if you have next steps."}},
        ]))

    def test_recovers_a_plan_too_large_for_the_truncated_text(self):
        big = json.dumps(self.PLAN)
        self.assertGreater(len(big), 200)
        span = self._run([{"role": "assistant", "content": big}],
                         text=big[-50:])   # what parse_transcript would leave
        self.assertIsNotNone(span, "plan lost to truncation")
        self.assertEqual(json.loads(span)["project"]["tasks"][0]["id"], "a")

    def test_prefers_the_substantive_plan_over_a_placeholder_copy(self):
        """Planners emit an abbreviated sketch first; accepting it would hand
        the fleet tasks whose prompts are literally '...'."""
        span = self._run([
            {"role": "assistant", "content": json.dumps(self.DECOY)
             + "\n\nand the full version:\n" + json.dumps(self.PLAN)},
        ])
        self.assertEqual(len(json.loads(span)["project"]["tasks"][0]["prompt"]), 200)

    def test_last_assistant_message_wins(self):
        first = dict(self.PLAN)
        second = json.loads(json.dumps(self.PLAN))
        second["project"]["tasks"][0]["id"] = "later"
        span = self._run([{"role": "assistant", "content": json.dumps(first)},
                          {"role": "tool", "content": "noise"},
                          {"role": "assistant", "content": json.dumps(second)}])
        self.assertEqual(json.loads(span)["project"]["tasks"][0]["id"], "later")

    def test_returns_none_when_there_is_genuinely_no_plan(self):
        self.assertIsNone(self._run([{"role": "assistant", "content": "sorry"}]))

    def test_falls_back_to_the_text_when_the_transcript_is_unreadable(self):
        res = type("R", (), {"transcript_path": "/nonexistent/x.jsonl",
                             "text": json.dumps(self.PLAN)})()
        self.assertIsNotNone(code_tasks._plan_json_from_run(res))

    def test_placeholder_plans_are_rejected_as_unsubstantive(self):
        self.assertFalse(code_tasks._plan_is_substantive(self.DECOY))
        self.assertTrue(code_tasks._plan_is_substantive(self.PLAN))


class OffPathModelsCanStillEscalate(unittest.TestCase):
    """A model routed explicitly but absent from ESCALATION_PATH must still
    be able to escalate — otherwise removing a tier from the path silently
    strands every task that names it."""

    def plan(self, prior):
        ts = code_tasks.load_taskfile(taskfile([BASIC]))   # BASIC uses gpt-oss-120b
        with capture_events() as ev:
            code_tasks.build_code_graph(FakeStore(prior), ts, taskfile="tf.json")
        return ev.first("run.resume") or {}

    def test_a_model_off_the_path_escalates_into_the_entry_tier(self):
        """A model that is not ON the escalation path counts as below its entry
        tier, so it escalates INTO the path rather than being stuck. gpt-oss was
        the live example; it is retired, so _next_tier is checked directly."""
        self.assertEqual(code_tasks._next_tier("some-off-path-model"),
                         config.ESCALATION_PATH[0])
        self.assertEqual(code_tasks._next_tier(config.ESCALATION_PATH[0]),
                         config.ESCALATION_PATH[1])
        self.assertIsNone(code_tasks._next_tier(config.ESCALATION_PATH[-1]))

    def test_the_top_tier_still_does_not_escalate(self):
        top = config.ESCALATION_PATH[-1]
        p = self.plan([{"id": "t1", "status": "failed", "model": top,
                        "error": "exhausted fix rounds"}])
        self.assertEqual(p["escalated_on_resume"], {})


class PullRequestIsTheGate(unittest.TestCase):
    """Nothing merges until every PR reviewer approves.

    The old flow merged locally and opened the PR afterwards, so reviewers
    could only object to work that had already landed — "send it back" could
    not withhold anything. publish now pushes and opens the PR; pr_merge is
    reachable only through a unanimous pr_review.
    """

    def graph(self):
        ts = code_tasks.load_taskfile(taskfile([BASIC]))
        with capture_events():
            return code_tasks.build_code_graph(FakeStore(), ts, taskfile="tf.json")

    def _edge(self, g, src, dst):
        return next((e for e in g.edges if e.src == src and e.dst == dst), None)

    def test_the_chain_ends_in_pr_merge_not_a_local_merge(self):
        g = self.graph()
        self.assertIn("pr_merge_t1", g.nodes)
        self.assertIn("pr_review_t1", g.nodes)
        # review is now three nodes: fan-out -> one node per reviewer -> join
        self.assertIn("pr_fanout_t1", g.nodes)
        self.assertIn("pr_reviewer_t1", g.nodes)
        self.assertIsNotNone(self._edge(g, "publish_t1", "pr_fanout_t1"))
        self.assertIsNotNone(self._edge(g, "pr_review_t1", "pr_merge_t1"))

    def test_merge_requires_approval(self):
        g = self.graph()
        e = self._edge(g, "pr_review_t1", "pr_merge_t1")
        self.assertFalse(e.when({"approved": False}, {}), "merged without approval")
        self.assertTrue(e.when({"approved": True}, {}))

    def test_rejection_goes_back_to_the_implementer(self):
        g = self.graph()
        e = self._edge(g, "pr_review_t1", "implement_t1")
        self.assertIsNotNone(e, "a rejected PR must return to the implementer")
        self.assertTrue(e.when({"approved": False}, {"runs": {"pr_review_t1": 1}}))
        self.assertFalse(e.when({"approved": True}, {"runs": {"pr_review_t1": 1}}))

    def test_the_review_loop_is_bounded(self):
        g = self.graph()
        back = self._edge(g, "pr_review_t1", "implement_t1")
        fail = self._edge(g, "pr_review_t1", "fail_t1")
        over = {"runs": {"pr_review_t1": config.PR_MAX_ROUNDS}}
        self.assertFalse(back.when({"approved": False}, over),
                         "loops forever past PR_MAX_ROUNDS")
        self.assertTrue(fail.when({"approved": False}, over))

    def test_publish_that_never_opened_a_pr_does_not_reach_review(self):
        g = self.graph()
        # publish now enters the review stage at its FAN-OUT node
        e = self._edge(g, "publish_t1", "pr_fanout_t1")
        self.assertFalse(e.when({"published": False, "reason": "push failed"}, {}))

    def test_tasks_branch_from_the_integration_branch_not_prod(self):
        # Tasks branch from BASE_BRANCH, whatever it is. When BASE and PROD
        # are the same branch that is the single-branch flow, not a bug; the
        # thing that must never happen is a task branching from something the
        # fleet does not merge into.
        self.assertTrue(config.BASE_BRANCH)
        src = pathlib.Path("code_tasks.py").read_text()
        self.assertIn("base = config.BASE_BRANCH", src)
        self.assertNotIn('base = "main"', src)

    def test_approval_parsing_fails_closed(self):
        self.assertFalse(code_tasks._parse_approval("looks fine to me")["approve"])
        self.assertTrue(code_tasks._parse_approval('{"approve": true}')["approve"])
        v = code_tasks._parse_approval(
            'first {"approve": true} then actually {"approve": false, "issues": ["x"]}')
        self.assertFalse(v["approve"], "the last verdict is the reviewer's answer")
        self.assertEqual(v["issues"], ["x"])

    def test_missing_approval_is_marked_truncated(self):
        v = code_tasks._parse_approval(
            "I read the diff and the tests pass locally. "
            "Want me to continue and post the verdict?")
        self.assertFalse(v["approve"])
        self.assertTrue(v.get("truncated"))
        self.assertFalse(
            code_tasks._parse_approval('{"approve": true}').get("truncated"))


class DescribeReportsTheRealBase(unittest.TestCase):
    """`--dry-run` output is what an operator reads before spending tokens; it
    hardcoded base=main long after tasks started branching from development."""

    def test_describe_names_the_configured_base_branch(self):
        ts = code_tasks.load_taskfile(taskfile([BASIC]))
        out = code_tasks.describe(ts)
        self.assertIn(f"base={config.BASE_BRANCH}", out)
        self.assertNotIn("base=main", out) if config.BASE_BRANCH != "main" else None


class ProjectChaining(unittest.TestCase):
    """A taskfile declaring `after` waits for whole upstream taskfiles.

    Per-task `deps` order tasks inside one taskfile; `after` orders whole
    taskfiles: no worktree allocates until every task of every upstream
    taskfile is merged. The failure this guards against is a dependent
    branching from a base that does not contain what it depends on.
    """

    def _depfile(self, d, name, ids):
        p = Path(d) / name
        p.write_text(json.dumps({"project": {
            "repo": "/tmp", "title": name, "tasks": [
                {"id": i, "title": i, "prompt": "x",
                 "model": config.ESCALATION_PATH[0],
                 "reviewer": config.cross_family_reviewer(config.ESCALATION_PATH[0])}
                for i in ids]}}))
        return str(p.resolve())

    def _row(self, tid, status):
        return {"id": tid, "status": status, "model": config.ESCALATION_PATH[0],
                "error": None}

    def _graph(self, ts, store, taskfile_arg="tf.json"):
        with capture_events():
            return code_tasks.build_code_graph(store, ts,
                                               taskfile=taskfile_arg)

    def _edge(self, g, src, dst):
        return next((e for e in g.edges if e.src == src and e.dst == dst), None)

    # -- loader ---------------------------------------------------------

    def test_after_must_be_a_list(self):
        with self.assertRaises(ValueError) as cm:
            code_tasks.load_taskfile(taskfile([BASIC], after="other.json"))
        self.assertIn("must be a list", str(cm.exception))

    def test_after_rejects_self_reference(self):
        p = taskfile([BASIC])
        doc = json.loads(p.read_text())
        doc["project"]["after"] = [str(p)]
        p.write_text(json.dumps(doc))
        with self.assertRaises(ValueError) as cm:
            code_tasks.load_taskfile(p)
        self.assertIn("itself", str(cm.exception))

    def test_missing_dep_files_are_fine_and_dedupe(self):
        """Chains are declared before the upstream projects are planned —
        a dep taskfile that does not exist yet is waited on, not rejected."""
        ts = code_tasks.load_taskfile(
            taskfile([BASIC], after=["ghost.json", "ghost.json"]))
        want = str((Path(config.TASKS_DIR) / "ghost.json").resolve())
        self.assertEqual(ts["after"], [want])

    def test_describe_lists_after(self):
        ts = code_tasks.load_taskfile(taskfile([BASIC], after=["ghost.json"]))
        out = code_tasks.describe(ts)
        self.assertIn("after:", out)
        self.assertIn("ghost.json", out)

    # -- graph wiring ---------------------------------------------------

    def test_without_after_heads_start_directly(self):
        ts = code_tasks.load_taskfile(taskfile([BASIC]))
        g = self._graph(ts, FakeStore())
        self.assertNotIn("chain_wait", g.nodes)
        self.assertIn("alloc_t1", g.starts)

    def test_with_after_every_head_gates_on_chain_wait(self):
        ts = code_tasks.load_taskfile(taskfile([BASIC], after=["ghost.json"]))
        g = self._graph(ts, FakeStore())
        self.assertEqual(g.starts, ["chain_wait"])
        e = self._edge(g, "chain_wait", "alloc_t1")
        self.assertIsNotNone(e)
        self.assertTrue(e.when({"ok": True}, {}))
        self.assertFalse(e.when({"ok": False}, {}),
                         "a blocked chain must not allocate a worktree")

    def test_conflict_repair_head_is_gated_too(self):
        ts = code_tasks.load_taskfile(taskfile([BASIC], after=["ghost.json"]))
        g = self._graph(ts, FakeStore([{"id": "t1", "status": "conflict",
                                        "model": config.ESCALATION_PATH[0],
                                        "error": "merge failed"}]))
        self.assertEqual(g.starts, ["chain_wait"])
        self.assertIsNotNone(self._edge(g, "chain_wait", "publish_t1"))

    def test_merged_skip_head_is_gated_too(self):
        ts = code_tasks.load_taskfile(taskfile([BASIC], after=["ghost.json"]))
        g = self._graph(ts, FakeStore([{"id": "t1", "status": "merged",
                                        "model": config.ESCALATION_PATH[0],
                                        "error": None}]))
        self.assertEqual(g.starts, ["chain_wait"])
        self.assertIsNotNone(self._edge(g, "chain_wait", "publish_t1"))

    def test_after_cycle_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            a = self._depfile(d, "a.json", ["a1"])
            b = self._depfile(d, "b.json", ["b1"])
            for name, dep in (("a.json", b), ("b.json", a)):
                p = Path(d) / name
                doc = json.loads(p.read_text())
                doc["project"]["after"] = [dep]
                p.write_text(json.dumps(doc))
            ts = code_tasks.load_taskfile(Path(d) / "a.json")
            with self.assertRaises(ValueError) as cm:
                self._graph(ts, FakeStore(), taskfile_arg=a)
            self.assertIn("cycle", str(cm.exception))

    # -- chain_status ---------------------------------------------------

    def test_chain_status_requires_every_upstream_id_merged(self):
        with tempfile.TemporaryDirectory() as d:
            dep = self._depfile(d, "dep.json", ["d1", "d2"])
            rows = [self._row("d1", "merged"), self._row("d2", "running")]
            st = code_tasks.chain_status(FakeStore(by_taskfile={dep: rows}), [dep])
            self.assertFalse(st["ok"])
            self.assertEqual(st["waiting"], [dep])
            st = code_tasks.chain_status(FakeStore(by_taskfile={
                dep: [self._row("d1", "merged"), self._row("d2", "merged")]}), [dep])
            self.assertTrue(st["ok"])

    def test_a_killed_upstream_run_is_not_a_merged_project(self):
        """The premature-merge hole: the dep's run process died after 3 of 4
        tasks merged. Row-counting alone sees only merged rows and calls the
        chain ready — so ids are parsed from the taskfile on disk, and the
        task with no row at all (d4) keeps the chain waiting."""
        with tempfile.TemporaryDirectory() as d:
            dep = self._depfile(d, "dep.json", ["d1", "d2", "d3", "d4"])
            rows = [self._row(i, "merged") for i in ("d1", "d2", "d3")]
            st = code_tasks.chain_status(FakeStore(by_taskfile={dep: rows}), [dep])
            self.assertFalse(st["ok"])
            self.assertEqual(st["waiting"], [dep])
            self.assertEqual(st["deps"][0]["unmerged"], ["d4"])

    def test_a_failed_upstream_task_blocks_the_chain(self):
        with tempfile.TemporaryDirectory() as d:
            dep = self._depfile(d, "dep.json", ["d1"])
            st = code_tasks.chain_status(FakeStore(by_taskfile={
                dep: [self._row("d1", "failed")]}), [dep])
            self.assertFalse(st["ok"])
            self.assertEqual(st["failed"], {dep: ["d1"]})

    def test_an_empty_upstream_taskfile_is_vacuously_done(self):
        with tempfile.TemporaryDirectory() as d:
            dep = self._depfile(d, "dep.json", [])
            st = code_tasks.chain_status(FakeStore(), [dep])
            self.assertTrue(st["ok"])

    def test_a_dep_taskfile_not_yet_on_disk_is_waited_on(self):
        ghost = str((Path(tempfile.mkdtemp()) / "ghost.json").resolve())
        st = code_tasks.chain_status(FakeStore(), [ghost])
        self.assertFalse(st["ok"])
        self.assertEqual(st["waiting"], [ghost])
        self.assertEqual(st["failed"], {})

    # -- the chain_wait node ---------------------------------------------

    def test_chain_wait_passes_when_upstream_is_merged(self):
        with tempfile.TemporaryDirectory() as d:
            dep = self._depfile(d, "dep.json", ["d1"])
            ts = code_tasks.load_taskfile(taskfile([BASIC], after=[dep]))
            g = self._graph(ts, FakeStore(
                by_taskfile={dep: [self._row("d1", "merged")]}))
            with capture_events() as ev:
                r = asyncio.run(g.nodes["chain_wait"].fn({}))
        self.assertTrue(r["ok"])
        self.assertTrue(ev.of("chain.ready"))
        self.assertFalse(ev.of("chain.blocked"))

    def test_chain_wait_blocks_on_a_failed_upstream_task(self):
        with tempfile.TemporaryDirectory() as d:
            dep = self._depfile(d, "dep.json", ["d1"])
            ts = code_tasks.load_taskfile(taskfile([BASIC], after=[dep]))
            g = self._graph(ts, FakeStore(
                by_taskfile={dep: [self._row("d1", "failed")]}))
            with capture_events() as ev:
                r = asyncio.run(g.nodes["chain_wait"].fn({}))
        self.assertFalse(r["ok"])
        self.assertIn("dependency failed", r["reason"])
        self.assertEqual(r["failed"], {dep: ["d1"]})
        self.assertTrue(ev.of("chain.blocked"))
        self.assertEqual(ev.first("chain.blocked")["failed_tasks"],
                         {dep: ["d1"]})

    def test_chain_wait_times_out(self):
        old = config.CHAIN_TIMEOUT
        config.CHAIN_TIMEOUT = 0.0   # first poll is already past the budget
        try:
            with tempfile.TemporaryDirectory() as d:
                dep = self._depfile(d, "dep.json", ["d1"])
                ts = code_tasks.load_taskfile(taskfile([BASIC], after=[dep]))
                g = self._graph(ts, FakeStore())   # no rows: still waiting
                with capture_events() as ev:
                    r = asyncio.run(g.nodes["chain_wait"].fn({}))
        finally:
            config.CHAIN_TIMEOUT = old
        self.assertFalse(r["ok"])
        self.assertTrue(r["timeout"])
        self.assertEqual(r["waiting"], [dep])
        self.assertTrue(ev.of("chain.blocked"))

    # -- status -----------------------------------------------------------

    def test_pending_chains_reports_readiness_per_chained_file(self):
        with tempfile.TemporaryDirectory() as d:
            dep = self._depfile(d, "dep.json", ["d1"])
            chained = self._depfile(d, "chained.json", ["c1"])
            doc = json.loads(Path(chained).read_text())
            doc["project"]["after"] = [dep]
            Path(chained).write_text(json.dumps(doc))
            self._depfile(d, "plain.json", ["p1"])   # no after: not reported
            old = config.TASKS_DIR
            config.TASKS_DIR = d
            try:
                out = code_tasks.pending_chains(FakeStore(
                    by_taskfile={dep: [self._row("d1", "merged")]}))
            finally:
                config.TASKS_DIR = old
        self.assertEqual([Path(e["taskfile"]).name for e in out],
                         ["chained.json"])
        self.assertTrue(out[0]["ready"])
        self.assertEqual(out[0]["after"], [dep])


class ReviewerSelectionIsLoadAware(unittest.TestCase):
    """Reviewers are picked by contention, not a fixed order.

    A fixed order sent every PR review to Kimi and GLM — the two scarcest
    models — while DeepSeek sat idle. With nine runs in flight that produced
    260 cap_wait events in an hour and three tasks giving up after waiting the
    full 30-minute lease timeout for a slot another model could have served
    immediately.
    """

    def _pick(self, usage, impl_family=None, n=2):
        # One model per FAMILY. The old pool was (STRONGEST, "GLM-5.3", ENTRY),
        # positional aliases that both resolved into the glm family once the
        # roster reordered — so "three families" quietly became two and the
        # reviewer pool could not be filled.
        pool = [m for m in DISTINCT_MODELS
                if config.MODEL_FAMILY.get(m) != impl_family]
        pool.sort(key=lambda m: (usage.get(m, 0) / max(1, config.driver_limit(m)),
                                 usage.get(m, 0)))
        return pool[:n]

    def test_the_idle_model_is_preferred_over_saturated_ones(self):
        # Saturate every candidate EXCEPT one and assert the idle one wins.
        # The old form named the two saturants positionally ({STRONGEST: 3,
        # "GLM-5.3": 4, ENTRY: 0}) and asserted picked[0] == ENTRY — which
        # silently required EXACTLY ONE idle model. On a three-family roster
        # (Union-Alpha joined 2026-09-16) a second model is idle and sorts
        # first, so the assertion tested the roster size, not the rule.
        idle = ENTRY
        usage = {m: config.driver_limit(m)
                 for m in DISTINCT_MODELS if m != idle}
        self.assertTrue(usage, "needs at least one model to saturate")
        picked = self._pick(usage)
        self.assertEqual(picked[0], idle,
                         f"{idle} is idle and must outrank the saturated "
                         f"{sorted(usage)}")

    def test_saturation_is_relative_to_each_cap_not_absolute(self):
        """The same absolute count means different things at different caps.

        Written against the real caps rather than hard-coded numbers: those
        moved when driver caps became sessions-divided-by-sessions-per-process,
        and a test that only passes for one particular set of caps is testing
        the constants, not the rule. Derives the caps over EVERY live family
        (`DISTINCT_MODELS`): the old form listed three positional aliases that
        collapsed to two distinct models once Union-Alpha joined, so the pool
        could return a model the `caps` dict never held (KeyError).
        """
        caps = {m: config.driver_limit(m) for m in DISTINCT_MODELS}
        if len(caps) < 2:
            self.skipTest("needs two live models to compare contention")
        roomiest = max(caps.values())
        if len(set(caps.values())) == 1:
            self.skipTest("all caps equal today — there is no relative saturation "
                          "to observe; the rule is exercised by the cap test below")
        # Everything at ONE in use: the model with the largest cap is the least
        # contended and must be picked first. Compared by CAP, not by name: a
        # tie makes max() arbitrary, and the rule is about the ratio.
        picked = self._pick({m: 1 for m in caps})
        self.assertEqual(caps[picked[0]], roomiest)

    def test_a_model_at_its_cap_is_never_preferred_to_an_idle_one(self):
        """THE RULE: an idle model beats one already at its cap, whatever the
        caps are and whichever model holds them. Numbers come from today's
        roster (DeepSeek driver cap 5, GLM 1 with two models); the assertion
        is on the ordering, not on which model won."""
        caps = {m: config.driver_limit(m) for m in DISTINCT_MODELS}
        if len(caps) < 2:
            self.skipTest("needs two live models to compare contention")
        full, idle = sorted(caps, key=lambda m: -caps[m])[:2]
        if caps[full] == 0:
            self.skipTest("no live model has a positive driver cap")
        picked = self._pick({full: caps[full], idle: 0})
        self.assertEqual(picked[0], idle,
                         f"{full} sits at its cap of {caps[full]} and must not "
                         f"outrank the idle {idle}")

    def test_the_implementers_family_is_never_chosen(self):
        # The RULE, over the whole roster: whichever family implements, no
        # reviewer offered shares it. The family list is derived, not the
        # hard-coded kimi/glm/deepseek of the three-model fleet.
        for fam in sorted(config.REVIEW_FAMILIES):
            picked = self._pick({}, impl_family=fam, n=2)
            self.assertTrue(picked, f"no reviewer at all for a {fam} implementer")
            for m in picked:
                self.assertNotEqual(config.MODEL_FAMILY[m], fam)

    def test_every_implementer_family_can_be_reviewed_when_it_must(self):
        """An implementer's family is excluded; when the roster still has
        another family, at least one reviewer must remain (the cross-review
        gate is thinner on a two-model fleet, never absent)."""
        for fam in sorted(config.REVIEW_FAMILIES):
            self.assertGreaterEqual(
                len(self._pick({}, impl_family=fam, n=len(config.REVIEW_FAMILIES))),
                1, f"no cross-family reviewer remains when {fam} implements")


class ResumingAnOpenPullRequest(unittest.TestCase):
    """A task whose PR is already open must not be re-implemented.

    Restarting it at alloc would discard a pushed branch and an open pull
    request that reviewers may have partly read, and burn a model redoing
    work that is sitting on GitHub waiting for approval.
    """

    def _graph(self, status):
        ts = code_tasks.load_taskfile(taskfile([BASIC]))
        prior = [{"id": "t1", "status": status, "model": config.ESCALATION_PATH[0], "error": None}]
        with capture_events():
            return code_tasks.build_code_graph(FakeStore(prior), ts, taskfile="tf.json")

    def test_in_review_resumes_at_publish_not_alloc(self):
        g = self._graph("in_review")
        self.assertIn("publish_t1", g.starts)
        self.assertNotIn("alloc_t1", g.starts)

    def test_conflict_still_resumes_at_publish(self):
        self.assertIn("publish_t1", self._graph("conflict").starts)

    def test_a_failed_task_still_starts_from_scratch(self):
        g = self._graph("failed")
        self.assertIn("alloc_t1", g.starts)


class ResumeKeepsAnOpenPullRequest(unittest.TestCase):
    """A stale 'running' row (or the 'failed' stale-reset writes) whose PR is
    still open keeps its commits; unfinished edits pass gate and review."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        base = Path(self.dir)
        self.repo = base / "proj"
        self.repo.mkdir()
        for a in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"],
                  ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", str(self.repo), *a], check=True,
                           capture_output=True)
        (self.repo / "f.txt").write_text("x\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "init"],
                       check=True, capture_output=True)
        self._orig = config.WORKTREE_ROOT
        config.WORKTREE_ROOT = str(base / "wts")
        self.addCleanup(setattr, config, "WORKTREE_ROOT", self._orig)

    def _row(self, status="running"):
        return [{"id": "t1", "status": status,
                 "model": config.ESCALATION_PATH[0],
                 "error": ("interrupted: run process exited before the task finished"
                           if status == "failed" else None)}]

    def _graph(self, find_pr, status="running"):
        ts = code_tasks.load_taskfile(taskfile([BASIC], repo=str(self.repo)))
        with mock.patch.object(gitstore, "find_pr", find_pr), capture_events():
            return code_tasks.build_code_graph(
                FakeStore(self._row(status)), ts, taskfile="tf.json")

    def _commit_on_branch(self):
        wt = asyncio.run(gitstore.alloc(self.repo, "t1", base="main"))
        (wt / "kept.txt").write_text("reviewed\n")
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "reviewed work"], cwd=wt,
                       check=True, capture_output=True)
        return subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "task/t1"],
            check=True, capture_output=True, text=True).stdout.strip()

    def test_stale_running_with_open_pr_resumes_at_publish(self):
        tip = self._commit_on_branch()

        async def open_pr(repo, task_id, state="open", *, wait_quota=True):
            return 5, "https://example/5", "OPEN"

        g = self._graph(open_pr)
        self.assertIn("publish_t1", g.starts)
        self.assertNotIn("alloc_t1", g.starts)
        again = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "task/t1"],
            check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(again, tip)

    def test_interrupted_pr_rework_runs_gate_and_review_before_publish(self):
        tip = self._commit_on_branch()
        wt = gitstore.worktree_for(self.repo, "t1")
        (wt / "kept.txt").write_text("reviewed, then partly reworked\n")

        async def open_pr(repo, task_id, state="open", *, wait_quota=True):
            return 5, "https://example/5", "OPEN"

        for status in ("running", "failed", "in_review", "conflict"):
            with self.subTest(status=status):
                g = self._graph(open_pr, status)
                self.assertEqual(g.starts, ["gate_t1"])
                self.assertTrue(any(e.src == "gate_t1" and e.dst == "review_t1"
                                    for e in g.edges))
                self.assertTrue(any(e.src == "review_t1" and e.dst == "publish_t1"
                                    for e in g.edges))
                self.assertEqual(subprocess.run(
                    ["git", "-C", str(self.repo), "rev-parse", "task/t1"],
                    check=True, capture_output=True, text=True).stdout.strip(), tip)

    def test_runtime_handoff_alone_does_not_trigger_re_review(self):
        self._commit_on_branch()
        wt = gitstore.worktree_for(self.repo, "t1")
        (wt / ".arc").mkdir()
        (wt / ".arc" / "handoff.md").write_text("handoff\n")

        async def open_pr(repo, task_id, state="open", *, wait_quota=True):
            return 5, "https://example/5", "OPEN"

        self.assertEqual(self._graph(open_pr).starts, ["publish_t1"])

    def test_stale_running_without_pr_still_reallocs(self):
        async def no_pr(repo, task_id, state="open", *, wait_quota=True):
            return None, None, None

        g = self._graph(no_pr)
        self.assertIn("alloc_t1", g.starts)
        self.assertNotIn("publish_t1", g.starts)

    def test_gh_failure_falls_back_to_alloc(self):
        async def boom(repo, task_id, state="open", *, wait_quota=True):
            raise RuntimeError("gh down")

        g = self._graph(boom)
        self.assertIn("alloc_t1", g.starts)

    def test_alloc_refuses_to_reset_while_pr_is_open(self):
        tip = self._commit_on_branch()

        async def open_pr(repo, task_id, state="open", *, wait_quota=True):
            return 12, "https://example/12", "OPEN"

        with mock.patch.object(gitstore, "find_pr", open_pr), capture_events() as ev:
            asyncio.run(gitstore.alloc(self.repo, "t1", base="main"))
        again = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "task/t1"],
            check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(again, tip)
        kept = [f for t, f in ev.seen if t == "task.branch_kept"]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["pr"], 12)
        self.assertEqual(
            [t for t, _ in ev.seen if t == "task.branch_reset"], [])


class ReviewersSendingAPullRequestBack(unittest.TestCase):
    """The rework implementer must actually be told why the PR was rejected.

    This path was dead in production. pr_review computed issues, posted them to
    GitHub and fired the edge back into implement — but implement only ever
    read review_ and gate_, and BOTH of those had passed (that is how the task
    reached publish). The implementer got an empty feedback string, re-read its
    own finished work, concluded there was nothing to do, and the task died as
    "no changes to publish" with the objections never delivered.
    """

    def _feedback(self, results):
        return code_tasks._rework_feedback("t1", results)

    def test_the_production_shape_no_longer_yields_empty_feedback(self):
        # Exactly what the graph holds after a PR rejection: review and gate
        # both passed, pr_review did not.
        fb = self._feedback({
            "review_t1": {"pass": True, "issues": []},
            "gate_t1": {"passed": True, "output": ""},
            "pr_review_t1": {"approved": False, "reviewers": [STRONGEST, ENTRY],
                             "issues": [f"[{STRONGEST}] leaks a file handle"]},
        })
        self.assertTrue(fb)
        self.assertIn("leaks a file handle", fb)
        self.assertIn(f"{STRONGEST}, {ENTRY}", fb)

    def test_it_tells_the_implementer_not_to_call_the_task_done(self):
        fb = self._feedback({"pr_review_t1": {"approved": False, "issues": ["x"]}})
        self.assertIn("Do not conclude", fb)

    def test_an_approved_pr_contributes_no_feedback(self):
        self.assertEqual(self._feedback({
            "pr_review_t1": {"approved": True, "issues": [], "reviewers": ["K"]}}), "")

    def test_a_gate_failure_is_reported_alongside_the_pr_issues(self):
        fb = self._feedback({
            "pr_review_t1": {"approved": False, "issues": ["style"], "reviewers": ["K"]},
            "gate_t1": {"passed": False, "output": "3 tests failed"},
        })
        self.assertIn("style", fb)
        self.assertIn("3 tests failed", fb)

    def test_the_issues_reach_the_prompt_the_model_actually_sees(self):
        p = code_tasks._impl_prompt(
            {"id": "t1", "title": "T1", "prompt": "do it", "files_hint": [],
             "model": "", "reviewer": ""},
            self._feedback({
                "pr_review_t1": {"approved": False, "reviewers": ["GLM-5.3"],
                                 "issues": ["[GLM-5.3] no test for the error path"]}}))
        self.assertIn("no test for the error path", p)
        self.assertIn("REJECTED", p)


class ChoosingPullRequestReviewers(unittest.TestCase):
    """Reviewer selection must never name a model the driver will refuse.

    The pool and the drivers' role rules were two separate lists, and they
    drifted: the pool offered DeepSeek, OpencodeDriver refused the role, and
    the ValueError killed pr_review one second after the PR opened. Seven pull
    requests were stranded that way in a single run — branch pushed, PR open,
    nobody coming back. Eligibility is now decided by building the driver.
    """

    def test_every_implementer_family_has_a_reviewer_it_can_use(self):
        """The RULE: every live implementer family must have at least one
        eligible PR reviewer. PR_REVIEWERS itself is capped at
        families-1 (two families -> one reviewer on the 2026-09-12 roster),
        so the count that must be satisfiable is min(wanted, families-1).

        Pins the DEFAULT mode: the rule is about CROSS-family reviewers, and
        under ARC_ALLOW_SAME_FAMILY_REVIEW=1 the pool deliberately inverts to
        same-family only (one model per family), so the cross-family count is
        1 by design and this assertion would be measuring the hatch, not the
        rule. Gates export that flag during a backend outage — the test must
        not depend on the operator's shell (see test_roster_stability).
        """
        with mock.patch.object(config, "ALLOW_SAME_FAMILY_REVIEW", False):
            wanted = min(config.PR_REVIEWERS, len(config.REVIEW_FAMILIES) - 1)
            for fam in sorted(config.REVIEW_FAMILIES):
                with self.subTest(family=fam):
                    self.assertGreaterEqual(
                        len(code_tasks._eligible_pr_reviewers(fam, None)),
                        max(1, wanted),
                        f"{fam} cannot field a cross-family PR reviewer")

    def test_it_never_picks_the_implementer_s_own_family(self):
        # Pins the DEFAULT pairing; gates run with ARC_ALLOW_SAME_FAMILY_REVIEW=1
        # exported during a backend outage, and under that flag the pool
        # deliberately inverts to same-family only (config.py same-family hatch).
        with mock.patch.object(config, "ALLOW_SAME_FAMILY_REVIEW", False):
            for fam in sorted(config.REVIEW_FAMILIES):
                for m in code_tasks._eligible_pr_reviewers(fam, None):
                    self.assertNotEqual(config.MODEL_FAMILY.get(m), fam)

    def test_every_model_it_offers_can_actually_be_built(self):
        for fam in sorted(config.REVIEW_FAMILIES):
            for m in code_tasks._eligible_pr_reviewers(fam, None):
                code_tasks._driver(m, "pr_reviewer", None)  # must not raise

    def test_a_retired_model_is_never_offered_as_a_reviewer(self):
        for fam in sorted(config.REVIEW_FAMILIES):
            offered = code_tasks._eligible_pr_reviewers(fam, None)
            for retired in ("gpt-oss-120b", "Kimi-K3", "DeepSeek-V4-Flash"):
                self.assertNotIn(retired, offered)


class ReviewerContention(unittest.TestCase):
    """Reviewer choice must respect whichever ceiling binds first.

    Scoring on the model's own cap alone sent reviews to GLM and DeepSeek while
    the single opencode pool those two share sat at 5/5 with seven reviewers
    queued behind it — and the kimi harness idle at 1/3.
    """

    def test_a_saturated_harness_makes_its_models_look_busy(self):
        usage = {"GLM-5.3": 1, "harness:opencode": config.harness_limit("opencode")}
        self.assertEqual(code_tasks._reviewer_pressure("GLM-5.3", usage), 1.0)

    def test_a_models_own_cap_still_counts_when_the_harness_is_free(self):
        usage = {"GLM-5.3": config.driver_limit("GLM-5.3"), "harness:opencode": 0}
        self.assertEqual(code_tasks._reviewer_pressure("GLM-5.3", usage), 1.0)

    def test_a_model_on_a_free_harness_is_preferred_when_one_pool_is_full(self):
        # The point is HARNESS contention, not model strength: every model on
        # a saturated harness inherits that pool's pressure, so a model on a
        # different, free harness must sort first however the roster is
        # ordered. Harnesses are read from the roster — with one harness in
        # use there is nothing to compare, and the rule is vacuous. Model
        # usage stays 0 so the harness layer is the ONLY differentiator: a
        # pool can be full cross-process while this process holds no lease
        # for the model, and with a driver cap of 1 a model-usage of 1 would
        # saturate the model layer and mask the harness signal.
        by_harness = {}
        for m in DISTINCT_MODELS:
            by_harness.setdefault(config.MODEL_HARNESS.get(m), []).append(m)
        if len(by_harness) < 2:
            self.skipTest("every live model shares one harness today")
        (busy_h, busy), (free_h, free) = sorted(
            by_harness.items(), key=lambda kv: -len(kv[1]))[:2]
        usage = {m: 0 for m in DISTINCT_MODELS}
        usage[f"harness:{busy_h}"] = config.harness_limit(busy_h)
        usage[f"harness:{free_h}"] = 0
        order = sorted(DISTINCT_MODELS,
                       key=lambda m: code_tasks._reviewer_pressure(m, usage))
        self.assertIn(order[0], free,
                      f"a model on the saturated {busy_h} pool outranked one on "
                      f"the free {free_h} pool")

    def test_an_idle_fleet_scores_everything_zero(self):
        for m in DISTINCT_MODELS:
            self.assertEqual(code_tasks._reviewer_pressure(m, {}), 0.0)

    def test_driver_routing_follows_the_roster(self):
        # Routing follows the roster row, whatever is on it today — no
        # hard-coded model->harness if-chain that goes stale on a transition.
        for m, h in config.MODEL_HARNESS.items():
            self.assertEqual(code_tasks._harness_of(m), h)
            self.assertEqual(code_tasks._driver(m, "implementer", None).harness, h)


class EveryTaskEventNamesItsTask(unittest.TestCase):
    """A task.* event with no `task` field cannot be acted on.

    task.gate (146 events) and task.reviewed (108) were both emitted without
    one, so the two per-node progress signals the dashboard depends on were
    anonymous in the log — you could see that A gate had failed, but not whose.
    """

    def _emits(self):
        import ast
        tree = ast.parse(pathlib.Path(code_tasks.__file__).read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "emit"
                    and getattr(node.func.value, "id", None) == "events"):
                continue
            if not (node.args and isinstance(node.args[0], ast.Constant)):
                continue
            name = node.args[0].value
            if isinstance(name, str) and name.startswith("task."):
                yield name, node

    def test_it_finds_the_emit_sites_at_all(self):
        self.assertGreater(len(list(self._emits())), 8)

    def test_every_task_event_passes_a_task(self):
        missing = sorted({name for name, node in self._emits()
                          if not any(k.arg == "task" for k in node.keywords)})
        self.assertEqual(missing, [],
                         f"emitted without a task= field: {missing}")


class PreMergeReviewFallsBackWhenFull(unittest.TestCase):
    """The planned pre-merge reviewer waits only when nothing eligible is free.

    Game tasks pinned to GLM sat at 4/4 while stronger cross-family seats
    were idle. Fallback is capacity, tier, and family — the taskfile token
    does not change, and a review is never skipped.
    """

    def _full(self, *models):
        usage = {}
        for m in models:
            usage[m] = config.driver_limit(m)
            h = config.MODEL_HARNESS[m]
            usage[f"harness:{h}"] = max(usage.get(f"harness:{h}", 0),
                                        config.harness_limit(h))
        return usage

    def test_idle_arc_reviewers_prefer_deepseek_for_a_third_family(self):
        ds = "DeepSeek-V4.1-Flash-thinking-max"
        glm = "GLM-5.3"
        # A third-family implementer leaves both ARC families eligible.
        with mock.patch.object(config, "ALLOW_SAME_FAMILY_REVIEW", False), \
             mock.patch.object(config, "ESCALATION_PATH", [ds, glm]):
            pool = code_tasks._eligible_pr_reviewers("openai", None)
        self.assertEqual(sorted(pool, key=lambda m: code_tasks._reviewer_rank(m, {})),
                         [ds, glm])
        self.assertEqual(sorted(pool, key=lambda m: code_tasks._reviewer_rank(
            m, {ds: config.driver_limit(ds)}))[0], glm)

    def _stand_in(self, patch_driver=True):
        """An idle hard-tier reviewer on its own harness.

        ARC_FLEET=local has two families, so the only cross-family seat for a
        GLM implementer is DeepSeek — weaker than a hard plan and the same
        family once DeepSeek implemented. The rule is about capacity, not
        which names happen to be live, so the stand-in is patched in beside
        the real roster.
        """
        alt, top = "Idle-Strong", config.TIER_ORDER[-1]
        real_dl, real_hl = config.driver_limit, config.harness_limit
        patches = [
            mock.patch.dict(config.MODEL_ROLES,
                            {alt: {"reviewer", "pr_reviewer"}}),
            mock.patch.dict(config.MODEL_FAMILY, {alt: "standin"}),
            mock.patch.dict(config.MODEL_TIER, {alt: top}),
            mock.patch.dict(config.MODEL_HARNESS, {alt: "standin-h"}),
            mock.patch.object(
                config, "driver_limit",
                lambda m, interactive=False: 8 if m == alt
                else real_dl(m, interactive)),
            mock.patch.object(
                config, "harness_limit",
                lambda h: 8 if h == "standin-h" else real_hl(h)),
        ]
        if patch_driver:
            real = code_tasks._driver

            def _drv(m, role, pol):
                if m == alt:
                    return mock.Mock(model=m, harness="standin-h", images=None)
                return real(m, role, pol)

            patches.append(mock.patch.object(code_tasks, "_driver", _drv))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return alt

    def test_a_saturated_planned_reviewer_yields_to_an_idle_stronger_one(self):
        self._stand_in()
        planned = config.REVIEW_FAMILIES["deepseek"]
        impl = "GLM-5.3"
        usage = self._full(planned)
        model, reason = code_tasks._select_reviewer("deepseek", impl, None, usage)
        self.assertEqual(reason, "planned_full_fallback")
        self.assertGreater(code_tasks._tier_rank(model),
                           code_tasks._tier_rank(planned))
        self.assertNotEqual(config.MODEL_FAMILY[model], config.MODEL_FAMILY[impl])
        self.assertNotEqual(config.MODEL_FAMILY[model], "deepseek")
        self.assertLess(code_tasks._reviewer_pressure(model, usage), 1.0)

    def test_a_full_harness_counts_as_no_headroom(self):
        self._stand_in()
        planned = config.REVIEW_FAMILIES["glm"]
        usage = {"harness:opencode": config.harness_limit("opencode")}
        model, reason = code_tasks._select_reviewer(
            "glm", "DeepSeek-V4.1-Flash-thinking-max", None, usage)
        self.assertEqual(reason, "planned_full_fallback")
        self.assertNotEqual(model, planned)
        self.assertGreaterEqual(code_tasks._tier_rank(model),
                                code_tasks._tier_rank(planned))
        self.assertNotEqual(config.MODEL_HARNESS[model], "opencode")

    def test_when_every_eligible_reviewer_is_full_the_planned_one_is_kept(self):
        planned = config.REVIEW_FAMILIES["glm"]
        usage = self._full(*config.REVIEW_FAMILIES.values())
        model, reason = code_tasks._select_reviewer(
            "glm", "DeepSeek-V4.1-Flash-thinking-max", None, usage)
        self.assertEqual((model, reason), (planned, "planned_full_no_alternative"))

    def test_an_idle_same_family_model_is_never_the_fallback(self):
        impl = "Claude-Opus-5.5"
        planned = config.REVIEW_FAMILIES["glm"]
        others = [m for fam, m in config.REVIEW_FAMILIES.items()
                  if fam != "anthropic"]
        model, reason = code_tasks._select_reviewer(
            "glm", impl, None, self._full(*others))
        self.assertEqual(model, planned)
        self.assertNotEqual(config.MODEL_FAMILY.get(model), "anthropic")
        self.assertEqual(reason, "planned_full_no_alternative")

    def test_an_idle_weaker_reviewer_is_not_chosen(self):
        planned = config.REVIEW_FAMILIES["glm"]
        weaker = config.REVIEW_FAMILIES["deepseek"]
        busy = [m for m in config.REVIEW_FAMILIES.values() if m != weaker]
        model, reason = code_tasks._select_reviewer(
            "glm", "Claude-Opus-5.5", None, self._full(*busy))
        self.assertEqual((model, reason), (planned, "planned_full_no_alternative"))
        self.assertLess(code_tasks._tier_rank(weaker), code_tasks._tier_rank(planned))

    def test_fallback_prefers_free_arc_then_headroom_and_claude_last(self):
        """Unlimited ARC seats, then the subscription seat with the most headroom.

        A recent usage_limit skips that seat. Claude sorts last. The
        implementer's own family is never the fallback.
        """
        seats = {
            "Codex-X": ("openai", "codex", 4),
            "Cursor-X": ("cursor", "cursor", 3),
            "Agy-X": ("google", "agy", 3),
            "Claude-X": ("anthropic", "claude", 2),
        }
        top = config.TIER_ORDER[-1]
        real_dl = config.driver_limit
        real_hl = config.harness_limit
        roles = {m: {"reviewer", "pr_reviewer"} for m in seats}
        fams = {m: spec[0] for m, spec in seats.items()}
        tiers = {m: top for m in seats}
        harnesses = {m: spec[1] for m, spec in seats.items()}
        caps = {m: spec[2] for m, spec in seats.items()}
        patches = [
            mock.patch.dict(config.MODEL_ROLES, roles),
            mock.patch.dict(config.MODEL_FAMILY, fams),
            mock.patch.dict(config.MODEL_TIER, tiers),
            mock.patch.dict(config.MODEL_HARNESS, harnesses),
            mock.patch.object(
                config, "driver_limit",
                lambda m, interactive=False: caps[m] if m in caps
                else real_dl(m, interactive)),
            mock.patch.object(
                config, "harness_limit",
                lambda h: 8 if h in {s[1] for s in seats.values()} else real_hl(h)),
        ]
        real = code_tasks._driver

        def _drv(m, role, pol):
            if m in seats:
                return mock.Mock(model=m, harness=harnesses[m], images=None)
            return real(m, role, pol)

        patches.append(mock.patch.object(code_tasks, "_driver", _drv))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        planned = config.REVIEW_FAMILIES["glm"]
        usage = self._full(planned)
        for m in config.MODEL_ROLES:
            if m in ("Cursor-X", "Claude-X"):
                continue
            usage[m] = config.driver_limit(m)
        usage["Cursor-X"] = 0
        usage["Codex-X"] = 2
        usage["Claude-X"] = 0
        usage["usage_limit:agy"] = 1
        model, reason = code_tasks._select_reviewer(
            "glm", "DeepSeek-V4.1-Flash-thinking-max", None, usage)
        self.assertEqual(reason, "planned_full_fallback")
        self.assertEqual(model, "Cursor-X")
        self.assertNotEqual(config.MODEL_FAMILY[model], "deepseek")
        self.assertNotEqual(model, "Claude-X")

    def test_a_crashed_fallback_reviewer_is_recorded_and_is_not_a_rejection(self):
        self._stand_in(patch_driver=False)
        planned = config.REVIEW_FAMILIES["deepseek"]
        store = FakeStore()
        store.lease_usage = lambda: self._full(planned)
        seen = {}

        class _Boom:
            def __init__(self, model):
                self.model, self.harness, self.images = model, "fake", None
                seen["model"] = model

            async def run(self, prompt, cwd, task_id=None, **kw):
                seen["avoid"] = kw.get("avoid_families")
                raise code_tasks.DriverError("reviewer backend down")

        ts = code_tasks.load_taskfile(taskfile([{
            "id": "t1", "title": "T1", "prompt": "do it", "verify_cmd": "true",
            "model": "GLM-5.3", "reviewer": "deepseek"}]))
        orig = code_tasks._driver
        code_tasks._driver = lambda model, role, pol: _Boom(model)
        wt = tempfile.mkdtemp(prefix="arc-rev-fallback-")
        self.addCleanup(shutil.rmtree, wt, ignore_errors=True)
        try:
            with capture_events() as ev:
                g = code_tasks.build_code_graph(store, ts, taskfile="tf.json")

                async def diff_full(path, base):
                    return "DIFF"

                async def blast(path, base=None, **kw):
                    return ""

                g_diff, g_blast = code_tasks.gitstore.diff_full, code_tasks.graft.blast
                code_tasks.gitstore.diff_full, code_tasks.graft.blast = diff_full, blast
                try:
                    out = asyncio.run(g.nodes["review_t1"].fn(
                        {"results": {"alloc_t1": {"worktree": wt},
                                     "implement_t1": {"model": "GLM-5.3"}},
                         "runs": {}}))
                finally:
                    code_tasks.gitstore.diff_full, code_tasks.graft.blast = g_diff, g_blast
        finally:
            code_tasks._driver = orig
        self.assertTrue(out["crashed"])
        self.assertFalse(out["pass"])
        self.assertNotEqual(out["reviewer_model"], planned)
        self.assertGreater(code_tasks._tier_rank(out["reviewer_model"]),
                           code_tasks._tier_rank(planned))
        self.assertNotEqual(config.MODEL_FAMILY[out["reviewer_model"]], "glm")
        self.assertEqual(seen["model"], out["reviewer_model"])
        self.assertEqual(store.harness_runs[0][0][2], out["reviewer_model"])
        self.assertEqual(seen["avoid"], {"glm"})
        selected = ev.first("task.reviewer_selected")
        self.assertEqual(selected["reason"], "planned_full_fallback")
        self.assertEqual(selected["model"], out["reviewer_model"])
        self.assertEqual(selected["planned_token"], "deepseek")


class AReviewerThatCrashedDidNotReview(unittest.TestCase):
    """A crashed reviewer is an inconclusive round, not a rejection.

    Observed on PR #12: both reviewers died on opencode contention, and the
    crash was posted to a PUBLIC pull request as "changes requested: reviewer
    crashed", then sent the implementer back to fix issues that did not exist.
    Three infrastructure blips would have failed a perfectly good task, because
    each one consumed one of three PR rounds.
    """

    def _outcomes(self, *pairs):
        """Calls the REAL aggregation. A copy of it here caught nothing —
        mutation testing showed the suite stayed green with the production
        logic broken."""
        issues, approvals, crashed, approved, inconclusive = \
            code_tasks._tally_reviews(list(pairs))
        return {"approved": approved, "issues": issues, "crashed": crashed,
                "inconclusive": inconclusive}

    CRASH = {"approve": False, "crashed": True, "issues": ["boom"]}
    OK = {"approve": True, "issues": []}
    NO = {"approve": False, "issues": ["real problem"]}

    def test_all_reviewers_crashing_is_inconclusive_not_a_rejection(self):
        r = self._outcomes(("A", self.CRASH), ("B", self.CRASH))
        self.assertTrue(r["inconclusive"])
        self.assertFalse(r["approved"])
        self.assertEqual(r["issues"], [])

    def test_one_crash_and_one_approval_is_inconclusive(self):
        # Unanimity is required and cannot be established, so retry the review.
        r = self._outcomes(("A", self.CRASH), ("B", self.OK))
        self.assertTrue(r["inconclusive"])

    def test_a_real_objection_beats_a_crash(self):
        r = self._outcomes(("A", self.CRASH), ("B", self.NO))
        self.assertFalse(r["inconclusive"])
        self.assertEqual(r["issues"], ["[B] real problem"])

    def test_unanimous_approval_still_merges(self):
        r = self._outcomes(("A", self.OK), ("B", self.OK))
        self.assertTrue(r["approved"])
        self.assertFalse(r["inconclusive"])

    def test_a_normal_rejection_is_unchanged(self):
        r = self._outcomes(("A", self.OK), ("B", self.NO))
        self.assertFalse(r["approved"])
        self.assertFalse(r["inconclusive"])
        self.assertEqual(r["issues"], ["[B] real problem"])

    def test_the_inconclusive_budget_is_separate_from_the_round_budget(self):
        self.assertGreater(config.PR_MAX_INCONCLUSIVE, 0)
        self.assertGreater(config.PR_MAX_ROUNDS, 0)


class InconclusiveReviewRouting(unittest.TestCase):
    """Where an inconclusive round sends the task."""

    def _edges(self):
        ts = code_tasks.load_taskfile(taskfile([BASIC]))
        with capture_events():
            g = code_tasks.build_code_graph(FakeStore([]), ts, taskfile="tf.json")
        return [e for e in g.edges if e.src == "pr_review_t1"]

    def _fires(self, dst, result):
        for e in self._edges():
            if e.dst == dst and (e.when is None or e.when(result, {})):
                return True
        return False

    def test_an_inconclusive_round_retries_the_review(self):
        r = {"approved": False, "inconclusive": True, "inconclusive_n": 1}
        self.assertTrue(self._fires("pr_fanout_t1", r))  # re-enter at the fan-out
        self.assertFalse(self._fires("implement_t1", r))

    def test_it_stops_retrying_once_the_budget_is_spent(self):
        r = {"approved": False, "inconclusive": True,
             "inconclusive_n": config.PR_MAX_INCONCLUSIVE}
        self.assertFalse(self._fires("pr_fanout_t1", r))
        self.assertTrue(self._fires("fail_t1", r))

    def test_a_real_rejection_still_goes_to_the_implementer(self):
        r = {"approved": False, "inconclusive": False, "issues": ["x"]}
        self.assertTrue(self._fires("implement_t1", r))
        self.assertFalse(self._fires("pr_review_t1", r))

    def test_approval_still_merges(self):
        self.assertTrue(self._fires("pr_merge_t1", {"approved": True}))


class AConflictingPullRequestIsRetried(unittest.TestCase):
    """A PR that conflicts with the base was a terminal state.

    Every task merges into one integration branch, so conflicts are the normal
    cost of parallelism — and most are not disagreements about the code, just a
    base that moved on under a long task. Those merge cleanly with no model.
    """

    def _edges(self, src):
        ts = code_tasks.load_taskfile(taskfile([BASIC]))
        with capture_events():
            g = code_tasks.build_code_graph(FakeStore([]), ts, taskfile="tf.json")
        return [e for e in g.edges if e.src == src]

    def _fires(self, src, dst, result):
        return any(e.dst == dst and (e.when is None or e.when(result, {}))
                   for e in self._edges(src))

    def test_a_resynced_branch_goes_back_for_review(self):
        # The diff changed, so the approval it already has no longer covers it.
        r = {"merged": False, "resynced": True, "resyncs": 1}
        self.assertTrue(self._fires("pr_merge_t1", "pr_fanout_t1", r))

    def test_a_clean_merge_does_not_loop_back(self):
        self.assertFalse(self._fires("pr_merge_t1", "pr_fanout_t1",
                                     {"merged": True, "pr": 4}))

    def test_a_terminal_conflict_does_not_loop_back(self):
        self.assertFalse(self._fires("pr_merge_t1", "pr_review_t1",
                                     {"merged": False, "reason": "conflict"}))

    def test_the_resync_budget_is_positive_and_finite(self):
        # Each resync rewrites the branch and costs a fresh review round, so
        # it must be bounded — but the operator has said tokens are not the
        # scarce resource, so the bound is generous rather than tight. What
        # this test protects is that a task cannot resync FOREVER.
        self.assertGreaterEqual(config.PR_MAX_RESYNCS, 1)
        self.assertLess(config.PR_MAX_RESYNCS, 100)


class ResumingAConflictedTask(unittest.TestCase):
    """A conflict resume already knows the branch does not merge.

    Without resyncing at publish, the task re-attaches to its PR, two reviewers
    read the stale diff and approve it, pr_merge THEN discovers the conflict,
    resyncs, and sends the changed diff back for two more reviewers. Four
    scarce reviewer slots to land one task, and the first two read a diff that
    was never going to be what merged.
    """

    def _graph(self, status):
        ts = code_tasks.load_taskfile(taskfile([BASIC]))
        prior = [{"id": "t1", "status": status, "model": config.ESCALATION_PATH[0], "error": None}]
        with capture_events():
            return code_tasks.build_code_graph(FakeStore(prior), ts, taskfile="tf.json")

    def test_a_conflicted_task_resumes_at_publish(self):
        self.assertIn("publish_t1", self._graph("conflict").starts)

    def test_publish_is_where_the_resync_happens(self):
        # The resync must precede review, not follow it: pr_merge's own resync
        # is the fallback for a base that moves DURING the run.
        src = pathlib.Path(code_tasks.__file__).read_text()
        pub = src[src.index("async def publish(ctx):\n            \"\"\"Commit"):]
        pub = pub[:pub.index("async def pr_review")]
        self.assertIn("sync_with_base", pub)
        self.assertIn("keep_conflicts=True", pub)

    def test_the_resynced_branch_is_pushed_before_the_pr_is_reattached(self):
        src = pathlib.Path(code_tasks.__file__).read_text()
        pub = src[src.index("async def publish(ctx):\n            \"\"\"Commit"):]
        pub = pub[:pub.index("async def pr_review")]
        push = pub.index("push_task_branch(repo, tid)\n                number")
        attach = pub.index("await gitstore.open_pr(repo, tid,\n"
                           "                                           f\"task({tid})")
        self.assertLess(push, attach,
                        "the merge commit exists only locally until it is pushed")

    def test_an_in_review_resume_also_resyncs(self):
        """Because the FIRST resume of a conflicted task marks it in_review.

        Gating the resync on prior_status=="conflict" meant the second resume
        no longer knew the branch conflicted and reviewed a diff that could not
        merge. Whether a branch merges is a fact about the branch, so publish
        asks git on every resume rather than trusting a status field another
        node overwrote.
        """
        g = self._graph("in_review")
        self.assertIn("publish_t1", g.starts)
        src = pathlib.Path(code_tasks.__file__).read_text()
        pub = src[src.index("async def publish(ctx):\n            \"\"\"Commit"):]
        pub = pub[:pub.index("async def pr_review")]
        # The sync is now UNCONDITIONAL. Gating it on a resume was already a
        # narrowing of gating it on the status column, and both were wrong for
        # the same reason: a task's branch drifts from base whether or not this
        # run happens to be a resume.
        self.assertIn("sync_with_base", pub)
        self.assertNotIn("if alloc_res is None:\n                ok, conflicts", pub)
        self.assertNotIn('prior_status == "conflict" and alloc_res is None', pub)

    def test_the_sync_happens_after_the_commit_not_before(self):
        """`git merge` refuses to run over local modifications it would
        overwrite, and at publish time the agent's entire output is still
        uncommitted in the worktree. Committing second would abort every sync
        on any task that actually wrote something."""
        src = pathlib.Path(code_tasks.__file__).read_text()
        pub = src[src.index("async def publish(ctx):\n            \"\"\"Commit"):]
        pub = pub[:pub.index("async def pr_review")]
        self.assertLess(pub.index("head = await gitstore.publish"),
                        pub.index("await gitstore.sync_with_base"))


class ResolvingARealMergeConflict(unittest.TestCase):
    """A genuine textual conflict had nowhere to go.

    sync_with_base could only report it, so the task re-attached to its PR,
    spent two reviewers on a diff that could not merge, and landed back in
    `conflict` unchanged. Editing files to reconcile two versions is exactly
    what an implementer is for.
    """

    def _fires(self, dst, result):
        ts = code_tasks.load_taskfile(taskfile([BASIC]))
        with capture_events():
            g = code_tasks.build_code_graph(FakeStore([]), ts, taskfile="tf.json")
        return any(e.dst == dst and (e.when is None or e.when(result, {}))
                   for e in g.edges if e.src == "publish_t1")

    RESOLVE = {"published": False, "resolve": True,
               "conflicts": ["a.py"], "base": "development"}

    def test_a_conflict_needing_resolution_goes_to_the_implementer(self):
        self.assertTrue(self._fires("implement_t1", self.RESOLVE))

    def test_it_must_not_go_to_alloc_which_would_reset_the_branch(self):
        self.assertFalse(self._fires("alloc_t1", self.RESOLVE))

    def test_an_ordinary_failed_publish_still_falls_through_to_alloc(self):
        self.assertTrue(self._fires("alloc_t1", {"published": False,
                                                 "reason": "no worktree"}))

    def test_the_implementer_is_told_which_files_conflict(self):
        fb = code_tasks._rework_feedback("t1", {"publish_t1": self.RESOLVE})
        self.assertIn("a.py", fb)
        self.assertIn("development", fb)

    def test_it_is_told_to_keep_both_sides_and_not_abort(self):
        fb = code_tasks._rework_feedback("t1", {"publish_t1": self.RESOLVE})
        for phrase in ("keep your task's change AND the incoming change",
                       "merge --abort", "Remove every conflict marker"):
            self.assertIn(phrase, fb)

    def test_a_normal_publish_contributes_no_conflict_feedback(self):
        self.assertEqual(
            code_tasks._rework_feedback("t1", {"publish_t1": {"published": True}}), "")


class TerminalFailuresSayWhy(unittest.TestCase):
    """Several paths reach `fail`, and it reported only one of them.

    pause-when-hidden was recorded as "exhausted escalation up to
    DeepSeek-V4-Flash" with ZERO escalations taken, two still permitted and a
    next tier available. It had actually run out of PR review rounds. The
    message sent the reader to audit the escalation config for a bug that was
    never there.
    """

    def _why(self, results, runs=None, escalations=0):
        """Mirrors fail()'s reason selection over a results dict."""
        gate_res = results.get("gate_t1") or {}
        rev_res = results.get("review_t1") or {}
        pr_res = results.get("pr_review_t1") or {}
        attempts = (runs or {}).get("implement_t1", 0)
        if pr_res and not pr_res.get("approved"):
            if pr_res.get("inconclusive"):
                return "inconclusive"
            return "pr-rejected"
        if not gate_res.get("passed", True):
            return "gate"
        if rev_res and not rev_res.get("pass", True):
            return "review"
        if escalations:
            return "escalation"
        return "no-path"

    def test_running_out_of_pr_rounds_is_not_called_an_escalation_failure(self):
        self.assertEqual(
            self._why({"pr_review_t1": {"approved": False, "pr": 9, "issues": ["x"]}}),
            "pr-rejected")

    def test_reviewers_that_kept_crashing_are_reported_as_inconclusive(self):
        self.assertEqual(
            self._why({"pr_review_t1": {"approved": False, "inconclusive": True}}),
            "inconclusive")

    def test_a_failing_gate_is_reported_as_a_gate_failure(self):
        self.assertEqual(self._why({"gate_t1": {"passed": False}}), "gate")

    def test_a_rejecting_reviewer_is_reported_as_a_review_failure(self):
        self.assertEqual(
            self._why({"gate_t1": {"passed": True}, "review_t1": {"pass": False}}),
            "review")

    def test_a_genuine_escalation_failure_still_says_so(self):
        self.assertEqual(self._why({}, escalations=2), "escalation")

    def test_no_escalation_taken_is_not_described_as_exhausting_one(self):
        # The exact misreport: zero escalations must never read as "exhausted".
        self.assertEqual(self._why({}, escalations=0), "no-path")

    def test_the_real_fail_node_carries_the_counts(self):
        src = pathlib.Path(code_tasks.__file__).read_text()
        body = src[src.index("async def fail(ctx):"):]
        body = body[:body.index("chain = {")]
        for field in ("escalations=escalations", "implement_attempts=attempts"):
            self.assertIn(field, body,
                          "the event must carry the numbers that make the "
                          "reason checkable")


class EveryErrorPathCaptures(unittest.TestCase):
    """A driver.error emitted without a fingerprint threw its traceback away.

    drivers.py captures what IT raises — but code_tasks catches what drivers
    re-raises, in the implementer and reviewer paths, and those two emitted
    their own driver.error with no capture at all. The implementer crash is the
    most consequential failure in the pipeline and was the last one still
    discarding its evidence. Observed live: a driver.error for
    projects-ui-cleanup with fingerprint=None.
    """

    def _emit_sites(self):
        import ast
        tree = ast.parse(pathlib.Path(code_tasks.__file__).read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "emit"
                    and getattr(node.func.value, "id", None) == "events"):
                continue
            if (node.args and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "driver.error"):
                yield node

    def test_the_scan_finds_both_sites(self):
        self.assertGreaterEqual(len(list(self._emit_sites())), 2)

    def test_every_driver_error_carries_a_fingerprint(self):
        missing = [n.lineno for n in self._emit_sites()
                   if not any(k.arg == "fingerprint" for k in n.keywords)]
        self.assertEqual(missing, [],
                         f"driver.error emitted with no capture at line(s) {missing}")

    def test_both_paths_call_errors_capture(self):
        src = pathlib.Path(code_tasks.__file__).read_text()
        for marker in ('node=f"implement_{tid}"', 'node=f"review_{tid}"'):
            self.assertIn(marker, src,
                          "the crash path must capture with its node name")


class StatusMustReflectRealityDuringRework(unittest.TestCase):
    """A task being actively worked must not read as `conflict`.

    Status was written "running" at alloc and at escalate and nowhere else. A
    task resuming at publish — conflict repair, or in_review with a PR open —
    goes straight to implement, so the database still said `conflict` while an
    agent was editing its worktree. Acting on that status races the agent: it
    nearly had me resolving the same merge conflict underneath one.
    """

    def test_implement_marks_the_task_running(self):
        src = pathlib.Path(code_tasks.__file__).read_text()
        body = src[src.index("async def implement(ctx):"):]
        body = body[:body.index("async def gate(ctx):")]
        self.assertIn('"running"', body,
                      "implement must record that work is happening")
        self.assertIn("upsert_code_task", body)

    def test_it_records_the_branch_too(self):
        # A resumed task's row may predate the branch it is now working on.
        src = pathlib.Path(code_tasks.__file__).read_text()
        body = src[src.index("async def implement(ctx):"):]
        body = body[:body.index("async def gate(ctx):")]
        self.assertIn('branch=f"task/{tid}"', body)

    def test_alloc_still_marks_it_running(self):
        src = pathlib.Path(code_tasks.__file__).read_text()
        body = src[src.index("async def alloc(ctx):"):]
        body = body[:body.index("async def implement(ctx):")]
        self.assertIn('"running"', body)


class ACrashedPreMergeReviewerIsNotARejection(unittest.TestCase):
    """The same distinction pr_review makes, in the gate-stage reviewer.

    graph-admission-control's verify gate passed FOUR times while its reviewer
    hit 18 consecutive capacity errors. Each crash returned pass:False, so the
    implementer was sent back to fix issues nobody had raised, one fix round at
    a time, until the task died as "exhausted escalation" on work that was
    never actually rejected.
    """

    def _edges(self, src="review_t1"):
        ts = code_tasks.load_taskfile(taskfile([BASIC]))
        with capture_events():
            g = code_tasks.build_code_graph(FakeStore([]), ts, taskfile="tf.json")
        return [e for e in g.edges if e.src == src]

    def _fires(self, dst, result, ctx=None):
        return any(e.dst == dst and (e.when is None or e.when(result, ctx or {}))
                   for e in self._edges())

    CRASH = {"pass": False, "crashed": True, "issues": ["reviewer crashed: boom"]}
    REJECT = {"pass": False, "issues": ["the null check is missing"]}

    def test_a_crash_retries_the_review(self):
        self.assertTrue(self._fires("review_t1", self.CRASH))

    def test_a_crash_does_not_go_to_the_implementer(self):
        self.assertFalse(self._fires("implement_t1", self.CRASH))

    def test_a_crash_does_not_trigger_an_escalation(self):
        self.assertFalse(self._fires("escalate_t1", self.CRASH))

    def test_a_real_rejection_still_goes_to_the_implementer(self):
        self.assertTrue(self._fires("implement_t1", self.REJECT))

    def test_a_real_rejection_does_not_retry_the_review(self):
        self.assertFalse(self._fires("review_t1", self.REJECT))

    def test_repeated_crashes_eventually_fail_the_task(self):
        ctx = {"runs": {"review_t1": config.MAX_REVIEW_CRASHES}}
        self.assertTrue(self._fires("fail_t1", self.CRASH, ctx))
        self.assertFalse(self._fires("review_t1", self.CRASH, ctx))

    def test_the_crash_budget_is_separate_from_the_fix_budget(self):
        self.assertGreater(config.MAX_REVIEW_CRASHES, 0)

    def test_a_passing_review_is_unaffected(self):
        self.assertTrue(self._fires("publish_t1", {"pass": True}))


class AJoinWaitsForEveryDependency(unittest.TestCase):
    """`deps` used to wire only the LAST dependency.

    A task declaring deps ["a", "b"] waited for b and started the moment b
    merged, whether or not a had. If a was the slower of the two, the dependent
    branched from a base missing the code it depended on. That is not a join.
    The engine has had a real one (gather=True) the whole time — the research
    and build graphs use it; the code graph never did.
    """

    def _graph(self, deps, merged=()):
        mk = {"model": config.ESCALATION_PATH[0], "reviewer": config.cross_family_reviewer(config.ESCALATION_PATH[0])}
        tasks = [{"id": "a", "title": "a", "prompt": "p", **mk},
                 {"id": "b", "title": "b", "prompt": "p", **mk},
                 {"id": "c", "title": "c", "prompt": "p", **mk, "deps": deps}]
        prior = [{"id": m, "status": "merged", "model": config.ESCALATION_PATH[0], "error": None} for m in merged]
        ts = code_tasks.load_taskfile(taskfile(tasks))
        with capture_events():
            return code_tasks.build_code_graph(FakeStore(prior), ts, taskfile="tf.json")

    def test_two_deps_gate_through_a_gather_node(self):
        g = self._graph(["a", "b"])
        self.assertIn("join_c", g.nodes)
        self.assertTrue(g.nodes["join_c"].gather)
        self.assertEqual(sorted(e.src for e in g.edges if e.dst == "join_c"),
                         ["pr_merge_a", "pr_merge_b"])

    def test_the_join_feeds_the_dependents_first_node(self):
        g = self._graph(["a", "b"])
        self.assertTrue(any(e.src == "join_c" and e.dst == "alloc_c" for e in g.edges))

    def test_neither_dep_alone_can_release_the_dependent(self):
        # The bug: pr_merge_b -> alloc_c directly. Neither dep may now do that.
        g = self._graph(["a", "b"])
        direct = [e.src for e in g.edges if e.dst == "alloc_c" and e.src.startswith("pr_merge_")]
        self.assertEqual(direct, [])

    def test_a_single_dep_keeps_the_direct_edge(self):
        g = self._graph(["b"])
        self.assertNotIn("join_c", g.nodes)
        self.assertTrue(any(e.src == "pr_merge_b" and e.dst == "alloc_c" for e in g.edges))

    def test_a_merged_dependent_also_joins_on_every_dep(self):
        # make_skip had the same deps[-1] wiring.
        g = self._graph(["a", "b"], merged={"c"})
        self.assertIn("join_c", g.nodes)
        self.assertTrue(any(e.src == "join_c" and e.dst == "publish_c" for e in g.edges))

    def test_the_join_actually_waits_at_runtime(self):
        """Engine-level: the gather node must not fire until BOTH sources have."""
        import asyncio
        from graph import Graph
        g = Graph("j"); seen = []
        slow_done = asyncio.Event()

        async def fast(ctx): return {"n": "fast"}
        async def slow(ctx):
            await slow_done.wait(); return {"n": "slow"}
        async def joined(ctx):
            seen.append(sorted(ctx["results"])); return {}
        g.node("fast", fast); g.node("slow", slow); g.node("join", joined, gather=True)
        g.edge("fast", "join"); g.edge("slow", "join"); g.start("fast"); g.start("slow")

        async def run():
            task = asyncio.create_task(g.run({}))
            await asyncio.sleep(0.05)
            self.assertEqual(seen, [], "join fired before the slow source finished")
            slow_done.set()
            await task
        asyncio.run(run())
        self.assertEqual(seen, [["fast", "slow"]])


class ReviewersAreRealGraphNodes(unittest.TestCase):
    """PR reviewers fan out as Spawn'd nodes, not inside one asyncio.gather.

    Inside one node they were invisible to the graph: not in the diagram, not
    checkpointed, not individually retryable, and a crashed reviewer surfaced
    only as a field on its parent's result. Each is now pr_reviewer_<tid> with
    its own retry policy and timeout, joined at pr_review_<tid>.
    """

    def _graph(self):
        ts = code_tasks.load_taskfile(taskfile([BASIC]))
        with capture_events():
            return code_tasks.build_code_graph(FakeStore([]), ts, taskfile="tf.json")

    def test_the_three_review_nodes_exist(self):
        g = self._graph()
        for n in ("pr_fanout_t1", "pr_reviewer_t1", "pr_review_t1"):
            self.assertIn(n, g.nodes)

    def test_the_reviewer_node_has_its_own_retry_and_timeout(self):
        g = self._graph()
        n = g.nodes["pr_reviewer_t1"]
        self.assertIsNotNone(n.retry)
        self.assertEqual(n.retry.on, (code_tasks.DriverError,))
        reviewer_total = config.total_timeout_for("reviewer")
        if reviewer_total > 0:
            self.assertGreater(n.timeout, reviewer_total)
        else:
            # Unlimited budgets (the default): no node timeout — graph.Node's
            # None = unbounded, and the driver's idle kill still bounds a
            # silent reviewer.
            self.assertIsNone(n.timeout)

    def test_the_fanout_returns_a_spawn_with_one_item_per_reviewer(self):
        """Run the real pr_fanout node against stubbed git/store."""
        import asyncio as aio
        from graph import Spawn
        g = self._graph()
        fan = g.nodes["pr_fanout_t1"].fn
        orig = code_tasks.gitstore.pr_diff
        code_tasks.gitstore.pr_diff = lambda repo, n: aio.sleep(0, result="diff --git a b")
        try:
            # Pin the DEFAULT mode: under ARC_ALLOW_SAME_FAMILY_REVIEW=1 the
            # reviewer pool inverts to same-family only, so PR_REVIEWERS
            # cross-family readers are not what this node is asked to fan out.
            with mock.patch.object(config, "ALLOW_SAME_FAMILY_REVIEW", False):
                out = aio.run(fan({"results": {"publish_t1": {"pr": 42}}, "runs": {}}))
        finally:
            code_tasks.gitstore.pr_diff = orig
        self.assertIsInstance(out, Spawn)
        self.assertEqual(out.target, "pr_reviewer_t1")
        self.assertEqual(out.join, "pr_review_t1")
        self.assertEqual(len(out.items), config.PR_REVIEWERS)
        models = [i["model"] for i in out.items]
        self.assertEqual(len(set(models)), len(models), "reviewers must differ")
        self.assertTrue(all(i["pr"] == 42 for i in out.items))

    def _retry_fanout(self, pool, tiers, prior, pressure, reviewers=1):
        g = self._graph()
        ctx = {"results": {"publish_t1": {"pr": 42},
                           "pr_review_t1": prior}, "runs": {"pr_review_t1": 1}}
        with (mock.patch.object(code_tasks.gitstore, "pr_diff", new=mock.AsyncMock(return_value="diff")),
              mock.patch.object(code_tasks, "_eligible_pr_reviewers", return_value=pool),
              mock.patch.object(code_tasks, "_reviewer_pressure",
                                side_effect=lambda m, usage: pressure[m]),
              mock.patch.dict(config.MODEL_TIER, tiers),
              mock.patch.object(config, "PR_REVIEWERS", reviewers),
              mock.patch.object(config, "PR_REVIEWERS_WANTED", 2),
              capture_events() as ev):
            out = asyncio.run(g.nodes["pr_fanout_t1"].fn(ctx))
        selected = [fields for name, fields in ev.seen
                    if name == "task.pr_review_selected"]
        thin = [fields for name, fields in ev.seen
                if name == "task.pr_review_thin"]
        return out, selected[0], (thin[0] if thin else None)

    def test_crashed_reviewer_loses_to_healthy_same_tier_even_when_idle(self):
        prior = {"inconclusive": True, "crashed_models": ["failed-hard"]}
        out, selection, thin = self._retry_fanout(
            ["failed-hard", "healthy-medium", "healthy-hard"],
            {"failed-hard": "hard", "healthy-medium": "medium",
             "healthy-hard": "hard"}, prior,
            {"failed-hard": 0, "healthy-medium": 0, "healthy-hard": 1})
        self.assertEqual([item["model"] for item in out.items], ["healthy-hard"])
        self.assertEqual(selection["reviewers"], ["healthy-hard"])
        self.assertEqual(selection["reason"], "healthy_same_or_stronger_after_crash")
        self.assertEqual(selection["crashed_before"], ["failed-hard"])
        self.assertEqual(thin["got"], 1)

    def test_crashed_reviewer_retries_when_only_alternative_is_weaker(self):
        for pool in (["failed-hard"], ["failed-hard", "healthy-medium"]):
            with self.subTest(pool=pool):
                out, selection, _ = self._retry_fanout(
                    pool, {"failed-hard": "hard", "healthy-medium": "medium"},
                    {"inconclusive": True, "crashed": ["failed-hard"]},
                    {"failed-hard": 0, "healthy-medium": 0})
                self.assertEqual([item["model"] for item in out.items],
                                 ["failed-hard"])
                self.assertEqual(selection["reason"], "retry_crashed_reviewer")

    def test_genuine_rejection_clears_crash_preference(self):
        g = self._graph()
        ctx = {"results": {
            "pr_fanout_t1": {"pr": 42, "round": 1,
                              "reviewers": ["failed-hard", "rejecting-hard"]},
            "pr_reviewer_t1": [
                {"model": "failed-hard", "crashed": True, "approve": False,
                 "issues": ["unavailable"]},
                {"model": "rejecting-hard", "approve": False,
                 "issues": ["missing test"]}]}, "runs": {}}
        with (mock.patch.object(code_tasks.gitstore, "_gh",
                                new=mock.AsyncMock(return_value=(0, "", ""))),
              capture_events()):
            result = asyncio.run(g.nodes["pr_review_t1"].fn(ctx))
        self.assertFalse(result["inconclusive"])
        self.assertEqual(result["crashed_models"], [])
        self.assertIn("[rejecting-hard] missing test", result["issues"])
        out, selection, _ = self._retry_fanout(
            ["failed-hard", "healthy-hard"],
            {"failed-hard": "hard", "healthy-hard": "hard"}, result,
            {"failed-hard": 0, "healthy-hard": 1})
        self.assertEqual([item["model"] for item in out.items], ["failed-hard"])
        self.assertEqual(selection["reason"], "least_loaded")

    def test_mixed_tier_crashes_each_take_a_healthy_replacement(self):
        prior = {"inconclusive": True,
                 "crashed_models": ["failed-hard", "failed-medium"]}
        out, selection, thin = self._retry_fanout(
            ["failed-hard", "failed-medium", "healthy-medium", "healthy-hard"],
            {"failed-hard": "hard", "failed-medium": "medium",
             "healthy-medium": "medium", "healthy-hard": "hard"},
            prior,
            {"failed-hard": 0, "failed-medium": 0,
             "healthy-medium": 0, "healthy-hard": 1},
            reviewers=2)
        self.assertEqual([item["model"] for item in out.items],
                         ["healthy-hard", "healthy-medium"])
        self.assertEqual(selection["reviewers"],
                         ["healthy-hard", "healthy-medium"])
        self.assertEqual(selection["reason"],
                         "healthy_same_or_stronger_after_crash")
        self.assertIsNone(thin)

    def test_no_pull_request_short_circuits_to_the_join(self):
        import asyncio as aio
        from graph import Spawn
        g = self._graph()
        out = aio.run(g.nodes["pr_fanout_t1"].fn({"results": {"publish_t1": {}}, "runs": {}}))
        self.assertNotIsInstance(out, Spawn)
        self.assertTrue(out["no_pr"])
        e = next(e for e in g.edges if e.src == "pr_fanout_t1" and e.dst == "pr_review_t1")
        self.assertTrue(e.when(out, {}))

    def test_the_join_tallies_the_spawned_verdicts(self):
        import asyncio as aio
        g = self._graph()
        join = g.nodes["pr_review_t1"].fn
        orig = code_tasks.gitstore._gh
        code_tasks.gitstore._gh = lambda *a, **k: aio.sleep(0, result=(0, "", ""))
        try:
            with capture_events() as ev:
                out = aio.run(join({"results": {
                    "pr_fanout_t1": {"pr": 7, "round": 1, "reviewers": ["A", "B"]},
                    "pr_reviewer_t1": [
                        {"model": "A", "approve": True, "issues": []},
                        {"model": "B", "approve": False, "issues": ["missing test"]}]},
                    "runs": {}}))
        finally:
            code_tasks.gitstore._gh = orig
        self.assertFalse(out["approved"])
        self.assertEqual(out["approvals"], ["A"])
        self.assertEqual(out["issues"], ["[B] missing test"])
        rev = [f for t, f in ev.seen if t == "task.pr_reviewed"]
        self.assertEqual(rev[0]["n_issues"], 1)

    def test_every_reviewer_crashing_is_inconclusive_at_the_join(self):
        import asyncio as aio
        g = self._graph()
        join = g.nodes["pr_review_t1"].fn
        orig = code_tasks.gitstore._gh
        code_tasks.gitstore._gh = lambda *a, **k: aio.sleep(0, result=(0, "", ""))
        try:
            out = aio.run(join({"results": {
                "pr_fanout_t1": {"pr": 7, "round": 1, "reviewers": ["A", "B"]},
                "pr_reviewer_t1": [
                    {"model": "A", "approve": False, "crashed": True, "issues": ["x"]},
                    {"model": "B", "approve": False, "crashed": True, "issues": ["y"]}]},
                "runs": {}}))
        finally:
            code_tasks.gitstore._gh = orig
        self.assertTrue(out["inconclusive"])
        self.assertEqual(out["inconclusive_n"], 1)
        self.assertEqual(sorted(out["crashed"]), ["A", "B"])
        self.assertEqual(out["crashed_models"], ["A", "B"])

    def test_inconclusive_retries_remember_earlier_crashes(self):
        g = self._graph()
        ctx = {"results": {
            "pr_review_t1": {"inconclusive": True, "inconclusive_n": 1,
                              "crashed_models": ["A"]},
            "pr_fanout_t1": {"pr": 7, "round": 2, "reviewers": ["B"]},
            "pr_reviewer_t1": [{"model": "B", "approve": False,
                                "crashed": True, "issues": ["unavailable"]}]},
            "runs": {}}
        with (mock.patch.object(code_tasks.gitstore, "_gh",
                                new=mock.AsyncMock(return_value=(0, "", ""))),
              capture_events()):
            out = asyncio.run(g.nodes["pr_review_t1"].fn(ctx))
        self.assertEqual(out["crashed_models"], ["A", "B"])
        self.assertEqual(out["inconclusive_n"], 2)

    def test_the_pipeline_diagram_now_shows_the_fanout(self):
        import dashboard
        topo = dashboard._build_graph_topologies()["code"]
        names = {n["name"] for n in topo["nodes"]}
        self.assertIn("pr_fanout", names)
        self.assertIn("pr_reviewer", names)


class RetiredModelsAreRemappedNotRejected(unittest.TestCase):
    """A taskfile written before a roster transition is still a good plan.

    gpt-oss retired 09-11; DeepSeek-V4 replaced 09-12; Kimi-K3 retired
    09-12 by operator decision.
    Fourteen taskfiles on disk named gpt-oss the morning after it left; failing
    every one of them with "must be an implementer" would have thrown away
    fourteen decompositions over a stale label.
    """

    def test_a_retired_model_loads_as_its_replacement(self):
        ts = code_tasks.load_taskfile(taskfile([{**BASIC, "model": "gpt-oss-120b"}]))
        self.assertEqual(ts["tasks"]["t1"]["model"], config.ESCALATION_PATH[0])

    def test_a_model_that_was_never_real_is_still_rejected(self):
        with self.assertRaises(ValueError):
            code_tasks.load_taskfile(taskfile([{**BASIC, "model": "GPT-9-Ultra"}]))

    def test_every_retired_name_has_a_live_destination(self):
        for name, where in code_tasks.RETIRED_MODELS.items():
            dest = where()
            if name in config.IMPLEMENTER_MODELS:
                continue  # not retired yet on this roster date
            self.assertIn(dest, config.IMPLEMENTER_MODELS,
                          f"{name} remaps to {dest!r}, which is not live")

    def test_retired_studio_subscription_models_keep_hard_tier_and_cross_review(self):
        for old in ("Cursor-Grok-4.7", "Antigravity-Gemini"):
            with self.subTest(model=old):
                target = code_tasks.RETIRED_MODELS[old]()
                self.assertIn(target, config.IMPLEMENT_TIERS["hard"])
                same_family = config.MODEL_FAMILY[target]
                with mock.patch.object(config, "ALLOW_SAME_FAMILY_REVIEW", False):
                    ts = code_tasks.load_taskfile(taskfile([
                        {**BASIC, "model": old, "reviewer": same_family}]))
                task = ts["tasks"]["t1"]
                self.assertEqual(task["model"], target)
                self.assertNotEqual(task["reviewer"], same_family)


class RetiredModelRemapKeepsCrossReview(unittest.TestCase):
    """A retired model's task lands on a live tier; if that tier's family is
    the task's reviewer, the reviewer flips. On 2026-09-12 the provider
    stopped serving DeepSeek: every old DeepSeek task with reviewer "glm"
    remapped to GLM-5.3 and then failed as a self-review, and check.sh's
    taskfile-validity step was red for the whole tasks directory."""

    def test_remapped_task_flips_a_now_same_family_reviewer(self):
        import config as cfg
        retired = next((m for m in code_tasks.RETIRED_MODELS
                        if m not in cfg.IMPLEMENTER_MODELS), None)
        if retired is None:
            self.skipTest("every model in RETIRED_MODELS is live today")
        target = code_tasks.RETIRED_MODELS[retired]() or cfg.ESCALATION_PATH[0]
        same = cfg.MODEL_FAMILY[target]
        if same not in cfg.REVIEW_FAMILIES:
            self.skipTest(f"{target}'s family cannot review, nothing to collide with")
        # Pins the default flip; the same-family capacity hatch (exported during
        # gate runs in an outage) makes the loader keep a same-family reviewer.
        with mock.patch.object(cfg, "ALLOW_SAME_FAMILY_REVIEW", False):
            ts = code_tasks.load_taskfile(taskfile([{**BASIC, "model": retired, "reviewer": same}]))
        t = ts["tasks"]["t1"]
        self.assertEqual(t["model"], target)
        self.assertNotEqual(cfg.MODEL_FAMILY.get(t["reviewer"], t["reviewer"]), same)

    def test_a_live_model_with_its_own_family_as_reviewer_is_still_rejected(self):
        same = {**BASIC, "model": STRONGEST, "reviewer": STRONGEST_FAMILY}
        # Pins the default rejection; the same-family capacity hatch (exported
        # during gate runs in an outage) makes the loader accept this pairing.
        with mock.patch.object(config, "ALLOW_SAME_FAMILY_REVIEW", False):
            with self.assertRaises(ValueError):
                code_tasks.load_taskfile(taskfile([same]))


class AnExternallyMergedPullRequestIsNotAConflict(unittest.TestCase):
    """pr_merge must accept a PR that someone else already merged.

    An operator merging from the GitHub UI, or a hand merge of a backlog,
    beats the fleet's own reviewers to it. `gh pr merge` on a merged PR exits
    non-zero, and that used to be recorded as a conflict: the task was marked
    failed and every task depending on it never started — for a change that
    had, in fact, landed.
    """

    def _graph(self):
        ts = code_tasks.load_taskfile(taskfile([BASIC]))
        with capture_events():
            return code_tasks.build_code_graph(FakeStore([]), ts, taskfile="tf.json")

    def _run_merge(self, state, merge_rc):
        import asyncio as aio
        g = self._graph()
        gs = code_tasks.gitstore
        calls = []
        saved = (gs.pr_state, gs.merge_pr, gs.fast_forward_base, gs.cleanup)
        gs.pr_state = lambda repo, n: aio.sleep(0, result=state)
        gs.merge_pr = lambda repo, n: (calls.append("merge_pr"),
                                       aio.sleep(0, result=merge_rc))[1]
        gs.fast_forward_base = lambda repo, base: aio.sleep(0, result=(True, ""))
        gs.cleanup = lambda repo, tid: aio.sleep(0)
        try:
            with capture_events() as ev:
                out = aio.run(g.nodes["pr_merge_t1"].fn(
                    {"results": {"pr_review_t1": {"pr": 7, "approved": True,
                                                  "approvals": 1}}, "runs": {}}))
        finally:
            gs.pr_state, gs.merge_pr, gs.fast_forward_base, gs.cleanup = saved
        return out, calls, ev

    def test_a_merged_pr_counts_as_merged_without_calling_gh(self):
        out, calls, ev = self._run_merge(
            {"state": "MERGED", "mergeable": "UNKNOWN"}, (False, "already merged"))
        self.assertTrue(out["merged"])
        self.assertEqual(calls, [], "gh pr merge must not run on a merged PR")
        self.assertIsNotNone(ev.first("task.merged_externally"))
        self.assertIsNotNone(ev.first("task.merged"))
        self.assertIsNone(ev.first("task.conflict"))

    def test_an_open_pr_is_still_merged_by_the_fleet(self):
        out, calls, ev = self._run_merge(
            {"state": "OPEN", "mergeable": "MERGEABLE"}, (True, "merged"))
        self.assertTrue(out["merged"])
        self.assertEqual(calls, ["merge_pr"])
        self.assertIsNone(ev.first("task.merged_externally"))


class PlanAmendmentWiring(unittest.TestCase):
    """The plan-amendment channel (plan_amend.py) is grafted into the graph.

    plan_amend's own behaviour lives in test_plan_amend.py; here we pin the
    WIRING, by source slice like the other ownership tests: every agent run
    harvests the proposals file (success AND crash paths — a reviewer that
    crashed may still have written one), publish sweeps once more before its
    `git add -A`, and the prompts only offer the channel when handed the real
    task ids (a guessed dep target costs a rejection).
    """

    def _slice(self, start, end):
        src = pathlib.Path(code_tasks.__file__).read_text()
        body = src[src.index(start):]
        return body[:body.index(end)]

    def test_roster_built_from_the_whole_taskfile(self):
        src = pathlib.Path(code_tasks.__file__).read_text()
        self.assertIn('roster = ([(tid, t["title"]) for tid, t in tasks.items()]',
                      src)

    def test_bench_virtual_taskfile_gets_no_channel(self):
        # orchbench passes a key like `orchbench:<variant>:<stamp>` — no file
        # on disk. Bench prompts must not drift from their fixed form, and a
        # proposal can never apply to a file that is not there, so both the
        # roster and the harvest guard out on a missing taskfile.
        src = pathlib.Path(code_tasks.__file__).read_text()
        self.assertIn("if taskfile and Path(taskfile).is_file() else None", src)
        hp = src.index("def harvest_proposals")
        self.assertIn("not Path(taskfile).is_file()", src[hp:hp + 900])

    def test_adds_never_stage_the_channel_file(self):
        # publish() and diff_full()'s intent-to-add both stage then unstage
        # the channel files. Ignored .arc paths cannot be named in git add's
        # exclusion pathspec (Git 2.55 errors), so they are reset afterwards.
        import gitstore
        src = pathlib.Path(gitstore.__file__).read_text()
        self.assertEqual(set(gitstore.CHANNEL_FILES),
                         {".arc/plan_proposals.jsonl", ".arc/board.jsonl",
                          ".arc/handoff.md"})
        self.assertIn(":!.reasonix", gitstore.NEVER_STAGE)
        self.assertIn("await _stage_without_runtime_files(wt, intent=True)", src)
        self.assertIn("await _stage_without_runtime_files(wt)", src)

    def test_publish_never_commits_harness_state(self):
        """A real repo: .reasonix state beside a real change stays unstaged."""
        import asyncio, subprocess, tempfile
        import gitstore
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
            run = lambda *a: subprocess.run(["git", "-C", d, *a], env=env,
                                            check=True, capture_output=True)
            run("init", "-q", "-b", "main")
            pathlib.Path(d, "a.txt").write_text("1")
            run("add", "a.txt"); run("commit", "-qm", "init")
            pathlib.Path(d, "a.txt").write_text("2")
            state = pathlib.Path(d, ".reasonix", "tasks", "run-1")
            state.mkdir(parents=True)
            (state / "events.jsonl").write_text("{}")
            old = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
            try:
                head = asyncio.run(gitstore.publish(d, "task(t): x"))
            finally:
                for k, v in old.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
            self.assertTrue(head)
            files = subprocess.run(["git", "-C", d, "show", "--name-only", "--format=", head],
                                   capture_output=True, text=True).stdout.split()
            self.assertEqual(files, ["a.txt"])

    def test_implement_harvests_on_success_and_crash(self):
        body = self._slice("async def implement(ctx):", "async def gate(ctx):")
        self.assertEqual(body.count('harvest_proposals(tid, wt, "implementer"'), 2)

    def test_review_harvests_on_success_and_crash(self):
        body = self._slice("async def review(ctx):", "async def publish(ctx):")
        self.assertGreaterEqual(body.count('harvest_proposals(tid, wt, "reviewer"'), 2)

    def test_pr_reviewer_harvests_on_success_and_crash(self):
        body = self._slice("async def pr_reviewer(ctx):", "async def pr_review(ctx)")
        self.assertEqual(body.count('harvest_proposals(tid, wt, "pr-reviewer"'), 2)

    def test_publish_sweeps_before_committing(self):
        body = self._slice("async def publish(ctx):", "async def pr_reviewer(ctx):")
        sweep = body.index('harvest_proposals(tid, wt, "publish-sweep"')
        commit = body.index("gitstore.publish(")
        self.assertLess(sweep, commit,
                        "publish commits with `git add -A`; the proposal file "
                        "must be collected (and deleted) first or it lands in the PR")

    def _task(self):
        return dict(BASIC, files_hint=[], verify_cmd="true")

    def test_prompts_offer_the_channel_only_with_a_roster(self):
        import plan_amend
        roster = [("t1", "T1"), ("t2", "T2")]
        p = code_tasks._impl_prompt(self._task(), "", roster=roster)
        self.assertIn(plan_amend.PROPOSALS_REL, p)
        self.assertIn("t1, t2", p, "dep targets come from the real ids")
        for m in config.IMPLEMENTER_MODELS:
            self.assertIn(m, p)
        self.assertNotIn(plan_amend.PROPOSALS_REL,
                         code_tasks._impl_prompt(self._task(), ""))

    def test_review_and_pr_review_prompts_offer_it_too(self):
        import plan_amend
        roster = [("t1", "T1"), ("t2", "T2")]
        r = code_tasks._review_prompt(self._task(), "diff", roster=roster)
        pr = code_tasks._pr_review_prompt(self._task(), "diff", 2, 1, "",
                                          roster=roster)
        self.assertIn(plan_amend.PROPOSALS_REL, r)
        self.assertIn(plan_amend.PROPOSALS_REL, pr)
        self.assertNotIn(plan_amend.PROPOSALS_REL,
                         code_tasks._review_prompt(self._task(), "diff"))


class TransientNetworkRetries(unittest.TestCase):
    """A network blip on push must not fail a finished, reviewed task."""

    def setUp(self):
        self._old = config.NET_RETRY_DELAYS
        config.NET_RETRY_DELAYS = [0, 0, 0]
        self.addCleanup(setattr, config, "NET_RETRY_DELAYS", self._old)

    def test_classifier(self):
        import gitstore
        self.assertTrue(gitstore.is_transient_network_error(
            "fatal: unable to access 'https://github.com/o/r.git/': SSL connection timeout"))
        self.assertTrue(gitstore.is_transient_network_error("Could not resolve host: github.com"))
        self.assertFalse(gitstore.is_transient_network_error(
            "! [rejected] task/t -> task/t (stale info)"))
        self.assertFalse(gitstore.is_transient_network_error(
            "GraphQL: No commits between main and task/t"))

    def test_network_failures_retry_until_success(self):
        import gitstore
        calls = []

        async def once():
            calls.append(1)
            if len(calls) < 3:
                return False, "fatal: unable to access 'https://github.com/o/r.git/'"
            return True, "pushed"
        with capture_events() as ev:
            ok, _note = asyncio.run(gitstore._retry_transient("push", once))
        self.assertTrue(ok)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(ev.of("git.retry")), 2)

    def test_real_refusals_are_not_retried(self):
        import gitstore
        calls = []

        async def once():
            calls.append(1)
            return False, "! [rejected] (stale info)"
        ok, _ = asyncio.run(gitstore._retry_transient("push", once))
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1)


class DossierWiring(unittest.TestCase):
    """The task dossier (dossier.py) is booted into every prompt and fed by
    every agent run: attempt 2 must see what attempt 1 wrote in its handoff."""

    def _slice(self, start, end):
        src = pathlib.Path(code_tasks.__file__).read_text()
        body = src[src.index(start):]
        return body[:body.index(end)]

    def test_every_prompt_carries_the_dossier(self):
        self.assertIn('dossier_block(tid, "implementer")',
                      self._slice("async def implement(ctx):", "async def gate(ctx):"))
        self.assertIn('dossier_block(tid, "reviewer")',
                      self._slice("async def review(ctx):", "async def escalate(ctx):"))
        self.assertIn('dossier_block(tid, "pr-reviewer")',
                      self._slice("async def pr_reviewer(ctx):", "async def pr_review(ctx)"))

    def test_dossier_leads_each_prompt(self):
        t = dict(BASIC, files_hint=[], verify_cmd="true")
        for p in (code_tasks._impl_prompt(t, "fb", dossier="DOSSIER-X"),
                  code_tasks._review_prompt(t, "diff", dossier="DOSSIER-X"),
                  code_tasks._pr_review_prompt(t, "diff", 1, 1, [],
                                               dossier="DOSSIER-X")):
            self.assertTrue(p.startswith("DOSSIER-X"))
        self.assertIn(".arc/handoff.md", code_tasks._impl_prompt(t, ""))

    def test_crash_paths_record_the_attempt(self):
        body = self._slice("async def implement(ctx):", "async def gate(ctx):")
        self.assertIn('outcome="crashed"', body)
        self.assertIn('outcome="usage_swap"', body)
        body = self._slice("async def review(ctx):", "async def escalate(ctx):")
        self.assertIn('outcome="crashed"', body)
        body = self._slice("async def escalate(ctx):", "async def publish(ctx):")
        self.assertIn("note_model_change", body)

    def test_reviewer_usage_swaps_record_why_and_who_ran(self):
        body = self._slice("async def review(ctx):", "async def escalate(ctx):")
        self.assertIn("note_model_change", body)
        body = self._slice("async def pr_reviewer(ctx):", "async def pr_review(ctx)")
        self.assertIn("note_model_change", body)
        self.assertIn('ran_model = getattr(res, "model", None) or model', body)
        self.assertIn('model=ran_model, role="pr-reviewer"', body)

    def test_an_empty_verify_gate_still_records_the_attempt(self):
        import dossier
        repo = tempfile.mkdtemp(prefix="arc-dossier-repo-")
        wt = tempfile.mkdtemp(prefix="arc-dossier-wt-")
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        self.addCleanup(shutil.rmtree, wt, ignore_errors=True)
        ts = code_tasks.load_taskfile(taskfile([{
            "id": "dz2", "title": "T", "prompt": "do it", "verify_cmd": "",
            "model": "GLM-5.3", "reviewer": "deepseek"}], repo=repo))
        with capture_events():
            g = code_tasks.build_code_graph(FakeStore(), ts, taskfile="tf.json")
            out = asyncio.run(g.nodes["gate_dz2"].fn(
                {"results": {"alloc_dz2": {"worktree": wt},
                             "implement_dz2": {"model": "GLM-5.3",
                                               "harness": "opencode"}},
                 "runs": {"implement_dz2": 1}}))
        self.assertTrue(out["passed"])
        att = dossier.get(Path(repo).name, "dz2")["attempts"]
        self.assertEqual([(a["outcome"], a["attempt"], a["model"]) for a in att],
                         [("passed", 1, "GLM-5.3")])

    def test_the_prompt_includes_the_dossier_on_attempt_2(self):
        repo = tempfile.mkdtemp(prefix="arc-dossier-repo-")
        wt = tempfile.mkdtemp(prefix="arc-dossier-wt-")
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        self.addCleanup(shutil.rmtree, wt, ignore_errors=True)
        ts = code_tasks.load_taskfile(taskfile([{
            "id": "dz1", "title": "T", "prompt": "do it", "verify_cmd": "true",
            "model": "GLM-5.3", "reviewer": "deepseek"}], repo=repo))
        prompts = []

        class Res:
            exit_code, transcript_path, seconds = 0, "", 1.0
            session_id, text, model, harness = "s1", "ok", None, None

        class Drv:
            harness, model, images = "opencode", "GLM-5.3", None

            async def run(self, prompt, cwd, **kw):
                prompts.append(prompt)
                arc = Path(cwd, ".arc")
                arc.mkdir(exist_ok=True)
                (arc / "handoff.md").write_text(
                    "## Decisions\n- keep the cache in store.py (one writer)\n"
                    "## Next step\nwire the CLI\n")
                return Res()

        async def hints(t, wt):
            return ""

        with mock.patch.object(code_tasks, "_driver", lambda m, r, p: Drv()), \
                mock.patch.object(code_tasks.graft, "hints", hints), \
                capture_events():
            g = code_tasks.build_code_graph(FakeStore(), ts, taskfile="tf.json")
            for runs in ({}, {"implement_dz1": 1}):
                asyncio.run(g.nodes["implement_dz1"].fn(
                    {"results": {"alloc_dz1": {"worktree": wt}}, "runs": runs}))
        self.assertNotIn("TASK DOSSIER", prompts[0])
        self.assertTrue(prompts[1].startswith("TASK DOSSIER"))
        self.assertIn("keep the cache in store.py", prompts[1])
        self.assertIn("wire the CLI", prompts[1])
        self.assertFalse(Path(wt, ".arc", "handoff.md").exists())


class AgentBoardWiring(unittest.TestCase):
    """Every governed run uses the agent board (agentboard.py, Rule 4c):
    claims on files_hint, the reader's digest in every prompt, agents'
    .arc/board.jsonl ingested after every run (crash paths too), and the
    orchestrator's own status / result / question posts."""

    def setUp(self):
        import agentboard
        self.ab = agentboard
        self.repo = tempfile.mkdtemp(prefix="arc-board-repo-")
        self.wt = tempfile.mkdtemp(prefix="arc-board-wt-")
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.wt, ignore_errors=True)
        self.project = Path(self.repo).name
        self.ts = code_tasks.load_taskfile(taskfile([{
            "id": "bw1", "title": "T", "prompt": "do it", "verify_cmd": "true",
            "model": "GLM-5.3", "reviewer": "deepseek",
            "files_hint": ["pkg/a.py"]}], repo=self.repo))
        self.prompts = []

    def _drv(self, text="ok", crash=None, line=None):
        prompts = self.prompts

        class Res:
            exit_code, transcript_path, seconds = 0, "", 1.0
            session_id, model, harness = "s1", None, None

        Res.text = text

        class Drv:
            harness, model, images = "opencode", "GLM-5.3", None

            async def run(self, prompt, cwd, **kw):
                prompts.append(prompt)
                if line is not None:
                    arc = Path(cwd, ".arc")
                    arc.mkdir(exist_ok=True)
                    with open(arc / "board.jsonl", "a") as f:
                        f.write(json.dumps(line) + "\n")
                if crash:
                    raise code_tasks.DriverError(crash)
                return Res()
        return Drv()

    def _graph(self):
        return code_tasks.build_code_graph(FakeStore(), self.ts, taskfile="tf.json")

    def _implement(self, g, runs=None):
        async def hints(t, wt):
            return ""
        with mock.patch.object(code_tasks.graft, "hints", hints):
            return asyncio.run(g.nodes["implement_bw1"].fn(
                {"results": {"alloc_bw1": {"worktree": self.wt}},
                 "runs": runs or {}}))

    def _msgs(self, **kw):
        return self.ab.thread(self.project, **kw)

    def test_the_claim_is_taken_and_released_on_fail(self):
        with mock.patch.object(code_tasks, "_driver", lambda m, r, p: self._drv()), \
                capture_events():
            g = self._graph()
            self._implement(g)
            live = self.ab.claims(self.project)
            self.assertEqual([(c["author"], c["paths"]) for c in live],
                             [("bw1/implementer", ["pkg/a.py"])])
            # A fix round renews the lease rather than stacking a second one.
            self._implement(g, {"implement_bw1": 1})
            self.assertEqual(len(self.ab.claims(self.project)), 1)
            asyncio.run(g.nodes["fail_bw1"].fn({"results": {}, "runs": {}}))
        self.assertEqual(self.ab.claims(self.project), [])

    def test_a_conflicting_claim_pings_the_other_task_and_reaches_the_prompt(self):
        self.ab.claim(self.project, task="other", author="other/implementer",
                      paths=["pkg"])
        with mock.patch.object(code_tasks, "_driver", lambda m, r, p: self._drv()), \
                capture_events():
            self._implement(self._graph())
        self.assertIn("CLAIM CONFLICTS", self.prompts[0])
        self.assertIn("other/implementer holds pkg", self.prompts[0])
        pings = [m for m in self._msgs(channel="task:other") if m["kind"] == "ping"]
        self.assertEqual(len(pings), 1)
        self.assertTrue({"other", "bw1"} <= set(pings[0]["mentions"]))

    def test_the_digest_replaces_the_old_block(self):
        with mock.patch.object(code_tasks, "_driver", lambda m, r, p: self._drv()), \
                mock.patch.object(code_tasks.board, "prompt_block",
                                  lambda *a, **k: "OLD-BLOCK"), capture_events():
            g = self._graph()
            self._implement(g)
            with mock.patch.object(code_tasks.agentboard, "digest_for",
                                   lambda *a, **k: ""):
                self._implement(g, {"implement_bw1": 1})
        self.assertIn(f"AGENT BOARD for {self.project}", self.prompts[0])
        self.assertNotIn("OLD-BLOCK", self.prompts[0])
        self.assertIn("board post", self.prompts[0])
        self.assertIn("your agent ID is bw1/implementer", self.prompts[0])
        self.assertIn("OLD-BLOCK", self.prompts[1], "fallback when the digest is empty")

    def test_files_the_attempt_changed_are_leased_after_the_run(self):
        """Most taskfiles carry no files_hint, so the pre-run lease covered
        nothing (every prison-escape claim had paths=[]). What the attempt
        actually changed is leased, so a sibling's digest warns it off."""
        import subprocess as sp
        for c in (["init", "-q", "-b", config.BASE_BRANCH],
                  ["config", "user.email", "t@t"], ["config", "user.name", "t"],
                  ["commit", "-q", "--allow-empty", "-m", "base"]):
            sp.run(["git", *c], cwd=self.wt, check=True, capture_output=True)
        base = self._drv()

        class Writes:
            harness, model, images = base.harness, base.model, None

            async def run(self, prompt, cwd, **kw):
                Path(cwd, "pkg").mkdir(exist_ok=True)
                Path(cwd, "pkg", "b.py").write_text("x = 1\n")
                return await base.run(prompt, cwd, **kw)
        with mock.patch.object(code_tasks, "_driver", lambda m, r, p: Writes()), \
                mock.patch.object(code_tasks.gitstore, "checkpoint",
                                  mock.AsyncMock(return_value=None)), \
                capture_events():
            self._implement(self._graph())
        live = self.ab.claims(self.project)
        self.assertEqual(len(live), 1, "renewed in place, not stacked")
        self.assertEqual(sorted(live[0]["paths"]), ["pkg/a.py", "pkg/b.py"])
        d = self.ab.digest_for(self.project, task="sib", role="implementer",
                               model="m", files_hint=["pkg/b.py"])
        self.assertIn("bw1/implementer holds", d)

    def test_implement_posts_its_status(self):
        with mock.patch.object(code_tasks, "_driver", lambda m, r, p: self._drv()), \
                capture_events():
            self._implement(self._graph())
        st = [m for m in self._msgs(channel="task:bw1") if m["kind"] == "status"]
        self.assertTrue(st and "implementing" in st[0]["body"])

    def test_the_etiquette_checklist_is_short_and_has_examples(self):
        """Every prompt carrying it pays for it out of the task's own
        attention, so it stays a checklist with concrete JSON examples."""
        e = code_tasks.BOARD_ETIQUETTE
        self.assertLess(len(e), 900, "the checklist must not crowd out the task")
        for kind in ("claim", "question", "answer", "result", "blocker"):
            self.assertIn(f'"kind":"{kind}"', e)
        # The interface question names a task and mentions it; the answer
        # points at a file:line rather than describing prose.
        self.assertIn("mentions", e)
        self.assertIn(".py:", e)
        self.assertIn("refs", e)

    def test_all_three_agent_roles_are_told_the_etiquette(self):
        t = code_tasks.load_taskfile(taskfile([{
            "id": "bw1", "title": "T", "prompt": "do it", "verify_cmd": "true",
            "model": "GLM-5.3", "reviewer": "deepseek",
            "files_hint": ["pkg/a.py"]}], repo=self.repo))["tasks"]["bw1"]
        for prompt in (code_tasks._impl_prompt(t, None, board="BOARD"),
                       code_tasks._review_prompt(t, "DIFF", board="BOARD"),
                       code_tasks._pr_review_prompt(t, "DIFF", 1, 1, [],
                                                     board="BOARD")):
            self.assertIn("BOARD", prompt)
            self.assertIn(code_tasks.BOARD_ETIQUETTE, prompt)

    def test_the_implementer_prompt_is_what_ingest_compares_against(self):
        """A body that is a bare copy of the prompt it was given is refused,
        so the node must RECORD the prompt before the run — otherwise the
        check silently never fires."""
        with mock.patch.object(code_tasks, "_driver", lambda m, r, p: self._drv()), \
                capture_events():
            self._implement(self._graph())
        [prompt] = self.prompts
        body = " ".join(prompt.split())
        line = {"kind": "status", "body": body}
        with mock.patch.object(code_tasks, "_driver",
                               lambda m, r, p: self._drv(line=line)), \
                capture_events():
            self._implement(self._graph(), {"implement_bw1": 1})
        errs = [m for m in self._msgs(kinds=["error"])]
        self.assertTrue(errs, "the echoed prompt must come back as an error post")
        self.assertIn("bare copy of your prompt", errs[0]["body"])

    def test_ingest_happens_after_a_crash(self):
        line = {"kind": "note", "body": "half-way: the parser is done"}
        with mock.patch.object(code_tasks, "_driver",
                               lambda m, r, p: self._drv(crash="boom", line=line)), \
                capture_events():
            out = self._implement(self._graph())
        self.assertTrue(out["crashed"])
        self.assertIn("half-way: the parser is done",
                      [m["body"] for m in self._msgs()])

    def test_a_broadcast_reaches_the_next_prompt_once(self):
        with mock.patch.object(code_tasks, "_driver", lambda m, r, p: self._drv()), \
                capture_events():
            g = self._graph()
            self._implement(g)
            self.ab.post(self.project, author="operator", channel="project",
                         kind="ping", body="BROADCAST: base moved, rebase")
            self._implement(g, {"implement_bw1": 1})
            self._implement(g, {"implement_bw1": 2})
        self.assertNotIn("BROADCAST", self.prompts[0])
        self.assertIn("BROADCAST", self.prompts[1])
        self.assertNotIn("BROADCAST", self.prompts[2], "delivered once, as unread")

    def test_a_reviewer_rejection_becomes_an_addressed_question(self):
        async def diff(*a, **k):
            return "diff --git a/x b/x"

        async def blast(*a, **k):
            return ""
        rej = json.dumps({"pass": False, "issues": ["the null check is missing"]})
        drv = self._drv(text=rej)
        drv.model = "DeepSeek-V4.1-Flash-thinking-max"
        with mock.patch.object(code_tasks, "_reviewer_driver", lambda *a: drv), \
                mock.patch.object(code_tasks, "_driver", lambda *a: drv), \
                mock.patch.object(code_tasks, "_select_reviewer",
                                  lambda tok, *a: (config.REVIEW_FAMILIES.get(tok, tok), "planned")), \
                mock.patch.object(code_tasks.gitstore, "diff_full", diff), \
                mock.patch.object(code_tasks.graft, "blast", blast), capture_events():
            out = asyncio.run(self._graph().nodes["review_bw1"].fn(
                {"results": {"alloc_bw1": {"worktree": self.wt}}, "runs": {}}))
        self.assertFalse(out["pass"])
        qs = [m for m in self._msgs() if m["kind"] == "question"]
        self.assertEqual(len(qs), 1)
        self.assertIn("bw1/implementer", qs[0]["mentions"])
        self.assertIn("the null check is missing", qs[0]["body"])
        self.assertEqual(qs[0]["state"], "open")
        digest = self.ab.digest_for(self.project, task="bw1", role="implementer",
                                    model="GLM-5.3")
        self.assertIn("the null check is missing", digest)

    def test_the_merge_result_carries_its_files(self):
        async def pr_state(repo, n):
            return {"state": "OPEN", "mergeable": "MERGEABLE"}

        async def ok(*a, **k):
            return True, ""

        async def nothing(*a, **k):
            return None

        async def changed(wt, base):
            return ["pkg/a.py", "tests/test_a.py"]
        url = "https://github.com/o/r/pull/7"
        self.ab.claim(self.project, task="bw1", author="bw1/implementer",
                      paths=["pkg/a.py"])
        with mock.patch.object(code_tasks.gitstore, "pr_state", pr_state), \
                mock.patch.object(code_tasks.gitstore, "merge_pr", ok), \
                mock.patch.object(code_tasks.gitstore, "fast_forward_base", ok), \
                mock.patch.object(code_tasks.gitstore, "cleanup", nothing), \
                mock.patch.object(code_tasks, "_changed_files", changed), \
                capture_events():
            out = asyncio.run(self._graph().nodes["pr_merge_bw1"].fn(
                {"results": {"alloc_bw1": {"worktree": self.wt},
                             "publish_bw1": {"published": True, "pr": 7, "url": url},
                             "pr_review_bw1": {"approved": True, "pr": 7}},
                 "runs": {}}))
        self.assertTrue(out["merged"])
        res = [m for m in self._msgs() if m["kind"] == "result"]
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["refs"]["files"], ["pkg/a.py", "tests/test_a.py"])
        self.assertEqual(res[0]["refs"]["pr"], url)
        self.assertEqual(self.ab.claims(self.project), [], "merge releases the claim")

    def _cancelling_drv(self, role):
        line = {"kind": "note", "body": f"{role} was half-way"}
        drv = self._drv(line=line)
        drv.model = "DeepSeek-V4.1-Flash-thinking-max"
        inner = drv.run

        async def run(prompt, cwd, **kw):
            await inner(prompt, cwd, **kw)
            raise asyncio.CancelledError()
        drv.run = run
        return drv

    def _assert_cancel_cleanup(self, role):
        self.assertIn(f"{role} was half-way", [m["body"] for m in self._msgs()])
        self.assertEqual(self.ab.claims(self.project), [],
                         "a cancelled run releases the implementer's claim")

    def test_a_cancelled_reviewer_ingests_and_releases(self):
        async def diff(*a, **k):
            return "diff --git a/x b/x"

        async def blast(*a, **k):
            return ""
        self.ab.claim(self.project, task="bw1", author="bw1/implementer",
                      paths=["pkg/a.py"])
        drv = self._cancelling_drv("reviewer")
        with mock.patch.object(code_tasks, "_reviewer_driver", lambda *a: drv), \
                mock.patch.object(code_tasks, "_driver", lambda *a: drv), \
                mock.patch.object(code_tasks, "_select_reviewer",
                                  lambda tok, *a: (config.REVIEW_FAMILIES.get(tok, tok), "planned")), \
                mock.patch.object(code_tasks.gitstore, "diff_full", diff), \
                mock.patch.object(code_tasks.graft, "blast", blast), capture_events():
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(self._graph().nodes["review_bw1"].fn(
                    {"results": {"alloc_bw1": {"worktree": self.wt}}, "runs": {}}))
        self._assert_cancel_cleanup("reviewer")

    def test_a_cancelled_pr_reviewer_ingests_and_releases(self):
        async def blast(*a, **k):
            return ""
        self.ab.claim(self.project, task="bw1", author="bw1/implementer",
                      paths=["pkg/a.py"])
        drv = self._cancelling_drv("pr-reviewer")
        item = {"model": drv.model, "pr": 7, "round": 1, "diff": "d",
                "n_reviewers": 1, "prior_issues": []}
        with mock.patch.object(code_tasks, "_driver", lambda *a: drv), \
                mock.patch.object(code_tasks.graft, "blast", blast), capture_events():
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(self._graph().nodes["pr_reviewer_bw1"].fn(
                    {"results": {"alloc_bw1": {"worktree": self.wt}},
                     "runs": {}, "spawn": item}))
        self._assert_cancel_cleanup("pr-reviewer")

    def test_an_empty_diff_merge_still_posts_its_result(self):
        self.ab.claim(self.project, task="bw1", author="bw1/implementer",
                      paths=["pkg/a.py"])
        url = "https://github.com/o/r/pull/9"
        with capture_events():
            g = self._graph()
            out = asyncio.run(g.nodes["pr_merge_bw1"].fn(
                {"results": {"publish_bw1": {"published": False, "merged": True,
                                             "empty": True, "pr": 9, "url": url}},
                 "runs": {}}))
            asyncio.run(g.nodes["pr_merge_bw1"].fn(
                {"results": {"publish_bw1": {"published": False, "merged": True,
                                             "empty": True}}, "runs": {}}))
        self.assertTrue(out["merged"])
        res = [m for m in self._msgs() if m["kind"] == "result"]
        self.assertEqual([(m["refs"]["files"], m["refs"]["pr"]) for m in res],
                         [([], url), ([], "")])
        self.assertEqual(self.ab.claims(self.project), [])

    def _slow_gate_graph(self, cmd):
        self.ts = code_tasks.load_taskfile(taskfile([{
            "id": "bw1", "title": "T", "prompt": "do it", "verify_cmd": cmd,
            "model": "GLM-5.3", "reviewer": "deepseek",
            "files_hint": ["pkg/a.py"]}], repo=self.repo))
        self.ab.claim(self.project, task="bw1", author="bw1/implementer",
                      paths=["pkg/a.py"])
        return self._graph()

    GATE_CTX = property(lambda self: {
        "results": {"alloc_bw1": {"worktree": self.wt},
                    "implement_bw1": {"model": "GLM-5.3", "harness": "opencode"}},
        "runs": {"implement_bw1": 1}})

    def test_a_gate_timeout_posts_its_status(self):
        with mock.patch.object(config, "GATE_TIMEOUT", 0.3), capture_events():
            out = asyncio.run(self._slow_gate_graph("sleep 5")
                              .nodes["gate_bw1"].fn(self.GATE_CTX))
        self.assertFalse(out["passed"])
        st = [m["body"] for m in self._msgs(channel="task:bw1")
              if m["kind"] == "status"]
        self.assertTrue(any("gate timed out" in b for b in st), st)

    def test_cancelling_the_graph_during_a_gate_releases_the_claim(self):
        g = self._slow_gate_graph("sleep 5")

        async def go():
            job = asyncio.ensure_future(g.nodes["gate_bw1"].fn(self.GATE_CTX))
            await asyncio.sleep(0.3)
            job.cancel()
            await job
        with capture_events():
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(go())
        self.assertEqual(self.ab.claims(self.project), [])
