"""Daily audit: triage the bugs, then check the codebase is actually sound.

Two different questions, deliberately in one report, because answering only the
first is how a codebase rots while every dashboard stays green:

  1. WHAT BROKE — distinct defects from errors.py, worst first, plus the tasks
     that failed and why.
  2. WHAT IS ROTTING — the checks nobody runs by hand: work stranded in a
     non-terminal state, worktrees and branches left behind by dead runs, leases
     pinning capacity for processes that no longer exist, event-log growth,
     docs that have drifted, and whether the test suite and gate still pass.

Every finding carries a SEVERITY and a concrete next action. A report that says
"14 warnings" and leaves the reader to work out which matter is a report nobody
reads twice.

Read-only by default. `--fix` performs only the reversible cleanups that are
already implemented elsewhere (reconcile's reaping), and says what it did.
"""
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import config

SEV = ("critical", "warning", "info")


def _finding(sev, area, what, detail="", action=""):
    return {"severity": sev, "area": area, "what": what,
            "detail": detail, "action": action}


def _sh(*args, cwd=None, timeout=30):
    try:
        r = subprocess.run(args, cwd=str(cwd or config.ROOT), capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


# ---- 1. what broke -------------------------------------------------------

def triage_errors(since_s=86400, limit=20):
    out = []
    try:
        import errors
        groups = errors.groups(since=time.time() - since_s, limit=limit)
    except Exception as exc:
        return [_finding("warning", "triage", "could not read the error store",
                         str(exc)[:200], "check the database is readable")]
    for g in groups:
        # One occurrence of something an hour ago is noise. Many occurrences,
        # or anything still firing, is a defect with a queue behind it.
        sev = "critical" if (g["count"] >= 5 and g["active"]) else \
              "warning" if g["count"] >= 3 or g["active"] else "info"
        out.append(_finding(
            sev, "defect",
            f"{g['kind']} x{g['count']} at {g['where'] or 'unknown'}",
            (g["message"] or "")[:300],
            f"fingerprint {g['fingerprint']}; hit {g['n_tasks']} task(s): "
            f"{', '.join(str(t) for t in g['tasks'][:5])}"))
    return out


def triage_tasks(store):
    out = []
    try:
        rows = store.code_tasks_all()
    except Exception as exc:
        return [_finding("warning", "triage", "could not read code_tasks",
                         str(exc)[:200], "")]
    # A non-terminal task with a live run is a task WORKING, not a task stuck.
    # Telling the operator to "re-run their project to resume" something that is
    # running right now is noise, and noise is how an audit stops being read.
    try:
        import reconcile
        live_files = {r.get("taskfile") for r in reconcile.live_runs()}
    except Exception:
        live_files = None
    stuck, working = [], 0
    for r in rows:
        if r.get("status") not in ("conflict", "in_review", "running"):
            continue
        if live_files is not None and r.get("taskfile") in live_files:
            working += 1
            continue
        stuck.append(r)
    if working:
        out.append(_finding("info", "tasks",
                            f"{working} task(s) in flight right now", "", ""))
    by_status = {}
    for r in stuck:
        by_status.setdefault(r["status"], []).append(r["id"])
    for status, ids in sorted(by_status.items()):
        sev = "critical" if status == "conflict" else "warning"
        out.append(_finding(
            sev, "tasks", f"{len(ids)} task(s) stranded in '{status}'",
            ", ".join(ids[:10]),
            "no run is working on them: re-run their project to resume; a task "
            "left non-terminal holds a worktree, a branch and possibly an open PR"))
    failed = [r for r in rows if r.get("status") == "failed"]
    reasons = {}
    for r in failed:
        key = (r.get("error") or "unknown")[:60]
        reasons[key] = reasons.get(key, 0) + 1
    for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1])[:5]:
        out.append(_finding("info", "tasks", f"{n} task(s) failed: {reason}", "",
                            "re-running the project resumes them"))
    return out


# ---- 2. what is rotting --------------------------------------------------

def _worktree_holds_work(repo, name):
    """Why this worktree must not be reaped, or "" if it is genuinely spent.

    The status column is not sufficient and never was: a task can be `failed`
    while its branch carries commits behind an OPEN pull request. Ask git and
    GitHub, not the database.
    """
    branch = f"task/{name}"
    rc, out, _ = _sh("git", "rev-list", "--count",
                     f"origin/{config.BASE_BRANCH}..{branch}", cwd=repo)
    ahead = int(out.strip()) if rc == 0 and out.strip().isdigit() else 0
    reasons = []
    if ahead:
        reasons.append(f"{ahead} unmerged commit(s) on {branch}")
    rc, out, _ = _sh("gh", "pr", "list", "--head", branch, "--state", "open",
                     "--json", "number", cwd=repo, timeout=20)
    if rc == 0 and out.strip() and out.strip() != "[]":
        try:
            n = json.loads(out)[0]["number"]
            reasons.append(f"pull request #{n} is open")
        except (ValueError, IndexError, KeyError):
            reasons.append("a pull request is open")
    wt = Path(config.WORKTREE_ROOT) / Path(repo).name / name
    rc, out, _ = _sh("git", "status", "--porcelain", cwd=wt)
    n_dirty = len([ln for ln in out.splitlines() if ln.strip()])
    if n_dirty:
        reasons.append(f"{n_dirty} uncommitted change(s)")
    return "; ".join(reasons)


def audit_git(repo=None, store=None):
    repo = Path(repo or config.ROOT)
    out = []
    rc, wt, _ = _sh("git", "worktree", "list", "--porcelain", cwd=repo)
    trees = [ln.split(" ", 1)[1] for ln in wt.splitlines() if ln.startswith("worktree ")]
    # Only worktrees WE allocated. opencode makes its own under /tmp/opencode
    # for snapshotting; they are detached HEADs, they are not tasks, and
    # reaping one could break a harness that is mid-run.
    root = Path(config.WORKTREE_ROOT).resolve()
    allocated = [t for t in trees
                 if Path(t).resolve() != repo.resolve()
                 and str(Path(t).resolve()).startswith(str(root))]
    # A worktree belonging to a task that is STILL WORKING is not a leak, it is
    # the task working. The first version counted all of them and reported
    # "8 worktrees — CRITICAL" while four of them were in active use. An audit
    # that raises a critical alarm during normal operation is one nobody reads
    # twice, which is the failure this module exists to avoid.
    # `live` unknown and `live` empty are NOT the same thing. An exception here
    # used to yield an empty set, which makes every worktree look not-live —
    # including one a task allocated seconds ago — and with --fix that is a
    # reap of work in progress. When we cannot tell, we say so and report
    # nothing rather than reporting everything.
    live, live_known = set(), store is not None
    if store is not None:
        try:
            live = {r["id"] for r in store.code_tasks_all()
                    if r.get("status") in ("running", "in_review", "conflict")}
        except Exception as exc:
            live_known = False
            out.append(_finding(
                "warning", "git", "cannot tell which worktrees are in use",
                str(exc)[:200],
                "skipping the orphan check rather than risk reaping live work"))
    if not live_known:
        return out
    orphan, risky = [], []
    for t in allocated:
        if Path(t).name in live:
            continue
        why = _worktree_holds_work(repo, Path(t).name)
        (risky if why else orphan).append((t, why))
    for t, why in risky:
        # A terminal task row does NOT mean the branch is disposable.
        # pause-when-hidden is `failed` AND three commits ahead behind open
        # PR #9 — reaping it on the strength of the status column alone would
        # have destroyed reviewable work.
        out.append(_finding(
            "warning", "git",
            f"{Path(t).name}: task is not running but the branch holds work",
            why, "do NOT reap it — resolve or close the PR first, or merge "
                 "the branch; reaping destroys commits"))
    orphan = [t for t, _ in orphan]
    in_use = len(allocated) - len(orphan)
    if orphan:
        out.append(_finding(
            "warning" if len(orphan) < 8 else "critical", "git",
            f"{len(orphan)} orphaned worktree(s)"
            + (f" ({in_use} more in active use)" if in_use else ""),
            ", ".join(Path(t).name for t in orphan[:10]),
            "their task is not running: main.py code reconcile --apply "
            "removes them"))
    elif in_use:
        out.append(_finding("info", "git",
                            f"{in_use} worktree(s) in active use", "", ""))
    rc, br, _ = _sh("git", "branch", "--list", "task/*", cwd=repo)
    branches = [b.strip("*+ ").strip() for b in br.splitlines() if b.strip()]
    merged = set()
    rc, m, _ = _sh("git", "branch", "--merged", config.BASE_BRANCH, cwd=repo)
    if rc == 0:
        # '+' marks a branch checked out in another worktree: it is merged but
        # deleting it would fail, so it is not a suggestion worth making.
        checked_out = {b.strip("*+ ").strip() for b in m.splitlines()
                       if b.lstrip().startswith(("+", "*"))}
        merged = {b.strip("*+ ").strip() for b in m.splitlines() if b.strip()}
        merged -= checked_out
    stale = [b for b in branches if b in merged]
    if stale:
        out.append(_finding(
            "info", "git", f"{len(stale)} task branch(es) already merged",
            ", ".join(stale[:10]), "safe to delete: git branch -d <name>"))
    rc, st, _ = _sh("git", "status", "--porcelain", cwd=repo)
    dirty = [ln for ln in st.splitlines() if ln.strip()]
    if dirty:
        out.append(_finding(
            "warning", "git", f"{len(dirty)} uncommitted change(s) in the main repo",
            "; ".join(dirty[:6]),
            "a dirty main repo is how a fleet merge picks up work nobody reviewed"))
    return out


def _watchdog_pids():
    """Pids running fleetwatch.py, found by argv shape like reconcile.live_runs.

    A bare "does any argument mention fleetwatch.py" test is wrong in both
    directions: `grep fleetwatch.py /proc/*/cmdline`, an editor or a reviewer's
    `sed -n 1,200p fleetwatch.py` all carry the token and none of them is the
    watchdog — a false negative here is exactly the alarm that gets an audit
    muted. So the interpreter must come first (`python -u fleetwatch.py`), the
    same discipline reconcile.live_runs uses for `main.py code run`.
    """
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = [a for a in (entry / "cmdline").read_bytes()
                    .decode(errors="replace").split("\0") if a]
        except OSError:
            continue
        for i, arg in enumerate(argv):
            if not Path(arg).name.startswith("python"):
                continue
            rest = [a for a in argv[i + 1:] if not a.startswith("-")]
            if rest and Path(rest[0]).name == "fleetwatch.py":
                pids.append(int(entry.name))
            break
    return pids


def _linger_state():
    """True/False for `Linger=yes`, None where loginctl cannot say.

    None means "do not report": loginctl is absent on non-systemd machines
    (containers, a dev box), and an audit that warns about a tool the host does
    not have is noise the operator learns to skip.
    """
    rc, so, _ = _sh("loginctl", "show-user",
                    os.environ.get("USER") or os.environ.get("LOGNAME") or "",
                    "-p", "Linger")
    if rc != 0:
        return None
    for line in so.splitlines():
        if line.startswith("Linger="):
            return line.split("=", 1)[1].strip().lower() in ("yes", "1", "true")
    return None


def audit_watchdog():
    """Is the watchdog actually running, and will it come back after a reboot?

    Both questions are about one failure seen twice on 2026-09-24: a WSL
    restart killed the fleet's runs AND the watchdog, and neither came back.
    The watchdog is a systemd USER unit, so without loginctl linger it is torn
    down when the last login session ends — a restart is exactly that — and
    nothing resumes the runs it was holding, which is how seven rows sat at
    `running` forever. No other check can see this: the rest of the audit
    reports on work, and this reports on the absence of the thing that drives
    it.
    """
    out = []
    try:
        import reconcile
        n_live = len(reconcile.live_runs())
    except Exception:
        n_live = 0
    if not _watchdog_pids():
        out.append(_finding(
            "warning", "watchdog", "no fleetwatch process is alive",
            f"{n_live} code run(s) alive with nothing to resume them if they die",
            "systemctl --user start arc-watchdog (install: cp "
            "deploy/arc-watchdog.service ~/.config/systemd/user/ && systemctl "
            "--user enable --now arc-watchdog)"))
    if _linger_state() is False:
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or "$USER"
        out.append(_finding(
            "warning", "watchdog", "loginctl linger is off for this user",
            "a user unit is stopped when the last session ends, so the watchdog "
            "does not survive a logout or a WSL restart",
            f"sudo loginctl enable-linger {user}"))
    return out


def audit_leases(store):
    out = []
    try:
        rows = list(store.driver_lease_rows() or [])
    except Exception as exc:
        return [_finding("warning", "leases", "could not read driver_leases",
                         str(exc)[:200], "")]
    dead = []
    for r in rows:
        pid = r["pid"]
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            dead.append(f"{r['model']}/{r['task']}")
        except (OSError, ValueError, TypeError):
            pass
    if dead:
        out.append(_finding(
            "critical", "leases", f"{len(dead)} lease(s) held by dead processes",
            ", ".join(dead[:10]),
            "they pin a model at cap until the TTL expires; "
            "main.py code reconcile --apply reaps them"))
    return out


def board_health_findings(project=None, since_hours=24):
    """Coordinate means USING the board; Rule 4c.

    Reads agentboard.board_health and turns it into findings with actions.
    Every threshold is deliberately loose — a report that fires on a quiet
    project is a report nobody reads twice. An unreadable board is INFO, not
    a warning: a tree predating the board is not a defect.
    """
    out = []
    try:
        import agentboard
    except Exception:  # noqa: BLE001
        return out
    projects = [project] if project else [p["project"] for p in agentboard.projects()]
    for proj in projects:
        try:
            h = agentboard.board_health(proj, since_hours=since_hours)
        except Exception as exc:  # noqa: BLE001
            out.append(_finding("info", "board", f"{proj}: board health unreadable",
                                str(exc)[:200], ""))
            continue
        out.extend(_board_findings(proj, h, since_hours))
    return out


def _board_findings(proj, h, since_hours):
    out = []
    kindline = ", ".join(f"{k} {v}" for k, v in list(h["by_kind"].items())[:6])
    agentline = ", ".join(f"{a} {n}" for a, n in list(h["by_agent"].items())[:6])
    out.append(_finding(
        "info", "board", f"{proj}: {h['posts']} board post(s) in {since_hours:g}h",
        (f"kinds: {kindline}\nagents: {agentline}") if h["posts"] else "", ""))

    if h["posts"] and h["tasks"]:
        if h["claim_share"] < 0.5:
            out.append(_finding(
                "warning", "board",
                f"{proj}: only {h['claim_share']:.0%} of tasks posted a claim",
                f"{h['claimed']}/{h['tasks']} task(s) leased the files they were "
                "expected to touch",
                "a task that never claims cannot be warned off a sibling's "
                "files; check the CLAIM CONFLICTS block reaches the "
                "implementer prompt"))
        if h["result_share"] < 0.5:
            out.append(_finding(
                "warning", "board",
                f"{proj}: only {h['result_share']:.0%} of tasks posted a result",
                f"{h['resulted']}/{h['tasks']} task(s) posted kind=result — the "
                "next agent cannot tell who knows what",
                "a result must list the files it changed in refs.files, so "
                "`agentboard.expertise` can route a question to it"))

    if h["unanswered"]:
        oldest = max(q["age_s"] for q in h["unanswered"])
        worst = ", ".join(f"#{q['id']} {q['author']} ({q['age_s'] / 3600:.1f}h)"
                          for q in h["unanswered"][:5])
        out.append(_finding(
            "warning" if oldest > 4 * 3600 else "info", "board",
            f"{proj}: {len(h['unanswered'])} unanswered question(s)",
            f"oldest {oldest / 3600:.1f}h: {worst}",
            "answer with kind=answer and reply_to=<id>; until someone does, "
            "the asker is blocked or guessing"))

    if h["median_answer_s"] is not None and h["median_answer_s"] > 2 * 3600:
        out.append(_finding(
            "info", "board",
            f"{proj}: median question answer takes "
            f"{h['median_answer_s'] / 60:.0f} min",
            f"{h['answered']} answered in the window",
            "a prompt cannot wait for a reply; if a question must be answered "
            "before the next attempt, post kind=blocker so the captain sees it"))

    for c in h["claim_conflicts"][:5]:
        out.append(_finding(
            "warning", "board",
            f"{proj}: {c['a']} and {c['b']} claim overlapping files",
            ", ".join(c["paths"][:6]),
            "they were pinged when the second claim landed; if both are still "
            "editing, one must release — `main.py board claims` shows every "
            "live lease"))

    for d in h["deaf"][:8]:
        out.append(_finding(
            "warning", "board",
            f"{proj}: {d['agent']} read its inbox but never replied",
            f"{d['pending']} mention(s) delivered to its prompt, last "
            f"{d['since_s'] / 3600:.1f}h ago, no answer or acknowledgement since",
            "the digest is delivered WITH the prompt, so the agent saw it — "
            "either it ignored the mention or answered elsewhere; a mention "
            "needing a reply should be kind=question, answered next round"))
    return out


_SEAT_TYPES = ("driver.start", "driver.done", "driver.error", "driver.cap_wait",
               "driver.usage_limit", "driver.usage_wait", "driver.usage_swap",
               "driver.queued", "driver.stale", "driver.cancelled",
               "driver.cap_timeout", "driver.heartbeat", "driver.progress")
_TERMINAL = ("driver.done", "driver.error", "driver.stale", "driver.cancelled",
             "driver.cap_timeout")
_LIVENESS = ("driver.heartbeat", "driver.progress")


def _load_seat_events():
    path = Path(config.EVENTS_LOG)
    if not path.is_file():
        return []
    out = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "driver." not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") in _SEAT_TYPES:
                out.append(ev)
    return out


def _epoch(value):
    """harness_runs.created_at is an ISO string from store._now(); tests pass epochs."""
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return 0.0
    text = str(value)
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return 0.0


def _runs_in_window(store, cutoff):
    if store is None:
        return []
    # created_at is TEXT in store._now()'s format. A float binds above every
    # ISO string in SQLite's type order and returns the whole table.
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat(timespec="seconds")
    try:
        with store.lock:
            rows = store.conn.execute(
                "SELECT model, seconds, created_at, task_id, attempt "
                "FROM harness_runs WHERE created_at >= ?", (cutoff_iso,)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _clip_iv(start, end, lo, hi):
    a, b = max(start, lo), min(end, hi)
    return (a, b) if b > a else None


def _task_key(task):
    """Fix-round ids are `<task>-x<attempt>`; a start on any attempt closes the wait."""
    text = str(task or "")
    head, sep, tail = text.rpartition("-x")
    if sep and tail.isdigit() and head:
        return head
    return text


def _pid_dead(pid):
    """A present pid that is gone. A missing pid is not evidence of death."""
    if pid is None or pid == "":
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except (OSError, ValueError, TypeError):
        return True
    return False


def _wait_intervals(events, now):
    """A wait stays open until that task starts, ends, or its owner pid dies."""
    opened, closed = {}, []
    for ev in sorted(events, key=lambda e: float(e.get("ts") or 0)):
        model, kind = ev.get("model"), ev.get("type")
        if not model:
            continue
        ts = float(ev.get("ts") or 0)
        key = (model, _task_key(ev.get("task")))
        if kind in ("driver.queued", "driver.cap_wait", "driver.usage_wait"):
            if _pid_dead(ev.get("pid")):
                if key in opened:
                    closed.append((model, opened.pop(key)[0], ts))
                continue
            opened.setdefault(key, [ts, ev.get("pid")])
        elif kind in ("driver.start",) + _TERMINAL and key in opened:
            closed.append((model, opened.pop(key)[0], ts))
    for (model, _), (t0, pid) in opened.items():
        if _pid_dead(pid):
            continue
        closed.append((model, t0, now))
    return closed


def _busy_intervals(model, runs, events, leases, cutoff, now):
    iv = []
    for run in runs:
        if run.get("model") != model:
            continue
        end = _epoch(run.get("created_at"))
        dur = float(run.get("seconds") or 0)
        clipped = _clip_iv(end - dur, end, cutoff, now)
        if clipped:
            iv.append(clipped)
    finished = {(run.get("task_id"), run.get("attempt"))
                for run in runs if run.get("model") == model}
    closed, live, starts = set(), {}, []
    for ev in events:
        if ev.get("model") != model:
            continue
        key = (ev.get("task"), ev.get("attempt"))
        kind = ev.get("type")
        ts = float(ev.get("ts") or 0)
        if kind in _TERMINAL:
            closed.add(key)
        elif kind in _LIVENESS:
            live[key] = max(live.get(key, 0.0), ts)
        elif kind == "driver.start":
            starts.append((key, ts, ev.get("pid")))
    open_tasks = set()
    for key, ts, pid in starts:
        if key in closed or key in finished or _pid_dead(pid):
            continue
        # Credit only through the last heartbeat. A live pid is itself
        # liveness; a flat lease-TTL horizon counted dead runs as a full day.
        if pid not in (None, "") and not _pid_dead(pid):
            end = now
        else:
            end = live.get(key, 0.0)
            if end <= ts:
                continue
        open_tasks.add(key[0])
        clipped = _clip_iv(ts, end, cutoff, now)
        if clipped:
            iv.append(clipped)
    for lease in leases:
        if (lease.get("model") != model or lease.get("task") in open_tasks
                or _pid_dead(lease.get("pid"))):
            continue
        clipped = _clip_iv(float(lease.get("acquired_at") or now), now, cutoff, now)
        if clipped:
            iv.append(clipped)
    return iv


def _occupied_seconds(intervals, cap, lo, hi):
    """Seat-seconds busy, concurrent seats kept but never above `cap`.

    Summing raw lengths counts the same moment once per overlapping run, so a
    pile of stale starts reports utilization far past 100%.
    """
    if cap <= 0 or not intervals:
        return 0.0
    points = {lo, hi}
    for a, b in intervals:
        if b > lo and a < hi:
            points.add(min(hi, max(lo, a)))
            points.add(min(hi, max(lo, b)))
    pts = sorted(p for p in points if lo <= p <= hi)
    total = 0.0
    for a, b in zip(pts, pts[1:]):
        if b <= a:
            continue
        held = sum(1 for x, y in intervals if x <= a and b <= y)
        total += min(cap, held) * (b - a)
    return total


def _free_seat_hours(cap, busy, masks, lo, hi):
    """Seat-hours this model was free while `masks` (other models waiting) overlap."""
    if cap <= 0 or not masks:
        return 0.0
    points = {lo, hi}
    for a, b in busy + masks:
        if b > lo and a < hi:
            points.add(min(hi, max(lo, a)))
            points.add(min(hi, max(lo, b)))
    pts = sorted(points)
    total = 0.0
    for a, b in zip(pts, pts[1:]):
        if b <= a or not any(x <= a and b <= y for x, y in masks):
            continue
        held = sum(1 for x, y in busy if x <= a and b <= y)
        total += max(0, cap - held) * (b - a)
    return total / 3600.0


def seat_utilization(since_hours=24, *, now=None, events=None, runs=None,
                     leases=None, store=None):
    """Per live-roster model: busy time, starvation, wasted idle, plan window.

    Busy seat-hours come from harness_runs.seconds (plus a lease or an open
    driver.start for work that has not finished). cap_waits, errors and the
    plan window come from the event log. Idle-while-waiting is the seat-hours
    this model was free while another model had a task queued or cap-waiting.
    """
    now = time.time() if now is None else now
    since_hours = float(since_hours or 0) or 24.0
    cutoff = now - since_hours * 3600.0
    if events is None:
        events = _load_seat_events()
    if runs is None:
        runs = _runs_in_window(store, cutoff)
    if leases is None:
        try:
            leases = list(store.driver_lease_rows() or []) if store is not None else []
        except Exception:
            leases = []
    events = [e for e in events if isinstance(e, dict)]
    in_window = [e for e in events
                 if cutoff <= float(e.get("ts") or 0) <= now]
    waits = _wait_intervals(in_window, now)
    try:
        import drivers
        plans = {w["model"]: w for w in drivers.active_plan_windows(events, now=now)
                 if w.get("model")}
    except Exception:
        plans = {}
    out = []
    for model, _fam, _harness, _tier, roster_cap, _roles in config.live_roster(check_api=False):
        try:
            cap = int(config.driver_limit(model))
        except Exception:
            cap = int(roster_cap or 0)
        busy_iv = _busy_intervals(model, runs, events, leases, cutoff, now)
        busy_s = _occupied_seconds(busy_iv, cap, cutoff, now)
        plan = plans.get(model)
        if plan and plan.get("since"):
            blocked = _clip_iv(float(plan["since"]), now, cutoff, now)
            if blocked and cap:
                busy_iv = busy_iv + [blocked] * cap
        masks = []
        for other, a, b in waits:
            if other == model:
                continue
            clipped = _clip_iv(a, b, cutoff, now)
            if clipped:
                masks.append(clipped)
        def _failed(ev):
            if ev.get("model") != model:
                return False
            kind = ev.get("type")
            if kind in ("driver.stale", "driver.cancelled"):
                return True
            if kind != "driver.error" or ev.get("capacity"):
                return False
            try:
                import drivers
                if drivers.is_usage_limit(str(ev.get("error") or "")):
                    return False
            except Exception:
                pass
            return True
        errs = sum(1 for e in in_window if _failed(e))
        dones = sum(1 for e in in_window
                    if e.get("type") == "driver.done" and e.get("model") == model)
        busy_hours = busy_s / 3600.0
        denom = cap * since_hours
        out.append({
            "model": model,
            "busy_hours": round(busy_hours, 3),
            "cap": cap,
            "utilization": round(100.0 * busy_hours / denom, 1) if denom else 0.0,
            "cap_waits": sum(1 for e in in_window
                             if e.get("type") == "driver.cap_wait" and e.get("model") == model),
            "idle_while_waiting_hours": round(
                _free_seat_hours(cap, busy_iv, masks, cutoff, now), 3),
            "error_rate": round(errs / (errs + dones), 3) if errs + dones else 0.0,
            "plan_spent": plan is not None,
            "resets_at": (plan or {}).get("resets_at"),
        })
    return out


def audit_seats(since_s=86400, store=None, **kw):
    """Warn when a seat sat idle for more than half the window while another was starved."""
    try:
        seats = seat_utilization(since_s / 3600.0, store=store, **kw)
    except Exception as exc:
        return [_finding("warning", "seats", "could not compute seat utilization",
                         str(exc)[:200], "check events.jsonl and harness_runs are readable")]
    hours = since_s / 3600.0
    starved = [s["model"] for s in seats if s["cap_waits"]]
    out = []
    for s in seats:
        others = [m for m in starved if m != s["model"]]
        if not others or s["idle_while_waiting_hours"] <= 0.5 * hours:
            continue
        if s["error_rate"] >= 0.5:
            action = "fix its errors"
        elif s["utilization"] >= 50:
            action = "raise its cap"
        else:
            action = "route more tiers to it"
        out.append(_finding(
            "warning", "seats",
            f"{s['model']} idle {s['idle_while_waiting_hours']:.1f}h "
            f"while {', '.join(others)} starved",
            f"cap {s['cap']}, utilization {s['utilization']:.0f}%, "
            f"{s['cap_waits']} cap-waits, error rate {s['error_rate']:.0%}",
            action))
    return out


def audit_gates(store=None, tasks_dir=None, repo=None):
    """Gates that pass WITHOUT the work being done.

    AGENTS.md Rule 4 requires a verify_cmd that fails when the work is wrong. A
    gate built from `grep -q '<string>' <file>` fails that rule whenever the
    string is already in the file: the implementer runs, sees nothing to prove,
    writes nothing, and the task dies as "no changes to publish" — which is
    exactly how archived-attention failed twice.

    A MERGED task's assertions passing is correct and expected, so those are
    excluded. Checking them anyway turns 4 real findings into 43 meaningless
    ones, which is its own kind of lie.
    """
    import re
    root = Path(repo or config.ROOT)
    tdir = Path(tasks_dir or config.TASKS_DIR)
    done = set()
    if store is not None:
        try:
            done = {r["id"] for r in store.code_tasks_all()
                    if r.get("status") == "merged"}
        except Exception:
            done = set()
    out = []
    for f in sorted(tdir.glob("*.json")) if tdir.is_dir() else []:
        try:
            proj = json.loads(f.read_text())["project"]
        except (OSError, ValueError, KeyError):
            continue
        if str(root.name) not in str(proj.get("repo", "")):
            continue
        for t in proj.get("tasks") or []:
            if t.get("id") in done:
                continue
            cmd = (t.get("verify_cmd") or "").strip()
            greps = re.findall(r"grep -q[i]* '([^']+)' ([^\s;&|]+)", cmd)
            checked = [(pat, path) for pat, path in greps if (root / path).exists()]
            if not checked:
                continue
            flag = "-qi" if "grep -qi" in cmd else "-q"
            passing = [f"{pat} in {path}" for pat, path in checked
                       if subprocess.run(["grep", flag, pat, str(root / path)],
                                         capture_output=True).returncode == 0]
            if len(passing) == len(checked):
                out.append(_finding(
                    "warning", "gates",
                    f"{f.name}::{t['id']} gate passes without the work",
                    "; ".join(passing[:3]),
                    "every grep in this verify_cmd already matches the "
                    "untouched file, so the gate cannot fail — give it an "
                    "assertion that is FALSE until the task is done"))
    return out


DB_BACKUP_KEEP_DAYS = 14


def _usable_backups_today(dest_dir, today=None):
    """Paths of the backups in dest_dir that were written TODAY and are usable.

    A file whose name looks like a backup is not one. A copy that died
    mid-write — a full disk, a killed process — leaves a stub behind, and a
    zero-byte stub satisfies sqlite's own `PRAGMA integrity_check` (there is
    nothing in it to be corrupt), so size and integrity are both checked, the
    same check the snapshot path applies before it counts a copy.

    The once-a-day guard must read THIS, never a raw glob: counting a stub as
    "already backed up today" would silently skip the day's real copy and
    leave the fleet with a file it can never restore from — precisely the
    silently-corrupt backup this function exists to prevent.

    Only today's files are opened: an audit must not re-read a directory of
    old backups to answer a question that is only about today.
    """
    import sqlite3
    day = today or time.strftime("%Y-%m-%d")
    out = []
    for p in sorted(dest_dir.glob("orchestrator-*.db")):
        try:
            if time.strftime("%Y-%m-%d", time.localtime(p.stat().st_mtime)) != day:
                continue
            if p.stat().st_size <= 0:
                continue
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            try:
                if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    continue
            finally:
                con.close()
        except (OSError, sqlite3.Error):
            continue  # vanished between glob and stat, or not a database
        out.append(p)
    return out


def audit_db_backup(db_path=None, snapshot=False, keep_days=DB_BACKUP_KEEP_DAYS):
    """The database is the fleet's memory, and nothing copied it.

    orchestrator.db holds every task's status, every lease, every error
    fingerprint, every harness run. The event log rotates; the taskfiles are
    snapshotted; the database just grew. One bad write on a full disk and the
    fleet forgets which of eighty tasks merged.

    The copy uses sqlite's online backup API, not a file copy: a file copy of a
    WAL-mode database mid-write is corrupt, silently, and you find out when you
    restore it. Each backup is then OPENED and integrity-checked before it is
    counted, because a backup nobody has verified is a hope, not a backup.

    Backups go to logs/db-backups/ (gitignored). Retained for `keep_days`.
    """
    import sqlite3
    src = Path(db_path or config.DB_PATH)
    dest_dir = Path(config.ROOT) / "logs" / "db-backups"
    out = []
    if not src.exists():
        return [_finding("warning", "backup", "database not found", str(src), "")]
    existing = sorted(dest_dir.glob("orchestrator-*.db")) if dest_dir.is_dir() else []
    newest_age_h = None
    if existing:
        newest_age_h = (time.time() - existing[-1].stat().st_mtime) / 3600
    if snapshot:
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = dest_dir / f"orchestrator-{stamp}.db"
        # One backup per DAY, however many times --fix runs. `main.py audit
        # --fix` passes snapshot=True (main.py:544), so anything that loops the
        # audit — a wrapper script, a retry, an operator re-running it — wrote
        # a fresh copy every invocation. Observed live 2026-09-15: thousands of
        # files at the same second, all younger than the retention window, so
        # age-based pruning could never catch up.
        #
        # Only a VERIFIED copy counts as today's (`_usable_backups_today`): a
        # copy that died mid-write leaves a stub whose mtime is still today,
        # and treating that stub as the day's backup would skip the real copy
        # and leave nothing restorable behind.
        today = time.strftime("%Y-%m-%d")
        fresh_today = _usable_backups_today(dest_dir, today)
        if fresh_today:
            out.append(_finding("info", "backup",
                                "database already backed up today",
                                str(sorted(fresh_today)[-1]),
                                "skipped a second copy (one per day)"))
            newest_age_h = 0.0
            return out
        try:
            # A connection used as a context manager commits, it does not
            # close. An open handle can recreate a WAL/SHM file after
            # rmtree has listed the directory, and the rmdir then fails
            # with ENOTEMPTY.
            a = sqlite3.connect(str(src))
            b = sqlite3.connect(str(dest))
            try:
                a.backup(b)
            finally:
                a.close()
                b.close()
            chk = sqlite3.connect(str(dest))
            try:
                ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
                n_tasks = chk.execute("SELECT COUNT(*) FROM code_tasks").fetchone()[0]
            finally:
                chk.close()
            if ok != "ok":
                dest.unlink(missing_ok=True)
                out.append(_finding("critical", "backup",
                                    "database backup FAILED integrity check", ok,
                                    "the live database may itself be corrupt — "
                                    "run PRAGMA integrity_check on it now"))
            else:
                out.append(_finding("info", "backup",
                                    f"database backed up ({dest.stat().st_size // 1024} KB, "
                                    f"{n_tasks} task rows, integrity ok)",
                                    str(dest), ""))
                newest_age_h = 0.0
        except sqlite3.Error as exc:
            # A copy that failed partway leaves a stub behind, and its mtime is
            # today — the next run's once-a-day guard would read it as the day's
            # backup and never take a real one. Drop it (best effort: the disk
            # may be full or the directory read-only, which is why this failed).
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            out.append(_finding("critical", "backup", "database backup failed",
                                str(exc)[:200], "check disk space and permissions"))
        # retention: keep the last N days, never fewer than 3 files
        cutoff = time.time() - keep_days * 86400
        keep = sorted(dest_dir.glob("orchestrator-*.db"))
        for old in keep[:-3]:
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
    if newest_age_h is None:
        out.append(_finding("warning", "backup", "the database has never been backed up",
                            str(src), "run: main.py audit --snapshot"))
    elif newest_age_h > 48:
        out.append(_finding("warning", "backup",
                            f"newest database backup is {newest_age_h:.0f}h old",
                            "", "the daily audit is not running — check the scheduler"))
    return out


def audit_tasks_backup(tasks_dir=None, snapshot=False):
    """Taskfiles are the DESIGN of every project and nothing versions them.

    They live in ~/tasks (config.TASKS_DIR), outside the repo, untracked. A
    taskfile holds the prompt, the decomposition, the model routing and the
    verify gate — days of engineering per wave — and an edit to one leaves no
    record of what it said before. I changed four gates today and could not
    have shown you the previous text.

    The snapshot goes to logs/ (gitignored), NOT into the repo: this repo is
    public, and operator task prompts are not mine to publish. Local history is
    the part that protects against a bad edit or a lost home directory; sharing
    them is a separate decision that belongs to the operator.
    """
    tdir = Path(tasks_dir or config.TASKS_DIR)
    files = sorted(tdir.glob("*.json")) if tdir.is_dir() else []
    if not files:
        return []
    snaps = Path(config.ROOT) / "logs" / "task-snapshots"
    today = snaps / time.strftime("%Y-%m-%d")
    if snapshot:
        try:
            today.mkdir(parents=True, exist_ok=True)
            for f in files:
                (today / f.name).write_text(f.read_text())
            return [_finding("info", "taskfiles",
                             f"snapshotted {len(files)} taskfile(s)",
                             str(today), "")]
        except OSError as exc:
            return [_finding("warning", "taskfiles", "snapshot failed",
                             str(exc)[:200], "")]
    have = sorted(p.name for p in snaps.glob("*")) if snaps.is_dir() else []
    if not have:
        return [_finding(
            "warning", "taskfiles",
            f"{len(files)} taskfile(s) have never been snapshotted",
            str(tdir),
            "they are untracked and unversioned — the prompts, gates and "
            "decomposition of every project exist in exactly one place. "
            "`main.py audit --fix` snapshots them to logs/task-snapshots/")]
    if have[-1] != time.strftime("%Y-%m-%d"):
        return [_finding("info", "taskfiles",
                         f"last taskfile snapshot: {have[-1]}", "",
                         "`main.py audit --fix` takes a fresh one")]
    return []


def audit_pr_collisions(store=None, repo=None):
    """Open PRs whose files a LIVE task is rewriting — a conflict you can see coming.

    Nothing warns about this today; you find out when the merge is refused,
    after the reviewers have already been spent. PR #9 carries changes to
    static/phone.html and static/usage.html while phone-shell and
    usage-informative are rewriting exactly those two files, so it is certain
    to conflict and equally certain to have been predictable.
    """
    repo = Path(repo or config.ROOT)
    out = []
    rc, raw, _ = _sh("gh", "pr", "list", "--state", "open", "--json",
                     "number,headRefName", cwd=repo, timeout=25)
    if rc != 0:
        # `gh` failed. Reporting no collisions would be an all-clear we did not
        # earn, and this check exists precisely to warn before a merge is
        # refused.
        return [_finding("info", "prs", "could not list pull requests",
                         "gh exited non-zero",
                         "collision warnings are unavailable until gh works")]
    try:
        prs = json.loads(raw) if raw.strip() else []
    except ValueError:
        prs = []
    if not prs:
        return out
    # What each live task is going to touch, from its taskfile.
    live_files = {}
    try:
        import reconcile
        live_ids = {r["id"] for r in (store.code_tasks_all() if store else [])
                    if r.get("status") in ("running", "in_review")}
        live_tf = {r.get("taskfile") for r in reconcile.live_runs()}
    except Exception:
        return out
    for tf in live_tf:
        try:
            proj = json.loads(Path(tf).read_text())["project"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        for t in proj.get("tasks") or []:
            if t.get("id") in live_ids:
                for f in t.get("files_hint") or []:
                    live_files.setdefault(f, set()).add(t["id"])
    for pr in prs:
        branch = pr.get("headRefName") or ""
        tid = branch[5:] if branch.startswith("task/") else None
        rc, out_files, _ = _sh("git", "diff", "--name-only",
                               f"origin/{config.BASE_BRANCH}...{branch}", cwd=repo)
        touched = {ln.strip() for ln in out_files.splitlines() if ln.strip()}
        hits = {f: sorted(live_files[f]) for f in touched & set(live_files)
                if tid not in live_files[f]}
        if hits:
            detail = "; ".join(f"{f} (being rewritten by {', '.join(who)})"
                               for f, who in sorted(hits.items())[:4])
            out.append(_finding(
                "warning", "prs",
                f"PR #{pr['number']} will conflict with work in flight",
                detail,
                "land or close it before those tasks merge, or expect a "
                "conflict after the reviewers have already been spent"))
    return out


def audit_invariants(store=None, repo=None):
    """Things that must be true if the pipeline is behaving, checked against reality.

    Each of these is a claim the database makes that git, the process table or
    GitHub can contradict. Every bug found today was a disagreement of exactly
    this shape — a status column believed over the world it describes — so the
    disagreements are now checked directly rather than discovered by their
    consequences.
    """
    if store is None:
        return []
    repo = Path(repo or config.ROOT)
    try:
        rows = [dict(r) for r in store.code_tasks_all()]
    except Exception as exc:
        return [_finding("warning", "invariants", "could not read code_tasks",
                         str(exc)[:200], "")]
    try:
        import reconcile
        live_tf = {r.get("taskfile") for r in reconcile.live_runs()}
    except Exception:
        return [_finding("info", "invariants", "cannot read the process table",
                         "", "skipping invariant checks rather than guessing")]

    rc, raw, _ = _sh("gh", "pr", "list", "--state", "open", "--json",
                     "number,headRefName", cwd=repo, timeout=25)
    prs, pr_known = {}, rc == 0
    if pr_known:
        try:
            prs = {p["headRefName"]: p["number"] for p in (json.loads(raw) if raw.strip() else [])}
        except ValueError:
            pr_known = False
    rc, wt, _ = _sh("git", "worktree", "list", "--porcelain", cwd=repo)
    trees = {Path(ln.split(" ", 1)[1]).name for ln in wt.splitlines()
             if ln.startswith("worktree ")}

    out = []
    for r in rows:
        tid, st = r.get("id"), r.get("status")
        if st == "running" and r.get("taskfile") not in live_tf:
            out.append(_finding(
                "warning", "invariants", f"{tid}: status 'running' but no run is alive",
                "", "its run died without settling the row — "
                    "main.py code reconcile --apply resets it"))
        if pr_known and st == "in_review" and f"task/{tid}" not in prs:
            out.append(_finding(
                "warning", "invariants", f"{tid}: status 'in_review' but no PR is open",
                "", "it cannot progress: re-run its project so publish "
                    "re-opens or re-attaches the PR"))
        if st == "merged" and tid in trees:
            out.append(_finding(
                "info", "invariants", f"{tid}: merged but its worktree remains",
                "", "main.py code reconcile --apply removes it"))
        if pr_known and st == "merged" and f"task/{tid}" in prs:
            out.append(_finding(
                "warning", "invariants",
                f"{tid}: merged but PR #{prs[f'task/{tid}']} is still open",
                "", "the merge did not close it — close it by hand"))
    known = {r.get("id") for r in rows}
    for br, n in prs.items():
        tid = br[5:] if br.startswith("task/") else None
        if tid and tid not in known:
            out.append(_finding(
                "warning", "invariants",
                f"PR #{n} is for task '{tid}', which the database does not know",
                "", "a branch from a lost database, or a hand-made PR on a "
                    "task/ branch — close it or recreate the task"))
    return out


def refresh_model_availability(timeout=20):
    """Snapshot the model ids the provider actually serves. Returns (ok, detail).

    config.live_roster consults this: a roster date is a plan, this is the fact.
    Never raises — a refresh that fails leaves the last snapshot in place, and
    an absent snapshot means "unknown", which falls back to the dates.
    """
    import urllib.error
    import urllib.request
    url = config.BASE_URL.rstrip("/") + "/models"
    key = os.getenv("ARC_API_KEY", "")
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        ids = sorted(m.get("id") or m.get("name") for m in (d.get("data") or d))
        if not ids:
            return False, "the API returned no models"
        path = Path(config.ROOT) / "logs" / "model-availability.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ts": time.time(), "base_url": config.BASE_URL,
                                    "models": ids}, indent=1))
        return True, f"{len(ids)} models"
    except (urllib.error.URLError, OSError, ValueError, TypeError) as exc:
        return False, str(exc)[:160]


def audit_roster():
    """Upcoming model transitions, and whether today's roster can staff the gate.

    The roster is dated (config.ROSTER): models arrive and leave on the
    provider's schedule. Two things an operator needs to hear before the day,
    not after: WHAT changes in the next two weeks, and whether the fleet left
    behind can still field PR_REVIEWERS cross-family reviewers for every
    implementer. It cannot on the two-model fleet of 2026-09-12 — two families
    means one cross-family reviewer each — and the merge gate quietly gets
    thinner unless someone is told.
    """
    out = []
    ok, detail = refresh_model_availability()
    if not ok:
        out.append(_finding("info", "roster", "could not refresh model availability",
                            detail, "the roster falls back to its dates until this works"))
    for c in config.roster_changes(horizon_days=14):
        sev = "warning" if c["in_days"] <= 2 else "info"
        out.append(_finding(
            sev, "roster", f"{c['model']} {c['change']} on {c['on']} ({c['in_days']}d)",
            "", "run the suite under ARC_ROSTER_DATE=%s before then" % c["on"]))
    try:
        import code_tasks
        for m in config.ESCALATION_PATH:
            fam = config.MODEL_FAMILY.get(m)
            pool = code_tasks._eligible_pr_reviewers(fam, None)
            if len(pool) < config.PR_REVIEWERS_WANTED:
                out.append(_finding(
                    "warning", "roster",
                    f"{m}'s PRs get {len(pool)} cross-family reviewer(s), "
                    f"config asks for {config.PR_REVIEWERS_WANTED}",
                    f"eligible: {pool}",
                    "the gate still needs unanimity among those who review; "
                    "lower PR_REVIEWERS to match, or add a review-capable family"))
    except Exception as exc:
        out.append(_finding("info", "roster", "could not evaluate reviewer coverage",
                            str(exc)[:120], ""))
    for m in config.deferred_models():
        out.append(_finding(
            "warning", "roster", f"{m} is scheduled but the API does not serve it",
            "", "the roster date has arrived and the provider has not — the "
                "incumbent is being kept; refresh with `main.py models refresh` "
                "once it appears"))
    for m in config.overstayed_models():
        out.append(_finding(
            "info", "roster", f"{m} is past its retirement date but still served",
            "", "kept live because its replacement is not available yet"))
    if not config.REVIEW_FAMILIES:
        out.append(_finding("critical", "roster", "NO review-capable model is live",
                            "", "nothing can be reviewed; the pipeline cannot merge"))
    return out


def audit_logs():
    out = []
    p = Path(config.EVENTS_LOG)
    try:
        size = p.stat().st_size
    except OSError:
        return [_finding("warning", "logs", "the event log is missing", str(p),
                         "no events will be recorded and the dashboard shows nothing; "
                         "start a run to recreate it, or check config.EVENTS_LOG points "
                         "at a writable path")]
    mb = size / 1e6
    if mb > 80:
        out.append(_finding("warning", "logs", f"event log is {mb:.0f} MB",
                            "rotation fires at 100 MB",
                            "expect a rotation soon; confirm the dashboard's "
                            "cursor handles it"))
    gates = Path(config.ROOT) / "logs" / "gates"
    n = len(list(gates.glob("*.log"))) if gates.is_dir() else 0
    if n > 200:
        out.append(_finding("info", "logs", f"{n} gate logs retained", "",
                            "prune logs/gates if disk matters"))
    return out


def audit_health():
    """Does the thing still build and pass its own gate?"""
    out = []
    # check.sh runs the whole suite (unit + JS + UI + taskfile checks) and on a
    # loaded box it is slower than a quiet one: a 600s cap reported "check.sh
    # FAILS" while a manual run right after passed in ~30s of tests under the
    # concurrent fleet. Give it real headroom, and if it DOES time out say so
    # instead of reporting a red gate — a timeout is our measurement failing,
    # not the code.
    budget = int(os.getenv("ARC_AUDIT_CHECK_TIMEOUT", "1800"))
    rc, so, se = _sh("./check.sh", timeout=budget)
    if rc != 0:
        combined = (so + se)
        if not so.strip() and "timed out" in se.lower():
            out.append(_finding(
                "warning", "health",
                f"check.sh did not finish within {budget}s",
                se.strip()[:200],
                "the audit's subprocess cap was hit under load — this is not a "
                "red gate; re-run with ARC_AUDIT_CHECK_TIMEOUT raised or on an "
                "idle box to get a real verdict"))
            return out
        tail = combined.strip().splitlines()[-12:]
        out.append(_finding("critical", "health", "check.sh FAILS",
                            "\n".join(tail),
                            "this is the gate every task must pass — nothing "
                            "can merge while it is red"))
    else:
        n = ""
        for ln in so.splitlines():
            if "Ran " in ln and " test" in ln:
                n = ln.strip()
        out.append(_finding("info", "health", "check.sh passes", n, ""))
    return out


# ---- report --------------------------------------------------------------

def run(store=None, since_s=86400, with_health=True, snapshot=False):
    findings = []
    findings += triage_errors(since_s)
    if store is not None:
        findings += triage_tasks(store)
        findings += audit_leases(store)
    findings += audit_seats(since_s, store)
    findings += audit_git(store=store)
    findings += audit_gates(store)
    findings += audit_pr_collisions(store)
    findings += audit_invariants(store)
    findings += audit_roster()
    findings += board_health_findings(since_hours=max(1.0, since_s / 3600.0))
    findings += audit_watchdog()
    findings += audit_tasks_backup(snapshot=snapshot)
    findings += audit_db_backup(snapshot=snapshot)
    findings += audit_logs()
    if with_health:
        findings += audit_health()
    order = {s: i for i, s in enumerate(SEV)}
    findings.sort(key=lambda f: order.get(f["severity"], 9))
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in SEV}
    return {"ts": time.time(), "since_s": since_s,
            "counts": counts, "findings": findings}


def render(report):
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(report["ts"]))
    c = report["counts"]
    lines = [f"ARC audit — {when}",
             f"  {c['critical']} critical · {c['warning']} warning · {c['info']} info",
             ""]
    if not report["findings"]:
        lines.append("  nothing to report.")
    cur = None
    for f in report["findings"]:
        if f["severity"] != cur:
            cur = f["severity"]
            lines.append(f"[{cur.upper()}]")
        lines.append(f"  {f['area']}: {f['what']}")
        if f["detail"]:
            for ln in str(f["detail"]).splitlines()[:6]:
                lines.append(f"      {ln[:160]}")
        if f["action"]:
            lines.append(f"      -> {f['action'][:200]}")
    return "\n".join(lines)
