"""Visual evidence for changes to THIS repo's own UI: the dashboard.

evidence.py (Rule 7d) shows reviewers what a Godot game looks like after a
change. The orchestrator's own UI — static/index.html, static/phone.html,
static/usage.html, static/panels/*.js, served by dashboard.py — had nothing:
a pull request that restyled the dashboard reached its reviewers and the
operator as a text diff, and "does it still look right?" was answered by
nobody. This module is the dashboard's equivalent (AGENTS.md Rule 7e):

  shots/       every view in tools/visual/capture.py (page x desktop/phone x
               light/dark), rendered headlessly from the task WORKTREE over a
               fixed fixture world with a frozen clock
  before/      the same views rendered from the task's MERGE BASE (never the
               live base branch — siblings' merges are not this change), for
               the views that changed
  compare/     before | after | difference per changed view, cropped to the
               region that moved, captioned with the share of pixels changed
  contact_sheet.png   every after-shot in one labeled grid

The manifest is shaped like evidence.py's, so the same publishing path — the
orphan `arc-evidence` branch, one directory per task attempt — carries it to
the pull request, and evidence.publish is reused unchanged.

Design rules:
- Nothing is written into the worktree. Captures go under
  config.EVIDENCE_DIR; the baseline is extracted with `git archive` into a
  temp dir, so no git worktree or ref is created or moved.
- The screenshot TOOL is the orchestrator's own (config.ROOT/tools/visual),
  used for both sides, so before and after differ only by the tree rendered.
- A machine that cannot capture (no Playwright, no Chromium) is an
  infrastructure gap, reported and never blamed on the implementer. A tree
  whose dashboard will not even start is the implementer's to fix.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import config
import evidence

KIND = "ui"
TOOL_DIR = Path(config.ROOT) / "tools" / "visual"

# What counts as "the dashboard UI". dashboard.py is included because it
# builds every payload the pages render; a change there can move pixels.
_UI_PREFIXES = ("static/",)
_UI_FILES = ("dashboard.py",)

# A view whose share of changed pixels is above this gets a before|after|diff
# panel. Rendering is pixel-identical run to run (tools/visual/README), so
# anything above noise is a real change.
MIN_CHANGE = 0.00005
_CROP_PAD = 160              # context kept around the changed region
_CROP_MAX = 1400             # tallest comparison panel, in source pixels
_CROP_MIN_W = 720            # narrowest crop: enough page to see where it is
_CROP_MIN_H = 420


def is_ui_path(path):
    p = str(path or "").lstrip("./")
    return p.startswith(_UI_PREFIXES) or p in _UI_FILES


def touches_ui(paths):
    """The UI paths among `paths` (a list of repo-relative file names)."""
    return [p for p in paths or [] if is_ui_path(p)]


_DIFF_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.M)


def diff_touches_ui(diff):
    """UI paths named by a unified diff's `diff --git` headers."""
    seen = []
    for a, b in _DIFF_HEADER.findall(diff or ""):
        for p in (a, b):
            if is_ui_path(p) and p not in seen:
                seen.append(p)
    return seen


def is_dashboard_tree(path):
    p = Path(path)
    return (p / "dashboard.py").is_file() and (p / "static" / "index.html").is_file()


def enabled_for(worktree, task=None, changed=()):
    """Whether this gate captures dashboard screenshots: a checkout of this
    repo, a diff that touches its UI, evidence not off, task not opted out."""
    if config.EVIDENCE_MODE == "off":
        return False
    if task is not None and task.get("evidence") is False:
        return False
    return is_dashboard_tree(worktree) and bool(touches_ui(changed))


# --- the capture tool -------------------------------------------------------

def _tool(name):
    spec = importlib.util.spec_from_file_location(f"arc_visual_{name}", TOOL_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tool_hash():
    h = hashlib.sha1()
    for f in sorted(TOOL_DIR.glob("*.py")):
        h.update(f.name.encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:10]


def run_capture(tree, out_dir, timeout=None):
    """Render every view of `tree` into `out_dir` (subprocess). Returns the
    tool's capture.json dict. Raises EvidenceUnavailable / EvidenceError."""
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    try:
        p = subprocess.run([sys.executable, str(TOOL_DIR / "capture.py"), "--out",
                            str(out_dir), "--tree", str(tree), "--force"],
                           capture_output=True, text=True, env=config.child_env(),
                           timeout=timeout or config.EVIDENCE_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise evidence.EvidenceError(f"screenshot capture timed out after {exc.timeout}s")
    if p.returncode == 3:
        raise evidence.EvidenceUnavailable((p.stdout + p.stderr).strip()[-300:])
    if p.returncode != 0:
        raise evidence.EvidenceError(
            "the dashboard did not render for its screenshots:\n"
            + (p.stderr or p.stdout).strip()[-1500:])
    return json.loads((out_dir / "capture.json").read_text(encoding="utf-8"))


def baseline(worktree, sha, timeout=None):
    """(dir, capture dict) of the views rendered at commit `sha`, cached.

    The tree is `git archive`d into a temp dir: exactly the commit, no
    uncommitted state, and no worktree registered or ref moved. Cached by
    sha AND the tool's own hash, so a changed capture tool re-renders."""
    cache = Path(config.EVIDENCE_DIR) / "_ui_baseline" / f"{sha}-{_tool_hash()}"
    done = cache / "capture.json"
    if done.is_file():
        return cache, json.loads(done.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="arc-ui-base-") as tmp:
        tar = Path(tmp) / "tree.tar"
        with open(tar, "wb") as fh:
            r = subprocess.run(["git", "-C", str(worktree), "archive", "--format=tar", sha],
                               stdout=fh, stderr=subprocess.PIPE, timeout=120,
                               env=config.child_env())
        if r.returncode != 0:
            raise evidence.EvidenceError(
                f"git archive {sha[:10]}: {r.stderr.decode(errors='replace')[:300]}")
        tree = Path(tmp) / "tree"
        with tarfile.open(tar) as tf:
            tf.extractall(tree, filter="data")
        work = cache.with_name(cache.name + ".tmp")
        res = run_capture(tree, work, timeout=timeout)
        if cache.exists():
            shutil.rmtree(cache)
        work.rename(cache)
        res["shots"] = [str(cache / Path(s).name) for s in res.get("shots") or []]
        done.write_text(json.dumps(res, indent=2), encoding="utf-8")
        return cache, res


# --- comparing ----------------------------------------------------------------

def _size(path):
    return _tool("compare")._size(path)


def _span(lo, length, want, total, lo_min):
    """[start, size) of a window of `want` (at least lo_min) around lo..lo+length."""
    size = min(total, max(lo_min, min(want, length + 2 * _CROP_PAD)))
    if length + 2 * _CROP_PAD > size:
        # The change is bigger than the window: show where it STARTS. A
        # layout shift's cause is at the top of its box (a tab that wrapped
        # pushes everything below it); centring cropped the cause away.
        start = max(0, lo - _CROP_PAD)
    else:
        start = lo + length // 2 - size // 2
    return max(0, min(start, total - size)), size


def crop_rect(box, width, height):
    """(x, y, w, h): the part of a page worth showing around a changed box.

    A panel is scaled to ~640 px wide, so a full 1440 px desktop row would be
    shrunk to unreadable text. The window is cropped to the change in BOTH
    directions, with context around it and a floor on its size so the reader
    still sees where on the page it is."""
    if not box:
        return 0, 0, width, min(height, _CROP_MAX)
    x, w = _span(box[0], box[2], width, width, min(width, _CROP_MIN_W))
    y, h = _span(box[1], box[3], _CROP_MAX, height, min(height, _CROP_MIN_H))
    return x, y, w, h


def _pad_to(src, dst, w, h):
    """`src` padded (grey, bottom/right) to w x h, as rgb24 PNG."""
    evidence._ffmpeg(["-i", str(src), "-vf", f"format=rgb24,pad={w}:{h}:0:0:0x808080",
                      str(dst)])
    return dst


def _panel(before, after, out, box, changed, *, before_sha, after_sha, scratch):
    """before | after | heatmap for one view, cropped to where it changed.

    `before` and `after` are already the same size (compare pads them)."""
    w, h = _size(after)
    x, y, cw, ch = crop_rect(box, w, h)
    prepared = []
    for i, src in enumerate((before, after)):
        dst = Path(scratch) / f"{Path(out).stem}-crop{i}.png"
        evidence._ffmpeg(["-i", str(src), "-vf", f"format=rgb24,crop={cw}:{ch}:{x}:{y}",
                          str(dst)])
        prepared.append(dst)
    rel = None
    if box:
        bx, by = max(box[0], x), max(box[1], y)
        bx1, by1 = min(box[0] + box[2], x + cw), min(box[1] + box[3], y + ch)
        rel = (bx - x, by - y, max(1, bx1 - bx), max(1, by1 - by))
    font = evidence._font_file()
    evidence._ffmpeg(["-i", str(prepared[0]), "-i", str(prepared[1]), "-filter_complex",
                      evidence._diff_filter("BEFORE " + (before_sha[:10] or "base"),
                                            "AFTER " + (after_sha[:10] or "worktree"),
                                            f"DIFF {changed:.2%} changed", rel, False, font),
                      "-compression_level", "9", str(out)])
    return evidence._shrink(out)


def compare(base_dir, shots, out_dir, *, before_sha="", after_sha=""):
    """[{"name", "changed", "box", "side_by_side", "before", "new"}] per view.

    A page whose height changed (a wrapped row, a new panel) is padded to the
    taller of the two and compared row for row from the top, so the box still
    points at where the layout moved instead of "100% changed"."""
    cmp = _tool("compare")
    out_dir = Path(out_dir)
    rows = []
    with tempfile.TemporaryDirectory(prefix="arc-ui-cmp-") as scratch:
        for shot in shots:
            shot = Path(shot)
            before = Path(base_dir) / shot.name
            if not before.exists():
                rows.append({"name": shot.stem, "new": True, "changed": None})
                continue
            b_img, a_img = before, shot
            sb, sa = _size(before), _size(shot)
            if sb != sa:
                w, h = max(sb[0], sa[0]), max(sb[1], sa[1])
                b_img = _pad_to(before, Path(scratch) / f"{shot.stem}-b.png", w, h)
                a_img = _pad_to(shot, Path(scratch) / f"{shot.stem}-a.png", w, h)
            d = cmp.diff(b_img, a_img, threshold=0)
            row = {"name": shot.stem, "changed": d["changed"],
                   "box": list(d["box"]) if d["box"] else None, "side_by_side": None}
            if sb != sa:
                row["size"] = {"before": list(sb), "after": list(sa)}
            if d["changed"] > MIN_CHANGE:
                (out_dir / "before").mkdir(parents=True, exist_ok=True)
                (out_dir / "compare").mkdir(parents=True, exist_ok=True)
                kept = out_dir / "before" / shot.name
                shutil.copyfile(before, kept)
                row["before"] = str(kept)
                try:
                    row["side_by_side"] = str(_panel(
                        b_img, a_img, out_dir / "compare" / shot.name, d["box"],
                        d["changed"], before_sha=before_sha, after_sha=after_sha,
                        scratch=scratch))
                except evidence.EvidenceError as exc:
                    row["panel_error"] = str(exc)[:300]
            rows.append(row)
    return rows


def capture(worktree, out_dir, *, base=None, project="", changed=(), timeout=None):
    """Capture the dashboard's views for `worktree`, and before/after vs the
    merge base with `base`. Returns the manifest (also out_dir/manifest.json)."""
    worktree, out_dir = Path(worktree), Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    started = time.time()
    head = evidence._git(["rev-parse", "HEAD"], worktree, check=False)
    manifest = {"kind": KIND, "worktree": str(worktree), "project": project,
                "head": head, "ui_files": touches_ui(changed), "shots": [],
                "videos": {}, "compare": [], "warnings": [], "coverage": {},
                "page_errors": {}}
    res = run_capture(worktree, out_dir / "shots", timeout=timeout)
    manifest["shots"] = res.get("shots") or []
    manifest["page_errors"] = res.get("page_errors") or {}
    evidence._cover(manifest, "views", "captured" if manifest["shots"] else "failed",
                    "" if manifest["shots"] else "no view rendered")
    sha = evidence.merge_base(worktree, base) if base else None
    base_errors = {}
    if not sha:
        evidence._cover(manifest, "baseline", "skipped", "no merge base")
        evidence._cover(manifest, "compare", "skipped", "no merge base")
    else:
        try:
            bdir, bres = baseline(worktree, sha, timeout=timeout)
            base_errors = bres.get("page_errors") or {}
            manifest["baseline"] = {"sha": sha, "dir": str(bdir), "status": "captured"}
            evidence._cover(manifest, "baseline", "captured")
            manifest["compare"] = compare(bdir, manifest["shots"], out_dir,
                                          before_sha=sha, after_sha=head)
            evidence._cover(manifest, "compare", "captured")
        except evidence.EvidenceUnavailable:
            raise
        except (evidence.EvidenceError, subprocess.SubprocessError, OSError) as exc:
            reason = f"baseline render failed: {str(exc)[:200]}"
            manifest["baseline"] = {"sha": sha, "dir": None, "status": "failed",
                                    "reason": reason}
            evidence._cover(manifest, "baseline", "failed", reason)
            evidence._cover(manifest, "compare", "skipped", "no baseline")
            manifest["warnings"].append(reason)
    # A JavaScript error the merge base did not throw is this change's.
    new_errs = {}
    for view, errs in manifest["page_errors"].items():
        fresh = [e for e in errs if e not in (base_errors.get(view) or [])]
        if fresh:
            new_errs[view] = fresh
    manifest["new_page_errors"] = new_errs
    for view, errs in new_errs.items():
        manifest["warnings"].append(f"view {view} throws a JavaScript error the merge "
                                    f"base did not: {errs[0]}")
    manifest["changed_views"] = [c["name"] for c in manifest["compare"]
                                 if c.get("changed") and c["changed"] > MIN_CHANGE]
    if manifest["compare"] and manifest["ui_files"] and not manifest["changed_views"]:
        manifest["no_visible_change"] = True
        manifest["warnings"].append(
            "NO VISIBLE CHANGE: this diff touches " + ", ".join(manifest["ui_files"][:5])
            + " but none of the captured views changed a pixel. Either the change "
            "is outside the captured views (a dialog, a hover state, another tab), "
            "or it does not do what it claims.")
    sheet = evidence.contact_sheet([(Path(s).stem, s) for s in manifest["shots"]],
                                   out_dir, cols=4)
    if sheet:
        manifest["contact_sheet"] = str(sheet)
    manifest["seconds"] = round(time.time() - started, 1)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


# --- presenting -------------------------------------------------------------

def _pct(x):
    return "—" if x is None else f"{x:.2%}"


def review_images(manifest, limit=8):
    """Changed views' before|after|diff panels first, then the contact sheet."""
    out = [c["side_by_side"] for c in manifest.get("compare") or [] if c.get("side_by_side")]
    sheet = manifest.get("contact_sheet")
    if sheet:
        out.append(sheet)
    if not out:
        out = list(manifest.get("shots") or [])
    return [p for p in out if Path(p).exists()][:limit]


def prompt_block(manifest, sees_images=True):
    """The reviewer's VISUAL EVIDENCE block.

    `sees_images` is the reviewer model's real capability. A blind model is
    told so plainly and is not asked to judge pixels it cannot see: the
    numbers, the JavaScript errors and the PR comment (for the human) are its
    evidence, and the committed golden-image test in check.sh is the gate."""
    if not manifest:
        return ""
    lines = ["VISUAL EVIDENCE — DASHBOARD SCREENSHOTS (captured after the verify gate "
             "passed, from this worktree and from the merge base, over the same fixed "
             "fixture data and frozen clock, so any difference is this diff's doing)."]
    if sees_images:
        lines.append(
            "The images attached to this review are real screenshots. LOOK at each "
            "one: a visible regression — broken or overlapping layout, clipped or "
            "unreadable text, lost contrast, a panel that disappeared, a phone "
            "layout that overflows — is a BLOCKING issue, exactly like a failing "
            "test. So is a change that does not show what the spec asks for. "
            "Only phone.html has dark-mode styles: the -dark views of index, "
            "projects and usage render exactly like their -light views, so they "
            "say nothing about how dark mode looks.")
    else:
        lines.append(
            "YOU CANNOT SEE IMAGES: this reviewer model rejects image input. Do not "
            "claim to have looked at the screenshots, and do not judge pixels. They "
            "are posted on the pull request for the human reviewer. Your visual "
            "evidence is the table below (which views changed, and how much) and "
            "the JavaScript errors; the golden-image test in ./check.sh is the "
            "deterministic gate. A view that changed when the spec implies it "
            "should not, or a new JavaScript error, is still a blocking issue.")
    if manifest.get("no_visible_change"):
        lines.append("*** NO VISIBLE CHANGE: the diff touches UI files but no captured "
                     "view changed. Verify the change another way (a test that "
                     "renders it) or reject for missing evidence. ***")
    for c in manifest.get("compare") or []:
        if c.get("new"):
            lines.append(f"- view {c['name']}: new (no baseline)")
        elif c.get("side_by_side"):
            lines.append(f"- view {c['name']}: {_pct(c.get('changed'))} of pixels changed; "
                         f"before|after|diff: {c['side_by_side']}")
    unchanged = [c["name"] for c in manifest.get("compare") or []
                 if not c.get("new") and not c.get("side_by_side")]
    if unchanged:
        lines.append(f"- unchanged views ({len(unchanged)}): " + ", ".join(unchanged))
    if not manifest.get("compare"):
        lines += [f"- screenshot: {s}" for s in manifest.get("shots") or []]
    if manifest.get("contact_sheet"):
        lines.append(f"- every view after the change: {manifest['contact_sheet']}")
    for k, c in (manifest.get("coverage") or {}).items():
        if c.get("status") != "captured":
            lines.append(f"- coverage {k}: {c.get('status')} — {c.get('reason')}")
    for view, errs in (manifest.get("new_page_errors") or {}).items():
        for e in errs:
            lines.append(f"- NEW JAVASCRIPT ERROR in {view}: {e}")
    return "\n".join(lines) + "\n"


def pr_markdown(manifest, web_base, *, task_id, attempt):
    """The PR comment: changed views as before|after|diff, then every view."""
    shots = manifest.get("shots") or []
    # Every link is relative to the capture dir; without a shot there is
    # nothing to link (compare and the contact sheet are built from shots).
    root = Path(shots[0]).parent.parent if shots else None

    def url(local, raw=True):
        rel = Path(local).relative_to(root)
        return f"{web_base}/{rel.as_posix()}" + ("?raw=true" if raw else "")

    head = (manifest.get("head") or "")[:10]
    base = ((manifest.get("baseline") or {}).get("sha") or "")[:10]
    lines = [f"### 🖼️ Dashboard screenshots — `{task_id}` attempt {attempt}"
             + (f" at `{head}`" if head else ""), ""]
    ui = manifest.get("ui_files") or []
    if ui:
        lines += ["UI files in this diff: " + ", ".join(f"`{p}`" for p in ui[:8]), ""]
    if root is None:
        lines += ["**No view was captured**, so there is nothing to compare.", ""]
    comp = [c for c in manifest.get("compare") or []
            if c.get("side_by_side") and root is not None]
    if comp:
        lines += [f"**Changed views — before \\| after \\| difference** (vs merge base "
                  f"`{base}`; cropped to the region that changed)", ""]
        for c in comp:
            lines += [f"**{c['name']}** — {_pct(c.get('changed'))} of pixels changed "
                      f"([after, full page]({url(root / 'shots' / (c['name'] + '.png'))}) · "
                      f"[before, full page]({url(c['before'])}))", "",
                      f"![{c['name']}]({url(c['side_by_side'])})", ""]
    elif manifest.get("compare"):
        lines += ["**No captured view changed** against the merge base "
                  f"`{base}`.", ""]
    unchanged = [c["name"] for c in manifest.get("compare") or []
                 if not c.get("new") and not c.get("side_by_side")]
    if unchanged and comp:
        lines += [f"Unchanged views: {', '.join(unchanged)}", ""]
    sheet = manifest.get("contact_sheet")
    if sheet and root is not None and Path(sheet).exists():
        lines += [f"**Every view after the change** ([full size]({url(sheet)}))", "",
                  f"![every view]({url(sheet)})", ""]
    if shots:
        lines += ["<details><summary>Full-page screenshots (" + str(len(shots))
                  + ")</summary>", ""]
        lines += [f"**{Path(s).stem}**\n\n![{Path(s).stem}]({url(s)})\n" for s in shots]
        lines += ["</details>", ""]
    errs = manifest.get("new_page_errors") or {}
    if errs:
        lines += ["**New JavaScript errors** (not thrown at the merge base)", ""]
        lines += [f"- `{v}`: `{e[:200]}`" for v, es in errs.items() for e in es]
        lines.append("")
    if manifest.get("warnings"):
        lines += ["**Warnings**", ""] + [f"- ⚠️ {w}" for w in manifest["warnings"]] + [""]
    lines.append(f"<sub>Captured by arc-orchestrator (AGENTS.md Rule 7e) in "
                 f"{manifest.get('seconds', '?')}s: fixture data, frozen clock, "
                 "headless Chromium.</sub>")
    return "\n".join(lines)


def board_body(manifest):
    n = len(manifest.get("shots") or [])
    changed = [f"{c['name']} {_pct(c.get('changed'))}" for c in manifest.get("compare") or []
               if c.get("side_by_side")]
    body = f"dashboard screenshots: {n} view(s)"
    body += ("; changed vs merge base: " + ", ".join(changed[:8])) if changed else \
        ("; no view changed" if manifest.get("compare") else "")
    if manifest.get("new_page_errors"):
        body += f"; {len(manifest['new_page_errors'])} view(s) with new JS errors"
    if manifest.get("shots"):
        body += f" — {Path(manifest['shots'][0]).parent.parent}"
    return body


# --- which module presents a manifest ---------------------------------------

def presenter(manifest):
    """This module for a dashboard manifest, evidence.py for a game's."""
    return sys.modules[__name__] if (manifest or {}).get("kind") == KIND else evidence


if __name__ == "__main__":                              # manual capture
    import argparse
    ap = argparse.ArgumentParser(description="capture dashboard screenshots, before/after")
    ap.add_argument("worktree")
    ap.add_argument("out_dir")
    ap.add_argument("--base", default="main")
    a = ap.parse_args()
    files = subprocess.run(["git", "-C", a.worktree, "diff", "--name-only",
                            evidence.merge_base(a.worktree, a.base) or "HEAD"],
                           capture_output=True, text=True,
                           env=config.child_env()).stdout.split()
    m = capture(a.worktree, a.out_dir, base=a.base, project=Path(a.worktree).name,
                changed=files)
    print(json.dumps(m, indent=2))
