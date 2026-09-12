"""Shared test scaffolding: import path, event capture, fake store."""
import atexit
import logging
import pathlib
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import events  # noqa: E402

# Tests must never write to the operator's real event log. Several of them
# deliberately drive failure paths, and check.sh runs the suite inside every
# task's verify gate — without this redirect each gate run injected fake
# node_error/driver.error records into logs/events.jsonl, where the dashboard
# reported them to the operator as real fleet problems.
_EVENT_DIR = tempfile.mkdtemp(prefix="arc-tests-events-")
config.EVENTS_LOG = str(pathlib.Path(_EVENT_DIR) / "events.jsonl")
# The same reasoning now applies to the DATABASE. errors.capture() writes to
# config.DB_PATH, and the suite deliberately drives failure paths — without
# this, every test run injected dozens of synthetic defects into the operator's
# triage list, where they are indistinguishable from real ones. Verified: a
# single run put 43 test exceptions into the production error table.
config.DB_PATH = str(pathlib.Path(_EVENT_DIR) / "test.db")
atexit.register(lambda: shutil.rmtree(_EVENT_DIR, ignore_errors=True))

# Several tests deliberately drive failure paths (node crashes, driver retry
# ladders, gather deadlocks). Their log output is expected, and printing it
# buries a real failure in the runner's output — and in check.sh's.
logging.getLogger("drivers").setLevel(logging.CRITICAL)
logging.getLogger("code-tasks").setLevel(logging.CRITICAL)
for _name in ("graph.t", "graph.code-tasks"):
    logging.getLogger(_name).setLevel(logging.CRITICAL)
logging.getLogger().addHandler(logging.NullHandler())


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

    def __init__(self, prior=None, by_taskfile=None):
        self._prior = list(prior or [])
        # Chaining tests need `code_tasks_for` to answer per taskfile key;
        # everything else keeps the old flat-list behavior.
        self._by_taskfile = dict(by_taskfile or {})
        self.upserts = []
        self.harness_runs = []

    def code_tasks_for(self, taskfile):
        if taskfile in self._by_taskfile:
            return list(self._by_taskfile[taskfile])
        return list(self._prior)

    def upsert_code_task(self, taskfile, tid, title, model, reviewer, status, **kw):
        self.upserts.append({"taskfile": taskfile, "id": tid, "model": model,
                             "reviewer": reviewer, "status": status, **kw})

    def save_harness_run(self, *a, **kw):
        self.harness_runs.append((a, kw))


# --- roster-aware fixture names ------------------------------------------
# The model roster is DATED (config.ROSTER): names change on the provider's
# schedule. A fixture that hard-codes "Kimi-K3" is a test that breaks on
# 2026-09-19 for a reason unrelated to what it tests. Use these instead, and
# run the suite under ARC_ROSTER_DATE=<date> before each transition.
import unittest as _ut

ENTRY = config.ESCALATION_PATH[0]        # weakest live implementer
STRONGEST = config.ESCALATION_PATH[-1]   # strongest live implementer
KIMI_LIVE = "Kimi-K3" in config.IMPLEMENTER_MODELS
DEEPSEEK_V4_LIVE = "DeepSeek-V4-Flash" in config.IMPLEMENTER_MODELS
needs_kimi = _ut.skipUnless(KIMI_LIVE, "tests Kimi-K3 behaviour; Kimi-K3 is not on today's roster")
needs_deepseek_v4 = _ut.skipUnless(DEEPSEEK_V4_LIVE, "tests DeepSeek-V4-Flash's restricted roles; not live")
needs_three_families = _ut.skipUnless(len(config.REVIEW_FAMILIES) >= 2 and len(config.IMPLEMENTER_MODELS) >= 3,
                                      "needs three implementer families on the roster")
STRONGEST_FAMILY = config.MODEL_FAMILY[STRONGEST]      # for "same-family reviewer" fixtures
STRONGEST_REVIEWER = config.cross_family_reviewer(STRONGEST)  # its correct cross-family reviewer
