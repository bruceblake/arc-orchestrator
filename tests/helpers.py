"""Shared test scaffolding: import path, event capture, fake store."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import events  # noqa: E402


class capture_events:
    """Context manager collecting every events.emit() call as (type, fields)."""

    def __init__(self):
        self.seen = []

    def __enter__(self):
        self._orig = events.emit

        def fake(type, **fields):
            self.seen.append((type, fields))

        events.emit = fake
        return self

    def __exit__(self, *exc):
        events.emit = self._orig
        return False

    def of(self, etype):
        return [f for t, f in self.seen if t == etype]

    def first(self, etype):
        hits = self.of(etype)
        return hits[0] if hits else None


class FakeStore:
    """Enough of store.Store for build_code_graph and the node coroutines."""

    def __init__(self, prior=None):
        self._prior = list(prior or [])
        self.upserts = []
        self.harness_runs = []

    def code_tasks_for(self, taskfile):
        return list(self._prior)

    def upsert_code_task(self, taskfile, tid, title, model, reviewer, status, **kw):
        self.upserts.append({"taskfile": taskfile, "id": tid, "model": model,
                             "reviewer": reviewer, "status": status, **kw})

    def save_harness_run(self, *a, **kw):
        self.harness_runs.append((a, kw))
