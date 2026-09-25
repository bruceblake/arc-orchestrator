#!/usr/bin/env python3
"""Compare screenshots against committed goldens, with a tolerance.

    ./py tools/visual/compare.py <golden_dir> <actual_dir> [--tolerance 0.00002]
        [--threshold 8] [--require-baseline] [--panels <dir>]

Per PNG in <actual_dir>: PASS (differs by no more than the tolerance), DIFF
(more than that), or NO-BASELINE (no golden of that name). A golden with no
capture is MISSING — a view that stopped rendering is a regression. Exit 1 on
any DIFF or MISSING; NO-BASELINE fails only with --require-baseline.

Two numbers make the tolerance, and both are needed:
  threshold  a pixel counts as changed when any colour channel moved by MORE
             than this (0-255) — room for anti-aliasing if a font or Chromium
             build shifts a glyph edge by a level or two.
  tolerance  the share of pixels allowed to change before the view is a DIFF.
             The default, 0.002%, is ~26 pixels of a 1440x900 page. It is
             that tight because the capture is pixel-EXACT run to run on one
             machine (fixture data, frozen clock: measured 0 changed pixels
             across repeated captures), and because a looser figure hides
             real edits — measured: dropping ONE letter from the "Overview"
             tab moved 0.076% of the desktop page, so a 0.2% tolerance
             passed a visible typo.

No imaging library is required. PNGs are decoded with ffmpeg when it is on
PATH (fast), otherwise with the pure-Python reader studio.evaluation.palette
already carries (slow, but correct).
"""
from __future__ import annotations

import argparse
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent

DEFAULT_THRESHOLD = 8
DEFAULT_TOLERANCE = 0.00002
# config.CHILD_ENV_DROP. Not imported: config loads .env into this process.
CHILD_ENV_DROP = ("ARC_DASHBOARD_TOKEN",)


def child_env():
    return {k: v for k, v in os.environ.items() if k not in CHILD_ENV_DROP}


def _size(path):
    with open(path, "rb") as fh:
        head = fh.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path}: not a PNG")
    return struct.unpack(">II", head[16:24])


def decode(path):
    """(width, height, rgb24 bytes) for a PNG."""
    w, h = _size(path)
    if shutil.which("ffmpeg"):
        p = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo",
                            "-pix_fmt", "rgb24", "-"], capture_output=True, timeout=120,
                           env=child_env())
        if p.returncode == 0 and len(p.stdout) == w * h * 3:
            return w, h, p.stdout
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from studio.evaluation import palette
    w, h, ch, rows = palette.read_png(path)
    if ch == 3:
        return w, h, b"".join(bytes(r) for r in rows)
    out = bytearray()
    for r in rows:
        for x in range(w):
            out += r[x * ch:x * ch + 3]
    return w, h, bytes(out)


def diff(a_path, b_path, threshold=DEFAULT_THRESHOLD):
    """{"changed": share, "pixels": n, "box": (x, y, w, h) | None, "size": ...}.

    Rows that are byte-identical are skipped wholesale, so an unchanged page
    costs almost nothing; only rows that differ are walked pixel by pixel. A
    size mismatch is reported as fully changed: a page that grew or shrank is
    a visible change by definition."""
    wa, ha, a = decode(a_path)
    wb, hb, b = decode(b_path)
    if (wa, ha) != (wb, hb):
        return {"changed": 1.0, "pixels": max(wa * ha, wb * hb), "box": None,
                "size": [[wa, ha], [wb, hb]]}
    stride = wa * 3
    changed = 0
    x0 = y0 = x1 = y1 = None
    for y in range(ha):
        o = y * stride
        ra, rb = a[o:o + stride], b[o:o + stride]
        if ra == rb:
            continue
        for x in range(wa):
            i = x * 3
            if (abs(ra[i] - rb[i]) > threshold or abs(ra[i + 1] - rb[i + 1]) > threshold
                    or abs(ra[i + 2] - rb[i + 2]) > threshold):
                changed += 1
                x0 = x if x0 is None or x < x0 else x0
                x1 = x if x1 is None or x > x1 else x1
                y0 = y if y0 is None else y0
                y1 = y
    box = None if x0 is None else (x0, y0, x1 - x0 + 1, y1 - y0 + 1)
    return {"changed": changed / float(wa * ha or 1), "pixels": changed, "box": box,
            "size": [wa, ha]}


def compare_dirs(golden_dir, actual_dir, *, threshold=DEFAULT_THRESHOLD,
                 tolerance=DEFAULT_TOLERANCE):
    """[{"name", "status", "changed", "box"}] for every PNG on either side."""
    golden_dir, actual_dir = Path(golden_dir), Path(actual_dir)
    names = sorted({p.name for p in golden_dir.glob("*.png")}
                   | {p.name for p in actual_dir.glob("*.png")})
    rows = []
    for name in names:
        g, a = golden_dir / name, actual_dir / name
        if not a.exists():
            rows.append({"name": name, "status": "MISSING", "changed": None, "box": None})
            continue
        if not g.exists():
            rows.append({"name": name, "status": "NO-BASELINE", "changed": None, "box": None})
            continue
        d = diff(g, a, threshold)
        rows.append({"name": name, "status": "PASS" if d["changed"] <= tolerance else "DIFF",
                     "changed": d["changed"], "box": d["box"]})
    return rows


def failed(rows, require_baseline=False):
    bad = {"DIFF", "MISSING"} | ({"NO-BASELINE"} if require_baseline else set())
    return [r for r in rows if r["status"] in bad]


def report(rows):
    lines = []
    for r in rows:
        extra = ""
        if r["changed"] is not None:
            extra = f"  {r['changed']:.3%} changed"
            if r["box"]:
                extra += "  box x={} y={} w={} h={}".format(*r["box"])
        lines.append(f"{r['status']:<12} {r['name']}{extra}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("golden_dir")
    ap.add_argument("actual_dir")
    ap.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    ap.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE)
    ap.add_argument("--require-baseline", action="store_true")
    ap.add_argument("--panels", default="",
                    help="write before|after|diff images of every DIFF view here")
    a = ap.parse_args(argv)
    rows = compare_dirs(a.golden_dir, a.actual_dir, threshold=a.threshold,
                        tolerance=a.tolerance)
    print(report(rows))
    bad = failed(rows, a.require_baseline)
    diffs = [r for r in rows if r["status"] == "DIFF"]
    if diffs and a.panels:
        # The same captioned before|after|diff panels a reviewer gets, so the
        # implementer can LOOK at what moved instead of reading a percentage.
        try:
            if str(REPO) not in sys.path:
                sys.path.insert(0, str(REPO))
            import ui_evidence
            shots = [Path(a.actual_dir) / r["name"] for r in diffs]
            made = ui_evidence.compare(a.golden_dir, shots, Path(a.panels),
                                       before_sha="golden", after_sha="this tree")
            for m in made:
                if m.get("side_by_side"):
                    print(f"  golden|now|diff: {m['side_by_side']}")
        except Exception as exc:                        # noqa: BLE001
            print(f"  (could not draw diff panels: {exc})")
    if not rows:
        print("no screenshots to compare")
        return 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
