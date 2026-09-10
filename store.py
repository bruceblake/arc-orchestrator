import os
import sqlite3
import threading
import time
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS rounds(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  topic TEXT,
  status TEXT NOT NULL,
  questions INTEGER,
  passed INTEGER,
  verify_rounds INTEGER,
  error TEXT
);
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  round_id INTEGER NOT NULL,
  question TEXT NOT NULL,
  synthesis TEXT,
  score REAL,
  passed INTEGER,
  verify_rounds INTEGER,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS answers(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL,
  family TEXT NOT NULL,
  text TEXT NOT NULL,
  critic_family TEXT,
  score REAL,
  verdict TEXT,
  issues TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seeds(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  round_id INTEGER,
  question TEXT NOT NULL,
  used INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS builds(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  iteration INTEGER,
  mode TEXT,
  status TEXT NOT NULL,
  passed INTEGER,
  integration_rounds INTEGER,
  error TEXT
);
CREATE TABLE IF NOT EXISTS build_modules(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  build_id INTEGER NOT NULL,
  module TEXT NOT NULL,
  filepath TEXT NOT NULL,
  producer TEXT,
  reviewer TEXT,
  attempt INTEGER,
  gate_attempts INTEGER,
  review_score REAL,
  passed INTEGER,
  tokens INTEGER,
  latency_ms INTEGER,
  code TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_round ON items(round_id);
CREATE INDEX IF NOT EXISTS idx_answers_item ON answers(item_id);
CREATE INDEX IF NOT EXISTS idx_seeds_used ON seeds(used);
CREATE INDEX IF NOT EXISTS idx_build_modules_build ON build_modules(build_id);
CREATE TABLE IF NOT EXISTS code_tasks(
  id TEXT NOT NULL,
  taskfile TEXT NOT NULL,
  title TEXT,
  model TEXT NOT NULL,
  reviewer TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  branch TEXT,
  worktree TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  finished_at TEXT,
  PRIMARY KEY (taskfile, id)
);
CREATE TABLE IF NOT EXISTS harness_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  harness TEXT NOT NULL,
  model TEXT NOT NULL,
  role TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  exit_code INTEGER,
  transcript TEXT,
  seconds REAL,
  verdict TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_harness_runs_task ON harness_runs(task_id);
CREATE TABLE IF NOT EXISTS bench_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  suites TEXT NOT NULL,
  label TEXT,
  config TEXT,
  status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bench_results(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  task_id TEXT NOT NULL,
  suite TEXT NOT NULL,
  tier TEXT,
  model TEXT NOT NULL,
  effort TEXT,
  harness TEXT NOT NULL,
  temperature REAL,
  n INTEGER NOT NULL DEFAULT 1,
  sample INTEGER NOT NULL DEFAULT 0,
  passed INTEGER,
  rounds INTEGER,
  seconds REAL,
  tokens INTEGER,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  error TEXT,
  output_path TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bench_results_run ON bench_results(run_id);
CREATE TABLE IF NOT EXISTS driver_leases(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  model TEXT NOT NULL,
  pid INTEGER NOT NULL,
  task TEXT,
  acquired_at REAL NOT NULL
);
"""


class Store:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def start_round(self, topic):
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO rounds(started_at, topic, status) VALUES (?, ?, 'running')",
                (_now(), topic),
            )
            self.conn.commit()
            return cur.lastrowid

    def finish_round(self, rid, status, *, error=None, questions=None, passed=None, verify_rounds=None):
        with self.lock:
            self.conn.execute(
                "UPDATE rounds SET finished_at=?, status=?, error=?, questions=?, passed=?, verify_rounds=? WHERE id=?",
                (_now(), status, error, questions, passed, verify_rounds, rid),
            )
            self.conn.commit()

    def fail_stale_rounds(self):
        with self.lock:
            cur = self.conn.execute(
                "UPDATE rounds SET status='failed', error='orphaned by restart' WHERE status='running'"
            )
            self.conn.commit()
            return cur.rowcount

    def fail_stale_builds(self):
        with self.lock:
            cur = self.conn.execute(
                "UPDATE builds SET status='failed', error='orphaned by restart' WHERE status='running'"
            )
            self.conn.commit()
            return cur.rowcount

    def add_items(self, rid, questions):
        ids = []
        with self.lock:
            for q in questions:
                cur = self.conn.execute(
                    "INSERT INTO items(round_id, question, created_at) VALUES (?, ?, ?)",
                    (rid, q, _now()),
                )
                ids.append(cur.lastrowid)
            self.conn.commit()
        return ids

    def save_answer(self, item_id, family, text, critic_family, score, verdict, issues):
        with self.lock:
            self.conn.execute(
                "INSERT INTO answers(item_id, family, text, critic_family, score, verdict, issues, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (item_id, family, text, critic_family, score, verdict, issues, _now()),
            )
            self.conn.commit()

    def save_final(self, item_id, synthesis, score, passed, verify_rounds):
        with self.lock:
            self.conn.execute(
                "UPDATE items SET synthesis=?, score=?, passed=?, verify_rounds=?, status='done' WHERE id=?",
                (synthesis, score, 1 if passed else 0, verify_rounds, item_id),
            )
            self.conn.commit()

    def add_seeds(self, rid, topics):
        with self.lock:
            self.conn.executemany(
                "INSERT INTO seeds(round_id, question, created_at) VALUES (?, ?, ?)",
                [(rid, t, _now()) for t in topics],
            )
            self.conn.commit()

    def take_seed(self):
        with self.lock:
            row = self.conn.execute(
                "SELECT id, question FROM seeds WHERE used=0 ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            self.conn.execute("UPDATE seeds SET used=1 WHERE id=?", (row["id"],))
            self.conn.commit()
            return row["question"]

    def start_build(self, iteration, mode):
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO builds(started_at, iteration, mode, status) VALUES (?, ?, ?, 'running')",
                (_now(), iteration, mode),
            )
            self.conn.commit()
            return cur.lastrowid

    def finish_build(self, bid, status, *, passed=None, integration_rounds=None, error=None):
        with self.lock:
            self.conn.execute(
                "UPDATE builds SET finished_at=?, status=?, passed=?, integration_rounds=?, error=? WHERE id=?",
                (_now(), status, passed, integration_rounds, error, bid),
            )
            self.conn.commit()

    def save_build_module(self, build_id, module, filepath, producer, reviewer, attempt,
                          gate_attempts, review_score, passed, tokens, latency_ms, code):
        with self.lock:
            self.conn.execute(
                "INSERT INTO build_modules(build_id, module, filepath, producer, reviewer, attempt, "
                "gate_attempts, review_score, passed, tokens, latency_ms, code, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (build_id, module, filepath, producer, reviewer, attempt, gate_attempts,
                 review_score, 1 if passed else 0, tokens, latency_ms, code, _now()),
            )
            self.conn.commit()

    def critique_matrix(self):
        with self.lock:
            rows = self.conn.execute(
                "SELECT family, critic_family, COUNT(*) AS n, AVG(score) AS s "
                "FROM answers GROUP BY family, critic_family"
            ).fetchall()
        return {
            "authors": sorted({r["family"] for r in rows}),
            "critics": sorted({r["critic_family"] for r in rows if r["critic_family"]}),
            "cells": [
                {"author": r["family"], "critic": r["critic_family"], "n": r["n"], "score": r["s"]}
                for r in rows
            ],
        }

    def build_stats(self):
        with self.lock:
            builds = {
                r["status"]: r["n"]
                for r in self.conn.execute("SELECT status, COUNT(*) AS n FROM builds GROUP BY status").fetchall()
            }
            by_family = [
                {
                    "family": r["producer"],
                    "modules": r["n"],
                    "passed": r["p"],
                    "avg_review": r["s"],
                    "tokens": r["t"],
                    "avg_latency_ms": r["l"],
                    "avg_attempts": r["a"],
                }
                for r in self.conn.execute(
                    "SELECT producer, COUNT(*) AS n, SUM(passed) AS p, AVG(review_score) AS s, "
                    "SUM(tokens) AS t, AVG(latency_ms) AS l, AVG(attempt) AS a "
                    "FROM build_modules GROUP BY producer"
                ).fetchall()
            ]
            recent = [
                dict(r)
                for r in self.conn.execute(
                    "SELECT id, iteration, mode, status, passed, integration_rounds, started_at, finished_at "
                    "FROM builds ORDER BY id DESC LIMIT 10"
                ).fetchall()
            ]
        return {"builds": builds, "by_family": by_family, "recent": recent}

    def save_harness_run(self, task_id, harness, model, role, attempt,
                         exit_code, transcript, seconds, verdict=None):
        with self.lock:
            self.conn.execute(
                "INSERT INTO harness_runs(task_id, harness, model, role, attempt, "
                "exit_code, transcript, seconds, verdict, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, harness, model, role, attempt, exit_code,
                 transcript, seconds, verdict, _now()),
            )
            self.conn.commit()

    def upsert_code_task(self, taskfile, tid, title, model, reviewer, status,
                         branch=None, worktree=None, error=None, finished=False):
        with self.lock:
            # model/reviewer MUST be updated on conflict: the escalate node
            # re-upserts a task at a stronger tier, and dropping that here left
            # the row (and therefore the next resume's starting tier, and the
            # dashboard) showing the model the task no longer runs at. branch
            # and worktree are COALESCEd because escalate re-upserts without
            # them and must not erase the allocation it is still using.
            self.conn.execute(
                "INSERT INTO code_tasks(id, taskfile, title, model, reviewer, status, "
                "branch, worktree, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(taskfile, id) DO UPDATE SET "
                "title=excluded.title, model=excluded.model, "
                "reviewer=excluded.reviewer, status=excluded.status, "
                "branch=COALESCE(excluded.branch, code_tasks.branch), "
                "worktree=COALESCE(excluded.worktree, code_tasks.worktree), "
                "error=NULL, finished_at=NULL",
                (tid, taskfile, title, model, reviewer, status, branch, worktree, _now()),
            )
            if finished:
                self.conn.execute(
                    "UPDATE code_tasks SET status=?, error=?, finished_at=? "
                    "WHERE taskfile=? AND id=?",
                    (status, error, _now(), taskfile, tid),
                )
            self.conn.commit()

    def code_status(self):
        with self.lock:
            tasks = [
                dict(r)
                for r in self.conn.execute(
                    "SELECT id, taskfile, title, model, reviewer, status, branch, "
                    "error, created_at, finished_at FROM code_tasks ORDER BY created_at DESC LIMIT 50"
                ).fetchall()
            ]
            runs = [
                dict(r)
                for r in self.conn.execute(
                    "SELECT task_id, harness, model, role, attempt, exit_code, "
                    "seconds, verdict FROM harness_runs ORDER BY id DESC LIMIT 50"
                ).fetchall()
            ]
        return {"tasks": tasks, "runs": runs}

    STALE_REASON = "reset-stale: owning run process died"

    def reset_stale_code_tasks(self, taskfile=None, reason=None):
        """Mark 'running' code tasks 'failed' — for when the run process that
        owned them is known dead. With `taskfile`, only that file's rows are
        touched (safe at the start of a new run of this taskfile; concurrent
        runs of OTHER taskfiles keep their rows). Without it, every running
        row is reset — never call that blanket form while a run is alive:
        concurrent code-run processes share this table.

        `reason` is written to the error column. It matters: code_tasks only
        escalates a resumed task to a stronger model when the recorded reason
        is a capability failure, so an infrastructure reason keeps the task on
        its own tier instead of piling it onto the scarcest one.
        """
        reason = reason or self.STALE_REASON
        with self.lock:
            if taskfile is None:
                cur = self.conn.execute(
                    "UPDATE code_tasks SET status='failed', error=?, finished_at=? "
                    "WHERE status='running'", (reason, _now()))
            else:
                cur = self.conn.execute(
                    "UPDATE code_tasks SET status='failed', error=?, finished_at=? "
                    "WHERE status='running' AND taskfile=?",
                    (reason, _now(), taskfile))
            self.conn.commit()
            return cur.rowcount

    def running_code_tasks(self, taskfile=None):
        """Rows still marked 'running', optionally scoped to one taskfile."""
        sql = ("SELECT id, taskfile, title, model, reviewer, branch, worktree "
               "FROM code_tasks WHERE status='running'")
        args = []
        if taskfile is not None:
            sql += " AND taskfile=?"
            args.append(taskfile)
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def driver_lease_rows(self):
        with self.lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT id, model, pid, task, acquired_at FROM driver_leases "
                "ORDER BY id").fetchall()]

    def release_leases_for_pid(self, pid):
        """Drop every lease this process owns — the shutdown counterpart to
        acquire_driver_lease, so a killed run does not pin a model at cap for
        the full DRIVER_LEASE_TTL."""
        with self.lock:
            cur = self.conn.execute("DELETE FROM driver_leases WHERE pid=?", (pid,))
            self.conn.commit()
            return cur.rowcount

    def reap_driver_leases(self, ttl):
        """Delete lease rows whose owner is dead or whose TTL has expired.
        Returns the number removed."""
        now = time.time()
        removed = 0
        with self.lock:
            for r in self.conn.execute(
                    "SELECT id, pid, acquired_at FROM driver_leases").fetchall():
                alive = True
                try:
                    os.kill(r["pid"], 0)
                except OSError:
                    alive = False
                if alive and r["acquired_at"] > now - ttl:
                    continue
                self.conn.execute("DELETE FROM driver_leases WHERE id=?", (r["id"],))
                removed += 1
            self.conn.commit()
        return removed

    def acquire_driver_lease(self, model, pid, task, cap, ttl):
        """Cross-process driver concurrency guard. Returns None and holds a
        lease row on success; returns the live in-use count when the model is
        at `cap`. SQLite WAL + this lock serialize acquisitions across all
        orchestrator processes sharing the DB, so a terminal queue and a
        dashboard-launched run cannot stack caps. Stale rows (older than ttl
        or owned by a dead pid) are reaped on every call."""
        with self.lock:
            now = time.time()
            rows = list(self.conn.execute(
                "SELECT id, pid, acquired_at FROM driver_leases WHERE model=?",
                (model,)))
            live = 0
            for r in rows:
                fresh = r["acquired_at"] > now - ttl
                alive = True
                try:
                    os.kill(r["pid"], 0)
                except OSError:
                    alive = False
                if fresh and alive:
                    live += 1
                else:
                    self.conn.execute("DELETE FROM driver_leases WHERE id=?",
                                      (r["id"],))
            if live >= cap:
                self.conn.commit()
                return live
            self.conn.execute(
                "INSERT INTO driver_leases(model, pid, task, acquired_at) "
                "VALUES (?,?,?,?)", (model, pid, task, now))
            self.conn.commit()
            return None

    def release_driver_lease(self, model, pid, task):
        with self.lock:
            self.conn.execute(
                "DELETE FROM driver_leases WHERE model=? AND pid=? AND task IS ?",
                (model, pid, task))
            self.conn.commit()

    def code_tasks_all(self, limit=500):
        with self.lock:
            return [
                dict(r)
                for r in self.conn.execute(
                    "SELECT id, taskfile, title, model, reviewer, status, branch, "
                    "worktree, error, created_at, finished_at FROM code_tasks "
                    "ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            ]

    def code_tasks_for(self, taskfile):
        with self.lock:
            return [
                dict(r)
                for r in self.conn.execute(
                    "SELECT id, title, model, reviewer, status, error FROM code_tasks "
                    "WHERE taskfile=? ORDER BY id", (taskfile,),
                ).fetchall()
            ]

    def harness_runs_prefix(self, task_id_prefix):
        with self.lock:
            return [
                dict(r)
                for r in self.conn.execute(
                    "SELECT task_id, harness, model, role, attempt, exit_code, "
                    "transcript, seconds, verdict FROM harness_runs WHERE task_id LIKE ? "
                    "ORDER BY id", (task_id_prefix + "%",),
                ).fetchall()
            ]

    def harness_runs_for(self, task_ids, limit=400):
        ids = list(task_ids)
        if not ids:
            return []
        q = ",".join("?" * len(ids))
        with self.lock:
            return [
                dict(r)
                for r in self.conn.execute(
                    f"SELECT task_id, harness, model, role, attempt, exit_code, "
                    f"transcript, seconds, verdict, created_at FROM harness_runs "
                    f"WHERE task_id IN ({q}) ORDER BY id DESC LIMIT ?",
                    (*ids, limit),
                ).fetchall()
            ]

    def harness_runs_all(self, limit=1500):
        with self.lock:
            return [
                dict(r)
                for r in self.conn.execute(
                    "SELECT task_id, harness, model, role, attempt, exit_code, "
                    "transcript, seconds, verdict, created_at FROM harness_runs "
                    "ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            ]

    def start_bench_run(self, suites, label, config):
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO bench_runs(started_at, suites, label, config, status) "
                "VALUES (?, ?, ?, ?, 'running')",
                (_now(), suites, label, config),
            )
            self.conn.commit()
            return cur.lastrowid

    def finish_bench_run(self, rid, status):
        with self.lock:
            self.conn.execute(
                "UPDATE bench_runs SET finished_at=?, status=? WHERE id=?",
                (_now(), status, rid),
            )
            self.conn.commit()

    def fail_stale_bench_runs(self):
        with self.lock:
            cur = self.conn.execute(
                "UPDATE bench_runs SET status='failed', finished_at=? WHERE status='running'",
                (_now(),),
            )
            self.conn.commit()
            return cur.rowcount

    def save_bench_result(self, run_id, task_id, suite, tier, model, effort, harness,
                          temperature, n, sample, passed, rounds, seconds, tokens,
                          prompt_tokens, completion_tokens, error, output_path):
        with self.lock:
            self.conn.execute(
                "INSERT INTO bench_results(run_id, task_id, suite, tier, model, effort,"
                " harness, temperature, n, sample, passed, rounds, seconds, tokens,"
                " prompt_tokens, completion_tokens, error, output_path, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, task_id, suite, tier, model, effort, harness, temperature,
                 n, sample, passed, rounds, seconds, tokens, prompt_tokens,
                 completion_tokens, error, output_path, _now()),
            )
            self.conn.commit()

    def bench_runs_list(self, limit=50):
        with self.lock:
            return [
                dict(r)
                for r in self.conn.execute(
                    "SELECT id, started_at, finished_at, suites, label, status FROM"
                    " bench_runs ORDER BY id DESC LIMIT ?", (limit,),
                ).fetchall()
            ]

    def bench_results(self, run_ids=None, limit=100000):
        sql = ("SELECT run_id, task_id, suite, tier, model, effort, harness,"
               " temperature, n, sample, passed, rounds, seconds, tokens,"
               " prompt_tokens, completion_tokens, error FROM bench_results")
        args = []
        if run_ids:
            sql += " WHERE run_id IN (" + ",".join("?" * len(run_ids)) + ")"
            args = list(run_ids)
        sql += " ORDER BY id LIMIT ?"
        args.append(limit)
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def stats(self):
        with self.lock:
            rounds = {
                r["status"]: r["n"]
                for r in self.conn.execute("SELECT status, COUNT(*) AS n FROM rounds GROUP BY status").fetchall()
            }
            item_row = self.conn.execute(
                "SELECT COUNT(*) AS n, AVG(score) AS s, AVG(passed) AS p FROM items WHERE status='done'"
            ).fetchone()
            answers = {
                r["family"]: r["n"]
                for r in self.conn.execute("SELECT family, COUNT(*) AS n FROM answers GROUP BY family").fetchall()
            }
            answers_score = {
                r["family"]: r["s"]
                for r in self.conn.execute(
                    "SELECT family, AVG(score) AS s FROM answers WHERE score IS NOT NULL GROUP BY family"
                ).fetchall()
            }
            seeds_unused = self.conn.execute(
                "SELECT COUNT(*) AS n FROM seeds WHERE used=0"
            ).fetchone()["n"]
            recent = [
                r["topic"]
                for r in self.conn.execute(
                    "SELECT topic FROM rounds WHERE status='ok' ORDER BY id DESC LIMIT 5"
                ).fetchall()
            ]
        return {
            "rounds": rounds,
            "items_done": item_row["n"],
            "avg_score": item_row["s"],
            "pass_rate": item_row["p"],
            "answers": answers,
            "answers_score": answers_score,
            "seeds_unused": seeds_unused,
            "recent_topics": recent,
        }