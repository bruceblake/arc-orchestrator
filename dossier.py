"""The task dossier: a durable, structured handoff in and out of every agent run.

A restart (reboot, watchdog relaunch), a plan-window swap to another harness,
or a tier escalation used to hand the next agent only the spec and the last
gate/review failure. It re-read the repo, retried approaches that had already
failed, and reversed decisions made two attempts earlier. The dossier is the
record that survives all three:

  * OUT of every run — the orchestrator records each attempt's outcome
    (`record_attempt`), and the agent writes `.arc/handoff.md` in its worktree
    (Done / Remaining / Decisions / Dead ends / Gotchas / Next step), which
    `harvest_handoff` reads and DELETES (the plan-proposal harvest pattern:
    publish does `git add -A`, and gitstore.CHANNEL_FILES unstages it too).
  * IN to every run — `render` is the prompt block every implement, review
    and PR-review prompt starts with once any history exists ("boot
    injection": context rebuilt from the record, not from a chat summary).

One row per (project, task) in `task_dossier` in config.DB_PATH; `data` is a
JSON object. Every write is a read-modify-write under BEGIN IMMEDIATE, so the
review fan-out and the implementer never lose each other's updates.
"""
import json
import re
import sqlite3
import time
from pathlib import Path

import config

HANDOFF_REL = ".arc/handoff.md"
MAX_ATTEMPTS = 20
MAX_LIST = 30          # decisions / dead ends / notes kept
OUTCOMES = ("gate_failed", "review_rejected", "crashed", "usage_swap",
            "passed", "merged")
# handoff.md heading -> dossier key. Scalars: newest wins. Lists: accumulate.
SECTIONS = {"done": "done", "remaining": "remaining", "decisions": "decisions",
            "dead ends": "dead_ends", "gotchas": "gotchas",
            "next step": "next_step"}
LIST_KEYS = ("decisions", "dead_ends")

SCHEMA = """CREATE TABLE IF NOT EXISTS task_dossier(
  project TEXT NOT NULL,
  task TEXT NOT NULL,
  updated_at REAL NOT NULL,
  data TEXT NOT NULL,
  PRIMARY KEY (project, task)
)"""


def _connect():
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    conn.execute(SCHEMA)
    return conn


def _empty():
    return {"attempts": [], "rolled_up": 0, "done": "", "remaining": "",
            "next_step": "", "gotchas": "", "decisions": [], "dead_ends": [],
            "notes": [], "operator_notes": [], "model_changes": [],
            "files": [], "pr": None}


def _load(conn, project, task):
    row = conn.execute("SELECT data FROM task_dossier WHERE project=? AND task=?",
                       (project, task)).fetchone()
    d = _empty()
    if row:
        try:
            d.update(json.loads(row[0]))
        except (ValueError, TypeError):
            pass
    return d


def _update(project, task, fn):
    """Apply fn(data) atomically and return the stored data."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        d = _load(conn, project, task)
        fn(d)
        conn.execute(
            "INSERT OR REPLACE INTO task_dossier(project, task, updated_at, data)"
            " VALUES (?,?,?,?)", (project, task, time.time(), json.dumps(d)))
        conn.execute("COMMIT")
        return d
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def get(project, task):
    conn = _connect()
    try:
        return _load(conn, project, task)
    finally:
        conn.close()


def find_project(task):
    """The project of the most recently updated dossier for `task`, or None."""
    conn = _connect()
    try:
        row = conn.execute("SELECT project FROM task_dossier WHERE task=? "
                           "ORDER BY updated_at DESC LIMIT 1", (task,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _norm(s):
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def _add_unique(lst, items, cap=MAX_LIST):
    seen = {_norm(x) for x in lst}
    for it in items:
        it = str(it).strip()
        if it and _norm(it) not in seen:
            lst.append(it)
            seen.add(_norm(it))
    del lst[:-cap]


def record_attempt(project, task, *, attempt, model, harness="", role="implementer",
                   outcome, summary="", failure_excerpt="", files_changed=(),
                   session_id=None):
    """Append one attempt; keep the last MAX_ATTEMPTS, count the rest."""
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown dossier outcome {outcome!r}")

    def fn(d):
        d["attempts"].append({
            "attempt": attempt, "model": model, "harness": harness or "",
            "role": role, "outcome": outcome, "summary": (summary or "")[:500],
            "failure_excerpt": (failure_excerpt or "")[-1200:],
            "files_changed": sorted(set(files_changed or ())),
            "session_id": session_id, "at": time.time()})
        extra = len(d["attempts"]) - MAX_ATTEMPTS
        if extra > 0:
            d["rolled_up"] = d.get("rolled_up", 0) + extra
            del d["attempts"][:extra]
        d["files"] = sorted(set(d.get("files") or ()) | set(files_changed or ()))
    return _update(project, task, fn)


def note_model_change(project, task, line):
    """Escalation / usage swap: say WHY the model changed."""
    return _update(project, task,
                   lambda d: d["model_changes"].append(str(line)[:300]))


def set_pr(project, task, number, url=""):
    return _update(project, task, lambda d: d.update(
        pr={"number": number, "url": url or ""} if number is not None else None))


def import_notes(project, task, text, author="operator"):
    """Operator/captain context IN; shown under 'Operator/captain notes'."""
    text = (text or "").strip()
    if not text:
        return get(project, task)
    return _update(project, task, lambda d: d["operator_notes"].append(
        {"author": author or "operator", "text": text[:2000], "at": time.time()}))


def _items(body):
    """Bullet items of a section; a bullet-less block is one item."""
    items, cur = [], None
    for line in body.splitlines():
        m = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$", line)
        if m:
            if cur is not None:
                items.append(cur)
            cur = m.group(1).strip()
        elif line.strip() and cur is not None:
            cur += " " + line.strip()
        elif line.strip():
            cur = line.strip()
    if cur is not None:
        items.append(cur)
    return [i for i in items if i]


def parse_handoff(text):
    """{key: str|list} from handoff.md; unknown headings and preamble -> notes."""
    out, notes = {}, []
    heading, buf = None, []

    def flush():
        body = "\n".join(buf).strip()
        if not body:
            return
        key = SECTIONS.get(_norm(heading).rstrip(":")) if heading else None
        if key in LIST_KEYS:
            out.setdefault(key, []).extend(_items(body))
        elif key:
            out[key] = body
        else:
            notes.append(f"{heading}: {body}" if heading else body)
    for line in (text or "").splitlines():
        m = re.match(r"^\s*#{1,6}\s+(.*?)\s*#*\s*$", line)
        if m:
            flush()
            heading, buf = m.group(1), []
        else:
            buf.append(line)
    flush()
    if notes:
        out["notes"] = notes
    return out


def harvest_handoff(project, task, worktree, *, attempt, model, role):
    """Read <worktree>/.arc/handoff.md into the dossier, then delete it.

    Returns the parsed sections ({} when there was no file). The delete
    happens before parsing: a malformed file must not survive into a commit.
    """
    path = Path(worktree) / HANDOFF_REL
    try:
        text = path.read_text(errors="replace")
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return {}
    try:
        path.unlink()
    except OSError:
        pass
    parsed = parse_handoff(text)
    if not parsed:
        return {}

    def fn(d):
        for key in ("done", "remaining", "next_step", "gotchas"):
            if parsed.get(key):
                d[key] = parsed[key][:3000]
        for key in LIST_KEYS:
            _add_unique(d[key], parsed.get(key, ()))
        _add_unique(d["notes"], [n[:1000] for n in parsed.get("notes", ())])
        d["handoff_by"] = {"attempt": attempt, "model": model, "role": role}
    _update(project, task, fn)
    return parsed


def has_history(d):
    return bool(d.get("attempts") or d.get("operator_notes") or d.get("done")
                or d.get("remaining") or d.get("decisions") or d.get("dead_ends")
                or d.get("notes") or d.get("model_changes")
                or d.get("next_step") or d.get("gotchas") or d.get("pr"))


def _tier(model):
    for tier, models in getattr(config, "IMPLEMENT_TIERS", {}).items():
        if model in (models if isinstance(models, (list, tuple)) else [models]):
            return tier
    return "?"


def _sections(d, role):
    """(title, body) in priority order — truncation drops from the end."""
    impl = [a for a in d["attempts"] if a.get("role") == "implementer"]
    n = max([a.get("attempt") or 0 for a in impl] or [0])
    models = []
    for a in impl:
        if not models or models[-1] != a["model"]:
            models.append(a["model"])
    state = [f"implementer attempts so far: {n} "
             f"({len(d['attempts']) + d.get('rolled_up', 0)} recorded runs)"]
    if models:
        state.append("model history: " + " -> ".join(
            f"{m} [{_tier(m)}]" for m in models))
    state += [f"model change: {c}" for c in d.get("model_changes", [])[-5:]]
    out = [("Current state", "\n".join(state))]
    if d.get("operator_notes"):
        out.append(("Operator/captain notes", "\n".join(
            f"- ({x.get('author')}) {x.get('text')}" for x in d["operator_notes"][-5:])))
    if role in ("reviewer", "pr-reviewer", "pr_reviewer") and d.get("done"):
        out.append(("What the implementer claims was done (verify it)", d["done"]))
    if d.get("remaining"):
        out.append(("Remaining", d["remaining"]))
    if d.get("next_step"):
        out.append(("Next step", d["next_step"]))
    if d.get("decisions"):
        out.append(("Decisions (do not relitigate)",
                    "\n".join(f"- {x}" for x in d["decisions"])))
    if d.get("dead_ends"):
        out.append(("Dead ends (do not retry)",
                    "\n".join(f"- {x}" for x in d["dead_ends"])))
    if d.get("gotchas"):
        out.append(("Gotchas", d["gotchas"]))
    last = d["attempts"][-3:]
    if last:
        lines = []
        for a in last:
            lines.append(f"- attempt {a.get('attempt')} {a.get('role')} "
                         f"{a.get('model')}: {a.get('outcome')}"
                         + (f" — {a['summary']}" if a.get("summary") else ""))
            if a.get("failure_excerpt"):
                lines.append("    " + a["failure_excerpt"][-400:].replace("\n", "\n    "))
        out.append(("Last attempts", "\n".join(lines)))
    if d.get("files"):
        out.append(("Files touched so far", ", ".join(d["files"][:60])))
    if d.get("pr"):
        out.append(("Open PR", f"#{d['pr'].get('number')} {d['pr'].get('url', '')}".strip()))
    if d.get("notes"):
        out.append(("Notes", "\n".join(f"- {x}" for x in d["notes"][-5:])))
    return out


def render(project, task, *, role="implementer", limit_chars=4000, data=None):
    """The prompt block; "" when the task has no history yet."""
    d = data if data is not None else get(project, task)
    if not has_history(d):
        return ""
    text = ("TASK DOSSIER — durable context from earlier runs of this task "
            "(possibly other models). Read it before acting.\n")
    for title, body in _sections(d, role):
        text += f"\n## {title}\n{body}\n"
    if limit_chars and len(text) > limit_chars:
        tail = "\n... [dossier truncated]\n"
        text = text[:max(0, limit_chars - len(tail))] + tail
    return text


def export(project, task, fmt="md"):
    if fmt == "json":
        return json.dumps(get(project, task), indent=2, default=str)
    return render(project, task, role="implementer", limit_chars=0) or \
        f"(no dossier for {project}/{task})\n"


HANDOFF_PROMPT = (
    "\nHANDOFF: before you finish, write .arc/handoff.md (create .arc/) with these sections:\n"
    "## Done / ## Remaining / ## Decisions (each with its reason) / "
    "## Dead ends (approaches that failed, and why) / ## Gotchas / ## Next step\n"
    "It is read by whoever continues this task — possibly a different model "
    "after a restart — and replaces re-reading the repo.\n"
    "Keep it short and concrete: file:line, commands, exact errors.\n"
    "The orchestrator reads and deletes it; it is never committed.\n"
    "Write it even if you finish the task: the reviewer sees your Done list.\n"
)
