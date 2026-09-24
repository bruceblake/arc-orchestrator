"""Bounded Studio autopilot: plan one current-phase feature, then queue a run.

Opt in with ARC_STUDIO_AUTOPILOT=1. The dashboard starts `start()` beside the
captain drain. One tick is cheap except for a single `studio.planner.plan`
call, and that call happens only when the planner seat has headroom and an
implementer seat is idle. Existing taskfiles are filled first.

Nothing here verifies, reviews, or merges. Dispatch is `captain.enqueue_run`,
which launches `main.py code run` — the same governed pipeline as a hand launch.
"""
from __future__ import annotations

import json
import hashlib
import fcntl
import os
import re
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import config
import events

INTERVAL_S = 20.0
BACKOFF_S = 300.0
MAX_RESUMES = 4
# A plan interrupted by a dashboard restart is not retried immediately.
# The file lock below closes the concurrent-process race.
PLAN_STALE_S = 3600.0
_TERMINAL_OK = ("merged", "done", "skipped")
_PERMANENT = "exhausted escalation"

_started = False
_started_lock = threading.Lock()


def enabled():
    return bool(getattr(config, "STUDIO_AUTOPILOT", False))


def _state_path():
    return Path(config.STUDIO_DIR) / "autopilot-state.json"


def _load_state():
    try:
        doc = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    doc.setdefault("backoff", {})
    doc.setdefault("resumes", {})
    doc.setdefault("noted", {})
    doc.setdefault("dispatched", {})
    return doc


def _save_state(doc):
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a+b") as claim:
        fcntl.flock(claim, fcntl.LOCK_EX)
        previous = _load_state()
        for key in ("backoff", "resumes"):
            current = doc.setdefault(key, {})
            for name, record in previous[key].items():
                metric = "until" if key == "backoff" else "n"
                if record.get(metric, 0) > current.get(name, {}).get(metric, 0):
                    current[name] = record
        doc["noted"] = {**previous["noted"], **doc.get("noted", {})}
        dispatched = doc.setdefault("dispatched", {})
        for name, ts in (previous.get("dispatched") or {}).items():
            if float(ts or 0) > float(dispatched.get(name) or 0):
                dispatched[name] = ts
        fd, temp_name = tempfile.mkstemp(prefix=".autopilot-state-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(doc, stream, indent=2)
            os.replace(temp_name, path)
        finally:
            Path(temp_name).unlink(missing_ok=True)


def _slug(text):
    source = str(text)
    raw = "".join(c if c.isalnum() or c in "-_" else "-" for c in source)
    stem = raw.strip("-")[:80] or "feature"
    if stem != source:
        stem += "-" + hashlib.sha256(source.encode()).hexdigest()[:8]
    return stem


def taskfile_path(project, feature_id):
    """Stable path for one roadmap feature. Never the phase-wide default."""
    name = f"studio-{_slug(project)}-{_slug(feature_id)}.json"
    return Path(config.TASKS_DIR) / name


def _plan_key(project, feature_id):
    return f"plan:{project}:{feature_id}"


def _note(doc, kind, body, **fields):
    """Emit once per distinct decision so a 20s loop does not flood the log."""
    key = kind + ":" + str(fields.get("project", "")) + ":" + str(
        fields.get("feature") or fields.get("taskfile") or "") + ":" + body[:80]
    if doc["noted"].get(key) == body:
        return
    doc["noted"][key] = body
    events.emit("studio.autopilot." + kind, **fields, detail=body[:300])
    try:
        import board
        board.post(config.ROOT, task="studio-autopilot", role="autopilot",
                   model=config.PLANNER_MODEL or "", harness="dashboard",
                   kind="note", body=body[:400],
                   project=fields.get("project") or "studio")
    except Exception:                                          # noqa: BLE001
        pass


def _backing_off(doc, key, now):
    rec = doc["backoff"].get(key) or {}
    return float(rec.get("until") or 0) > now


def _backoff(doc, key, reason, now, *, permanent=False):
    rec = doc["backoff"].setdefault(key, {"n": 0})
    rec["n"] = int(rec.get("n") or 0) + 1
    rec["reason"] = reason[:300]
    if permanent:
        rec["until"] = now + 10 * 365 * 24 * 3600
        rec["permanent"] = True
    else:
        rec["until"] = now + BACKOFF_S * (2 ** min(rec["n"] - 1, 6))
    return rec


def _repo(project):
    from studio import status
    from studio.engine import stage_manager
    repo = status.repo_for(project)
    if repo:
        return repo
    return str(stage_manager.state(project).get("repo") or "")


def _rows(store, path):
    if store is None:
        return []
    resolved = str(Path(path).resolve())
    name = Path(path).name
    try:
        rows = store.code_tasks_for(resolved) or store.code_tasks_for(name)
    except Exception:                                          # noqa: BLE001
        return []
    return list(rows or [])


def _permanent(rows):
    """True when every unfinished task already exhausted escalation."""
    if not rows:
        return False
    open_rows = [r for r in rows if r.get("status") not in _TERMINAL_OK]
    if not open_rows:
        return False
    return all(r.get("status") == "failed"
               and _PERMANENT in str(r.get("error") or "") for r in open_rows)


def _complete(tasks, rows):
    if not tasks or not rows:
        return False
    by_id = {r.get("id"): r for r in rows}
    return all(by_id.get(t.get("id"), {}).get("status") in _TERMINAL_OK
               for t in tasks)


def _idle_implementers(cap):
    idle = []
    for model in sorted(config.IMPLEMENTER_MODELS):
        if cap.get(model, {}).get("batch_headroom", 0) > 0:
            idle.append(model)
    return idle


def _planner_free(cap, db_path=None, now=None):
    model = config.PLANNER_MODEL
    if not model:
        return False
    seat = cap.get(model, {})
    if seat.get("batch_headroom", 0) <= 0:
        return False
    harness = seat.get("harness")
    if harness:
        import captain
        import drivers
        used = captain._live_lease_counts(db_path).get(f"harness:{harness}", 0)
        if used >= (seat.get("harness_cap") or 0):
            return False
        if drivers._usage_blocked_until.get(harness, 0) > (now or time.time()):
            return False
        if harness == "claude":
            from studio import status
            window = status.plan_windows() or {}
            if window.get("as_of", 0) > (now or time.time()) - 5 * 3600:
                windows = list(window.get("windows", {}).values())
                active = [v for v in windows if _reset_future(
                    v.get("resets_at"), now or time.time())]
                rejected = window.get("overage") == "rejected" and (not windows or active)
                if rejected or any((v.get("used") or 0) >= 1 for v in active):
                    return False
    return True


def _reset_future(value, now):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() > now
    except (AttributeError, TypeError, ValueError):
        return False


def _queued(path, db_path):
    import captain
    canonical = str(Path(path).resolve())
    try:
        items = captain._run_queue(db_path).active(captain.RUN_TOPIC)
    except Exception:                                          # noqa: BLE001
        return False
    for it in items:
        if it.get("dedupe_key") == canonical:
            return True
    return False


def _enqueue(kind, path, repo, reason, models, db_path):
    import captain
    if captain._taskfile_live(path) or _queued(path, db_path):
        return False
    _id, created = captain.enqueue_run(
        kind, path, repo, reason, sorted(models), db_path=db_path)
    return bool(created)


def _named_features(project):
    from studio import status
    named = set()
    for _path, proj in status._taskfiles_for(project):
        for t in proj.get("tasks") or []:
            if t.get("feature"):
                named.add(t["feature"])
    return named


def _taskfile_phase(path, proj):
    """Phase named in a task prompt, else the phase encoded in the filename."""
    from studio import status
    from studio.schemas.task import phase_index
    found = []
    for task in proj.get("tasks") or []:
        if isinstance(task, dict):
            found.extend(re.findall(
                r"(?m)^PHASE: (PHASE_[A-Z0-9_]+)\s*$", task.get("prompt") or ""))
    if not found:
        return status._phase_of_taskfile(path)
    return max(found, key=lambda p: phase_index(p))


def _operator_stopped(path_key, doc):
    """True when `run.stopped` for this taskfile is newer than our last dispatch."""
    import fleetwatch
    since = float((doc.get("dispatched") or {}).get(path_key) or 0)
    try:
        stopped = fleetwatch._stopped_since(since)
    except Exception:                                          # noqa: BLE001
        return False
    return path_key in stopped


def _dispatch_existing(project, repo, phase, cap, idle, doc, store, db_path, now):
    """Enqueue unstarted runs and bounded resumes. Returns how many queued."""
    from studio import status
    from studio.schemas.task import phase_index
    import captain
    n = 0
    current_i = phase_index(phase)
    for path, proj in status._taskfiles_for(project):
        tasks = [t for t in (proj.get("tasks") or []) if isinstance(t, dict)]
        models = {t.get("model") for t in tasks if t.get("model")}
        rows = _rows(store, path)
        key = str(path.resolve())
        name = path.name
        if _complete(tasks, rows):
            continue
        tf_phase = _taskfile_phase(path, proj)
        tf_i = phase_index(tf_phase)
        if current_i >= 0 and tf_i > current_i:
            _note(doc, "stop",
                  f"not launching {name}; phase {tf_phase} is ahead of {phase}",
                  project=project, taskfile=name, reason="future-phase")
            continue
        if captain._taskfile_live(path) or _queued(path, db_path):
            _note(doc, "stop", f"leave {name} alone; a run is already live or queued",
                  project=project, taskfile=name, reason="duplicate")
            continue
        if _permanent(rows) or (doc["resumes"].get(key, {}).get("n") or 0) >= MAX_RESUMES:
            _backoff(doc, "task:" + key, "permanent failure", now, permanent=True)
            _note(doc, "stop", f"stop {name}: permanent failure, not resuming",
                  project=project, taskfile=name, reason="permanent")
            continue
        if _backing_off(doc, "task:" + key, now):
            _note(doc, "stop", f"backoff {name} after a failed dispatch",
                  project=project, taskfile=name, reason="backoff")
            continue
        if models and idle and not (models & set(idle)):
            continue
        if not idle:
            _note(doc, "stop", f"hold {name}; no idle implementer seat",
                  project=project, taskfile=name, reason="capacity")
            continue
        kind = "run" if not rows else "resume"
        if kind == "resume" and _operator_stopped(key, doc):
            _note(doc, "stop", f"stop {name}: operator ended the run",
                  project=project, taskfile=name, reason="operator-stop")
            continue
        if kind == "resume":
            rec = doc["resumes"].setdefault(key, {"n": 0})
            rec["n"] = int(rec.get("n") or 0) + 1
        if not _enqueue(kind, path, repo, "studio autopilot", models, db_path):
            continue
        doc.setdefault("dispatched", {})[key] = now
        _backoff(doc, "task:" + key, kind, now)
        _note(doc, "dispatch", f"{kind} {name}", project=project,
              taskfile=name, reason=kind)
        events.emit("studio.autopilot.queue", project=project, taskfile=name,
                    kind=kind)
        n += 1
    return n


def _candidate(project, phase, repo, doc, now):
    from studio import status
    from studio.schemas.task import phase_index
    named = _named_features(project)
    current_i = phase_index(phase)
    for feat in status.roadmap(repo):
        fid = feat.get("id")
        if not fid or fid in named:
            continue
        fphase = feat.get("phase") or ""
        if fphase != phase:
            # Future phases wait for promotion; other phases are not current.
            if current_i >= 0 and phase_index(fphase) > current_i:
                _note(doc, "stop",
                      f"not planning {fid}; phase {fphase} is ahead of {phase}",
                      project=project, feature=fid, reason="future-phase")
            continue
        path = taskfile_path(project, fid)
        if path.exists():
            _note(doc, "stop", f"not planning {fid}; {path.name} already exists",
                  project=project, feature=fid, reason="exists")
            continue
        if _backing_off(doc, _plan_key(project, fid), now):
            _note(doc, "stop", f"backoff planning {fid}",
                  project=project, feature=fid, reason="backoff")
            continue
        inflight = doc.get("inflight") or {}
        if inflight.get("feature") == fid and inflight.get("project") == project:
            age = now - float(inflight.get("started") or now)
            if age < PLAN_STALE_S:
                _note(doc, "stop", f"plan of {fid} already in flight",
                      project=project, feature=fid, reason="inflight")
                return None
        return feat
    return None


def _plan_one(project, phase, repo, feat, doc, db_path, now):
    fid = feat["id"]
    path = taskfile_path(project, fid)
    lock_path = Path(config.STUDIO_DIR) / "autopilot-plan.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as claim:
        try:
            fcntl.flock(claim, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _note(doc, "stop", f"plan of {fid} already claimed",
                  project=project, feature=fid, reason="claimed")
            return None
        if path.exists() or _backing_off(_load_state(), _plan_key(project, fid), now):
            return None
        return _plan_claimed(project, phase, repo, feat, doc, db_path, now, path)


def _validate_plan(path, project, repo, phase, fid):
    data = json.loads(path.read_text(encoding="utf-8"))
    body = data.get("project") or {}
    tasks = body.get("tasks") or []
    if (body.get("name") != project or
            Path(body.get("repo") or "").resolve() != Path(repo).resolve() or
            not tasks):
        raise ValueError("planned project, repo, or task list does not match")
    for task in tasks:
        prompt = task.get("prompt") or ""
        phases = re.findall(r"(?m)^PHASE: (PHASE_[A-Z0-9_]+)$", prompt)
        if task.get("feature") != fid or phases != [phase]:
            raise ValueError("planned task has a different feature or phase")


def _plan_claimed(project, phase, repo, feat, doc, db_path, now, path):
    from studio import planner
    fid = feat["id"]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".studio-plan-", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temp = Path(temp_name)
    doc["inflight"] = {"project": project, "feature": fid,
                       "path": str(path), "started": now}
    _save_state(doc)
    goal = (f"Plan only the current-phase roadmap feature {fid!r}: "
            f"{feat.get('title') or fid}. "
            f"Set \"feature\" to {fid!r} on every task. "
            f"Stay in {phase}. Do not plan a later phase.")
    try:
        planner.plan(goal, repo, phase=phase, project=project, out_path=temp)
        _validate_plan(temp, project, repo, phase, fid)
        os.link(temp, path)  # Fails if another process published this feature.
    except Exception as exc:                                  # noqa: BLE001
        doc["inflight"] = None
        _backoff(doc, _plan_key(project, fid), str(exc), now)
        _note(doc, "stop", f"plan {fid} failed: {exc}", project=project,
              feature=fid, reason="plan-failed")
        _save_state(doc)
        return None
    finally:
        temp.unlink(missing_ok=True)
    doc["inflight"] = None
    written = path
    _note(doc, "plan", f"planned {fid} -> {written.name}", project=project,
          feature=fid, taskfile=written.name, reason="planned")
    if written.is_file() and _enqueue("run", written, repo,
                                      f"studio autopilot planned {fid}", set(), db_path):
        events.emit("studio.autopilot.queue", project=project,
                    taskfile=written.name, feature=fid, kind="run")
    _save_state(doc)
    return written


def tick(db_path=None, now=None):
    """One pass. Returns a small summary. Never raises.

    Disabled deployments return immediately so the dashboard thread stays idle.
    """
    if not enabled():
        return {"enabled": False}
    now = time.time() if now is None else now
    summary = {"enabled": True, "planned": None, "dispatched": 0}
    try:
        summary.update(_tick(db_path, now))
    except Exception as exc:                                  # noqa: BLE001
        try:
            import errors
            errors.capture(exc, node="studio.autopilot")
        except Exception:                                     # noqa: BLE001
            pass
        events.emit("studio.autopilot.stop", reason="error",
                    detail=str(exc)[:300])
    return summary


def _tick(db_path, now):
    import captain
    from store import Store
    doc = _load_state()
    cap = captain.capacity_snapshot(db_path)
    idle = _idle_implementers(cap)
    store = None
    try:
        store = Store(db_path or config.DB_PATH)
    except Exception:                                         # noqa: BLE001
        store = None
    try:
        return _scan(doc, cap, idle, store, db_path, now)
    finally:
        if store is not None:
            try:
                store.conn.close()
            except Exception:                                 # noqa: BLE001
                pass


def _scan(doc, cap, idle, store, db_path, now):
    from studio import status
    from studio.engine import stage_manager
    dispatched = 0
    planned = None
    for project in status.projects():
        phase = stage_manager.current_phase(project)
        repo = _repo(project)
        if not repo:
            _note(doc, "stop", f"{project} has no game repo",
                  project=project, reason="no-repo")
            continue
        dispatched += _dispatch_existing(
            project, repo, phase, cap, idle, doc, store, db_path, now)
        if planned is not None:
            continue
        if not status.roadmap(repo):
            _note(doc, "stop", f"{project} has no studio_roadmap.json features",
                  project=project, reason="no-roadmap")
            continue
        if not _planner_free(cap, db_path, now):
            _note(doc, "stop", "planner seat spent or blocked; not planning",
                  project=project, reason="planner-capacity")
            continue
        if not idle:
            _note(doc, "stop", "no idle implementer seat; not planning",
                  project=project, reason="implementer-capacity")
            continue
        # Existing work that we just queued fills the idle seats. Plan only
        # when nothing current is waiting to run.
        if dispatched:
            continue
        feat = _candidate(project, phase, repo, doc, now)
        if feat is None:
            continue
        written = _plan_one(project, phase, repo, feat, doc, db_path, now)
        if written is not None:
            planned = str(written)
    _save_state(doc)
    return {"planned": planned, "dispatched": dispatched}


def start(db_path=None, interval=INTERVAL_S):
    """Daemon thread. No-op when the deployment has not opted in."""
    global _started
    if not enabled():
        return None
    with _started_lock:
        if _started:
            return None
        _started = True

    def loop():
        while True:
            try:
                tick(db_path)
            except Exception as exc:                          # noqa: BLE001
                try:
                    import errors
                    errors.capture(exc, node="studio.autopilot")
                except Exception:                             # noqa: BLE001
                    pass
            time.sleep(interval)

    t = threading.Thread(target=loop, name="studio-autopilot", daemon=True)
    t.start()
    return t
