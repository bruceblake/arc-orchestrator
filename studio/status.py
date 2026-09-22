"""Everything the dashboard's Studio view shows, in one read-only snapshot.

The Studio view is the "workbench" that published AI game-dev workflows keep
arriving at (2026-09): one page, updated live, where the operator watches
assets and scenes come together and steers before tokens are wasted on the
wrong thing. This module assembles that page's data; dashboard.py only routes
to it.

Two rules shape it:

  READ-ONLY. Nothing here writes state, starts work, or emits events. The view
  polls every few seconds, so a snapshot that ran a gate "for real" — events,
  side effects — would flood the log the operator is trying to read.
  stage_manager.check is called with emit=False for exactly that reason.

  CONTAINED. The only files this module will hand back are images inside the
  studio directory of a named project (image_path). The dashboard has no
  authentication (AGENTS.md Rule 6b), so a route that returned an arbitrary
  path from a query string would be a file-read primitive for anyone on the
  LAN.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import config

_PROJECT_RE = re.compile(r"[A-Za-z0-9._-]{1,80}")
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")

PHASE_LABELS = {
    "PHASE_0_TARGET_GROUNDING": "Target",
    "PHASE_1_GRAYBOX_PROTOTYPING": "Graybox",
    "PHASE_2_3D_ASSET_AND_ANIMATION": "Assets & animation",
    "PHASE_3_ATMOSPHERE_LIGHTING": "Lighting",
    "PHASE_4_NETWORKED_QA": "Networked QA",
}


# --- discovery ----------------------------------------------------------------
def projects():
    """Studio projects: every studio run directory that has phase state."""
    root = Path(config.STUDIO_DIR)
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir()
                  if d.is_dir() and (d / "stage.json").exists())


def _taskfiles_for(project):
    """[(path, project-dict)] for every taskfile whose project.name matches."""
    out = []
    tdir = Path(config.TASKS_DIR)
    for f in sorted(tdir.glob("*.json")) if tdir.is_dir() else []:
        try:
            proj = json.loads(f.read_text(encoding="utf-8", errors="replace")).get("project") or {}
        except (OSError, ValueError):
            continue
        if proj.get("name") == project:
            out.append((f, proj))
    return out


def repo_for(project):
    """The game repo a studio project builds, from its taskfiles."""
    for _f, proj in _taskfiles_for(project):
        if proj.get("repo"):
            return proj["repo"]
    guess = Path.home() / "repos" / project
    return str(guess) if guess.is_dir() else ""


def _phase_of_taskfile(path):
    stem = Path(path).stem.lower()
    for phase in PHASE_LABELS:
        if phase.lower() in stem:
            return phase
    return ""


# --- the pieces ---------------------------------------------------------------
def _phases(current):
    from studio.schemas.task import PHASES
    ci = PHASES.index(current) if current in PHASES else 0
    return [{"id": p, "label": PHASE_LABELS.get(p, p), "n": i,
             "state": "done" if i < ci else "current" if i == ci else "todo"}
            for i, p in enumerate(PHASES)]


def _metrics(repo):
    """Bucket A targets against what was actually measured."""
    from studio.engine import stage_manager
    rows = []
    if not repo:
        return rows
    try:
        target = json.loads((Path(repo) / "studio_target.json").read_text())
    except (OSError, ValueError):
        return rows
    measured = stage_manager.load_metrics(repo) if Path(repo).is_dir() else {}
    for key, want in stage_manager._numeric_targets(target.get("bucket_a", {})).items():
        got = measured.get(key)
        ok = None
        if isinstance(got, (int, float)):
            ok = abs(got - want) <= abs(want) * stage_manager.BUCKET_A_TOLERANCE
        rows.append({"key": key, "target": want, "measured": got, "ok": ok})
    return rows


def _task_board(project, store, live_tasks):
    rows_by_id = {}
    try:
        for r in (store.code_tasks_all() if store else []):
            rows_by_id.setdefault(r.get("id"), []).append(r)
    except Exception:                                        # noqa: BLE001
        pass
    try:
        import reconcile
        runs = {Path(r["taskfile"]).name: r.get("pid")
                for r in reconcile.live_runs() if r.get("taskfile")}
    except Exception:                                        # noqa: BLE001
        runs = {}
    boards = []
    for path, proj in _taskfiles_for(project):
        tasks = []
        for t in proj.get("tasks") or []:
            tid = t.get("id", "")
            mine = [r for r in rows_by_id.get(tid, [])
                    if str(r.get("taskfile", "")).endswith(path.name)]
            row = mine[-1] if mine else {}
            status = row.get("status") or "pending"
            role = live_tasks.get(tid) if tid in live_tasks else None
            if role is not None and status in ("pending", "failed", "running"):
                # What is ACTUALLY happening: a task under cross-family review
                # is not "running" in the sense an operator reads it.
                status = ("in_review" if role in ("reviewer", "pr_reviewer")
                          else "running")
            tasks.append({"id": tid, "title": t.get("title") or tid,
                          "model": row.get("model") or t.get("model"),
                          "reviewer": row.get("reviewer") or t.get("reviewer"),
                          "deps": t.get("deps") or [], "status": status,
                          "live": tid in live_tasks, "live_role": role or "",
                          "error": (row.get("error") or "")[:300]})
        done = sum(1 for t in tasks if t["status"] in ("merged", "done"))
        boards.append({"file": path.name, "phase": _phase_of_taskfile(path),
                       "run_pid": runs.get(path.name), "tasks": tasks,
                       "done": done, "total": len(tasks)})
    return boards


def _rounds(project, keep=3):
    from studio.evaluation import judge_loop
    from studio.memory import compactor
    out = []
    for n in compactor.rounds(project)[-keep:]:
        meta = compactor.round_meta(project, n) or {}
        images = [{"name": i.get("name"), "kind": i.get("kind") or "",
                   "note": i.get("note") or "",
                   "url": _image_url(project, i.get("path", ""))}
                  for i in meta.get("images") or []]
        verdicts = [{"model": v.get("model"), "score": v.get("score"),
                     "a": v.get("bucket_a_score"), "b": v.get("bucket_b_score"),
                     "pass": v.get("pass"), "validation": bool(v.get("validation")),
                     "summary": v.get("summary", ""),
                     "artifacts": v.get("artifacts") or [],
                     "directives": (v.get("directives") or [])[:8]}
                    for v in judge_loop.verdicts(project, n, include_validation=True)]
        out.append({"round": n, "phase": meta.get("phase", ""), "images": images,
                    "verdicts": verdicts})
    return list(reversed(out))


def _previews(project, limit=12):
    """The asset workbench: operator renders and mesh reports, newest first."""
    d = config.studio_run_dir(project)
    shots = sorted((d / "previews").glob("*.png"), key=lambda p: p.stat().st_mtime,
                   reverse=True)[:limit] if (d / "previews").is_dir() else []
    meshes = []
    for r in sorted(d.glob("mesh-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            m = json.loads(r.read_text())
        except (OSError, ValueError):
            continue
        meshes.append({"asset": Path(m.get("model_path", r.stem)).name,
                       "triangles": m.get("triangles"),
                       "budget": m.get("max_triangle_count") or None,
                       "over": bool(m.get("over_budget_by")),
                       "non_manifold": m.get("non_manifold_edges"),
                       "materials": m.get("material_count"),
                       "bones": ((m.get("skeleton") or {}).get("bone_count")),
                       "actions": m.get("action_count")})
    return {"renders": [{"name": p.stem, "url": _image_url(project, str(p))} for p in shots],
            "meshes": meshes}


def _image_url(project, path):
    try:
        rel = Path(path).resolve().relative_to(config.studio_run_dir(project).resolve())
    except (ValueError, OSError):
        return ""
    return f"/api/studio/image?project={project}&path={rel.as_posix()}"


def image_path(project, rel):
    """Resolve an image request to a file, or None. Never escapes the project."""
    if not project or not _PROJECT_RE.fullmatch(project) or not rel:
        return None
    base = config.studio_run_dir(project).resolve()
    try:
        target = (base / rel).resolve()
        target.relative_to(base)
    except (ValueError, OSError):
        return None
    if target.suffix.lower() not in _IMAGE_SUFFIXES or not target.is_file():
        return None
    return target


def plan_windows():
    """How much of the Claude plan's rolling windows the fleet has used.

    Claude Code reports its own rate-limit state in every headless transcript
    (`rate_limit_event`), so the newest one is the freshest reading there is.
    This matters because the operator's own interactive session draws on the
    same plan: when the five-hour window is spent there is no overage.
    """
    tdir = Path(config.ROOT) / "logs" / "harness"
    if not tdir.is_dir():
        return None
    files = sorted(tdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:40]
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if '"rate_limit_event"' not in text:
            continue
        for line in reversed(text.splitlines()):
            if '"rate_limit_event"' not in line:
                continue
            try:
                info = json.loads(line).get("rate_limit_info") or {}
            except ValueError:
                continue
            wins = info.get("unifiedWindows") or {}
            return {"source": "claude", "status": info.get("status"),
                    "overage": info.get("overageStatus"),
                    "windows": {k: {"used": v.get("utilization"),
                                    "resets_at": v.get("resetsAt")}
                                for k, v in wins.items()},
                    "as_of": f.stat().st_mtime}
    return None


def _roster():
    out = []
    for m in sorted(config.MODEL_ROLES):
        h = config.MODEL_HARNESS.get(m, "")
        out.append({"model": m, "harness": h, "family": config.MODEL_FAMILY.get(m),
                    "roles": sorted(config.MODEL_ROLES[m]),
                    "cap": config.driver_limit(m),
                    "harness_cap": config.harness_limit(h) if h else None,
                    "external": m in config.EXTERNAL_MODELS})
    return out


# --- the snapshot -------------------------------------------------------------
def project_snapshot(project, store=None, live_tasks=()):
    from studio.engine import stage_manager
    from studio.evaluation import judge_loop
    from studio.memory import compactor
    live_tasks = dict(live_tasks) if isinstance(live_tasks, dict) else {t: "" for t in live_tasks}
    repo = repo_for(project)
    state = stage_manager.state(project)
    phase = state.get("phase")
    gate = None
    if repo and Path(repo).is_dir():
        try:
            gate = stage_manager.check(project, repo, phase, emit=False)
        except Exception as exc:                             # noqa: BLE001
            gate = {"phase": phase, "passed": False,
                    "failures": [f"gate could not run: {exc}"], "detail": {}}
    try:
        arb = judge_loop.assess(project).to_dict()
    except Exception:                                        # noqa: BLE001
        arb = None
    fuzz = stage_manager.latest_fuzz_report(project)
    return {
        "name": project, "repo": repo, "phase": phase,
        "phase_label": PHASE_LABELS.get(phase, phase),
        "phases": _phases(phase), "entered_ts": state.get("entered_ts"),
        "history": state.get("history") or [],
        "gate": gate, "metrics": _metrics(repo),
        "boards": _task_board(project, store, live_tasks),
        "rounds": _rounds(project), "baseline": compactor.baseline(project),
        "workbench": _previews(project), "arbitration": arb,
        "fuzz": ({k: fuzz.get(k) for k in ("bots", "seconds", "messages_sent",
                                            "authority_violations", "desyncs",
                                            "crashes", "tick_p50_ms", "tick_p95_ms",
                                            "server_unreachable", "ts")}
                 if fuzz else None),
    }


def snapshot(store=None, live_tasks=()):
    from studio import budget
    names = projects()
    return {
        "ts": time.time(),
        "fleet": config.FLEET, "studio_active": config.STUDIO,
        "planner": config.PLANNER_MODEL,
        "openai_model": getattr(config, "STUDIO_OPENAI_MODEL", None),
        "codex_effort": getattr(config, "CODEX_REASONING_EFFORT", None),
        "roster": _roster(),
        "spend": budget.summary(),
        "plan": plan_windows(),
        "projects": [project_snapshot(n, store, live_tasks) for n in names],
    }
