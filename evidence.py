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


def diff_stats(before, after, step=4, threshold=24):
    """(changed_share, mean_abs_delta) between two same-size screenshots."""
    wb, hb, pb = _pixels(before, step)
    wa, ha, pa = _pixels(after, step)
    if (wb, hb) != (wa, ha):
        return None, None
    changed, total = 0, 0
    for (r1, g1, b1), (r2, g2, b2) in zip(pb, pa):
        d = max(abs(r1 - r2), abs(g1 - g2), abs(b1 - b2))
        total += d
        changed += d > threshold
    n = len(pa) or 1
    return changed / n, total / n / 255.0


def compare(baseline_dir, shots, out_dir):
    """Per camera: before | after | amplified difference, and the numbers."""
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for shot in shots:
        before = Path(baseline_dir) / shot.name
        if not before.exists():
            rows.append({"name": shot.stem, "new": True})
            continue
        changed, delta = diff_stats(before, shot)
        side = out_dir / f"{shot.stem}.png"
        try:
            # rgb24 first: the renders carry alpha, and the difference of
            # two opaque alphas is 0 — a fully transparent diff panel.
            _ffmpeg(["-i", str(before), "-i", str(shot), "-filter_complex",
                     "[0]format=rgb24,split[a0][a1];[1]format=rgb24,split[b0][b1];"
                     "[a0][b0]blend=all_mode=difference,lutrgb=r='min(val*4,255)':"
                     "g='min(val*4,255)':b='min(val*4,255)'[d];"
                     "[a1][b1][d]hstack=inputs=3,scale=1440:-1", str(side)])
        except EvidenceError:
            side = None
        rows.append({"name": shot.stem, "changed": changed, "delta": delta,
                     "side_by_side": str(side) if side else None})
    return rows


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
                manifest["compare"] = compare(bdir, shots, out_dir / "compare")
    manifest["seconds"] = round(time.time() - started, 1)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2),
                                           encoding="utf-8")
    return manifest


# --- presenting -------------------------------------------------------------

def _pct(x):
    return "—" if x is None else f"{x:.1%}"


def review_images(manifest, limit=8):
    """Images to attach for a reviewer, most informative first."""
    out = [c["side_by_side"] for c in manifest.get("compare") or []
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
