"""Scope-lock review verdicts: block only on issues THIS diff introduced.

A reviewer reads the full diff, so a pre-existing wart — a flaky test, a bug
the task never touched — used to block a merge and burn one of the
implementer's fix rounds on work it did not do. Every issue now carries a
label: `introduced-by-this-diff` blocks, `pre-existing` is collected as a
follow-up and never blocks.

These tests drive the REAL parser, the REAL graph nodes and the REAL gh_ops
caller, so the contract is pinned where it is used rather than restated here.
"""
import asyncio
import contextlib
import io
import json
import shutil
import tempfile
import types
import unittest
from pathlib import Path

from helpers import capture_events  # noqa: E402  (first: sets the test env)
from helpers import ENTRY, ENTRY_REVIEWER, FakeStore  # noqa: E402

import code_tasks  # noqa: E402
import config  # noqa: E402
import gh_ops  # noqa: E402


TASK = {"id": "t1", "title": "T1", "prompt": "do it", "verify_cmd": "true",
        "model": ENTRY, "reviewer": ENTRY_REVIEWER}

BLOCKING = "new.py:3 — off-by-one in the loop this diff adds"
PRE_EXISTING = "old.py:9 — flaky test that predates this task"
FOLLOW_UP = "old.py:11 — unrelated naming wart"

NEW = json.dumps({"pass": False, "issues": [
    {"label": "introduced-by-this-diff", "text": BLOCKING},
    {"label": "pre-existing", "text": PRE_EXISTING}],
    "follow_ups": [FOLLOW_UP]})

OLD = '{"pass": false, "issues": ["plain text issue"]}'


class _FakeDriver:
    """A harness that answers with one fixed verdict."""

    def __init__(self, text):
        self.text, self.harness, self.model = text, "fake", "Fake-Model"

    async def run(self, prompt, cwd, task_id=None):
        return types.SimpleNamespace(text=self.text, exit_code=0,
                                     transcript_path="", seconds=0.0)


@contextlib.contextmanager
def _stubs(text):
    """Every driver answers `text`; git and graft never touch a real repo."""
    drv = _FakeDriver(text)

    async def diff_full(wt, base):
        return "DIFF"

    async def blast(wt, base=None, **kw):
        return ""

    orig = (code_tasks._driver, code_tasks.gitstore.diff_full, code_tasks.graft.blast)
    code_tasks._driver = lambda model, role, pol: drv
    code_tasks.gitstore.diff_full = diff_full
    code_tasks.graft.blast = blast
    try:
        yield drv
    finally:
        (code_tasks._driver, code_tasks.gitstore.diff_full,
         code_tasks.graft.blast) = orig


def _graph():
    fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"project": {"repo": "/tmp", "title": "t", "tasks": [TASK]}}, fh)
    fh.close()
    ts = code_tasks.load_taskfile(Path(fh.name))
    with capture_events():
        return code_tasks.build_code_graph(FakeStore(), ts, taskfile="tf.json")


class ReviewScope(unittest.TestCase):
    def setUp(self):
        self.wt = tempfile.mkdtemp(prefix="arc-review-scope-")
        self.addCleanup(shutil.rmtree, self.wt, ignore_errors=True)

    def _ctx(self):
        return {"results": {"alloc_t1": {"worktree": self.wt},
                            "implement_t1": {"harness": "x"}}, "runs": {}}

    # --- the parser ----------------------------------------------------
    def test_the_old_flat_format_still_parses_and_still_blocks(self):
        v = code_tasks._parse_verdict(OLD)
        self.assertFalse(v["pass"])
        self.assertEqual(v["issues"], ["plain text issue"],
                         "an unlabelled issue cannot be ignored")

    def test_a_labelled_verdict_separates_blocking_from_pre_existing(self):
        v = code_tasks._parse_verdict(NEW)
        self.assertFalse(v["pass"])
        self.assertEqual(v["issues"], [BLOCKING])
        follow = v.get("follow_ups") or []
        self.assertIn(PRE_EXISTING, follow)
        self.assertIn(FOLLOW_UP, follow)

    def test_rejecting_only_pre_existing_issues_is_not_a_block(self):
        """pass=false is decided by the LABELS, not by the flag typed."""
        v = code_tasks._parse_verdict(json.dumps({"pass": False, "issues": [
            {"label": "pre-existing", "text": PRE_EXISTING}]}))
        self.assertTrue(v["pass"])
        self.assertEqual(v["issues"], [])
        self.assertIn(PRE_EXISTING, v.get("follow_ups") or [])
        self.assertIsNone(v.get("truncated"))

    def test_a_named_introduced_issue_beats_a_passing_flag(self):
        v = code_tasks._parse_verdict(json.dumps({"pass": True, "issues": [
            {"label": "introduced-by-this-diff", "text": BLOCKING}]}))
        self.assertFalse(v["pass"])
        self.assertEqual(v["issues"], [BLOCKING])

    def test_an_unknown_label_blocks_rather_than_being_discarded(self):
        v = code_tasks._parse_verdict(json.dumps({"pass": False, "issues": [
            {"label": "nit", "text": "half-labelled"}]}))
        self.assertFalse(v["pass"])
        self.assertEqual(v["issues"], ["half-labelled"])

    def test_a_rejection_naming_no_issue_fails_closed(self):
        v = code_tasks._parse_verdict('{"pass": false}')
        self.assertFalse(v["pass"])
        self.assertTrue(v["issues"])

    # --- the prompts ---------------------------------------------------
    def test_both_prompts_demand_the_scope_lock(self):
        prompts = [code_tasks._review_prompt(TASK, "DIFF"),
                   code_tasks._pr_review_prompt(TASK, "DIFF", 1, 1, [])]
        for p in prompts:
            self.assertIn("introduced-by-this-diff", p)
            self.assertIn("pre-existing", p)
            self.assertIn("follow_ups", p)
            if config.REQUIRE_TESTS:
                self.assertIn("MUST come with tests", p)

    # --- the review node -----------------------------------------------
    def test_the_implementer_is_handed_only_the_blocking_issues(self):
        g = _graph()
        with _stubs(NEW):
            with capture_events() as ev:
                out = asyncio.run(g.nodes["review_t1"].fn(self._ctx()))
        self.assertFalse(out["pass"])
        self.assertEqual(out["issues"], [BLOCKING])
        fb = code_tasks._rework_feedback("t1", {"review_t1": out})
        self.assertIn(BLOCKING, fb)
        self.assertNotIn(PRE_EXISTING, fb)
        self.assertNotIn(FOLLOW_UP, fb)
        reviewed = [f for t, f in ev.seen if t == "task.reviewed"][0]
        self.assertEqual(reviewed["n_issues"], 1)
        self.assertIn(PRE_EXISTING, reviewed["follow_ups"])
        self.assertIn(FOLLOW_UP, reviewed["follow_ups"])
        self.assertEqual(reviewed["n_follow_ups"], 2)

    def test_a_pre_existing_only_rejection_does_not_stop_the_merge(self):
        g = _graph()
        with _stubs(json.dumps({"pass": False, "issues": [
                {"label": "pre-existing", "text": PRE_EXISTING}]})):
            out = asyncio.run(g.nodes["review_t1"].fn(self._ctx()))
        self.assertTrue(out["pass"], "the publish edge is gated on r['pass']")
        self.assertEqual(out["issues"], [])

    # --- the PR path ---------------------------------------------------
    def test_the_pr_reviewer_verdict_is_scope_locked(self):
        g = _graph()
        ctx = self._ctx()
        ctx["spawn"] = {"model": "Fake-Model", "diff": "DIFF", "n_reviewers": 1,
                        "round": 1, "prior_issues": []}
        with _stubs(json.dumps({"approve": False, "issues": [
                {"label": "introduced-by-this-diff", "text": BLOCKING},
                {"label": "pre-existing", "text": PRE_EXISTING}],
                "follow_ups": [FOLLOW_UP]})):
            out = asyncio.run(g.nodes["pr_reviewer_t1"].fn(ctx))
        self.assertFalse(out["approve"])
        self.assertEqual(out["issues"], [BLOCKING])
        self.assertIn(PRE_EXISTING, out["follow_ups"])
        self.assertIn(FOLLOW_UP, out["follow_ups"])

    def test_follow_ups_land_on_the_pr_reviewed_event(self):
        g = _graph()
        orig = code_tasks.gitstore._gh
        code_tasks.gitstore._gh = lambda *a, **k: asyncio.sleep(0, result=(0, "", ""))
        try:
            with capture_events() as ev:
                out = asyncio.run(g.nodes["pr_review_t1"].fn({"results": {
                    "pr_fanout_t1": {"pr": 4, "round": 1, "reviewers": ["A"]},
                    "pr_reviewer_t1": [{"model": "A", "approve": False,
                                        "issues": [BLOCKING],
                                        "follow_ups": [PRE_EXISTING]}]},
                    "runs": {}}))
        finally:
            code_tasks.gitstore._gh = orig
        self.assertEqual(out["issues"], [f"[A] {BLOCKING}"])
        self.assertEqual(out["follow_ups"], [f"[A] {PRE_EXISTING}"])
        reviewed = [f for t, f in ev.seen if t == "task.pr_reviewed"][0]
        self.assertEqual(reviewed["n_issues"], 1)
        self.assertEqual(reviewed["n_follow_ups"], 1)
        self.assertIn(PRE_EXISTING, reviewed["follow_ups"][0])
        fb = code_tasks._rework_feedback("t1", {"pr_review_t1": out})
        self.assertIn(BLOCKING, fb)
        self.assertNotIn(PRE_EXISTING, fb)

    # --- the other caller ----------------------------------------------
    def test_a_gh_ops_caller_still_reads_the_verdict(self):
        """gh_ops imports _parse_verdict; its contract must not break."""
        async def preflight():
            return None

        async def gh(args, cwd=None):
            return ""

        orig = (gh_ops._preflight, gh_ops._gh, gh_ops._driver)
        gh_ops._preflight, gh_ops._gh = preflight, gh
        gh_ops._driver = lambda model, role: _FakeDriver(OLD)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = asyncio.run(gh_ops.pr_review("/tmp", 1))
        finally:
            gh_ops._preflight, gh_ops._gh, gh_ops._driver = orig
        self.assertEqual(rc, 0)
        self.assertIn("plain text issue", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
