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


def wants(*stems, tasks_dir=None):
    d = pathlib.Path(tasks_dir or (pathlib.Path.home() / "tasks"))
    out = {}
    for stem in stems:
        proj = json.loads((d / f"{stem}.json").read_text())["project"]
        for t in proj["tasks"]:
            for f in t.get("files_hint") or []:
                out.setdefault(f, []).append(f"{stem}:{t['id']}")
    return out


def check(*stems, db_path="orchestrator.db", tasks_dir=None):
    held, want = owned(db_path), wants(*stems, tasks_dir=tasks_dir)
    clashes = {f: (held[f], want[f]) for f in set(held) & set(want)}
    return clashes, held, want


if __name__ == "__main__":
    clashes, held, want = check(*sys.argv[1:])
    print("  in flight:", ", ".join(sorted(held)) or "nothing")
    for f, (by, mine) in sorted(clashes.items()):
        print(f"  CLASH {f}: held by {by}, wanted by {mine}")
    print("  ->", "SAFE to launch" if not clashes else "DO NOT launch")
    sys.exit(1 if clashes else 0)
