"""The shared board is the context a harness swap cannot carry in a session id."""
import json
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401  (redirects the event log before board emits)
import board
import config


class BoardPostsAreReadableAndScoped(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wt = Path(self.tmp.name) / "wt"
        self.wt.mkdir()
        self._old = config.BOARD_DIR
        config.BOARD_DIR = Path(self.tmp.name) / "boards"
        self.addCleanup(setattr, config, "BOARD_DIR", self._old)

    def test_a_post_lands_on_the_task_and_the_project(self):
        pid = board.post(self.wt, task="player-controller", role="implementer",
                         model="Composer-2.5", harness="cursor", kind="result",
                         body="wrote the crouch capsule", session_id="ffc0",
                         project="prison-escape")
        task_lines = (self.wt / board.REL).read_text().splitlines()
        proj_lines = board.project_path("prison-escape").read_text().splitlines()
        self.assertEqual(len(task_lines), 1)
        self.assertEqual(task_lines, proj_lines)
        rec = json.loads(task_lines[0])
        self.assertEqual(rec["id"], pid)
        self.assertEqual(rec["harness"], "cursor")
        self.assertEqual(rec["session_id"], "ffc0")

    def test_prompt_names_the_harness_that_owns_the_session(self):
        board.post(self.wt, task="player-controller", role="implementer",
                   model="Composer-2.5", harness="cursor", kind="handoff",
                   body="usage limit; continued on cursor", session_id="ffc0",
                   project="prison")
        text = board.prompt_block(self.wt, project="prison", task="player-controller")
        self.assertIn("ONLY on the harness", text)
        self.assertIn("via cursor", text)
        self.assertIn("session=ffc0", text)
        self.assertNotIn("Other tasks", text)

    def test_sibling_tasks_show_up_as_other_tasks(self):
        board.post(self.wt, task="cell-doors", role="implementer",
                   model="GLM-5.3", harness="opencode", kind="note",
                   body="door api is toggle(id)", project="prison")
        text = board.prompt_block(self.wt, project="prison", task="player-controller")
        self.assertIn("Other tasks", text)
        self.assertIn("toggle(id)", text)

    def test_body_is_one_line_and_capped(self):
        board.post(self.wt, task="t", role="implementer", model="m",
                   harness="codex", kind="result",
                   body="a\n" + ("b" * 1000))
        rec = json.loads((self.wt / board.REL).read_text())
        self.assertNotIn("\n", rec["body"])
        self.assertLessEqual(len(rec["body"]), 400)

    def test_empty_board_adds_nothing_to_the_prompt(self):
        self.assertEqual(board.prompt_block(self.wt, project="none", task="t"), "")
