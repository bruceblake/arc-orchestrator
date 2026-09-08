# ARC LLM Orchestrator

24/7 self-perpetuating multi-model graph over Virginia Tech ARC's LLM API
(`https://llm-api.arc.vt.edu/api/v1`). Every question fans out to all four model
families in parallel, gets cross-critiqued by *different* families against live web
evidence, synthesized, verified, and — on failure — sent back through a bounded
refine loop. Verified results and new research seeds are persisted to SQLite, so
the system generates its own work forever.

## The graph

```
pick_topic ──> gen_questions ──┬─> answer_gpt-oss ────┐
                               ├─> answer_glm ────────┤
                               ├─> answer_kimi ───────┤  gather (fan-in)
                               ├─> answer_deepseek ───┤
                               └─> research_web ──────┘   (legacy-tool-calling + server:websearch)
                                           │
                                           ▼
                                       critique      each answer scored 0-10 by a family
                                           │          that did NOT write it, against web evidence
                                           ▼
                                       synthesize <──────────────┐
                                           │                      │ fail & rounds < MAX_VERIFY_ROUNDS
                                           ▼                      │ (conditional loop-back)
                                       verify ────────────────────┘
                                           │ pass, or rounds exhausted
                                           ▼
                                  store_results + extract_seeds   (terminal)
```

- `critique` is a gather node: it waits for all five parallel branches.
- `verify -> synthesize` is a conditional cycle: failed rounds carry feedback back
  into a stronger synthesis, bounded by `ARC_MAX_VERIFY_ROUNDS` (default 3).
- `store_results` saves answers/critiques/verdicts and asks a rotating model for
  new topics, which `pick_topic` consumes next round — the self-perpetuating loop.

## Concurrency

Per-family semaphores match the documented limits exactly, so the pool holds up
to **27 requests in flight simultaneously**:

| Family | Models | Concurrent | Context |
|---|---|---|---|
| gpt-oss | gpt-oss-120b (+thinking variants) | 10 | 128k |
| deepseek | DeepSeek-V4-Flash (+thinking variants) | 10 | 512k |
| glm | GLM-5.3 (+thinking variants) | 4 | 128k |
| kimi | Kimi-K3 (+thinking variants) | 3 | 128k |

Web research uses the `-legacy-tool-calling` variants with
`tool_ids: ["server:websearch"]`. All requests stream (the API caps
non-streaming responses at 8,000 tokens). Roles (questioner, synthesizer,
verifier, seeder) rotate across families every round so every model does every
job. Retries use exponential backoff and honor `Retry-After` on 429/5xx.

## Setup

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

Test everything without spending a single request first:

```bash
.venv/bin/python main.py once --dry-run --questions 3 -v
```

Then one real round before committing to 24/7:

```bash
.venv/bin/python main.py once --questions 3
```

## The Minecraft build workload

A second, independent graph (`build_work.py`) has LLM families write a playable
browser voxel game module by module, with every module forced through a
verification gauntlet before it can ship:

```
planner ──> produce_engine ─┐
           produce_world ───┤
           produce_player ──┤  produce = implement → syntax gate (node --check,
           produce_ui ──────┤   fallback bracket lexer) → contract check →
           produce_main ────┤   cross-model review → fix loop (≤ ARC_MAX_MODULE_RETRIES)
           produce_html ────┘
                    │
                    ▼
              assemble (fan-in) ──> integration_review ──pass──> metrics_store
                                       │ fail & rounds < ARC_MAX_INTEGRATION_ROUNDS
                                       ▼
                                  wiring_fix (re-produces only the broken modules)
```

- Producers/reviewers rotate across families (`family[(iteration + i) % 4]`,
  reviewer +2 offset) so no model ever reviews its own code.
- `integration_review` reads the **assembled files from disk**, so a module that
  claims to export `World` but doesn't actually gets caught.
- `--iterations N` runs create-then-improve cycles; each iteration starts from
  the previous iteration's files on disk and the critiques in the DB.
- Output lands in `production/minecraft/` (`index.html` + `js/*.js`), served by
  the dashboard at `/api/code`.

## Live dashboard

```bash
.venv/bin/python main.py serve              # http://localhost:8787
```

Shows both graphs as live SVGs (node colors = latest event), the model
leaderboard (tokens/latency/errors per family), the critique matrix heatmap,
build gauntlet chips, a filterable event stream (2s polling), and a code
viewer for the generated game. All state comes from `orchestrator.db` plus the
append-only event log (`logs/events.jsonl`, JSONL, rotates at 100 MB) — the
dashboard is read-only and never touches the API.

## Commands

```bash
.venv/bin/python main.py run                      # 24/7 forever (Ctrl+C stops cleanly)
.venv/bin/python main.py run --rounds 20          # stop after 20 rounds
.venv/bin/python main.py run --pipeline 3         # 3 rounds in flight at once
.venv/bin/python main.py once [--questions N]     # single round
.venv/bin/python main.py build [--iterations N]   # Minecraft build workload
.venv/bin/python main.py serve [--port P] [--db F]# dashboard
.venv/bin/python main.py status                   # DB statistics
.venv/bin/python main.py graph                    # print both graph topologies
```

All commands accept `--dry-run` (simulated model calls, separate `dry-run.db`;
the build workload also writes to `production/minecraft-dry-run/` so it can
never clobber real artifacts unless you explicitly set `ARC_BUILD_OUTPUT_DIR`)
and `-v` (debug logging: every node firing, gather waits, edge routing).

## Run 24/7 with systemd

```bash
mkdir -p ~/.config/systemd/user
cp deploy/arc-orchestrator.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now arc-orchestrator
loginctl enable-linger $USER        # keep running after logout

journalctl --user -u arc-orchestrator -f        # follow the logs
systemctl --user stop arc-orchestrator          # graceful stop
```

`Restart=always` brings it back after crashes and reboots; the supervisor marks
orphaned rounds as failed on startup, so the DB never lies. A second unit,
`deploy/arc-dashboard.service`, runs the dashboard the same way.

## Tuning (environment variables in .env)

| Variable | Default | Meaning |
|---|---|---|
| `ARC_QUESTIONS_PER_ROUND` | 10 | questions per round; >= 10 saturates all four families |
| `ARC_PIPELINE_ROUNDS` | 2 | rounds in flight simultaneously |
| `ARC_MAX_VERIFY_ROUNDS` | 3 | max synthesis attempts before shipping best effort |
| `ARC_VERIFY_PASS_SCORE` | 7.5 | verifier score (0-10) required to pass |
| `ARC_SEEDS_PER_ROUND` | 3 | new topics generated per round |
| `ARC_ROUND_COOLDOWN` | 5 | seconds between round launches |
| `ARC_STATS_INTERVAL` | 300 | seconds between stats log lines |
| `ARC_REQUEST_TIMEOUT` | 600 | per-request timeout (seconds) |
| `ARC_MAX_RETRIES` | 4 | retries per request on 429/5xx/network errors |
| `ARC_SESSION_RETRIES` | 12 | retries on 400 "concurrent session limit" (per-user caps) |
| `ARC_SESSION_BACKOFF_CAP` | 30 | max backoff per session-limit retry (seconds) |
| `ARC_LIMIT_<FAMILY>` | docs limit | override a family's semaphore (e.g. `ARC_LIMIT_KIMI=1`) |
| `ARC_EVENTS_LOG` | logs/events.jsonl | event log path (dashboard tail) |
| `ARC_BUILD_OUTPUT_DIR` | production/minecraft | where the build workload writes |
| `ARC_MAX_MODULE_RETRIES` | 3 | fix-loop attempts per module in the gauntlet |
| `ARC_MAX_INTEGRATION_ROUNDS` | 3 | wiring_fix ⇄ integration_review cycles |
| `ARC_REVIEW_PASS_SCORE` | 6.5 | cross-model review score required to ship |
| `ARC_DASHBOARD_PORT` | 8787 | dashboard port |

A round makes roughly `13 x questions` model calls (answers, critiques,
research, synthesis, verification, questions, seeds). Defaults are polite; the
per-model semaphores are the hard guarantee that you never exceed ARC's
documented concurrency limits.

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
| `config.py` | model registry + concurrency limits + tuning |
| `pool.py` | API client: per-family semaphores, streaming, websearch, retries, dry-run |
| `graph.py` | graph engine: nodes, conditional edges, fan-out/fan-in, cycles, max_steps guard |
| `work.py` | the round graph (the actual multi-model workflow) |
| `scheduler.py` | 24/7 supervisor: overlapping rounds, backoff, stats, signals |
| `store.py` | SQLite persistence: rounds, items, answers, critiques, seeds, builds |
| `build_work.py` | the Minecraft build graph + verification gauntlet |
| `events.py` | append-only JSONL event log (contextvars-tagged) |
| `dashboard.py` + `static/index.html` | read-only live dashboard |
| `main.py` | CLI: run / once / build / serve / status / graph |