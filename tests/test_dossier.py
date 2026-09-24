"""The task dossier: durable handoff context in and out of every agent run."""
import asyncio
import io
import json
import os
import subprocess
import tempfile
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from helpers import capture_events  # noqa: F401  (sys.path + DB redirect)

import config
import dossier
import gitstore


def _ids():
    return f"proj-{uuid.uuid4().hex[:8]}", "t1"


class RecordAndCap(unittest.TestCase):
    def test_attempts_append_and_cap_with_a_rolled_up_count(self):
        p, t = _ids()
        for i in range(1, 26):
            dossier.record_attempt(p, t, attempt=i, model="GLM-5.3",
                                   harness="opencode", role="implementer",
                                   outcome="gate_failed", summary=f"try {i}",
                                   failure_excerpt="boom",
                                   files_changed=[f"f{i % 3}.py"],
                                   session_id=None)
        d = dossier.get(p, t)
        self.assertEqual(len(d["attempts"]), dossier.MAX_ATTEMPTS)
        self.assertEqual(d["rolled_up"], 5)
        self.assertEqual(d["attempts"][0]["attempt"], 6)
        self.assertEqual(d["attempts"][-1]["attempt"], 25)
        self.assertEqual(d["files"], ["f0.py", "f1.py", "f2.py"])

    def test_unknown_outcome_is_refused(self):
        p, t = _ids()
        with self.assertRaises(ValueError):
            dossier.record_attempt(p, t, attempt=1, model="m", outcome="meh")

    def test_table_creation_is_idempotent(self):
        dossier._connect().close()
        dossier._connect().close()
        self.assertEqual(dossier.get(*_ids())["attempts"], [])


class HarvestHandoff(unittest.TestCase):
    def _write(self, wt, text):
        (Path(wt) / ".arc").mkdir(exist_ok=True)
        (Path(wt) / dossier.HANDOFF_REL).write_text(text)

    def setUp(self):
        self.wt = tempfile.mkdtemp(prefix="arc-handoff-")
        self.p, self.t = _ids()

    def harvest(self, text, attempt=1):
        self._write(self.wt, text)
        return dossier.harvest_handoff(self.p, self.t, self.wt, attempt=attempt,
                                       model="GLM-5.3", role="implementer")

    def test_sections_parse_and_the_file_is_deleted(self):
        self.harvest("## Done\nparser\n## Remaining\nCLI\n"
                     "## Decisions\n- sqlite, not files (one writer)\n"
                     "## Dead ends\n- regex over markdown: broke on nesting\n"
                     "## Gotchas\ntests redirect DB_PATH\n## Next step\nwire main.py\n")
        self.assertFalse((Path(self.wt) / dossier.HANDOFF_REL).exists())
        d = dossier.get(self.p, self.t)
        self.assertEqual(d["done"], "parser")
        self.assertEqual(d["remaining"], "CLI")
        self.assertEqual(d["next_step"], "wire main.py")
        self.assertEqual(d["gotchas"], "tests redirect DB_PATH")
        self.assertEqual(d["decisions"], ["sqlite, not files (one writer)"])
        self.assertEqual(d["dead_ends"], ["regex over markdown: broke on nesting"])

    def test_free_form_text_lands_in_notes(self):
        self.harvest("I fixed the bug but ran out of time.\n## Musings\nmaybe split it\n")
        notes = dossier.get(self.p, self.t)["notes"]
        self.assertIn("I fixed the bug but ran out of time.", notes)
        self.assertIn("Musings: maybe split it", notes)

    def test_newest_wins_and_lists_accumulate_deduplicated(self):
        self.harvest("## Remaining\nA and B\n## Decisions\n- use X (fast)\n")
        self.harvest("## Remaining\nB only\n## Decisions\n- Use  X (fast)\n"
                     "- keep Y (compat)\n## Dead ends\n- Z\n", attempt=2)
        d = dossier.get(self.p, self.t)
        self.assertEqual(d["remaining"], "B only")
        self.assertEqual(d["decisions"], ["use X (fast)", "keep Y (compat)"])
        self.assertEqual(d["dead_ends"], ["Z"])

    def test_no_file_is_a_no_op(self):
        self.assertEqual(dossier.harvest_handoff(
            self.p, self.t, self.wt, attempt=1, model="m", role="implementer"), {})


class Render(unittest.TestCase):
    def _seed(self):
        p, t = _ids()
        wt = tempfile.mkdtemp(prefix="arc-render-")
        (Path(wt) / ".arc").mkdir()
        (Path(wt) / dossier.HANDOFF_REL).write_text(
            "## Done\nDONE-CLAIM\n## Remaining\nREMAINING-X\n## Decisions\n- DEC-1\n"
            "## Dead ends\n- DEAD-1\n## Gotchas\nGOTCHA-1\n## Next step\nNEXT-1\n")
        dossier.harvest_handoff(p, t, wt, attempt=1, model="m", role="implementer")
        for i, m in enumerate(["DeepSeek-V4.1-Flash-thinking-max", "GLM-5.3"], 1):
            dossier.record_attempt(p, t, attempt=i, model=m, outcome="gate_failed",
                                   failure_excerpt=f"EXCERPT-{i}",
                                   files_changed=["a.py"])
        dossier.note_model_change(p, t, "escalation 1: DS -> GLM")
        dossier.set_pr(p, t, 42, "https://x/pull/42")
        dossier.import_notes(p, t, "OPERATOR-SAYS", "captain")
        return p, t

    def test_empty_history_renders_nothing(self):
        self.assertEqual(dossier.render(*_ids(), role="implementer"), "")

    def test_ordering(self):
        text = dossier.render(*self._seed(), role="implementer")
        order = ["Current state", "OPERATOR-SAYS", "REMAINING-X", "NEXT-1",
                 "DEC-1", "DEAD-1", "GOTCHA-1", "EXCERPT-2", "a.py", "#42"]
        pos = [text.index(k) for k in order]
        self.assertEqual(pos, sorted(pos))
        self.assertIn("do not relitigate", text)
        self.assertIn("do not retry", text)
        self.assertIn("escalation 1: DS -> GLM", text)
        self.assertIn("GLM-5.3 [hard]", text)
        self.assertNotIn("DONE-CLAIM", text)

    def test_reviewer_sees_the_claimed_done(self):
        p, t = self._seed()
        self.assertIn("DONE-CLAIM", dossier.render(p, t, role="reviewer"))
        self.assertIn("DONE-CLAIM", dossier.render(p, t, role="pr-reviewer"))

    def test_next_step_and_gotchas_alone_still_render(self):
        p, t = _ids()
        wt = tempfile.mkdtemp(prefix="arc-render-")
        (Path(wt) / ".arc").mkdir()
        (Path(wt) / dossier.HANDOFF_REL).write_text(
            "## Next step\nNEXT-ONLY\n## Gotchas\nGOTCHA-ONLY\n")
        dossier.harvest_handoff(p, t, wt, attempt=1, model="m", role="implementer")
        text = dossier.render(p, t, role="implementer")
        self.assertIn("NEXT-ONLY", text)
        self.assertIn("GOTCHA-ONLY", text)
        q, u = _ids()
        dossier.set_pr(q, u, 7, "https://x/pull/7")
        self.assertIn("#7", dossier.render(q, u, role="pr-reviewer"))

    def test_char_cap(self):
        p, t = self._seed()
        for role in ("implementer", "reviewer", "pr-reviewer"):
            text = dossier.render(p, t, role=role, limit_chars=300)
            self.assertLessEqual(len(text), 300)
            self.assertTrue(text.startswith("TASK DOSSIER"))
            self.assertIn("truncated", text)


class ExportAndCli(unittest.TestCase):
    def test_export_md_and_json(self):
        p, t = _ids()
        dossier.import_notes(p, t, "hello from ops", "operator")
        self.assertIn("hello from ops", dossier.export(p, t, "md"))
        self.assertEqual(json.loads(dossier.export(p, t, "json"))
                         ["operator_notes"][0]["text"], "hello from ops")

    def test_cli_prints_and_injects(self):
        import main
        p, t = _ids()
        t = f"cli-{uuid.uuid4().hex[:6]}"
        dossier.record_attempt(p, t, attempt=1, model="GLM-5.3", outcome="crashed")
        for argv, want in (
                (["code", "context", t, "--note", "look at store.py"], "look at store.py"),
                (["code", "context", t, "--json"], '"outcome": "crashed"')):
            buf = io.StringIO()
            with mock.patch("sys.argv", ["main.py", *argv]), redirect_stdout(buf), \
                    self.assertRaises(SystemExit) as ex:
                main.main()
            self.assertEqual(ex.exception.code, 0)
            self.assertIn(want, buf.getvalue())


class PublishNeverStagesHandoff(unittest.TestCase):
    def test_real_repo(self):
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")

            def run(*a):
                return subprocess.run(["git", "-C", d, *a], env=env, check=True,
                                      capture_output=True, text=True).stdout
            run("init", "-q", "-b", "main")
            Path(d, "a.txt").write_text("1")
            run("add", "a.txt")
            run("commit", "-qm", "init")
            Path(d, "a.txt").write_text("2")
            Path(d, ".arc").mkdir()
            Path(d, dossier.HANDOFF_REL).write_text("## Done\nx\n")
            with mock.patch.dict(os.environ, env):
                head = asyncio.run(gitstore.publish(d, "task(t): x"))
                diff = asyncio.run(gitstore.diff_full(d, "main"))
            self.assertTrue(head)
            files = run("show", "--name-only", "--format=", head).split()
            self.assertEqual(files, ["a.txt"])
            self.assertNotIn("handoff.md", diff)


if __name__ == "__main__":
    unittest.main()
