"""Visual evidence: screenshots and video of every change to a game.

A green gate proves the code does what its tests measure. It does not show
anyone what the change LOOKS like, and a game is judged by looking at it. So
for every task whose worktree is a Godot project, after the verify gate
passes, the orchestrator captures:

  shots/       one PNG per fixed anchor camera (studio camera_system), so the
               same viewpoints can be compared attempt after attempt
  flythrough   a video that flies a camera through those anchors (Godot's
               built-in movie writer), as .mp4 plus an inline .gif preview
  playtest     a recording of tools/playtest.gd, when the game has one: the
               scripted route played, not just the level looked at
  compare/     before | after | difference, per camera, against the SAME
               cameras rendered at the commit the task branched from

That evidence then goes everywhere a decision is made (AGENTS.md Rule 7d):
the pre-merge reviewer and every PR reviewer are shown the images, the pull
request gets a comment with the screenshots, the before/after comparisons
and the videos inline, and the agent board gets a post pointing at them.

Design rules, each learned the hard way somewhere in this repo:

- NOTHING is written into the worktree. publish() runs `git add -A`; a
  harness script or a report left behind would land in the PR. The capture
  harnesses run from a temp directory (Godot accepts an absolute --script
  path), and any untracked file the capture itself created is deleted.
- The baseline is the MERGE BASE, not the live base branch — the same rule
  gitstore.diff_full follows. Comparing against a main that siblings have
  since moved would show their merges as this task's visual changes.
- A capture that cannot run for lack of a display, Godot or ffmpeg is an
  infrastructure gap, reported but never blamed on the implementer. A game
  that will not render when those are present is the implementer's bug.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import config
import events

MODES = ("required", "best-effort", "off")


class EvidenceUnavailable(RuntimeError):
    """The machine cannot capture (no display, Godot or ffmpeg). Not the task's fault."""


class EvidenceError(RuntimeError):
    """The project would not render. The implementer's to fix."""


# --- detection --------------------------------------------------------------

def is_godot_project(path):
    return (Path(path) / "project.godot").is_file()


def enabled_for(worktree, task=None):
    """Whether this task gets visual evidence: a Godot worktree, the mode not
    off, and the task not opted out with `"evidence": false` (docs-only)."""
    if config.EVIDENCE_MODE == "off":
        return False
    if task is not None and task.get("evidence") is False:
        return False
    return is_godot_project(worktree)


def run_dir(project, task_id, attempt):
    return (Path(config.EVIDENCE_DIR) / _slug(project) / _slug(task_id)
            / f"x{int(attempt)}")


def _slug(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(s or "x")).strip("-") or "x"


# --- the flythrough harness -------------------------------------------------

# Run from a temp file, never from the worktree. Typed explicitly: projects
# commonly treat "inferred Variant" warnings as errors.
CAPTURE_HARNESS = '''extends SceneTree
# Written by arc-orchestrator evidence.py into a temp dir; never committed.
# Flies a camera through anchor viewpoints while --write-movie records.
#   godot --path <project> --write-movie out.avi --fixed-fps 30 \\
#         --script <this> -- <cameras.json> <seconds> [scene]

func _initialize() -> void:
\tvar args := OS.get_cmdline_user_args()
\tif args.size() < 2:
\t\tpush_error("usage: capture.gd -- <cameras.json> <seconds> [scene]")
\t\tquit(2)
\t\treturn
\tvar f := FileAccess.open(args[0], FileAccess.READ)
\tif f == null:
\t\tpush_error("cannot read cameras: " + args[0])
\t\tquit(2)
\t\treturn
\tvar parsed: Variant = JSON.parse_string(f.get_as_text())
\tf.close()
\tvar cams: Array = []
\tif typeof(parsed) == TYPE_DICTIONARY:
\t\tcams = (parsed as Dictionary).get("cameras", [])
\tif cams.is_empty():
\t\tpush_error("no cameras to fly through")
\t\tquit(2)
\t\treturn
\tvar seconds: float = float(args[1])
\tvar scene_path: String = args[2] if args.size() > 2 else str(ProjectSettings.get_setting("application/run/main_scene", ""))
\tvar root := get_root()
\tif scene_path != "":
\t\tvar packed: PackedScene = load(scene_path) as PackedScene
\t\tif packed == null:
\t\t\tpush_error("cannot load scene: " + scene_path)
\t\t\tquit(2)
\t\t\treturn
\t\troot.add_child(packed.instantiate())
\tvar cam := Camera3D.new()
\tcam.name = "EvidenceCamera"
\troot.add_child(cam)
\tawait process_frame
\tcam.make_current()
\t_ensure_inspection_light(root)
\tvar fps: float = float(Engine.get_physics_ticks_per_second())
\tvar mw_fps: int = int(ProjectSettings.get_setting("editor/movie_writer/fps", 30))
\tif mw_fps > 0:
\t\tfps = float(mw_fps)
\tvar total: int = int(maxf(1.0, seconds * fps))
\tvar legs: int = maxi(1, cams.size() - 1)
\tfor i in range(total + 1):
\t\tvar t: float = float(i) / float(total) * float(legs)
\t\tvar a: int = mini(int(t), cams.size() - 1)
\t\tvar b: int = mini(a + 1, cams.size() - 1)
\t\tvar ca: Dictionary = cams[a]
\t\tvar cb: Dictionary = cams[b]
\t\tvar k: float = smoothstep(0.0, 1.0, t - float(a))
\t\tvar pa: Vector3 = _v(ca.get("position", [0, 2, 5]))
\t\tvar pb: Vector3 = _v(cb.get("position", [0, 2, 5]))
\t\tvar la: Vector3 = _v(ca.get("look_at", [0, 0, 0]))
\t\tvar lb: Vector3 = _v(cb.get("look_at", [0, 0, 0]))
\t\tvar p: Vector3 = pa.lerp(pb, k)
\t\tvar look: Vector3 = la.lerp(lb, k)
\t\tif not p.is_equal_approx(look):
\t\t\tcam.look_at_from_position(p, look, Vector3.UP)
\t\tcam.fov = lerpf(float(ca.get("fov", 70.0)), float(cb.get("fov", 70.0)), k)
\t\tawait process_frame
\tprint("EVIDENCE_CAPTURE_OK ", total)
\tquit(0)


func _v(a: Array) -> Vector3:
\treturn Vector3(float(a[0]), float(a[1]), float(a[2]))


# Same rule as the studio render harness: an unlit graybox renders black, so
# neutral inspection light is added ONLY when the scene brings none.
func _ensure_inspection_light(root: Node) -> void:
\tif root.find_children("*", "Light3D", true, false).is_empty():
\t\tvar sun := DirectionalLight3D.new()
\t\tsun.name = "EvidenceInspectionSun"
\t\tsun.rotation_degrees = Vector3(-50.0, 35.0, 0.0)
\t\tsun.light_energy = 1.2
\t\tsun.shadow_enabled = true
\t\troot.add_child(sun)
\tif root.find_children("*", "WorldEnvironment", true, false).is_empty():
\t\tvar env := Environment.new()
\t\tvar sky := Sky.new()
\t\tsky.sky_material = ProceduralSkyMaterial.new()
\t\tenv.background_mode = Environment.BG_SKY
\t\tenv.sky = sky
\t\tenv.ambient_light_source = Environment.AMBIENT_SOURCE_SKY
\t\tenv.ambient_light_energy = 0.6
\t\tvar we := WorldEnvironment.new()
\t\twe.environment = env
\t\troot.add_child(we)
'''


# --- running things ---------------------------------------------------------

def _display():
    return config.STUDIO_DISPLAY or os.environ.get("DISPLAY", "")


def _require_tools():
    from studio.engine import godot
    if not godot.godot_bin():
        raise EvidenceUnavailable("godot is not installed (ARC_GODOT_BIN)")
    if not shutil.which("ffmpeg"):
        raise EvidenceUnavailable("ffmpeg is not installed")
    if not _display():
        raise EvidenceUnavailable(
            "no display: Godot's --headless mode cannot render. WSLg gives :0; "
            "elsewhere start Xvfb and set ARC_STUDIO_DISPLAY")


def _godot(project, args, *, timeout):
    """(rc, output) of one Godot run WITH a display (rendering needs one)."""
    from studio.engine import godot
    return godot._run(args, project=project, timeout=timeout, display=_display())


def _ffmpeg(args, timeout=300):
    p = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args],
                       capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise EvidenceError(f"ffmpeg {' '.join(args[:4])}...: {p.stderr.strip()[-400:]}")


def _untracked(project):
    """Untracked, non-ignored files: what `git add -A` would pick up."""
    p = subprocess.run(["git", "-C", str(project), "ls-files", "--others",
                        "--exclude-standard", "-z"], capture_output=True, text=True)
    if p.returncode != 0:
        return set()
    return {x for x in p.stdout.split("\0") if x}


@contextlib.contextmanager
def _leave_no_trace(project):
    """Delete every untracked file the capture itself creates in `project`."""
    before = _untracked(project)
    try:
        yield
    finally:
        for rel in sorted(_untracked(project) - before):
            path = Path(project) / rel
            with contextlib.suppress(OSError):
                path.unlink()
            # Empty directories the capture created (e.g. studio_shots/).
            parent = path.parent
            while parent != Path(project) and parent.is_dir():
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent


def main_scene(project):
    """The project's run/main_scene (res:// or uid://), or ""."""
    try:
        text = (Path(project) / "project.godot").read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r'^run/main_scene\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else ""


def _cameras(project):
    from studio.evaluation import camera_system
    return [c.to_dict() for c in camera_system.load_anchors(project)]


def _render_shots(project, cameras, out_dir, scratch, *, timeout):
    """One PNG per camera via the studio render harness, run from `scratch`."""
    from studio.engine import godot
    out_dir.mkdir(parents=True, exist_ok=True)
    harness = Path(scratch) / "render.gd"
    harness.write_text(godot._HARNESS_SRC, encoding="utf-8")
    cam_file = Path(scratch) / "cameras.json"
    cam_file.write_text(json.dumps({"cameras": cameras}), encoding="utf-8")
    # The harness renders only what it is given: without the scene argument
    # every camera photographs an empty world (sky over a ground plane).
    args = ["--rendering-driver", "opengl3", "--resolution",
            config.EVIDENCE_RESOLUTION, "--script", str(harness),
            "--", str(cam_file), str(out_dir)]
    scene = main_scene(project)
    if scene:
        args.append(scene)
    rc, out = _godot(project, args, timeout=timeout)
    shots = sorted(out_dir.glob("*.png"))
    if not shots:
        raise EvidenceError(
            f"the project rendered no screenshots (godot rc={rc}):\n"
            + "\n".join(godot.output_errors(out)[:15] or [out[-1500:]]))
    (out_dir / "manifest.json").unlink(missing_ok=True)
    return shots


def _video(src_avi, out_stem):
    """.mp4 (h264, plays in a browser) and a small looping .gif preview."""
    mp4 = out_stem.with_suffix(".mp4")
    gif = out_stem.with_suffix(".gif")
    _ffmpeg(["-i", str(src_avi), "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-crf", "28", "-movflags", "+faststart", str(mp4)])
    _ffmpeg(["-i", str(src_avi), "-t", str(config.EVIDENCE_GIF_SECONDS), "-vf",
             "fps=8,scale=480:-1:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse",
             str(gif)])
    Path(src_avi).unlink(missing_ok=True)
    return mp4, gif


def _flythrough(project, cameras, out_dir, scratch, *, timeout):
    harness = Path(scratch) / "capture.gd"
    harness.write_text(CAPTURE_HARNESS, encoding="utf-8")
    cam_file = Path(scratch) / "fly_cameras.json"
    cam_file.write_text(json.dumps({"cameras": cameras}), encoding="utf-8")
    avi = Path(scratch) / "flythrough.avi"
    rc, out = _godot(project, ["--rendering-driver", "opengl3", "--resolution",
                               config.EVIDENCE_RESOLUTION, "--write-movie", str(avi),
                               "--fixed-fps", str(config.EVIDENCE_FPS),
                               "--script", str(harness), "--", str(cam_file),
                               str(config.EVIDENCE_SECONDS)], timeout=timeout)
    if not avi.exists() or "EVIDENCE_CAPTURE_OK" not in out:
        from studio.engine import godot
        raise EvidenceError(f"flythrough recording failed (godot rc={rc}):\n"
                            + "\n".join(godot.output_errors(out)[:15] or [out[-1500:]]))
    return _video(avi, out_dir / "flythrough")


def _playtest(project, out_dir, scratch, *, timeout):
    """Record the scripted playtest, when the game has one. None otherwise.

    A playtest that fails or crashes here is recorded as a warning, not an
    error: the verify gate already judged it, and the video of a failing run
    is exactly what a reviewer needs to see."""
    if not (Path(project) / "tools" / "playtest.gd").is_file():
        return None, None
    avi = Path(scratch) / "playtest.avi"
    rc, out = _godot(project, ["--rendering-driver", "opengl3", "--resolution",
                               config.EVIDENCE_RESOLUTION, "--write-movie", str(avi),
                               "--fixed-fps", str(config.EVIDENCE_FPS),
                               "--script", "res://tools/playtest.gd"], timeout=timeout)
    note = None if rc == 0 else f"the playtest exited {rc} while being recorded"
    shots_src = Path(project) / "studio_shots"
    if shots_src.is_dir():
        dest = out_dir / "playtest_shots"
        dest.mkdir(parents=True, exist_ok=True)
        for png in sorted(shots_src.glob("*.png"))[:12]:
            shutil.copy2(png, dest / png.name)
    if not avi.exists() or avi.stat().st_size == 0:
        return None, note or "the playtest produced no video"
    return _video(avi, out_dir / "playtest"), note


# --- comparing --------------------------------------------------------------

def _pixels(path, step):
    from studio.evaluation import palette
    w, h, ch, rows = palette.read_png(path)
    return w, h, [(rows[y][x * ch], rows[y][x * ch + 1], rows[y][x * ch + 2])
                  for y in range(0, h, step) for x in range(0, w, step)]


def blank_share(path, step=8):
    """Share of sampled pixels equal (within 6/255) to the most common colour.

    Near 1.0 means a solid frame — the grey or black of a scene with no
    camera, no light, or a crash before the first draw."""
    try:
        _w, _h, px = _pixels(path, step)
    except Exception:                                   # noqa: BLE001
        return None
    if not px:
        return None
    counts = {}
    for p in px:
        key = tuple(c // 8 for c in p)
        counts[key] = counts.get(key, 0) + 1
    return max(counts.values()) / len(px)


def frame_diff(before, after, threshold=0):
    """(changed_share, mean_abs_delta, box) for two same-size screenshots.

    EVERY pixel is compared — not a stride-N sample. A sample on a lattice
    misses a change that falls between its points, and then the panel both
    loses the box and (because the share comes back 0.0) is captioned NO
    CHANGE: a reviewer is shown a confident lie about a real edit. Scan
    everything and no pixel can hide.

    ANY non-zero colour difference counts (threshold defaults to 0). A
    tolerance here is a second way to lose a real edit: at 24, an edited
    20/255 block was captioned NO CHANGE and framed by no box while the
    unthresholded heatmap still glowed at it — the picture and the text
    disagreeing, which is the very confusion this evidence exists to remove.
    The predicate is deliberately the one the heatmap already uses, so "the
    panel glows" and "the panel says something changed" cannot diverge.

    One pass yields the share, the delta AND the box together, so the number
    and the region can never disagree about what changed.

    Rows that are byte-identical are skipped without a per-pixel loop, which
    is what makes the full scan cheap: an unchanged render costs almost
    nothing, and only the rows that actually differ are walked."""
    from studio.evaluation import palette
    wb, hb, chb, rb = palette.read_png(before)
    wa, ha, cha, ra = palette.read_png(after)
    if (wb, hb) != (wa, ha):
        return None, None, None
    x0 = y0 = x1 = y1 = None
    changed = total = 0
    for y, (b_row, a_row) in enumerate(zip(rb, ra)):
        if b_row == a_row:
            continue
        for x in range(wb):
            # Each frame is walked with ITS OWN stride: a baseline render and a
            # new one need not both be RGBA, and one stride for both compares
            # misaligned pixels of identical frames (0.75 "changed").
            i, j = x * chb, x * cha
            # The first three channels are what is seen; a difference in alpha
            # alone is not a visual change.
            d = abs(b_row[i] - a_row[j])
            for k in (1, 2):
                dk = abs(b_row[i + k] - a_row[j + k])
                if dk > d:
                    d = dk
            total += d
            if d > threshold:
                changed += 1
                x0 = x if x0 is None or x < x0 else x0
                x1 = x if x1 is None or x > x1 else x1
                y0 = y if y0 is None else y0
                y1 = y
    n = (wb * hb) or 1
    box = None if x0 is None else (x0, y0, x1 - x0 + 1, y1 - y0 + 1)
    return changed / n, total / n / 255.0, box


def diff_stats(before, after, threshold=0):
    """(changed_share, mean_abs_delta) between two same-size screenshots."""
    changed, delta, _box = frame_diff(before, after, threshold)
    return changed, delta


def change_box(before, after, threshold=0):
    """(x, y, w, h) around the pixels that changed, or None if none did."""
    return frame_diff(before, after, threshold)[2]


_CAPTION_H = 56                  # caption bar height, in output pixels
_PANEL_W = 640                   # before | after | diff panel width
_TILE_W, _TILE_H = 480, 270      # contact-sheet tile (16:9, like the renders)
_LABEL_H = 32                    # room under each tile for its name
_SHEET_COLS = 3                  # tiles per contact-sheet row
_MAX_BYTES = 2 * 1024 * 1024     # every image we publish stays under this
_FONTS = {}

# A caption bar is what makes a panel self-describing. 'BEFORE'/'AFTER' are
# positional, and position is exactly what a reviewer viewing one image on a
# phone cannot see; the sha says which commit each frame came from.


def _font_file():
    """A TTF path for ffmpeg drawtext, or None when this machine has none."""
    if not _FONTS:
        names = ("AdwaitaSans-Regular.ttf", "DejaVuSans.ttf", "AdwaitaMono-Regular.ttf")
        found = None
        for root in ("/usr/share/fonts", "/usr/local/share/fonts",
                     str(Path.home() / ".fonts"),
                     str(Path.home() / ".local/share/fonts")):
            if not Path(root).is_dir():
                continue
            for name in names:
                hits = sorted(Path(root).rglob(name))
                if hits:
                    found = str(hits[0])
                    break
            if found:
                break
        _FONTS["font"] = found
    return _FONTS["font"]


def _esc(text):
    """Escape a caption for a filtergraph option value.

    `:` `'` `\\` and `,` are filtergraph syntax; `%` and `[` `]` only because
    the text is quoted — expansion=none below keeps ffmpeg from reading the
    rest as a text expansion (`DIFF 12.5% changed` is a syntax error without
    it: "Stray % near ' changed'")."""
    return "".join("\\" + c if c in "\\':%[],;" else c for c in str(text))


def _caption(text, *, font, fontsize=26, y=10, x="(w-text_w)/2"):
    """One drawtext filter, or "" when there is no font to draw with."""
    if not font:
        return ""
    return (f"drawtext=fontfile={_esc(font)}:text='{_esc(text)}':x={x}:y={y}:"
            f"fontsize={fontsize}:fontcolor=white:box=1:boxcolor=black@0.7:"
            "boxborderw=6:expansion=none")


def _cap_chain(label, font, *, extra=""):
    """Add a caption bar above the current picture and burn `label` into it."""
    text = _caption(label, font=font)
    return (f"pad=iw:ih+{_CAPTION_H}:0:{_CAPTION_H}:0x111111"
            + (f",{text}" if text else "") + extra)


def _diff_filter(lab_b, lab_a, lab_d, box, no_change, font):
    """before | after | heatmap, captioned; the diff is over the dimmed after.

    The heat comes from the amplified difference ADDED to a dimmed copy of the
    after frame, so an unchanged area shows the room rather than black, and a
    few changed pixels glow against it. Nothing changed means the panel still
    shows the dimmed frame AND says NO CHANGE in large text — a black rectangle
    is otherwise indistinguishable from a render that failed."""
    amp = "lutrgb=r='min(val*4,255)':g='min(val*4,255)':b='min(val*4,255)'"
    dim = "lutrgb=r='val*0.45':g='val*0.45':b='val*0.45'"
    extra = ""
    if no_change:
        # Without a font there is no label to draw: the panel is still the
        # dimmed after frame, never a black rectangle.
        label = _caption("NO CHANGE", font=font, fontsize=max(28, _PANEL_W // 12),
                         y="(h-text_h)/2")
        extra = f",{label}" if label else ""
    chain = [
        # rgb24 first: the renders carry alpha, and the difference of two
        # opaque alphas is 0 — a fully transparent diff panel.
        "[0]format=rgb24,split=2[braw][bpanel]",
        "[1]format=rgb24,split=3[araw][apanel][adim]",
        f"[braw][araw]blend=all_mode=difference,{amp}[diff]",
        f"[adim]{dim}[dim]",
        "[dim][diff]blend=all_mode=screen[heat]",
        f"[bpanel]scale={_PANEL_W}:-2," + _cap_chain(lab_b, font) + "[bcap]",
        f"[apanel]scale={_PANEL_W}:-2," + _cap_chain(lab_a, font) + "[acap]",
        "[heat]"
        # The box is drawn BEFORE the panel is scaled, so a literal pixel
        # thickness is multiplied by the scale factor: t=5 on a 64px test frame
        # becomes a 37px slab on a 480px panel, burying the change it marks.
        # iw-relative keeps the frame ~3px whatever the render resolution.
        + (f"drawbox=x={box[0]}:y={box[1]}:w={box[2]}:h={box[3]}:"
           "color=0xff2d2d@1:t=max(1\\,iw/160)," if box else "")
        + f"scale={_PANEL_W}:-2," + _cap_chain(lab_d, font, extra=extra) + "[dcap]",
        "[bcap][acap][dcap]hstack=inputs=3,scale=1440:-2",
    ]
    return ";".join(chain)


def _shrink(path, limit=_MAX_BYTES):
    """Re-encode smaller until the PNG fits `limit` (the PR embeds it inline)."""
    for width in (1200, 960, 780):
        if Path(path).stat().st_size <= limit:
            break
        tmp = Path(str(path) + ".small.png")
        try:
            _ffmpeg(["-i", str(path), "-vf", f"scale={width}:-2",
                     "-compression_level", "9", str(tmp)])
        except EvidenceError:
            break
        os.replace(tmp, path)
    return Path(path)


def compare(baseline_dir, shots, out_dir, *, before_sha="", after_sha=""):
    """Per camera: before | after | heatmap, each captioned, and the numbers.

    Every panel says what it is and which commit it came from, the diff panel
    is a heatmap over the dimmed after image with a box around the changed
    region, and an unchanged camera says NO CHANGE."""
    out_dir.mkdir(parents=True, exist_ok=True)
    font = _font_file()
    rows = []
    for shot in shots:
        before = Path(baseline_dir) / shot.name
        if not before.exists():
            rows.append({"name": shot.stem, "new": True})
            continue
        # One exact pass: the share, the delta and the box come from the same
        # pixel comparison, so the caption cannot say NO CHANGE about a pixel
        # the box is framing.
        changed, delta, box = frame_diff(before, shot)
        lab_b = "BEFORE " + (before_sha[:10] or "baseline")
        lab_a = "AFTER " + (after_sha[:10] or "worktree")
        lab_d = ("DIFF unavailable" if changed is None
                 else f"DIFF {changed:.1%} changed")
        side = out_dir / f"{shot.stem}.png"
        try:
            # No box IS "nothing changed": both read the same pass, so the
            # label cannot contradict the glow the reviewer can see.
            _ffmpeg(["-i", str(before), "-i", str(shot), "-filter_complex",
                     _diff_filter(lab_b, lab_a, lab_d, box, box is None, font),
                     "-compression_level", "9", str(side)])
            _shrink(side)
        except EvidenceError:
            side = None
        rows.append({"name": shot.stem, "changed": changed, "delta": delta,
                     "box": list(box) if box else None,
                     "side_by_side": str(side) if side else None})
    return rows


def contact_sheet(shots, out_dir, *, cols=_SHEET_COLS, name="contact_sheet.png"):
    """One labeled grid of every shot: the first thing a reviewer should see.

    `shots` is a list of (label, path). Panels are always in the same order, so
    two attempts can be compared by eye. Returns the path, or None when there
    is nothing to show or ffmpeg cannot draw the grid."""
    items = [(str(lbl), Path(p)) for lbl, p in shots if Path(p).exists()]
    if not items:
        return None
    out = Path(out_dir) / name
    out.parent.mkdir(parents=True, exist_ok=True)
    font = _font_file()
    tile_h = _TILE_H + _LABEL_H
    chains, labels = [], []
    for i, (lbl, _p) in enumerate(items):
        text = _caption(lbl, font=font, fontsize=24, y=_TILE_H + 2)
        chains.append(
            f"[{i}:v]format=rgb24,scale={_TILE_W}:{_TILE_H}:"
            f"force_original_aspect_ratio=decrease,pad={_TILE_W}:{tile_h}:"
            f"(ow-iw)/2:0:0x111111" + (f",{text}" if text else "") + f"[t{i}]")
        labels.append(f"[t{i}]")
    rows = [labels[i:i + cols] for i in range(0, len(labels), cols)]
    grid_w = _TILE_W * cols
    outs = []
    for r, row in enumerate(rows):
        joined = "".join(row)
        chains.append(joined + (f"hstack=inputs={len(row)}[r{r}]" if len(row) > 1
                                else f"null[r{r}]"))
        # A short last row is left-padded to the grid width so the rows stack.
        chains.append(f"[r{r}]pad={grid_w}:{tile_h}:0:0:0x111111[p{r}]")
        outs.append(f"[p{r}]")
    chains.append("".join(outs)
                  + (f"vstack=inputs={len(outs)}[grid]" if len(outs) > 1
                     else "null[grid]"))
    chains.append(f"[grid]scale={min(grid_w, 1600)}:-2")
    try:
        _ffmpeg([*[a for _lbl, p in items for a in ("-i", str(p))],
                 "-filter_complex", ";".join(chains),
                 "-compression_level", "9", str(out)])
    except EvidenceError:
        return None
    return _shrink(out)


# --- baseline at the merge base ---------------------------------------------

def _git(args, cwd, check=True, timeout=120):
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                       text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise EvidenceError(f"git {' '.join(args)}: {p.stderr.strip()[:300]}")
    return p.stdout.strip()


def merge_base(worktree, base):
    return _git(["merge-base", base, "HEAD"], worktree, check=False) or None


def baseline(project, repo, sha, *, timeout):
    """Screenshots of `sha` from the fixed cameras, rendered once and cached.

    Rendered in a throwaway detached worktree of the blessed clone, so the
    baseline is exactly the commit — no uncommitted state, no other task's
    files. Returns the directory, or None when the commit cannot render (a
    base that predates the scene has no "before")."""
    dest = Path(config.EVIDENCE_DIR) / _slug(project) / "baseline" / sha
    if any(dest.glob("*.png")):
        return dest
    lock = Path(config.EVIDENCE_DIR) / _slug(project) / ".baseline.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        if any(dest.glob("*.png")):
            return dest
        with tempfile.TemporaryDirectory(prefix="arc-evidence-base-") as tmp:
            wt = Path(tmp) / "wt"
            _git(["worktree", "add", "--detach", str(wt), sha], repo)
            try:
                if not is_godot_project(wt):
                    return None
                from studio.engine import godot
                godot.import_assets(wt, timeout=timeout)
                try:
                    _render_shots(wt, _cameras(wt), dest, tmp, timeout=timeout)
                except EvidenceError:
                    shutil.rmtree(dest, ignore_errors=True)
                    return None
            finally:
                _git(["worktree", "remove", "--force", str(wt)], repo, check=False)
    return dest


# --- capture ----------------------------------------------------------------

def capture(worktree, out_dir, *, repo=None, base=None, project="",
            timeout=None):
    """Capture every kind of evidence for `worktree` into `out_dir`.

    Returns the manifest dict (also written to out_dir/manifest.json).
    Raises EvidenceUnavailable when this machine cannot capture at all, and
    EvidenceError when the project itself will not render.
    """
    _require_tools()
    from studio.engine import godot
    timeout = timeout or config.EVIDENCE_TIMEOUT
    worktree, out_dir = Path(worktree), Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    started = time.time()
    manifest = {"worktree": str(worktree), "head": _git(["rev-parse", "HEAD"], worktree,
                                                        check=False),
                "shots": [], "videos": {}, "compare": [], "warnings": []}
    with tempfile.TemporaryDirectory(prefix="arc-evidence-") as scratch, \
            _leave_no_trace(worktree):
        godot.import_assets(worktree, timeout=timeout)
        cams = _cameras(worktree)
        shots = _render_shots(worktree, cams, out_dir / "shots", scratch,
                              timeout=timeout)
        manifest["shots"] = [str(s) for s in shots]
        for s in shots:
            share = blank_share(s)
            if share is not None and share >= config.EVIDENCE_BLANK_SHARE:
                manifest["warnings"].append(
                    f"camera '{s.stem}' rendered a nearly solid frame "
                    f"({share:.0%} one colour): nothing visible from there, or "
                    "the scene failed to draw")
        try:
            mp4, gif = _flythrough(worktree, cams, out_dir, scratch, timeout=timeout)
            manifest["videos"]["flythrough"] = {"mp4": str(mp4), "gif": str(gif)}
        except EvidenceError as exc:
            manifest["warnings"].append(str(exc).splitlines()[0])
        pt, note = _playtest(worktree, out_dir, scratch, timeout=timeout)
        if pt:
            manifest["videos"]["playtest"] = {"mp4": str(pt[0]), "gif": str(pt[1])}
        if note:
            manifest["warnings"].append(note)
        manifest["playtest_shots"] = [str(p) for p in
                                      sorted((out_dir / "playtest_shots").glob("*.png"))]
    if repo and base:
        sha = merge_base(worktree, base)
        if sha:
            try:
                bdir = baseline(project or Path(repo).name, repo, sha, timeout=timeout)
            except (EvidenceError, subprocess.SubprocessError, OSError) as exc:
                bdir = None
                manifest["warnings"].append(f"no baseline: {str(exc)[:200]}")
            manifest["baseline"] = {"sha": sha, "dir": str(bdir) if bdir else None}
            if bdir:
                manifest["compare"] = compare(
                    bdir, shots, out_dir / "compare",
                    before_sha=sha, after_sha=manifest.get("head", ""))
    sheet = contact_sheet(capture_shots(manifest), out_dir)
    if sheet:
        manifest["contact_sheet"] = str(sheet)
    manifest["seconds"] = round(time.time() - started, 1)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2),
                                           encoding="utf-8")
    return manifest


# --- presenting -------------------------------------------------------------

def _pct(x):
    return "—" if x is None else f"{x:.1%}"


def capture_shots(manifest):
    """[(label, path)] from the manifest: every render, in capture order.

    The shots ARE the after images (this worktree's renders), so each tile is
    (camera name, shot). Scene renders — the other producer of shots — follow,
    named by their scene. These labels are what the contact sheet prints under
    each tile."""
    out = [(Path(s).stem, s) for s in manifest.get("shots") or []]
    for sc in manifest.get("scenes") or []:
        name = Path(str(sc.get("path") or "")).stem
        for s in sc.get("shots") or []:
            out.append((name or Path(s).stem, s))
    return out


def review_images(manifest, limit=8):
    """Images to attach for a reviewer, most informative first.

    The contact sheet comes FIRST: one labeled grid of every camera is what a
    reviewer should see before eight full-size panels."""
    sheet = manifest.get("contact_sheet")
    out = [sheet] if sheet and Path(sheet).exists() else []
    out += [c["side_by_side"] for c in manifest.get("compare") or []
            if c.get("side_by_side")]
    compared = {Path(c["side_by_side"]).stem for c in manifest.get("compare") or []
                if c.get("side_by_side")}
    out += [s for s in manifest.get("shots") or [] if Path(s).stem not in compared]
    out += list(manifest.get("playtest_shots") or [])
    return [p for p in out if Path(p).exists()][:limit]


def prompt_block(manifest):
    """Text for a reviewer prompt: what was captured, where, and what changed."""
    if not manifest:
        return ""
    lines = ["VISUAL EVIDENCE (captured after the verify gate passed). Look at it: "
             "a visual regression or a change that does not show what the task "
             "asks for is a blocking issue, exactly like a failing test."]
    for c in manifest.get("compare") or []:
        if c.get("new"):
            lines.append(f"- camera {c['name']}: new viewpoint (no baseline)")
        else:
            lines.append(f"- camera {c['name']}: {_pct(c.get('changed'))} of pixels "
                         f"changed vs the branch point; before|after|diff: "
                         f"{c.get('side_by_side') or '(unavailable)'}")
    if not manifest.get("compare"):
        lines += [f"- screenshot: {s}" for s in manifest.get("shots") or []]
    for kind, v in (manifest.get("videos") or {}).items():
        lines.append(f"- {kind} video: {v.get('mp4')} (preview {v.get('gif')})")
    for p in manifest.get("playtest_shots") or []:
        lines.append(f"- playtest screenshot: {p}")
    for w in manifest.get("warnings") or []:
        lines.append(f"- WARNING: {w}")
    return "\n".join(lines) + "\n"


def board_body(manifest):
    n = len(manifest.get("shots") or [])
    vids = ", ".join(sorted((manifest.get("videos") or {})))
    changed = [f"{c['name']} {_pct(c.get('changed'))}" for c in
               manifest.get("compare") or [] if c.get("changed")]
    body = f"evidence: {n} screenshot(s)" + (f", video: {vids}" if vids else "")
    if changed:
        body += "; changed vs branch point: " + ", ".join(changed[:6])
    if manifest.get("warnings"):
        body += f"; {len(manifest['warnings'])} warning(s)"
    return body + f" — {Path(manifest.get('shots', ['.'])[0]).parent.parent}"


# --- publishing to the game repo --------------------------------------------

def _github_slug(repo):
    url = _git(["remote", "get-url", "origin"], repo, check=False)
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url or "")
    return (m.group(1), url) if m else (None, url)


def publish(repo, project, task_id, attempt, manifest):
    """Push this capture to the game repo's evidence branch.

    Returns the web base URL of the uploaded directory, or None when the repo
    has no GitHub remote. Files go under <task>/x<attempt>/ on the orphan
    branch config.EVIDENCE_BRANCH, so a PR comment can show them inline (a
    private repo's own files render for anyone who can see the PR)."""
    slug, remote = _github_slug(repo)
    if not slug or not config.EVIDENCE_PUBLISH:
        return None
    src = Path(manifest["shots"][0]).parent.parent if manifest.get("shots") else None
    if not src or not src.is_dir():
        return None
    rel = f"{_slug(task_id)}/x{int(attempt)}"
    cache = Path(config.EVIDENCE_DIR) / "_git" / _slug(project)
    cache.parent.mkdir(parents=True, exist_ok=True)
    branch = config.EVIDENCE_BRANCH
    with open(str(cache) + ".lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        if not (cache / ".git").exists():
            cache.mkdir(parents=True, exist_ok=True)
            _git(["init", "-q"], cache)
            _git(["remote", "add", "origin", remote], cache)
        _git(["config", "user.name", "arc-orchestrator"], cache)
        _git(["config", "user.email", "arc-orchestrator@users.noreply.github.com"], cache)
        for tries in range(3):
            fetched = subprocess.run(
                ["git", "-C", str(cache), "fetch", "-q", "origin", branch],
                capture_output=True, timeout=180).returncode == 0
            if fetched:
                _git(["checkout", "-q", "-B", branch, "FETCH_HEAD"], cache)
                _git(["clean", "-qfdx"], cache)
            else:
                _git(["checkout", "-q", "--orphan", branch], cache, check=False)
                _git(["rm", "-rq", "--cached", "--ignore-unmatch", "."], cache, check=False)
                (cache / "README.md").write_text(
                    "Visual evidence captured by arc-orchestrator for pull requests.\n"
                    "One directory per task attempt: screenshots, before/after "
                    "comparisons, flythrough and playtest videos.\n", encoding="utf-8")
            dest = cache / rel
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest, ignore=shutil.ignore_patterns("*.avi"))
            _git(["add", "-A", "."], cache)
            _git(["commit", "-qm", f"evidence: {task_id} attempt {attempt}"], cache,
                 check=False)
            pushed = subprocess.run(
                ["git", "-C", str(cache), "push", "-q", "origin", f"HEAD:{branch}"],
                capture_output=True, text=True, timeout=300)
            if pushed.returncode == 0:
                return f"https://github.com/{slug}/blob/{branch}/{rel}"
            time.sleep(2 * (tries + 1))       # someone else pushed: refetch
        raise EvidenceError(f"could not push evidence: {pushed.stderr.strip()[:300]}")


def pr_markdown(manifest, web_base, *, task_id, attempt):
    """The PR comment: screenshots, before/after comparisons and videos inline."""
    def url(local, raw=True):
        rel = Path(local).relative_to(Path(manifest["shots"][0]).parent.parent)
        return f"{web_base}/{rel.as_posix()}" + ("?raw=true" if raw else "")
    head = (manifest.get("head") or "")[:10]
    lines = [f"### 🎥 Visual evidence — `{task_id}` attempt {attempt}"
             + (f" at `{head}`" if head else ""), ""]
    sheet = manifest.get("contact_sheet")
    if sheet and Path(sheet).exists():
        # First, before the videos and panels: one labeled grid of everything
        # captured is what tells a reviewer at a glance what they are looking at.
        lines += [f"**Every camera** ([full size]({url(sheet, raw=True)}))", "",
                  f"![contact sheet]({url(sheet)})", ""]
    vids = manifest.get("videos") or {}
    for kind in ("playtest", "flythrough"):
        v = vids.get(kind)
        if v:
            lines += [f"**{kind.capitalize()}** ([full video]({url(v['mp4'], raw=True)}))",
                      "", f"![{kind}]({url(v['gif'])})", ""]
    comp = [c for c in manifest.get("compare") or [] if c.get("side_by_side")]
    if comp:
        base = (manifest.get("baseline") or {}).get("sha", "")[:10]
        lines += [f"**Before \\| after \\| difference** (vs branch point `{base}`)", "",
                  "| camera | changed | before · after · diff |", "|---|---|---|"]
        for c in comp:
            lines.append(f"| {c['name']} | {_pct(c.get('changed'))} | "
                         f"![{c['name']}]({url(c['side_by_side'])}) |")
        lines.append("")
    else:
        lines += ["**Screenshots**", ""]
        lines += [f"![{Path(s).stem}]({url(s)})" for s in manifest.get("shots") or []]
        lines.append("")
    pts = manifest.get("playtest_shots") or []
    if pts:
        lines += ["**Playtest screenshots**", ""]
        lines += [f"![{Path(p).stem}]({url(p)})" for p in pts[:6]]
        lines.append("")
    if manifest.get("warnings"):
        lines += ["**Warnings**", ""] + [f"- ⚠️ {w}" for w in manifest["warnings"]] + [""]
    lines.append(f"<sub>Captured by arc-orchestrator (AGENTS.md Rule 7d) in "
                 f"{manifest.get('seconds', '?')}s.</sub>")
    return "\n".join(lines)


def emit(kind, **fields):
    events.emit(f"evidence.{kind}", **fields)


if __name__ == "__main__":                              # manual capture
    import argparse
    ap = argparse.ArgumentParser(description="capture visual evidence of a Godot project")
    ap.add_argument("project_dir")
    ap.add_argument("out_dir")
    ap.add_argument("--base", default="", help="ref to compare against (merge base)")
    a = ap.parse_args()
    m = capture(a.project_dir, a.out_dir, repo=a.project_dir if a.base else None,
                base=a.base or None, project=Path(a.project_dir).name)
    print(json.dumps(m, indent=2))
