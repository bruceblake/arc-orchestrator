"""Headless CLI drivers for the coding harnesses (kimi, opencode).

Role map (hard rule): gpt-oss-120b handles very basic implementation,
DeepSeek-V4-Flash medium implementation, and GLM-5.3 / Kimi-K3 (kimi CLI) the
hard tasks plus all planning and reviewing. A task is always reviewed by the
*other* of kimi/glm when a strong model implemented it. ARC rejects over-limit
requests per model, so per-model semaphores cap concurrent harness instances
below the account limits (config.driver_limit).
"""
import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import config
import events

log = logging.getLogger("drivers")
TRANSCRIPT_DIR = Path(config.ROOT) / "logs" / "harness"


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

    async def run(self, prompt, worktree, session_id=None, task_id=None):
        gate = _gate(self.model)
        attempt = 0
        sid = session_id
        continuation = None
        while True:
            attempt += 1
            events.emit("driver.start", harness=self.harness, model=self.model,
                        role=self.role, task=task_id, attempt=attempt,
                        resume=bool(sid))
            await gate.acquire()
            try:
                result = await self._once(continuation or prompt, worktree, sid,
                                          task_id, attempt)
            except DriverError as exc:
                gate.release()
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
                events.emit("driver.error", harness=self.harness, model=self.model,
                            task=task_id, attempt=attempt, error=str(exc)[:300],
                            will_resume=bool(sid))
                if attempt > config.MAX_RETRIES:
                    raise
                backoff = min(30, 2 ** attempt)
                log.warning("%s attempt %d failed (%s); retry in %ds",
                            self.model, attempt, exc, backoff)
                await asyncio.sleep(backoff)
                continue
            gate.release()
            events.emit("driver.done", harness=self.harness, model=self.model,
                        role=self.role, task=task_id, attempt=attempt,
                        seconds=round(result.seconds, 1),
                        tokens=result.tokens, prompt_tokens=result.prompt_tokens,
                        completion_tokens=result.completion_tokens)
            return result

    async def _once(self, prompt, worktree, session_id, task_id, attempt):
        argv = self.argv(prompt, session_id)
        t0 = time.monotonic()
        # opencode (bun/JS) resolves its project from $PWD rather than getcwd(),
        # and a subprocess inherits the parent's $PWD — set it to the worktree
        # or edits land wherever the orchestrator was launched from.
        env = dict(os.environ, PWD=str(worktree))
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
        chunks = []
        last_chunk_t = time.monotonic()
        deadline = t0 + config.DRIVER_TIMEOUT
        try:
            with open(tpath, "wb") as fh:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    chunk = await asyncio.wait_for(proc.stdout.read(65536), remaining)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    last_chunk_t = time.monotonic()
                    fh.write(chunk)
                    fh.flush()
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            err_task.cancel()
            partial = b"".join(chunks).decode(errors="replace")
            psid, _ = parse_transcript(partial)
            idle = round(time.monotonic() - last_chunk_t, 1)
            kind = "stalled" if idle < config.DRIVER_TIMEOUT - 30 else "timed out"
            events.emit("driver.stalled" if kind == "stalled" else "driver.timeout",
                        harness=self.harness, model=self.model, task=task_id,
                        attempt=attempt, bytes=sum(len(c) for c in chunks),
                        idle_s=idle, session_id=psid or session_id)
            raise DriverError(
                f"{argv[0]} {kind} after {config.DRIVER_TIMEOUT}s "
                f"(idle {idle}s, {sum(len(c) for c in chunks)} bytes)",
                session_id=psid or session_id)
        err = await err_task
        await proc.wait()
        out = b"".join(chunks)
        raw = out.decode(errors="replace")
        sid, text = parse_transcript(raw)
        toks, ptok, ctok = transcript_tokens(raw)
        if proc.returncode != 0:
            raise DriverError(
                f"{argv[0]} exited {proc.returncode}: {err.decode(errors='replace')[-300:]}",
                session_id=sid or session_id)
        return DriverResult(self.harness, self.model, self.role, proc.returncode,
                            session_id or sid, str(tpath), text,
                            round(time.monotonic() - t0, 1), toks, ptok, ctok)


class KimiDriver(Driver):
    harness = "kimi"
    model = "Kimi-K3"

    def __init__(self, role, bench=False):
        if not bench and role not in ("planner", "reviewer", "implementer"):
            raise ValueError(f"KimiDriver role must be planner|reviewer|implementer, got {role!r}")
        self.role = role

    def argv(self, prompt, session_id):
        a = ["kimi"]
        if session_id:
            a += ["--session", session_id]
        return a + ["-p", prompt, "--output-format", "stream-json"]


class OpencodeDriver(Driver):
    harness = "opencode"

    def __init__(self, model, role, bench=False):
        if not bench:
            if model in ("gpt-oss-120b", "DeepSeek-V4-Flash") and role != "implementer":
                raise ValueError(f"{model} may only implement, not {role!r}")
            if model == "GLM-5.3" and role not in ("planner", "reviewer", "implementer"):
                raise ValueError(f"GLM-5.3 may only plan/review/implement, not {role!r}")
            if model not in config.IMPLEMENTER_MODELS:
                raise ValueError(f"unmapped opencode model: {model!r}")
        self.model = model
        self.role = role

    def argv(self, prompt, session_id):
        a = ["opencode", "run", "-m", f"ARC/{self.model}", "--auto", "--format", "json"]
        if session_id:
            a.append("-c")
        return a + [prompt]
