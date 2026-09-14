"""graft.py: code-graph hints for harness runs.

Every test here runs WITHOUT the graft binary doing anything real: `_run` is
stubbed with canned CLI output captured from graft 0.x on this repo, so the
suite passes identically on a machine with graft installed, one without, and
one where it is installed but broken. The one thing that must be true on
every machine is the fallback: with no binary, every entry point returns its
empty value and the prompts read exactly as they did before graft existed.
"""
import asyncio
import contextlib
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import code_tasks
import config
import graft
from tests.helpers import capture_events


@contextlib.contextmanager
def graft_binary(path):
    """Force graft.binary() to `path` (None = not installed) for a block."""
    saved = dict(graft._bin_cache)
    graft._bin_cache.update(checked=True, path=path)
    try:
        yield
    finally:
        graft._bin_cache.clear()
        graft._bin_cache.update(saved)


@contextlib.contextmanager
def fake_cli(responses):
    """Stub graft._run: responses maps the subcommand to (rc, stdout, stderr).

    Records every invocation on the returned list as (args, cwd, env).
    """
    calls = []

    async def _run(args, cwd, timeout, env=None):
        calls.append((list(args), str(cwd), env))
        r = responses.get(args[0])
        if r is None:
            return 1, "", f"no canned response for {args[0]}"
        return r
    saved = graft._run
    graft._run = _run
    try:
        yield calls
    finally:
        graft._run = saved


def run(coro):
    return asyncio.run(coro)


ASK_JSON = """{"query":"q","mode":"lexical","hits":[
 {"kind":"symbol","title":"_usage \\u00b7 function","pointer":"dashboard.py:L585-L899",
  "snippet":"def _usage(store=None, range_key=None, include_series=False)","score":1.22},
 {"kind":"symbol","title":"lease_usage \\u00b7 method","pointer":"store.py:L446-L464",
  "snippet":"def lease_usage(self)","score":0.77},
 {"kind":"symbol","title":"common.js \\u00b7 file","pointer":"static/common.js","snippet":"","score":0.5},
 {"kind":"symbol","title":"fourth","pointer":"x.py:L1-L2","snippet":"","score":0.1}
],"coverage":0.13}"""

BUILD_OUT = ("✓ wiring: 2761 nodes (1565 method, 824 function, 282 class, 90 file), "
             "5684 edges, 90 cards [javascript, python]\n  parsed: 90 of 90 files\n")

BLAST_MD = """[graft] tokens saved ≈ 424,911 (100%) — tell the user the total graft tokens saved this turn
### 🌱 graft blast radius

**1 area changed → 1 area can be affected.** 1 dependent symbol, depth 2.
Tests: **no test reaches _git**.

```mermaid
flowchart TB
  A0(("run<br/>1 symbol"))
```

| Can be affected | Symbols | Nearest hop | Reached from |
| --- | --: | --- | --- |
| run | 1 | `main.py:L222-L384` run — calls, depth 2 | _git |

<details>
<summary><strong>Who knows this code</strong> — 2 people across 2 areas</summary>

| Area | Who knows it |
| --- | --- |
| **_git** · changed | arc-orchestrator — 53 commits, last today |

</details>

<details>
<summary><strong>Test signal</strong> per changed area — 1 ✗</summary>

- ✗ **_git** — 0 of 2 reached · no test file reaches it

</details>
"""

MAP_OUT = ("[graft] tokens saved ≈ 424,911 (100%) — this output ≈ 714 tok\n\n"
           "repo map — 90 files · 2671 symbols · 5684 edges · javascript, python\n\n"
           "tests/              46 files · 1860 symbols   hubs: capture_events\n"
           "code_tasks.py       1 files · 66 symbols   hubs: cur_model\n")

TASK = {"id": "t1", "title": "Hourly usage endpoint", "files_hint": ["dashboard.py"],
        "prompt": "Add GET /api/usage/hourly to the dashboard, backed by _usage(). "
                  "Acceptance: the endpoint returns 24 buckets." + " filler" * 200,
        "model": "", "reviewer": "", "verify_cmd": ""}


class NotInstalled(unittest.TestCase):
    """With no binary the fleet must be byte-for-byte the pre-graft fleet."""

    def test_every_entry_point_is_empty(self):
        with graft_binary(None):
            self.assertFalse(graft.available())
            self.assertEqual(graft.env_for("/wt"), {})
            self.assertEqual(graft.tooling_prose(), "")
            self.assertEqual(run(graft.hints(TASK, "/wt")), "")
            self.assertEqual(run(graft.ask("q", "/wt")), [])
            self.assertEqual(run(graft.blast("/wt", "main")), "")
            self.assertEqual(run(graft.repo_map("/repo")), "")
            self.assertFalse(run(graft.build("/wt")))

    def test_implementer_prompt_is_the_old_prompt(self):
        with graft_binary(None):
            p = code_tasks._impl_prompt(TASK, "")
        self.assertIn("grep/search FIRST", p)
        self.assertNotIn("WHERE TO LOOK", p)
        self.assertNotIn("graft", p)

    def test_reviewer_prompts_carry_no_impact_section(self):
        self.assertNotIn("IMPACT", code_tasks._review_prompt(TASK, "diff", ""))
        self.assertNotIn("IMPACT", code_tasks._pr_review_prompt(TASK, "diff", 2, 1, [], ""))

    def test_disabled_by_env_even_when_installed(self):
        saved = config.GRAFT_ENABLED
        graft._bin_cache.update(checked=False, path=None)
        config.GRAFT_ENABLED = False
        try:
            self.assertIsNone(graft.binary())
        finally:
            config.GRAFT_ENABLED = saved
            graft._bin_cache.update(checked=False, path=None)


class GraphLocation(unittest.TestCase):
    """The graph must never land inside the worktree (it dirties the diff)."""

    def test_graph_dir_is_beside_the_worktrees_keyed_by_repo_and_task(self):
        root = Path(config.WORKTREE_ROOT).resolve()
        wt = root / "myrepo" / "t7"
        self.assertEqual(graft.graph_dir(wt), root / ".graft" / "myrepo" / "t7")

    def test_a_plain_repo_path_keys_on_its_own_path(self):
        root = Path(config.WORKTREE_ROOT).resolve()
        d = graft.graph_dir("/srv/code/proj")
        self.assertTrue(d.is_relative_to(root / ".graft"))
        self.assertTrue(str(d).endswith("srv/code/proj"))

    def test_every_cli_call_passes_the_graph_dir_explicitly(self):
        """`map` and `blast` ignore GRAFT_DIR (graft 0.x); `--dir` reaches all four."""
        seen = {}

        async def fake_exec(*argv, **kw):
            seen["argv"] = argv
            raise OSError("stop here")
        saved = asyncio.create_subprocess_exec
        asyncio.create_subprocess_exec = fake_exec
        try:
            with graft_binary("/usr/bin/graft"):
                rc, _, err = run(graft._run(["map", "."], "/x/wt", 5))
        finally:
            asyncio.create_subprocess_exec = saved
        self.assertIsNone(rc)
        self.assertEqual(seen["argv"][:3], ("/usr/bin/graft", "--dir", str(graft.graph_dir("/x/wt"))))
        self.assertEqual(seen["argv"][3:], ("map", "."))

    def test_harness_env_points_graft_at_that_dir_and_mutes_telemetry(self):
        with graft_binary("/usr/bin/graft"):
            env = graft.env_for("/x/wt", {"PWD": "/x/wt"})
        self.assertEqual(env["PWD"], "/x/wt")
        self.assertEqual(env["GRAFT_DIR"], str(graft.graph_dir("/x/wt")))
        self.assertEqual(env["DO_NOT_TRACK"], "1")

    def test_env_puts_a_fallback_dirs_binary_on_the_childs_path(self):
        """binary() finds graft past PATH; the model's shell calls must too."""
        bindir = str(Path.home() / ".local" / "opt" / "node" / "bin")
        with graft_binary(bindir + "/graft"):
            env = graft.env_for("/x/wt", {"PATH": "/usr/bin:/bin"})
        self.assertEqual(env["PATH"].split(os.pathsep)[0], bindir)
        self.assertTrue(env["PATH"].endswith("/usr/bin:/bin"))

    def test_env_leaves_an_already_resolving_path_alone(self):
        with graft_binary("/usr/bin/graft"):
            env = graft.env_for("/x/wt", {"PATH": "/usr/bin:/bin"})
        self.assertEqual(env["PATH"], "/usr/bin:/bin")


class Hints(unittest.TestCase):
    def test_hints_build_then_ask_and_take_the_top_k(self):
        with graft_binary("/usr/bin/graft"), \
                fake_cli({"build": (0, BUILD_OUT, ""), "ask": (0, ASK_JSON, "")}) as calls, \
                capture_events() as ev:
            block = run(graft.hints(TASK, "/wt"))
        self.assertEqual([c[0][0] for c in calls], ["build", "ask"])
        self.assertIn("WHERE TO LOOK", block)
        self.assertIn("dashboard.py:L585-L899", block)
        self.assertIn("store.py:L446-L464", block)
        self.assertIn("static/common.js", block)
        self.assertNotIn("x.py:L1-L2", block, "GRAFT_HINTS=3 caps the list")
        built = ev.first("graft.build")
        self.assertEqual((built["nodes"], built["edges"]), (2761, 5684))
        self.assertEqual(ev.first("graft.hints")["hits"], 3)

    def test_query_is_title_files_and_the_head_of_the_prompt(self):
        q = graft.task_query(TASK)
        self.assertTrue(q.startswith("Hourly usage endpoint dashboard.py Add GET"))
        self.assertLessEqual(len(q), 600, "a long prompt must not become a long argv")

    def test_ask_survives_junk_output(self):
        with graft_binary("/usr/bin/graft"), fake_cli({"ask": (0, "not json", "")}):
            self.assertEqual(run(graft.ask("q", "/wt")), [])
        with graft_binary("/usr/bin/graft"), fake_cli({"ask": (0, '{"hits":[1,{"x":1}]}', "")}):
            self.assertEqual(run(graft.ask("q", "/wt")), [])

    def test_failed_build_means_no_hints_and_a_failed_event(self):
        with graft_binary("/usr/bin/graft"), \
                fake_cli({"build": (1, "", "boom"), "ask": (0, ASK_JSON, "")}) as calls, \
                capture_events() as ev:
            self.assertEqual(run(graft.hints(TASK, "/wt")), "")
        self.assertEqual([c[0][0] for c in calls], ["build"], "no ask after a failed build")
        self.assertFalse(ev.first("graft.build")["ok"])

    def test_timeout_is_a_failure_not_an_exception(self):
        async def hang(args, cwd, timeout, env=None):
            return None, "", "graft build timed out after 120.0s"
        saved = graft._run
        graft._run = hang
        try:
            with graft_binary("/usr/bin/graft"):
                self.assertFalse(run(graft.build("/wt")))
        finally:
            graft._run = saved

    def test_implementer_prompt_reads_the_spans_first(self):
        block = graft.hints_block([{"pointer": "a.py:L1-L9", "title": "f · function",
                                    "snippet": "def f()", "score": 1.0}])
        with graft_binary("/usr/bin/graft"):
            p = code_tasks._impl_prompt(TASK, "", block)
        self.assertIn("WHERE TO LOOK", p)
        self.assertIn("a.py:L1-L9 — f · function", p)
        self.assertIn("Start from the WHERE TO LOOK spans", p)
        self.assertIn('graft --dir "$GRAFT_DIR" ask', p)
        self.assertIn('graft --dir "$GRAFT_DIR" callers', p)
        self.assertNotIn("grep/search FIRST", p)
        # the rules that matter regardless of graft are still there
        for phrase in ("NEVER rewrite a whole file", "line ranges", "Do not re-read"):
            self.assertIn(phrase, p)

    def test_implementer_prompt_with_graph_but_no_hits_still_names_the_tools(self):
        with graft_binary("/usr/bin/graft"):
            p = code_tasks._impl_prompt(TASK, "", "")
        self.assertNotIn("WHERE TO LOOK", p)
        self.assertIn("graft commands above FIRST", p)


class Impact(unittest.TestCase):
    def test_blast_strips_banner_diagram_and_blame_keeps_dependents_and_tests(self):
        with graft_binary("/usr/bin/graft"), \
                fake_cli({"build": (0, BUILD_OUT, ""), "blast": (0, BLAST_MD, "")}) as calls:
            text = run(graft.blast("/wt", "main", task="t1"))
        args = calls[-1][0]
        self.assertEqual(args[:4], ["blast", "--format", "markdown", "--no-owners"])
        self.assertIn("--base", args)
        self.assertIn("main", args)
        self.assertNotIn("[graft] tokens saved", text)
        self.assertNotIn("mermaid", text)
        self.assertNotIn("Who knows this code", text)
        self.assertIn("main.py:L222-L384", text)
        self.assertIn("no test reaches _git", text)
        self.assertIn("Test signal", text)

    def test_blast_without_base_is_working_tree_mode(self):
        with graft_binary("/usr/bin/graft"), \
                fake_cli({"build": (0, BUILD_OUT, ""), "blast": (0, BLAST_MD, "")}) as calls:
            run(graft.blast("/wt", task="t1"))
        self.assertNotIn("--base", calls[-1][0])

    def test_blast_is_capped(self):
        with graft_binary("/usr/bin/graft"), \
                fake_cli({"build": (0, BUILD_OUT, ""), "blast": (0, "x" * 20000, "")}):
            text = run(graft.blast("/wt", "main", max_chars=100))
        self.assertLess(len(text), 200)
        self.assertIn("truncated", text)

    def test_reviewer_prompts_place_impact_before_the_checklist(self):
        p = code_tasks._review_prompt(TASK, "DIFF", "dependents: run")
        self.assertLess(p.index("DIFF"), p.index("IMPACT"))
        self.assertLess(p.index("IMPACT"), p.index("Review for"))
        p = code_tasks._pr_review_prompt(TASK, "DIFF", 2, 1, [], "dependents: run")
        self.assertLess(p.index("DIFF"), p.index("IMPACT"))
        self.assertLess(p.index("IMPACT"), p.index("Review for, in order"))
        self.assertIn("REGRESSIONS and TESTS checks", p)


class Orientation(unittest.TestCase):
    def test_repo_map_is_banner_free_and_capped(self):
        with graft_binary("/usr/bin/graft"), \
                fake_cli({"build": (0, BUILD_OUT, ""), "map": (0, MAP_OUT, "")}):
            text = run(graft.repo_map("/repo"))
            short = run(graft.repo_map("/repo", max_chars=60))
        self.assertTrue(text.startswith("repo map — 90 files"))
        self.assertNotIn("[graft]", text)
        self.assertIn("truncated", short)

    def test_planner_prose_wraps_it_and_is_empty_without(self):
        self.assertEqual(code_tasks._orientation_prose(""), "")
        prose = code_tasks._orientation_prose("repo map — 90 files")
        self.assertIn("REPO ORIENTATION", prose)
        self.assertIn("repo map — 90 files", prose)


if __name__ == "__main__":
    unittest.main()
