import asyncio
import hashlib
import json
import random
import re
import time
import uuid
from collections import Counter, defaultdict

from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)

import config
import events
from config import FAMILIES, FAMILY_ORDER


class EmptyStreamError(Exception):
    pass


RETRYABLE = (
    RateLimitError,
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    EmptyStreamError,
)


def _retry_after(exc):
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if headers:
        val = headers.get("retry-after")
        if val:
            try:
                return float(val)
            except (TypeError, ValueError):
                return None
    return None


def _is_session_limit(exc):
    """ARC reports account-level concurrency caps as HTTP 400 with this detail."""
    if not isinstance(exc, BadRequestError):
        return False
    msg = str(exc).lower()
    return "session limit" in msg or "concurrent" in msg


def _first_question(text):
    m = re.search(r"Question:\s*(.+)", text)
    return m.group(1).strip() if m else text[:60]


class ArcPool:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.client = None
        if not dry_run:
            if not config.API_KEY or "PASTE-YOUR-KEY" in config.API_KEY:
                raise RuntimeError(
                    "ARC_API_KEY is not set; add your key from llm.arc.vt.edu "
                    "(User profile > Settings > Account > API keys) to the .env file"
                )
            self.client = AsyncOpenAI(
                api_key=config.API_KEY,
                base_url=config.BASE_URL,
                timeout=config.REQUEST_TIMEOUT,
                max_retries=0,
            )
        self.sems = {f: asyncio.Semaphore(config.family_limit(f)) for f in FAMILY_ORDER}
        self.inflight = {f: 0 for f in FAMILY_ORDER}
        self.stats = {
            "requests": Counter(),
            "tokens": Counter(),
            "errors": Counter(),
            "retries": Counter(),
        }
        self._verify_attempts = defaultdict(int)

    def resolve_model(self, family, effort="default", websearch=False):
        # chat() rejects an unknown family with ValueError; this raised a bare
        # KeyError for the same mistake, so `main.py ask --family <gone>` gave
        # a traceback instead of the message chat() would have printed.
        if family not in FAMILIES:
            raise ValueError(f"unknown family: {family}; "
                             f"choices: {sorted(FAMILIES)}")
        fam = FAMILIES[family]
        if websearch:
            if not fam.websearch_model:
                raise ValueError(f"family {family} has no websearch variant")
            return fam.websearch_model
        return fam.models.get(effort) or fam.models["default"]

    async def chat(
        self,
        family,
        messages,
        *,
        effort="default",
        websearch=False,
        purpose="chat",
        temperature=None,
        meta=None,
    ):
        if family not in FAMILIES:
            raise ValueError(f"unknown family: {family}")
        model = self.resolve_model(family, effort, websearch)
        key = f"{purpose}:{family}"
        attempt = 0
        session_attempts = 0
        while True:
            try:
                text, info = await self._once(family, model, messages, websearch, purpose, temperature)
                if meta is not None:
                    info["attempts"] = session_attempts + attempt + 1
                    meta.update(info)
                return text
            except BadRequestError as exc:
                if not _is_session_limit(exc):
                    self.stats["errors"][key] += 1
                    events.emit("request", family=family, model=model, purpose=purpose,
                                ok=False, error=str(exc)[:200])
                    raise
                session_attempts += 1
                if session_attempts > config.SESSION_RETRIES:
                    self.stats["errors"][key] += 1
                    events.emit("request", family=family, model=model, purpose=purpose,
                                ok=False, error="session limit exhausted", attempts=session_attempts)
                    raise
                self.stats["retries"][key] += 1
                delay = min(3.0 * (2 ** (session_attempts - 1)) + random.random() * 2, config.SESSION_BACKOFF_CAP)
                await asyncio.sleep(delay)
            except RETRYABLE as exc:
                attempt += 1
                if attempt > config.MAX_RETRIES:
                    self.stats["errors"][key] += 1
                    events.emit("request", family=family, model=model, purpose=purpose,
                                ok=False, error=str(exc)[:200])
                    raise
                self.stats["retries"][key] += 1
                delay = _retry_after(exc)
                if delay is None:
                    delay = min((2 ** (attempt - 1)) * 1.5 + random.random(), 60)
                await asyncio.sleep(delay)
            except Exception as exc:
                self.stats["errors"][key] += 1
                events.emit("request", family=family, model=model, purpose=purpose,
                            ok=False, error=str(exc)[:200])
                raise

    async def _once(self, family, model, messages, websearch, purpose, temperature):
        key = f"{purpose}:{family}"
        t0 = time.monotonic()
        req_id = uuid.uuid4().hex[:12]
        if self.dry_run:
            self.stats["requests"][key] += 1
            events.emit("request_start", req_id=req_id, family=family, model="dry-run",
                        purpose=purpose, websearch=websearch)
            text = await self._fake_chat(family, messages, purpose, websearch)
            info = {
                "model": "dry-run",
                "tokens": max(1, len(text) // 4),
                "latency_ms": int((time.monotonic() - t0) * 1000),
            }
            events.emit("request", family=family, purpose=purpose,
                        websearch=websearch, ok=True, req_id=req_id, **info)
            return text, info
        kwargs = {"model": model, "messages": messages, "stream": True}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if websearch:
            kwargs["extra_body"] = {"tool_ids": ["server:websearch"]}
        self.stats["requests"][key] += 1
        async with self.sems[family]:
            self.inflight[family] += 1
            events.emit("request_start", req_id=req_id, family=family, model=model,
                        purpose=purpose, websearch=websearch)
            try:
                stream = await self.client.chat.completions.create(**kwargs)
                parts = []
                usage = None
                async for chunk in stream:
                    if getattr(chunk, "usage", None):
                        usage = chunk.usage
                    if chunk.choices:
                        content = getattr(chunk.choices[0].delta, "content", None)
                        if content:
                            parts.append(content)
                text = "".join(parts)
                if not text.strip():
                    raise EmptyStreamError(f"empty stream from {model}")
                tokens = usage.total_tokens if usage else max(1, len(text) // 4)
                self.stats["tokens"][key] += tokens
                info = {
                    "model": model,
                    "tokens": tokens,
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                }
                if usage:
                    for field in ("prompt_tokens", "completion_tokens"):
                        val = getattr(usage, field, None)
                        if val is not None:
                            info[field] = val
                events.emit("request", family=family, purpose=purpose,
                            websearch=websearch, ok=True, req_id=req_id, **info)
                return text, info
            except Exception as exc:
                events.emit("request_end", req_id=req_id, family=family, model=model,
                            purpose=purpose, ok=False, error=str(exc)[:200])
                raise
            finally:
                self.inflight[family] -= 1

    async def _fake_chat(self, family, messages, purpose, websearch):
        await asyncio.sleep(random.uniform(0.02, 0.15))
        u = messages[-1]["content"]
        if purpose == "questions":
            m = re.search(r"Generate (\d+)", u)
            n = int(m.group(1)) if m else 4
            t = re.search(r"about:\s*(.+)", u)
            topic = t.group(1).strip() if t else "dry-run"
            return json.dumps(
                {"questions": [f"What is unresolved about {topic} (part {i + 1})?" for i in range(n)]}
            )
        if purpose == "answer":
            return f"[dry-run {family}] Rigorous answer to: {_first_question(u)}"
        if purpose == "research":
            return (
                f"[dry-run websearch via {family}] Current web findings on: {_first_question(u)} "
                "— Source A (2026) reports X, Source B (2026) reports Y."
            )
        if purpose == "critique":
            h = int(hashlib.sha256(u.encode()).hexdigest(), 16)
            return json.dumps(
                {"score": 5 + h % 5, "verdict": "solid" if h % 2 else "flawed", "issues": "minor unsupported claims"}
            )
        if purpose == "synthesis":
            v = 2 if "rejected" in u.lower() else 1
            return f"[dry-run synthesis v{v}] Consolidated, web-grounded answer."
        if purpose == "verify":
            q = _first_question(u)
            key = hashlib.sha256(q.encode()).hexdigest()[:12]
            self._verify_attempts[key] += 1
            if self._verify_attempts[key] == 1 and int(key, 16) % 3 == 0:
                return json.dumps({"passed": False, "score": 5.0, "feedback": "claims lack web grounding"})
            return json.dumps({"passed": True, "score": 8.5, "feedback": ""})
        if purpose == "seeds":
            m = re.search(r"propose\s+(\d+)", u)
            n = int(m.group(1)) if m else 3
            return json.dumps({"topics": [f"Dry-run follow-up topic {i + 1}" for i in range(n)]})
        if purpose == "bench":
            return ("```python\n# dry-run placeholder solution\ndef placeholder():\n"
                    "    return None\n```")
        if purpose == "plan":
            return json.dumps({
                "modules": {
                    "engine": ["Engine"],
                    "world": ["World"],
                    "player": ["Player"],
                    "ui": ["UI"],
                    "main": ["Main"],
                    "html": ["<html", "</html>"],
                }
            })
        if purpose == "implement":
            m = re.search(r"MODULE: (\w+)", u)
            module = m.group(1) if m else "engine"
            if module == "html":
                return (
                    "```html\n<!DOCTYPE html>\n<html>\n<head><meta charset=\"utf-8\">"
                    "<title>voxel</title></head>\n<body>\n"
                    "<script src=\"https://unpkg.com/three@0.160.0/build/three.min.js\"></script>\n"
                    "<script src=\"js/engine.js\"></script>\n<script src=\"js/world.js\"></script>\n"
                    "<script src=\"js/player.js\"></script>\n<script src=\"js/ui.js\"></script>\n"
                    "<script src=\"js/main.js\"></script>\n</body>\n</html>\n```"
                )
            syms = re.search(r"symbols: \[([^\]]*)\]", u)
            names = [s.strip().strip("'\"") for s in syms.group(1).split(",")] if syms else []
            names = [n for n in names if n and not n.startswith("<")] or [module.title()]
            parts = [f"// {module} dry-run implementation"]
            for n in names:
                parts.append(
                    f"class {n} {{\n  constructor() {{ this.ready = true; }}\n  update(dt) {{ }}\n}}\nwindow.{n} = {n};"
                )
            return "```js\n" + "\n".join(parts) + "\n```"
        if purpose == "review":
            return json.dumps({"score": 8.2, "verdict": "ok", "issues": ""})
        if purpose == "integration":
            m = re.search(r"Integration round: (\d+)", u)
            rn = int(m.group(1)) if m else 1
            if rn <= 1:
                return json.dumps({
                    "passed": False,
                    "score": 6.0,
                    "fixes": [
                        {"module": "main", "directive": "call World.generate() during boot before first render"},
                        {"module": "player", "directive": "clamp terminal fall speed to avoid tunneling"},
                    ],
                })
            return json.dumps({"passed": True, "score": 8.6, "fixes": []})
        return f"[dry-run {family}] {u[:80]}"

    def snapshot(self):
        return {
            "requests": dict(self.stats["requests"]),
            "tokens": dict(self.stats["tokens"]),
            "errors": dict(self.stats["errors"]),
            "retries": dict(self.stats["retries"]),
            "inflight": dict(self.inflight),
            "capacity": {f: config.family_limit(f) for f in FAMILY_ORDER},
        }