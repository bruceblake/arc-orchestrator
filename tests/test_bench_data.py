"""Structural guards for the bench_data suite registry and selection helper.

bench.py resolves every micro-benchmark run through SUITES/tasks_for (see the
deferred import in main.py), so a suite registered with an empty task list, a
duplicate task_id, or a task missing one of the documented keys does not fail
at definition time — it breaks (or silently truncates) a benchmark run far
from the change that caused it. These tests are cheap enough for every gate
and catch a malformed suite the moment it is added.

bench_data.py is not committed to this repo — the real file exists only as
an untracked copy in the operator's checkout — so on a worktree allocated
from main every test below skips with a reason saying exactly that, instead
of erroring the gate. The guards were written against, and verified to pass
against, the real module; they activate unchanged the moment bench_data.py
is committed to main.
"""

import unittest

from helpers import capture_events  # noqa: F401  (sys.path)

try:
    from bench_data import SUITES, tasks_for
    _SKIP_REASON = ""
except ModuleNotFoundError as exc:
    if exc.name != "bench_data":
        raise
    SUITES = None
    tasks_for = None
    _SKIP_REASON = (
        "bench_data.py is not committed to this repo (the real file exists "
        "only as an untracked copy in the operator's checkout), so this "
        "worktree has no registry to guard; these tests activate the moment "
        "it is committed to main. Do not stub or recreate the module to "
        "make them run."
    )

DOCUMENTED_KEYS = {"task_id", "suite", "tier", "kind", "entry", "prompt", "files", "timeout"}
DOCUMENTED_TIERS = {"easy", "medium", "hard"}
DOCUMENTED_KINDS = {"function", "package"}
DOCUMENTED_SUITES = {"humaneval", "original", "package"}


@unittest.skipUnless(not _SKIP_REASON, _SKIP_REASON)
class TestBenchDataSuites(unittest.TestCase):
    """The registry shape that the bench runner and the report rely on.

    Registry malformations surface mid-run or as silently wrong scores, so
    every structural rule the dataset follows is checked up front here.
    """

    def test_documented_suites_are_present(self):
        """humaneval/original/package are the dataset's documented suite names;
        a rename would break every bench invocation and doc that names them."""
        for name in DOCUMENTED_SUITES:
            self.assertIn(name, SUITES, f"documented suite {name!r} missing from the registry")

    def test_every_suite_has_a_nonempty_desc_and_tasks_list(self):
        """A suite without metadata renders as a broken row in the bench
        report, and one without tasks gives the runner nothing to execute."""
        for name, suite in SUITES.items():
            with self.subTest(suite=name):
                for key in ("desc", "tasks"):
                    self.assertIn(key, suite, f"suite {name!r} lacks {key!r}")
                self.assertIsInstance(suite["desc"], str, f"suite {name!r} desc must be a str")
                self.assertTrue(suite["desc"].strip(), f"suite {name!r} has an empty desc")
                self.assertIsInstance(suite["tasks"], list, f"suite {name!r} tasks must be a list")
                self.assertTrue(suite["tasks"], f"suite {name!r} has no tasks")

    def test_every_task_has_the_keys_and_vocabulary_the_runner_needs(self):
        """Each task is documented to carry the eight keys the harnesses read
        (task_id/suite/tier/kind/entry/prompt/files/timeout) with tier in
        {easy,medium,hard} and kind in {function,package}; a missing or
        out-of-vocabulary value is a KeyError waiting to happen mid-run."""
        for name, suite in SUITES.items():
            for task in suite["tasks"]:
                with self.subTest(suite=name, task_id=task.get("task_id")):
                    missing = DOCUMENTED_KEYS - set(task)
                    self.assertFalse(
                        missing,
                        f"task {task.get('task_id')!r} in {name!r} is missing keys: {sorted(missing)}",
                    )
                    self.assertIsInstance(task["task_id"], str, "task_id must be a str")
                    self.assertTrue(task["task_id"].strip(), "task_id must be a non-empty slug")
                    self.assertIn(task["tier"], DOCUMENTED_TIERS, f"unknown tier {task['tier']!r}")
                    self.assertIn(task["kind"], DOCUMENTED_KINDS, f"unknown kind {task['kind']!r}")

    def test_task_ids_are_unique_within_each_suite(self):
        """Duplicate ids would make per-task results collide in the report and
        let one task's pass/fail overwrite another's."""
        for name, suite in SUITES.items():
            seen = set()
            for task in suite["tasks"]:
                with self.subTest(suite=name, task_id=task.get("task_id")):
                    self.assertNotIn(task["task_id"], seen, f"duplicate task_id in suite {name!r}")
                    seen.add(task["task_id"])

    def test_task_suite_field_matches_the_registry_key(self):
        """A task whose 'suite' field disagrees with its registry key was
        copy-pasted between suites and would be mislabelled in every report."""
        for name, suite in SUITES.items():
            for task in suite["tasks"]:
                with self.subTest(suite=name, task_id=task.get("task_id")):
                    self.assertEqual(
                        task["suite"], name,
                        f"task {task['task_id']!r} registered under {name!r} but tagged {task['suite']!r}",
                    )


@unittest.skipUnless(not _SKIP_REASON, _SKIP_REASON)
class TestBenchDataTasksFor(unittest.TestCase):
    """tasks_for is the single resolution path from suite names to tasks.

    The bench CLI and the harnesses all select work through it, so a filter
    that silently drops tasks or mangles order skews every measured score.
    """

    def test_single_suite_returns_exactly_that_suites_tasks_in_order(self):
        """Selecting one suite must yield precisely its registered tasks in
        registry order — nothing dropped, nothing extra."""
        for name, suite in SUITES.items():
            with self.subTest(suite=name):
                self.assertEqual(tasks_for([name]), suite["tasks"])

    def test_multiple_suites_concatenate_in_the_requested_order(self):
        """Selection order follows the argument list, not the registry, so a
        caller that asks for [a, b] and one that asks for [b, a] measure
        different (correctly ordered) runs."""
        names = list(SUITES)
        self.assertEqual(
            tasks_for(names),
            [t for n in names for t in SUITES[n]["tasks"]],
        )
        self.assertEqual(
            tasks_for(list(reversed(names))),
            [t for n in reversed(names) for t in SUITES[n]["tasks"]],
        )

    def test_empty_suite_list_returns_no_tasks(self):
        """Asking for nothing must yield an empty run, not every task."""
        self.assertEqual(tasks_for([]), [])

    def test_tier_filter_returns_only_tasks_of_the_requested_tiers(self):
        """Tier filtering is how a bench invocation scopes difficulty; leaking
        another tier or dropping a match silently changes what was measured."""
        for name, suite in SUITES.items():
            for tier in sorted({t["tier"] for t in suite["tasks"]}):
                with self.subTest(suite=name, tier=tier):
                    self.assertEqual(
                        tasks_for([name], tiers=[tier]),
                        [t for t in suite["tasks"] if t["tier"] == tier],
                    )
        names = list(SUITES)
        tier_union = sorted({t["tier"] for s in SUITES.values() for t in s["tasks"]})[:2]
        self.assertEqual(
            tasks_for(names, tiers=tier_union),
            [t for n in names for t in SUITES[n]["tasks"] if t["tier"] in tier_union],
        )

    def test_limit_truncates_to_a_prefix_of_the_unfiltered_selection(self):
        """limit must cap the run at the requested size without reordering —
        the truncated set has to be the head of the full selection."""
        everything = tasks_for(list(SUITES))
        self.assertGreater(len(everything), 0, "registry has no tasks at all")
        n = min(3, len(everything))
        self.assertEqual(tasks_for(list(SUITES), limit=n), everything[:n])

    def test_limit_larger_than_the_selection_returns_everything(self):
        """An oversized limit must not clip or error — callers pass round
        numbers and expect all matching tasks back."""
        names = list(SUITES)
        everything = tasks_for(names)
        self.assertEqual(tasks_for(names, limit=len(everything) + 10), everything)

    def test_unknown_suite_raises_value_error_naming_the_suite(self):
        """A typo'd suite name must fail loudly with the bad name in the
        message; a silent empty run would score as a flawless benchmark."""
        with self.assertRaisesRegex(ValueError, "no-such-suite"):
            tasks_for(["no-such-suite"])

    def test_unknown_suite_raises_even_when_mixed_with_valid_suites(self):
        """One bad name among valid ones must still raise, not quietly drop
        the unknown suite and benchmark a subset of what was asked for."""
        with self.assertRaises(ValueError):
            tasks_for(["humaneval", "does-not-exist"])


if __name__ == "__main__":
    unittest.main()