"""Fixed, deterministic fixture state for rendering the dashboard headlessly.

Visual regression only means something if the same code renders the same
pixels every time. The live dashboard reads the operator's orchestrator.db,
logs/events.jsonl, ~/tasks and /proc — all of which change by the second — so
a screenshot of it is a screenshot of the weather. This module writes a small,
fixed world instead: two taskfiles, a handful of task rows in every status the
UI colours differently, harness runs, and an event log — every timestamp
pinned relative to FROZEN_NOW, which the fixture server and the browser also
use as "now".

Everything lands under one directory the caller owns (normally a temp dir),
and NOTHING here ever opens the operator's real state: the store is created at
the path given, never at config.DB_PATH.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# 2026-09-20 12:00:00 UTC. The server's time.time() and the page's Date are
# both pinned here, so "3m ago" renders the same on every capture.
FROZEN_NOW = 1789905600.0

REPO = "/home/operator/repos/demo-app"      # displayed, never opened

TASKFILES = {
    "demo-checkout.json": {
        "project": {
            "repo": REPO,
            "title": "Checkout flow: cart, payment form and receipt page",
            "pattern": "diamond",
            "tasks": [
                {"id": "cart-model", "title": "Cart model with line items and totals",
                 "model": "DeepSeek-V4.1-Flash-thinking-max", "reviewer": "glm",
                 "deps": [], "prompt": "Add a cart model.", "verify_cmd": "make test"},
                {"id": "payment-form", "title": "Payment form with validation",
                 "model": "GLM-5.3", "reviewer": "deepseek",
                 "deps": ["cart-model"], "prompt": "Add a payment form.",
                 "verify_cmd": "make test"},
                {"id": "receipt-page", "title": "Receipt page and e-mail",
                 "model": "DeepSeek-V4.1-Flash-thinking-max", "reviewer": "glm",
                 "deps": ["cart-model"], "prompt": "Add a receipt page.",
                 "verify_cmd": "make test"},
                {"id": "checkout-e2e", "title": "End-to-end checkout test",
                 "model": "DeepSeek-V4.1-Flash-thinking-max", "reviewer": "glm",
                 "deps": ["payment-form", "receipt-page"],
                 "prompt": "Add an end-to-end test.", "verify_cmd": "make e2e"},
            ],
        }
    },
    "demo-docs.json": {
        "project": {
            "repo": REPO,
            "title": "Docs refresh",
            "pattern": "fanout",
            "tasks": [
                {"id": "docs-install", "title": "Rewrite the install guide",
                 "model": "DeepSeek-V4.1-Flash-thinking-max", "reviewer": "glm",
                 "deps": [], "prompt": "Rewrite docs/install.md.",
                 "verify_cmd": "test -f docs/install.md"},
                {"id": "docs-api", "title": "Document the public API",
                 "model": "GLM-5.3", "reviewer": "deepseek",
                 "deps": [], "prompt": "Document the API.",
                 "verify_cmd": "test -f docs/api.md"},
            ],
        }
    },
}

# (taskfile, id, title, model, reviewer, status, created -s, finished -s, error)
ROWS = [
    ("demo-checkout.json", "cart-model", "Cart model with line items and totals",
     "DeepSeek-V4.1-Flash-thinking-max", "glm", "merged", 7200, 5400, None),
    ("demo-checkout.json", "payment-form", "Payment form with validation",
     "GLM-5.3", "deepseek", "in_review", 5000, None, None),
    ("demo-checkout.json", "receipt-page", "Receipt page and e-mail",
     "DeepSeek-V4.1-Flash-thinking-max", "glm", "failed", 5000, 1800,
     "exhausted escalation: 1 escalation(s), ended on GLM-5.3"),
    ("demo-docs.json", "docs-install", "Rewrite the install guide",
     "DeepSeek-V4.1-Flash-thinking-max", "glm", "merged", 90000, 88200, None),
    ("demo-docs.json", "docs-api", "Document the public API",
     "GLM-5.3", "deepseek", "conflict", 90000, 86400,
     "conflict in docs/api.md"),
]

# (task_id, harness, model, role, attempt, exit, seconds, verdict, age -s)
RUNS = [
    ("cart-model-x1", "reasonix", "DeepSeek-V4.1-Flash-thinking-max", "implementer", 1, 0, 412.0, None, 6800),
    ("cart-model-x1", "opencode", "GLM-5.3", "reviewer", 1, 0, 188.0, "pass", 6300),
    ("payment-form-x1", "opencode", "GLM-5.3", "implementer", 1, 0, 1310.0, None, 4200),
    ("payment-form-x1", "reasonix", "DeepSeek-V4.1-Flash-thinking-max", "reviewer", 1, 0, 240.0, "fail", 3900),
    ("payment-form-x2", "opencode", "GLM-5.3", "implementer", 2, 0, 640.0, None, 3000),
    ("receipt-page-x1", "reasonix", "DeepSeek-V4.1-Flash-thinking-max", "implementer", 1, 1, 905.0, None, 4000),
    ("docs-install-x1", "reasonix", "DeepSeek-V4.1-Flash-thinking-max", "implementer", 1, 0, 150.0, None, 89000),
]


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _events():
    """A short, fixed event history: what the activity feed and timelines read."""
    ev = []

    def e(age, typ, **f):
        ev.append({"ts": FROZEN_NOW - age, "type": typ, "workload": "code", **f})

    e(7200, "worktree.alloc", task="cart-model", branch="task/cart-model")
    e(7190, "driver.start", task="cart-model-x1", harness="reasonix",
      model="DeepSeek-V4.1-Flash-thinking-max", role="implementer")
    e(6800, "driver.done", task="cart-model-x1", harness="reasonix",
      model="DeepSeek-V4.1-Flash-thinking-max", role="implementer",
      seconds=412.0, tokens=184000, prompt_tokens=170000, completion_tokens=14000)
    e(6790, "task.gate", task="cart-model", attempt=1, passed=True, cmd="make test")
    e(6300, "task.reviewed", task="cart-model", passed=True, reviewer="GLM-5.3")
    e(6100, "task.pr_opened", task="cart-model", pr=41)
    e(5400, "task.merged", task="cart-model", pr=41)
    e(5000, "worktree.alloc", task="payment-form", branch="task/payment-form")
    e(4200, "driver.done", task="payment-form-x1", harness="opencode", model="GLM-5.3",
      role="implementer", seconds=1310.0, tokens=402000, prompt_tokens=380000,
      completion_tokens=22000)
    e(4190, "task.gate", task="payment-form", attempt=1, passed=True, cmd="make test")
    e(3900, "task.reviewed", task="payment-form", passed=False, reviewer="DeepSeek-V4.1-Flash-thinking-max",
      issues=["card number field accepts letters"])
    e(3000, "task.gate", task="payment-form", attempt=2, passed=True, cmd="make test")
    e(2900, "task.pr_opened", task="payment-form", pr=42)
    e(4000, "task.gate", task="receipt-page", attempt=1, passed=False, cmd="make test",
      tail="failing: test_receipt_total")
    e(1800, "task.failed", task="receipt-page",
      error="exhausted escalation: 1 escalation(s), ended on GLM-5.3")
    e(86400, "task.conflict", task="docs-api", files=["docs/api.md"])
    ev.sort(key=lambda r: r["ts"])
    return ev


def build(root):
    """Write the fixture world under `root` and return its paths as a dict."""
    root = Path(root)
    tasks = root / "tasks"
    home = root / "home"
    repos = root / "repos"
    for d in (tasks, home, repos, root / "worktrees"):
        d.mkdir(parents=True, exist_ok=True)
    for name, data in TASKFILES.items():
        (tasks / name).write_text(json.dumps(data, indent=2), encoding="utf-8")
    db = root / "orchestrator.db"
    if db.exists():
        db.unlink()
    import store                                  # schema lives there
    s = store.Store(str(db))
    for tf, tid, title, model, rev, status, created, finished, err in ROWS:
        s.upsert_code_task(str(tasks / tf), tid, title, model, rev, status,
                           branch=f"task/{tid}")
    s.conn.close()
    con = sqlite3.connect(str(db))
    for tf, tid, _t, _m, _r, status, created, finished, err in ROWS:
        con.execute("UPDATE code_tasks SET created_at=?, finished_at=?, error=? "
                    "WHERE taskfile=? AND id=?",
                    (_iso(FROZEN_NOW - created),
                     _iso(FROZEN_NOW - finished) if finished else None,
                     err, str(tasks / tf), tid))
    for tid, harness, model, role, attempt, rc, secs, verdict, age in RUNS:
        con.execute("INSERT INTO harness_runs(task_id, harness, model, role, attempt, "
                    "exit_code, transcript, seconds, verdict, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (tid, harness, model, role, attempt, rc, "", secs, verdict,
                     _iso(FROZEN_NOW - age)))
    con.commit()
    con.close()
    events_log = root / "logs" / "events.jsonl"
    events_log.parent.mkdir(parents=True, exist_ok=True)
    events_log.write_text("".join(json.dumps(r) + "\n" for r in _events()),
                          encoding="utf-8")
    return {"root": str(root), "db": str(db), "events": str(events_log),
            "tasks": str(tasks), "home": str(home), "repos": str(repos),
            "worktrees": str(root / "worktrees")}
