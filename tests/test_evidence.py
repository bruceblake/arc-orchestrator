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


def _png(path, pixel_at, w=64, h=36, alpha=None):
    """Write an 8-bit RGB (or RGBA) PNG whose pixel (x, y) is pixel_at(x, y)."""
    def rgba(x, y):
        px = bytes(pixel_at(x, y))
        return px + bytes([alpha(x, y)]) if alpha else px
    rows = b"".join(b"\x00" + b"".join(rgba(x, y) for x in range(w))
                    for y in range(h))
    ch = lambda t, d: (struct.pack(">I", len(d)) + t + d
                       + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff))
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n"
                           + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6 if alpha else 2, 0, 0, 0))
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

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
    def test_a_machine_without_a_font_still_writes_panels(self):
        """A missing font is an infrastructure gap, never a crashing capture."""
        with unittest.mock.patch.object(evidence, "_font_file", return_value=None):
            base, cur = self.d / "base", self.d / "cur"
            base.mkdir(), cur.mkdir()
            _png(base / "cam.png", lambda x, y: (9, 9, 9))
            _png(cur / "cam.png", lambda x, y: (9, 9, 9))
            row = evidence.compare(base, [cur / "cam.png"], self.d / "cmp")[0]
            self.assertIsNotNone(row["side_by_side"], "no font must not break the panel")


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

    def test_a_single_pixel_change_off_the_old_sample_lattice_is_found(self):
        """A change the stride-4 grid stepped over was captioned NO CHANGE and
        drew no box — a confident lie about a real edit. Every pixel counts."""
        _png(self.d / "a.png", lambda x, y: (40, 40, 40))
        for x, y in ((5, 5), (63, 35), (1, 2), (62, 34)):   # (5,5)/(63,35) were the misses
            _png(self.d / "b.png",
                 lambda px, py, x=x, y=y: (250, 30, 30) if (px, py) == (x, y)
                 else (40, 40, 40))
            changed, _delta, box = evidence.frame_diff(self.d / "a.png", self.d / "b.png")
            self.assertGreater(changed, 0.0, f"the 1px change at ({x}, {y}) was missed")
            self.assertEqual(box, (x, y, 1, 1), f"the box does not frame ({x}, {y})")

    def test_an_alpha_only_change_is_not_a_visual_change(self):
        """The diff panel renders rgb24; alpha alone is invisible, not a change."""
        for name, a in (("a", 255), ("b", 0)):
            _png(self.d / f"{name}.png", lambda x, y: (40, 40, 40),
                 alpha=lambda x, y, a=a: a)
        self.assertEqual(evidence.frame_diff(self.d / "a.png", self.d / "b.png"),
                         (0.0, 0.0, None))

    def test_frames_of_different_channel_counts_are_not_misaligned(self):
        """A baseline render and a new one need not both be RGBA. One stride
        for both compares misaligned pixels: identical frames once reported
        '0.75 changed' with a full-frame box."""
        _png(self.d / "rgb.png", lambda x, y: (40, 40, 40))
        _png(self.d / "rgba.png", lambda x, y: (40, 40, 40), alpha=lambda x, y: 255)
        self.assertEqual(evidence.frame_diff(self.d / "rgb.png", self.d / "rgba.png"),
                         (0.0, 0.0, None))
        _png(self.d / "red.png", lambda x, y: (250, 30, 30), alpha=lambda x, y: 255)
        changed, _delta, box = evidence.frame_diff(self.d / "rgb.png", self.d / "red.png")
        self.assertEqual(changed, 1.0, "a real change across formats must still be found")
        self.assertEqual(box, (0, 0, 64, 36))

    def test_a_low_contrast_change_is_not_declared_unchanged(self):
        """A 20/255 edit is visible and the heatmap glows at it. A detection
        tolerance above it captioned the panel NO CHANGE with no box: the
        picture said 'changed', the text said 'not'."""
        _png(self.d / "a.png", lambda x, y: (100, 100, 100))
        _png(self.d / "b.png",
             lambda x, y: (120, 100, 100) if 16 <= x < 48 and 10 <= y < 26
             else (100, 100, 100))
        changed, _delta, box = evidence.frame_diff(self.d / "a.png", self.d / "b.png")
        self.assertAlmostEqual(changed, 32 * 16 / (64 * 36), places=6)
        self.assertEqual(box, (16, 10, 32, 16))
        # and the smallest change a byte can hold is still a change
        _png(self.d / "c.png",
             lambda x, y: (101, 100, 100) if (x, y) == (9, 9) else (100, 100, 100))
        changed, _delta, box = evidence.frame_diff(self.d / "a.png", self.d / "c.png")
        self.assertGreater(changed, 0.0, "a 1/255 edit was declared no change")
        self.assertEqual(box, (9, 9, 1, 1))

    def test_an_identical_frame_is_still_no_change(self):
        """Lowering the tolerance must not make every frame 'changed'."""
        scene = lambda x, y: ((x * 4) % 256, (y * 7) % 256, (x + y) % 256)  # noqa: E731
        _png(self.d / "a.png", scene)
        _png(self.d / "b.png", scene)
        self.assertEqual(evidence.frame_diff(self.d / "a.png", self.d / "b.png"),
                         (0.0, 0.0, None))

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
        self.assertTrue(by["cam"]["box"], "the row records where it changed")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
    def test_a_low_contrast_panel_is_framed_not_no_change(self):
        """The end-to-end version of the second rejection: a 20/255 change
        must be framed by the box and not captioned NO CHANGE, so the panel
        text agrees with the glow the reviewer can see."""
        base, cur = self.d / "base", self.d / "cur"
        base.mkdir(), cur.mkdir()
        _png(base / "cam.png", lambda x, y: (100, 100, 100))
        _png(cur / "cam.png",
             lambda x, y: (120, 100, 100) if 16 <= x < 48 and 10 <= y < 26
             else (100, 100, 100))
        row = {r["name"]: r for r in evidence.compare(
            base, sorted(cur.glob("*.png")), self.d / "cmp")}["cam"]
        self.assertEqual(row["box"], [16, 10, 32, 16])
        w, h, c, rows = _rgb(Path(row["side_by_side"]))
        panel_w = w // 3
        bar = evidence._CAPTION_H * panel_w // evidence._PANEL_W
        # framed: red pixels sit at the patch, scaled into the panel
        s = panel_w / 64.0
        red = [x for y in range(h) for x in range(2 * panel_w, w)
               if (lambda q: q[0] > 150 and q[1] < 90 and q[2] < 90)(
                   tuple(rows[y][x * c:x * c + 3]))]
        self.assertTrue(red, "the 20/255 change was not framed")
        self.assertLess(abs(min(red) - 2 * panel_w - int(16 * s)), panel_w // 6,
                        "the box is not near the change")
        # not declared unchanged: NO CHANGE is large text in the panel BODY
        body_text = sum(1 for y in range(bar, h, 2) for x in range(2 * panel_w, w, 2)
                        if sum(rows[y][x * c:x * c + 3]) > 600)
        self.assertLess(body_text, 100, "the panel says NO CHANGE about a real edit")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
    def test_a_one_pixel_change_is_not_captioned_no_change(self):
        """The end-to-end version of the bug: the diff panel must draw a box
        and must NOT claim NO CHANGE for a change one pixel wide."""
        base, cur = self.d / "base", self.d / "cur"
        base.mkdir(), cur.mkdir()
        _png(base / "cam.png", lambda x, y: (110, 110, 110))
        _png(cur / "cam.png",
             lambda x, y: (255, 255, 255) if (x, y) == (5, 5) else (110, 110, 110))
        row = {r["name"]: r for r in evidence.compare(
            base, sorted(cur.glob("*.png")), self.d / "cmp")}["cam"]
        self.assertGreater(row["changed"], 0.0)
        self.assertEqual(row["box"], [5, 5, 1, 1])
        w, h, c, rows = _rgb(Path(row["side_by_side"]))
        panel_w = w // 3
        # the box is drawn: red pixels at the change, scaled into the panel
        s = panel_w / 64.0
        box_x, box_y = int(5 * s), int(5 * s)
        red = [(x, y) for y in range(h) for x in range(2 * panel_w, w)
               if (lambda q: q[0] > 150 and q[1] < 90 and q[2] < 90)(
                   tuple(rows[y][x * c:x * c + 3]))]
        self.assertTrue(red, "no box was drawn around the 1px change")
        self.assertLess(abs(min(x for x, _y in red) - 2 * panel_w - box_x), panel_w // 8,
                        "the box is not near the change")
        # and the panel is captioned with a percentage, not NO CHANGE
        lit = sum(1 for y in range(0, evidence._CAPTION_H * panel_w // evidence._PANEL_W)
                  for x in range(2 * panel_w, w, 2)
                  if sum(rows[y][x * c:x * c + 3]) > 600)
        self.assertGreater(lit, 20, "the diff panel has no caption at all")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
    def test_each_panel_carries_a_caption_bar(self):
        """A panel with no 'BEFORE'/'AFTER' on it cannot be told from its twin."""
        base, cur = self.d / "base", self.d / "cur"
        base.mkdir(), cur.mkdir()
        _png(base / "cam.png", lambda x, y: (30, 60, 90))
        _png(cur / "cam.png", lambda x, y: (30, 60, 90))
        row = {r["name"]: r for r in evidence.compare(
            base, sorted(cur.glob("*.png")), self.d / "cmp")}["cam"]
        w, h, c, rows = _rgb(Path(row["side_by_side"]))
        panel_w = w // 3
        # every panel carries a bar: it sits above the picture, and it is drawn
        # (white text) on all three, which is what tells them apart
        for i, panel in enumerate(("BEFORE", "AFTER", "DIFF")):
            lit = sum(1 for y in range(0, evidence._CAPTION_H * panel_w // evidence._PANEL_W)
                      for x in range(i * panel_w, (i + 1) * panel_w, 2)
                      if sum(rows[y][x * c:x * c + 3]) > 600)
            self.assertGreater(lit, 20, f"the {panel} panel has no caption")
        # and the picture under the bar is the shot itself, untouched: the panel
        # is exactly the caption bar taller than the source at panel scale
        _w0, h0, _c0, _rows0 = _rgb(cur / "cam.png")
        bar = h - int(round(h0 * panel_w / 64.0))
        self.assertEqual(bar, evidence._CAPTION_H * panel_w // evidence._PANEL_W)
        # a solid source, so below the bar every pixel must be that colour
        below = {tuple(rows[y][x * c:x * c + 3])
                 for y in range(bar + 1, h, 7) for x in range(3, panel_w - 3, 7)}
        self.assertEqual(len(below), 1, f"the picture below the caption changed: {below}")

    def test_the_changed_region_box_is_computed(self):
        a, b = self.d / "a.png", self.d / "b.png"
        _png(a, lambda x, y: (40, 40, 40))
        _png(b, lambda x, y: (250, 30, 30) if 24 <= x < 40 and 8 <= y < 20 else (40, 40, 40))
        x, y, w, h = evidence.change_box(a, b)
        self.assertLessEqual(x, 24, "the box starts after the change")
        self.assertLessEqual(y, 8, "the box starts below the change")
        self.assertGreaterEqual(x + w, 40, "the box ends before the change")
        self.assertGreaterEqual(y + h, 20, "the box ends above the change")
        self.assertLess(w * h, 64 * 36 // 2, "the box is the whole frame, not the change")
        self.assertIsNone(evidence.change_box(a, a), "identical frames have no box")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
    def test_an_unchanged_camera_says_no_change(self):
        """Black reads as a failed render as much as it reads as 'nothing changed'."""
        base, cur = self.d / "base", self.d / "cur"
        base.mkdir(), cur.mkdir()
        _png(base / "cam.png", lambda x, y: (60, 150, 60))
        _png(cur / "cam.png", lambda x, y: (60, 150, 60))
        row = {r["name"]: r for r in evidence.compare(
            base, sorted(cur.glob("*.png")), self.d / "cmp")}["cam"]
        self.assertEqual(row["changed"], 0.0)
        self.assertIsNone(row["box"], "nothing changed, so there is no box")
        w, h, c, rows = _rgb(Path(row["side_by_side"]))
        panel_w = w // 3
        bar = evidence._CAPTION_H * panel_w // evidence._PANEL_W   # bar in output px
        lit = sum(1 for y in range(bar, h, 2) for x in range(2 * panel_w, w, 2)
                  if sum(rows[y][x * c:x * c + 3]) > 600)
        self.assertGreater(lit, 100, "NO CHANGE was not drawn in the diff panel")
        # ... and away from the centered label the panel is the dimmed after
        # frame, never black enough to read as a failed render
        dark = sum(1 for y in range(bar + 2, h, 3) for x in range(2 * panel_w + 2, w, 3)
                   if not (2 * panel_w + 100 < x < w - 100)          # the label's own box
                   and sum(rows[y][x * c:x * c + 3]) < 24)
        self.assertEqual(dark, 0, "the diff panel is black: it reads as a failed render")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
    def test_the_diff_panel_glows_where_it_changed(self):
        """A wall of one flat colour, with a shape that moved: the moved shape
        is the only thing the heatmap may light up."""
        wall = lambda x, y: (110, 110, 110)                     # noqa: E731
        box_at = lambda x0: (lambda x, y:                          # noqa: E731
                             (250, 250, 250) if x0 <= x < x0 + 12 and 12 <= y < 24
                             else wall(x, y))
        base, cur = self.d / "base", self.d / "cur"
        base.mkdir(), cur.mkdir()
        _png(base / "cam.png", box_at(8))
        _png(cur / "cam.png", box_at(40))
        row = {r["name"]: r for r in evidence.compare(
            base, sorted(cur.glob("*.png")), self.d / "cmp")}["cam"]
        self.assertTrue(row["box"])                      # there is a region to frame
        w, h, c, rows = _rgb(Path(row["side_by_side"]))
        panel_w = w // 3
        bar = evidence._CAPTION_H * panel_w // evidence._PANEL_W
        s = panel_w / 64.0                               # output px per source px
        x, y, bw, bh = row["box"]
        # drawbox runs before the caption bar is padded on, so the box travels
        # down by `bar` output pixels with the picture.
        ox, oy, ox2, oy2 = x * s, bar + y * s, (x + bw) * s, bar + (y + bh) * s

        def bright(xx, yy):
            return sum(rows[yy][(2 * panel_w + xx) * c:(2 * panel_w + xx) * c + 3]) > 600

        inside = sum(1 for yy in range(int(oy), int(oy2)) for xx in range(int(ox), int(ox2))
                     if bright(xx, yy))
        area = (ox2 - ox) * (oy2 - oy)
        self.assertGreater(inside / area, 0.05,
                           "the changed region is not lit up in the diff panel")
        # nothing else in the picture glows (the caption bar does, by design)
        outside = sum(1 for yy in range(bar + 2, h) for xx in range(2, panel_w - 2)
                      if not (ox <= xx <= ox2 and oy <= yy <= oy2) and bright(xx, yy))
        self.assertEqual(outside, 0, "the heatmap glows where nothing changed")
        dark = sum(1 for yy in range(bar + 2, h, 3) for xx in range(2, panel_w - 2, 3)
                   if sum(rows[yy][(2 * panel_w + xx) * c:(2 * panel_w + xx) * c + 3]) < 24)
        self.assertEqual(dark, 0, "the diff panel is black: it reads as a failed render")


def _rgb(path):
    """(w, h, channels, rows) of a PNG via the studio palette reader."""
    from studio.evaluation import palette
    return palette.read_png(path)


class ContactSheet(unittest.TestCase):
    """One labeled grid of every shot: what a reviewer should see first."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        self.colors = {"north": (200, 20, 20), "east": (20, 200, 20),
                       "south": (20, 20, 200), "west": (200, 200, 20),
                       "roof": (200, 20, 200)}

    def _shots(self, names):
        out = []
        for n in names:
            col = self.colors[n]
            _png(self.d / f"{n}.png", lambda x, y, col=col: col)
            out.append((n, self.d / f"{n}.png"))
        return out

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
    def test_every_shot_has_its_tile_and_its_name(self):
        names = list(self.colors)
        sheet = evidence.contact_sheet(self._shots(names), self.d / "sheet")
        self.assertTrue(sheet.exists())
        self.assertLess(sheet.stat().st_size, evidence._MAX_BYTES)
        w, h, c, rows = _rgb(sheet)
        tile_w = w // evidence._SHEET_COLS
        rows_of_tiles = -(-len(names) // evidence._SHEET_COLS)
        tile_h = h // rows_of_tiles             # five tiles, three columns: two rows
        self.assertEqual(w, tile_w * evidence._SHEET_COLS)
        self.assertEqual(tile_h, evidence._TILE_H + evidence._LABEL_H)
        for i, n in enumerate(names):
            left = (i % evidence._SHEET_COLS) * tile_w
            top = (i // evidence._SHEET_COLS) * tile_h
            mid_x = (left + tile_w // 2) * c
            # the tile shows that camera's render ...
            self.assertEqual(tuple(rows[top + tile_h // 2][mid_x:mid_x + 3]), self.colors[n],
                             f"tile {i} is not {n}'s shot")
            # ... and its name is written in the band under it
            band = range(top + evidence._TILE_H, top + tile_h)
            lit = sum(1 for y in band for x in range(left + 8, left + tile_w - 8, 2)
                      if sum(rows[y][x * c:x * c + 3]) > 200)
            self.assertGreater(lit, 10, f"'{n}' is not written under its tile")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
    def test_scene_renders_are_in_the_sheet_too(self):
        """manifest['scenes'] is the other producer of shots (Rule 7d)."""
        _png(self.d / "north.png", lambda x, y: self.colors["north"])
        _png(self.d / "lab.png", lambda x, y: (11, 22, 33))
        manifest = {"shots": [str(self.d / "north.png")],
                    "scenes": [{"path": "res://scenes/labs/suspicion_lab.tscn",
                                "shots": [str(self.d / "lab.png")]}]}
        pairs = evidence.capture_shots(manifest)
        self.assertIn("suspicion_lab", [lbl for lbl, _p in pairs],
                      "a scene render has no name")
        self.assertEqual(len(pairs), 2, "a scene render has no tile")

    def test_an_image_over_two_megabytes_is_resized(self):
        """The PR embeds these inline; GitHub silently drops oversized blobs."""
        from studio.evaluation import palette
        over = self.d / "over.png"
        _png(over, lambda x, y: ((x * 7 + y * 13) % 256, (x * 31) % 256, (y * 17) % 256),
             w=700, h=700)
        with over.open("ab") as fh:                     # pad it past the limit
            fh.write(b"\x00" * (evidence._MAX_BYTES + 1024))
        resized = evidence._shrink(over)
        self.assertLess(resized.stat().st_size, evidence._MAX_BYTES)
        self.assertTrue(palette.read_png(resized)[:2], "the resized file is still a PNG")
        small = self.d / "small.png"
        _png(small, lambda x, y: (1, 2, 3))
        before = small.stat().st_size
        self.assertEqual(evidence._shrink(small).stat().st_size, before,
                         "an image under the limit is left alone")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
    def test_reviewers_are_sent_the_sheet_first(self):
        sheet = self.d / "contact_sheet.png"
        _png(sheet, lambda x, y: (5, 5, 5))
        manifest = _manifest(self.d)
        manifest["contact_sheet"] = str(sheet)
        self.assertEqual(Path(evidence.review_images(manifest)[0]), sheet)
        md = evidence.pr_markdown(manifest, "https://x/y/blob/b/r", task_id="t", attempt=1)
        self.assertLess(md.index("contact_sheet.png"), md.index("playtest.gif"),
                        "the contact sheet is not linked first")


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
