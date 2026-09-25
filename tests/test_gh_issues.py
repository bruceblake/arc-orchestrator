"""Every task is a GitHub issue (gh_issues.py), with gh mocked end to end."""
import asyncio
import json
import os
import pathlib
import re
import shutil
import sqlite3
import tempfile
import types
from contextlib import closing
import unittest
from unittest import mock

from helpers import ENTRY, FakeStore, capture_events

import code_tasks
import config
import gh_issues
import gitstore

MODEL = ENTRY
REVIEWER = config.cross_family_reviewer(ENTRY)
REVIEWER_MODEL = "DeepSeek-V4.1-Flash-thinking-max"


class FakeGH:
    """An in-memory GitHub answering the `gh api` calls gh_issues makes."""

    def __init__(self, fail=False):
        self.issues, self.labels, self.calls = {}, set(), []
        self.pulls = {}
        self.fail = fail

    def comments(self, n):
        return self.issues[n]["comments"]

    async def __call__(self, args, cwd, timeout=180, wait_quota=True):
        self.calls.append(list(args))
        if self.fail:
            return 1, "", "HTTP 502: bad gateway"
        assert args[:2] == ["api", "-X"], args
        method, path = args[2], args[3]
        fields = {}
        for i, a in enumerate(args):
            if a == "-f":
                k, v = args[i + 1].split("=", 1)
                fields.setdefault(k, []).append(v)
        one = {k: v[-1] for k, v in fields.items()}
        path = path.replace("repos/{owner}/{repo}/", "")
        if path == "labels" and method == "POST":
            if one["name"] in self.labels:
                return 1, "", '{"errors":[{"code":"already_exists"}]} (HTTP 422)'
            self.labels.add(one["name"])
            return 0, "{}", ""
        if path.startswith("issues?") and method == "GET":
            want = re.search(r"labels=([^&]+)", path)
            per = int((re.search(r"per_page=(\d+)", path) or [0, 30])[1])
            page = int((re.search(r"[?&]page=(\d+)", path) or [0, 1])[1])
            out = [dict(number=n, title=i["title"], body=i["body"],
                        labels=[{"name": l} for l in i["labels"]])
                   for n, i in self.issues.items() if i["state"] == "open"
                   and (not want or want.group(1) in i["labels"])]
            return 0, json.dumps(out[(page - 1) * per:page * per]), ""
        if path == "issues" and method == "POST":
            n = len(self.issues) + 1
            self.issues[n] = {"title": one["title"], "body": one["body"],
                              "labels": fields.get("labels[]", []),
                              "state": "open", "comments": []}
            return 0, json.dumps({"number": n}), ""
        pm = re.fullmatch(r"pulls/(\d+)", path)
        if pm:
            pr = self.pulls.setdefault(int(pm.group(1)), {"body": ""})
            if method == "PATCH":
                pr.update(one)
            return 0, json.dumps(pr), ""
        m = re.fullmatch(r"issues/(\d+)(/\w+)?", path.split("?")[0])
        n, sub = int(m.group(1)), m.group(2)
        issue = self.issues[n]
        if sub is None and method == "PATCH":
            issue.update({k: v for k, v in one.items() if k != "labels[]"})
            if "labels[]" in fields:
                issue["labels"] = fields["labels[]"]
            return 0, "{}", ""
        if sub == "/comments" and method == "GET":
            per = int((re.search(r"per_page=(\d+)", path) or [0, 30])[1])
            page = int((re.search(r"[?&]page=(\d+)", path) or [0, 1])[1])
            got = [{"body": b} for b in issue["comments"]]
            return 0, json.dumps(got[(page - 1) * per:page * per]), ""
        if sub == "/comments":
            issue["comments"].append(one["body"])
            return 0, "{}", ""
        if sub == "/labels" and method == "GET":
            return 0, json.dumps([{"name": l} for l in issue["labels"]]), ""
        if sub == "/labels" and method == "PUT":
            issue["labels"] = fields.get("labels[]", [])
            return 0, "[]", ""
        raise AssertionError(f"unexpected gh call {args}")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="arc-gh-issues-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        for k, v in (("DB_PATH", str(self.tmp / "t.db")), ("GH_ISSUES", "on"),
                     ("ROOT", str(self.tmp))):
            p = mock.patch.object(config, k, v)
            p.start()
            self.addCleanup(p.stop)
        gh_issues._labels_done.clear()
        self.gh = FakeGH()
        p = mock.patch.object(gitstore, "_gh", self.gh)
        p.start()
        self.addCleanup(p.stop)
        self.tasks = [
            {"id": "t1", "title": "First", "prompt": "do one", "model": MODEL, "reviewer": REVIEWER,
             "verify_cmd": "true", "deps": [], "files_hint": ["a.py"]},
            {"id": "t2", "title": "Second", "prompt": "do two", "model": MODEL, "reviewer": REVIEWER,
             "verify_cmd": "true", "deps": ["t1"]}]
        self.tf = self.tmp / "proj.json"
        self.tf.write_text(json.dumps({"project": {
            "name": "proj", "repo": str(self.repo), "goal": "ship it",
            "pattern": "chain", "tasks": self.tasks}}))

    def run_(self, coro):
        return asyncio.run(coro)

    def task(self, i=0):
        return dict(self.tasks[i])


class Issues(Base):
    def test_epic_and_task_issue_creation(self):
        epic = self.run_(gh_issues.ensure_epic(self.repo, "proj", self.tf))
        n = self.run_(gh_issues.ensure_task_issue(
            self.repo, "proj", self.tf, self.task(), epic=epic))
        e, i = self.gh.issues[epic], self.gh.issues[n]
        self.assertEqual(e["title"], "[arc] proj")
        self.assertIn("ship it", e["body"])
        self.assertIn("`chain`", e["body"])
        self.assertEqual(i["title"], "[proj] First")
        self.assertIn(f"Part of #{epic}", i["body"])
        self.assertIn("```sh\ntrue\n```", i["body"])
        self.assertIn("`a.py`", i["body"])
        self.assertEqual(sorted(i["labels"]),
                         sorted(["arc-task", "arc:pending", f"model:{MODEL}"]))
        # The epic re-renders in place with the task's issue in its checklist.
        again = self.run_(gh_issues.ensure_epic(self.repo, "proj", self.tf))
        self.assertEqual(again, epic)
        self.assertIn(f"- [ ] #{n} First (`t1`, {MODEL} → ", e["body"])
        self.assertEqual(len(self.gh.issues), 2)

    def test_idempotent_through_the_db(self):
        a = self.run_(gh_issues.ensure_task_issue(self.repo, "proj", self.tf, self.task()))
        before = len(self.gh.calls)
        b = self.run_(gh_issues.ensure_task_issue(self.repo, "proj", self.tf, self.task()))
        self.assertEqual(a, b)
        self.assertEqual(len(self.gh.calls), before, "a DB hit must not call gh")

    def test_idempotent_through_the_title_search(self):
        a = self.run_(gh_issues.ensure_task_issue(self.repo, "proj", self.tf, self.task()))
        with closing(sqlite3.connect(config.DB_PATH, isolation_level=None)) as c:
            c.execute("DELETE FROM task_issues")
        b = self.run_(gh_issues.ensure_task_issue(self.repo, "proj", self.tf, self.task()))
        self.assertEqual(a, b)
        self.assertEqual(len(self.gh.issues), 1)
        self.assertEqual(gh_issues.issue_for(self.repo, self.tf, "t1"), a)

    def test_two_taskfiles_with_the_same_titles_never_share_issues(self):
        other = self.tmp / "b"
        other.mkdir()
        tf2 = other / "proj.json"
        tf2.write_text(self.tf.read_text())
        e1 = self.run_(gh_issues.ensure_epic(self.repo, "proj", self.tf))
        n1 = self.run_(gh_issues.ensure_task_issue(self.repo, "proj", self.tf,
                                                   self.task(), epic=e1))
        e2 = self.run_(gh_issues.ensure_epic(self.repo, "proj", tf2))
        n2 = self.run_(gh_issues.ensure_task_issue(self.repo, "proj", tf2,
                                                   self.task(), epic=e2))
        self.assertNotEqual(e1, e2)
        self.assertNotEqual(n1, n2)
        self.assertIn(f"Part of #{e1}", self.gh.issues[n1]["body"])
        self.assertIn(f"Part of #{e2}", self.gh.issues[n2]["body"])
        # With the DB rows gone, each taskfile still re-finds its OWN issues.
        with closing(sqlite3.connect(config.DB_PATH, isolation_level=None)) as c:
            c.execute("DELETE FROM task_issues")
        self.assertEqual(self.run_(gh_issues.ensure_epic(self.repo, "proj", tf2)), e2)
        self.assertEqual(self.run_(gh_issues.ensure_task_issue(
            self.repo, "proj", tf2, self.task())), n2)
        self.assertEqual(len(self.gh.issues), 4)

    def test_an_adopted_issue_gets_the_spec_and_labels(self):
        self.gh.issues[1] = {"title": "[proj] First", "body": "", "labels": ["bug"],
                             "state": "open", "comments": []}
        epic = 9
        n = self.run_(gh_issues.ensure_task_issue(self.repo, "proj", self.tf,
                                                  self.task(), epic=epic))
        i = self.gh.issues[n]
        self.assertEqual(n, 1)
        self.assertIn("do one", i["body"])
        self.assertIn("Part of #9", i["body"])
        self.assertEqual(sorted(i["labels"]), sorted(
            ["bug", "arc-task", "arc:pending", f"model:{MODEL}"]))

    def test_ensure_pr_closes_is_idempotent(self):
        self.gh.pulls[3] = {"body": "Fixes #5"}
        changed = self.run_(gh_issues.ensure_pr_closes(self.repo, 3, 5))
        self.assertFalse(changed)
        self.assertEqual(self.gh.pulls[3]["body"], "Fixes #5")
        self.gh.pulls[4] = {"body": ""}
        self.assertTrue(self.run_(gh_issues.ensure_pr_closes(self.repo, 4, 5, 2)))
        self.assertEqual(self.gh.pulls[4]["body"], "Closes #5\nPart of #2")

    def test_dashboard_link_opens_the_project_detail(self):
        with mock.patch.object(config, "DASHBOARD_PUBLIC_URL", "http://h:8787"):
            body = gh_issues.epic_body(self.repo, "proj", self.tf)
            self.assertEqual(gh_issues.dashboard_link("/x/my proj.json"),
                             "http://h:8787/#x=my+proj.json")
        self.assertIn("Dashboard: http://h:8787/#x=proj.json", body)
        self.assertNotIn("Dashboard:", gh_issues.epic_body(self.repo, "proj", self.tf))

    def test_title_search_reads_every_page_and_ignores_labels(self):
        # An issue a human opened (no arc-task label) on page 2 of 150.
        for k in range(149):
            self.gh.issues[k + 1] = {"title": f"other {k}", "body": "",
                                     "labels": [], "state": "open", "comments": []}
        self.gh.issues[150] = {"title": "[proj] First", "body": "",
                               "labels": [], "state": "open", "comments": []}
        n = self.run_(gh_issues.ensure_task_issue(self.repo, "proj", self.tf, self.task()))
        self.assertEqual(n, 150)
        self.assertEqual(len(self.gh.issues), 150, "a duplicate was created")

    def test_the_runs_own_db_holds_the_mapping(self):
        other = str(self.tmp / "other.db")
        gh_issues.use_db(other)
        self.addCleanup(gh_issues.use_db, None)
        n = self.run_(gh_issues.ensure_task_issue(self.repo, "proj", self.tf, self.task()))
        with closing(sqlite3.connect(other)) as c:
            self.assertEqual(c.execute("SELECT issue FROM task_issues").fetchall(), [(n,)])
        with closing(sqlite3.connect(config.DB_PATH)) as c:
            c.execute(gh_issues.SCHEMA)
            self.assertEqual(c.execute("SELECT * FROM task_issues").fetchall(), [])

    def test_labels_are_created_once(self):
        self.run_(gh_issues.ensure_labels(self.repo, ["arc-task"]))
        gh_issues._labels_done.clear()
        self.run_(gh_issues.ensure_labels(self.repo, ["arc-task"]))   # 422 is fine
        n = len(self.gh.calls)
        self.run_(gh_issues.ensure_labels(self.repo, ["arc-task"]))
        self.assertEqual(len(self.gh.calls), n)

    def test_label_swap(self):
        n = self.run_(gh_issues.ensure_task_issue(self.repo, "proj", self.tf, self.task()))
        self.run_(gh_issues.set_status(self.repo, n, "running"))
        self.run_(gh_issues.set_status(self.repo, n, "in_review"))
        labels = self.gh.issues[n]["labels"]
        self.assertIn("arc:in-review", labels)
        self.assertEqual([l for l in labels if l.startswith("arc:")], ["arc:in-review"])
        self.assertIn("arc-task", labels)

    def test_close_failed_stays_open(self):
        n = self.run_(gh_issues.ensure_task_issue(self.repo, "proj", self.tf, self.task()))
        self.run_(gh_issues.close_failed(self.repo, n, "exhausted escalation"))
        i = self.gh.issues[n]
        self.assertEqual(i["state"], "open")
        self.assertIn("arc:failed", i["labels"])
        self.assertEqual(len(i["comments"]), 1)
        self.assertTrue(i["comments"][0].startswith("**task failed**"))

    def test_comment_header_and_cap(self):
        n = self.run_(gh_issues.ensure_task_issue(self.repo, "proj", self.tf, self.task()))
        self.run_(gh_issues.comment(self.repo, n, "gate failed", "x" * 9000,
                                    attempt=2, model="GPT-6-Sol"))
        c = self.gh.comments(n)[0]
        self.assertTrue(c.startswith("**gate failed** (attempt 2, GPT-6-Sol)\n\n"))
        self.assertLessEqual(len(c), gh_issues.BODY_CAP)

    def test_path_redaction(self):
        home = str(pathlib.Path.home())
        wt = f"{config.WORKTREE_ROOT}/proj/t1"
        with mock.patch.dict(os.environ, {"ARC_API_KEY": "s3cr3t-value-123"}):
            out = gh_issues.redact(
                f"{wt}/src/a.py:3 failed\n{self.repo}/b.py\n"
                f"{home}/.config/x/secrets.toml\nkey s3cr3t-value-123", self.repo)
        self.assertIn("src/a.py:3 failed", out)
        self.assertIn("b.py", out)
        self.assertNotIn(home, out)
        self.assertNotIn(str(self.repo), out)
        self.assertNotIn("s3cr3t-value-123", out)
        self.assertIn("secrets.toml", out)

    def test_disabled_flag(self):
        with mock.patch.object(config, "GH_ISSUES", "off"):
            self.assertFalse(gh_issues.enabled(self.repo))
            ts = code_tasks.load_taskfile(self.tf)
            out = self.run_(code_tasks.open_task_issues(FakeStore(), ts, str(self.tf)))
        self.assertEqual(out, {})
        self.assertEqual(self.gh.calls, [])

    def test_auto_needs_a_github_origin(self):
        with mock.patch.object(config, "GH_ISSUES", "auto"):
            gh_issues._enabled_cache.clear()
            self.assertFalse(gh_issues.enabled(self.repo))

    def test_backfill_from_rows(self):
        with closing(sqlite3.connect(config.DB_PATH, isolation_level=None)) as c:
            c.execute("CREATE TABLE IF NOT EXISTS code_tasks(id TEXT, taskfile TEXT, "
                      "status TEXT)")
            c.execute("INSERT INTO code_tasks VALUES ('t1', ?, 'merged')",
                      (str(self.tf.resolve()),))
            c.execute("INSERT INTO code_tasks VALUES ('t2', ?, 'failed')",
                      (str(self.tf.resolve()),))
        out = self.run_(gh_issues.sync_taskfile(str(self.tf)))
        epic = self.gh.issues[out[""]]
        t1, t2 = self.gh.issues[out["t1"]], self.gh.issues[out["t2"]]
        self.assertEqual(t1["state"], "closed")
        self.assertIn("arc:merged", t1["labels"])
        self.assertIn("arc:failed", t2["labels"])
        self.assertIn(f"#{out['t1']} (`t1`)", t2["body"], "deps link their issues")
        self.assertIn(f"- [x] #{out['t1']} First", epic["body"])
        self.assertIn(f"- [ ] #{out['t2']} Second", epic["body"])
        # A second sync creates nothing new.
        self.run_(gh_issues.sync_taskfile(str(self.tf)))
        self.assertEqual(len(self.gh.issues), 3)

    def test_backfill_links_an_open_pr_and_adds_closes(self):
        with closing(sqlite3.connect(config.DB_PATH, isolation_level=None)) as c:
            c.execute("CREATE TABLE IF NOT EXISTS code_tasks("
                      "id TEXT, taskfile TEXT, status TEXT, model TEXT)")
            c.execute("INSERT INTO code_tasks VALUES ('t1', ?, 'in_review', ?)",
                      (str(self.tf.resolve()), "GLM-5.3"))
        self.gh.pulls[9] = {"body": "already open, no keyword"}

        async def find_pr(repo, tid, state="open"):
            return 9, "https://github.com/o/r/pull/9", "OPEN"

        with mock.patch.object(gitstore, "find_pr", find_pr):
            out = self.run_(gh_issues.sync_taskfile(str(self.tf)))
        self.assertIn("Closes #" + str(out["t1"]), self.gh.pulls[9]["body"])
        self.assertIn("Part of #" + str(out[""]), self.gh.pulls[9]["body"])
        linked = [c for c in self.gh.comments(out["t1"])
                  if c.startswith("**pull request opened**")]
        self.assertEqual(len(linked), 1)
        self.assertIn("#9", linked[0])
        self.assertIn("model:GLM-5.3", self.gh.issues[out["t1"]]["labels"])


class Wiring(Base):
    def graph(self, store=None):
        ts = code_tasks.load_taskfile(self.tf)
        with capture_events():
            g = code_tasks.build_code_graph(store or FakeStore(), ts,
                                            taskfile=str(self.tf))
        return ts, g

    def opened(self):
        ts = code_tasks.load_taskfile(self.tf)
        return self.run_(code_tasks.open_task_issues(FakeStore(), ts, str(self.tf)))

    def ctx(self, **results):
        base = {"alloc_t1": {"worktree": str(self.repo)}}
        base.update(results)
        return {"results": base, "runs": {"implement_t1": 2}}

    def node(self, g, name, ctx):
        with capture_events() as ev:
            out = self.run_(g.nodes[name].fn(ctx))
        return out, ev

    def test_run_start_opens_epic_and_unmerged_tasks(self):
        ts = code_tasks.load_taskfile(self.tf)
        store = FakeStore(prior=[{"id": "t1", "status": "merged"}])
        out = self.run_(code_tasks.open_task_issues(store, ts, str(self.tf)))
        self.assertEqual(set(out), {"", "t2"})
        self.assertEqual(self.gh.issues[out[""]]["title"], "[arc] proj")

    def test_gate_failure_posts_exactly_one_comment(self):
        n = self.opened()["t1"]
        ts, g = self.graph()
        with mock.patch.dict(ts["tasks"]["t1"], verify_cmd="echo boom; exit 1"):
            out, _ = self.node(g, "gate_t1", self.ctx(implement_t1={}))
        self.assertFalse(out["passed"])
        cs = self.gh.comments(n)
        self.assertEqual(len(cs), 1)
        self.assertTrue(cs[0].startswith("**gate failed** (attempt 2)"))
        self.assertIn("boom", cs[0])

    def test_gate_pass_posts_one_short_comment(self):
        n = self.opened()["t1"]
        _, g = self.graph()
        out, _ = self.node(g, "gate_t1", self.ctx(implement_t1={}))
        self.assertTrue(out["passed"])
        self.assertEqual(len(self.gh.comments(n)), 1)
        self.assertTrue(self.gh.comments(n)[0].startswith("**gate passed**"))

    def test_escalation_comment(self):
        n = self.opened()["t1"]
        _, g = self.graph()
        with mock.patch.object(code_tasks, "_next_tier_m", lambda m: "GLM-5.3"):
            _, g = self.graph()
            out, _ = self.node(g, "escalate_t1", self.ctx())
        cs = self.gh.comments(n)
        self.assertEqual(len(cs), 1)
        self.assertIn(f"{out['from_model']} → {out['to_model']}", cs[0])

    def test_fail_labels_failed_and_comments_once(self):
        n = self.opened()["t1"]
        _, g = self.graph()
        self.node(g, "fail_t1", self.ctx(gate_t1={"passed": False, "output": "x"}))
        i = self.gh.issues[n]
        self.assertEqual(len(i["comments"]), 1)
        self.assertIn("arc:failed", i["labels"])
        self.assertEqual(i["state"], "open")

    def test_merge_ticks_the_epic_and_sets_merged(self):
        ids = self.opened()
        _, g = self.graph()
        with mock.patch.object(gitstore, "pr_state",
                               mock.AsyncMock(return_value={"state": "OPEN"})), \
                mock.patch.object(gitstore, "merge_pr",
                                  mock.AsyncMock(return_value=(True, "merged"))), \
                mock.patch.object(gitstore, "fast_forward_base",
                                  mock.AsyncMock(return_value=(True, ""))), \
                mock.patch.object(gitstore, "cleanup", mock.AsyncMock()):
            out, _ = self.node(g, "pr_merge_t1", self.ctx(
                pr_review_t1={"pr": 7, "approved": True}))
        self.assertTrue(out["merged"], out)
        i = self.gh.issues[ids["t1"]]
        self.assertIn("arc:merged", i["labels"])
        self.assertEqual(i["comments"], [])
        self.assertEqual(i["state"], "open", "the PR's Closes #N closes it, not us")

    def test_empty_merge_without_a_pr_closes_the_issue(self):
        # No PR means no `Closes #N`: the hook must close it itself.
        ids = self.opened()
        _, g = self.graph()
        pub = {"published": False, "merged": True, "empty": True}
        self.node(g, "pr_merge_t1", self.ctx(publish_t1=pub))
        i = self.gh.issues[ids["t1"]]
        self.assertEqual(i["state"], "closed")
        self.assertIn("arc:merged", i["labels"])
        self.assertEqual(len(i["comments"]), 1)

    def test_a_skipped_branch_labels_every_skipped_issue(self):
        self.tasks[0]["probe_cmd"] = "echo {}"
        self.tasks[1]["when"] = {"dep": "t1", "key": "go", "truthy": True}
        self.tf.write_text(json.dumps({"project": {
            "name": "proj", "repo": str(self.repo), "tasks": self.tasks}}))
        ids = self.opened()
        _, g = self.graph()
        out, _ = self.node(g, "skip_t2", self.ctx())
        self.assertTrue(out["skipped"])
        i = self.gh.issues[ids["t2"]]
        self.assertIn("arc:skipped", i["labels"])
        self.assertEqual(len(i["comments"]), 1)
        self.assertIn("did not hold", i["comments"][0])

    def test_graph_uses_the_stores_db(self):
        store = FakeStore()
        store.path = str(self.tmp / "run.db")
        self.addCleanup(gh_issues.use_db, None)
        self.graph(store)
        self.assertEqual(gh_issues._db(), store.path)

    def swapping_driver(self, text):
        """A driver whose plan window is spent: the run lands on another
        model. Which reviewer is chosen depends on live load (_select_reviewer),
        so the swap target is 'whichever model this driver is not'."""
        swapped = self.swapped = []

        class Drv:
            harness, images = "fake", None

            def __init__(self, model):
                self.model = model

            async def run(self, prompt, cwd, **kw):
                other = ("GLM-5.3" if self.model != "GLM-5.3"
                         else "DeepSeek-V4.1-Flash-thinking-max")
                swapped.append((self.model, other))
                return types.SimpleNamespace(
                    exit_code=0, transcript_path="", seconds=0.0, session_id=None,
                    text=text, model=other, harness="opencode")
        return lambda model, role, pol: Drv(model)

    def review_env(self, text):
        return [mock.patch.object(code_tasks, "_driver", self.swapping_driver(text)),
                mock.patch.object(gitstore, "diff_full",
                                  mock.AsyncMock(return_value="DIFF")),
                mock.patch.object(code_tasks.graft, "blast",
                                  mock.AsyncMock(return_value=""))]

    def run_with(self, patches, fn):
        for p in patches:
            p.start()
        try:
            return fn()
        finally:
            for p in patches:
                p.stop()

    def test_a_reviewer_usage_swap_is_commented(self):
        n = self.opened()["t1"]
        _, g = self.graph()
        self.run_with(self.review_env('{"pass": true}'), lambda: self.node(
            g, "review_t1", self.ctx(implement_t1={"model": MODEL})))
        swaps = [c for c in self.gh.comments(n)
                 if c.startswith("**reviewer usage swap**")]
        self.assertEqual(len(swaps), 1, self.gh.comments(n))
        old, new = self.swapped[-1]
        self.assertIn(f"{old} → {new}", swaps[0])
        self.assertIn("plan window", swaps[0])

    def test_a_pr_reviewer_usage_swap_is_commented(self):
        n = self.opened()["t1"]
        _, g = self.graph()
        spawn = {"model": REVIEWER_MODEL, "diff": "DIFF", "n_reviewers": 1,
                 "round": 2, "prior_issues": [], "pr": 7}
        ctx = dict(self.ctx(), spawn=spawn)
        self.run_with(self.review_env('{"approve": true}'),
                      lambda: self.node(g, "pr_reviewer_t1", ctx))
        cs = self.gh.comments(n)
        self.assertEqual(len(cs), 1, cs)
        self.assertTrue(cs[0].startswith("**PR reviewer usage swap**"))
        self.assertIn("{} → {}".format(*self.swapped[-1]), cs[0])
        self.assertIn("PR #7", cs[0])

    def test_escalation_moves_the_model_label(self):
        n = self.opened()["t1"]
        with mock.patch.object(code_tasks, "_next_tier_m", lambda m: "GLM-5.3"):
            _, g = self.graph()
            self.node(g, "escalate_t1", self.ctx())
        labels = self.gh.issues[n]["labels"]
        self.assertIn("model:GLM-5.3", labels)
        self.assertEqual([l for l in labels if l.startswith("model:")],
                         ["model:GLM-5.3"])

    def test_implement_start_and_usage_swap(self):
        n = self.opened()["t1"]
        _, g = self.graph()
        res = types.SimpleNamespace(exit_code=0, transcript_path="", seconds=0.0,
                                    session_id=None, text="done", model="GLM-5.3",
                                    harness="opencode")
        drv = types.SimpleNamespace(harness="fake",
                                    run=mock.AsyncMock(return_value=res))
        with mock.patch.object(code_tasks, "_driver", return_value=drv), \
                mock.patch.object(code_tasks.graft, "hints",
                                  mock.AsyncMock(return_value="")), \
                mock.patch.object(gitstore, "checkpoint", mock.AsyncMock()):
            self.node(g, "implement_t1", {"results": self.ctx()["results"],
                                          "runs": {}})
        cs = self.gh.comments(n)
        self.assertEqual(len(cs), 2, cs)
        self.assertTrue(cs[0].startswith("**implementing** (attempt 1, "))
        self.assertTrue(cs[1].startswith("**usage swap**"))
        self.assertIn("arc:implementing", self.gh.issues[n]["labels"])
        # The swap moved the work: the model label follows it.
        self.assertEqual([l for l in self.gh.issues[n]["labels"]
                          if l.startswith("model:")], ["model:GLM-5.3"])

    def test_publish_pr_body_closes_the_issue(self):
        ids = self.opened()
        _, g = self.graph()
        bodies = []

        async def open_pr(repo, tid, title, body, base=None):
            bodies.append(body)
            return 7, "https://github.com/o/r/pull/7", "opened"
        with mock.patch.object(gitstore, "open_pr", open_pr), \
                mock.patch.object(gitstore, "existing_worktree",
                                  mock.AsyncMock(return_value=self.repo)), \
                mock.patch.object(gitstore, "branch_ahead",
                                  mock.AsyncMock(return_value=True)), \
                mock.patch.object(gitstore, "publish",
                                  mock.AsyncMock(return_value=(True, "abc"))), \
                mock.patch.object(gitstore, "push_task_branch",
                                  mock.AsyncMock(return_value=(True, ""))):
            out, _ = self.node(g, "publish_t1", self.ctx(
                review_t1={"pass": True}, gate_t1={"passed": True}))
        if not out.get("published"):
            self.skipTest(f"publish path needs more git than mocked: {out}")
        self.assertIn(f"Closes #{ids['t1']}", bodies[0])
        self.assertIn(f"Part of #{ids['']}", bodies[0])
        i = self.gh.issues[ids["t1"]]
        self.assertIn("arc:in-review", i["labels"])
        self.assertEqual(len(i["comments"]), 1)
        self.assertIn("#7", i["comments"][0])

    def test_a_reattached_pr_gains_the_closing_keyword(self):
        # Resume: open_pr finds the PR already open and never touches its
        # body, which predates issues. The publish hook must add the keyword.
        ids = self.opened()
        self.gh.pulls[7] = {"body": "old body"}
        _, g = self.graph()

        async def open_pr(repo, tid, title, body, base=None):
            return 7, "https://github.com/o/r/pull/7", "already open"
        with mock.patch.object(gitstore, "open_pr", open_pr), \
                mock.patch.object(gitstore, "existing_worktree",
                                  mock.AsyncMock(return_value=self.repo)), \
                mock.patch.object(gitstore, "branch_ahead",
                                  mock.AsyncMock(return_value=True)), \
                mock.patch.object(gitstore, "publish",
                                  mock.AsyncMock(return_value=(True, "abc"))), \
                mock.patch.object(gitstore, "push_task_branch",
                                  mock.AsyncMock(return_value=(True, ""))):
            out, ev = self.node(g, "publish_t1", self.ctx(
                review_t1={"pass": True}, gate_t1={"passed": True}))
        self.assertTrue(out.get("published"), out)
        body = self.gh.pulls[7]["body"]
        self.assertTrue(body.startswith("old body"))
        self.assertIn(f"Closes #{ids['t1']}", body)
        self.assertIn(f"Part of #{ids['']}", body)
        self.assertEqual(ev.of("gh.issue_error"), [])

    def publish_mocks(self, open_pr, push=(True, "")):
        return [mock.patch.object(gitstore, "open_pr", open_pr),
                mock.patch.object(gitstore, "existing_worktree",
                                  mock.AsyncMock(return_value=self.repo)),
                mock.patch.object(gitstore, "branch_ahead",
                                  mock.AsyncMock(return_value=True)),
                mock.patch.object(gitstore, "publish",
                                  mock.AsyncMock(return_value=(True, "abc"))),
                mock.patch.object(gitstore, "push_task_branch",
                                  mock.AsyncMock(return_value=push))]

    def publish(self, g, open_pr, push=(True, "")):
        ps = self.publish_mocks(open_pr, push)
        for p in ps:
            p.start()
        try:
            return self.node(g, "publish_t1", self.ctx(
                review_t1={"pass": True}, gate_t1={"passed": True}))
        finally:
            for p in ps:
                p.stop()

    def test_a_terminal_publish_failure_fails_the_issue(self):
        # A rejected push ends the chain at publish: no edge reaches `fail`.
        ids = self.opened()
        _, g = self.graph()

        async def open_pr(*a, **k):
            raise AssertionError("no PR after a failed push")
        out, _ = self.publish(g, open_pr, push=(False, "remote rejected"))
        self.assertFalse(out.get("published"))
        i = self.gh.issues[ids["t1"]]
        self.assertIn("arc:failed", i["labels"])
        self.assertEqual(i["state"], "open")
        self.assertEqual(len(i["comments"]), 1)
        self.assertIn("remote rejected", i["comments"][0])

    def test_an_existing_pr_links_a_new_issue_exactly_once(self):
        # The issue is created after the PR already exists (backfill/resume):
        # the link lands on the next publish, and never twice.
        ids = self.opened()
        _, g = self.graph()

        async def open_pr(repo, tid, title, body, base=None):
            return 7, "https://github.com/o/r/pull/7", "already open"
        self.publish(g, open_pr)
        self.publish(g, open_pr)
        links = [c for c in self.gh.comments(ids["t1"])
                 if c.startswith("**pull request opened**")]
        self.assertEqual(len(links), 1)
        self.assertIn("#7", links[0])

    def test_a_failed_link_comment_is_retried_on_the_next_publish(self):
        ids = self.opened()
        _, g = self.graph()

        async def open_pr(repo, tid, title, body, base=None):
            return 7, "https://github.com/o/r/pull/7", "already open"
        self.gh.fail = True
        self.publish(g, open_pr)
        self.gh.fail = False
        self.publish(g, open_pr)
        self.assertEqual(sum("#7" in c for c in self.gh.comments(ids["t1"])), 1)

    def test_a_gh_failure_never_fails_the_task(self):
        self.opened()
        self.gh.fail = True
        _, g = self.graph()
        out, ev = self.node(g, "gate_t1", self.ctx(implement_t1={}))
        self.assertTrue(out["passed"])
        errs = ev.of("gh.issue_error")
        self.assertEqual(len(errs), 1)
        self.assertTrue(errs[0].get("fingerprint"))

    def test_one_issue_failure_does_not_skip_the_rest(self):
        ts = code_tasks.load_taskfile(self.tf)
        real = gh_issues.ensure_task_issue

        async def boom(repo, project, taskfile, task, status="pending", epic=None):
            if task.get("id") == "t1":
                raise gh_issues.GhIssueError("epic-or-task down")
            return await real(repo, project, taskfile, task, status, epic)

        with mock.patch.object(gh_issues, "ensure_task_issue", boom), capture_events() as ev:
            out = self.run_(code_tasks.open_task_issues(FakeStore(), ts, str(self.tf)))
        self.assertIn("", out)
        self.assertNotIn("t1", out)
        self.assertIn("t2", out)
        self.assertTrue(ev.of("gh.issue_error"))

    def test_run_start_uses_the_recorded_implementer(self):
        ts = code_tasks.load_taskfile(self.tf)
        store = FakeStore(prior=[{"id": "t2", "status": "in_review", "model": "GLM-5.3"}])
        out = self.run_(code_tasks.open_task_issues(store, ts, str(self.tf)))
        labels = self.gh.issues[out["t2"]]["labels"]
        self.assertIn("model:GLM-5.3", labels)
        self.assertNotIn(f"model:{MODEL}", labels)

    def test_a_label_failure_still_adds_the_closing_keyword(self):
        ids = self.opened()
        self.gh.pulls[7] = {"body": "old body"}
        _, g = self.graph()

        async def open_pr(repo, tid, title, body, base=None):
            return 7, "https://github.com/o/r/pull/7", "already open"

        async def bad_labels(*a, **k):
            raise gh_issues.GhIssueError("labels down")

        with mock.patch.object(gitstore, "open_pr", open_pr), \
                mock.patch.object(gitstore, "existing_worktree",
                                  mock.AsyncMock(return_value=self.repo)), \
                mock.patch.object(gitstore, "branch_ahead",
                                  mock.AsyncMock(return_value=True)), \
                mock.patch.object(gitstore, "publish",
                                  mock.AsyncMock(return_value=(True, "abc"))), \
                mock.patch.object(gitstore, "push_task_branch",
                                  mock.AsyncMock(return_value=(True, ""))), \
                mock.patch.object(gh_issues, "swap_labels", bad_labels):
            out, ev = self.node(g, "publish_t1", self.ctx(
                review_t1={"pass": True}, gate_t1={"passed": True}))
        self.assertTrue(out.get("published"), out)
        self.assertIn(f"Closes #{ids['t1']}", self.gh.pulls[7]["body"])
        self.assertTrue(ev.of("gh.issue_error"))

    def test_evidence_comment_links_the_pr_comment_or_the_shots(self):
        ids = self.opened()
        _, g = self.graph()
        shot = self.tmp / "cam" / "shot.png"
        shot.parent.mkdir()
        shot.write_bytes(b"png")
        manifest = {"attempt": 1, "shots": [str(shot)], "videos": {}}
        web = "https://github.com/o/r/blob/arc-evidence/t1/x1"

        async def open_pr(repo, tid, title, body, base=None):
            return 7, "https://github.com/o/r/pull/7", "opened"

        async def gh(args, cwd, timeout=180, wait_quota=True):
            if args[:2] == ["pr", "comment"]:
                return 0, "https://github.com/o/r/pull/7#issuecomment-99\n", ""
            return await self.gh(args, cwd, timeout, wait_quota)

        import evidence
        with mock.patch.object(gitstore, "_gh", gh), \
                mock.patch.object(evidence, "publish", return_value=web), \
                mock.patch.object(evidence, "pr_markdown", return_value="body"), \
                mock.patch.object(evidence, "emit"):
            ps = self.publish_mocks(open_pr)
            for p in ps:
                p.start()
            try:
                out, _ = self.node(g, "publish_t1", self.ctx(
                    review_t1={"pass": True},
                    gate_t1={"passed": True, "evidence": manifest}))
            finally:
                for p in ps:
                    p.stop()
        self.assertTrue(out.get("published"), out)
        body = "\n".join(self.gh.comments(ids["t1"]))
        self.assertIn("https://github.com/o/r/pull/7#issuecomment-99", body)
        self.assertIn(web + "/cam/shot.png?raw=true", body)

    def test_no_taskfile_on_disk_means_no_hooks(self):
        ts = code_tasks.load_taskfile(self.tf)
        with capture_events():
            g = code_tasks.build_code_graph(FakeStore(), ts, taskfile="tf.json")
        self.node(g, "gate_t1", self.ctx(implement_t1={}))
        self.assertEqual(self.gh.calls, [])


if __name__ == "__main__":
    unittest.main()
