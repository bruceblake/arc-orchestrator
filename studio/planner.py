"""The studio planner: the system prompt, and the call that turns a goal into
a taskfile.

This module is the studio's equivalent of `code_tasks.plan_tasks`, and it
follows the same principle: THE PROMPT IS GENERATED, NOT WRITTEN DOWN. The
roster, the tiers, the worker briefs, the phase gates and the concurrency caps
are all read from config and the schema at call time, so the prompt cannot
describe a fleet that no longer exists. Every prompt this repo has hard-coded
a model name into has eventually lied.

What the planner is told, in order:

  1. WHAT IS BEING BUILT — the game brief. Specific enough that a task can be
     judged against it, short enough to sit in every prompt.
  2. WHERE THE PROJECT IS — the phase, and what that phase is allowed to
     produce. A planner that does not know it is in graybox will happily order
     a rigged character.
  3. WHO IS AVAILABLE — today's workers, their briefs, their tiers and what
     each may legally hold, from the roster.
  4. HOW WORK IS GOVERNED — the rules the plan must satisfy or the loader
     will reject it: tier routing, cross-family review, honest verify_cmds,
     dependency shape.
  5. THE OUTPUT CONTRACT — a taskfile, in the exact shape the loader takes.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import config
from studio import openrouter
from studio.schemas.task import (
    GameTask, PHASES, PHASE_INTENT, WORKERS, compile_taskfile,
    implementing_workers, preferred_reviewer, worker_model,
    PHASE_1_GRAYBOX_PROTOTYPING, PHASE_2_3D_ASSET_AND_ANIMATION,
    PHASE_3_ATMOSPHERE_LIGHTING,
    PHASE_4_NETWORKED_QA,
)

GAME_BRIEF = """THE GAME — "prison escape", a 3D multiplayer cops-and-robbers \
game in Godot 4.

Two asymmetric sides on one authoritative server. INMATES win by escaping the \
facility; GUARDS win by keeping the count correct until lights out. The \
tension comes from a routine both sides must visibly obey: roll call, yard \
time, chow hall, lockup, curfew. An inmate who is somewhere they should not be \
during a scheduled event is conspicuous, and that — not a health bar — is the \
core risk.

Systems that define the game:
  * THE ROUTINE. A server-driven state machine (roll call -> yard -> chow -> \
lockup -> curfew) that moves every NPC and constrains where players are \
supposed to be. Everything else keys off it.
  * SUSPICION, not detection. Guards accumulate suspicion about an inmate from \
what they observe — wrong place, wrong time, carrying something, running. It \
decays. It is visible to the inmate as a meter, which is what makes stealth \
playable rather than guesswork.
  * CONTRABAND AND CRAFTING. Components are stolen from the world (a spoon, a \
sheet, a lighter) and combined into tools (shiv, vent key, fake wall paper, \
rope). Carrying contraband raises suspicion; hiding it is a placement problem.
  * CLEARANCE. Doors, gates and wings have clearance levels. A stolen guard \
card is progress; the server decides what it opens, never the client.
  * ROUTES. Vents, laundry, the yard wall, the service gate. Each is a \
multi-step route with its own tell, so an escape is a plan, not a button.

Non-negotiables:
  * The SERVER is authoritative for position, clearance, inventory and the \
routine. Clients predict and reconcile; they never assert state.
  * It must be playable as graybox primitives before a single asset is modelled.
"""


def _roster_prose():
    """Today's workers, from the roster — never a written-down list."""
    lines = []
    for worker in sorted(WORKERS):
        try:
            model = worker_model(worker)
        except ValueError:
            continue                  # no live model for this worker today
        tier = next((t for t, ms in config.IMPLEMENT_TIERS.items() if model in ms), "-")
        roles = sorted(config.MODEL_ROLES[model])
        may_impl = "implementer" in roles
        lines.append(
            f'  "{worker}" -> {model} [tier: {tier}; roles: {", ".join(roles)}]'
            f'{"" if may_impl else "   ** NEVER an implementer **"}\n'
            f"      {WORKERS[worker]['brief']}")
    return "\n".join(lines)


def _capacity_prose():
    """How much can actually run at once, so the plan's width is realistic."""
    rows = []
    for worker in sorted(WORKERS):
        try:
            model = worker_model(worker)
        except ValueError:
            continue
        rows.append(f"  {worker}: at most {config.driver_limit(model)} at once")
    harness = ", ".join(f"{h} pool {config.harness_limit(h)}"
                        for h in sorted(set(config.MODEL_HARNESS.values())))
    return "\n".join(rows) + f"\n  (shared harness ceilings: {harness})"


def _review_prose():
    pairs = []
    for worker in implementing_workers():
        model = worker_model(worker)
        rev = preferred_reviewer(model) or config.cross_family_reviewer(model)
        pairs.append(f"  {worker} ({model}) -> reviewer \"{rev}\"")
    return "\n".join(pairs)


VERIFY_GUIDANCE = """VERIFY COMMANDS (this is where plans usually go wrong).

Every task needs a `verify_cmd` whose EXIT CODE depends on the change being \
correct. It runs as a shell command inside the task's git worktree, with a \
360-second budget. Greps are not gates: `grep -q ClassName file.gd` passes for \
a file that does not compile.

Good gates for this project:
  * GDScript parses:   godot --headless --path . --check-only --script res://x.gd --quit
  * Project imports:   godot --headless --path . --import --quit
  * Logic tests:       godot --headless --path . tests/run_tests.tscn
  * Measured metrics:  godot --headless --path . tools/measure.tscn && \\
                       python3 tools/assert_metrics.py
  * Mesh budget:       python3 -m studio.engine.operators.astra_operator \\
                       verify <asset> --max-tris N
  * Netcode:           python3 -m studio.qa.deepseek_fuzzer <project> --bots 16

A task that writes a system MUST ship a test that fails without it."""


# How the work is run, distilled from the published AI game-dev workflows the
# operator pointed this studio at (2026-09): an orchestrator that hands out
# work and checks what comes back; one task, one fresh session, easy to test;
# a "gauntlet" where every piece is measured against numbers AND judged by an
# independent critic before it is accepted; assets built one at a time in a
# workbench and approved before anything is assembled; and a scripted playtest
# that both measures and looks. The planner is told this in plain words
# because every one of those is a planning decision.
METHOD = """HOW THIS STUDIO WORKS (plan to it):

- ONE TASK = ONE FRESH SESSION, EASY TO TEST. If you cannot say in one sentence \
how the gate proves a task worked, split the task.
- THE GAUNTLET. Every task is measured (its verify_cmd: numbers, not greps), \
then read by an independent critic from a different model family, then \
PR-reviewed, and only then merged; a failure at any step returns it to the \
implementer with the failure as feedback. Plan tasks whose gates can FAIL.
- BUILD PIECES, THEN ASSEMBLE. Assets and systems are built one at a time and \
proved in isolation before a task wires them together. Never plan "build the \
level" — plan the kit pieces, then the assembly.
- LABS. Anything that moves (a controller, an animation, a creature, an AI) \
gets a small lab scene (scenes/labs/<thing>.tscn) where it can be exercised \
alone; its test drives the lab.
- BEST OF N for hero pieces. For an asset or system the whole game rests on, \
plan two or three independent proposals in parallel and a separate task that \
compares them and keeps one (the others are deleted in that task).
- THE PLAYTEST. tools/playtest.gd drives the game along a real route with \
StudioPlaytest (tools/studio_playtest.gd): named checks with value and \
expected, plus screenshots along the way, written to studio_playtest.json. \
The phase gate requires it to pass from phase 1 on, and the screenshots go \
to the visual judge. When a task adds something a player does, it extends \
the playtest to do it.
- ART DIRECTION. studio_art_direction.md and the colour bible \
(studio_target.json bucket_b.palette_hex) are handed to every asset task; \
materials use palette colours, which a program checks on the renders.
- CHARACTERS AND CREATURES start from a rigged base mesh built in logically \
separated low-poly parts (never one fused mesh) and are adapted, rigged and \
animated; animations are directed from reference clips, frame by frame. Do \
not plan modelling an organic character from nothing.
- COMPLEX FEATURES get a short design note (docs/design/<feature>.md) as their \
own first task: research the genre's conventions, then the rules.
- A feature from studio_roadmap.json is named on the task as "feature".
- A REFERENCE IMAGE FOR EVERY VISUAL FEATURE. A visual task names the target \
image(s) it must match (studio_refs/<feature>.png, or add a task that produces \
them first). The one feature built without a target in the reference build \
came out worst.
- PARTITION BIG SCENES. Environment work is planned per region (cell block, \
yard, mess hall, city block...), one sub-task each, then an assembly task: \
one agent across a whole map produces sporadic quality.
- EVERY CHANGE IS SEEN. After the gate, the orchestrator captures screenshots, \
a flythrough and the playtest as video, and compares them with the branch \
point; reviewers see them. Plan so each task's result is visible from the \
fixed cameras or the playtest route, and extend the playtest when a task adds \
something a player does.
- ANIMATION IS JUDGED ON VIDEO: paired clips in sync, weapons stay attached, no \
pop or teleport at clip start/end, hit reactions exist, attacks can be \
interrupted. Plan the lab scene that shows each of these.
"""


def _phase_prose(phase):
    body = [f"CURRENT PHASE: {phase}", PHASE_INTENT[phase], ""]
    if phase == PHASE_1_GRAYBOX_PROTOTYPING:
        body.append(
            "HARD RULE for this phase: primitives only. No .gltf/.glb/.fbx/"
            ".obj/.blend anywhere in the project — the phase gate scans for "
            "them and fails. Do not plan modelling, texturing or lighting "
            "tasks; they belong to phases 2 and 3 and planning them here "
            "wastes a whole round.")
    if phase == PHASE_2_3D_ASSET_AND_ANIMATION:
        body.append(
            "Every asset task must carry animation_requirements with a "
            "max_triangle_count, and every exported asset must be measured "
            "(the gate reads mesh reports). A synchronised interaction is ONE "
            "task producing BOTH clips, never two tasks producing one each — "
            "two independently authored clips do not line up.")
    if phase == PHASE_3_ATMOSPHERE_LIGHTING:
        body.append(
            "Lighting is built as PRESETS the game switches between (day, "
            "night, lockdown, alarm, blackout), each a scene resource with its "
            "settings exposed, not one hand-tuned lighting pass. The gate "
            "enforces a frame-rate floor and a cap on shadow-casting lights "
            "(tools/perf.gd): every shadow caster re-renders the scene from "
            "its own point of view, so plan lamps that light without "
            "shadowing wherever the shadow is not doing gameplay work.")
    if phase == PHASE_4_NETWORKED_QA:
        body.append(
            "The server must already expose studio_protocol.json (transport "
            "websocket/tcp/udp) or the swarm cannot fuzz it. If it does not, "
            "the FIRST task of this phase is to publish it.")
    return "\n".join(body)


def system_prompt(phase, repo):
    """The full studio planner prompt for a phase."""
    return f"""You are the system architect and planner for an autonomous game \
studio. You decompose a goal into a small graph of tasks that other models \
will implement, and you are the only model allowed to plan.

{GAME_BRIEF}

{_phase_prose(phase)}

{METHOD}

THE WORKERS AVAILABLE TODAY (roster-derived; use no other name):
{_roster_prose()}

HOW MUCH CAN RUN AT ONCE:
{_capacity_prose()}

GOVERNANCE — a plan that breaks any of these is REJECTED by the loader before \
a single model runs:
  1. `assigned_worker` must be one of the worker ids above, and must be one \
that may implement. The visual judge never implements.
  2. Every task is reviewed by a model from a DIFFERENT family. Leave \
`reviewer` unset and the correct cross-family reviewer is filled in for you:
{_review_prose()}
  3. Every task needs an honest `verify_cmd` (see below). There is no \
exception, including for documentation.
  4. Tasks with no `deps` start in parallel. Add a dep ONLY when one task \
genuinely needs another's merged output — a dep you added "to be safe" \
serialises the fleet and costs hours.
  5. Keep tasks small: under ~30 minutes of work for one agent, one coherent \
concern each. Split anything bigger.
  6. The repository is {repo}. Tasks never run git; the orchestrator commits, \
opens the pull request and merges.

{VERIFY_GUIDANCE}

OUTPUT: exactly one JSON object, no prose, no code fence:

{{
  "pattern": "<fanout|chain|diamond|single|router|hierarchical>",
  "tasks": [
    {{
      "task_id": "lowercase-hyphenated",
      "title": "short imperative title",
      "phase": "{phase}",
      "assigned_worker": "<worker id>",
      "prompt": "<what to build, precisely. Name files, scenes, node paths, \
signals and the acceptance condition. This is the ONLY thing the implementer \
sees.>",
      "verify_cmd": "<a real gate>",
      "deps": [],
      "target_asset": "<optional path>",
      "animation_requirements": {{"clip_name": "...", "sync_target_clip": \
"...", "max_triangle_count": 0, "rig_type": "..."}},
      "computer_use_enabled": false,
      "feature": "<optional studio_roadmap.json feature id>"
    }}
  ]
}}

Two to six tasks. Prefer breadth: independent tasks that can run at once beat \
a chain of dependent ones."""


async def _plan_via_harness(model, phase, repo, goal, project):
    """Plan through the model's own CLI harness (the subscription profile).

    A subscription model has no API route at all — it is reached only through
    its CLI — so the planner runs as an ordinary planner-role driver, exactly
    as `main.py code plan` runs GLM-5.3. That also gives the planning call a
    transcript and a harness_runs row (Rule 7) and puts it under the harness
    cap, which matters here: on Claude Pro the cap is ONE session, shared with
    the operator's own Claude Code.

    It runs in a scratch directory, not the game repo: a planner reads the
    goal and writes a plan, and has no business editing the code it plans.
    """
    import tempfile
    import drivers
    driver = drivers.driver_for(model, "planner")
    prompt = (system_prompt(phase, repo)
              + "\n\n--- THE GOAL ---\n" + goal
              + "\n\nReply with the JSON object only. Do not create or edit any "
                "files; the plan is your whole answer.")
    with tempfile.TemporaryDirectory(prefix="studio-plan-") as scratch:
        res = await driver.run(prompt, scratch, task_id=f"plan-{project}")
    if res.exit_code != 0 and not (res.text or "").strip():
        raise ValueError(f"the planner ({model}) exited {res.exit_code} with no output; "
                         f"transcript: {res.transcript_path}")
    usage = {"prompt_tokens": res.prompt_tokens,
             "completion_tokens": res.completion_tokens,
             "cost_usd": 0.0}          # covered by the subscription
    return _full_reply(res), usage


def _full_reply(res):
    """The planner's WHOLE final answer, read back from its transcript.

    DriverResult.text is cut to a head plus a 3000-char tail (sized for
    review verdicts), and a real plan is far longer: a 10-task phase plan
    measured 34,396 chars, and the cut JSON failed to parse after a
    successful planning session. The transcript holds the uncut final
    record; fall back to res.text when it cannot be read.
    """
    text = res.text or ""
    path = getattr(res, "transcript_path", "") or ""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, ValueError):
        return text
    final = None
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "result" and isinstance(obj.get("result"), str):
            final = obj["result"]                      # claude / cursor / reasonix
        elif obj.get("event") == "result" and isinstance(obj.get("result"), dict) \
                and isinstance(obj["result"].get("response"), str):
            final = obj["result"]["response"]          # antigravity
        else:
            item = obj.get("item") if isinstance(obj.get("item"), dict) else {}
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                final = item["text"]                   # codex
    return final if final and len(final) > len(text) else text


def plan(goal, repo, *, phase, project="prison-escape", model=None,
         out_path=None, pattern=""):
    """Ask the planner for a taskfile, validate it, and write it.

    Validation is the real loader: `compile_taskfile` produces an ordinary
    taskfile and `code_tasks.load_taskfile` checks it under the governance
    rules. A plan that does not survive that is rejected here, with the
    loader's own message, rather than failing hours later mid-run.
    """
    model = model or config.PLANNER_MODEL
    if not config.model_may(model, "planner"):
        raise ValueError(
            f"{model} may not plan on today's roster "
            f"(fleet={config.FLEET}); planner is {config.PLANNER_MODEL}")
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r}")
    if config.STUDIO_API:
        msg, usage = openrouter.chat(
            model,
            [{"role": "system", "content": system_prompt(phase, repo)},
             {"role": "user", "content": goal}],
            temperature=0.3, max_tokens=16000,
            response_format={"type": "json_object"},
            task=f"plan:{project}:{phase}")
        text = msg.content or ""
    else:
        text, usage = asyncio.run(_plan_via_harness(model, phase, repo, goal,
                                                    project))
    doc = openrouter.parse_json_object(text)
    if not doc or not isinstance(doc.get("tasks"), list) or not doc["tasks"]:
        raise ValueError(
            "the planner returned no usable task list:\n" + text[:2000])
    tasks = []
    for raw in doc["tasks"]:
        raw.setdefault("phase", phase)
        tasks.append(GameTask.from_dict(raw))
    taskfile = compile_taskfile(
        name=project, repo=repo, tasks=tasks,
        pattern=pattern or str(doc.get("pattern", "")), goal=goal)
    out_path = Path(out_path or (Path(config.TASKS_DIR) / f"{project}-{phase.lower()}.json"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(taskfile, indent=2) + "\n", encoding="utf-8")
    return {"path": str(out_path), "taskfile": taskfile,
            "tasks": len(taskfile["project"]["tasks"]),
            "cost_usd": usage.get("cost_usd", 0.0)}
