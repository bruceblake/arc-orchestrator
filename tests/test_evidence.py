"""Visual evidence (evidence.py, AGENTS.md Rule 7d): every game change is SEEN.

Unit tests run everywhere. The one real-render test needs Godot, ffmpeg and a
display, and is skipped (with the reason) where they are missing — CI has none.
"""
import json
import os
import pathlib
import shutil
import struct
import subprocess
import tempfile
import unittest
import unittest.mock
import zlib
from pathlib import Path

import helpers  # noqa: F401  (redirects the event log and db)
import config
import evidence

ROOT = Path(__file__).resolve().parent.parent


def _png(path, pixel_at, w=64, h=36):
    """Write an 8-bit RGB PNG whose pixel (x, y) is pixel_at(x, y)."""
    rows = b"".join(b"\x00" + b"".join(bytes(pixel_at(x, y)) for x in range(w))
                    for y in range(h))
    ch = lambda t, d: (struct.pack(">I", len(d)) + t + d
                       + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff))
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n"
                           + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                           + ch(b"IDAT", zlib.compress(rows)) + ch(b"IEND", b""))


class Detection(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_only_godot_projects_and_opt_out_honoured(self):
        self.assertFalse(evidence.enabled_for(self.d))
        (self.d / "project.godot").write_text('run/main_scene="res://w.tscn"\n')
        self.assertTrue(evidence.enabled_for(self.d, {"id": "t"}))
        self.assertFalse(evidence.enabled_for(self.d, {"id": "t", "evidence": False}),
                         "a docs-only task opts out with evidence: false")
        old = config.EVIDENCE_MODE
        config.EVIDENCE_MODE = "off"
        try:
            self.assertFalse(evidence.enabled_for(self.d))
        finally:
            config.EVIDENCE_MODE = old

    def test_main_scene_is_read_from_project_godot(self):
        (self.d / "project.godot").write_text(
            '[application]\nrun/main_scene="res://scenes/world.tscn"\n')
        self.assertEqual(evidence.main_scene(self.d), "res://scenes/world.tscn")

    def test_missing_tools_are_an_infrastructure_gap(self):
        with unittest.mock.patch.object(evidence.shutil, "which", return_value=None), \
                unittest.mock.patch("studio.engine.godot.godot_bin", return_value="/x/godot"):
            with self.assertRaises(evidence.EvidenceUnavailable):
                evidence._require_tools()


class Comparing(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_identical_shots_have_no_change(self):
        a, b = self.d / "a.png", self.d / "b.png"
        _png(a, lambda x, y: (x * 3, y * 5, 90))
        _png(b, lambda x, y: (x * 3, y * 5, 90))
        changed, delta = evidence.diff_stats(a, b)
        self.assertEqual((changed, delta), (0.0, 0.0))

    def test_a_changed_region_is_measured(self):
        a, b = self.d / "a.png", self.d / "b.png"
        _png(a, lambda x, y: (40, 40, 40))
        _png(b, lambda x, y: (240, 40, 40) if x < 32 else (40, 40, 40))
        changed, _ = evidence.diff_stats(a, b)
        self.assertAlmostEqual(changed, 0.5, delta=0.05)

    def test_different_sizes_are_not_compared(self):
        a, b = self.d / "a.png", self.d / "b.png"
        _png(a, lambda x, y: (1, 2, 3))
        _png(b, lambda x, y: (1, 2, 3), w=32)
        self.assertEqual(evidence.diff_stats(a, b), (None, None))

    def test_a_solid_frame_is_flagged_blank(self):
        a, b = self.d / "grey.png", self.d / "scene.png"
        _png(a, lambda x, y: (77, 77, 77))
        _png(b, lambda x, y: (x * 4 % 256, y * 7 % 256, (x + y) % 256))
        self.assertGreater(evidence.blank_share(a), 0.99)
        self.assertLess(evidence.blank_share(b), 0.5)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
    def test_compare_writes_before_after_diff(self):
        base, cur = self.d / "base", self.d / "cur"
        base.mkdir(), cur.mkdir()
        _png(base / "cam.png", lambda x, y: (40, 40, 40))
        _png(cur / "cam.png", lambda x, y: (240, 40, 40) if x < 8 else (40, 40, 40))
        _png(cur / "newcam.png", lambda x, y: (1, 2, 3))
        rows = evidence.compare(base, sorted(cur.glob("*.png")), self.d / "cmp")
        by = {r["name"]: r for r in rows}
        self.assertTrue(by["newcam"]["new"])
        side = Path(by["cam"]["side_by_side"])
        self.assertTrue(side.exists())
        self.assertGreater(by["cam"]["changed"], 0.05)


class LeavesNoTrace(unittest.TestCase):
    """publish() runs `git add -A`: the capture must not leave files behind."""

    def test_new_untracked_files_are_removed_old_ones_kept(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        subprocess.run(["git", "init", "-q", str(d)], check=True)
        (d / ".gitignore").write_text(".godot/\n")
        (d / "agent_work.gd").write_text("# the implementer's new file\n")
        with evidence._leave_no_trace(d):
            (d / "studio_playtest.json").write_text("{}")
            (d / "studio_shots").mkdir()
            (d / "studio_shots" / "a.png").write_bytes(b"x")
            (d / ".godot").mkdir()
            (d / ".godot" / "cache").write_text("ignored")
        self.assertTrue((d / "agent_work.gd").exists(), "the agent's work is kept")
        self.assertFalse((d / "studio_playtest.json").exists())
        self.assertFalse((d / "studio_shots").exists())
        self.assertTrue((d / ".godot" / "cache").exists(), "ignored files are not touched")


def _manifest(d):
    shots = d / "shots"
    shots.mkdir(parents=True)
    for n in ("overview", "corridor"):
        _png(shots / f"{n}.png", lambda x, y: (9, 9, 9))
    cmp = d / "compare"
    cmp.mkdir()
    _png(cmp / "overview.png", lambda x, y: (9, 9, 9))
    for f in ("flythrough.mp4", "flythrough.gif", "playtest.mp4", "playtest.gif"):
        (d / f).write_bytes(b"x")
    return {"head": "abcdef1234567", "shots": [str(shots / "overview.png"),
                                               str(shots / "corridor.png")],
            "videos": {"flythrough": {"mp4": str(d / "flythrough.mp4"),
                                      "gif": str(d / "flythrough.gif")},
                       "playtest": {"mp4": str(d / "playtest.mp4"),
                                    "gif": str(d / "playtest.gif")}},
            "compare": [{"name": "overview", "changed": 0.123, "delta": 0.01,
                         "side_by_side": str(cmp / "overview.png")},
                        {"name": "corridor", "new": True}],
            "baseline": {"sha": "0123456789abc"},
            "warnings": ["camera 'x' rendered a nearly solid frame"],
            "playtest_shots": [], "seconds": 40}


class Presenting(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        self.m = _manifest(self.d)

    def test_reviewers_get_comparisons_first_then_uncompared_shots(self):
        imgs = evidence.review_images(self.m)
        self.assertEqual([Path(p).parent.name for p in imgs], ["compare", "shots"])
        self.assertEqual(Path(imgs[1]).stem, "corridor")

    def test_prompt_block_names_every_artifact_and_blocks_regressions(self):
        text = evidence.prompt_block(self.m)
        self.assertIn("blocking issue", text)
        self.assertIn("12.3%", text)
        self.assertIn("new viewpoint", text)
        self.assertIn("flythrough video", text)
        self.assertIn("playtest video", text)
        self.assertIn("WARNING", text)
        self.assertEqual(evidence.prompt_block(None), "")

    def test_pr_markdown_embeds_images_and_links_videos(self):
        md = evidence.pr_markdown(self.m, "https://github.com/o/r/blob/arc-evidence/t/x2",
                                  task_id="t", attempt=2)
        self.assertIn("![playtest](https://github.com/o/r/blob/arc-evidence/t/x2/playtest.gif?raw=true)", md)
        self.assertIn("[full video](https://github.com/o/r/blob/arc-evidence/t/x2/flythrough.mp4", md)
        self.assertIn("compare/overview.png?raw=true", md)
        self.assertIn("12.3%", md)
        self.assertIn("⚠️", md)
        self.assertIn("abcdef1234", md)

    def test_board_post_summarises(self):
        body = evidence.board_body(self.m)
        self.assertIn("2 screenshot(s)", body)
        self.assertIn("flythrough", body)
        self.assertIn("overview 12.3%", body)


class PipelineWiring(unittest.TestCase):
    """The capture reaches every place a decision is made (Rule 7d)."""

    @classmethod
    def setUpClass(cls):
        cls.src = (ROOT / "code_tasks.py").read_text()

    def _body(self, start, end):
        s = self.src[self.src.index(start):]
        return s[:s.index(end)]

    def test_gate_captures_after_a_pass_and_can_fail_on_it(self):
        body = self._body("        async def gate(ctx):", "        async def review(ctx):")
        self.assertIn("evidence.enabled_for(wt, t)", body)
        self.assertIn("shown, eerr = await capture_evidence(wt, attempt)", body)
        self.assertIn('"evidence": shown', body)

    def test_both_reviews_see_the_images(self):
        for start, end in (("        async def review(ctx):", "        async def publish(ctx):"),
                           ("        async def pr_reviewer(ctx):", "        async def pr_review(ctx):")):
            body = self._body(start, end)
            self.assertIn("evidence.review_images(shown)", body, start)
            self.assertIn("evidence.prompt_block(shown)", body, start)

    def test_the_pr_gets_the_evidence(self):
        self.assertIn("await post_pr_evidence(ctx, number)", self.src)

    def test_capture_never_writes_into_the_worktree(self):
        src = (ROOT / "evidence.py").read_text()
        self.assertIn("_leave_no_trace(worktree)", src)
        self.assertNotIn("ensure_render_harness", src,
                         "the studio harness writes INTO the project; evidence must not")


def _have_renderer():
    from studio.engine import godot
    return bool(godot.godot_bin() and shutil.which("ffmpeg") and evidence._display())


@unittest.skipUnless(os.getenv("ARC_TEST_RENDER") == "1" and _have_renderer(),
                     "real render test: set ARC_TEST_RENDER=1 on a machine with "
                     "Godot, ffmpeg and a display")
class RealCapture(unittest.TestCase):
    def test_scaffold_game_is_captured_and_compared_without_trace(self):
        from studio import scaffold
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
            repo = Path(d) / "game"
            scaffold.create(str(repo), project="evidence-test", force=True)
            run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], env=env,
                                            check=True, capture_output=True)
            if not (repo / ".git").exists():
                run("init", "-q", "-b", "main")
            run("add", "-A")
            run("commit", "-qm", "scaffold", "--allow-empty")
            old = config.EVIDENCE_DIR
            config.EVIDENCE_DIR = Path(d) / "evidence"
            try:
                m = evidence.capture(repo, Path(d) / "out", repo=repo, base="HEAD",
                                     project="evidence-test")
            finally:
                config.EVIDENCE_DIR = old
            self.assertGreaterEqual(len(m["shots"]), 4)
            self.assertIn("flythrough", m["videos"])
            self.assertTrue(Path(m["videos"]["flythrough"]["mp4"]).stat().st_size > 0)
            self.assertTrue(m["compare"])
            self.assertTrue(all((c.get("changed") or 0) < 0.01 for c in m["compare"]),
                            "HEAD against itself must not change")
            st = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                                capture_output=True, text=True).stdout
            self.assertEqual(st.strip(), "", "the capture left files in the worktree")


if __name__ == "__main__":
    unittest.main()
