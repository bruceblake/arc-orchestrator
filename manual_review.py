"""Human checkpoints: the queue of pull requests waiting for a person's word.

AGENTS.md Rule 5's manual gate. When a task wants a human checkpoint
(`human_review` in its taskfile, else the project's, else
config.PR_MANUAL_REVIEW), a PR the fleet's reviewers approved is held in
code_tasks._await_manual_review until a human decides. This module is the
durable record of those holds, in the shared `manual_reviews` table of
config.DB_PATH, so the run process that waits and the dashboard that shows
the "Needs you" queue agree without sharing memory:

  waiting    the fleet approved; nobody has decided yet
  approved   a human approved (dashboard, phone, or the GitHub label)
  rejected   a human asked for changes; `comment` becomes the implementer's
             feedback on the same path as a reviewer's issues
  timeout    ARC_PR_MANUAL_TIMEOUT ran out (treated as a rejection)

A decision made while the run is down is kept (`consumed` = 0) and honoured
when the run resumes and reaches the same PR round, instead of asking again.

Rule 6b: the dashboard's decide route acts only on a row this table already
holds in `waiting`, named by (task, pr, round). Nothing from a request becomes
a path, a command or a git ref.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time

import config

DECISIONS = {"approve": "approved", "reject": "rejected"}
STATES = ("waiting", "approved", "rejected", "timeout")
MAX_COMMENT = 4000
RECENT = 20

SCHEMA = """CREATE TABLE IF NOT EXISTS manual_reviews(
  task TEXT NOT NULL,
  pr INTEGER NOT NULL,
  round INTEGER NOT NULL,
  project TEXT NOT NULL DEFAULT '',
  repo TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  requested_at REAL NOT NULL,
  decided_at REAL,
  decided_by TEXT NOT NULL DEFAULT '',
  comment TEXT NOT NULL DEFAULT '',
  reviewers TEXT NOT NULL DEFAULT '[]',
  pid INTEGER,
  consumed INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (task, pr, round)
)"""

_COLS = ("task", "pr", "round", "project", "repo", "url", "title", "status",
         "requested_at", "decided_at", "decided_by", "comment", "reviewers",
         "pid", "consumed")


@contextlib.contextmanager
def _connect():
    """Autocommit connection, always closed (sqlite3's own `with` does not)."""
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    try:
        conn.execute(SCHEMA)
        yield conn
    finally:
        conn.close()


def _row(r):
    if r is None:
        return None
    d = dict(zip(_COLS, r))
    try:
        d["reviewers"] = json.loads(d["reviewers"] or "[]")
    except ValueError:
        d["reviewers"] = []
    d["consumed"] = bool(d["consumed"])
    return d


def wanted(task):
    """Does this task want a human checkpoint before its PR merges?

    The taskfile decides (task `human_review`, else the project's, both
    resolved by code_tasks.load_taskfile into task["human_review"]); None
    there means the taskfile did not say, and the fleet-wide
    ARC_PR_MANUAL_REVIEW applies.
    """
    v = (task or {}).get("human_review")
    return config.PR_MANUAL_REVIEW if v is None else bool(v)


def summarize_reviewers(outcomes):
    """The fleet's verdicts on this round, small enough to store and show."""
    out = []
    for model, v in outcomes or []:
        v = v or {}
        out.append({"model": str(model),
                    "approve": bool(v.get("approve")),
                    "crashed": bool(v.get("crashed")),
                    "issues": [str(i)[:400] for i in (v.get("issues") or [])][:10],
                    "follow_ups": [str(i)[:400] for i in (v.get("follow_ups") or [])][:10]})
    return out


def request(task, pr, round_n, *, project="", repo="", url="", title="",
            reviewers=None, pid=None):
    """Open (or re-open) a hold. Returns the row.

    A row already decided but not yet consumed is returned unchanged: the
    human answered while no run was waiting, and that answer stands.
    """
    now = time.time()
    with _connect() as conn:
        cur = _row(conn.execute(
            f"SELECT {','.join(_COLS)} FROM manual_reviews WHERE task=? AND pr=? AND round=?",
            (task, int(pr), int(round_n))).fetchone())
        if cur and cur["status"] != "waiting" and not cur["consumed"]:
            return cur
        conn.execute(
            "INSERT INTO manual_reviews(task, pr, round, project, repo, url, title, "
            "status, requested_at, reviewers, pid, consumed, decided_at, decided_by, comment) "
            "VALUES(?,?,?,?,?,?,?,'waiting',?,?,?,0,NULL,'','') "
            "ON CONFLICT(task, pr, round) DO UPDATE SET project=excluded.project, "
            "repo=excluded.repo, url=excluded.url, title=excluded.title, "
            "status='waiting', requested_at=excluded.requested_at, "
            "reviewers=excluded.reviewers, pid=excluded.pid, consumed=0, "
            "decided_at=NULL, decided_by='', comment=''",
            (task, int(pr), int(round_n), str(project), str(repo), str(url),
             str(title)[:300], now, json.dumps(reviewers or []),
             pid if pid is not None else os.getpid()))
    return get(task, pr, round_n)


def get(task, pr, round_n):
    with _connect() as conn:
        return _row(conn.execute(
            f"SELECT {','.join(_COLS)} FROM manual_reviews WHERE task=? AND pr=? AND round=?",
            (task, int(pr), int(round_n))).fetchone())


def decide(task, pr, round_n, decision, *, comment="", by="dashboard"):
    """A human's decision on a waiting hold. Raises KeyError when no such hold
    is waiting, ValueError for a bad decision. Returns the updated row."""
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {sorted(DECISIONS)}")
    comment = str(comment or "").strip()[:MAX_COMMENT]
    status = DECISIONS[decision]
    with _connect() as conn:
        n = conn.execute(
            "UPDATE manual_reviews SET status=?, decided_at=?, decided_by=?, comment=? "
            "WHERE task=? AND pr=? AND round=? AND status='waiting'",
            (status, time.time(), str(by)[:80], comment, task, int(pr),
             int(round_n))).rowcount
    if not n:
        raise KeyError(f"no pull request waiting for a human: {task} #{pr} round {round_n}")
    return get(task, pr, round_n)


def settle(task, pr, round_n, status, *, by="", comment=""):
    """Record an outcome the fleet saw itself (a GitHub label, a timeout) and
    mark it consumed. Never raises: the run's own decision is already made."""
    try:
        with _connect() as conn:
            conn.execute(
                "UPDATE manual_reviews SET status=?, decided_at=COALESCE(decided_at, ?), "
                "decided_by=CASE WHEN decided_by='' THEN ? ELSE decided_by END, "
                "comment=CASE WHEN comment='' THEN ? ELSE comment END, consumed=1 "
                "WHERE task=? AND pr=? AND round=?",
                (status, time.time(), str(by)[:80], str(comment)[:MAX_COMMENT],
                 task, int(pr), int(round_n)))
    except sqlite3.Error:
        pass


def alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError, ValueError):
        return True
    return True


def waiting():
    """Every hold nobody has decided, newest first, each with `live`: whether
    the run that is waiting on it is still alive."""
    try:
        with _connect() as conn:
            rows = [_row(r) for r in conn.execute(
                f"SELECT {','.join(_COLS)} FROM manual_reviews WHERE status='waiting' "
                "ORDER BY requested_at DESC").fetchall()]
    except sqlite3.Error:
        return []
    for r in rows:
        r["live"] = alive(r["pid"])
    return rows


def recent(limit=RECENT):
    """Decided holds, newest decision first."""
    try:
        with _connect() as conn:
            rows = [_row(r) for r in conn.execute(
                f"SELECT {','.join(_COLS)} FROM manual_reviews WHERE status!='waiting' "
                "ORDER BY COALESCE(decided_at, requested_at) DESC LIMIT ?",
                (int(limit),)).fetchall()]
    except sqlite3.Error:
        return []
    return rows
