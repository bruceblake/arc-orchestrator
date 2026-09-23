"""The spend guard the local fleet never needed.

The orchestrator's retry budgets are generous by design — config.py says so
out loud: "Tokens are not the scarce resource here; a task abandoned one
attempt short of working is." That is TRUE ON ARC, where the models are
campus-served and the only cost of another attempt is time.

It is false for the studio fleet. Sixteen fix rounds across five escalation
tiers is up to eighty implementation attempts, and the top tier bills at
$50 per million completion tokens. The same generosity that makes the local
fleet reliable would make the studio fleet expensive in a way nobody notices
until the invoice.

So every direct studio model call passes through here first. The ceiling is
checked BEFORE the call, not after: a budget you discover you crossed is not
a budget. Harness-side spend (implementers, reviewers) is metered separately
by the existing usage page, which prices `harness_runs` through the same
config.cost_of — this ledger covers the calls studio.openrouter makes.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import config
import events

LEDGER = "spend.jsonl"


class BudgetExceeded(RuntimeError):
    """Raised instead of making a call that would cross the ceiling."""


def _ledger_path():
    config.STUDIO_DIR.mkdir(parents=True, exist_ok=True)
    return config.STUDIO_DIR / LEDGER


def record(model, usage, *, task=""):
    """Append one call's cost to the ledger."""
    row = {
        "ts": time.time(),
        "model": model,
        "task": task,
        "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
        "cost_usd": float(usage.get("cost_usd", 0.0) or 0.0),
    }
    with _ledger_path().open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


def rows(since=None):
    """Ledger rows, newest last. `since` is a unix timestamp."""
    path = _ledger_path()
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue                      # a torn write never blocks a run
        if since is not None and float(row.get("ts", 0)) < since:
            continue
        out.append(row)
    return out


def spent(since=None):
    """Total USD spent on direct studio calls."""
    return round(sum(float(r.get("cost_usd", 0.0) or 0.0) for r in rows(since)), 6)


def remaining(since=None):
    """USD left under the ceiling; None when no ceiling is configured."""
    if config.STUDIO_BUDGET_USD <= 0:
        return None
    return round(config.STUDIO_BUDGET_USD - spent(since), 6)


def guard(model, *, task="", since=None):
    """Refuse to start a call that the ceiling cannot cover.

    The estimate is deliberately crude — one call's cost is not knowable
    before it is made — so this stops at the ceiling rather than trying to
    predict the last call exactly. Set ARC_STUDIO_BUDGET_USD=0 to disable.
    """
    left = remaining(since)
    if left is None:
        return None
    if left <= 0:
        events.emit("studio.budget_exceeded", model=model, task=task,
                    spent_usd=spent(since),
                    ceiling_usd=config.STUDIO_BUDGET_USD)
        raise BudgetExceeded(
            f"studio budget exhausted: ${spent(since):.2f} of "
            f"${config.STUDIO_BUDGET_USD:.2f} spent on direct model calls. "
            f"Refusing to call {model} for {task or 'an unnamed task'}. "
            "Raise ARC_STUDIO_BUDGET_USD, or set it to 0 to remove the "
            "ceiling entirely.")
    return left


def summary(since=None):
    """Spend broken down by model, for `main.py studio budget`."""
    per = {}
    for r in rows(since):
        m = r.get("model", "?")
        e = per.setdefault(m, {"calls": 0, "prompt_tokens": 0,
                               "completion_tokens": 0, "cost_usd": 0.0})
        e["calls"] += 1
        e["prompt_tokens"] += int(r.get("prompt_tokens", 0) or 0)
        e["completion_tokens"] += int(r.get("completion_tokens", 0) or 0)
        e["cost_usd"] += float(r.get("cost_usd", 0.0) or 0.0)
    for e in per.values():
        e["cost_usd"] = round(e["cost_usd"], 6)
    return {
        "ceiling_usd": config.STUDIO_BUDGET_USD,
        "spent_usd": spent(since),
        "remaining_usd": remaining(since),
        "by_model": dict(sorted(per.items(), key=lambda kv: -kv[1]["cost_usd"])),
    }
