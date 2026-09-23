"""Module C2: the blind, rotating visual judge.

Three properties make this a judge rather than a second opinion from the
author:

  BLIND.     The judge sees renders, the target buckets, and the phase it is
             judging. It does NOT see the commit history, the task prompt, the
             implementer's reasoning, or the previous verdict's text. A judge
             told what was attempted grades the attempt; a judge shown only
             the result grades the result. Everything withheld here is
             withheld on purpose, and `build_messages` is the only place that
             decides what goes in.

  ROTATED.   A different model judges each round (`JUDGE_ROTATION`). One model
             judging every round of a twenty-round build does not measure the
             build converging, it measures the build converging ON THAT
             MODEL'S TASTE — and it cannot see its own blind spots by
             construction. Rotation is also what makes the arbitrator
             meaningful: disagreement between rounds is information only when
             the rounds had different readers.

  CRASH-AWARE. A judge that errored, or ended without a parseable verdict, did
             NOT judge. It returns `crashed` and the caller re-runs the JUDGE.
             This is AGENTS.md Rule 2's hardest-won lesson carried into the
             visual loop: a crash recorded as a rejection sends the
             implementer off to fix defects nobody found. That mistake cost a
             real task eighteen rounds of a passing gate.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path

import config
import events
from studio import openrouter
from studio.evaluation import arbitrator
from studio.memory import compactor
from studio.schemas.task import PHASE_INTENT

TARGET_FILE = "studio_target.json"

# Preference order, strongest visual reader first. Filtered to today's roster
# at call time, so this list never routes to a model that has left.
# Both profiles' judges, strongest visual reader first. Filtered to today's
# roster at call time, so this never routes to a model that is not live:
# under ARC_FLEET=studio it resolves to the subscription CLIs, under
# studio-api to the OpenRouter models.
# Antigravity-Gemini is Gemini on the subscription profile. Without it that
# profile's panel was Claude and GPT-6-Sol only: two families judging every
# round, one of which usually wrote the code being judged.
JUDGE_ROTATION = ("Gemini-3.8-Flash", "Antigravity-Gemini", "Claude-Opus-5.5",
                  "GPT-6-Astra", "GPT-6-Sol", "GPT-6-Luna")

ARTIFACT_KINDS = ("clipping", "missing_material", "light_leak", "z_fighting",
                  "floating_geometry", "inverted_normals", "texture_stretch",
                  "lod_popping", "other")


def available_judges():
    """Rotation members on today's roster that may hold a reviewer role."""
    return [m for m in JUDGE_ROTATION
            if m in config.MODEL_ROLES and config.model_may(m, "reviewer")]


def judge_for_round(round_n, *, judges=None):
    """Which model judges this round. Deterministic, so a run is replayable."""
    pool = list(judges or available_judges())
    if not pool:
        raise ValueError(
            "no visual judge is available on today's roster "
            f"(fleet={config.FLEET}). The judge rotation is "
            f"{list(JUDGE_ROTATION)}; run with ARC_FLEET=studio.")
    return pool[(int(round_n) - 1) % len(pool)]


def load_target(project_dir):
    """The two target buckets the judge scores against.

    Bucket A is EXACT: measurements, counts, layout facts that are either
    matched or not. Bucket B is ATMOSPHERE: palette, light, mood — matched by
    degree. They are scored separately because a build can nail one and miss
    the other completely, and a single blended number hides exactly that.
    """
    path = Path(project_dir) / TARGET_FILE
    if not path.exists():
        raise FileNotFoundError(
            f"{path} does not exist. Phase 0 (target grounding) produces it: "
            "Bucket A (exact geometry) and Bucket B (atmosphere). Without a "
            "target the judge has nothing to score against and its numbers "
            "would be invented.")
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not doc.get("bucket_a") and not doc.get("bucket_b"):
        raise ValueError(f"{path}: needs at least one of bucket_a / bucket_b")
    return doc


def _bucket_text(bucket):
    if isinstance(bucket, dict):
        return "\n".join(f"  - {k}: {v}" for k, v in bucket.items())
    if isinstance(bucket, (list, tuple)):
        return "\n".join(f"  - {v}" for v in bucket)
    return f"  {bucket}"


SYSTEM_PROMPT = """You are an independent visual judge for a 3D multiplayer \
prison-escape game. You are shown rendered frames and a written target. You \
score what is IN THE FRAMES.

You do not have, and must not ask for, the commit history, the task list, the \
developer's intent, or any previous verdict. You are not reviewing an effort; \
you are measuring a result against a target. If something is wrong you say \
what is wrong and where, in a directive an engineer can act on without \
guessing which object you meant.

Reply with ONE JSON object and nothing else:

{
  "bucket_a_score": <0-100, exact-geometry conformance>,
  "bucket_b_score": <0-100, atmosphere/style conformance>,
  "score": <0-100, overall>,
  "pass": <true|false>,
  "artifacts": [
    {"camera": "<camera name>", "kind": "<one of: %s>",
     "severity": "low|medium|high", "note": "<what and where>"}
  ],
  "directives": ["<one actionable change per string>"],
  "summary": "<two sentences at most>"
}

Rules for the fields:
- Every artifact MUST name the camera it is visible in.
- A directive is an instruction, not an observation: "raise the corridor \
fill light" not "the corridor is dark".
- Do not invent defects to seem thorough. An empty artifacts list is a valid \
verdict when the frames are clean.
- Judge ONLY what this phase is responsible for; the phase brief says what \
that is. Unlit graybox primitives are not a lighting defect in phase 1.
""" % ("|".join(ARTIFACT_KINDS))


def build_messages(target, phase, round_n, images, *, extra_note=""):
    """The whole judge context. Nothing reaches the model that is not here."""
    parts = [openrouter.text_part(
        f"ROUND {round_n}. PHASE: {phase}\n{PHASE_INTENT.get(phase, '')}\n\n"
        "TARGET BUCKET A (exact geometry — matched or not):\n"
        f"{_bucket_text(target.get('bucket_a', {})) or '  (none given)'}\n\n"
        "TARGET BUCKET B (atmosphere and style — matched by degree):\n"
        f"{_bucket_text(target.get('bucket_b', {})) or '  (none given)'}"
        + (f"\n\n{extra_note}" if extra_note else ""))]
    for img in images:
        role = img.get("role", "current")
        label = (f"[{role}] camera '{img['name']}'"
                 f"{' (' + img['kind'] + ')' if img.get('kind') else ''}"
                 f" round {img.get('round', round_n)}")
        if img.get("note"):
            label += f"\nThis camera is looking for: {img['note']}"
        if role == "baseline":
            label += ("\nThis is the BASELINE for comparison, not the frame "
                      "under judgement.")
        parts.append(openrouter.text_part(label))
        parts.append(openrouter.image_part(img["path"]))
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": parts}]


def _normalise(verdict):
    """Coerce a parsed verdict into the contract, or return None if it isn't one."""
    if not isinstance(verdict, dict):
        return None
    def num(key, default=None):
        v = verdict.get(key, default)
        try:
            return max(0.0, min(100.0, float(v)))
        except (TypeError, ValueError):
            return None
    score = num("score")
    a, b = num("bucket_a_score"), num("bucket_b_score")
    if score is None:
        # Salvage: an overall score is derivable from the two buckets. The
        # repo's verdict parser takes the same posture — a model that gave
        # usable numbers in an unusable shape has not failed.
        halves = [v for v in (a, b) if v is not None]
        score = sum(halves) / len(halves) if halves else None
    if score is None:
        return None
    arts = []
    for art in verdict.get("artifacts") or []:
        if not isinstance(art, dict):
            continue
        arts.append({
            "camera": str(art.get("camera", "")),
            "kind": str(art.get("kind", "other")),
            "severity": str(art.get("severity", "medium")),
            "note": str(art.get("note", "")),
        })
    directives = [str(d) for d in (verdict.get("directives") or []) if str(d).strip()]
    passed = verdict.get("pass")
    if not isinstance(passed, bool):
        passed = score >= config.STUDIO_JUDGE_PASS
    return {"score": round(score, 1),
            "bucket_a_score": None if a is None else round(a, 1),
            "bucket_b_score": None if b is None else round(b, 1),
            "pass": bool(passed), "artifacts": arts, "directives": directives,
            "summary": str(verdict.get("summary", ""))[:1000]}


# Every judge on the rotation is a REASONING model, and reasoning tokens are
# spent before a single character of the verdict is emitted. A budget sized
# for the JSON alone returns finish_reason="length" and content=None — an
# empty answer that looks exactly like a crash. 12000 leaves room for the
# thinking and the verdict; the verdict itself is under 1000 tokens.
JUDGE_MAX_TOKENS = 12000


def _cli_prompt(target, phase, round_n, images, extra_note=""):
    """The judge prompt as ONE string, for harnesses with no system role.

    Same content as build_messages, flattened: a CLI harness takes a single
    prompt and reads the images from disk itself.
    """
    lines = [SYSTEM_PROMPT, "", f"ROUND {round_n}. PHASE: {phase}",
             PHASE_INTENT.get(phase, ""), "",
             "TARGET BUCKET A (exact geometry — matched or not):",
             _bucket_text(target.get("bucket_a", {})) or "  (none given)", "",
             "TARGET BUCKET B (atmosphere and style — matched by degree):",
             _bucket_text(target.get("bucket_b", {})) or "  (none given)", ""]
    if extra_note:
        lines += [extra_note, ""]
    lines.append("THE FRAMES, in the order the files are given:")
    for img in images:
        role = img.get("role", "current")
        line = (f"  {Path(img['path']).name} = camera '{img['name']}'"
                f"{' (' + img['kind'] + ')' if img.get('kind') else ''}"
                f", round {img.get('round', round_n)}")
        if role == "baseline":
            line += " — BASELINE for comparison, not the frame under judgement"
        if img.get("note"):
            line += f"\n      looking for: {img['note']}"
        lines.append(line)
    lines += ["", "Reply with the JSON object and nothing else."]
    return "\n".join(lines)


def _stage_images(project, round_n, model, images):
    """Copy the context frames into one directory the harness can read.

    The frames live across several round directories; a harness is given ONE
    working directory. Copying (rather than pointing at the archive) also
    keeps a judge from wandering into other rounds' evidence, which is part of
    judging blind.
    """
    safe = model.replace("/", "_").replace(":", "-")
    stage = compactor.round_dir(project, round_n, create=True) / f"judge-{safe}"
    stage.mkdir(parents=True, exist_ok=True)
    staged = []
    for img in images:
        src = Path(img["path"])
        dest = stage / f"{img.get('role', 'current')}-r{img.get('round', round_n)}-{src.name}"
        if not dest.exists():
            shutil.copy2(src, dest)
        staged.append({**img, "path": str(dest)})
    return stage, staged


async def _cli_judge(project, round_n, *, target, phase, images, model):
    """Judge through a subscription CLI harness (no per-token billing).

    Runs the model's own driver, so this inherits everything the harness layer
    already provides: the concurrency lease, the retry ladder, the stall
    clock, a live transcript on disk, and a harness_runs row (Rule 7).
    """
    import drivers
    stage, staged = _stage_images(project, round_n, model, images)
    driver = drivers.driver_for(model, "reviewer")
    driver.images = [i["path"] for i in staged]
    prompt = _cli_prompt(target, phase, round_n, staged)
    res = await driver.run(prompt, stage,
                           task_id=f"judge-{project}-r{round_n}")
    usage = {"prompt_tokens": res.prompt_tokens,
             "completion_tokens": res.completion_tokens,
             "cost_usd": 0.0,        # covered by the subscription
             "finish_reason": "", "reasoning_tokens": 0}
    if res.exit_code != 0 and not (res.text or "").strip():
        raise openrouter.OpenRouterError(
            f"{model} exited {res.exit_code} without output")
    return res.text or "", usage


def judge(project, round_n, *, project_dir, phase, images=None, model=None,
          extra_note="", max_tokens=JUDGE_MAX_TOKENS):
    """Judge one round. Returns a verdict dict, or a crash record.

    A crash record is `{"crashed": True, "error": ...}`. The caller must
    re-run the JUDGE on it — never treat it as a rejection and never send it
    back to an implementer as feedback.
    """
    target = load_target(project_dir)
    imgs = images if images is not None else compactor.context_images(project, round_n)
    if not imgs:
        raise ValueError(
            f"round {round_n} has no archived renders to judge; call "
            "studio.memory.compactor.archive after rendering")
    validation = False
    if not model and config.STUDIO_FREE_JUDGE_MODEL:
        model, validation = config.STUDIO_FREE_JUDGE_MODEL, True
    elif model and model == config.STUDIO_FREE_JUDGE_MODEL:
        validation = True
    model = model or judge_for_round(round_n)
    started = time.time()
    backend = "api" if config.STUDIO_API else "cli"
    events.emit("studio.judge_start", project=str(project), round=int(round_n),
                model=model, phase=phase, images=len(imgs), backend=backend)
    try:
        if backend == "api":
            msg, usage = openrouter.chat(
                model, build_messages(target, phase, round_n, imgs,
                                      extra_note=extra_note),
                temperature=0.1, max_tokens=max_tokens,
                response_format={"type": "json_object"},
                task=f"judge:{project}:r{round_n}")
            raw_text = msg.content or ""
        else:
            raw_text, usage = asyncio.run(_cli_judge(
                project, round_n, target=target, phase=phase, images=imgs,
                model=model))
    except Exception as exc:                                # noqa: BLE001
        events.emit("studio.judge_crashed", project=str(project),
                    round=int(round_n), model=model, error=str(exc)[:300])
        return {"crashed": True, "model": model, "round": int(round_n),
                "error": str(exc)[:300]}
    content = (raw_text or "").strip()
    if not content and usage.get("finish_reason") == "length":
        # Not a crash and not a bad verdict: the model never got to speak.
        # Saying so precisely is the difference between "raise the budget" and
        # a fruitless hunt through the judge prompt.
        events.emit("studio.judge_crashed", project=str(project),
                    round=int(round_n), model=model, error="token budget exhausted",
                    reasoning_tokens=usage.get("reasoning_tokens", 0))
        return {"crashed": True, "model": model, "round": int(round_n),
                "error": (f"{model} spent its entire {max_tokens}-token budget on "
                          f"reasoning ({usage.get('reasoning_tokens', 0)} reasoning "
                          "tokens) without emitting a verdict. Re-run with a larger "
                          "max_tokens."),
                "finish_reason": "length"}
    parsed = _normalise(openrouter.parse_json_object(content))
    if parsed is None:
        # No parseable verdict is a CRASH, not a failing score. Scoring it 0
        # would send the build back to be fixed for defects the judge never
        # named — the exact fail-closed mistake Rule 2 documents.
        events.emit("studio.judge_crashed", project=str(project),
                    round=int(round_n), model=model,
                    error="no parseable verdict",
                    head=content[:300])
        return {"crashed": True, "model": model, "round": int(round_n),
                "error": "the judge produced no parseable verdict",
                "raw": content[:2000]}
    parsed.update({"model": model, "validation": validation,
                   "round": int(round_n), "phase": phase,
                   "ts": time.time(), "seconds": round(time.time() - started, 1),
                   "images": [i["name"] for i in imgs],
                   "cost_usd": usage.get("cost_usd", 0.0)})
    safe = model.replace("/", "_").replace(":", "-")
    out = compactor.round_dir(project, round_n, create=True) / f"verdict-{safe}.json"
    out.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
    events.emit("studio.judge_done", project=str(project), round=int(round_n),
                model=model, score=parsed["score"], passed=parsed["pass"],
                artifacts=len(parsed["artifacts"]),
                cost_usd=usage.get("cost_usd", 0.0))
    return parsed


def verdicts(project, round_n, *, include_validation=False):
    """Every verdict recorded for a round."""
    d = compactor.round_dir(project, round_n)
    if not d.exists():
        return []
    out = []
    for path in sorted(d.glob("verdict-*.json")):
        try:
            v = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        # A verdict from the free VALIDATION judge proves the loop runs; it is
        # not evidence about the build, so it never reaches a gate. Pass
        # include_validation=True to see it in a report.
        if v.get("validation") and not include_validation:
            continue
        out.append(v)
    return out


def history(project):
    """[(round, [directives])] across all judged rounds, for the arbitrator."""
    out = []
    for n in compactor.rounds(project):
        ds = []
        for v in verdicts(project, n):
            ds.extend(v.get("directives") or [])
        if ds:
            out.append((n, ds))
    return out


def assess(project):
    """Is the judge loop converging, or are the judges arguing?"""
    return arbitrator.assess(history(project))
