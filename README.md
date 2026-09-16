# ARC LLM Orchestrator

Runs research questions past every live AI model family at once (via Virginia
Tech's ARC LLM API), has them critique each other, verifies the results, and
stores everything in SQLite. A second workload has the models build a browser
game piece by piece, and a third drives the two-model coding fleet
(GLM-5.3 on `opencode`, DeepSeek-V4.1-Flash-thinking-max on `reasonix`) through a
governed per-task pipeline. A web dashboard shows it all live — and you can
open that dashboard from your laptop or phone.

## Quick start — see the dashboard from your laptop and phone

The dashboard is a small website hosted by this machine. You need two things:
the server running, and the right address (`localhost` only ever means "this
same device", so your phone and laptop need this machine's real network address).

**1. Start the server (on this machine):**

```bash
cd ~/arc-orchestrator
./start.sh
```

It prints the exact addresses to use, for example:

```
Open in a browser:
  on this computer:   http://localhost:8787
  laptop or phone:    http://10.0.0.153:8787   (same wifi/network)
  laptop or phone:    http://100.90.233.101:8787   (tailscale - works even away from home)
```

**2. On your laptop:** open the address marked "same wifi/network"
(e.g. `http://10.0.0.153:8787` — use the one `start.sh` prints, yours may differ).

**3. On your phone:** same address, but add `/phone.html` for the small-screen
page (e.g. `http://10.0.0.153:8787/phone.html`). Add it to your home screen for
one-tap access.

**4. Stop the server:**

```bash
./stop.sh
```

Want it to start by itself on boot? See "Run 24/7 with systemd" below.

## Troubleshooting

**`Address already in use` / `port 8787 is already in use`**
The server is *already running* — this is not an error in your setup. Just open
the address in your browser. To restart cleanly: `./stop.sh && ./start.sh`.

**Can't open it from your phone or laptop**
- Both devices must be on the **same wifi/network** as this machine.
- Use the `http://10.0.0.x:8787`-style address that `start.sh` prints — **never**
  `localhost` or `127.0.0.1` (those always point back at the device itself).
- Keep the `http://` in front and the `:8787` at the end.
- Firewall blocking it? Allow the port: `sudo ufw allow 8787`
- Some routers stop devices from seeing each other ("AP isolation" / "client
  isolation") — turn that off in the router settings.

**Away from home**
Use the address `start.sh` labels "tailscale" (e.g. `http://100.90.233.101:8787`) —
it works from anywhere as long as the other device is on the same Tailscale network.

**Logs**
Server log: `logs/server.log`. Live activity feed the dashboard reads:
`logs/events.jsonl`.

## What the dashboard shows

| Page | What it is |
|---|---|
| `/` | Full dashboard: live graph diagrams (node colors = what's happening now), model leaderboard, critique heatmap, event stream, generated-game code viewer |
| `/phone.html` | The important bits, laid out for a phone screen |
| `/usage.html` | Token/request usage per model |

The pages read `orchestrator.db` and the event log and never touch the model
API themselves — but the dashboard is **not read-only**. Its buttons create
task files, start and stop fleet runs, archive projects and open promotion
PRs, and a fleet run edits repositories and pushes to GitHub.

## Who can reach the dashboard

It listens on **every interface** (so your phone can open it) and has **no
login**. Out of the box, anyone who can reach the port — every device on the
wifi, everyone on your Tailscale network — can view everything *and* press
every button. Two settings in `.env` narrow that:

| Setting | Effect |
|---|---|
| `ARC_DASHBOARD_TOKEN=<long random string>` | Every action (POST) must carry the token. The dashboard asks for it once, the first time you press a button, and keeps it in that browser. Viewing stays open. |
| `ARC_DASHBOARD_BIND=<address>` | Listen on one address only: `127.0.0.1` for this machine alone, or your Tailscale IP (the `100.x.y.z` one `start.sh` prints) so only your own devices can reach it at all. |

Set the token if the machine is on a network you do not fully control; set
both if it is ever exposed beyond that. Cross-site requests from other
websites you have open are refused regardless (the server accepts only
`application/json` from its own origin), so a malicious page cannot press the
buttons for you — but that protects against a web page, not a neighbour.

## The research graph

Every round fans one question out to every live model family in parallel, then
cross-critiques, synthesizes, and verifies. Failed rounds loop back into a
stronger synthesis (bounded by `ARC_MAX_VERIFY_ROUNDS`, default 6):

```
pick_topic ──> gen_questions ──┬─> answer_glm ────────┐
                               ├─> answer_deepseek ───┤  gather (fan-in)
                               └─> research_web ──────┘   (one per family
                                            │             in config.FAMILY_ORDER)
                                           │
                                           ▼
                                       critique      each answer scored 0-10 by a family
                                           │          that did NOT write it
                                           ▼
                                       synthesize <──────────────┐
                                           │                      │ fail & rounds < MAX_VERIFY_ROUNDS
                                           ▼                      │
                                       verify ────────────────────┘
                                           │ pass, or rounds exhausted
                                           ▼
                                  store_results + extract_seeds
```

`store_results` also asks a rotating model for new topics, which `pick_topic`
consumes next round — the system generates its own work forever.

### Concurrency

Per-family semaphores follow the ARC ceilings on the two-model fleet (14
requests in flight max): deepseek 10, glm 4 (the official ARC docs value,
adopted 2026-09-15 — the intervening 4 → 3 was a one-day pin against a
backend rejection text that turned out to describe a shared account key) — see
[docs/concurrency-limits.md](docs/concurrency-limits.md). Web research uses the
`-legacy-tool-calling` variants with `tool_ids: ["server:websearch"]`. All
requests stream (the API caps non-streaming at 8,000 tokens). Roles rotate
across families every round so every model does every job. Retries use
exponential backoff and honor `Retry-After` on 429/5xx.

## The Minecraft build workload

A second, independent graph (`build_work.py`) has the model families write a
playable browser voxel game module by module, with every module forced through
a verification gauntlet:

```
planner ──> produce_engine ─┐
           produce_world ───┤
           produce_player ──┤  produce = implement → syntax gate → contract check →
           produce_ui ──────┤   cross-model review → fix loop (≤ ARC_MAX_MODULE_RETRIES)
           produce_main ────┤
           produce_html ────┘
                    │
                    ▼
              assemble (fan-in) ──> integration_review ──pass──> metrics_store
                                       │ fail & rounds < ARC_MAX_INTEGRATION_ROUNDS
                                       ▼
                                  wiring_fix (re-produces only the broken modules)
```

- Producers/reviewers rotate across families so no model reviews its own code.
- `integration_review` reads the assembled files from disk, so a module that
  claims to export `World` but doesn't gets caught.
- `--iterations N` runs create-then-improve cycles, each starting from the
  previous files on disk and the critiques in the DB.
- Output lands in `production/minecraft/`, viewable in the dashboard at `/api/code`.

## Setup (first time only)

```bash
cd ~/arc-orchestrator
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Put your API key in `.env` (get it from https://llm.arc.vt.edu →
User profile → Settings → Account → API keys):

```
ARC_API_KEY=sk-...
```

Optional but recommended for the code fleet — install
[Graft](https://github.com/trailhq/Graft), the code-graph CLI that hands each
harness the exact `file:line` spans its task points at instead of letting it
grep the worktree on the API's clock (see [Code-graph context](#code-graph-context-graft)):

```bash
deploy/install-graft.sh        # npm i -g @nanonets/graft, with a no-compiler fallback
.venv/bin/python main.py doctor   # "PASS  graft code-graph hints: on"
```

Test everything without spending a single request:

```bash
.venv/bin/python main.py once --dry-run --questions 3 -v
```

Then one real round before committing to 24/7:

```bash
.venv/bin/python main.py once --questions 3
```

## All commands

```bash
./start.sh                                  # easiest way: start the dashboard + print your addresses
./stop.sh                                   # stop the dashboard
.venv/bin/python main.py run                # research rounds, 24/7 forever (Ctrl+C stops cleanly)
.venv/bin/python main.py run --rounds 20    # stop after 20 rounds
.venv/bin/python main.py run --pipeline 3   # 3 rounds in flight at once
.venv/bin/python main.py once [--questions N]     # single round
.venv/bin/python main.py build [--iterations N]   # Minecraft build workload
.venv/bin/python main.py serve [--port P]         # dashboard manually (prints all addresses too)
.venv/bin/python main.py status                   # DB statistics
.venv/bin/python main.py graph                    # print both graph topologies
.venv/bin/python main.py bench list               # benchmark suites/tasks
.venv/bin/python main.py bench run [options]      # run benchmark jobs
.venv/bin/python main.py bench report [--run-id N]  # score tables (default: latest run)
.venv/bin/python main.py bench runs               # past benchmark runs
```

### Benchmarking models x harnesses

`bench` answers: which ARC model, effort level, harness, and option set solves
coding tasks best. Three suites (31 tasks total; `bench list` details):

- `humaneval` — 15 canonical HumanEval-style tasks (sanity baseline; saturated
  for frontier models, treat accordingly)
- `original` — 11 hand-written 2026 function tasks with deterministic edge
  cases (contamination-resistant)
- `package` — 5 multi-file mini-project specs (suited to the CLI harnesses)

Variables swept independently:

- **model x effort**: `--models 'deepseek:low;max,glm,...'` (efforts
  after `:` are `;`-separated) or `all`
  (per-family effort names validated against `config.FAMILIES`; deepseek uses
  `max`, not `high`)
- **harness**: `direct` (single chat call -> extract code -> run tests),
  `fanout` (best-of-N parallel samples, test-based selection),
  `fixloop` (aider-style test feedback, <= `--max-rounds`),
  `review` (cross-family reviewer over test results),
  `opencode` (full CLI agent with shell access in a scratch dir; live for
  GLM-5.3) — the retired `kimi` harness solver remains only for historical
  comparisons and refuses any family but the retired kimi one
- **sampling**: `--n` direct samples per cell (pass@1 = mean over samples),
  `--fanout-n` inner samples for fanout (default 4), `--temperature`

Metrics per (model, effort, harness) cell: pass@1 %, any-sample coverage %,
avg seconds, avg tokens; plus suite x model and tier x harness matrices and
unbiased pass@k / pass^n for multi-sample cells (`bench report`). Every job
writes `logs/bench/<run>/<task>__<model-effort>__<harness>__s<i>/` with the
candidate files and runs the task's tests in a subprocess. Results live in
the `bench_runs`/`bench_results` SQLite tables.

Methodology note: published results (SWE-bench, aider, Terminal-Bench) show
the same model swings 10-25 points across scaffolds, so the *harness is the
unit of measurement* — never compare a `direct` score for model A against a
`fixloop` score for model B and call the model better.

```bash
.venv/bin/python main.py bench run --plan --models all \
    --harnesses direct,fanout,fixloop,review --n 4   # print matrix, run nothing
.venv/bin/python main.py bench run --limit 6 --models all --efforts default \
    --harnesses direct --n 4 --dry-run               # validate plumbing, no API calls
.venv/bin/python main.py bench run --models all --harnesses direct,fanout \
    --n 4 --label weekly                            # real run
```

Parallel benchmark jobs are bounded by the per-family API concurrency, which
is governed by the pool semaphores in `.env`.

All commands accept `--dry-run` (simulated model calls, separate `dry-run.db`;
the build workload writes to `production/minecraft-dry-run/` so it can never
clobber real artifacts) and `-v` (debug logging).

## Run 24/7 with systemd

```bash
mkdir -p ~/.config/systemd/user
cp deploy/arc-orchestrator.service ~/.config/systemd/user/
cp deploy/arc-dashboard.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now arc-orchestrator arc-dashboard
loginctl enable-linger $USER        # keep running after logout

journalctl --user -u arc-orchestrator -f        # follow the logs
systemctl --user stop arc-orchestrator          # graceful stop
```

`Restart=always` brings everything back after crashes and reboots; the
supervisor marks orphaned rounds as failed on startup, so the DB never lies.

## Tuning (environment variables in .env)

| Variable | Default | Meaning |
|---|---|---|
| `ARC_QUESTIONS_PER_ROUND` | 10 | questions per round; >= 10 saturates every live family |
| `ARC_PIPELINE_ROUNDS` | 4 | rounds in flight simultaneously |
| `ARC_MAX_VERIFY_ROUNDS` | 6 | max synthesis attempts before shipping best effort |
| `ARC_VERIFY_PASS_SCORE` | 7.5 | verifier score (0-10) required to pass |
| `ARC_SEEDS_PER_ROUND` | 3 | new topics generated per round |
| `ARC_ROUND_COOLDOWN` | 5 | seconds between round launches |
| `ARC_STATS_INTERVAL` | 300 | seconds between stats log lines |
| `ARC_REQUEST_TIMEOUT` | 1200 | per-request timeout (seconds) |
| `ARC_MAX_RETRIES` | 24 | retries per request on 429/5xx/network errors |
| `ARC_SESSION_RETRIES` | 24 | retries on 400 "concurrent session limit" (per-user caps) |
| `ARC_SESSION_BACKOFF_CAP` | 60 | max backoff per session-limit retry (seconds) |
| `ARC_LIMIT_<FAMILY>` | docs limit | override a family's semaphore (e.g. `ARC_LIMIT_DEEPSEEK=6`) |
| `ARC_EVENTS_LOG` | logs/events.jsonl | event log path (dashboard tail) |
| `ARC_BUILD_OUTPUT_DIR` | production/minecraft | where the build workload writes |
| `ARC_MAX_MODULE_RETRIES` | 6 | fix-loop attempts per module in the gauntlet |
| `ARC_MAX_INTEGRATION_ROUNDS` | 6 | wiring_fix ⇄ integration_review cycles |
| `ARC_REVIEW_PASS_SCORE` | 6.5 | cross-model review score required to ship |
| `ARC_DASHBOARD_PORT` | 8787 | dashboard port |
| `ARC_REPO_ROOT` | `~` | the only directory tree the dashboard accepts a project repo from (`/api/projects/create`, and every task file it runs) |
| `ARC_DRIVER_LEASE_WAIT` | 10800 | max seconds a task waits for a driver lease before failing on capacity |
| `ARC_DRIVER_CAPACITY_BACKOFF_CAP` | 600 | max capacity-backoff sleep on a harness retry (seconds) |
| `ARC_BASE_URL` | https://llm-api.arc.vt.edu/api/v1 | ARC API base URL |
| `ARC_MAX_GRAPH_STEPS` | 6000 | max graph steps before a run is aborted |
| `ARC_BASE_BRANCH` | main | branch the fleet's pull requests target and merge into; set it to `development` (keeping `ARC_PROD_BRANCH=main`) for a two-branch flow with `code promote` |
| `ARC_PROD_BRANCH` | main | production branch; promotion (`code promote`, the dashboard button) exists only while it differs from `ARC_BASE_BRANCH` |
| `ARC_PR_REVIEWERS` | 2 | PR reviewers *wanted* before merge; the two-family fleet can field only one cross-family reviewer, and `task.pr_review_thin` records the shortfall |
| `ARC_PR_MAX_ROUNDS` | 16 | PR review rounds before a task fails |
| `ARC_REQUIRE_TESTS` | 1 | require changes to ship tests that fail without them |
| `ARC_GRAFT` | 1 | code-graph hints for harness runs (needs the `graft` binary; `0` = off, for an A/B) |
| `ARC_GRAFT_BIN` | (unset) | path to the `graft` binary; unset = `graft` on PATH, then `~/.local/opt/node/bin` |
| `ARC_GRAFT_HINTS` | 3 | `file:line` spans from the code graph handed to an implementer |
| `ARC_REASONIX_BIN` | reasonix | the reasonix CLI; unset = `reasonix` on PATH, then `~/.local/opt/node/bin/reasonix` |
| `ARC_REASONIX_HOME` | logs/reasonix-home | the fleet's private `REASONIX_HOME` (generated config.toml + .env holding the ARC key) |
| `ARC_HARNESS_LIMIT_REASONIX` | 7 | max concurrent reasonix processes (the DeepSeek harness pool); 7/7 measured clean 2026-09-14 |
| `ARC_DASHBOARD_BIND` | 0.0.0.0 | address the dashboard listens on (see *Who can reach the dashboard*) |
| `ARC_DASHBOARD_TOKEN` | (unset) | when set, every dashboard action must carry it; viewing stays open |

A round makes roughly `13 x questions` model calls. Defaults are polite; the
per-model semaphores are the hard guarantee that you never exceed ARC's
documented concurrency limits.

## Tests

Stdlib `unittest`, no extra dependencies:

```bash
.venv/bin/python -m unittest discover -s tests -t tests
```

`./check.sh` is the fuller gate — module compilation and imports, the unit
tests, a JavaScript syntax check plus a DOM-reference check on every dashboard
page, and validation of every task file in `~/tasks`. Every task file that
edits **this** repo starts its `verify_cmd` with `./check.sh &&`, so the fleet
cannot merge a change that breaks the orchestrator it is running on. A grep
gate only proves a string is present; it never proves the change works.

They cover the parts that historically broke silently: graph execution and
drain-on-failure semantics, taskfile validation, reviewer-verdict parsing,
resume/escalation planning, driver slot accounting and backoff classification,
lease caps, orphan reaping, and review-diff isolation between parallel tasks
(the last two run against real temporary git repos). Run them before
committing engine changes — several of the bugs they encode were found only
by running the fleet for hours.

## Extending

Add a node in `work.py`, wire it with edges, and (if it joins parallel
branches) mark `gather=True`:

```python
async def my_node(ctx):
    text = await pool.chat("glm", [...], purpose="my_step")
    return {"out": text}

g.node("my_node", my_node)
g.edge("synthesize", "my_node")
g.edge("my_node", "verify")
```

Nodes read the shared state blackboard (`ctx["results"][node_name]`) and return
their own result; conditional edges are `when=lambda result, ctx: ...` lambdas.
ARC's model list is a registry in `config.py` — add or rename models there as
the service evolves. Only `config.py`, `work.py`, and `.env` ever need touching.

## Files

| File | Role |
|---|---|
| `start.sh` / `stop.sh` | easy dashboard start/stop with access URLs |
| `config.py` | model registry + concurrency limits + tuning |
| `pool.py` | API client: per-family semaphores, streaming, websearch, retries, dry-run |
| `graph.py` | graph engine: nodes, conditional edges, fan-out/fan-in, cycles, max_steps guard |
| `work.py` | the round graph (the actual multi-model workflow) |
| `scheduler.py` | 24/7 supervisor: overlapping rounds, backoff, stats, signals |
| `store.py` | SQLite persistence: rounds, items, answers, critiques, seeds, builds |
| `build_work.py` | the Minecraft build graph + verification gauntlet |
| `events.py` | append-only JSONL event log (contextvars-tagged) |
| `dashboard.py` + `static/` | live dashboard (desktop, phone, usage pages) and its action endpoints — see *Who can reach the dashboard* |
| `main.py` | CLI: run / once / build / serve / status / graph / bench / code |
| `bench.py` | benchmark runner: solvers, scoring (pass@k), report tables |
| `bench_data.py` | 31-task dataset (humaneval / original / package suites) |
| `orchbench.py` | orchestration variant benchmark: governed DAG per policy variant |

---

## Multi-harness code orchestration

A third workload (`code_tasks.py`) drives coding agents as first-class
harnesses: instead of talking to the ARC API directly, it shells out to the
`opencode` (GLM-5.3) and `reasonix` (Reasonix, the DeepSeek-native
cache-first agent) CLIs, which do their
own tool use (file edits, shell commands) inside per-task git worktrees. The
orchestrator is the only git actor — harnesses only write files inside their
worktree.

### Role map (hard rule)

| Model | Harness CLI | Tier, allowed roles |
|---|---|---|
| `DeepSeek-V4.1-Flash-thinking-max` | `reasonix` | medium — implement, review, PR-review; never plans. Much faster than GLM-5.3, so it carries the implementation load |
| `GLM-5.3` | `opencode` | hard — implement, plan, review, PR-review; the fleet's strongest model, the `code plan` planner, and the last escalation stage |

Every implementation must pass a deterministic verify gate (a shell command
run inside the worktree) and then a review by the *other* model family — a
reviewer never shares a family with the implementer it reviews
(**GLM-5.3 → deepseek, DeepSeek → glm**). Rejections
feed back into a fix loop bounded by `ARC_MAX_FIX_ROUNDS` (default 16).
Cross-family review is unconditional: the temporary same-family-review
override (2026-09-12..14, while GLM-5.3's backend was unstable) was removed
once it stabilised.

Per-model governor caps keep concurrent harness instances under the measured
or provider-published ARC ceilings (deepseek 10, glm 4 — deepseek's published
2026-09-12, glm's the official docs.arc.vt.edu value adopted 2026-09-15) minus an interactive reserve, and **per-harness caps (opencode 5, reasonix 7)**
keep opencode and reasonix under their own pools — the effective numbers come from
`config.driver_limit(model)` and `config.harness_limit(harness)`, computed
from `_MODEL_DRIVER_CAP` / `_HARNESS_CAP`. Override
with `ARC_DRIVER_LIMIT_<FAMILY>`
(e.g. `ARC_DRIVER_LIMIT_DEEPSEEK=3`) or `ARC_HARNESS_LIMIT_<HARNESS>`.

### Per-task pipeline

```
alloc worktree (~/worktrees/<repo>/<id>, branch task/<id>, from main)
  → graft build (code graph beside the worktree; ~2 s, no model) + graft ask
    → the task's top-3 file:line spans go into the implementer prompt
  → implementer writes code IN THE WORKTREE
  → verify gate (verify_cmd, cwd=worktree)
  → cross-family review (strict-JSON verdict {"pass": ...}) with the graph's
    blast radius — dependents of the changed symbols, tests reaching them —
    in the prompt; fix loop ≤ 8
    (ARC_MAX_FIX_ROUNDS; then tier escalation with a fresh budget)
  → publish commit (trailers: Harness/Model/Reviewer/Task-Id) + push + open PR
  → one cross-family PR review (`task.pr_review_thin` records the thin gate)
  → gh pr merge --squash --delete-branch (serialized)
  → worktree + branch cleanup
```

Tasks claiming dependencies (`deps`) are ordered by graph edges
(`publish_<dep> → alloc_<task>`); every worktree branches from `main`, which
already contains each dep's merge.

### Code-graph context (Graft)

Before this, a harness found the code it had to change the way every coding
agent does by default: grep, read, grep again — and each of those turns
re-sends the whole conversation to the API. On this fleet that is paid twice:
in ARC tokens, and in wall-clock against per-model concurrency caps that are
shared with the rest of campus. `graft.py` removes most of that search:

- **Implementer** — `graft build` parses the worktree with tree-sitter into a
  symbol graph (deterministic, no model, no key; ~2 s cold / ~0.2 s
  incremental for this repo), then `graft ask` ranks the task's title, files
  and prompt against it. The top `ARC_GRAFT_HINTS` (3) `file:line` spans go
  into the prompt as **WHERE TO LOOK**, and the harness is told the
  `graft ask / skeleton / callers / grep` commands exist so its remaining
  searches are one exact call each. Recomputed per attempt, so a rework's
  spans describe the tree as it is now.
- **Reviewers** — `graft blast --base <branch>` lists which symbols depend on
  the changed ones and which tests reach them: the REGRESSIONS and TESTS
  checks, precomputed from graph edges instead of grepped for.
- **Planner** — `graft map` (directory clusters, hub symbols, hotspots, ~900
  tokens) opens the planner prompt, so the slowest model is not spending its
  first minutes on `ls` and `grep`.

The graph never lives inside the worktree: `graft build` there would edit
`.gitignore` and drop an `.ignore` file into the task's diff, which the SCOPE
rule tells reviewers to reject. It lives under `~/worktrees/.graft/<repo>/<id>/`
and reaches the harness as `GRAFT_DIR`. Every call is optional and
fail-silent — no binary, a timeout, a parse error, and the run proceeds exactly
as before; `ARC_GRAFT=0` turns it off for an A/B. `graft.build` / `graft.hints`
events in the log carry the build time, node/edge counts and hit counts.
Graft's own 162-run sweep reports 46% fewer tool calls, 42% fewer tokens and
60% less wall-clock at equal correctness; read that as the ceiling, and take
the fleet's number from `task.budget` tokens before and after.

### Task files

```json
{"project": {"repo": "/abs/path/to/repo", "title": "short label",
 "tasks": [
   {"id": "t01-html", "title": "Create src/index.html hello page",
    "prompt": "Create the file src/index.html: <detailed spec>",
    "model": "DeepSeek-V4.1-Flash-thinking-max", "reviewer": "glm",
    "verify_cmd": "test -f src/index.html && grep -q Hello src/index.html",
    "files_hint": ["src/index.html"], "deps": []}
 ]}}
```

- `model` must be a live implementer (`DeepSeek-V4.1-Flash-thinking-max` or
  `GLM-5.3` today); `reviewer` names a review
  family (`glm`, `deepseek`) that differs from the implementer's.
- `verify_cmd` runs with the worktree as cwd; empty string skips the gate.
- `deps` list task ids that must merge first; ids are unique, cycles rejected.

### Commands

```bash
.venv/bin/python main.py code run tasks.json [--dry-run] [--repo PATH] [-v]
.venv/bin/python main.py code plan "one-sentence goal" /path/to/repo
.venv/bin/python main.py code list [--json] [--db PATH]
.venv/bin/python main.py code status [--reset-stale]
.venv/bin/python main.py code reconcile [--dry-run] [--force]
.venv/bin/python main.py code bench run [--variants LIST|all] [--plan] [--stamp S]
.venv/bin/python main.py code bench report [--stamp S]
.venv/bin/python main.py doctor
```

- `code run --dry-run` prints the resolved DAG (implement/review pairing,
  deps, verify gates) with no model calls and no git mutations.
- `code plan` asks GLM-5.3 (the planner) to break a goal into 2–6 task JSON
  entries and writes the file to `~/tasks/`, ready for `code run`. Big goals
  need a longer driver timeout:
  `ARC_DRIVER_TIMEOUT=5400 .venv/bin/python main.py code plan ...` — see
  [docs/runbook.md](docs/runbook.md) § "Planning a large goal".
- `code status` dumps the `code_tasks` and `harness_runs` tables: per-task
  status, and per-firing harness/model/role/attempt/seconds/verdict rows.
- `code list` shows one row per task file in `~/tasks/` with per-project
  merge progress (declared vs. `merged` tasks, status counts, last activity);
  `--json` emits the same data for scripts. `main.py doctor` runs the
  pre-flight checks (`ARC_API_KEY` set and not the placeholder, each live
  harness binary found — `opencode` and `reasonix`, derived from
  `config.MODEL_HARNESS` and resolved through `config.harness_bin` — the
  worktree/tasks dirs creatable, the timeout
  invariant `DRIVER_LEASE_TTL > DRIVER_TIMEOUT > DRIVER_IDLE_TIMEOUT`, and no
  stale `running` rows) and exits non-zero on any failure.
  `code reconcile` reaps everything a killed run left behind — stale
  `running` rows, driver leases held by dead processes, and orphaned
  worktrees. It refuses to run while a `code run` is alive, and never deletes
  a worktree holding uncommitted agent edits or a branch with unmerged
  commits. `--dry-run` reports without changing anything.

  `--reset-stale` marks tasks stuck at `running` (their run process died)
  as `failed` — use only when no run is alive.
- `code bench` is the orchestration-level benchmark (`orchbench.py`):
  instead of scoring one model on one task, it runs the **entire governed
  DAG** on a fresh `filetoolkit` repo (6 tiered tasks) once per declared
  policy variant — reviewer carousel, implement-only models, self/no-review,
  harness swaps, flat and mis-routing, fix-loop bounds (`orchbench.VARIANTS`).
  Each variant appends merges/green-tests/fix-rounds/review-rejects/timings
  to `logs/orchbench/<stamp>/results.jsonl`; `report` prints the table.

### Paths

- `~/repos/<project>` — blessed clone; merges land on its `main`.
- `~/worktrees/<project>/<task-id>` — scratch worktrees, removed after merge.
- `~/tasks/` — task files live here, including planner output.
- `logs/harness/` — one JSONL transcript per harness firing, named
  `<task-id>-x<attempt>-<role>-<n>.jsonl` (unique per fix-loop attempt);
  full implementer sessions and reviewer verdicts are preserved here.
- SQLite tables `code_tasks` / `harness_runs` in `orchestrator.db`.

### Gotcha for future driver work

opencode (bun/JS) resolves its project directory from the **`$PWD`
environment variable**, not from `getcwd()`. A spawned subprocess inherits
the parent's `$PWD`, so passing `cwd=worktree` alone is not enough — edits
silently land wherever the orchestrator itself was launched from.
`drivers.py` therefore spawns every harness with
`env=dict(os.environ, PWD=str(worktree))`. Any new subprocess driver for
JS-based tooling must do the same. Similarly, the retired kimi CLI's
stream-json put assistant text in `"content"` fields (opencode uses `"text"`), and
`drivers.parse_transcript` accepts both.

## Worktrees and project chains

Every fleet task runs in its own git worktree at
`~/worktrees/<project>/<task-id>` on a branch `task/<task-id>` cut from
`main`. The orchestrator (`gitstore.py`) is the only git actor: harnesses
only write files inside their worktree and never commit.

Merges back to `main` go through a pull request: `publish` pushes `task/<id>`
and opens the PR, one cross-family reviewer reads the real PR diff, and only
`pr_merge` merges (`gh pr merge --squash --delete-branch`). A task whose PR
will not merge cleanly is resynced and re-reviewed; a real textual conflict
ends in status `conflict` and `main` stays untouched.

Projects can be chained into sequences: put `"after": ["other-taskfile.json"]`
in a taskfile's project object and `code run` waits until that whole
project's tasks are merged before starting (aborting if the dependency
project failed); `--no-wait` checks once instead of waiting. Details:
[docs/taskfile-schema.md](docs/taskfile-schema.md) and
[docs/runbook.md](docs/runbook.md).

## Governance
Multi-model orchestration is governed by [AGENTS.md](AGENTS.md).

- [AGENTS.md](AGENTS.md) (governance + model roles)
- [docs/orchestration-contract.md](docs/orchestration-contract.md) (per-task orchestrator contract)
- [docs/model-tiers.md](docs/model-tiers.md) (tier routing + cross-review matrix)
- [docs/concurrency-limits.md](docs/concurrency-limits.md) (concurrency limits and overrides)
- [docs/taskfile-schema.md](docs/taskfile-schema.md) (taskfile reference)
- [docs/runbook.md](docs/runbook.md) (operator runbook)
- [docs/orchestration-roadmap.md](docs/orchestration-roadmap.md) (living roadmap: what is in flight, what waits for a planner, what is named but not yet specified)
- [docs/self-improvement-projects.md](docs/self-improvement-projects.md) (ready-to-run task files that point the fleet at this repo)
- [docs/audit-2026-09-09.md](docs/audit-2026-09-09.md) (reliability audit: the twelve defects behind the stalled fleet, and why each fix is shaped the way it is)
