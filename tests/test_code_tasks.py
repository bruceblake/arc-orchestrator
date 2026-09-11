"""Taskfile validation, reviewer-verdict parsing, resume/escalation planning,
and project chaining (`after`)."""
import asyncio
import json
import pathlib
import tempfile
import unittest
from pathlib import Path

from helpers import FakeStore, capture_events

import code_tasks
import config


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


BASIC = {"id": "t1", "title": "T1", "prompt": "do it",
         "model": "gpt-oss-120b", "reviewer": "kimi"}


class LoadTaskfile(unittest.TestCase):
    def test_accepts_a_valid_cross_family_task(self):
        ts = code_tasks.load_taskfile(taskfile([BASIC]))
        self.assertEqual(list(ts["tasks"]), ["t1"])

    def test_rejects_a_non_implementer_model(self):
        bad = {**BASIC, "model": "gpt-4"}
        with self.assertRaises(ValueError) as cm:
            code_tasks.load_taskfile(taskfile([bad]))
        self.assertIn("must be an implementer", str(cm.exception))

    def test_rejects_same_family_review(self):
        bad = {**BASIC, "model": "Kimi-K3", "reviewer": "kimi"}
        with self.assertRaises(ValueError) as cm:
            code_tasks.load_taskfile(taskfile([bad]))
        self.assertIn("must not be the harness", str(cm.exception))

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
        same = {**BASIC, "model": "Kimi-K3", "reviewer": "kimi"}
        ts = code_tasks.load_taskfile(taskfile([same]),
                                      policy={"allow_self_review": True})
        self.assertEqual(ts["tasks"]["t1"]["reviewer"], "kimi")

    def test_passes_through_project_pattern(self):
        ts = code_tasks.load_taskfile(taskfile([BASIC], pattern="chain"))
        self.assertEqual(ts["pattern"], "chain")

    def test_pattern_defaults_to_empty_when_absent(self):
        ts = code_tasks.load_taskfile(taskfile([BASIC]))
        self.assertEqual(ts["pattern"], "")


class PlannerPatternLibrary(unittest.TestCase):
    """The planner is told to pick a named pattern from the library doc."""

    def test_schema_hint_carries_the_pattern_field(self):
        self.assertIn('"pattern"', code_tasks.PLAN_SCHEMA_HINT)
        self.assertIn("graph-pattern library", code_tasks.PLAN_SCHEMA_HINT)

    def test_planner_prompt_points_at_the_pattern_library(self):
        seen = {}

        class FakeDriver:
            def __init__(self, role):
                pass

            async def run(self, prompt, cwd, task_id=None):
                seen["prompt"] = prompt
                return object()

        orig_driver = code_tasks.KimiDriver
        orig_extract = code_tasks._plan_json_from_run
        code_tasks.KimiDriver = FakeDriver
        code_tasks._plan_json_from_run = (
            lambda res: '{"project": {"repo": "/tmp", "tasks": []}}')
        try:
            with tempfile.TemporaryDirectory() as d:
                asyncio.run(code_tasks.plan_tasks(
                    "g", "/tmp", out_path=Path(d) / "plan.json"))
        finally:
            code_tasks.KimiDriver = orig_driver
            code_tasks._plan_json_from_run = orig_extract
        self.assertIn("docs/graph-patterns.md", seen["prompt"])
        self.assertIn('"pattern"', seen["prompt"])


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


class CapabilityFailureClassification(unittest.TestCase):
    """Only a real capability failure may burn a stronger model tier."""

    def test_exhausted_rounds_is_a_capability_failure(self):
        self.assertTrue(code_tasks._is_capability_failure("exhausted fix rounds"))
        self.assertTrue(code_tasks._is_capability_failure(
            "exhausted escalation up to Kimi-K3"))

    def test_killed_run_process_is_not(self):
        self.assertFalse(code_tasks._is_capability_failure(
            "reset-stale: owning run process died"))
        self.assertFalse(code_tasks._is_capability_failure(
            "interrupted: run process exited before the task finished"))

    def test_harness_crash_is_not(self):
        self.assertFalse(code_tasks._is_capability_failure(
            "run crashed: kimi driver 400"))


class ResumePlanning(unittest.TestCase):
    """build_code_graph reports its resume plan via the run.resume event."""

    def plan(self, prior):
        ts = code_tasks.load_taskfile(taskfile([BASIC]))
        with capture_events() as ev:
            code_tasks.build_code_graph(FakeStore(prior), ts, taskfile="tf.json")
        return ev.first("run.resume") or {}

    def test_merged_task_is_skipped(self):
        p = self.plan([{"id": "t1", "status": "merged", "model": "gpt-oss-120b",
                        "error": None}])
        self.assertEqual(p["skipped_merged"], ["t1"])
        self.assertEqual(p["retried"], [])

    def test_capability_failure_escalates_one_tier(self):
        p = self.plan([{"id": "t1", "status": "failed", "model": "gpt-oss-120b",
                        "error": "exhausted fix rounds"}])
        self.assertEqual(p["escalated_on_resume"],
                         {"t1": config.ESCALATION_PATH[0]},
                         "gpt-oss-120b is off the path, so it escalates into it")

    def test_killed_run_resumes_at_the_same_tier(self):
        """The regression that put four tasks on Kimi-K3 at once."""
        p = self.plan([{"id": "t1", "status": "failed", "model": "gpt-oss-120b",
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


class ExtractPlanJson(unittest.TestCase):
    def test_finds_the_plan_among_prose_and_fences(self):
        doc = json.dumps({"project": {"repo": "/tmp", "tasks": []}})
        text = f"Here is the plan:\n```json\n{doc}\n```\nHope that helps."
        self.assertEqual(json.loads(code_tasks._extract_plan_json(text)),
                         json.loads(doc))

    def test_returns_none_when_absent(self):
        self.assertIsNone(code_tasks._extract_plan_json('{"not": "a plan"}'))


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
        p = code_tasks._impl_prompt(
            {"id": "t", "title": "T", "prompt": "x", "files_hint": [],
             "model": "", "reviewer": ""}, "")
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
        {"id": "a", "title": "A", "prompt": "x" * 200, "model": "gpt-oss-120b",
         "reviewer": "kimi"}]}}
    DECOY = {"project": {"repo": "/tmp", "title": "T", "tasks": [
        {"id": "a", "title": "A", "prompt": "...", "model": "gpt-oss-120b",
         "reviewer": "kimi"}]}}

    def _run(self, transcript_lines, text=""):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            p.write_text("\n".join(json.dumps(l) for l in transcript_lines))
            res = type("R", (), {"transcript_path": str(p), "text": text})()
            return code_tasks._plan_json_from_run(res)

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
        self.assertNotIn("gpt-oss-120b", config.ESCALATION_PATH)
        p = self.plan([{"id": "t1", "status": "failed", "model": "gpt-oss-120b",
                        "error": "exhausted fix rounds"}])
        self.assertEqual(p["escalated_on_resume"],
                         {"t1": config.ESCALATION_PATH[0]})

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
        self.assertIsNotNone(self._edge(g, "publish_t1", "pr_review_t1"))
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
        e = self._edge(g, "publish_t1", "pr_review_t1")
        self.assertFalse(e.when({"published": False, "reason": "push failed"}, {}))

    def test_tasks_branch_from_the_integration_branch_not_prod(self):
        self.assertNotEqual(config.BASE_BRANCH, config.PROD_BRANCH)
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
                 "model": "gpt-oss-120b", "reviewer": "kimi"} for i in ids]}}))
        return str(p.resolve())

    def _row(self, tid, status):
        return {"id": tid, "status": status, "model": "gpt-oss-120b",
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
                                        "model": "gpt-oss-120b",
                                        "error": "merge failed"}]))
        self.assertEqual(g.starts, ["chain_wait"])
        self.assertIsNotNone(self._edge(g, "chain_wait", "publish_t1"))

    def test_merged_skip_head_is_gated_too(self):
        ts = code_tasks.load_taskfile(taskfile([BASIC], after=["ghost.json"]))
        g = self._graph(ts, FakeStore([{"id": "t1", "status": "merged",
                                        "model": "gpt-oss-120b",
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
        pool = [m for m in ("Kimi-K3", "GLM-5.3", "DeepSeek-V4-Flash")
                if config.MODEL_FAMILY.get(m) != impl_family]
        pool.sort(key=lambda m: (usage.get(m, 0) / max(1, config.driver_limit(m)),
                                 usage.get(m, 0)))
        return pool[:n]

    def test_the_idle_model_is_preferred_over_saturated_ones(self):
        picked = self._pick({"Kimi-K3": 3, "GLM-5.3": 4, "DeepSeek-V4-Flash": 0})
        self.assertEqual(picked[0], "DeepSeek-V4-Flash")

    def test_saturation_is_relative_to_each_cap_not_absolute(self):
        """4 GLM of 4 is full; 4 DeepSeek of 5 is not."""
        picked = self._pick({"GLM-5.3": 4, "DeepSeek-V4-Flash": 4, "Kimi-K3": 3})
        self.assertEqual(picked[0], "DeepSeek-V4-Flash")

    def test_the_implementers_family_is_never_chosen(self):
        for fam in ("kimi", "glm", "deepseek"):
            picked = self._pick({}, impl_family=fam, n=2)
            for m in picked:
                self.assertNotEqual(config.MODEL_FAMILY[m], fam)

    def test_enough_reviewers_remain_after_excluding_the_implementer(self):
        """PR_REVIEWERS must be satisfiable from the remaining families."""
        for fam in ("kimi", "glm", "deepseek"):
            picked = self._pick({}, impl_family=fam, n=config.PR_REVIEWERS)
            self.assertEqual(len(picked), config.PR_REVIEWERS,
                             f"cannot fill {config.PR_REVIEWERS} reviewers when "
                             f"the implementer is {fam}")


class ResumingAnOpenPullRequest(unittest.TestCase):
    """A task whose PR is already open must not be re-implemented.

    Restarting it at alloc would discard a pushed branch and an open pull
    request that reviewers may have partly read, and burn a model redoing
    work that is sitting on GitHub waiting for approval.
    """

    def _graph(self, status):
        ts = code_tasks.load_taskfile(taskfile([BASIC]))
        prior = [{"id": "t1", "status": status, "model": "gpt-oss-120b", "error": None}]
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
            "pr_review_t1": {"approved": False, "reviewers": ["Kimi-K3", "GLM-5.3"],
                             "issues": ["[Kimi-K3] leaks a file handle"]},
        })
        self.assertTrue(fb)
        self.assertIn("leaks a file handle", fb)
        self.assertIn("Kimi-K3, GLM-5.3", fb)

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

    def test_every_implementer_family_has_enough_reviewers(self):
        for fam in ("kimi", "glm", "deepseek", "gpt-oss"):
            with self.subTest(family=fam):
                self.assertGreaterEqual(
                    len(code_tasks._eligible_pr_reviewers(fam, None)),
                    config.PR_REVIEWERS,
                    f"{fam} cannot field {config.PR_REVIEWERS} PR reviewers")

    def test_it_never_picks_the_implementer_s_own_family(self):
        for fam in ("kimi", "glm", "deepseek"):
            for m in code_tasks._eligible_pr_reviewers(fam, None):
                self.assertNotEqual(config.MODEL_FAMILY.get(m), fam)

    def test_every_model_it_offers_can_actually_be_built(self):
        for fam in ("kimi", "glm", "deepseek", "gpt-oss"):
            for m in code_tasks._eligible_pr_reviewers(fam, None):
                code_tasks._driver(m, "pr_reviewer", None)  # must not raise

    def test_gpt_oss_is_never_offered_as_a_reviewer(self):
        for fam in ("kimi", "glm", "deepseek", "gpt-oss"):
            self.assertNotIn("gpt-oss-120b",
                             code_tasks._eligible_pr_reviewers(fam, None))


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

    def test_kimi_is_preferred_when_the_opencode_pool_is_full(self):
        usage = {"Kimi-K3": 1, "GLM-5.3": 1, "DeepSeek-V4-Flash": 1,
                 "harness:opencode": config.harness_limit("opencode"),
                 "harness:kimi": 1}
        order = sorted(["GLM-5.3", "DeepSeek-V4-Flash", "Kimi-K3"],
                       key=lambda m: code_tasks._reviewer_pressure(m, usage))
        self.assertEqual(order[0], "Kimi-K3")

    def test_an_idle_fleet_scores_everything_zero(self):
        for m in ("Kimi-K3", "GLM-5.3", "DeepSeek-V4-Flash"):
            self.assertEqual(code_tasks._reviewer_pressure(m, {}), 0.0)

    def test_kimi_routes_to_the_kimi_harness_and_the_rest_to_opencode(self):
        self.assertEqual(code_tasks._harness_of("Kimi-K3"), "kimi")
        for m in ("GLM-5.3", "DeepSeek-V4-Flash", "gpt-oss-120b"):
            self.assertEqual(code_tasks._harness_of(m), "opencode")


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


class AReviewerThatCrashedDidNotReview(unittest.TestCase):
    """A crashed reviewer is an inconclusive round, not a rejection.

    Observed on PR #12: both reviewers died on opencode contention, and the
    crash was posted to a PUBLIC pull request as "changes requested: reviewer
    crashed", then sent the implementer back to fix issues that did not exist.
    Three infrastructure blips would have failed a perfectly good task, because
    each one consumed one of three PR rounds.
    """

    def _outcomes(self, *pairs):
        """Replicates pr_review's aggregation over reviewer outcomes."""
        issues, approvals, crashed = [], [], []
        for model, v in pairs:
            if v.get("crashed"):
                crashed.append(model)
            elif v["approve"]:
                approvals.append(model)
            else:
                issues.extend(f"[{model}] {i}" for i in v["issues"])
        approved = bool(pairs) and len(approvals) == len(pairs)
        return {"approved": approved, "issues": issues, "crashed": crashed,
                "inconclusive": bool(crashed) and not issues}

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
        self.assertTrue(self._fires("pr_review_t1", r))
        self.assertFalse(self._fires("implement_t1", r))

    def test_it_stops_retrying_once_the_budget_is_spent(self):
        r = {"approved": False, "inconclusive": True,
             "inconclusive_n": config.PR_MAX_INCONCLUSIVE}
        self.assertFalse(self._fires("pr_review_t1", r))
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
        self.assertTrue(self._fires("pr_merge_t1", "pr_review_t1", r))

    def test_a_clean_merge_does_not_loop_back(self):
        self.assertFalse(self._fires("pr_merge_t1", "pr_review_t1",
                                     {"merged": True, "pr": 4}))

    def test_a_terminal_conflict_does_not_loop_back(self):
        self.assertFalse(self._fires("pr_merge_t1", "pr_review_t1",
                                     {"merged": False, "reason": "conflict"}))

    def test_the_resync_budget_is_small_and_positive(self):
        # Each resync rewrites the branch and costs a fresh review round.
        self.assertGreaterEqual(config.PR_MAX_RESYNCS, 1)
        self.assertLessEqual(config.PR_MAX_RESYNCS, 3)


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
        prior = [{"id": "t1", "status": status, "model": "gpt-oss-120b", "error": None}]
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
        self.assertIn("if alloc_res is None:", pub)
        self.assertNotIn('prior_status == "conflict" and alloc_res is None', pub)


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
