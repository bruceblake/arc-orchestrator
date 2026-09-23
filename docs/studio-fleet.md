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
- **Gemini is not on this profile.** On the operator's Google plan it is usable
  only inside the Antigravity IDE, not from a headless CLI. `GeminiDriver`
  stays in `drivers.py` for an account that can use it, and Gemini remains
  available per token on `studio-api`. The visual judge here rotates between
  Claude and GPT-6, both of which read images.

A consumer plan is metered on rolling windows — a live run shows
`five_hour: {utilization: 0.4}` and, when exhausted,
`overageStatus: "rejected"` with no fallback. **The fleet does not cap the
plan seats locally** (operator directive 2026-09-22): `claude` and `codex`
run up to `ARC_SUBSCRIPTION_SESSION_CAP` (default 32) concurrent sessions,
and the plan's usage window is the real limit. This was once 1 for Claude and
2 for Codex; set `ARC_SUBSCRIPTION_SESSION_CAP=1` to go back to a single seat
when you want your own interactive session to have the plan to itself.

**When a window runs out, the task waits for it to reset — it does not
fail.** `drivers.Driver.run` recognises the plans' refusals ("usage limit
reached", "You've hit your limit · resets 3pm", Codex's
`usage_limit_reached`, a rejected `rate_limit_event`), reads the reset time
the refusal names, and parks the harness until then plus
`ARC_USAGE_LIMIT_MARGIN`. A refusal that names no time is retried every
`ARC_USAGE_LIMIT_POLL` seconds. The wait does not count as an attempt, costs
no fix round or escalation, and every other task on the same harness waits
for the same reset rather than spending its own refusal. The dashboard sees
`driver.usage_limit` once and `driver.usage_wait` every five minutes while
parked. One driver run waits at most `ARC_USAGE_LIMIT_MAX_WAIT` (8 days, so a
weekly window fits) before the attempt fails normally.

`studio-api` is the same roles through OpenRouter: billed per token, but with
real parallelism and no plan windows. Use it when the work outgrows the plans.

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

## 10. Environment

| Variable | Default | Meaning |
|---|---|---|
| `ARC_FLEET` | local | `local` or `studio`; fatal on any other value |
| `ARC_EXTERNAL_CONTEXT` | 262144 | context budget declared for OpenRouter-served models (the 64k `ARC_OPENCODE_CONTEXT` exists for an ARC pathology those models do not share) |
| `ARC_STUDIO_DIR` | `logs/studio` | renders, verdicts, phase state, fuzz reports, spend ledger |
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
| `ARC_SUBSCRIPTION_SESSION_CAP` | 32 | concurrent sessions per plan seat (Claude Code, Codex): the roster cap, driver cap and harness pool for both; 1 restores a single seat |
| `ARC_USAGE_LIMIT_MAX_WAIT` | 691200 (8 days) | the longest one driver run waits for a spent plan window to reset before the attempt fails |
| `ARC_USAGE_LIMIT_POLL` | 900 | re-check interval, in seconds, when a usage-limit refusal names no reset time |
| `ARC_USAGE_LIMIT_MARGIN` | 60 | seconds added past a named reset time before retrying |
| `ARC_HARNESS_LIMIT_CLAUDE` | `ARC_SUBSCRIPTION_SESSION_CAP` | concurrent Claude Code sessions (harness pool only) |
| `ARC_HARNESS_LIMIT_CODEX` | `ARC_SUBSCRIPTION_SESSION_CAP` | concurrent `codex exec` sessions (harness pool only) |
| `ARC_HARNESS_LIMIT_GEMINI` | 2 | concurrent Gemini CLI sessions |
| `ARC_STUDIO_FREE_JUDGE_MODEL` | (unset) | a raw OpenRouter `vendor/id` used instead of the roster judge, to exercise the visual loop at $0; its verdicts are marked `validation` and never gate a phase |
| `ARC_GODOT_BIN` | (unset) | pin the Godot binary |
| `ARC_BLENDER_BIN` | (unset) | pin the Blender binary |
| `ARC_OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | OpenRouter endpoint |

`OPENROUTER_API_KEY` is the provider's own credential, not an `ARC_*` knob; it
lives in `.env` alongside `ARC_API_KEY` and is shared with the operator's
opencode config.
