"""Module D: keep every render on disk, keep almost none of it in context.

A visual loop generates images faster than any context window can hold them.
Seven cameras a round for twenty rounds is 140 screenshots; attaching even a
tenth of that to a judge call wastes the window on history the judge was
explicitly told not to consider, and on a 1M-context model it is still real
money per round.

The split this module enforces:

  DISK   everything, forever, under logs/studio/<project>/round_<n>/. That is
         the evidence trail — Rule 7 applies to renders exactly as it does to
         transcripts. A verdict you cannot re-examine the pixels for is a
         claim, not a finding.

  CONTEXT  the four canonical anchors from the BASELINE round, plus every
           render from the most recent round. The anchors are what makes a
           before/after judgement possible at all; the latest round is what
           is being judged. Nothing else earns its tokens.

`reset_baseline` is the other half. When a phase is promoted, the old
baseline stops being a fair comparison — a graybox anchor shot is not
evidence about a lit, textured build, and leaving it in context invites the
judge to score phase 3 against phase 1's silhouette. Promotion moves the
baseline forward.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import config

BASELINE_FILE = "baseline.json"
META_FILE = "meta.json"


def project_dir(project, create=False):
    return config.studio_run_dir(project, create=create)


def round_dir(project, round_n, create=False):
    d = project_dir(project) / f"round_{int(round_n)}"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def archive(project, round_n, images, *, cameras=(), phase="", note=""):
    """Copy a round's renders into the archive and record what they are.

    `images` are paths as rendered (inside a worktree, which is disposable);
    `cameras` are the Camera objects or dicts they came from, so the archive
    records each shot's KIND — an anchor and an adversarial shot are used
    differently by every consumer downstream.
    """
    dest = round_dir(project, round_n, create=True)
    by_name = {}
    for c in cameras:
        d = c.to_dict() if hasattr(c, "to_dict") else dict(c)
        by_name[d.get("name", "")] = d
    entries = []
    for img in images:
        img = Path(img)
        if not img.exists():
            raise FileNotFoundError(f"render missing: {img}")
        out = dest / img.name
        if img.resolve() != out.resolve():
            shutil.copy2(img, out)
        cam = by_name.get(img.stem, {})
        entries.append({
            "name": img.stem,
            "path": str(out),
            "kind": cam.get("kind", ""),
            "note": cam.get("note", ""),
        })
    meta = {"project": str(project), "round": int(round_n), "phase": phase,
            "ts": time.time(), "note": note, "images": entries}
    (dest / META_FILE).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def round_meta(project, round_n):
    """What was archived for a round, or None if the round has none."""
    path = round_dir(project, round_n) / META_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def rounds(project):
    """Archived round numbers, ascending."""
    d = project_dir(project)
    if not d.exists():
        return []
    out = []
    for child in d.iterdir():
        if child.is_dir() and child.name.startswith("round_"):
            try:
                out.append(int(child.name.split("_", 1)[1]))
            except ValueError:
                continue
    return sorted(out)


def latest_round(project):
    r = rounds(project)
    return r[-1] if r else 0


# --- the baseline -----------------------------------------------------------
def baseline(project):
    """The current baseline {phase, round, ts}, or None if never set."""
    path = project_dir(project) / BASELINE_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def reset_baseline(project, *, phase="", round_n=None, reason=""):
    """Start comparing from here.

    Called on every phase promotion. Comparing a lit, textured build against
    a graybox anchor shot does not measure progress, it measures the phase
    change — and a judge handed both will describe the obvious difference
    instead of the defects it was asked to find.
    """
    d = project_dir(project, create=True)
    doc = {"phase": phase, "round": int(round_n if round_n is not None
                                        else latest_round(project)),
           "ts": time.time(), "reason": reason}
    (d / BASELINE_FILE).write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return doc


# --- what actually reaches a model ------------------------------------------
def context_images(project, round_n=None, *, keep_rounds=None):
    """The bounded image set for a judge call.

    Returns [{"path", "name", "kind", "round", "role"}]: baseline anchors
    first (role="baseline"), then the recent rounds (role="current").
    """
    keep_rounds = config.STUDIO_KEEP_ROUNDS if keep_rounds is None else keep_rounds
    keep_rounds = max(1, int(keep_rounds))
    round_n = latest_round(project) if round_n is None else int(round_n)
    out, seen = [], set()

    base = baseline(project)
    if base and base.get("round") and int(base["round"]) != round_n:
        meta = round_meta(project, int(base["round"]))
        for img in (meta or {}).get("images", []):
            # Anchors only. An adversarial angle is generated fresh each round
            # and has no counterpart to compare against, so it is not a
            # baseline.
            if img.get("kind") and img["kind"] != "anchor":
                continue
            if img["path"] in seen or not Path(img["path"]).exists():
                continue
            seen.add(img["path"])
            out.append({**img, "round": int(base["round"]), "role": "baseline"})

    for n in range(max(1, round_n - keep_rounds + 1), round_n + 1):
        meta = round_meta(project, n)
        for img in (meta or {}).get("images", []):
            if img["path"] in seen or not Path(img["path"]).exists():
                continue
            seen.add(img["path"])
            out.append({**img, "round": n, "role": "current"})
    return out


def stats(project):
    """Disk vs context: what is kept, and what is actually sent."""
    all_imgs = 0
    for n in rounds(project):
        all_imgs += len((round_meta(project, n) or {}).get("images", []))
    ctx = context_images(project)
    return {
        "project": str(project),
        "rounds_archived": len(rounds(project)),
        "images_on_disk": all_imgs,
        "images_in_context": len(ctx),
        "baseline": baseline(project),
        "dir": str(project_dir(project)),
    }
