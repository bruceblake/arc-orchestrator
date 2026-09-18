"""Tests for dream_rsi.py — Dream-RSI replay over recorded history.

Hermetic: every test builds synthetic code_tasks/harness_runs rows (or an
in-memory store) and never touches a model, git, or the network. The point is
that replay is a deterministic, zero-execution evaluation of exploration
policies — the paper's "dreaming" — over the records the orchestrator already
writes.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from helpers import capture_events  # noqa: F401  (sys.path + redirects)

import dream_rsi as d
import events
import store as store_mod


def _clean_review():
    return '{"pass": true, "issues": []}'


def _reject(n=1):
    return json.dumps({"pass": False, "issues": ["x"] * n})


def _branchy_rows():
    """Three tasks, unequal branches and a rework — a real choice for a policy."""
    tasks = [
        {"id": "a", "status": "merged", "model": "DS"},
        {"id": "b", "status": "merged", "model": "GLM"},
        {"id": "c", "status": "merged", "model": "DS"},
    ]
    runs = [
        # a: two attempts, the second cleanly reviewed.
        {"task_id": "a-x1", "role": "implementer", "attempt": 1,
         "exit_code": 0, "seconds": 40, "verdict": None},
        {"task_id": "a-x1", "role": "reviewer", "attempt": 1,
         "exit_code": 0, "seconds": 30, "verdict": _reject()},
        {"task_id": "a-x2", "role": "implementer", "attempt": 2,
         "exit_code": 0, "seconds": 40, "verdict": None},
        {"task_id": "a-x2", "role": "reviewer", "attempt": 2,
         "exit_code": 0, "seconds": 20, "verdict": _clean_review()},
        # b: one clean attempt.
        {"task_id": "b-x1", "role": "implementer", "attempt": 1,
         "exit_code": 0, "seconds": 40, "verdict": None},
        {"task_id": "b-x1", "role": "reviewer", "attempt": 1,
         "exit_code": 0, "seconds": 20, "verdict": _clean_review()},
        # c: three attempts, all rejected — a wasteful branch.
        *[r for k in (1, 2, 3) for r in (
            {"task_id": f"c-x{k}", "role": "implementer", "attempt": k,
             "exit_code": 0, "seconds": 30, "verdict": None},
            {"task_id": f"c-x{k}", "role": "reviewer", "attempt": k,
             "exit_code": 0, "seconds": 10, "verdict": _reject(2)})],
    ]
    return tasks, runs


class BuildTree(unittest.TestCase):
    def test_attempts_of_one_task_form_a_chain_off_the_root(self):
        tasks, runs = _branchy_rows()
        t = d.build_tree("demo", tasks, runs)
        self.assertEqual(t.nodes["a-x1"].parent, "root")
        self.assertEqual(t.nodes["a-x2"].parent, "a-x1")   # a rework chains
        self.assertEqual(t.nodes["b-x1"].parent, "root")
        self.assertEqual(t.nodes["c-x3"].parent, "c-x2")

    def test_deps_place_a_branch_off_its_last_dependency(self):
        tasks, runs = _branchy_rows()
        t = d.build_tree("demo", tasks, runs, deps_by_task={"b": ["a"]})
        # b hangs off a's best attempt, not the root.
        self.assertEqual(t.nodes["b-x1"].parent, "a-x2")
        self.assertEqual(t.nodes["c-x1"].parent, "root")

    def test_implementer_and_reviewer_of_one_attempt_merge_into_one_node(self):
        """The paper's node is one generation–evaluation attempt: the artifact
        AND its evaluation. Two harness runs on one (task, attempt) must not
        collide on one node id (which produced a self-parent bug)."""
        tasks, runs = _branchy_rows()
        t = d.build_tree("demo", tasks, runs)
        self.assertIn("a-x2", t.nodes)
        self.assertNotEqual(t.nodes["a-x2"].parent, "a-x2")
        # the review verdict is folded into the attempt's score.
        self.assertGreater(t.nodes["a-x2"].score, t.nodes["a-x1"].score)

    def test_a_task_with_no_recorded_run_still_becomes_a_node(self):
        t = d.build_tree("demo", [{"id": "z", "status": "pending", "model": "DS"}], [])
        self.assertIn("z-x1", t.nodes)
        self.assertEqual(t.nodes["z-x1"].outcome, "unrun")

    def test_task_of_strips_only_the_attempt_suffix(self):
        self.assertEqual(d._task_of("task-a-x3"), "task-a")
        self.assertEqual(d._task_of("task-a"), "task-a")
        self.assertEqual(d._task_of("weird-x"), "weird-x")   # no digits


class AttemptScore(unittest.TestCase):
    def test_merge_beats_a_rejected_attempt(self):
        merged = d.attempt_score(0, "implementer", _clean_review(), "merged")
        rejected = d.attempt_score(0, "implementer", _reject(3), "merged")
        self.assertGreater(merged, rejected)

    def test_more_issues_cost_more(self):
        one = d.attempt_score(0, "implementer", _reject(1), "merged")
        five = d.attempt_score(0, "implementer", _reject(5), "merged")
        self.assertGreater(one, five)

    def test_escalation_is_penalised(self):
        base = d.attempt_score(0, "implementer", None, "merged")
        esc = d.attempt_score(0, "implementer", None, "merged", escalations=2)
        self.assertLess(esc, base)

    def test_failed_task_scores_below_merged(self):
        f = d.attempt_score(0, "implementer", None, "failed")
        m = d.attempt_score(0, "implementer", None, "merged")
        self.assertLess(f, m)


class Replay(unittest.TestCase):
    def test_replay_is_deterministic(self):
        trees = [d.build_tree("demo", *_branchy_rows())]
        p = d.GreedyBest()
        a = d.replay(trees[0], p, W=3, max_rounds=12)
        b = d.replay(trees[0], p, W=3, max_rounds=12)
        self.assertEqual(a, b)

    def test_replay_reads_only_the_revealed_prefix(self):
        """A policy cannot reveal a node that is not reachable from the root.

        Replay is prefix-observable: it only ever advances the frontier. A
        cheat policy that names the globally best-scoring node — even an
        unrevealed one — must not make it appear; every revealed node stays on
        a chain of recorded parents up to the root.
        """
        t = d.build_tree("demo", *_branchy_rows())
        best = max((n for n in t.nodes.values() if n.id != "root"),
                   key=lambda n: n.score).id

        class Cheat(d.ExplorationPolicy):
            name = "cheat"

            def choose(self, tree, eligible, W):
                # Try to summon the global best node, revealed or not.
                return [best] if best in eligible else list(eligible)[:W]

        r = d.replay(t, Cheat(), W=3, max_rounds=12)
        for nid in r.revealed:                    # only reachable nodes appear
            seen, cur = set(), nid
            while cur != "root" and cur not in seen:
                seen.add(cur)
                cur = t.nodes[cur].parent
            self.assertEqual(cur, "root", f"{nid} not reachable from root")

    def test_objective_matches_eq1_hand_computed(self):
        """Eq.1 with hand-computed constants on a fixed tree — not recomputed
        from the result's own fields (which would only re-check rounding)."""
        tasks = [{"id": "a", "status": "merged", "model": "DS"}]
        runs = [
            {"task_id": "a-x1", "role": "implementer", "attempt": 1,
             "exit_code": 0, "seconds": 10, "verdict": None},
            {"task_id": "a-x1", "role": "reviewer", "attempt": 1,
             "exit_code": 0, "seconds": 5, "verdict": _clean_review()},
        ]
        t = d.build_tree("demo", tasks, runs)
        r = d.replay(t, d.RecursiveFixed(), W=1, max_rounds=12)
        quality = t.nodes["a-x1"].score          # one node: quality == its score
        self.assertEqual(r.quality, round(quality, 4))
        self.assertEqual(r.n_attempts, 1)
        self.assertEqual(r.rounds, 1)
        expected = round(quality - d.beta1() * 1 + d.beta2() * (1 / 1), 4)
        self.assertAlmostEqual(r.score, expected, places=4)

    def test_a_wider_batch_cannot_increase_rounds(self):
        t = d.build_tree("demo", *_branchy_rows())
        serial = d.replay(t, d.RecursiveFixed(), W=1, max_rounds=12)
        wide = d.replay(t, d.RecursiveFixed(), W=3, max_rounds=12)
        self.assertLessEqual(wide.rounds, serial.rounds)

    def test_eligible_offers_only_real_moves(self):
        """A revealed leaf with no unrevealed child is a dead end, not a move —
        otherwise a policy spins on it until the round limit."""
        t = d.build_tree("demo", *_branchy_rows())
        t.reset()
        self.assertIn("root", t.eligible())          # root still has children
        r = d.replay(t, d.GreedyBest(), W=3, max_rounds=12)
        # Every revealed node is reachable and the run terminated well under
        # the round limit (no spinning).
        self.assertLess(r.rounds, 12)


class Selection(unittest.TestCase):
    def test_selection_is_the_argmax_and_includes_the_incumbent(self):
        trees = [d.build_tree("demo", *_branchy_rows())]
        res = d.improve(trees, W=3, max_rounds=12)
        self.assertIn("fixed", res.mean_by_policy)   # incumbent present
        best = max(res.mean_by_policy, key=lambda k: res.mean_by_policy[k])
        self.assertEqual(res.selected, best)

    def test_selected_is_never_worse_than_the_incumbent(self):
        """Paper §3: because the incumbent is a candidate, V* >= V^0 on the
        fixed history."""
        trees = [d.build_tree("demo", *_branchy_rows())]
        res = d.improve(trees, W=3, max_rounds=12)
        self.assertGreaterEqual(res.mean_by_policy[res.selected],
                                res.mean_by_policy["fixed"])

    def test_an_extra_candidate_can_win(self):
        """A candidate that dominates the built-ins is selected — selection
        really is by score, not a fixed table."""
        tree = d.build_tree("demo", *_branchy_rows())

        class AlwaysAll(d.ExplorationPolicy):
            name = "always-all"

            def choose(self, tree, eligible, W):
                return list(eligible)[:W]

        res = d.improve([tree], extra_policies=[AlwaysAll()], W=3, max_rounds=12)
        self.assertIn("always-all", res.mean_by_policy)
        best = max(res.mean_by_policy, key=lambda k: res.mean_by_policy[k])
        self.assertEqual(res.selected, best)


class PolicyAgent(unittest.TestCase):
    def test_agent_source_compiles_to_a_policy(self):
        src = ("class Policy(ExplorationPolicy):\n"
               "    name = 'agent'\n"
               "    def choose(self, tree, eligible, W):\n"
               "        return [n for n in eligible if n != 'root'][:W]\n")
        pol = d.compile_policy(src)
        self.assertIsNotNone(pol)
        self.assertEqual(pol.name, "agent")

    def test_bad_source_is_dropped_not_fatal(self):
        self.assertIsNone(d.compile_policy("def nope(): pass"))
        self.assertIsNone(d.compile_policy("class Policy: pass"))
        self.assertIsNone(d.compile_policy("syntax error !!!"))

    def test_agent_exception_is_swallowed(self):
        def boom(_ctx):
            raise RuntimeError("agent down")
        self.assertIsNone(d.propose_source(boom, {}))

    def test_agent_authored_policy_only_wins_by_score(self):
        """Agent source is never executed online except by scoring best — a
        deliberately bad agent policy does not get selected."""
        tree = d.build_tree("demo", *_branchy_rows())
        bad_src = ("class Policy(ExplorationPolicy):\n"
                   "    name = 'agent-bad'\n"
                   "    def choose(self, tree, eligible, W):\n"
                   "        return []\n")           # stops immediately
        pol = d.compile_policy(bad_src)
        res = d.improve([tree], extra_policies=[pol], W=3, max_rounds=12)
        self.assertNotEqual(res.selected, "agent-bad")


class LoadTrees(unittest.TestCase):
    def test_builds_one_tree_per_taskfile_from_a_store(self):
        db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db.close()
        self.addCleanup(lambda: os.unlink(db.name))
        s = store_mod.Store(db.name)
        s.upsert_code_task("/tmp/one.json", "a", "A", "DS", "glm", "merged")
        s.save_harness_run("a-x1", "reasonix", "DS", "implementer", 1, 0,
                           "/tmp/t.jsonl", 12.0, None)
        s.upsert_code_task("/tmp/two.json", "b", "B", "GLM", "deepseek", "merged")
        trees = d.load_trees(s)
        names = sorted(t.name for t in trees)
        self.assertIn("one.json", names)
        self.assertIn("two.json", names)
        one = next(t for t in trees if t.name == "one.json")
        self.assertIn("a-x1", one.nodes)


class ReportAndEvents(unittest.TestCase):
    def test_save_run_writes_a_report_and_emits_an_event(self):
        trees = [d.build_tree("demo", *_branchy_rows())]
        res = d.improve(trees, W=3, max_rounds=12)
        with capture_events() as ev:
            events.emit("dream.completed", selected=res.selected,
                        means=res.mean_by_policy)
        got = ev.of("dream.completed")
        self.assertTrue(got)
        self.assertEqual(got[0]["selected"], res.selected)
        out = d.save_run(res, path=os.path.join(tempfile.mkdtemp(), "r.jsonl"))
        self.assertTrue(Path(out).exists())
        row = json.loads(Path(out).read_text().splitlines()[0])
        self.assertEqual(row["selected"], res.selected)


class RegressionFixes(unittest.TestCase):
    """Bugs a fresh-context review found in the first cut, each with the
    failing case that provoked the fix. Recorded history is written by many
    code paths and agents, so the replay must never be taken down by a
    malformed row, a broken policy, or an off-nominal store order."""

    def test_attempt_score_survives_non_list_issues(self):
        # issues as an int, a dict or a bare string has no length here; the
        # first cut did len(issues) and raised TypeError.
        for bad in (5, {"a": 1}, "boom"):
            d.attempt_score(0, "reviewer", {"pass": False, "issues": bad},
                            "failed")
        # A string is NOT a list of issue strings: counted as zero issues.
        s = d.attempt_score(0, "reviewer", {"pass": False, "issues": "boom"}, "failed")
        self.assertGreater(s, d.attempt_score(0, "reviewer",
                                              _reject(5), "failed"))

    def test_verdict_accepts_the_pr_reviewer_approve_shape(self):
        # Pre-merge reviewer writes {"pass": ...}; PR reviewer writes
        # {"approve": ...} (code_tasks._parse_approval). Reading only `pass`
        # scored every approving PR review as a rejection.
        ok = d.attempt_score(0, "pr-reviewer", {"approve": True, "issues": []},
                             "merged")
        bad = d.attempt_score(0, "pr-reviewer",
                              {"approve": False, "issues": ["x"]}, "merged")
        self.assertGreater(ok, bad)

    def test_build_tree_folds_a_pr_reviewer_run_into_the_node(self):
        tasks = [{"id": "a", "status": "merged", "model": "DS"}]
        runs = [
            {"task_id": "a", "role": "implementer", "attempt": 1,
             "exit_code": 0, "seconds": 10, "verdict": None},
            {"task_id": "a", "role": "reviewer", "attempt": 1,
             "exit_code": 0, "seconds": 5, "verdict": _clean_review()},
            {"task_id": "a", "role": "pr-reviewer", "attempt": 1,
             "exit_code": 0, "seconds": 5,
             "verdict": json.dumps({"approve": False, "issues": ["nope"]})},
        ]
        t = d.build_tree("demo", tasks, runs)
        node = t.nodes["a-x1"]                    # one attempt, all runs folded
        self.assertEqual(node.outcome, "review_reject")
        clean = d.build_tree("demo", tasks, runs[:2]).nodes["a-x1"]
        self.assertLess(node.score, clean.score)  # the PR rejection hurts

    def test_build_tree_survives_garbage_field_values(self):
        tasks = [{"id": "a", "status": "merged", "model": "DS"}]
        runs = [{"task_id": "a-x1", "role": "implementer", "attempt": "?",
                 "exit_code": "abc", "seconds": "x", "verdict": None}]
        t = d.build_tree("demo", tasks, runs)     # must not raise
        self.assertIn("a-x1", t.nodes)

    def test_unrun_task_is_not_scored_as_a_success(self):
        tasks = [{"id": "a", "status": "", "model": "DS"}]
        t = d.build_tree("demo", tasks, [])
        # No harness ran, so no "ran to completion" credit: well under 0.5.
        self.assertLess(t.nodes["a-x1"].score, 0.5)

    def test_deps_parenting_survives_newest_first_store_order(self):
        # store.code_tasks_all is ORDER BY created_at DESC, so the dependent
        # is seen FIRST. Without topo-ordering, `parent_for` finds no dep node
        # and every branch flattens to a root child.
        tasks = [{"id": "b", "status": "merged", "model": "DS"},
                 {"id": "a", "status": "merged", "model": "DS"}]   # reversed
        runs = [
            {"task_id": "a-x1", "role": "implementer", "attempt": 1,
             "exit_code": 0, "seconds": 5, "verdict": None},
            {"task_id": "a-x1", "role": "reviewer", "attempt": 1,
             "exit_code": 0, "seconds": 5, "verdict": _clean_review()},
        ]
        t = d.build_tree("demo", tasks, runs, deps_by_task={"b": ["a"]})
        self.assertEqual(t.nodes["b-x1"].parent, "a-x1")

    def test_a_raising_policy_does_not_take_improve_down(self):
        tree = d.build_tree("demo", *_branchy_rows())

        class Boom(d.ExplorationPolicy):
            name = "boom"

            def choose(self, tree, eligible, W):
                raise RuntimeError("agent-authored policy went wrong")

        res = d.improve([tree], extra_policies=[Boom()], W=3, max_rounds=12)
        self.assertIn("fixed", res.mean_by_policy)

    def test_a_policy_returning_none_does_not_take_improve_down(self):
        tree = d.build_tree("demo", *_branchy_rows())

        class Noneish(d.ExplorationPolicy):
            name = "noneish"

            def choose(self, tree, eligible, W):
                return None

        res = d.improve([tree], extra_policies=[Noneish()], W=3, max_rounds=12)
        self.assertTrue(res.mean_by_policy)

    def test_zero_width_replay_terminates(self):
        # W=0 made batch[:W] empty, so the loop spun to max_rounds doing
        # nothing — a near-hang at a large limit.
        t = d.build_tree("demo", *_branchy_rows())
        r = d.replay(t, d.GreedyBest(), W=0, max_rounds=10 ** 6)
        self.assertLess(r.rounds, 100)

    def test_compile_policy_is_a_real_sandbox(self):
        # CPython injects the real __builtins__ by default, so `import os`
        # (subprocess, file I/O) worked despite the docstring.
        evil = ("import os\n"
                "class Policy(ExplorationPolicy):\n"
                "    name = 'evil'\n"
                "    def choose(self, tree, eligible, W):\n"
                "        return []\n")
        self.assertIsNone(d.compile_policy(evil))
        good = ("class Policy(ExplorationPolicy):\n"
                "    name = 'good'\n"
                "    def choose(self, tree, eligible, W):\n"
                "        return sorted(eligible)[:W]\n")
        self.assertIsNotNone(d.compile_policy(good))   # non-import policy is fine

    def test_topological_order_keeps_declaration_order_without_deps(self):
        self.assertEqual(d._topo_order(["c", "a", "b"], {}), ["c", "a", "b"])
        self.assertEqual(d._topo_order(["b", "a"], {"b": ["a"]}), ["a", "b"])
        # A cycle (loader-rejected, but a stale file could hold one) does not
        # spin: the blocked remainder is appended as written.
        self.assertEqual(sorted(d._topo_order(["a", "b"],
                                              {"a": ["b"], "b": ["a"]})),
                         ["a", "b"])

    def test_topological_order_dedupes_a_repeated_id(self):
        # A duplicate id must not be placed twice (it would build two nodes).
        self.assertEqual(d._topo_order(["a", "b", "a"], {}), ["a", "b"])

    def test_deps_on_a_missing_task_do_not_block_placement(self):
        # A dep naming an absent task is not a constraint — it is filtered
        # against the known ids, so "x" is not left blocked. Locks the intended
        # semantics (two nodes, so an ordering regression would show).
        self.assertEqual(d._topo_order(["x", "y"], {"x": ["ghost"]}),
                         ["x", "y"])


class SandboxHardening(unittest.TestCase):
    """The restricted builtins dict is not, by itself, a sandbox: CPython
    objects leak the interpreter through dunder attributes. These pin the
    structural gate that closes the escape (verified: it reached the real
    ``open``/``__import__`` before this fix)."""

    def test_subclasses_escape_is_blocked(self):
        escape = (
            "class Policy(ExplorationPolicy):\n"
            "    name = 'esc'\n"
            "    def choose(self, tree, eligible, W):\n"
            "        for c in object.__subclasses__():\n"
            "            g = c.__init__.__globals__\n"
            "            return [g['__builtins__']]\n"
            "        return []\n")
        self.assertIsNone(d.compile_policy(escape))

    def test_class_climb_escape_is_blocked(self):
        escape = (
            "class Policy(ExplorationPolicy):\n"
            "    name = 'esc2'\n"
            "    def choose(self, tree, eligible, W):\n"
            "        return ().__class__.__bases__[0].__subclasses__()\n")
        self.assertIsNone(d.compile_policy(escape))

    def test_dunder_access_is_blocked_anywhere(self):
        for src in ("x = ().__class__\n",
                    "class Policy(ExplorationPolicy):\n"
                    "    def choose(self, t, e, W):\n"
                    "        return [a for a in e if a != 'root'][:W]\n"
                    "    name = __name__\n"):
            self.assertIsNone(d.compile_policy(src), src)

    def test_builtin_getattr_eval_open_are_refused(self):
        for name in ("getattr", "eval", "exec", "open", "vars", "globals"):
            src = ("class Policy(ExplorationPolicy):\n"
                   "    name = 'x'\n"
                   "    def choose(self, t, e, W):\n"
                   f"        return [{name}]\n")
            self.assertIsNone(d.compile_policy(src), name)

    def test_a_legitimate_policy_still_compiles(self):
        good = ("class Policy(ExplorationPolicy):\n"
                "    name = 'good'\n"
                "    def choose(self, tree, eligible, W):\n"
                "        ranked = sorted(eligible, key=lambda n: tree.nodes[n].score)\n"
                "        return ranked[:W]\n")
        pol = d.compile_policy(good)
        self.assertIsNotNone(pol)
        self.assertEqual(pol.name, "good")

    def test_format_string_field_walk_escape_is_blocked(self):
        # `str.format` resolves `{0.choose.__globals__}` at RUNTIME, so the
        # dunder lives inside a string Constant and the AST attr/name checks
        # never see it. Verified live: this exfiltrated config.API_KEY.
        for src in (
                "class Policy(ExplorationPolicy):\n"
                "    name = 'e'\n"
                "    def choose(self, tree, eligible, W):\n"
                "        return ['{0.choose.__globals__}'.format(ExplorationPolicy)]\n",
                "class Policy(ExplorationPolicy):\n"
                "    name = 'e'\n"
                "    def choose(self, tree, eligible, W):\n"
                "        return ['{a.b.c}'.format_map({'a.b.c': 1})]\n",
                "class Policy(ExplorationPolicy):\n"
                "    name = 'e'\n"
                "    def choose(self, tree, eligible, W):\n"
                "        f = '{}'.format\n"
                "        return [f]\n"):
            self.assertIsNone(d.compile_policy(src), src)

    def test_non_dunder_introspection_accessors_are_blocked(self):
        # `.mro()` is NOT a dunder, so the dunder check misses it — yet it
        # returns the REAL `object` class (verified: ExplorationPolicy.mro()
        # leaked `<class 'object'>`). The frame accessors are the same class of
        # hole. All are refused by name.
        for label, src in (
                ("mro", "class Policy(ExplorationPolicy):\n"
                        "    name = 'e'\n"
                        "    def choose(self, tree, eligible, W):\n"
                        "        return [str(c) for c in ExplorationPolicy.mro()]\n"),
                ("int.mro", "class Policy(ExplorationPolicy):\n"
                            "    name = 'e'\n"
                            "    def choose(self, tree, eligible, W):\n"
                            "        return [str(c) for c in int.mro()]\n"),
                ("gi_frame", "class Policy(ExplorationPolicy):\n"
                             "    name = 'e'\n"
                             "    def choose(self, tree, eligible, W):\n"
                             "        def g():\n"
                             "            yield 1\n"
                             "        return [str(g().gi_frame)]\n"),
                ("f_builtins", "class Policy(ExplorationPolicy):\n"
                               "    name = 'e'\n"
                               "    def choose(self, tree, eligible, W):\n"
                               "        return [str(tree.f_builtins)]\n")):
            self.assertIsNone(d.compile_policy(src), label)

    def test_format_call_is_blocked_even_for_plain_substitution(self):
        # `.format`/`.format_map` are refused wholesale: their FIELD NAMES are
        # data the AST cannot see, so the accessor itself is the hole. A plain
        # substitution uses an f-string, whose fields ARE parsed as AST.
        for src in ("class Policy(ExplorationPolicy):\n"
                    "    name = 'ok'\n"
                    "    def choose(self, tree, eligible, W):\n"
                    "        return ['n={}'.format(len(eligible))]\n",
                    "class Policy(ExplorationPolicy):\n"
                    "    name = 'ok'\n"
                    "    def choose(self, tree, eligible, W):\n"
                    "        return ['{p}'.format_map({'p': 1})]\n"):
            self.assertIsNone(d.compile_policy(src), src)

    def test_fstring_with_a_plain_field_still_compiles(self):
        src = ("class Policy(ExplorationPolicy):\n"
               "    name = 'ok'\n"
               "    def choose(self, tree, eligible, W):\n"
               "        n = len(eligible)\n"
               "        ranked = sorted(eligible, key=lambda x: tree.nodes[x].score)\n"
               "        return ranked[:W]\n")
        self.assertIsNotNone(d.compile_policy(src))


class Isolation(unittest.TestCase):
    """Every tree must see ONLY its own taskfile's runs — `run_rows` is the
    whole store, and task ids recur across taskfiles (7 do in the real store)."""

    def test_a_foreign_windows_run_does_not_leak_into_the_tree(self):
        # Two taskfiles reuse the id "api"; each has its own time window. The
        # first taskfile's tree must take only the run inside ITS window.
        first = [{"id": "api", "status": "merged", "model": "DS",
                  "created_at": "2026-09-09T00:00:00+00:00",
                  "finished_at": "2026-09-09T00:30:00+00:00"}]
        second = [{"id": "api", "status": "merged", "model": "DS",
                   "created_at": "2026-09-09T13:00:00+00:00",
                   "finished_at": "2026-09-09T13:30:00+00:00"}]
        runs = [
            {"task_id": "api", "role": "implementer", "attempt": 1,
             "exit_code": 0, "seconds": 5, "verdict": None,
             "created_at": "2026-09-09T00:10:00+00:00"},
            {"task_id": "api", "role": "implementer", "attempt": 1,
             "exit_code": 1, "seconds": 5, "verdict": None,
             "created_at": "2026-09-09T13:10:00+00:00"},
        ]
        t1 = d.build_tree("first", first, runs)
        t2 = d.build_tree("second", second, runs)
        # Each window took exactly one run; neither absorbed the other's. The
        # kept runs differ (exit 0 vs exit 1), so their scores must differ.
        self.assertEqual(len([n for n in t1.nodes.values() if n.id != "root"]), 1)
        self.assertEqual(len([n for n in t2.nodes.values() if n.id != "root"]), 1)
        self.assertNotEqual(t1.nodes["api-x1"].score, t2.nodes["api-x1"].score)
        self.assertGreater(t1.nodes["api-x1"].score, t2.nodes["api-x1"].score)

    def test_run_without_a_timestamp_is_kept(self):
        # A row with no created_at cannot be ruled out of any window.
        tasks = [{"id": "api", "status": "merged", "model": "M",
                  "created_at": "2026-09-09T00:00:00+00:00",
                  "finished_at": "2026-09-09T00:30:00+00:00"}]
        runs = [{"task_id": "api", "role": "implementer", "attempt": 1,
                 "exit_code": 0, "seconds": 5, "verdict": None}]
        t = d.build_tree("demo", tasks, runs)
        self.assertIn("api-x1", t.nodes)

    def test_naive_and_aware_timestamps_do_not_raise(self):
        # One legacy row is tz-naive (space-separated); comparing it against an
        # aware run must not raise TypeError, and it must be read as UTC.
        tasks = [{"id": "api", "status": "merged", "model": "M",
                  "created_at": "2026-09-09 00:00:00",
                  "finished_at": "2026-09-09 00:30:00"}]
        runs = [{"task_id": "api", "role": "implementer", "attempt": 1,
                 "exit_code": 0, "seconds": 5, "verdict": None,
                 "created_at": "2026-09-09T00:10:00+00:00"}]
        t = d.build_tree("demo", tasks, runs)     # must not raise
        self.assertEqual(t.nodes["api-x1"].outcome, "merged")
        self.assertEqual(t.nodes["api-x1"].score, 2.0)   # run kept, exit 0, merged
        outside = d.build_tree("demo2", tasks,
                               [dict(runs[0],
                                     created_at="2026-09-09T05:00:00+00:00")])
        self.assertEqual(outside.nodes["api-x1"].outcome, "unrun")  # ran elsewhere

    def test_an_open_window_is_bounded_by_the_next_same_id_start(self):
        # A row with finished_at IS NULL (2 exist in the real store) gave an
        # OPEN-ENDED window that absorbed a later taskfile's run for a reused
        # id. Two same-id rows are sequential, so the open window must end at
        # the next row's start.
        open_a = [{"id": "api", "status": "running", "model": "D",
                   "created_at": "2026-09-09T00:00:00+00:00",
                   "finished_at": None}]
        later = [{"task_id": "api", "role": "implementer", "attempt": 1,
                  "exit_code": 1, "seconds": 5, "verdict": None,
                  "created_at": "2026-09-09T13:10:00+00:00"}]
        # A lone open window cannot be bounded -> the run still lands (control).
        self.assertNotEqual(
            d.build_tree("solo", open_a, later).nodes["api-x1"].outcome,
            "unrun")
        # With a later same-id row, A's window is [00:00, 13:00] and the 13:10
        # run belongs to the later taskfile, NOT to A.
        rows = open_a + [{"id": "api", "status": "merged", "model": "D",
                          "created_at": "2026-09-09T13:00:00+00:00",
                          "finished_at": "2026-09-09T13:30:00+00:00"}]
        w = d._closed_windows(rows)["api"]
        self.assertEqual(len(w), 2)
        self.assertEqual(w[0][1], d._ts("2026-09-09T13:00:00+00:00"))
        self.assertFalse(d._run_in_taskfile_window(
            d._ts("2026-09-09T13:10:00+00:00"), [w[0]]))
        self.assertTrue(d._run_in_taskfile_window(
            d._ts("2026-09-09T13:10:00+00:00"), [w[1]]))

    def test_window_helper_brackets_and_accepts_unparseable(self):
        from datetime import datetime
        w = [(datetime(2026, 9, 9, 0, 0), datetime(2026, 9, 9, 0, 30))]
        self.assertTrue(d._run_in_taskfile_window(
            datetime(2026, 9, 9, 0, 10), w))
        self.assertFalse(d._run_in_taskfile_window(
            datetime(2026, 9, 9, 1, 10), w))
        self.assertTrue(d._run_in_taskfile_window(None, w))       # unknown -> keep
        self.assertTrue(d._run_in_taskfile_window(                     # open end
            datetime(2026, 9, 9, 1, 10), [(w[0][0], None)]))

    def test_legacy_dash_x_task_id_is_still_attached_when_known(self):
        # A row may carry a "<tid>-xN" suffix (legacy / prompt path); when the
        # stripped head is a task in THIS tree it still attaches.
        tasks = [{"id": "a", "status": "merged", "model": "M"}]
        runs = [{"task_id": "a-x1", "role": "implementer", "attempt": 1,
                 "exit_code": 0, "seconds": 5, "verdict": None}]
        t = d.build_tree("demo", tasks, runs)
        self.assertIn("a-x1", t.nodes)


class PolicyNameCollision(unittest.TestCase):
    def test_two_policies_sharing_a_name_score_separately(self):
        tree = d.build_tree("demo", *_branchy_rows())

        class A(d.ExplorationPolicy):
            name = "same"

            def choose(self, tree, eligible, W):
                return list(eligible)[:W]

        class B(d.ExplorationPolicy):
            name = "same"

            def choose(self, tree, eligible, W):
                return []

        res = d.score_policies([tree], [A(), B()])
        # Both are scored; the collision did not merge them into one entry.
        self.assertEqual(len(res.mean_by_policy), 2)
        self.assertEqual(len(set(res.mean_by_policy.values())), 2)
        # The argmax is the expanding policy, not the stopper.
        self.assertTrue(res.selected.startswith("same"))


if __name__ == "__main__":
    unittest.main()
