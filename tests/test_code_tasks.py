"""Taskfile validation, reviewer-verdict parsing, and resume/escalation planning."""
import asyncio
import json
import pathlib
import tempfile
import unittest
from pathlib import Path

from helpers import FakeStore, capture_events

import code_tasks
import config


def taskfile(tasks, repo="/tmp", title="t", pattern=None):
    """Write a taskfile to a temp path and return it."""
    doc = {"project": {"repo": repo, "title": title, "tasks": tasks}}
    if pattern is not None:
        doc["project"]["pattern"] = pattern
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
