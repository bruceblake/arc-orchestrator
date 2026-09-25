"""Agent IDs are stable, and agents (and the operator) can reach each other.

Each test here failed before the fix it guards:

- a driver run is named `<task>-x3` / `<task>-pr2`; that suffix leaked into
  board authors and channels, so a handoff was posted to `task:<task>-x3`
  (read by nobody) and every attempt counted as a separate task;
- the prompt's `./py main.py board post` only works in a worktree of THIS
  repo, and there it wrote `<worktree>/orchestrator.db`, not the fleet DB;
- the dashboard Messages tab posts in the open channel, and an operator note
  in `task:<id>` without an @mention never reached the agent's prompt;
- a `claim` line an agent wrote was stored as a message but took no lease.
"""
import json
import os
import shlex
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401  (redirects the event log and DB before import)
import agentboard
import board
import config

P = "prison"


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for name, val in (("DB_PATH", str(root / "board.db")),
                          ("BOARD_DIR", root / "boards"),
                          ("WORKTREE_ROOT", str(root / "worktrees"))):
            self.addCleanup(setattr, config, name, getattr(config, name))
            setattr(config, name, val)
        self.root = root
        self.wt = root / "worktrees" / P / "doors"
        self.wt.mkdir(parents=True)

    def msgs(self, **where):
        return [m for m in agentboard._select(P)
                if all(m.get(k) == v for k, v in where.items())]


class StableIds(Case):
    def test_attempt_suffix_never_becomes_part_of_the_agent_id(self):
        self.assertEqual(agentboard.agent_id("doors-x3", "reviewer"), "doors/reviewer")
        self.assertEqual(agentboard.agent_id("doors-pr2", "pr_reviewer"),
                         "doors/pr-reviewer")
        self.assertEqual(agentboard.canonical_channel("dm:doors-x1/implementer"),
                         "dm:doors/implementer")

    def test_a_real_task_id_that_ends_like_a_suffix_is_kept(self):
        c = sqlite3.connect(config.DB_PATH)
        with c:
            c.execute("CREATE TABLE code_tasks(id TEXT, taskfile TEXT, worktree TEXT)")
            c.execute("INSERT INTO code_tasks VALUES('probe-x2','t.json','')")
        c.close()
        self.assertEqual(agentboard.canonical_task("probe-x2"), "probe-x2")
        self.assertEqual(agentboard.canonical_task("probe-x2-x5"), "probe-x2")

    def test_different_project_task_row_does_not_stop_canonicalizing_attempt(self):
        # A code_tasks row id probe-x2 in a DIFFERENT project/taskfile must NOT
        # stop task probe attempt 2 in this project from canonicalizing to probe.
        c = sqlite3.connect(config.DB_PATH)
        with c:
            c.execute("CREATE TABLE IF NOT EXISTS code_tasks(id TEXT, taskfile TEXT, worktree TEXT)")
            c.execute("INSERT INTO code_tasks VALUES('probe-x2','other.json','')")
        c.close()
        self.assertEqual(agentboard.canonical_task("probe-x2", project=P), "probe")

    def test_a_real_task_id_that_ends_like_a_suffix_is_kept_in_this_project(self):
        # A code_tasks row id probe-x2 in THIS project must still be kept.
        c = sqlite3.connect(config.DB_PATH)
        with c:
            c.execute("CREATE TABLE IF NOT EXISTS code_tasks(id TEXT, taskfile TEXT, worktree TEXT)")
            c.execute(f"INSERT INTO code_tasks VALUES('probe-x2', '{P}.json', '')")
        c.close()
        self.assertEqual(agentboard.canonical_task("probe-x2", project=P), "probe-x2")
        self.assertEqual(agentboard.canonical_task("probe-x2-x5", project=P), "probe-x2")

    def test_a_driver_board_post_lands_in_the_task_channel_under_the_stable_id(self):
        # drivers.py posts with its run name (task_id=f"{tid}-x{attempt}").
        board.post(self.wt, task="doors-x3", role="reviewer", model="GPT-6-Sol",
                   harness="codex", kind="handoff", body="swapped", project=P)
        m = self.msgs(kind="handoff")[0]
        self.assertEqual(m["author"], "doors/reviewer")
        self.assertEqual(m["author_task"], "doors")
        self.assertEqual(m["channel"], "task:doors")
        self.assertEqual(m["author_model"], "GPT-6-Sol")

    def test_a_usage_swap_handoff_reaches_the_role_that_continues(self):
        import drivers
        drivers.post_handoff(self.wt, "doors-x4", "pr_reviewer", "GPT-6-Sol",
                             "codex", "usage limit; continuing on cursor")
        d = agentboard.digest_for(P, task="doors", role="pr-reviewer",
                                  model="Cursor-Grok-4.7")
        self.assertIn("usage limit; continuing on cursor", d)

    def test_health_counts_one_task_not_one_per_attempt(self):
        # Rows written by the old code: author_task carried the suffix.
        for n in (1, 2, 3):
            with agentboard._lock:
                agentboard._insert(agentboard._db(P), {
                    "id": f"old{n}", "project": P, "channel": f"task:doors-x{n}",
                    "ts": agentboard.time.time(), "author": f"doors-x{n}/reviewer",
                    "author_model": "", "author_role": "reviewer",
                    "author_task": f"doors-x{n}", "kind": "handoff", "body": "b",
                    "mentions": [], "reply_to": None, "refs": {}, "state": ""})
        agentboard.claim(P, task="doors", author="doors/implementer",
                         paths=["a.gd"])
        h = agentboard.board_health(P)
        self.assertEqual(h["tasks"], 1)
        self.assertEqual(h["claim_share"], 1.0)


class Delivery(Case):
    def test_operator_note_in_the_task_channel_reaches_the_agent(self):
        # What the dashboard Messages tab sends with `task:doors` open.
        agentboard.post(P, author="operator", channel="task:doors", kind="note",
                        body="use the new lock API", author_role="operator")
        d = agentboard.digest_for(P, task="doors", role="implementer",
                                  model="GLM-5.3", mark_seen=True)
        self.assertIn("use the new lock API", d)
        again = agentboard.digest_for(P, task="doors", role="implementer",
                                      model="GLM-5.3", mark_seen=True)
        self.assertNotIn("use the new lock API", again.split("HOW TO POST")[0])

    def test_the_tasks_own_orchestrator_posts_are_not_echoed_back(self):
        agentboard.post(P, author="doors/orchestrator", channel="task:doors",
                        kind="status", body="implementing: attempt 2")
        d = agentboard.digest_for(P, task="doors", role="implementer", model="m")
        self.assertNotIn("implementing: attempt 2", d)

    def test_a_dm_to_the_task_reaches_each_of_its_agents(self):
        agentboard.post(P, author="locks/implementer", channel="dm:doors",
                        body="which signal do you emit?")
        d = agentboard.digest_for(P, task="doors", role="reviewer", model="m")
        self.assertIn("which signal do you emit?", d)

    def test_the_digest_names_the_readers_own_id(self):
        d = agentboard.digest_for(P, task="doors-x2", role="implementer", model="m")
        self.assertIn("your agent ID is doors/implementer", d)

    def test_dm_and_mention_with_attempt_suffix_delivered_to_implementer(self):
        # dm:doors-x2 and a mention doors-x2 are delivered to doors/implementer
        agentboard.post(P, author="operator", channel="dm:doors-x2",
                        body="check the hinges")
        agentboard.post(P, author="operator", channel="project",
                        body="pinging @doors-x2 please check")
        d = agentboard.digest_for(P, task="doors", role="implementer", model="GLM-5.3")
        self.assertIn("check the hinges", d)
        self.assertIn("pinging @doors-x2 please check", d)


class CliFromAnyWorktree(Case):
    """The exact command the prompt hands an agent, run the way a harness
    would: from a worktree of ANOTHER repo (no ./py, no main.py), with an
    environment that does not name the fleet DB."""

    def _prompt_command(self, agent, channel, body):
        howto = agentboard.how_to_post(P, agent)
        cmd = howto.split("from any directory: `", 1)[1].split("`", 1)[0]
        return (cmd.replace("dm:<task>/<role>", channel)
                   .replace('"<body>"', shlex.quote(body)))

    def test_a_post_from_a_game_worktree_lands_in_the_fleet_db(self):
        game_wt = self.root / "worktrees" / P / "locks"
        game_wt.mkdir(parents=True)
        self.assertFalse((game_wt / "py").exists())
        env = {k: v for k, v in os.environ.items() if k != "ARC_DB_PATH"}
        env["ARC_EVENTS_LOG"] = str(self.root / "events.jsonl")
        cmd = self._prompt_command("locks/implementer", "dm:doors/implementer",
                                   "is door.open() async?")
        r = subprocess.run(cmd, shell=True, cwd=game_wt, env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        self.assertFalse((game_wt / "orchestrator.db").exists())
        m = self.msgs(channel="dm:doors/implementer")
        self.assertEqual(len(m), 1, "the post must land in the DB the fleet reads")
        self.assertEqual(m[0]["author"], "locks/implementer")
        d = agentboard.digest_for(P, task="doors", role="implementer", model="m")
        self.assertIn("is door.open() async?", d)


class ClaimLines(Case):
    def test_a_claim_line_takes_a_real_lease(self):
        (self.wt / ".arc").mkdir()
        (self.wt / ".arc" / "board.jsonl").write_text(json.dumps(
            {"kind": "claim", "body": "claiming door.gd",
             "refs": {"paths": ["scripts/door.gd"]}}) + "\n")
        agentboard.ingest_file(P, self.wt, task="doors", role="implementer",
                               model="m")
        live = agentboard.claims(P)
        self.assertEqual([c["paths"] for c in live], [["scripts/door.gd"]])
        self.assertEqual(live[0]["author"], "doors/implementer")
        other = agentboard.digest_for(P, task="locks", role="implementer",
                                      model="m", files_hint=["scripts/door.gd"])
        self.assertIn("doors/implementer holds scripts/door.gd", other)

    def test_ingest_line_mentioning_driver_run_accepted_not_unknown(self):
        # a .arc/board.jsonl line mentioning @doors-x1/implementer is ingested, not rejected as unknown
        c = sqlite3.connect(config.DB_PATH)
        with c:
            c.execute("CREATE TABLE IF NOT EXISTS code_tasks(id TEXT, taskfile TEXT, worktree TEXT)")
            c.execute(f"INSERT INTO code_tasks VALUES('doors', '{P}.json', '{self.wt}')")
        c.close()
        (self.wt / ".arc").mkdir(exist_ok=True)
        line = json.dumps({"body": "ping @doors-x1/implementer ready for review"})
        (self.wt / ".arc" / "board.jsonl").write_text(line + "\n")
        n = agentboard.ingest_file(P, self.wt, task="doors", role="reviewer",
                                   model="m")
        self.assertEqual(n, 1)
        errs = self.msgs(kind="error")
        self.assertEqual(errs, [], f"unexpected errors: {errs}")
        msgs = self.msgs(author="doors/reviewer")
        self.assertTrue(any("ready for review" in m["body"] for m in msgs))
        stored = [m for m in msgs if "ready for review" in m["body"]][0]
        self.assertEqual(stored["mentions"], ["doors/implementer"])


class DashboardAgentsView(Case):
    def test_recent_runs_carry_the_stable_agent_id(self):
        import dashboard

        class S:
            def harness_runs_all(self, limit):
                return [{"task_id": "doors-x3", "role": "reviewer",
                         "model": "GPT-6-Sol", "harness": "codex",
                         "attempt": 3, "exit_code": 0, "created_at": None}]
        rows = dashboard._recent_agent_runs(S())
        self.assertEqual(rows[0]["agent"], "doors/reviewer")


if __name__ == "__main__":
    unittest.main()
