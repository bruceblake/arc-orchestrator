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
        self.assertTrue(text.rstrip().endswith(
            agentboard.how_to_post(P, "doors/implementer")))
        self.assertIn(".arc/board.jsonl", text)
        self.assertIn("main.py board post", text)

    def test_char_cap_drops_the_lowest_priority_first(self):
        self._seed()
        for i in range(30):
            agentboard.post(P, author="captain", body=f"@doors ping {i} " + "z" * 150)
        howto = agentboard.how_to_post(P, "doors/implementer")
        cap = len(howto) + 600
        text = agentboard.digest_for(P, task="doors", role="implementer", model="m",
                                     files_hint=["scripts/locks/"], limit_chars=cap)
        self.assertLessEqual(len(text), cap)
        self.assertIn("Unread mentions", text)
        self.assertNotIn("Who knows", text)
        self.assertTrue(text.endswith(howto))

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
                     {"channel": "project", "kind": "question",
                      "body": "@captain api?", "mentions": ["captain"]})
        self.assertEqual(self.ingest(), 2)
        self.assertEqual(self.ingest(), 0)
        self._append({"body": "done"})
        self.assertEqual(self.ingest(), 1)
        msgs = agentboard.thread(P)
        self.assertEqual([m["body"] for m in msgs], ["halfway", "@captain api?", "done"])
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


class IngestValidation(BoardCase):
    """A post that reaches nobody is worse than no post: it looks like
    coordination happened. These are the three ways a line is refused, and
    each refusal comes back to the agent as kind=error so it learns next
    round (Rule 4c)."""

    def _append(self, *lines):
        path = self.wt / agentboard.REL
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for line in lines:
                f.write((line if isinstance(line, str) else json.dumps(line)) + "\n")

    def ingest(self, role="implementer"):
        return agentboard.ingest_file(P, self.wt, task="doors", role=role,
                                      model="GLM-5.3")

    def _errors(self):
        return [m["body"] for m in agentboard.thread(P, kinds=["error"])]

    def _code_task(self, tid, *, project=P, taskfile=None, model="GLM-5.3",
                   reviewer="deepseek"):
        """One `code_tasks` row. `project` decides which board project its
        worktree (or taskfile stem) belongs to — the ONLY thing that may make
        its id addressable here."""
        wt = str(Path(config.WORKTREE_ROOT) / project / tid) if project else ""
        tf = taskfile if taskfile is not None else f"/t/{project}.json"
        with agentboard._lock:
            c = agentboard._db(P)
            c.execute("CREATE TABLE IF NOT EXISTS code_tasks(id TEXT, taskfile"
                      " TEXT, title TEXT, model TEXT, reviewer TEXT, status TEXT,"
                      " branch TEXT, worktree TEXT, error TEXT, created_at TEXT,"
                      " finished_at TEXT, PRIMARY KEY(taskfile, id))")
            c.execute("INSERT OR REPLACE INTO code_tasks VALUES"
                      "(?,?,?,?,?,?,?,?,?,?,?)",
                      (tid, tf, tid, model, reviewer, "merged", "", wt, "", "", ""))
            c.commit()

    def test_a_reviewer_family_token_is_not_an_address(self):
        """`code_tasks.reviewer` holds a FAMILY (`deepseek`, `glm`), never a
        model name, and `_targets` cannot match a family — so `@deepseek`
        validated and was delivered to nobody, the same hole as
        `@implementer`."""
        self._code_task("doors", reviewer="deepseek")
        self._code_task("doors2", reviewer="glm")
        known = agentboard.known_targets(P)
        for fam in ("deepseek", "glm"):
            self.assertNotIn(fam, known, f"{fam} is a family, not an address")
            self.assertEqual(agentboard.unknown_mentions([fam], known), [fam])
        self._append({"kind": "ping", "body": "cc @deepseek and @glm"})
        self.assertEqual(self.ingest(), 0)
        self.assertIn("@deepseek", self._errors()[0])

    def test_another_projects_task_id_is_not_addressable_here(self):
        """A task id from a different project reaches nobody on this board;
        `known_targets` used to add every id in the database."""
        self._code_task("elsewhere-task", project="elsewhere",
                        taskfile="/t/elsewhere.json")
        self._code_task("doors", project=P, taskfile=f"/t/{P}.json")
        known = agentboard.known_targets(P)
        self.assertNotIn("elsewhere-task", known)
        self.assertIn("doors", known)
        self._append({"kind": "question", "body": "is @elsewhere-task done?"})
        self.assertEqual(self.ingest(), 0)
        self.assertIn("@elsewhere-task", self._errors()[0])

    def test_this_projects_taskfile_stem_makes_its_ids_addressable(self):
        """The dashboard's rule: the worktree sits under
        `<WORKTREE_ROOT>/<project>/`, or the taskfile stem is the project."""
        self._code_task("no-worktree-yet", project=None,
                        taskfile=f"/t/{P}.json")            # stem matches
        self._code_task("other-stem", project=None,
                        taskfile="/t/something-else.json")  # neither
        known = agentboard.known_targets(P)
        self.assertIn("no-worktree-yet", known)
        self.assertNotIn("other-stem", known)

    def test_a_sibling_task_that_has_not_posted_is_still_addressable(self):
        """The reason the code_tasks table is consulted at all: a task that
        has not spoken yet must still be reachable."""
        self._code_task("quiet-sibling", project=P)
        self.assertEqual(agentboard.thread(P), [], "nothing posted")
        self.assertIn("quiet-sibling", agentboard.known_targets(P))
        self._append({"kind": "question", "body": "@quiet-sibling api ready?"})
        self.assertEqual(self.ingest(), 1)
        self.assertEqual(self._errors(), [])

    def test_every_code_task_id_accepted_here_is_delivered_here(self):
        """The round-4 invariant, now covering the code_tasks source too.

        Stated precisely: a token is accepted iff it addresses at least one
        agent that PARTICIPATES in this project. `_addressed` is a pure
        mention-vs-agent test, so `@theirs` "reaches" `theirs/implementer` in
        the abstract — but that agent belongs to another project and will
        never read this board, which is exactly why the token is rejected.
        """
        self._code_task("mine", project=P)
        self._code_task("theirs", project="elsewhere",
                        taskfile="/t/elsewhere.json")
        known = agentboard.known_targets(P)
        roles = ("implementer", "reviewer", "pr_reviewer", "orchestrator")
        participating = ({f"{tid}/{r}" for tid in ("mine", "locks")
                          for r in roles} | {"captain", "operator", "planner"})
        model = "GLM-5.3"
        for tok in ("mine", "theirs", "deepseek"):
            mid = agentboard.post(P, author="locks/implementer",
                                  body=f"ping @{tok}", mentions=[tok])
            [msg] = [m for m in agentboard.thread(P) if m["id"] == mid]
            reaches = [a for a in participating
                       if agentboard._addressed(msg, a, model)]
            accepted = not agentboard.unknown_mentions([tok], known)
            self.assertEqual(
                accepted, bool(reaches),
                f"@{tok}: accepted={accepted} but participating agents "
                f"reached={reaches}")

    def test_a_mention_to_a_real_task_or_role_is_accepted(self):
        agentboard.post(P, author="locks/implementer", body="hi", author_task="locks")
        self._append({"kind": "question", "body": "@locks api ready?",
                      "mentions": ["locks"]},
                     {"kind": "ping", "body": "cc @captain @operator @all"},
                     {"kind": "note", "body": "for @GLM-5.3"},
                     {"kind": "note", "body": "for @doors/implementer"})
        self.assertEqual(self.ingest(), 4)
        self.assertEqual(self._errors(), [])

    def test_a_mention_naming_nothing_is_refused_with_the_reason(self):
        self._append({"kind": "question", "body": "is @ghostt api ready?"})
        self.assertEqual(self.ingest(), 0)
        [err] = self._errors()
        self.assertIn("@ghostt", err)
        self.assertIn("name nothing in this project", err)
        # The error lands in the agent's own task channel, so it reads it.
        self.assertEqual(agentboard.thread(P, kinds=["error"])[0]["channel"],
                         "task:doors")

    def test_a_mention_is_checked_in_the_mentions_field_too(self):
        self._append({"kind": "ping", "body": "no mention here",
                      "mentions": ["typo-id"]})
        self.assertEqual(self.ingest(), 0)
        self.assertIn("@typo-id", self._errors()[0])

    def test_a_role_suffixed_typo_is_refused_end_to_end(self):
        """The reviewer's case: `@not-a-task/implementer` used to pass ingest
        because `known_targets` added the role names and the old clause
        accepted any head before a role. It reaches nobody, so it must be
        refused like any other typo."""
        self._append({"kind": "question",
                      "body": "is @not-a-task/implementer done with its part?"})
        self.assertEqual(self.ingest(), 0)
        [err] = self._errors()
        self.assertIn("@not-a-task/implementer", err)
        self.assertIn("name nothing in this project", err)

    def test_a_real_task_with_a_role_suffix_is_still_accepted_end_to_end(self):
        agentboard.post(P, author="locks/implementer", body="x",
                        author_task="locks")
        self._append({"kind": "question",
                      "body": "@locks/reviewer can you re-read my diff?"})
        self.assertEqual(self.ingest(), 1)
        self.assertEqual(self._errors(), [])

    def test_a_bare_role_mention_is_refused_end_to_end(self):
        """`@implementer` is not an address: it reaches nobody, so accepting
        it would make an unanswered question look delivered."""
        self._append({"kind": "ping", "body": "cc @implementer and @reviewer"})
        self.assertEqual(self.ingest(), 0)
        [err] = self._errors()
        self.assertIn("@implementer", err)

    def test_a_bare_copy_of_the_prompt_is_rejected(self):
        prompt = ("You are implementing one task in this repository.\n\n"
                  "TASK doors: build the doors\n\nDo the thing carefully, keep "
                  "every request small, and locate the code first with the "
                  "graft commands you were given.")
        agentboard.record_prompt(P, task="doors", role="implementer", text=prompt)
        self._append({"kind": "status", "body": prompt.replace("\n", " ")})
        self.assertEqual(self.ingest(), 0)
        self.assertIn("bare copy of your prompt", self._errors()[0])

    def test_a_short_body_that_matches_prompt_wording_is_fine(self):
        agentboard.record_prompt(P, task="doors", role="implementer",
                                 text="You are implementing one task; tests pass.")
        self._append({"kind": "status", "body": "tests pass"})
        self.assertEqual(self.ingest(), 1)

    def test_the_prompt_of_a_DIFFERENT_role_does_not_reject_the_body(self):
        agentboard.record_prompt(P, task="doors", role="reviewer",
                                 text="X" * 300)
        self._append({"kind": "status", "body": "X" * 300})
        self.assertEqual(self.ingest(role="implementer"), 1)

    def test_a_tasks_own_id_is_always_addressable(self):
        self._append({"kind": "note", "body": "@doors check this"})
        self.assertEqual(self.ingest(), 1)

    def test_kind_outside_KINDS_is_still_rejected(self):
        for bad in ("shout", {"kind": "note"}, 3):
            self._append({"kind": bad, "body": "x"})
        self.assertEqual(self.ingest(), 0)
        self.assertEqual(len(self._errors()), 3)

    def test_a_store_that_cannot_be_read_does_not_reject_every_mention(self):
        """known_targets fails OPEN: a broken store must not turn every
        mention into a rejection and silence the whole board."""
        orig = agentboard._db
        agentboard._db = lambda *a, **k: (_ for _ in ()).throw(OSError("db gone"))
        try:
            self.assertEqual(agentboard.known_targets(P),
                             set(agentboard.MENTION_FREE),
                             "an unreadable store yields the always-legal roles")
        finally:
            agentboard._db = orig
        # unknown_mentions itself reports only what names nobody.
        free = set(agentboard.MENTION_FREE)
        self.assertEqual(agentboard.unknown_mentions(["all", "captain"], free), [])
        self.assertEqual(agentboard.unknown_mentions(["@operator", "planner"], free),
                         [])
        self.assertEqual(agentboard.unknown_mentions(["nobody-here"], free),
                         ["nobody-here"])
        self.assertEqual(agentboard.unknown_mentions(["doors/implementer"], free),
                         ["doors/implementer"], "half-known is not known here")
        # The HEAD decides: a known task makes `<task>/<role>` real...
        self.assertEqual(
            agentboard.unknown_mentions(["doors/implementer"], free | {"doors"}), [])
        # ...a role name alone never does.
        self.assertEqual(
            agentboard.unknown_mentions(["doors/implementer"], free | {"implementer"}),
            ["doors/implementer"])

    def test_a_role_name_never_blesses_an_unknown_task(self):
        """`@not-a-task/implementer` is precisely the typo this check exists
        for: `known_targets` used to add the role names, and the old
        `tail in known` clause then accepted ANY head spelled before a role —
        the mention passed ingest and was delivered to nobody."""
        agentboard.post(P, author="doors/implementer", body="x",
                        author_task="doors")
        known = agentboard.known_targets(P)
        for bogus in ("not-a-task/implementer", "ghost/reviewer",
                      "nope/pr_reviewer", "dors/implementer"):
            self.assertEqual(agentboard.unknown_mentions([bogus], known),
                             [bogus], f"@{bogus} must be rejected")
        # The real addresses still resolve.
        for good in ("doors", "doors/implementer", "doors/reviewer",
                     "doors/pr_reviewer", "GLM-5.3", "captain", "operator",
                     "all"):
            self.assertEqual(agentboard.unknown_mentions([good], known), [],
                             f"@{good} must be accepted")

    def test_a_bare_role_is_not_an_address(self):
        """A role name is a SUFFIX, never an address: `_targets` matches an
        agent's full `task/role`, its task, its model or `all`, so
        `@implementer` reaches nobody and must not validate."""
        agentboard.post(P, author="doors/implementer", body="x",
                        author_task="doors")
        known = agentboard.known_targets(P)
        for role in ("implementer", "reviewer", "pr_reviewer"):
            self.assertNotIn(role, known)
            self.assertEqual(agentboard.unknown_mentions([role], known), [role])

    def test_every_accepted_mention_actually_reaches_an_agent(self):
        """The check's whole purpose. Pairs `unknown_mentions` with the
        delivery path (`_addressed`, used by inbox and digest) so the two
        cannot drift: every accepted mention must reach the agent it NAMES,
        and the typos must be rejected AND reach nobody.

        `@<task>/<role>` addresses that exact agent — `@doors/reviewer` is the
        reviewer of `doors`, not its implementer — so the recipient is part of
        the claim.
        """
        agentboard.post(P, author="doors/implementer", body="x",
                        author_task="doors")
        known = agentboard.known_targets(P)
        model = "GLM-5.3"
        cases = {
            "doors": ["doors/implementer", "doors/reviewer"],   # both roles
            "doors/implementer": ["doors/implementer"],
            "doors/reviewer": ["doors/reviewer"],               # NOT the impl
            "GLM-5.3": ["doors/implementer", "doors/reviewer", "captain",
                        "operator", "planner"],
            "captain": ["captain"],
            "operator": ["operator"],
            "all": ["doors/implementer", "doors/reviewer", "captain",
                    "operator", "planner"],
        }
        everyone = ["doors/implementer", "doors/reviewer", "captain",
                    "operator", "planner"]
        for tok, recipients in cases.items():
            mid = agentboard.post(P, author="locks/implementer",
                                  body=f"ping @{tok}", mentions=[tok])
            [msg] = [m for m in agentboard.thread(P) if m["id"] == mid]
            self.assertEqual(agentboard.unknown_mentions([tok], known), [],
                             f"@{tok} is a real address and must validate")
            for who in everyone:
                self.assertEqual(
                    agentboard._addressed(msg, who, model), who in recipients,
                    f"@{tok} -> {who} should be {who in recipients}")
        # And the typos: rejected, and reaching nobody at all.
        for tok in ("not-a-task/implementer", "ghost/reviewer", "implementer"):
            mid = agentboard.post(P, author="locks/implementer",
                                  body=f"ping @{tok}", mentions=[tok])
            [msg] = [m for m in agentboard.thread(P) if m["id"] == mid]
            self.assertEqual(agentboard.unknown_mentions([tok], known), [tok],
                             f"@{tok} must be rejected")
            for who in ("doors/implementer", "doors/reviewer", "locks/implementer"):
                self.assertFalse(agentboard._addressed(msg, who, model),
                                 "a rejected mention must reach nobody")


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


class PipelineDelivery(BoardCase):
    """What agentboard-pipeline-wiring relies on: broadcasts reach every
    reader once, and a usage swap's handoff lands on the board."""

    def test_a_project_channel_ping_is_an_unread_mention_for_every_task(self):
        agentboard.post(P, author="operator", channel="project", kind="ping",
                        body="freeze merges for ten minutes")
        agentboard.post(P, author="operator", channel="project", kind="note",
                        body="a plain note is not a broadcast")
        d = agentboard.digest_for(P, task="doors", role="implementer", model="m")
        head, _, _ = d.partition("Open questions")
        self.assertIn("freeze merges", head)
        self.assertNotIn("a plain note", head)
        agentboard.mark_read(P, "doors/implementer", "inbox", time.time())
        d = agentboard.digest_for(P, task="doors", role="implementer", model="m")
        self.assertNotIn("freeze merges", d)

    def test_a_usage_swap_handoff_is_posted_to_the_task_channel(self):
        import drivers
        mid = drivers.post_handoff(self.wt, "doors-x3", "implementer", "GLM-5.3",
                                   "opencode", "usage limit on opencode/GLM-5.3; "
                                   "this attempt continues on cursor/X.",
                                   to_model="X", to_harness="cursor")
        self.assertTrue(mid)
        [m] = agentboard.thread(P, channel="task:doors")
        self.assertEqual(m["kind"], "handoff")
        self.assertIn("doors/implementer", m["mentions"])
        self.assertTrue(m["body"].startswith("usage limit on opencode/GLM-5.3"))
        self.assertEqual(m["refs"]["to_model"], "X")

    def test_a_handoff_outside_a_worktree_is_a_no_op(self):
        import drivers
        self.assertIsNone(drivers.post_handoff(self.tmp.name, "doors-x1",
                                               "reviewer", "m", "h", "b"))

    def test_mark_seen_marks_what_the_digest_read_not_the_clock(self):
        agentboard.post(P, author="operator", channel="project", kind="ping",
                        body="first broadcast")
        d1 = agentboard.digest_for(P, task="doors", role="implementer",
                                   model="m", mark_seen=True)
        # Posted right after, very likely inside the same millisecond.
        agentboard.post(P, author="operator", channel="project", kind="ping",
                        body="second broadcast")
        d2 = agentboard.digest_for(P, task="doors", role="implementer",
                                   model="m", mark_seen=True)
        d3 = agentboard.digest_for(P, task="doors", role="implementer",
                                   model="m", mark_seen=True)
        self.assertIn("first broadcast", d1)
        self.assertNotIn("first broadcast", d2)
        self.assertIn("second broadcast", d2)
        self.assertNotIn("broadcast", d3)

    def test_more_than_eight_pings_are_all_delivered_across_prompts(self):
        for i in range(11):
            agentboard.post(P, author="operator", channel="project", kind="ping",
                            body=f"broadcast-{i:02d}")
        seen = []
        for _ in range(3):
            d = agentboard.digest_for(P, task="doors", role="implementer",
                                      model="m", mark_seen=True)
            seen.append([i for i in range(11) if f"broadcast-{i:02d}" in d])
        self.assertEqual(seen[0], list(range(8)), "oldest first")
        self.assertEqual(seen[1], [8, 9, 10], "the rest stay unread")
        self.assertEqual(seen[2], [])

    def test_mentions_cut_by_the_char_budget_stay_unread(self):
        for i in range(4):
            agentboard.post(P, author="operator", channel="project", kind="ping",
                            body=f"long-{i} " + "x" * 250)
        got = set()
        for _ in range(6):
            d = agentboard.digest_for(P, task="doors", role="implementer",
                                      model="m", mark_seen=True,
                                      limit_chars=len(agentboard.how_to_post(
                                          P, "doors/implementer")) + 700)
            got |= {i for i in range(4) if f"long-{i} " in d}
        self.assertEqual(got, {0, 1, 2, 3})


class BoardHealth(BoardCase):
    """Whether agents use the board WELL, on synthetic messages.

    `board_health` is what `main.py audit` prints a section from, so the
    arithmetic has to be right on a hand-built history: the shares, the median
    answer latency, the questions nobody answered, the overlapping claims,
    and who read a mention without replying.
    """

    def setUp(self):
        super().setUp()
        self.t = time.time()

    def _post(self, author, kind="note", body="x", **kw):
        kw.setdefault("ts", self.t)
        return agentboard.post(P, author=author, kind=kind, body=body, **kw)

    def test_posts_are_counted_by_agent_and_by_kind(self):
        self._post("doors/implementer", "status")
        self._post("doors/implementer", "claim", author_task="doors")
        self._post("locks/implementer", "question", author_task="locks")
        h = agentboard.board_health(P)
        self.assertEqual(h["posts"], 3)
        self.assertEqual(h["by_agent"]["doors/implementer"], 2)
        self.assertEqual(h["by_kind"]["status"], 1)
        self.assertEqual(h["by_kind"]["question"], 1)

    def test_the_claim_and_result_shares_count_tasks_not_posts(self):
        self._post("doors/implementer", "claim", author_task="doors",
                   refs={"paths": ["pkg/a.py"]})
        self._post("doors/implementer", "result", author_task="doors")
        self._post("locks/implementer", "status", author_task="locks")
        self._post("hatch/implementer", "status", author_task="hatch")
        h = agentboard.board_health(P)
        self.assertEqual(h["tasks"], 3)
        self.assertEqual(h["claimed"], 1)
        self.assertEqual(h["resulted"], 1)
        self.assertAlmostEqual(h["claim_share"], 1 / 3)
        self.assertAlmostEqual(h["result_share"], 1 / 3)

    def test_a_claim_on_no_paths_is_not_a_claim(self):
        """The pre-attempt lease on an empty files_hint: every prison-escape
        claim had paths=[] and the share still read 80%."""
        agentboard.claim(P, task="doors", author="doors/implementer", paths=[])
        self._post("locks/implementer", "claim", "claiming", author_task="locks")
        h = agentboard.board_health(P)
        self.assertEqual(h["tasks"], 2)
        self.assertEqual(h["claimed"], 0)
        agentboard.claim(P, task="doors", author="doors/implementer",
                         paths=["scenes/door.tscn"])
        self.assertEqual(agentboard.board_health(P)["claimed"], 1)

    def test_a_live_claim_without_a_post_still_counts_as_claimed(self):
        agentboard.claim(P, task="doors", author="doors/implementer",
                         paths=["pkg/a.py"])
        h = agentboard.board_health(P)
        self.assertEqual(h["claimed"], 1)
        self.assertEqual(h["tasks"], 1)

    def test_the_median_is_taken_over_answers_only(self):
        for i, delay in enumerate((60, 120, 600)):
            q = self._post("locks/implementer", "question", f"q{i}",
                           ts=self.t + i * 1000)
            self._post("doors/implementer", "answer", f"a{i}",
                       reply_to=q, ts=self.t + i * 1000 + delay)
        h = agentboard.board_health(P)
        self.assertEqual(h["answered"], 3)
        self.assertEqual(h["median_answer_s"], 120.0)

    def test_unanswered_questions_are_listed_with_their_age(self):
        self._post("locks/implementer", "question", "still nobody told me",
                   ts=self.t - 7200)
        h = agentboard.board_health(P)
        self.assertEqual(h["answered"], 0)
        self.assertEqual(len(h["unanswered"]), 1)
        self.assertIn("nobody told me", h["unanswered"][0]["body"])
        self.assertGreater(h["unanswered"][0]["age_s"], 7000)

    def test_an_answer_in_the_channel_also_closes_a_question(self):
        self._post("locks/implementer", "question", "api?", channel="task:doors",
                   ts=self.t - 500)
        self._post("doors/implementer", "answer", "yes",
                   channel="task:doors", ts=self.t - 200)
        h = agentboard.board_health(P)
        self.assertEqual(h["answered"], 1)
        self.assertEqual(h["median_answer_s"], 300.0)

    def test_an_answer_from_the_asker_itself_does_not_close_it(self):
        self._post("locks/implementer", "question", "anyone?", ts=self.t - 500)
        self._post("locks/implementer", "answer", "never mind", ts=self.t - 100)
        self.assertEqual(agentboard.board_health(P)["answered"], 0)

    def test_the_asker_answering_its_own_question_with_reply_to_stays_open(self):
        """The path that actually happens: the asker replies to its own
        question with `reply_to`, which `_insert` marks state='answered'. That
        flag is not "someone replied", so the question must stay unanswered —
        otherwise it vanishes from `unanswered` and the median stays None
        while nobody has said anything."""
        q = self._post("locks/implementer", "question", "is @doors ready?",
                       mentions=["doors"], ts=self.t - 500)
        self._post("locks/implementer", "answer", "never mind, found it",
                   reply_to=q, ts=self.t - 100)
        # The store's own flag IS set — the metric must not trust it.
        [row] = agentboard.thread(P, kinds=["question"])
        self.assertEqual(row["state"], "answered", "precondition: _insert flags it")
        h = agentboard.board_health(P)
        self.assertEqual(h["answered"], 0)
        self.assertEqual([x["id"] for x in h["unanswered"]], [q])
        self.assertIsNone(h["median_answer_s"])
        self.assertEqual(h["unanswered"][0]["age_s"], 500)

    def test_a_real_reply_after_the_asker_gave_up_still_closes_it(self):
        """The self-answer must not poison the question either: when someone
        else DOES reply, it closes and the latency is measured from the
        question — not from the asker's own post."""
        q = self._post("locks/implementer", "question", "is @doors ready?",
                       mentions=["doors"], ts=self.t - 500)
        self._post("locks/implementer", "answer", "never mind", reply_to=q,
                   ts=self.t - 400)
        self._post("doors/implementer", "answer", "yes, since this morning",
                   reply_to=q, ts=self.t - 100)
        h = agentboard.board_health(P)
        self.assertEqual(h["answered"], 1)
        self.assertEqual(h["unanswered"], [])
        self.assertEqual(h["median_answer_s"], 400.0,
                         "measured from the question, not the self-answer")

    def test_a_self_answer_does_not_leave_a_median_behind(self):
        """A lone self-answer must not contribute a latency: there was no
        answer, so there is no time-to-answer to average."""
        for i in range(3):
            q = self._post("locks/implementer", "question", f"q{i}",
                           ts=self.t - 900 + i * 100)
            self._post("locks/implementer", "answer", f"self{i}", reply_to=q,
                       ts=self.t - 800 + i * 100)
        h = agentboard.board_health(P)
        self.assertEqual(h["answered"], 0)
        self.assertIsNone(h["median_answer_s"])
        self.assertEqual(len(h["unanswered"]), 3)

    def test_overlapping_live_claims_are_reported(self):
        agentboard.claim(P, task="doors", author="doors/implementer",
                         paths=["pkg/"])
        agentboard.claim(P, task="locks", author="locks/implementer",
                         paths=["pkg/a.py"])
        [c] = agentboard.board_health(P)["claim_conflicts"]
        self.assertEqual({c["a"], c["b"]},
                         {"doors/implementer", "locks/implementer"})
        self.assertEqual(c["paths"], ["pkg/"])

    def test_the_conflict_is_found_whichever_author_claimed_first(self):
        """The pair must be found by IDENTITY, not by name order. The old
        guard compared authors lexicographically, so `locks` claiming FIRST
        and `doors` second reported NOTHING — the collision the metric exists
        to find. Both orderings must report exactly one pair."""
        for first, second in (("locks/implementer", "doors/implementer"),
                              ("doors/implementer", "locks/implementer")):
            with self.subTest(first=first):
                with agentboard._lock:
                    agentboard._db(P).execute("DELETE FROM board_claims")
                agentboard.claim(P, task=first.split("/")[0], author=first,
                                 paths=["pkg/"], ttl_s=3600)
                agentboard.claim(P, task=second.split("/")[0], author=second,
                                 paths=["pkg/a.py"], ttl_s=3600)
                conflicts = agentboard.board_health(P)["claim_conflicts"]
                self.assertEqual(len(conflicts), 1, f"{first} then {second}")
                self.assertEqual({conflicts[0]["a"], conflicts[0]["b"]},
                                 {first, second})
                self.assertEqual(conflicts[0]["paths"], ["pkg/"])

    def test_each_pair_is_reported_once_not_twice(self):
        agentboard.claim(P, task="doors", author="doors/implementer",
                         paths=["pkg/"])
        agentboard.claim(P, task="locks", author="locks/implementer",
                         paths=["pkg/a.py"])
        agentboard.claim(P, task="hatch", author="hatch/implementer",
                         paths=["pkg/b.py"])
        # doors' `pkg/` covers both of the others, so doors-locks and
        # doors-hatch are two pairs — but locks-hatch are SIBLINGS under pkg/,
        # so they do not overlap and are correctly not a third.
        conflicts = agentboard.board_health(P)["claim_conflicts"]
        self.assertEqual(len(conflicts), 2, "one entry per pair, not per direction")
        self.assertEqual([{c["a"], c["b"]} for c in conflicts],
                         [{"doors/implementer", "locks/implementer"},
                          {"doors/implementer", "hatch/implementer"}])

    def test_a_released_claim_is_not_a_conflict_any_more(self):
        """claims() drops released leases, so board_health must too —
        otherwise the audit names a lease nobody holds."""
        agentboard.claim(P, task="doors", author="doors/implementer",
                         paths=["pkg/"])
        agentboard.claim(P, task="locks", author="locks/implementer",
                         paths=["pkg/a.py"])
        self.assertEqual(len(agentboard.board_health(P)["claim_conflicts"]), 1)
        agentboard.release(P, "locks", "locks/implementer")
        self.assertEqual(agentboard.board_health(P)["claim_conflicts"], [],
                         "the released half is gone")
        self.assertEqual([c["author"] for c in agentboard.claims(P)],
                         ["doors/implementer"])

    def test_an_expired_claim_is_not_a_conflict_any_more(self):
        agentboard.claim(P, task="doors", author="doors/implementer",
                         paths=["pkg/"], ttl_s=3600)
        agentboard.claim(P, task="locks", author="locks/implementer",
                         paths=["pkg/a.py"], ttl_s=-1)   # already expired
        self.assertEqual(agentboard.board_health(P)["claim_conflicts"], [])

    def test_a_released_claim_still_counts_towards_the_claim_share(self):
        """The SHARE counts claims POSTED, the conflicts count claims HELD —
        a task that claimed and then released did claim."""
        agentboard.claim(P, task="doors", author="doors/implementer",
                         paths=["pkg/"])
        agentboard.post(P, author="locks/implementer", kind="result",
                        body="done", author_task="locks")
        agentboard.release(P, "doors", "doors/implementer")
        h = agentboard.board_health(P)
        self.assertEqual(h["claimed"], 1)
        self.assertEqual(h["tasks"], 2)

    def test_a_task_reclaiming_the_same_paths_is_not_a_conflict(self):
        agentboard.claim(P, task="doors", author="doors/implementer",
                         paths=["pkg/"])
        agentboard.claim(P, task="doors", author="doors/implementer",
                         paths=["pkg/a.py"], note="fix round 2")
        self.assertEqual(agentboard.board_health(P)["claim_conflicts"], [])

    def test_claims_on_unrelated_paths_are_not_a_conflict(self):
        agentboard.claim(P, task="doors", author="doors/implementer",
                         paths=["pkg/a.py"])
        agentboard.claim(P, task="locks", author="locks/implementer",
                         paths=["other/b.py"])
        self.assertEqual(agentboard.board_health(P)["claim_conflicts"], [])

    def test_an_agent_that_read_a_mention_and_never_replied_is_flagged(self):
        agentboard.post(P, author="captain", channel="project", body="@all freeze")
        agentboard.mark_read(P, "doors/implementer", "inbox", self.t + 1)
        h = agentboard.board_health(P)
        self.assertEqual([d["agent"] for d in h["deaf"]], ["doors/implementer"])
        self.assertEqual(h["deaf"][0]["pending"], 1)

    def test_an_agent_that_answered_after_reading_is_not_flagged(self):
        agentboard.post(P, author="captain", channel="project", body="@all freeze")
        agentboard.mark_read(P, "doors/implementer", "inbox", self.t + 1)
        self._post("doors/implementer", "note", "acknowledged, freezing now",
                   ts=self.t + 2)
        self.assertEqual(agentboard.board_health(P)["deaf"], [])

    def test_an_agent_still_reading_is_not_flagged(self):
        agentboard.post(P, author="captain", channel="project", body="@all freeze")
        self.assertEqual(agentboard.board_health(P)["deaf"], [],
                         "nothing was delivered to a prompt yet")

    def test_the_window_bounds_every_metric(self):
        self._post("doors/implementer", "claim", "old", author_task="doors",
                   ts=self.t - 48 * 3600)
        self._post("locks/implementer", "result", "new", author_task="locks")
        h = agentboard.board_health(P, since_hours=24)
        self.assertEqual(h["posts"], 1)
        self.assertEqual(h["tasks"], 1)
        self.assertEqual(h["claimed"], 0)

    def test_an_empty_board_is_all_zeros_not_an_error(self):
        h = agentboard.board_health("never-used")
        self.assertEqual(h["posts"], 0)
        self.assertEqual(h["tasks"], 0)
        self.assertEqual(h["claim_share"], 0.0)
        self.assertIsNone(h["median_answer_s"])
        self.assertEqual(h["unanswered"], [])
        self.assertEqual(h["deaf"], [])
