"""Lightweight dashboard: static UI + JSON APIs over the event log and sqlite DB.

Run alongside the orchestrator (separate process):
    python main.py serve [--port 8787]

Usage layers (why a model can appear under more than one source):
  arc-pool          raw API requests the pool made (request/request_end events, carry tokens)
  driver:<harness>  whole agent task runs in an external CLI harness (driver.* events;
                    driver.done carries tokens since drivers.py learned transcript_tokens)
  kimi-code         HISTORICAL: per-API-call usage parsed from the retired
                    kimi CLI's session wire logs (2026-09-12, Kimi-K3). Kept so
                    old interactive sessions still price instead of $0.00.
"""
import asyncio
import errno
import hmac
import json
import logging
import os
import sys
import re
import signal
import socket
import sqlite3
import subprocess
import threading
import time
from collections import OrderedDict
from datetime import date as _date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import config
import gitstore
import orchchat
from store import Store

log = logging.getLogger("dashboard")

_lines_cache = {"key": None, "lines": []}
MAX_EVENTS_PER_RESPONSE = 3000

# Live models first; the retired ones (Kimi-K3, gpt-oss-120b,
# DeepSeek-V4-Flash) stay mapped so their HISTORICAL usage/harness_runs rows
# still render a name and a price instead of a raw id and $0.00. They are not
# offered anywhere as a routing choice — see config.MODEL_ROLES.
PRETTY = {"GLM-5.3": "GLM 5.3",
          "DeepSeek-V4.1-Flash-thinking-max": "DeepSeek V4.1 Flash max",
          "DeepSeek-V4.1-Flash": "DeepSeek V4.1 Flash",
          "Kimi-K3": "Kimi K3 (retired)",
          "gpt-oss-120b": "gpt-oss 120B (retired)",
          "DeepSeek-V4-Flash": "DeepSeek V4 Flash (retired)"}

# Rolling windows plus one calendar window. "today" is deliberately not a
# synonym for 24h: at 09:00 a rolling day is mostly yesterday, and "what has
# the fleet done today" is the question an operator actually asks.
RANGES = ["1h", "3h", "6h", "today", "24h", "7d", "all"]
_RANGE_SECONDS = {"1h": 3600, "3h": 3 * 3600, "6h": 6 * 3600,
                  "24h": 86400, "7d": 7 * 86400}


def _range_cutoff(range_key, now):
    """Epoch seconds the window starts at, or None for 'all'."""
    if range_key == "today":
        return _day_window(time.strftime("%Y-%m-%d", time.localtime(now)))[0]
    return None if range_key == "all" else now - _RANGE_SECONDS[range_key]


def _day_window(date_s):
    """(start_epoch, end_epoch) — the half-open window of one calendar day.

    ONE definition of "a day" for both cuts of the same traffic: the daily
    window ("today") and /api/usage/hourly?date=. Two copies would disagree at
    a DST boundary or off a midnight edge, and the operator would be looking
    at two views of the same day with different numbers and no way to tell
    which to believe. tm_isdst=-1 lets mktime resolve the flag.

    The end is the NEXT CALENDAR day's midnight, re-derived from the date, not
    `start + 86400`. A local day is 23 or 25 hours long across a DST
    transition: a fixed 86400 ends at 23:00 on the fall-back day (dropping an
    hour the daily rows still count) and runs into the next local day on the
    spring-forward one (stealing an hour the daily rows count under the other
    date). That is how the daily and hourly views came to disagree on those
    two days a year. Because the window is the span between two consecutive
    local midnights, "inside [start, end)" and "local date == date_s" are the
    SAME predicate — and that is the one the daily rows are keyed by.
    """
    y, m, d = (int(p) for p in date_s.split("-"))
    start = time.mktime((y, m, d, 0, 0, 0, 0, 0, -1))
    nxt = _date(y, m, d) + timedelta(days=1)
    end = time.mktime((nxt.year, nxt.month, nxt.day, 0, 0, 0, 0, 0, -1))
    return start, end


def _bucket_points(points, start, n_buckets, bucket_secs, locate=None):
    """Cut `points` into `n_buckets` buckets — the ONE bucketing.

    `points` carries the (ts, family, model, requests, ok, errors,
    failed_attempts, tokens, task_runs) tuples the event walk in _usage emits.
    Every counter is carried EXPLICITLY rather than derived: a crashed driver
    attempt is not a failed request (it adds to failed_attempts without adding
    a request), so an `ok = requests - errors` identity would report -1 for it
    and the hourly buckets would disagree with the daily totals beside them.
    Returns (buckets, totals, by_model): buckets[i]["totals"]/["by_model"] hold
    the counters in that slice, totals sums the whole window, and by_model sums
    it keyed by model. Points outside the window (or without a usable ts) are
    dropped, never clamped into an edge bucket.

    `locate` names the bucket for a ts, or None to drop the point. The default
    is the fixed-width `(ts - start) // bucket_secs`, which is right only when
    a bucket IS a fixed number of seconds. The hourly view passes one that
    reads the LOCAL hour, because its buckets are labelled wall-clock hours:
    fixed-width arithmetic assumes a 86400 s day, so after an intra-day DST
    transition every later label is off by an hour and the day's own 25th hour
    has no bucket to fall into.
    """
    def zero():
        return {"requests": 0, "ok": 0, "errors": 0, "failed_attempts": 0,
                "tokens": 0, "task_runs": 0}

    buckets = [{"totals": zero(), "by_model": {}} for _ in range(n_buckets)]
    totals = zero()
    by_model = {}
    for ts, _family, model, req, ok, err, failed, tok, tr in points:
        if not ts or ts < start:
            continue
        if locate is None:
            idx = int((ts - start) // bucket_secs)
        else:
            idx = locate(ts)
        if idx is None or idx < 0 or idx >= n_buckets:
            continue
        for acc in (buckets[idx]["totals"], totals):
            acc["requests"] += req
            acc["ok"] += ok
            acc["errors"] += err
            acc["failed_attempts"] += failed
            acc["tokens"] += tok
            acc["task_runs"] += tr
        for acc in (buckets[idx]["by_model"].setdefault(model, {"requests": 0, "tokens": 0}),
                    by_model.setdefault(model, {"requests": 0, "tokens": 0})):
            acc["requests"] += req
            acc["tokens"] += tok
    return buckets, totals, by_model

_launch_registry = {}  # abspath taskfile -> {"pid", "log", "started", "dry_run"}


def _pretty(model):
    if model in PRETTY:
        return PRETTY[model]
    return re.sub(r"-(thinking|legacy)[\w-]*$", "", model or "unknown").replace("-", " ")


# GET /api/activity — the curated set the fleet activity feed shows.
# Deliberately NOT the whole log: `driver.heartbeat` fires several times a
# second per harness and would swamp a feed a human reads, and node_start /
# driver.done are per-step noise. These are the moments that mean something
# happened to a task, or to the fleet's capacity to run one.
ACTIVITY_TYPES = frozenset((
    "task.reviewed", "task.failed", "task.escalated", "task.merged",
    "task.pr_opened", "task.pr_reviewed", "task.resynced",
    "task.review_degraded", "driver.stalled",
    "driver.usage_limit", "driver.usage_swap",
    "chain.wait", "chain.ready", "chain.blocked",
))
# A feed is a window, not a log download: the page asks for 50, and a
# hand-typed ?limit= must not turn this endpoint into "read the whole history".
ACTIVITY_MAX_LIMIT = 500


def _load_event_lines():
    path = Path(config.EVENTS_LOG)
    try:
        st = path.stat()
    except OSError:
        return []
    key = (st.st_size, st.st_mtime_ns)
    if _lines_cache["key"] != key:
        try:
            _lines_cache["lines"] = path.read_text(encoding="utf-8", errors="replace").splitlines()
            _lines_cache["key"] = key
        except OSError:
            return []
    return _lines_cache["lines"]


def _ts(v):
    return v if isinstance(v, (int, float)) else None


_kimi_cache = {}  # wire.jsonl path -> {"key": (size, mtime_ns), "agg": parsed}
STALE_INFLIGHT_S = 600  # ignore unanswered llm.request older than this (dead client)
# An unmatched driver.start settles within its total budget + retry backoff:
# the driver itself errors (and settles) every attempt it owns. Anything older
# belongs to a killed run and must not count against concurrency caps. With
# unlimited budgets (the default) the 24h lease TTL is the only outer bound.
_longest_total = config.longest_total_timeout()
DRIVER_STALE_S = float(os.getenv("ARC_DRIVER_STALE_S", "0")) or (
    (_longest_total + 240) if _longest_total > 0
    else config.DRIVER_LEASE_TTL + 240)
POOL_STALE_S = 7200  # unmatched pool request_start older than this is a dead client
_stale_emitted = set()  # keys already reported via driver.stale / request.stale
_over_emitted = set()   # families already reported via inflight.over_cap


def _emit_event(etype, **fields):
    """Best-effort event emission from the dashboard process (instrumentation)."""
    try:
        import events
        events.emit(etype, **fields)
    except Exception:
        pass


def _parse_kimi_wire(path):
    """Parse one kimi-code wire.jsonl into per-model counters.

    Relevant event shapes (times in ms):
      {"type":"llm.request","model":"Kimi-K3","modelAlias":"arc/kimi-k3","agentId":"main","time":...}
      {"type":"usage.record","model":"arc/kimi-k3","usage":{"inputOther":N,"output":N,
       "inputCacheRead":N,"inputCacheCreation":N},"usageScope":"turn"|"session","time":...}
      {"type":"context.append_loop_event","event":{"type":"step.end",...},"time":...}
    A request counts as in flight when the newest llm.request is not followed by
    any usage.record / step.end (turn-scope usage records mark completed calls;
    session-scope ones are cumulative snapshots and are skipped for totals).
    """
    models = {}
    recent = []  # (ts_s, req_delta, tok_delta)
    turn_log = []  # (ts_s, alias, req_delta, prompt_delta, completion_delta, ok_delta)
    alias_real = {}
    last_req = None  # (ts_s, real_model, agent)
    last_done = 0.0
    pending = []  # ts_s of requests not yet answered, FIFO
    latency_total_ms = 0.0
    latency_count = 0
    file_totals = {"requests": 0, "ok": 0, "prompt": 0, "completion": 0,
                   "cache_read": 0, "last_ts": None}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        if "llm.request" in line:
            kind = "request"
        elif "usage.record" in line:
            kind = "usage"
        elif '"step.end"' in line:
            kind = "step"
        else:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        ts = e.get("time")
        ts = ts / 1000 if isinstance(ts, (int, float)) else None
        if kind == "request":
            alias = e.get("modelAlias") or e.get("model") or "unknown"
            alias_real[alias] = e.get("model") or alias
            mod = models.setdefault(alias, {"requests": 0, "ok": 0, "prompt": 0,
                                            "completion": 0, "last_ts": None})
            mod["requests"] += 1
            file_totals["requests"] += 1
            pending.append(ts)
            if ts:
                mod["last_ts"] = ts
                recent.append((ts, 1, 0))
                turn_log.append((ts, alias, 1, 0, 0, 0))
                if file_totals["last_ts"] is None or ts > file_totals["last_ts"]:
                    file_totals["last_ts"] = ts
                if last_req is None or ts >= last_req[0]:
                    last_req = (ts, e.get("model") or alias, e.get("agentId") or path.parent.name)
            continue
        if ts and ts > last_done:
            last_done = ts
        if kind == "usage":
            if e.get("usageScope") != "turn":
                continue
            alias = e.get("model") or "unknown"
            mod = models.setdefault(alias, {"requests": 0, "ok": 0, "prompt": 0,
                                            "completion": 0, "last_ts": None})
            u = e.get("usage") or {}
            inp = ((u.get("inputOther") or 0) + (u.get("inputCacheRead") or 0)
                   + (u.get("inputCacheCreation") or 0))
            out = u.get("output") or 0
            mod["ok"] += 1
            mod["prompt"] += inp
            mod["completion"] += out
            file_totals["ok"] += 1
            file_totals["prompt"] += inp
            file_totals["completion"] += out
            file_totals["cache_read"] += u.get("inputCacheRead") or 0
            if pending and ts is not None and pending[0] is not None:
                dt = ts - pending[0]
                if 0 <= dt < 3600:
                    latency_total_ms += dt * 1000
                    latency_count += 1
            if pending:
                pending.pop(0)
            if ts:
                mod["last_ts"] = ts
                if file_totals["last_ts"] is None or ts > file_totals["last_ts"]:
                    file_totals["last_ts"] = ts
                recent.append((ts, 0, inp + out))
                turn_log.append((ts, alias, 0, inp, out, 1))
    return {"models": models, "alias_real": alias_real, "recent": recent,
            "last_req": last_req, "last_done": last_done, "file_totals": file_totals,
            "turn_log": turn_log,
            "avg_latency_ms": round(latency_total_ms / latency_count) if latency_count else None}


_fleet_names_cache = {"key": 0.0, "names": frozenset()}


def _fleet_task_names(store):
    """Task ids the fleet has ever run — cached ~30s.

    kimi-code names each session directory `wd_<cwd-basename>_<hash>`, and a
    fleet driver runs with cwd set to its worktree, whose basename is the task
    id. That is how a session is recognised as the fleet's own.
    """
    now = time.time()
    if now - _fleet_names_cache["key"] < 30:
        return _fleet_names_cache["names"]
    names = set()
    try:
        for row in (store.code_tasks_all() if store else []):
            if row.get("id"):
                names.add(row["id"])
    except Exception:
        pass
    try:
        root = Path(config.WORKTREE_ROOT)
        for repo_dir in root.iterdir() if root.is_dir() else []:
            for wt in repo_dir.iterdir() if repo_dir.is_dir() else []:
                names.add(wt.name)
    except OSError:
        pass
    _fleet_names_cache.update(key=now, names=frozenset(names))
    return _fleet_names_cache["names"]


def _session_task(path):
    """Task id from a `.../wd_<task>_<hash>/session_*/agents/<a>/wire.jsonl`."""
    for part in path.parts:
        if part.startswith("wd_"):
            stem = part[3:]
            return stem.rsplit("_", 1)[0] if "_" in stem else stem
    return None


def _kimi_code_usage(now, fleet_names=frozenset()):
    """Aggregate all kimi-code session logs into dashboard-shaped rows + points.

    Token totals cover every session (kimi's stream-json carries no usage, so
    the wire logs are the only source). The IN-FLIGHT list, however, excludes
    sessions belonging to the fleet's own worktrees: those are already counted
    from driver.start/driver.done, and counting them twice made a single fleet
    driver read as several agents against the ARC account cap — killed attempts
    leave an unanswered llm.request that looks live for STALE_INFLIGHT_S, so a
    retried task inflated the number further.
    """
    root = Path.home() / ".kimi-code" / "sessions"
    agg = {}
    inflight = []
    points = []  # (ts, requests_delta, tokens_delta)
    turns = []  # (ts, real_model, requests_delta, prompt_delta, completion_delta, ok_delta)
    live = set()
    paths = root.glob("*/*/agents/*/wire.jsonl") if root.is_dir() else []
    for path in paths:
        sp = str(path)
        live.add(sp)
        try:
            st = path.stat()
        except OSError:
            continue
        key = (st.st_size, st.st_mtime_ns)
        ent = _kimi_cache.get(sp)
        if ent is None or ent["key"] != key:
            ent = {"key": key, "agg": _parse_kimi_wire(path)}
            _kimi_cache[sp] = ent
        data = ent["agg"]
        for alias, m in data["models"].items():
            real = data["alias_real"].get(alias, alias)
            if real == "unknown":
                continue  # internal calls without a model field (titles, cron)
            row = agg.setdefault(real, {"model": real, "pretty": _pretty(real),
                                        "family": "kimi-code", "source": "kimi-code",
                                        "requests": 0, "ok": 0, "errors": 0,
                                        "failed_attempts": 0, "tokens": 0,
                                        "prompt_tokens": 0, "completion_tokens": 0,
                                        "avg_latency_ms": None, "last_ts": None})
            row["requests"] += m["requests"]
            row["ok"] += m["ok"]
            row["prompt_tokens"] += m["prompt"]
            row["completion_tokens"] += m["completion"]
            if m["last_ts"] and (row["last_ts"] is None or m["last_ts"] > row["last_ts"]):
                row["last_ts"] = m["last_ts"]
        points.extend(data["recent"])
        for t, alias, rd, pd, cd, ok_delta in data.get("turn_log", []):
            if t is None:
                continue
            real = data["alias_real"].get(alias, alias)
            if real == "unknown":
                continue  # internal calls without a model field (titles, cron)
            turns.append((t, real, rd, pd, cd, ok_delta))
        lr = data["last_req"]
        owner = _session_task(path)
        if owner and owner in fleet_names:
            continue  # the fleet's own driver — already counted via driver.*
        if lr and lr[1] != "unknown" and lr[0] > data["last_done"] and (now - lr[0]) < STALE_INFLIGHT_S:
            inflight.append({"req_id": "kimi-code/" + lr[2], "family": "kimi-code",
                             "model": lr[1], "pretty": _pretty(lr[1]), "source": "kimi-code",
                             "purpose": "kimi-code session", "harness": "kimi-code",
                             "role": None, "task": None, "websearch": False,
                             "started": lr[0], "elapsed_s": round(now - lr[0], 1),
                             "transcript": None})
    for sp in [p for p in _kimi_cache if p not in live]:
        del _kimi_cache[sp]
    models = list(agg.values())
    for m in models:
        m["tokens"] = m["prompt_tokens"] + m["completion_tokens"]
    return {"models": models, "inflight": inflight, "points": points, "turns": turns}


def _series_window(range_key, now, min_ts):
    """(start_epoch, bucket_secs, n_points) — 1h axis stays exactly as it always was."""
    if range_key in ("3h", "6h"):
        span = _RANGE_SECONDS[range_key]
        return int((now - span) // 300) * 300, 300, span // 300 + 1
    if range_key == "today":
        start = int(_range_cutoff("today", now) // 300) * 300
        return start, 300, max(2, int((now - start) // 300) + 2)
    if range_key == "24h":
        return int((now - 86400) // 300) * 300, 300, 289
    if range_key == "7d":
        return int((now - 7 * 86400) // 3600) * 3600, 3600, 169
    if range_key == "all":
        if not min_ts:
            min_ts = now - 3600
        span = now - min_ts
        bucket = 86400
        if span > 120 * 86400:
            import math
            bucket = math.ceil(span / (119 * 86400)) * 86400
        return int(min_ts // bucket) * bucket, bucket, min(int(span // bucket) + 2, 120)
    return int((now - 3600) // 60) * 60, 60, 61


_transcript_cache = {}  # path -> {"key": (size, mtime_ns), "tok": (tokens, prompt, completion)}


def _xkey(task):
    """('world-persistence', 2) from 'world-persistence-x2' — DB ids carry no -xN suffix."""
    m = re.match(r"^(.*?)-x(\d+)$", task or "")
    return (m.group(1), int(m.group(2))) if m else (task, None)


def _activity_file(taskfile_of, tid):
    """The taskfile an activity event's task id belongs to, or None.

    Driver events carry the HARNESS's id, never a code_tasks row key: an
    implement/review attempt is `<tid>-xN` and a PR reviewer is `<tid>-prN`
    (code_tasks.py passes both straight to driver.run), while the rows are
    keyed by the BASE id alone (`_xkey`: "DB ids carry no -xN suffix"). An
    exact-match lookup therefore missed every driver event - and a stalled
    harness is exactly the row an operator wants to open, so it rendered as a
    button that opened nothing.
    """
    if not tid:
        return None
    if tid in taskfile_of:
        return taskfile_of[tid]      # a task id that IS the row key
    base, _x = _xkey(tid)            # '<tid>-xN' -> '<tid>'
    return taskfile_of.get(re.sub(r"-pr\d+$", "", base))    # '<tid>-prN'


def _transcript_toks(tpath):
    """(tokens, prompt, completion) for one transcript file, cached by size+mtime."""
    if not tpath:
        return (0, 0, 0)
    try:
        from drivers import transcript_tokens
    except Exception:
        return (0, 0, 0)
    tp = str(tpath)
    try:
        st = Path(tp).stat()
    except OSError:
        return (0, 0, 0)
    ck = (st.st_size, st.st_mtime_ns)
    ent = _transcript_cache.get(tp)
    if ent is None or ent["key"] != ck:
        try:
            raw = Path(tp).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return (0, 0, 0)
        ent = {"key": ck, "tok": transcript_tokens(raw)}
        _transcript_cache[tp] = ent
    return ent["tok"]


def _opencode_token_backfill(store, done_tok_keys):
    """Token points for OLD opencode runs whose driver.done pre-dates token plumbing.

    Returns list of (ts, model, tokens, prompt, completion). A run is skipped when a
    driver.done event for the same (harness, model, task, attempt) already carried
    tokens (event log wins) or the transcript vanished.
    """
    if store is None:
        return []
    try:
        from drivers import transcript_tokens
    except Exception:
        return []
    points = []
    try:
        rows = store.harness_runs_all()
    except Exception:
        return points
    for row in rows:
        if row.get("harness") != "opencode":
            continue  # kimi usage is already counted from the wire logs
        key = (row.get("harness"), row.get("model"), row.get("task_id"), row.get("attempt"))
        if key in done_tok_keys:
            continue
        tp = row.get("transcript") or ""
        path = Path(tp)
        try:
            st = path.stat()
        except OSError:
            continue
        ck = (st.st_size, st.st_mtime_ns)
        ent = _transcript_cache.get(tp)
        if ent is None or ent["key"] != ck:
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            ent = {"key": ck, "tok": transcript_tokens(raw)}
            _transcript_cache[tp] = ent
        tokens, prompt, completion = ent["tok"]
        if not tokens:
            continue
        try:
            ts = datetime.fromisoformat(row["created_at"]).timestamp()
        except (TypeError, ValueError):
            continue
        points.append((ts, row.get("model"), tokens, prompt, completion))
    return points


def _collect_inflight(now, store=None):
    """Unmatched start events across all three layers -> live agent rows."""
    rows = []
    starts = {}      # request_start req_id -> event (in-flight raw pool/stream requests)
    driver_starts = {}  # (harness, model, role, task, attempt) -> event (in-flight task runs)
    driver_progress = {}  # same key -> newest driver.progress heartbeat
    driver_last = {}  # same key -> newest start/heartbeat/stalled/timeout event
    for line in _load_event_lines():
        try:
            e = json.loads(line)
        except Exception:
            continue
        etype = e.get("type")
        if etype == "request_start":
            rid = e.get("req_id")
            if rid:
                starts[rid] = e
        elif etype in ("request", "request_end"):
            starts.pop(e.get("req_id"), None)
        elif etype == "driver.start":
            key = (e.get("harness"), e.get("model"), e.get("role"),
                   e.get("task"), e.get("attempt"))
            driver_starts[key] = e
            driver_last[key] = e
        elif etype in ("driver.heartbeat", "driver.stalled", "driver.timeout"):
            # Liveness/failure pings for an in-flight attempt — they settle
            # nothing, but the newest one is what "last_event_s" reports.
            driver_last[(e.get("harness"), e.get("model"), e.get("role"),
                         e.get("task"), e.get("attempt"))] = e
        elif etype == "driver.progress":
            # Not a terminal event — it settles nothing. It is proof the driver
            # was alive at that moment, and carries the idle/CPU sample that
            # says whether it is working or blocked.
            driver_progress[(e.get("harness"), e.get("model"), e.get("role"),
                             e.get("task"), e.get("attempt"))] = e
        elif etype in ("driver.done", "driver.error", "driver.stale",
                       "driver.cancelled", "driver.cap_timeout"):
            key = (e.get("harness"), e.get("model"), e.get("role"), e.get("task"), e.get("attempt"))
            if key in driver_starts:
                del driver_starts[key]
                driver_last.pop(key, None)
            else:  # driver.error/stale may carry no role; settle by the other fields
                for k in list(driver_starts):
                    if k[:2] == key[:2] and k[3:] == key[3:]:
                        del driver_starts[k]
                        driver_last.pop(k, None)
                        break
        elif etype == "request.stale":
            starts.pop(e.get("req_id"), None)
    # Prune phantom in-flight rows: starts that can no longer be real because
    # their owner would have errored out long ago (killed runs never settle).
    # Emit one event per pruned key so the reconciliation survives restarts.
    for rid, e in list(starts.items()):
        started = _ts(e.get("ts")) or now
        if started < now - POOL_STALE_S:
            del starts[rid]
            skey = ("req", rid)
            if skey not in _stale_emitted:
                _stale_emitted.add(skey)
                _emit_event("request.stale", req_id=rid, model=e.get("model"),
                            family=e.get("family"), age_s=round(now - started),
                            note="request_start with no end for >2h — dead client, pruned from in-flight")
    for key, e in list(driver_starts.items()):
        started = _ts(e.get("ts")) or now
        # Two independent prune rules:
        #  1. the owner process is gone — `driver.start` carries the pid, and a
        #     dead pid is proof the run cannot still be working, so the row is
        #     dropped NOW instead of waiting out DRIVER_STALE_S below. That
        #     ceiling is DRIVER_LEASE_TTL + 240 (~24h) under the default
        #     unlimited budgets, so a killed run used to render as a live
        #     agent for a full day (measured 2026-09-15: a dead driver shown
        #     for ~18h). A start with NO pid is silent evidence and is NOT
        #     treated as dead — only a present pid that `_pid_alive` rejects.
        #  2. age: a start with no pid, or with a pid that is somehow still
        #     alive, still ages out at DRIVER_STALE_S.
        pid = e.get("pid")
        dead_owner = pid is not None and not _pid_alive(pid)
        if dead_owner or started < now - DRIVER_STALE_S:
            del driver_starts[key]
            if key not in _stale_emitted:
                _stale_emitted.add(key)
                _emit_event("driver.stale", harness=key[0], model=key[1], role=key[2],
                            task=key[3], attempt=key[4], age_s=round(now - started),
                            **({"pid": pid} if dead_owner else {}),
                            note=("driver.start names a pid that is gone — owning process is dead, pruned from in-flight"
                                  if dead_owner else
                                  "driver.start older than DRIVER_TIMEOUT+buffer with no done/error — run was killed, pruned from in-flight"))
    for rid, e in starts.items():
        started = _ts(e.get("ts")) or now
        rows.append({"req_id": rid, "family": e.get("family"), "model": e.get("model"),
                     "pretty": _pretty(e.get("model")), "source": "arc-pool",
                     "purpose": e.get("purpose"), "harness": None,
                     "role": None, "task": None, "websearch": bool(e.get("websearch")),
                     "started": started, "elapsed_s": round(max(0.0, now - started), 1),
                     "transcript": None})
    for (harness, model, role, task, attempt), e in driver_starts.items():
        started = _ts(e.get("ts")) or now
        transcript = None
        if task:
            hits = sorted(Path(config.ROOT, "logs", "harness").glob(
                f"{task}-{role}-{attempt}.jsonl")) if role else []
            if hits:
                transcript = hits[-1].name
        prog = driver_progress.get((harness, model, role, task, attempt)) or {}
        prog_ts = _ts(prog.get("ts"))
        # A progress sample is only current while it keeps arriving: the driver
        # samples every config.DRIVER_PROGRESS_INTERVAL, so a newest sample
        # older than twice that was taken by a run that has stopped sampling
        # (killed, or wedged). Its cpu_delta_s/state are then FROZEN historical
        # values — a 0.0 CPU reading from hours ago rendered as "no CPU right
        # now" — so they are dropped to None rather than carried forward as if
        # current. `bytes` stays: it is a cumulative total, truthful either way.
        # No JS change is needed: static/panels/agents.js:57 already guards the
        # "· no CPU" suffix on `a.cpu_delta_s != null`, so None renders nothing.
        fresh_sample = (prog_ts is not None
                        and (now - prog_ts) <= config.DRIVER_PROGRESS_INTERVAL * 2)
        # Idle time from the newest heartbeat, carried forward to now.
        idle_s = None
        if prog_ts is not None and isinstance(prog.get("idle_s"), (int, float)):
            idle_s = round(prog["idle_s"] + max(0.0, now - prog_ts), 1)
        # Age of the agent's most recent liveness event (start/heartbeat/
        # stalled/timeout) — the heartbeat the UI renders per agent. A run
        # is "stalled" when that newest event is a stall report or its idle
        # time has grown far past what a live driver would tolerate.
        last = driver_last.get((harness, model, role, task, attempt)) or e
        last_ts = _ts(last.get("ts")) or started
        rows.append({"req_id": f"driver/{harness}:{model}:{role}:{task}",
                     "family": config.MODEL_FAMILY.get(model, "harness"), "model": model,
                     "pretty": _pretty(model), "source": f"driver:{harness}",
                     "purpose": f"{harness} {role}", "harness": harness,
                     "role": role, "task": task, "websearch": False,
                     "pid": e.get("pid"),
                     "started": started, "elapsed_s": round(max(0.0, now - started), 1),
                     "idle_s": idle_s, "bytes": prog.get("bytes"),
                     "state": prog.get("state") if fresh_sample else None,
                     "cpu_delta_s": prog.get("cpu_delta_s") if fresh_sample else None,
                     "stuck": bool(idle_s is not None
                                   and idle_s > config.DRIVER_IDLE_TIMEOUT * 0.5),
                     "last_event_s": round(max(0.0, now - last_ts), 1),
                     "stalled": bool(last.get("type") == "driver.stalled"
                                     or (idle_s is not None and idle_s > 300)),
                     "transcript": transcript})
    kimi = _kimi_code_usage(now, _fleet_task_names(store))
    rows.extend(kimi["inflight"])
    rows.sort(key=lambda r: -r["elapsed_s"])
    return rows, kimi


def _window_kimi_models(turns, cutoff):
    """Per-model kimi-code rows restricted to turns at/after `cutoff`.

    `_kimi_code_usage` returns the all-time `models` plus a per-turn `turns`
    log; `_usage` re-sums only the turns inside the window so a narrow range
    shows less traffic and `last_ts` reflects the last event WITHIN the range,
    not overall.
    """
    agg = {}
    for ts, real, req_delta, prompt_delta, completion_delta, ok_delta in turns:
        if ts is None or ts < cutoff:
            continue
        row = agg.setdefault(real, {"model": real, "pretty": _pretty(real),
                                    "family": "kimi-code", "source": "kimi-code",
                                    "requests": 0, "ok": 0, "errors": 0,
                                    "failed_attempts": 0, "tokens": 0,
                                    "prompt_tokens": 0, "completion_tokens": 0,
                                    "avg_latency_ms": None, "last_ts": None})
        row["requests"] += req_delta
        row["ok"] += ok_delta
        row["prompt_tokens"] += prompt_delta
        row["completion_tokens"] += completion_delta
        if row["last_ts"] is None or ts > row["last_ts"]:
            row["last_ts"] = ts
    models = list(agg.values())
    for m in models:
        m["tokens"] = m["prompt_tokens"] + m["completion_tokens"]
    return models


def _usage(store=None, range_key=None, include_series=False, window=None, with_points=False):
    """Usage aggregates for /api/usage, honoring the requested range.

    `range_key` selects the aggregation window: 1h/3h/6h/today/24h/7d/all. A
    missing or unrecognized key (including None) falls back to "1h" — the usage
    page's default. "today" is a CALENDAR day, not a rolling 24 hours: at 09:00
    a rolling day is mostly yesterday, and "what has the fleet done today" is
    the question an operator actually asks. "all" is the historical view: nothing is dropped. A windowed range
    bounds totals, per-model/family rows, and the series points, while the
    in-flight list is never trimmed — a live agent is current by definition.

    `window=(start, end)` pins the cut to an explicit half-open epoch range
    instead of `range_key` — the hook /api/usage/hourly uses so an arbitrary
    calendar day re-cuts THE SAME walk rather than filtering events itself.
    `with_points=True` additionally returns the raw walk points as `points`,
    which is what the hourly bucketer reads.

    Per-model `cost` prices prompt/completion tokens at each model's rate via
    `config.cost_of`. Where an event reports a token count but no prompt/
    completion breakdown, the excess is priced at the completion rate, so the
    figure is an UPPER bound rather than an under-count.
    """
    now = time.time()
    range_key = range_key if range_key in RANGES else "1h"
    cutoff = window[0] if window else _range_cutoff(range_key, now)
    win_end = window[1] if window else None

    def new_model(model, family, source):
        return {"model": model, "pretty": _pretty(model), "family": family, "source": source,
                "requests": 0, "ok": 0, "errors": 0, "failed_attempts": 0, "tokens": 0,
                "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0,
                "latency_total_ms": 0, "avg_latency_ms": None, "last_ts": None}

    def new_family(family):
        try:
            limit = config.family_limit(family)
        except Exception:
            limit = None
        return {"family": family, "limit": limit, "requests": 0, "ok": 0, "errors": 0,
                "failed_attempts": 0, "tokens": 0, "inflight": 0}

    by_model = {}
    by_family = {f: new_family(f) for f in config.FAMILY_ORDER}
    totals = {"requests": 0, "ok": 0, "errors": 0, "failed_attempts": 0, "tokens": 0,
              "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
    # (ts, family, model, requests, ok, errors, failed_attempts, tokens,
    # task_runs). The hourly view (/api/usage/hourly) buckets exactly these
    # points, so a counter added to the event walk reaches BOTH cuts — the
    # alternative, an hourly walk with its own filter, reports different
    # numbers for the same day and nobody can tell which of the two is wrong.
    pts = []
    done_tok_keys = set()

    def in_window(ts):
        """Is this event inside the requested cut? — the ONE window test.

        Every filter below (the event walk, the opencode backfill, the kimi
        turns) goes through it, so the daily row and the hourly buckets for one
        date can never disagree about which events count. A missing ts is
        outside every bounded window: it cannot be placed in a bucket.
        """
        if ts is None:
            return False
        if cutoff is not None and ts < cutoff:
            return False
        if win_end is not None and ts >= win_end:
            return False
        return True

    # models/inflight/points for kimi-code sessions up front (cached, shared with inflight)
    inflight, kimi = _collect_inflight(now, store)

    ev_lines = _load_event_lines()
    lines = ev_lines
    if window is not None:
        # An explicit window scans from a seek point just before the day, and
        # the in-loop `win_end` check bounds the end. The seek alone is not
        # enough: events are append-ordered but not timestamp-ordered (a
        # killed run can write a stale ts later), and a day cut must not
        # inherit the neighbours' traffic.
        lines = ev_lines[_first_event_at_or_after(ev_lines, cutoff - 86400):]
    elif cutoff is not None:
        lines = ev_lines[_first_event_at_or_after(ev_lines, cutoff):]
    for line in lines:
        try:
            e = json.loads(line)
        except Exception:
            continue
        etype = e.get("type")
        if win_end is not None and not in_window(_ts(e.get("ts"))):
            # An explicit day window is exclusive at the end: an event at
            # 00:00:00 tomorrow belongs to tomorrow's buckets, not tonight's.
            continue
        if etype in ("driver.start", "driver.done", "driver.error"):
            if etype == "driver.start":
                continue
            model = e.get("model") or "unknown"
            family = config.MODEL_FAMILY.get(model, "harness")
            source = f"driver:{e.get('harness') or 'unknown'}"
            mod = by_model.setdefault((family, model, source), new_model(model, family, source))
            mod["harness"] = e.get("harness")
            fam = by_family.setdefault(family, new_family(family))
            if etype == "driver.error":
                mod["failed_attempts"] += 1
                fam["failed_attempts"] += 1
                totals["failed_attempts"] += 1
                # A failed driver attempt produced no series point, so the code
                # fleet's failures were invisible on the timeline: an hour of
                # capacity rejections rendered as a quiet hour rather than a bad
                # one. Capacity is tracked separately because it is the
                # provider refusing, not the work being wrong.
                ts = _ts(e.get("ts"))
                if ts:
                    pts.append((ts, family, model, 0, 0, 0, 1, 0, 0))
                continue
            mod["requests"] += 1
            mod["ok"] += 1
            fam["requests"] += 1
            fam["ok"] += 1
            totals["requests"] += 1
            totals["ok"] += 1
            ts = _ts(e.get("ts"))
            mod["last_ts"] = ts
            seconds = e.get("seconds")
            if isinstance(seconds, (int, float)):
                mod["latency_total_ms"] += round(seconds * 1000)
            toks = e.get("tokens") or 0
            if toks:
                base, xnum = _xkey(e.get("task"))
                done_tok_keys.add((e.get("harness"), model, base, xnum))
                ptoks = e.get("prompt_tokens") or 0
                ctoks = e.get("completion_tokens") or 0
                mod["tokens"] += toks
                mod["prompt_tokens"] += ptoks
                mod["completion_tokens"] += ctoks
                fam["tokens"] += toks
                totals["tokens"] += toks
                totals["prompt_tokens"] += ptoks
                totals["completion_tokens"] += ctoks
            if ts:
                pts.append((ts, family, model, 1, 1, 0, 0, toks, 1))
            continue
        if etype not in ("request", "request_start", "request_end"):
            continue
        if etype == "request_start":
            continue
        model = e.get("model") or "unknown"
        family = e.get("family") or "unknown"
        if model == "dry-run" or family == "dry-run":
            continue
        mod = by_model.setdefault((family, model, "arc-pool"), new_model(model, family, "arc-pool"))
        fam = by_family.setdefault(family, new_family(family))
        if etype == "request_end":
            mod["failed_attempts"] += 1
            fam["failed_attempts"] += 1
            totals["failed_attempts"] += 1
            continue
        mod["requests"] += 1
        fam["requests"] += 1
        totals["requests"] += 1
        mod["last_ts"] = e.get("ts")
        ts = _ts(e.get("ts"))
        if e.get("ok"):
            mod["ok"] += 1
            fam["ok"] += 1
            totals["ok"] += 1
            tokens = e.get("tokens") or 0
            mod["tokens"] += tokens
            fam["tokens"] += tokens
            totals["tokens"] += tokens
            for field in ("prompt_tokens", "completion_tokens"):
                val = e.get(field) or 0
                mod[field] += val
                totals[field] += val
            lat = e.get("latency_ms")
            if isinstance(lat, (int, float)):
                mod["latency_total_ms"] += lat
            if ts:
                pts.append((ts, family, model, 1, 1, 0, 0, tokens, 0))
        else:
            mod["errors"] += 1
            fam["errors"] += 1
            totals["errors"] += 1
            # A failed request produced NO point at all, so the timeline showed
            # traffic dipping during an outage rather than errors spiking — the
            # shape that makes a bad hour look like a quiet one.
            if ts:
                pts.append((ts, family, model, 1, 0, 1, 0, 0, 0))

    # Backfill opencode tokens from transcripts for pre-plumbing runs.
    for ts, model, toks, ptoks, ctoks in _opencode_token_backfill(store, done_tok_keys):
        if not in_window(ts):
            continue
        family = config.MODEL_FAMILY.get(model, "harness")
        mod = by_model.setdefault((family, model, "driver:opencode"),
                                  new_model(model, family, "driver:opencode"))
        mod["harness"] = "opencode"
        fam = by_family.setdefault(family, new_family(family))
        mod["tokens"] += toks
        mod["prompt_tokens"] += ptoks
        mod["completion_tokens"] += ctoks
        fam["tokens"] += toks
        totals["tokens"] += toks
        totals["prompt_tokens"] += ptoks
        totals["completion_tokens"] += ctoks
        if mod["last_ts"] is None or ts > mod["last_ts"]:
            mod["last_ts"] = ts
        pts.append((ts, family, model, 0, 0, 0, 0, toks, 0))

    # Merge kimi-code CLI sessions so the dashboard also shows interactive traffic,
    # which goes straight to llm-api.arc.vt.edu and never touches the event log.
    #
    # The per-TURN log is the source, not the coarser `points`: it carries the
    # model and every counter separately, and it is what the daily per-model
    # rows are summed from (_window_kimi_models). kimi credits `ok` from a
    # different wire event (usage.record) than `requests` (llm.request), so a
    # stream that knows only requests/tokens cannot express it — deriving
    # ok = requests there made the hourly buckets disagree with the daily row
    # for the same day, the exact drift this endpoint exists to avoid.
    for t, real, rd, pd, cd, ok_delta in kimi.get("turns", []):
        if not in_window(t):
            continue
        pts.append((t, "kimi-code", real, rd, ok_delta, 0, 0, pd + cd, 0))
    kimi_models = kimi["models"] if cutoff is None else _window_kimi_models(kimi.get("turns", []), cutoff)
    if kimi_models:
        fam = new_family("kimi-code")
        for row in kimi_models:
            fam["requests"] += row["requests"]
            fam["ok"] += row["ok"]
            fam["tokens"] += row["tokens"]
            totals["requests"] += row["requests"]
            totals["ok"] += row["ok"]
            totals["tokens"] += row["tokens"]
            totals["prompt_tokens"] += row["prompt_tokens"]
            totals["completion_tokens"] += row["completion_tokens"]
        by_family["kimi-code"] = fam
        by_model.update({(r["family"], r["model"], r["source"]): r for r in kimi_models})

    for row in inflight:
        fam = by_family.get(row["family"])
        if fam is not None:
            fam["inflight"] += 1

    # Over-cap detection. A family's headless drivers and any interactive
    # sessions of the same ARC account share ONE cap, so where two family
    # keys bill the same account their combined in-flight count is what must
    # be checked. Empty today (kimi + kimi-code retired 2026-09-12, and the
    # live families do not share an account); kept as the seam for one that
    # does.
    shared = {}
    for fkey, fam in by_family.items():
        parts = shared.get(fkey, (fkey,))
        eff = sum(by_family.get(p, {}).get("inflight", 0) for p in parts)
        fam["inflight_shared"] = eff
        lim = fam.get("limit")
        fam["at_cap"] = bool(lim and eff >= lim)
        fam["over_cap"] = bool(lim and eff > lim)
        if fam["over_cap"]:
            if fkey not in _over_emitted:
                _over_emitted.add(fkey)
                _emit_event("inflight.over_cap", family=fkey, inflight=eff, limit=lim,
                            shared_with=list(parts[1:]),
                            note="in-flight above per-account ARC cap — killed runs left phantom rows or a real breach")
        elif not fam["at_cap"]:
            _over_emitted.discard(fkey)

    # Recent driver-level problems for the dashboard error panel (newest last).
    recent_driver = []
    _DRV_TYPES = ("driver.error", "driver.stalled", "driver.timeout", "driver.stale",
                  "driver.cancelled", "inflight.over_cap", "request.stale",
                  "task.failed")
    for line in reversed(ev_lines):
        if '"driver.' not in line and '"task.failed"' not in line and '"inflight.' not in line \
                and '"request.stale"' not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") not in _DRV_TYPES:
            continue
        recent_driver.append({k: e[k] for k in
                              ("type", "ts", "harness", "model", "role", "task", "attempt",
                               "error", "note", "family", "inflight", "limit", "idle_s",
                               "age_s", "session_id")
                              if k in e})
        if len(recent_driver) >= 40:
            break
    recent_driver.reverse()

    start, bucket, n = _series_window(range_key, now, min((p[0] for p in pts), default=None))
    series = None
    if include_series:
        series = {f: [{"t": start + i * bucket, "requests": 0, "tokens": 0,
                       "errors": 0} for i in range(n)]
                  for f in config.FAMILY_ORDER}
        for ts, family, _model, req, _ok, err, failed, tok, _tr in pts:
            if not ts or ts < start:
                continue
            idx = int((ts - start) // bucket)
            if idx >= n:
                continue
            pts_list = series.setdefault(family, [{"t": start + i * bucket, "requests": 0,
                                                   "tokens": 0, "errors": 0}
                                                  for i in range(n)])
            pts_list[idx]["requests"] += req
            pts_list[idx]["tokens"] += tok
            # The timeline's "errors" band is a badness counter, not the totals'
            # request-error counter: the original walk emitted a point for BOTH
            # a failed request and a crashed driver attempt, and an hour of
            # capacity rejections must still read as a bad hour. Same semantics,
            # now summed from the two explicit deltas instead of one conflated
            # one.
            pts_list[idx]["errors"] += err + failed

    # The 30-day breakdown is a list of LOCAL calendar days. It used to bucket
    # by UTC day (ts // 86400) while LABELLING the bucket with local time, so
    # the row called "09-13" actually held local 09-12 20:00-24:00 — four hours
    # of traffic sitting under the wrong date for anyone east or west of UTC,
    # and the reason /api/usage/hourly could not agree with it. Bucketing
    # through _day_window keeps a label and its contents the same day, the same
    # definition range=today uses.
    today0 = _today_str()
    daily = []
    day_idx = {}
    for ds in _last_dates(30, today0):
        start, _end = _day_window(ds)
        rec = {"date": ds,
               "requests": 0, "tokens": 0, "task_runs": 0, "families": {}}
        day_idx[start] = rec
        daily.append(rec)
    for ts, family, _model, req, _ok, _err, _failed, tok, tr in pts:
        if not ts:
            continue
        rec = day_idx.get(_day_window(_ds(ts))[0])
        if rec is None:
            continue
        rec["requests"] += req
        rec["tokens"] += tok
        rec["task_runs"] += tr
        f = rec["families"].setdefault(family, {"requests": 0, "tokens": 0, "task_runs": 0})
        f["requests"] += req
        f["tokens"] += tok
        f["task_runs"] += tr

    models = []
    for mod in by_model.values():
        lat = mod.pop("latency_total_ms", 0) or 0
        if mod["ok"] and lat:
            mod["avg_latency_ms"] = round(lat / mod["ok"])
        # Price the split prompt/completion at their own rates, then price any
        # excess `tokens` a split cannot account for (an event that reported a
        # token count but no prompt/completion breakdown) at the completion
        # rate — an upper bound, same guidance as _projects and config.cost_of.
        # Without it the figure is a LOWER bound, which is not honest.
        mod["cost"] = round(config.cost_of(mod["model"], mod["prompt_tokens"],
                                           mod["completion_tokens"])
                            + config.cost_of(mod["model"], 0,
                                             max(0, mod["tokens"]
                                                 - mod["prompt_tokens"]
                                                 - mod["completion_tokens"])), 4)
        models.append(mod)
    models.sort(key=lambda m: -m["requests"])
    totals["cost"] = round(sum(m["cost"] for m in models), 4)

    families = [by_family[f] for f in config.FAMILY_ORDER]
    families += [v for k, v in sorted(by_family.items()) if k not in config.FAMILY_ORDER]

    res = {"now": now, "range": range_key, "bucket_secs": bucket, "daily": daily,
           "models": models, "families": families, "inflight": inflight,
           "recent_driver_events": recent_driver,
           "totals": totals}
    if include_series:
        res["series"] = series
    if with_points:
        res["points"] = pts
    return res


def _ds(ts):
    """Local calendar date (YYYY-MM-DD) of an epoch timestamp."""
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _today_str():
    return _ds(time.time())


def _last_dates(n, today_s=None):
    """The `n` calendar dates ending today, oldest first — as date STRINGS.

    Stepping by CALENDAR date rather than subtracting 86400s from an epoch.
    A fixed-size day drifts across a DST change: `_ds(today0 - k*86400)`
    re-derives the SAME local date twice and a whole calendar date is missing
    from the list (verified with TZ=America/New_York, today=2026-03-09: no
    2026-03-08 row). Every point dated that missing day then finds no row in
    the daily index and is skipped, so a full day of traffic disappears from
    the daily view for the 30 days that follow each spring-forward.
    date+timedelta has no such hole.
    """
    end = _date.fromisoformat(today_s or _today_str())
    return [(end - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]


def _day_points(store, date_s):
    """The event-walk points of ONE calendar day — the shared collection.

    Walks the SAME aggregation the daily view uses (`_usage`, with the window
    pinned to the day) and returns its points plus the window bounds. `_usage`
    already restricted them to [start, end) through its own `in_window`, so
    there is no second filter here to drift from it — and a second filter is
    exactly how the two cuts of one day would come to disagree.
    """
    start, end = _day_window(date_s)
    usage = _usage(store, "all", window=(start, end), with_points=True)
    return usage.get("points") or [], start, end


def _usage_hourly(store, date_s):
    """Usage for one calendar date, cut into 24 one-hour buckets.

    Shape: {date, hours: [{hour, by_model, totals}] × 24, totals}. Every hour
    is present even with no traffic — a missing bar and a zero bar look
    different to an operator scanning a chart, and "nothing happened at 04:00"
    is the answer they came for. `totals` sums the day, so it agrees with the
    daily view's row for the same date by construction (same points, same
    filter). Raises ValueError for a malformed or impossible date.

    Hour h is the local wall-clock hour h of `date`, so its label matches what
    it holds on every day of the year: the bucket is chosen by reading the ts's
    own local hour, not by `start + h*3600`. Fixed-width arithmetic assumes a
    86400 s day, so on a transition day it mislabels every hour after the
    change by one and has nowhere to put the 25th hour of a fall-back day —
    those points were dropped and the day's totals silently disagreed with the
    daily view beside them.
    """
    # Strictly YYYY-MM-DD. _date.fromisoformat alone is too permissive in
    # 3.11+ (it takes "20260910" and "2026-9-1"), and a date the route
    # silently reinterpreted would show a day the operator never asked for.
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_s or ""):
        raise ValueError(f"date must be YYYY-MM-DD: {date_s!r}")
    try:
        day = _date.fromisoformat(date_s)
        day + timedelta(days=1)  # 9999-12-31 parses but has no next day
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"not a calendar date: {date_s} ({exc})")
    points, start, _end = _day_points(store, date_s)

    def local_hour(ts):
        """Bucket index of `ts` = its local hour, or None if another date.

        [start, end) is exactly the span between this date's midnight and the
        next one, so a ts inside it is on `date` and its local hour is a valid
        0..23 bucket even on a 23- or 25-hour day, where the 23rd hour repeats
        and falls in the same bucket twice.
        """
        lt = time.localtime(ts)
        if (lt.tm_year, lt.tm_mon, lt.tm_mday) != (day.year, day.month, day.day):
            return None
        return lt.tm_hour

    buckets, totals, _by_model = _bucket_points(points, start, 24, 3600,
                                                locate=local_hour)
    hours = []
    for i, b in enumerate(buckets):
        hours.append({"hour": i, "by_model": b["by_model"], "totals": b["totals"]})
    return {"date": date_s, "hours": hours, "totals": totals}


_fleet_cache = {"key": 0.0, "models": [], "totals": {}}  # refreshed at most every ~2s
FLEET_CACHE_S = 2.0


def _fleet(store):
    """All-history code-fleet totals for /api/fleet.

    Wraps _usage(store, "all") — no separate event walk — and merges its
    per-source rows into one row per model (driver:<harness>, arc-pool, and
    the historical kimi-code wire logs all feed the same fleet). account_cap/
    driver_cap come from
    config and are None for families/models it does not know. The computed
    aggregate is cached ~2s so rapid polling stays cheap.
    """
    now = time.time()
    if _fleet_cache["key"] and now - _fleet_cache["key"] <= FLEET_CACHE_S:
        return {"totals": _fleet_cache["totals"], "models": _fleet_cache["models"],
                "ranges": RANGES, "ts": now}
    usage = _usage(store, "all")
    fam_state = {f["family"]: f for f in usage["families"]}
    agg = {}
    for m in usage["models"]:
        row = agg.setdefault(m["model"], {
            "model": m["model"], "pretty": m["pretty"],
            "family": config.MODEL_FAMILY.get(m["model"], m["family"]),
            "requests": 0, "ok": 0, "errors": 0, "tokens": 0,
            "prompt_tokens": 0, "completion_tokens": 0,
            "lat_ms": 0.0, "lat_n": 0, "last_ts": None})
        for field in ("requests", "ok", "errors", "tokens",
                      "prompt_tokens", "completion_tokens"):
            row[field] += m.get(field) or 0
        lat, ok = m.get("avg_latency_ms"), m.get("ok") or 0
        if lat and ok:
            row["lat_ms"] += lat * ok
            row["lat_n"] += ok
        if m.get("last_ts") and (row["last_ts"] is None or m["last_ts"] > row["last_ts"]):
            row["last_ts"] = m["last_ts"]
    models = []
    for row in agg.values():
        try:
            account_cap = config.family_limit(row["family"])
        except Exception:
            account_cap = None
        try:
            driver_cap = config.driver_limit(row["model"])
        except Exception:
            driver_cap = None
        models.append({"model": row["model"], "pretty": row["pretty"],
                       "family": row["family"], "requests": row["requests"],
                       "ok": row["ok"], "errors": row["errors"], "tokens": row["tokens"],
                       "prompt_tokens": row["prompt_tokens"],
                       "completion_tokens": row["completion_tokens"],
                       "avg_latency_ms": round(row["lat_ms"] / row["lat_n"]) if row["lat_n"] else None,
                       "last_ts": row["last_ts"], "account_cap": account_cap,
                       "driver_cap": driver_cap,
                       "inflight": fam_state.get(row["family"], {}).get("inflight", 0),
                       "inflight_shared": fam_state.get(row["family"], {}).get("inflight_shared", 0),
                       "at_cap": fam_state.get(row["family"], {}).get("at_cap", False),
                       "over_cap": fam_state.get(row["family"], {}).get("over_cap", False)})
    models.sort(key=lambda m: -m["requests"])
    totals = {f: usage["totals"][f] for f in
              ("requests", "ok", "errors", "tokens", "prompt_tokens", "completion_tokens")}
    _fleet_cache.update(key=now, models=models, totals=totals)
    return {"totals": totals, "models": models, "ranges": RANGES, "ts": time.time()}


ROLE_LABEL = {"pr_reviewer": "PR review", "reviewer": "gate review",
              "implementer": "coding", "planner": "planning"}
# How long a wait may sit unrefreshed before it is presumed dead. driver.queued
# fires once, then cap_wait re-fires roughly once a minute, so a live wait is
# always younger than this unless its run process was killed.
WAIT_STALE_S = 900


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False
    return True


def _harness_of(model):
    """Which local harness runs this model, from the roster."""
    return config.MODEL_HARNESS.get(model, "opencode")


def _first_event_at_or_after(lines, ts):
    """Index of the first event whose ts >= `ts`, or len(lines) if none.

    Scans BACKWARD from the end rather than binary-searching: the log is
    append-ordered but individual lines can be malformed or missing a ts, which
    a bisect cannot step over. Callers want a recent cutoff, so the walk is
    short in practice and the answer is exact.
    """
    i = len(lines)
    for idx in range(len(lines) - 1, -1, -1):
        line = lines[idx]
        if '"ts"' not in line:
            continue
        try:
            t = json.loads(line).get("ts")
        except (ValueError, TypeError):
            continue
        if t is None:
            continue
        if t < ts:
            return i
        i = idx
    return i


def _errors(range_key="24h", limit=40):
    """Distinct DEFECTS for the triage panel, worst first.

    Not an error log — a flat list of occurrences answers "what happened",
    which is the question you can already answer by reading the feed. This
    answers "what should I fix", by collapsing every occurrence of one defect
    into a single row with its count, its span, the tasks it hit, and the
    traceback that was previously thrown away.
    """
    import errors as _errors_mod
    now = time.time()
    spans = {"1h": 3600, "24h": 86400, "7d": 604800, "all": None}
    secs = spans.get(range_key, 86400)
    since = 0 if secs is None else now - secs
    try:
        groups = _errors_mod.groups(since=since, limit=limit)
    except Exception as exc:
        return {"ready": False, "reason": str(exc)[:200], "groups": [],
                "ranges": list(spans), "range": range_key, "ts": now}
    # age_s / span_s / active come from errors.groups(): a defect seen once an
    # hour ago is cold, one seen 30 times in the last five minutes is on fire,
    # and that is a property of the group rather than of this rendering.
    return {"ready": True, "groups": groups, "total": sum(g["count"] for g in groups),
            "ranges": list(spans), "range": range_key, "ts": now}


def _queue(store):
    """Who is holding a model slot right now, and who is queued behind them.

    Two separate queues sit in front of every driver attempt and they fail in
    different ways, so they are reported separately rather than summed:

      process  — this run's own asyncio.Semaphore (driver.slot_wait)
      fleet    — the cross-process DB lease shared by every run (driver.cap_wait)

    Running comes from the lease table because that IS the definition of
    occupying a slot, and it carries a pid so a dead run cannot pin capacity in
    the UI. Waiting comes from the event log, pairing each driver.queued with
    its terminal event; anything whose pid is gone, or that has gone quiet for
    WAIT_STALE_S, is dropped rather than shown as a phantom queue.
    """
    now = time.time()
    running, waiting = [], []
    roles, leases = {}, []
    try:
        leases = list(store.driver_lease_rows() or [])
    except Exception:
        leases = []

    # Last event per attempt decides that attempt's state.
    state = {}
    for line in _load_event_lines()[-6000:]:
        if '"driver.' not in line:
            continue
        try:
            e = json.loads(line)
        except (ValueError, TypeError):
            continue
        kind = e.get("type", "")
        if not kind.startswith("driver."):
            continue
        key = (e.get("task"), e.get("model"), e.get("attempt"))
        if e.get("role"):
            roles[(e.get("task"), e.get("model"))] = e["role"]
        if kind in ("driver.queued", "driver.slot_wait", "driver.cap_wait"):
            state[key] = e
        elif kind.startswith("driver."):
            state.pop(key, None)  # start/done/error/timeout/cancelled all settle it

    for (task, model, _attempt), e in state.items():
        ts = _ts(e.get("ts")) or 0
        if now - ts > WAIT_STALE_S or not _pid_alive(e.get("pid")):
            continue
        kind = e.get("type")
        waiting.append({
            "task": task, "model": model, "pretty": _pretty(model),
            "role": e.get("role"), "role_label": ROLE_LABEL.get(e.get("role"), e.get("role")),
            "scope": e.get("scope") or ("fleet" if kind == "driver.cap_wait"
                                        else "process"),
            "harness": e.get("harness"),
            "seconds": round(now - ts) if ts else None,
            "in_use": e.get("in_use"), "cap": e.get("cap"),
        })

    blocked = {(w["task"], w["model"]) for w in waiting}
    harness_running = {}
    for r in leases:
        pid = r["pid"] if isinstance(r, dict) or hasattr(r, "keys") else None
        if not _pid_alive(pid):
            continue
        ts = _ts(r["acquired_at"]) or 0
        model = r["model"]
        # A harness lease is the SAME attempt as its model lease, held one
        # level out. Counting it as another running task would double every
        # row; it is capacity accounting, so it is reported as capacity.
        if model.startswith("harness:"):
            harness_running[model.split(":", 1)[1]] = \
                harness_running.get(model.split(":", 1)[1], 0) + 1
            continue
        # A task can hold its MODEL lease while still queued for the harness
        # lease nested inside it. The model lease makes it look running and the
        # harness wait makes it look queued, and it was reported as both. It is
        # not running until it holds every slot it needs, so the wait wins.
        if (r["task"], model) in blocked:
            continue
        role = roles.get((r["task"], model))
        running.append({
            "task": r["task"], "model": model, "pretty": _pretty(model),
            "role": role, "role_label": ROLE_LABEL.get(role, role or "working"),
            "pid": pid, "seconds": round(now - ts) if ts else None,
        })

    running.sort(key=lambda x: -(x["seconds"] or 0))
    waiting.sort(key=lambda x: -(x["seconds"] or 0))

    models = []
    for model in sorted(set(config.MODEL_FAMILY)
                        | {r["model"] for r in running}
                        | {w["model"] for w in waiting}):
        if model.startswith("harness:"):
            continue
        try:
            cap = config.driver_limit(model)
        except Exception:
            continue
        run_n = sum(1 for r in running if r["model"] == model)
        wait_n = sum(1 for w in waiting if w["model"] == model)
        models.append({
            "model": model, "pretty": _pretty(model), "cap": cap,
            "running": run_n, "waiting": wait_n, "free": max(0, cap - run_n),
            "reviewers_waiting": sum(1 for w in waiting if w["model"] == model
                                     and w["role"] == "pr_reviewer"),
        })
    models.sort(key=lambda m: (-(m["running"] + m["waiting"]), m["model"]))

    # The harness is a real ceiling and usually the BINDING one: opencode's
    # models can each be under their own cap while the single local opencode
    # process pool is saturated. Without this row that shows up as "everything
    # idle, nothing progressing".
    harnesses = []
    # From the ROSTER's live models, not a literal pair: the retired kimi
    # harness would otherwise keep a permanent 0/x card, and a harness added
    # by a ROSTER row (dsh, 2026-09-12) would be missing from the panel.
    # Busiest first (running + queued), then by name, so the binding harness is
    # the one at the top of the panel.
    _hs = {_harness_of(m) for m in config.MODEL_HARNESS}
    for h in sorted(_hs, key=lambda h: (
            -(harness_running.get(h, 0)
              + sum(1 for w in waiting if w.get("scope") == "harness"
                    and w.get("harness") == h)), h)):
        cap = config.harness_limit(h)
        run_n = harness_running.get(h, 0)
        wait_n = sum(1 for w in waiting if w.get("scope") == "harness"
                     and w.get("harness") == h)
        run_n = run_n or sum(1 for r in running
                             if _harness_of(r["model"]) == h)
        harnesses.append({"harness": h, "cap": cap, "running": run_n,
                          "waiting": wait_n, "free": max(0, cap - run_n)})

    return {"running": running, "waiting": waiting, "models": models,
            "harnesses": harnesses,
            "totals": {"running": len(running), "waiting": len(waiting),
                       "reviewers_waiting": sum(1 for w in waiting
                                                if w["role"] == "pr_reviewer"),
                       "capacity": sum(m["cap"] for m in models)},
            "ts": now}


def _task_slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower())[:40].strip("-")


def _valid_repo(p):
    """The repo path if it is an existing directory under config.REPO_ROOT.

    Resolved before the prefix check, so `<root>/../elsewhere` cannot walk
    out of the fence, and compared as a path, not a string prefix, so
    `<root>-evil` is not mistaken for something under `<root>`.
    """
    if not isinstance(p, str) or not p or not os.path.isabs(p):
        return None
    root = Path(config.REPO_ROOT).resolve()
    try:
        path = Path(p).resolve()
    except OSError:
        return None
    if path == root or not path.is_relative_to(root):
        return None
    return path if path.is_dir() else None


def _repo_problem(path, base="main"):
    """Why this directory cannot host a task run, or None if it can.

    Checked at CREATE time on purpose. Without it the project is written
    happily and the failure surfaces minutes later inside gitstore.alloc as
    "fatal: not in a git directory" or "fatal: invalid reference: main",
    by which point a worktree and a DB row already exist.
    """
    if not (path / ".git").exists():
        return (f"{path} is not a git repository — run `git init` in it and "
                f"make one commit on {base}")
    try:
        r = subprocess.run(["git", "-C", str(path), "rev-parse", "--verify", base],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"could not inspect {path}: {exc}"
    if r.returncode != 0:
        return (f"{path} has no '{base}' branch with any commits — tasks branch "
                f"from {base}; create it with `git commit` on {base}")
    return None


_PHASE_WORD = {"alloc": "preparing worktree", "implement": "writing code",
               "pr_review": "pull request under review",
               "pr_merge": "merging the pull request",
               "gate": "running the verify gate", "review": "under review",
               "escalate": "escalating to a stronger model",
               "publish": "committing and merging", "fail": "giving up"}


_kimi_task_tokens_cache = {"key": 0.0, "map": {}}


def _kimi_tokens_by_task():
    """{task-id: tokens} from kimi-code wire logs, cached ~30s.

    kimi's stream-json transcript carries NO usage, so every kimi task showed
    "0 tok" — indistinguishable from a task that had done nothing. The wire
    logs DO record it, and their directories are named wd_<task-id>_<hash>, so
    it can be attributed properly instead of shown as a dash. One task
    measured 688,407 tokens while the console reported zero.
    """
    now = time.time()
    if now - _kimi_task_tokens_cache["key"] < 30:
        return _kimi_task_tokens_cache["map"]
    out = {}
    root = Path.home() / ".kimi-code" / "sessions"
    try:
        paths = list(root.glob("*/*/agents/*/wire.jsonl")) if root.is_dir() else []
    except OSError:
        paths = []
    for path in paths:
        tid = _session_task(path)
        if not tid:
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        key = (st.st_size, st.st_mtime_ns)
        ent = _kimi_cache.get(str(path))
        if ent is None or ent["key"] != key:
            ent = {"key": key, "agg": _parse_kimi_wire(path)}
            _kimi_cache[str(path)] = ent
        ft = ent["agg"]["file_totals"]
        out[tid] = out.get(tid, 0) + (ft["prompt"] or 0) + (ft["completion"] or 0)
    _kimi_task_tokens_cache.update(key=now, map=out)
    return out


def _task_progress(ids):
    """Per running task: what step it is on, and whether it is actually moving.

    "Is this progressing?" was unanswerable from the console. The events to
    answer it already existed — node_start/node_end say which step, and
    driver.progress carries bytes/idle_s every 60s — but nothing joined them,
    so a task that had produced nothing for ten minutes looked exactly like
    one mid-edit.
    """
    want = {i for i in (ids or []) if i}
    if not want:
        return {}
    open_node, prog, prev_bytes = {}, {}, {}
    for line in _load_event_lines():
        if ('"node_' not in line and '"driver.progress"' not in line
                and '"driver.start"' not in line):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        t = e.get("type")
        if t in ("node_start", "node_end"):
            node = e.get("node") or ""
            step, _, tid = node.partition("_")
            if tid in want:
                if t == "node_start":
                    open_node[tid] = step
                elif open_node.get(tid) == step:
                    open_node.pop(tid, None)
        elif t in ("driver.progress", "driver.start"):
            base, _x = _xkey(e.get("task"))
            if base not in want:
                continue
            if t == "driver.start":
                prog[base] = {"attempt": e.get("attempt"), "bytes": 0,
                              "idle_s": 0, "elapsed_s": 0, "ts": _ts(e.get("ts"))}
                prev_bytes[base] = 0
                continue
            before = prog.get(base, {}).get("bytes", 0)
            prev_bytes[base] = before
            prog[base] = {"attempt": e.get("attempt"), "bytes": e.get("bytes") or 0,
                          "idle_s": e.get("idle_s"), "elapsed_s": e.get("elapsed_s"),
                          "cpu_delta_s": e.get("cpu_delta_s"), "ts": _ts(e.get("ts"))}
    # Why the last gate rejected the task. Without this a fix loop is
    # indistinguishable from progress: the console says "writing code" while
    # the same gate rejects the same work over and over.
    gate_fail = {}
    for line in _load_event_lines():
        if '"task.gate"' not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        tid = e.get("module") or e.get("task")
        if tid in want:
            gate_fail[tid] = bool(e.get("passed"))

    out = {}
    now = time.time()
    for tid, step in open_node.items():
        pr = prog.get(tid) or {}
        age = now - pr["ts"] if pr.get("ts") else None
        grew = pr.get("bytes", 0) > prev_bytes.get(tid, 0)
        idle = pr.get("idle_s")
        # "moving" is deliberately generous: the harness can legitimately go
        # quiet for minutes waiting on a queued request (measured p99 TTFT is
        # ~50s, tail to 309s), so silence alone is not stuck.
        moving = grew or (idle is not None and idle < config.DRIVER_IDLE_TIMEOUT / 2)
        out[tid] = {
            "step": step, "label": _PHASE_WORD.get(step, step),
            "attempt": pr.get("attempt"), "bytes": pr.get("bytes"),
            "idle_s": idle, "elapsed_s": pr.get("elapsed_s"),
            "stale_report_s": round(age) if age is not None else None,
            "moving": bool(moving),
            "last_gate_failed": gate_fail.get(tid) is False,
            "gate_log": (f"{tid}-x{pr.get('attempt') or 1}.log"
                         if gate_fail.get(tid) is False else None),
        }
    return out


def _when_label(w):
    """`task.when` as one short line for an edge label / tooltip."""
    try:
        import code_tasks
        loaded = code_tasks._load_when("x", w)
        return code_tasks.when_text(loaded) if loaded else ""
    except Exception:
        return f"{w.get('dep')}.{w.get('key')} ?"


def _project_phase(statuses, ids, run_pid):
    """One word for where a project stands: running | done | attention | new.

    (_projects turns `new` into `chained` when the taskfile declares `after`
    and its chain is not yet merged — it cannot start, so "never run" is
    the wrong shelf for it.)

    The operator's question is "what needs me?", and a status dict of five
    counters does not answer it. `attention` means finished executing with
    something unresolved — a failure or conflict that will not fix itself.
    """
    total = len(ids)
    if run_pid or statuses.get("running"):
        return "running"
    # in_review: the PR is open and waiting on reviewers. Not "running" (no
    # agent is burning a slot) and not "attention" (nothing is wrong yet).
    if statuses.get("in_review"):
        return "in_review"
    if statuses.get("failed") or statuses.get("conflict"):
        return "attention"
    # A skipped task (its `when` did not hold) is complete, not pending: a
    # router whose untaken branch counted as unfinished would never be done.
    if total and statuses.get("merged", 0) + statuses.get("skipped", 0) >= total:
        return "done"
    if not statuses:
        return "new"
    return "attention"


def _task_loop_stats(store, task_ids):
    """Fix-loop stats per base task id: implement-attempt max (harness_runs is
    authoritative), task.escalated/task.conflict event counts, newest reviewer
    verdict as {"pass", "n_issues"} (or None), and the newest gate/review
    outcome as last_bounce (or None) — what last sent this task back to
    code."""
    want = [i for i in (task_ids or []) if i]
    stats = {i: {"attempts": 0, "escalations": 0, "conflicts": 0,
                 "last_verdict": None, "last_bounce": None}
             for i in want}
    if not want:
        return stats
    try:
        hrows = store.harness_runs_all() if store else []
    except Exception:
        hrows = []
    verdict_seen = set()
    for row in hrows:  # newest first (ORDER BY id DESC)
        base = row.get("task_id")
        if base not in stats:
            continue
        s = stats[base]
        if row.get("role") == "implementer":
            try:
                s["attempts"] = max(s["attempts"], int(row.get("attempt") or 0))
            except (TypeError, ValueError):
                pass
        elif row.get("role") == "reviewer" and base not in verdict_seen and row.get("verdict"):
            try:
                v = json.loads(row["verdict"])
            except ValueError:
                continue
            if isinstance(v, dict) and "pass" in v:
                s["last_verdict"] = {"pass": bool(v.get("pass")),
                                     "n_issues": len(v.get("issues") or [])}
                verdict_seen.add(base)
    for ln in _load_event_lines():
        if ('"task.escalated"' not in ln and '"task.conflict"' not in ln
                and '"task.gate"' not in ln and '"task.reviewed"' not in ln):
            continue
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        base, _x = _xkey(e.get("task") or e.get("module"))
        if base not in stats:
            continue
        etype = e.get("type")
        if etype == "task.escalated":
            stats[base]["escalations"] += 1
        elif etype == "task.conflict":
            stats[base]["conflicts"] += 1
        elif etype == "task.gate":
            # Oldest-to-newest walk: last write wins, so this lands on the
            # outcome that most recently passed or bounced the task.
            tail = (e.get("tail") or "").strip()
            stats[base]["last_bounce"] = {
                "kind": "gate", "passed": bool(e.get("passed")),
                "attempt": e.get("attempt"),
                "reason": tail[-160:] or None,
                "n_issues": None, "ts": e.get("ts")}
        elif etype == "task.reviewed":
            stats[base]["last_bounce"] = {
                "kind": "review", "passed": bool(e.get("passed")),
                "attempt": None, "reason": None,
                "n_issues": e.get("n_issues"), "ts": e.get("ts")}
    return stats


def _chain_gate_state(fname, lines=None):
    """What the chain_wait gate of this taskfile last reported, from the event
    log: {'state': 'waiting'|'ready'|'blocked', 'ts', 'waited_s', 'reason'}
    or None when no run of it has reached the gate."""
    latest = None
    for line in reversed(lines if lines is not None else _load_event_lines()):
        if '"chain.' not in line or fname not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if Path(str(e.get("taskfile") or "")).name != fname:
            continue
        latest = e
        break
    if not latest:
        return None
    kind = latest.get("type", "")
    return {"state": {"chain.wait": "waiting", "chain.ready": "ready",
                      "chain.blocked": "blocked"}.get(kind, "waiting"),
            "ts": latest.get("ts"), "waited_s": latest.get("waited_s"),
            "reason": latest.get("reason") or
            (", ".join(f"{Path(k).name}: {', '.join(v)}"
                       for k, v in (latest.get("failed_tasks") or {}).items()) or None)}


def _project_chain(store, path, titles, dependents, ev_lines=None):
    """A project's place in the taskfile chain (Rule 9), for the page.

    `project.after` has been enforced by the runner since the chain gate
    landed, but the dashboard never read it: a chained project looked like
    any other "never run" project, and a run sitting in chain_wait looked
    like a run doing nothing. This is the same readiness the gate computes
    (`code_tasks.chain_status`), plus the reverse edges — which projects are
    waiting on THIS one — and what the gate last said in the event log.
    Returns None for a project with no chain in either direction.
    """
    import code_tasks
    after = code_tasks._read_after(path)
    blocks = sorted(dependents.get(str(path.resolve()), set()))
    if not after and not blocks:
        return None
    if after:
        try:
            st = code_tasks.chain_status(store, after)
        except Exception:
            st = {"ok": False, "waiting": list(after), "failed": {}, "deps": []}
    else:
        st = {"ok": True, "waiting": [], "failed": {}, "deps": []}
    deps = []
    for d in st.get("deps") or []:
        name = Path(d["taskfile"]).name
        deps.append({"file": name, "title": titles.get(d["taskfile"]) or name,
                     "exists": bool(d.get("readable")),
                     "n_tasks": d.get("n_tasks"), "merged": d.get("merged", 0),
                     "failed": d.get("failed") or [],
                     "state": ("failed" if d.get("failed") else
                               "waiting" if d["taskfile"] in st["waiting"] else "ready")})
    gate = _chain_gate_state(path.name, ev_lines)
    return {"after": [d["file"] for d in deps], "deps": deps, "ready": bool(st["ok"]),
            "blocks": [{"file": Path(k).name, "title": titles.get(k) or Path(k).name}
                       for k in blocks],
            "gate": gate}


def _chain_dag_nodes(chain, heads):
    """The chain gate drawn into a project's DAG: one dashed node per
    upstream taskfile, feeding every head task (a task with no in-file
    deps), exactly where `chain_wait` sits in the real graph."""
    if not chain or not chain.get("deps"):
        return [], []
    gate = chain.get("gate") or {}
    nodes, edges = [], []
    for d in chain["deps"]:
        status = {"failed": "failed", "waiting": "running" if gate.get("state") == "waiting" else "pending",
                  "ready": "merged"}[d["state"]]
        if gate.get("state") == "blocked" and d["state"] != "ready":
            status = "failed"
        nid = f"after:{d['file']}"
        nodes.append({"id": nid, "kind": "chain", "file": d["file"],
                      "title": f"after {d['title']}", "status": status,
                      "merged": d["merged"], "n_tasks": d["n_tasks"], "live": status == "running"})
        edges.extend({"src": nid, "dst": h, "kind": "chain"} for h in heads)
    return nodes, edges


def _projects(store):
    now = time.time()
    inflight, _kimi = _collect_inflight(now, store)
    live_tasks = set()
    live = {}  # base task id -> {"seconds": latest elapsed, "tokens": partial transcript tokens}
    for row in inflight:
        if not row.get("task"):
            continue
        base, _x = _xkey(row["task"])
        if not base:
            continue
        live_tasks.add(row["task"])
        live_tasks.add(base)
        ent = live.setdefault(base, {"seconds": 0.0, "tokens": 0})
        ent["seconds"] = max(ent["seconds"], row.get("elapsed_s") or 0.0)
        if row.get("transcript"):
            toks, _p, _c = _transcript_toks(Path(config.ROOT) / "logs" / "harness" / row["transcript"])
            ent["tokens"] = max(ent["tokens"], toks)
    try:
        rows_all = store.code_tasks_all() if store else []
    except Exception:
        rows_all = []
    # per-task tokens/seconds from driver.done events (historical, all sources)
    ev_stats = {}  # base task id -> {"tokens","seconds","runs"}
    ev_done_keys = set()  # (harness, model, role, base, xnum) already counted
    for ln in _load_event_lines():
        if '"driver.done"' not in ln:
            continue
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        base, xnum = _xkey(e.get("task"))
        if not base:
            continue
        ev_done_keys.add((e.get("harness"), e.get("model"), e.get("role"), base, xnum))
        s = ev_stats.setdefault(base, {"tokens": 0, "seconds": 0.0, "runs": 0,
                                       "prompt_tokens": 0, "completion_tokens": 0,
                                       "cost": 0.0})
        s["tokens"] += e.get("tokens") or 0
        s["prompt_tokens"] += e.get("prompt_tokens") or 0
        s["completion_tokens"] += e.get("completion_tokens") or 0
        # Price each event at ITS own model. Cross-family review and tier
        # escalation both mix models under one task id; pricing the whole
        # bucket at the taskfile's implementer mis-charges every reviewer run
        # (a cheap-model task reviewed by GLM would price GLM's expensive
        # tokens at the cheap rate, so the figure was not even an upper bound).
        s["cost"] += config.cost_of(e.get("model"), e.get("prompt_tokens") or 0,
                                    e.get("completion_tokens") or 0)
        s["seconds"] += e.get("seconds") or 0.0
        s["runs"] += 1
    # Supplement from harness_runs rows whose driver.done fell out of the event
    # log (the log is bounded; the DB keeps every run). Seconds always; tokens
    # via cached transcript parse (the retired kimi transcripts carry no usage
    # -> 0 there, but agent-time is complete either way).
    try:
        hrows = store.harness_runs_all() if store else []
    except Exception:
        hrows = []
    for row in hrows:
        base = row.get("task_id")
        if not base:
            continue
        key = (row.get("harness"), row.get("model"), row.get("role"), base, row.get("attempt"))
        if key in ev_done_keys:
            continue
        s = ev_stats.setdefault(base, {"tokens": 0, "seconds": 0.0, "runs": 0,
                                       "prompt_tokens": 0, "completion_tokens": 0,
                                       "cost": 0.0})
        s["seconds"] += row.get("seconds") or 0.0
        s["runs"] += 1
        toks, ptoks, ctoks = _transcript_toks(row.get("transcript"))
        s["tokens"] += toks
        s["prompt_tokens"] += ptoks
        s["completion_tokens"] += ctoks
        s["cost"] += config.cost_of(row.get("model"), ptoks, ctoks)
    import reconcile
    run_by_file = {}
    for r in reconcile.live_runs():
        if r.get("taskfile"):
            run_by_file[Path(r["taskfile"]).name] = r["pid"]
    try:
        archived = store.archived_projects() if store else {}
    except Exception:
        archived = {}
    out = []
    tdir = Path(config.TASKS_DIR)
    # Parse every task file first and collect ALL task ids, so the fix-loop
    # stats (a full harness_runs read plus a full event-log walk) are computed
    # once for the whole page instead of once per project file.
    parsed = []
    all_ids = []
    for f in sorted(tdir.glob("*.json")) if tdir.is_dir() else []:
        try:
            data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        proj = data.get("project") or {}
        tdefs = [t for t in (proj.get("tasks") or []) if isinstance(t, dict)]
        ids = [t.get("id") for t in tdefs if t.get("id")]
        parsed.append((f, proj, tdefs, ids))
        all_ids.extend(ids)
    all_loop_stats = _task_loop_stats(store, sorted(set(all_ids)))
    # Chains: who each taskfile waits on (`after`) and, reversed, who waits
    # on it — both are needed to draw a project's place in the chain.
    import code_tasks
    titles = {str(f.resolve()): (proj.get("title") or f.stem) for f, proj, _t, _i in parsed}
    dependents = {}
    for f, proj, _t, _i in parsed:
        for key in code_tasks._read_after(f):
            dependents.setdefault(key, set()).add(str(f.resolve()))
    ev_lines = _load_event_lines()
    kimi_tok = _kimi_tokens_by_task()
    for f, proj, tdefs, ids in parsed:
        idset = set(ids)
        rows = [r for r in rows_all if r.get("taskfile")
                and (r["taskfile"] == str(f) or r["taskfile"].endswith("/" + f.name))]
        statuses = {}
        orphan_rows = 0
        last = None
        per_task = {}
        for r in rows:
            if r.get("id") not in idset:
                # history for ids the taskfile no longer declares — real, but
                # not this project's current state; tally separately
                orphan_rows += 1
                continue
            statuses[r["status"]] = statuses.get(r["status"], 0) + 1
            per_task[r["id"]] = r.get("status") or "pending"
            for k in ("created_at", "finished_at"):
                v = r.get(k)
                if v and (last is None or v > last):
                    last = v
        nodes = []
        loop_stats = all_loop_stats
        for t in tdefs:
            tid = t.get("id")
            if not tid:
                continue
            ev = ev_stats.get(tid, {})
            lv = live.get(tid, {})
            ls = loop_stats.get(tid, {})
            node_model = t.get("model")
            ptok = ev.get("prompt_tokens", 0)
            ctok = ev.get("completion_tokens", 0)
            tot = max(ev.get("tokens", 0), kimi_tok.get(tid, 0))
            # Prompts/completions are already priced per-model above — each
            # driver.done (and each harness_runs row) carries its own model,
            # and cross-review plus tier escalation mix several under one task
            # id. Only the excess a split cannot account for — historical
            # kimi-wire tokens, which carry no prompt/completion split — is
            # priced here, at kimi_wire_model()'s completion rate, since it is
            # the retired kimi harness's wire log (NOT the node's declared
            # model: a node can name any model and still have kimi wire tokens
            # under it).
            extra = max(0, tot - (ptok + ctok))
            node_cost = ev.get("cost", 0.0)
            if extra:
                node_cost += config.cost_of(config.kimi_wire_model(), 0, extra)
            node = {"id": tid, "title": t.get("title") or tid,
                    "model": node_model, "reviewer": t.get("reviewer"),
                    "status": per_task.get(tid, "pending"),
                    "live": tid in live_tasks,
                    # kimi transcripts carry no usage; its wire logs do.
                    "tokens": tot,
                    "tokens_source": ("kimi-wire" if kimi_tok.get(tid, 0) > ev.get("tokens", 0)
                                      else "transcript"),
                    "seconds": round(ev.get("seconds", 0.0), 1),
                    "cost": round(node_cost, 4),
                    "live_tokens": lv.get("tokens", 0),
                    "live_seconds": round(lv.get("seconds", 0.0), 1),
                    "attempts": ls.get("attempts", 0),
                    "escalations": ls.get("escalations", 0),
                    "conflicts": ls.get("conflicts", 0),
                    "last_verdict": ls.get("last_verdict"),
                    "last_bounce": ls.get("last_bounce")}
            nodes.append(node)
        edges = []
        for t in tdefs:
            if not t.get("id"):
                continue
            w = t.get("when") if isinstance(t.get("when"), dict) else None
            for d in (t.get("deps") or t.get("depends") or []):
                if d not in ids:
                    continue
                e = {"src": d, "dst": t["id"]}
                if w and w.get("dep") == d:
                    # A conditional release: drawn dashed, labelled with the
                    # condition, like the pipeline's own when= edges.
                    e["conditional"] = True
                    e["when"] = _when_label(w)
                edges.append(e)
        for n in nodes:
            if n["attempts"] > 1 or n["escalations"] > 0:
                edges.append({"src": n["id"], "dst": n["id"], "kind": "fixloop",
                              "attempts": n["attempts"], "escalations": n["escalations"]})
        tok_total = sum(n["tokens"] for n in nodes)
        sec_total = round(sum(n["seconds"] for n in nodes), 1)
        live_tok = sum(n["live_tokens"] for n in nodes)
        live_sec = round(sum(n["live_seconds"] for n in nodes), 1)
        cost_total = round(sum(n["cost"] for n in nodes), 4)
        # live tokens have no prompt/completion split; price at the completion
        # rate (an upper bound) per node model, matching $/usage's guidance.
        live_cost = round(sum(config.cost_of(n["model"], 0, n["live_tokens"])
                              for n in nodes if n["live_tokens"]), 4)
        merged_n = sum(1 for n in nodes if n["status"] == "merged")
        errors = [{"id": r["id"], "status": r["status"], "error": r["error"]}
                  for r in rows if r.get("error") and r.get("id") in idset]
        try:
            mtime_iso = datetime.utcfromtimestamp(f.stat().st_mtime).isoformat() + "+00:00"
        except OSError:
            mtime_iso = None
        chain = _project_chain(store, f, titles, dependents, ev_lines)
        heads = [t["id"] for t in tdefs if t.get("id")
                 and not [d for d in (t.get("deps") or t.get("depends") or []) if d in ids]]
        cnodes, cedges = _chain_dag_nodes(chain, heads)
        phase = _project_phase(statuses, ids, run_by_file.get(f.name))
        if phase == "new" and chain and not chain["ready"]:
            phase = "chained"
        out.append({"file": f.name, "title": proj.get("title") or f.stem,
                    "chain": chain,
                    "repo": proj.get("repo"), "n_tasks": len(tdefs), "task_ids": ids,
                    "models": sorted({t.get("model") for t in tdefs if t.get("model")}),
                    "reviewers": sorted({t.get("reviewer") for t in tdefs if t.get("reviewer")}),
                    "statuses": statuses,
                    "orphan_rows": orphan_rows,
                    "dag": {"nodes": cnodes + nodes, "edges": cedges + edges},
                    "progress": {"done": merged_n, "total": len(ids)},
                    "tokens": tok_total + live_tok, "seconds": round(sec_total + live_sec, 1),
                    "done_tokens": tok_total, "done_seconds": sec_total,
                    "live_tokens": live_tok, "live_seconds": live_sec,
                    "cost": round(cost_total + live_cost, 4),
                    "done_cost": cost_total, "live_cost": live_cost,
                    "errors": errors,
                    "archived": str(f) in archived,
                    "archived_at": archived.get(str(f)),
                    "phase": phase,
                    # NOT "progress": that key already means {done, total}.
                    "task_progress": (_task_progress(ids)
                                      if (statuses.get("running")
                                          or run_by_file.get(f.name)) else {}),
                    "run_pid": run_by_file.get(f.name),
                    "active": statuses.get("running", 0) > 0 or any(i in live_tasks for i in ids),
                    "last_activity": last or mtime_iso})
    out.sort(key=lambda p: p["last_activity"] or "", reverse=True)
    return out


def _git_block(repo, gh=None):
    info = {"repo": str(repo), "branch": None, "log": [], "worktrees": [], "dirty": []}
    if not repo or not repo.is_dir():
        info["error"] = "repo not found on disk"
        return info

    def git(*args):
        try:
            r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                               text=True, timeout=5)
            return r.stdout if r.returncode == 0 else ""
        except Exception:
            return ""
    info["branch"] = git("branch", "--show-current").strip() or None
    info["log"] = [l for l in git("log", "--oneline", "-6").splitlines() if l]
    info["worktrees"] = [l.strip() for l in git("worktree", "list").splitlines() if l.strip()]
    info["dirty"] = [l for l in git("status", "--short").splitlines() if l]
    if isinstance(gh, dict):
        for k in ("remote", "gh_installed", "gh_authed", "ready", "reason"):
            info[k] = gh.get(k)
    return info


def _transcript_span(fpath):
    """(birth epoch, seconds) from a transcript's own event timestamps.

    stat times lie for streamed transcripts that rewrite on flush; the first
    and last event timestamps are the attempt's real start and length. Falls
    back to mtime/None for empty or unreadable files.
    """
    try:
        raw = fpath.read_bytes()
        if not raw:
            raise ValueError("empty")
        first = raw.split(b"\n", 1)[0]
        last = raw.rstrip().rsplit(b"\n", 1)[-1]
        t0 = json.loads(first).get("timestamp")
        t1 = json.loads(last).get("timestamp")
        if not isinstance(t0, (int, float)) or not isinstance(t1, (int, float)):
            raise ValueError("no timestamps")
        return t0 / 1000.0, max(0.0, round((t1 - t0) / 1000.0))
    except Exception:
        try:
            return fpath.stat().st_mtime, None
        except OSError:
            return time.time(), None


def _live_transcript_rows(task_ids, finished, task_rows):
    """Synthetic harness-run rows for transcripts on disk with no DB row.

    Rule 7 streams transcripts live, but the harness_runs row is written only
    when the driver call RETURNS (code_tasks.save_harness_run); a killed run
    process or a cancelled graph leaves a transcript with no row, and the
    project view reported "no runs yet" for a task that had run many times
    (empty-diff-publish, 2026-09-14). The file on disk is the fact: merge in
    what the DB never recorded, flagged live while it is still being written.
    """
    try:
        hdir = Path(config.ROOT, "logs", "harness")
        live_s = config.DRIVER_IDLE_TIMEOUT + 60
        now = time.time()
        have = {Path(r.get("transcript") or "").name for r in finished}
        task_row = {r.get("id"): r for r in task_rows if r.get("id")}
        synth = []
        for tid in task_ids:
            if not tid:
                continue
            pat = re.compile(re.escape(tid)
                             + r"-(?:x\d+|pr\d+)-([a-z_]+)-(\d+)\.jsonl")
            for f in sorted(hdir.glob(f"{tid}-*.jsonl")):
                if f.name in have:
                    continue
                m = pat.fullmatch(f.name)
                if not m:
                    continue
                live = False
                try:
                    live = (now - f.stat().st_mtime) <= live_s
                except OSError:
                    continue
                # Birth/length come from the stream's own event timestamps —
                # a driver that rewrites the file on flush makes stat's ctime
                # worthless (it made a 90-minute attempt look 7 seconds old).
                birth, span = _transcript_span(f)
                role = m.group(1)
                trow = task_row.get(tid) or {}
                model = trow.get("model")
                if role != "implementer":
                    model = config.REVIEW_FAMILIES.get(
                        trow.get("reviewer"), model or trow.get("reviewer"))
                synth.append({
                    "task_id": tid, "harness": config.MODEL_HARNESS.get(model),
                    "model": model, "role": role, "attempt": int(m.group(2)),
                    "exit_code": None, "seconds": span,
                    "verdict": None, "transcript": f.name,
                    "created_at": datetime.fromtimestamp(birth).isoformat(),
                    "live": live})

        def ts(r):
            try:
                return datetime.fromisoformat(r.get("created_at") or "").timestamp()
            except (TypeError, ValueError):
                return 0.0

        return sorted(list(finished) + synth, key=ts, reverse=True)
    except Exception:
        return finished


def _project_detail(store, fname):
    if not re.fullmatch(r"[\w.-]+\.json", fname or ""):
        return {"error": "bad file name"}, 400
    path = Path(config.TASKS_DIR) / fname
    if not path.is_file():
        return {"error": "not found"}, 404
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        return {"error": f"invalid JSON: {exc}"}, 400
    proj = data.get("project") or {}
    tdefs = [t for t in (proj.get("tasks") or []) if isinstance(t, dict)]
    ids = [t.get("id") for t in tdefs if t.get("id")]
    idset = set(ids)
    try:
        rows_all = store.code_tasks_all() if store else []
        rows = [r for r in rows_all if r.get("taskfile")
                and (r["taskfile"] == str(path) or r["taskfile"].endswith("/" + fname))]
        runs = store.harness_runs_for(ids) if store else []
    except Exception:
        rows, runs = [], []
    runs = _live_transcript_rows(ids, runs, rows)
    loop_stats = _task_loop_stats(store, ids)
    for r in rows:
        ls = loop_stats.get(r.get("id"))
        if ls:
            r.update(ls)
    lines = _load_event_lines()
    events = []
    for line in reversed(lines):
        hit = next((i for i in ids if i and i in line), None)
        if hit:
            try:
                events.append(json.loads(line))
            except Exception:
                pass
            if len(events) >= 80:
                break
    events.reverse()
    repo_v = _valid_repo(proj.get("repo") or "")
    try:
        gh = asyncio.run(gitstore.github_status(repo_v)) if repo_v else None
    except Exception:
        gh = None
    import reconcile
    run_pid = next((r["pid"] for r in reconcile.live_runs()
                    if r.get("taskfile") and Path(r["taskfile"]).name == fname), None)
    import code_tasks
    titles, dependents = {}, {}
    tdir = Path(config.TASKS_DIR)
    for g in sorted(tdir.glob("*.json")) if tdir.is_dir() else []:
        try:
            gp = (json.loads(g.read_text(encoding="utf-8", errors="replace"))
                  .get("project") or {})
        except Exception:
            continue
        titles[str(g.resolve())] = gp.get("title") or g.stem
        for key in code_tasks._read_after(g):
            dependents.setdefault(key, set()).add(str(g.resolve()))
    chain = _project_chain(store, path, titles, dependents, lines)
    return {"file": fname, "title": proj.get("title") or path.stem,
            "repo": proj.get("repo"), "run_pid": run_pid,
            "chain": chain,
            "task_progress": _task_progress(ids),
            "tasks": tdefs, "rows": rows, "runs": runs, "events": events,
            "git": _git_block(repo_v, gh)}, 200


def _recent_agent_runs(store, limit=40):
    """Finished harness runs, newest first — the other half of an agents view.

    /api/agents only ever showed what is in flight, so the moment an agent
    finished it vanished with no trace of whether it succeeded. An operator
    asking "did that review pass?" had nowhere to look.
    """
    try:
        rows = store.harness_runs_all(limit=limit * 3) if store else []
    except Exception:
        return []
    out = []
    for r in rows[:limit]:
        verdict = None
        if r.get("verdict"):
            try:
                v = json.loads(r["verdict"])
                if isinstance(v, dict) and "pass" in v:
                    verdict = {"pass": bool(v["pass"]),
                               "issues": len(v.get("issues") or [])}
            except ValueError:
                pass
        ts = None
        try:
            ts = datetime.fromisoformat(r["created_at"]).timestamp()
        except (TypeError, ValueError):
            pass
        out.append({
            "task": r.get("task_id"), "model": r.get("model"),
            "pretty": _pretty(r.get("model")), "harness": r.get("harness"),
            "role": r.get("role"), "attempt": r.get("attempt"),
            "exit_code": r.get("exit_code"),
            "ok": r.get("exit_code") == 0,
            "seconds": r.get("seconds"), "verdict": verdict,
            "transcript": (Path(r["transcript"]).name
                           if r.get("transcript") else None),
            "ts": ts,
        })
    return out


def _studio_approve(body):
    """Record the operator's sign-off on a workbench asset.

    Rule 6b discipline: this writes ONE thing — an approvals entry in a known
    studio project's own directory — and only for an asset that project has
    actually measured (a mesh report exists for it). No path, no free-form
    target, nothing the request can point somewhere else.
    """
    from studio import approvals
    from studio import status as studio_status
    project = str((body or {}).get("project") or "")
    asset = str((body or {}).get("asset") or "")
    state = str((body or {}).get("state") or "")
    if project not in studio_status.projects():
        return {"error": "unknown studio project"}, 404
    if state not in approvals.STATES:
        return {"error": f"state must be one of {list(approvals.STATES)}"}, 400
    known = set()
    for r in config.studio_run_dir(project).glob("mesh-*.json"):
        try:
            known.add(Path(json.loads(r.read_text()).get("model_path", r.stem)).name)
        except (OSError, ValueError):
            continue
    if asset not in known:
        return {"error": "no measured asset by that name in this project"}, 404
    note = str((body or {}).get("note") or "")[:2000]
    if state == "rejected" and not note.strip():
        return {"error": "a rejection needs a note saying what must change"}, 400
    return {"ok": True, "asset": asset,
            **approvals.decide(project, asset, state, note=note, by="dashboard")}, 200


# --- human playtesting (studio/playtest.py) -----------------------------------
# Rule 6b, strictly: every value these routes act on must be BYTE-IDENTICAL to
# a member of a list the server computes — the studio projects, that project's
# builds, its sessions, its findings. No path comes from a body, and the one
# process launched (Godot on a build snapshot) has a fixed argv.
def _pt_project(body):
    from studio import status as studio_status
    project = (body or {}).get("project")
    if not isinstance(project, str) or project not in studio_status.projects():
        return None
    return project


def _pt_session(project, body, required=True):
    from studio import playtest
    sid = (body or {}).get("session")
    if not required and sid in (None, ""):
        return ""
    if not isinstance(sid, str) or sid not in playtest.session_ids(project):
        return None
    return sid


def _playtest_launch(body):
    """Launch one build of a studio game for a human to play."""
    from studio import playtest
    project = _pt_project(body)
    if project is None:
        return {"error": "unknown studio project"}, 404
    build = (body or {}).get("build")
    if not isinstance(build, str) or build not in [b["id"] for b in playtest.builds(project)]:
        return {"error": "unknown build for this project"}, 404
    try:
        return {"ok": True, "session": playtest.launch(project, build, wait=False)}, 200
    except playtest.Unavailable as exc:
        return {"error": str(exc)}, 409
    except KeyError:
        return {"error": "unknown build for this project"}, 404


def _playtest_stop(body):
    from studio import playtest
    project = _pt_project(body)
    if project is None:
        return {"error": "unknown studio project"}, 404
    sid = _pt_session(project, body)
    if sid is None:
        return {"error": "unknown session"}, 404
    playtest.stop(project, sid)
    return {"ok": True}, 200


def _playtest_finding(body):
    """A finding typed in the dashboard, optionally tied to a session."""
    from studio import playtest
    project = _pt_project(body)
    if project is None:
        return {"error": "unknown studio project"}, 404
    sid = _pt_session(project, body, required=False)
    if sid is None:
        return {"error": "unknown session"}, 404
    note = (body or {}).get("note")
    if not isinstance(note, str):
        return {"error": "note must be a string"}, 400
    try:
        f = playtest.add_finding(project, category=(body or {}).get("category"),
                                 severity=(body or {}).get("severity"),
                                 note=note[:playtest.MAX_NOTE], session=sid or None)
    except ValueError as exc:
        return {"error": str(exc)}, 400
    return {"ok": True, "finding": f}, 200


def _playtest_triage(body):
    from studio import playtest
    project = _pt_project(body)
    if project is None:
        return {"error": "unknown studio project"}, 404
    fid = (body or {}).get("finding")
    if not isinstance(fid, str) or fid not in playtest.load_findings(project):
        return {"error": "unknown finding"}, 404
    state = (body or {}).get("state")
    if not isinstance(state, str) or state not in playtest.STATES:
        return {"error": f"state must be one of {list(playtest.STATES[1:])}"}, 400
    note, link = (body or {}).get("note") or "", (body or {}).get("link") or ""
    if not isinstance(note, str) or not isinstance(link, str):
        return {"error": "note and link must be strings"}, 400
    try:
        f = playtest.triage(project, fid, state, note=note, link=link)
    except ValueError as exc:
        return {"error": str(exc)}, 400
    except KeyError:
        return {"error": "unknown finding"}, 404
    return {"ok": True, "finding": f}, 200


def _playtest_survey(body):
    from studio import playtest
    project = _pt_project(body)
    if project is None:
        return {"error": "unknown studio project"}, 404
    sid = _pt_session(project, body)
    if sid is None:
        return {"error": "unknown session"}, 404
    note = (body or {}).get("note") or ""
    if not isinstance(note, str):
        return {"error": "note must be a string"}, 400
    try:
        playtest.survey(project, sid, fun=(body or {}).get("fun"),
                        clarity=(body or {}).get("clarity"),
                        difficulty=(body or {}).get("difficulty"), note=note)
    except ValueError as exc:
        return {"error": str(exc)}, 400
    return {"ok": True}, 200


_PLAYTEST_POSTS = {
    "/api/studio/playtest/launch": _playtest_launch,
    "/api/studio/playtest/stop": _playtest_stop,
    "/api/studio/playtest/finding": _playtest_finding,
    "/api/studio/playtest/triage": _playtest_triage,
    "/api/studio/playtest/survey": _playtest_survey,
}


def _live_task_ids():
    """{task id: role} for every task with a harness running right now.

    Drivers name a fix-round attempt `<task>-x<n>` (the transcript is
    `<task>-x<n>-<role>-<attempt>.jsonl`), so the suffix is stripped to get
    back to the taskfile's id.
    """
    try:
        inflight, _ = _collect_inflight(time.time(), Handler.store)
    except Exception:                                        # noqa: BLE001
        return set()
    out = {}
    for a in inflight:
        t = a.get("task")
        if t:
            out[re.sub(r"-x\d+$", "", str(t))] = a.get("role") or "running"
    return out


def _agents(store):
    now = time.time()
    inflight, _kimi = _collect_inflight(now, store)
    _prune_registry()
    # chat entries are not harness runs; keep them out of `runs` (the
    # dashboard counts that list as "harness runs today")
    runs = [{"taskfile": k, **v} for k, v in _launch_registry.items()
            if v.get("kind") != "chat"]
    chats = [{"session": k, **v} for k, v in _launch_registry.items()
             if v.get("kind") == "chat"]
    return {"now": now, "agents": inflight, "runs": runs, "chats": chats,
            "recent": _recent_agent_runs(store)}


_work_status_cache = {"at": 0.0, "value": None}


def _work_status(store):
    """Live project DAGs with task-level pipeline and wait evidence.

    The task table records durable outcomes, not the active graph node. Event
    pairs provide that node, while driver events distinguish a running harness
    from a usage-window or capacity wait. An old unmatched event is never
    presented as live unless the owning project run is alive.
    """
    now = time.time()
    if _work_status_cache["value"] is not None and now - _work_status_cache["at"] < 5:
        return _work_status_cache["value"]
    projects = _projects(store)
    agents, _ = _collect_inflight(now, store)
    ids = {n["id"] for p in projects for n in p["dag"]["nodes"]
           if n.get("kind") != "chain"}
    live_tasks_by_pid = {
        str(p["run_pid"]): {n["id"] for n in p["dag"]["nodes"]
                             if n.get("kind") != "chain"}
        for p in projects if p.get("run_pid")}
    # Match the full task suffix. partition('_') silently turns
    # pr_reviewer_<id> into stage 'pr' and task 'reviewer_<id>'.
    suffixes = sorted(ids, key=len, reverse=True)

    def task_of(raw):
        base, _ = _xkey(raw)
        base = re.sub(r"-pr\d+$", "", base or "")
        return base if base in ids else None

    def node_of(name):
        for tid in suffixes:
            if name.endswith("_" + tid):
                return tid, name[:-(len(tid) + 1)]
        return None, None

    state = {(pid, tid): {"node": None, "stage_since": None,
                          "open_nodes": {}, "driver_event": None}
             for pid, tids in live_tasks_by_pid.items() for tid in tids}
    for line in _load_event_lines():
        if not any(marker in line for marker in ('"node_', '"driver.')):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        # All current graph and driver events carry a run_id ending in their
        # owner PID. Exclude old unmatched starts from a previous run.
        run_id = str(e.get("run_id") or "")
        pid = run_id.rsplit("-", 1)[-1]
        if not run_id or pid not in live_tasks_by_pid:
            continue
        kind = e.get("type")
        if kind in ("node_start", "node_end", "node_error"):
            tid, stage = node_of(e.get("node") or "")
            if tid is None or tid not in live_tasks_by_pid[pid]:
                continue
            s = state[(pid, tid)]
            if kind == "node_start":
                if s["node"] != stage or not s["open_nodes"].get(stage):
                    s["stage_since"] = _ts(e.get("ts"))
                s["node"] = stage
                s["open_nodes"][stage] = s["open_nodes"].get(stage, 0) + 1
                s["driver_event"] = None  # a new graph firing clears old waits
            elif s["open_nodes"].get(stage, 0):
                s["open_nodes"][stage] -= 1
                if not s["open_nodes"][stage]:
                    del s["open_nodes"][stage]
                if s["node"] == stage and stage not in s["open_nodes"]:
                    s["node"] = next(reversed(s["open_nodes"]), None)
                    if s["node"] is None:
                        s["stage_since"] = None
                        s["driver_event"] = None
        elif isinstance(kind, str) and kind.startswith("driver."):
            tid = task_of(e.get("task"))
            if tid is None or tid not in live_tasks_by_pid[pid]:
                continue
            if kind in ("driver.queued", "driver.slot_wait", "driver.cap_wait",
                        "driver.start", "driver.progress", "driver.heartbeat",
                        "driver.usage_limit", "driver.usage_wait",
                        "driver.usage_swap", "driver.stalled", "driver.timeout",
                        "driver.done", "driver.error", "driver.cancelled",
                        "driver.cap_timeout"):
                state[(pid, tid)]["driver_event"] = e

    live_agents = {}
    for a in agents:
        tid = task_of(a.get("task"))
        if tid and a.get("source", "").startswith("driver:"):
            live_agents.setdefault(tid, []).append(a)

    for p in projects:
        nodes = {n["id"]: n for n in p["dag"]["nodes"] if n.get("kind") != "chain"}
        tasks = []
        live_run = bool(p.get("run_pid"))
        chain_ready = not p.get("chain") or p["chain"].get("ready", True)
        for tid, n in nodes.items():
            s = state.get((str(p.get("run_pid")), tid)) or {
                "node": None, "stage_since": None, "driver_event": None}
            ev = s["driver_event"] or {}
            # An unmatched driver.start from a previous run may still appear
            # in /api/agents. Keep only agents started inside this node firing.
            task_agents = ([a for a in live_agents.get(tid, [])
                            if s["stage_since"] is not None
                            and str(a.get("pid")) == str(p.get("run_pid"))
                            and (a.get("started") or 0) >= s["stage_since"] - 2]
                           if live_run else [])
            task_agents.sort(key=lambda a: a.get("started") or 0, reverse=True)
            agent = task_agents[0] if task_agents else None
            status = n.get("status") or "pending"
            deps = [e["src"] for e in p["dag"]["edges"]
                    if e.get("dst") == tid and e.get("kind") != "fixloop"
                    and e.get("src") in nodes
                    and nodes[e["src"]].get("status") not in ("merged", "skipped")]
            failed_deps = [dep for dep in deps
                           if nodes[dep].get("status") in ("failed", "conflict")]
            stage = s["node"] if live_run and status in ("pending", "running", "in_review") else None
            activity, reason = "unknown", None
            if status in ("merged", "skipped"):
                activity = "done"
            elif status in ("failed", "conflict"):
                activity, reason = "blocked", next(
                    (x.get("error") for x in p.get("errors", []) if x.get("id") == tid), None)
            elif not live_run and status in ("running", "in_review"):
                activity, reason = "stalled", "owning project run is not live"
            elif failed_deps:
                activity, reason = "blocked", "upstream task needs repair: " + ", ".join(failed_deps)
            elif deps:
                activity, reason = "waiting", "waiting for " + ", ".join(deps)
            elif not chain_ready:
                failed_chain = [d.get("title") or d.get("file")
                                for d in (p.get("chain") or {}).get("deps", [])
                                if d.get("state") == "failed"]
                if failed_chain:
                    activity, reason = "blocked", "upstream project needs repair: " + ", ".join(failed_chain)
                else:
                    activity, reason = "waiting", "waiting for upstream project to merge"
            elif not live_run:
                activity = "waiting"
            elif stage:
                activity = "working"
                # node_start clears driver_event. A missing timestamp is not
                # ancient: now - 0 is seconds since the epoch, so a node that
                # just opened would look stalled until the first driver event.
                ev_ts = _ts(ev.get("ts"))
                ev_age = (now - ev_ts) if ev_ts is not None else 0
                if ev.get("type") in ("driver.usage_limit", "driver.usage_wait") and ev_age < 420:
                    reset = ev.get("resets_at")
                    activity, reason = "usage_wait", (
                        f"usage limit; resets at {datetime.fromtimestamp(reset).astimezone().isoformat()}"
                        if isinstance(reset, (int, float)) else
                        "usage limit; waiting for the next reset check")
                elif ev.get("type") in ("driver.slot_wait", "driver.cap_wait", "driver.queued") and ev_age < 120:
                    activity = "queued"
                    reason = ("waiting for a driver slot" if ev.get("type") == "driver.cap_wait"
                              else "waiting for an available model or harness slot")
                elif agent and (agent.get("stalled") or
                                (agent.get("last_event_s") or 0) >
                                config.DRIVER_PROGRESS_INTERVAL * 3):
                    activity, reason = "stalled", "agent idle past stall threshold"
                elif ev.get("type") == "driver.stalled" and ev_age < 420:
                    activity, reason = "stalled", "agent reported a stall"
                elif (stage in ("implement", "review", "pr_reviewer") and not agent
                      and ev_ts is not None and ev_age > 420):
                    activity, reason = "stalled", "no recent agent heartbeat"
                elif stage == "chain_wait":
                    activity, reason = "waiting", "waiting for upstream project to merge"
            elif status == "in_review":
                activity, reason = "waiting", "pull request is awaiting review"
            else:
                # A live project PID does not prove this particular task has
                # entered a driver queue. Reserve "queued" for a fresh slot
                # event above; until then it is ready for graph scheduling.
                activity, reason = "waiting", (
                    "ready; waiting for the task scheduler" if live_run else None)
            if stage is None and not deps and not chain_ready and activity in ("waiting", "blocked"):
                stage = "chain_wait"
            elif stage is None and deps and activity in ("waiting", "blocked"):
                stage = "dependency_wait"
            task = {"id": tid, "title": n.get("title"), "status": status,
                    "stage": stage, "stage_since": s["stage_since"] if stage else None,
                    "activity": activity, "reason": reason, "blocked_by": deps,
                    "model": (agent or {}).get("model") or n.get("model"),
                    "role": (agent or {}).get("role"),
                    "agents": [{"model": a.get("model"), "role": a.get("role"),
                                "harness": a.get("harness"), "started": a.get("started"),
                                "elapsed_s": a.get("elapsed_s"), "idle_s": a.get("idle_s"),
                                "stalled": bool(a.get("stalled") or
                                                (a.get("last_event_s") or 0) >
                                                config.DRIVER_PROGRESS_INTERVAL * 3),
                                "last_event_s": a.get("last_event_s")}
                               for a in task_agents],
                    "started_at": (agent or {}).get("started"),
                    "last_event_at": _ts(ev.get("ts")) if live_run else None,
                    "idle_s": (agent or {}).get("idle_s"),
                    "elapsed_s": (agent or {}).get("elapsed_s"),
                    "usage_resets_at": (ev.get("resets_at") if activity == "usage_wait" else None)}
            tasks.append(task)
        p["tasks"] = tasks
    out = {"now": now, "projects": projects, "agents": agents}
    _work_status_cache.update(at=now, value=out)
    return out


_TRANSCRIPT_RE = re.compile(r"^[\w.-]+\.jsonl$")


_gh_cache = {"key": 0.0, "data": None}
GH_CACHE_S = 60.0  # the GitHub quota is shared with every fleet run


def _github(store, repo=None):
    """Open and recent pull requests, plus the review trail we recorded.

    GitHub shows 0 reviews on these PRs — a bot cannot formally approve a pull
    request opened by its own account, so reviewer verdicts land as comments.
    The authoritative record of who approved is our own task.pr_reviewed
    events, so both are returned and the console shows them together.
    """
    now = time.time()
    if _gh_cache["data"] is not None and now - _gh_cache["key"] < GH_CACHE_S:
        return _gh_cache["data"]
    repo = repo or Path(config.ROOT)
    out = {"now": now, "ready": False, "reason": None, "prs": [],
           "base": config.BASE_BRANCH, "prod": config.PROD_BRANCH}

    def gh(*args, timeout=25):
        try:
            r = subprocess.run(["gh", "-R", "", *args] if False else ["gh", *args],
                               cwd=str(repo), capture_output=True, text=True,
                               timeout=timeout)
            return r.stdout if r.returncode == 0 else ""
        except Exception:
            return ""

    def git(*args):
        try:
            r = subprocess.run(["git", "-C", str(repo), *args],
                               capture_output=True, text=True, timeout=5)
            return r.stdout if r.returncode == 0 else ""
        except Exception:
            return ""

    remote = git("remote", "get-url", "origin").strip()
    if not remote:
        out["reason"] = "no git remote configured"
        _gh_cache.update(key=now, data=out)
        return out
    out["ready"] = True
    out["remote"] = remote
    out["repo_url"] = re.sub(r"\.git$", "", remote.replace("git@github.com:",
                                                            "https://github.com/"))
    raw = gh("pr", "list", "--state", "all", "--limit", "20", "--json",
             "number,title,state,headRefName,baseRefName,additions,deletions,"
             "createdAt,updatedAt,url,isDraft,mergedAt")
    try:
        prs = json.loads(raw) if raw.strip() else []
    except ValueError:
        prs = []

    # our own verdicts, keyed by PR number
    verdicts = {}
    for line in _load_event_lines():
        if '"task.pr_reviewed"' not in line and '"task.pr_opened"' not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        n = e.get("pr") or e.get("number")
        if not n:
            continue
        v = verdicts.setdefault(n, {"task": e.get("task"), "rounds": []})
        if e.get("type") == "task.pr_reviewed":
            v["rounds"].append({
                "round": e.get("round"), "approved": e.get("approved"),
                "approvals": e.get("approvals") or [],
                "reviewers": e.get("reviewers") or [],
                "issues": e.get("n_issues") or 0,
                # What they actually objected to, so the verdict is readable
                # here rather than only on GitHub.
                "detail": e.get("issues") or [],
                "inconclusive": bool(e.get("inconclusive")),
                "crashed": e.get("crashed") or [],
                "ts": _ts(e.get("ts"))})

    ahead = git("rev-list", "--count",
                f"{config.PROD_BRANCH}..{config.BASE_BRANCH}").strip()
    out["unpromoted"] = int(ahead) if ahead.isdigit() else 0
    # A PR nobody is working on is the failure mode that cost this repo the
    # most: publish opened it, pr_review never ran, the run ended, and the
    # branch sat on GitHub with no process coming back for it. Seven at once,
    # and nothing in the UI said so — each looked like a healthy open PR.
    import reconcile  # imported per-function here, as elsewhere in this module
    try:
        live = {r.get("taskfile") for r in reconcile.live_runs()}
    except OSError:
        live = None  # cannot read the process table: report nothing, not everything
    try:
        owner = {r["id"]: r for r in (store.code_tasks_all() if store else [])}
    except (AttributeError, sqlite3.Error):
        owner = {}
    for pr in prs:
        n = pr.get("number")
        v = verdicts.get(n, {})
        pr["task"] = v.get("task")
        pr["rounds"] = v.get("rounds", [])
        pr["approvals"] = (v["rounds"][-1]["approvals"] if v.get("rounds") else [])
        pr["reviewers"] = (v["rounds"][-1]["reviewers"] if v.get("rounds") else [])
        pr["stranded"] = _pr_is_stranded(pr, owner, live)
        out["prs"].append(pr)
    out["stranded"] = sum(1 for p in out["prs"] if p.get("stranded"))
    _gh_cache.update(key=now, data=out)
    return out


def _pr_is_stranded(pr, owner, live):
    """True when this PR is open and no run is CURRENTLY working on it.

    Note what this does and does not claim. It is true both for a PR that was
    genuinely orphaned (its run died mid-flight) and for one whose project is
    simply queued behind others — the fleet keeps no durable queue state, so
    from here those are indistinguishable. The UI therefore says "no run",
    which is exactly true of both, rather than "abandoned", which would be
    alarming and often wrong. The operator action is the same either way:
    re-run its project.

    Deliberately conservative — an unclear answer is NOT a warning:
      - only OPEN pull requests count
      - `live` is None when the run list could not be read at all, and then
        nothing is reported rather than everything
      - a task whose taskfile has a live run is being worked on right now
      - a task the DB does not know is skipped: it may be a human's branch,
        and calling someone's own PR abandoned is worse than staying quiet
    """
    if (pr.get("state") or "").upper() != "OPEN" or live is None:
        return False
    task = pr.get("task")
    if not task:
        head = pr.get("headRefName") or ""
        task = head[5:] if head.startswith("task/") else None
    row = owner.get(task) if task else None
    if row is None:
        return False
    if row.get("status") in ("merged", "failed"):
        return False
    taskfile = row.get("taskfile")
    return not (taskfile and taskfile in live)


def _task_deliverable(repo, task_id, want_patch=False):
    """What a finished task actually changed, from its commit.

    A merged task showed a green chip and nothing else — no way to see whether
    it wrote the thing you asked for or something else entirely. The commit is
    right there: gitstore.publish tags it `task(<id>): <title>`.
    """
    if not repo or not repo.is_dir():
        return {"error": "repo not found"}
    if not re.fullmatch(r"[\w.-]{1,80}", task_id or ""):
        return {"error": "bad task id"}

    def git(*args, limit=200000):
        try:
            r = subprocess.run(["git", "-C", str(repo), *args],
                               capture_output=True, text=True, timeout=10)
            return r.stdout[:limit] if r.returncode == 0 else ""
        except Exception:
            return ""

    sha = git("log", "--format=%H", "-1", f"--grep=^task({task_id}):").strip()
    if not sha:
        return {"task": task_id, "found": False,
                "reason": "no commit for this task — it never reached publish"}
    subject = git("log", "-1", "--format=%s", sha).strip()
    when = git("log", "-1", "--format=%cI", sha).strip()
    stat = [l for l in git("show", "--stat", "--format=", sha).splitlines() if l.strip()]
    files = []
    for line in stat[:-1] if stat else []:
        name, _, churn = line.partition("|")
        if name.strip():
            files.append({"path": name.strip(), "churn": churn.strip()})
    out = {"task": task_id, "found": True, "sha": sha[:10], "subject": subject,
           "when": when, "files": files,
           "summary": stat[-1].strip() if stat else ""}
    if want_patch:
        # Bounded: a review diff can be huge and this renders in a drawer.
        patch = git("show", "--format=", sha, limit=120000)
        out["patch"] = patch
        out["truncated"] = len(patch) >= 120000
    return out


def _transcript_tail(fname, tail):
    if not _TRANSCRIPT_RE.fullmatch(fname or ""):
        return {"error": "bad file name"}, 400
    path = Path(config.ROOT) / "logs" / "harness" / fname
    if not path.is_file():
        return {"error": "not found"}, 404
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return {"error": str(exc)}, 500
    return {"file": fname, "total_lines": len(lines), "lines": lines[-tail:]}, 200


# The transcript drawer polls every 3 s; a reasonix transcript is ~4 MB, so
# re-reading and re-reducing the whole file per poll would burn the dashboard
# process. One reducer per file keeps its stream offset, and a poll consumes
# only the bytes appended since the last one. Eviction is LRU: every hit moves
# the entry to the newest end, so a hot multi-MB transcript cannot be dropped
# by churn and fully re-parsed (~44k records) on its next poll. Like the other
# module-level caches here (_gh_cache, _launch_registry) it is mutated without
# a lock.
_activity_cache = OrderedDict()


def _probe_transcript(data):
    """True when the first records look like a reasonix or opencode transcript.

    Anything else (kimi-cli's role/tool_calls records, zero-byte files left by
    a killed run) gets the raw tail, exactly as before this view existed.

    The scan walks COMPLETE lines from the start, because the first line can be
    far larger than any fixed window: a captain/planner prompt is one record of
    13 KB+, and a byte window that cut it mid-record made every such file look
    unrecognizable — the whole transcript then fell back to raw JSON with no
    folded thinking. Bounding by LINE COUNT (not bytes) inspects the opening
    records without materializing a multi-MB tail, and a record larger than any
    prompt is still fine because no byte cap is applied.
    """
    lines = []
    start = 0
    for _ in range(50):
        nl = data.find("\n", start)
        if nl == -1:
            break
        lines.append(data[start:nl])       # only COMPLETE lines; the tail is
        start = nl + 1                     # never sliced, so a multi-MB file
    for line in lines:                     # is not materialized to inspect 50
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if isinstance(obj.get("kind"), str):
            return True
        if obj.get("type") in ("result", "text", "tool_use", "step_start", "step_finish"):
            return True
    return False


def _activity_entry(path):
    """The cache entry for one transcript, caught up to the file's current end.

    None when the file cannot be read or its shape is not one the reducer
    understands — the caller then serves the raw tail.
    """
    from drivers import TranscriptActivity
    try:
        st = path.stat()
    except OSError:
        return None
    key = (st.st_size, st.st_mtime_ns)
    pkey = str(path)
    ent = _activity_cache.get(pkey)
    if ent is not None:
        if ent["key"] == key:
            _activity_cache.move_to_end(pkey)   # LRU: a fresh poll renews it
            return ent if ent["shape_ok"] else None
        if not ent["shape_ok"] or st.st_size < ent["key"][0]:
            # The first probe saw nothing recognizable yet (or the file was
            # rewritten — a shrunk file invalidates the saved offset).
            ent = None
    if ent is None:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                chunk = fh.read()
                off = fh.tell()
        except OSError:
            return None
        if not _probe_transcript(chunk):
            if chunk:
                # Shape is decided by the first bytes and never flips; remember
                # it so a finished kimi file isn't re-probed on every poll.
                if len(_activity_cache) >= 128:
                    _activity_cache.popitem(last=False)
                _activity_cache[pkey] = {"key": key, "off": 0, "parser": None,
                                         "shape_ok": False}
            return None
        if len(_activity_cache) >= 128:
            _activity_cache.popitem(last=False)
        ent = {"key": key, "off": off, "parser": TranscriptActivity(), "shape_ok": True}
        ent["parser"].feed(chunk)
        _activity_cache[pkey] = ent
        return ent
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(ent["off"])
            chunk = fh.read()
            ent["off"] = fh.tell()
    except OSError:
        return None
    if chunk:
        ent["parser"].feed(chunk)
    ent["key"] = key
    _activity_cache.move_to_end(pkey)           # LRU: an appended-to file is hot
    return ent


def _transcript_activity_view(fname, tail):
    """A transcript reduced to readable activity blocks, or None for the raw path."""
    if not _TRANSCRIPT_RE.fullmatch(fname or ""):
        return None
    path = Path(config.ROOT) / "logs" / "harness" / fname
    if not path.is_file():
        return None
    ent = _activity_entry(path)
    if ent is None:
        return None
    tail = min(max(tail, 1), 400)
    blocks = list(ent["parser"].blocks)
    pending = ent["parser"].pending()
    if pending:
        # A pending line out of a file that stopped growing long ago is not
        # "thinking right now" — it is what a dead harness was in the middle
        # of. pending() stays time-pure by design; the staleness read happens
        # here, the one place the file's mtime is known.
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = 0
        if age >= 60:
            span = f"{int(age // 60)}m" if age >= 120 else f"{int(age)}s"
            pending += f" — no new output for {span}, stream may be stalled/dead"
    return {"mode": "activity", "file": fname, "blocks": blocks[-tail:],
            "pending": pending, "total_blocks": ent["parser"].produced}, 200


def _prune_registry():
    for key, rec in list(_launch_registry.items()):
        pid = rec.get("pid")
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except ProcessLookupError:
                alive = False
            except PermissionError:
                alive = True
            except Exception:
                alive = False
        if not alive:
            del _launch_registry[key]


def _spawn_logged(argv, log_name, env_extra=None):
    log_dir = Path(config.ROOT) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    lf = open(log_dir / log_name, "ab", buffering=0)
    # Unbuffered: with stdout redirected to a file Python block-buffers it,
    # so a live run's log stayed EMPTY until the process exited and "view
    # log" on a running project showed nothing at all.
    env = dict(os.environ, PYTHONUNBUFFERED="1", **(env_extra or {}))
    proc = subprocess.Popen(argv, cwd=str(config.ROOT), stdout=lf, stderr=subprocess.STDOUT,
                            start_new_session=True, close_fds=True, env=env)
    return proc, log_name


def _write_taskfile_atomically(fpath, doc):
    """Write via a temp file + rename, so a crash cannot leave a partial one.

    A plain write_text truncates first and fills after. A process killed in
    that window leaves a ZERO-BYTE task file, which then fails every later run
    of it and every check.sh gate with "Expecting value: line 1 column 1" —
    observed for real on 2026-09-09. rename(2) within a directory is atomic:
    readers see either the old file or the complete new one.
    """
    tmp = fpath.with_name(fpath.name + f".tmp{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, fpath)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _create_project(body):
    if not isinstance(body, dict):
        return {"error": "JSON body required"}, 400
    repo = _valid_repo(body.get("repo"))
    if repo is None:
        return {"error": f"repo must be an existing absolute path under {config.REPO_ROOT}"}, 400
    problem = _repo_problem(repo)
    if problem:
        return {"error": problem}, 400
    overwrite = bool(body.get("overwrite"))
    py = str(Path(config.ROOT) / ".venv" / "bin" / "python")

    goal = (body.get("goal") or "").strip() if isinstance(body.get("goal"), str) else ""
    if goal:
        if not (3 <= len(goal) <= 2000):
            return {"error": "goal must be 3..2000 chars"}, 400
        slug = _task_slug(goal)
        expect = slug + ".json"
        if (Path(config.TASKS_DIR) / expect).exists() and not overwrite:
            return {"error": f"{expect} already exists; pass overwrite=true to replan",
                    "exists": True, "file": expect}, 409
        proc, log_name = _spawn_logged(
            [py, "main.py", "code", "plan", goal, str(repo)], f"plan-{slug}.log")
        _launch_registry[str(Path(config.TASKS_DIR) / expect)] = {
            "pid": proc.pid, "log": log_name, "started": time.time(), "dry_run": False,
            "kind": "plan"}
        return {"mode": "plan", "pid": proc.pid, "log": log_name,
                "taskfile": expect,
                "note": f"{config.PLANNER_MODEL} is drafting the task file"}, 200

    title = (body.get("title") or "").strip() if isinstance(body.get("title"), str) else ""
    tasks = body.get("tasks")
    if not title:
        return {"error": f"title required (or pass goal to plan with "
                         f"{config.PLANNER_MODEL})"}, 400
    if not isinstance(tasks, list) or not tasks or len(tasks) > 50:
        return {"error": "tasks must be a list of 1..50 task objects"}, 400
    clean, seen = [], set()
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            return {"error": f"task {i} is not an object"}, 400
        tid = (t.get("id") or "").strip() if isinstance(t.get("id"), str) else ""
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,60}", tid):
            return {"error": f"task {i}: id must match [a-z0-9][a-z0-9-]{{0,60}}"}, 400
        if tid in seen:
            return {"error": f"duplicate task id {tid!r}"}, 400
        seen.add(tid)
        if not (t.get("title") or "").strip() or not (t.get("prompt") or "").strip():
            return {"error": f"task {tid}: title and prompt are required"}, 400
        entry = {"id": tid, "title": t["title"].strip(), "prompt": t["prompt"].strip()}
        for opt in ("model", "reviewer", "verify_cmd", "base"):
            if isinstance(t.get(opt), str) and t[opt].strip():
                entry[opt] = t[opt].strip()
        # The entry tier of TODAY'S roster, never a literal: this default
        # would have written DeepSeek-V4-Flash into new taskfiles the morning
        # after it was withdrawn, and every one of them would then fail
        # validation with "must be an implementer".
        entry.setdefault("model", config.ESCALATION_PATH[0])
        if "reviewer" not in entry:
            # Cross-family, from the roster — the literal {Kimi: glm, GLM: kimi}
            # map this replaces would have defaulted every task to a retired
            # family the day after Kimi-K3 left (2026-09-12).
            entry["reviewer"] = (config.cross_family_reviewer(entry["model"])
                                 or next(iter(config.REVIEW_FAMILIES), "glm"))
        deps_in = t.get("deps") if isinstance(t.get("deps"), list) else t.get("depends")
        if isinstance(deps_in, list):
            deps = [d for d in deps_in if isinstance(d, str)]
            all_ids = {x.get("id") for x in tasks if isinstance(x, dict) and x.get("id")}
            unknown = [d for d in deps if d not in all_ids]
            if unknown:
                return {"error": f"task {tid}: deps references unknown id(s) {unknown}"}, 400
            if deps:
                entry["deps"] = deps
        clean.append(entry)
    fname = _task_slug(title) + ".json"
    fpath = Path(config.TASKS_DIR) / fname
    if fpath.exists() and not overwrite:
        return {"error": f"{fname} already exists; pass overwrite=true", "exists": True,
                "file": fname}, 409
    Path(config.TASKS_DIR).mkdir(parents=True, exist_ok=True)
    doc = {"project": {"repo": str(repo), "title": title, "tasks": clean}}
    _write_taskfile_atomically(fpath, doc)
    return {"mode": "tasks", "file": fname, "n_tasks": len(clean)}, 200


def _run_project(body):
    if not isinstance(body, dict):
        return {"error": "JSON body required"}, 400
    fname = body.get("file") or ""
    if not re.fullmatch(r"[\w.-]+\.json", fname):
        return {"error": "bad file name"}, 400
    path = Path(config.TASKS_DIR) / fname
    if not path.is_file():
        return {"error": "not found"}, 404
    try:
        proj = (json.loads(path.read_text(encoding="utf-8", errors="replace"))
                .get("project") or {})
    except Exception as exc:
        return {"error": f"invalid task file: {exc}"}, 400
    repo = _valid_repo(proj.get("repo") or "")
    if repo is None:
        return {"error": f"task file names an unusable repo: {proj.get('repo')!r}"}, 400
    problem = _repo_problem(repo)
    if problem:
        return {"error": problem}, 400
    dry_run = bool(body.get("dry_run"))
    _prune_registry()
    key = str(path)
    rec = _launch_registry.get(key)
    if rec:
        return {"error": "this task file already has a running process",
                "pid": rec.get("pid"), "log": rec.get("log")}, 409
    # The registry only knows runs THIS dashboard launched. A run started from
    # a terminal or by run-queue.sh is invisible to it, and two processes on
    # the same task file share task ids, worktrees and branches.
    import reconcile
    others = [r["pid"] for r in reconcile.live_runs()
              if r.get("taskfile") and Path(r["taskfile"]).name == fname]
    if others:
        return {"error": f"this task file is already being run by pid "
                         f"{', '.join(map(str, others))} (started outside this "
                         f"dashboard — the queue, or a terminal)",
                "pid": others[0]}, 409
    venv_py = Path(config.ROOT) / ".venv" / "bin" / "python"
    # A dashboard served from a git worktree has no .venv of its own (it is
    # gitignored); its own interpreter is the one with the dependencies.
    argv = [str(venv_py) if venv_py.exists() else sys.executable, "main.py",
            "code", "run", str(path)]
    if dry_run:
        argv.append("--dry-run")
    slug = _task_slug(path.stem)
    log_name = f"run-{slug}-{int(time.time())}.log"
    # Launch under the fleet the taskfile was planned for: a studio taskfile's
    # models do not exist on the local roster.
    try:
        fleet = (json.loads(path.read_text(encoding="utf-8")).get("project") or {}).get("fleet")
    except (OSError, ValueError):
        fleet = None
    if isinstance(fleet, str) and fleet:
        proc, log_name = _spawn_logged(argv, log_name, {"ARC_FLEET": fleet})
    else:
        proc, log_name = _spawn_logged(argv, log_name)
    # "started" must mean the run is actually going, not merely that a
    # process was forked. On 09-12 a bad asyncio.run() in `code run` killed
    # every run in its first second; the button said "started (pid N)" and
    # the operator was left staring at a project that never changed. So wait
    # a moment and, if the process is already gone, say so — with the tail
    # of its log, which is where the reason is. A dry run legitimately exits
    # fast, and its exit code says whether that was success.
    died = _exited_early(proc, 1.5)
    if died is not None and not (dry_run and died == 0):
        return {"error": f"the run exited immediately (exit {died}) — see logs/{log_name}",
                "log": log_name, "exit": died, "tail": _log_tail(log_name, 12)}, 500
    if died is None:
        _launch_registry[key] = {"pid": proc.pid, "log": log_name, "started": time.time(),
                                 "dry_run": dry_run, "kind": "run"}
    return {"pid": proc.pid, "log": log_name, "dry_run": dry_run,
            "finished": died is not None}, 200


def _exited_early(proc, seconds):
    """The exit code if `proc` ends within `seconds`, else None."""
    try:
        return proc.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        return None


_RUN_LOG_RE = re.compile(r"^(run|plan)-[\w.-]+\.log$")

# --- task timeline: everything that happened to one task -------------------
# Debugging a task used to mean grepping events.jsonl, the harness
# transcripts, error_events, gate output and logs/evidence by hand. This is
# the one read-only view that gathers them, and the whole of its input is a
# task id plus an OPTIONAL taskfile name — never a command, a path or a ref
# from a request body (AGENTS.md Rule 6b: the dashboard is unauthenticated
# and listens on every interface).
#
# The id is validated before it is used as a path segment under logs/evidence
# or as a harness_runs LIKE prefix, exactly like the file parameters of
# /api/gate-log and /api/transcript. `.` and `-` are legal INSIDE an id
# (`deepseek-v4.1-flash`), but an id made only of dots is a traversal — ".."
# matched the character class and then became a path segment under the
# evidence root, which is exactly the hole this validates against.
_TASK_ID_RE = re.compile(r"^(?!\.+$)[A-Za-z0-9_.-]{1,120}$")

# How far back the timeline reads events.jsonl. The log is append-ordered and
# rotates at 100 MiB, so the newest records are the last lines: a 100 MiB file
# must not be parsed to answer "what happened to this one task". Two bounds
# guard that — the number of LINES examined and the number of events kept —
# and both are reported, so a truncated answer is visible rather than passed
# off as the whole history.
TIMELINE_SCAN_LINES = 20000
TIMELINE_MAX_EVENTS = 4000
_TIMELINE_BYTES_PER_LINE = 512      # tail window sized from the line budget

# Which events belong on a task's timeline. Prefix families, not an
# enumeration of today's names: the driver and code-task namespaces grow
# (`driver.usage_limit`, `driver.usage_swap`, `driver.queued`… all exist and
# all belong here), and a hand-kept list would silently drop the next one.
# That silent drop is exactly the failure this feature exists to end — the
# scan is bounded separately, so breadth here costs nothing.
TIMELINE_PREFIXES = ("driver.", "task.", "worktree.", "git.", "evidence.",
                     "chain.")
# Events that are technically in those namespaces but are fleet weather on a
# task that happens to share the account, not this task's history.
TIMELINE_SKIP = frozenset(("driver.heartbeat", "driver.queued",
                           "driver.slot_wait", "driver.cap_timeout"))
# What the drawer renders as the body of an entry. A driver.done carries a
# verdict, a task.gate its output tail; dumping the whole event is unreadable.
TIMELINE_BODY_KEYS = ("tail", "output", "why", "reason", "error", "issues",
                      "message", "note", "review_issues")


def _timeline_owns(etype):
    """Whether an event type is one a task timeline shows."""
    if not isinstance(etype, str):
        return False
    return (etype not in TIMELINE_SKIP
            and etype.startswith(TIMELINE_PREFIXES))


def _timeline_id_matches(ev_task, tid):
    """Whether this event's task field names this task or one of its attempts.

    Fix rounds record `<tid>-x<attempt>` and PR rounds `<tid>-pr<n>`, so a
    timeline matching only the bare id would miss every attempt that was not
    the first — which is exactly where the debugging is.

    The suffix is matched EXPLICITLY (`-x`, `-pr`), not as a bare `-`: a
    sibling task named `<id>-something` (task ids are hyphenated English, so
    `t1-sibling` and `t1` are both real) shares the prefix without being an
    attempt of this task, and its events would otherwise appear here.
    """
    if not isinstance(ev_task, str):
        return False
    if ev_task == tid:
        return True
    return ev_task.startswith((tid + "-x", tid + "-pr"))


def _evidence_root():
    return Path(config.EVIDENCE_DIR).resolve()


def _evidence_url(root, p):
    """The /api/evidence-file URL for a path inside the evidence root, or None.

    Containment, not string matching: a manifest naming /etc/passwd or
    ../../id_rsa resolves OUTSIDE the root and gets no URL at all.
    """
    try:
        rel = Path(p).resolve().relative_to(root)
    except (ValueError, OSError):
        return None
    return "/api/evidence-file?path=" + str(rel)


def _timeline_evidence(tid):
    """Evidence manifests under logs/evidence/<project>/<tid>/x*/manifest.json.

    EVERY project directory is walked, because the previous cap of the first
    64 (alphabetically) silently dropped the evidence of any task whose
    project sorts later — and an absent manifest is indistinguishable from a
    run that captured none, so the drawer reported it as "no evidence" rather
    than as a missing lookup. The walk is a readdir plus a stat per candidate;
    only a directory that actually has a `<tid>/x*` subtree costs anything.

    Each manifest's shots become /api/evidence-file URLs, but only for files
    that resolve inside the evidence root.
    """
    root = _evidence_root()
    out = []
    if not root.is_dir():
        return out
    try:
        projects = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return out
    for proj in projects:
        tdir = proj / tid
        if not tdir.is_dir():
            continue
        try:
            attempts = sorted(a for a in tdir.iterdir()
                              if a.is_dir() and a.name.startswith("x"))
        except OSError:
            continue
        for adir in attempts:
            mf = adir / "manifest.json"
            if not mf.is_file():
                continue
            try:
                man = json.loads(mf.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue                    # a half-written manifest is skipped
            if not isinstance(man, dict):
                continue
            cands, shots, seen = [], [], set()
            cands += list(man.get("shots") or [])
            cands += list(man.get("playtest_shots") or [])
            cands += [c.get("side_by_side") for c in (man.get("compare") or [])
                      if isinstance(c, dict)]
            for sc in (man.get("scenes") or []):
                if isinstance(sc, dict):
                    cands += list(sc.get("shots") or [])
            for s in cands:
                if not s or str(s) in seen:
                    continue
                seen.add(str(s))
                url = _evidence_url(root, s)
                if url:
                    shots.append({"name": Path(str(s)).name, "url": url})
            out.append({
                "project": proj.name, "attempt": adir.name,
                "manifest": str(mf), "seconds": man.get("seconds"),
                # A manifest records no instant of its own; the capture wrote
                # it into the attempt directory, so that directory's mtime is
                # when this evidence belongs on the timeline.
                "ts": _ts_of(mf.stat().st_mtime) if mf.exists() else None,
                "shots": shots, "videos": man.get("videos") or {},
                "compare": [c for c in (man.get("compare") or [])
                            if isinstance(c, dict)],
                "coverage": man.get("coverage"),
                "godot_errors": man.get("godot_errors") or [],
                "no_visible_change": man.get("no_visible_change"),
                "warnings": man.get("warnings") or [],
            })
    return out


def _timeline_events(tid, max_events=None, scan_lines=None):
    """This task's events, oldest first, from a BOUNDED backward scan.

    events.jsonl is append-ordered, so the newest records are the last lines,
    and a task's own attempts are near the end. Reading the whole history on
    every drawer open is what this avoids: at most `scan_lines` lines are
    examined and the scan stops once `max_events` matches are held.
    `truncated` reports that a bound was hit, so the page can say the view is
    partial instead of showing a cut-off history as if it were complete.
    """
    path = Path(config.EVENTS_LOG)
    try:
        st = path.stat()
    except OSError:
        return {"events": [], "scanned": 0, "truncated": False}
    # Resolved at CALL time, not bound as a default: the module constants are
    # the live policy, and a default argument would freeze the value the
    # module was imported with (so lowering the bound has no effect).
    max_events = TIMELINE_MAX_EVENTS if max_events is None else max_events
    scan_lines = TIMELINE_SCAN_LINES if scan_lines is None else scan_lines
    # mtime-gated cache: the drawer polls, and re-parsing an unchanged tail
    # every 3 s is the same waste twice.
    key = (str(path), st.st_size, st.st_mtime_ns, tid,
           int(max_events), int(scan_lines))
    # The check-and-read is ONE critical section. This server is a
    # ThreadingHTTPServer, so two overlapping GETs used to interleave as
    # "key matches for task A, value already replaced by task B" and one
    # task's timeline came back holding another task's events. The lock is
    # held across the hit test and the publish only — never across the scan —
    # so it serialises a dict lookup, not the work.
    with _timeline_cache_lock:
        if _timeline_cache["key"] == key:
            return _timeline_cache["value"]
    lines, window_is_partial = [], False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            window = min(st.st_size,
                         max(1, int(scan_lines)) * _TIMELINE_BYTES_PER_LINE)
            window_is_partial = window < st.st_size
            if window:
                fh.seek(st.st_size - window)
            if window_is_partial:
                fh.readline()               # discard the partial first line
            lines = fh.read().splitlines()
    except OSError:
        lines = []
    if len(lines) > scan_lines:
        lines = lines[-scan_lines:]
        window_is_partial = True
    events, scanned, hit_bound = [], 0, False
    for line in reversed(lines):
        if len(events) >= max_events:
            hit_bound = True
            break
        scanned += 1
        try:
            e = json.loads(line)
        except Exception:
            continue                        # corrupt line: skip, never 500
        if not isinstance(e, dict):
            continue
        if _timeline_owns(e.get("type")) and _timeline_id_matches(e.get("task"), tid):
            events.append(e)
    events.reverse()                        # the scan walked backwards
    # This call's OWN value is what it returns — never a re-read of the
    # shared dict, which another thread may have replaced while the scan ran.
    value = {"events": events, "scanned": scanned,
             "truncated": bool(hit_bound or window_is_partial)}
    with _timeline_cache_lock:
        _timeline_cache["key"], _timeline_cache["value"] = key, value
    return value


_timeline_cache = {"key": None, "value": None}
# Guards the key/value pair above as ONE snapshot. Dashboard.Handler runs on a
# ThreadingHTTPServer, so the pair is read and written from several threads.
_timeline_cache_lock = threading.Lock()


def _ts_of(v):
    """A sortable epoch from whatever a source records time as.

    events.jsonl writes epochs, but harness_runs and error_events store an ISO
    string (`Store._now`). Sorting the two together needs one type, so an ISO
    timestamp is converted and anything unparsable is None.
    """
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v:
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _timeline_entry(e):
    """One event reduced to the shape the drawer renders, body text inline.

    A driver.done carries a verdict, a task.gate its output tail, a
    task.failed its reason. `body` picks the first of these that is present so
    the page shows WHY an entry matters rather than a JSON blob.
    """
    out = {"kind": "event", "ts": e.get("ts"), "type": e.get("type"),
           "task": e.get("task"), "run_id": e.get("run_id")}
    for k, v in e.items():
        if k not in out:
            out[k] = v
    for k in TIMELINE_BODY_KEYS:
        v = e.get(k)
        if v:
            out["body"] = v if isinstance(v, list) else str(v)[:2000]
            break
    return out


def _timeline(tid, taskfile=None, store=None):
    """Everything recorded about one task, in time order.

    Four sources, one list: this task's events (bounded backward scan of
    events.jsonl), its harness_runs rows (with the transcript path to open),
    its error_events rows with their fingerprints, and its evidence manifests.
    Read-only, and every source is optional — a task that never ran has an
    empty timeline, not an error.
    """
    import errors as _errors_mod
    store = store if store is not None else Handler.store
    ev = _timeline_events(tid)
    entries = [_timeline_entry(e) for e in ev["events"]]
    # harness_runs: rows for the task itself and each `<tid>-x<n>` attempt.
    # harness_runs_prefix does the LIKE, so one query covers both shapes.
    try:
        runs = store.harness_runs_prefix(tid) if store else []
    except Exception:
        runs = []
    runs = [r for r in runs if _timeline_id_matches(r.get("task_id"), tid)]
    for r in runs:
        tpath = r.get("transcript")
        entries.append({
            "kind": "run", "ts": _ts_of(r.get("created_at")), "type": "harness.run",
            "task": r.get("task_id"), "harness": r.get("harness"),
            "model": r.get("model"), "role": r.get("role"),
            "attempt": r.get("attempt"), "exit_code": r.get("exit_code"),
            "seconds": r.get("seconds"),
            "verdict": (r.get("verdict") or "")[:400],
            "transcript": str(tpath) if tpath else None,
            "file": Path(str(tpath)).name if tpath else None,
        })
    # error_events: the fingerprint is the whole point of the table (Rule 7b),
    # so it is carried through and the drawer highlights these rows.
    errs = []
    try:
        errs = _errors_mod.recent(limit=200, task=tid, task_prefix=True)
    except Exception:
        errs = []
    for r in errs:
        entries.append({
            "kind": "error", "ts": r.get("ts"), "type": "error",
            "task": r.get("task"), "fingerprint": r.get("fingerprint"),
            "error_kind": r.get("kind"), "message": r.get("message"),
            "where": r.get("where_"), "model": r.get("model"),
            "node": r.get("node"), "run_id": r.get("run_id"),
            "traceback": (r.get("traceback") or "")[-4000:],
        })
    evidence = _timeline_evidence(tid)
    for man in evidence:
        entries.append({
            "kind": "evidence", "ts": man.get("ts"), "type": "evidence.manifest",
            "task": tid, "attempt": man["attempt"], "project": man["project"],
            "manifest": man["manifest"], "shots": man["shots"],
            "videos": man["videos"], "compare": man["compare"],
            "coverage": man["coverage"], "godot_errors": man["godot_errors"],
            "no_visible_change": man["no_visible_change"],
            "warnings": man["warnings"], "seconds": man["seconds"],
        })
    # Time order, across all four sources. The keys mixed epochs (events) and
    # ISO strings (harness_runs, error_events), which is why _ts_of normalises
    # them first. An entry with no usable timestamp sorts FIRST rather than
    # being dropped — an undated entry is still evidence — and the sort is
    # stable, so equal stamps keep the order they were built in.
    entries.sort(key=lambda x: _ts_of(x.get("ts"))
                 if _ts_of(x.get("ts")) is not None else float("-inf"))
    project = None
    if taskfile:
        try:
            tf = Path(config.TASKS_DIR) / taskfile
            project = json.loads(tf.read_text(encoding="utf-8",
                                              errors="replace"))
        except Exception:
            project = None
    status = None
    try:
        for row in (store.code_tasks_all(2000) if store else []):
            if row.get("id") == tid and (not taskfile
                                         or Path(str(row.get("taskfile") or "")).name == taskfile):
                status = row.get("status")
                break
    except Exception:
        status = None
    return {"id": tid, "taskfile": taskfile, "status": status,
            "title": (project or {}).get("title") if isinstance(project, dict) else None,
            "entries": entries, "counts": {
                "events": len(ev["events"]), "runs": len(runs),
                "errors": len(errs), "evidence": len(evidence)},
            "scanned": ev["scanned"], "truncated": ev["truncated"],
            "scan_limit": TIMELINE_SCAN_LINES, "ts": time.time()}


def _log_tail(log_name, n):
    """Last n lines of logs/<log_name>, [] if unreadable."""
    if not _RUN_LOG_RE.fullmatch(log_name or ""):
        return []
    try:
        lines = (Path(config.ROOT) / "logs" / log_name).read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-n:]


# --- Orchestrator chat + repo listing -------------------------------------
# Rule 6b: the dashboard is unauthenticated, so these routes are deliberately
# narrow — /api/chat/start accepts a repo ONLY as a byte-identical member of
# the /api/repos allowlist and spawns one fixed argv; the message text goes
# into the session jsonl and the model prompt, never into a shell string.

_SESSION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")       # orchchat.SESSION_RE
_REPO_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


def _chat_dir():
    """$ARC_CHAT_DIR, default logs/chat — resolved at call time, same rule
    as orchchat.chat_dir, so both processes always see the same sessions."""
    return Path(os.getenv("ARC_CHAT_DIR") or Path.cwd() / "logs" / "chat")


def _repos_dir():
    """$ARC_REPOS_DIR, default ~/repos — the root GET /api/repos scans."""
    return Path(os.getenv("ARC_REPOS_DIR") or str(Path.home() / "repos")).expanduser()


def _git_quick(path, *args):
    """git stdout with a hard 2 s cap, None on any failure — one wedged or
    half-built repo must never slow down the whole scan."""
    try:
        r = subprocess.run(["git", "-C", str(path), *args],
                           capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def _taskfiles_on(path):
    """How many taskfiles in TASKS_DIR target this checkout. The repo picker
    needs it to say how much governed work lives on each repo; a taskfile
    that will not parse simply does not count."""
    try:
        files = list(Path(config.TASKS_DIR).glob("*.json"))
    except OSError:
        return 0
    n = 0
    for f in files:
        try:
            repo = json.loads(f.read_text(encoding="utf-8"))["project"]["repo"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if Path(repo).resolve() == Path(path).resolve():
            n += 1
    return n


def _repo_entry(name, path):
    branch = _git_quick(path, "rev-parse", "--abbrev-ref", "HEAD")
    remotes = _git_quick(path, "remote")
    url = _git_quick(path, "remote", "get-url", "origin")
    head = _git_quick(path, "log", "-1", "--format=%ct %s")
    last_commit = last_subject = None
    if head:
        ts, _, subject = head.strip().partition(" ")
        if ts.isdigit():
            last_commit = int(ts)
            last_subject = subject.strip()[:80] or None
    return {"name": name, "path": str(path),
            "branch": (branch or "").strip(),
            "remote": bool((remotes or "").strip()),
            "remote_url": url.strip() if url else None,
            "last_commit": last_commit,
            "last_subject": last_subject,
            "projects": _taskfiles_on(path)}


def _list_repos():
    """The repo allowlist: this repo first, then every git checkout directly
    under ARC_REPOS_DIR, sorted by name."""
    repos = [_repo_entry("arc-orchestrator", config.ROOT)]
    try:
        children = sorted((c for c in _repos_dir().iterdir() if c.is_dir()),
                          key=lambda c: c.name)
    except OSError:
        children = []
    for c in children:
        if (c / ".git").exists():
            repos.append(_repo_entry(c.name, c))
    return repos


# Module-level hook so tests patch remote creation and never hit GitHub.
# The 09-12 minecraft-test run threw away eight minutes of model work because
# publish found no remote the dashboard could have created at repo birth.
_ensure_remote = lambda path, name=None, private=True: asyncio.run(
    gitstore.ensure_remote(path, name, private))


def _short_remote(url):
    """github.com/owner/name — the note shows the short form, not the URL."""
    return re.sub(r"^(https?://|git@)", "", url).removesuffix(".git")


def _create_repo(body):
    if not isinstance(body, dict):
        return {"error": "JSON body required"}, 400
    name = body.get("name")
    if not isinstance(name, str) or not _REPO_NAME_RE.fullmatch(name):
        return {"error": "bad repo name (expected ^[a-z0-9][a-z0-9-]{0,40}$)"}, 400
    private = body.get("private", True)
    if not isinstance(private, bool):
        return {"error": "private must be a boolean"}, 400
    want_remote = body.get("remote", True)
    if not isinstance(want_remote, bool):
        return {"error": "remote must be a boolean"}, 400
    path = _repos_dir() / name
    if path.exists():
        return {"error": f"{path} already exists", "exists": True}, 409
    # Local first: git init + one initial commit. The GitHub remote comes
    # after, best-effort — a machine without gh still gets its local repo.
    try:
        path.mkdir(parents=True)
        (path / "README.md").write_text(f"# {name}\n", encoding="utf-8")
        for argv in (["git", "init", "-q", "-b", "main"],
                     ["git", "add", "README.md"],
                     ["git", "-c", "user.name=arc-orchestrator",
                      "-c", "user.email=arc-orchestrator@localhost",
                      "commit", "-q", "-m", "init"]):
            r = subprocess.run(argv, cwd=path, capture_output=True, text=True,
                               timeout=30)
            if r.returncode != 0:
                raise RuntimeError(f"{' '.join(argv[:2])} failed: {r.stderr.strip()}")
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
        import shutil
        shutil.rmtree(path, ignore_errors=True)  # a failed repo retries by name
        return {"error": f"git setup failed: {exc}"}, 500
    remote_url, remote_note = None, None
    if want_remote:
        try:
            ok, url_or_reason = _ensure_remote(path, name, private)
        except Exception as exc:  # a broken gh must not bury the local repo
            ok, url_or_reason = False, f"ensure_remote failed: {exc}"[:200]
        if ok:
            remote_url = url_or_reason
            remote_note = f"created {_short_remote(url_or_reason)}"
        else:
            # NOT a 500: the local repo stays, and `code run` retries the
            # remote itself before ever refusing (main.py).
            remote_note = f"{url_or_reason} — local only"
    return {"name": name, "path": str(path),
            "remote_url": remote_url, "remote_note": remote_note}, 200


def _repo_remote(body):
    """POST /api/repos/remote — create the GitHub remote for an EXISTING
    checkout. Same allowlist rule as /api/chat/start (Rule 6b): the repo must
    be one of the /api/repos entries, never an arbitrary path."""
    if not isinstance(body, dict):
        return {"error": "JSON body required"}, 400
    allowed = {r["path"] for r in _list_repos()}
    repo = body.get("repo")
    if not isinstance(repo, str) or repo not in allowed:
        return {"error": "repo is not one of the /api/repos entries"}, 400
    private = body.get("private", True)
    if not isinstance(private, bool):
        return {"error": "private must be a boolean"}, 400
    try:
        ok, url_or_reason = _ensure_remote(repo, Path(repo).name, private)
    except Exception as exc:
        return {"error": f"ensure_remote failed: {exc}"[:400]}, 500
    if ok:
        return {"remote_url": url_or_reason,
                "note": f"created {_short_remote(url_or_reason)}"}, 200
    return {"remote_url": None, "note": url_or_reason}, 200


def _chat_key(session):
    return f"chat:{session}"


def _chat_running(session):
    _prune_registry()  # reaps dead pids, chat entries included
    return _chat_key(session) in _launch_registry


def _chat_start(body):
    if not isinstance(body, dict):
        return {"error": "JSON body required"}, 400
    session = body.get("session")
    if not isinstance(session, str) or not _SESSION_RE.fullmatch(session):
        return {"error": "bad session id (expected ^[a-z0-9][a-z0-9-]{0,39}$)"}, 400
    message = body.get("message")
    if not isinstance(message, str) or not 1 <= len(message) <= 8000:
        return {"error": "message must be a string of 1..8000 characters"}, 400
    repo = body.get("repo")
    allowed = {r["path"] for r in _list_repos()}
    if not isinstance(repo, str) or repo not in allowed:
        return {"error": "repo is not one of the /api/repos entries"}, 400
    if _chat_running(session):
        return {"error": "a chat turn is already running for this session"}, 409
    path = _chat_dir() / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"role": "user", "ts": time.time(),
                            "text": message}) + "\n")
    argv = [str(Path(config.ROOT) / ".venv" / "bin" / "python"), "main.py",
            "chat", "--session", session, "--repo", repo]
    proc, log_name = _spawn_logged(argv, f"chat-{session}.log")
    _launch_registry[_chat_key(session)] = {
        "pid": proc.pid, "log": log_name, "started": time.time(), "kind": "chat"}
    return {"pid": proc.pid}, 200


def _chat_poll(session, since):
    path = _chat_dir() / f"{session}.jsonl"
    turns = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line:
                turns.append(json.loads(line))
    except (OSError, ValueError):
        turns = []
    taskfile = next((t["taskfile"] for t in reversed(turns)
                     if t.get("role") == "assistant" and t.get("taskfile")), None)
    return {"turns": turns[since:], "running": _chat_running(session),
            "taskfile": taskfile}


def _chat_sessions():
    """GET /api/chat/sessions — every readable session, newest first.

    Read-only and parameterless, so it cannot widen the unauthenticated
    surface Rule 6b describes. The listing lives in orchchat.list_sessions so
    the dashboard and `main.py chat` agree on what a session file is; this
    wrapper adds the one guarantee the picker needs from the route: it never
    fails. An unreadable chat dir is an empty list, not a 500.
    """
    try:
        return {"sessions": orchchat.list_sessions()}
    except Exception:                # a listing must never break the picker
        return {"sessions": []}


def _captain_dir():
    """$ARC_CAPTAIN_DIR, default logs/captain — resolved at call time, same
    rule as captain.captain_dir, so both processes agree on the sessions."""
    return Path(os.getenv("ARC_CAPTAIN_DIR")
                or Path.cwd() / "logs" / "captain")


def _captain_key(session):
    return f"captain:{session}"


def _captain_running(session):
    _prune_registry()
    return _captain_key(session) in _launch_registry


def _captain_state():
    """GET /api/captain/state — the live snapshot the panel renders.

    Read-only. Never fails: a broken db or a missing log is empty state, not a
    500, because the panel polls it continuously.
    """
    try:
        import captain
        state = captain.fleet_state()
    except Exception:
        state = {}
    try:
        sessions = _captain_sessions()["sessions"]
    except Exception:
        sessions = []
    return {"state": state, "sessions": sessions, "autopilot": _autopilot_view()}


def _autopilot_view():
    """GET /api/captain/autopilot — the autopilot's state, latest findings,
    recent actions and unacknowledged escalations. Never raises."""
    try:
        import captain_autopilot
        return captain_autopilot.view()
    except Exception:
        return {"paused": False, "running": False, "findings": [], "actions": [],
                "escalations": []}


def _autopilot_pause(body):
    """POST /api/captain/autopilot/pause {"paused": bool} — toggles the pause
    file. Guarded like every POST (_refuse_post: JSON, same origin, and
    ARC_DASHBOARD_TOKEN when set)."""
    if not isinstance(body, dict) or not isinstance(body.get("paused"), bool):
        return {"error": "body must be {\"paused\": true|false}"}, 400
    import captain_autopilot
    return {"paused": captain_autopilot.set_paused(body["paused"], by="dashboard")}, 200


def _autopilot_ack(body):
    """POST /api/captain/autopilot/ack {"id": "<escalation id>"}."""
    esc = body.get("id") if isinstance(body, dict) else None
    import captain_autopilot
    if not captain_autopilot.ack_escalation(esc, by="dashboard"):
        return {"error": "no such escalation"}, 404
    return {"ok": True, "id": esc}, 200


def _captain_sessions():
    """Captain sessions, newest first — same tolerant listing as chat, but
    over captain.captain_dir(). Never raises."""
    try:
        import captain as _cap
        d = _cap.captain_dir()
        out = []
        for path in sorted(d.iterdir()):
            name = path.name
            if not name.endswith(".jsonl") or name == "queue.jsonl":
                continue
            name = name[: -len(".jsonl")]
            if not _SESSION_RE.fullmatch(name) or not path.is_file():
                continue
            turns = orchchat._read_turns(path)
            if not turns:
                continue
            out.append({"name": name, "turns": len(turns),
                        "mtime": path.stat().st_mtime})
        out.sort(key=lambda s: (s["mtime"], s["name"]), reverse=True)
        return {"sessions": out}
    except Exception:
        return {"sessions": []}


def _captain_start(body):
    """POST /api/captain/start — append the operator turn and spawn one
    captain turn. Same allowlist discipline as /api/chat/start (Rule 6b):
    the repo must be a byte-identical member of /api/repos and the spawned
    argv is fixed."""
    if not isinstance(body, dict):
        return {"error": "JSON body required"}, 400
    session = body.get("session")
    if not isinstance(session, str) or not _SESSION_RE.fullmatch(session):
        return {"error": "bad session id (expected ^[a-z0-9][a-z0-9-]{0,39}$)"}, 400
    message = body.get("message")
    if not isinstance(message, str) or not 1 <= len(message) <= 8000:
        return {"error": "message must be a string of 1..8000 characters"}, 400
    repo = body.get("repo")
    allowed = {r["path"] for r in _list_repos()}
    if not isinstance(repo, str) or repo not in allowed:
        return {"error": "repo is not one of the /api/repos entries"}, 400
    if _captain_running(session):
        return {"error": "a captain turn is already running for this session"}, 409
    path = _captain_dir() / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"role": "user", "ts": time.time(),
                            "text": message}) + "\n")
    argv = [str(Path(config.ROOT) / ".venv" / "bin" / "python"), "main.py",
            "captain", "--session", session, "--repo", repo]
    proc, log_name = _spawn_logged(argv, f"captain-{session}.log")
    _launch_registry[_captain_key(session)] = {
        "pid": proc.pid, "log": log_name, "started": time.time(),
        "kind": "captain"}
    return {"pid": proc.pid}, 200


def _captain_running_transcript(session):
    """The live transcript of the captain turn running for `session`, or None.

    A captain turn runs `drivers.Driver._once` with task_id
    ``captain-<session>`` and role ``planner`` (captain.run_turn), so its live
    output streams to the deterministic name
    ``captain-<session>-planner-<attempt>.jsonl`` under logs/harness. The newest
    such file is what the operator wants to see while the turn is in flight.
    """
    d = Path(config.ROOT) / "logs" / "harness"
    prefix = f"captain-{session}-planner-"
    best = None
    try:
        for p in d.glob(prefix + "*.jsonl"):
            if not _TRANSCRIPT_RE.fullmatch(p.name):
                continue
            m = p.stat().st_mtime
            if best is None or m > best[1]:
                best = (p.name, m)
    except OSError:
        return None
    return best[0] if best else None


def _captain_thinking(session, tail=12):
    """The running turn's live thinking: {pending, blocks} or None.

    Reuses the transcript activity reducer, the SAME reader the transcript
    drawer uses, so the captain panel shows one folded "thought" line plus the
    last few readable blocks (tool calls, results) as they stream — no second
    parser to drift from the drawer's.
    """
    fname = _captain_running_transcript(session)
    if fname is None:
        return None
    out = _transcript_activity_view(fname, tail)
    if out is None:
        # Non-reducer shape (kimi-like) or unreadable: fall back to the raw tail
        # so the operator still sees SOMETHING live rather than a spinner.
        obj, _code = _transcript_tail(fname, tail)
        if isinstance(obj, dict) and obj.get("lines"):
            return {"pending": "", "blocks": obj["lines"]}
        return None
    obj, _code = out
    blocks = [b for b in (obj.get("blocks") or []) if not _captain_prompt_echo(b)]
    return {"pending": obj.get("pending") or "", "blocks": blocks}


# The captain's own prompt is echoed back by the harness as its first `text`
# record, so the reduced blocks would open with a wall of persona prose. It is
# recognizable by a phrase that appears ONLY in the injected prompt.
_CAPTAIN_ECHO_MARK = "You are the CAPTAIN of the ARC multi-model coding fleet"


def _captain_prompt_echo(block):
    """True when a reduced block is the echoed captain persona prompt."""
    return isinstance(block, str) and _CAPTAIN_ECHO_MARK in block


def _captain_poll(session, since):
    path = _captain_dir() / f"{session}.jsonl"
    turns = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line:
                turns.append(json.loads(line))
    except (OSError, ValueError):
        turns = []
    running = _captain_running(session)
    resp = {"turns": turns[since:], "running": running}
    if running:
        thinking = _captain_thinking(session)
        if thinking is not None:
            resp["thinking"] = thinking
    return resp


def _captain_queue():
    """GET /api/captain/queue — runs waiting on capacity.

    Live rows come from the durable workqueue. JSONL lines that predate it
    still show. Never raises.
    """
    try:
        import captain
        return captain.queue_view()
    except Exception:
        return {"queued": []}


_HEALTH_PROBLEMS = ("driver.error", "driver.stalled", "driver.timeout",
                    "driver.cap_wait", "inflight.over_cap", "task.failed",
                    "task.conflict", "graph.draining", "run.interrupted")

# A heartbeat (driver.progress, cap_wait) proves the fleet is ALIVE, not that
# it is MOVING. Only these events mean a unit of work actually advanced.
_PROGRESS_EVENTS = ("node_end", "task.gate", "task.merged", "driver.done")

# Nothing legitimate goes quiet for longer than one harness attempt's total
# budget plus gate and retry slack — when budgets are finite. With unlimited
# budgets (the default) the stale-driver bound stands in, or every long
# implement node reads as stalled — the false alarm that gets a watchdog
# ignored. Override with ARC_STALL_THRESHOLD_S.
WATCHDOG_STALL_S = float(os.getenv(
    "ARC_STALL_THRESHOLD_S", str(int(DRIVER_STALE_S) + 900)))


def _stall_diagnosis(q, live_runs, running_rows, stalled_for_s):
    """Name the resource a stalled fleet is stuck behind, from queue evidence.

    Ordered most-specific first; every branch is something the operator can
    click through in the same dashboard (queue view, health strip, task list).
    """
    totals = q["totals"]
    # Dead run: nothing running, nothing waiting, no live process — but the
    # store still has rows marked running. Those tasks never move again on
    # their own; the fix is resuming the taskfile, not raising any cap.
    if not live_runs and not totals["running"] and not totals["waiting"]:
        return (f"the run process is dead: {running_rows} task(s) still "
                f"marked running but no run process is alive")
    # One saturated harness with model slots free: the shared harness process
    # pool is the binding ceiling, not any model's cap — the layer people
    # forget (Rule 6). Raising model caps here fixes nothing.
    for h in q["harnesses"]:
        if h["waiting"] and h["running"] >= h["cap"] \
                and any(m["waiting"] and m["free"] for m in q["models"]):
            return (f"the {h['harness']} harness is saturated "
                    f"({h['running']}/{h['cap']}) while model slots are free")
    # Every model that has queued work is at its own cap: fleet saturation.
    queued = [m for m in q["models"] if m["waiting"]]
    if queued and all(m["running"] >= m["cap"] for m in queued):
        return (f"every model with queued work is at its cap "
                f"({sum(m['waiting'] for m in queued)} waiting)")
    # Drivers queued with none started and no cap full: the queue itself.
    if not totals["running"] and totals["waiting"]:
        return f"all {totals['waiting']} driver(s) are queued and none has started"
    return (f"no node has finished for {int(stalled_for_s)}s with "
            f"{totals['running']} driver(s) running")


def _watchdog(store, live_runs):
    """Is the fleet advancing, and if not, what is it stuck behind?

    The queue view (who holds what, who waits) shows a fleet that can look
    busy while every driver sits queued behind a saturated cap. This adds
    the time dimension: when did a unit of work last actually COMPLETE, is
    that longer ago than WATCHDOG_STALL_S, and if so, the single most
    likely resource it is stuck behind.

    `progress` is the ts of the newest _PROGRESS_EVENTS entry; if the log
    records no advance at all, the log's oldest event bounds it from below —
    a wedged fleet must not read as "unknown". IDLE is not STALLED: an empty
    fleet with no work is healthy, and calling it a stall is the false alarm
    that gets a watchdog ignored.
    """
    now = time.time()
    q = _queue(store)
    totals = q["totals"]
    try:
        running_rows = len(list(store.running_code_tasks() or [])) if store else 0
    except Exception:
        running_rows = 0

    last = None
    for line in reversed(_load_event_lines()):
        if not any(f'"{t}"' in line for t in _PROGRESS_EVENTS):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") in _PROGRESS_EVENTS:
            last = _ts(e.get("ts"))
            if last:
                break
    if last is None:
        lines = _load_event_lines()
        if lines:
            try:
                last = _ts(json.loads(lines[0]).get("ts"))
            except (ValueError, TypeError, AttributeError):
                last = None

    stalled_for_s = round(now - last, 1) if last else None
    has_work = bool(totals["running"] or totals["waiting"] or live_runs
                    or running_rows)
    stalled = bool(has_work and stalled_for_s is not None
                   and stalled_for_s >= WATCHDOG_STALL_S)

    if not has_work:
        state, diagnosis = "idle", "nothing to do: no work in flight"
    elif stalled:
        state, diagnosis = "stalled", _stall_diagnosis(
            q, live_runs, running_rows, stalled_for_s)
    else:
        state, diagnosis = "moving", ""
    return {"progress": last, "stalled_for_s": stalled_for_s,
            "stalled": stalled, "state": state, "diagnosis": diagnosis,
            "threshold_s": WATCHDOG_STALL_S}


# The revision this PROCESS is serving, captured once at import. The fleet
# edits this server's own source and merges it while the server runs; a running
# process keeps serving what it loaded, so a newly merged route 404s and a
# renamed function takes the page blank. The UI has had a banner for this since
# c59a76f — reading a field no commit ever produced. Half a feature is the
# same as none, and this is the half that was missing.
_SERVED_AT = time.time()


def _git_out(*args):
    try:
        r = subprocess.run(["git", "-C", str(config.ROOT), *args],
                           capture_output=True, text=True, timeout=5)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


_SERVED_HEAD = _git_out("rev-parse", "HEAD").strip() or None
_stale_cache = {"key": 0.0, "files": []}
_SOURCE_PATHS = ("dashboard.py", "static/", "config.py", "store.py", "reconcile.py")


def _stale_source(now=None):
    """Source files that changed in the repo AFTER this process started serving.

    Compares the HEAD captured at import against the repo's HEAD now, over the
    files this server actually executes or serves. Cached ~10 s: it shells out,
    and the health endpoint is polled. An empty list when git is unavailable is
    honest here — with no repo there is nothing to be stale relative to.
    """
    now = now if now is not None else time.time()
    if now - _stale_cache["key"] < 10:
        return _stale_cache["files"]
    files = []
    if _SERVED_HEAD:
        head = _git_out("rev-parse", "HEAD").strip()
        if head and head != _SERVED_HEAD:
            out = _git_out("diff", "--name-only", _SERVED_HEAD, head, "--",
                           *_SOURCE_PATHS)
            files = sorted({ln.strip() for ln in out.splitlines() if ln.strip()})
    _stale_cache.update(key=now, files=files)
    return files


_httpd = None
_restarting = False


def _graceful_reexec():
    global _httpd, _restarting
    log.info("Graceful restart requested (PID %d); closing server and re-executing...", os.getpid())
    if _httpd:
        try:
            _httpd.server_close()
        except Exception as exc:
            log.warning("error closing httpd socket: %s", exc)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        logging.shutdown()
    except Exception:
        pass
    if sys.argv and sys.argv[0].endswith(".py"):
        script = str(Path(sys.argv[0]).resolve())
        args = [sys.executable, script] + sys.argv[1:]
    else:
        args = [sys.executable] + sys.argv
    os.execv(sys.executable, args)


_reexec_fn = _graceful_reexec
_restart_timer = None


def _restart(body):
    """POST /api/restart — graceful restart of the dashboard server.

    Re-execs this process in-place using os.execv so fresh bytecode and static
    files from the latest git HEAD are loaded. Preserves PID, file descriptors
    (logs/server.log), and environment.
    """
    global _restarting, _restart_timer
    if not isinstance(body, dict):
        return {"error": "JSON body required"}, 400
    if _restarting:
        return {"ok": True, "status": "already_restarting"}, 200
    _prune_registry()
    active_interactive = [
        k for k, v in _launch_registry.items()
        if v.get("kind") in ("chat", "captain", "plan")
    ]
    force = bool(body.get("force"))
    if active_interactive and not force:
        return {
            "error": "An interactive turn is in progress (chat/captain/plan); pass force: true to restart anyway",
            "active": True,
            "sessions": active_interactive,
        }, 409

    _restarting = True

    def _trigger():
        if _httpd:
            try:
                _httpd.shutdown()
            except Exception:
                pass
        elif _reexec_fn != _graceful_reexec:
            _reexec_fn()

    if _restart_timer:
        try:
            _restart_timer.cancel()
        except Exception:
            pass
    _restart_timer = threading.Timer(0.3, _trigger)
    _restart_timer.start()
    return {"ok": True, "status": "restarting", "pid": os.getpid()}, 200


_arc_cache = {"key": 0.0, "value": None}


def _arc_status(now=None):
    """Is the ARC API reachable from here — i.e. is the VPN up? Cached 20 s."""
    now = now if now is not None else time.time()
    if _arc_cache["value"] is not None and now - _arc_cache["key"] < 20:
        return _arc_cache["value"]
    try:
        import drivers
        up, detail = drivers.arc_reachable(timeout=4.0)
    except Exception as exc:
        up, detail = None, f"probe failed: {exc}"[:120]
    val = {"reachable": up, "detail": detail, "checked_at": now}
    _arc_cache.update(key=now, value=val)
    return val


def _health(store):
    """Small, cheap fleet-health payload for the Projects page.

    The same facts live in /api/usage, but that response is ~34KB and is only
    fetched by the Usage page — so the one screen an operator actually watches
    showed nothing when the fleet was wedged at its concurrency cap.
    """
    now = time.time()
    inflight, _kimi = _collect_inflight(now, store)
    import reconcile
    import drivers

    # Two different caps govern the same model and must not be conflated:
    #   driver_cap  — how many harness instances THIS fleet may run
    #                 (config.driver_limit), deliberately below the account cap
    #                 so interactive use still has room;
    #   account_cap — ARC's per-account limit (config.family_limit), which the
    #                 fleet's drivers AND any interactive session on the same
    #                 account (historically the operator's kimi-code CLI) both
    #                 consume.
    # Counting interactive sessions against driver_cap once reported a model
    # at "4/2 OVER CAP" while the fleet was correctly running one driver.
    per_model = {}

    def ent_for(model, family=None):
        return per_model.setdefault(model, {
            "model": model, "pretty": _pretty(model),
            "family": config.MODEL_FAMILY.get(model, family or "harness"),
            "drivers": 0, "account": 0, "driver_cap": None, "account_cap": None,
            "oldest_s": 0.0})

    for model in config.IMPLEMENTER_MODELS:
        ent_for(model)          # always show every governed model, even at 0
    for row in inflight:
        model = row.get("model")
        if not model:
            continue
        ent = ent_for(model, row.get("family"))
        ent["account"] += 1
        if str(row.get("source") or "").startswith("driver:"):
            ent["drivers"] += 1
        ent["oldest_s"] = max(ent["oldest_s"], row.get("elapsed_s") or 0.0)
    for ent in per_model.values():
        try:
            ent["driver_cap"] = config.driver_limit(ent["model"])
        except Exception:
            ent["driver_cap"] = None
        try:
            ent["account_cap"] = config.family_limit(ent["family"])
        except Exception:
            ent["account_cap"] = None
        dcap, acap = ent["driver_cap"], ent["account_cap"]
        ent["at_cap"] = bool(dcap and ent["drivers"] >= dcap)
        ent["over_cap"] = bool((dcap and ent["drivers"] > dcap)
                               or (acap and ent["account"] > acap))
        ent["account_at_cap"] = bool(acap and ent["account"] >= acap)

    problems = []
    for line in reversed(_load_event_lines()):
        if not any(t in line for t in ('"driver.', '"task.failed"', '"task.conflict"',
                                       '"inflight.', '"graph.', '"run.interrupted"')):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") not in _HEALTH_PROBLEMS:
            continue
        problems.append({k: e[k] for k in
                         ("type", "ts", "model", "harness", "role", "task", "attempt",
                          "error", "note", "reason", "family", "inflight", "limit",
                          "in_use", "cap", "idle_s", "capacity", "taskfile",
                          "blocked", "wire", "state", "cpu_delta_s", "bytes",
                          "last_activity")
                         if k in e})
        if len(problems) >= 25:
            break

    try:
        leases = store.driver_lease_rows() if store else []
    except Exception:
        leases = []
    runs = reconcile.live_runs()
    return {"now": now, "models": sorted(per_model.values(),
                                         key=lambda m: (-m["account"], m["model"])),
            "agents": inflight, "runs": runs,
            "watchdog": _watchdog(store, runs),
            "leases": leases, "problems": problems,
            "stale_source": _stale_source(now),
            # The VPN expiring is the one outage that makes the whole fleet look
            # idle rather than broken: every driver waits for ARC, nothing runs,
            # nothing errors. An operator staring at zeros needs to be told why.
            "arc": _arc_status(now),
            "plans": drivers.active_plan_windows(_load_event_lines(), now),
            "served_head": (_SERVED_HEAD or "")[:12], "served_at": _SERVED_AT}


def _metrics(store):
    """Per-model success/cost rollup + code-task outcome counts."""
    models = {}

    def ent_for(model):
        return models.setdefault(model, {
            "model": model, "pretty": _pretty(model), "runs": 0, "ok": 0,
            "failed": 0, "avg_seconds": 0.0, "total_tokens": 0,
            "avg_tokens_per_run": 0.0, "stall_count": 0,
            "termination_count": 0})

    try:
        rows = store.harness_runs_all() if store else []
    except Exception:
        rows = []
    secs = {}
    for row in rows:
        model = row.get("model")
        if not model:
            continue
        ent = ent_for(model)
        ent["runs"] += 1
        try:
            code = int(row.get("exit_code"))
        except (TypeError, ValueError):
            code = None
        if code == 0:
            ent["ok"] += 1
        else:
            ent["failed"] += 1
        secs.setdefault(model, []).append(row.get("seconds") or 0.0)
        tok, _p, _c = _transcript_toks(row.get("transcript"))
        ent["total_tokens"] += tok or 0
    for model, ent in models.items():
        ss = secs.get(model) or []
        ent["avg_seconds"] = round(sum(ss) / len(ss), 3) if ss else 0.0
        if ent["runs"]:
            ent["avg_tokens_per_run"] = round(ent["total_tokens"] / ent["runs"], 1)
    for line in _load_event_lines():
        if '"driver.stalled"' not in line and '"driver.timeout"' not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        et = e.get("type")
        if et not in ("driver.stalled", "driver.timeout"):
            continue
        model = e.get("model")
        if not model:
            continue
        ent = ent_for(model)
        if et == "driver.stalled":
            ent["stall_count"] += 1
        else:
            ent["termination_count"] += 1

    tasks = {"total": 0, "merged": 0, "failed": 0, "conflict": 0, "merge_rate": 0.0}
    try:
        trows = store.code_tasks_all() if store else []
    except Exception:
        trows = []
    for row in trows:
        tasks["total"] += 1
        if row.get("status") in ("merged", "failed", "conflict"):
            tasks[row["status"]] += 1
    if tasks["total"]:
        tasks["merge_rate"] = round(tasks["merged"] / tasks["total"], 4)

    return {"now": time.time(), "models": sorted(
        models.values(), key=lambda m: (-m["runs"], m["model"])), "tasks": tasks}


def _archive_project(body):
    """Archive or restore a project. The task file is never touched."""
    if not isinstance(body, dict):
        return {"error": "JSON body required"}, 400
    fname = body.get("file") or ""
    if not re.fullmatch(r"[\w.-]+\.json", fname):
        return {"error": "bad file name"}, 400
    path = Path(config.TASKS_DIR) / fname
    if not path.is_file():
        return {"error": "not found"}, 404
    archived = bool(body.get("archived", True))
    import reconcile
    if archived and any(r.get("taskfile") and Path(r["taskfile"]).name == fname
                        for r in reconcile.live_runs()):
        return {"error": "this project is running — stop it before archiving"}, 409
    try:
        Handler.store.set_project_archived(str(path), archived)
    except Exception as exc:
        return {"error": str(exc)}, 500
    _emit_event("project.archived" if archived else "project.restored",
                taskfile=str(path))
    resp = {"file": fname, "archived": archived}
    if archived:
        try:
            bad = [r for r in Handler.store.code_tasks_all()
                   if r.get("taskfile")
                   and (r["taskfile"] == str(path) or r["taskfile"].endswith("/" + fname))
                   and r.get("status") in ("failed", "conflict")]
        except Exception:
            bad = []
        if bad:
            resp["warning"] = (f"{len(bad)} task(s) ended failed/conflict — "
                               "archiving hides this project from the active list")
    return resp, 200


def _retry_task(body):
    """Reset ONE task of a task file to 'pending' so the next `code run`
    re-executes it; every other task keeps its recorded status."""
    if not isinstance(body, dict):
        return {"error": "JSON body required"}, 400
    fname = body.get("file") or ""
    if not re.fullmatch(r"[\w.-]+\.json", fname):
        return {"error": "bad file name"}, 400
    tid = body.get("task") or ""
    path = Path(config.TASKS_DIR) / fname
    if not path.is_file():
        return {"error": "not found"}, 404
    # rows may be keyed by the resolved path or a bare name — match the way
    # _project_detail does, then re-upsert under the row's own key so the
    # ON CONFLICT(taskfile, id) hits the existing row.
    row = next((r for r in Handler.store.code_tasks_all()
                if r.get("taskfile")
                and (r["taskfile"] == str(path)
                     or r["taskfile"].endswith("/" + fname))
                and r.get("id") == tid), None)
    if row is None:
        return {"error": "task id not found"}, 404
    import reconcile
    if any(r.get("taskfile") and Path(r["taskfile"]).name == fname
           for r in reconcile.live_runs()):
        return {"error": "this project is running — stop it before retrying"}, 409
    Handler.store.upsert_code_task(row["taskfile"], tid, row["title"],
                                   row["model"], row["reviewer"], "pending")
    _emit_event("task.reset", taskfile=str(path), task=tid)
    # Resetting to `pending` used to be the whole action, on the theory that
    # "the next `code run` re-executes it". Nothing schedules that run. A retry
    # clicked while the fleet was idle changed a label and did nothing else —
    # graph-admission-control sat `pending` for nine hours that way. If no run
    # holds this task file, start one; if one does, it will pick the reset up.
    launched, launch_note = None, "a run already holds this task file"
    import reconcile
    if not any(r.get("taskfile") and Path(r["taskfile"]).name == fname
               for r in reconcile.live_runs()):
        res, code = _run_project({"file": fname})
        if code == 200:
            launched, launch_note = res.get("pid"), "started a run for it"
        else:
            launch_note = f"could not start a run: {res.get('error')}"
    return {"file": fname, "task": tid, "status": "pending",
            "launched_pid": launched, "note": launch_note}, 200


def _escalate_task(body):
    """Move ONE task to a stronger model, by the operator's judgement.

    The fix budget escalates only after repeated failure. The operator can see
    a task struggling well before that — an implementer looping on a design
    problem it cannot hold in context, a task whose tier was planned too
    optimistically — and should not have to burn three rounds to prove it.

    Two writes so the change sticks in both worlds:
      - a `model_overrides` row, which cur_model() reads at every node boundary,
        so a RUNNING task moves up at its very next step without a restart;
      - the taskfile's `model` field, so a fresh run of the project starts on
        the new model rather than rediscovering the problem from the bottom.
    The reviewer follows automatically: it is derived from the implementer's
    family at every call, so a cross-family reviewer stays cross-family.
    """
    import code_tasks
    if not isinstance(body, dict):
        return {"error": "JSON body required"}, 400
    fname = body.get("file") or ""
    if not re.fullmatch(r"[\w.-]+\.json", fname):
        return {"error": "bad file name"}, 400
    tid = body.get("task") or ""
    path = Path(config.TASKS_DIR) / fname
    if not path.is_file():
        return {"error": "not found"}, 404
    try:
        doc = json.loads(path.read_text())
        tasks = doc["project"]["tasks"]
    except (ValueError, KeyError, TypeError) as exc:
        return {"error": f"taskfile unreadable: {exc}"[:200]}, 500
    t = next((x for x in tasks if x.get("id") == tid), None)
    if t is None:
        return {"error": "task id not found"}, 404

    row = next((r for r in Handler.store.code_tasks_all()
                if r.get("taskfile") and Path(r["taskfile"]).name == fname
                and r.get("id") == tid), None)
    current = (Handler.store.get_model_override(row["taskfile"], tid) if row else None) \
        or (row or {}).get("model") or t.get("model")
    target = body.get("to_model")
    if not target:
        target = code_tasks._next_tier(current)
        if target is None:
            return {"error": f"{current} is already the top tier"}, 409
    if target not in config.IMPLEMENTER_MODELS:
        return {"error": f"unknown model {target!r}; choose from "
                         f"{sorted(config.IMPLEMENTER_MODELS)}"}, 400
    # Escalation is monotonic: refuse to move DOWN a tier.
    ci, ni = code_tasks._tier_index(current), code_tasks._tier_index(target)
    ci = -1 if ci is None else ci
    ni = -1 if ni is None else ni
    if ni <= ci and target != current:
        return {"error": f"{target} is not above {current}; escalation only moves up"}, 409
    if target == current:
        return {"error": f"already on {current}"}, 409

    reason = (body.get("reason") or "operator escalation")[:200]
    key = row["taskfile"] if row else str(path)
    Handler.store.set_model_override(key, tid, target, reason)
    # the taskfile too, so a fresh run starts here
    t["model"] = target
    t["reviewer"] = code_tasks._reviewer_for(t, target)
    _write_taskfile_atomically(path, doc)
    if row:
        Handler.store.upsert_code_task(row["taskfile"], tid, row["title"], target,
                                       t.get("reviewer") or row.get("reviewer"),
                                       row.get("status") or "pending")
    _emit_event("task.escalated", taskfile=str(path), task=tid, from_model=current,
                to_model=target, manual=True, reason=reason)
    import reconcile
    live = any(r.get("taskfile") and Path(r["taskfile"]).name == fname
               for r in reconcile.live_runs())
    return {"file": fname, "task": tid, "from": current, "to": target,
            "reviewer": t.get("reviewer"),
            "note": ("a run is live — it will use the new model at its next step"
                     if live else "no run is live — the next run starts on the new model"),
            "live": live}, 200


def _stop_project(body):
    """SIGTERM every `code run` process owning this task file.

    SIGTERM (not KILL) on purpose: the run installs a handler that cancels the
    graph, kills its harness children, marks unfinished tasks failed and
    releases its driver leases. A KILL would skip all of that and leave exactly
    the orphans `code reconcile` exists to clean up.
    """
    if not isinstance(body, dict):
        return {"error": "JSON body required"}, 400
    fname = body.get("file") or ""
    if not re.fullmatch(r"[\w.-]+\.json", fname):
        return {"error": "bad file name"}, 400
    path = Path(config.TASKS_DIR) / fname
    import reconcile
    targets = [r for r in reconcile.live_runs()
               if r.get("taskfile") and Path(r["taskfile"]).name == fname]
    if not targets:
        _prune_registry()
        return {"error": "no run process is active for this task file",
                "stopped": []}, 409
    stopped = []
    for r in targets:
        try:
            os.kill(r["pid"], signal.SIGTERM)
            stopped.append(r["pid"])
        except OSError as exc:
            log.warning("could not signal pid %s: %s", r["pid"], exc)
    _launch_registry.pop(str(path), None)
    _emit_event("run.stop_requested", taskfile=str(path), pids=stopped)
    return {"stopped": stopped,
            "note": "sent SIGTERM; the run settles its tasks and leases as it exits"}, 200


def _graph_topology(g):
    return {
        "name": g.name,
        "starts": list(g.starts),
        "nodes": [{"name": n, "gather": node.gather} for n, node in g.nodes.items()],
        "edges": [{"src": e.src, "dst": e.dst, "conditional": e.when is not None} for e in g.edges],
    }


def _build_graph_topologies():
    """The shape of the pipeline the fleet ACTUALLY runs, derived from the code.

    This renders the governed code-task pipeline from its graph definition.

    The topology is built from code_tasks.build_code_graph on a one-task
    synthetic taskfile and the per-task suffix stripped, so the diagram is
    generated from the same code that constructs the live graph and cannot
    drift from it. The loops it shows — fix, escalation, send-back, resync,
    inconclusive retry — are exactly the ones that are not obvious from the
    happy path and that a reader most needs to see.
    """
    import json as _json
    import tempfile
    import code_tasks
    tf = {"project": {"repo": str(config.ROOT), "title": "shape",
                      "tasks": [{"id": "t", "title": "t", "prompt": "p",
                                 "model": config.ESCALATION_PATH[0],
                                 "reviewer": config.cross_family_reviewer(config.ESCALATION_PATH[0]),
                                 "verify_cmd": "", "files_hint": [], "deps": []}]}}
    path = Path(tempfile.mkdtemp()) / "shape.json"
    path.write_text(_json.dumps(tf))
    try:
        tasks = code_tasks.load_taskfile(path)
        g = code_tasks.build_code_graph(None, tasks, taskfile=None)
    finally:
        try:
            path.unlink()
            path.parent.rmdir()
        except OSError:
            pass
    topo = _graph_topology(g)

    def strip(n):
        return n[:-2] if n.endswith("_t") else n
    topo["name"] = "code-tasks pipeline"
    topo["starts"] = [strip(n) for n in topo["starts"]]
    topo["nodes"] = [{"name": strip(n["name"]), "gather": n["gather"]} for n in topo["nodes"]]
    topo["edges"] = [{"src": strip(e["src"]), "dst": strip(e["dst"]),
                      "conditional": e["conditional"]} for e in topo["edges"]]
    return {"code": topo}


_BOARD_TOKEN = re.compile(r"^[A-Za-z0-9_.:/-]+$")
_BOARD_REPLY = re.compile(r"^[A-Za-z0-9]{1,32}$")


def _board_token(value):
    """A project or channel name: the allowed alphabet, and no path escape.

    The alphabet includes ``/`` and ``.`` because channels look like
    ``task:<id>`` and ``dm:<task>/<role>``. ``..`` and a leading slash are
    still a traversal, so they are rejected even though the class allows
    those characters. Nothing here is ever opened as a path (Rule 6b).
    """
    if not isinstance(value, str) or not _BOARD_TOKEN.fullmatch(value):
        return None
    if value.startswith("/") or "\\" in value:
        return None
    if any(part == ".." for part in value.split("/")):
        return None
    return value


def _board_projects():
    import agentboard
    with agentboard._lock:
        rows = agentboard._conn().execute(
            "SELECT project, COUNT(*) AS n, MAX(ts) AS last_ts "
            "FROM board_messages GROUP BY project ORDER BY last_ts DESC"
        ).fetchall()
    return [{"project": r["project"], "count": r["n"], "last_ts": r["last_ts"]}
            for r in rows]


def _board_task_rows(store, project):
    """code_tasks rows whose worktree (or taskfile stem) is this board project."""
    root = Path(config.WORKTREE_ROOT)
    out = []
    for row in store.code_tasks_all(2000):
        wt = row.get("worktree") or ""
        proj = ""
        if wt:
            try:
                rel = Path(wt).resolve().relative_to(root.resolve())
                proj = rel.parts[0] if rel.parts else ""
            except (ValueError, OSError):
                if project in Path(wt).parts:
                    proj = project
        stem = Path(row.get("taskfile") or "").stem
        if proj == project or stem == project:
            out.append(row)
    return out


def _board_channels(store, project):
    import agentboard
    tasks = _board_task_rows(store, project)
    by_id = {r["id"]: r for r in tasks}
    seen = {}
    for c in agentboard.channels(project, reader="operator"):
        seen[c["channel"]] = {
            "channel": c["channel"], "last_ts": c["last_ts"], "count": c["count"],
            "unread": c.get("unread_for") or 0,
        }
    for name in ("project", "captain", "operator"):
        seen.setdefault(name, {"channel": name, "last_ts": None, "count": 0, "unread": 0})
    for tid, row in by_id.items():
        name = f"task:{tid}"
        slot = seen.setdefault(name, {"channel": name, "last_ts": None, "count": 0, "unread": 0})
        slot["status"] = row.get("status") or ""
        slot["model"] = row.get("model") or ""
        slot["title"] = row.get("title") or ""
    for slot in seen.values():
        ch = slot["channel"]
        if ch.startswith("task:") and "status" not in slot:
            row = by_id.get(ch[5:])
            slot["status"] = (row or {}).get("status") or ""
    order = {"project": 0, "captain": 2, "operator": 3}
    def key(slot):
        ch = slot["channel"]
        if ch.startswith("dm:"):
            rank = 4
        elif ch.startswith("task:"):
            rank = 1
        else:
            rank = order.get(ch, 5)
        return (rank, -(slot["last_ts"] or 0), ch)
    return {"channels": sorted(seen.values(), key=key),
            "tasks": [{"id": r["id"], "status": r.get("status") or "",
                       "model": r.get("model") or "", "title": r.get("title") or ""}
                      for r in tasks]}


def _board_claim_view(project):
    import agentboard
    rows = agentboard.claims(project)
    for c in rows:
        hits = []
        for other in rows:
            if other["id"] == c["id"] or other.get("author") == c.get("author"):
                continue
            if any(agentboard.paths_overlap(p, q)
                   for p in c.get("paths") or [] for q in other.get("paths") or []):
                hits.append(other.get("author") or "")
        c["overlaps"] = hits
        c["conflict"] = bool(hits)
    return rows


def _board_get(path, q, store):
    """JSON body and status for one board read. None when path is unrelated."""
    import agentboard
    if path == "/api/board/projects":
        return {"projects": _board_projects()}, 200
    project = _board_token((q.get("project") or [""])[0])
    if path not in ("/api/board/channels", "/api/board/thread", "/api/board/inbox",
                    "/api/board/claims", "/api/board/expertise"):
        return None
    if not project:
        return {"error": "project must match [A-Za-z0-9_.:/-]+ and must not traverse"}, 400
    if path == "/api/board/channels":
        return _board_channels(store, project), 200
    if path == "/api/board/claims":
        return {"claims": _board_claim_view(project)}, 200
    if path == "/api/board/expertise":
        return {"expertise": agentboard.expertise(project)}, 200
    if path == "/api/board/inbox":
        agent = _board_token((q.get("agent") or [""])[0])
        if not agent:
            return {"error": "agent must match [A-Za-z0-9_.:/-]+ and must not traverse"}, 400
        return {"messages": agentboard.inbox(project, agent)}, 200
    channel = (q.get("channel") or [""])[0]
    if channel:
        channel = _board_token(channel)
        if not channel or not agentboard.valid_channel(channel):
            return {"error": "invalid channel"}, 400
    since = (q.get("since") or [None])[0]
    since_ts = None
    if since not in (None, ""):
        try:
            since_ts = float(since)
        except (TypeError, ValueError):
            return {"error": "since must be a timestamp"}, 400
    kinds = None
    raw_kinds = (q.get("kinds") or [""])[0]
    if raw_kinds:
        kinds = [k for k in raw_kinds.split(",") if k]
        if any(k not in agentboard.KINDS for k in kinds):
            return {"error": "unknown kind"}, 400
    return {"messages": agentboard.thread(project, channel or None, since_ts, kinds=kinds)}, 200


def _board_post(body):
    """Operator post. Author is always ``operator``. No path, command, or ref
    from the body is read, opened, or stored — extra keys are ignored."""
    import agentboard
    if not isinstance(body, dict):
        return {"error": "body must be an object"}, 400
    project = _board_token(body.get("project"))
    channel = _board_token(body.get("channel"))
    if not project or not channel or not agentboard.valid_channel(channel):
        return {"error": "project and channel must match [A-Za-z0-9_.:/-]+ and a real channel"}, 400
    kind = body.get("kind") if isinstance(body.get("kind"), str) else ""
    if kind not in agentboard.KINDS:
        return {"error": "kind must be one of the board kinds"}, 400
    text = body.get("body")
    if not isinstance(text, str):
        return {"error": "body text must be a string"}, 400
    text = text[:config.BOARD_BODY_MAX]
    mentions = body.get("mentions") or []
    if not isinstance(mentions, list) or any(not isinstance(m, str) or not _board_token(m.lstrip("@")) for m in mentions):
        return {"error": "mentions must be a list of names"}, 400
    if len(mentions) > 20:
        return {"error": "too many mentions"}, 400
    reply_to = body.get("reply_to") or None
    if reply_to is not None and (not isinstance(reply_to, str) or not _BOARD_REPLY.fullmatch(reply_to)):
        return {"error": "invalid reply_to"}, 400
    mid = agentboard.post(
        project, author="operator", channel=channel, kind=kind, body=text,
        mentions=[m.lstrip("@") for m in mentions], reply_to=reply_to,
        author_role="operator")
    return {"id": mid, "author": "operator"}, 200


def _board_read(body):
    """Mark a channel read for the operator, up to ``ts``. Same name checks
    as a post: nothing in the body is opened as a path or run as a command."""
    import agentboard
    if not isinstance(body, dict):
        return {"error": "body must be an object"}, 400
    project = _board_token(body.get("project"))
    channel = _board_token(body.get("channel"))
    if not project or not channel or not agentboard.valid_channel(channel):
        return {"error": "project and channel must match [A-Za-z0-9_.:/-]+ and a real channel"}, 400
    try:
        ts = float(body.get("ts"))
    except (TypeError, ValueError):
        return {"error": "ts must be a timestamp"}, 400
    agentboard.mark_read(project, "operator", channel, ts)
    return {"ok": True, "reader": "operator"}, 200


class Handler(BaseHTTPRequestHandler):
    server_version = "ArcDashboard/2.0"
    store = None

    def log_message(self, fmt, *args):
        log.debug("%s " + fmt, self.address_string(), *args)

    def _json(self, obj, code=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        try:
            body = path.read_bytes()
        except OSError:
            return self._json({"error": "not found"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path == "/":
                return self._file(Path(config.ROOT) / "static" / "index.html", "text/html; charset=utf-8")
            if u.path == "/usage.html":
                return self._file(Path(config.ROOT) / "static" / "usage.html", "text/html; charset=utf-8")
            if u.path == "/phone.html":
                return self._file(Path(config.ROOT) / "static" / "phone.html", "text/html; charset=utf-8")
            if u.path == "/common.js":
                return self._file(Path(config.ROOT) / "static" / "common.js", "application/javascript; charset=utf-8")
            if re.fullmatch(r"/panels/[a-z]+\.js", u.path):
                return self._file(Path(config.ROOT) / "static" / u.path[1:], "application/javascript; charset=utf-8")
            if u.path == "/api/usage":
                q = parse_qs(u.query)
                range_key = q.get("range", ["1h"])[0]
                include_series = q.get("series", ["0"])[0] == "1"
                return self._json(_usage(Handler.store, range_key, include_series))
            if u.path == "/api/usage/hourly":
                # keep_blank_values: an explicit `?date=` is a malformed date,
                # not an absent one. Only a MISSING param means today.
                q = parse_qs(u.query, keep_blank_values=True)
                # A missing ?date= means "today" — the page's default and the
                # question an operator asks. A malformed one is a 400 with a
                # JSON error rather than a silent fallback: quietly showing
                # today's numbers under a typo'd date is how a UI lies.
                date_s = q.get("date", [_today_str()])[0]
                try:
                    return self._json(_usage_hourly(Handler.store, date_s))
                except ValueError as exc:
                    return self._json({"error": str(exc), "date": date_s}, 400)
            if u.path == "/api/fleet":
                return self._json(_fleet(Handler.store))
            if u.path == "/api/queue":
                return self._json(_queue(Handler.store))
            if u.path == "/api/audit":
                import scheduler_audit
                rep = scheduler_audit.latest()
                return self._json({"ready": rep is not None, "report": rep,
                                   "last_run": scheduler_audit.last_run(),
                                   "due": scheduler_audit.due()})
            if u.path == "/api/errors":
                q = parse_qs(u.query)
                return self._json(_errors(q.get("range", ["24h"])[0],
                                          int(q.get("limit", ["40"])[0])))
            if u.path == "/api/projects":
                return self._json({"projects": _projects(Handler.store)})
            if u.path == "/api/work-status":
                return self._json(_work_status(Handler.store))
            if u.path == "/api/repos":
                return self._json({"repos": _list_repos()})
            if u.path == "/api/chat/poll":
                q = parse_qs(u.query)
                session = q.get("session", [""])[0]
                if not _SESSION_RE.fullmatch(session):
                    return self._json({"error": "bad session id"}, 400)
                try:
                    since = max(int(q.get("since", ["0"])[0]), 0)
                except ValueError:
                    since = 0
                return self._json(_chat_poll(session, since))
            if u.path == "/api/chat/sessions":
                return self._json(_chat_sessions())
            if u.path == "/api/captain/state":
                return self._json(_captain_state())
            if u.path == "/api/captain/sessions":
                return self._json(_captain_sessions())
            if u.path == "/api/captain/queue":
                return self._json(_captain_queue())
            if u.path == "/api/captain/autopilot":
                return self._json(_autopilot_view())
            if u.path == "/api/captain/poll":
                q = parse_qs(u.query)
                session = q.get("session", [""])[0]
                if not _SESSION_RE.fullmatch(session):
                    return self._json({"error": "bad session id"}, 400)
                try:
                    since = max(int(q.get("since", ["0"])[0]), 0)
                except ValueError:
                    since = 0
                return self._json(_captain_poll(session, since))
            if u.path == "/api/studio":
                # The Studio view: phases, gates, the phase task board, the
                # render workbench and judge verdicts, read-only (studio.status).
                from studio import status as studio_status
                return self._json(studio_status.snapshot(
                    Handler.store, live_tasks=_live_task_ids()))
            if u.path == "/api/studio/image":
                # Rule 6b: this server has no authentication, so it serves an
                # image ONLY from inside the named project's studio directory
                # (studio.status.image_path refuses traversal, non-images and
                # malformed project names). Never a path taken as-is.
                from studio import status as studio_status
                q = parse_qs(u.query)
                img = studio_status.image_path(q.get("project", [""])[0],
                                               q.get("path", [""])[0])
                if img is None:
                    return self._json({"error": "not found"}, 404)
                ctype = {".png": "image/png", ".webp": "image/webp"}.get(
                    img.suffix.lower(), "image/jpeg")
                return self._file(img, ctype)
            if u.path == "/api/studio/playtest/shot":
                # A playtest screenshot: project, session and file name are
                # each allowlisted and the resolved path must stay inside that
                # session's directory (studio.playtest.shot_path).
                from studio import playtest
                q = parse_qs(u.query)
                img = playtest.shot_path(q.get("project", [""])[0],
                                         q.get("session", [""])[0],
                                         q.get("file", [""])[0])
                if img is None:
                    return self._json({"error": "not found"}, 404)
                return self._file(img, "image/png")
            if u.path == "/api/project":
                q = parse_qs(u.query)
                obj, code = _project_detail(Handler.store, q.get("file", [""])[0])
                return self._json(obj, code)
            if u.path == "/api/agents":
                return self._json(_agents(Handler.store))
            if u.path == "/api/github":
                return self._json(_github(Handler.store))
            if u.path == "/api/health":
                return self._json(_health(Handler.store))
            if u.path == "/api/metrics":
                return self._json(_metrics(Handler.store))
            if u.path == "/api/task-diff":
                q = parse_qs(u.query)
                fname = q.get("file", [""])[0]
                if not re.fullmatch(r"[\w.-]+\.json", fname):
                    return self._json({"error": "bad file name"}, 400)
                tf = Path(config.TASKS_DIR) / fname
                if not tf.is_file():
                    return self._json({"error": "not found"}, 404)
                try:
                    proj = json.loads(tf.read_text(encoding="utf-8", errors="replace")).get("project") or {}
                except Exception as exc:
                    return self._json({"error": f"invalid task file: {exc}"}, 400)
                return self._json(_task_deliverable(
                    _valid_repo(proj.get("repo") or ""), q.get("task", [""])[0],
                    want_patch=q.get("patch", ["0"])[0] == "1"))
            if u.path == "/api/plan-proposals":
                # Read-only view of the plan_proposals table (plan_amend.py).
                # file= is a bare taskfile name under TASKS_DIR, never a path —
                # same allowlist discipline as /api/task-diff (AGENTS.md Rule 6b).
                q = parse_qs(u.query)
                fname = q.get("file", [""])[0]
                taskfile = None
                if fname:
                    if not re.fullmatch(r"[\w.-]+\.json", fname):
                        return self._json({"error": "bad file name"}, 400)
                    taskfile = str((Path(config.TASKS_DIR) / fname).resolve())
                try:
                    limit = min(max(int(q.get("limit", ["100"])[0]), 1), 500)
                except ValueError:
                    limit = 100
                return self._json({"proposals": Handler.store.list_plan_proposals(
                    taskfile, limit)})
            if u.path == "/api/run-log":
                # The stdout/stderr of a run or plan process the dashboard
                # launched (logs/run-*.log, logs/plan-*.log) — what "view log"
                # opens after a Run click, and the only place a run that
                # crashed at startup explains itself.
                q = parse_qs(u.query)
                fn = q.get("file", [""])[0]
                if not _RUN_LOG_RE.fullmatch(fn):
                    return self._json({"error": "bad file name"}, 400)
                if not (Path(config.ROOT) / "logs" / fn).is_file():
                    return self._json({"error": "no such run log"}, 404)
                try:
                    n = min(max(int(q.get("lines", ["200"])[0]), 1), 2000)
                except ValueError:
                    n = 200
                lines = _log_tail(fn, n)
                return self._json({"file": fn, "lines": lines})
            if u.path == "/api/gate-log":
                q = parse_qs(u.query)
                fn = q.get("file", [""])[0]
                if not re.fullmatch(r"[\w.-]+\.log", fn):
                    return self._json({"error": "bad file name"}, 400)
                path = Path(config.ROOT) / "logs" / "gates" / fn
                if not path.is_file():
                    return self._json({"error": "no gate log for this attempt"}, 404)
                try:
                    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError as exc:
                    return self._json({"error": str(exc)}, 500)
                return self._json({"file": fn, "total_lines": len(lines),
                                   "lines": lines[-200:]})
            if u.path == "/api/transcript":
                q = parse_qs(u.query)
                activity = q.get("view", [""])[0] == "activity"
                tail = q.get("tail", ["150" if activity else "200"])[0]
                try:
                    tail = min(max(int(tail), 1), 1000)
                except ValueError:
                    tail = 150 if activity else 200
                if activity:
                    out = _transcript_activity_view(q.get("file", [""])[0], tail)
                    if out is not None:
                        # kimi-shaped or unknown transcripts resolve to None
                        # here and fall through to the raw tail below.
                        return self._json(*out)
                obj, code = _transcript_tail(q.get("file", [""])[0], tail)
                return self._json(obj, code)
            if u.path == "/api/summary":
                st = Handler.store
                return self._json({
                    "ts": time.time(),
                    "research": st.stats(),
                    "critique_matrix": st.critique_matrix(),
                    "limits": {f: config.family_limit(f) for f in config.FAMILY_ORDER},
                    "event_log": str(config.EVENTS_LOG),
                })
            if u.path == "/api/events":
                q = parse_qs(u.query)
                after = int(q.get("after", ["0"])[0])
                lines = _load_event_lines()
                reset = after > len(lines)
                start = 0 if reset else after
                # A cold client asking for "today" used to walk the whole log
                # from line 0 — every page load downloaded the entire history
                # (677KB and climbing, rotating only at 100MB) to count a
                # handful of today's merges. `since` seeds the cursor instead.
                if start == 0 and q.get("since"):
                    try:
                        start = _first_event_at_or_after(lines, float(q["since"][0]))
                    except (TypeError, ValueError):
                        pass
                chunk = lines[start:start + MAX_EVENTS_PER_RESPONSE]
                events = []
                for line in chunk:
                    try:
                        events.append(json.loads(line))
                    except Exception:
                        pass
                return self._json({"events": events, "next": start + len(chunk),
                                   "reset": reset, "total": len(lines)})
            if u.path == "/api/activity":
                # The fleet activity feed: the last N curated lifecycle
                # events, newest first, each in the panel's own contract
                # ({ts, type, task, run_id, context} plus the event's own
                # fields). Read through the tolerant `_load_event_lines`
                # reader, so a truncated or garbage line (a killed writer
                # mid-append) is skipped instead of 500ing the panel.
                q = parse_qs(u.query, keep_blank_values=True)
                try:
                    limit = int(q.get("limit", ["50"])[0])
                except (TypeError, ValueError):
                    limit = 50
                if limit <= 0:
                    limit = 50
                limit = min(limit, ACTIVITY_MAX_LIMIT)
                lines = _load_event_lines()
                # id -> taskfile, for the click-through. Built from the
                # code_tasks table rather than from the page's loaded list:
                # the feed shows history, and a task that has since been
                # archived or filtered out must still open its project.
                taskfile_of = {}
                try:
                    for row in self.store.code_tasks_all(1000):
                        tid = row.get("id")
                        if tid and tid not in taskfile_of:
                            tf = row.get("taskfile")
                            taskfile_of[tid] = (str(Path(tf).name)
                                                if tf else None)
                except Exception:
                    taskfile_of = {}      # a feed must never 500 on a lookup
                out = []
                # Walk BACKWARDS: the log is append-ordered, so the newest
                # events are the last lines and there is no need to parse the
                # whole 100MB history to answer "the last 50".
                for line in reversed(lines):
                    if len(out) >= limit:
                        break
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue          # corrupt line: skip, never 500
                    if not isinstance(e, dict):
                        continue          # valid JSON, but not an event object
                    if e.get("type") not in ACTIVITY_TYPES:
                        continue          # heartbeat and friends would swamp it
                    # Tolerant like the json.loads above: a line can be valid
                    # JSON and still hold a field of the wrong SHAPE (an older
                    # or hand-edited writer). A feed must degrade, never 500.
                    ctx = e.get("context")
                    ctx = dict(ctx) if isinstance(ctx, dict) else {}
                    for k in ("workload", "round", "iteration", "module",
                              "run_id"):
                        if e.get(k) is not None:
                            ctx.setdefault(k, e[k])
                    # `file` is what makes the row clickable: a task id alone
                    # does not name a taskfile, and the page cannot open a
                    # project it cannot name. Resolved from the code_tasks
                    # rows (the authoritative id -> taskfile mapping), so a
                    # click works even for a task whose project is not in the
                    # page's current filter or has since been archived.
                    tid = e.get("task")
                    file_of = _activity_file(taskfile_of, tid)
                    # Contract keys LAST: the five keys the panel is promised
                    # ({ts,type,task,run_id,context}) are the normalised ones,
                    # so a raw field of the same name cannot shadow them. Every
                    # other field of the event rides through untouched beside
                    # them — that is how the review round, issue count, reason
                    # and reviewer model reach the feed.
                    out.append({**e, "ts": e.get("ts"), "type": e.get("type"),
                                "task": tid, "run_id": e.get("run_id"),
                                "file": file_of,
                                "context": ctx})
                return self._json({"events": out, "limit": limit,
                                   "total": len([1 for ln in lines
                                                 if ln.strip()])})
            if u.path == "/api/graph-shapes":
                # The graph BETWEEN tasks: the pattern catalogue the planner
                # chooses from, every taskfile classified by the shape its deps
                # actually form, and what the engine can and cannot express.
                import graph_shapes
                return self._json(graph_shapes.describe())
            if u.path == "/api/evidence-file":
                # Read-only bytes of ONE evidence artifact, for the timeline
                # drawer's thumbnails and video links. The path is confined to
                # the evidence root by resolving it and testing containment —
                # a ".." segment, an absolute path elsewhere, or a symlink out
                # of the tree all resolve outside and are refused.
                q = parse_qs(u.query, keep_blank_values=True)
                rel = q.get("path", [""])[0]
                root = _evidence_root()
                bad = None
                try:
                    target = (root / rel).resolve()
                except OSError:
                    bad = "bad path"
                    target = None
                if target is None or rel.strip() == "" or Path(rel).is_absolute():
                    bad = bad or "bad path"
                elif target != root and root not in target.parents:
                    bad = "path outside the evidence directory"
                elif not target.is_file():
                    bad = "not found"
                if bad:
                    return self._json({"error": bad},
                                      404 if bad == "not found" else 400)
                ctype = ("video/mp4" if target.suffix == ".mp4"
                         else "image/gif" if target.suffix == ".gif"
                         else "image/png" if target.suffix == ".png"
                         else "application/json" if target.suffix == ".json"
                         else "application/octet-stream")
                return self._file(target, ctype)
            if re.fullmatch(r"/api/tasks/[^/]+/timeline", u.path):
                # Everything recorded about one task, in time order: its
                # events, harness runs with transcript paths, error_events
                # rows with fingerprints, and its evidence manifests. GET
                # only, and the id is validated before it is used as a path
                # segment or a LIKE prefix (Rule 6b).
                tid = u.path[len("/api/tasks/"):-len("/timeline")]
                if not _TASK_ID_RE.fullmatch(tid):
                    return self._json({"error": "bad task id"}, 400)
                q = parse_qs(u.query, keep_blank_values=True)
                taskfile = q.get("taskfile", [""])[0] or None
                if taskfile and not re.fullmatch(r"[\w.-]+\.json", taskfile):
                    return self._json({"error": "bad file name"}, 400)
                return self._json(_timeline(tid, taskfile, Handler.store))
            if u.path == "/api/pipeline":
                import pipeline_doc
                return self._json(pipeline_doc.describe(
                    _build_graph_topologies()["code"]))
            if u.path == "/api/graphs":
                return self._json(_build_graph_topologies())
            if u.path.startswith("/api/board/"):
                q = parse_qs(u.query)
                got = _board_get(u.path, q, Handler.store)
                if got is not None:
                    return self._json(got[0], got[1])
            return self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as exc:
            log.exception("handler error")
            try:
                self._json({"error": str(exc)}, 500)
            except Exception:
                pass

    def _refuse_post(self):
        """Why this POST must not be acted on, as (status, error) — or None.

        Every POST changes state: it writes a task file, starts or stops a
        fleet run, opens a pull request. The server listens on every
        interface and has no login, so what is checked here is the whole of
        the distance between a browser tab on the wrong website — or any
        device on the wifi — and a fleet run that pushes to GitHub.

        1. Content-Type must be application/json. A cross-origin request that
           carries it is not a CORS "simple request": the browser asks this
           server for permission first (a preflight), this server never
           grants cross-origin access, so the browser refuses to send it.
           Without the check a text/plain POST from any page the operator
           had open went straight through — the body was parsed as JSON no
           matter what it claimed to be.
        2. If the browser names an Origin, it must be this server. The
           dashboard's own pages send their origin, which is the Host they
           were served from; a page on another site cannot forge that.
        3. When ARC_DASHBOARD_TOKEN is set, the request must carry it. This
           is what stands between every other device on the network and the
           run button.
        """
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            return 415, "POST bodies must be application/json"
        origin = (self.headers.get("Origin") or "").strip()
        if origin:
            host = (self.headers.get("Host") or "").strip().lower()
            if not host or urlparse(origin).netloc.lower() != host:
                return 403, "cross-origin request refused"
        token = config.DASHBOARD_TOKEN
        if token:
            auth = (self.headers.get("Authorization") or "").strip()
            given = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
            if not given or not hmac.compare_digest(given.encode(), token.encode()):
                return 401, "this dashboard requires a token for actions (ARC_DASHBOARD_TOKEN)"
        return None

    def do_POST(self):
        u = urlparse(self.path)
        try:
            refused = self._refuse_post()
            if refused:
                return self._json({"error": refused[1]}, refused[0])
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > 256 * 1024:
                return self._json({"error": "body size must be 1 byte..256KB"}, 400)
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8", errors="replace"))
            except ValueError as exc:
                return self._json({"error": f"invalid JSON: {exc}"}, 400)
            if u.path == "/api/studio/approve":
                obj, code = _studio_approve(body)
                return self._json(obj, code)
            if u.path in _PLAYTEST_POSTS:
                obj, code = _PLAYTEST_POSTS[u.path](body)
                return self._json(obj, code)
            if u.path == "/api/projects/create":
                obj, code = _create_project(body)
                return self._json(obj, code)
            if u.path == "/api/projects/run":
                obj, code = _run_project(body)
                return self._json(obj, code)
            if u.path == "/api/repos/create":
                obj, code = _create_repo(body)
                return self._json(obj, code)
            if u.path == "/api/repos/remote":
                obj, code = _repo_remote(body)
                return self._json(obj, code)
            if u.path == "/api/chat/start":
                obj, code = _chat_start(body)
                return self._json(obj, code)
            if u.path == "/api/captain/start":
                obj, code = _captain_start(body)
                return self._json(obj, code)
            if u.path == "/api/captain/autopilot/pause":
                obj, code = _autopilot_pause(body)
                return self._json(obj, code)
            if u.path == "/api/captain/autopilot/ack":
                obj, code = _autopilot_ack(body)
                return self._json(obj, code)
            if u.path == "/api/projects/stop":
                obj, code = _stop_project(body)
                return self._json(obj, code)
            if u.path == "/api/promote":
                import gitstore

                async def go():
                    await gitstore.ensure_base_branch(Path(config.ROOT))
                    return await gitstore.open_promotion_pr(Path(config.ROOT))
                try:
                    n, url, note = asyncio.run(go())
                except Exception as exc:
                    return self._json({"error": str(exc)}, 500)
                _gh_cache["key"] = 0.0            # force a refresh
                _emit_event("promotion.opened" if url else "promotion.skipped",
                            number=n, url=url, note=note)
                return self._json({"number": n, "url": url, "note": note})
            if u.path == "/api/projects/archive":
                obj, code = _archive_project(body)
                return self._json(obj, code)
            if u.path == "/api/projects/retry-task":
                obj, code = _retry_task(body)
                return self._json(obj, code)
            if u.path == "/api/projects/escalate-task":
                obj, code = _escalate_task(body)
                return self._json(obj, code)
            if u.path == "/api/restart":
                obj, code = _restart(body)
                return self._json(obj, code)
            if u.path == "/api/board/post":
                obj, code = _board_post(body)
                return self._json(obj, code)
            if u.path == "/api/board/read":
                obj, code = _board_read(body)
                return self._json(obj, code)
            return self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as exc:
            log.exception("handler error")
            try:
                self._json({"error": str(exc)}, 500)
            except Exception:
                pass


def _lan_addresses():
    """Best-effort list of this machine's non-loopback IPv4 addresses, for printing URLs."""
    addrs = []
    try:
        import fcntl
        import struct

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for _, ifname in socket.if_nameindex():
                if ifname == "lo":
                    continue
                try:
                    res = fcntl.ioctl(sock.fileno(), 0x8915, struct.pack("256s", ifname.encode()[:15]))  # SIOCGIFADDR
                except OSError:
                    continue
                ip = socket.inet_ntoa(res[20:24])
                if not ip.startswith("127.") and ip not in addrs:
                    addrs.append(ip)
        finally:
            sock.close()
    except Exception:
        pass
    if not addrs:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(("192.0.2.1", 80))  # TEST-NET: picks the outbound interface, sends nothing
            addrs.append(sock.getsockname()[0])
            sock.close()
        except OSError:
            pass
    return addrs


def serve(port=None, db_path=None):
    global _httpd
    port = port or config.DASHBOARD_PORT
    db_path = db_path or config.DB_PATH
    Handler.store = Store(db_path)
    bind = config.DASHBOARD_BIND
    try:
        httpd = ThreadingHTTPServer((bind, port), Handler)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print(f"port {port} is already in use -- the dashboard is probably already running.")
            print(f"just open http://localhost:{port} in a browser (or run ./stop.sh, then start it again).")
            raise SystemExit(1)
        raise
    _httpd = httpd
    log.info("dashboard on http://%s:%d (db=%s, events=%s)", bind, port, db_path, config.EVENTS_LOG)
    # The daily audit runs from here. WSL has no working cron and sleeps when
    # idle; this server is the process that is awake when the operator is.
    try:
        import scheduler_audit
        scheduler_audit.start(Handler.store)
        nxt = scheduler_audit.last_run()
        log.info("daily audit scheduler armed (last run: %s)",
                 time.strftime("%Y-%m-%d %H:%M", time.localtime(nxt)) if nxt else "never — will run now")
    except Exception as exc:
        log.error("daily audit scheduler did not start: %s", exc)
    try:
        import captain
        captain.start_drain(db_path)
        log.info("captain queue drain armed")
    except Exception as exc:
        log.error("captain queue drain did not start: %s", exc)
    try:
        from studio import autopilot
        armed = autopilot.start(db_path)
        log.info("studio autopilot %s", "armed" if armed else "off")
    except Exception as exc:
        log.error("studio autopilot did not start: %s", exc)
    print(f"dashboard: http://localhost:{port}", flush=True)
    # Bound to one address: that is the only one worth printing. Bound to
    # all of them: list the LAN ones, which is what a phone needs.
    for ip in ([bind] if bind not in ("0.0.0.0", "", "::") else _lan_addresses()):
        if ip.startswith("127."):
            continue
        print(f"  from your laptop/phone: http://{ip}:{port}  (small screens: http://{ip}:{port}/phone.html)", flush=True)
    if not config.DASHBOARD_TOKEN:
        print("  note: no ARC_DASHBOARD_TOKEN is set — anyone who can reach this address can "
              "start and stop fleet runs. See README: Who can reach the dashboard.", flush=True)
    try:
        httpd.serve_forever()
    except (KeyboardInterrupt, OSError):
        pass
    finally:
        _httpd = None
    if _restarting:
        _graceful_reexec()
