"""Headless CLI drivers for the coding harnesses (opencode, reasonix; dsh bench-only, kimi retired).

Role map (hard rule): the fleet is TWO models since 2026-09-12 —
DeepSeek-V4.1-Flash-thinking-max (the `reasonix` harness) implements and
reviews/PR-reviews the medium tier, and GLM-5.3 (opencode) plans, implements
the hard tier, and reviews. A task is always reviewed by a *different model
family* than the one that implemented it (Rule 2). ARC rejects over-limit
requests per model, so per-model semaphores cap concurrent harness instances
below the account limits (config.driver_limit).

KimiDriver remains importable so historical transcripts and harness_runs rows
still resolve, but its model is off the roster: constructing it raises
ValueError (Kimi-K3 retired by operator decision 2026-09-12).
"""
import asyncio
import contextlib
import json
import logging
import os
import pathlib
import random
import re
import signal
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import config
import errors
import events
import graft

log = logging.getLogger("drivers")
TRANSCRIPT_DIR = Path(config.ROOT) / "logs" / "harness"
# Liveness ping emitted from the _pump streaming loop. Unlike
# driver.progress (a /proc sample that only fires on a read timeout), this
# fires on a wall clock whether or not output is arriving, so the dashboard
# can show a per-agent heartbeat age and flag a stalled run.
HEARTBEAT_INTERVAL = 15


class DriverError(RuntimeError):
    def __init__(self, message, session_id=None, capacity=False,
                 usage_limit=False, resets_at=None):
        super().__init__(message)
        self.session_id = session_id
        # Set when a subscription plan refused on its usage window; resets_at
        # is the epoch it said the window reopens (None when it did not say).
        # Classified at the exit site, where the WHOLE transcript is still in
        # hand — the message above keeps only its last 300 characters, which
        # can cut the reset time off.
        self.usage_limit = usage_limit
        self.resets_at = resets_at
        # Set when the harness exited non-zero because ARC refused the request
        # (queue full / session limit) rather than because it crashed. The
        # exited-1 paths in _pump/_pump_dual cannot call the caller's ladders,
        # so they carry the classification on the exception; Driver.run reads
        # it so a capacity exit is retried on the capacity backoff and is NOT
        # stored as a defect. Without this every "backend queue is full" exit
        # landed in the error triage table as a crash (172 occurrences of one
        # fingerprint, all capacity).
        self.capacity = capacity


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
    """In-process semaphore, sized to the model's FULL cap.

    The interactive reservation is enforced by the cross-process LEASE, not
    here: that is the one every run process shares, so it is the only place a
    reservation actually reserves anything. Sizing this to the batch cap as
    well would double-charge the reservation and starve interactive work of
    the slot it was held for.
    """
    if model not in _semaphores:
        _semaphores[model] = asyncio.Semaphore(config.driver_limit(model, interactive=True))
    return _semaphores[model]


def _harness_gate(harness):
    """The whole harness's slot, shared by every model it serves.

    Distinct from the per-model gate: every opencode-backed model (GLM-5.3
    today; DeepSeek, GLM and gpt-oss historically) runs through one local
    binary backed by one sqlite store, so their model caps can sum to more
    than the harness can survive.
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


def arc_reachable(timeout=6.0):
    """(reachable, detail). Probes BASE_URL; a 403 naming the VPN is the down state.

    Any HTTP answer that is not that 403 counts as reachable — a 404 or 401 from
    the API root still proves the network path exists. Only the VPN 403 and a
    connection failure mean the fleet should wait rather than spend a driver.
    """
    import urllib.error
    import urllib.request
    url = config.BASE_URL.rstrip("/") + "/models"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "arc-orchestrator"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read(400).decode(errors="replace")
        except Exception:
            pass
        if exc.code == 403 and Driver.is_vpn_error(body):
            return False, "VPN: " + body.strip()[:120]
        return True, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return False, f"unreachable: {str(exc)[:120]}"


_vpn_down_since = None


async def wait_for_arc(task_id=None, poll_s=30.0):
    """Block until ARC is reachable, emitting one event on the way down and up.

    Polls slowly on purpose: the VPN comes back when a human reconnects it,
    which is minutes to hours, not seconds. Nothing is spent while waiting —
    no driver, no lease, no attempt.
    """
    global _vpn_down_since
    up, detail = await asyncio.to_thread(arc_reachable)
    if up:
        return
    if _vpn_down_since is None:
        _vpn_down_since = time.time()
        events.emit("arc.unreachable", detail=detail, task=task_id)
        log.error("ARC unreachable (%s) — the fleet is waiting, not retrying", detail)
    while True:
        await asyncio.sleep(poll_s)
        up, detail = await asyncio.to_thread(arc_reachable)
        if up:
            down_for = round(time.time() - (_vpn_down_since or time.time()))
            _vpn_down_since = None
            events.emit("arc.reachable", after_s=down_for, task=task_id)
            log.warning("ARC reachable again after %ds — resuming", down_for)
            return


# --- subscription usage windows ---------------------------------------------
# A plan-backed harness (Claude Code, Codex) that has spent its usage window
# refuses every request until the window resets. That is not a crash (the
# fix loop would repair code nobody criticised) and not ARC-style contention
# (a 10-minute backoff walks straight back into the same refusal for hours):
# the only correct response is to wait for the reset. The phrasings below are
# the plans' own — "Claude AI usage limit reached|<epoch>", "You've hit your
# limit · resets 3pm (Europe/London)", "5-hour limit reached", Codex's
# "You've hit your usage limit ... try again in 2 hours 13 minutes" and its
# `usage_limit_reached` error code. ARC's "concurrent session limit reached"
# deliberately does NOT match: that is capacity, and clears in seconds.
_USAGE_LIMIT_RE = re.compile(
    r"usage[ _]limit|hit your (?:usage )?limit|"
    r"\b(?:5|five)[- _]hour limit|\bweekly limit|\bseven[ _]day limit|"
    r"\bopus limit", re.I)


def is_usage_limit(text):
    """True when `text` is a subscription plan refusing on its usage window."""
    return bool(_USAGE_LIMIT_RE.search(text or ""))


_UNIT_S = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def _rejected_window(text):
    """From a stream carrying a REJECTED `rate_limit_event` (Claude Code's
    per-window status object), the epoch it resets — 0.0 when it names none.
    None when no window in the stream was rejected."""
    found = None
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{") or "rejected" not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        info = obj.get("rate_limit_info") if isinstance(obj, dict) else None
        if isinstance(info, dict) and info.get("status") == "rejected":
            at = info.get("resetsAt") or info.get("resets_at")
            if isinstance(at, (int, float)) and at > 0:
                return at / 1000 if at > 1e12 else float(at)
            found = 0.0
    return found


def usage_reset_at(text, now=None):
    """Epoch seconds the refused usage window resets, or None if unstated.

    Checked most-specific first: an explicit epoch (Claude's `...|<epoch>`
    suffix, or a stream `rate_limit_event` marked rejected with `resetsAt`),
    then a relative "try again in 2 hours 13 minutes" / `resets_in_seconds`,
    then a wall-clock "resets 3pm (Zone)" — the next such time after `now`.
    A stray `resets_in_seconds` on a window that is NOT exhausted (Codex
    reports every window on every turn) is ignored unless it sits in an
    object that says the limit was reached.
    """
    now = time.time() if now is None else now
    text = text or ""
    # Stream objects first: they carry machine-readable reset times.
    at = _rejected_window(text)
    if at:
        return at
    m = re.search(r"limit reached\|(\d{10,13})\b", text, re.I)
    if m:
        at = int(m.group(1))
        return at / 1000 if at > 1e12 else float(at)
    m = re.search(r'"resets_in_seconds"\s*:\s*(\d+)', text)
    if m and re.search(r"usage_limit_reached|limit_reached", text):
        return now + int(m.group(1))
    m = re.search(r"try again in ((?:\s*(?:and\s+|,\s*)?\d+\s*"
                  r"(?:days?|hours?|hrs?|minutes?|mins?|seconds?|secs?|[dhms])\b)+)",
                  text, re.I)
    if m:
        total = sum(int(n) * _UNIT_S[u[0].lower()]
                    for n, u in re.findall(r"(\d+)\s*([a-z]+)", m.group(1), re.I))
        if total:
            return now + total
    m = re.search(r"(?:resets?\s+(?:at\s+)?|try again at\s+)"
                  r"(\d{1,2})(?::(\d{2}))?\s*([ap]m)"
                  r"(?:\s*\(([A-Za-z_]+/[A-Za-z_/]+)\))?", text, re.I)
    if m:
        import datetime
        tz = None
        if m.group(4):
            try:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(m.group(4))
            except Exception:
                tz = None
        hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
        cur = datetime.datetime.fromtimestamp(now, tz)
        at = cur.replace(hour=hour, minute=int(m.group(2) or 0),
                         second=0, microsecond=0)
        if at.timestamp() <= now:
            at += datetime.timedelta(days=1)
        return at.timestamp()
    return None


# A refusal that names no reset (Codex's "try again at 3:39 PM" used to miss
# the parser) must stay visible. Six hours covers a 5-hour Claude window
# without leaving yesterday's refusal on the board all week.
_PLAN_UNKNOWN_HOLD_S = 6 * 3600
_PLAN_LABEL = {"claude": "Claude", "codex": "Codex", "cursor": "Cursor",
               "agy": "Antigravity"}


def active_plan_windows(lines, now=None):
    """Subscription plans that are spent right now, one row per harness.

    Read from the event log, not from a run process's memory: the dashboard
    and the captain are other processes, and a swap onto another seat used
    to make the spent plan disappear. A later `driver.done` on the same
    harness means a request got through, so the window is clear. A stated
    `resets_at` in the future stays up even when the run has moved on.
    """
    now = time.time() if now is None else now
    limits, swaps, dones = {}, {}, {}
    for line in lines or []:
        if isinstance(line, dict):
            e = line
        else:
            if '"driver.usage_limit"' not in line and '"driver.usage_swap"' not in line \
                    and '"driver.done"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
        kind = e.get("type")
        harness = e.get("harness")
        ts = e.get("ts") or 0
        if not harness or kind not in ("driver.usage_limit", "driver.usage_swap",
                                       "driver.done"):
            continue
        if kind == "driver.done":
            dones[harness] = max(dones.get(harness, 0), ts)
        elif kind == "driver.usage_swap":
            prev = swaps.get(harness)
            if prev is None or ts >= prev.get("ts", 0):
                swaps[harness] = e
        else:
            prev = limits.get(harness)
            if prev is None or ts >= prev.get("ts", 0):
                limits[harness] = e
    out = []
    for harness, rec in limits.items():
        ts = rec.get("ts") or 0
        if dones.get(harness, 0) > ts:
            continue
        stated = rec.get("resets_at")
        if not isinstance(stated, (int, float)) or stated <= 0:
            stated = usage_reset_at(rec.get("error") or "", ts)
        future = stated if isinstance(stated, (int, float)) and stated > now else None
        if future is None and ts + _PLAN_UNKNOWN_HOLD_S <= now:
            continue
        swap = swaps.get(harness)
        swapped = None
        if swap and (swap.get("ts") or 0) >= ts:
            swapped = swap.get("to_model")
        out.append({
            "harness": harness,
            "label": _PLAN_LABEL.get(harness, harness),
            "model": rec.get("model"),
            "since": ts,
            "resets_at": future,
            "swapped_to": swapped,
            "task": rec.get("task"),
        })
    out.sort(key=lambda r: r["label"])
    return out


# harness -> epoch its plan's usage window resets. Shared by every driver in
# this process, so once one attempt learns the plan is out, its siblings wait
# for the same reset instead of each spending a refusal to find out.
_usage_blocked_until = {}

# Failover order when a plan window is spent. Cursor is its own subscription,
# so it is tried before Claude: a Codex refusal should not spend the planner's
# Claude plan while `agent` still has quota. Same-harness models are never
# substitutes — another GPT on Codex is the same ChatGPT window.
# Cursor's Grok first, then Antigravity, then GLM-5.3 on ARC (unlimited
# usage, operator directive 2026-09-24: spend free seats before the smallest
# plan), then Claude and Codex, then OpenCode Zen free implementers
# (preference 5), then billed API models (6). The tier floor still applies,
# so DeepSeek (medium) only substitutes for medium work.
_SWAP_PREFERENCE = {"cursor": 0, "agy": 1, "reasonix": 2, "opencode": 2,
                    "claude": 3, "codex": 4}
_ZEN_SWAP_RANK = 5


def _tier_rank(model):
    tier = config.MODEL_TIER.get(model)
    return config.TIER_ORDER.index(tier) if tier in config.TIER_ORDER else -1


def usage_substitute(model, harness, role="implementer", exclude=(),
                     avoid_families=(), allow_planner=False):
    """A same-or-stronger model on a harness that is not `harness` and not blocked.

    None when swapping is off, or no seat qualifies — the caller then parks
    until this harness's window resets, which is the old behaviour.

    Implementation, gate review and PR review all swap. A planner stays put:
    the plan is one seat on purpose. A substitute must hold the role on the
    roster, sit at the same tier or above (Rule 1: a hard task never quietly
    drops to a free medium model), and come from none of `avoid_families`.
    Review callers pass the implementer's family, so a swapped reviewer
    cannot land in the family that wrote the code (Rule 2).

    ``allow_planner`` is the captain autopilot's opt-in (a driver with
    ``planner_swap`` set): its turns are advisory, not a plan, so a spent
    PLANNER_MODEL window moves to another planner-capable seat (GLM-5.3).
    """
    roles = ("implementer", "reviewer", "pr_reviewer") + (
        ("planner",) if allow_planner else ())
    if not config.USAGE_SWAP or role not in roles:
        return None
    now = time.time()
    skip = set(exclude)
    skip.add(model)
    avoid = set(avoid_families)
    floor = _tier_rank(model)
    ranked = []
    for candidate, cand_harness in config.MODEL_HARNESS.items():
        if candidate in skip or cand_harness == harness:
            continue
        if not config.model_may(candidate, role):
            continue
        if _tier_rank(candidate) < floor:
            continue
        if config.MODEL_FAMILY.get(candidate) in avoid:
            continue
        if _usage_blocked_until.get(cand_harness, 0) > now:
            continue
        if candidate.startswith("Zen-"):
            rank = _ZEN_SWAP_RANK
        else:
            rank = _SWAP_PREFERENCE.get(cand_harness, 6)
        ranked.append((rank, candidate))
    if not ranked:
        return None
    ranked.sort()
    return ranked[0][1]


async def wait_for_usage_reset(harness, model, task_id, until, budget_s):
    """Sleep until `until` (epoch) or `budget_s` runs out; returns seconds slept.

    Emits driver.usage_wait every few minutes so the dashboard can show a
    task that is parked on a plan window rather than dead.
    """
    slept = 0.0
    while True:
        left = min(until - time.time(), budget_s - slept)
        if left <= 0:
            return slept
        events.emit("driver.usage_wait", harness=harness, model=model,
                    task=task_id, resets_at=round(until),
                    remaining_s=round(left))
        step = min(left, 300.0)
        await asyncio.sleep(step)
        slept += step


async def _lease_acquire(model, task_id, emit_ctx, cap=None, report_as=None,
                         interactive=False):
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
                                      deadline, sub, interactive)
    finally:
        if sub is not None:
            with contextlib.suppress(Exception):
                sub.__exit__(None, None, None)


_slot_queue = None


def _slot_q():
    """The shared Queue for slot notifications, or None if it cannot be opened.

    Cached deliberately. Constructing one opens a sqlite connection AND re-runs
    the schema script; doing that per lease release measured 69x the cost of
    reusing it, on a path that runs on every driver attempt. It is also the
    difference between one connection and a churn of them.
    """
    global _slot_queue
    # Tests (and `--db`) repoint config.DB_PATH; a handle cached against the
    # old path would quietly write to the wrong database.
    if _slot_queue is not None and _slot_queue.db_path != str(config.DB_PATH):
        _slot_queue = None
    if _slot_queue is None:
        try:
            import workqueue
            _slot_queue = workqueue.Queue(config.DB_PATH)
        except Exception:
            return None
    return _slot_queue


def _slot_subscription(model):
    """A push subscription for this lease key, or None if unavailable.

    Never fatal: the queue is an optimisation over a working poll loop, and a
    fleet that cannot open a unix socket should still run, more slowly.
    """
    q = _slot_q()
    if q is None:
        return None
    try:
        return q.subscribe(f"slot:{model}").__enter__()
    except Exception:
        return None


async def _lease_wait_loop(model, task_id, emit_ctx, cap, shown, deadline, sub,
                           interactive=False):
    waits = 0
    while True:
        limit = config.driver_limit(model, interactive) if cap is None else cap
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
        q = _slot_q()
        if q is not None:
            q.notify(f"slot:{model}")


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
    # reasonix: {"kind": "tool_dispatch"|"tool_result"|"message"|"usage"...}
    rk = obj.get("kind")
    if isinstance(rk, str):
        tool = obj.get("tool") if isinstance(obj.get("tool"), dict) else None
        name = tool.get("name") if tool else None
        return f"{rk}:{name}" if name else rk
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


def _kn(n):
    """23_804 -> "23.8k"; small numbers stay plain; junk becomes "?"."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _squash(text):
    """Whitespace-collapse a paragraph fragment to single spaces."""
    return " ".join(str(text or "").split())


def _fold(text, head=700, tail=500):
    """Keep the head and tail of a long paragraph, counting the folded middle."""
    if len(text) <= head + tail:
        return text
    omitted = len(text) - head - tail
    return f"{text[:head]}\n ⋯ [+{omitted} chars folded] ⋯\n{text[len(text) - tail:]}"


class TranscriptActivity:
    """Incremental JSONL -> readable-blocks reducer for harness transcripts.

    A raw tail of a reasonix transcript is ~95% 2-4 char reasoning delta
    fragments (44k fragments against ~350 actionable records in a 4 MB file),
    so the drawer showed disconnected scraps and NO thinking. This reducer
    folds the stream into one block per THING — a folded reasoning paragraph,
    a tool call, its result — and tracks how far it consumed, so a follower
    can feed it only the bytes appended since the last poll.
    """

    def __init__(self, maxlen=400):
        self.blocks = deque(maxlen=maxlen)
        # Every block ever emitted. The deque evicts history past maxlen, but
        # this counter never goes backwards — it is the "total_blocks" the
        # dashboard reports, so a long run does not saturate at 400.
        self.produced = 0
        self._rest = ""                    # trailing partial line, not yet a record
        self._delta = []                   # open delta-group text parts
        self._delta_kind = None            # "reasoning" | "text"
        self._delta_id = None              # (messageId, attemptId)
        self.unknown = 0                   # complete lines we could not place
        # (messageId, squashed) of the text groups already rendered as ✎:
        # a reasonix `message` record is the FINAL of its text deltas, and
        # rendering both would double every assistant message.
        self._emitted_text = deque(maxlen=8)

    def _push(self, block):
        self.blocks.append(block)
        self.produced += 1

    def feed(self, chunk):
        """Consume a piece of the stream; complete records become blocks."""
        parts = (self._rest + chunk).split("\n")
        self._rest = parts.pop()           # last segment may not be complete yet
        for line in parts:
            self._line(line)

    def finish(self):
        """Flush the trailing partial line and any still-open delta group."""
        rest, self._rest = self._rest, ""
        if rest.strip():
            self._line(rest)
        self._flush_delta()

    def pending(self):
        """What the agent is doing RIGHT NOW, or None between records."""
        if not self._delta_kind:
            return None
        text = _squash("".join(self._delta))
        if not text:
            return None
        if self._delta_kind == "reasoning":
            return f"💭 … thinking ({_kn(len(text))} chars so far): {text[-260:]}"
        return f"✎ writing ({_kn(len(text))} chars so far): {text[-260:]}"

    def _line(self, line):
        line = line.strip()
        if not line:
            return
        if not line.startswith("{"):
            self.unknown += 1
            return
        try:
            obj = json.loads(line)
        except ValueError:
            self.unknown += 1
            return
        if not isinstance(obj, dict):
            self.unknown += 1
            return
        kind = obj.get("kind")
        if isinstance(kind, str):
            if kind in ("reasoning", "text"):
                self._delta_part(kind, obj)
                return
            self._flush_delta()
            self._reasonix(kind, obj)
            return
        typ = obj.get("type")
        self._flush_delta()
        if typ == "result":                # reasonix run summary — before opencode
            self._result(obj)
        elif isinstance(typ, str):
            self._opencode(typ, obj)
        else:
            self.unknown += 1

    # -- delta groups -------------------------------------------------------

    def _delta_part(self, kind, obj):
        did = (obj.get("messageId"), obj.get("attemptId"))
        if self._delta_kind and (kind != self._delta_kind or did != self._delta_id):
            self._flush_delta()
        self._delta_kind = kind
        self._delta_id = did
        self._delta.append(obj.get("text") or "")

    def _flush_delta(self):
        if not self._delta_kind:
            return
        kind, mid = self._delta_kind, (self._delta_id or (None, None))[0]
        text = _squash("".join(self._delta))
        self._delta, self._delta_kind, self._delta_id = [], None, None
        if not text:
            return
        if kind == "reasoning":
            self._push("💭 " + _fold(text))
        else:
            self._push("✎ " + _fold(text))
            self._emitted_text.append((mid, text))

    # -- reasonix ({kind: ...}) ---------------------------------------------

    def _reasonix(self, kind, obj):
        tool = obj.get("tool") if isinstance(obj.get("tool"), dict) else {}
        if kind == "tool_dispatch":
            if tool.get("partial"):
                return                     # streaming stub; the final one follows
            self._push("🔧 " + self._tool_line(tool))
        elif kind == "tool_result":
            state = tool.get("runState")
            mark = "" if state in ("completed", None) else "✗ "
            self._push("   ↳ " + mark + _squash(tool.get("output"))[:220])
        elif kind == "message":
            text = _squash(obj.get("text"))
            if not text:
                return
            for mid, emitted in self._emitted_text:
                if mid == obj.get("messageId") and emitted == text:
                    return                 # already shown as the ✎ text block
            self._push("✉ " + _fold(text, head=300, tail=200))
        elif kind == "user_message":
            text = _squash(obj.get("text"))
            if text:
                self._push("▸ " + _fold(text, head=250, tail=150))
        elif kind == "usage" and isinstance(obj.get("usage"), dict):
            u = obj["usage"]
            self._push(f"— usage {_kn(u.get('totalTokens'))} tok "
                               f"(+{_kn(u.get('completionTokens'))} out, "
                               f"{_kn(u.get('cacheHitTokens'))} cached)")
        elif kind == "notice":
            text = _squash(obj.get("text") or obj.get("message"))
            detail = _squash(obj.get("detail"))
            if text:
                self._push(f"— {text}" + (f" ({detail})" if detail else ""))
        # turn_started/turn_phase/stream_attempt/tool_started/tool_progress/
        # read_status/… : bookkeeping, not activity.

    @staticmethod
    def _tool_line(tool):
        name = tool.get("name") or "tool"
        arg = ""
        raw = tool.get("args")
        if isinstance(raw, str):
            try:
                args = json.loads(raw)
            except ValueError:
                args = None
            if isinstance(args, dict):
                for key in ("path", "file_path", "filePath", "pattern",
                            "command", "query", "url", "description"):
                    if args.get(key):
                        arg = _squash(args[key])[:90]
                        break
        return f"{name} {arg}" if arg else name

    def _result(self, obj):
        text = obj.get("result")
        if not isinstance(text, str):
            text = obj.get("text")
        mark = "✗" if obj.get("is_error") else "✓"
        body = _squash(text)
        self._push(f"{mark} result: " + _fold(body, head=300, tail=200)
                           if body else f"{mark} result")

    # -- opencode ({type: ...}) ----------------------------------------------

    def _opencode(self, typ, obj):
        part = obj.get("part") if isinstance(obj.get("part"), dict) else {}
        if typ == "text":
            text = _squash(part.get("text"))
            if text:
                self._push("✎ " + _fold(text))
        elif typ == "tool_use":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            inp = state.get("input") if isinstance(state.get("input"), dict) else {}
            arg = ""
            for key in ("filePath", "file_path", "path", "command",
                        "pattern", "query", "url", "description"):
                if inp.get(key):
                    arg = _squash(inp[key])[:90]
                    break
            block = f"🔧 {part.get('tool') or 'tool'}" + (f" {arg}" if arg else "")
            out = _squash(state.get("output"))
            if out:
                block += "\n   ↳ " + out[:220]
            self._push(block)
        elif typ == "step_finish":
            toks = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            self._push(f"— step ({_kn(toks.get('total'))} tok)")
        # step_start: a step's own step_finish reports its cost.


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
    key = (st.st_size, st.st_mtime_ns, config.OPENCODE_CONTEXT,
           config.EXTERNAL_CONTEXT, config.FLEET)
    if _fleet_cfg["key"] == key and _fleet_cfg["path"]:
        return _fleet_cfg["path"]
    try:
        doc = json.loads(src.read_text(encoding="utf-8"))
        for m in (doc.get("provider", {}).get("ARC", {}).get("models") or {}).values():
            m.setdefault("limit", {})["context"] = config.OPENCODE_CONTEXT
        # EXTERNAL models (served by a provider other than ARC) get the same
        # fleet context budget, so the harness compacts in time there too. The
        # provider id and model id come from config's alias, so this cannot
        # drift from the roster the way a hardcoded provider name would.
        for _ext in config.EXTERNAL_MODELS:
            _alias = config.MODEL_HARNESS_ALIAS.get(_ext, "")
            if not _alias:
                continue
            _prov, _, _mid = _alias.partition("/")
            _pmodels = ((doc.get("provider", {}).get(_prov) or {})
                        .get("models") or {})
            if _mid in _pmodels:
                # Not OPENCODE_CONTEXT: that budget exists for an ARC
                # pathology (requests stop returning near 55-60k input), which
                # an OpenRouter-served model does not share. See
                # config.opencode_context_for.
                _pmodels[_mid].setdefault("limit", {})["context"] = (
                    config.opencode_context_for(_ext))
        # Lowering context can leave `output` above it (an external model may
        # declare a large native output window), which asks the API for more
        # completion tokens than the budget allows. Clamp output under context.
        for _p in (doc.get("provider") or {}).values():
            for _m in (_p.get("models") or {}).values():
                _lim = _m.get("limit")
                if _lim and _lim.get("output", 0) > _lim.get("context", 0):
                    _lim["output"] = _lim["context"]
        doc.setdefault("compaction", {})["auto"] = True
        doc["compaction"].setdefault("threshold", 0.75)
        config.OPENCODE_FLEET_CONFIG.write_text(
            json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError) as exc:
        log.warning("could not build the fleet opencode config: %s", exc)
        return None
    _fleet_cfg.update(key=key, path=str(config.OPENCODE_FLEET_CONFIG))
    return _fleet_cfg["path"]


def _reasonix_provider(model):
    """The model's provider name in the fleet toml — one entry PER MODEL.

    context_window is a per-provider setting in reasonix, and the roster's
    real windows differ 4x (DeepSeek-V4.1-Flash 512K, GLM-5.3 128K), so a
    single shared `arc` provider cannot serve both: at a shared 128K DeepSeek
    compacts at a quarter of its window (the 2026-09-13 fleet-ops failure:
    sessions folded at ~52K until the loop-guard refused the model's writes),
    at a shared 512K GLM never compacts at all. `--model <slug>/<model>`
    selects provider and model together.
    """
    return "arc-" + re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")


def _reasonix_config_toml(models):
    """The fleet's reasonix.toml: one `arc`-family provider per model.

    kind="openai" is reasonix's OpenAI-compatible adapter (chat/completions
    under base_url). Every live roster model is listed so `--model
    <slug>/<name>` resolves for any of them; the ARC model id is passed
    through verbatim, which is what the gateway expects. context_window is
    config.reasonix_context(model) — the model's REAL window, not the opencode
    budget: at 65536 reasonix folded one fleet-ops attempt's 393,635-token
    session at line 7 of its transcript, and after enough folds the
    loop-guard refused the model's writes mid-task ("blocked: the current
    constraints forbid state mutation", 2026-09-13). bash_timeout_seconds
    matches GATE_TIMEOUT because an implementer runs ./check.sh through
    reasonix's own bash, and a harness timeout below the gate's kills an
    honest suite mid-run. The sandbox is off because reasonix's shell tool
    refuses to run at all without bubblewrap — "refusing to run unconfined"
    on this WSL box — and a gate that cannot run tests is a gate that fails
    every task. Permissions are handled by --permission-mode on argv;
    [permissions] mode=allow is the belt to that brace so no writer fallback
    ever waits for an answer nobody gives.
    """
    head = (
        "# Generated by arc-orchestrator (drivers.reasonix_fleet_home); edits\n"
        "# are overwritten whenever ARC_API_KEY, ARC_BASE_URL, the roster or a\n"
        "# context window changes.\n"
        # Tracks the config schema version reasonix 1.38.7 stamps on save:
        # without it the binary re-serializes the file (defaults, comments)
        # on EVERY load, this function then sees a diff and rewrites it back —
        # a two-writers-per-attempt thrash measured 2026-09-13. A future
        # reasonix bump re-migrates once; bump the number to match.
        "config_version = 10\n"
        f"default_model = {json.dumps(_reasonix_provider(models[0]) + '/' + models[0])}"
    )
    providers = "\n\n".join(
        "[[providers]]\n"
        f"name           = {json.dumps(_reasonix_provider(m))}\n"
        "kind           = \"openai\"\n"
        f"base_url       = {json.dumps(config.BASE_URL)}\n"
        f"models         = [{json.dumps(m)}]\n"
        "api_key_env    = \"ARC_API_KEY\"\n"
        "web_search     = false\n"
        f"context_window = {config.reasonix_context(m)}"
        for m in models
    )
    tail = (
        "[tools]\n"
        f"bash_timeout_seconds = {int(config.GATE_TIMEOUT)}\n\n"
        "[permissions]\n"
        "mode = \"allow\"\n\n"
        "[sandbox]\n"
        "bash = \"off\"\n\n"
        "[environment]\n"
        "enabled = false\n"
    )
    return head + "\n\n" + providers + "\n\n" + tail


def reasonix_fleet_home():
    """Path of the fleet's private REASONIX_HOME, (re)generated as needed.

    Written only when the rendered config or key differs from what is on
    disk, so concurrent drivers do not thrash the file and reasonix's own
    session state under the same home is left alone. The .env is 0600: it
    holds the ARC key.
    """
    home = Path(config.REASONIX_FLEET_HOME)
    models = [m for m, h in config.MODEL_HARNESS.items() if h == "reasonix"]
    models += [m for m in config.MODEL_HARNESS if m not in models]
    if not models:
        models = ["DeepSeek-V4.1-Flash-thinking-max"]
    cfg = _reasonix_config_toml(models)
    env = f"ARC_API_KEY={config.API_KEY}\n"
    try:
        home.mkdir(parents=True, exist_ok=True)
        cfg_path, env_path = home / "config.toml", home / ".env"
        if not cfg_path.exists() or cfg_path.read_text(encoding="utf-8") != cfg:
            cfg_path.write_text(cfg, encoding="utf-8")
        if not env_path.exists() or env_path.read_text(encoding="utf-8") != env:
            env_path.write_text(env, encoding="utf-8")
            os.chmod(env_path, 0o600)
    except OSError as exc:
        log.warning("could not write the fleet reasonix home %s: %s", home, exc)
    return str(home)


async def _terminate(proc):
    """Kill a harness and everything it spawned; safe to call twice.

    A harness is a process GROUP, not a process. opencode runs the shell
    commands the agent asks for, language servers, test runners; a gate that
    starts with ./check.sh runs a whole unittest suite under it. Killing only
    the direct child — which this did — left all of that alive: still
    writing into the worktree, still holding pipes, and for anything mid
    request, still holding an ARC slot. Every harness is spawned as its own
    session leader (start_new_session=True in spawn()), so its pid is also
    its process-group id and the whole tree can be signalled at once.

    Only while the child is unreaped, though: once proc.wait() has collected
    it the pid may belong to anyone, and killpg on a recycled pid would take
    out an unrelated process group. A harness that exited on its own is
    expected to have cleaned up its own children.
    """
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        # Not ours to signal as a group (should not happen for a child we
        # spawned); fall back to the process itself.
        try:
            proc.kill()
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(proc.wait(), 10)
    except asyncio.TimeoutError:
        log.warning("harness pid %s did not exit after SIGKILL", proc.pid)


async def spawn(argv, *, cwd, env=None, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE):
    """Start a harness (or a gate) the way _terminate expects to find it.

    Its own session, so the process group is ours to kill as one (see
    _terminate); stdin from /dev/null, because nothing here ever answers a
    prompt — every harness takes its instructions on argv — and a child that
    inherits a terminal's stdin and then reads it stops the whole run on a
    tty read nobody will see.
    """
    return await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd), env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=stdout, stderr=stderr, start_new_session=True)


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


def _result_head(payload, limit=20000):
    """payload up to the end of its FIRST balanced {...}, else "".

    The verdict of a review is the first object in reasonix's `.result`, and
    the analysis that follows can push the payload past the 3000-char tail
    window — the object itself straddles the cut, so slicing the head at a
    fixed width would hand `_parse_verdict` a half object (measured: the real
    3303/3477/4308-char payloads end their verdict object exactly at the end
    of the payload, past char 3000). Cut at the object boundary instead.
    """
    depth, in_str, esc, start = 0, False, False, -1
    for i, c in enumerate(payload[:limit]):
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                return payload[:i + 1]
    return ""


def parse_transcript(raw):
    """(session_id, assistant-text tail) from captured stdout, defensive.

    A harness that ends its stream with a final result object — reasonix's
    `{"type": "result", "result": "...", "session_id": "..."}` — gets that
    object's text as THE answer: its stream also carries "text" on phase and
    tool events ("checking", "working"), which the generic dig would fold
    into the verdict a reviewer's JSON is parsed from.
    """
    texts, sid_holder = [], [None]
    final = None
    codex_msg, codex_sid = None, None
    agy_msg, agy_sid = None, None
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if (isinstance(obj, dict) and obj.get("type") == "result"
                and isinstance(obj.get("result"), str)):
            final = obj
            continue
        # `codex exec --json` (measured 2026-09-22): a thread.started event
        # carrying the session id, and the answer as item.completed items of
        # type agent_message. Its stream ALSO carries reasoning and command
        # items with "text" fields, which the generic dig would fold into a
        # reviewer's verdict — so the LAST agent_message is taken as THE
        # answer, the same way reasonix's result object is.
        if isinstance(obj, dict) and obj.get("type") == "thread.started":
            codex_sid = obj.get("thread_id") or codex_sid
            continue
        if isinstance(obj, dict) and obj.get("type") == "item.completed":
            item = obj.get("item") or {}
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                codex_msg = item["text"]
            continue
        # `agy --print --output-format stream-json`: events are `event`, not
        # `type`. The answer is result.response; step_update.text_delta is the
        # same text arriving in pieces, and folding it would double a verdict.
        if isinstance(obj, dict) and obj.get("event") in (
                "init", "step_update", "result"):
            if obj.get("event") == "init" and isinstance(
                    obj.get("conversation_id"), str):
                agy_sid = obj["conversation_id"]
            elif obj.get("event") == "result" and isinstance(
                    obj.get("result"), dict):
                body = obj["result"]
                if isinstance(body.get("response"), str):
                    agy_msg = body["response"]
                if isinstance(body.get("conversation_id"), str):
                    agy_sid = body["conversation_id"]
            continue
        _dig(obj, texts, sid_holder)
    if agy_msg is not None and final is None and codex_msg is None:
        sid = agy_sid or sid_holder[0]
        tail = agy_msg[-3000:]
        if len(agy_msg) > len(tail):
            head = _result_head(agy_msg)
            return sid, (head + "\n" + tail) if head else tail
        return sid, tail
    if codex_msg is not None and final is None:
        return codex_sid or sid_holder[0], codex_msg[-3000:]
    if final is not None:
        sid = final.get("session_id") if isinstance(final.get("session_id"), str) else None
        payload = final["result"] or raw
        tail = payload[-3000:]
        if len(payload) > len(tail):
            # A payload longer than the window lost its HEAD — and reasonix
            # puts a review's verdict JSON at the very START of `.result`
            # ({"pass": false, "issues": [...]}). Measured 2026-09-15: eleven
            # completed reviews were thrown away as "review ended without a
            # parseable verdict" while their verdict sat in the dropped head
            # of a multi-KB payload — five of them outright pass/0-issues
            # approvals; the sub-3000-char payloads survived, which is why
            # some verdicts did come through. Keep the tail AND the head: a
            # duplicated verdict span is harmless (_parse_verdict takes the
            # LAST carrying span), a dropped one is a discarded review.
            # Untruncated payloads return byte-identical to before, so this
            # cannot double-count them.
            head = _result_head(payload)
            return sid or sid_holder[0], (head + "\n" + tail) if head else tail
        return sid or sid_holder[0], tail
    return sid_holder[0], ("".join(texts) or raw)[-3000:]


def harness_error_detail(out, err):
    """The most informative text for a non-zero harness exit.

    Both live harnesses put the reason on STDOUT and exit with stderr empty or
    noisy: opencode names its failures on stdout (which produced the useless
    "opencode exited 1: "), and reasonix ends its stdout stream with the
    terminal result object carrying `is_error: true` and the provider's text in
    `.result` — ARC's "concurrent session limit reached for model 'X'" lands
    there verbatim (verified 2026-09-13). stderr is progress chatter. So the
    STDOUT object is preferred over stderr chatter; stderr is the fallback for
    a harness that names its failure there. Never returns "" — a genuinely
    silent exit says so rather than printing "exited 1: " with nothing after
    it.
    """
    for candidate in (out, err):
        text = (candidate or "").strip()
        if text:
            return text
    return "no output on stdout or stderr"


def transcript_tokens(raw):
    """(tokens, prompt, completion) summed over opencode `step_finish` usage.

    opencode emits {"type":"step_finish", "part":{"tokens":{"total","input",
    "output","reasoning","cache":{"read","write"}}}} per step, where
    total = input + output + reasoning + cache.read + cache.write.
    Kimi stream-json carries no usage (kimi-code wire logs capture it instead).
    reasonix emits {"kind":"usage","usage":{"promptTokens","completionTokens",
    "totalTokens","cacheHitTokens",...}} after every model round-trip; prompt
    tokens there already include the cache hits.
    """
    tokens = prompt = completion = 0
    for line in raw.splitlines():
        # Claude Code / Codex / Gemini headless: the TERMINAL result object
        # carries the whole run's usage, so it is summed once rather than per
        # step. Without this the subscription harnesses reported (0, 0, 0) and
        # the usage page showed a run that cost nothing — Rule 7 evidence that
        # quietly says "no tokens" is worse than none, because it looks like a
        # measurement. Cache reads and writes are prompt tokens (they are
        # charged as such), matching the opencode branch below.
        if '"type":"result"' in line or '"type": "result"' in line:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            u = e.get("usage")
            if isinstance(u, dict) and ("input_tokens" in u or "output_tokens" in u):
                p_ = ((u.get("input_tokens") or 0)
                      + (u.get("cache_read_input_tokens") or 0)
                      + (u.get("cache_creation_input_tokens") or 0))
                c_ = (u.get("output_tokens") or 0)
                prompt += p_
                completion += c_
                tokens += (u.get("total_tokens") or (p_ + c_))
                continue
            if isinstance(u, dict) and ("prompt_tokens" in u or "completion_tokens" in u):
                p_ = u.get("prompt_tokens") or 0
                c_ = u.get("completion_tokens") or 0
                prompt += p_
                completion += c_
                tokens += (u.get("total_tokens") or (p_ + c_))
                continue
        # codex exec --json: one turn.completed per turn with the turn's usage.
        # input_tokens already INCLUDES cached_input_tokens (OpenAI's usage
        # convention), so the cached count is not added a second time.
        if '"turn.completed"' in line:
            try:
                u = (json.loads(line).get("usage") or {})
            except (ValueError, AttributeError):
                continue
            if isinstance(u, dict) and "input_tokens" in u:
                p_ = u.get("input_tokens") or 0
                c_ = u.get("output_tokens") or 0
                prompt += p_
                completion += c_
                tokens += p_ + c_
            continue
        if '"kind":"usage"' in line or '"kind": "usage"' in line:
            try:
                u = (json.loads(line).get("usage") or {})
            except (ValueError, AttributeError):
                continue
            if isinstance(u, dict) and "promptTokens" in u:
                prompt += u.get("promptTokens") or 0
                completion += u.get("completionTokens") or 0
                tokens += u.get("totalTokens") or (
                    (u.get("promptTokens") or 0) + (u.get("completionTokens") or 0))
            continue
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
    # Interactive work has a HUMAN waiting on it. Batch work does not. The only
    # thing this changes is queue position: an interactive driver takes the next
    # free slot ahead of queued batch work rather than lining up behind it. It
    # does not raise any cap — the provider's ceiling is the provider's ceiling.
    interactive = False

    def argv(self, prompt, session_id):
        raise NotImplementedError

    def extra_env(self, worktree):
        """Harness-specific environment for a run (see ReasonixDriver)."""
        return {}

    # Text ARC returns when the account is already at its per-model
    # concurrency cap. These are NOT crashes: the harness never got a slot, so
    # retrying 2s later just re-enters the same cap and deepens the pile-up
    # (observed live: four taskfiles resumed at once put 9 Kimi requests
    # against a cap of 3, and every retry came straight back as a 400).
    # "rate_limit"/"quota" cover dsh's `dsh: RATE_LIMIT:` / `dsh: QUOTA:`
    # error codes on stderr (the others are opencode/kimi/HTTP phrasings).
    # "queue is full" / "backend queue" cover ARC's newer under-load refusal,
    # `{"detail":"backend queue is full"}` — the same saturation the marker
    # list exists for, but no longer worded as a session limit (observed live
    # 2026-09-15: opencode exits 1 carrying it, and without the marker it was
    # filed as a crash and counted as a defect).
    _CAPACITY_MARKERS = ("provider.api_error: 400", "status code (no body)",
                         "session limit", "concurrent", "rate limit", "429",
                         "rate_limit", "quota", "queue is full", "backend queue")

    @classmethod
    def is_capacity_error(cls, text):
        low = (text or "").lower()
        return any(m in low for m in cls._CAPACITY_MARKERS)

    # ARC sits behind the VT campus VPN, and the VPN session expires every 24h.
    # When it does, every request 403s with this exact text. That is not a
    # crash and not capacity — retrying it on either ladder burns the whole
    # attempt budget against an endpoint that cannot answer, which is how ten
    # kimi runs and thirteen opencode timeouts were spent overnight on 09-11.
    _VPN_MARKERS = ("restricted to the vt campus vpn", "connect to the vpn",
                    "provider.auth_error: 403")

    @classmethod
    def is_vpn_error(cls, text):
        low = (text or "").lower()
        return any(m in low for m in cls._VPN_MARKERS)

    async def _swap_run(self, prompt, worktree, task_id, tried, avoid_families):
        """Re-run `prompt` on another harness, or None when there is no seat.

        The result carries the SUBSTITUTE's model and harness, so callers
        record (and trailers name) the model that actually did the work."""
        sub = usage_substitute(self.model, self.harness, self.role,
                               exclude=tried, avoid_families=avoid_families,
                               allow_planner=getattr(self, "planner_swap", False))
        if not sub:
            return None
        events.emit("driver.usage_swap", harness=self.harness, model=self.model,
                    role=self.role, task=task_id, to_model=sub,
                    to_harness=config.MODEL_HARNESS.get(sub))
        log.warning("%s: plan usage limit reached; swapping this attempt to %s",
                    self.model, sub)
        # The substitute must not resume this harness's session. The board
        # is what it (and the next fix round) reads instead.
        import board
        to_h = config.MODEL_HARNESS.get(sub) or "?"
        board.post(worktree, task=task_id or "", role=self.role, model=self.model,
                   harness=self.harness, kind="handoff",
                   body=(f"usage limit on {self.harness}/{self.model}; "
                         f"this attempt continues on {to_h}/{sub}. "
                         f"Do not resume a {self.harness} session there."))
        other = driver_for(sub, self.role,
                           interactive=getattr(self, "interactive", False))
        # A Codex session id is meaningless to `agent` or `claude`. The
        # substitute starts in the same worktree and reads what is there.
        return await other.run(prompt, worktree, session_id=None, task_id=task_id,
                               _swapped_from=set(tried) | {self.model},
                               avoid_families=avoid_families)

    async def run(self, prompt, worktree, session_id=None, task_id=None,
                  _swapped_from=None, avoid_families=()):
        attempt = 0
        sid = session_id
        continuation = None
        usage_waited = 0.0     # seconds this run has spent parked on a plan window
        tried = set(_swapped_from or ())
        while True:
            attempt += 1
            # A sibling already learned this harness's plan is out. Move to
            # another seat when one is free; otherwise wait for the same reset
            # rather than spend a refusal rediscovering it.
            blocked = _usage_blocked_until.get(self.harness, 0)
            if blocked > time.time():
                swapped = await self._swap_run(prompt, worktree, task_id, tried,
                                               avoid_families)
                if swapped is not None:
                    return swapped
                usage_waited += await wait_for_usage_reset(
                    self.harness, self.model, task_id, blocked,
                    config.USAGE_LIMIT_MAX_WAIT - usage_waited)
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
                if self.is_vpn_error(str(exc)):
                    # Not a crash, not capacity: the network path is gone.
                    # Wait for it to come back and retry the SAME attempt
                    # number — an attempt that never reached the API was not
                    # an attempt.
                    events.emit("driver.vpn_down", harness=self.harness,
                                model=self.model, task=task_id, attempt=attempt)
                    await wait_for_arc(task_id)
                    attempt -= 1
                    continue
                if sid and "no rollout found" in str(exc).lower():
                    # The id belonged to another harness, or the rollout was
                    # deleted. Retrying resume repeats the same instant exit
                    # until MAX_RETRIES. Drop the id and continue from the
                    # worktree and the shared board.
                    events.emit("driver.resume_missing", harness=self.harness,
                                model=self.model, task=task_id, attempt=attempt,
                                session_id=sid)
                    import board
                    board.post(worktree, task=task_id or "", role=self.role,
                               model=self.model, harness=self.harness,
                               kind="handoff",
                               body=(f"session {sid} is not a {self.harness} "
                                     "rollout; continuing without resume."))
                    sid = None
                    # Keep the original prompt. `continuation` replaces it
                    # entirely, and a one-line note would drop the task.
                    continuation = (
                        "The previous session id is not a rollout this harness "
                        "can resume. Continue from the files already in this "
                        "worktree and from the SHARED BOARD below. Do not "
                        "assume a prior chat.\n\n" + prompt
                    )
                    attempt -= 1
                    continue
                if getattr(exc, "usage_limit", False) or is_usage_limit(str(exc)):
                    # The plan's usage window is spent. Park until it resets
                    # (or poll when the refusal named no time) and retry the
                    # SAME attempt number — like the VPN path, a request the
                    # plan refused to serve was not an attempt at the task.
                    budget = config.USAGE_LIMIT_MAX_WAIT - usage_waited
                    resets_at = getattr(exc, "resets_at", None)
                    until = (resets_at + config.USAGE_LIMIT_MARGIN if resets_at
                             else time.time() + config.USAGE_LIMIT_POLL)
                    events.emit("driver.usage_limit", harness=self.harness,
                                model=self.model, role=self.role, task=task_id,
                                attempt=attempt, error=str(exc)[:300],
                                resets_at=round(resets_at) if resets_at else None,
                                wait_s=round(max(0, until - time.time())),
                                waited_s=round(usage_waited))
                    if budget <= 0:
                        raise
                    _usage_blocked_until[self.harness] = max(
                        _usage_blocked_until.get(self.harness, 0), until)
                    # The full prompt, not `continuation`: a continuation
                    # only makes sense inside this harness's own session, and
                    # the substitute starts a fresh one.
                    swapped = await self._swap_run(
                        prompt, worktree, task_id, tried, avoid_families)
                    if swapped is not None:
                        return swapped
                    log.warning("%s: plan usage limit reached; waiting %.0fs for "
                                "the window to reset", self.model,
                                max(0, until - time.time()))
                    usage_waited += await wait_for_usage_reset(
                        self.harness, self.model, task_id, until, budget)
                    attempt -= 1
                    continue
                # A capacity refusal the harness reported AS an exit-1 (ARC
                # "backend queue is full") arrives carrying the flag set by
                # _pump; honour it alongside the text sniff so a queue-full
                # exit is not captured as a defect and is retried on the
                # capacity backoff, not the crash ladder.
                capacity = (getattr(exc, "capacity", False)
                            or self.is_capacity_error(str(exc))
                            or "unanswered for" in str(exc))
                # Capacity errors are expected weather and would swamp triage;
                # everything else is a defect worth a traceback and a group.
                fp = None if capacity else errors.capture(
                    exc, task=task_id, model=self.model, node="driver",
                    harness=self.harness, attempt=attempt)
                events.emit("driver.error", harness=self.harness, model=self.model,
                            task=task_id, attempt=attempt, error=str(exc)[:300],
                            will_resume=bool(sid), capacity=capacity,
                            fingerprint=fp)
                if attempt > config.MAX_RETRIES:
                    raise
                if capacity:
                    backoff = min(config.DRIVER_CAPACITY_BACKOFF * attempt,
                                  config.DRIVER_CAPACITY_BACKOFF_CAP)
                    backoff += random.uniform(0, backoff * 0.25)
                else:
                    backoff = min(60, 2 ** attempt)
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
                                  "attempt": attempt, "pid": os.getpid()},
                                 interactive=self.interactive)
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
        # GRAFT_DIR: the worktree's code graph lives beside the worktree, not
        # in it (graft.py explains why); the harness's own `graft ask` calls
        # must look where the orchestrator built it.
        env.update(graft.env_for(worktree))
        env.update(self.extra_env(worktree))
        TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        tpath = TRANSCRIPT_DIR / f"{task_id or 'adhoc'}-{self.role}-{attempt}.jsonl"
        proc = await spawn(argv, cwd=worktree, env=env)
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
        total_budget = config.total_timeout_for(self.role)
        deadline = t0 + total_budget if total_budget > 0 else float("inf")
        interval = config.DRIVER_PROGRESS_INTERVAL

        def written():
            return sum(len(c) for c in chunks)

        idle_budget = config.idle_timeout_for(self.role)
        try:
            with open(tpath, "wb") as fh:
                while True:
                    now = time.monotonic()
                    idle_for = now - last_chunk_t
                    if idle_for >= idle_budget or now >= deadline:
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
                                         idle_budget - idle_for,
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
            stalled = idle >= idle_budget - 1
            kind = "stalled" if stalled else "timed out"
            limit = (f"{idle_budget}s idle" if stalled
                     else f"{config.total_timeout_for(self.role)}s total")
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
                session_id=psid or session_id,
                capacity=blocked)  # blocked with no CPU = saturated, not a crash
        err = await err_task
        await proc.wait()
        raw = b"".join(chunks).decode(errors="replace")
        sid, text = parse_transcript(raw)
        toks, ptok, ctok = transcript_tokens(raw)
        if proc.returncode != 0:
            # Both live harnesses name their failures on STDOUT: opencode
            # reports there and exits with an empty stderr (which produced the
            # useless "opencode exited 1: ", repeated four times per task by
            # the retry ladder), and reasonix ends its stream with the
            # `is_error: true` result object carrying the provider's text. The
            # stdout object is preferred over stderr chatter, and a genuinely
            # silent exit says so instead of trailing off after the colon.
            err_text = err.decode(errors="replace")
            detail = harness_error_detail(text or raw, err_text)
            # A plan out of its usage window is judged on the FULL output: the
            # reset time may sit in a stream event or in stderr, not in the
            # tail kept on the message.
            usage = (is_usage_limit(detail) or is_usage_limit(err_text)
                     or _rejected_window(raw) is not None)
            # An exit that carried a capacity refusal (ARC "backend queue is
            # full" / "concurrent session limit") is expected weather, not a
            # crash: mark it so Driver.run retries it on the capacity ladder
            # and skips errors.capture.
            raise DriverError(f"{argv[0]} exited {proc.returncode}: {detail[-300:]}",
                              session_id=sid or session_id,
                              capacity=self.is_capacity_error(detail),
                              usage_limit=usage,
                              resets_at=(usage_reset_at("\n".join((detail, err_text, raw)))
                                         if usage else None))
        return DriverResult(self.harness, self.model, self.role, proc.returncode,
                            session_id or sid, str(tpath), text,
                            round(time.monotonic() - t0, 1), toks, ptok, ctok)


class KimiDriver(Driver):
    """HISTORICAL — Kimi-K3 is retired (operator decision 2026-09-12).

    Importable so old transcripts, harness_runs rows and the usage page keep
    resolving, but it can never run: `Kimi-K3` is not in config.MODEL_ROLES,
    so __init__ raises ValueError pointing the caller at a live model. The
    kimi CLI harness is likewise absent from config.MODEL_HARNESS.
    """
    harness = "kimi"
    model = "Kimi-K3"

    def __init__(self, role, bench=False, interactive=False):
        _GH_OPS = ("issue-triager", "issue-maker", "pr-reviewer")
        if not bench:
            # Kimi-K3 is off the roster as of 2026-09-12 (retired early by
            # operator decision; its ROSTER row was deleted). Constructing
            # this driver must fail loudly rather than send a request to a
            # model the fleet may no longer route to.
            if self.model not in config.MODEL_ROLES:
                raise ValueError("Kimi-K3 is not on today's roster — it was "
                                 "retired 2026-09-12; route this role to "
                                 "config.PLANNER_MODEL or another live model")
            need = "planner" if role in _GH_OPS else role
            if not config.model_may(self.model, need):
                raise ValueError(f"KimiDriver may hold "
                                 f"{sorted(config.MODEL_ROLES[self.model])}, not {role!r}")
        self.role = role
        self.interactive = interactive

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

    def __init__(self, model, role, bench=False, interactive=False):
        if not bench:
            # Role permissions come from the roster (config.ROSTER), not from a
            # per-model if-chain here. The chain named models that leave on
            # dates the provider chooses, and forgot one every time the roster
            # changed. gh-ops roles are harness capabilities, not model tiers.
            _GH_OPS = ("issue-triager", "issue-maker", "pr-reviewer")
            if model not in config.MODEL_ROLES:
                raise ValueError(f"{model!r} is not on today's roster "
                                 f"({sorted(config.MODEL_ROLES)})")
            # gh-ops (triage issues, draft issues, review PRs from the CLI) is
            # judgement work: it needs a model the roster trusts to PLAN.
            need = "planner" if role in _GH_OPS else role
            if not config.model_may(model, need):
                raise ValueError(
                    f"{model} may hold {sorted(config.MODEL_ROLES[model])}, "
                    f"not {role!r}")
        self.model = model
        self.role = role
        self.interactive = interactive

    def model_arg(self):
        """The `provider/model` string this driver names its model with.

        An EXTERNAL model (served by a provider other than ARC) uses its full
        provider/model string; an ARC model keeps the ARC/ prefix its provider
        block declares. config owns both the external set and the alias so
        routing cannot drift from the roster. ocserve splits it into
        {providerID, model} per route.
        """
        return (config.harness_model(self.model, "opencode")
                or config.provider_model_alias(self.model)
                or f"ARC/{self.model}")

    async def _once(self, prompt, worktree, session_id, task_id, attempt):
        """One attempt against the shared `opencode serve` server.

        The one-shot `opencode run` spawn is gone (task opencode-serve-only):
        this is the ONLY path. The guarded plumbing around it (model gate,
        harness gate, DB leases, driver.start/done/error) is
        Driver.run/_guarded_once — shared, untouched. What differs from the
        base Driver._once is the transport: a session bound to the worktree
        over x-opencode-directory instead of a spawned process.
        """
        import ocserve
        t0 = time.monotonic()
        TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        tpath = TRANSCRIPT_DIR / f"{task_id or 'adhoc'}-{self.role}-{attempt}.jsonl"
        stall = config.idle_timeout_for(self.role)
        total = config.total_timeout_for(self.role) or None
        # The fleet opencode config (lowered context budget) was set per-spawn
        # on the deleted one-shot path; the shared server gets it once, at
        # start, so it compacts in time exactly as the spawned process did.
        server_env = {}
        cfg = opencode_fleet_config()
        if cfg:
            server_env["OPENCODE_CONFIG"] = cfg
        handle = ocserve.get_shared_server(env=server_env)
        client = await ocserve.OcserveClient.create(
            handle, worktree=worktree, model=self.model_arg())
        # The transcript gets the SAME {type: text|tool_use|step_finish, ...}
        # line dicts the one-shot stream writes, so parse_transcript,
        # transcript_tokens and the dashboard tails keep working unchanged.
        fh = open(tpath, "ab")

        def on_event(frame):
            for line in _serve_transcript_lines(frame):
                try:
                    fh.write((json.dumps(line) + "\n").encode())
                    fh.flush()
                except (OSError, TypeError, ValueError):
                    pass

        try:
            result = await client.prompt(
                prompt, model=self.model_arg(),
                timeout=total, stall_timeout=stall, on_event=on_event)
        except ocserve.CapacityFull as exc:
            # Same contract as the one-shot capacity exit: flag it so Driver.run
            # retries on the capacity backoff rather than the crash ladder.
            raise DriverError(str(exc), session_id=client.session_id,
                              capacity=True) from exc
        except ocserve.StreamStalled as exc:
            # The idle-kill analogue: dispose and let Driver.run retry. Blocked
            # with no progress is the capacity signature here too (mirrors the
            # one-shot stall path's `capacity=blocked`).
            await _serve_dispose(client)
            raise DriverError(str(exc), session_id=client.session_id,
                              capacity=True) from exc
        except ocserve.OcserveError as exc:
            await _serve_dispose(client)
            raise DriverError(str(exc), session_id=client.session_id) from exc
        finally:
            fh.close()
        await _serve_dispose(client)
        text = result.text or ""
        tok = result.tokens or {}
        cache = tok.get("cache") or {}
        tokens = tok.get("total") or 0
        prompt_tokens = ((tok.get("input") or 0) + (cache.get("read") or 0)
                         + (cache.get("write") or 0))
        completion_tokens = (tok.get("output") or 0) + (tok.get("reasoning") or 0)
        return DriverResult(self.harness, self.model, self.role, 0,
                            client.session_id or session_id, str(tpath), text,
                            round(time.monotonic() - t0, 1),
                            tokens, prompt_tokens, completion_tokens)


async def _serve_dispose(client):
    """Best-effort dispose; a failed dispose must not mask the real error."""
    try:
        await client.dispose()
    except Exception as exc:                      # noqa: BLE001 - teardown
        log.warning("ocserve: dispose failed: %s", exc)


def _serve_transcript_lines(frame):
    """Render one serve SSE frame as zero or more one-shot-shaped lines.

    The one-shot transcript is the watch surface (Rule 7): the dashboard tails
    logs/harness/*.jsonl and reads {type: text|tool_use|step_finish, part:{...}}
    records. The server's /event frames carry the same opencode shapes under
    `properties.part`, so they are re-wrapped into the one-shot spelling rather
    than inventing a third format. Anything the one-shot stream does not put on
    stdout (server.connected, session.idle, message.updated) is dropped.
    """
    if not isinstance(frame, dict):
        return []
    etype = frame.get("type")
    props = frame.get("properties") if isinstance(frame.get("properties"), dict) else {}
    part = props.get("part") if isinstance(props.get("part"), dict) else None
    if etype == "message.part.updated" and part is not None:
        if part.get("type") == "text":
            return [{"type": "text", "part": part}]
        if part.get("type") in ("tool", "tool_use"):
            return [{"type": "tool_use", "part": part}]
        if part.get("type") == "step-finish":
            return [{"type": "step_finish", "part": part}]
    return []


def _dsh_log_line(e):
    """Render one dsh session-log event as one transcript line (None: skip).

    Only the event kinds that show what the agent is doing render; the
    bookkeeping kinds (turn/meta, usage, ...) return None.
    """
    t, d = e.get("type"), e.get("data") or {}
    ts = time.strftime("%H:%M:%S", time.localtime((e.get("time") or 0) / 1000))
    step = d.get("step")
    if t == "tool/call":
        return (f"[dsh-log {ts} step {step}] TOOL {d.get('name')}: "
                f"{str(d.get('arguments'))[:260]}")
    if t == "tool/result":
        parts = (d.get("message") or {}).get("content") or []
        texts, err = [], ""
        for p in parts:
            if not isinstance(p, dict):
                continue
            if p.get("isError"):
                err = " ERROR"
            for sub in p.get("content") or []:
                if isinstance(sub, dict) and sub.get("text"):
                    texts.append(str(sub["text"]))
            if p.get("text"):
                texts.append(str(p["text"]))
        return f"[dsh-log {ts} step {step}] RESULT{err}: {' '.join(texts).strip()[:400]}"
    if t == "assistant/message":
        parts = ((d.get("message") or {}).get("content")) or []
        texts = [p.get("text", "") for p in parts
                 if isinstance(p, dict) and p.get("type") == "text"]
        text = " ".join(x for x in texts if x.strip())[:500]
        if text:
            tok = (d.get("usage") or {}).get("outputTokens", "?")
            return f"[dsh-log {ts} step {step}] ASSISTANT: {text} (+{tok}tok)"
    return None


class DeepseekDriver(Driver):
    """DeepSeek's own harness (dsh, github.com/deepseek-ai/deepseek-harness).

    Operator decision 2026-09-12: DeepSeek-V4.1-Flash-thinking-max runs here
    instead of opencode. dsh changes the stream contract, which is why a
    subclass could not fix this with argv alone:

    - stdout carries at most the final assistant message, buffered until the
      run ends. Mid-run BOTH pipes can stay silent for many minutes of
      perfectly healthy agentic work — measured the hard way on 2026-09-12,
      when ten implementer attempts were stall-killed at the idle budget
      while dsh was past step 50 of real work in its session log. No live
      `dsh: reasoning:` deltas arrive in the headless profile.
    - Live progress IS visible in dsh's own session log,
      ~/.dsh/sessions/<cwd-mangled>/session-*/session.v3.jsonl.zstd, which
      grows on every model round-trip and tool call. _session_probe watches
      it and the pump counts its growth as activity, exactly like a pipe
      chunk.
    - Errors surface as a nonzero exit with `dsh: <CODE>: <msg>` on stderr
      (RATE_LIMIT, QUOTA, AUTH, TRANSPORT, TIMEOUT...); dsh's own
      streamIdleTimeoutMs (default 300 s) aborts a held stream from inside,
      so a dead API connection does not need our idle clock to notice it.

    Both pipes still land in the transcript file (what the dashboard tails);
    stdout is additionally kept verbatim as the result text, because that is
    where the answer is when dsh prints one. Since the pipes are silent
    mid-run by design, the transcript ALSO gets the session log rendered
    live: whenever the probe sees it grow, new events are appended as
    `[dsh-log <ts> step <n>] TOOL/RESULT/ASSISTANT ...` lines, so a
    dashboard tail shows the chain of action instead of bare heartbeats.

    No session resume: `dsh --profile headless` accepts nothing but the task
    text, so an interrupted attempt simply retries the original prompt (the
    worktree keeps files already written). dsh prints no token usage either,
    so DriverResult carries (0, 0, 0) — cost attribution for dsh runs is
    lost until the harness reports usage.

    Role rules come from the roster, same as the other drivers: dsh serves
    whichever model its ROSTER row names, and a model without "planner" in
    its roles cannot be constructed as one — the planner role is intrinsically
    refused here rather than banned by a side list.
    """

    harness = "dsh"

    @staticmethod
    def _session_probe(worktree):
        """Activity probe over dsh's own session log.

        Returns a closure answering "has the log grown since the last call?".
        The log dir is the run's cwd with slashes turned to dashes, wrapped
        in dashes (e.g. /tmp -> --tmp--). A run whose file never appears gets
        no probe resets and keeps the ordinary pipe/deadline behaviour.
        """
        root = (Path.home() / ".dsh" / "sessions"
                / ("--" + str(worktree).strip("/").replace("/", "-") + "--"))
        best = [None]

        def probe():
            try:
                newest = max(
                    (p.stat().st_mtime
                     for p in root.glob("session-*/session.v3.jsonl.zstd")),
                    default=None)
            except OSError:
                return False
            if newest is not None and (best[0] is None or newest > best[0]):
                best[0] = newest
                return True
            return False

        return probe

    @staticmethod
    def _session_tail(worktree):
        """Render NEW session-log events as transcript lines (closure).

        Same mangled root as _session_probe. Each call re-reads the newest
        session file and returns the rendered lines for events with a seq
        past the last one seen. The full decode on every call is the simple
        option: it only runs when the probe reports growth, and the sidecar
        dsh_tail.py already proves a 20 s poll of it is cheap. A run whose
        newest file CHANGES (a fresh attempt's session) restarts from seq 0
        of that file — the backlog is that attempt's opening steps, not all
        of history. Anything that breaks (no file yet, zstd missing, a
        partial write) yields no lines, never an exception into the pump.
        """
        root = (Path.home() / ".dsh" / "sessions"
                / ("--" + str(worktree).strip("/").replace("/", "-") + "--"))
        current = [None]
        last_seq = [-1]

        def tail():
            try:
                files = sorted(
                    root.glob("session-*/session.v3.jsonl.zstd"),
                    key=lambda p: p.stat().st_mtime)
            except OSError:
                return []
            if not files:
                return []
            if files[-1] != current[0]:
                current[0] = files[-1]
                last_seq[0] = -1
            try:
                out = subprocess.run(["zstd", "-dc", str(files[-1])],
                                     capture_output=True, timeout=60).stdout
            except (OSError, subprocess.SubprocessError):
                return []
            lines = []
            for raw in out.decode(errors="replace").splitlines():
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue
                if (e.get("seq") or -1) <= last_seq[0]:
                    continue
                last_seq[0] = e.get("seq") or last_seq[0]
                line = _dsh_log_line(e)
                if line:
                    lines.append(line)
            return lines

        return tail

    def __init__(self, model, role, bench=False, interactive=False):
        if not bench:
            # Same roster-check idiom as OpencodeDriver; gh-ops roles are
            # harness capabilities that need a planner-grade model.
            _GH_OPS = ("issue-triager", "issue-maker", "pr-reviewer")
            if model not in config.MODEL_ROLES:
                raise ValueError(f"{model!r} is not on today's roster "
                                 f"({sorted(config.MODEL_ROLES)})")
            need = "planner" if role in _GH_OPS else role
            if not config.model_may(model, need):
                raise ValueError(
                    f"{model} may hold {sorted(config.MODEL_ROLES[model])}, "
                    f"not {role!r}")
        self.model = model
        self.role = role
        self.interactive = interactive

    def argv(self, prompt, session_id):
        # session_id unused: the headless profile cannot resume (verified
        # against `dsh --profile headless --help` on 0.1.5-rc.1).
        return [config.dsh_bin(), "--profile", "headless", prompt]

    async def _once(self, prompt, worktree, session_id, task_id, attempt):
        argv = self.argv(prompt, session_id)
        t0 = time.monotonic()
        env = dict(
            os.environ, PWD=str(worktree),
            # dsh talks to the deepseek-official provider under these names
            # (see ~/.dsh/cordis.patch.yml); values mirror the ARC endpoint
            # every other harness uses.
            DEEPSEEK_API_KEY=config.API_KEY,
            DEEPSEEK_BASE_URL=config.BASE_URL,
            DSH_TELEMETRY_MODE="DISABLED",
            # Headless runs cannot answer an approval prompt — this flips the
            # approval policy to `never`; without it the default
            # workspace-write preset stalls forever on the first tool call.
            DSH_PERMISSION_MODE="danger-full-access",
            **graft.env_for(worktree),   # GRAFT_DIR, as in Driver._once
        )
        TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        tpath = TRANSCRIPT_DIR / f"{task_id or 'adhoc'}-{self.role}-{attempt}.jsonl"
        proc = await spawn(argv, cwd=worktree, env=env)
        try:
            return await self._pump_dual(proc, argv, tpath, t0, task_id,
                                         attempt,
                                         probe=self._session_probe(worktree),
                                         tail=self._session_tail(worktree))
        finally:
            await _terminate(proc)

    async def _pump_dual(self, proc, argv, tpath, t0, task_id, attempt,
                         probe=None, tail=None):
        """Driver._pump, multiplexed over both of dsh's pipes.

        Identical stall/deadline/heartbeat/forensics semantics to the stock
        pump — kept as a copy rather than shared because interleaving a
        generic stream set into _pump risks the harness (opencode) the whole
        fleet already runs on. A chunk on EITHER pipe resets the idle clock,
        and so does the session-log probe: dsh's pipes can stay silent for
        the whole run while it works, the session log cannot. When the probe
        fires, `tail` renders the new session-log events into the transcript
        (`_session_tail`), which is the only live chain of action a dsh run
        ever shows.
        """
        out_chunks, err_chunks = [], []
        q = asyncio.Queue()

        async def feed(stream, tag):
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    break
                q.put_nowait((tag, chunk))
            q.put_nowait((tag, None))

        readers = [asyncio.create_task(feed(proc.stdout, "out")),
                   asyncio.create_task(feed(proc.stderr, "err"))]
        open_streams = 2
        last_chunk_t = time.monotonic()
        last_progress_t = time.monotonic()
        last_hb_t = time.monotonic()
        last_hb = None  # (bytes, idle_s) as of the last driver.heartbeat
        last_cpu = None
        last_probe_t = None
        total_budget = config.total_timeout_for(self.role)
        deadline = t0 + total_budget if total_budget > 0 else float("inf")
        interval = config.DRIVER_PROGRESS_INTERVAL
        idle_budget = config.idle_timeout_for(self.role)

        def written():
            return sum(len(c) for c in out_chunks) + sum(len(c) for c in err_chunks)

        try:
            with open(tpath, "wb") as fh:
                while open_streams:
                    now = time.monotonic()
                    if probe is not None and probe():
                        last_chunk_t = now
                        last_probe_t = now
                        if tail is not None:
                            for line in tail():
                                fh.write(line.encode(errors="replace") + b"\n")
                            fh.flush()
                    idle_for = now - last_chunk_t
                    if idle_for >= idle_budget or now >= deadline:
                        raise asyncio.TimeoutError
                    if (now - last_hb_t >= HEARTBEAT_INTERVAL
                            and last_hb != (written(), round(idle_for, 1))):
                        events.emit("driver.heartbeat", harness=self.harness,
                                    model=self.model, role=self.role,
                                    task=task_id, attempt=attempt,
                                    bytes=written(), idle_s=round(idle_for, 1),
                                    sess_idle_s=(round(now - last_probe_t, 1)
                                                 if last_probe_t else None),
                                    seconds=round(now - t0, 1))
                        last_hb_t = now
                        last_hb = (written(), round(idle_for, 1))
                    wait = max(0.05, min(deadline - now,
                                         idle_budget - idle_for,
                                         interval - (now - last_progress_t),
                                         HEARTBEAT_INTERVAL - (now - last_hb_t)))
                    try:
                        tag, chunk = await asyncio.wait_for(q.get(), wait)
                    except asyncio.TimeoutError:
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
                    if chunk is None:
                        open_streams -= 1
                        continue
                    (out_chunks if tag == "out" else err_chunks).append(chunk)
                    last_chunk_t = time.monotonic()
                    fh.write(chunk)
                    fh.flush()
        except asyncio.TimeoutError:
            # Evidence BEFORE the kill, same as the stock pump; there is no
            # kimi-style wire log to check for an unanswered request.
            snap = proc_snapshot(proc.pid)
            partial = (b"".join(err_chunks) + b"\n" + b"".join(out_chunks)
                       ).decode(errors="replace")[-6000:]
            await _terminate(proc)
            for r in readers:
                r.cancel()
            idle = round(time.monotonic() - last_chunk_t, 1)
            total = round(time.monotonic() - t0, 1)
            stalled = idle >= idle_budget - 1
            kind = "stalled" if stalled else "timed out"
            limit = (f"{idle_budget}s idle" if stalled
                     else f"{config.total_timeout_for(self.role)}s total")
            cpu_delta = (round(snap["cpu_s"] - last_cpu, 2)
                         if last_cpu is not None and "cpu_s" in snap else None)
            blocked = (snap.get("state") in ("S", "D") and (cpu_delta or 0) < 0.5)
            events.emit("driver.stalled" if stalled else "driver.timeout",
                        harness=self.harness, model=self.model, role=self.role,
                        task=task_id, attempt=attempt, bytes=written(),
                        idle_s=idle, elapsed_s=total,
                        cpu_delta_s=cpu_delta, blocked=blocked,
                        last_activity=activity_tail(partial),
                        records=partial.count("\n"), **snap)
            detail = "; process blocked with no CPU burn" if blocked else ""
            raise DriverError(
                f"{argv[0]} {kind} after {limit} "
                f"(total {total}s, idle {idle}s, {written()} bytes{detail})",
                capacity=blocked)
        for r in readers:
            await r
        await proc.wait()
        out = b"".join(out_chunks).decode(errors="replace")
        err = b"".join(err_chunks).decode(errors="replace")
        if proc.returncode != 0:
            # dsh names its failures on stderr (`dsh: RATE_LIMIT: ...`); fall
            # back to the stdout tail the way the stock pump falls back to
            # stderr's, so an exit can never report "exited 1: " with nothing.
            detail = err.strip()
            if not detail:
                detail = out.strip()[-300:] or "no output on stdout or stderr"
            raise DriverError(f"{argv[0]} exited {proc.returncode}: {detail[-300:]}",
                              capacity=self.is_capacity_error(detail))
        return DriverResult(self.harness, self.model, self.role, proc.returncode,
                            None, str(tpath), out.strip()[-3000:],
                            round(time.monotonic() - t0, 1), 0, 0, 0)


class ReasonixDriver(Driver):
    """Reasonix (github.com/esengine/DeepSeek-Reasonix), DeepSeek's cache-first
    coding agent — the DeepSeek harness by operator decision 2026-09-13,
    replacing dsh.

    Why it fits the base Driver where dsh did not: `reasonix run
    --output-format stream-json` prints one JSON object per event on stdout
    — tool dispatches and results, streamed text, a `usage` receipt after
    every model round-trip — and ends with a `{"type": "result"}` object
    carrying the final answer, the session id and totals. So the stall clock
    sees real progress, the transcript the dashboard tails is the agent's
    actual activity, parse_transcript takes the answer from the result
    object, and transcript_tokens prices the run from the receipts (dsh
    reported nothing; 0 tokens per run).

    Errors are the same object with `is_error: true` and the provider's text
    in `result` — ARC's "concurrent session limit reached for model 'X'"
    lands there verbatim (verified 2026-09-13) — plus a non-zero exit, so
    Driver.run's capacity ladder engages through the existing markers.

    Configuration is a private REASONIX_HOME (reasonix_fleet_home), because
    reasonix reads keys only from `<home>/.env`, never the shell. Session
    resume is `-c`: continue the newest session in this workspace, which in
    a task worktree is this task's.
    """

    harness = "reasonix"

    def __init__(self, model, role, bench=False, interactive=False):
        if not bench:
            # Same roster-check idiom as OpencodeDriver; gh-ops roles are
            # harness capabilities that need a planner-grade model.
            _GH_OPS = ("issue-triager", "issue-maker", "pr-reviewer")
            if model not in config.MODEL_ROLES:
                raise ValueError(f"{model!r} is not on today's roster "
                                 f"({sorted(config.MODEL_ROLES)})")
            need = "planner" if role in _GH_OPS else role
            if not config.model_may(model, need):
                raise ValueError(
                    f"{model} may hold {sorted(config.MODEL_ROLES[model])}, "
                    f"not {role!r}")
        self.model = model
        self.role = role
        self.interactive = interactive

    def argv(self, prompt, session_id):
        # bypassPermissions is the headless posture reasonix 1.38 actually
        # accepts (the docs' `danger-full-access` is rejected by the binary:
        # "want manual, ask, auto, acceptEdits, dontAsk, plan, or
        # bypassPermissions"). The workspace root defaults to cwd, which
        # spawn() sets to the worktree. <slug>/<model> picks this model's own
        # provider — context_window is per-provider and windows differ 4x.
        a = [config.reasonix_bin(), "run", "--model",
             f"{_reasonix_provider(self.model)}/{self.model}",
             "--permission-mode", "bypassPermissions",
             "--output-format", "stream-json"]
        if session_id:
            a.append("-c")
        return a + [prompt]

    def extra_env(self, worktree):
        return {"REASONIX_HOME": reasonix_fleet_home(),
                "REASONIX_TELEMETRY": "off",
                "REASONIX_WORKSPACE_ROOT": str(worktree)}


# --- subscription-CLI harnesses ----------------------------------------------
# Claude Code, the Codex CLI and the Gemini CLI run on the OPERATOR'S OWN
# PLANS rather than on per-token API billing. That is the whole reason they
# exist here: the studio roster's frontier models are otherwise a metered
# expense, and a plan the operator already pays for is not.
#
# All three fit the base Driver unmodified, because all three stream JSON
# events on stdout in headless mode:
#
#   claude -p --output-format stream-json    ends with {"type":"result",...}
#   codex exec --json                        JSONL events
#   gemini -p -o stream-json                 JSONL events
#
# so the stdout pump, the stall clock, the live transcript the dashboard
# tails, and parse_transcript all work as they do for reasonix.
#
# WHAT IS DIFFERENT, and it is not technical: a consumer plan is metered for
# one human at one terminal, on rolling windows. config._HARNESS_CAP holds
# these to 1-2 concurrent sessions for that reason, and `claude` is 1 because
# the operator's own interactive session shares the same plan. Parallel fleet
# work belongs on ARC, which is free and genuinely concurrent; these slots
# carry the roles that need frontier quality.


def _check_roster(model, role, bench):
    """The roster/role gate every driver applies (Rules 1 and 2).

    Factored out rather than copied a fourth time: the duplicated version of
    this check is exactly the shape of the drift AGENTS.md Rule 2 warns about,
    where a hand-kept opinion about who may do what diverges from the roster.
    """
    if bench:
        return
    _GH_OPS = ("issue-triager", "issue-maker", "pr-reviewer")
    if model not in config.MODEL_ROLES:
        raise ValueError(f"{model!r} is not on today's roster "
                         f"({sorted(config.MODEL_ROLES)})")
    need = "planner" if role in _GH_OPS else role
    if not config.model_may(model, need):
        raise ValueError(f"{model} may hold {sorted(config.MODEL_ROLES[model])}, "
                         f"not {role!r}")


class ClaudeCodeDriver(Driver):
    """Claude Code (`claude -p`) on the operator's Claude subscription.

    Headless Claude Code streams the same event objects the interactive TUI
    renders and ends with `{"type": "result", "result": ..., "session_id":
    ...}` — the shape parse_transcript already treats as THE answer, so a
    review verdict comes back intact without a new parser.

    `--verbose` is REQUIRED alongside `--output-format stream-json` in print
    mode; without it the CLI refuses the combination, and a driver that
    silently fell back to text would lose the per-event stream the stall clock
    depends on.
    """

    harness = "claude"

    def __init__(self, model, role, bench=False, interactive=False):
        _check_roster(model, role, bench)
        self.model = model
        self.role = role
        self.interactive = interactive

    # Renders the visual judge attaches. Claude Code has no image FLAG: its
    # Read tool opens image files, so the paths are named in the prompt and
    # the agent reads them. Set by studio.evaluation.judge_loop.
    images = ()

    def argv(self, prompt, session_id):
        if self.images:
            prompt = (prompt + "\n\nRead these image files before answering "
                      "(use your Read tool on each):\n"
                      + "\n".join(f"  {i}" for i in self.images))
        a = [config.claude_bin(), "-p", "--output-format", "stream-json",
             "--verbose", "--permission-mode", "bypassPermissions"]
        if config.CLAUDE_CLI_MODEL:
            a += ["--model", config.CLAUDE_CLI_MODEL]
        if session_id:
            a += ["--resume", session_id]
        return a + [prompt]

    def extra_env(self, worktree):
        # Never let an API key hijack a subscription run: with
        # ANTHROPIC_API_KEY set, Claude Code bills the API account instead of
        # the plan, which is the opposite of why this harness exists.
        return {"ANTHROPIC_API_KEY": "", "CLAUDE_CODE_DISABLE_TELEMETRY": "1"}


class CodexDriver(Driver):
    """The Codex CLI (`codex exec`) on the operator's ChatGPT plan.

    `--json` prints events as JSONL, `-C` sets the workspace root to the task
    worktree, and `--skip-git-repo-check` keeps it from refusing a worktree it
    does not recognise as a repository root.

    Sandbox posture is `config.CODEX_SANDBOX` (default `workspace-write`): the
    agent may edit its own worktree and nothing outside it. `codex exec` is
    non-interactive, so there is no approval prompt to deadlock on — which is
    why the far blunter `--dangerously-bypass-approvals-and-sandbox` is not
    the default here.
    """

    harness = "codex"

    def __init__(self, model, role, bench=False, interactive=False):
        _check_roster(model, role, bench)
        self.model = model
        self.role = role
        self.interactive = interactive

    images = ()          # attached with -i, which codex exec supports natively

    def argv(self, prompt, session_id):
        # `resume` is a subcommand of exec, not a flag, so it has to sit
        # immediately after `exec`.
        a = [config.codex_bin(), "exec"] + (["resume", session_id] if session_id else [])
        # The sandbox goes in as a CONFIG override, never as `-s`. `codex exec`
        # accepts `-s` but `codex exec resume` does not ("unexpected argument
        # '-s' found", exit 2) — and every fix round resumes. On the first live
        # studio run (2026-09-22) that failed each fix attempt instantly: one
        # task burned all 16 fix rounds in minutes and was about to escalate
        # onto Claude. `-c sandbox_mode=...` is accepted by both, and the
        # session record confirms it takes effect (sandbox_policy:
        # workspace-write, and a resumed session still writes files).
        a += ["--json", "--skip-git-repo-check",
              "-c", f'sandbox_mode="{config.CODEX_SANDBOX}"']
        if not session_id:
            for img in self.images:
                a += ["-i", str(img)]
        if config.CODEX_CLI_MODEL:
            a += ["-m", config.CODEX_CLI_MODEL]
        if config.CODEX_REASONING_EFFORT:
            # Verified 2026-09-22: the session record then carries
            # "reasoning_effort":"high" for gpt-6-sol.
            a += ["-c", f'model_reasoning_effort="{config.CODEX_REASONING_EFFORT}"']
        return a + [prompt]

    def extra_env(self, worktree):
        return {"OPENAI_API_KEY": "", "CODEX_QUIET_MODE": "1"}


class CursorDriver(Driver):
    """The Cursor Agent CLI (`agent --print`) on the operator's Cursor plan.

    `--output-format stream-json` emits one JSON object per line and ends with
    `{"type": "result", "result": ..., "session_id": ...}`, which
    parse_transcript already treats as the answer. `--force` and `--trust`
    keep it from stopping on an approval or a workspace-trust prompt, and
    `--sandbox disabled` lets it write the task worktree. The worktree is the
    boundary: the process cwd is that directory.

    CURSOR_API_KEY is blanked so a key in the environment cannot bill the API
    account instead of the logged-in plan, the same rule as Claude and Codex.
    """

    harness = "cursor"

    def __init__(self, model, role, bench=False, interactive=False):
        _check_roster(model, role, bench)
        self.model = model
        self.role = role
        self.interactive = interactive

    images = ()

    def argv(self, prompt, session_id):
        if self.images:
            prompt = (prompt + "\n\nRead these image files before answering "
                      "(use your Read tool on each):\n"
                      + "\n".join(f"  {i}" for i in self.images))
        a = [config.cursor_bin(), "--print", "--output-format", "stream-json",
             "--force", "--trust", "--sandbox", "disabled"]
        if config.CURSOR_CLI_MODEL:
            a += ["--model", config.CURSOR_CLI_MODEL]
        if session_id:
            a += ["--resume", session_id]
        return a + [prompt]

    def extra_env(self, worktree):
        return {"CURSOR_API_KEY": ""}


class AntigravityDriver(Driver):
    """Antigravity CLI (`agy --print`) on the operator's Google account.

    `--output-format stream-json` emits `{"event": ...}` lines and ends a turn
    with `{"event":"result","result":{"response","conversation_id",...}}`.
    parse_transcript reads `response` as the answer and ignores `text_delta`.
    `--dangerously-skip-permissions` is the documented unattended-write flag
    (there is no `--yolo`). `--sandbox` is left off so the agent can edit the
    worktree, which is the process cwd.

    Resume is `--conversation <id>`, not `--resume`. GEMINI_API_KEY is blanked
    so an API-key provider setting cannot bill a key instead of the signed-in
    account. The account itself is a one-time `agy` login; this driver does
    not start that browser flow.
    """

    harness = "agy"

    def __init__(self, model, role, bench=False, interactive=False):
        _check_roster(model, role, bench)
        self.model = model
        self.role = role
        self.interactive = interactive

    images = ()

    def argv(self, prompt, session_id):
        if self.images:
            prompt = (prompt + "\n\nRead these image files before answering "
                      "(use your Read tool on each):\n"
                      + "\n".join(f"  {i}" for i in self.images))
        # `--print` takes the NEXT argument as the prompt. It is not a boolean
        # like `agent --print`. It has to come last, immediately before the
        # prompt: `agy --print --output-format ...` handed `--output-format`
        # to `--print` and exited 2 on every review (measured 2026-09-23).
        a = [config.agy_bin(), "--output-format", "stream-json",
             "--dangerously-skip-permissions"]
        if config.AGY_CLI_MODEL:
            a += ["--model", config.AGY_CLI_MODEL]
        if session_id:
            a += ["--conversation", session_id]
        return a + ["--print", prompt]

    def extra_env(self, worktree):
        return {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": ""}


class GeminiDriver(Driver):
    """The Gemini CLI (`gemini -p`) on the operator's Google AI plan.

    This is the studio's VISUAL JUDGE as well as a reviewer: the CLI resolves
    `@path` references in a prompt by reading that file, images included, so a
    judge prompt can attach real renders without a multimodal API call.

    `--approval-mode yolo` is what makes it non-interactive; without it the
    CLI waits for approval on its first tool use and the stall clock kills a
    session that was only ever waiting for a human.
    """

    harness = "gemini"

    def __init__(self, model, role, bench=False, interactive=False):
        _check_roster(model, role, bench)
        self.model = model
        self.role = role
        self.interactive = interactive

    # The Gemini CLI resolves `@path` inside a prompt by reading that file,
    # images included — which is what makes it the visual judge without a
    # single multimodal API call.
    images = ()

    def argv(self, prompt, session_id):
        if self.images:
            prompt = ("\n".join(f"@{i}" for i in self.images)
                      + "\n\n" + prompt)
        a = [config.gemini_bin(), "--output-format", "stream-json",
             "--approval-mode", "yolo"]
        if config.GEMINI_CLI_MODEL:
            a += ["-m", config.GEMINI_CLI_MODEL]
        if session_id:
            a += ["--session-id", session_id]
        return a + ["-p", prompt]

    def extra_env(self, worktree):
        return {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": "",
                "GEMINI_CLI_DISABLE_TELEMETRY": "1"}


def driver_for(model, role, bench=False, interactive=False):
    """The driver for `model` on the harness its ROSTER row names.

    ONE mapping from model -> harness, shared by code_tasks, gh_ops and
    orchchat, so a hand-kept harness choice here cannot drift from the roster
    the way a per-model if-chain did. A model with no roster row (retired:
    Kimi-K3, gpt-oss-120b, DeepSeek-V4-Flash) raises ValueError instead of
    silently falling back to opencode.
    """
    harness = config.MODEL_HARNESS.get(model)
    if harness is None:
        raise ValueError(f"{model!r} is not on today's roster "
                         f"({sorted(config.MODEL_HARNESS)})")
    if harness == "claude":
        return ClaudeCodeDriver(model, role, bench=bench, interactive=interactive)
    if harness == "codex":
        return CodexDriver(model, role, bench=bench, interactive=interactive)
    if harness == "gemini":
        return GeminiDriver(model, role, bench=bench, interactive=interactive)
    if harness == "cursor":
        return CursorDriver(model, role, bench=bench, interactive=interactive)
    if harness == "agy":
        return AntigravityDriver(model, role, bench=bench, interactive=interactive)
    if harness == "kimi":
        return KimiDriver(role, bench=bench, interactive=interactive)
    if harness == "dsh":
        return DeepseekDriver(model, role, bench=bench, interactive=interactive)
    if harness == "reasonix":
        return ReasonixDriver(model, role, bench=bench, interactive=interactive)
    return OpencodeDriver(model, role, bench=bench, interactive=interactive)
