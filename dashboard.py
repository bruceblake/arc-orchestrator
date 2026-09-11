"""Lightweight dashboard: static UI + JSON APIs over the event log and sqlite DB.

Run alongside the orchestrator (separate process):
    python main.py serve [--port 8787]

Usage layers (why a model can appear under more than one source):
  arc-pool          raw API requests the pool made (request/request_end events, carry tokens)
  driver:<harness>  whole agent task runs in an external CLI harness (driver.* events;
                    driver.done carries tokens since drivers.py learned transcript_tokens)
  kimi-code         per-API-call usage parsed from kimi-code session wire logs
                    (covers every kimi-code session, interactive ones included)
"""
import asyncio
import errno
import json
import logging
import os
import re
import signal
import socket
import sqlite3
import subprocess
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import config
import gitstore
from store import Store

log = logging.getLogger("dashboard")

_lines_cache = {"key": None, "lines": []}
MAX_EVENTS_PER_RESPONSE = 3000

PRETTY = {"Kimi-K3": "Kimi K3", "GLM-5.3": "GLM 5.3", "gpt-oss-120b": "gpt-oss 120B",
          "DeepSeek-V4-Flash": "DeepSeek V4 Flash"}

# Rolling windows plus one calendar window. "today" is deliberately not a
# synonym for 24h: at 09:00 a rolling day is mostly yesterday, and "what has
# the fleet done today" is the question an operator actually asks.
RANGES = ["1h", "3h", "6h", "today", "24h", "7d", "all"]
_RANGE_SECONDS = {"1h": 3600, "3h": 3 * 3600, "6h": 6 * 3600,
                  "24h": 86400, "7d": 7 * 86400}


def _range_cutoff(range_key, now):
    """Epoch seconds the window starts at, or None for 'all'."""
    if range_key == "today":
        lt = time.localtime(now)
        return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0,
                            lt.tm_wday, lt.tm_yday, lt.tm_isdst))
    return None if range_key == "all" else now - _RANGE_SECONDS[range_key]

_launch_registry = {}  # abspath taskfile -> {"pid", "log", "started", "dry_run"}


def _pretty(model):
    if model in PRETTY:
        return PRETTY[model]
    return re.sub(r"-(thinking|legacy)[\w-]*$", "", model or "unknown").replace("-", " ")


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
# An unmatched driver.start can never outlive DRIVER_TIMEOUT + retry backoff:
# the driver itself errors (and settles) every attempt it owns. Anything older
# belongs to a killed run and must not count against concurrency caps.
DRIVER_STALE_S = config.DRIVER_TIMEOUT + 240
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
        if started < now - DRIVER_STALE_S:
            del driver_starts[key]
            if key not in _stale_emitted:
                _stale_emitted.add(key)
                _emit_event("driver.stale", harness=key[0], model=key[1], role=key[2],
                            task=key[3], attempt=key[4], age_s=round(now - started),
                            note="driver.start older than DRIVER_TIMEOUT+buffer with no done/error — run was killed, pruned from in-flight")
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
                     "started": started, "elapsed_s": round(max(0.0, now - started), 1),
                     "idle_s": idle_s, "bytes": prog.get("bytes"),
                     "state": prog.get("state"),
                     "cpu_delta_s": prog.get("cpu_delta_s"),
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


def _usage(store=None, range_key=None, include_series=False):
    """Usage aggregates for /api/usage, honoring the requested range.

    `range_key` selects the aggregation window: 1h/3h/6h/today/24h/7d/all. A
    missing or unrecognized key (including None) falls back to "1h" — the usage
    page's default. "today" is a CALENDAR day, not a rolling 24 hours: at 09:00
    a rolling day is mostly yesterday, and "what has the fleet done today" is
    the question an operator actually asks. "all" is the historical view: nothing is dropped. A windowed range
    bounds totals, per-model/family rows, and the series points, while the
    in-flight list is never trimmed — a live agent is current by definition.

    Per-model `cost` prices prompt/completion tokens at each model's rate via
    `config.cost_of`. Where an event reports a token count but no prompt/
    completion breakdown, the excess is priced at the completion rate, so the
    figure is an UPPER bound rather than an under-count.
    """
    now = time.time()
    range_key = range_key if range_key in RANGES else "1h"
    cutoff = _range_cutoff(range_key, now)

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
    pts = []  # (ts, family, req_delta, tok_delta, task_run_delta)
    done_tok_keys = set()

    # models/inflight/points for kimi-code sessions up front (cached, shared with inflight)
    inflight, kimi = _collect_inflight(now, store)

    ev_lines = _load_event_lines()
    lines = ev_lines
    if cutoff is not None:
        lines = ev_lines[_first_event_at_or_after(ev_lines, cutoff):]
    for line in lines:
        try:
            e = json.loads(line)
        except Exception:
            continue
        etype = e.get("type")
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
                    pts.append((ts, family, 0, 0, 0, 1))
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
                pts.append((ts, family, 1, toks, 1, 0))
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
                pts.append((ts, family, 1, tokens, 0, 0))
        else:
            mod["errors"] += 1
            fam["errors"] += 1
            totals["errors"] += 1
            # A failed request produced NO point at all, so the timeline showed
            # traffic dipping during an outage rather than errors spiking — the
            # shape that makes a bad hour look like a quiet one.
            if ts:
                pts.append((ts, family, 1, 0, 0, 1))

    # Backfill opencode tokens from transcripts for pre-plumbing runs.
    for ts, model, toks, ptoks, ctoks in _opencode_token_backfill(store, done_tok_keys):
        if cutoff is not None and (ts is None or ts < cutoff):
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
        pts.append((ts, family, 0, toks, 0, 0))

    # Merge kimi-code CLI sessions so the dashboard also shows interactive traffic,
    # which goes straight to llm-api.arc.vt.edu and never touches the event log.
    # A windowed range narrows kimi's per-model totals from its per-turn log; the
    # all-time `models` stays the historical view for range=all.
    pts.extend((ts, "kimi-code", req, tok, 0, 0) for ts, req, tok in kimi["points"]
               if cutoff is None or (ts is not None and ts >= cutoff))
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

    # Over-cap detection. Kimi-K3 headless drivers (family "kimi") and kimi CLI
    # sessions (family "kimi-code") share ONE ARC account, so their combined
    # in-flight count is checked against kimi's limit.
    shared = {"kimi": ("kimi", "kimi-code")}
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
        for ts, family, req, tok, _tr, err in pts:
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
            pts_list[idx]["errors"] += err

    day0 = int(now // 86400)
    daily = []
    day_idx = {}
    for i in range(30):
        d = day0 - 29 + i
        rec = {"date": time.strftime("%Y-%m-%d", time.localtime(d * 86400)),
               "requests": 0, "tokens": 0, "task_runs": 0, "families": {}}
        day_idx[d] = rec
        daily.append(rec)
    for ts, family, req, tok, tr, _err in pts:
        if not ts:
            continue
        rec = day_idx.get(int(ts // 86400))
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
    return res


_fleet_cache = {"key": 0.0, "models": [], "totals": {}}  # refreshed at most every ~2s
FLEET_CACHE_S = 2.0


def _fleet(store):
    """All-history code-fleet totals for /api/fleet.

    Wraps _usage(store, "all") — no separate event walk — and merges its
    per-source rows into one row per model (driver:<harness>, arc-pool and
    kimi-code all feed the same fleet). account_cap/driver_cap come from
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
    """Which local harness runs this model. Mirrors code_tasks._driver."""
    return "kimi" if model == "Kimi-K3" else "opencode"


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
    for h in ("opencode", "kimi"):
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


def _project_phase(statuses, ids, run_pid):
    """One word for where a project stands: running | done | attention | new.

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
    if total and statuses.get("merged", 0) >= total:
        return "done"
    if not statuses:
        return "new"
    return "attention"


def _task_loop_stats(store, task_ids):
    """Fix-loop stats per base task id: implement-attempt max (harness_runs is
    authoritative), task.escalated/task.conflict event counts, newest reviewer
    verdict as {"pass", "n_issues"} (or None)."""
    want = [i for i in (task_ids or []) if i]
    stats = {i: {"attempts": 0, "escalations": 0, "conflicts": 0, "last_verdict": None}
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
        if '"task.escalated"' not in ln and '"task.conflict"' not in ln:
            continue
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        base, _x = _xkey(e.get("task") or e.get("module"))
        if base not in stats:
            continue
        if e.get("type") == "task.escalated":
            stats[base]["escalations"] += 1
        elif e.get("type") == "task.conflict":
            stats[base]["conflicts"] += 1
    return stats


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
        # Price each event at ITS own model. Cross-review (Kimi <-> GLM) and
        # tier escalation both mix models under one task id; pricing the whole
        # bucket at the taskfile's implementer mis-charges every reviewer run
        # (a gpt-oss task reviewed by GLM would price GLM's expensive tokens at
        # gpt-oss rates, so the figure was not even an upper bound).
        s["cost"] += config.cost_of(e.get("model"), e.get("prompt_tokens") or 0,
                                    e.get("completion_tokens") or 0)
        s["seconds"] += e.get("seconds") or 0.0
        s["runs"] += 1
    # Supplement from harness_runs rows whose driver.done fell out of the event
    # log (the log is bounded; the DB keeps every run). Seconds always; tokens
    # via cached transcript parse (kimi transcripts carry no usage -> 0 there,
    # but agent-time is complete either way).
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
            # id. Only the excess a split cannot account for — the kimi-wire
            # tokens, which carry no prompt/completion split — is priced here,
            # at Kimi's completion rate, since it is kimi's wire log.
            extra = max(0, tot - (ptok + ctok))
            node_cost = ev.get("cost", 0.0)
            if extra:
                node_cost += config.cost_of("Kimi-K3", 0, extra)
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
                    "last_verdict": ls.get("last_verdict")}
            nodes.append(node)
        edges = [{"src": d, "dst": t["id"]} for t in tdefs if t.get("id")
                 for d in (t.get("deps") or t.get("depends") or []) if d in ids]
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
        out.append({"file": f.name, "title": proj.get("title") or f.stem,
                    "repo": proj.get("repo"), "n_tasks": len(tdefs), "task_ids": ids,
                    "models": sorted({t.get("model") for t in tdefs if t.get("model")}),
                    "reviewers": sorted({t.get("reviewer") for t in tdefs if t.get("reviewer")}),
                    "statuses": statuses,
                    "orphan_rows": orphan_rows,
                    "dag": {"nodes": nodes, "edges": edges},
                    "progress": {"done": merged_n, "total": len(ids)},
                    "tokens": tok_total + live_tok, "seconds": round(sec_total + live_sec, 1),
                    "done_tokens": tok_total, "done_seconds": sec_total,
                    "live_tokens": live_tok, "live_seconds": live_sec,
                    "cost": round(cost_total + live_cost, 4),
                    "done_cost": cost_total, "live_cost": live_cost,
                    "errors": errors,
                    "archived": str(f) in archived,
                    "archived_at": archived.get(str(f)),
                    "phase": _project_phase(statuses, ids,
                                            run_by_file.get(f.name)),
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
    return {"file": fname, "title": proj.get("title") or path.stem,
            "repo": proj.get("repo"), "run_pid": run_pid,
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


def _agents(store):
    now = time.time()
    inflight, _kimi = _collect_inflight(now, store)
    _prune_registry()
    runs = [{"taskfile": k, **v} for k, v in _launch_registry.items()]
    return {"now": now, "agents": inflight, "runs": runs,
            "recent": _recent_agent_runs(store)}


_TRANSCRIPT_RE = re.compile(r"^[\w.-]+\.jsonl$")


_gh_cache = {"key": 0.0, "data": None}
GH_CACHE_S = 20.0


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


def _spawn_logged(argv, log_name):
    log_dir = Path(config.ROOT) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    lf = open(log_dir / log_name, "ab", buffering=0)
    proc = subprocess.Popen(argv, cwd=str(config.ROOT), stdout=lf, stderr=subprocess.STDOUT,
                            start_new_session=True, close_fds=True)
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
                "taskfile": expect, "note": "Kimi-K3 is drafting the task file"}, 200

    title = (body.get("title") or "").strip() if isinstance(body.get("title"), str) else ""
    tasks = body.get("tasks")
    if not title:
        return {"error": "title required (or pass goal to plan with Kimi-K3)"}, 400
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
        entry.setdefault("model", "DeepSeek-V4-Flash")
        if "reviewer" not in entry:
            entry["reviewer"] = {"Kimi-K3": "glm", "GLM-5.3": "kimi"}.get(entry["model"], "kimi")
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
    argv = [str(Path(config.ROOT) / ".venv" / "bin" / "python"), "main.py", "code", "run",
            str(path)]
    if dry_run:
        argv.append("--dry-run")
    slug = _task_slug(path.stem)
    log_name = f"run-{slug}-{int(time.time())}.log"
    proc, log_name = _spawn_logged(argv, log_name)
    _launch_registry[key] = {"pid": proc.pid, "log": log_name, "started": time.time(),
                             "dry_run": dry_run, "kind": "run"}
    return {"pid": proc.pid, "log": log_name, "dry_run": dry_run}, 200


_HEALTH_PROBLEMS = ("driver.error", "driver.stalled", "driver.timeout",
                    "driver.cap_wait", "inflight.over_cap", "task.failed",
                    "task.conflict", "graph.draining", "run.interrupted")

# A heartbeat (driver.progress, cap_wait) proves the fleet is ALIVE, not that
# it is MOVING. Only these events mean a unit of work actually advanced.
_PROGRESS_EVENTS = ("node_end", "task.gate", "task.merged", "driver.done")

# Nothing legitimate goes quiet for longer than one driver attempt plus gate
# and retry slack: DRIVER_TIMEOUT bounds a single harness run, so a threshold
# below it flags every long implement node as stalled — the false alarm that
# gets a watchdog ignored. Override with ARC_STALL_THRESHOLD_S.
WATCHDOG_STALL_S = float(os.getenv(
    "ARC_STALL_THRESHOLD_S", str(int(config.DRIVER_TIMEOUT) + 900)))


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


def _health(store):
    """Small, cheap fleet-health payload for the Projects page.

    The same facts live in /api/usage, but that response is ~34KB and is only
    fetched by the Usage page — so the one screen an operator actually watches
    showed nothing when the fleet was wedged at its concurrency cap.
    """
    now = time.time()
    inflight, _kimi = _collect_inflight(now, store)
    import reconcile

    # Two different caps govern the same model and must not be conflated:
    #   driver_cap  — how many harness instances THIS fleet may run
    #                 (config.driver_limit), deliberately below the account cap
    #                 so interactive use still has room;
    #   account_cap — ARC's per-account limit (config.family_limit), which the
    #                 fleet's drivers AND the operator's own interactive
    #                 kimi-code sessions both consume.
    # Counting interactive sessions against driver_cap reported "Kimi-K3 4/2
    # OVER CAP" while the fleet was correctly running a single driver.
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
            "leases": leases, "problems": problems}


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
    return {"file": fname, "task": tid, "status": "pending"}, 200


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
    from pool import ArcPool
    from work import Roles, build_round_graph
    from build_work import build_build_graph

    pool = ArcPool(dry_run=True)
    store = Store(":memory:")
    round_g = build_round_graph(pool, store, Roles(), {})
    build_g = build_build_graph(pool, store, build_id=0, iteration=1, mode="create",
                                out_dir=Path("."), current_files={})
    return {"round": _graph_topology(round_g), "build": _graph_topology(build_g)}


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
            if u.path == "/api/fleet":
                return self._json(_fleet(Handler.store))
            if u.path == "/api/queue":
                return self._json(_queue(Handler.store))
            if u.path == "/api/errors":
                q = parse_qs(u.query)
                return self._json(_errors(q.get("range", ["24h"])[0],
                                          int(q.get("limit", ["40"])[0])))
            if u.path == "/api/projects":
                return self._json({"projects": _projects(Handler.store)})
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
                tail = q.get("tail", ["200"])[0]
                try:
                    tail = min(max(int(tail), 1), 1000)
                except ValueError:
                    tail = 200
                obj, code = _transcript_tail(q.get("file", [""])[0], tail)
                return self._json(obj, code)
            if u.path == "/api/summary":
                st = Handler.store
                return self._json({
                    "ts": time.time(),
                    "research": st.stats(),
                    "critique_matrix": st.critique_matrix(),
                    "build": st.build_stats(),
                    "limits": {f: config.family_limit(f) for f in config.FAMILY_ORDER},
                    "event_log": str(config.EVENTS_LOG),
                    "build_dir": str(config.BUILD_OUTPUT_DIR),
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
            if u.path == "/api/graphs":
                return self._json(_build_graph_topologies())
            if u.path == "/api/code":
                q = parse_qs(u.query)
                rel = q.get("file", [""])[0]
                root = Path(config.BUILD_OUTPUT_DIR).resolve()
                target = (root / rel).resolve()
                if not target.is_relative_to(root) or not target.is_file():
                    return self._json({"error": "not found"}, 404)
                return self._json({"file": rel, "code": target.read_text(encoding="utf-8", errors="replace")})
            return self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as exc:
            log.exception("handler error")
            try:
                self._json({"error": str(exc)}, 500)
            except Exception:
                pass

    def do_POST(self):
        u = urlparse(self.path)
        try:
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
            if u.path == "/api/projects/create":
                obj, code = _create_project(body)
                return self._json(obj, code)
            if u.path == "/api/projects/run":
                obj, code = _run_project(body)
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
    port = port or config.DASHBOARD_PORT
    db_path = db_path or config.DB_PATH
    Handler.store = Store(db_path)
    try:
        httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print(f"port {port} is already in use -- the dashboard is probably already running.")
            print(f"just open http://localhost:{port} in a browser (or run ./stop.sh, then start it again).")
            raise SystemExit(1)
        raise
    log.info("dashboard on http://0.0.0.0:%d (db=%s, events=%s)", port, db_path, config.EVENTS_LOG)
    print(f"dashboard: http://localhost:{port}", flush=True)
    for ip in _lan_addresses():
        print(f"  from your laptop/phone: http://{ip}:{port}  (small screens: http://{ip}:{port}/phone.html)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
