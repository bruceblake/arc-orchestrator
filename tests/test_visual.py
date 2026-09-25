"""Visual review of the orchestrator's own dashboard (AGENTS.md Rule 7e).

tools/visual renders the dashboard headlessly over fixture data, compares the
renders to committed goldens (check.sh runs that part for real), and the
pipeline captures before/after screenshots of any diff that touches the UI,
hands them to the reviewers and posts them on the pull request. These tests
pin the pieces that do not need a browser; the real render runs in check.sh's
"visual regression" step, which skips cleanly where Chromium is missing.
"""
import asyncio
import contextlib
import io
import importlib.util
import json
import os
import shutil
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from helpers import FakeStore, capture_events

import code_tasks
import config
import drivers
import evidence
import ui_evidence

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(
        f"visual_{name}", ROOT / "tools" / "visual" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


compare = _load("compare")


def _png(path, pixel_at, w=32, h=20):
    rows = b"".join(b"\x00" + b"".join(bytes(pixel_at(x, y)) for x in range(w))
                    for y in range(h))

    def ch(t, d):
        return (struct.pack(">I", len(d)) + t + d
                + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff))
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n"
                           + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                           + ch(b"IDAT", zlib.compress(rows)) + ch(b"IEND", b""))
    return Path(path)


def _tmp(test):
    d = Path(tempfile.mkdtemp(prefix="arc-visual-test-"))
    test.addCleanup(shutil.rmtree, d, ignore_errors=True)
    return d


GREY = lambda x, y: (40, 40, 40)                                    # noqa: E731


class GoldenTolerance(unittest.TestCase):
    """compare.py: identical passes, noise within tolerance passes, a real
    change fails — and the DEFAULT tolerance is tight enough to see a typo."""

    def setUp(self):
        self.d = _tmp(self)
        (self.d / "golden").mkdir()
        (self.d / "now").mkdir()

    def _pair(self, after, name="v.png", w=32, h=20):
        _png(self.d / "golden" / name, GREY, w, h)
        _png(self.d / "now" / name, after, w, h)

    def test_identical_is_pass(self):
        self._pair(GREY)
        rows = compare.compare_dirs(self.d / "golden", self.d / "now")
        self.assertEqual([r["status"] for r in rows], ["PASS"])
        self.assertEqual(rows[0]["changed"], 0.0)

    def test_one_pixel_off_within_tolerance_is_pass(self):
        self._pair(lambda x, y: (200, 0, 0) if (x, y) == (3, 4) else (40, 40, 40))
        rows = compare.compare_dirs(self.d / "golden", self.d / "now",
                                    tolerance=1 / (32 * 20))
        self.assertEqual(rows[0]["status"], "PASS")
        self.assertEqual(rows[0]["box"], (3, 4, 1, 1))

    def test_a_level_of_antialias_noise_is_under_the_threshold(self):
        self._pair(lambda x, y: (44, 40, 40))
        rows = compare.compare_dirs(self.d / "golden", self.d / "now", tolerance=0)
        self.assertEqual(rows[0]["status"], "PASS")

    def test_gross_change_is_diff_and_fails(self):
        self._pair(lambda x, y: (250, 250, 250) if x > 10 else (40, 40, 40))
        rows = compare.compare_dirs(self.d / "golden", self.d / "now")
        self.assertEqual(rows[0]["status"], "DIFF")
        self.assertTrue(compare.failed(rows))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(compare.main([str(self.d / "golden"), str(self.d / "now")]), 1)

    def test_a_typo_sized_change_fails_at_the_default_tolerance(self):
        """Measured: one dropped letter moved 0.076% of a desktop page, and
        the first default (0.2%) passed it. 40 changed pixels of a 1440x900
        page must fail with the defaults."""
        hit = {(100 + i % 8, 130 + i // 8) for i in range(40)}
        self._pair(lambda x, y: (230, 230, 230) if (x, y) in hit else (40, 40, 40),
                   w=1440, h=900)
        rows = compare.compare_dirs(self.d / "golden", self.d / "now")
        self.assertEqual(rows[0]["status"], "DIFF", rows)

    def test_size_change_missing_view_and_new_view(self):
        _png(self.d / "golden" / "grew.png", GREY, 32, 20)
        _png(self.d / "now" / "grew.png", GREY, 32, 24)
        _png(self.d / "golden" / "gone.png", GREY)
        _png(self.d / "now" / "new.png", GREY)
        rows = {r["name"]: r["status"] for r in
                compare.compare_dirs(self.d / "golden", self.d / "now")}
        self.assertEqual(rows, {"grew.png": "DIFF", "gone.png": "MISSING",
                                "new.png": "NO-BASELINE"})
        self.assertEqual(len(compare.failed(
            compare.compare_dirs(self.d / "golden", self.d / "now"),
            require_baseline=True)), 3)

    def test_goldens_are_committed_for_every_view(self):
        capture = _load("capture")
        want = {f"{v[0]}.png" for v in capture.views()}
        have = {p.name for p in (ROOT / "tests" / "visual" / "golden").glob("*.png")}
        self.assertEqual(want, have)
        self.assertEqual(len(want), 16)       # 4 pages x desktop/phone x light/dark


class RunScriptInAFreshCheckout(unittest.TestCase):
    def test_log_dir_is_created_before_anything_is_written_under_it(self):
        """A fresh clone has no logs/ (gitignored). run.sh redirected the
        capture log into logs/visual/ before anything created it, so
        check.sh failed on every fresh checkout: "logs/visual/check.log: No
        such file or directory"."""
        src = (ROOT / "tools" / "visual" / "run.sh").read_text()
        body = src[src.index('out=logs/visual/check'):]
        self.assertIn("mkdir -p logs/visual", body)
        self.assertLess(body.index("mkdir -p logs/visual"), body.index('>"$out.log"'))


class FixtureIsolation(unittest.TestCase):
    """The fixture server never reads the operator's live state."""

    def test_every_tree_derived_path_moves_under_the_fixture(self):
        serve = _load("serve")
        import types
        tree = Path("/some/tree")
        cfg = types.SimpleNamespace(ROOT=tree, DB_PATH="/some/tree/orchestrator.db",
                                    EVENTS_LOG="/some/tree/logs/events.jsonl",
                                    BOARD_DIR=Path("/some/tree/logs/boards"),
                                    TASKS_DIR="/home/op/tasks", PORT=8787)
        serve._redirect_config(cfg, tree, "/fx/root")
        self.assertEqual(cfg.DB_PATH, "/fx/root/orchestrator.db")
        self.assertEqual(cfg.EVENTS_LOG, "/fx/root/logs/events.jsonl")
        self.assertEqual(cfg.BOARD_DIR, Path("/fx/root/logs/boards"))
        self.assertEqual(cfg.ROOT, Path("/fx/root"))
        self.assertEqual(cfg.TASKS_DIR, "/home/op/tasks")   # env handles this one
        self.assertEqual(cfg.PORT, 8787)

    def test_fixture_builds_a_store_with_every_status(self):
        fixture = _load("fixture")
        d = _tmp(self)
        fx = fixture.build(d)
        import sqlite3
        con = sqlite3.connect(fx["db"])
        statuses = {r[0] for r in con.execute("SELECT status FROM code_tasks")}
        con.close()
        self.assertTrue({"merged", "failed", "conflict", "in_review"} <= statuses)
        self.assertTrue(Path(fx["events"]).read_text().strip())
        # Every timestamp is pinned before the frozen "now".
        for line in Path(fx["events"]).read_text().splitlines():
            self.assertLess(json.loads(line)["ts"], fixture.FROZEN_NOW)


class UiDetection(unittest.TestCase):
    def test_paths(self):
        self.assertTrue(ui_evidence.is_ui_path("static/index.html"))
        self.assertTrue(ui_evidence.is_ui_path("static/panels/fleet.js"))
        self.assertTrue(ui_evidence.is_ui_path("dashboard.py"))
        self.assertFalse(ui_evidence.is_ui_path("config.py"))
        self.assertFalse(ui_evidence.is_ui_path("docs/static/x.md"))
        self.assertEqual(ui_evidence.touches_ui(["config.py", "static/usage.html"]),
                         ["static/usage.html"])

    def test_diff_headers(self):
        diff = ("diff --git a/config.py b/config.py\n+x\n"
                "diff --git a/static/phone.html b/static/phone.html\n+y\n")
        self.assertEqual(ui_evidence.diff_touches_ui(diff), ["static/phone.html"])
        self.assertEqual(ui_evidence.diff_touches_ui("diff --git a/a.py b/a.py\n"), [])

    def test_enabled_only_for_a_dashboard_tree_with_a_ui_diff(self):
        d = _tmp(self)
        self.assertFalse(ui_evidence.enabled_for(d, {}, ["static/index.html"]))
        (d / "static").mkdir()
        (d / "static" / "index.html").write_text("<html></html>")
        (d / "dashboard.py").write_text("")
        self.assertTrue(ui_evidence.enabled_for(d, {}, ["static/index.html"]))
        self.assertFalse(ui_evidence.enabled_for(d, {}, ["config.py"]))
        self.assertFalse(ui_evidence.enabled_for(d, {"evidence": False},
                                                 ["static/index.html"]))
        with mock.patch.object(config, "EVIDENCE_MODE", "off"):
            self.assertFalse(ui_evidence.enabled_for(d, {}, ["static/index.html"]))


def _manifest(d):
    shots = d / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    a = _png(shots / "index-desktop-dark.png", GREY)
    b = _png(shots / "usage-desktop-dark.png", GREY)
    (d / "compare").mkdir(exist_ok=True)
    (d / "before").mkdir(exist_ok=True)
    side = _png(d / "compare" / "index-desktop-dark.png", GREY)
    before = _png(d / "before" / "index-desktop-dark.png", GREY)
    return {"kind": "ui", "shots": [str(a), str(b)], "head": "abc1234567",
            "baseline": {"sha": "def7654321"}, "ui_files": ["static/index.html"],
            "compare": [{"name": "index-desktop-dark", "changed": 0.0123,
                         "box": [1, 1, 2, 2], "side_by_side": str(side),
                         "before": str(before)},
                        {"name": "usage-desktop-dark", "changed": 0.0, "box": None,
                         "side_by_side": None}],
            "coverage": {}, "warnings": [], "new_page_errors":
                {"usage-desktop-dark": ["TypeError: x is undefined"]}}


class Presenting(unittest.TestCase):
    def test_seeing_reviewer_is_told_to_look_and_that_regressions_block(self):
        m = _manifest(_tmp(self))
        block = ui_evidence.prompt_block(m, sees_images=True)
        self.assertIn("LOOK at each", block)
        self.assertIn("BLOCKING", block)
        self.assertIn(m["compare"][0]["side_by_side"], block)
        self.assertIn("unchanged views (1): usage-desktop-dark", block)
        self.assertIn("NEW JAVASCRIPT ERROR", block)
        self.assertNotIn("CANNOT SEE", block)

    def test_blind_reviewer_is_not_asked_to_judge_pixels(self):
        block = ui_evidence.prompt_block(_manifest(_tmp(self)), sees_images=False)
        self.assertIn("YOU CANNOT SEE IMAGES", block)
        self.assertNotIn("LOOK at each", block)
        self.assertIn("1.23%", block)          # the numbers are still its evidence

    def test_review_images_put_the_changed_panels_first(self):
        m = _manifest(_tmp(self))
        self.assertEqual(ui_evidence.review_images(m)[0], m["compare"][0]["side_by_side"])

    def test_pr_markdown_embeds_before_after_diff_inline(self):
        d = _tmp(self)
        m = _manifest(d)
        web = "https://github.com/o/r/blob/arc-evidence/t/x1"
        md = ui_evidence.pr_markdown(m, web, task_id="t", attempt=1)
        self.assertIn(f"![index-desktop-dark]({web}/compare/index-desktop-dark.png?raw=true)", md)
        self.assertIn(f"{web}/before/index-desktop-dark.png?raw=true", md)
        self.assertIn(f"{web}/shots/usage-desktop-dark.png?raw=true", md)
        self.assertIn("1.23% of pixels changed", md)
        self.assertIn("TypeError: x is undefined", md)

    def test_presenter_dispatch(self):
        self.assertIs(ui_evidence.presenter({"kind": "ui"}), ui_evidence)
        self.assertIs(ui_evidence.presenter({"shots": []}), evidence)
        self.assertIs(ui_evidence.presenter(None), evidence)

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_compare_writes_a_cropped_panel_only_for_changed_views(self):
        d = _tmp(self)
        (d / "base").mkdir()
        (d / "now").mkdir()
        _png(d / "base" / "a.png", GREY, 64, 40)
        _png(d / "now" / "a.png", lambda x, y: (250, 0, 0) if x < 8 and y < 8 else (40, 40, 40), 64, 40)
        _png(d / "base" / "b.png", GREY, 64, 40)
        _png(d / "now" / "b.png", GREY, 64, 44)       # the page grew
        _png(d / "base" / "c.png", GREY, 64, 40)
        _png(d / "now" / "c.png", GREY, 64, 40)
        rows = {r["name"]: r for r in ui_evidence.compare(
            d / "base", [d / "now" / n for n in ("a.png", "b.png", "c.png")], d / "out")}
        self.assertTrue(Path(rows["a"]["side_by_side"]).is_file())
        self.assertEqual(rows["a"]["box"], [0, 0, 8, 8])
        self.assertIsNone(rows["c"]["side_by_side"])
        # A grown page is padded and compared, not declared 100% changed.
        self.assertLess(rows["b"]["changed"], 1.0)
        self.assertEqual(rows["b"]["size"], {"before": [64, 40], "after": [64, 44]})


class NoVisibleChangeFlag(unittest.TestCase):
    """"No view changed" is only an alarm when the diff touched UI files."""

    def _capture(self, changed):
        d = _tmp(self)
        wt = d / "wt"
        (wt / "static").mkdir(parents=True)
        shot = d / "shot.png"
        _png(shot, GREY)

        def run_capture(tree, out, timeout=None):
            Path(out).mkdir(parents=True, exist_ok=True)
            p = Path(out) / "index-desktop-dark.png"
            shutil.copyfile(shot, p)
            return {"shots": [str(p)], "page_errors": {}}
        base = d / "base"
        base.mkdir()
        shutil.copyfile(shot, base / "index-desktop-dark.png")
        with mock.patch.object(ui_evidence, "run_capture", run_capture), \
                mock.patch.object(ui_evidence, "baseline", lambda wt, sha, timeout=None: (base, {})), \
                mock.patch.object(evidence, "merge_base", lambda wt, b: "abc"), \
                mock.patch.object(evidence, "contact_sheet", lambda *a, **k: None):
            return ui_evidence.capture(wt, d / "out", base="main", changed=changed)

    def test_ui_diff_with_no_changed_view_is_flagged(self):
        m = self._capture(["static/index.html"])
        self.assertTrue(m.get("no_visible_change"))

    def test_non_ui_diff_is_not_flagged(self):
        m = self._capture(["config.py"])
        self.assertFalse(m.get("no_visible_change"))
        self.assertEqual(m["warnings"], [])


class CropWindow(unittest.TestCase):
    def test_a_small_change_is_centred_with_context(self):
        x, y, w, h = ui_evidence.crop_rect((700, 500, 20, 10), 1440, 2000)
        self.assertTrue(x <= 700 and x + w >= 720 and y <= 500 and y + h >= 510)
        self.assertGreaterEqual(w, 720)

    def test_a_change_taller_than_the_window_shows_where_it_starts(self):
        """A wrapped tab shifts the whole page below it: the cause is at the
        top of the box, and centring the window cropped it away."""
        x, y, w, h = ui_evidence.crop_rect((0, 245, 390, 1700), 390, 2400)
        self.assertLessEqual(y, 245)
        self.assertEqual(h, 1400)


class ReviewerPrompts(unittest.TestCase):
    T = {"id": "t1", "title": "T", "prompt": "spec", "verify_cmd": "true"}
    UI = "diff --git a/static/index.html b/static/index.html\n+<b>x</b>\n"
    NON_UI = "diff --git a/config.py b/config.py\n+X = 1\n"

    def test_ui_diff_gets_the_visual_clause_in_both_prompts(self):
        for p in (code_tasks._review_prompt(self.T, self.UI),
                  code_tasks._pr_review_prompt(self.T, self.UI, 1, 1, [])):
            self.assertIn("VISUAL — this diff changes the dashboard UI", p)
            self.assertIn("visual regression", p)
            self.assertIn("tests/visual/golden/", p)

    def test_non_ui_diff_prompt_is_unchanged(self):
        with mock.patch.object(code_tasks, "_visual_review_prose", lambda d: ""):
            before = (code_tasks._review_prompt(self.T, self.NON_UI),
                      code_tasks._pr_review_prompt(self.T, self.NON_UI, 1, 1, []))
        after = (code_tasks._review_prompt(self.T, self.NON_UI),
                 code_tasks._pr_review_prompt(self.T, self.NON_UI, 1, 1, []))
        self.assertEqual(before, after)
        self.assertNotIn("VISUAL", after[0] + after[1])

    def test_evidence_block_follows_what_the_driver_can_see(self):
        m = _manifest(_tmp(self))

        class Blind:
            pass

        class Sees:
            sees_images = True
        self.assertIn("YOU CANNOT SEE", code_tasks._evidence_block(m, Blind()))
        self.assertIn("LOOK at each", code_tasks._evidence_block(m, Sees()))
        self.assertEqual(code_tasks._evidence_block(None, Sees()), "")


class DriversSeeImages(unittest.TestCase):
    def test_which_harnesses_see(self):
        self.assertFalse(drivers.sees_images(drivers.OpencodeDriver))  # GLM: 400 on images
        self.assertTrue(drivers.sees_images(drivers.ReasonixDriver))
        self.assertTrue(drivers.sees_images(drivers.ClaudeCodeDriver))
        self.assertFalse(drivers.sees_images(object()))

    def test_reasonix_is_told_to_call_view_image_not_decode_bytes(self):
        p = drivers._view_image_prompt("review this", ["/x/a.png", "/x/b.png"])
        self.assertIn("view_image", p)
        self.assertIn("Do NOT read the PNG bytes", p)
        self.assertIn("/x/a.png", p)
        self.assertEqual(drivers._view_image_prompt("review this", ()), "review this")
        drv = drivers.ReasonixDriver("DeepSeek-V4.1-Flash-thinking-max", "reviewer", bench=True)
        drv.images = ["/x/a.png"]
        self.assertIn("view_image", drv.argv("p", None)[-1])


BASIC = {"id": "t1", "title": "T1", "prompt": "do it", "verify_cmd": "true",
         "model": config.ESCALATION_PATH[0],
         "reviewer": config.cross_family_reviewer(config.ESCALATION_PATH[0])}


class GateCapturesUiEvidence(unittest.TestCase):
    """The gate captures screenshots for a UI diff, and only for one."""

    def _run(self, changed, capture):
        tmp = _tmp(self)
        (tmp / "static").mkdir()
        (tmp / "static" / "index.html").write_text("<html></html>")
        (tmp / "dashboard.py").write_text("")
        tf = tmp / "tf.json"
        tf.write_text(json.dumps({"project": {"repo": "/tmp", "title": "t",
                                              "tasks": [BASIC]}}))
        ts = code_tasks.load_taskfile(str(tf))
        with capture_events():
            g = code_tasks.build_code_graph(FakeStore(), ts, taskfile="tf.json")
        ctx = {"results": {"alloc_t1": {"worktree": str(tmp)},
                           "implement_t1": {"harness": "x"}}, "runs": {}}

        async def files(wt, base):
            return changed
        with mock.patch.object(config, "ROOT", str(tmp)), \
                mock.patch.object(config, "EVIDENCE_DIR", tmp / "ev"), \
                mock.patch.object(code_tasks, "_changed_files", files), \
                mock.patch.object(ui_evidence, "capture", capture), \
                mock.patch.object(code_tasks.board, "post", lambda *a, **k: None):
            with capture_events() as ev:
                res = asyncio.run(g.nodes["gate_t1"].fn(ctx))
        return res, ev

    def test_a_ui_diff_is_captured_and_handed_on(self):
        calls = []

        def cap(wt, out, **kw):
            calls.append(kw)
            return {"kind": "ui", "shots": [], "compare": [], "warnings": []}
        res, ev = self._run(["static/index.html"], cap)
        self.assertTrue(res["passed"])
        self.assertEqual(res["evidence"]["kind"], "ui")
        self.assertEqual(calls[0]["changed"], ["static/index.html"])
        self.assertTrue([e for e in ev.of("evidence.captured") if e.get("surface") == "ui"])

    def test_a_non_ui_diff_is_not_captured(self):
        def cap(*a, **k):
            raise AssertionError("captured a non-UI diff")
        res, _ = self._run(["config.py"], cap)
        self.assertTrue(res["passed"])
        self.assertIsNone(res["evidence"])

    def test_a_dashboard_that_will_not_render_fails_the_gate(self):
        def cap(*a, **k):
            raise evidence.EvidenceError("ImportError in dashboard.py")
        with mock.patch.object(config, "EVIDENCE_MODE", "required"):
            res, _ = self._run(["dashboard.py"], cap)
        self.assertFalse(res["passed"])
        self.assertIn("ImportError in dashboard.py", res["output"])

    def test_no_browser_never_fails_the_task(self):
        def cap(*a, **k):
            raise evidence.EvidenceUnavailable("chromium will not launch")
        res, ev = self._run(["static/index.html"], cap)
        self.assertTrue(res["passed"])
        self.assertTrue(ev.of("evidence.unavailable"))


if __name__ == "__main__":
    unittest.main()
