"""graph_shapes: the catalogue is complete and drawable, classify() names the
shape a taskfile's deps actually form, and /api/graph-shapes serves both.

The classifier is what tells a planner (and the dashboard) that a taskfile
labelled "diamond" is wired as a chain — so its rules are pinned here with
one fixture per shape, including the redundant-root join that the real
minecraft taskfile has (scaffold, a, b, c, d → integrate).
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helpers import capture_events  # noqa: F401,E402  (sys.path + event redirect)

import config  # noqa: E402
import graph_shapes as gs  # noqa: E402


def T(i, deps=()):
    return {"id": i, "title": i, "prompt": "p", "deps": list(deps)}


class Catalogue(unittest.TestCase):
    def test_every_pattern_is_complete_and_its_sketch_is_a_dag_over_its_own_nodes(self):
        for p in gs.PATTERNS:
            with self.subTest(p=p["id"]):
                for k in ("id", "name", "gist", "when", "how", "pitfalls", "sketch", "aliases"):
                    self.assertTrue(p.get(k) not in (None, "", []), f"{p['id']} lacks {k}")
                sk = p["sketch"]
                names = {n["name"] for n in sk["nodes"]}
                for e in sk["edges"]:
                    self.assertIn(e["src"], names)
                    self.assertIn(e["dst"], names)
                self.assertTrue(set(sk["starts"]) <= names)
                self.assertTrue(len(sk["nodes"]) >= 1)

    def test_ids_are_unique_and_aliases_resolve(self):
        self.assertEqual(len(gs.PATTERN_IDS), len(set(gs.PATTERN_IDS)))
        self.assertEqual(gs.normalize_pattern("fan-out-fan-in"), "fanout")
        self.assertEqual(gs.normalize_pattern("Fan Out"), "fanout")
        self.assertEqual(gs.normalize_pattern("orchestrator-workers"), "fanout")
        self.assertEqual(gs.normalize_pattern("Diamond"), "diamond")
        self.assertEqual(gs.normalize_pattern("evaluator_optimizer"), "evaluator")
        self.assertIsNone(gs.normalize_pattern("no-such-shape"))
        self.assertIsNone(gs.normalize_pattern(""))
        self.assertIsNone(gs.normalize_pattern(None))

    def test_decision_table_points_only_at_catalogue_ids(self):
        for kind, pid, n in gs.DECISIONS:
            self.assertIn(pid, gs.PATTERN_IDS, kind)


class Classify(unittest.TestCase):
    def shape(self, tasks, pattern=""):
        return gs.classify({"tasks": tasks, "pattern": pattern})

    def test_single(self):
        c = self.shape([T("a")])
        self.assertEqual((c["shape"], c["width"], c["depth"]), ("single", 1, 1))

    def test_chain(self):
        c = self.shape([T("a"), T("b", ["a"]), T("c", ["b"])])
        self.assertEqual((c["shape"], c["width"], c["depth"], c["heads"]), ("chain", 1, 3, ["a"]))

    def test_fanout_of_independent_heads(self):
        c = self.shape([T("a"), T("b"), T("c")])
        self.assertEqual((c["shape"], c["width"], c["depth"]), ("fanout", 3, 1))
        self.assertEqual(c["joins"], [])

    def test_fanout_under_one_head_is_fanout_not_chain(self):
        # api → panel, api → doc: one head, two tails. Declared "chain" is a
        # mismatch — exactly the slip the classifier exists to catch.
        c = self.shape([T("api"), T("panel", ["api"]), T("doc", ["api"])], pattern="chain")
        self.assertEqual((c["shape"], c["width"], c["depth"]), ("fanout", 2, 2))
        self.assertTrue(c["mismatch"])
        self.assertEqual(c["declared"], "chain")

    def test_fanout_with_fanin_join(self):
        c = self.shape([T("a"), T("b"), T("c"), T("merge", ["a", "b", "c"])], pattern="fan-out-fan-in")
        self.assertEqual((c["shape"], c["width"], c["depth"], c["joins"]), ("fanin", 3, 2, ["merge"]))
        self.assertFalse(c["mismatch"])   # fanin is the catalogue's fan-out/fan-in

    def test_diamond(self):
        c = self.shape([T("contract"), T("a", ["contract"]), T("b", ["contract"]),
                        T("verify", ["a", "b"])], pattern="diamond")
        self.assertEqual((c["shape"], c["width"], c["depth"]), ("diamond", 2, 3))
        self.assertFalse(c["mismatch"])

    def test_diamond_survives_a_join_that_also_lists_the_root(self):
        # The real minecraft taskfile: integrate deps [scaffold, a, b, c, d].
        c = self.shape([T("scaffold"), T("a", ["scaffold"]), T("b", ["scaffold"]),
                        T("c", ["scaffold"]), T("d", ["scaffold"]),
                        T("integrate", ["scaffold", "a", "b", "c", "d"])])
        self.assertEqual(c["shape"], "diamond")
        self.assertIn("4 sides", c["reason"])

    def test_hierarchical(self):
        c = self.shape([T("root"), T("m1", ["root"]), T("m2", ["root"]),
                        T("m1a", ["m1"]), T("m1b", ["m1"]), T("m2a", ["m2"]),
                        T("join1", ["m1a", "m1b"]), T("all", ["join1", "m2a"])])
        self.assertEqual(c["shape"], "hierarchical")
        self.assertEqual(len(c["joins"]), 2)

    def test_mixed_and_empty(self):
        self.assertEqual(self.shape([])["shape"], "empty")
        # two roots, a join in the middle, and a tail after it: not a clean fan-in
        c = self.shape([T("a"), T("b"), T("j", ["a", "b"]), T("k", ["j"]), T("z", ["b"])])
        self.assertIn(c["shape"], ("mixed", "hierarchical"))

    def test_unknown_dep_ids_are_ignored(self):
        c = self.shape([T("a", ["ghost"]), T("b", ["a"])])
        self.assertEqual(c["shape"], "chain")

    def test_built_in_labels_never_mismatch(self):
        c = self.shape([T("a"), T("b", ["a"])], pattern="evaluator-optimizer")
        self.assertFalse(c["mismatch"])

    def test_accepts_the_loader_dict_form(self):
        tasks = {"a": T("a"), "b": T("b", ["a"])}
        self.assertEqual(gs.classify({"tasks": tasks})["shape"], "chain")


class Describe(unittest.TestCase):
    def test_lists_every_taskfile_with_its_shape_and_used_by(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "one.json").write_text(json.dumps({"project": {
                "title": "One", "repo": "/r", "pattern": "diamond", "tasks": [
                    T("c"), T("a", ["c"]), T("b", ["c"]), T("v", ["a", "b"])]}}))
            (Path(d) / "two.json").write_text(json.dumps({"project": {
                "title": "Two", "repo": "/r", "after": ["/x/one.json"],
                "tasks": [T("a"), T("b", ["a"])]}}))
            (Path(d) / "junk.json").write_text("{not json")
            out = gs.describe(d)
        files = {p["file"]: p for p in out["projects"]}
        self.assertEqual(set(files), {"one.json", "two.json"})
        self.assertEqual(files["one.json"]["detected"]["shape"], "diamond")
        self.assertEqual(files["two.json"]["detected"]["shape"], "chain")
        self.assertEqual(files["two.json"]["after"], ["one.json"])
        by_id = {p["id"]: p for p in out["patterns"]}
        self.assertEqual(by_id["diamond"]["used_by"], ["one.json"])
        self.assertEqual(by_id["chain"]["used_by"], ["two.json"])
        self.assertTrue(out["engine"]["has"] and out["engine"]["missing"])
        self.assertEqual([c["model"] for c in out["caps"]], list(config.ESCALATION_PATH))
        self.assertTrue(out["decisions"])

    def test_engine_claims_are_checked_against_graph_py(self):
        names = {h["name"] for h in gs._engine()["has"]}
        for want in ("conditional edges", "joins", "dynamic fan-out", "subgraphs",
                     "taskfile chaining"):
            self.assertIn(want, names)


class PlannerProse(unittest.TestCase):
    def test_prose_carries_catalogue_decisions_caps_and_the_two_graph_rule(self):
        p = gs.planner_prose()
        self.assertIn("fixed per-task pipeline", p)
        for pat in gs.PATTERNS:
            if pat["id"] in ("evaluator", "escalate"):
                self.assertNotIn(f"    {pat['id']}: ", p)  # built in, not a choice
            else:
                self.assertIn(f"    {pat['id']}: {pat['gist']}", p)
        self.assertIn("Decision table", p)
        for m in config.ESCALATION_PATH:
            self.assertIn(f"{m} {config.driver_limit(m)}", p)
        self.assertIn('"after"', p)


if __name__ == "__main__":
    unittest.main()
