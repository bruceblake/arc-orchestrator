"""Fleet watchdog: keep every started project running until it is done.

`main.py code run <taskfile>` is already the resume path (merged tasks are
skipped, interrupted tasks restart at the same tier, open PRs re-attach), and
drivers already sleep through a usage limit until its `resets_at`. What was
missing is something that notices a run process is GONE — killed, crashed,
OOM'd with WSL, or exited on a chain gate — and runs that command again.

Each tick:

1. **Record** every live `code run` exactly as it was launched: argv, cwd and
   environment (runs are split across checkouts and ARC_FLEET values, and a
   service's bare environment lacks the PATH that finds the harnesses).
   Kept in logs/watchdog/runs.json (0600: the environment carries keys).
2. **Resume** every recorded taskfile that has no live run and still has
   unfinished work, with the recorded argv/cwd/env. A taskfile with
   `project.after` is launched only once `chain_status` is ready, so a blocked
   chain waits here instead of exiting and relaunching in a loop.
3. **Back off.** A run that dies within MIN_HEALTHY_S counts as a quick
   failure; each one doubles the wait (10 min .. 2 h). After MAX_QUICK_FAILS
   in a row the taskfile is parked and reported — it needs a person, not
   another launch. Any merge resets the counter.

Taskfiles live outside the repo (~/tasks, sometimes /tmp) and a reboot takes
the ones under /tmp with it. So on first sight of a live run its taskfile is
copied to logs/watchdog/taskfiles/ (mangled path name, never overwritten by a
copy taken from a different path, and never by a source OLDER than the copy),
a watched taskfile under a temp directory raises
`watchdog.tmp_taskfile`, and a taskfile whose original has vanished is
restored from its copy before the run is resumed (`watchdog.taskfile_kept` /
`watchdog.taskfile_restored`).

It never kills a run and never touches git. It DOES write one file of its own:
a taskfile whose original has been deleted is recreated at that same path from
its durable copy, and only then, because the resume path is driven by the file
on disk. An existing taskfile is never touched, and an existing copy is never
replaced by a source older than it.

Status: logs/watchdog/status.json, log: logs/watchdog/watchdog.log,
events: watchdog.resume / watchdog.parked / watchdog.done /
watchdog.taskfile_kept / watchdog.taskfile_restored / watchdog.tmp_taskfile.

    .venv/bin/python fleetwatch.py            # loop forever (the service)
    .venv/bin/python fleetwatch.py --once     # one tick
    .venv/bin/python fleetwatch.py --watch <taskfile>   # add a taskfile
    .venv/bin/python fleetwatch.py --ignore <taskfile>  # stop watching one
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import config  # noqa: E402
import events  # noqa: E402
import reconcile  # noqa: E402
from store import Store  # noqa: E402

STATE_DIR = Path(os.environ.get("ARC_WATCHDOG_DIR", HERE / "logs" / "watchdog"))
TICK_S = int(os.environ.get("ARC_WATCHDOG_TICK", "300"))
MIN_HEALTHY_S = 300
BACKOFF_MIN_S = 600
BACKOFF_MAX_S = 7200
MAX_QUICK_FAILS = 6
# A task in one of these states will not move by re-running: `failed` after
# the last escalation tier is a capability verdict, not an interruption.
TERMINAL = {"merged", "skipped"}
# Durable copies of the taskfiles in flight — see `keep_taskfile`.
TASKFILE_DIR = "taskfiles"
# A taskfile under one of these is one reboot away from being gone: WSL wipes
# /tmp on restart, and a taskfile that vanished is what parked seven runs on
# 2026-09-24. A watched taskfile living here is reported below.
TMP_DIRS = tuple(dict.fromkeys(("/tmp", "/var/tmp", tempfile.gettempdir())))


def _now():
    return time.time()


def _load(name, default):
    try:
        return json.loads((STATE_DIR / name).read_text())
    except (OSError, ValueError):
        return default


def _save(name, obj):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = STATE_DIR / name
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
    os.replace(tmp, p)


def log(msg):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(STATE_DIR / "watchdog.log", "a") as f:
        f.write(line + "\n")


def _proc_launch(pid):
    """(argv, cwd, env) of a live process, or None if it vanished."""
    p = Path(f"/proc/{pid}")
    try:
        argv = [a for a in (p / "cmdline").read_bytes().decode(errors="replace")
                .split("\0") if a]
        cwd = os.readlink(p / "cwd")
        env = dict(kv.split("=", 1) for kv in (p / "environ").read_bytes()
                   .decode(errors="replace").split("\0") if "=" in kv)
    except OSError:
        return None
    return argv, cwd, env


def _under_tmp(p):
    """True when `p` (any path form) resolves inside a temp directory."""
    try:
        q = str(Path(p).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return any(q == d or q.startswith(d + os.sep) for d in TMP_DIRS)


def _tf_copy_name(tf):
    """The durable copy's filename: the basename, made unique by a path digest.

    Two different files can share a basename (`~/tasks/plan.json` and
    `/tmp/wave2/plan.json` both exist in practice), so a digest of the full
    path is what keeps them apart. Path-mangled rather than content-addressed
    because the copy must be findable from the taskfile PATH alone: on resume
    the original may be gone and nothing else identifies it.
    """
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", Path(tf).stem)[:60] or "taskfile"
    digest = hashlib.sha256(str(Path(tf).expanduser()).encode()).hexdigest()[:12]
    return f"{stem}.{digest}.json"


def keep_taskfile(tf, rec=None):
    """Copy the taskfile at `tf` into the watchdog state dir; return the copy.

    The durable instance of a plan: taskfiles live in ~/tasks or /tmp, neither
    of which survives a WSL reboot or a tmpfs wipe, and on 2026-09-24 a restart
    left seven runs with rows stuck at `running` and no file to resume.

    The copy is written once, on first sight, and then left alone — resume is
    driven by the file on disk, and re-reading every tick could capture an
    in-flight edit. It is only replaced when all three hold: the source's bytes
    differ, the source is the same path the copy was taken from (a different
    path that happens to share the basename must never silently swap plans),
    and the source is STRICTLY NEWER than the copy. The mtime rule is what
    makes "never overwriting a newer copy" true: a rollback that preserved
    mtimes, a half-finished write, or a tick that read stale bytes would
    otherwise replace a good durable copy with older content, and when the
    original is gone the copy is all that is left.
    """
    src = Path(tf)
    if not src.is_file():
        return rec.get("taskfile_copy") if rec else None
    dest = STATE_DIR / TASKFILE_DIR / _tf_copy_name(tf)
    try:
        text = src.read_text(encoding="utf-8")
    except OSError:
        return rec.get("taskfile_copy") if rec else None
    prior = dest.read_text(encoding="utf-8") if dest.is_file() else None
    try:
        fresh = prior is None or src.stat().st_mtime_ns > dest.stat().st_mtime_ns
    except OSError:
        fresh = prior is None
    if prior != text and fresh and \
            (rec is None or rec.get("taskfile_source") in (None, str(src))):
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, dest)
            if rec is not None:
                rec["taskfile_source"] = str(src)
            log(f"KEEP {src.name} -> {dest} ({len(text)} bytes)")
            events.emit("watchdog.taskfile_kept", taskfile=str(tf),
                        copy=str(dest), bytes=len(text))
        except OSError as exc:
            log(f"KEEP FAILED {src}: {type(exc).__name__}: {exc}")
            return rec.get("taskfile_copy") if rec else None
    if not dest.is_file():
        return None
    if rec is not None:
        rec.setdefault("taskfile_source", str(src))
        rec["taskfile_copy"] = str(dest)
    return dest


def restore_taskfile(tf, rec=None):
    """Put a durable copy back at `tf` when the original is gone; report it.

    Returns the path to run from: the original when it exists, `tf` after a
    successful restore, else None. The restore is deliberate — the copy was
    taken from this exact path, so writing it back changes nothing for any
    other reader (a `deps` entry, a chain gate), while resuming with
    logs/watchdog/taskfiles/ as the run's taskfile would resolve
    `project.after` relative to the wrong directory.
    """
    p = Path(tf)
    if p.is_file():
        return tf
    copy = (rec or {}).get("taskfile_copy") or str(
        STATE_DIR / TASKFILE_DIR / _tf_copy_name(tf))
    src = Path(copy)
    if not src.is_file():
        return None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".restore.tmp")
        tmp.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        os.replace(tmp, p)
    except OSError as exc:
        log(f"RESTORE FAILED {tf}: {type(exc).__name__}: {exc}")
        return None
    log(f"RESTORE {tf} <- {src} (original gone; resuming from the durable "
        "copy taken while it was live)")
    events.emit("watchdog.taskfile_restored", taskfile=str(tf), copy=str(src))
    return tf


def _unfinished(store, tf):
    """Task ids of `tf` that re-running could still move, or None if unreadable.

    `failed` rows count as unfinished: resume restarts interrupted ones at the
    same tier and capability failures one tier up (Rule 4). The quick-failure
    parking is what stops a task that fails every time from looping.
    """
    try:
        tasks = json.loads(Path(tf).read_text())["project"]["tasks"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    rows = {r["id"]: r["status"] for r in store.code_tasks_for(tf)}
    return [t["id"] for t in tasks if rows.get(t["id"]) not in TERMINAL]


def _after(tf):
    """`project.after`, resolved as the loader does (bare names -> ~/tasks).

    Not `load_taskfile`: it refuses a taskfile planned for another ARC_FLEET
    than this process's, and the watchdog serves every fleet.
    """
    import code_tasks
    return code_tasks._read_after(tf)


def _merged_count(store, tf):
    return sum(1 for r in store.code_tasks_for(tf) if r["status"] == "merged")


def _stopped_since(since):
    """{taskfile: ts} of `run.stopped` events after `since`.

    `run.stopped` is emitted only when a run receives SIGTERM/SIGINT — the
    dashboard's Stop button, `kill`, Ctrl-C. That is a person deciding the run
    should end, and resuming it would overrule them. A crash, SIGKILL, OOM or
    WSL shutdown emits nothing, and those are exactly the deaths to resume.
    """
    out = {}
    try:
        with open(config.EVENTS_LOG, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 8 * 1024 * 1024))
            tail = f.read().decode(errors="replace").splitlines()[1:]
    except OSError:
        return out
    for line in tail:
        if '"run.stopped"' not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("ts", 0) > since and e.get("taskfile"):
            out[str(Path(e["taskfile"]).resolve())] = e["ts"]
    return out


def _launch(tf, rec):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    out = STATE_DIR / "runs" / f"{Path(tf).stem}-{time.strftime('%Y%m%d-%H%M%S')}.log"
    out.parent.mkdir(parents=True, exist_ok=True)
    fh = open(out, "ab")
    proc = subprocess.Popen(rec["argv"], cwd=rec["cwd"], env=config.child_env(rec["env"]),
                            stdin=subprocess.DEVNULL, stdout=fh, stderr=fh,
                            start_new_session=True)
    fh.close()
    return proc.pid, out


def tick(store):
    runs = _load("runs.json", {})
    for rec in runs.values():
        if isinstance(rec.get("env"), dict):
            rec["env"] = config.child_env(rec["env"])
    ignore = set(_load("ignore.json", []))
    live = {}
    for r in reconcile.live_runs():
        if not r.get("taskfile"):
            continue
        got = _proc_launch(r["pid"])
        if not got:
            continue
        argv, cwd, env = got
        tf = str((Path(cwd) / r["taskfile"]).resolve())
        live[tf] = r["pid"]
        if tf in ignore:
            continue
        rec = runs.setdefault(tf, {})
        if rec.get("pid") != r["pid"]:
            rec.update(started=_now())
        # --force is a one-off human override; never replay it.
        rec.update(argv=[a for a in argv if a != "--force"], cwd=cwd, env=config.child_env(env),
                   pid=r["pid"], last_seen=_now(), parked=False)
        rec.setdefault("quick_fails", 0)
        keep_taskfile(tf, rec)
        if _under_tmp(tf) and not rec.get("tmp_warned"):
            rec["tmp_warned"] = True
            log(f"TMP {tf}: this taskfile lives under a temp directory, which a "
                "reboot wipes — the durable copy is what resumes it (move the "
                f"plan to {config.TASKS_DIR} to fix it)")
            events.emit("watchdog.tmp_taskfile", taskfile=tf,
                        copy=rec.get("taskfile_copy"))

    status = {"ts": _now(), "live": {}, "waiting": {}, "parked": {}, "done": []}
    stopped = _stopped_since(min((r.get("started") or _now()) for r in runs.values())
                             if runs else _now())
    for tf, rec in list(runs.items()):
        if tf in ignore:
            runs.pop(tf)
            continue
        if tf in live:
            status["live"][tf] = live[tf]
            rec["merged_seen"] = max(rec.get("merged_seen", 0), _merged_count(store, tf))
            continue
        left = _unfinished(store, tf)
        if left is None:
            # The file itself is gone — a WSL reboot wiped /tmp, or somebody
            # moved it. Put the durable copy back where it was and carry on;
            # resume is driven by the file on disk, so without this the row
            # stays `running` forever and nothing ever picks it up.
            if restore_taskfile(tf, rec):
                left = _unfinished(store, tf)
        if left is None:
            status["parked"][tf] = "taskfile unreadable"
            continue
        if not left:
            log(f"DONE {tf}: every task merged or skipped")
            events.emit("watchdog.done", taskfile=tf)
            status["done"].append(tf)
            runs.pop(tf)
            continue
        # The run we were watching is gone. Was it stopped on purpose?
        if rec.get("pid") and stopped.get(tf, 0) > rec.get("started", 0):
            log(f"STOPPED {tf}: run.stopped (SIGTERM/Ctrl-C) — an operator "
                "ended it; no longer watching (re-add with --watch)")
            events.emit("watchdog.released", taskfile=tf, reason="run.stopped")
            runs.pop(tf)
            continue
        # Was it healthy?
        if rec.get("pid"):
            lived = rec.get("last_seen", 0) - rec.get("started", 0)
            merged = _merged_count(store, tf)
            if merged > rec.get("merged_seen", 0):
                rec["quick_fails"] = 0
            elif lived < MIN_HEALTHY_S:
                rec["quick_fails"] = rec.get("quick_fails", 0) + 1
            rec["merged_seen"] = merged
            rec["exited"] = _now()
            rec["pid"] = None
            log(f"EXIT {Path(tf).name}: lived {lived:.0f}s, "
                f"{len(left)} unfinished, quick_fails={rec['quick_fails']}")
        if rec.get("quick_fails", 0) >= MAX_QUICK_FAILS:
            if not rec.get("parked"):
                log(f"PARK {tf}: {rec['quick_fails']} quick exits in a row — "
                    f"needs a person (see {STATE_DIR / 'runs'})")
                events.emit("watchdog.parked", taskfile=tf,
                            quick_fails=rec["quick_fails"], unfinished=left)
                rec["parked"] = True
            status["parked"][tf] = f"{rec['quick_fails']} quick exits; unfinished {left}"
            continue
        wait = min(BACKOFF_MAX_S, BACKOFF_MIN_S * (2 ** rec.get("quick_fails", 0))) \
            if rec.get("quick_fails") else 60
        since = _now() - rec.get("exited", 0)
        if since < wait:
            status["waiting"][tf] = f"backoff {wait - since:.0f}s"
            continue
        after = _after(tf)
        if after:
            import code_tasks
            st = code_tasks.chain_status(store, after)
            if not st["ok"]:
                status["waiting"][tf] = ("chain: " + "; ".join(
                    f"{Path(d['taskfile']).name} {d['merged']}/{d['n_tasks']}"
                    + (f" failed {d['failed']}" if d["failed"] else "")
                    for d in st["deps"]))
                continue
        pid, out = _launch(tf, rec)
        rec.update(pid=pid, started=_now(), last_seen=_now())
        log(f"RESUME {Path(tf).name} pid={pid} unfinished={left} log={out}")
        events.emit("watchdog.resume", taskfile=tf, pid=pid, unfinished=left,
                    quick_fails=rec.get("quick_fails", 0))
        status["live"][tf] = pid
    _save("runs.json", runs)
    _save("status.json", status)
    return status


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--watch", metavar="TASKFILE",
                    help="watch a taskfile that is not running now, launched "
                         "like the recorded run of --like (default: any)")
    ap.add_argument("--like", metavar="TASKFILE",
                    help="with --watch: copy argv/cwd/env from this recorded run")
    ap.add_argument("--ignore", metavar="TASKFILE")
    ap.add_argument("--unpark", metavar="TASKFILE")
    args = ap.parse_args()
    store = Store(config.DB_PATH)
    if args.ignore:
        tf = str(Path(args.ignore).resolve())
        ign = set(_load("ignore.json", []))
        ign.add(tf)
        _save("ignore.json", sorted(ign))
        log(f"IGNORE {tf}")
        return
    if args.unpark:
        tf = str(Path(args.unpark).resolve())
        runs = _load("runs.json", {})
        if tf in runs:
            runs[tf].update(quick_fails=0, parked=False)
            _save("runs.json", runs)
            log(f"UNPARK {tf}")
        return
    if args.watch:
        tf = str(Path(args.watch).resolve())
        runs = _load("runs.json", {})
        tpl = runs.get(str(Path(args.like).resolve())) if args.like else None
        tpl = tpl or next((r for r in runs.values() if r.get("argv")), None)
        if not tpl:
            sys.exit("no recorded run to copy a launch from; run a tick first")
        i = next(i for i, a in enumerate(tpl["argv"]) if Path(a).name == "main.py")
        argv = tpl["argv"][:i + 1] + ["code", "run", tf]
        runs[tf] = {"argv": argv, "cwd": tpl["cwd"], "env": tpl["env"],
                    "pid": None, "quick_fails": 0, "exited": 0}
        _save("runs.json", runs)
        log(f"WATCH {tf} (launch like {tpl['cwd']})")
        return
    while True:
        try:
            st = tick(store)
            log(f"tick: live={len(st['live'])} waiting={len(st['waiting'])} "
                f"parked={len(st['parked'])}")
        except Exception as exc:  # the watchdog must outlive its own bugs
            log(f"tick error: {type(exc).__name__}: {exc}")
        if args.once:
            return
        time.sleep(TICK_S)


if __name__ == "__main__":
    main()
