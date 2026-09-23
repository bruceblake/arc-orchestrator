"""Shared board for agents working one task, and one project.

A harness session id belongs to the harness that minted it. After a usage
swap, the next fix round used to hand that id to a different CLI (`codex exec
resume` with a Cursor chat id) and die with "no rollout found". The board is
the context that survives the swap: every implementer, reviewer and handoff
appends one JSON line, and the next prompt quotes the recent lines.

Two files, same line shape:

- `.arc/board.jsonl` in the task worktree — this task's thread. Never
  committed (`git add -A` pathspec-excludes it, like plan proposals).
- `logs/boards/<project>.jsonl` — every task in the project, so a sibling
  can see an interface another task already settled. Outside any worktree,
  so publish cannot pick it up.

Agents may append a line themselves. The orchestrator also posts, so a swap
is recorded even when the agent never writes the file.
"""
import json
import time
import uuid
from pathlib import Path

import events
import config

REL = ".arc/board.jsonl"
KINDS = ("note", "handoff", "result", "error")
_BODY_MAX = 400


def _slug(project):
    return "".join(c if c.isalnum() or c in "._-" else "-" for c in str(project or "project"))


def project_path(project, create=False):
    d = config.BOARD_DIR
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d / f"{_slug(project)}.jsonl"


def task_path(worktree):
    p = Path(worktree) / REL
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def post(worktree, *, task, role, model, harness, kind="note", body="",
         session_id=None, project=None):
    """Append one post. Returns its id. Never raises."""
    kind = kind if kind in KINDS else "note"
    rec = {
        "id": uuid.uuid4().hex[:12],
        "ts": round(time.time(), 3),
        "task": str(task or ""),
        "role": str(role or ""),
        "model": str(model or ""),
        "harness": str(harness or ""),
        "kind": kind,
        "body": (body or "").replace("\n", " ")[:_BODY_MAX],
    }
    if session_id:
        # Named so a later prompt can say which harness may resume it.
        rec["session_id"] = str(session_id)
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    try:
        with task_path(worktree).open("a", encoding="utf-8") as f:
            f.write(line)
        if project:
            with project_path(project, create=True).open("a", encoding="utf-8") as f:
                f.write(line)
        events.emit("board.post", task=rec["task"], role=rec["role"],
                    model=rec["model"], harness=rec["harness"],
                    kind=rec["kind"], post=rec["id"])
    except OSError as exc:
        events.emit("board.post_failed", task=rec["task"], error=str(exc)[:200])
    return rec["id"]


def _read(path, limit):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("body"):
            out.append(obj)
    return out


def recent(worktree, limit=8):
    return _read(Path(worktree) / REL, limit)


def project_recent(project, limit=8):
    return _read(project_path(project), limit)


def prompt_block(worktree, project=None, task=None, limit=8):
    """Text for an implement or review prompt, or "" when the board is empty."""
    mine = recent(worktree, limit)
    others = []
    if project:
        for row in project_recent(project, limit):
            if task and row.get("task") == task:
                continue
            others.append(row)
    if not mine and not others:
        return ""
    lines = [
        "SHARED BOARD. Posts are how agents on this task (and sibling tasks "
        "in the project) hand work across harnesses. A session_id resumes "
        "ONLY on the harness named in that post — never pass another "
        "harness's id to your own resume.",
        "You may append one JSON line to .arc/board.jsonl "
        '{"kind":"note","body":"<one sentence>"} — keep body under 400 characters.',
        "",
    ]
    def fmt(row):
        sid = f" session={row['session_id']} (harness {row.get('harness') or '?'})" if row.get("session_id") else ""
        return (f"- [{row.get('kind') or 'note'}] {row.get('task') or '?'} "
                f"{row.get('role') or '?'} {row.get('model') or '?'} "
                f"via {row.get('harness') or '?'}{sid}: {row.get('body')}")
    if mine:
        lines.append("This task:")
        lines.extend(fmt(r) for r in mine)
    if others:
        lines.append("Other tasks in this project:")
        lines.extend(fmt(r) for r in others[-4:])
    return "\n".join(lines) + "\n"
