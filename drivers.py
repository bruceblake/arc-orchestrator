"""Headless CLI drivers for the coding harnesses (kimi, opencode).

Role map (hard rule): gpt-oss-120b handles very basic implementation,
DeepSeek-V4-Flash medium implementation, and GLM-5.3 / Kimi-K3 (kimi CLI) the
hard tasks plus all planning and reviewing. A task is always reviewed by the
*other* of kimi/glm when a strong model implemented it. ARC rejects over-limit
requests per model, so per-model semaphores cap concurrent harness instances
below the account limits (config.driver_limit).
"""
import asyncio
import contextlib
import json
import logging
import os
import pathlib
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path

import config
import events

log = logging.getLogger("drivers")
TRANSCRIPT_DIR = Path(config.ROOT) / "logs" / "harness"
# Liveness ping emitted from the _pump streaming loop. Unlike
# driver.progress (a /proc sample that only fires on a read timeout), this
# fires on a wall clock whether or not output is arriving, so the dashboard
# can show a per-agent heartbeat age and flag a stalled run.
HEARTBEAT_INTERVAL = 15


class DriverError(RuntimeError):
    def __init__(self, message, session_id=None):
        super().__init__(message)
        self.session_id = session_id


@dataclass
class DriverResult:
    harness: str
    model: str
    role: str
    exit_code: int
    session_id: str = None
    transcript_path: str = ""
    text: str = ""
    seconds: float = 0.0
    tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


_semaphores = {}


def _gate(model):
    if model not in _semaphores:
        _semaphores[model] = asyncio.Semaphore(config.driver_limit(model))
    return _semaphores[model]


def _harness_gate(harness):
    """The whole harness's slot, shared by every model it serves.

    Distinct from the per-model gate: opencode runs GLM, DeepSeek and gpt-oss
    through one local binary backed by one sqlite store, so their model caps
    sum to far more than the harness can survive.
    """
    key = f"harness:{harness}"
    if key not in _semaphores:
        _semaphores[key] = asyncio.Semaphore(config.harness_limit(harness))
    return _semaphores[key]


_lease_store = None


def _lease_db():
    global _lease_store
    if _lease_store is None:
        import store as _s
        _lease_store = _s.Store(config.DB_PATH)
    return _lease_store


async def _lease_acquire(model, task_id, emit_ctx, cap=None, report_as=None):
    """Wait until this model is below its cross-process cap (store holds the
    lease). Emits driver.cap_wait roughly once a minute while waiting.

    Bounded by config.DRIVER_LEASE_WAIT. This used to be `while True:`, and it
    runs BEFORE _once starts the attempt's clock — so a task queued behind a
    saturated model waited forever with neither DRIVER_TIMEOUT nor
    DRIVER_IDLE_TIMEOUT able to rescue it, and the run just sat there.
    """
    # The lease KEY and the name reported to the dashboard differ for a
    # harness lease: it is keyed "harness:opencode" but it belongs to a real
    # model's attempt, and reporting the key as the model both invented a
    # model that does not exist and split one attempt across two rows.
    shown = report_as or model
    waits = 0
    deadline = time.monotonic() + config.DRIVER_LEASE_WAIT
    # Push, with the poll kept as the backstop. A slot freed one second after a
    # poll used to go unnoticed for the rest of the 20s tick, and that lands on
    # exactly the PR-reviewer handoffs that are the fleet's scarcest resource.
    # The subscription is best-effort by design: a datagram can be dropped and
    # a publisher can die between release and notify, so the timeout below is
    # still what guarantees progress. Push only makes it arrive sooner.
    sub = _slot_subscription(model)
    try:
        return await _lease_wait_loop(model, task_id, emit_ctx, cap, shown,
                                      deadline, sub)
    finally:
        if sub is not None:
            with contextlib.suppress(Exception):
                sub.__exit__(None, None, None)


def _slot_subscription(model):
    """A push subscription for this lease key, or None if unavailable.

    Never fatal: the queue is an optimisation over a working poll loop, and a
    fleet that cannot open a unix socket should still run, more slowly.
    """
    try:
        import workqueue
        return workqueue.Queue(config.DB_PATH).subscribe(f"slot:{model}").__enter__()
    except Exception:
        return None


async def _lease_wait_loop(model, task_id, emit_ctx, cap, shown, deadline, sub):
    waits = 0
    while True:
        limit = config.driver_limit(model) if cap is None else cap
        in_use = _lease_db().acquire_driver_lease(
            model, os.getpid(), task_id, limit, config.DRIVER_LEASE_TTL)
        if in_use is None:
            return
        if time.monotonic() >= deadline:
            events.emit("driver.cap_timeout", model=shown, task=task_id,
                        in_use=in_use, cap=limit,
                        waited_s=round(config.DRIVER_LEASE_WAIT), **emit_ctx)
            # Worded to match Driver.is_capacity_error, so the retry ladder
            # uses the long capacity backoff rather than the crash schedule.
            raise DriverError(
                f"{shown} concurrent session limit: no driver slot after "
                f"{config.DRIVER_LEASE_WAIT:.0f}s ({in_use}/{limit} in use)")
        if waits % 3 == 0:
            events.emit("driver.cap_wait", model=shown, task=task_id,
                        in_use=in_use, cap=limit, **emit_ctx)
        waits += 1
        nap = min(20, max(1, deadline - time.monotonic()))
        if sub is not None:
            await sub.wait_async(nap)   # returns early the moment a slot frees
        else:
            await asyncio.sleep(nap)


def _lease_release(model, task_id):
    """Release a slot. Never raises — this runs in a `finally`.

    An exception from here would REPLACE whatever error the attempt was already
    unwinding with, turning a readable driver failure into a sqlite traceback
    from a cleanup path. A release that genuinely fails is recoverable on its
    own: the lease carries a TTL and reap_driver_leases also drops rows whose
    pid is gone. Masking the real error is not recoverable.
    """
    try:
        _lease_db().release_driver_lease(model, os.getpid(), task_id)
    except Exception as exc:
        log.warning("could not release %s lease for %s (%s); the TTL reaper "
                    "will clear it", model, task_id, exc)
    # Tell whoever is queued for this exact key that a slot just opened. Best
    # effort: a release must never fail because a notification could not be
    # delivered, so every error here is swallowed deliberately.
    with contextlib.suppress(Exception):
        import workqueue
        workqueue.Queue(config.DB_PATH).notify(f"slot:{model}")


def proc_snapshot(pid):
    """Cheap /proc forensics for a harness that has gone quiet.

    The question a stall raises is always the same: is the process spinning
    (an agent loop) or blocked (a hung API request)? `state` and the CPU
    counters answer it. 'S' with flat CPU and an open socket is a request the
    server never answered — which is what ARC does at its concurrency cap.
    """
    out = {}
    try:
        stat = pathlib.Path(f"/proc/{pid}/stat").read_text()
        # comm may contain spaces/parens; fields after the final ')' are fixed.
        fields = stat[stat.rfind(")") + 2:].split()
        ticks = os.sysconf("SC_CLK_TCK") or 100
        out["state"] = fields[0]
        out["cpu_s"] = round((int(fields[11]) + int(fields[12])) / ticks, 2)
        out["threads"] = int(fields[17])
    except (OSError, IndexError, ValueError):
        pass
    try:
        fds = pathlib.Path(f"/proc/{pid}/fd")
        socks = 0
        for fd in fds.iterdir():
            try:
                if os.readlink(fd).startswith("socket:"):
                    socks += 1
            except OSError:
                continue
        out["sockets"] = socks
    except OSError:
        pass
    return out


def _describe_record(obj):
    """One short label for a transcript record, across both harness shapes.

    opencode emits {"type": "step_finish"|"text"|"tool_use", ...}; kimi emits
    OpenAI-style {"role": "assistant"|"tool", "tool_calls": [...]}. What we
    want from either is the same: what was the agent doing.
    """
    if not isinstance(obj, dict):
        return None
    kind = obj.get("type")
    if isinstance(kind, str):
        tool = (obj.get("part") or {}).get("tool") if isinstance(obj.get("part"), dict) else None
        return f"{kind}:{tool}" if tool else kind
    role = obj.get("role")
    if not isinstance(role, str):
        return None
    calls = obj.get("tool_calls")
    if isinstance(calls, list) and calls:
        names = []
        for c in calls[:3]:
            fn = (c or {}).get("function") if isinstance(c, dict) else None
            name = (fn or {}).get("name") if isinstance(fn, dict) else None
            if name:
                names.append(str(name)[:30])
        if names:
            return f"{role}:{'+'.join(names)}"
    return role


def activity_tail(raw, n=6):
    """The last few things the harness did before going quiet.

    Names what the agent was doing at the moment it hung — which tool it had
    just called, whether it was mid-message — without dragging whole transcript
    payloads into the event log. If the hang always follows a particular kind
    of step, this is where that shows up.
    """
    # Split by LINES, not by a byte tail: one kimi record can be 50KB (a big
    # tool result), so slicing the last N characters lands mid-line and nothing
    # parses. Transcripts are already fully in memory here.
    out = []
    for line in reversed(raw.splitlines()[-40:]):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            label = _describe_record(json.loads(line))
        except ValueError:
            continue
        if label:
            out.append(label)
            if len(out) >= n:
                break
    return list(reversed(out))


def _wire_stall_evidence(task_id):
    """For kimi: does its session log end on an unanswered llm.request?

    That is the difference between "the agent is thinking" and "the API never
    replied", and it is the single most useful fact about one of these stalls.
    """
    if not task_id:
        return None
    base = re.sub(r"-x\d+$", "", task_id)
    root = pathlib.Path.home() / ".kimi-code" / "sessions"
    newest, newest_m = None, 0
    try:
        for d in root.glob(f"wd_{base}_*"):
            for wire in d.glob("*/agents/*/wire.jsonl"):
                try:
                    m = wire.stat().st_mtime
                except OSError:
                    continue
                if m > newest_m:
                    newest, newest_m = wire, m
    except OSError:
        return None
    if newest is None:
        return None
    last_req = last_done = None
    try:
        for line in newest.read_text(errors="replace").splitlines()[-400:]:
            if '"llm.request"' in line:
                last_req = line
                last_done = None
            elif '"usage.record"' in line or '"step.end"' in line:
                last_done = line
    except OSError:
        return None
    if last_req is None:
        return None
    if last_done is not None:
        return {"awaiting_api": False, "wire": newest.name}
    try:
        req = json.loads(last_req)
    except ValueError:
        req = {}
    ts = req.get("time")
    return {"awaiting_api": True,
            "model": req.get("model"),
            "waiting_s": round(time.time() - ts / 1000, 1) if isinstance(ts, (int, float)) else None,
            "session": newest.parent.parent.parent.name}


_fleet_cfg = {"key": None, "path": None}


def opencode_fleet_config():
    """Path to a fleet-scoped opencode config, or None to use the default.

    opencode ships a 131072-token limit and compacts at 75% of it, so it never
    compacted at all before requests got too large to come back. Its
    compaction DOES work (unlike kimi's), so a smaller budget is a genuine win
    — measured: two compactions inside one GLM-5.3 run, which then carried on
    to 621KB against ~350KB at the default. It sends the model KEY to the API,
    so a lower-limit alias is rejected; the budget has to come from a whole
    config file, selected per-process via $OPENCODE_CONFIG.

    Derived from the operator's own config so provider settings and API keys
    stay in one place, and regenerated whenever that source changes. The
    operator's interactive opencode keeps the full window.
    """
    if not config.USE_FLEET_ALIASES:
        return None
    src = config.OPENCODE_CONFIG
    try:
        st = src.stat()
    except OSError:
        return None
    key = (st.st_size, st.st_mtime_ns, config.OPENCODE_CONTEXT)
    if _fleet_cfg["key"] == key and _fleet_cfg["path"]:
        return _fleet_cfg["path"]
    try:
        doc = json.loads(src.read_text(encoding="utf-8"))
        for m in (doc.get("provider", {}).get("ARC", {}).get("models") or {}).values():
            m.setdefault("limit", {})["context"] = config.OPENCODE_CONTEXT
        doc.setdefault("compaction", {})["auto"] = True
        doc["compaction"].setdefault("threshold", 0.75)
        config.OPENCODE_FLEET_CONFIG.write_text(
            json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError) as exc:
        log.warning("could not build the fleet opencode config: %s", exc)
        return None
    _fleet_cfg.update(key=key, path=str(config.OPENCODE_FLEET_CONFIG))
    return _fleet_cfg["path"]


async def _terminate(proc):
    """Kill a harness process if it is still running; safe to call twice."""
    if proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), 10)
    except asyncio.TimeoutError:
        log.warning("harness pid %s did not exit after SIGKILL", proc.pid)


def _dig(obj, texts, sid_holder):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("session_id", "sessionId", "session") and isinstance(v, str) and not sid_holder[0]:
                sid_holder[0] = v
            elif k in ("text", "content") and isinstance(v, str):
                texts.append(v)
            else:
                _dig(v, texts, sid_holder)
    elif isinstance(obj, list):
        for item in obj:
            _dig(item, texts, sid_holder)


def parse_transcript(raw):
    """(session_id, assistant-text tail) from captured stdout, defensive."""
    texts, sid_holder = [], [None]
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            _dig(json.loads(line), texts, sid_holder)
        except ValueError:
            continue
    return sid_holder[0], ("".join(texts) or raw)[-3000:]


def transcript_tokens(raw):
    """(tokens, prompt, completion) summed over opencode `step_finish` usage.

    opencode emits {"type":"step_finish", "part":{"tokens":{"total","input",
    "output","reasoning","cache":{"read","write"}}}} per step, where
    total = input + output + reasoning + cache.read + cache.write.
    Kimi stream-json carries no usage (kimi-code wire logs capture it instead).
    """
    tokens = prompt = completion = 0
    for line in raw.splitlines():
        if '"step_finish"' not in line or '"tokens"' not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        t = e.get("tokens") or (e.get("part") or {}).get("tokens")
        if not isinstance(t, dict):
            continue
        tokens += t.get("total") or 0
        cache = t.get("cache") or {}
        prompt += (t.get("input") or 0) + (cache.get("read") or 0) + (cache.get("write") or 0)
        completion += (t.get("output") or 0) + (t.get("reasoning") or 0)
    return tokens, prompt, completion


class Driver:
    harness = "?"
    model = "?"
    role = "?"

    def argv(self, prompt, session_id):
        raise NotImplementedError

    # Text ARC returns when the account is already at its per-model
    # concurrency cap. These are NOT crashes: the harness never got a slot, so
    # retrying 2s later just re-enters the same cap and deepens the pile-up
    # (observed live: four taskfiles resumed at once put 9 Kimi requests
    # against a cap of 3, and every retry came straight back as a 400).
    _CAPACITY_MARKERS = ("provider.api_error: 400", "status code (no body)",
                         "session limit", "concurrent", "rate limit", "429")

    @classmethod
    def is_capacity_error(cls, text):
        low = (text or "").lower()
        return any(m in low for m in cls._CAPACITY_MARKERS)

    async def run(self, prompt, worktree, session_id=None, task_id=None):
        attempt = 0
        sid = session_id
        continuation = None
        while True:
            attempt += 1
            # driver.queued, not driver.start: this attempt has not got a slot
            # yet. Emitting "start" here counted every QUEUED driver as
            # in-flight, which is how the dashboard once reported kimi at 9/3
            # and triggered a fleet-wide serialization that was never needed.
            # driver.start is emitted by _guarded_once once both slots are held.
            events.emit("driver.queued", harness=self.harness, model=self.model,
                        role=self.role, task=task_id, attempt=attempt,
                        resume=bool(sid), pid=os.getpid())
            try:
                result = await self._guarded_once(continuation or prompt, worktree,
                                                  sid, task_id, attempt)
            except asyncio.CancelledError:
                # Every driver.start needs a terminal event or the dashboard
                # counts this attempt as in-flight (and against the model's
                # cap) until its stale sweep fires ~19 minutes later.
                events.emit("driver.cancelled", harness=self.harness,
                            model=self.model, role=self.role, task=task_id,
                            attempt=attempt)
                raise
            except DriverError as exc:
                if exc.session_id and not sid:
                    sid = exc.session_id
                    continuation = (
                        "The previous attempt was interrupted by a timeout/harness "
                        "error before finishing. Do NOT restart from scratch: inspect "
                        "the files you already wrote in this worktree, then continue "
                        "exactly where you left off until the task is fully done."
                    )
                    events.emit("driver.resume", harness=self.harness,
                                model=self.model, task=task_id, attempt=attempt,
                                session_id=sid)
                # A stall where the process was blocked with an unanswered
                # request outstanding IS a capacity symptom, even though the
                # error text carries no 400 — retrying it on the crash ladder
                # walks straight back into whatever is saturated.
                capacity = self.is_capacity_error(str(exc)) or "unanswered for" in str(exc)
                events.emit("driver.error", harness=self.harness, model=self.model,
                            task=task_id, attempt=attempt, error=str(exc)[:300],
                            will_resume=bool(sid), capacity=capacity)
                if attempt > config.MAX_RETRIES:
                    raise
                if capacity:
                    backoff = min(config.DRIVER_CAPACITY_BACKOFF * attempt,
                                  config.DRIVER_CAPACITY_BACKOFF_CAP)
                    backoff += random.uniform(0, backoff * 0.25)
                else:
                    backoff = min(30, 2 ** attempt)
                log.warning("%s attempt %d failed (%s%s); retry in %.0fs",
                            self.model, attempt, "at capacity: " if capacity else "",
                            exc, backoff)
                await asyncio.sleep(backoff)
                continue
            events.emit("driver.done", harness=self.harness, model=self.model,
                        role=self.role, task=task_id, attempt=attempt,
                        seconds=round(result.seconds, 1),
                        tokens=result.tokens, prompt_tokens=result.prompt_tokens,
                        completion_tokens=result.completion_tokens)
            return result

    async def _guarded_once(self, prompt, worktree, sid, task_id, attempt):
        """One attempt, holding both concurrency slots.

        Releases the in-process semaphore and the cross-process DB lease on
        EVERY exit path. Previously only success and DriverError released them,
        so a missing harness binary, a bug in _once, or cancellation during
        shutdown leaked a semaphore permit for the life of the process and left
        a lease row pinning the model at cap until its 30-minute TTL.
        """
        gate = _gate(self.model)
        # Two different queues sit in front of every attempt, and only the
        # second one used to be instrumented. A task blocked here — on this
        # process's own semaphore — showed up nowhere at all, so a run with
        # more tasks than slots looked idle rather than queued.
        if gate.locked():
            events.emit("driver.slot_wait", harness=self.harness, model=self.model,
                        role=self.role, task=task_id, attempt=attempt,
                        scope="process", cap=config.driver_limit(self.model),
                        pid=os.getpid())
        await gate.acquire()
        try:
            # attempt MUST be here: the dashboard keys a wait on
            # (task, model, attempt) and settles it with the matching
            # driver.start. Without it a cap_wait keyed (task, model, None)
            # is never settled by a start keyed (task, model, 1), and the
            # queue view carries a phantom entry until it ages out.
            await _lease_acquire(self.model, task_id,
                                 {"harness": self.harness, "role": self.role,
                                  "attempt": attempt, "pid": os.getpid()})
            try:
                hgate = _harness_gate(self.harness)
                if hgate.locked():
                    events.emit("driver.slot_wait", harness=self.harness,
                                model=self.model, role=self.role, task=task_id,
                                attempt=attempt, scope="harness",
                                cap=config.harness_limit(self.harness),
                                pid=os.getpid())
                await hgate.acquire()
                try:
                    hkey = f"harness:{self.harness}"
                    await _lease_acquire(
                        hkey, task_id,
                        {"harness": self.harness, "role": self.role,
                         "attempt": attempt, "pid": os.getpid(),
                         "scope": "harness"},
                        cap=config.harness_limit(self.harness),
                        report_as=self.model)
                    try:
                # Both slots held: this driver is genuinely occupying capacity
                # now, so this is the event in-flight accounting must pair with.
                        events.emit("driver.start", harness=self.harness,
                                    model=self.model, role=self.role,
                                    task=task_id, attempt=attempt,
                                    pid=os.getpid())
                        return await self._once(prompt, worktree, sid,
                                                task_id, attempt)
                    finally:
                        _lease_release(hkey, task_id)
                finally:
                    hgate.release()
            finally:
                _lease_release(self.model, task_id)
        finally:
            gate.release()

    async def _once(self, prompt, worktree, session_id, task_id, attempt):
        argv = self.argv(prompt, session_id)
        t0 = time.monotonic()
        # opencode (bun/JS) resolves its project from $PWD rather than getcwd(),
        # and a subprocess inherits the parent's $PWD — set it to the worktree
        # or edits land wherever the orchestrator was launched from.
        env = dict(os.environ, PWD=str(worktree))
        if self.harness == "opencode":
            cfg = opencode_fleet_config()
            if cfg:
                env["OPENCODE_CONFIG"] = cfg
        TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        tpath = TRANSCRIPT_DIR / f"{task_id or 'adhoc'}-{self.role}-{attempt}.jsonl"
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(worktree), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        # Stream stdout to the transcript file as it arrives so the dashboard
        # can tail a live agent mid-run; stderr drains concurrently so a big
        # stderr never deadlocks the child on a full pipe.
        err_task = asyncio.create_task(proc.stderr.read())
        try:
            return await self._pump(proc, err_task, argv, tpath, t0,
                                    session_id, task_id, attempt)
        finally:
            # Never leave the harness running. Cancellation (a Stop from the
            # dashboard, SIGTERM, a draining graph) unwinds through here while
            # the child is mid-request, and an orphaned kimi/opencode keeps
            # holding an ARC slot long after the orchestrator that spawned it
            # is gone — which is what made a killed run make the cap WORSE.
            await _terminate(proc)
            err_task.cancel()

    async def _pump(self, proc, err_task, argv, tpath, t0,
                    session_id, task_id, attempt):
        """Drain the harness's stdout into the transcript until it exits.

        Also the instrumentation point. A harness that goes quiet is the fleet's
        dominant failure mode, and stdout silence alone cannot say why — so
        this samples /proc while the process is still alive (spinning or
        blocked?), records what the agent was last doing, and for kimi checks
        whether its session log ends on an unanswered llm.request. Those facts
        are gone the moment the process is killed, so they are gathered first.
        """
        chunks = []
        last_chunk_t = time.monotonic()
        last_progress_t = time.monotonic()
        last_hb_t = time.monotonic()
        last_hb = None  # (bytes, idle_s) as of the last driver.heartbeat
        last_cpu = None
        deadline = t0 + config.DRIVER_TIMEOUT
        interval = config.DRIVER_PROGRESS_INTERVAL

        def written():
            return sum(len(c) for c in chunks)

        try:
            with open(tpath, "wb") as fh:
                while True:
                    now = time.monotonic()
                    idle_for = now - last_chunk_t
                    if idle_for >= config.DRIVER_IDLE_TIMEOUT or now >= deadline:
                        raise asyncio.TimeoutError
                    # Heartbeat: at most one driver.heartbeat every
                    # HEARTBEAT_INTERVAL seconds, and only when something
                    # moved (bytes written or the idle clock ticked) — a pure
                    # wall-clock ping even for chatty agents that never hit
                    # the read-timeout progress path.
                    if (now - last_hb_t >= HEARTBEAT_INTERVAL
                            and last_hb != (written(), round(idle_for, 1))):
                        events.emit("driver.heartbeat", harness=self.harness,
                                    model=self.model, role=self.role,
                                    task=task_id, attempt=attempt,
                                    bytes=written(), idle_s=round(idle_for, 1),
                                    seconds=round(now - t0, 1))
                        last_hb_t = now
                        last_hb = (written(), round(idle_for, 1))
                    wait = max(0.05, min(deadline - now,
                                         config.DRIVER_IDLE_TIMEOUT - idle_for,
                                         interval - (now - last_progress_t),
                                         HEARTBEAT_INTERVAL - (now - last_hb_t)))
                    try:
                        chunk = await asyncio.wait_for(proc.stdout.read(65536), wait)
                    except asyncio.TimeoutError:
                        # Nothing arrived in this window. The loop head decides
                        # whether that is a stall; here we only emit a heartbeat
                        # so a live agent's progress is observable, and so the
                        # CPU delta at stall time has something to compare to.
                        if time.monotonic() - last_progress_t >= interval:
                            snap = proc_snapshot(proc.pid)
                            cpu = snap.get("cpu_s")
                            events.emit(
                                "driver.progress", harness=self.harness,
                                model=self.model, role=self.role, task=task_id,
                                attempt=attempt, bytes=written(),
                                idle_s=round(time.monotonic() - last_chunk_t, 1),
                                elapsed_s=round(time.monotonic() - t0, 1),
                                cpu_delta_s=(round(cpu - last_cpu, 2)
                                             if cpu is not None and last_cpu is not None
                                             else None),
                                **snap)
                            last_cpu = cpu
                            last_progress_t = time.monotonic()
                        continue
                    if not chunk:
                        break
                    chunks.append(chunk)
                    last_chunk_t = time.monotonic()
                    fh.write(chunk)
                    fh.flush()
        except asyncio.TimeoutError:
            # Gather evidence BEFORE the kill — /proc vanishes with the process.
            snap = proc_snapshot(proc.pid)
            partial = b"".join(chunks).decode(errors="replace")
            wire = _wire_stall_evidence(task_id) if self.harness == "kimi" else None
            await _terminate(proc)
            psid, _ = parse_transcript(partial)
            idle = round(time.monotonic() - last_chunk_t, 1)
            total = round(time.monotonic() - t0, 1)
            stalled = idle >= config.DRIVER_IDLE_TIMEOUT - 1
            kind = "stalled" if stalled else "timed out"
            limit = (f"{config.DRIVER_IDLE_TIMEOUT}s idle" if stalled
                     else f"{config.DRIVER_TIMEOUT}s total")
            cpu_delta = (round(snap["cpu_s"] - last_cpu, 2)
                         if last_cpu is not None and "cpu_s" in snap else None)
            # Blocked, burning no CPU, with an unanswered request outstanding =
            # the server never replied. That is a capacity symptom, not a crash,
            # and Driver.run backs off accordingly.
            blocked = (snap.get("state") in ("S", "D") and (cpu_delta or 0) < 0.5)
            events.emit("driver.stalled" if stalled else "driver.timeout",
                        harness=self.harness, model=self.model, role=self.role,
                        task=task_id, attempt=attempt, bytes=written(),
                        idle_s=idle, elapsed_s=total,
                        cpu_delta_s=cpu_delta, blocked=blocked,
                        last_activity=activity_tail(partial),
                        records=partial.count("\n"),
                        wire=wire, session_id=psid or session_id, **snap)
            detail = ""
            if wire and wire.get("awaiting_api"):
                detail = (f"; {wire.get('model')} request unanswered for "
                          f"{wire.get('waiting_s')}s")
            elif blocked:
                detail = "; process blocked with no CPU burn"
            raise DriverError(
                f"{argv[0]} {kind} after {limit} "
                f"(total {total}s, idle {idle}s, {written()} bytes{detail})",
                session_id=psid or session_id)
        err = await err_task
        await proc.wait()
        raw = b"".join(chunks).decode(errors="replace")
        sid, text = parse_transcript(raw)
        toks, ptok, ctok = transcript_tokens(raw)
        if proc.returncode != 0:
            # opencode reports plenty of its failures on STDOUT and exits with
            # an empty stderr, which produced the useless "opencode exited 1: "
            # — a message that says a run died and nothing about why, and that
            # the retry ladder then repeated four times per task. Fall back to
            # the tail of stdout, and say so when there is genuinely nothing.
            detail = err.decode(errors="replace").strip()
            if not detail:
                detail = (text or raw).strip()[-300:] or "no output on stdout or stderr"
            raise DriverError(f"{argv[0]} exited {proc.returncode}: {detail[-300:]}",
                              session_id=sid or session_id)
        return DriverResult(self.harness, self.model, self.role, proc.returncode,
                            session_id or sid, str(tpath), text,
                            round(time.monotonic() - t0, 1), toks, ptok, ctok)


class KimiDriver(Driver):
    harness = "kimi"
    model = "Kimi-K3"

    def __init__(self, role, bench=False):
        if not bench and role not in ("planner", "reviewer", "pr_reviewer",
                                      "implementer", "issue-triager",
                                      "issue-maker", "pr-reviewer"):
            raise ValueError("KimiDriver role must be planner|reviewer|"
                             "pr_reviewer|implementer|issue-triager|"
                             f"issue-maker|pr-reviewer, got {role!r}")
        self.role = role

    def argv(self, prompt, session_id):
        a = ["kimi"]
        alias = config.harness_model(self.model, "kimi")
        if alias:
            a += ["-m", alias]
        if session_id:
            a += ["--session", session_id]
        return a + ["-p", prompt, "--output-format", "stream-json"]


class OpencodeDriver(Driver):
    harness = "opencode"

    def __init__(self, model, role, bench=False):
        if not bench:
            # DeepSeek may also review an OPEN PR. Judging a bounded diff
            # against a spec is a materially smaller job than authoring the
            # change, and with only three cross-family-eligible models a
            # two-reviewer merge gate is otherwise unreachable whenever the
            # implementer is Kimi or GLM — which is most tasks. gpt-oss-120b
            # stays implement-only.
            if model == "gpt-oss-120b" and role != "implementer":
                raise ValueError(f"{model} may only implement, not {role!r}")
            if model == "DeepSeek-V4-Flash" and role not in ("implementer", "pr_reviewer"):
                raise ValueError(
                    f"{model} may only implement or review a PR, not {role!r}")
            if model == "GLM-5.3" and role not in ("planner", "reviewer",
                                                   "pr_reviewer", "implementer",
                                                   "issue-triager", "issue-maker",
                                                   "pr-reviewer"):
                raise ValueError(
                    f"GLM-5.3 may only plan/review/implement/gh-ops, not {role!r}")
            if model not in config.IMPLEMENTER_MODELS:
                raise ValueError(f"unmapped opencode model: {model!r}")
        self.model = model
        self.role = role

    def argv(self, prompt, session_id):
        alias = config.harness_model(self.model, "opencode") or f"ARC/{self.model}"
        a = ["opencode", "run", "-m", alias, "--auto", "--format", "json"]
        if session_id:
            a.append("-c")
        return a + [prompt]
