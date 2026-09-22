"""Module B: the five phases and the gates between them.

A phase gate answers one question: is there EVIDENCE that this phase is done?
Not "did the tasks merge" — merged tasks prove the code was accepted, not that
the corridor is the width the target says it is.

Two kinds of evidence, and the order matters:

  DETERMINISTIC first. Bucket A is numbers — corridor width, crouch clearance,
  sprint speed, triangle counts, tick rate. Those are compared by this module
  against `studio_metrics.json` with no model involved, because a measurement
  a model reports is a claim and a measurement a program computes is a fact.
  The graybox "primitives only" rule is checked the same way: by looking for
  mesh files, not by asking.

  JUDGED second, and only on what cannot be measured. Bucket B is atmosphere,
  and there is no assertion for "reads as a prison at dusk". That is what the
  visual judge is for, and it gates only after the numbers already agree.

Phases never skip and never run in parallel. `promote` refuses unless the
current phase's gate passes, and every promotion resets the render baseline
(see studio.memory.compactor) because the previous phase's frames stop being
a fair comparison the moment the phase changes.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import config
import events
from studio.evaluation import judge_loop
from studio.memory import compactor
from studio.schemas.task import (
    PHASES, PHASE_INTENT, phase_index,
    PHASE_0_TARGET_GROUNDING, PHASE_1_GRAYBOX_PROTOTYPING,
    PHASE_2_3D_ASSET_AND_ANIMATION, PHASE_3_ATMOSPHERE_LIGHTING,
    PHASE_4_NETWORKED_QA,
)

STATE_FILE = "stage.json"
METRICS_FILE = "studio_metrics.json"
MESH_SUFFIXES = (".gltf", ".glb", ".fbx", ".obj", ".dae", ".blend")
# Tolerance for a Bucket A number, as a fraction. A corridor specified at
# 2.4m is met at 2.35m; it is not met at 1.8m.
BUCKET_A_TOLERANCE = float(0.05)


class GateFailure(RuntimeError):
    pass


# --- persisted phase state ---------------------------------------------------
def _state_path(project):
    return config.studio_run_dir(project, create=True) / STATE_FILE


def state(project):
    path = _state_path(project)
    if not path.exists():
        return {"phase": PHASES[0], "entered_ts": None, "history": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {"phase": PHASES[0], "entered_ts": None, "history": []}


def current_phase(project):
    return state(project).get("phase", PHASES[0])


def _save(project, doc):
    _state_path(project).write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return doc


def next_phase(phase):
    i = phase_index(phase)
    if i < 0:
        raise ValueError(f"unknown phase {phase!r}")
    return PHASES[i + 1] if i + 1 < len(PHASES) else None


# --- evidence readers --------------------------------------------------------
def load_metrics(project_dir):
    """The measured numbers a graybox/QA task wrote, or {} when there are none."""
    path = Path(project_dir) / METRICS_FILE
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise GateFailure(f"{path} is not valid JSON: {exc}")
    return doc if isinstance(doc, dict) else {}


def _numeric_targets(bucket_a):
    """The entries of Bucket A that are numbers, so they can be checked."""
    out = {}
    if isinstance(bucket_a, dict):
        for k, v in bucket_a.items():
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                out[k] = float(v)
            elif isinstance(v, dict) and isinstance(v.get("value"), (int, float)):
                out[k] = float(v["value"])
    return out


def check_bucket_a(project_dir, *, tolerance=BUCKET_A_TOLERANCE):
    """Compare measured metrics against Bucket A's numeric entries.

    Returns (failures, checked_count). A target with no measurement is a
    failure: an unmeasured dimension is not a met dimension, and letting it
    pass is how a graybox gets promoted on four numbers out of eleven.
    """
    target = judge_loop.load_target(project_dir)
    wanted = _numeric_targets(target.get("bucket_a", {}))
    if not wanted:
        return [], 0
    metrics = load_metrics(project_dir)
    failures = []
    for key, want in wanted.items():
        if key not in metrics:
            failures.append(
                f"bucket A target {key!r}={want} has no measurement in "
                f"{METRICS_FILE}; a dimension nobody measured is not a "
                "dimension that was met")
            continue
        try:
            got = float(metrics[key])
        except (TypeError, ValueError):
            failures.append(f"{METRICS_FILE}: {key!r}={metrics[key]!r} is not a number")
            continue
        limit = abs(want) * tolerance
        if abs(got - want) > limit:
            failures.append(
                f"bucket A {key!r}: target {want}, measured {got} "
                f"(outside +/-{tolerance:.0%})")
    return failures, len(wanted)


def find_mesh_assets(project_dir):
    """Imported mesh files in the project — what phase 1 must NOT contain."""
    root = Path(project_dir)
    out = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in MESH_SUFFIXES:
            continue
        if ".godot" in path.parts or "addons" in path.parts:
            continue
        out.append(str(path.relative_to(root)))
    return sorted(out)


def latest_judged_round(project):
    """The highest round that carries at least one non-crashed verdict."""
    for n in reversed(compactor.rounds(project)):
        if judge_loop.verdicts(project, n):
            return n
    return 0


def judge_status(project, *, minimum=None):
    """(failures, detail) for the visual half of a gate."""
    minimum = config.STUDIO_JUDGE_PASS if minimum is None else minimum
    n = latest_judged_round(project)
    if not n:
        return ([f"no visual verdict has been recorded yet; render a round and "
                 f"run the judge (score >= {minimum} is required)"], {})
    vs = judge_loop.verdicts(project, n)
    scores = [v["score"] for v in vs if isinstance(v.get("score"), (int, float))]
    if not scores:
        return ([f"round {n} has verdict files but no usable scores"], {"round": n})
    worst = min(scores)
    detail = {"round": n, "scores": scores, "worst": worst,
              "judges": [v.get("model") for v in vs]}
    failures = []
    if worst < minimum:
        failures.append(
            f"visual judge score {worst} (round {n}) is below the "
            f"{minimum} required to promote")
    # Oscillation is a gate failure in its own right: promoting a phase whose
    # judges cannot agree carries the argument forward into a phase where it
    # is more expensive to settle.
    arb = judge_loop.assess(project)
    if arb.halt:
        failures.append(f"judges are oscillating: {arb.reason}")
        detail["arbitration"] = arb.to_dict()
    return failures, detail


def latest_fuzz_report(project):
    d = config.studio_run_dir(project)
    reports = sorted(d.glob("fuzz-*.json"))
    if not reports:
        return None
    try:
        return json.loads(reports[-1].read_text(encoding="utf-8"))
    except ValueError:
        return None


# --- the gates ---------------------------------------------------------------
def gate_phase_0(project, project_dir):
    failures = []
    try:
        target = judge_loop.load_target(project_dir)
    except (FileNotFoundError, ValueError) as exc:
        return [str(exc)], {}
    if not target.get("bucket_a"):
        failures.append(
            "bucket_a is empty: phase 0 must produce the EXACT geometry "
            "targets (cell dimensions, corridor width, vent bore, door "
            "clearances) that every later phase is measured against")
    if not target.get("bucket_b"):
        failures.append(
            "bucket_b is empty: phase 0 must produce the atmosphere target "
            "(palette, light temperature, wear, mood) the visual judge scores")
    nums = _numeric_targets(target.get("bucket_a", {}))
    if not nums:
        failures.append(
            "bucket_a contains no NUMERIC targets, so nothing in it can be "
            "checked without a model. Give the measurable facts as numbers "
            "(e.g. \"corridor_width_m\": 2.4)")
    return failures, {"bucket_a_numeric": len(nums)}


def gate_phase_1(project, project_dir):
    failures, detail = [], {}
    meshes = find_mesh_assets(project_dir)
    if meshes:
        failures.append(
            "phase 1 is primitives only, but the project contains mesh "
            f"assets: {meshes[:8]}{'...' if len(meshes) > 8 else ''}. "
            "Modelled assets belong to phase 2 — the point of graybox is to "
            "prove the spatial design before any of it is expensive to change")
    detail["mesh_assets"] = len(meshes)
    a_fail, a_count = check_bucket_a(project_dir)
    failures += a_fail
    detail["bucket_a_checked"] = a_count
    j_fail, j_detail = judge_status(project)
    failures += j_fail
    detail["judge"] = j_detail
    return failures, detail


def gate_phase_2(project, project_dir):
    failures, detail = [], {}
    meshes = find_mesh_assets(project_dir)
    if not meshes:
        failures.append(
            "phase 2 produces modelled assets, but the project contains no "
            "mesh files at all")
    detail["mesh_assets"] = len(meshes)
    reports = sorted(config.studio_run_dir(project).glob("mesh-*.json"))
    if not reports:
        failures.append(
            "no mesh reports found. Every phase-2 asset must be measured by "
            "studio.engine.operators.astra_operator.verify_mesh_metrics — a "
            "triangle budget nobody measured is not a budget")
    over = []
    for r in reports:
        try:
            doc = json.loads(r.read_text(encoding="utf-8"))
        except ValueError:
            continue
        budget = doc.get("max_triangle_count") or 0
        tris = doc.get("triangles") or 0
        if budget and tris > budget:
            over.append(f"{doc.get('model_path', r.name)}: {tris} > {budget}")
        if doc.get("non_manifold_edges"):
            failures.append(
                f"{doc.get('model_path', r.name)}: "
                f"{doc['non_manifold_edges']} non-manifold edges")
    if over:
        failures.append("triangle budget exceeded: " + "; ".join(over[:6]))
    detail["mesh_reports"] = len(reports)
    j_fail, j_detail = judge_status(project)
    failures += j_fail
    detail["judge"] = j_detail
    return failures, detail


def gate_phase_3(project, project_dir):
    failures, detail = [], {}
    j_fail, j_detail = judge_status(project)
    failures += j_fail
    detail["judge"] = j_detail
    n = latest_judged_round(project)
    blocking = []
    for v in judge_loop.verdicts(project, n) if n else []:
        for art in v.get("artifacts") or []:
            if art.get("severity") == "high":
                blocking.append(f"{art.get('camera')}: {art.get('kind')} — {art.get('note')}")
    if blocking:
        failures.append(
            "high-severity visual artifacts are unresolved: "
            + "; ".join(blocking[:6]))
    detail["high_severity"] = len(blocking)
    return failures, detail


def gate_phase_4(project, project_dir):
    failures, detail = [], {}
    report = latest_fuzz_report(project)
    if not report:
        failures.append(
            "no fuzz report found. Phase 4 is proven by the bot swarm "
            "(studio.qa.deepseek_fuzzer), not by the absence of complaints")
        return failures, detail
    detail["report"] = {k: report.get(k) for k in
                        ("bots", "seconds", "authority_violations",
                         "desyncs", "crashes", "tick_p95_ms")}
    if report.get("authority_violations"):
        failures.append(
            f"{report['authority_violations']} client actions were accepted "
            "that the server should have rejected — the server is not "
            "authoritative")
    if report.get("desyncs"):
        failures.append(f"{report['desyncs']} state desyncs observed")
    if report.get("crashes"):
        failures.append(f"the server crashed {report['crashes']} time(s) under fuzz")
    return failures, detail


GATES = {
    PHASE_0_TARGET_GROUNDING: gate_phase_0,
    PHASE_1_GRAYBOX_PROTOTYPING: gate_phase_1,
    PHASE_2_3D_ASSET_AND_ANIMATION: gate_phase_2,
    PHASE_3_ATMOSPHERE_LIGHTING: gate_phase_3,
    PHASE_4_NETWORKED_QA: gate_phase_4,
}


def check(project, project_dir, phase=None):
    """Run the gate for `phase` (default: the current one).

    Returns {"phase", "passed", "failures", "detail"}. Never raises for a
    failing gate — a failure is the answer, not an error.
    """
    phase = phase or current_phase(project)
    gate = GATES.get(phase)
    if gate is None:
        raise ValueError(f"unknown phase {phase!r}")
    try:
        failures, detail = gate(project, project_dir)
    except GateFailure as exc:
        failures, detail = [str(exc)], {}
    result = {"phase": phase, "passed": not failures,
              "failures": failures, "detail": detail,
              "intent": PHASE_INTENT.get(phase, "")}
    events.emit("studio.gate", project=str(project), phase=phase,
                passed=result["passed"], failures=len(failures))
    return result


def promote(project, project_dir, *, force=False, reason=""):
    """Advance to the next phase, if this one's gate passes.

    `force` records an operator override rather than pretending the gate
    passed — an overridden promotion stays visible in the history forever,
    because the next person to ask "how did a phase-1 defect reach phase 3"
    deserves an answer.
    """
    phase = current_phase(project)
    nxt = next_phase(phase)
    if nxt is None:
        raise ValueError(f"{phase} is the final phase; there is nothing to promote to")
    result = check(project, project_dir, phase)
    if not result["passed"] and not force:
        return {"promoted": False, "from": phase, "to": nxt, **result}
    doc = state(project)
    doc.setdefault("history", []).append({
        "from": phase, "to": nxt, "ts": time.time(),
        "forced": bool(force and not result["passed"]),
        "reason": reason,
        "failures_at_promotion": result["failures"],
    })
    doc["phase"] = nxt
    doc["entered_ts"] = time.time()
    _save(project, doc)
    compactor.reset_baseline(
        project, phase=nxt,
        reason=f"promoted from {phase}" + (" (forced)" if force else ""))
    events.emit("studio.phase_promoted", project=str(project), **{"from": phase},
                to=nxt, forced=bool(force and not result["passed"]))
    return {"promoted": True, "from": phase, "to": nxt, **result}


def describe(project, project_dir):
    """Everything an operator needs to see about where a project stands."""
    phase = current_phase(project)
    result = check(project, project_dir, phase)
    return {
        "project": str(project),
        "phase": phase,
        "phase_number": phase_index(phase),
        "intent": PHASE_INTENT.get(phase, ""),
        "next_phase": next_phase(phase),
        "gate": result,
        "rounds": compactor.rounds(project),
        "baseline": compactor.baseline(project),
        "history": state(project).get("history", []),
    }
