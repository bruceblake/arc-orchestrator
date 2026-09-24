"""The agent coordination board: typed, addressed, claimed, digested."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

import helpers  # noqa: F401  (redirects the event log and DB before import)
import agentboard
import board
import config

P = "prison"


class BoardCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for name, val in (("DB_PATH", str(root / "board.db")),
                          ("BOARD_DIR", root / "boards"),
                          ("WORKTREE_ROOT", str(root / "worktrees"))):
            self.addCleanup(setattr, config, name, getattr(config, name))
            setattr(config, name, val)
        self.wt = root / "worktrees" / P / "doors"
        self.wt.mkdir(parents=True)


class PostAndThread(BoardCase):
    def test_replies_nest_under_their_parent_oldest_first(self):
        q = agentboard.post(P, author="doors/implementer", kind="question",
                            body="what is the door api?")
        agentboard.post(P, author="guards/implementer", body="unrelated")
        a = agentboard.post(P, author="locks/implementer", kind="answer",
                            body="toggle(id)", reply_to=q)
        roots = agentboard.thread(P)
        self.assertEqual([r["id"] for r in roots][0], q)
        self.assertEqual(len(roots), 2)
        self.assertEqual([r["id"] for r in roots[0]["replies"]], [a])
        # The answer closed the question.
        self.assertEqual(roots[0]["state"], "answered")

    def test_thread_filters_by_channel_and_kind(self):
        agentboard.post(P, author="captain", channel="captain", kind="decision", body="d")
        agentboard.post(P, author="captain", kind="note", body="n")
        self.assertEqual(len(agentboard.thread(P, channel="captain")), 1)
        self.assertEqual([m["body"] for m in agentboard.thread(P, kinds=["note"])], ["n"])

    def test_body_is_capped(self):
        agentboard.post(P, author="operator", body="x" * (config.BOARD_BODY_MAX + 50))
        self.assertEqual(len(agentboard.thread(P)[0]["body"]), config.BOARD_BODY_MAX)

    def test_unknown_kind_and_channel_fall_back(self):
        agentboard.post(P, author="operator", kind="bogus", channel="nowhere", body="b")
        m = agentboard.thread(P)[0]
        self.assertEqual((m["kind"], m["channel"]), ("note", "project"))

    def test_never_raises_on_a_broken_db_path(self):
        config.DB_PATH = str(Path(self.tmp.name) / "no" / "such" / "dir" / "x.db")
        mid = agentboard.post(P, author="operator", body="lost")
        self.assertTrue(mid)
        self.assertEqual(agentboard.thread(P), [])
        self.assertEqual(agentboard.inbox(P, "doors/implementer"), [])
        self.assertEqual(agentboard.claims(P), [])
        self.assertIsInstance(agentboard.digest_for(P, task="doors", role="implementer",
                                                    model="m"), str)
        self.assertEqual(agentboard.ingest_file(P, self.wt, task="doors",
                                                role="implementer", model="m"), 0)


class Mentions(BoardCase):
    def test_parsed_from_the_body(self):
        self.assertEqual(
            agentboard.parse_mentions("ask @doors and @locks/reviewer, cc @GLM-5.3. "
                                      "@captain @operator @all mail a@b.c"),
            ["doors", "locks/reviewer", "GLM-5.3", "captain", "operator", "all"])

    def test_inbox_sees_mentions_dms_all_and_open_questions(self):
        me = "doors/implementer"
        agentboard.post(P, author="locks/implementer", body="hey @doors")
        agentboard.post(P, author="locks/implementer", body="for @GLM-5.3")
        agentboard.post(P, author="captain", body="@all stand-up")
        agentboard.post(P, author="captain", channel=f"dm:{me}", body="direct")
        agentboard.post(P, author="doors/reviewer", channel="task:doors",
                        kind="question", body="why this?")
        agentboard.post(P, author="locks/implementer", body="not for you")
        agentboard.post(P, author=me, body="my own @doors post")
        bodies = [m["body"] for m in agentboard.inbox(P, me, model="GLM-5.3")]
        self.assertEqual(bodies, ["hey @doors", "for @GLM-5.3", "@all stand-up",
                                  "direct", "why this?"])
        self.assertNotIn("for @GLM-5.3",
                         [m["body"] for m in agentboard.inbox(P, me)])


class Claims(BoardCase):
    def test_prefix_overlap_is_reported_as_a_conflict(self):
        first = agentboard.claim(P, task="doors", author="doors/implementer",
                                 paths=["scripts/systems/"])
        self.assertEqual(first.conflicts, [])
        second = agentboard.claim(P, task="locks", author="locks/implementer",
                                  paths=["scripts/systems/x.gd", "README.md"])
        self.assertEqual([c["author"] for c in second.conflicts], ["doors/implementer"])
        third = agentboard.claim(P, task="ui", author="ui/implementer",
                                 paths=["scripts/sys"])
        self.assertEqual(third.conflicts, [])
        # Each claim also posted a 'claim' message; the conflicting one names it.
        msgs = agentboard.thread(P, kinds=["claim"])
        self.assertEqual(len(msgs), 3)
        self.assertIn("doors/implementer", msgs[1]["mentions"])

    def test_concurrent_claims_cannot_both_miss_the_overlap(self):
        n = 8
        gate = threading.Barrier(n)
        results = []

        def race(i):
            gate.wait()
            results.append(agentboard.claim(P, task=f"t{i}", author=f"t{i}/implementer",
                                            paths=["scripts/shared.gd"]))
        threads = [threading.Thread(target=race, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(agentboard.claims(P)), n)
        # Exactly one claimant won the path clean; every later one saw the rest.
        self.assertEqual(sorted(len(r.conflicts) for r in results), list(range(n)))

    def test_expired_claims_are_not_live(self):
        agentboard.claim(P, task="doors", author="doors/implementer",
                         paths=["a.gd"], ttl_s=-1)
        self.assertEqual(agentboard.claims(P), [])
        self.assertEqual(len(agentboard.claims(P, include_expired=True)), 1)
        c = agentboard.claim(P, task="locks", author="locks/implementer", paths=["a.gd"])
        self.assertEqual(c.conflicts, [])

    def test_release_ends_the_authors_claims(self):
        agentboard.claim(P, task="doors", author="doors/implementer", paths=["a.gd"])
        agentboard.claim(P, task="doors", author="doors/implementer", paths=["b.gd"])
        agentboard.claim(P, task="locks", author="locks/implementer", paths=["c.gd"])
        self.assertEqual(agentboard.release(P, "doors", "doors/implementer"), 2)
        self.assertEqual([c["author"] for c in agentboard.claims(P)], ["locks/implementer"])
        self.assertEqual(agentboard.release(P, "doors", "doors/implementer"), 0)
        self.assertEqual(len(agentboard.thread(P, kinds=["release"])), 1)


class Expertise(BoardCase):
    def test_derived_from_results_answers_and_claims(self):
        agentboard.post(P, author="doors/implementer", author_model="GLM-5.3",
                        kind="result", body="done #doors #physics",
                        refs={"files": ["scripts/doors/door.gd"]})
        agentboard.post(P, author="doors/implementer", kind="answer", body="yes",
                        refs={"files": ["scripts/doors/door.gd", "README.md"]})
        agentboard.post(P, author="locks/implementer", kind="note", body="#ignored",
                        refs={"files": ["x.gd"]})
        agentboard.claim(P, task="locks", author="locks/implementer", paths=["scripts/locks/"])
        e = agentboard.expertise(P)
        self.assertEqual(e["doors/implementer"]["paths"][0], "scripts/doors/door.gd")
        self.assertEqual(e["doors/implementer"]["results"], 1)
        self.assertIn("physics", e["GLM-5.3"]["topics"])
        self.assertEqual(e["locks/implementer"]["paths"], ["scripts/locks/"])
        self.assertNotIn("ignored", e["locks/implementer"]["topics"])

    def test_channels_count_unread(self):
        agentboard.post(P, author="captain", body="one")
        agentboard.post(P, author="captain", body="two")
        agentboard.post(P, author="captain", channel="task:doors", body="t")
        chans = {c["channel"]: c for c in agentboard.channels(P, reader="doors/implementer")}
        self.assertEqual(chans["project"]["count"], 2)
        self.assertEqual(chans["project"]["unread_for"], 2)
        agentboard.mark_read(P, "doors/implementer", "project", time.time() + 1)
        chans = {c["channel"]: c for c in agentboard.channels(P, reader="doors/implementer")}
        self.assertEqual(chans["project"]["unread_for"], 0)


class Digest(BoardCase):
    def _seed(self):
        agentboard.post(P, author="captain", kind="decision", body="doors use signals")
        agentboard.post(P, author="locks/implementer", kind="status", body="locks at 50%")
        agentboard.post(P, author="locks/implementer", kind="result", body="locks done",
                        refs={"files": ["scripts/locks/lock.gd"]})
        agentboard.claim(P, task="locks", author="locks/implementer",
                         paths=["scripts/locks/"], note="refactoring")
        agentboard.post(P, author="locks/reviewer", kind="question",
                        body="@doors/implementer which signal name?")
        agentboard.post(P, author="captain", channel="dm:doors/implementer",
                        body="please hurry")

    def test_sections_come_in_priority_order(self):
        self._seed()
        text = agentboard.digest_for(P, task="doors", role="implementer", model="GLM-5.3",
                                     files_hint=["scripts/locks/lock.gd"])
        order = ["Unread mentions", "Open questions", "Live claims by OTHERS",
                 "Recent decisions", "Latest status of sibling", "Who knows"]
        pos = [text.index(s) for s in order]
        self.assertEqual(pos, sorted(pos))
        self.assertIn("please hurry", text)
        self.assertIn("which signal name", text)
        self.assertIn("refactoring", text)
        self.assertIn("locks done", text)            # latest sibling status wins
        self.assertNotIn("locks at 50%", text)
        self.assertTrue(text.rstrip().endswith(agentboard.HOW_TO_POST))
        self.assertIn(".arc/board.jsonl", text)
        self.assertIn("main.py board post", text)

    def test_char_cap_drops_the_lowest_priority_first(self):
        self._seed()
        for i in range(30):
            agentboard.post(P, author="captain", body=f"@doors ping {i} " + "z" * 150)
        text = agentboard.digest_for(P, task="doors", role="implementer", model="m",
                                     files_hint=["scripts/locks/"], limit_chars=900)
        self.assertLessEqual(len(text), 900)
        self.assertIn("Unread mentions", text)
        self.assertNotIn("Who knows", text)
        self.assertTrue(text.endswith(agentboard.HOW_TO_POST))

    def test_another_role_on_the_same_task_is_shown_its_claims(self):
        agentboard.claim(P, task="doors", author="doors/reviewer",
                         paths=["scripts/doors/"], note="checking door.gd")
        text = agentboard.digest_for(P, task="doors", role="implementer", model="m",
                                     files_hint=["scripts/doors/door.gd"])
        self.assertIn("doors/reviewer holds scripts/doors/", text)
        own = agentboard.digest_for(P, task="doors", role="reviewer", model="m",
                                    files_hint=["scripts/doors/door.gd"])
        self.assertNotIn("Live claims by OTHERS", own)

    def test_read_mentions_are_not_unread(self):
        agentboard.post(P, author="captain", body="@doors old news")
        agentboard.mark_read(P, "doors/implementer", "inbox", time.time() + 1)
        text = agentboard.digest_for(P, task="doors", role="implementer", model="m")
        self.assertNotIn("old news", text)


class Ingest(BoardCase):
    def _append(self, *lines):
        path = self.wt / agentboard.REL
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for line in lines:
                f.write((line if isinstance(line, str) else json.dumps(line)) + "\n")

    def ingest(self):
        return agentboard.ingest_file(P, self.wt, task="doors", role="implementer",
                                      model="GLM-5.3")

    def test_idempotent_and_incremental(self):
        self._append({"kind": "status", "body": "halfway"},
                     {"channel": "project", "kind": "question", "body": "@locks api?",
                      "mentions": ["locks"]})
        self.assertEqual(self.ingest(), 2)
        self.assertEqual(self.ingest(), 0)
        self._append({"body": "done"})
        self.assertEqual(self.ingest(), 1)
        msgs = agentboard.thread(P)
        self.assertEqual([m["body"] for m in msgs], ["halfway", "@locks api?", "done"])
        self.assertEqual(msgs[0]["channel"], "task:doors")   # default channel
        self.assertEqual(msgs[1]["author"], "doors/implementer")
        self.assertEqual(msgs[1]["author_model"], "GLM-5.3")

    def test_invalid_lines_become_errors_once(self):
        self._append("{not json", {"kind": "shout", "body": "x"},
                     {"kind": "note"}, {"channel": "elsewhere", "body": "x"},
                     {"body": "fine"})
        self.assertEqual(self.ingest(), 1)
        self.assertEqual(self.ingest(), 0)
        errs = agentboard.thread(P, kinds=["error"])
        self.assertEqual(len(errs), 4)
        self.assertIn("not valid JSON", errs[0]["body"])
        self.assertIn("unknown kind", errs[1]["body"])

    def test_forgotten_offset_does_not_duplicate(self):
        self._append({"body": "one"})
        self.ingest()
        with agentboard._lock:
            agentboard._conn().execute("DELETE FROM board_reads")
        self.ingest()
        self.assertEqual(len(agentboard.thread(P)), 1)

    def test_partial_last_line_waits(self):
        path = self.wt / agentboard.REL
        path.parent.mkdir(parents=True)
        path.write_text('{"body": "whole"}\n{"body": "hal')
        self.assertEqual(self.ingest(), 1)
        with path.open("a") as f:
            f.write('f"}\n')
        self.assertEqual(self.ingest(), 1)
        self.assertEqual([m["body"] for m in agentboard.thread(P)], ["whole", "half"])


class BackCompat(BoardCase):
    def test_board_post_lands_in_the_task_channel(self):
        long_body = "b" * 1000
        pid = board.post(self.wt, task="doors", role="implementer", model="GLM-5.3",
                         harness="opencode", kind="result", body=long_body,
                         session_id="s1", project=P)
        [m] = agentboard.thread(P, channel="task:doors")
        self.assertEqual(m["id"], pid)
        self.assertEqual(m["author"], "doors/implementer")
        self.assertEqual(m["body"], long_body)      # full body, not the 400 cap
        self.assertEqual(m["refs"], {"harness": "opencode", "session_id": "s1"})
        # Its JSONL echo in the worktree is not ingested twice.
        self.assertEqual(agentboard.ingest_file(P, self.wt, task="doors",
                                                role="implementer", model="GLM-5.3"), 0)

    def test_project_is_inferred_from_the_worktree_path(self):
        board.post(self.wt, task="doors", role="reviewer", model="m",
                   harness="reasonix", body="inferred")
        self.assertEqual(agentboard.thread(P)[0]["body"], "inferred")

    def test_legacy_project_jsonl_is_imported_once(self):
        path = board.project_path(P, create=True)
        rec = {"id": "legacy1", "ts": 1.0, "task": "doors", "role": "implementer",
               "model": "m", "harness": "cursor", "kind": "handoff", "body": "old"}
        path.write_text(json.dumps(rec) + "\n")
        agentboard.thread(P)
        agentboard._migrated.clear()          # a second process
        msgs = agentboard.thread(P)
        self.assertEqual([m["id"] for m in msgs], ["legacy1"])
        self.assertEqual(msgs[0]["channel"], "task:doors")


class InferProject(BoardCase):
    def test_worktree_path(self):
        self.assertEqual(agentboard.infer_project(self.wt / "scripts"), (P, "doors"))
        self.assertEqual(agentboard.infer_project(self.tmp.name), (None, None))


if __name__ == "__main__":
    unittest.main()
