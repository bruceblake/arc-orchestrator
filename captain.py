"""Captain: a conversational supervisor over the governed fleet.

`main.py captain` is the engine behind the dashboard's Captain panel. The
operator talks to it; it gathers live fleet state, answers, and issues a
BOUNDED set of actions (plan / run / resume / status / amend) that it does
NOT execute itself — every action is a fixed `main.py` argv, spawned
detached. The captain never edits code, never touches git, and never invents
a command: all the safety of the governed pipeline (worktrees, verify gates,
cross-family review, the PR gate) is untouched by this module.

Two differences from `orchchat.py` (the planning-only chat), both load-bearing:

1. **State, not just conversation.** Every turn is preceded by a compact
   snapshot of the fleet — task rows by status, the three concurrency layers,
   chain readiness, and recent events. That snapshot is what makes the captain
   a supervisor instead of a planner: it can see a `failed` task, a
   `chain.blocked`, or a jammed GLM slot and talk about it.

2. **Capacity-aware scheduling.** The fleet's scarcest resource is GLM-5.3's
   per-account concurrency. Before an action fans work out, the captain checks
   the three cap layers (account, driver leases, harness pool) and, when the
   work would over-subscribe, QUEUES it instead of launching into a wall of
   400s — the failure the fortnite session hit on 2026-09-17
   (`concurrent session limit reached for model 'GLM-5.3'`).

Exit codes match orchchat: 0 for every completed turn (a failed action is
recorded IN the appended turn — the dashboard polls this process), 1 only
when the session file itself cannot be read or written.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import config
import project_contract
import errors
import events
import orchchat
from store import Store

SESSION_RE = orchchat.SESSION_RE

# Actions the captain is allowed to take. Anything else in a ```captain block
# is dropped, never executed.
ACTION_KINDS = ("plan", "run", "resume", "status", "amend")

HISTORY_CHARS = orchchat.HISTORY_CHARS
EVENTS_TAIL = 40          # recent event lines shown to the captain
EVENTS_TAIL_BYTES = 2 * 1024 * 1024
EVENTS_KEEP = ("task.", "chain.", "driver.cap_wait", "driver.error",
               "run.", "graph.", "plan.", "promotion.", "dream.")


CAPTAIN_PERSONA = (
    f"You are the CAPTAIN of the ARC multi-model coding fleet, running on "
    f"{config.PLANNER_MODEL}. You are a supervisor, not an implementer: you "
    "do not write code and you never run git. A two-model fleet (GLM-5.3, hard "
    "tier and the planner; DeepSeek-V4.1-Flash-thinking-max, the fast "
    "medium-tier workhorse) builds software as a governed task DAG — every "
    "task gets its own git worktree, a deterministic verify gate, a "
    "cross-family review, and a pull request that a human-visible gate merges.\n\n"
    "Each turn you are given a LIVE FLEET STATE snapshot. Use it. Keep the "
    "project in check: name tasks that are failed, conflicted, or stuck; call "
    "out chain gates that are waiting; and flag capacity pressure — the "
    f"fleet's scarcest slot is {config.PLANNER_MODEL}'s per-account "
    "concurrency, and fanning plan/run work out past it produces capacity "
    "400s, not progress. When capacity is tight, prefer fewer concurrent "
    "tasks and say why.\n\n"
    "You manage the project by issuing ACTIONS. Reply to the operator in "
    "plain prose, then — only when you actually want the fleet to do "
    "something — exactly ONE ```captain fenced block containing a JSON object: "
    '{\"actions\": [{\"kind\": \"plan\", \"goal\": \"<goal text>\"}, '
    '{\"kind\": \"run\", \"taskfile\": \"<name>.json\"}, '
    '{\"kind\": \"resume\", \"taskfile\": \"<name>.json\"}, '
    '{\"kind\": \"status\"}]}. '
    "Rules: `plan` drafts a NEW taskfile from a goal (the planner does the "
    "work); `run` starts a planned taskfile (and `resume` is the SAME command "
    "— re-running skips merged tasks, resumes failures a tier up, repairs "
    "conflicts); `status` just reports the snapshot; `amend` may attach a "
    "note or a verify/model change to a taskfile. Prefer 2-6 small tasks when "
    "you plan. If you are only answering a question, omit the block entirely."
)


# --- capacity: the three layers, read live -------------------------------

def _live_lease_counts(db_path=None):
    """{model: count} of leases whose owner process is still alive."""
    try:
        store = Store(db_path or config.DB_PATH)
        return store.lease_usage()
    except Exception as exc:            # a locked/absent db must not kill a turn
        errors.capture(exc, node="captain.leases")
        return {}


def capacity_snapshot(db_path=None):
    """Every live model's three-layer budget and current pressure.

    Returns {model: {"account", "driver_cap", "harness", "harness_cap",
    "in_use", "batch_headroom"}}. `in_use` is the live driver-lease count —
    the only cross-process truth about how many slots this account already
    spends (AGENTS.md Rule 6: leases close the cross-process hole).
    """
    leases = _live_lease_counts(db_path)
    out = {}
    for model in sorted(config.IMPLEMENTER_MODELS | {config.PLANNER_MODEL}):
        if model is None:
            continue
        harness = config.MODEL_HARNESS.get(model)
        cap = config.driver_limit(model)
        in_use = int(leases.get(model, 0))
        out[model] = {
            "account": config.family_limit(config.MODEL_FAMILY[model]),
            "driver_cap": cap,
            "harness": harness,
            "harness_cap": config.harness_limit(harness) if harness else None,
            "in_use": in_use,
            "batch_headroom": max(0, cap - in_use),
        }
    return out


def plan_pressure(taskfile, db_path=None):
    """How a taskfile's plan would spend the fleet's slots.

    Counts the implementer models the taskfile names and checks each has live
    batch headroom. `admit` is False when ANY model the plan needs has NO free
    driver slot. More tasks than free slots still admits: the run's own driver
    leases pace them (`driver.cap_wait`), whereas demanding a slot per task
    would queue any taskfile wider than a model's driver cap forever — the captain then queues instead of launching into
    capacity refusals. A taskfile that will not parse admits (the run itself
    reports the parse error, which is the honest place for it).
    """
    try:
        loaded = load_taskfile_safe(taskfile)
    except Exception as exc:
        return {"admit": True, "reason": f"taskfile unreadable here: {exc}",
                "models": {}, "deficit": {}}
    if loaded is None:
        return {"admit": True, "reason": "taskfile not found", "models": {},
                "deficit": {}}
    need = {}
    for t in loaded["tasks"].values():
        need[t["model"]] = need.get(t["model"], 0) + 1
    cap = capacity_snapshot(db_path)
    deficit = {}
    for model, want in need.items():
        if cap.get(model, {}).get("batch_headroom", 0) <= 0:
            deficit[model] = want
    return {"admit": not deficit,
            "reason": ("capacity free" if not deficit
                       else "no free slot for " + ", ".join(
                           f"{m} (+{d})" for m, d in sorted(deficit.items()))),
            "models": need, "deficit": deficit}


def load_taskfile_safe(taskfile):
    """The loaded taskfile, or None when it does not exist.

    Validated through the REAL loader (code_tasks.load_taskfile), so a plan's
    pressure is computed against exactly what a run would execute.
    """
    import code_tasks
    path = _taskfile_path(taskfile)
    if path is None or not path.is_file():
        return None
    return code_tasks.load_taskfile(str(path))


def captain_dir():
    """$ARC_CAPTAIN_DIR, default <cwd>/logs/captain.

    Captain sessions live here, NOT in the chat dir, so the planning-chat
    session picker (`/api/chat/sessions`) never lists a captain session and
    vice versa. Resolved at call time, like orchchat.chat_dir, so tests and
    the dashboard can redirect it.
    """
    return Path(os.getenv("ARC_CAPTAIN_DIR") or Path.cwd() / "logs" / "captain")


def _session_path(session):
    return captain_dir() / f"{session}.jsonl"


def _taskfile_path(taskfile):
    """Resolve a bare taskfile name under TASKS_DIR; None if it escapes it."""
    if not isinstance(taskfile, str) or not taskfile.strip():
        return None
    # The name comes from model output steered by a dashboard message, and
    # `code run` executes the file's verify_cmd strings (Rule 6b), so an
    # absolute path or a `..` that lands outside TASKS_DIR is refused.
    try:
        root = Path(config.TASKS_DIR).resolve()
        rp = (root / taskfile.strip()).resolve()
    except (OSError, ValueError):
        return None
    if rp.parent != root:
        return None
    return rp


# --- fleet state ----------------------------------------------------------

def _task_rows(store, limit=200):
    try:
        return store.code_tasks_all(limit=limit)
    except Exception as exc:
        errors.capture(exc, node="captain.task_rows")
        return []


def _recent_events(path=None, n=EVENTS_TAIL, keep=EVENTS_KEEP):
    """The last `n` interesting events, oldest first."""
    path = Path(path or config.EVENTS_LOG)
    try:
        # The dashboard polls this; the log grows to 100 MiB before rotating,
        # so read only its newest bytes.
        with path.open("rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - EVENTS_TAIL_BYTES))
            tail = fh.read().decode("utf-8", errors="replace").splitlines()
        if size > EVENTS_TAIL_BYTES:
            tail = tail[1:]        # the first line was cut mid-record
    except OSError:
        return []
    out = []
    for line in reversed(tail):
        line = line.strip()
        if not line or not any(k in line for k in keep):
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
        if len(out) >= n:
            break
    return list(reversed(out))


def fleet_state(store=None, db_path=None):
    """The compact snapshot the captain reasons over, as a dict."""
    store = store or Store(db_path or config.DB_PATH)
    rows = _task_rows(store)
    by_status = {}
    for r in rows:
        by_status.setdefault(r.get("status") or "?", []).append(
            {"id": r.get("id"), "taskfile": r.get("taskfile"),
             "model": r.get("model"), "error": (r.get("error") or "")[:200]})
    attention = [r for r in rows
                 if r.get("status") in ("failed", "conflict", "in_review")]
    return {
        "capacity": capacity_snapshot(db_path),
        "task_status_counts": {k: len(v) for k, v in by_status.items()},
        "needs_attention": attention[:20],
        "recent_events": _recent_events(),
        "tasks_dir": str(config.TASKS_DIR),
        "planner_model": config.PLANNER_MODEL,
    }


def _state_block(state):
    """Render the state dict as compact text for the prompt."""
    lines = ["LIVE FLEET STATE",
             f"planner model: {state['planner_model']}"]
    counts = state.get("task_status_counts") or {}
    lines.append("task counts: " + (", ".join(
        f"{k}={v}" for k, v in sorted(counts.items())) or "none"))
    lines.append("capacity (model: in_use/driver_cap; harness used/cap; account):")
    for model, c in (state.get("capacity") or {}).items():
        lines.append(f"  {model}: {c['in_use']}/{c['driver_cap']} slots, "
                     f"harness {c.get('harness')} cap {c.get('harness_cap')}, "
                     f"account {c['account']}")
    att = state.get("needs_attention") or []
    if att:
        lines.append("needs attention:")
        for r in att:
            lines.append(f"  {r.get('status')} {r.get('id')} "
                         f"({Path(r.get('taskfile') or '').name}) "
                         f"{r.get('error') or ''}".rstrip())
    else:
        lines.append("needs attention: none")
    evs = state.get("recent_events") or []
    if evs:
        lines.append("recent events (oldest first):")
        for e in evs:
            extra = {k: v for k, v in e.items()
                     if k not in ("ts", "type", "run_id")}
            lines.append(f"  {e.get('type')} {json.dumps(extra, default=str)[:200]}")
    return "\n".join(lines)


def build_prompt(repo, state, turns):
    return (CAPTAIN_PERSONA
            + f"\n\nTARGET REPO: {repo}\n\n"
            + project_contract.captain_block(repo)
            + "\n"
            + _state_block(state)
            + "\n\nCONVERSATION WITH THE OPERATOR (oldest first). Reply as "
              "the captain:\n\n"
            + orchchat._history(turns))


def _captain_driver():
    """The planner/captain model, interactive so a human never queues behind
    three implementers (drivers.Driver.run; orchchat._planner_driver)."""
    model = config.PLANNER_MODEL
    if model is None:
        raise RuntimeError("no planner-capable model on today's roster")
    import drivers
    return drivers.driver_for(model, "planner", interactive=True)


# --- action parsing + execution ------------------------------------------

def parse_actions(text):
    """The validated action list from the LAST ```captain block (or []).

    Unknown kinds, missing fields, and non-object entries are DROPPED, never
    executed: the captain's action set is fixed in code, and a malformed block
    degrades to a prose-only turn rather than a surprise.
    """
    blocks = re.findall(r"```captain[^\n]*\n(.*?)```", text or "", re.DOTALL)
    if not blocks:
        return []
    try:
        data = json.loads(blocks[-1].strip())
    except ValueError:
        return []
    raw = data.get("actions") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    out = []
    for a in raw:
        if not isinstance(a, dict):
            continue
        kind = a.get("kind")
        if kind not in ACTION_KINDS:
            continue
        act = {"kind": kind}
        if kind == "plan":
            goal = a.get("goal")
            if not isinstance(goal, str) or not (3 <= len(goal.strip()) <= 2000):
                continue
            act["goal"] = goal.strip()
        elif kind in ("run", "resume"):
            tf = a.get("taskfile")
            if not isinstance(tf, str) or not tf.strip():
                continue
            act["taskfile"] = tf.strip()
            if isinstance(a.get("dry_run"), bool):
                act["dry_run"] = a["dry_run"]
        elif kind == "amend":
            tf = a.get("taskfile")
            if not isinstance(tf, str) or not tf.strip():
                continue
            act["taskfile"] = tf.strip()
            act["reason"] = (a.get("reason") or "")[:1000]
        out.append(act)
    return out


def _queue_path():
    return Path(os.getenv("ARC_CAPTAIN_DIR") or Path.cwd() / "logs" / "captain") \
        / "queue.jsonl"


def _enqueue(entry):
    p = _queue_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# Durable delivery of capacity-queued run/resume. The JSONL file stays as the
# historical log the queue view already knew; the work itself lives in the
# shared workqueue so a killed dashboard does not drop it.
RUN_TOPIC = "captain.run"
DRAIN_INTERVAL_S = 20.0
DRAIN_LEASE_S = 120.0
_drain_started = False
_drain_lock = threading.Lock()
_run_queues = {}
_run_queues_lock = threading.Lock()


def _run_queue(db_path=None):
    import workqueue
    path = str(db_path or config.DB_PATH)
    with _run_queues_lock:
        q = _run_queues.get(path)
        if q is None:
            q = workqueue.Queue(path)
            _run_queues[path] = q
        return q


def enqueue_run(kind, path, repo, reason, models, dry_run=False, db_path=None):
    """Queue one taskfile run. Keyed on the canonical path, so a second
    enqueue while the first is still live is a no-op.

    Returns (item_id, created). ``created`` is False when live work for this
    path already exists. The JSONL line is the historical record; ``durable``
    marks lines the live view should not repeat once the queue row is gone.
    """
    canonical = str(Path(path).resolve())
    entry = {"ts": time.time(), "kind": kind, "taskfile": Path(path).name,
             "path": canonical, "repo": repo,
             "queue_until": "a driver slot frees", "reason": reason,
             "models": models, "dry_run": bool(dry_run)}
    try:
        item_id, created = _run_queue(db_path).enqueue(
            RUN_TOPIC, canonical, payload=entry)
        entry["durable"] = True
    except Exception as exc:
        errors.capture(exc, node="captain.enqueue")
        item_id, created = None, False
    _enqueue(entry)
    return item_id, created


def _jsonl_entries():
    try:
        lines = _queue_path().read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def queue_view(db_path=None):
    """What GET /api/captain/queue shows.

    Live pending/claimed rows are the queue. JSONL lines written before the
    durable queue (no ``durable`` flag) stay visible. Lines this module wrote
    are hidden once their row leaves the live set, so a delivered run does not
    keep looking queued. Never raises; a database that will not open falls
    back to the JSONL log.
    """
    try:
        items = _run_queue(db_path).active(RUN_TOPIC, limit=100)
    except Exception as exc:
        errors.capture(exc, node="captain.queue_view")
        return {"queued": list(reversed(_jsonl_entries()[-100:]))}
    live = []
    for it in items:
        payload = it.get("payload") if isinstance(it.get("payload"), dict) else {}
        row = dict(payload)
        row.setdefault("taskfile", Path(it.get("dedupe_key") or "").name)
        row["state"] = it.get("state")
        live.append(row)
    legacy = [e for e in _jsonl_entries() if not e.get("durable")]
    return {"queued": (live + list(reversed(legacy)))[:100]}


def _taskfile_live(path):
    """True when a `main.py code run` of this taskfile is already alive."""
    import reconcile
    want = Path(path).resolve()
    try:
        runs = reconcile.live_runs()
    except Exception as exc:
        errors.capture(exc, node="captain.drain")
        return False
    for run in runs:
        tf = run.get("taskfile")
        if not tf:
            continue
        other = Path(tf)
        if other.name != want.name:
            continue
        if not other.is_absolute():
            return True
        try:
            if other.resolve() == want:
                return True
        except OSError:
            if str(other) == str(want):
                return True
    return False


def _deliver(q, item, db_path=None):
    """Recheck one claimed run and launch, drop, or put it back.

    Order is file validity, then a live duplicate, then capacity. A blocked
    plan goes back to pending for a later pass — the caller does not claim
    again in this pass, so a full fleet does not spin.
    """
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    name = payload.get("taskfile") or Path(item.get("dedupe_key") or "").name
    kind = payload.get("kind") if payload.get("kind") in ("run", "resume") else "run"
    path = _taskfile_path(name)
    if path is None or not path.is_file():
        q.fail(item["id"], error=f"no such taskfile: {name}", retry=False)
        events.emit("captain.drain", action=kind, taskfile=name, dropped="missing")
        return {"taskfile": name, "result": "missing"}
    if _taskfile_live(path):
        q.complete(item["id"], {"skipped": "live"})
        events.emit("captain.drain", action=kind, taskfile=path.name,
                    skipped="live")
        return {"taskfile": path.name, "result": "live"}
    pressure = plan_pressure(str(path), db_path=db_path)
    if not pressure["admit"]:
        q.fail(item["id"], error=pressure["reason"], retry=True)
        return {"taskfile": path.name, "result": "retained",
                "reason": pressure["reason"]}
    argv = [_python(), "main.py", "code", "run", str(path)]
    if payload.get("dry_run"):
        argv.append("--dry-run")
    try:
        proc = _spawn_detached(argv, f"captain-run-{path.stem}.log")
    except Exception as exc:
        errors.capture(exc, node="captain.drain")
        q.fail(item["id"], error=str(exc), retry=True)
        return {"taskfile": path.name, "result": "retained", "reason": str(exc)}
    q.complete(item["id"], {"pid": proc.pid})
    events.emit("captain.action", action=kind, taskfile=path.name,
                pid=proc.pid, drained=True)
    return {"taskfile": path.name, "result": "launched", "pid": proc.pid}


def drain_once(db_path=None, limit=100):
    """Reclaim expired claims, then deliver one batch.

    Claiming the batch up front is what keeps a still-blocked head from being
    claimed again the moment it is put back. A worker that dies mid-batch
    leaves a lease; the next pass's reclaim returns that work to pending.
    """
    q = _run_queue(db_path)
    q.reclaim(RUN_TOPIC)
    claimed = []
    for _ in range(max(1, int(limit))):
        item = q.claim(RUN_TOPIC, worker=f"captain-drain:{os.getpid()}",
                       lease_s=DRAIN_LEASE_S)
        if item is None:
            break
        claimed.append(item)
    return [_deliver(q, item, db_path=db_path) for item in claimed]


def start_drain(db_path=None, interval=DRAIN_INTERVAL_S):
    """Daemon thread the dashboard starts. One pass, then sleep — never a
    tight loop while capacity is still blocked."""
    global _drain_started
    with _drain_lock:
        if _drain_started:
            return None
        _drain_started = True

    def loop():
        while True:
            try:
                drain_once(db_path=db_path)
            except Exception as exc:
                errors.capture(exc, node="captain.drain")
            time.sleep(interval)

    t = threading.Thread(target=loop, name="captain-drain", daemon=True)
    t.start()
    return t


def _python():
    return str(Path(config.ROOT) / ".venv" / "bin" / "python")


def _spawn_detached(argv, log_name):
    """Launch a fixed argv detached, streaming to logs/<log_name>."""
    log_dir = Path(config.ROOT) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    lf = open(log_dir / log_name, "ab", buffering=0)
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(argv, cwd=str(config.ROOT), stdout=lf,
                            stderr=subprocess.STDOUT, start_new_session=True,
                            close_fds=True, env=env)
    return proc


def execute_actions(actions, repo, db_path=None):
    """Run the validated actions against the allowlists; return their results.

    Each action is a FIXED argv. `run`/`resume` are capacity-gated: a plan
    that cannot be admitted is queued (workqueue topic ``captain.run``, keyed
    by canonical path, plus a JSONL line) with the reason, never launched
    into a capacity wall. Results are recorded in the turn so the operator
    sees exactly what the captain did and why.
    """
    results = []
    for act in actions:
        kind = act["kind"]
        if kind == "status":
            results.append({"kind": "status", "ok": True,
                            "note": "reported the live snapshot"})
        elif kind == "plan":
            slug = orchchat._slug(act["goal"])
            expect = slug + ".json"
            proc = _spawn_detached(
                [_python(), "main.py", "code", "plan", act["goal"], repo],
                f"captain-plan-{slug}.log")
            events.emit("captain.action", action="plan", goal=act["goal"][:200],
                        pid=proc.pid, taskfile=expect)
            results.append({"kind": "plan", "ok": True, "pid": proc.pid,
                            "taskfile": expect,
                            "note": f"{config.PLANNER_MODEL} is drafting {expect}"})
        elif kind in ("run", "resume"):
            path = _taskfile_path(act["taskfile"])
            if path is None or not path.is_file():
                results.append({"kind": kind, "ok": False,
                                "error": f"no such taskfile: {act['taskfile']}"})
                continue
            pressure = plan_pressure(str(path), db_path=db_path)
            if not pressure["admit"]:
                enqueue_run(kind, path, repo, pressure["reason"],
                            pressure["models"], dry_run=bool(act.get("dry_run")),
                            db_path=db_path)
                events.emit("captain.action", action=kind, taskfile=path.name,
                            queued=True, reason=pressure["reason"])
                results.append({"kind": kind, "ok": False, "queued": True,
                                "taskfile": path.name,
                                "error": pressure["reason"],
                                "note": "queued until capacity frees"})
                continue
            argv = [_python(), "main.py", "code", "run", str(path)]
            if act.get("dry_run"):
                argv.append("--dry-run")
            proc = _spawn_detached(argv, f"captain-run-{path.stem}.log")
            events.emit("captain.action", action=kind, taskfile=path.name,
                        pid=proc.pid)
            results.append({"kind": kind, "ok": True, "pid": proc.pid,
                            "taskfile": path.name,
                            "note": f"launched {kind} of {path.name}"})
        elif kind == "amend":
            path = _taskfile_path(act["taskfile"])
            if path is None or not path.is_file():
                results.append({"kind": "amend", "ok": False,
                                "error": f"no such taskfile: {act['taskfile']}"})
                continue
            try:
                store = Store(db_path or config.DB_PATH)
                store.save_plan_proposal(str(path), "project", "captain",
                                         "planner", config.PLANNER_MODEL,
                                         "note", "note", act.get("reason", ""),
                                         {})
                events.emit("captain.action", action="amend", taskfile=path.name,
                            reason=act.get("reason", "")[:200])
                results.append({"kind": "amend", "ok": True,
                                "taskfile": path.name,
                                "note": "note recorded against the taskfile"})
            except Exception as exc:
                fp = errors.capture(exc, node="captain.amend")
                results.append({"kind": "amend", "ok": False,
                                "error": f"{fp}: {exc}"})
    return results


# --- the turn -------------------------------------------------------------

async def run_turn(session, repo):
    """One captain turn: state -> model -> prose + bounded actions."""
    if not session or not SESSION_RE.fullmatch(session):
        print(f"captain: invalid session id {session!r}", file=sys.stderr)
        return 1
    path = _session_path(session)
    try:
        turns = orchchat.load_session(path)
    except (OSError, ValueError) as exc:
        print(f"captain: cannot read session {path}: {exc}", file=sys.stderr)
        return 1

    problem = orchchat._repo_problem(repo)
    if problem is not None:
        err = orchchat._safe_append(path, {
            "role": "assistant", "ts": time.time(),
            "text": "I can't supervise a project in that repository — "
                    "please give me a valid one.",
            "error": problem})
        return 1 if err else 0

    try:
        state = fleet_state()
    except Exception as exc:
        fp = errors.capture(exc, node="captain.state")
        state = {"planner_model": config.PLANNER_MODEL, "capacity": {},
                 "task_status_counts": {}, "needs_attention": [],
                 "recent_events": [], "error": f"{fp}: {exc}"}

    task_id = f"captain-{session}"
    try:
        res = await _captain_driver().run(build_prompt(repo, state, turns),
                                          Path(repo), task_id=task_id)
    except Exception as exc:
        fp = errors.capture(exc, task=task_id, model=config.PLANNER_MODEL,
                            node="captain")
        err = orchchat._safe_append(path, {
            "role": "assistant", "ts": time.time(),
            "text": "Sorry — the captain model failed on that request. "
                    "Please try again.",
            "error": f"{fp}: {exc}"})
        return 1 if err else 0

    try:
        text = orchchat._reply_text(res)
    except Exception as exc:
        fp = errors.capture(exc, task=task_id, node="captain.reply_text")
        text = (getattr(res, "text", "") or "")
        if not text:
            err = orchchat._safe_append(path, {
                "role": "assistant", "ts": time.time(),
                "text": "Sorry — the captain's reply could not be read.",
                "error": f"{fp}: {exc}"})
            return 1 if err else 0
    turn = {"role": "assistant", "ts": time.time(), "text": text}
    if not text.strip():
        turn["error"] = "captain returned an empty reply"
    else:
        actions = parse_actions(text)
        if actions:
            try:
                turn["actions"] = execute_actions(actions, repo)
            except Exception as exc:
                fp = errors.capture(exc, task=task_id, node="captain.execute")
                turn["error"] = f"{fp}: {exc}"
    err = orchchat._safe_append(path, turn)
    return 1 if err else 0
