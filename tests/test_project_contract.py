"""The product repo's contract is named in prompts and never confused with the fleet's."""
import tempfile
import unittest
from pathlib import Path

import code_tasks
import project_contract
from captain import build_prompt as captain_prompt
from orchchat import build_prompt as chat_prompt


def _outside(tmp):
    """A file that is not inside the repo, for the symlink-escape case."""
    path = Path(tmp) / "secret.txt"
    path.write_text("secret marker", encoding="utf-8")
    return path


class Discover(unittest.TestCase):
    def test_missing_repo_is_an_empty_contract(self):
        info = project_contract.discover("/no/such/repo")
        self.assertEqual(info["root_files"], [])
        self.assertEqual(info["cursor_rules"], [])
        self.assertIn("project contract: none", project_contract.status_line("/no/such/repo"))

    def test_names_root_files_and_cursor_rules(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "AGENTS.md").write_text("Layout: src/ and tests/.\n", encoding="utf-8")
            (root / "CLAUDE.md").write_text("Run pytest.\n", encoding="utf-8")
            rules = root / ".cursor" / "rules"
            rules.mkdir(parents=True)
            (rules / "style.mdc").write_text("use tabs\n", encoding="utf-8")
            (rules / "notes.txt").write_text("ignore me\n", encoding="utf-8")
            info = project_contract.discover(root)
            self.assertEqual(info["root_files"], ["AGENTS.md", "CLAUDE.md"])
            self.assertEqual(info["cursor_rules"], ["style.mdc"])
            line = project_contract.status_line(root)
            self.assertIn("AGENTS.md", line)
            self.assertIn("CLAUDE.md", line)
            self.assertIn(".cursor/rules", line)

    def test_symlink_outside_the_repo_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            root.mkdir()
            outside = _outside(d)
            (root / "AGENTS.md").symlink_to(outside)
            info = project_contract.discover(root)
            self.assertEqual(info["root_files"], [])
            block = project_contract.planner_block(root)
            self.assertNotIn("secret marker", block)

    def test_cursor_rules_are_capped(self):
        with tempfile.TemporaryDirectory() as d:
            rules = Path(d) / ".cursor" / "rules"
            rules.mkdir(parents=True)
            for i in range(project_contract.MAX_RULES + 5):
                (rules / f"r{i:02d}.md").write_text("x\n", encoding="utf-8")
            info = project_contract.discover(d)
            self.assertEqual(len(info["cursor_rules"]), project_contract.MAX_RULES)


class Prose(unittest.TestCase):
    def test_planner_excerpt_includes_the_contract_and_stops(self):
        with tempfile.TemporaryDirectory() as d:
            marker = "UNIQUE_LAYOUT_RULE"
            (Path(d) / "AGENTS.md").write_text(
                marker + ("\n" + ("word " * 800)), encoding="utf-8")
            block = project_contract.planner_block(d)
            self.assertIn(marker, block)
            self.assertIn("excerpt truncated", block)
            self.assertIn("Do not copy fleet", block)
            self.assertLess(len(block), 4000)

    def test_missing_contract_tells_the_planner_when_to_add_one(self):
        with tempfile.TemporaryDirectory() as d:
            block = project_contract.planner_block(d)
            self.assertIn("first task must", block)
            self.assertIn("do not add a contract task", block.lower())

    def test_roles_differ_when_the_contract_is_absent(self):
        with tempfile.TemporaryDirectory() as d:
            impl = project_contract.role_block(d, "implementer")
            rev = project_contract.role_block(d, "reviewer")
            self.assertIn("Follow only this task", impl)
            self.assertIn("not in the tree", rev)
            self.assertNotIn("Review for", impl)
            self.assertNotIn("Review for", rev)

    def test_roles_name_the_file_when_it_exists(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "AGENTS.md").write_text("Do not touch generated/.\n", encoding="utf-8")
            impl = project_contract.role_block(d, "implementer")
            rev = project_contract.role_block(d, "reviewer")
            self.assertIn("read AGENTS.md", impl)
            self.assertIn("orchestrator's AGENTS.md does not apply", impl)
            self.assertIn("Quote the rule", rev)

    def test_captain_warns_when_the_repo_has_no_contract(self):
        with tempfile.TemporaryDirectory() as d:
            text = project_contract.captain_block(d)
            self.assertIn("project contract: none", text)
            self.assertIn("Tell the operator", text)
            (Path(d) / "AGENTS.md").write_text("layout\n", encoding="utf-8")
            present = project_contract.captain_block(d)
            self.assertIn("project contract: AGENTS.md", present)
            self.assertIn("bake their rules", present)


class PromptsCarryTheContract(unittest.TestCase):
    TASK = {"id": "t1", "title": "Add a page", "prompt": "do the thing",
            "files_hint": [], "verify_cmd": "true"}

    def test_implementer_omits_the_block_unless_one_is_passed(self):
        bare = code_tasks._impl_prompt(self.TASK, "")
        self.assertNotIn("Project contract", bare)
        told = code_tasks._impl_prompt(self.TASK, "", contract="Project contract: read AGENTS.md")
        self.assertLess(told.index("Project contract"), told.index("Rules:"))

    def test_reviewer_contract_stays_after_the_checklist_heading(self):
        p = code_tasks._review_prompt(
            self.TASK, "DIFF", "dependents: run", contract="Project contract: quote it")
        self.assertLess(p.index("DIFF"), p.index("IMPACT"))
        self.assertLess(p.index("IMPACT"), p.index("Review for"))
        self.assertLess(p.index("Review for"), p.index("Project contract"))

    def test_describe_prints_the_status_line(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "AGENTS.md").write_text("hi\n", encoding="utf-8")
            raw = {
                "project": {
                    "repo": d,
                    "title": "t",
                    "tasks": [{
                        "id": "t1", "title": "A", "prompt": "p",
                        "model": "DeepSeek-V4.1-Flash-thinking-max",
                        "reviewer": "glm", "verify_cmd": "true", "deps": [],
                    }],
                }
            }
            path = Path(d) / "tasks.json"
            path.write_text(__import__("json").dumps(raw), encoding="utf-8")
            out = code_tasks.describe(code_tasks.load_taskfile(path))
            self.assertIn("project contract: AGENTS.md", out)

    def test_captain_and_chat_include_the_contract(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "AGENTS.md").write_text("UNIQUE_CHAT_RULE keep src/app.\n", encoding="utf-8")
            state = {"planner_model": "x", "task_status_counts": {},
                     "capacity": {}, "needs_attention": [], "recent_events": []}
            cap = captain_prompt(d, state, [])
            self.assertIn("project contract: AGENTS.md", cap)
            self.assertLess(cap.index("TARGET REPO"), cap.index("project contract"))
            chat = chat_prompt(d, [])
            self.assertIn("UNIQUE_CHAT_RULE", chat)


if __name__ == "__main__":
    unittest.main()
