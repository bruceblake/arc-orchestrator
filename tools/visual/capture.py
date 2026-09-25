#!/usr/bin/env python3
"""Screenshot every dashboard page, headless, over a fixed fixture world.

    ./py tools/visual/capture.py --out <dir> [--tree <checkout>] [--only index-desktop-dark,...]

For each view in VIEWS (page x viewport x colour scheme) this writes
`<out>/<page>-<viewport>-<scheme>.png` plus `<out>/capture.json` (the shots,
and every JavaScript error the pages threw — a page that throws is a
regression even when it looks fine).

The dashboard is served from `--tree` (default: this checkout) by
tools/visual/serve.py on a free port, over fixture data with a pinned clock,
so two captures of the same code produce the same pixels. The browser clock is
pinned to the same instant, CSS animations and the caret are frozen, and the
viewport, device scale, locale and time zone are fixed.

Exit codes: 0 captured, 3 the machine cannot capture (no Playwright or no
browser — an infrastructure gap, never a test failure), 4 a page threw a
JavaScript error (only with --strict), 1 anything else.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent

VIEWPORTS = {"desktop": (1440, 900), "phone": (390, 844)}
SCHEMES = ("light", "dark")
# (name, path, tab to click first or None)
PAGES = (
    ("index", "/", None),
    ("projects", "/", "projects"),
    ("usage", "/usage.html", None),
    ("phone", "/phone.html", None),
)
MAX_HEIGHT = 2400          # a full-page shot is clipped here: a reviewer reads
SETTLE_MS = 3200           # index.html staggers its first polls up to 2.4 s


def views():
    """Every (view name, path, tab, viewport, scheme), in a fixed order."""
    out = []
    for name, path, tab in PAGES:
        for vp in VIEWPORTS:
            for scheme in SCHEMES:
                out.append((f"{name}-{vp}-{scheme}", path, tab, vp, scheme))
    return out


class Unavailable(RuntimeError):
    """This machine cannot take screenshots (no Playwright / no browser)."""


def _browser_env():
    # Same host workaround as smoke.py: chromium's NSS libraries are unpacked
    # under ~/.local/opt/nsslibs on the fleet box (docs/visual-testing.md).
    lib = Path.home() / ".local" / "opt" / "nsslibs" / "usr" / "lib"
    if lib.is_dir():
        cur = os.environ.get("LD_LIBRARY_PATH", "")
        if str(lib) not in cur.split(os.pathsep):
            os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(p for p in (str(lib), cur) if p)


def start_server(tree, fixture_dir, python=None, timeout=45):
    """(process, port) for tools/visual/serve.py serving `tree`."""
    # stderr to a file, not a pipe: nobody drains a pipe after startup, and a
    # chatty server would block on a full one mid-capture.
    log = open(Path(fixture_dir) / "server.log", "w+", encoding="utf-8")
    proc = subprocess.Popen(
        [python or sys.executable, str(HERE / "serve.py"), "--tree", str(tree),
         "--fixture", str(fixture_dir), "--port", "0"],
        stdout=subprocess.PIPE, stderr=log, text=True, start_new_session=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if line.startswith("PORT "):
            return proc, int(line.split()[1])
        if not line and proc.poll() is not None:
            break
    err = ""
    try:
        proc.kill()
        log.seek(0)
        err = log.read()[-1500:]
    except Exception:                                   # noqa: BLE001
        pass
    raise RuntimeError(f"fixture dashboard did not start for {tree}:\n{err}")


def stop_server(proc):
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:                                   # noqa: BLE001
        try:
            proc.kill()
        except Exception:                               # noqa: BLE001
            pass


def _shoot(base_url, out_dir, selected, frozen_s):
    _browser_env()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise Unavailable(f"playwright is not importable: {exc}") from exc
    shots, errors = [], {}
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:                        # noqa: BLE001
            raise Unavailable(f"chromium will not launch: {str(exc)[:300]}") from exc
        try:
            opened = []
            for name, path, tab, vp, scheme in selected:
                w, h = VIEWPORTS[vp]
                ctx = browser.new_context(
                    viewport={"width": w, "height": h}, device_scale_factor=1,
                    is_mobile=(vp == "phone"), has_touch=(vp == "phone"),
                    color_scheme=scheme, locale="en-US", timezone_id="UTC",
                    reduced_motion="reduce")
                page = ctx.new_page()
                # Seconds: the Python API scales a number by 1000 itself.
                page.clock.set_fixed_time(frozen_s)
                errs = errors.setdefault(name, [])
                page.on("pageerror", lambda exc, errs=errs: errs.append(str(exc)[:300]))
                page.goto(base_url + path, wait_until="load")
                if tab:
                    page.click(f'button.tab[data-tab="{tab}"]')
                opened.append((name, page, ctx))
            # One settle for every page at once: they load in parallel.
            opened[0][1].wait_for_timeout(SETTLE_MS) if opened else None
            for name, page, ctx in opened:
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:                       # noqa: BLE001
                    pass
                page.evaluate("document.fonts && document.fonts.ready")
                full_h = page.evaluate("document.documentElement.scrollHeight")
                w = page.viewport_size["width"]
                out = Path(out_dir) / f"{name}.png"
                page.screenshot(path=str(out), full_page=True, animations="disabled",
                                caret="hide",
                                clip={"x": 0, "y": 0, "width": w,
                                      "height": max(1, min(int(full_h), MAX_HEIGHT))})
                shots.append(str(out))
                ctx.close()
        finally:
            browser.close()
    return shots, {k: v for k, v in errors.items() if v}


def capture(out_dir, tree=None, only=None, python=None):
    """Capture every view of `tree`'s dashboard into `out_dir`.

    Returns {"tree", "shots": [paths], "page_errors": {view: [msg]}}.
    Raises Unavailable when the machine cannot take screenshots."""
    import fixture
    tree = Path(tree or REPO).resolve()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    selected = [v for v in views() if not only or v[0] in only]
    with tempfile.TemporaryDirectory(prefix="arc-visual-fx-") as fx:
        proc, port = start_server(tree, fx, python=python)
        try:
            shots, errs = _shoot(f"http://127.0.0.1:{port}", out_dir, selected,
                                 fixture.FROZEN_NOW)
        finally:
            stop_server(proc)
    result = {"tree": str(tree), "shots": shots, "page_errors": errs}
    (out_dir / "capture.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def _refuse_worktree_out(out, force):
    """A capture written into a git worktree ships with publish's `git add -A`."""
    out = Path(out).resolve()
    for parent in (out, *out.parents):
        if (parent / ".git").exists():
            rel = out.relative_to(parent)
            ignored = subprocess.run(["git", "-C", str(parent), "check-ignore", "-q",
                                      str(rel / "x.png")]).returncode == 0
            if not ignored and not force:
                raise SystemExit(f"refusing to write screenshots to {out}: inside the "
                                 f"git checkout {parent} and not gitignored (use a "
                                 "path under logs/, or --force)")
            break


def main(argv=None):
    sys.path.insert(0, str(HERE))
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, help="directory for the PNGs")
    ap.add_argument("--tree", default=str(REPO), help="checkout to render (default: this one)")
    ap.add_argument("--only", default="", help="comma-separated view names")
    ap.add_argument("--force", action="store_true",
                    help="allow --out inside a checkout even if not gitignored")
    ap.add_argument("--list", action="store_true", help="print the view names and exit")
    ap.add_argument("--strict", action="store_true",
                    help="exit 4 when any page throws a JavaScript error")
    a = ap.parse_args(argv)
    if a.list:
        print("\n".join(v[0] for v in views()))
        return 0
    _refuse_worktree_out(a.out, a.force)
    only = {s for s in a.only.split(",") if s}
    try:
        res = capture(a.out, tree=a.tree, only=only)
    except Unavailable as exc:
        print(f"SKIP: cannot capture screenshots on this machine: {exc}")
        return 3
    for s in res["shots"]:
        print(s)
    for view, errs in res["page_errors"].items():
        for e in errs:
            print(f"PAGE ERROR {view}: {e}")
    if a.strict and res["page_errors"]:
        return 4
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    sys.exit(main())
