"""gh_ops helpers: repo targeting, verdict-JSON extraction, cross-review
pairing, taskfile writing (schema-correct per code_tasks.load_taskfile),
the gh auth preflight, and the PR-diff truncation bound."""
import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from helpers import capture_events, needs_kimi, needs_deepseek_v4, needs_three_families, ENTRY, STRONGEST  # noqa: F401  (helpers sets the test env)

import code_tasks
import config
import gh_ops


def _rows():
    return [
        {"number": 7, "title": "Fix crash on empty input", "kind": "bug",
         "size": "S", "tier": "basic", "model": config.ESCALATION_PATH[0],
         "actionable": True, "summary": "guard against empty input"},
        {"number": 9, "title": "Add export endpoint", "kind": "feature",
         "size": "M", "tier": "hard", "model": "not-a-model",
         "actionable": True, "summary": "new endpoint"},  # bogus model -> fallback
        {"number": 11, "title": "How do I configure X?", "kind": "question",
         "size": "S", "tier": "basic", "model": config.ESCALATION_PATH[0],
         "actionable": False, "summary": "just a question"},
    ]


class RepoTarget(unittest.TestCase):
    def test_directory_targets_cwd_with_no_repo_flag(self):
        with tempfile.TemporaryDirectory() as d:
            rargs, cwd = gh_ops._repo_target(d)
        self.assertEqual(rargs, [])
        self.assertEqual(cwd, str(Path(d).resolve()))

    def test_owner_name_uses_repo_flag(self):
        rargs, cwd = gh_ops._repo_target("octocat/hello-world")
        self.assertEqual(rargs, ["--repo", "octocat/hello-world"])
        self.assertIsNone(cwd)


class ReviewerFor(unittest.TestCase):
    @needs_three_families
    def test_hard_models_cross_review_each_other(self):
        """A top-tier model draws the strongest review family that is not its own.

        This named the pairs literally ("GLM is reviewed by kimi") until the
        roster reordered on 2026-09-12 and deepseek became the strongest review
        family -- the code was right and the test was asserting last month's
        roster. REVIEW_FAMILIES is ordered strongest-first, so the rule states
        itself.
        """
        strongest_first = list(config.REVIEW_FAMILIES)
        for model in config.IMPLEMENT_TIERS.get("hard", []):
            fam = config.MODEL_FAMILY[model]
            expected = next(f for f in strongest_first if f != fam)
            self.assertEqual(gh_ops._reviewer_for(model, 0), expected,
                             f"{model} should be reviewed by {expected}")

    def test_other_models_alternate_and_never_self(self):
        # the models below the "hard" tier, from today's roster
        for model in config.IMPLEMENT_TIERS.get("medium", []):
            for i in range(4):
                rev = gh_ops._reviewer_for(model, i)
                self.assertIn(rev, tuple(config.REVIEW_FAMILIES))
                self.assertNotEqual(rev, config.MODEL_FAMILY[model])


class JsonFrom(unittest.TestCase):
    def test_finds_last_parseable_dict_with_key(self):
        text = ('noise {"issues": [1]} more prose\n'
                '{"issues": [{"number": 3}], "extra": true}')
        obj = gh_ops._json_from(text, "issues")
        self.assertEqual(obj["issues"], [{"number": 3}])

    def test_skips_unbalanced_and_keyless_json(self):
        text = '{"title": "x", broken { also {"other": 1}'
        self.assertIsNone(gh_ops._json_from(text, "title"))

    def test_returns_none_without_the_key(self):
        self.assertIsNone(gh_ops._json_from('{"a": 1}', "issues"))


class WriteTaskfile(unittest.TestCase):
    def _write(self, repo, rows=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        orig = config.TASKS_DIR
        config.TASKS_DIR = tmp.name
        self.addCleanup(setattr, config, "TASKS_DIR", orig)
        with contextlib.redirect_stdout(io.StringIO()):
            gh_ops._write_taskfile(repo, rows if rows is not None else _rows())
        files = list(Path(tmp.name).glob("*.json"))
        self.assertEqual(len(files), 1, "expected exactly one taskfile")
        return files[0]

    def test_written_taskfile_passes_the_loader_with_cross_review(self):
        with tempfile.TemporaryDirectory() as repo_dir:
            path = self._write(repo_dir)
            doc = json.loads(path.read_text())
            # project.repo must be the real local path, not a CWD-relative slug.
            self.assertEqual(doc["project"]["repo"],
                             str(Path(repo_dir).resolve()))
            loaded = code_tasks.load_taskfile(path)  # raises on any violation
        self.assertEqual(len(loaded["tasks"]), 2)  # question dropped
        for tid, t in loaded["tasks"].items():
            self.assertRegex(tid, r"[a-z0-9][a-z0-9-]{0,60}")
            self.assertIn(t["model"], config.IMPLEMENTER_MODELS)
            self.assertNotEqual(config.MODEL_FAMILY[t["model"]],
                                config.MODEL_FAMILY.get(t["reviewer"],
                                                        t["reviewer"]),
                                f"task {tid}: reviewer shares the "
                                "implementer's harness")

    def test_bogus_model_falls_back_to_a_tier_model(self):
        path = self._write("/tmp")
        doc = json.loads(path.read_text())
        task9 = next(t for t in doc["project"]["tasks"]
                     if t["id"] == "issue-9")
        # 'hard' tier fallback: last of IMPLEMENT_TIERS['hard'].
        self.assertEqual(task9["model"], config.IMPLEMENT_TIERS["hard"][-1])

    def test_unresolvable_owner_name_writes_a_marked_placeholder(self):
        with tempfile.TemporaryDirectory() as home:
            orig_home = Path.home
            Path.home = classmethod(lambda cls: Path(home))
            self.addCleanup(setattr, Path, "home", orig_home)
            path = self._write("owner/no-such-repo-xyz")
            doc = json.loads(path.read_text())
            self.assertTrue(doc["project"]["repo"].startswith("EDIT-ME"),
                            "placeholder must be clearly marked for editing")
        # ...and the placeholder still satisfies the loader's shape.
        code_tasks.load_taskfile(path)

    def test_existing_blessed_clone_is_used(self):
        with tempfile.TemporaryDirectory() as home:
            clone = Path(home) / "repos" / "myrepo"
            clone.mkdir(parents=True)
            orig_home = Path.home
            Path.home = classmethod(lambda cls: Path(home))
            self.addCleanup(setattr, Path, "home", orig_home)
            path = self._write("owner/myrepo")
            doc = json.loads(path.read_text())
            self.assertEqual(doc["project"]["repo"], str(clone))

    def test_no_actionable_issues_writes_nothing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        orig = config.TASKS_DIR
        config.TASKS_DIR = tmp.name
        self.addCleanup(setattr, config, "TASKS_DIR", orig)
        with contextlib.redirect_stdout(io.StringIO()):
            gh_ops._write_taskfile("/tmp", [{"number": 1, "kind": "question",
                                             "actionable": False}])
        self.assertEqual(list(Path(tmp.name).glob("*.json")), [])


class Preflight(unittest.TestCase):
    def test_unauthenticated_gh_exits_with_login_hint(self):
        async def denied(*a, **kw):
            raise RuntimeError("gh auth status exited 1: not logged in")

        async def go():
            await gh_ops._preflight()

        orig = gh_ops._gh
        gh_ops._gh = denied
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                with self.assertRaises(SystemExit) as ctx:
                    asyncio.run(go())
            self.assertEqual(ctx.exception.code, 1)
            self.assertIn("run: gh auth login", buf.getvalue())
        finally:
            gh_ops._gh = orig

    def test_missing_gh_binary_exits_with_install_hint(self):
        async def missing(*a, **kw):
            raise RuntimeError("gh CLI not found (install: x)")

        orig = gh_ops._gh
        gh_ops._gh = missing
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                with self.assertRaises(SystemExit):
                    asyncio.run(gh_ops._preflight())
            self.assertIn("not found", buf.getvalue())
        finally:
            gh_ops._gh = orig


class PrPrompt(unittest.TestCase):
    def test_diff_is_truncated_at_max_diff(self):
        diff = "x" * (gh_ops.MAX_DIFF + 5000)
        prompt = gh_ops._pr_prompt("owner/repo", 3, "{}", diff)
        self.assertIn(f"truncated to {gh_ops.MAX_DIFF} chars", prompt)
        self.assertNotIn("x" * (gh_ops.MAX_DIFF + 1), prompt)

    def test_short_diff_passes_through_whole(self):
        prompt = gh_ops._pr_prompt("owner/repo", 3, "{}", "small diff")
        self.assertIn("small diff", prompt)
        self.assertNotIn("truncated", prompt)


if __name__ == "__main__":
    unittest.main()
