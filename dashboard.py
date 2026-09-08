"""Lightweight dashboard: static UI + JSON APIs over the event log and sqlite DB.

Run alongside the orchestrator (separate process):
    python main.py serve [--port 8787]
"""
import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import config
from store import Store

log = logging.getLogger("dashboard")

_lines_cache = {"key": None, "lines": []}
MAX_EVENTS_PER_RESPONSE = 3000


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


_kimi_cache = {}  # wire.jsonl path -> {"key": (size, mtime_ns), "agg": parsed}
STALE_INFLIGHT_S = 600  # ignore unanswered llm.request older than this (dead client)


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
    alias_real = {}
    last_req = None  # (ts_s, real_model, agent)
    last_done = 0.0
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
            if ts:
                mod["last_ts"] = ts
                recent.append((ts, 1, 0))
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
            if ts:
                mod["last_ts"] = ts
                recent.append((ts, 0, inp + out))
    return {"models": models, "alias_real": alias_real, "recent": recent,
            "last_req": last_req, "last_done": last_done}


def _kimi_code_usage(now):
    """Aggregate all kimi-code session logs into dashboard-shaped rows."""
    root = Path.home() / ".kimi-code" / "sessions"
    window_start = int((now - 3600) // 60)
    series = [{"t": (window_start + i) * 60, "requests": 0, "tokens": 0} for i in range(61)]
    agg = {}
    inflight = []
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
            row = agg.setdefault(real, {"model": real, "family": "kimi-code", "requests": 0,
                                        "ok": 0, "errors": 0, "failed_attempts": 0, "tokens": 0,
                                        "prompt_tokens": 0, "completion_tokens": 0,
                                        "avg_latency_ms": None, "last_ts": None})
            row["requests"] += m["requests"]
            row["ok"] += m["ok"]
            row["prompt_tokens"] += m["prompt"]
            row["completion_tokens"] += m["completion"]
            if m["last_ts"] and (row["last_ts"] is None or m["last_ts"] > row["last_ts"]):
                row["last_ts"] = m["last_ts"]
        for ts, req, tok in data["recent"]:
            idx = int(ts // 60) - window_start
            if 0 <= idx < len(series):
                series[idx]["requests"] += req
                series[idx]["tokens"] += tok
        lr = data["last_req"]
        if lr and lr[1] != "unknown" and lr[0] > data["last_done"] and (now - lr[0]) < STALE_INFLIGHT_S:
            inflight.append({"req_id": "kimi-code/" + lr[2], "family": "kimi-code",
                             "model": lr[1], "purpose": "kimi-code session",
                             "websearch": False, "started": lr[0],
                             "elapsed_s": round(now - lr[0], 1)})
    for sp in [p for p in _kimi_cache if p not in live]:
        del _kimi_cache[sp]
    models = list(agg.values())
    for m in models:
        m["tokens"] = m["prompt_tokens"] + m["completion_tokens"]
    return {"models": models, "inflight": inflight, "series": series}


def _usage():
    """Usage aggregates over the event log for the /api/usage endpoint."""
    now = time.time()

    def new_model(model, family):
        return {"model": model, "family": family, "requests": 0, "ok": 0, "errors": 0,
                "failed_attempts": 0, "tokens": 0, "prompt_tokens": 0, "completion_tokens": 0,
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
    starts = {}
    totals = {"requests": 0, "ok": 0, "errors": 0, "failed_attempts": 0, "tokens": 0,
              "prompt_tokens": 0, "completion_tokens": 0}
    window_start = int((now - 3600) // 60)
    series = {f: [{"t": (window_start + i) * 60, "requests": 0, "tokens": 0} for i in range(61)]
              for f in config.FAMILY_ORDER}

    for line in _load_event_lines():
        try:
            e = json.loads(line)
        except Exception:
            continue
        etype = e.get("type")
        if etype not in ("request", "request_start", "request_end"):
            continue
        model = e.get("model") or "unknown"
        family = e.get("family") or "unknown"
        mod = by_model.setdefault((family, model), new_model(model, family))
        fam = by_family.setdefault(family, new_family(family))
        if etype == "request_start":
            rid = e.get("req_id")
            if rid:
                starts[rid] = e
            continue
        rid = e.get("req_id")
        if rid:
            starts.pop(rid, None)
        if etype == "request_end":
            mod["failed_attempts"] += 1
            fam["failed_attempts"] += 1
            totals["failed_attempts"] += 1
            continue
        mod["requests"] += 1
        fam["requests"] += 1
        totals["requests"] += 1
        mod["last_ts"] = e.get("ts")
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
        else:
            mod["errors"] += 1
            fam["errors"] += 1
            totals["errors"] += 1
        ts = e.get("ts")
        if isinstance(ts, (int, float)) and e.get("ok"):
            idx = int(ts // 60) - window_start
            pts = series.get(family)
            if pts is not None and 0 <= idx < len(pts):
                pts[idx]["requests"] += 1
                pts[idx]["tokens"] += e.get("tokens") or 0

    inflight = []
    for rid, e in starts.items():
        try:
            started = float(e.get("ts") or now)
        except (TypeError, ValueError):
            started = now
        inflight.append({"req_id": rid, "family": e.get("family"), "model": e.get("model"),
                         "purpose": e.get("purpose"), "websearch": bool(e.get("websearch")),
                         "started": started, "elapsed_s": round(max(0.0, now - started), 1)})
    inflight.sort(key=lambda r: -r["elapsed_s"])
    for row in inflight:
        fam = by_family.get(row["family"])
        if fam is not None:
            fam["inflight"] += 1

    models = []
    for mod in by_model.values():
        if mod["ok"]:
            mod["avg_latency_ms"] = round(mod["latency_total_ms"] / mod["ok"])
        del mod["latency_total_ms"]
        models.append(mod)
    models.sort(key=lambda m: -m["requests"])

    families = [by_family[f] for f in config.FAMILY_ORDER]
    families += [v for k, v in sorted(by_family.items()) if k not in config.FAMILY_ORDER]

    # Merge kimi-code CLI sessions so the dashboard also shows interactive traffic,
    # which goes straight to llm-api.arc.vt.edu and never touches the event log.
    kimi = _kimi_code_usage(now)
    if kimi["models"] or kimi["inflight"]:
        fam = new_family("kimi-code")
        for row in kimi["models"]:
            fam["requests"] += row["requests"]
            fam["ok"] += row["ok"]
            fam["tokens"] += row["tokens"]
            totals["requests"] += row["requests"]
            totals["ok"] += row["ok"]
            totals["tokens"] += row["tokens"]
            totals["prompt_tokens"] += row["prompt_tokens"]
            totals["completion_tokens"] += row["completion_tokens"]
        fam["inflight"] = len(kimi["inflight"])
        families.append(fam)
        series["kimi-code"] = kimi["series"]
        models.extend(kimi["models"])
        models.sort(key=lambda m: -m["requests"])
        inflight.extend(kimi["inflight"])
        inflight.sort(key=lambda r: -r["elapsed_s"])
    return {"now": now, "models": models, "families": families,
            "inflight": inflight, "totals": totals, "series": series}


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
    server_version = "ArcDashboard/1.0"
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
            if u.path == "/api/usage":
                return self._json(_usage())
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
                chunk = lines[start:start + MAX_EVENTS_PER_RESPONSE]
                events = []
                for line in chunk:
                    try:
                        events.append(json.loads(line))
                    except Exception:
                        pass
                return self._json({"events": events, "next": start + len(chunk), "reset": reset})
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


def serve(port=None, db_path=None):
    port = port or config.DASHBOARD_PORT
    db_path = db_path or config.DB_PATH
    Handler.store = Store(db_path)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.info("dashboard on http://0.0.0.0:%d (db=%s, events=%s)", port, db_path, config.EVENTS_LOG)
    print(f"dashboard: http://localhost:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass