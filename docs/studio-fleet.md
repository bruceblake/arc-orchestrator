# The studio fleet — autonomous 3D game development

`ARC_FLEET=studio` is a second roster and a second workload on the same
orchestrator. It exists to build a 3D multiplayer prison-escape game in
Godot 4, and it changes exactly two things: **which models are live**, and
**which workload modules are available**. It changes nothing about how work is
governed. A studio task allocates a worktree, runs a `verify_cmd`, is reviewed
by a different model family, opens a pull request and merges — AGENTS.md
Rules 1–9, byte for byte.

> **It costs real money.** Every local model is ARC-served and effectively
> free. Every studio model is billed to `OPENROUTER_API_KEY`. `GPT-6-Astra` is
> about fifty times GLM-5.3 per token. Read [Budget](#budget) before a long
> run.

---

## 1. The three profiles

| | `local` (default) | `studio` | `studio-api` |
|---|---|---|---|
| Frontier models | — | Claude Code (Opus 5.5), Codex CLI (**GPT-6 Sol, high effort**) | Opus 5.5, GPT-6 Sol, Grok-4.7, Gemini-3.8-Flash |
| Reached via | — | **the operator's own subscriptions** | OpenRouter, per token |
| Always present | GLM-5.3 + DeepSeek on ARC | same | same |
| Families | 2 | 4 | 6 |
| Planner | GLM-5.3 | Claude-Opus-5.5 | Claude-Opus-5.5 |
| PR reviewers | 1 (thin, Rule 5) | 2 | 2 |
| Concurrency | full | **1–2 per CLI** | full |
| Cost | free | free (plan quota) | metered |

`ARC_FLEET` is read once at import in `config.py` and is **fatal on a typo**,
unlike the tuning knobs around it. A mistyped tuning value costs you a
default; a mistyped fleet name would silently run the wrong roster while you
believed something else was building your game.

### Why two studio profiles

`studio` runs the frontier roles on **subscription CLIs** — Claude Code on a
Claude plan and `codex exec` on a ChatGPT plan. No per-token billing at all.
Both stream JSON events on stdout in headless mode, so the existing `Driver`
pump, stall clock, live transcript and `parse_transcript` work unmodified;
`drivers.ClaudeCodeDriver` and `CodexDriver` are thin argv wrappers, not a new
execution path.

Verified live on 2026-09-22, not assumed:

- **Claude Code** on Claude Pro runs `claude-opus-5-5` with
  `apiKeySource: "none"` — the plan, not an API key.
- **Codex** on the ChatGPT plan runs **`gpt-6-astra` by default**, and the plan
  also serves `gpt-6-sol` and `gpt-6-luna`. The studio runs **`GPT-6-Sol` at
  `high` reasoning effort** (operator decision); `codex exec -m` is derived
  from the roster name and the effort is passed as
  `-c model_reasoning_effort="high"`, which the session record confirms. It is
  passed explicitly because Sol's own default is `medium`.
- **Cursor Agent CLI** (`agent --print`) on the Cursor subscription runs
  **Grok 4.7** (`grok-4.7-high`). The roster name is `Cursor-Grok-4.7`, not
  OpenRouter's `Grok-4.7`. It is a separate plan, so a spent Codex or Claude
  window can keep implementing without waiting and without spending the other
  frontier plan first.
- **Antigravity CLI** (`agy --print`) on the Google account is the next seat
  after Cursor. Install is `~/.local/bin/agy`. One interactive `agy` sign-in
  caches the account; `agy models` lists slugs only after that. An empty
  `ARC_AGY_MODEL` leaves the account's default model. The old Gemini CLI is
  still not a roster row. Gemini remains available per token on `studio-api`
  as the judge. The visual judge on this profile rotates between Claude and
  GPT-6, both of which read images.

A consumer plan is metered on rolling windows — a live run shows
`five_hour: {utilization: 0.4}` and, when exhausted,
`overageStatus: "rejected"` with no fallback. **The fleet does not cap the
plan seats locally** (operator directive 2026-09-22): `claude` and `codex`
run up to `ARC_SUBSCRIPTION_SESSION_CAP` (default 32) concurrent sessions,
and the plan's usage window is the real limit. This was once 1 for Claude and
2 for Codex; set `ARC_SUBSCRIPTION_SESSION_CAP=1` to go back to a single seat
when you want your own interactive session to have the plan to itself.

**When a window runs out, the attempt moves to a free harness, and waits
only if every seat is blocked.** `drivers.Driver.run` recognises the plans'
refusals ("usage limit
reached", "You've hit your limit · resets 3pm", Codex's
`usage_limit_reached`, a rejected `rate_limit_event`), marks that harness
blocked, and reruns the same prompt on the next free seat: Cursor's
`agent` CLI (`Cursor-Grok-4.7`) first, then Antigravity (`agy`), then Claude,
then Codex, then OpenCode Zen, then a billed API model. Implementation, gate
review and PR review all move. A planner does not. A review swap skips the
implementer's family, so the cross-family gate still holds. A substitute starts fresh in the same
worktree; a Codex session cannot be resumed on `agent` or `agy`. `ARC_USAGE_SWAP=0` skips that and parks until the
reset the refusal names, plus
`ARC_USAGE_LIMIT_MARGIN`. A refusal that names no time is retried every
`ARC_USAGE_LIMIT_POLL` seconds. The wait does not count as an attempt, costs
no fix round or escalation, and every other task on the same harness takes
the same substitute rather than spending its own refusal. The dashboard sees
`driver.usage_limit` once and `driver.usage_wait` every five minutes while
parked. One driver run waits at most `ARC_USAGE_LIMIT_MAX_WAIT` (8 days, so a
weekly window fits) before the attempt fails normally.

`studio-api` is the same roles through OpenRouter: billed per token, but with
real parallelism and no plan windows. Use it when the work outgrows the plans.

### OpenCode Zen free models (parallel zero-cost pools)

With `ARC_ZEN_FREE=1` (opt-in; off by default because most free slugs may train on what they are sent, and because they add eleven medium-tier stages to the escalation path), the roster admits
every slug returned by `https://opencode.ai/zen/v1/models` whose id contains
`free` or is `big-pickle`. Each slug is a **separate family** with its own
`ARC_ZEN_MODEL_CAP` (default 2) so independent partner rate limits do not
block each other — the intended “max free throughput” pattern is one concurrent
task per slug, then rotate when a pool returns `FreeUsageLimitError`.

Roster names are `Zen-<Slug>` (for example `Zen-Mimo-V2.6-Flash-Free`); the
harness is `opencode` with provider alias `opencode/<slug>`. They implement
only: pre-merge review and PR review stay on GLM-5.3 / DeepSeek / the studio
frontier models. On a spent subscription window, `ARC_USAGE_SWAP` tries Cursor,
Antigravity, Claude, and Codex first, then any unblocked `Zen-*` implementer,
then billed OpenRouter rows.

Re-sync `config.ZEN_OPENCODE_SLUGS` when Zen rotates its free lineup (the live
list is also visible as `opencode models | grep opencode/`).

### Subscription models have no provider alias

`EXTERNAL_MODELS` means *on the roster but not served by the ARC API* — which
is true of both profiles, and is what stops `live_roster` deferring a row
merely because ARC's availability snapshot has never heard of it.
`MODEL_HARNESS_ALIAS` is narrower: it exists only under `studio-api`. A
subscription model is reached by its own CLI and has **no** alias, and a test
pins that, because an alias there would quietly route a plan-backed model
through a billed account.

Each driver also blanks the matching API key in `extra_env` — with
`ANTHROPIC_API_KEY` set, Claude Code bills the API account instead of the
plan, which is the opposite of why the harness exists.

### Roster order is load-bearing

`config.ROSTER` is written weakest → strongest, and `_STRONGEST_FIRST`
reverses within a tier. The **last hard-tier row wins** the planner role, the
head of `REVIEW_FAMILIES`, and the final escalation stage. `Claude-Opus-5.5`
is last on purpose.

### One fix this required

`config.IMPLEMENT_TIERS` and the default escalation path used to select on
TIER alone. `Gemini-3.8-Flash` is medium-tier and holds **no implementer
role** — it is the judge. Without a role filter it would have been offered to
the planner as a medium implementer and placed in the escalation path, and
the driver constructor would then have raised `ValueError` on every attempt to
use it. That is a hand-kept list disagreeing with the drivers' own role rules,
which is precisely the drift AGENTS.md Rule 2 forbids. Both now filter on
`"implementer" in roles`. The local fleet is unaffected — both its models
implement.

---

## 2. Workers

A **worker** is a studio role bound to a roster model
(`studio/schemas/task.py`). The binding is by model name only; every
capability question is answered by the roster at call time.

| Worker | Model | Tier | Does |
|---|---|---|---|
| `opus_architect` | Claude-Opus-5.5 | hard | Authoritative netcode, the prison routine state machine, inventory, contraband crafting, clearance |
| `gpt_6_astra_operator` | GPT-6-Astra | hard | Modelling, retopology, rigging, weight transfer, synchronised animation, editor computer-use |
| `grok_feature_driver` | Grok-4.7 | medium | Player controllers, HUD and UI data-binding |
| `gemini_visual_judge` | Gemini-3.8-Flash | — | **Never implements.** Scores renders, audits space |
| `deepseek_qa_swarm` | DeepSeek-V4.1-Flash-thinking-max | medium | Headless bot swarm, packet fuzzing, desync hunting |
| `glm_content_swarm` | GLM-5.3 | hard | Spawn tables, dialogue trees, announcements, achievements |

### Review pairing

`config.cross_family_reviewer` returns *the strongest review-capable family
that is not the implementer's*. On the two-family local fleet that is exactly
right. On six families it sends **every** task to `anthropic`: Opus-5.5
reviewing all five other workers at $4/$20 per Mtok through a driver cap of 2,
while a cheap 1M-context multimodal reviewer sits idle.

So `studio/schemas/task.py` carries `STUDIO_REVIEW_PREFERENCE`: a preference
**among equally legal reviewers**, every candidate filtered through
`config.REVIEW_FAMILIES` and checked to be a different family, falling back to
`config.cross_family_reviewer`. It can never widen what config allows — only
choose within it. Architectural work goes to the other frontier reader;
mechanical and content work goes to the judge family.

---

## 3. The five phases

`studio/engine/stage_manager.py`. Phases never skip and never run in parallel.

| Phase | Produces | Gate checks |
|---|---|---|
| `PHASE_0_TARGET_GROUNDING` | `studio_target.json`: Bucket A (exact geometry) and Bucket B (atmosphere) | both buckets present; Bucket A contains **numbers** |
| `PHASE_1_GRAYBOX_PROTOTYPING` | Primitive block-out, movement, measured dimensions | **no mesh files anywhere**; measured metrics match Bucket A within 5%; judge score |
| `PHASE_2_3D_ASSET_AND_ANIMATION` | Modelled, rigged, animated assets | mesh reports exist; triangle budgets respected; no non-manifold edges; judge score |
| `PHASE_3_ATMOSPHERE_LIGHTING` | Lighting, day/night, searchlights, alarm states | judge score; **no high-severity artifacts** |
| `PHASE_4_NETWORKED_QA` | A server that survives the swarm | fuzz report: zero authority violations, zero desyncs, zero crashes |

**Deterministic evidence first, judged evidence second.** Bucket A is compared
by `stage_manager.check_bucket_a` against `studio_metrics.json` with no model
involved, because a measurement a model *reports* is a claim and a measurement
a program *computes* is a fact. A Bucket A target with no measurement is a
**failure**, not a pass — an unmeasured dimension is not a met dimension.

Promotion resets the render baseline (`studio/memory/compactor.py`): once the
phase changes, the previous phase's frames stop being a fair comparison.
`--force` promotes over a failing gate and records that in the history
forever, because the next person asking how a phase-1 defect reached phase 3
deserves an answer.

---

## 4. Cameras and the blind judge

`studio/evaluation/`.

* **Rounds 1–2** render four fixed anchors (isometric overview, cell corridor,
  guard station, perimeter fence), so consecutive verdicts differ because the
  *game* changed, not the viewpoint.
* **From round `ARC_STUDIO_ADVERSARIAL_ROUND`** (default 3) the set gains
  proc-gen adversarial angles — inside a vent bend, under the tower, behind a
  door, the unvisited side of a wall. Give a model the same four viewpoints
  every round and it optimises for those four viewpoints; that is visual
  overfitting, and a fixed rig cannot see it by construction.
* Cameras are **deterministic** given `(project, round)`. An adversarial angle
  you cannot reproduce is an angle you cannot re-check after a fix.

The judge is **blind** (no commit history, no task prompt, no previous
verdict — `build_messages` is the only thing that decides what it sees) and
**rotated** (a different model each round, so disagreement between rounds is
information).

A judge that errors, or returns no parseable verdict, is **`crashed`, not
failing**. The caller re-runs the judge. Scoring a crash as zero would send
the build back to fix defects nobody named — AGENTS.md Rule 2's hardest-won
lesson, carried into the visual loop.

`studio/evaluation/arbitrator.py` stops the loop when judges **oscillate**:
round 1 says brighten, round 2 says darken, round 3 says brighten. Three
rounds is the smallest window that distinguishes an argument (A, B, A) from a
change of mind (A, B). It halts and names the conflict rather than picking a
winner — a machine that adjudicates between two judges has just become a third
judge with no better information.

---

## 5. The 3D operator

### How frontier models actually make 3D assets

Published accounts of GPT-6 Astra's Blender workflow (September 2026) agree on
the method, and it is **not** native 3D generation: the model "writes real
Blender Python to create objects, modifiers, materials, and cameras", then
"renders frames to inspect the outcome, then revises its own script wherever
the render diverges from the brief." The render-and-look step is what turns a
correct script into a good asset. An operator that only runs scripts is
modelling blind.

They agree on the limits too: "strong results on regular, geometric objects
and weaker results on organic forms"; a rigged cheetah showed "rubbery
deformation"; the guidance is to start with rigid objects before characters.
No standardised Blender benchmark or published success rate exists yet.

For this game that is a useful split rather than a problem. The prison is
overwhelmingly hard-surface — walls, bars, bunks, vents, towers, doors — which
is exactly where the method is strong. Characters should start from a supplied
rigged base mesh that the operator adapts, retargets and animates.

### The tools

`studio/engine/operators/astra_operator.py` gives the operator its tools, and
the system prompt makes the loop mandatory — BUILD, RENDER, COMPARE, REVISE ONE
THING, MEASURE:

1. `execute_blender_script` — **build**: reproducible, diffable, reviewable.
2. `render_preview` — **look**: renders the `.blend` to an image and puts it in
   front of the model. With no camera in the scene it creates a three-quarter
   camera framing every mesh, the angle that shows silhouette, depth and
   proportion at once. It checks the image was actually written — Blender can
   exit 0 having produced nothing, and "here is your render" with no render is
   confident, empty feedback.
3. `verify_mesh_metrics` — **measure**: triangles, non-manifold edges, loose
   vertices, n-gons, UVs, materials, bone hierarchy. The phase-2 gate reads
   these reports, so an unmeasured asset cannot pass.
4. `capture_screen` — **observed**, only when a script cannot tell it what it
   needs.
5. `mouse_click` / `keyboard_input` — **manual**, last resort, and the prompt
   asks it to justify every click.

"Revise one thing at a time" is deliberate: change several at once and you can
no longer tell which one helped.

A clicked action is invisible to review, impossible to replay and
unattributable when it breaks. Computer use is here because some editor
operations genuinely have no other path.

The UV-overlap metric is an **approximation** (bounding-box grid) and the
report says so in `uv_overlap_method`. A mesh report that overstates its own
certainty ends an argument that should have continued.

### The same toolkit as a shell command

`run()` above is the API-profile tool loop. On the **subscription profile**
the same four capabilities are reached as a shell command, because Codex
and Claude Code both have shell access and no billed tool loop
is involved:

```bash
python -m studio.engine.operators.astra_operator verify <asset> --max-tris N
python -m studio.engine.operators.astra_operator blender <script.py>
python -m studio.engine.operators.astra_operator render <file.blend>   # prints the PNG path first
python -m studio.engine.operators.astra_operator shot
python -m studio.engine.operators.astra_operator click <x> <y>
python -m studio.engine.operators.astra_operator keys ctrl+s
python -m studio.engine.operators.astra_operator doctor
```

`verify` is built to be a task's `verify_cmd` directly: it exits non-zero when
an asset breaks its triangle budget or is non-manifold, so the gate's exit
code depends on the asset being correct (Rule 4).

> **Same posture as AGENTS.md Rule 6b:** `execute_blender_script` runs
> model-authored Python with the operator's privileges, exactly as
> `code_tasks.gate` runs a model-authored shell string. Accepted on a trusted
> machine. Do not expose it over a network.

---

## 6. The fuzz swarm

`studio/qa/deepseek_fuzzer.py` asks one question: **is the server
authoritative?** A bot asserts an illegal state — out of bounds, NaN vector,
speed hack, a door above its clearance — and the server either rejects it or
it does not. Tick latency and desync counts are secondary; a server that takes
a client's word about position is a chat relay with physics.

It reads `studio_protocol.json` from the game repo (schema in the module's
`PROTOCOL_DOC`). `tcp`, `udp` and `websocket` are implemented with the standard
library. Godot's default `ENetMultiplayerPeer` is **not** — ENet has its own
reliability framing a stdlib client cannot speak, so expose
`WebSocketMultiplayerPeer` for QA (which a browser client would want anyway).
The loader says this rather than pretending to connect.

---

## 7. Rendering on this machine

`godot --headless` **has no renderer**. It is right for syntax checks, imports,
exports and logic tests, and useless for the visual judge.

There is also no `--screenshot` flag: a render is a *scene* that positions a
camera, waits for the frame to draw, and saves the viewport itself.
`studio/engine/godot.py` writes that harness into the game project
(`ensure_render_harness`) and keeps it current — it is studio infrastructure,
not game code.

On WSL2 with WSLg you already have a GPU-backed display at `:0`
(`/dev/dxg` passthrough), so renders are hardware-accelerated. Otherwise run
Xvfb and point `ARC_STUDIO_DISPLAY` at it for deterministic software
rendering.

---

## 8. Budget

The orchestrator's retry budgets are generous on purpose — `config.py` says
tokens are not the scarce resource. **That is true on ARC and false here.**
Sixteen fix rounds across five escalation tiers is up to eighty implementation
attempts, and the top tier bills at $50/Mtok completion.

`studio/budget.py` meters every direct studio call (judge, Astra, planner) and
checks the ceiling **before** the call, because a budget you discover you
crossed is a bill. `ARC_STUDIO_BUDGET_USD=0` disables it. Harness-side spend
is metered by the existing usage page through the same `config.cost_of`.

    .venv/bin/python main.py studio budget

Published OpenRouter rates, read 2026-09-22, USD per million tokens:

| Model | Prompt | Completion |
|---|---|---|
| GPT-6-Astra | 10.00 | 50.00 |
| **GPT-6-Sol** (default) | **2.00** | **10.00** |
| GPT-6-Luna | 0.10 | 0.50 |
| Claude-Opus-5.5 | 4.00 | 20.00 |
| Grok-4.7 | 1.60 | 4.80 |
| Gemini-3.8-Flash | 0.75 | 3.75 |

### Why Sol, not Astra

The GPT-6 line is tiered 100x apart at the extremes for the **same** 1.05M
context, the same image input and the same tool support. Almost everything
this workload calls "3D work" is writing Blender Python and engine glue —
ordinary strong-coding-model work — and the pipeline is designed to RETRY, so
a 5x rate applies to every fix round, not just the successful one.

So on **`studio-api`** `ARC_STUDIO_OPENAI_MODEL` defaults to `GPT-6-Sol`. Move
to Astra deliberately, for work that has actually stalled, and compare whether
it converged in fewer rounds. That comparison is what `main.py code bench`
exists for; paying 5x on an untested assumption is not a measurement.

On **`studio`** the default is also `GPT-6-Sol`, at `high` reasoning effort
(`ARC_CODEX_REASONING`), by operator decision: Sol at high effort in place of
Astra, trading some depth for a slower draw on the plan's rolling allowance.
`ARC_STUDIO_OPENAI_MODEL=GPT-6-Astra` switches back.

---

## 9. Getting started

Subscription profile (no credits needed):

```bash
# 0. one-time: log the CLIs in to your own plans
claude          # already logged in if you use Claude Code
codex login     # sign in with your ChatGPT account (see the WSL note below)
```

**On WSL**, `codex login` opens a browser on the Windows side that has to call
back to `localhost:1455` inside WSL, and that handoff can fail. A failed
attempt also keeps running and holds the port, so every retry fails too
(`default login callback port is unavailable` in `~/.codex/log`). If Codex is
already logged in on Windows, share that login instead:

```bash
ln -s /mnt/c/Users/<you>/.codex/auth.json ~/.codex/auth.json
codex login status          # -> Logged in using ChatGPT
```

Symlink it; don't copy it. Two copies of one OAuth refresh token can log each
other out when either one refreshes.

```bash
# 1. API profile only: register the models with opencode (backs the file up)
ARC_FLEET=studio-api .venv/bin/python main.py studio provision --write

# 2. what works on this machine
ARC_FLEET=studio .venv/bin/python main.py studio doctor

# 3. a Godot graybox starter
.venv/bin/python main.py studio scaffold ~/repos/prison-escape

# 4. phase 0: edit studio_target.json, then check it
.venv/bin/python main.py studio gate prison-escape ~/repos/prison-escape

# 5. plan the phase, dry-run it, run it
ARC_FLEET=studio .venv/bin/python main.py studio plan \
    "block out the cell wing and the yard" ~/repos/prison-escape
.venv/bin/python main.py code run ~/tasks/prison-escape-phase_1_graybox_prototyping.json --dry-run
ARC_FLEET=studio .venv/bin/python main.py code run ~/tasks/prison-escape-phase_1_graybox_prototyping.json

# 6. render, judge, promote
ARC_FLEET=studio .venv/bin/python main.py studio render prison-escape ~/repos/prison-escape
ARC_FLEET=studio .venv/bin/python main.py studio judge  prison-escape ~/repos/prison-escape
ARC_FLEET=studio .venv/bin/python main.py studio promote prison-escape ~/repos/prison-escape
```

External toolchain:

```bash
sudo pacman -Syu blender godot mesa ffmpeg xorg-server-xvfb xdotool
```

The `-Syu` matters. A plain `pacman -S` against a package database that is
days old asks the mirrors for package versions they have already deleted, and
every download fails with a 404 (measured here: a 14-day-old database, and
`blender-5.2.1-2` returning HTTP 404 on the first mirror). Arch does not
support partial upgrades, so refresh and upgrade in one step. `mesa` is listed
explicitly because rendering on WSLg needs its d3d12 driver, and it is not
installed by default.

Godot's `--headless` mode (script checks, imports, the raycast measurer,
logic tests) needs **none** of this: the official self-contained Linux build
runs on a bare WSL Arch install, and is a reasonable stopgap installed to
`~/.local/opt/godot` without sudo. RENDERING does: a minimal Arch install has
no `libX11`, `libxkbcommon`, `fontconfig` or GL at all, so Godot cannot reach
the WSLg display even though its socket exists. Installing the `godot` and
`blender` packages pulls those libraries in as dependencies.

Subscription CLI: `npm install -g @openai/codex`.

---

## 9b. The method, and where each piece of it lives

The studio's working method is taken from published AI game-development
workflows (September 2026: a week-long space-game build with 100+ agents, two
GPT-6 Astra asset and animation workflows, and a "games that don't suck"
framework). Each practice below is enforced by code, not left to a prompt:

| Practice from the workflows | Where it is enforced |
|---|---|
| An orchestrator hands out work and checks what comes back; it does not build | the planner (`studio/planner.py`) plus the governed pipeline |
| One task = one fresh session, easy to test | planner method block; every task needs a `verify_cmd` that can fail (Rule 4) |
| **The gauntlet**: every piece measured against numbers *and* judged by an independent critic before it is accepted | `verify_cmd` gate → cross-family review (Rule 2) → PR review → merge, with the bounded fix loop; the Studio board shows each task's trail |
| Show the AI what you mean: references, art direction | phase-0 gate requires `studio_art_direction.md` |
| The **colour bible**: one palette every model must use | `bucket_b.palette_hex`; `studio/evaluation/palette.py` checks renders (phase 3 gate) |
| Test two ways: a script that **plays** the game and prints pass/fail numbers, and **screenshots** another agent looks at | `tools/playtest.gd` + `StudioPlaytest`; gated from phase 1; `studio playtest` archives the screenshots for the judge |
| Build assets one at a time in a live **workbench**, approve them, *then* assemble | Studio → Workbench; `studio approve`; phase-2 gate requires every measured asset approved |
| Labs / playgrounds for anything that moves | planner method block (`scenes/labs/`) |
| Several proposals for hero pieces, keep the best | planner method block (best-of-N) |
| Characters from low-poly parts, rigged and animated from reference clips | planner method block; the operator's prompt refuses to model organic characters from nothing |
| Lighting as presets (day / night / emergency / blackout) | phase-3 planner guidance |
| Shadows halved one build's frame rate | phase-3 perf gate (`tools/perf.gd`): fps floor + shadow-caster cap |
| The last check is a human | promotions are manual (`studio promote`); optional manual PR gate below |

### Scripted playtest contract

`tools/playtest.gd` drives the game along a real route (simulated input, not a
teleport) and writes `studio_playtest.json` through the scaffold's
`StudioPlaytest` helper:

```json
{"passed": true, "seconds": 7.4,
 "checks": [{"name": "walks_the_corridor", "passed": true, "value": -9.61, "expected": -9.6}],
 "screenshots": ["studio_shots/02_corridor_mid.png"]}
```

A playtest with no checks fails the gate: a playtest that measures nothing
proves nothing. Headless runs still measure; with a display the screenshots
are real frames, lit with neutral inspection light when the scene has none.

### Feature tracking

The Studio view has five sub-views per project:

- **Overview**: gate, Bucket A, phase tasks.
- **Board**: Kanban with columns Backlog → Planned → Building → In review → Done / Blocked. Backlog is `studio_roadmap.json`: features not yet in any plan. A task names the feature it serves with `"feature"`.
- **Changelog**: merged task commits on the game repo, with model, critic and PR link.
- **Evidence**: playtest checks, perf, colour bible, fuzz.
- **Workbench**: assets with approve/reject, plus renders and verdicts.

Every task shows its gauntlet trail: fix rounds, gate ✓/✗, critic ✓/✗, PR rounds, escalations and the manual decision.

### Manual PR review: your own agents as the last word

You can put an agent you drive by hand on any PR, for example Gemini in the Antigravity IDE or Cursor. With `ARC_PR_MANUAL_REVIEW=1`, a PR the fleet's reviewers approved is **not merged** until someone labels it on GitHub:

- `manual-approved`: the fleet merges it.
- `manual-rejected`: every comment posted while it waited (other than the fleet's own) becomes the implementer's feedback, and a new PR round begins.

The wait polls GitHub and uses no model time. A timeout, if you set one, rejects the PR and never merges it.

```bash
.venv/bin/python main.py studio review-pack ~/repos/<game> <PR#>
```

This writes a paste-ready review: instructions, the task spec (found through the commit's `Task-Id`), the gate, what the fleet's reviewers said, the full diff, and the exact label commands. It also creates the two labels on the repo.

## 10. Environment

| Variable | Default | Meaning |
|---|---|---|
| `ARC_FLEET` | local | `local` or `studio`; fatal on any other value |
| `ARC_EXTERNAL_CONTEXT` | 262144 | context budget declared for OpenRouter-served models (the 64k `ARC_OPENCODE_CONTEXT` exists for an ARC pathology those models do not share) |
| `ARC_STUDIO_DIR` | `logs/studio` | renders, verdicts, phase state, fuzz reports, spend ledger |
| `ARC_BOARD_DIR` | `logs/boards` | project-wide agent board (`board.py`); per-task threads stay in the worktree at `.arc/board.jsonl` and are not committed |
| `ARC_STUDIO_BUDGET_USD` | 25 | ceiling for direct studio calls; 0 disables |
| `ARC_STUDIO_JUDGE_PASS` | 75 | judge score (0–100) a phase must reach to promote |
| `ARC_STUDIO_ADVERSARIAL_ROUND` | 3 | first round that adds adversarial cameras |
| `ARC_STUDIO_ADVERSARIAL_CAMERAS` | 3 | how many it adds |
| `ARC_STUDIO_OSCILLATION_ROUNDS` | 3 | consecutive contradicting rounds before the arbitrator halts |
| `ARC_STUDIO_KEEP_ROUNDS` | 1 | rounds of renders kept in context (all are kept on disk) |
| `ARC_STUDIO_FUZZ_BOTS` | 16 | bots in the swarm |
| `ARC_STUDIO_FUZZ_SECONDS` | 60 | how long the swarm runs |
| `ARC_STUDIO_DISPLAY` | `$DISPLAY` | X display for rendering and computer use |
| `ARC_STUDIO_OPENAI_MODEL` | GPT-6-Sol | which GPT-6 tier the studio runs: `GPT-6-Luna`, `GPT-6-Sol` or `GPT-6-Astra`; fatal on any other value |
| `ARC_CLAUDE_MODEL` | opus | model argument for the Claude Code harness |
| `ARC_CODEX_REASONING` | high | Codex reasoning effort (`low`, `medium`, `high`, `xhigh`, `max`; empty = the model's default, `medium` for Sol). `ultra` is refused: it delegates to sub-agents past the harness cap |
| `ARC_CODEX_MODEL` | (unset) | model argument for `codex exec`; empty derives it from the roster (`GPT-6-Astra` -> `gpt-6-astra`) |
| `ARC_GEMINI_MODEL` | (unset) | model argument for `gemini -p`, for an account whose plan allows the Gemini CLI (not on the default subscription roster) |
| `ARC_CODEX_SANDBOX` | workspace-write | Codex sandbox policy; the agent may edit its worktree and nothing outside it |
| `ARC_CLAUDE_BIN` | (unset) | pin the Claude Code binary |
| `ARC_CODEX_BIN` | (unset) | pin the Codex binary (npm global bins are often off PATH) |
| `ARC_GEMINI_BIN` | (unset) | pin the Gemini CLI binary |
| `ARC_CURSOR_BIN` | agent | pin the Cursor Agent CLI (`agent`) |
| `ARC_CURSOR_MODEL` | grok-4.7-high | model id passed to `agent --model` (Cursor's Grok 4.7) |
| `ARC_AGY_BIN` | agy | pin the Antigravity CLI (`agy`) |
| `ARC_AGY_MODEL` | (unset) | model slug passed to `agy --model`; empty leaves the signed-in account's default |
| `ARC_USAGE_SWAP` | 1 | on a spent plan window, rerun an implementation or review on the next free harness (Cursor Grok 4.7, then Antigravity, then Claude, then Codex, then OpenCode Zen free models, then billed API models); a review never lands in the implementer's family; a planner is not swapped; `0` waits out the reset on the same model |
| `ARC_ZEN_FREE` | 0 | admit every live OpenCode Zen free slug as its own roster family (`Zen-*`, `opencode/<slug>`); `0` drops them |
| `ARC_ZEN_MODEL_CAP` | 2 | per-slug driver/account cap — each partner pool is independent, so run different slugs in parallel and wait for daily resets per model |
| `ARC_SUBSCRIPTION_SESSION_CAP` | 32 | concurrent sessions per plan seat (Claude Code, Codex, Cursor, Antigravity): the roster cap, driver cap and harness pool; 1 restores a single seat |
| `ARC_USAGE_LIMIT_MAX_WAIT` | 691200 (8 days) | the longest one driver run waits for a spent plan window to reset before the attempt fails |
| `ARC_USAGE_LIMIT_POLL` | 900 | re-check interval, in seconds, when a usage-limit refusal names no reset time |
| `ARC_USAGE_LIMIT_MARGIN` | 60 | seconds added past a named reset time before retrying |
| `ARC_HARNESS_LIMIT_CLAUDE` | `ARC_SUBSCRIPTION_SESSION_CAP` | concurrent Claude Code sessions (harness pool only) |
| `ARC_HARNESS_LIMIT_CODEX` | `ARC_SUBSCRIPTION_SESSION_CAP` | concurrent `codex exec` sessions (harness pool only) |
| `ARC_HARNESS_LIMIT_GEMINI` | 2 | concurrent Gemini CLI sessions |
| `ARC_HARNESS_LIMIT_CURSOR` | `ARC_SUBSCRIPTION_SESSION_CAP` | concurrent `agent` sessions (harness pool only) |
| `ARC_HARNESS_LIMIT_AGY` | `ARC_SUBSCRIPTION_SESSION_CAP` | concurrent `agy` sessions (harness pool only) |
| `ARC_PR_MANUAL_REVIEW` | 0 | `1` holds every fleet-approved PR for a human label (`manual-approved` / `manual-rejected`) |
| `ARC_PR_MANUAL_POLL` | 30 | seconds between GitHub checks while a PR waits for manual review |
| `ARC_PR_MANUAL_TIMEOUT` | 0 | give up waiting after this many seconds and REJECT (0 = wait as long as it takes) |
| `ARC_STUDIO_PALETTE_MIN` | 0.6 | share of a render's pixels that must sit on the colour bible |
| `ARC_STUDIO_PALETTE_TOLERANCE` | 48 | RGB distance that counts as "on" a palette colour |
| `ARC_STUDIO_PLAYTEST_MIN_CHECKS` | 3 | fewest scripted-playtest checks the phase gates accept |
| `ARC_STUDIO_MIN_FPS` | 45 | phase-3 frame-rate floor (5th-percentile fps from `tools/perf.gd`) |
| `ARC_STUDIO_MAX_SHADOW_LIGHTS` | 8 | phase-3 cap on shadow-casting lights |
| `ARC_STUDIO_FREE_JUDGE_MODEL` | (unset) | a raw OpenRouter `vendor/id` used instead of the roster judge, to exercise the visual loop at $0; its verdicts are marked `validation` and never gate a phase |
| `ARC_GODOT_BIN` | (unset) | pin the Godot binary |
| `ARC_BLENDER_BIN` | (unset) | pin the Blender binary |
| `ARC_OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | OpenRouter endpoint |

`OPENROUTER_API_KEY` is the provider's own credential, not an `ARC_*` knob; it
lives in `.env` alongside `ARC_API_KEY` and is shared with the operator's
opencode config.
