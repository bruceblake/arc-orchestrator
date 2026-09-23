"""Asset sign-off: the human's approve / reject on each workbench asset.

From the workflow this studio follows (2026-09): build every asset on its
own, look at it in a live workbench, and APPROVE it — "give approval to the
model for what assets were locked in" — before anything is assembled. The
phase-2 gate requires every measured asset to be approved, so an asset nobody
looked at cannot reach assembly.

Stored per project under the studio directory, never in the game repo: a
sign-off is the operator's judgement about a build, not part of the build.
"""
from __future__ import annotations

import json
import re
import time

import config

APPROVALS = "approvals.json"
STATES = ("approved", "rejected")
_ASSET_RE = re.compile(r"[A-Za-z0-9._ -]{1,120}")


def _path(project):
    return config.studio_run_dir(project, create=True) / APPROVALS


def load(project):
    p = config.studio_run_dir(project) / APPROVALS
    if not p.exists():
        return {}
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


def decide(project, asset, state, *, note="", by="operator"):
    if state not in STATES:
        raise ValueError(f"state must be one of {STATES}")
    if not asset or not _ASSET_RE.fullmatch(asset):
        raise ValueError(f"bad asset name {asset!r}")
    doc = load(project)
    doc[asset] = {"state": state, "note": str(note)[:2000], "by": str(by)[:80],
                  "ts": time.time()}
    _path(project).write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return doc[asset]


def status_of(project, assets):
    """{asset: "approved"|"rejected"|"pending"} for the given asset names."""
    doc = load(project)
    return {a: (doc.get(a) or {}).get("state", "pending") for a in assets}
