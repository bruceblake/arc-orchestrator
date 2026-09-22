"""The direct OpenRouter path, for the two things a coding harness cannot do.

Most studio work goes through the normal pipeline: a taskfile task, an
opencode harness, a worktree, a gate, review, a pull request. That path
cannot do two things this workload needs:

  * send IMAGES to a model (the visual judge scores renders), and
  * drive a tool loop over a virtual desktop and Blender (the Astra
    operator).

Both are model calls with attachments and tools, not code edits, so they talk
to OpenRouter directly through the OpenAI-compatible SDK the repo already
depends on. Everything else about them still obeys the repo's rules: every
call is metered against the budget (Rule: studio.budget), every call leaves an
event (Rule 7), and a model name is always a ROSTER name resolved through
config — never a provider id written inline, which would let this module
outlive a roster change.
"""
from __future__ import annotations

import base64
import json
import os
import mimetypes
from pathlib import Path

import config
import events

_DOTENV_LOADED = False


def api_key():
    """The OpenRouter key, from the environment or the repo's .env.

    The fleet's own processes are not login shells and systemd units get a
    minimal environment, so falling back to .env is what makes a scheduled
    studio run work at all.
    """
    global _DOTENV_LOADED
    key = os.getenv("OPENROUTER_API_KEY", "")
    if key:
        return key
    if not _DOTENV_LOADED:
        _DOTENV_LOADED = True
        try:
            from dotenv import load_dotenv
            load_dotenv(Path(config.ROOT) / ".env")
        except Exception:                                   # noqa: BLE001
            return ""
    return os.getenv("OPENROUTER_API_KEY", "")


def provider_id(model):
    """The OpenRouter id for a roster model name.

    Resolved through config.MODEL_HARNESS_ALIAS, which is keyed by roster
    model and gated on EXTERNAL_MODELS, so this cannot name a model the
    roster has dropped. The alias is `openrouter/<vendor>/<id>`; OpenRouter
    itself wants `<vendor>/<id>`.
    """
    # The validation hatch: a raw `vendor/id` that is deliberately NOT on the
    # roster. Accepted only when it is the configured free-judge model, so a
    # typo elsewhere still fails loudly instead of silently calling something
    # nobody chose.
    if model and model == config.STUDIO_FREE_JUDGE_MODEL and "/" in model:
        return model
    alias = config.provider_model_alias(model)
    if not alias:
        raise ValueError(
            f"{model!r} is not an external model on today's roster "
            f"(fleet={config.FLEET}). Studio model calls require "
            "ARC_FLEET=studio.")
    prefix, _, rest = alias.partition("/")
    if prefix != "openrouter" or not rest:
        raise ValueError(f"{model!r} is not served by OpenRouter (alias={alias!r})")
    return rest


class OpenRouterError(RuntimeError):
    pass


def _client():
    key = api_key()
    if not key:
        raise OpenRouterError(
            "OPENROUTER_API_KEY is not set (checked the environment and "
            f"{Path(config.ROOT) / '.env'}). Every studio model is served by "
            "OpenRouter; without the key nothing in this package can run.")
    try:
        from openai import OpenAI
    except ImportError as exc:                              # pragma: no cover
        raise OpenRouterError(f"the openai SDK is not installed: {exc}")
    return OpenAI(base_url=config.OPENROUTER_BASE_URL, api_key=key)


def image_part(path):
    """One image as an OpenAI-style content part (a base64 data URL)."""
    path = Path(path)
    data = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    b64 = base64.b64encode(data).decode("ascii")
    return {"type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"}}


def text_part(text):
    return {"type": "text", "text": text}


def chat(model, messages, *, tools=None, temperature=0.2, max_tokens=8000,
         response_format=None, task="", timeout=900):
    """One OpenRouter chat call. Returns (message, usage_dict).

    `model` is a ROSTER name (e.g. "Gemini-3.8-Flash"), not a provider id.
    The call is charged against the studio budget BEFORE it is made — see
    studio.budget.guard — because a ceiling checked afterwards is a bill.
    """
    from studio import budget

    pid = provider_id(model)
    budget.guard(model, task=task)
    client = _client()
    kwargs = {
        "model": pid,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "timeout": timeout,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    if response_format:
        kwargs["response_format"] = response_format
    events.emit("studio.call", model=model, provider_model=pid, task=task,
                tools=bool(tools))
    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception as exc:                                # noqa: BLE001
        events.emit("studio.call_error", model=model, task=task,
                    error=str(exc)[:300])
        raise OpenRouterError(f"{model} ({pid}): {exc}") from exc
    usage = getattr(resp, "usage", None)
    u = {"prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
         "completion_tokens": getattr(usage, "completion_tokens", 0) or 0}
    u["cost_usd"] = config.cost_of(model, u["prompt_tokens"], u["completion_tokens"])
    budget.record(model, u, task=task)
    if not resp.choices:
        raise OpenRouterError(f"{model}: the response carried no choices")
    choice = resp.choices[0]
    # Reasoning models spend the token budget on reasoning BEFORE any visible
    # content, and OpenRouter reports that as finish_reason="length" with
    # content=None. Measured 2026-09-22: google/gemini-3.8-flash answered
    # "say ok" with 36 reasoning tokens and no content at max_tokens=5.
    # Callers must be able to tell "ran out of room" from "said nothing",
    # so the reason travels with the usage.
    u["finish_reason"] = getattr(choice, "finish_reason", "") or ""
    u["reasoning_tokens"] = (
        (getattr(getattr(usage, "completion_tokens_details", None),
                 "reasoning_tokens", 0) or 0) if usage else 0)
    return choice.message, u


def parse_json_object(text):
    """Parse a JSON object out of model text, tolerating fences and prose.

    Deliberately the same posture as code_tasks._parse_verdict: a model that
    wrapped good JSON in ```json or added a sentence of preamble has NOT
    failed, and treating that as a failure costs a whole round.
    """
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        pass
    depth, start = 0, -1
    for i, ch in enumerate(t):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    obj = json.loads(t[start:i + 1])
                    if isinstance(obj, dict):
                        return obj
                except ValueError:
                    start = -1
    return None
