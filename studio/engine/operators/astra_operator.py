"""Module A: the 3D / rigging / animation operator, and its computer use.

GPT-6-Astra is the only worker in the studio that acts on a DESKTOP rather
than on a repository. Modelling, retopology, weight transfer and animation
are done through Blender; the parts of an engine editor that have no CLI are
done by looking at a screen and clicking it. Neither fits the
prompt-in/diff-out shape of a coding harness, so this module gives Astra a
tool loop instead.

Four tools, and a deliberate order of preference between them:

    execute_blender_script   SCRIPTED. Reproducible, diffable, reviewable.
    verify_mesh_metrics      MEASURED. The gate's evidence for phase 2.
    capture_screen           OBSERVED. Only to see what a script cannot.
    mouse_click/keyboard     MANUAL. The last resort.

That order is the whole design opinion of this module. A clicked action is
invisible to review, impossible to replay and unattributable when it breaks —
so the system prompt tells Astra to reach for a script first and to justify
every click. Computer use is here because some editor operations genuinely
have no other path, not because it is a nice way to work.

SAFETY NOTE, in the spirit of AGENTS.md Rule 6b: `execute_blender_script` runs
model-authored Python with the operator's own privileges, exactly as
`code_tasks.gate` runs a model-authored `verify_cmd` shell string. That is an
accepted property of this repo on a trusted machine, not an oversight. Do not
expose this module over a network interface.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import config
import events
from studio import openrouter

BLENDER_TIMEOUT = 900          # a modelling script that runs longer is stuck
SCREENSHOT_TIMEOUT = 60
INPUT_TIMEOUT = 20
PROBE = Path(__file__).with_name("_mesh_probe.py")
RENDER_PROBE = Path(__file__).with_name("_render_probe.py")
RENDER_MARK_OPEN = "<<<STUDIO_RENDER_JSON"
RENDER_MARK_CLOSE = "STUDIO_RENDER_JSON>>>"
MARK_OPEN = "<<<STUDIO_MESH_JSON"
MARK_CLOSE = "STUDIO_MESH_JSON>>>"

OPERATOR_MODEL = "GPT-6-Astra"


class OperatorError(RuntimeError):
    pass


# --- the environment ---------------------------------------------------------
def blender_bin():
    pinned = config.BLENDER_BIN
    if pinned:
        return pinned if (os.path.exists(pinned) or shutil.which(pinned)) else None
    return shutil.which("blender")


def display():
    return config.STUDIO_DISPLAY or os.environ.get("DISPLAY", "")


def _tool(name):
    return shutil.which(name)


def doctor():
    """What the operator can actually do on this machine right now."""
    b, d = blender_bin(), display()
    grab = _tool("ffmpeg") or _tool("import") or _tool("scrot")
    xdo = _tool("xdotool")
    return {
        "blender": b or "",
        "blender_version": _blender_version() if b else "",
        "display": d,
        "screen_capture_via": (Path(grab).name if grab else ""),
        "xdotool": xdo or "",
        "can_model": bool(b),
        "can_verify_meshes": bool(b),
        "can_see_screen": bool(d and grab),
        "can_click": bool(d and xdo),
        "missing": [n for n, ok in (
            ("blender", bool(b)), ("a display", bool(d)),
            ("ffmpeg (or imagemagick/scrot)", bool(grab)),
            ("xdotool", bool(xdo))) if not ok],
    }


def _blender_version():
    try:
        out = subprocess.run([blender_bin(), "--version"], capture_output=True,
                             text=True, timeout=60, env=config.child_env())
        return (out.stdout or "").strip().splitlines()[0] if out.stdout else ""
    except (OSError, subprocess.SubprocessError):
        return ""


# --- tool 1: scripted Blender ------------------------------------------------
def execute_blender_script(script, *, cwd=None, timeout=BLENDER_TIMEOUT,
                           blend_file=""):
    """Run a Python script inside headless Blender. Returns a result dict."""
    exe = blender_bin()
    if not exe:
        raise OperatorError(
            "blender is not installed (install it with `sudo pacman -S "
            "blender`, or set ARC_BLENDER_BIN). Modelling, rigging and mesh "
            "verification all go through it.")
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(script)
        script_path = fh.name
    argv = [exe, "-b"]
    if blend_file:
        argv.append(blend_file)
    argv += ["-P", script_path]
    started = time.time()
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           cwd=cwd or None, env=config.child_env())
        rc, out, err = p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        rc, out, err = 124, "", f"blender timed out after {timeout}s"
    except OSError as exc:
        rc, out, err = 127, "", str(exc)
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass
    # Blender exits 0 on a script exception unless -noaudio/--python-exit-code
    # is used, so the traceback in stderr is the real signal.
    failed = rc != 0 or "Traceback (most recent call last)" in err
    result = {"ok": not failed, "returncode": rc,
              "stdout": out[-6000:], "stderr": err[-4000:],
              "seconds": round(time.time() - started, 1)}
    events.emit("studio.blender", ok=result["ok"], rc=rc,
                seconds=result["seconds"])
    return result


# --- tool 2: measured meshes -------------------------------------------------
def verify_mesh_metrics(model_path, *, max_triangle_count=0, project=""):
    """Inspect an asset: triangles, manifoldness, UVs, materials, skeleton.

    Writes the report into the project's studio directory as `mesh-<name>.json`
    so `stage_manager.gate_phase_2` can find it. A measurement that is not
    recorded cannot gate anything.
    """
    exe = blender_bin()
    if not exe:
        raise OperatorError("blender is not installed; cannot verify meshes")
    model_path = str(Path(model_path).resolve())
    argv = [exe, "-b", "--python-exit-code", "1", "-P", str(PROBE), "--",
            model_path, str(int(max_triangle_count or 0))]
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=BLENDER_TIMEOUT, env=config.child_env())
    except subprocess.TimeoutExpired:
        raise OperatorError(f"mesh probe timed out on {model_path}")
    except OSError as exc:
        raise OperatorError(f"could not run blender: {exc}")
    text = (p.stdout or "") + (p.stderr or "")
    if MARK_OPEN not in text or MARK_CLOSE not in text:
        raise OperatorError(
            f"the mesh probe produced no report for {model_path} "
            f"(rc={p.returncode}).\n{text[-2000:]}")
    body = text.split(MARK_OPEN, 1)[1].split(MARK_CLOSE, 1)[0]
    try:
        report = json.loads(body)
    except ValueError as exc:
        raise OperatorError(f"the mesh probe emitted invalid JSON: {exc}")
    if report.get("error"):
        raise OperatorError(f"{model_path}: {report['error']}")
    report["ts"] = time.time()
    if project:
        d = config.studio_run_dir(project, create=True)
        name = Path(model_path).stem
        (d / f"mesh-{name}.json").write_text(json.dumps(report, indent=2),
                                             encoding="utf-8")
    events.emit("studio.mesh_verified", model=model_path,
                triangles=report.get("triangles"),
                budget=max_triangle_count,
                over=bool(report.get("over_budget_by")),
                non_manifold=report.get("non_manifold_edges"))
    return report


# --- tool 2b: seeing what was BUILT -------------------------------------------
# Distinct from capture_screen, and the more important of the two. A
# screenshot shows the desktop; a render shows the MODEL — the geometry, its
# proportions, its silhouette, its materials under light — from a camera
# chosen to reveal them.
#
# This is the step published accounts of Astra's Blender workflow put at the
# centre of it: write bpy, render, inspect, revise "wherever the render
# diverges from the brief". An operator without it writes geometry blind and
# can only be as good as its first guess.
def render_preview(blend_file, out_path=None, *, camera="", resolution="1280x720",
                   engine="BLENDER_EEVEE_NEXT", project=""):
    """Render a .blend to PNG. Returns {"render": path, ...report}."""
    exe = blender_bin()
    if not exe:
        raise OperatorError("blender is not installed; cannot render a preview")
    blend = Path(blend_file)
    if not blend.exists():
        raise OperatorError(f"no such .blend: {blend}")
    out = Path(out_path) if out_path else (
        (config.studio_run_dir(project, create=True) / "previews"
         if project else Path(tempfile.gettempdir()))
        / f"{blend.stem}-{int(time.time() * 1000)}.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    argv = [exe, "-b", str(blend), "--python-exit-code", "1",
            "-P", str(RENDER_PROBE), "--",
            str(out), camera or "-", resolution, engine]
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=BLENDER_TIMEOUT, env=config.child_env())
    except subprocess.TimeoutExpired:
        raise OperatorError(f"render timed out after {BLENDER_TIMEOUT}s")
    except OSError as exc:
        raise OperatorError(f"could not run blender: {exc}")
    text = (p.stdout or "") + (p.stderr or "")
    report = {}
    if RENDER_MARK_OPEN in text and RENDER_MARK_CLOSE in text:
        try:
            report = json.loads(text.split(RENDER_MARK_OPEN, 1)[1]
                                .split(RENDER_MARK_CLOSE, 1)[0])
        except ValueError:
            report = {}
    if report.get("error"):
        raise OperatorError(f"render failed: {report['error']}")
    # Check the PRODUCT, not the exit code: Blender can exit 0 having written
    # nothing, and a missing image handed to a model as "here is your render"
    # would be the worst kind of feedback — confident and empty.
    if not out.exists() or out.stat().st_size == 0:
        raise OperatorError(
            f"blender reported success but wrote no image (rc={p.returncode})."
            f"\n{text[-1500:]}")
    events.emit("studio.render_preview", blend=str(blend), out=str(out),
                engine=report.get("engine", ""), camera=report.get("camera", ""))
    return {"render": str(out), **report}


# --- tool 3: seeing the screen -----------------------------------------------
def capture_screen(out_path=None):
    """Screenshot the virtual display. Returns the PNG path."""
    disp = display()
    if not disp:
        raise OperatorError(
            "no DISPLAY to capture. WSLg provides :0 on this machine; "
            "otherwise start Xvfb and set ARC_STUDIO_DISPLAY.")
    out_path = Path(out_path or (Path(tempfile.gettempdir())
                                 / f"astra-screen-{int(time.time() * 1000)}.png"))
    env = config.child_env(DISPLAY=disp)
    if _tool("ffmpeg"):
        argv = ["ffmpeg", "-y", "-loglevel", "error", "-f", "x11grab",
                "-i", disp, "-frames:v", "1", str(out_path)]
    elif _tool("import"):
        argv = ["import", "-window", "root", str(out_path)]
    elif _tool("scrot"):
        argv = ["scrot", str(out_path)]
    else:
        raise OperatorError(
            "no screen-capture tool found (install ffmpeg, imagemagick or scrot)")
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=SCREENSHOT_TIMEOUT, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise OperatorError(f"screen capture failed: {exc}")
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise OperatorError(
            f"screen capture produced no image (rc={p.returncode}): "
            f"{(p.stderr or '')[-500:]}")
    return out_path


# --- tool 4: driving the screen ----------------------------------------------
def _xdotool(args):
    disp = display()
    if not disp:
        raise OperatorError("no DISPLAY; cannot send input")
    if not _tool("xdotool"):
        raise OperatorError(
            "xdotool is not installed (`sudo pacman -S xdotool`); computer-use "
            "clicks and keystrokes need it")
    try:
        p = subprocess.run(["xdotool"] + list(args), capture_output=True,
                           text=True, timeout=INPUT_TIMEOUT,
                           env=config.child_env(DISPLAY=disp))
    except (OSError, subprocess.SubprocessError) as exc:
        raise OperatorError(f"xdotool failed: {exc}")
    if p.returncode != 0:
        raise OperatorError(f"xdotool {' '.join(args)}: {(p.stderr or '').strip()}")
    return (p.stdout or "").strip()


_BUTTONS = {"left": "1", "middle": "2", "right": "3"}


def mouse_click(x, y, button="left"):
    if button not in _BUTTONS:
        raise OperatorError(f"unknown button {button!r}; use {sorted(_BUTTONS)}")
    _xdotool(["mousemove", "--sync", str(int(x)), str(int(y))])
    _xdotool(["click", _BUTTONS[button]])
    events.emit("studio.click", x=int(x), y=int(y), button=button)
    return {"ok": True, "x": int(x), "y": int(y), "button": button}


def keyboard_input(keys):
    """Send keystrokes. A list of key names ("ctrl+s") or literal text."""
    if isinstance(keys, str):
        keys = [keys]
    sent = []
    for k in keys:
        # A chord is a key sequence; anything else is typed as text.
        if len(k) > 1 and ("+" in k or k.lower() in _KEYNAMES):
            _xdotool(["key", "--clearmodifiers", k])
        else:
            _xdotool(["type", "--clearmodifiers", "--delay", "12", k])
        sent.append(k)
    events.emit("studio.keys", keys=sent[:20])
    return {"ok": True, "sent": sent}


_KEYNAMES = {"return", "enter", "tab", "escape", "esc", "space", "backspace",
             "delete", "up", "down", "left", "right", "home", "end", "page_up",
             "page_down", "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8",
             "f9", "f10", "f11", "f12"}


# --- the tool loop -----------------------------------------------------------
TOOLS = [
    {"type": "function", "function": {
        "name": "execute_blender_script",
        "description": (
            "Run a Python script inside headless Blender (bpy is available). "
            "PREFER THIS over clicking: a script is reproducible, reviewable "
            "and replayable. Use it to generate and retopologise meshes, bind "
            "armatures, transfer weights, author or retime actions, and "
            "export GLTF/GLB/FBX."),
        "parameters": {"type": "object", "properties": {
            "script": {"type": "string", "description": "Python source to run."},
            "blend_file": {"type": "string",
                           "description": "Optional .blend to open first."},
        }, "required": ["script"]}}},
    {"type": "function", "function": {
        "name": "verify_mesh_metrics",
        "description": (
            "Measure an exported asset: triangle count, non-manifold edges, "
            "loose vertices, n-gons, UV coverage, materials, and the bone "
            "hierarchy. Run this on every asset you export — the phase-2 gate "
            "reads these reports, and an unmeasured asset cannot pass it."),
        "parameters": {"type": "object", "properties": {
            "model_path": {"type": "string"},
            "max_triangle_count": {"type": "integer",
                                   "description": "The task's triangle budget, or 0."},
        }, "required": ["model_path"]}}},
    {"type": "function", "function": {
        "name": "render_preview",
        "description": (
            "Render a .blend file to an image and SHOW IT TO YOU. Use this "
            "after every meaningful modelling change: build, render, compare "
            "the render to the brief and the reference, then revise. This is "
            "how you see proportion, silhouette, gaps, intersections and "
            "materials — do not assume a script produced what you intended. "
            "With no camera given, a three-quarter camera framing every mesh "
            "is created for you."),
        "parameters": {"type": "object", "properties": {
            "blend_file": {"type": "string"},
            "camera": {"type": "string",
                       "description": "Name of a camera in the scene, or omit."},
            "resolution": {"type": "string", "description": "e.g. 1280x720"},
        }, "required": ["blend_file"]}}},
    {"type": "function", "function": {
        "name": "capture_screen",
        "description": (
            "Screenshot the virtual display and show it to you. Use it to see "
            "the state of a GUI application you are driving."),
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "mouse_click",
        "description": (
            "Click at absolute screen coordinates. LAST RESORT: only for "
            "editor operations with no scriptable equivalent. Say in your "
            "message why a script could not do it."),
        "parameters": {"type": "object", "properties": {
            "x": {"type": "integer"}, "y": {"type": "integer"},
            "button": {"type": "string", "enum": ["left", "middle", "right"]},
        }, "required": ["x", "y"]}}},
    {"type": "function", "function": {
        "name": "keyboard_input",
        "description": (
            "Send keystrokes to the focused window. Chords like 'ctrl+s' are "
            "sent as keys; anything else is typed as literal text."),
        "parameters": {"type": "object", "properties": {
            "keys": {"type": "array", "items": {"type": "string"}},
        }, "required": ["keys"]}}},
]

SYSTEM_PROMPT = """You are the 3D, rigging and animation operator for a \
multiplayer prison-escape game built in Godot 4. You work through Blender and, \
when nothing else will do, through a virtual desktop.

How you work — this loop is not optional:
1. BUILD with a Blender script, saving the .blend. Scripts are reviewable and \
replayable; clicks are neither.
2. RENDER it with render_preview and LOOK at the image. Your script is a \
guess about geometry until you have seen the result.
3. COMPARE the render to the brief and any reference: proportion, \
silhouette, gaps, intersections, floating parts, scale against a 1.8m human.
4. REVISE ONE THING, then render again. Changing several things at once \
means you can no longer tell which change helped.
5. MEASURE the finished asset with verify_mesh_metrics before you export or \
report done. A triangle budget you did not measure is not a budget, and the \
phase gate reads your reports.

Use capture_screen, mouse_click and keyboard_input only when an operation has \
no scriptable path — and say in your message why.

Know where you are strong. Regular, hard-surface, architectural forms — \
walls, bars, bunks, vents, towers, furniture, doors — are where this method \
excels, because they have a checkable source. Organic forms and deforming \
characters are markedly weaker: if a task asks you to model a character from \
nothing, say so and propose adapting a supplied base mesh instead.

Hard constraints:
- Respect the triangle budget in the task. Report the real number even when \
it is over; do not quietly decimate to hit it without saying so.
- Geometry must be manifold and free of loose vertices unless the task says \
otherwise.
- Exports go to the path the task names, in the format it names.
- A synchronised animation pair is ONE authored interaction: the two clips' \
contact frames must line up at the same clip time. If you cannot make them \
line up, say so rather than exporting a pair that only looks right in one clip.
- You are not the git actor. Write files where the task tells you; never run \
git."""


def _dispatch(name, args, *, project):
    if name == "execute_blender_script":
        return execute_blender_script(args.get("script", ""),
                                      blend_file=args.get("blend_file", ""))
    if name == "verify_mesh_metrics":
        return verify_mesh_metrics(
            args.get("model_path", ""),
            max_triangle_count=int(args.get("max_triangle_count") or 0),
            project=project)
    if name == "render_preview":
        return render_preview(args.get("blend_file", ""),
                              camera=args.get("camera", ""),
                              resolution=args.get("resolution", "1280x720"),
                              project=project)
    if name == "capture_screen":
        return {"screenshot": str(capture_screen())}
    if name == "mouse_click":
        return mouse_click(args.get("x", 0), args.get("y", 0),
                           args.get("button", "left"))
    if name == "keyboard_input":
        return keyboard_input(args.get("keys", []))
    raise OperatorError(f"unknown tool {name!r}")


def run(goal, *, project="", max_steps=24, model=OPERATOR_MODEL, extra=""):
    """Drive Astra through a 3D task. Returns a transcript summary.

    Every step is emitted as an event and every tool result is kept, so a run
    can be reconstructed afterwards — Rule 7 applies here exactly as it does
    to a harness transcript.
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": goal + (f"\n\n{extra}" if extra else "")}]
    steps, screenshots = [], []
    for step in range(1, int(max_steps) + 1):
        msg, usage = openrouter.chat(model, messages, tools=TOOLS,
                                     temperature=0.15, max_tokens=8000,
                                     task=f"astra:{project or 'adhoc'}:s{step}")
        calls = getattr(msg, "tool_calls", None) or []
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [{"id": c.id, "type": "function",
                            "function": {"name": c.function.name,
                                         "arguments": c.function.arguments}}
                           for c in calls] or None,
        })
        if not calls:
            steps.append({"step": step, "text": msg.content or "", "tools": []})
            events.emit("studio.astra_done", project=str(project), steps=step)
            return {"ok": True, "steps": steps, "final": msg.content or "",
                    "screenshots": screenshots}
        used = []
        for call in calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except ValueError:
                args = {}
            try:
                result = _dispatch(name, args, project=project)
                ok = True
            except (OperatorError, OSError, ValueError) as exc:
                result, ok = {"error": str(exc)}, False
            used.append({"tool": name, "ok": ok})
            events.emit("studio.astra_tool", project=str(project), step=step,
                        tool=name, ok=ok)
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": json.dumps(result, default=str)[:12000]})
            # A render or a screenshot is only useful as an IMAGE; a tool
            # result is text, so the picture goes back as its own user turn.
            for key, label in (("render", "Your render of the scene. Compare it "
                                "to the brief before changing anything:"),
                               ("screenshot", "Screenshot of the virtual display:")):
                img = result.get(key) if isinstance(result, dict) else None
                if img and Path(img).exists():
                    screenshots.append(img)
                    messages.append({"role": "user", "content": [
                        openrouter.text_part(label), openrouter.image_part(img)]})
        steps.append({"step": step, "text": msg.content or "", "tools": used})
    events.emit("studio.astra_exhausted", project=str(project), steps=max_steps)
    return {"ok": False, "steps": steps,
            "final": f"stopped after {max_steps} steps without a final answer",
            "screenshots": screenshots}


# --- shell entrypoint --------------------------------------------------------
# The same four capabilities, exposed as a COMMAND rather than as API tool
# definitions. This is what makes the subscription profile work: Codex, Claude
# Code and the Gemini CLI all have shell access, so they reach Blender, the
# mesh probe, the screen and the keyboard through this entrypoint instead of
# through a billed tool loop. `run()` above stays the API-profile path.
#
# `verify` is also designed to be a task's verify_cmd directly: it exits
# non-zero when an asset breaks its triangle budget or is non-manifold, so the
# gate's exit code depends on the asset actually being correct (Rule 4).
def _cli(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        prog="python -m studio.engine.operators.astra_operator",
        description="Blender, mesh measurement and computer use, as shell commands.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("verify", help="measure an asset; non-zero if it fails")
    v.add_argument("model_path")
    v.add_argument("--max-tris", type=int, default=0, dest="max_tris")
    v.add_argument("--project", default="")
    v.add_argument("--allow-non-manifold", action="store_true")

    b = sub.add_parser("blender", help="run a Python script in headless Blender")
    b.add_argument("script", help="path to a .py file, or - for stdin")
    b.add_argument("--blend", default="")

    r = sub.add_parser("render", help="render a .blend to PNG so you can look at it")
    r.add_argument("blend_file")
    r.add_argument("--out", default="")
    r.add_argument("--camera", default="")
    r.add_argument("--resolution", default="1280x720")
    r.add_argument("--project", default="")

    s = sub.add_parser("shot", help="screenshot the virtual display")
    s.add_argument("--out", default="")

    c = sub.add_parser("click", help="click at absolute screen coordinates")
    c.add_argument("x", type=int)
    c.add_argument("y", type=int)
    c.add_argument("--button", default="left", choices=sorted(_BUTTONS))

    k = sub.add_parser("keys", help="send keystrokes ('ctrl+s' or literal text)")
    k.add_argument("keys", nargs="+")

    sub.add_parser("doctor", help="what the operator can do on this machine")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "doctor":
            print(json.dumps(doctor(), indent=2))
            return 0 if doctor()["can_model"] else 1

        if args.cmd == "verify":
            report = verify_mesh_metrics(args.model_path,
                                         max_triangle_count=args.max_tris,
                                         project=args.project)
            print(json.dumps(report, indent=2))
            problems = []
            if report.get("over_budget_by"):
                problems.append(
                    f"{report['triangles']} triangles exceeds the budget of "
                    f"{args.max_tris} by {report['over_budget_by']}")
            if report.get("non_manifold_edges") and not args.allow_non_manifold:
                problems.append(
                    f"{report['non_manifold_edges']} non-manifold edges")
            for p in problems:
                print(f"FAIL: {p}", file=sys.stderr)
            return 1 if problems else 0

        if args.cmd == "blender":
            src = (sys.stdin.read() if args.script == "-"
                   else Path(args.script).read_text(encoding="utf-8"))
            result = execute_blender_script(src, blend_file=args.blend)
            print(result["stdout"])
            if result["stderr"]:
                print(result["stderr"], file=sys.stderr)
            return 0 if result["ok"] else 1

        if args.cmd == "render":
            result = render_preview(args.blend_file, args.out or None,
                                    camera=args.camera,
                                    resolution=args.resolution,
                                    project=args.project)
            # The path on its own line, first, so a CLI agent can hand it
            # straight to its image viewer (codex -i, @path, Read).
            print(result["render"])
            print(json.dumps(result, indent=2))
            return 0

        if args.cmd == "shot":
            print(capture_screen(args.out or None))
            return 0

        if args.cmd == "click":
            print(json.dumps(mouse_click(args.x, args.y, args.button)))
            return 0

        if args.cmd == "keys":
            print(json.dumps(keyboard_input(args.keys)))
            return 0
    except OperatorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_cli())
