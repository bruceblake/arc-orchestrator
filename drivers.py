"""Headless CLI drivers for the coding harnesses (kimi, opencode).

Role map (hard rule): Kimi-K3 (kimi CLI) and GLM-5.3 (opencode) only plan and
review; gpt-oss-120b and DeepSeek-V4-Flash (opencode) only implement. ARC
rejects over-limit requests per model, so per-model semaphores cap concurrent
harness instances below the account limits (config.driver_limit).
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
    pass


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
        while True:
            attempt += 1
            events.emit("driver.start", harness=self.harness, model=self.model,
                        role=self.role, task=task_id, attempt=attempt)
            await gate.acquire()
            try:
                result = await self._once(prompt, worktree, sid, task_id, attempt)
            except DriverError as exc:
                gate.release()
                events.emit("driver.error", harness=self.harness, model=self.model,
                            task=task_id, attempt=attempt, error=str(exc)[:300])
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
                        seconds=round(result.seconds, 1))
            return result

    async def _once(self, prompt, worktree, session_id, task_id, attempt):
        argv = self.argv(prompt, session_id)
        t0 = time.monotonic()
        # opencode (bun/JS) resolves its project from $PWD rather than getcwd(),
        # and a subprocess inherits the parent's $PWD — set it to the worktree
        # or edits land wherever the orchestrator was launched from.
        env = dict(os.environ, PWD=str(worktree))
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(worktree), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), config.DRIVER_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise DriverError(f"{argv[0]} timed out after {config.DRIVER_TIMEOUT}s")
        TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        tpath = TRANSCRIPT_DIR / f"{task_id or 'adhoc'}-{self.role}-{attempt}.jsonl"
        tpath.write_bytes(out)
        sid, text = parse_transcript(out.decode(errors="replace"))
        if proc.returncode != 0:
            raise DriverError(
                f"{argv[0]} exited {proc.returncode}: {err.decode(errors='replace')[-300:]}")
        return DriverResult(self.harness, self.model, self.role, proc.returncode,
                            session_id or sid, str(tpath), text,
                            round(time.monotonic() - t0, 1))


class KimiDriver(Driver):
    harness = "kimi"
    model = "Kimi-K3"

    def __init__(self, role):
        if role not in ("planner", "reviewer"):
            raise ValueError(f"KimiDriver role must be planner|reviewer, got {role!r}")
        self.role = role

    def argv(self, prompt, session_id):
        a = ["kimi"]
        if session_id:
            a += ["--session", session_id]
        return a + ["-p", prompt, "--output-format", "stream-json"]


class OpencodeDriver(Driver):
    harness = "opencode"

    def __init__(self, model, role):
        if model in config.IMPLEMENTER_MODELS and role != "implementer":
            raise ValueError(f"{model} may only implement, not {role!r}")
        if model == "GLM-5.3" and role not in ("planner", "reviewer"):
            raise ValueError(f"GLM-5.3 may only plan/review, not {role!r}")
        if model not in config.IMPLEMENTER_MODELS | {"GLM-5.3"}:
            raise ValueError(f"unmapped opencode model: {model!r}")
        self.model = model
        self.role = role

    def argv(self, prompt, session_id):
        a = ["opencode", "run", "-m", f"ARC/{self.model}", "--auto", "--format", "json"]
        if session_id:
            a.append("-c")
        return a + [prompt]
