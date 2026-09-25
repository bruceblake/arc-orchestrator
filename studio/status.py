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
                          "feature": t.get("feature", ""),
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
                       "approval": "pending",
                       "triangles": m.get("triangles"),
                       "budget": m.get("max_triangle_count") or None,
                       "over": bool(m.get("over_budget_by")),
                       "non_manifold": m.get("non_manifold_edges"),
                       "materials": m.get("material_count"),
                       "bones": ((m.get("skeleton") or {}).get("bone_count")),
                       "actions": m.get("action_count")})
    from studio import approvals
    states = approvals.status_of(project, [m["asset"] for m in meshes])
    notes = approvals.load(project)
    for m in meshes:
        m["approval"] = states.get(m["asset"], "pending")
        m["approval_note"] = (notes.get(m["asset"]) or {}).get("note", "")
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


# --- the gauntlet, per task ---------------------------------------------------
_TAIL_BYTES = 4 * 1024 * 1024


def _event_tail():
    """The newest few MB of the event log: enough for a live project's trail
    without reading a 100MB log on every dashboard poll."""
    path = Path(config.EVENTS_LOG)
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - _TAIL_BYTES))
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = data.splitlines()
    return lines[1:] if size > _TAIL_BYTES else lines


def gauntlet(task_ids):
    """Every check each task has been through: the "checks from every angle".

    implement attempts, gate passes/fails, critic verdicts, PR rounds and
    escalations, from the events the pipeline already emits. The board shows
    this so a task that merged on the first try and one that fought through
    eleven fix rounds do not look the same.
    """
    want = {i: {"attempts": 0, "gate_pass": 0, "gate_fail": 0, "review_pass": 0,
                "review_fail": 0, "pr_rounds": 0, "pr_rejects": 0,
                "escalations": 0, "manual": ""} for i in task_ids if i}
    if not want:
        return want
    rounds = {i: set() for i in want}
    for line in _event_tail():
        if '"task' not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        t = str(e.get("task") or e.get("module") or "")
        base = re.sub(r"-x\d+$", "", t)
        g = want.get(base)
        if g is None:
            continue
        kind = e.get("type", "")
        if kind == "task.gate":
            g["gate_pass" if e.get("passed") else "gate_fail"] += 1
        elif kind == "task.reviewed":
            g["review_pass" if e.get("passed", e.get("pass")) else "review_fail"] += 1
        elif kind == "task.pr_reviewed":
            g["pr_rounds"] += 1
            if not e.get("approved") and not e.get("inconclusive"):
                g["pr_rejects"] += 1
        elif kind == "task.escalated":
            g["escalations"] += 1
        elif kind == "task.pr_manual":
            g["manual"] = e.get("decision", "")
        elif kind == "task.pr_awaiting_manual":
            g["manual"] = "awaiting"
        elif kind == "driver.start" and e.get("role") == "implementer":
            # A fix ROUND, not a spawn: one round's harness may be retried many
            # times on a transient error, and counting each retry made a task
            # look like it had been attempted two hundred times.
            rounds[base].add(t)
    for i, g in want.items():
        g["attempts"] = len(rounds[i])
    return want


# --- features: the board, the roadmap, the changelog --------------------------
KANBAN = ("backlog", "planned", "building", "review", "done", "blocked")
_COLUMN = {"pending": "planned", "running": "building", "in_review": "review",
           "merged": "done", "done": "done", "failed": "blocked",
           "conflict": "blocked", "skipped": "blocked"}


def roadmap(repo):
    """Planned features from the game repo's studio_roadmap.json."""
    try:
        doc = json.loads((Path(repo) / "studio_roadmap.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [f for f in doc.get("features") or [] if isinstance(f, dict) and f.get("id")]


def kanban(boards, features):
    """Cards in columns: tasks by status, and roadmap features not yet planned."""
    cols = {c: [] for c in KANBAN}
    planned_features = set()
    for b in boards:
        for t in b["tasks"]:
            if t.get("feature"):
                planned_features.add(t["feature"])
            cols[_COLUMN.get(t["status"], "planned")].append({
                "kind": "task", "id": t["id"], "title": t["title"],
                "phase": b.get("phase", ""), "model": t.get("model"),
                "feature": t.get("feature", ""), "live": t.get("live"),
                "live_role": t.get("live_role", ""), "gauntlet": t.get("gauntlet"),
                "error": t.get("error", "")})
    for f in features:
        if f["id"] not in planned_features:
            cols["backlog"].append({"kind": "feature", "id": f["id"],
                                    "title": f.get("title") or f["id"],
                                    "phase": f.get("phase", "")})
    return cols


def changelog(repo, limit=40):
    """What shipped: merged task commits on the game repo's main, newest first."""
    if not repo or not (Path(repo) / ".git").exists():
        return []
    try:
        import subprocess
        # What GitHub merged, when the local branch has not caught up yet:
        # origin/main is only as fresh as the last fetch, but it is never
        # BEHIND a merge the fleet made, which the local main can be.
        ref = "main"
        if subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor",
                           "main", "origin/main"], capture_output=True,
                          timeout=10, env=config.child_env()).returncode == 0:
            ref = "origin/main"
        out = subprocess.run(
            ["git", "-C", str(repo), "log", ref, f"-n{limit}",
             "--date=iso-strict", "--format=%H%x1f%ad%x1f%s%x1f%b%x1e"],
            capture_output=True, text=True, timeout=20, env=config.child_env()).stdout
        remote = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"],
                                capture_output=True, text=True, timeout=10,
                                env=config.child_env()).stdout.strip()
    except (OSError, ValueError):
        return []
    web = ""
    m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", remote)
    if m:
        web = f"https://github.com/{m.group(1)}"
    entries = []
    for rec in out.split("\x1e"):
        parts = rec.strip("\n").split("\x1f")
        if len(parts) < 3:
            continue
        sha, date, subject = parts[0], parts[1], parts[2]
        body = parts[3] if len(parts) > 3 else ""
        tm = re.match(r"task\(([^)]+)\):\s*(.*?)(?:\s*\(#(\d+)\))?$", subject)
        trailers = dict(re.findall(r"(?m)^(Model|Reviewer|Harness):\s*(.+)$", body))
        pr = tm.group(3) if tm else (re.search(r"\(#(\d+)\)$", subject) or [None, None])[1]
        entries.append({
            "sha": sha[:7], "date": date, "task": tm.group(1) if tm else "",
            "title": tm.group(2) if tm else subject,
            "pr": int(pr) if pr else None,
            "pr_url": f"{web}/pull/{pr}" if (web and pr) else "",
            "model": trailers.get("Model", ""), "reviewer": trailers.get("Reviewer", "")})
    return entries


def evidence(project, repo):
    """The latest playtest, perf and palette readings, for the Studio view."""
    from studio.engine import stage_manager
    out = {}
    pt = stage_manager._read_json(Path(repo) / stage_manager.PLAYTEST_REPORT) if repo else None
    if pt:
        out["playtest"] = {"passed": pt.get("passed"), "seconds": pt.get("seconds"),
                           "checks": [c for c in pt.get("checks") or [] if isinstance(c, dict)][:30],
                           "screenshots": len(pt.get("screenshots") or [])}
    pf = stage_manager._read_json(Path(repo) / stage_manager.PERF_REPORT) if repo else None
    if pf:
        out["perf"] = {k: pf.get(k) for k in ("fps_avg", "fps_p5", "frame_ms_p95",
                                               "draw_calls", "shadow_lights", "renderer")}
        out["perf"]["min_fps"] = config.STUDIO_MIN_FPS
        out["perf"]["max_shadow_lights"] = config.STUDIO_MAX_SHADOW_LIGHTS
    try:
        _f, pal = stage_manager.palette_status(project, repo) if repo else ([], {})
        if pal.get("shares"):
            out["palette"] = {"round": pal.get("round"), "shares": pal["shares"],
                              "min": config.STUDIO_PALETTE_MIN}
    except Exception:                                        # noqa: BLE001
        pass
    return out


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


def _agent_thread(repo, limit=8):
    """Recent shared-board posts for this game repo.

    The name is the repo directory only — never a path — and session ids stay
    out of the payload. Ownership is the harness that may resume, when a post
    recorded one.
    """
    if not repo:
        return []
    name = Path(repo).name
    if not name or name in (".", ".."):
        return []
    try:
        import board
        rows = board.project_recent(name, limit)
    except Exception:                                        # noqa: BLE001
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = {
            "task": str(row.get("task") or ""),
            "role": str(row.get("role") or ""),
            "model": str(row.get("model") or ""),
            "harness": str(row.get("harness") or ""),
            "kind": str(row.get("kind") or ""),
            "body": str(row.get("body") or ""),
            "timestamp": row.get("ts"),
        }
        if row.get("session_id"):
            item["session_owner"] = item["harness"]
        out.append(item)
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
    boards = _task_board(project, store, live_tasks)
    trail = gauntlet([t["id"] for b in boards for t in b["tasks"]])
    for b in boards:
        for t in b["tasks"]:
            t["gauntlet"] = trail.get(t["id"])
    feats = roadmap(repo) if repo else []
    try:
        # Human playtesting (studio/playtest.py). Its snapshot already never
        # raises; the guard is for the import, so a broken module costs the
        # Playtest tab and not the whole Studio view.
        from studio import playtest
        human = playtest.snapshot(project)
    except Exception as exc:                                 # noqa: BLE001
        human = {"error": f"{type(exc).__name__}: {exc}"[:500]}
    return {
        "name": project, "repo": repo, "phase": phase,
        "phase_label": PHASE_LABELS.get(phase, phase),
        "phases": _phases(phase), "entered_ts": state.get("entered_ts"),
        "history": state.get("history") or [],
        "gate": gate, "metrics": _metrics(repo),
        "boards": boards, "thread": _agent_thread(repo),
        "kanban": kanban(boards, feats), "roadmap": feats,
        "changelog": changelog(repo), "evidence": evidence(project, repo),
        "rounds": _rounds(project), "baseline": compactor.baseline(project),
        "workbench": _previews(project), "arbitration": arb,
        "fuzz": ({k: fuzz.get(k) for k in ("bots", "seconds", "messages_sent",
                                            "authority_violations", "desyncs",
                                            "crashes", "tick_p50_ms", "tick_p95_ms",
                                            "server_unreachable", "ts")}
                 if fuzz else None),
        "playtest": human,
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
