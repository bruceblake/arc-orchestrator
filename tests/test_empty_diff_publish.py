"""An empty-diff publish must be a no-op success, not a failed task.

When the worktree is clean (gitstore.publish -> None) and the branch is not
ahead of the base, the publish node should mark the task merged without
touching GitHub, and pr_merge should complete so dependents are released.
"""

import asyncio
import contextlib
import json
import tempfile
import unittest
from pathlib import Path

from helpers import FakeStore, capture_events

import code_tasks
import config


def taskfile(tasks, repo="/tmp", title="t", after=None, pattern=None):
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


BASIC = {
    "id": "t1",
    "title": "T1",
    "prompt": "do it",
    "model": config.ESCALATION_PATH[0],
    "reviewer": config.cross_family_reviewer(config.ESCALATION_PATH[0]),
}


def graph(tasks=None):
    st = FakeStore([])
    path = taskfile(tasks or [BASIC])
    ts = code_tasks.load_taskfile(path)
    with capture_events():
        g = code_tasks.build_code_graph(st, ts, taskfile="tf.json")
    return st, g


@contextlib.contextmanager
def stubbed(**fns):
    saved = {n: getattr(code_tasks.gitstore, n) for n in fns}
    for n, fn in fns.items():
        setattr(code_tasks.gitstore, n, fn)
    try:
        yield
    finally:
        for n, fn in saved.items():
            setattr(code_tasks.gitstore, n, fn)


def never(name):
    async def boom(*a, **kw):
        raise AssertionError(f"gitstore.{name} must not be called")
    return boom


class EmptyDiffPublishTests(unittest.TestCase):
    def test_fresh_empty_diff_merges_without_gh(self):
        st, g = graph()
        calls = []

        async def fake_publish(wt, msg, trailers):
            calls.append("publish")
            return None

        async def fake_sync(wt, base, keep_conflicts=False):
            return (True, [], "merged cleanly")

        async def fake_ahead(repo, tid, base):
            calls.append("branch_ahead")
            return False

        async def fake_cleanup(repo, tid):
            calls.append("cleanup")

        ctx = {
            "results": {
                "alloc_t1": {"worktree": "/tmp/wt"},
                "implement_t1": {"ok": True},
                "gate_t1": {"passed": True, "verdict": "ok"},
            },
            "runs": {},
        }
        with stubbed(publish=fake_publish, sync_with_base=fake_sync,
                     branch_ahead=fake_ahead, cleanup=fake_cleanup,
                     open_pr=never("open_pr"), _gh=never("_gh"),
                     push_task_branch=never("push_task_branch")):
            with capture_events() as ev:
                out = asyncio.run(g.nodes["publish_t1"].fn(ctx))
        self.assertTrue(out["merged"])
        self.assertTrue(out["empty"])
        self.assertFalse(out["published"])
        self.assertIn("branch_ahead", calls)
        self.assertIn("cleanup", calls)
        self.assertEqual(ev.first("task.merged"),
                         {"task": "t1", "note": "empty diff: nothing to publish"})
        self.assertTrue(ev.first("task.resynced"))
        merged = [u for u in st.upserts if u["status"] == "merged"]
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["id"], "t1")
        self.assertTrue(merged[0]["finished"])

    def test_pr_merge_completes_without_touching_the_pr(self):
        st, g = graph()
        ctx = {
            "results": {
                "publish_t1": {"published": False, "merged": True,
                               "empty": True, "head": None},
                "gate_t1": {"passed": True, "verdict": "ok"},
            },
            "runs": {},
        }
        with stubbed(pr_state=never("pr_state")):
            with capture_events():
                out = asyncio.run(g.nodes["pr_merge_t1"].fn(ctx))
        self.assertTrue(out["merged"])
        self.assertEqual(out["verdict"], "ok")

    def test_empty_publish_edge_reaches_pr_merge(self):
        st, g = graph()
        edges = [e for e in g.edges
                 if e.src == "publish_t1" and e.dst == "pr_merge_t1"]
        self.assertEqual(len(edges), 1)
        e = edges[0]
        self.assertTrue(e.when({"empty": True}, {}))
        self.assertFalse(e.when({"published": True, "pr": 5}, {}))
        self.assertFalse(e.when({"published": False, "reason": "x"}, {}))
        self.assertTrue(e.on_drain)

    def test_alloc_retry_edge_ignores_merged_noop(self):
        st, g = graph()
        edges = [e for e in g.edges
                 if e.src == "publish_t1" and e.dst == "alloc_t1"]
        self.assertEqual(len(edges), 1)
        e = edges[0]
        self.assertFalse(
            e.when({"published": False, "merged": True, "empty": True},
                   {"results": {}}))
        self.assertTrue(e.when({"published": False, "reason": "x"},
                               {"results": {}}))

    def test_dependent_released_by_merged_noop(self):
        t2 = dict(BASIC, id="t2", title="T2", deps=["t1"])
        st, g = graph([BASIC, t2])
        edges = [e for e in g.edges
                 if e.src == "pr_merge_t1" and e.dst == "alloc_t2"]
        self.assertEqual(len(edges), 1)
        self.assertIsNone(edges[0].when)

    def test_reworked_empty_diff_still_fails(self):
        st, g = graph()
        calls = []

        async def fake_publish(wt, msg, trailers):
            calls.append("publish")
            return None

        async def fake_sync(wt, base, keep_conflicts=False):
            return (True, [], "already up to date")

        async def fake_ahead(repo, tid, base):
            calls.append("branch_ahead")
            return False

        ctx = {
            "results": {
                "alloc_t1": {"worktree": "/tmp/wt"},
                "implement_t1": {"ok": True},
                "gate_t1": {"passed": True, "verdict": "ok"},
                "pr_review_t1": {"approved": False, "verdicts": []},
            },
            "runs": {},
        }
        with stubbed(publish=fake_publish, sync_with_base=fake_sync,
                     branch_ahead=fake_ahead,
                     open_pr=never("open_pr")):
            with capture_events() as ev:
                out = asyncio.run(g.nodes["publish_t1"].fn(ctx))
        self.assertFalse(out["merged"])
        self.assertEqual(out["reason"], "no changes")
        self.assertIsNone(ev.first("task.merged"))
        self.assertNotIn("cleanup", calls)

    def test_real_commits_still_open_a_pr(self):
        st, g = graph()
        calls = []

        async def fake_publish(wt, msg, trailers):
            calls.append("publish")
            return None

        async def fake_sync(wt, base, keep_conflicts=False):
            return (True, [], "already up to date")

        async def fake_ahead(repo, tid, base):
            calls.append("branch_ahead")
            return True

        async def fake_push(repo, tid):
            calls.append("push_task_branch")
            return (True, "pushed")

        async def fake_open_pr(repo, tid, title, body, base):
            calls.append("open_pr")
            return (7, "http://pr/7", "opened")

        ctx = {
            "results": {
                "alloc_t1": {"worktree": "/tmp/wt"},
                "implement_t1": {"ok": True},
                "gate_t1": {"passed": True, "verdict": "ok"},
            },
            "runs": {},
        }
        with stubbed(publish=fake_publish, sync_with_base=fake_sync,
                     branch_ahead=fake_ahead, push_task_branch=fake_push,
                     open_pr=fake_open_pr, cleanup=never("cleanup")):
            with capture_events() as ev:
                out = asyncio.run(g.nodes["publish_t1"].fn(ctx))
        self.assertTrue(out["published"])
        self.assertEqual(out["pr"], 7)
        self.assertIsNone(ev.first("task.merged"))


if __name__ == "__main__":
    unittest.main()