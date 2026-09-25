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
import math
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

# How many distinct Godot error lines a manifest keeps. Enough to name every
# real failure, few enough that a per-frame repeat cannot bury the rest.
_MAX_GODOT_ERRORS = 30

# The reason string for "this game has no scripted playtest" — one constant,
# because coverage reasons and tests both key on it.
_NO_PLAYTEST = "tools/playtest.gd not found"


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


def non_visual(task):
    """True for a task marked `"visual": false`: its change is not meant to
    be seen, so the reviewer is not told to reject an unchanged picture."""
    return task is not None and task.get("visual") is False


def run_dir(project, task_id, attempt):
    return (Path(config.EVIDENCE_DIR) / _slug(project) / _slug(task_id)
            / f"x{int(attempt)}")


def latest_manifest(project, task_id):
    """The newest attempt's manifest for a task, or None (read-only)."""
    tdir = Path(config.EVIDENCE_DIR) / _slug(project) / _slug(task_id)
    try:
        attempts = sorted((p for p in tdir.glob("x*") if p.name[1:].isdigit()),
                          key=lambda p: int(p.name[1:]))
    except OSError:
        return None
    for adir in reversed(attempts):
        try:
            m = json.loads((adir / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(m, dict):
            m.setdefault("attempt", int(adir.name[1:]))
            return m
    return None


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
\t\t# Said out loud: the capture is lit, but a PLAYER sees this scene black.
\t\tprint("EVIDENCE_SCENE_UNLIT")
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


# The scripted playtest, recorded so a reviewer can SEE the route. It extends
# the game's own tools/playtest.gd (so the route, the input and the pass/fail
# are exactly the game's), and only adds what a recording needs: neutral light
# when the scene brings none (an unlit graybox records as black) and a chase
# camera over the player (a first-person view of a grey wall shows nothing).
PLAYTEST_WRAPPER = """extends "res://tools/playtest.gd"
# Written by arc-orchestrator evidence.py into a temp dir; never committed.

func _initialize() -> void:
\t_arc_follow()
\tsuper()


func _arc_follow() -> void:
\tawait process_frame
\tawait process_frame
\tvar root := get_root()
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
\tvar cam := Camera3D.new()
\tcam.name = "EvidenceChaseCamera"
\troot.add_child(cam)
\tcam.make_current()
\tprint("EVIDENCE_PLAYTEST_CAMERA")
\tvar tick := 0
\twhile true:
\t\tvar target := _arc_player(root)
\t\tif target != null and target.is_inside_tree():
\t\t\tvar p: Vector3 = target.global_position
\t\t\tif tick % 30 == 0:
\t\t\t\t_arc_cutaway(root, p.y + 1.9)
\t\t\tcam.look_at_from_position(p + Vector3(0.0, 11.0, 8.0), p, Vector3.UP)
\t\tif not cam.current:
\t\t\tcam.make_current()
\t\ttick += 1
\t\tawait process_frame


# Cutaway: geometry lying wholly above the player's head (ceilings, roofs) is
# hidden from the RECORDING so the overhead camera sees the route inside a
# building. Visibility is not collision: the playtest itself is unchanged.
func _arc_cutaway(root: Node, above: float) -> void:
\tfor n in root.find_children("*", "GeometryInstance3D", true, false):
\t\tvar gi := n as GeometryInstance3D
\t\tif gi != null and gi.visible:
\t\t\tvar box: AABB = gi.global_transform * gi.get_aabb()
\t\t\tif box.position.y >= above:
\t\t\t\tgi.visible = false


func _arc_player(root: Node) -> Node3D:
\tfor n in get_nodes_in_group("player"):
\t\tif n is Node3D:
\t\t\treturn n as Node3D
\tvar bodies := root.find_children("*", "CharacterBody3D", true, false)
\tfor b in bodies:
\t\tif str(b.name).to_lower().contains("player"):
\t\t\treturn b as Node3D
\treturn (bodies[0] as Node3D) if not bodies.is_empty() else null
"""


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


def _godot(project, args, *, timeout, log=None):
    """(rc, output) of one Godot run WITH a display (rendering needs one).

    `log`, when given, collects the raw output of EVERY render call: Godot
    prints script and scene errors while still exiting 0, so a run that
    "succeeded" can still have said what it could not load. capture() turns
    that into manifest['godot_errors']."""
    from studio.engine import godot
    rc, out = godot._run(args, project=project, timeout=timeout, display=_display())
    if log is not None:
        log.append(out)
    return rc, out


# Godot's own error lines. Narrower than studio.engine.godot.output_errors on
# purpose: that list judges whether a run did what it printed a 0 for, this one
# is the four kinds a manifest records (the task's contract).
_GODOT_ERROR_RE = re.compile(r"SCRIPT ERROR|ERROR:|push_error|Parse Error", re.I)


def godot_errors(*texts, limit=_MAX_GODOT_ERRORS):
    """Deduped Godot error lines from every render call's output, capped.

    Short, stable lines: the manifest and the PR comment show them verbatim,
    so an error repeated once a frame occupies one slot of the cap."""
    out = []
    for text in texts:
        for line in (text or "").splitlines():
            line = line.strip()
            if line and line not in out and _GODOT_ERROR_RE.search(line):
                out.append(line)
                if len(out) >= limit:
                    return out
    return out


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


def _render_shots(project, cameras, out_dir, scratch, *, timeout, log=None,
                  scene=None):
    """One PNG per camera via the studio render harness, run from `scratch`.

    `scene` (res:// path) renders that scene instead of the main scene: how a
    changed lab scene is photographed on its own (see capture_scenes)."""
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
    scene = main_scene(project) if scene is None else scene
    if scene:
        args.append(scene)
    rc, out = _godot(project, args, timeout=timeout, log=log)
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


def _flythrough(project, cameras, out_dir, scratch, *, timeout, log=None):
    harness = Path(scratch) / "capture.gd"
    harness.write_text(CAPTURE_HARNESS, encoding="utf-8")
    cam_file = Path(scratch) / "fly_cameras.json"
    cam_file.write_text(json.dumps({"cameras": cameras}), encoding="utf-8")
    avi = Path(scratch) / "flythrough.avi"
    rc, out = _godot(project, ["--rendering-driver", "opengl3", "--resolution",
                               config.EVIDENCE_RESOLUTION, "--write-movie", str(avi),
                               "--fixed-fps", str(config.EVIDENCE_FPS),
                               "--script", str(harness), "--", str(cam_file),
                               str(config.EVIDENCE_SECONDS)], timeout=timeout, log=log)
    if not avi.exists() or "EVIDENCE_CAPTURE_OK" not in out:
        from studio.engine import godot
        raise EvidenceError(f"flythrough recording failed (godot rc={rc}):\n"
                            + "\n".join(godot.output_errors(out)[:15] or [out[-1500:]]))
    mp4, gif = _video(avi, out_dir / "flythrough")
    return mp4, gif, "EVIDENCE_SCENE_UNLIT" in out


UNLIT_WARNING = ("the scene has NO light of its own: these captures add a neutral "
                 "inspection light, but a player running the game sees a black "
                 "screen — add a DirectionalLight3D/WorldEnvironment to the scene")


def _playtest(project, out_dir, scratch, *, timeout, log=None):
    """Record the scripted playtest. Returns (video|None, note|None, reason).

    `reason` says, in EVERY branch, why there is no playtest video — that is
    what manifest['coverage']['playtest'] reports. A bare (None, None) used to
    be indistinguishable from "the video was lost", which is exactly the
    silence this task removes.

    A playtest that fails or crashes here is recorded as a warning, not an
    error: the verify gate already judged it, and the video of a failing run
    is exactly what a reviewer needs to see."""
    if not (Path(project) / "tools" / "playtest.gd").is_file():
        return None, None, _NO_PLAYTEST
    avi = Path(scratch) / "playtest.avi"
    # Recorded through PLAYTEST_WRAPPER: the same script, plus inspection light
    # and a chase camera. The bare script recorded a graybox with no light
    # through a first-person camera — a 35 KB gif of black frames. A playtest
    # the wrapper cannot extend, or that exits non-zero under it, is recorded
    # bare too; the wrapper's recording is kept only if the bare run leaves
    # no video.
    wrapper = Path(scratch) / "playtest_evidence.gd"
    wrapper.write_text(PLAYTEST_WRAPPER, encoding="utf-8")
    wrapped_avi = Path(scratch) / "playtest_wrapped.avi"
    wrapped = None
    for script in (str(wrapper), "res://tools/playtest.gd"):
        avi.unlink(missing_ok=True)
        rc, out = _godot(project, ["--rendering-driver", "opengl3", "--resolution",
                                   config.EVIDENCE_RESOLUTION, "--write-movie", str(avi),
                                   "--fixed-fps", str(config.EVIDENCE_FPS),
                                   "--script", script], timeout=timeout, log=log)
        note = None if rc == 0 else f"the playtest exited {rc} while being recorded"
        if script != str(wrapper):
            break
        if rc == 0 and "EVIDENCE_PLAYTEST_CAMERA" in out:
            break
        if avi.exists() and avi.stat().st_size > 0:
            os.replace(avi, wrapped_avi)
            wrapped = (rc, note)
    if (not avi.exists() or avi.stat().st_size == 0) and wrapped:
        os.replace(wrapped_avi, avi)
        rc, note = wrapped
    shots_src = Path(project) / "studio_shots"
    if shots_src.is_dir():
        dest = out_dir / "playtest_shots"
        dest.mkdir(parents=True, exist_ok=True)
        for png in sorted(shots_src.glob("*.png"))[:12]:
            shutil.copy2(png, dest / png.name)
    if not avi.exists() or avi.stat().st_size == 0:
        return None, note, f"playtest exited {rc} but wrote no video"
    return _video(avi, out_dir / "playtest"), note, ""


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


def baseline(project, repo, sha, *, timeout, log=None):
    """Screenshots of `sha` from the fixed cameras, rendered once and cached.

    Rendered in a throwaway detached worktree of the blessed clone, so the
    baseline is exactly the commit — no uncommitted state, no other task's
    files. Returns (dir|None, status, reason): `status` is one of captured,
    skipped or failed and `reason` says which — a render that FAILED must not
    read the same as a base commit that simply has no Godot project, because
    only the first one is a defect a reviewer has to know about.

    `log` collects the baseline render's own output, so a "no baseline" that is
    really a Godot script error reaches manifest['godot_errors'] instead of
    vanishing."""
    dest = Path(config.EVIDENCE_DIR) / _slug(project) / "baseline" / sha
    if any(dest.glob("*.png")):
        return dest, "captured", ""
    lock = Path(config.EVIDENCE_DIR) / _slug(project) / ".baseline.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        if any(dest.glob("*.png")):
            return dest, "captured", ""
        with tempfile.TemporaryDirectory(prefix="arc-evidence-base-") as tmp:
            wt = Path(tmp) / "wt"
            _git(["worktree", "add", "--detach", str(wt), sha], repo)
            try:
                if not is_godot_project(wt):
                    return None, "skipped", "the base commit is not a Godot project"
                from studio.engine import godot
                godot.import_assets(wt, timeout=timeout)
                try:
                    _render_shots(wt, _cameras(wt), dest, tmp, timeout=timeout,
                                  log=log)
                except EvidenceError as exc:
                    # Keep the reason: "baseline render failed: <why>" is a
                    # different finding from "this base predates the scene".
                    shutil.rmtree(dest, ignore_errors=True)
                    return None, "failed", f"baseline render failed: {str(exc)[:200]}"
            finally:
                _git(["worktree", "remove", "--force", str(wt)], repo, check=False)
    return dest, "captured", ""


# --- changed scenes ---------------------------------------------------------
#
# The fixed anchor cameras photograph the MAIN scene. A task that adds a lab
# scene, or changes a script only a lab scene uses, is invisible to them: PR
# #19 of prison-escape-test changed scenes/labs/security_cameras.tscn and every
# comparison read 0.0% — four shots of an unchanged prison yard. So every
# scene the diff adds or changes, and every scene that instances a changed
# script or scene, is rendered on its own: loaded alone, framed on the
# bounds of what it draws, before (at the merge base) and after, with the
# SAME cameras on both sides so the difference panel means something.

# Loads ONE scene and writes the world-space bounds of every visible
# GeometryInstance3D (nodes built in _ready included), so the cameras can be
# placed around what the scene actually draws.
SCENE_PROBE = '''extends SceneTree
# Written by arc-orchestrator evidence.py into a temp dir; never committed.
#   godot --path <project> --script <this> -- <scene> <out.json>

func _initialize() -> void:
\tvar args := OS.get_cmdline_user_args()
\tif args.size() < 2:
\t\tpush_error("usage: probe.gd -- <scene> <out.json>")
\t\tquit(2)
\t\treturn
\tvar packed: PackedScene = load(args[0]) as PackedScene
\tif packed == null:
\t\tpush_error("cannot load scene: " + args[0])
\t\tquit(2)
\t\treturn
\tvar inst: Node = packed.instantiate()
\tif inst == null:
\t\tpush_error("cannot instantiate scene: " + args[0])
\t\tquit(2)
\t\treturn
\tget_root().add_child(inst)
\tawait process_frame
\tawait process_frame
\tvar nodes: Array = inst.find_children("*", "GeometryInstance3D", true, false)
\tif inst is GeometryInstance3D:
\t\tnodes.append(inst)
\tvar boxes: Array = []
\tfor n in nodes:
\t\tvar gi := n as GeometryInstance3D
\t\tif gi == null or not gi.is_visible_in_tree():
\t\t\tcontinue
\t\tvar box: AABB = gi.global_transform * gi.get_aabb()
\t\tvar vals: Array = [box.position.x, box.position.y, box.position.z,
\t\t\t\tbox.size.x, box.size.y, box.size.z]
\t\tvar ok := true
\t\tfor v in vals:
\t\t\tif not is_finite(float(v)):
\t\t\t\tok = false
\t\tif ok and box.size.length() > 0.0:
\t\t\tboxes.append(vals)
\t\tif boxes.size() >= 4000:
\t\t\tbreak
\tvar canvas: int = inst.find_children("*", "CanvasItem", true, false).size()
\tif inst is CanvasItem:
\t\tcanvas += 1
\tvar f := FileAccess.open(args[1], FileAccess.WRITE)
\tif f == null:
\t\tpush_error("cannot write " + args[1])
\t\tquit(2)
\t\treturn
\tf.store_string(JSON.stringify({"boxes": boxes, "canvas_items": canvas}))
\tf.close()
\tprint("EVIDENCE_PROBE_OK ", boxes.size())
\tquit(0)
'''

_SCENE_EXCLUDED = ("tests/", "tools/", ".godot/", "addons/", ".arc/")


def parse_name_status(text):
    """{path: 'A'|'M'|'D'} from `git diff --name-status --no-renames` output."""
    out = {}
    for line in (text or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0]:
            out[parts[-1].strip()] = parts[0][0]
    return out


def scene_texts(worktree):
    """{relative .tscn path: text} for every scene a player could be shown."""
    root = Path(worktree)
    out = {}
    for p in sorted(root.rglob("*.tscn")):
        rel = p.relative_to(root).as_posix()
        if rel.startswith(_SCENE_EXCLUDED) or "/." in "/" + rel:
            continue
        try:
            out[rel] = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return out


def changed_scenes(status, texts, *, limit=None):
    """The scenes whose look this diff changes, most direct first.

    `status` is {path: A|M|D} (parse_name_status plus untracked files as A),
    `texts` is scene_texts() of the AFTER tree. A scene is included when:
      added     the diff adds it
      changed   the diff modifies it
      dependent it references (ext_resource path="res://...") a script, scene
                or resource the diff adds or modifies
    Tests and tools are never shown (a test scene is not what a player sees),
    and a deleted scene has no "after" to render. Returns
    (rendered, skipped): the first `limit` entries and the rest, each
    {"path", "res", "status", "why"}."""
    limit = config.EVIDENCE_MAX_SCENES if limit is None else limit
    touched = sorted(p for p, st in (status or {}).items()
                     if st in ("A", "M") and _is_gameplay(p))
    direct, dependent = [], []
    for rel in sorted(texts or {}):
        if rel.startswith(_SCENE_EXCLUDED):
            continue
        st = (status or {}).get(rel)
        if st in ("A", "M"):
            direct.append({"path": rel, "res": "res://" + rel,
                           "status": "added" if st == "A" else "changed",
                           "why": "added by this diff" if st == "A"
                           else "modified by this diff"})
            continue
        uses = [p for p in touched if p != rel
                and (f'path="res://{p}"' in texts[rel])]
        if uses:
            dependent.append({"path": rel, "res": "res://" + rel,
                              "status": "dependent",
                              "why": "uses " + ", ".join(uses[:3])
                              + (f" (+{len(uses) - 3} more)" if len(uses) > 3 else "")})
    direct.sort(key=lambda e: (e["status"] != "added", e["path"]))
    every = direct + dependent
    return every[:max(0, limit)], every[max(0, limit):]


def scene_bounds(boxes, *, outlier=50.0):
    """{"position", "size"}: the union of `boxes` ([x, y, z, sx, sy, sz]).

    A box whose longest side is more than `outlier` times the median longest
    side is left out — a 2 km ground plane under a 20 m room would otherwise
    frame the room as a speck. None when there is nothing 3D to frame."""
    valid = []
    for b in boxes or []:
        try:
            v = [float(x) for x in b]
        except (TypeError, ValueError):
            continue
        if len(v) == 6 and all(math.isfinite(x) for x in v) and max(v[3:]) > 0:
            valid.append(v)
    if not valid:
        return None
    sides = sorted(max(b[3:]) for b in valid)
    med = sides[len(sides) // 2]
    keep = [b for b in valid if max(b[3:]) <= outlier * med] or valid
    lo = [min(b[i] for b in keep) for i in range(3)]
    hi = [max(b[i] + b[i + 3] for b in keep) for i in range(3)]
    return {"position": [round(x, 4) for x in lo],
            "size": [round(h - l, 4) for l, h in zip(lo, hi)]}


# Views around a scene's bounds: (name, direction from the centre). "top" is
# tilted a hair off vertical so look_at has a defined up; the elevated views
# see over the walls of a room, which a level view cannot.
FRAME_VIEWS = (("overview", (1.0, 1.1, 1.0)),
               ("top", (0.0, 1.0, 0.02)),
               ("front", (0.0, 0.7, 1.0)),
               ("side", (1.0, 0.7, 0.0)))


def _norm(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _center(bounds):
    return [p + s / 2 for p, s in zip(bounds["position"], bounds["size"])]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0]]


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _corners(bounds):
    p, s = bounds["position"], bounds["size"]
    return [[p[0] + s[0] * i, p[1] + s[1] * j, p[2] + s[2] * k]
            for i in (0, 1) for j in (0, 1) for k in (0, 1)]


def view_basis(direction):
    """(u, right, up) of a camera placed along `direction` from its target and
    looking back at it, with world +Y as up — Godot's look_at convention."""
    u = _norm(direction)
    fwd = [-x for x in u]
    right = _norm(_cross(fwd, [0.0, 1.0, 0.0]))
    up = _cross(right, fwd)
    return u, right, up


def fit_distance(bounds, direction, *, fov=50.0, aspect=16 / 9, margin=1.08):
    """Smallest distance from the bounds' centre, along `direction`, at which
    all eight corners are inside the frame (with `margin` of air).

    `fov` is Godot's vertical Camera3D.fov (keep_aspect = KEEP_HEIGHT); the
    horizontal half-angle follows from `aspect`. For a corner at camera-space
    offset (x, y) and depth z toward the camera, it is in frame when
    |x| <= (d - z)·tan(h/2) and |y| <= (d - z)·tan(v/2) — so d is the largest
    z + |x|/tan(h/2) (or |y|/tan(v/2)) over the corners. A tight fit, unlike a
    bounding sphere, which frames a flat 20 m room at ~60% of the picture."""
    c = _center(bounds)
    u, right, up = view_basis(direction)
    tv = math.tan(math.radians(fov) / 2)
    th = tv * aspect
    d = 0.0
    for corner in _corners(bounds):
        o = [a - b for a, b in zip(corner, c)]
        x, y, z = _dot(o, right) * margin, _dot(o, up) * margin, _dot(o, u)
        d = max(d, z + abs(x) / th, z + abs(y) / tv)
    # Never inside the box, and never so close a degenerate box fills nothing.
    return max(d, 1.0, max(bounds["size"]) * 0.5 + 0.5)


def in_frame(camera, point, *, aspect=16 / 9):
    """Whether `point` projects inside `camera`'s picture (tests use this)."""
    pos, look = camera["position"], camera["look_at"]
    u, right, up = view_basis([a - b for a, b in zip(pos, look)])
    o = [a - b for a, b in zip(point, pos)]
    depth = -_dot(o, u)
    if depth <= 0:
        return False
    tv = math.tan(math.radians(camera.get("fov", 70.0)) / 2)
    return (abs(_dot(o, right)) <= depth * tv * aspect + 1e-6
            and abs(_dot(o, up)) <= depth * tv + 1e-6)


def frame_cameras(bounds, *, fov=50.0, aspect=16 / 9, margin=1.08, views=FRAME_VIEWS):
    """Cameras (camera_system dicts) that each show ALL of `bounds`.

    Every camera looks at the centre from its own `fit_distance`, so every
    corner of the box is inside its view and the content fills the frame. A
    scene with no 3D content (a 2D/UI scene) gets one "screen" camera: its
    canvas draws regardless of where the 3D camera points."""
    if not bounds:
        return [{"name": "screen", "position": [0.0, 0.0, 10.0],
                 "look_at": [0.0, 0.0, 0.0], "fov": 70.0, "kind": "auto"}]
    c = _center(bounds)
    out = []
    for name, direction in views:
        u = _norm(direction)
        d = fit_distance(bounds, direction, fov=fov, aspect=aspect, margin=margin)
        out.append({"name": name,
                    "position": [round(c[i] + u[i] * d, 4) for i in range(3)],
                    "look_at": [round(x, 4) for x in c], "fov": fov,
                    "kind": "auto"})
    return out


def orbit_cameras(bounds, *, n=8, elevation=40.0, fov=50.0, aspect=16 / 9,
                  margin=1.08):
    """n+1 keyframes circling the bounds (the last closes the loop), for the
    per-scene orbit video: every side of the scene, not one angle of it."""
    if not bounds:
        return frame_cameras(None)
    c = _center(bounds)
    el = math.radians(elevation)
    dirs = [[math.cos(el) * math.cos(2 * math.pi * i / n), math.sin(el),
             math.cos(el) * math.sin(2 * math.pi * i / n)] for i in range(n + 1)]
    # One radius for the whole orbit (the widest any keyframe needs), so the
    # camera circles instead of bobbing in and out.
    d = max(fit_distance(bounds, u, fov=fov, aspect=aspect, margin=margin)
            for u in dirs)
    out = []
    for i, u in enumerate(dirs):
        out.append({"name": f"orbit{i}",
                    "position": [round(c[k] + u[k] * d, 4) for k in range(3)],
                    "look_at": [round(x, 4) for x in c], "fov": fov})
    return out


def _probe_scene(project, res, scratch, *, timeout, log=None):
    """(bounds|None, canvas_items) of one scene, from SCENE_PROBE."""
    harness = Path(scratch) / "probe.gd"
    harness.write_text(SCENE_PROBE, encoding="utf-8")
    out_json = Path(scratch) / "probe.json"
    out_json.unlink(missing_ok=True)
    rc, out = _godot(project, ["--rendering-driver", "opengl3", "--resolution",
                               "320x180", "--script", str(harness), "--", res,
                               str(out_json)], timeout=timeout, log=log)
    if "EVIDENCE_PROBE_OK" not in out or not out_json.exists():
        from studio.engine import godot
        raise EvidenceError(f"could not load {res} (godot rc={rc}):\n"
                            + "\n".join(godot.output_errors(out)[:10] or [out[-800:]]))
    data = json.loads(out_json.read_text(encoding="utf-8"))
    return scene_bounds(data.get("boxes")), int(data.get("canvas_items") or 0)


@contextlib.contextmanager
def _detached(repo, sha, *, timeout):
    """A throwaway detached worktree of `repo` at `sha`, assets imported."""
    with tempfile.TemporaryDirectory(prefix="arc-evidence-before-") as tmp:
        wt = Path(tmp) / "wt"
        _git(["worktree", "add", "--detach", str(wt), sha], repo)
        try:
            if is_godot_project(wt):
                from studio.engine import godot
                godot.import_assets(wt, timeout=timeout)
            yield wt
        finally:
            _git(["worktree", "remove", "--force", str(wt)], repo, check=False)


def _scene_video(project, res, cams, out_stem, scratch, *, timeout, log=None):
    """An orbit around one scene, recorded like the flythrough."""
    harness = Path(scratch) / "capture.gd"
    harness.write_text(CAPTURE_HARNESS, encoding="utf-8")
    cam_file = Path(scratch) / "orbit_cameras.json"
    cam_file.write_text(json.dumps({"cameras": cams}), encoding="utf-8")
    avi = Path(scratch) / (out_stem.name + ".avi")
    rc, out = _godot(project, ["--rendering-driver", "opengl3", "--resolution",
                               config.EVIDENCE_RESOLUTION, "--write-movie", str(avi),
                               "--fixed-fps", str(config.EVIDENCE_FPS),
                               "--script", str(harness), "--", str(cam_file),
                               str(config.EVIDENCE_SCENE_SECONDS), res],
                     timeout=timeout, log=log)
    if not avi.exists() or "EVIDENCE_CAPTURE_OK" not in out:
        raise EvidenceError(f"orbit recording of {res} failed (godot rc={rc})")
    return _video(avi, out_stem)


def max_changed(rows):
    """The largest measured change among compare rows, or None."""
    vals = [r["changed"] for r in rows or [] if r.get("changed") is not None]
    return max(vals) if vals else None


def capture_scenes(worktree, out_dir, scratch, *, repo=None, sha=None,
                   timeout, log=None, limit=None):
    """Render every scene the diff changes, auto-framed, before and after.

    Returns (entries, skipped, warnings). An entry is the changed_scenes()
    dict plus bounds, cameras, shots (after), before, compare rows,
    max_changed, video and error. A scene that will not load alone (it needs
    a parent that provides something) is a WARNING on that entry, not a gate
    failure: the verify gate judges behaviour, this only shows it."""
    worktree, out_dir = Path(worktree), Path(out_dir)
    ref = sha or "HEAD"
    status = parse_name_status(_git(["diff", "--name-status", "--no-renames", ref],
                                    worktree, check=False))
    for rel in _untracked(worktree):
        status.setdefault(rel, "A")
    picked, skipped = changed_scenes(status, scene_texts(worktree), limit=limit)
    warnings = []
    if skipped:
        warnings.append(f"{len(skipped)} more changed scene(s) not rendered "
                        f"(ARC_EVIDENCE_MAX_SCENES={len(picked)}): "
                        + ", ".join(e["path"] for e in skipped[:6]))
    for e in picked:
        sdir = out_dir / "scenes" / _slug(e["path"][:-len(".tscn")])
        e.update({"dir": str(sdir), "shots": [], "before": [], "compare": [],
                  "max_changed": None, "error": ""})
        try:
            bounds, canvas = _probe_scene(worktree, e["res"], scratch,
                                          timeout=timeout, log=log)
            e["bounds"], e["canvas_items"] = bounds, canvas
            e["cameras"] = frame_cameras(bounds)
            shots = _render_shots(worktree, e["cameras"], sdir / "after", scratch,
                                  timeout=timeout, log=log, scene=e["res"])
            e["shots"] = [str(s) for s in shots]
        except (EvidenceError, OSError, ValueError) as exc:
            e["error"] = str(exc).splitlines()[0][:300]
            warnings.append(f"scene {e['path']} did not render: {e['error']}")
            continue
        for s in shots:
            share = blank_share(s)
            if share is not None and share >= config.EVIDENCE_BLANK_SHARE:
                warnings.append(f"scene {e['path']} view '{s.stem}' is a nearly "
                                f"solid frame ({share:.0%} one colour)")
        if bounds:
            try:
                mp4, gif = _scene_video(worktree, e["res"], orbit_cameras(bounds),
                                        sdir / "orbit", scratch, timeout=timeout,
                                        log=log)
                e["video"] = {"mp4": str(mp4), "gif": str(gif)}
            except EvidenceError as exc:
                warnings.append(str(exc).splitlines()[0][:300])
    want_before = [e for e in picked if e.get("shots") and e["status"] != "added"]
    if want_before and repo and sha:
        head = _git(["rev-parse", "HEAD"], worktree, check=False)
        try:
            with _detached(repo, sha, timeout=timeout) as before_wt:
                for e in want_before:
                    if not (before_wt / e["path"]).is_file():
                        e["status"], e["why"] = "added", "not present at the branch point"
                        continue
                    sdir = Path(e["dir"])
                    try:
                        before = _render_shots(before_wt, e["cameras"], sdir / "before",
                                               scratch, timeout=timeout, log=log,
                                               scene=e["res"])
                    except EvidenceError as exc:
                        warnings.append(f"scene {e['path']} did not render at the "
                                        f"branch point: {str(exc).splitlines()[0][:200]}")
                        continue
                    e["before"] = [str(b) for b in before]
                    e["compare"] = compare(sdir / "before",
                                           [Path(s) for s in e["shots"]],
                                           sdir / "compare", before_sha=sha,
                                           after_sha=head)
                    e["max_changed"] = max_changed(e["compare"])
        except (EvidenceError, subprocess.SubprocessError, OSError) as exc:
            warnings.append(f"no branch-point render of the changed scenes: "
                            f"{str(exc)[:200]}")
    return picked, skipped, warnings


# --- capture ----------------------------------------------------------------

def _cover(manifest, kind, status, reason=""):
    """Record what happened to ONE kind of evidence: captured, skipped, failed.

    `reason` is mandatory in spirit: a non-captured kind with no reason is the
    silent gap this exists to close ("nothing changed visually" and "the
    capture did not happen" must never look the same to a reviewer)."""
    manifest.setdefault("coverage", {})[kind] = {"status": status, "reason": reason}


# A change a player can SEE: a script or a scene, not a test or a tool.
_GAMEPLAY_SUFFIXES = (".gd", ".tscn", ".tres")
_GAMEPLAY_EXCLUDED = ("tests/", "tools/")


def _is_gameplay(path):
    """Does a change to this ONE path change what the game looks like?

    Tests and tools are excluded on purpose: `tests/foo.gd` has a gameplay
    suffix, but a diff that only touches it is not a gameplay change. This is
    the single definition `gameplay_diff` and `no_visible_change` both use, so
    a hand-supplied gameplay list cannot disagree with the worktree scan."""
    p = (path or "").strip()
    return p.endswith(_GAMEPLAY_SUFFIXES) and not p.startswith(_GAMEPLAY_EXCLUDED)


def gameplay_diff(worktree, ref):
    """Paths this task changed that change what the game looks like."""
    names = _git(["diff", "--name-only", ref or "HEAD"], worktree, check=False)
    names += "\n" + "\n".join(sorted(_untracked(worktree)))
    return sorted({p.strip() for p in names.splitlines() if _is_gameplay(p)})


def scene_shots(scenes):
    """The shots that ACTUALLY exist under manifest['scenes'].

    `scenes` lists ATTEMPTED renders, so a nonempty list is not proof a scene
    was drawn — an entry whose `shots` are missing (or never written) rendered
    nothing, and must neither count as a captured scene nor suppress the
    no-visible-change flag."""
    return [s for sc in (scenes or []) for s in (sc.get("shots") or [])
            if Path(s).exists()]


def no_visible_change(compare_rows, gameplay, scenes, *, min_change=None):
    """Whether a gameplay diff produced no visual change anyone can see.

    True only when ALL of these hold (Rule 7d — the flag a reviewer needs): the
    diff touches a .gd/.tscn/.tres outside tests/ and tools/, there is at least
    one comparison to read, and EVERY comparison moved less than
    `config.EVIDENCE_MIN_CHANGE` of its pixels. When changed scenes were
    rendered, THEIR before/after comparisons are the ones judged (the main
    scene's fixed cameras are expected to read 0% for a change in a lab
    scene), and a rendered scene with no comparison — a new scene — shows the
    change by existing, so the flag stays off.

    Every clause guards against a false alarm. With no comparison row there is
    nothing that could have been unchanged (the coverage table says why there
    is none), a `new` viewpoint is not a "no change" reading, and a scene
    render is another look at the same change — a scene entry with no image on
    disk is not a render, so it does not count (see `scene_shots`)."""
    min_change = config.EVIDENCE_MIN_CHANGE if min_change is None else min_change
    if not any(_is_gameplay(p) for p in (gameplay or [])):
        return False
    if scene_shots(scenes):
        # The changed scenes ARE the evidence: the main scene's fixed cameras
        # are expected to read 0% when a lab scene changed. A scene rendered
        # with nothing to compare against (it is new, or its branch-point
        # render failed) shows the change by existing, so it is not "no
        # change"; a scene whose before/after moved nothing is.
        drawn = [sc for sc in scenes or [] if scene_shots([sc])]
        if any(not sc.get("compare") for sc in drawn):
            return False
        rows = [r for sc in drawn for r in sc["compare"]]
    else:
        rows = list(compare_rows or [])
    if not rows:
        return False
    return all(c.get("changed") is not None and c["changed"] < min_change
               for c in rows)


def capture(worktree, out_dir, *, repo=None, base=None, project="",
            timeout=None, non_visual=False):
    """Capture every kind of evidence for `worktree` into `out_dir`.

    `non_visual` (the task's `"visual": false`) is recorded in the manifest,
    where prompt_block reads it — also when a resumed review loads the
    manifest back from disk.

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
    glog = []                      # raw output of EVERY render call
    manifest = {"worktree": str(worktree), "project": project,
                "out_dir": str(out_dir),
                "head": _git(["rev-parse", "HEAD"], worktree,
                             check=False),
                "shots": [], "videos": {}, "compare": [], "warnings": [],
                "coverage": {}, "godot_errors": [],
                "non_visual": bool(non_visual)}
    with tempfile.TemporaryDirectory(prefix="arc-evidence-") as scratch, \
            _leave_no_trace(worktree):
        godot.import_assets(worktree, timeout=timeout)
        cams = _cameras(worktree)
        shots = _render_shots(worktree, cams, out_dir / "shots", scratch,
                              timeout=timeout, log=glog)
        manifest["shots"] = [str(s) for s in shots]
        _cover(manifest, "fixed_cameras", "captured" if shots else "failed",
               "" if shots else "no camera rendered a screenshot")
        for s in shots:
            share = blank_share(s)
            if share is not None and share >= config.EVIDENCE_BLANK_SHARE:
                manifest["warnings"].append(
                    f"camera '{s.stem}' rendered a nearly solid frame "
                    f"({share:.0%} one colour): nothing visible from there, or "
                    "the scene failed to draw")
        try:
            mp4, gif, unlit = _flythrough(worktree, cams, out_dir, scratch,
                                          timeout=timeout, log=glog)
            manifest["videos"]["flythrough"] = {"mp4": str(mp4), "gif": str(gif)}
            manifest["scene_unlit"] = unlit
            if unlit:
                manifest["warnings"].append(UNLIT_WARNING)
            _cover(manifest, "flythrough", "captured")
        except EvidenceError as exc:
            _cover(manifest, "flythrough", "failed", str(exc).splitlines()[0])
            manifest["warnings"].append(str(exc).splitlines()[0])
        pt, note, reason = _playtest(worktree, out_dir, scratch, timeout=timeout,
                                     log=glog)
        if pt:
            manifest["videos"]["playtest"] = {"mp4": str(pt[0]), "gif": str(pt[1])}
            _cover(manifest, "playtest", "captured")
        else:
            _cover(manifest, "playtest",
                   "skipped" if reason == _NO_PLAYTEST else "failed", reason)
        if note:
            manifest["warnings"].append(note)
        manifest["playtest_shots"] = [str(p) for p in
                                      sorted((out_dir / "playtest_shots").glob("*.png"))]
        _cover(manifest, "playtest_shots",
               "captured" if manifest["playtest_shots"] else "skipped",
               "" if manifest["playtest_shots"] else
               (_NO_PLAYTEST if not (worktree / "tools" / "playtest.gd").is_file()
                else "the playtest wrote no screenshots to studio_shots/"))
    if repo and base:
        sha = merge_base(worktree, base)
        if not sha:
            _cover(manifest, "baseline", "skipped", "no merge base")
            _cover(manifest, "compare", "skipped", "no merge base")
        else:
            try:
                bdir, bstatus, breason = baseline(project or Path(repo).name, repo,
                                                  sha, timeout=timeout, log=glog)
            except (EvidenceError, subprocess.SubprocessError, OSError) as exc:
                bdir, bstatus = None, "failed"
                breason = f"baseline render failed: {str(exc)[:200]}"
                # The baseline render's own Godot output, kept like any other:
                # "the base commit will not render" is often a script error the
                # reviewer should read, not a mystery.
                manifest["warnings"].append(f"no baseline: {str(exc)[:200]}")
            if bdir:
                # Cached renders and fresh ones both land here.
                bstatus, breason = "captured", ""
            _cover(manifest, "baseline", bstatus,
                   "" if bstatus == "captured"
                   else (breason or f"no baseline at {sha[:10]}"))
            manifest["baseline"] = {"sha": sha, "dir": str(bdir) if bdir else None,
                                    "status": bstatus, "reason": breason}
            if bdir:
                manifest["compare"] = compare(
                    bdir, shots, out_dir / "compare",
                    before_sha=sha, after_sha=manifest.get("head", ""))
                _cover(manifest, "compare", "captured" if manifest["compare"] else "failed",
                       "" if manifest["compare"] else "no camera had a baseline image")
            else:
                _cover(manifest, "compare", "skipped",
                       manifest["coverage"]["baseline"]["reason"]
                       or "no baseline to compare against")
    else:
        _cover(manifest, "baseline", "skipped", "no merge base")
        _cover(manifest, "compare", "skipped", "no merge base")
    # Every scene the diff changes, rendered on its own and framed on its
    # content, before and after (the fixed cameras only ever see the main
    # scene). Its own scratch dir: the one above is gone by now.
    sha = (manifest.get("baseline") or {}).get("sha") or None
    with tempfile.TemporaryDirectory(prefix="arc-evidence-scenes-") as scratch, \
            _leave_no_trace(worktree):
        picked, skipped, swarn = capture_scenes(
            worktree, out_dir, scratch, repo=repo, sha=sha, timeout=timeout,
            log=glog)
    manifest["scenes"] = picked
    manifest["scenes_skipped"] = [e["path"] for e in skipped]
    manifest["warnings"] += swarn
    compared = [e for e in picked if e.get("compare")]
    _cover(manifest, "scene_compare",
           "captured" if compared else "skipped",
           "" if compared else
           ("the diff changes no scene" if not picked else
            "no changed scene had a branch-point render to compare with "
            "(new scenes have no before)"))
    scenes = manifest.get("scenes") or []
    # A scene entry with no shots on disk rendered nothing: `scenes` is a list
    # of ATTEMPTED renders, so a nonempty list is not proof a scene was drawn.
    drawn = scene_shots(scenes)
    _cover(manifest, "scenes",
           "captured" if drawn else ("skipped" if not scenes else "failed"),
           "" if drawn else
           ("the diff adds or changes no scene, and no scene uses a changed "
            "script" if not scenes else
            f"{len(scenes)} scene render(s) produced no image"))
    man_glog, glog[:] = list(glog), []
    manifest["godot_errors"] = godot_errors(*man_glog)
    for line in manifest["godot_errors"]:
        emit("godot_error", task=project, line=line[:300])
    ref = (manifest.get("baseline") or {}).get("sha") or ""
    gameplay = gameplay_diff(worktree, ref)
    manifest["gameplay_diff"] = gameplay
    # `scenes`, not `drawn`: no_visible_change applies scene_shots() itself, and
    # passing the helper would iterate the function object (TypeError on any
    # gameplay diff — which capture_evidence then swallows, losing the whole
    # manifest). `drawn` is a list of shot PATHS and is not a scenes list.
    if no_visible_change(manifest["compare"], gameplay, scenes):
        manifest["no_visible_change"] = True
        manifest["warnings"].append(
            "NO VISIBLE CHANGE: this diff touches "
            + ", ".join(gameplay[:5])
            + f" but every view changed <{config.EVIDENCE_MIN_CHANGE:.1%} of its "
            "pixels (" + ("the changed scenes rendered before and after"
                          if any(sc.get("compare") for sc in scenes)
                          else "the main scene's fixed cameras")
            + ") — either the change is genuinely invisible, or the capture "
            "did not see it (see coverage)")
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


def _scene_label(sc):
    return Path(str(sc.get("path") or "")).stem or "scene"


def _ranked_panels(sc):
    """One changed scene's images, most informative first: its before|after|
    diff panels by share changed (largest first), or — for a scene with no
    before — its after shots."""
    rows = [c for c in sc.get("compare") or [] if c.get("side_by_side")]
    if rows:
        rows.sort(key=lambda c: -(c.get("changed") or 0.0))
        return [c["side_by_side"] for c in rows]
    return list(sc.get("shots") or [])


def capture_shots(manifest):
    """[(label, path)] from the manifest: every render, changed scenes first.

    The changed scenes are what the task is about, so their tiles lead the
    contact sheet, labelled "<scene>/<view>"; the main scene's fixed cameras
    follow, labelled by camera. These labels are what the contact sheet prints
    under each tile."""
    out = []
    for sc in manifest.get("scenes") or []:
        name = _scene_label(sc)
        for s in sc.get("shots") or []:
            out.append((f"{name}/{Path(s).stem}", s))
    out += [(Path(s).stem, s) for s in manifest.get("shots") or []]
    return out


def review_images(manifest, limit=10):
    """Images to attach for a reviewer, most informative first.

    The changed scenes lead — the two most-changed before|after|diff panels of
    each (or its after shots when the scene is new) — because they are the
    change itself. Then the contact sheet (one labeled grid of every view),
    the rest of the scene panels, the main scene's comparisons, uncompared
    shots and playtest shots."""
    scenes = [sc for sc in manifest.get("scenes") or [] if scene_shots([sc])]
    ranked = [_ranked_panels(sc) for sc in scenes]
    out = [p for r in ranked for p in r[:2]]
    sheet = manifest.get("contact_sheet")
    if sheet and Path(sheet).exists():
        out.append(sheet)
    out += [p for r in ranked for p in r[2:]]
    out += [c["side_by_side"] for c in manifest.get("compare") or []
            if c.get("side_by_side")]
    compared = {Path(c["side_by_side"]).stem for c in manifest.get("compare") or []
                if c.get("side_by_side")}
    out += [s for s in manifest.get("shots") or [] if Path(s).stem not in compared]
    out += list(manifest.get("playtest_shots") or [])
    seen, keep = set(), []
    for p in out:
        if p and p not in seen and Path(p).exists():
            seen.add(p)
            keep.append(p)
    return keep[:limit]


_COVERAGE_ORDER = ("scenes", "scene_compare", "fixed_cameras", "flythrough",
                   "playtest", "playtest_shots", "baseline", "compare")


def coverage_lines(manifest):
    """One line per evidence kind: what was captured, what was not, and why.

    A reviewer must be able to tell "nothing changed visually" apart from
    "the capture did not happen", so a non-captured kind is never omitted."""
    cov = manifest.get("coverage") or {}
    out = []
    for kind in _COVERAGE_ORDER:
        c = cov.get(kind)
        if not c:
            continue
        out.append(f"- coverage {kind}: {c.get('status')}"
                   + (f" — {c['reason']}" if c.get("reason") else ""))
    return out


def scene_summary(sc):
    """One line for one changed scene: what it is and how much it changed."""
    head = f"`{sc.get('path')}` ({sc.get('status')}: {sc.get('why')})"
    if sc.get("error"):
        return head + f" — DID NOT RENDER: {sc['error']}"
    if not scene_shots([sc]):
        return head + " — no image"
    if sc.get("compare"):
        per = ", ".join(f"{c['name']} {_pct(c.get('changed'))}"
                        for c in sc["compare"] if not c.get("new"))
        return head + f" — {_pct(sc.get('max_changed'))} of pixels changed at most ({per})"
    return head + " — NEW: no before image; the after renders show it"


def prompt_block(manifest):
    """Text for a reviewer prompt: what was captured, where, and what changed."""
    if not manifest:
        return ""
    lines = ["VISUAL EVIDENCE (captured after the verify gate passed). Look at it: "
             "a visual regression or a change that does not show what the task "
             "asks for is a blocking issue, exactly like a failing test."]
    scenes = manifest.get("scenes") or []
    if manifest.get("non_visual"):
        lines += ["This task is marked NON-VISUAL (`\"visual\": false`): an "
                  "unchanged picture is expected. A visible regression is still "
                  "a blocking issue."]
    else:
        lines += [
            "BLOCKING RULE: if the change this task asks for is NOT VISIBLE in "
            "this evidence — the new or changed thing is absent from the scene "
            "renders, or a changed scene's before|after|diff shows no difference "
            "where the task says there should be one — REJECT with the issue "
            "\"the change is not visible in the evidence\" and say what you "
            "expected to see where. The only exception is a task explicitly "
            "marked non-visual."]
    if scenes:
        base = ((manifest.get("baseline") or {}).get("sha") or "")[:10]
        lines += ["",
                  "CHANGED SCENES — each rendered ALONE, framed on its own content, "
                  "with the SAME cameras before (branch point "
                  f"{base or 'unknown'}) and after. These are the images to judge "
                  "the change by; the main scene's fixed cameras below only show "
                  "the level around it."]
        for sc in scenes:
            lines.append("- " + scene_summary(sc).replace("`", ""))
            for p in _ranked_panels(sc):
                lines.append(f"    image: {p}")
            if sc.get("video"):
                lines.append(f"    orbit video: {sc['video'].get('mp4')} "
                             f"(preview {sc['video'].get('gif')})")
        if manifest.get("scenes_skipped"):
            lines.append("- also changed but not rendered (cap): "
                         + ", ".join(manifest["scenes_skipped"]))
        lines.append("If you cannot open images, judge by the numbers: a changed "
                     "scene at 0.0% did not change on screen.")
    if manifest.get("no_visible_change"):
        lines += [
            "",
            "*** NO VISIBLE CHANGE — READ THIS BEFORE APPROVING. ***",
            "This diff touches gameplay files ("
            + ", ".join(manifest.get("gameplay_diff") or [])[:300] + ") but the "
            "evidence shows NO visual change: every view differs from the branch "
            "point by less than "
            f"{config.EVIDENCE_MIN_CHANGE:.1%} of its pixels"
            + (", including every changed scene rendered on its own."
               if any(sc.get("compare") for sc in scenes)
               else ", and no scene render shows the change either."),
            "That is either a genuinely invisible change or a capture that missed "
            "it — you cannot tell which from the images, and neither can the "
            "orchestrator. So you MUST do one of these two things: (a) verify the "
            "change another way and say which — the tests that fail without it, "
            "the scene stats in the coverage table, a camera that should have "
            "moved and why it did not; or (b) REJECT for missing evidence, "
            "because a gameplay change nobody can see has not been demonstrated.",
            "Approving on an unchanged screenshot is not an option.",
            ""]
    if scenes:
        lines.append("MAIN SCENE (fixed anchor cameras):")
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
    lines += coverage_lines(manifest)
    for e in manifest.get("godot_errors") or []:
        lines.append(f"- GODOT ERROR: {e}")
    for w in manifest.get("warnings") or []:
        lines.append(f"- WARNING: {w}")
    return "\n".join(lines) + "\n"


def _coverage_table(manifest, short=False):
    """The coverage table for a PR comment / board post: kind | status | reason.

    `short` keeps a board line to one line: only the kinds that are not
    captured, which are the ones a reader has to know about."""
    cov = manifest.get("coverage") or {}
    rows = [(k, c) for k, c in cov.items()
            if not short or c.get("status") != "captured"]
    if not rows:
        return []
    order = {k: i for i, k in enumerate(_COVERAGE_ORDER)}
    rows.sort(key=lambda kv: order.get(kv[0], 99))
    out = ["| evidence | status | reason |", "|---|---|---|"]
    out += [f"| {k} | {c.get('status') or '?'} | "
            f"{(c.get('reason') or '—').replace('|', '/')} |" for k, c in rows]
    return out


def board_body(manifest):
    n = len(manifest.get("shots") or [])
    vids = ", ".join(sorted((manifest.get("videos") or {})))
    changed = [f"{c['name']} {_pct(c.get('changed'))}" for c in
               manifest.get("compare") or [] if c.get("changed")]
    body = f"evidence: {n} screenshot(s)" + (f", video: {vids}" if vids else "")
    scenes = manifest.get("scenes") or []
    if scenes:
        body += "; changed scenes: " + ", ".join(
            f"{_scene_label(sc)} "
            + ("did not render" if sc.get("error") else
               _pct(sc.get("max_changed")) if sc.get("compare") else "new")
            for sc in scenes[:6])
    if changed:
        body += "; changed vs branch point: " + ", ".join(changed[:6])
    gaps = [f"{k}:{c.get('status')}"
            + (f" ({c['reason'][:60]})" if c.get("reason") else "")
            for k, c in (manifest.get("coverage") or {}).items()
            if c.get("status") != "captured"]
    if gaps:
        body += "; gaps: " + ", ".join(gaps[:5])
    if manifest.get("godot_errors"):
        body += f"; {len(manifest['godot_errors'])} godot error(s)"
    if manifest.get("no_visible_change"):
        body += "; NO VISIBLE CHANGE for a gameplay diff"
    if manifest.get("warnings"):
        body += f"; {len(manifest['warnings'])} warning(s)"
    body += f" — {Path(manifest.get('shots', ['.'])[0]).parent.parent}"
    if scenes:
        body += "\n\n**Changed scenes**\n" + "\n".join(
            "- " + scene_summary(sc) for sc in scenes)
    table = _coverage_table(manifest)
    if table:
        body += "\n\n**Evidence coverage**\n" + "\n".join(table)
    errs = manifest.get("godot_errors") or []
    if errs:
        # Every unique line, in capture order. Only the total is bounded, so one
        # chatty frame cannot push the remaining errors off the post.
        body += "\n\n**Godot errors**\n"
        budget, shown = 3000, 0
        for e in errs:
            if budget - len(e) < 0:
                body += f"- (+{len(errs) - shown} more — see the PR comment)\n"
                break
            budget -= len(e)
            shown += 1
            body += f"- `{e[:300]}`\n"
    if manifest.get("warnings"):
        body += "\n\n**Warnings**\n" + "\n".join(f"- {w}" for w in manifest["warnings"])
    return body

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
    src = (evidence_root(manifest)
           if manifest.get("shots") or manifest.get("out_dir") else None)
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


def evidence_root(manifest):
    """The capture's out_dir: every published path is relative to it."""
    if manifest.get("out_dir"):
        return Path(manifest["out_dir"])
    return Path(manifest["shots"][0]).parent.parent


def pr_markdown(manifest, web_base, *, task_id, attempt):
    """The PR comment: the changed scenes FIRST, then everything else.

    Image URLs are `https://github.com/<repo>/blob/<evidence branch>/<path>?raw=true`.
    That is the form that renders in a PRIVATE repo: GitHub leaves github.com
    image URLs un-proxied (checked on the rendered body_html of a posted
    comment — no camo rewrite), so the viewer's browser fetches them with its
    own GitHub session and is redirected to a tokened raw URL. A
    raw.githubusercontent.com link would 404 for the same viewer (no token),
    and a camo-proxied external host cannot read a private repo at all. mp4
    never plays inline in a comment, so every video is an inline GIF plus a
    link to the mp4."""
    root = evidence_root(manifest)

    def url(local, raw=True):
        rel = Path(local).relative_to(root)
        return f"{web_base}/{rel.as_posix()}" + ("?raw=true" if raw else "")

    def img(local, alt, width=None):
        if width:
            return f'<img src="{url(local)}" alt="{alt}" width="{width}">'
        return f"![{alt}]({url(local)})"

    head = (manifest.get("head") or "")[:10]
    base = ((manifest.get("baseline") or {}).get("sha") or "")[:10]
    lines = [f"### 🎥 Visual evidence — `{task_id}` attempt {attempt}"
             + (f" at `{head}`" if head else ""), ""]
    scenes = manifest.get("scenes") or []
    if scenes:
        # The change itself, before anything else: one row per scene with the
        # share of pixels changed, then each scene's most-changed panel.
        lines += [f"#### Changed scenes — before \\| after \\| difference"
                  + (f" vs branch point `{base}`" if base else ""), "",
                  "Each scene is rendered on its own, framed on its content, with "
                  "the same cameras before and after.", "",
                  "| scene | why | changed |", "|---|---|---|"]
        for sc in scenes:
            if sc.get("error"):
                state = "⚠️ did not render"
            elif sc.get("compare"):
                state = f"**{_pct(sc.get('max_changed'))}**"
                if (sc.get("max_changed") or 0) < config.EVIDENCE_MIN_CHANGE:
                    state += " 🚩"
            elif scene_shots([sc]):
                state = "new scene"
            else:
                state = "no image"
            lines.append(f"| `{sc.get('path')}` | {sc.get('status')}: "
                         f"{(sc.get('why') or '').replace('|', '/')} | {state} |")
        lines.append("")
        for sc in scenes:
            if not scene_shots([sc]):
                if sc.get("error"):
                    lines += [f"**`{sc.get('path')}`** did not render: "
                              f"`{sc['error'][:200]}`", ""]
                continue
            rows = sorted([c for c in sc.get("compare") or [] if c.get("side_by_side")],
                          key=lambda c: -(c.get("changed") or 0.0))
            if rows:
                top = rows[0]
                lines += [f"**`{sc.get('path')}`** — {_pct(sc.get('max_changed'))} "
                          f"changed (view `{top['name']}`)", "",
                          img(top["side_by_side"], f"{_scene_label(sc)} {top['name']}"),
                          ""]
                if len(rows) > 1:
                    lines += [f"<details><summary>{len(rows) - 1} more view(s) of "
                              f"{_scene_label(sc)}</summary>", ""]
                    for c in rows[1:]:
                        lines += [f"`{c['name']}` — {_pct(c.get('changed'))}", "",
                                  img(c["side_by_side"], f"{_scene_label(sc)} {c['name']}"),
                                  ""]
                    lines += ["</details>", ""]
            else:
                lines += [f"**`{sc.get('path')}`** — new scene (nothing to compare "
                          "with at the branch point)", "",
                          " ".join(img(s, f"{_scene_label(sc)} {Path(s).stem}", 400)
                                   for s in sc.get("shots") or []), ""]
            v = sc.get("video")
            if v and Path(v.get("gif") or "").exists():
                lines += [f"Orbit of `{_scene_label(sc)}` "
                          f"([full video, mp4]({url(v['mp4'])}))", "",
                          img(v["gif"], f"{_scene_label(sc)} orbit"), ""]
        if manifest.get("scenes_skipped"):
            lines += ["Also changed, not rendered (ARC_EVIDENCE_MAX_SCENES): "
                      + ", ".join(f"`{p}`" for p in manifest["scenes_skipped"]), ""]
    if manifest.get("no_visible_change"):
        lines += ["**🚩 No visible change** — this diff touches gameplay files ("
                  + ", ".join(f"`{p}`" for p in (manifest.get("gameplay_diff") or [])[:5])
                  + ") but no rendered view moved. Either the change is invisible or "
                  "the capture missed it; the coverage table below says which kinds "
                  "ran.", ""]
    sheet = manifest.get("contact_sheet")
    if sheet and Path(sheet).exists():
        # One labeled grid of everything captured: what tells a reviewer at a
        # glance what they are looking at, before the videos and panels.
        lines += [f"**Every view** ([full size]({url(sheet, raw=True)}))", "",
                  img(sheet, "contact sheet"), ""]
    vids = manifest.get("videos") or {}
    for kind in ("playtest", "flythrough"):
        v = vids.get(kind)
        if v:
            lines += [f"**{kind.capitalize()}** ([full video]({url(v['mp4'], raw=True)}))",
                      "", img(v["gif"], kind), ""]
    comp = [c for c in manifest.get("compare") or [] if c.get("side_by_side")]
    if comp:
        title = (f"**Main scene, fixed cameras — before \\| after \\| difference** "
                 f"(vs branch point `{base}`)")
        table = ["| camera | changed | before · after · diff |", "|---|---|---|"]
        table += [f"| {c['name']} | {_pct(c.get('changed'))} | "
                  f"{img(c['side_by_side'], c['name'])} |" for c in comp]
        if scenes:
            # Secondary when scenes changed: the level around the change.
            lines += ["<details><summary>Main scene, fixed cameras "
                      f"({', '.join(c['name'] + ' ' + _pct(c.get('changed')) for c in comp)})"
                      "</summary>", "", title, ""] + table + ["", "</details>", ""]
        else:
            lines += [title, ""] + table + [""]
    elif manifest.get("shots"):
        lines += ["**Screenshots**", ""]
        lines += [img(s, Path(s).stem) for s in manifest.get("shots") or []]
        lines.append("")
    pts = manifest.get("playtest_shots") or []
    if pts:
        lines += ["**Playtest screenshots**", ""]
        lines += [img(p, Path(p).stem) for p in pts[:6]]
        lines.append("")
    table = _coverage_table(manifest)
    if table:
        lines += ["**Evidence coverage**", ""] + table + [""]
    if manifest.get("godot_errors"):
        lines += ["**Godot errors** (from the render runs — Godot exits 0 with these "
                  "on stdout)", ""] + [f"- `{e}`" for e in manifest["godot_errors"]] + [""]
    if manifest.get("warnings"):
        lines += ["**Warnings**", ""] + [f"- ⚠️ {w}" for w in manifest["warnings"]] + [""]
    lines.append(f"<sub>Captured by arc-orchestrator (AGENTS.md Rule 7d) in "
                 f"{manifest.get('seconds', '?')}s. Images load with your GitHub "
                 "session (private repo); files are on the "
                 f"`{config.EVIDENCE_BRANCH}` branch.</sub>")
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
