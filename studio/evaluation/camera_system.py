"""Module C1: the cameras the visual judge scores.

Rounds 1-2 render from four FIXED anchors, so two consecutive verdicts are
about the game changing rather than the viewpoint changing. From round 3
(``config.STUDIO_ADVERSARIAL_ROUND``) the set gains proc-gen adversarial
angles pointed at the places a model learns to neglect: inside a vent bend,
under the guard tower, behind a door, the far side of a wall it only ever
textured from the front.

The adversarial cameras exist because of a specific failure mode. Give a model
the same four viewpoints every round and it will optimise for those four
viewpoints — the corridor looks immaculate down the anchor axis and the
geometry behind the camera is unlit, untextured, or missing. That is visual
overfitting, and it is invisible to a fixed camera rig by construction.

Everything here is DETERMINISTIC given (project, round). An adversarial angle
that cannot be reproduced is an angle you cannot re-check after a fix, so the
RNG is seeded from the project name and round number and never from the clock.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass, asdict
from pathlib import Path

import config

ANCHOR = "anchor"
ADVERSARIAL = "adversarial"

CAMERA_FILE = "studio_cameras.json"


@dataclass(frozen=True)
class Camera:
    name: str
    position: tuple
    look_at: tuple
    fov: float = 70.0
    kind: str = ANCHOR
    note: str = ""

    def to_dict(self):
        d = asdict(self)
        d["position"] = list(self.position)
        d["look_at"] = list(self.look_at)
        return d


# The four canonical viewpoints of a prison-escape level. Coordinates are in
# metres in Godot's Y-up, -Z-forward convention, sized for the default graybox
# block-out (a ~60x60m yard with a two-storey cell block). A project overrides
# them wholesale by committing studio_cameras.json — see load_anchors.
DEFAULT_ANCHORS = (
    Camera("isometric_overview", (38.0, 34.0, 38.0), (0.0, 2.0, 0.0), 45.0, ANCHOR,
           "The whole facility: block, yard, wall, towers. Reads silhouette, "
           "massing and layout legibility."),
    Camera("cell_corridor", (0.0, 1.7, 14.0), (0.0, 1.6, -12.0), 65.0, ANCHOR,
           "Eye height down the cell block corridor. Reads repetition, "
           "material variation and the corridor's sightline."),
    Camera("guard_station", (-9.0, 3.2, -6.0), (2.0, 1.5, 4.0), 70.0, ANCHOR,
           "Over the guard station toward the block. Reads the surveillance "
           "relationship: what a guard can actually see."),
    Camera("perimeter_fence", (0.0, 2.0, 40.0), (0.0, 6.0, 18.0), 60.0, ANCHOR,
           "Outside the wire looking in. Reads the perimeter silhouette, "
           "tower placement and the wall's read against the sky."),
)

# What an adversarial camera is FOR. Each kind describes where to put it and
# what neglect it is hunting; the text travels with the render into the judge
# prompt, so a low score is attributable to a specific suspicion.
HOTSPOT_KINDS = {
    "vent_bend": "Inside a ventilation shaft at a bend, looking around the "
                 "corner. Hunting: untextured interior faces, missing "
                 "collision, geometry that only exists from outside.",
    "under_tower": "Beneath the guard tower looking up at its underside. "
                   "Hunting: unlit underfaces, floating supports, a structure "
                   "modelled only from eye level.",
    "behind_door": "Behind an opened door in the gap it leaves. Hunting: "
                   "single-sided geometry, z-fighting against the wall, "
                   "door frames that do not meet the wall.",
    "wall_backside": "The unvisited side of a perimeter or cell wall. "
                     "Hunting: missing material assignment, light leaking "
                     "through the seam, inverted normals.",
    "floor_seam": "Low and close to where two floor sections meet. Hunting: "
                  "z-fighting, gaps the player can see through, mismatched "
                  "tiling scale.",
    "ceiling_corner": "A high corner looking down into the room. Hunting: "
                      "light leaks at the wall/ceiling join, shadow acne, "
                      "geometry that stops short of the ceiling.",
}


def _seed(project, round_n):
    """A stable integer seed for (project, round)."""
    h = hashlib.sha256(f"{project}:{round_n}".encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big")


def load_anchors(project_dir=None):
    """The four anchors, from the project's override file when it has one.

    A graybox block-out at a different scale than the default would put every
    anchor inside a wall, so a project may commit `studio_cameras.json`:

        {"anchors": [{"name": ..., "position": [x,y,z], "look_at": [x,y,z],
                      "fov": 45, "note": "..."}]}

    Overriding is all-or-nothing on purpose: a half-overridden rig mixes two
    coordinate scales and produces four renders of nothing in particular.
    """
    if project_dir:
        path = Path(project_dir) / CAMERA_FILE
        if path.exists():
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise ValueError(f"{path}: not valid JSON ({exc})")
            raw = doc.get("anchors")
            if not isinstance(raw, list) or not raw:
                raise ValueError(f"{path}: 'anchors' must be a non-empty list")
            out = []
            for a in raw:
                out.append(Camera(
                    name=str(a["name"]),
                    position=tuple(float(v) for v in a["position"]),
                    look_at=tuple(float(v) for v in a["look_at"]),
                    fov=float(a.get("fov", 70.0)),
                    kind=ANCHOR,
                    note=str(a.get("note", "")),
                ))
            return tuple(out)
    return DEFAULT_ANCHORS


def load_hotspots(project_dir=None):
    """Declared adversarial hotspots, or () when the project declares none.

    A hotspot is a place the level designer (human or model) knows is easy to
    neglect: {"kind": "vent_bend", "position": [x,y,z], "look_at": [x,y,z]}.
    With none declared, adversarial cameras are synthesised around the anchor
    volume instead — worse targeted, still unpredictable.
    """
    if not project_dir:
        return ()
    path = Path(project_dir) / CAMERA_FILE
    if not path.exists():
        return ()
    doc = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for h in doc.get("hotspots", []) or []:
        kind = str(h.get("kind", "")) or "wall_backside"
        out.append({
            "kind": kind,
            "position": [float(v) for v in h["position"]],
            "look_at": [float(v) for v in h.get("look_at", [0, 0, 0])],
        })
    return tuple(out)


def _synthetic(rng, anchors, kind, idx):
    """An adversarial camera derived from the anchor volume.

    Used when a project declares no hotspots: orbit the scene centre at an
    unusual radius and an unusual height, and look slightly off-centre so the
    frame is not the postcard composition an anchor would give.
    """
    xs = [a.position[0] for a in anchors]
    ys = [a.position[1] for a in anchors]
    zs = [a.position[2] for a in anchors]
    cx, cz = (min(xs) + max(xs)) / 2.0, (min(zs) + max(zs)) / 2.0
    span = max(max(xs) - min(xs), max(zs) - min(zs)) or 20.0
    radius = span * rng.uniform(0.12, 0.42)
    theta = rng.uniform(0, 2 * math.pi)
    # Deliberately low or deliberately high — eye level is what the anchors
    # already cover.
    height = rng.choice([rng.uniform(0.25, 0.9), rng.uniform(4.5, min(max(ys), 12.0) or 8.0)])
    px, pz = cx + radius * math.cos(theta), cz + radius * math.sin(theta)
    tx = cx + rng.uniform(-span * 0.15, span * 0.15)
    tz = cz + rng.uniform(-span * 0.15, span * 0.15)
    return Camera(
        name=f"adv_{kind}_{idx}",
        position=(round(px, 2), round(height, 2), round(pz, 2)),
        look_at=(round(tx, 2), round(rng.uniform(0.5, 3.0), 2), round(tz, 2)),
        fov=round(rng.uniform(55.0, 95.0), 1),
        kind=ADVERSARIAL,
        note=HOTSPOT_KINDS.get(kind, "") + " (synthesised: no hotspot declared)",
    )


def adversarial_cameras(project, round_n, *, project_dir=None, count=None):
    """`count` adversarial cameras for this project and round, deterministic."""
    count = config.STUDIO_ADVERSARIAL_CAMERAS if count is None else count
    if count <= 0:
        return ()
    rng = random.Random(_seed(project, round_n))
    anchors = load_anchors(project_dir)
    hotspots = list(load_hotspots(project_dir))
    rng.shuffle(hotspots)
    out = []
    for i in range(count):
        if hotspots:
            h = hotspots[i % len(hotspots)]
            jitter = [round(v + rng.uniform(-0.35, 0.35), 2) for v in h["position"]]
            out.append(Camera(
                name=f"adv_{h['kind']}_{i + 1}",
                position=tuple(jitter),
                look_at=tuple(round(float(v), 2) for v in h["look_at"]),
                fov=round(rng.uniform(60.0, 95.0), 1),
                kind=ADVERSARIAL,
                note=HOTSPOT_KINDS.get(h["kind"], ""),
            ))
        else:
            kind = rng.choice(sorted(HOTSPOT_KINDS))
            out.append(_synthetic(rng, anchors, kind, i + 1))
    return tuple(out)


def cameras_for_round(project, round_n, *, project_dir=None):
    """The full camera set for a round: anchors always, adversarial from N."""
    if round_n < 1:
        raise ValueError(f"round must be >= 1, got {round_n}")
    cams = list(load_anchors(project_dir))
    if round_n >= config.STUDIO_ADVERSARIAL_ROUND:
        cams += list(adversarial_cameras(project, round_n, project_dir=project_dir))
    return tuple(cams)


def to_json(cameras):
    """The payload studio.engine.godot.render consumes."""
    return [c.to_dict() for c in cameras]
