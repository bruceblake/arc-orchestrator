"""The Godot 4 CLI, as the studio uses it.

Three facts about Godot drive everything in this module:

1. ``--headless`` HAS NO RENDERER. It runs the engine with a dummy video
   driver, which is exactly what you want for syntax checks, imports, exports
   and logic tests — and completely useless for the visual judge, which needs
   pixels. Renders therefore run WITH a display (``STUDIO_DISPLAY``): WSLg's
   :0 here, which has real GPU passthrough via /dev/dxg, or an Xvfb display
   for a deterministic software-rendered run.

2. There is no ``--screenshot`` flag. A render is a SCENE the project runs
   that positions a camera, waits for the frame to actually draw, and saves
   the viewport image itself. That harness scene has to live inside the game
   project, so this module writes it there (``ensure_render_harness``) and
   keeps it current — it is studio infrastructure, not game code, and no
   implementer should be asked to maintain it.

3. Godot exits 0 far more eagerly than you would like: a scene that fails to
   load, a script that errors at runtime, and a successful run can all end in
   a zero exit code with the error on stdout. So every helper here checks its
   PRODUCT — the file that should exist, the image that should have been
   written — and never trusts the exit code alone.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import config

# A render that has not converged is worse than no render: the judge would
# score a half-lit frame as a lighting defect. The harness waits this many
# frames after positioning each camera before it saves.
SETTLE_FRAMES = 6
RENDER_HARNESS = "studio_render.gd"


def godot_bin():
    """The Godot 4 binary, or None when it is not installed."""
    pinned = config.GODOT_BIN
    if pinned:
        return pinned if os.path.exists(pinned) or shutil.which(pinned) else None
    for name in ("godot", "godot4", "Godot", "godot-headless"):
        found = shutil.which(name)
        if found:
            return found
    return None


def version():
    """Godot's reported version string, or None if it cannot be run."""
    exe = godot_bin()
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                             timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return (out.stdout or out.stderr).strip().splitlines()[0] if (
        out.stdout or out.stderr) else None


def available():
    return godot_bin() is not None


class GodotError(RuntimeError):
    pass


def _run(args, *, project, timeout, display=None, env=None):
    """Run godot with `args` against `project`. Returns (rc, stdout+stderr)."""
    exe = godot_bin()
    if not exe:
        raise GodotError(
            "godot is not installed or not on PATH. Install it "
            "(`sudo pacman -S godot`) or set ARC_GODOT_BIN.")
    argv = [exe, "--path", str(project)] + list(args)
    e = dict(os.environ)
    if env:
        e.update(env)
    if display:
        e["DISPLAY"] = display
    else:
        # A headless run must NOT inherit a display: with one set, Godot may
        # still try to open a window and block on a compositor that is not
        # there. Headless means headless.
        e.pop("DISPLAY", None)
        e.pop("WAYLAND_DISPLAY", None)
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           env=e)
    except subprocess.TimeoutExpired:
        raise GodotError(f"godot timed out after {timeout}s: {' '.join(args)}")
    except OSError as exc:
        raise GodotError(f"could not run godot: {exc}")
    return p.returncode, ((p.stdout or "") + (p.stderr or ""))


# Godot reports script and scene failures on stdout while still exiting 0.
# These are the patterns that mean "this run did not do what it printed a 0
# for"; they are matched case-insensitively against the combined output.
_ERROR_PATTERNS = (
    r"SCRIPT ERROR",
    r"Parse Error",
    r"Failed loading resource",
    r"Cannot open file",
    r"ERROR: Cannot instantiate",
    r"Condition \"[^\"]*\" is true\. Returning",
)


def output_errors(text):
    """Godot error lines in `text` — the real failure signal, not the rc."""
    hits = []
    for line in (text or "").splitlines():
        if any(re.search(p, line, re.I) for p in _ERROR_PATTERNS):
            hits.append(line.strip())
    return hits


def import_assets(project, timeout=900):
    """Run Godot's importer so .godot/ is populated before anything else.

    A fresh checkout has no import cache, and a scene that references an
    un-imported asset fails to load. This is the first thing a gate should do
    on a clean worktree.
    """
    rc, out = _run(["--headless", "--import", "--quit"], project=project,
                   timeout=timeout)
    errs = output_errors(out)
    if rc != 0 or errs:
        raise GodotError("asset import failed:\n" + "\n".join(errs[:20] or [out[-2000:]]))
    return out


def script_check(project, script, timeout=120):
    """Parse-check one GDScript file. Raises GodotError with the parse error."""
    rc, out = _run(["--headless", "--check-only", "--script", str(script), "--quit"],
                   project=project, timeout=timeout)
    errs = output_errors(out)
    if rc != 0 or errs:
        raise GodotError(f"{script}: " + ("\n".join(errs[:20]) or out[-2000:]))
    return True


def check_all_scripts(project, timeout=600):
    """Parse-check every .gd file in the project. Returns the list checked."""
    project = Path(project)
    checked, failures = [], []
    for gd in sorted(project.rglob("*.gd")):
        if ".godot" in gd.parts or "addons" in gd.parts:
            continue
        rel = gd.relative_to(project)
        try:
            script_check(project, f"res://{rel.as_posix()}", timeout=timeout)
            checked.append(str(rel))
        except GodotError as exc:
            failures.append(str(exc))
    if failures:
        raise GodotError("\n".join(failures))
    return checked


def run_scene(project, scene, *, user_args=(), timeout=600, display=None,
              headless=True):
    """Run one scene to completion. The scene is responsible for quitting."""
    args = (["--headless"] if headless else []) + [str(scene)]
    if user_args:
        args += ["--"] + [str(a) for a in user_args]
    rc, out = _run(args, project=project, timeout=timeout,
                   display=None if headless else (display or config.STUDIO_DISPLAY))
    return rc, out


MEASURER = "tools/measure.gd"
METRICS_FILE = "studio_metrics.json"


def measure(project, timeout=600):
    """Run the project's raycast measurer; returns the metrics it wrote.

    The phase gate compares Bucket A against studio_metrics.json, and that
    file is gitignored in the game repo (it is evidence ABOUT a build, not
    part of it). So on the repo's main — where the gate looks — nothing ever
    wrote it, and the gate reported every dimension "not measured" forever.
    The gate command runs this first, so it judges what is actually merged.

    Returns None when the project has no measurer. Raises GodotError when the
    measurer exists but fails, because a broken measurer is a finding.
    """
    project = Path(project)
    if not (project / MEASURER).exists():
        return None
    import_assets(project, timeout=timeout)
    out_file = project / METRICS_FILE
    before = out_file.stat().st_mtime if out_file.exists() else 0
    rc, out = _run(["--headless", "--script", f"res://{MEASURER}"],
                   project=project, timeout=timeout)
    errs = output_errors(out)
    if rc != 0 or errs or "STUDIO_METRICS_OK" not in out:
        raise GodotError("the measurer failed:\n" + "\n".join(errs[:20] or [out[-2000:]]))
    if not out_file.exists() or out_file.stat().st_mtime <= before:
        raise GodotError(f"the measurer reported success but did not write {METRICS_FILE}")
    return json.loads(out_file.read_text(encoding="utf-8"))


def export_release(project, preset, out_path, timeout=1800):
    """Export a build. Checks the artifact exists rather than trusting rc."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rc, out = _run(["--headless", "--export-release", preset, str(out_path)],
                   project=project, timeout=timeout)
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise GodotError(
            f"export produced no artifact at {out_path} (rc={rc}).\n"
            + "\n".join(output_errors(out)[:20] or [out[-2000:]]))
    return out_path


# --- the render harness ------------------------------------------------------
# Written INTO the game project by ensure_render_harness. It reads a camera
# list as JSON, renders one PNG per camera, and writes a manifest. It is the
# only bridge between camera_system.py and actual pixels.
_HARNESS_SRC = '''extends SceneTree
# GENERATED BY studio/engine/godot.py — do not edit by hand; it is rewritten.
#
# Renders one PNG per camera from a JSON camera list and writes a manifest.
# Usage:
#   godot --path <project> studio_render.gd -- <cameras.json> <out_dir> [scene]
#
# It runs as a SceneTree script rather than a scene so it can be dropped into
# any project without touching that project's main scene.

const SETTLE_FRAMES := %(settle)d

func _initialize() -> void:
\tvar args := OS.get_cmdline_user_args()
\tif args.size() < 2:
\t\tpush_error("usage: studio_render.gd -- <cameras.json> <out_dir> [scene]")
\t\tquit(2)
\t\treturn
\tvar cameras_path: String = args[0]
\tvar out_dir: String = args[1]
\tvar scene_path: String = args[2] if args.size() > 2 else ""
\tvar f := FileAccess.open(cameras_path, FileAccess.READ)
\tif f == null:
\t\tpush_error("cannot read cameras: " + cameras_path)
\t\tquit(2)
\t\treturn
\tvar parsed: Variant = JSON.parse_string(f.get_as_text())
\tf.close()
\tif typeof(parsed) != TYPE_DICTIONARY or not parsed.has("cameras"):
\t\tpush_error("cameras json must be an object with a \\"cameras\\" array")
\t\tquit(2)
\t\treturn
\tDirAccess.make_dir_recursive_absolute(out_dir)
\tvar root := get_root()
\tif scene_path != "":
\t\tvar packed := load(scene_path)
\t\tif packed == null:
\t\t\tpush_error("cannot load scene: " + scene_path)
\t\t\tquit(2)
\t\t\treturn
\t\troot.add_child(packed.instantiate())
\tvar cam := Camera3D.new()
\troot.add_child(cam)
\t# _initialize runs BEFORE the tree starts: nothing is "inside the tree"
\t# yet, so look_at() fails and scenes have not run _ready() (the graybox
\t# level builds itself there). One frame lets both happen.
\tawait process_frame
\tcam.make_current()
\t_ensure_inspection_light(root)
\tvar manifest := []
\tfor entry in parsed["cameras"]:
\t\tvar pos: Array = entry.get("position", [0, 2, 5])
\t\tvar look: Array = entry.get("look_at", [0, 0, 0])
\t\tvar p := Vector3(pos[0], pos[1], pos[2])
\t\tvar target := Vector3(look[0], look[1], look[2])
\t\tif p.is_equal_approx(target):
\t\t\tcam.position = p
\t\telse:
\t\t\tcam.look_at_from_position(p, target, Vector3.UP)
\t\tcam.fov = float(entry.get("fov", 70.0))
\t\tfor _i in range(SETTLE_FRAMES):
\t\t\tawait process_frame
\t\tawait RenderingServer.frame_post_draw
\t\tvar img := root.get_texture().get_image()
\t\tvar name: String = str(entry.get("name", "camera"))
\t\tvar dest := out_dir.path_join(name + ".png")
\t\tvar err := img.save_png(dest)
\t\tif err != OK:
\t\t\tpush_error("could not save " + dest)
\t\t\tquit(3)
\t\t\treturn
\t\tmanifest.append({"name": name, "path": dest, "kind": entry.get("kind", "")})
\tvar mf := FileAccess.open(out_dir.path_join("manifest.json"), FileAccess.WRITE)
\tmf.store_string(JSON.stringify({"cameras": manifest}, "  "))
\tmf.close()
\tprint("STUDIO_RENDER_OK ", manifest.size())
\tquit(0)


# A graybox has no lights on purpose (lighting is phase 3), but a judge cannot
# score what it cannot see: an unlit render is solid black. So when a scene
# brings NO light and NO environment, the harness adds neutral inspection
# lighting (a sun and a procedural sky) and nothing else. A scene with its own
# lighting renders exactly as authored; phase 3 is judged on the game's
# lights, never on these.
func _ensure_inspection_light(root: Node) -> void:
\tif root.find_children("*", "Light3D", true, false).is_empty():
\t\tvar sun := DirectionalLight3D.new()
\t\tsun.name = "StudioInspectionSun"
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
\t\twe.name = "StudioInspectionEnvironment"
\t\twe.environment = env
\t\troot.add_child(we)
''' % {"settle": SETTLE_FRAMES}


def ensure_render_harness(project):
    """Write (or refresh) the render harness inside the game project."""
    dest = Path(project) / RENDER_HARNESS
    if not dest.exists() or dest.read_text(encoding="utf-8") != _HARNESS_SRC:
        dest.write_text(_HARNESS_SRC, encoding="utf-8")
    return dest


def render(project, cameras, out_dir, *, scene="", timeout=900, display=None,
           resolution="1600x900"):
    """Render one PNG per camera. Returns the list of image paths written.

    `cameras` is a list of dicts from studio.evaluation.camera_system. This
    needs a DISPLAY: see the module docstring for why --headless cannot do it.
    """
    project, out_dir = Path(project), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    disp = display or config.STUDIO_DISPLAY or os.environ.get("DISPLAY", "")
    if not disp:
        raise GodotError(
            "no display available for rendering. Godot's --headless mode has "
            "no renderer, so a render needs DISPLAY: WSLg provides :0 here, "
            "or start Xvfb and set ARC_STUDIO_DISPLAY.")
    ensure_render_harness(project)
    cam_file = out_dir / "cameras.json"
    cam_file.write_text(json.dumps({"cameras": list(cameras)}, indent=2),
                        encoding="utf-8")
    # `--script` is essential. A bare .gd path is taken as a SCENE to open:
    # Godot then runs the project's main scene in a window forever and the
    # harness never executes (the first live render hung for 400s+ exactly so).
    args = ["--rendering-driver", "opengl3", "--resolution", resolution,
            "--script", RENDER_HARNESS, "--", str(cam_file), str(out_dir)]
    if scene:
        args.append(scene)
    rc, out = _run(args, project=project, timeout=timeout, display=disp)
    manifest = out_dir / "manifest.json"
    if not manifest.exists():
        raise GodotError(
            f"render produced no manifest (rc={rc}). Godot output:\n"
            + "\n".join(output_errors(out)[:20] or [out[-2000:]]))
    shots = json.loads(manifest.read_text(encoding="utf-8"))["cameras"]
    missing = [s["name"] for s in shots if not Path(s["path"]).exists()]
    if missing:
        raise GodotError(f"render manifest lists images that do not exist: {missing}")
    return [Path(s["path"]) for s in shots]


def doctor():
    """What the Godot layer can and cannot do right now."""
    exe = godot_bin()
    disp = config.STUDIO_DISPLAY or os.environ.get("DISPLAY", "")
    return {
        "godot_bin": exe or "",
        "godot_version": version() or "",
        "can_check_scripts": bool(exe),
        "can_export": bool(exe),
        "display": disp,
        "can_render": bool(exe and disp),
        "why_not_render": "" if (exe and disp) else (
            "godot not installed" if not exe else
            "no DISPLAY (--headless cannot render; use WSLg :0 or Xvfb)"),
    }
