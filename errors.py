"""Structured exception capture: keep the evidence, and group it.

Every catch site in this repo reduced its exception to ``str(exc)[:300]``. That
string says an error happened and nothing about WHERE — no file, no line, no
frame. Debugging a fleet failure meant guessing which of several plausible call
paths produced a message like "opencode exited 1:", and the honest answer was
usually that nobody could tell.

Two things are needed to fix that, and only one of them is "log more":

**Keep the traceback.** ``capture()`` stores the full formatted traceback, the
exception chain, and whatever context the caller knows (task, model, node),
keyed by an id the short message can carry. The message stays short; the
evidence stops being thrown away.

**Group by cause, not by text.** A hundred identical bugs are one bug. Every
capture gets a FINGERPRINT derived from the exception type plus the frames
inside this repo — deliberately ignoring line numbers, which shift on every
edit, and ignoring the message, which usually embeds a path or an id that makes
every occurrence look unique. Two failures with the same fingerprint are the
same defect and are counted as one, with a count and a first/last seen.

Capture NEVER raises. It runs on error paths, frequently inside ``except``
blocks and ``finally`` clauses, and an instrumentation layer that can turn a
handled error into an unhandled one is worse than no instrumentation at all.
"""
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import traceback
from pathlib import Path

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS error_events(
    id           INTEGER PRIMARY KEY,
    ts           REAL NOT NULL,
    fingerprint  TEXT NOT NULL,
    kind         TEXT NOT NULL,
    message      TEXT NOT NULL,
    traceback    TEXT,
    where_        TEXT,
    task         TEXT,
    model        TEXT,
    node         TEXT,
    run_id       TEXT,
    pid          INTEGER,
    context      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS error_by_fp ON error_events(fingerprint, ts DESC);
CREATE INDEX IF NOT EXISTS error_by_ts ON error_events(ts DESC);
CREATE INDEX IF NOT EXISTS error_by_task ON error_events(task, ts DESC);
"""

_conn = None
_lock = threading.RLock()
# Paths that are OURS. A traceback's useful frames are the ones in this repo;
# stdlib and site-packages frames are the same for every bug and would make two
# different defects fingerprint identically whenever they failed in, say,
# asyncio.
_ROOT = str(Path(__file__).resolve().parent)
# A defect last seen inside this window is still firing.
ACTIVE_WINDOW_S = 900


def _db():
    global _conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False,
                                    timeout=30, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.executescript(SCHEMA)
        return _conn


def reset_for_tests():
    """Drop the cached connection so a test can repoint config.DB_PATH."""
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        _conn = None


def _our_frames(exc):
    """(file, function) for each frame inside this repo, innermost last."""
    out = []
    for fr in traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []:
        if str(Path(fr.filename).resolve()).startswith(_ROOT):
            out.append((Path(fr.filename).name, fr.name))
    return out


# Substrings that make two occurrences of ONE bug look like two: absolute
# paths, pids, hex ids, timings, line/byte counts.
_NOISE = [
    (re.compile(r"/[\w./~-]+"), "<path>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<addr>"),
    (re.compile(r"\b[0-9a-f]{8,}\b", re.I), "<hex>"),
    (re.compile(r"\b\d+(\.\d+)?s\b"), "<dur>"),
    (re.compile(r"\b\d{3,}\b"), "<n>"),
]


def normalise(message):
    """A message with its per-occurrence noise removed, for fingerprinting."""
    text = str(message or "")
    for pat, repl in _NOISE:
        text = pat.sub(repl, text)
    return " ".join(text.split())[:300]


def fingerprint(exc, extra=""):
    """A stable id for THIS DEFECT.

    Built from the exception type and the names of our own frames — not line
    numbers (they move on every edit) and not the raw message (it usually
    carries a path or an id that makes each occurrence unique). The point is
    that a hundred occurrences of one bug count as one bug.
    """
    frames = _our_frames(exc)
    parts = [type(exc).__name__] + [f"{f}:{fn}" for f, fn in frames[-6:]]
    if extra:
        parts.append(str(extra))
    if not frames:
        # No frames of ours at all (raised before entry, or re-created from a
        # string): fall back to the normalised message so it still groups.
        parts.append(normalise(exc))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def capture(exc, *, task=None, model=None, node=None, where=None, **context):
    """Record an exception with its traceback. Returns a short error id.

    Never raises. This runs inside `except` and `finally` blocks, where an
    instrumentation failure would turn a handled error into an unhandled one.
    """
    try:
        fp = fingerprint(exc)
        frames = _our_frames(exc)
        tb = "".join(traceback.format_exception(type(exc), exc,
                                                exc.__traceback__))[-8000:]
        row = (
            time.time(), fp, type(exc).__name__, str(exc)[:1000], tb,
            where or (f"{frames[-1][0]}:{frames[-1][1]}" if frames else None),
            task, model, node, _run_id(), os.getpid(),
            json.dumps(context, default=str)[:4000],
        )
        with _lock:
            _db().execute(
                "INSERT INTO error_events(ts, fingerprint, kind, message,"
                " traceback, where_, task, model, node, run_id, pid, context)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", row)
        return fp
    except Exception:
        return "uncaptured"


_RUN_ID = None


def _run_id():
    """A per-PROCESS id, so one run's events can be stitched back together.

    Events from one task are spread across the run process, its drivers and the
    dashboard reading them back; without a shared id there is no way to ask
    "show me everything that happened in that run" after the fact.
    """
    global _RUN_ID
    if _RUN_ID is None:
        _RUN_ID = f"{int(time.time())}-{os.getpid()}"
    return _RUN_ID


def run_id():
    return _run_id()


# ---- reading: triage ----------------------------------------------------

def groups(since=None, limit=50):
    """Distinct DEFECTS, worst first — the triage list.

    One row per fingerprint with its count, when it was first and last seen,
    and which tasks it hit. A flat error log answers "what happened"; this
    answers "what should I fix", which is a different and more useful question.
    """
    since = since if since is not None else 0
    with _lock:
        rows = _db().execute(
            "SELECT fingerprint, COUNT(*) n, MIN(ts) first_ts, MAX(ts) last_ts,"
            " kind, where_, COUNT(DISTINCT task) n_tasks"
            " FROM error_events WHERE ts >= ?"
            " GROUP BY fingerprint ORDER BY n DESC, last_ts DESC LIMIT ?",
            (since, limit)).fetchall()
        out = []
        for r in rows:
            sample = _db().execute(
                "SELECT message, task, model, node, traceback FROM error_events"
                " WHERE fingerprint=? ORDER BY ts DESC LIMIT 1",
                (r["fingerprint"],)).fetchone()
            tasks = [x["task"] for x in _db().execute(
                "SELECT DISTINCT task FROM error_events WHERE fingerprint=?"
                " AND task IS NOT NULL LIMIT 8", (r["fingerprint"],))]
            now = time.time()
            age = now - (r["last_ts"] or now)
            out.append({
                "fingerprint": r["fingerprint"], "count": r["n"],
                "kind": r["kind"], "where": r["where_"],
                "first_ts": r["first_ts"], "last_ts": r["last_ts"],
                "n_tasks": r["n_tasks"], "tasks": tasks,
                "age_s": round(age),
                "span_s": round((r["last_ts"] or 0) - (r["first_ts"] or 0)),
                # Whether it is still happening, which is the difference
                # between a defect to fix and one already fixed. Derived here
                # rather than in each consumer — the audit and the dashboard
                # both need it, and it is a property of the group, not of how
                # it is displayed.
                "active": age < ACTIVE_WINDOW_S,
                "message": (sample["message"] if sample else ""),
                "traceback": (sample["traceback"] if sample else ""),
            })
    return out


def recent(limit=100, since=None, task=None):
    sql = "SELECT * FROM error_events WHERE ts >= ?"
    args = [since if since is not None else 0]
    if task:
        sql += " AND task=?"
        args.append(task)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    with _lock:
        return [dict(r) for r in _db().execute(sql, args)]


def prune(older_than_s):
    with _lock:
        cur = _db().execute("DELETE FROM error_events WHERE ts < ?",
                            (time.time() - older_than_s,))
    return cur.rowcount
