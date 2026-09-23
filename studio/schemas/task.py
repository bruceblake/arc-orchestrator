"""Game task schema, and the compiler that turns one into a taskfile entry.

A `GameTask` carries what the game workload needs and the code pipeline has
no concept of: which PHASE the work belongs to, which WORKER does it, which
asset it targets, what the animation must sync against, and whether the
operator needs a virtual desktop. None of that changes how the work is
governed. `to_task()` compiles a GameTask down to an ordinary taskfile task —
id, title, prompt, model, reviewer, verify_cmd, deps — and from that point on
it is indistinguishable from any other task in this repo: same worktree, same
gate, same cross-family review, same pull request, same merge.

That direction is the whole contract, and it only works one way. A game task
may add fields; it may never remove a governance one. In particular there is
no path here that produces a task without a `verify_cmd` (Rule 4) or with a
same-family reviewer (Rule 2) — `to_task` refuses both.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

import config

# --- phases (Module B's vocabulary) -----------------------------------------
# Ordered. A project moves forward one phase at a time, and stage_manager
# enforces that the gate of phase N passed before phase N+1 may be planned.
PHASE_0_TARGET_GROUNDING = "PHASE_0_TARGET_GROUNDING"
PHASE_1_GRAYBOX_PROTOTYPING = "PHASE_1_GRAYBOX_PROTOTYPING"
PHASE_2_3D_ASSET_AND_ANIMATION = "PHASE_2_3D_ASSET_AND_ANIMATION"
PHASE_3_ATMOSPHERE_LIGHTING = "PHASE_3_ATMOSPHERE_LIGHTING"
PHASE_4_NETWORKED_QA = "PHASE_4_NETWORKED_QA"

PHASES = (
    PHASE_0_TARGET_GROUNDING,
    PHASE_1_GRAYBOX_PROTOTYPING,
    PHASE_2_3D_ASSET_AND_ANIMATION,
    PHASE_3_ATMOSPHERE_LIGHTING,
    PHASE_4_NETWORKED_QA,
)

PHASE_INTENT = {
    PHASE_0_TARGET_GROUNDING:
        "Ingest reference into Bucket A (exact geometry: cell dimensions, "
        "corridor widths, vent bore, door clearances) and Bucket B "
        "(atmosphere: palette, light temperature, grime, mood). Produces the "
        "target the judge scores every later round against. No gameplay code.",
    PHASE_1_GRAYBOX_PROTOTYPING:
        "Primitives only — boxes, capsules, planes. No textures, no meshes, "
        "no lighting work. Proves the SPATIAL design: walk/sprint/crouch "
        "speeds, sightlines from the guard tower, crouch clearance in vents, "
        "door widths, the cell-to-yard route. If it is not fun as boxes it "
        "will not be fun with art.",
    PHASE_2_3D_ASSET_AND_ANIMATION:
        "Replace primitives with modelled, retopologised, rigged assets "
        "inside the triangle budget; bind clothing and armour to the "
        "armature; author synchronised interaction clips (a takedown and its "
        "hit reaction are ONE authored pair, not two clips that happen to "
        "play together).",
    PHASE_3_ATMOSPHERE_LIGHTING:
        "Lighting passes, shadow maps, the day/night cycle, guard "
        "searchlights, and the alarm state. This is where Bucket B is won or "
        "lost, and where the adversarial cameras earn their keep.",
    PHASE_4_NETWORKED_QA:
        "Bot swarms against the authoritative server: server authority, "
        "reconciliation under loss, collision desync through vents, packet "
        "boundary fuzzing, tick-rate stability, memory growth.",
}


def phase_index(phase):
    """Position of `phase` in the pipeline, or -1 if it is not a phase."""
    return PHASES.index(phase) if phase in PHASES else -1


# --- workers ----------------------------------------------------------------
# A worker is a ROLE IN THE STUDIO bound to a roster model. The binding is by
# model NAME only; every capability question (may it implement? may it plan?
# what is it paired with for review?) is answered by config's roster at call
# time. This file must never grow a second opinion about what a model may do
# — that drift is what AGENTS.md Rule 2 calls out, and it cost this repo seven
# pull requests.
# Each worker lists CANDIDATE models, strongest/preferred first, and resolves
# to the first one live on today's roster. That is what lets one worker id mean
# "the 3D and implementation operator" across both studio profiles: GPT-6-Astra
# when the fleet runs on OpenRouter, the Codex CLI when it runs on the
# operator's ChatGPT plan. A taskfile written for one profile still routes on
# the other, which is the same courtesy code_tasks.RETIRED_MODELS extends to
# taskfiles written before a roster change.
WORKERS = {
    "opus_architect": {
        "models": ("Claude-Opus-5.5",),
        "brief": "System architect and netcode. Authoritative multiplayer "
                 "protocol (delta compression, snapshot interpolation, client "
                 "prediction, server reconciliation), the prison routine state "
                 "machine, inventory, contraband crafting, clearance levels.",
    },
    "gpt_6_astra_operator": {
        "models": ("GPT-6-Astra", "GPT-6-Sol", "GPT-6-Luna"),
        "brief": "3D, rigging and animation operator. Modular asset "
                 "generation and retopology under a triangle budget, "
                 "auto-rigging and weight transfer, synchronised animation "
                 "clips, and GUI automation of the editor for what the CLI "
                 "does not expose.",
    },
    "grok_feature_driver": {
        # Subscription profile: Grok 4.7 on the Cursor plan (`agent`).
        # studio-api: OpenRouter's Grok-4.7. The GPT-6 tiers remain the
        # fallback for a roster with neither. Cursor-Grok was missing here,
        # so every Grok task silently resolved to GPT-6-Sol and piled onto
        # one subscription window.
        "models": ("Cursor-Grok-4.7", "Grok-4.7", "GPT-6-Sol", "GPT-6-Luna",
                   "GPT-6-Astra"),
        "brief": "In-engine feature driver. Player input controllers "
                 "(sneak/sprint/crawl/crouch, first and third person), HUD and "
                 "UI data-binding: suspicion meter, stamina, noise radius, "
                 "clock.",
    },
    "gemini_visual_judge": {
        # studio-api: Gemini-3.8-Flash via OpenRouter. Subscription profile:
        # Gemini through Antigravity (`agy`). Without the second name this
        # worker had NO live model on the studio profile.
        "models": ("Gemini-3.8-Flash", "Antigravity-Gemini"),
        # Antigravity-Gemini may implement on the roster; THIS worker never
        # does, so it is not offered to the planner as an implementing worker.
        "judge_only": True,
        "brief": "Multimodal visual judge and spatial auditor. Scores renders "
                 "against Bucket A and Bucket B, flags clipping, missing "
                 "materials, light leaks and z-fighting. NEVER implements.",
    },
    "deepseek_qa_swarm": {
        "models": ("DeepSeek-V4.1-Flash-thinking-max",),
        "brief": "Headless multiplayer QA swarm: bot clients, packet-boundary "
                 "fuzzing, desync hunting, tick-rate and leak reporting.",
    },
    "glm_content_swarm": {
        "models": ("GLM-5.3",),
        "brief": "Procedural content: contraband spawn tables, inmate dialogue "
                 "trees, guard announcements, achievement definitions.",
    },
}


def worker_model(worker):
    """The roster model a worker id is bound to.

    Raises for an unknown worker, and for a worker whose model is not on
    TODAY's roster — which is what happens when the studio modules are used
    under ARC_FLEET=local. Failing here, by name, beats failing later inside a
    driver constructor with a role error that does not mention the worker.
    """
    if worker not in WORKERS:
        raise ValueError(
            f"unknown worker {worker!r}; known workers: {sorted(WORKERS)}")
    candidates = WORKERS[worker]["models"]
    for model in candidates:
        if model in config.MODEL_ROLES:
            return model
    raise ValueError(
        f"worker {worker!r} has no live model on today's roster "
        f"(fleet={config.FLEET}); it can use {list(candidates)}. "
        "Run with ARC_FLEET=studio (subscription CLIs) or "
        "ARC_FLEET=studio-api (OpenRouter).")


def implementing_workers():
    """Worker ids whose model may hold the `implementer` role today."""
    out = []
    for w in WORKERS:
        try:
            model = worker_model(w)
        except ValueError:
            continue                      # no live model for this worker today
        if WORKERS[w].get("judge_only"):
            continue
        if config.model_may(model, "implementer"):
            out.append(w)
    return sorted(out)


# --- review pairing ---------------------------------------------------------
# WHICH cross-family reviewer, among the several the studio roster now offers.
#
# This is NOT a second opinion about who MAY review — that question is
# answered by config.REVIEW_FAMILIES and the driver constructors, and this
# module never contradicts them (see resolved_reviewer, which filters every
# preference through config and falls back to config.cross_family_reviewer).
# It is a preference among EQUALLY LEGAL reviewers, and it exists because the
# default rule does not fit a six-family fleet.
#
# config.cross_family_reviewer returns "the strongest review-capable family
# that is not the implementer's". On the two-model local fleet that is exactly
# right — there is only one other family. On the studio roster it sends EVERY
# task to anthropic: Opus-5.5 reviews all five other workers, at $4/$20 per
# Mtok, through a driver cap of 2. Review becomes the bottleneck and the
# largest line on the bill, while a 1M-context multimodal reviewer sits idle.
#
# So: the architectural work is reviewed by the other hard-tier model, and the
# mechanical work is reviewed by the judge family, which is fast, cheap and
# already reading this project's renders every round.
STUDIO_REVIEW_PREFERENCE = {
    # Architect's own work gets the other frontier reader, not a flash model.
    "Claude-Opus-5.5":  ("openai", "google", "xai", "glm", "deepseek"),
    # GPT-6 work goes to the judge family when it is live (studio-api), and
    # otherwise to GLM on ARC — NOT to anthropic first: on the subscription
    # profile Claude has ONE slot, shared with the operator's own session.
    "GPT-6-Astra":      ("google", "glm", "anthropic", "xai", "deepseek"),
    # Mechanical and content work: Gemini first. It is review-capable on the
    # roster, an order of magnitude cheaper, and has the widest lane (cap 6).
    "Grok-4.7":         ("google", "anthropic", "openai", "glm", "deepseek"),
    "DeepSeek-V4.1-Flash-thinking-max": ("google", "xai", "openai", "anthropic", "glm"),
    "GLM-5.3":          ("google", "xai", "openai", "anthropic", "deepseek"),
    # On the subscription profile `anthropic` has ONE slot, shared with the
    # operator's own Claude Code session, so ARC-served GLM comes before it:
    # sending every review to anthropic would serialise the whole fleet
    # behind the human at the keyboard.
    "GPT-6-Sol":        ("google", "glm", "anthropic", "xai", "deepseek"),
    "GPT-6-Luna":       ("google", "glm", "anthropic", "xai", "deepseek"),
    # The two other subscription seats: each reviewed by a different plan,
    # so one spent usage window never stalls both writing and reviewing.
    "Cursor-Grok-4.7":  ("google", "glm", "openai", "deepseek", "anthropic"),
    "Antigravity-Gemini": ("cursor", "glm", "openai", "deepseek", "anthropic"),
}


def preferred_reviewer(model):
    """The studio's preferred cross-family reviewer for `model`, or None.

    Every candidate is checked against config: it must be a live
    review-capable family AND a different family from the implementer. A
    preference that fails either test is skipped, so this can never widen
    what config allows — only choose within it.
    """
    own = config.MODEL_FAMILY.get(model)
    for fam in STUDIO_REVIEW_PREFERENCE.get(model, ()):
        if fam in config.REVIEW_FAMILIES and fam != own:
            return fam
    return None


# --- the task itself --------------------------------------------------------
@dataclass
class AnimationRequirements:
    """The animation contract for a phase-2 task.

    `sync_target_clip` is the point of this dataclass. An interaction between
    two characters is ONE authored pair of clips whose contact frames line up;
    naming the counterpart here is what lets the gate check the pairing
    instead of trusting that two separately-authored clips happen to match.
    """
    clip_name: str = ""
    sync_target_clip: str = ""
    max_triangle_count: int = 0
    rig_type: str = ""

    def validate(self, where):
        if self.max_triangle_count < 0:
            raise ValueError(f"{where}: max_triangle_count must not be negative")
        if self.sync_target_clip and not self.clip_name:
            raise ValueError(
                f"{where}: sync_target_clip names a counterpart for a clip "
                "this task does not declare; set clip_name")


@dataclass
class GameTask:
    task_id: str
    phase: str
    assigned_worker: str
    prompt: str
    title: str = ""
    target_asset: str = ""
    animation_requirements: AnimationRequirements | None = None
    computer_use_enabled: bool = False
    verify_cmd: str = ""
    reviewer: str = ""
    deps: list = field(default_factory=list)
    files_hint: list = field(default_factory=list)
    feature: str = ""       # a studio_roadmap.json feature id, for the board

    # -- construction --------------------------------------------------------
    @classmethod
    def from_dict(cls, d):
        anim = d.get("animation_requirements")
        return cls(
            task_id=d["task_id"],
            phase=d["phase"],
            assigned_worker=d["assigned_worker"],
            prompt=d.get("prompt", ""),
            title=d.get("title", ""),
            target_asset=d.get("target_asset", ""),
            animation_requirements=(
                AnimationRequirements(**anim) if isinstance(anim, dict) else None),
            computer_use_enabled=bool(d.get("computer_use_enabled", False)),
            verify_cmd=d.get("verify_cmd", ""),
            reviewer=d.get("reviewer", ""),
            deps=list(d.get("deps", [])),
            files_hint=list(d.get("files_hint", [])),
            feature=str(d.get("feature", "") or ""),
        )

    def to_dict(self):
        d = asdict(self)
        if d.get("animation_requirements") is None:
            d.pop("animation_requirements", None)
        return d

    # -- validation ----------------------------------------------------------
    def validate(self):
        where = f"game task {self.task_id!r}"
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,60}", self.task_id or ""):
            raise ValueError(
                f"{where}: task_id must match [a-z0-9][a-z0-9-]{{0,60}} — it "
                "becomes a worktree path and a git ref")
        if self.phase not in PHASES:
            raise ValueError(f"{where}: unknown phase {self.phase!r}; one of {list(PHASES)}")
        model = worker_model(self.assigned_worker)
        if not config.model_may(model, "implementer"):
            raise ValueError(
                f"{where}: worker {self.assigned_worker!r} ({model}) may not "
                f"implement — its roster roles are "
                f"{sorted(config.MODEL_ROLES.get(model, ()))}. "
                "The visual judge scores renders; it does not write code.")
        if not self.prompt.strip():
            raise ValueError(f"{where}: prompt must not be empty")
        # Rule 4, enforced where the task is BORN rather than where it runs.
        # The loader tolerates an empty verify_cmd (it passes trivially); a
        # game task has no excuse for one, and a silent pass here would be a
        # phase promoted on evidence nobody produced.
        if not self.verify_cmd.strip():
            raise ValueError(
                f"{where}: verify_cmd is mandatory (AGENTS.md Rule 4). Give it "
                "a command whose exit code depends on this change being "
                "correct — a Godot headless test, a script check, a mesh "
                "metric assertion.")
        if self.animation_requirements:
            self.animation_requirements.validate(where)
            if self.phase != PHASE_2_3D_ASSET_AND_ANIMATION:
                raise ValueError(
                    f"{where}: animation_requirements belong to "
                    f"{PHASE_2_3D_ASSET_AND_ANIMATION}, not {self.phase}")
        if self.computer_use_enabled and self.assigned_worker != "gpt_6_astra_operator":
            raise ValueError(
                f"{where}: computer_use_enabled is only meaningful for the "
                "gpt_6_astra_operator worker — it is the only one with a "
                "virtual display and a tool loop")
        return self

    # -- compilation ---------------------------------------------------------
    def resolved_reviewer(self):
        """The reviewer family token for this task.

        Explicit `reviewer` wins; otherwise the roster's cross-family pairing.
        Either way the result is checked to be a DIFFERENT family from the
        implementer, because a same-family reviewer is a second pass by the
        same model, not an independent reading (Rule 2).
        """
        model = worker_model(self.assigned_worker)
        reviewer = (self.reviewer or preferred_reviewer(model)
                    or config.cross_family_reviewer(model))
        if reviewer is None:
            raise ValueError(
                f"game task {self.task_id!r}: no cross-family reviewer exists "
                f"for {model} on today's roster")
        if reviewer not in config.REVIEW_FAMILIES:
            raise ValueError(
                f"game task {self.task_id!r}: reviewer {reviewer!r} is not a "
                f"review-capable family today ({sorted(config.REVIEW_FAMILIES)})")
        if (config.MODEL_FAMILY[model] == reviewer
                and not config.ALLOW_SAME_FAMILY_REVIEW):
            raise ValueError(
                f"game task {self.task_id!r}: reviewer {reviewer!r} is "
                f"{model}'s own family — cross-family review is mandatory "
                "(AGENTS.md Rule 2)")
        return reviewer

    def to_task(self):
        """Compile to a taskfile task dict, exactly as load_taskfile expects."""
        self.validate()
        return {
            "id": self.task_id,
            "title": self.title or self.task_id.replace("-", " "),
            "prompt": self.render_prompt(),
            "model": worker_model(self.assigned_worker),
            "reviewer": self.resolved_reviewer(),
            "verify_cmd": self.verify_cmd,
            "deps": list(self.deps),
            "files_hint": list(self.files_hint),
            **({"feature": self.feature} if self.feature else {}),
        }

    def render_prompt(self):
        """The implementer prompt: the task's own words plus its studio context.

        The phase intent and the asset/animation contract are appended rather
        than left implicit, because the implementer harness sees ONLY this
        string — it has no access to the GameTask object, the stage manager or
        the judge's target buckets.
        """
        parts = [self.prompt.strip(), "", "--- studio context ---",
                 f"PHASE: {self.phase}", PHASE_INTENT[self.phase]]
        spec = WORKERS[self.assigned_worker]
        parts += ["", f"YOUR ROLE ({self.assigned_worker}): {spec['brief']}"]
        if self.target_asset:
            parts += ["", f"TARGET ASSET: {self.target_asset}"]
        a = self.animation_requirements
        if a:
            parts += ["", "ANIMATION CONTRACT:",
                      f"  clip:            {a.clip_name or '(none)'}",
                      f"  must sync with:  {a.sync_target_clip or '(none)'}",
                      f"  triangle budget: {a.max_triangle_count or '(unbounded)'}",
                      f"  rig:             {a.rig_type or '(unspecified)'}"]
            if a.sync_target_clip:
                parts.append(
                    "  The two clips are one authored PAIR: their contact "
                    "frames must line up frame-for-frame at the same clip "
                    "time, and the gate checks that, not just that both clips "
                    "exist.")
        if self.phase == PHASE_1_GRAYBOX_PROTOTYPING:
            parts += ["", "GRAYBOX RULE: primitives only. No imported meshes, "
                          "no textures, no material work, no lighting passes. "
                          "Shape and metrics are the deliverable."]
        return "\n".join(parts)


def compile_taskfile(*, name, repo, tasks, pattern="", after=None, goal=""):
    """Build a complete taskfile dict from GameTasks.

    The result is a plain taskfile — `main.py code run` loads it with no
    knowledge that a game produced it, and `--dry-run` validates it under the
    real governance rules.
    """
    seen, compiled = set(), []
    for t in tasks:
        gt = t if isinstance(t, GameTask) else GameTask.from_dict(t)
        if gt.task_id in seen:
            raise ValueError(f"duplicate task_id {gt.task_id!r}")
        seen.add(gt.task_id)
        compiled.append(gt.to_task())
    for t in compiled:
        for d in t["deps"]:
            if d not in seen:
                raise ValueError(f"task {t['id']}: unknown dep {d!r}")
    # The fleet a taskfile was planned under travels WITH it: its models only
    # exist on that roster, so `code run`, check.sh and the dashboard's Run
    # button all need to know which fleet can load it.
    project = {"name": name, "repo": str(repo), "fleet": config.FLEET,
               "tasks": compiled}
    if pattern:
        project["pattern"] = pattern
    if after:
        project["after"] = list(after)
    if goal:
        project["goal"] = goal
    return {"project": project}


def write_taskfile(path, doc):
    """Write a compiled taskfile, creating parent directories."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return path
