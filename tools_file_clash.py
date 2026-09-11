"""Would starting these taskfiles collide with work already in flight?

Two agents editing one file is the main cause of `conflict` in this fleet, and
the answer is not obvious by eye: a taskfile's OTHER tasks may have merged
hours ago and their files are then free. An earlier version of this check
aggregated every task in any taskfile that had any live task, and reported a
clash on a file whose owner had long since merged.

Only tasks that are themselves non-terminal own anything.
"""
import json
import pathlib
import sqlite3
import sys

LIVE = ("running", "in_review", "conflict")


def owned(db_path="orchestrator.db"):
    """{file: [task ids]} for tasks that are actually still in flight."""
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    out = {}
    rows = db.execute(
        f"SELECT id, taskfile FROM code_tasks WHERE status IN "
        f"({','.join('?' * len(LIVE))})", LIVE).fetchall()
    for r in rows:
        try:
            proj = json.loads(pathlib.Path(r["taskfile"]).read_text())["project"]
        except (OSError, ValueError, KeyError):
            continue
        for t in proj.get("tasks") or []:
            if t.get("id") != r["id"]:
                continue                      # a SIBLING's files are not ours
            for f in t.get("files_hint") or []:
                out.setdefault(f, []).append(r["id"])
    return out


def wants(*stems, tasks_dir=None, db_path="orchestrator.db"):
    """Files these taskfiles would actually touch on their NEXT run.

    A MERGED task is excluded: on resume it collapses to a skip stub and edits
    nothing. Counting it reported a clash on code_tasks.py between
    graph-patterns-research — merged hours earlier — and a live task, which
    would have blocked a relaunch for work that was never going to happen.
    """
    d = pathlib.Path(tasks_dir or (pathlib.Path.home() / "tasks"))
    done = set()
    try:
        db = sqlite3.connect(db_path)
        done = {r[0] for r in db.execute(
            "SELECT id FROM code_tasks WHERE status='merged'")}
    except sqlite3.Error:
        pass
    out = {}
    for stem in stems:
        proj = json.loads((d / f"{stem}.json").read_text())["project"]
        for t in proj["tasks"]:
            if t.get("id") in done:
                continue
            for f in t.get("files_hint") or []:
                out.setdefault(f, []).append(f"{stem}:{t['id']}")
    return out


def check(*stems, db_path="orchestrator.db", tasks_dir=None):
    """Clashes between `stems` and OTHER in-flight work.

    A taskfile's own tasks are excluded. Re-running a taskfile to resume it is
    the normal recovery path — resync, conflict repair, a retry after a failed
    merge — and refusing because the task being resumed already holds its own
    files would block exactly the operation that fixes things. The first
    version did that: it told me not to relaunch projects-ui-and-patterns
    because projects-ui-cleanup held static/index.html, and
    projects-ui-cleanup IS the task that taskfile exists to run.
    """
    held = owned(db_path)
    want = wants(*stems, tasks_dir=tasks_dir, db_path=db_path)
    mine = set()
    d = pathlib.Path(tasks_dir or (pathlib.Path.home() / "tasks"))
    for stem in stems:
        try:
            proj = json.loads((d / f"{stem}.json").read_text())["project"]
        except (OSError, ValueError, KeyError):
            continue
        mine |= {t["id"] for t in proj.get("tasks") or []}
    clashes = {}
    for f in set(held) & set(want):
        others = [t for t in held[f] if t not in mine]
        if others:
            clashes[f] = (others, want[f])
    return clashes, held, want


def internal(*stems, tasks_dir=None, db_path="orchestrator.db"):
    """Files two of the LAUNCHED taskfiles would both touch.

    check() only compares a batch against work already in flight, which is a
    different question and silently answered "safe" for a wave whose own
    members collide. Launching dashboard-modularise beside dashboard-ux is
    exactly that case: both own static/index.html, and nothing was in flight to
    reveal it.
    """
    want = {}
    d = pathlib.Path(tasks_dir or (pathlib.Path.home() / "tasks"))
    done = set()
    try:
        db = sqlite3.connect(db_path)
        done = {r[0] for r in db.execute(
            "SELECT id FROM code_tasks WHERE status='merged'")}
    except sqlite3.Error:
        pass
    for stem in stems:
        try:
            proj = json.loads((d / f"{stem}.json").read_text())["project"]
        except (OSError, ValueError, KeyError):
            continue
        for t in proj.get("tasks") or []:
            if t.get("id") in done:
                continue
            for f in t.get("files_hint") or []:
                want.setdefault(f, set()).add(stem)
    return {f: sorted(v) for f, v in want.items() if len(v) > 1}


if __name__ == "__main__":
    stems = sys.argv[1:]
    clashes, held, want = check(*stems)
    within = internal(*stems)
    print("  in flight:", ", ".join(sorted(held)) or "nothing")
    for f, (by, mine) in sorted(clashes.items()):
        print(f"  CLASH {f}: held by {by}, wanted by {mine}")
    for f, projects in sorted(within.items()):
        print(f"  CLASH {f}: {' and '.join(projects)} would both edit it")
    bad = bool(clashes or within)
    print("  ->", "DO NOT launch together" if bad else "SAFE to launch")
    sys.exit(1 if bad else 0)
