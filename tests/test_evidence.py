"""Visual evidence (evidence.py, AGENTS.md Rule 7d): every game change is SEEN.

Unit tests run everywhere. The one real-render test needs Godot, ffmpeg and a
display, and is skipped (with the reason) where they are missing — CI has none.
"""
import json
import math
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
        # Changed scenes lead the sheet, labelled <scene>/<view>.
        self.assertEqual(pairs[0][0], "suspicion_lab/lab", "a scene render has no name")
        self.assertEqual(len(pairs), 2, "a scene render has no tile")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
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
    shots.mkdir(parents=True, exist_ok=True)
    for n in ("overview", "corridor"):
        _png(shots / f"{n}.png", lambda x, y: (9, 9, 9))
    cmp = d / "compare"
    cmp.mkdir(exist_ok=True)
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
            "coverage": {"fixed_cameras": {"status": "captured", "reason": ""},
                         "flythrough": {"status": "captured", "reason": ""},
                         "playtest": {"status": "skipped",
                                      "reason": "tools/playtest.gd not found"},
                         "baseline": {"status": "captured", "reason": ""}},
            "godot_errors": ["SCRIPT ERROR: Invalid access to property 'hp'"],
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

    def test_prompt_block_carries_the_coverage_and_godot_errors(self):
        text = evidence.prompt_block(self.m)
        self.assertIn("coverage playtest: skipped — tools/playtest.gd not found", text)
        self.assertIn("coverage fixed_cameras: captured", text)
        self.assertIn("GODOT ERROR: SCRIPT ERROR: Invalid access", text)

    def test_pr_markdown_shows_the_coverage_table_and_godot_errors(self):
        md = evidence.pr_markdown(self.m, "https://github.com/o/r/blob/arc-evidence/t/x2",
                                  task_id="t", attempt=2)
        self.assertIn("**Evidence coverage**", md)
        self.assertIn("| playtest | skipped | tools/playtest.gd not found |", md)
        self.assertIn("**Godot errors**", md)
        self.assertIn("`SCRIPT ERROR: Invalid access to property 'hp'`", md)

    def test_board_body_names_the_gaps_without_a_table(self):
        body = evidence.board_body(self.m)
        self.assertIn("gaps: playtest:skipped (tools/playtest.gd not found)", body)
        self.assertIn("1 godot error(s)", body)


class Coverage(unittest.TestCase):
    """No silent gaps: every skipped or empty part is recorded AND flagged."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_playtest_without_the_script_says_so(self):
        video, note, reason = evidence._playtest(self.d, self.d, self.d, timeout=1)
        self.assertEqual((video, note), (None, None))
        self.assertEqual(reason, "tools/playtest.gd not found")

    def test_playtest_that_exits_0_with_no_video_says_so(self):
        """The x17 case: rc=0, no .avi — which used to read as 'no warning'."""
        (self.d / "tools").mkdir()
        (self.d / "tools" / "playtest.gd").write_text("# playtest\n")
        with unittest.mock.patch.object(evidence, "_godot", return_value=(0, "")):
            video, note, reason = evidence._playtest(self.d, self.d, self.d, timeout=1)
        self.assertIsNone(video)
        self.assertIsNone(note, "rc=0 is not a warning — the REASON carries it")
        self.assertEqual(reason, "playtest exited 0 but wrote no video")

    def test_godot_errors_are_parsed_deduped_and_capped(self):
        out = ("Godot Engine v4.3\n"
               "SCRIPT ERROR: Invalid access to property 'hp'\n"
               "ERROR: Cannot instantiate the scene\n"
               "  at: push_error (core/variant/variant_utility.cpp:1)\n"
               "Parse Error: unexpected token\n"
               "SCRIPT ERROR: Invalid access to property 'hp'\n"
               "this line is fine\n")
        errs = evidence.godot_errors(out)
        self.assertEqual(errs, ["SCRIPT ERROR: Invalid access to property 'hp'",
                                "ERROR: Cannot instantiate the scene",
                                "at: push_error (core/variant/variant_utility.cpp:1)",
                                "Parse Error: unexpected token"],
                         "deduped, in order, including push_error frames")
        self.assertEqual(evidence.godot_errors("nothing here\n"), [])
        self.assertEqual(evidence.godot_errors(None, ""), [])
        self.assertEqual(len(evidence.godot_errors(*[f"ERROR: {i}" for i in range(90)])), 30)

    def test_every_render_call_is_logged_for_the_parser(self):
        log = []
        with unittest.mock.patch("studio.engine.godot._run",
                                 return_value=(0, "SCRIPT ERROR: boom")):
            evidence._godot(self.d, ["--version"], timeout=1, log=log)
        self.assertEqual(log, ["SCRIPT ERROR: boom"])
        self.assertEqual(evidence.godot_errors(*log), ["SCRIPT ERROR: boom"])

    def test_no_visible_change_flag_on_and_off(self):
        rows = [{"name": "overview", "changed": 0.0},
                {"name": "corridor", "changed": 0.001}]
        self.assertTrue(evidence.no_visible_change(rows, ["player.gd"], []))
        # Off: something moved.
        self.assertFalse(evidence.no_visible_change(
            [{"name": "overview", "changed": 0.4}], ["player.gd"], []))
        # Off: not a gameplay diff (docs, tests, tools).
        self.assertFalse(evidence.no_visible_change(rows, [], []))
        self.assertFalse(evidence.no_visible_change(rows, ["tests/t.gd"], []))
        # Off: a scene render shows the change instead (an entry whose image is
        # really on disk — an ATTEMPTED scene with no file is not a render, see
        # test_a_scene_entry_with_no_image_is_not_a_scene_render).
        _png(self.d / "scene.png", lambda x, y: (9, 9, 9))
        self.assertFalse(evidence.no_visible_change(
            rows, ["player.gd"],
            [{"path": "res://w.tscn", "shots": [str(self.d / "scene.png")]}]))
        # Still ON when that same scene entry rendered nothing.
        self.assertTrue(evidence.no_visible_change(
            rows, ["player.gd"], [{"path": "res://w.tscn", "shots": []}]))
        # Off: nothing to compare (a `new` viewpoint is not "unchanged").
        self.assertFalse(evidence.no_visible_change([{"name": "n", "new": True}],
                                                    ["player.gd"], []))
        self.assertFalse(evidence.no_visible_change([], ["player.gd"], []))

    def test_the_threshold_is_configurable(self):
        rows = [{"name": "overview", "changed": 0.02}]
        self.assertFalse(evidence.no_visible_change(rows, ["player.gd"], []))
        self.assertTrue(evidence.no_visible_change(rows, ["player.gd"], [],
                                                   min_change=0.05))

    def test_gameplay_diff_ignores_tests_and_tools(self):
        subprocess.run(["git", "init", "-q", str(self.d)], check=True)
        for rel in ("player.gd", "world.tscn", "art/mat.tres",
                    "tests/test_player.gd", "tools/playtest.gd", "README.md"):
            path = self.d / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x")
        (self.d / "new.gd").write_text("y")            # untracked, still a change
        self.assertEqual(evidence.gameplay_diff(self.d, "HEAD"),
                         ["art/mat.tres", "new.gd", "player.gd", "world.tscn"])

    def test_prompt_block_leads_with_the_no_visible_change_demand(self):
        m = _manifest(self.d)
        m["no_visible_change"] = True
        m["gameplay_diff"] = ["scripts/suspicion.gd"]
        text = evidence.prompt_block(m)
        self.assertIn("NO VISIBLE CHANGE", text)
        self.assertIn("scripts/suspicion.gd", text)
        self.assertIn("REJECT for missing evidence", text)
        self.assertLess(text.index("NO VISIBLE CHANGE"), text.index("camera overview"))
        # Off by default: an ordinary capture does not carry the demand.
        self.assertNotIn("NO VISIBLE CHANGE",
                         evidence.prompt_block(_manifest(self.d)))

    def test_a_failed_baseline_render_is_failed_not_skipped(self):
        """Review: baseline() swallowed the render error and returned None.

        "the base commit will not render" and "this base has no Godot project"
        are different findings: the first is a defect a reviewer must read, the
        second is legal. Both used to collapse into one silent `skipped`."""
        repo = self.d / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "project.godot").write_text("[application]\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=a@b",
                        "-c", "user.name=t", "commit", "-qm", "init"], check=True)
        sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                             capture_output=True, text=True).stdout.strip()
        with unittest.mock.patch.object(config, "EVIDENCE_DIR", str(self.d / "ev")), \
                unittest.mock.patch.object(evidence, "_git"), \
                unittest.mock.patch.object(evidence, "is_godot_project",
                                           return_value=True), \
                unittest.mock.patch.object(evidence, "_cameras", return_value=[]), \
                unittest.mock.patch("studio.engine.godot.import_assets"), \
                unittest.mock.patch.object(
                    evidence, "_render_shots",
                    side_effect=evidence.EvidenceError("no camera rendered")):
            bdir, status, reason = evidence.baseline("p", str(repo), sha, timeout=1)
        self.assertIsNone(bdir)
        self.assertEqual(status, "failed")
        self.assertIn("baseline render failed", reason)
        self.assertIn("no camera rendered", reason, "the render error itself is lost")
        # A base that is not a Godot project is skipped, and says which.
        with unittest.mock.patch.object(config, "EVIDENCE_DIR", str(self.d / "ev2")), \
                unittest.mock.patch.object(evidence, "_git"), \
                unittest.mock.patch.object(evidence, "is_godot_project",
                                           return_value=False):
            bdir, status, reason = evidence.baseline("p", str(repo), sha, timeout=1)
        self.assertIsNone(bdir)
        self.assertEqual(status, "skipped")
        self.assertIn("not a Godot project", reason)

    def test_a_scene_entry_with_no_image_is_not_a_scene_render(self):
        """Review: a nonempty `scenes` list used to suppress the flag on its own."""
        _png(self.d / "real.png", lambda x, y: (9, 9, 9))
        rows = [{"name": "overview", "changed": 0.0}]
        # An entry whose shot is on disk is a real render: it suppresses the flag.
        real = [{"path": "res://w.tscn", "shots": [str(self.d / "real.png")]}]
        self.assertEqual(evidence.scene_shots(real), [str(self.d / "real.png")])
        self.assertFalse(evidence.no_visible_change(rows, ["player.gd"], real))
        # An entry that rendered nothing (no shots, or a missing file) is not.
        for empty in ([{"path": "res://w.tscn", "shots": []}],
                      [{"path": "res://w.tscn"}],
                      [{"path": "res://w.tscn", "shots": [str(self.d / "gone.png")]}]):
            self.assertEqual(evidence.scene_shots(empty), [],
                             f"{empty} claims a render it does not have")
            self.assertTrue(evidence.no_visible_change(rows, ["player.gd"], empty),
                            "an image-less scene entry hid a no-visible-change diff")

    def test_board_body_shows_every_row_and_every_error_line(self):
        """Review: the board showed '5 gaps' and a count, not the table/text.

        Rule 7d says the evidence goes to the agent board, so a reviewer who
        never opens the PR must be able to read WHICH kind was skipped and WHAT
        Godot said — a count hides exactly the part they need."""
        m = _manifest(self.d)
        m["coverage"] = {k: {"status": "captured", "reason": ""} for k in
                         ("fixed_cameras", "baseline")}
        m["coverage"]["playtest"] = {"status": "skipped",
                                     "reason": "tools/playtest.gd not found"}
        m["coverage"]["flythrough"] = {"status": "failed",
                                       "reason": "flythrough recording failed"}
        m["coverage"]["compare"] = {"status": "captured", "reason": ""}
        m["godot_errors"] = ["SCRIPT ERROR: Invalid access to property 'hp'",
                             "Parse Error: unexpected token"]
        body = evidence.board_body(m)
        # Every row, captured ones included — not just the gaps.
        for kind in ("fixed_cameras", "flythrough", "playtest", "baseline", "compare"):
            self.assertIn(f"| {kind} |", body, f"{kind} missing from the board table")
        self.assertIn("| playtest | skipped | tools/playtest.gd not found |", body)
        self.assertIn("| flythrough | failed | flythrough recording failed |", body)
        # The error TEXT, not a count.
        self.assertIn("SCRIPT ERROR: Invalid access to property 'hp'", body)
        self.assertIn("Parse Error: unexpected token", body)

    def test_board_body_still_leads_with_one_summary_line(self):
        """The legacy 400-char JSONL line keeps the summary; the table follows."""
        m = _manifest(self.d)
        m["coverage"] = {"playtest": {"status": "skipped", "reason": "no script"}}
        body = evidence.board_body(m)
        self.assertTrue(body.startswith("evidence: "),
                        "the board's one-line summary must come first")
        self.assertLess(body.index("evidence: "), body.index("**Evidence coverage**"))
        self.assertLess(len(body.splitlines()[0]), 400,
                        "the summary line itself exceeds the legacy cap")


class CaptureWiring(unittest.TestCase):
    """capture() itself, driven end to end with the renderers stubbed out.

    The unit tests above call the pieces; these call capture(), which is where
    the arguments are actually wired. A wrong argument (passing the
    scene_shots FUNCTION where a list belongs) raises only on the real path,
    and code_tasks.capture_evidence swallows it — the reviewer then got no
    images, no coverage and no flag, and nothing said so."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        self.wt = self.d / "wt"
        (self.wt / "scripts").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.wt)], check=True)
        (self.wt / "project.godot").write_text("[application]\n")
        (self.wt / "scripts" / "suspicion.gd").write_text("var hp = 1\n")
        (self.wt / "README.md").write_text("x\n")

    def _capture(self, *, repo=None, base=None, out=None, **kw):
        """capture() with renders stubbed: two PNGs, no videos, no playtest."""
        def fake_shots(project, cameras, out_dir, scratch, *, timeout, log=None):
            out_dir.mkdir(parents=True, exist_ok=True)
            made = []
            for i, name in enumerate(("overview", "corridor")):
                p = out_dir / f"{name}.png"
                _png(p, lambda x, y, i=i: (9 + i, 9, 9))
                made.append(p)
            return made

        old = config.EVIDENCE_DIR
        config.EVIDENCE_DIR = self.d / "ev"
        self.addCleanup(setattr, config, "EVIDENCE_DIR", old)
        with unittest.mock.patch.object(evidence, "_require_tools"), \
                unittest.mock.patch.object(evidence, "contact_sheet", return_value=None), \
                unittest.mock.patch("studio.engine.godot.import_assets"), \
                unittest.mock.patch.object(evidence, "_cameras",
                                           return_value=[{"name": "overview"},
                                                         {"name": "corridor"}]), \
                unittest.mock.patch.object(evidence, "_render_shots",
                                           side_effect=fake_shots), \
                unittest.mock.patch.object(
                    evidence, "_flythrough",
                    side_effect=evidence.EvidenceError("no movie")), \
                unittest.mock.patch.object(evidence, "_playtest",
                                           return_value=(None, None, "no script")):
            return evidence.capture(self.wt, out or self.d / "out", project="p",
                                    repo=repo, base=base, **kw)

    def test_a_gameplay_diff_flags_without_raising(self):
        """Review: capture() passed the scene_shots FUNCTION, so any gameplay
        diff raised TypeError before the manifest was written — and
        capture_evidence swallowed it, dropping the capture entirely."""
        m = self._capture()                  # no merge base -> no compare rows
        self.assertEqual(m["gameplay_diff"], ["scripts/suspicion.gd"],
                         "the gameplay diff was not detected")
        self.assertEqual(len(m["shots"]), 2, "the capture produced no manifest")
        # No comparison row means "nothing to compare", not "no change".
        self.assertNotIn("no_visible_change", m)
        self.assertEqual(m["coverage"]["fixed_cameras"]["status"], "captured")
        self.assertEqual(m["coverage"]["baseline"]["reason"], "no merge base")

    def test_an_unchanged_gameplay_diff_sets_the_flag(self):
        """The same call with compare rows that all moved under the threshold."""
        rows = [{"name": "overview", "changed": 0.0},
                {"name": "corridor", "changed": 0.001}]
        with unittest.mock.patch.object(evidence, "compare", return_value=rows), \
                unittest.mock.patch.object(evidence, "merge_base",
                                           return_value="a" * 40), \
                unittest.mock.patch.object(evidence, "baseline",
                                           return_value=(Path("/nonexistent"),
                                                         "captured", "")):
            m = self._capture(repo=str(self.wt), base="main")
        self.assertTrue(m.get("no_visible_change"),
                        "an invisible gameplay diff went unflagged")
        self.assertEqual(m["compare"], rows)
        self.assertTrue(any("NO VISIBLE CHANGE" in w for w in m["warnings"]))


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
            # Dispatched through ui_evidence.presenter: a game capture is still
            # presented by evidence.py, a dashboard capture by ui_evidence.py.
            self.assertIn("ui_evidence.presenter(shown).review_images(shown)", body, start)
            self.assertIn("_evidence_block(shown, ", body, start)
        self.assertIn("return evidence.prompt_block(shown)",
                      self._body("def _evidence_block(", "def _visual_review_prose("))

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


# --- changed scenes (the PR #19 regression) ----------------------------------
#
# prison-escape-test PR #19 added scenes/labs/security_cameras.tscn. The
# capture only ever rendered the MAIN scene from its fixed cameras, so every
# comparison read 0.0%, the manifest said NO VISIBLE CHANGE, and the PR
# comment showed four photos of an unchanged prison yard.

_PR19_STATUS = {
    "scenes/labs/security_cameras.tscn": "A",
    "scripts/security/blind_spot_map.gd": "A",
    "scripts/security/security_camera.gd": "A",
    "scripts/security/security_camera.gd.uid": "A",
    "tests/run_tests.tscn": "M",
    "tests/test_security_cameras.gd": "A",
}
_PR19_TEXTS = {
    "scenes/labs/security_cameras.tscn":
        '[ext_resource type="Script" path="res://scripts/security/security_camera.gd" id="1"]\n',
    "scenes/world.tscn": '[ext_resource type="PackedScene" path="res://scenes/graybox_prison.tscn" id="1"]\n',
    "scenes/labs/vent_lab.tscn": "[node name=\"VentLab\" type=\"Node3D\"]\n",
    "tests/run_tests.tscn":
        '[ext_resource type="Script" path="res://scripts/security/security_camera.gd" id="1"]\n',
}


class ChangedScenes(unittest.TestCase):
    def test_the_pr19_diff_selects_the_lab_scene_it_added(self):
        picked, skipped = evidence.changed_scenes(_PR19_STATUS, _PR19_TEXTS, limit=6)
        self.assertEqual([e["path"] for e in picked],
                         ["scenes/labs/security_cameras.tscn"],
                         "the scene the diff added must be rendered — and a test "
                         "scene that uses the same script must not")
        self.assertEqual(picked[0]["status"], "added")
        self.assertEqual(picked[0]["res"], "res://scenes/labs/security_cameras.tscn")
        self.assertEqual(skipped, [])

    def test_a_scene_that_uses_a_changed_script_is_rendered_too(self):
        status = {"scripts/player.gd": "M", "scenes/labs/a.tscn": "M",
                  "scenes/labs/new.tscn": "A", "scenes/old.tscn": "D"}
        texts = {"scenes/labs/a.tscn": "",
                 "scenes/labs/new.tscn": "",
                 "scenes/world.tscn": 'path="res://scripts/player.gd"',
                 "scenes/menu.tscn": 'path="res://scripts/menu.gd"'}
        picked, skipped = evidence.changed_scenes(status, texts, limit=6)
        self.assertEqual([(e["path"], e["status"]) for e in picked],
                         [("scenes/labs/new.tscn", "added"),
                          ("scenes/labs/a.tscn", "changed"),
                          ("scenes/world.tscn", "dependent")])
        self.assertIn("scripts/player.gd", picked[2]["why"])
        # The cap keeps the most direct first and REPORTS the rest.
        picked, skipped = evidence.changed_scenes(status, texts, limit=1)
        self.assertEqual([e["path"] for e in picked], ["scenes/labs/new.tscn"])
        self.assertEqual([e["path"] for e in skipped],
                         ["scenes/labs/a.tscn", "scenes/world.tscn"])

    def test_name_status_is_parsed(self):
        text = "A\tscenes/a.tscn\nM\tscripts/b.gd\nD\tscenes/gone.tscn\n\n"
        self.assertEqual(evidence.parse_name_status(text),
                         {"scenes/a.tscn": "A", "scripts/b.gd": "M",
                          "scenes/gone.tscn": "D"})

    def test_scene_texts_skip_tests_tools_and_the_import_cache(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        for rel in ("scenes/a.tscn", "tests/t.tscn", "tools/x.tscn",
                    ".godot/imported/c.tscn", "addons/p/d.tscn"):
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text("[gd_scene]\n")
        self.assertEqual(sorted(evidence.scene_texts(d)), ["scenes/a.tscn"])


class AutoFraming(unittest.TestCase):
    """Cameras placed around a scene's content, not at fixed world spots."""

    BOXES = ({"position": [-0.4, -0.2, -0.4], "size": [20.8, 3.4, 20.8]},   # the lab
             {"position": [100.0, 0.0, -50.0], "size": [2.0, 30.0, 2.0]},   # a tower
             {"position": [0.0, 0.0, 0.0], "size": [0.1, 0.1, 0.1]})        # a prop

    def test_every_corner_of_the_content_is_in_every_view(self):
        for b in self.BOXES:
            for cam in evidence.frame_cameras(b):
                for corner in evidence._corners(b):
                    self.assertTrue(evidence.in_frame(cam, corner),
                                    f"{cam['name']} cuts off {corner} of {b}")

    def test_the_fit_is_tight_not_a_speck(self):
        """A camera 20% closer must lose a corner: the content fills the view."""
        b = self.BOXES[0]
        c = evidence._center(b)
        for cam in evidence.frame_cameras(b):
            closer = dict(cam, position=[ci + (p - ci) * 0.8
                                         for p, ci in zip(cam["position"], c)])
            self.assertFalse(all(evidence.in_frame(closer, k)
                                 for k in evidence._corners(b)),
                             f"{cam['name']} is framed loosely")

    def test_cameras_look_at_the_content(self):
        b = self.BOXES[1]
        centre = evidence._center(b)
        cams = evidence.frame_cameras(b)
        self.assertEqual({c["name"] for c in cams}, {"overview", "top", "front", "side"})
        for c in cams:
            self.assertEqual(c["look_at"], [round(x, 4) for x in centre])
            self.assertTrue(evidence.in_frame(c, centre))

    def test_bounds_union_and_outliers(self):
        room = [0, 0, 0, 20, 3, 20]
        pillar = [9, 0, 9, 2, 3, 2]
        ground = [-5000, -1, -5000, 10000, 1, 10000]
        self.assertEqual(evidence.scene_bounds([room, pillar]),
                         {"position": [0, 0, 0], "size": [20, 3, 20]})
        # A 10 km ground plane does not shrink the room to a speck.
        self.assertEqual(evidence.scene_bounds([room, pillar, pillar, ground]),
                         {"position": [0, 0, 0], "size": [20, 3, 20]})
        self.assertIsNone(evidence.scene_bounds([]))
        self.assertIsNone(evidence.scene_bounds([[0, 0, 0, float("inf"), 1, 1]]))

    def test_a_2d_scene_gets_one_screen_camera(self):
        cams = evidence.frame_cameras(None)
        self.assertEqual([c["name"] for c in cams], ["screen"])

    def test_the_orbit_circles_at_one_radius_and_closes(self):
        b = self.BOXES[0]
        c = evidence._center(b)
        orbit = evidence.orbit_cameras(b, n=8)
        self.assertEqual(len(orbit), 9)
        self.assertEqual(orbit[0]["position"], orbit[-1]["position"])
        radii = {round(math.dist(o["position"], c), 2) for o in orbit}
        self.assertEqual(len(radii), 1, f"the orbit bobs: {radii}")
        for o in orbit:
            for corner in evidence._corners(b):
                self.assertTrue(evidence.in_frame(o, corner))


class ScenesDecideTheFlag(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        _png(self.d / "after.png", lambda x, y: (9, 9, 9))

    def _scene(self, **kw):
        return dict({"path": "scenes/labs/security_cameras.tscn", "status": "changed",
                     "why": "modified by this diff",
                     "shots": [str(self.d / "after.png")]}, **kw)

    def test_the_pr19_case_is_not_no_visible_change(self):
        """A new scene rendered on its own IS the visible change, even while
        every fixed camera on the main scene reads 0.0%."""
        yard = [{"name": n, "changed": 0.0} for n in
                ("cell_corridor", "guard_station", "isometric_overview")]
        gameplay = ["scenes/labs/security_cameras.tscn",
                    "scripts/security/security_camera.gd"]
        self.assertTrue(evidence.no_visible_change(yard, gameplay, []),
                        "precondition: the fixed cameras alone flag it")
        self.assertFalse(evidence.no_visible_change(
            yard, gameplay, [self._scene(status="added", compare=[])]))

    def test_a_changed_scene_that_moved_nothing_is_flagged(self):
        flat = [{"name": "top", "changed": 0.0}, {"name": "side", "changed": 0.001}]
        self.assertTrue(evidence.no_visible_change(
            [{"name": "yard", "changed": 0.3}], ["scripts/a.gd"],
            [self._scene(compare=flat)]),
            "the changed scene is the evidence; a busy main scene cannot hide it")
        moved = [{"name": "top", "changed": 0.016}]
        self.assertFalse(evidence.no_visible_change(
            [{"name": "yard", "changed": 0.0}], ["scripts/a.gd"],
            [self._scene(compare=moved)]))


def _scene_manifest(d):
    """_manifest plus one changed scene (compared) and one new scene."""
    m = _manifest(d)
    sdir = d / "scenes" / "scenes-labs-lab"
    for sub in ("after", "compare"):
        (sdir / sub).mkdir(parents=True, exist_ok=True)
    for n in ("top", "side"):
        _png(sdir / "after" / f"{n}.png", lambda x, y: (1, 2, 3))
        _png(sdir / "compare" / f"{n}.png", lambda x, y: (4, 5, 6))
    (sdir / "orbit.gif").write_bytes(b"x")
    (sdir / "orbit.mp4").write_bytes(b"x")
    ndir = d / "scenes" / "scenes-labs-new"
    (ndir / "after").mkdir(parents=True, exist_ok=True)
    _png(ndir / "after" / "top.png", lambda x, y: (7, 8, 9))
    m["out_dir"] = str(d)
    m["scenes"] = [
        {"path": "scenes/labs/lab.tscn", "status": "changed", "why": "modified by this diff",
         "shots": [str(sdir / "after" / "top.png"), str(sdir / "after" / "side.png")],
         "compare": [{"name": "top", "changed": 0.016,
                      "side_by_side": str(sdir / "compare" / "top.png")},
                     {"name": "side", "changed": 0.027,
                      "side_by_side": str(sdir / "compare" / "side.png")}],
         "max_changed": 0.027,
         "video": {"mp4": str(sdir / "orbit.mp4"), "gif": str(sdir / "orbit.gif")}},
        {"path": "scenes/labs/new.tscn", "status": "added", "why": "added by this diff",
         "shots": [str(ndir / "after" / "top.png")], "compare": [], "max_changed": None}]
    return m


class ScenesArePresentedFirst(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        self.m = _scene_manifest(self.d)
        sheet = self.d / "contact_sheet.png"
        _png(sheet, lambda x, y: (5, 5, 5))
        self.m["contact_sheet"] = str(sheet)
        self.web = "https://github.com/o/r/blob/arc-evidence/t/x2"

    def test_pr_comment_leads_with_the_changed_scenes_and_their_share(self):
        md = evidence.pr_markdown(self.m, self.web, task_id="t", attempt=2)
        top = md.index("Changed scenes")
        for later in ("contact_sheet.png", "playtest.gif", "flythrough.gif",
                      "compare/overview.png"):
            self.assertLess(top, md.index(later), f"{later} comes before the scenes")
        self.assertIn("| `scenes/labs/lab.tscn` | changed: modified by this diff | **2.7%** |", md)
        self.assertIn("| `scenes/labs/new.tscn` | added: added by this diff | new scene |", md)
        # The most-changed view is the one shown open, inline, raw.
        side = f"{self.web}/scenes/scenes-labs-lab/compare/side.png?raw=true"
        self.assertIn(f"![lab side]({side})", md)
        self.assertLess(md.index(side), md.index("<details>"))
        # New scenes show their after shots inline; videos are gif + mp4 link.
        self.assertIn(f'<img src="{self.web}/scenes/scenes-labs-new/after/top.png?raw=true"', md)
        self.assertIn(f"![lab orbit]({self.web}/scenes/scenes-labs-lab/orbit.gif?raw=true)", md)
        self.assertIn(f"({self.web}/scenes/scenes-labs-lab/orbit.mp4?raw=true)", md)
        # The main scene's fixed cameras are demoted, not dropped.
        self.assertIn("<summary>Main scene, fixed cameras", md)

    def test_an_unmoved_changed_scene_is_flagged_in_the_table(self):
        self.m["scenes"][0]["max_changed"] = 0.0
        md = evidence.pr_markdown(self.m, self.web, task_id="t", attempt=2)
        self.assertIn("**0.0%** 🚩", md)

    def test_reviewers_get_the_most_changed_scene_panel_first(self):
        imgs = [Path(p) for p in evidence.review_images(self.m)]
        self.assertEqual(imgs[0], self.d / "scenes/scenes-labs-lab/compare/side.png")
        self.assertEqual(imgs[1], self.d / "scenes/scenes-labs-lab/compare/top.png")
        self.assertEqual(imgs[2], self.d / "scenes/scenes-labs-new/after/top.png")
        self.assertEqual(imgs[3].name, "contact_sheet.png")

    def test_prompt_makes_an_invisible_change_blocking(self):
        text = evidence.prompt_block(self.m)
        self.assertIn("BLOCKING RULE", text)
        self.assertIn("the change is not visible in the evidence", text)
        self.assertLess(text.index("CHANGED SCENES"), text.index("camera overview"))
        self.assertIn("scenes/labs/lab.tscn (changed: modified by this diff) — 2.7% "
                      "of pixels changed at most", text)
        self.assertIn("scenes/labs/new.tscn (added: added by this diff) — NEW", text)
        self.assertIn("image: " + str(self.d / "scenes/scenes-labs-lab/compare/side.png"), text)
        self.m["non_visual"] = True
        text = evidence.prompt_block(self.m)
        self.assertNotIn("BLOCKING RULE", text)
        self.assertIn("NON-VISUAL", text)

    def test_board_names_each_changed_scene(self):
        body = evidence.board_body(self.m)
        self.assertIn("changed scenes: lab 2.7%, new new", body.splitlines()[0])


class CaptureScenesWiring(unittest.TestCase):
    """capture_scenes() on a real git repo, Godot stubbed: a modified scene
    is rendered after AND at the branch point with the same cameras, and the
    difference is measured."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        self.wt = self.d / "game"
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        self.git = lambda *a: subprocess.run(["git", "-C", str(self.wt), *a], env=env,
                                             check=True, capture_output=True,
                                             text=True).stdout.strip()
        (self.wt / "scenes").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.wt)], check=True)
        (self.wt / "project.godot").write_text("[application]\n")
        (self.wt / "scenes" / "lab.tscn").write_text("pillar at 10\n")
        (self.wt / "scenes" / "yard.tscn").write_text("unchanged\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        (self.wt / "scenes" / "lab.tscn").write_text("pillar at 15\n")
        (self.wt / "scenes" / "new.tscn").write_text("new\n")

    def test_a_modified_scene_is_compared_and_a_new_one_is_shown(self):
        calls = []

        def fake_render(project, cameras, out_dir, scratch, *, timeout, log=None,
                        scene=None):
            calls.append((Path(project) == self.wt, scene, [c["name"] for c in cameras]))
            text = (Path(project) / scene[len("res://"):]).read_text()
            col = (200, 0, 0) if "15" in text else (0, 0, 200)
            out_dir.mkdir(parents=True, exist_ok=True)
            made = []
            for c in cameras:
                _png(out_dir / f"{c['name']}.png",
                     lambda x, y: col if x < 20 else (50, 50, 50))
                made.append(out_dir / f"{c['name']}.png")
            return made

        with unittest.mock.patch.object(
                evidence, "_probe_scene",
                return_value=({"position": [0, 0, 0], "size": [20, 3, 20]}, 0)), \
                unittest.mock.patch.object(evidence, "_render_shots",
                                           side_effect=fake_render), \
                unittest.mock.patch.object(evidence, "_scene_video",
                                           side_effect=evidence.EvidenceError("x")), \
                unittest.mock.patch.object(evidence, "_ffmpeg",
                                           side_effect=evidence.EvidenceError("no ffmpeg")), \
                unittest.mock.patch("studio.engine.godot.import_assets"):
            # ffmpeg only draws the panels (CI has none); the measurement is
            # frame_diff, pure Python, and is what this asserts.
            picked, skipped, warns = evidence.capture_scenes(
                self.wt, self.d / "out", str(self.d), repo=self.wt, sha=self.base,
                timeout=5)
        by = {e["path"]: e for e in picked}
        self.assertEqual(sorted(by), ["scenes/lab.tscn", "scenes/new.tscn"],
                         "the unchanged yard must not be rendered")
        lab = by["scenes/lab.tscn"]
        self.assertEqual(lab["status"], "changed")
        self.assertEqual(len(lab["compare"]), 4)
        self.assertGreater(lab["max_changed"], 0.2, "the moved pillar was not measured")
        # Same cameras on both sides: before and after are the same viewpoints.
        before = [c for c in calls if not c[0]]
        self.assertEqual(before, [(False, "res://scenes/lab.tscn",
                                   ["overview", "top", "front", "side"])])
        self.assertEqual(by["scenes/new.tscn"]["compare"], [])
        self.assertEqual(len(by["scenes/new.tscn"]["shots"]), 4)
        self.assertEqual(skipped, [])
        # The throwaway branch-point worktree is gone.
        self.assertEqual(self.git("worktree", "list").count("\n"), 0)

    def test_a_scene_that_will_not_load_is_a_warning_not_a_crash(self):
        with unittest.mock.patch.object(
                evidence, "_probe_scene",
                side_effect=evidence.EvidenceError("could not load res://scenes/lab.tscn")):
            picked, _skipped, warns = evidence.capture_scenes(
                self.wt, self.d / "out", str(self.d), timeout=5)
        self.assertTrue(all(e["error"] for e in picked))
        self.assertTrue(any("did not render" in w for w in warns))


class PlaytestRecording(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        (self.d / "tools").mkdir()
        (self.d / "tools" / "playtest.gd").write_text("extends SceneTree\n")

    def test_the_wrapper_extends_the_games_playtest_with_light_and_a_camera(self):
        w = evidence.PLAYTEST_WRAPPER
        self.assertTrue(w.startswith('extends "res://tools/playtest.gd"'))
        self.assertIn("super()", w)
        self.assertIn("DirectionalLight3D.new()", w)
        self.assertIn("EvidenceChaseCamera", w)
        self.assertIn("_arc_cutaway", w)

    def test_the_wrapper_is_recorded_first_and_the_bare_script_is_the_fallback(self):
        scripts = []

        def fake(project, args, *, timeout, log=None):
            script = args[args.index("--script") + 1]
            scripts.append(script)
            avi = Path(args[args.index("--write-movie") + 1])
            avi.write_bytes(b"avi")
            return 1, "Parse Error: cannot extend" if script.endswith(
                "playtest_evidence.gd") else ""

        with unittest.mock.patch.object(evidence, "_godot", side_effect=fake), \
                unittest.mock.patch.object(evidence, "_video",
                                           return_value=("m.mp4", "m.gif")):
            video, _note, reason = evidence._playtest(self.d, self.d, self.d, timeout=1)
        self.assertEqual([Path(s).name for s in scripts],
                         ["playtest_evidence.gd", "playtest.gd"])
        self.assertEqual(video, ("m.mp4", "m.gif"))
        self.assertEqual(reason, "")

    def _record(self, results):
        """_playtest with Godot faked: `results[name]` = (rc, out, video bytes)."""
        runs, fed = [], []

        def fake(project, args, *, timeout, log=None):
            name = Path(args[args.index("--script") + 1]).name
            runs.append(name)
            rc, out, data = results[name]
            if data:
                Path(args[args.index("--write-movie") + 1]).write_bytes(data)
            return rc, out

        def fake_video(avi, stem):
            fed.append(Path(avi).read_bytes())
            return ("m.mp4", "m.gif")

        with unittest.mock.patch.object(evidence, "_godot", side_effect=fake), \
                unittest.mock.patch.object(evidence, "_video", side_effect=fake_video):
            video, note, reason = evidence._playtest(self.d, self.d, self.d, timeout=1)
        return runs, fed, video, note

    def test_a_wrapper_that_exits_non_zero_falls_back_to_the_bare_script(self):
        runs, fed, video, note = self._record({
            "playtest_evidence.gd": (1, "EVIDENCE_PLAYTEST_CAMERA\nSCRIPT ERROR", b"wrapped"),
            "playtest.gd": (0, "", b"bare")})
        self.assertEqual(runs, ["playtest_evidence.gd", "playtest.gd"])
        self.assertEqual(fed, [b"bare"])
        self.assertIsNone(note)

    def test_the_wrapper_video_is_kept_when_the_bare_run_records_nothing(self):
        runs, fed, video, note = self._record({
            "playtest_evidence.gd": (1, "EVIDENCE_PLAYTEST_CAMERA", b"wrapped"),
            "playtest.gd": (1, "", b"")})
        self.assertEqual(runs, ["playtest_evidence.gd", "playtest.gd"])
        self.assertEqual(fed, [b"wrapped"])
        self.assertEqual(video, ("m.mp4", "m.gif"))
        self.assertIn("exited 1", note)

    def test_a_clean_wrapper_run_is_not_repeated(self):
        runs, fed, _video, note = self._record({
            "playtest_evidence.gd": (0, "EVIDENCE_PLAYTEST_CAMERA", b"wrapped"),
            "playtest.gd": (0, "", b"bare")})
        self.assertEqual(runs, ["playtest_evidence.gd"])
        self.assertEqual(fed, [b"wrapped"])
        self.assertIsNone(note)


class ResumedReviewsSeeEvidence(unittest.TestCase):
    def test_latest_manifest_is_the_highest_attempt(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        old = config.EVIDENCE_DIR
        config.EVIDENCE_DIR = d
        self.addCleanup(setattr, config, "EVIDENCE_DIR", old)
        for n in (2, 9, 10):
            a = d / "proj" / "task" / f"x{n}"
            a.mkdir(parents=True)
            (a / "manifest.json").write_text(json.dumps({"n": n}))
        self.assertEqual(evidence.latest_manifest("proj", "task"),
                         {"n": 10, "attempt": 10})
        self.assertIsNone(evidence.latest_manifest("proj", "other"))

    def test_both_reviews_fall_back_to_the_capture_on_disk(self):
        src = (ROOT / "code_tasks.py").read_text()
        self.assertEqual(src.count("shown = review_evidence(ctx)"), 2)
        self.assertIn("evidence.latest_manifest(project_slug, tid)", src)
        self.assertIn("non_visual=evidence.non_visual(t)", src)


def _load_task(tmp, **flags):
    """One task loaded through the real loader, with `flags` on it."""
    import code_tasks
    model = config.ESCALATION_PATH[0]
    task = {"id": "t1", "title": "T1", "prompt": "do it", "model": model,
            "reviewer": config.cross_family_reviewer(model), **flags}
    path = Path(tmp) / "tf.json"
    path.write_text(json.dumps({"project": {"repo": str(tmp), "title": "t",
                                            "tasks": [task]}}))
    return code_tasks.load_taskfile(str(path))["tasks"]["t1"]


class TaskFlagsReachRuntime(unittest.TestCase):
    """`"visual": false` and `"evidence": false` survive the loader and change
    what the pipeline does — the loader once rebuilt each task from a fixed
    key set and silently dropped both."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        (self.d / "project.godot").write_text("[application]\n")

    def test_the_loader_keeps_the_flags(self):
        t = _load_task(self.d, visual=False, evidence=False)
        self.assertIs(t["visual"], False)
        self.assertIs(t["evidence"], False)
        plain = _load_task(self.d)
        self.assertNotIn("visual", plain)
        self.assertNotIn("evidence", plain)

    def test_a_non_boolean_flag_is_rejected(self):
        for flag in ("visual", "evidence"):
            with self.assertRaises(ValueError) as cm:
                _load_task(self.d, **{flag: "false"})
            self.assertIn(f"{flag} must be true or false", str(cm.exception))

    def test_evidence_false_turns_the_capture_off(self):
        self.assertTrue(evidence.enabled_for(self.d, _load_task(self.d)))
        self.assertFalse(evidence.enabled_for(self.d, _load_task(self.d, evidence=False)))

    def test_visual_false_drops_the_blocking_rule(self):
        m = {"shots": [], "compare": [], "scenes": []}
        t = _load_task(self.d, visual=False)
        self.assertTrue(evidence.non_visual(t))
        text = evidence.prompt_block({**m, "non_visual": evidence.non_visual(t)})
        self.assertNotIn("BLOCKING RULE", text)
        self.assertIn("NON-VISUAL", text)
        t = _load_task(self.d)
        self.assertFalse(evidence.non_visual(t))
        self.assertIn("BLOCKING RULE",
                      evidence.prompt_block({**m, "non_visual": evidence.non_visual(t)}))


class NonVisualSurvivesResume(unittest.TestCase):
    """A resumed review reads the manifest from disk (review_evidence ->
    latest_manifest), so the non-visual flag must be IN the written file."""

    setUp = CaptureWiring.setUp
    _capture = CaptureWiring._capture

    def test_the_written_manifest_carries_the_flag(self):
        old = config.EVIDENCE_DIR
        config.EVIDENCE_DIR = self.d / "ev"
        self.addCleanup(setattr, config, "EVIDENCE_DIR", old)
        t = _load_task(self.d, visual=False)
        self._capture(out=evidence.run_dir("p", "t1", 3),
                      non_visual=evidence.non_visual(t))
        m = evidence.latest_manifest("p", "t1")
        self.assertIs(m["non_visual"], True)
        self.assertNotIn("BLOCKING RULE", evidence.prompt_block(m))

    def test_a_visual_task_still_gets_the_blocking_rule_on_resume(self):
        old = config.EVIDENCE_DIR
        config.EVIDENCE_DIR = self.d / "ev"
        self.addCleanup(setattr, config, "EVIDENCE_DIR", old)
        self._capture(out=evidence.run_dir("p", "t1", 1))
        m = evidence.latest_manifest("p", "t1")
        self.assertIs(m["non_visual"], False)
        self.assertIn("BLOCKING RULE", evidence.prompt_block(m))


if __name__ == "__main__":
    unittest.main()
