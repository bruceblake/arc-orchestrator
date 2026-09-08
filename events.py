"""Append-only JSONL event log shared by all workloads and the dashboard.

Every event carries the current workload context (workload / round /
iteration / module) taken from contextvars, so events emitted deep inside
graph node coroutines are attributed correctly as long as the context was
set before the graph's worker tasks were created.
"""
import json
import os
import threading
import time
from contextvars import ContextVar
from pathlib import Path

import config

_workload = ContextVar("workload", default=None)
_round = ContextVar("round", default=None)
_iteration = ContextVar("iteration", default=None)
_module = ContextVar("module", default=None)

_lock = threading.Lock()
_checked_rotation = False
_bytes_written = 0
_ROTATE_BYTES = 100 * 1024 * 1024
_RECHECK_EVERY = 8 * 1024 * 1024


def set_context(*, workload=None, round=None, iteration=None, module=None):
    if workload is not None:
        _workload.set(workload)
    if round is not None:
        _round.set(round)
    if iteration is not None:
        _iteration.set(iteration)
    if module is not None:
        _module.set(module)


def context():
    return {
        "workload": _workload.get(),
        "round": _round.get(),
        "iteration": _iteration.get(),
        "module": _module.get(),
    }


def emit(type, **fields):
    """Append one event; never raises (dashboard loss is acceptable, work is not)."""
    global _checked_rotation, _bytes_written
    rec = {"ts": round(time.time(), 3), "type": type}
    rec.update(context())
    rec.update(fields)
    line = json.dumps(rec, default=str)
    path = Path(config.EVENTS_LOG)
    with _lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _bytes_written += len(line) + 1
            if (not _checked_rotation or _bytes_written >= _RECHECK_EVERY) and path.exists():
                _checked_rotation = True
                _bytes_written = 0
                if path.stat().st_size > _ROTATE_BYTES:
                    os.replace(path, path.with_name(path.name + ".1"))
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass