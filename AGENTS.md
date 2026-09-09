# AGENTS.md — governance for the ARC multi-model orchestrator

This is the main governance document for this repository. **Every agent (human
or AI) that works in this repo reads this file first.** It states what this
repo is, which models are allowed to do what, and the hard rules the code
enforces. Detail lives in the docs under `docs/` (see [Links](#links)); this
file is normative where it says MUST/NEVER, and every rule carries the code
that enforces it. Everything here is true of the code as it exists today.

---

## 1. Mission

This repo is a **multi-model coding orchestrator**. A fleet of four models
builds software — and this repo itself — as a directed graph of small tasks:

- **Kimi-K3**, running in the **`kimi` CLI harness** (`drivers.KimiDriver`), is
  the main orchestrator/planner: it breaks a project goal into 2–6 small
  tasks, decides the graph fanout (which tasks run in parallel), the
  dependency order, the model routing (who implements what tier), and the
  reviewer pairing, via `main.py code plan` (`code_tasks.plan_tasks`).
- **GLM-5.3**, **gpt-oss-120b**, and **DeepSeek-V4-Flash**, running in
  **`opencode`** (`drivers.OpencodeDriver`), implement — plus GLM-5.3 and
  Kimi-K3 themselves take the hard implementation tasks.
- **Every implementation is gated and cross-reviewed before merge**: a
  deterministic `verify_cmd` gate runs first, then a reviewer model from the
  *other* strong harness reviews the full diff, and only then does the
  orchestrator (the only git actor) commit and merge to `main` under a
  process-wide lock.

The code workload is the primary occupant of this repo, but the same
orchestrator core (`graph.py`, `pool.py`, `events.py`, `store.py`) also runs
two other workloads: a 24/7 research-question round workload
(`work.py` + `scheduler.py`, `main.py run`) and a Minecraft-style browser-game
build workload (`build_work.py`, `main.py build`). All workloads share the
event log and the dashboard.

---

## 2. Model fleet and roles

Routing is decided at plan time (by Kimi-K3 in `main.py code plan`, or by
whoever writes a taskfile by hand) and is **enforced again by the loader**,
`code_tasks.load_taskfile`. There is no runtime triage. Full reference:
[docs/model-tiers.md](docs/model-tiers.md).

| Model | Harness | Tier | Allowed roles | Per-account API cap | Driver semaphore cap |
|---|---|---|---|---|---|
| Kimi-K3 | `kimi` CLI (`KimiDriver`) | hard | Implement, Plan, Review | 3 | 2 |
| GLM-5.3 | `opencode` (`OpencodeDriver`) | hard | Implement, Plan, Review | 4 | 3 |
| gpt-oss-120b | `opencode` (`OpencodeDriver`) | basic | **Implement only** | 10 | 8 |
| DeepSeek-V4-Flash | `opencode` (`OpencodeDriver`) | medium | **Implement only** | 10 | 8 |

- **Tiers** (`config.IMPLEMENT_TIERS`): `basic` → gpt-oss-120b (very basic /
  mechanical work only), `medium` → DeepSeek-V4-Flash (moderate work only),
  `hard` → GLM-5.3 or Kimi-K3 (multi-file reasoning, delicate design,
  architectural judgment — on top of their planning/reviewing duties).
- **Only GLM-5.3 and Kimi-K3 may plan or review.** `drivers.OpencodeDriver`
  raises `ValueError` if gpt-oss-120b or DeepSeek-V4-Flash is constructed with
  a `planner` or `reviewer` role; `KimiDriver` only accepts
  `planner|reviewer|implementer`.
- `main.py code plan` uses Kimi-K3 as the planner. The planner prompt
  (`code_tasks.plan_tasks`) instructs it to spread work across all four models
  so independent tasks run in parallel, keep tasks small (<30 min for one
  agent), add `deps` only when one task truly needs another's output, and give
  every task a meaningful `verify_cmd`.
- Thinking variants (`*-thinking-low/high/max`) and the
  `*-legacy-tool-calling` websearch models registered in `config.FAMILIES`
  belong to the research/build workloads; the code workload routes only the
  four base model names above.

---

## 3. Governance rules

These rules are normative. Each is enforced by the code cited; the pointers
are where to look when a rule surprises you.

### Rule 1 — Tier routing is mandatory and loader-enforced

A task's `model` MUST be one of `config.IMPLEMENTER_MODELS`
(`gpt-oss-120b`, `DeepSeek-V4-Flash`, `GLM-5.3`, `Kimi-K3`) and MUST match the
difficulty tier it was planned for (`config.IMPLEMENT_TIERS`).

- `code_tasks.load_taskfile` (code_tasks.py:35) raises `ValueError` if a
  task's model is not in `config.IMPLEMENTER_MODELS`.
- `drivers.OpencodeDriver.__init__` (drivers.py:215) raises `ValueError` if
  gpt-oss-120b or DeepSeek-V4-Flash is given any role other than
  `implementer`.
- The planner prompt (code_tasks.py:333) assigns implementers by tier:
  gpt-oss-120b for very basic/mechanical tasks, DeepSeek-V4-Flash for medium,
  GLM-5.3 or Kimi-K3 for hard.

Never route a basic-tier task to a strong model or a hard task to a basic one.
Tier reference: [docs/model-tiers.md](docs/model-tiers.md).

### Rule 2 — Cross-review is mandatory and loader-enforced; never same-harness self-review

Every task MUST be reviewed, and the reviewer MUST NOT share a harness with
the implementer it reviews when a strong model implemented.

- `code_tasks.load_taskfile` (code_tasks.py:41) raises `ValueError` unless
  `reviewer` is exactly `"kimi"` or `"glm"`, and again (code_tasks.py:44) if
  the implementer's family is `kimi` or `glm` and the reviewer is the same
  one: **Kimi-K3 code is reviewed by GLM-5.3 and vice versa.**
  (gpt-oss-120b / DeepSeek-V4-Flash implementations may be reviewed by
  either `kimi` or `glm` — never by themselves, since they cannot review at
  all.)
- The review node (code_tasks.py:208) instantiates `KimiDriver("reviewer")`
  or `OpencodeDriver("GLM-5.3", "reviewer")` accordingly and sends the full
  diff (`gitstore.diff_full`) with the original spec; the verdict must be
  JSON: `{"pass": true}` or `{"pass": false, "issues": [...]}`.
- A failed review sends the issues back to the implementer as feedback
  (bounded fix loop, Rule 4); nothing merges without `pass: true`
  (edge `review_<tid> -> publish_<tid>`, code_tasks.py:257).

Full pipeline contract: [docs/orchestration-contract.md](docs/orchestration-contract.md).

### Rule 3 — Every task runs in its own git worktree off the repo's `main` branch

- `gitstore.alloc` (gitstore.py:47) creates `~/worktrees/<project>/<task-id>`
  on branch `task/<task-id>` from base `main` (`config.WORKTREE_ROOT`,
  override `ARC_WORKTREE_ROOT`). A blessed clone at `~/repos/<project>` keeps
  `main` clean.
- Tasks with no `deps` are graph start nodes and run in parallel; a dependent
  task is wired `publish_<last-dep> -> alloc_<tid>` (code_tasks.py:267) so it
  allocates only after its dependency has merged.
- **The orchestrator is the only git actor** (gitstore.py docstring). Harnesses
  only write files inside their worktree; the implementation prompt
  (code_tasks.py:107) tells every implementer: *"do not git-commit (the
  orchestrator handles git); keep changes minimal and working."* The same
  applies to you: **NEVER `git commit`, `git merge`, or `git push` in a task
  worktree** — publish/merge is done for you by Rule 5.

### Rule 4 — Every task MUST define an honest `verify_cmd` gate, run before review

- The gate node (code_tasks.py:190) runs the task's `verify_cmd` as a shell
  command **in the worktree**, under `config.GATE_TIMEOUT` = **180 s**
  (override `ARC_GATE_TIMEOUT`); a timeout kills the process and fails the
  gate. Only its stdout/stderr tail (last 2000 chars) is kept.
- The gate MUST pass before review happens (edge `gate_<tid> ->
  review_<tid>` fires only `when r["passed"]`, code_tasks.py:256).
- A gate or review failure loops back to `implement` with the failure output
  as feedback while `runs <= config.MAX_FIX_ROUNDS` (3, override
  `ARC_MAX_FIX_ROUNDS`); after that the task is marked `failed`
  (code_tasks.py:243).
- The loader permits an empty `verify_cmd` (it then passes trivially,
  code_tasks.py:192) — which is exactly why this rule is normative: a
  taskfile with no honest gate is a bug. The planner prompt requires a
  "meaningful verify_cmd"; hand-written taskfiles get one too. Run the tests,
  build, or a targeted check — something whose exit code actually depends on
  the change being correct.

### Rule 5 — All publishes commit + merge under a process lock; `main` stays green

- `publish` (code_tasks.py:222) commits the worktree on `task/<tid>` with
  message `task(<tid>): <title>` and trailers `Harness`, `Model`, `Reviewer`,
  `Task-Id` (`gitstore.publish`).
- Merges are serialized by the module-level `asyncio.Lock` `_merge_lock`
  (code_tasks.py:23): `gitstore.merge_to_main` (`git merge --no-ff`) plus
  `gitstore.cleanup` (worktree + branch removal) happen one at a time, so
  concurrent task completions cannot interleave merges.
- Task status lifecycle (table `code_tasks` in `store.py`; `pending` is the
  schema default): `running` at alloc → `merged` on success, `conflict` if the
  merge raises `gitstore.GitError` (main is left untouched — that is how it
  stays green), or `failed` when fix rounds are exhausted. Watch them with
  `main.py code status`.
- Because merges are serialized and dependent allocs wait for
  `publish_<dep>`, every task branches from a `main` that already contains
  all of its dependencies (comment at code_tasks.py:157).

### Rule 6 — Concurrency caps are two-layer; know both before launching anything

| Layer | Where | gpt-oss | deepseek | glm | kimi | Override |
|---|---|---|---|---|---|---|
| Per-account API caps | `config.FAMILIES[*].limit` (ARC rejects over-limit per key) | 10 | 10 | 4 | 3 | `ARC_LIMIT_<FAMILY>` |
| Driver semaphores | `config._MODEL_DRIVER_CAP` via `drivers._gate` → `config.driver_limit` | 8 | 8 | 3 | 2 | `ARC_DRIVER_LIMIT_<FAMILY>` |

- The **account caps are per API key, not per process** — other agents and
  interactive sessions share them (config.py:88).
- The **driver semaphores bound concurrent `kimi`/`opencode` harness
  instances in this process** and sit deliberately below the account caps to
  reserve headroom for interactive use (config.py:108, drivers.py:47).
- For the research workload, `pool.py` additionally enforces per-family
  `asyncio.Semaphore(config.family_limit(f))` client-side.
- Full explanation, including how the ARC API rejects over-limit requests:
  [docs/concurrency-limits.md](docs/concurrency-limits.md).

### Rule 7 — Evidence is mandatory: every agent run leaves a live transcript and events

- `drivers.Driver._once` (drivers.py:145) **streams harness stdout live** to
  `logs/harness/<task_id>-<role>-<attempt>.jsonl` (`TRANSCRIPT_DIR` =
  `logs/harness`; the dashboard tails these mid-run). `task_id` is `plan` for
  the planner and `<tid>-x<attempt>` for fix-round attempts, so a full retry
  history is kept. Each run also gets a `harness_runs` row in
  `orchestrator.db` via `store.save_harness_run` (harness, model, role,
  attempt, exit code, transcript path, seconds, review verdict).
- All state transitions emit events to `logs/events.jsonl`
  (`config.EVENTS_LOG`; append-only JSONL with workload context, rotated at
  100 MiB to `events.jsonl.1`): `driver.start` / `driver.done` /
  `driver.error`, `worktree.alloc`, `task.gate`, `task.reviewed`,
  `task.merged`, `task.failed`.
- Harness-level resilience: `config.DRIVER_TIMEOUT` = 900 s per harness
  invocation (override `ARC_DRIVER_TIMEOUT`), retries with exponential
  backoff (capped at 30 s) up to `config.MAX_RETRIES` = 4.
- If you cannot show a transcript or an event for a claim about a run, do not
  make the claim.

### Rule 8 — Dry-run before every run

- Before executing a taskfile for real, run
  `main.py code run <taskfile> --dry-run` (main.py:244). It runs the full
  loader validation (Rule 1/2 rules, unknown deps, dependency cycles via
  `_topo`) and prints the resolved DAG (`describe`), calling **no models and
  touching no git**; it even uses a separate `dry-run.db` (main.py:21).
- `code plan` also prints the `describe` summary of the file it just wrote —
  read it. A dry-run that surprises you is a taskfile bug; fix the taskfile,
  not the dry-run.

---

## 4. Repo file map

Top-level Python modules (one role each):

| File | Role |
|---|---|
| `build_work.py` | Minecraft-style browser-game build workload: planner → 6 parallel module producers (each an implement → syntax gate → contract check → cross-model review → fix gauntlet) → assemble → bounded integration-review cycle |
| `code_tasks.py` | The multi-harness code workload: taskfile loader/validation, the Kimi-K3 planner prompt (`plan_tasks`), per-task chain `alloc → implement → gate → review → publish/fail`, fix-loop edges, serialized merge lock |
| `config.py` | Single source of truth: model families + caps, tier maps, driver caps, timeouts, paths — every `ARC_*` env override lives here |
| `dashboard.py` | Dashboard server (`main.py serve`, default port 8787): static UI + JSON APIs over `orchestrator.db`, `logs/events.jsonl` and live harness transcripts — **not read-only**: `do_POST` (dashboard.py:999) serves `/api/projects/create`, which spawns `main.py code plan` (goal mode) or writes taskfiles into `~/tasks` directly (dashboard.py:840-842), and `/api/projects/run`, which launches `main.py code run` (optionally `--dry-run`) subprocesses via `subprocess.Popen` (dashboard.py:768-770) |
| `drivers.py` | Headless CLI harness drivers: `KimiDriver` (`kimi` CLI) and `OpencodeDriver` (`opencode`); per-model semaphores, retries, timeouts, live transcript streaming to `logs/harness/` |
| `events.py` | Append-only JSONL event log `logs/events.jsonl` with contextvars attribution (`workload`/`round`/`iteration`/`module`) and 100 MiB rotation |
| `gitstore.py` | The only git actor: blessed clone `~/repos/<project>`, worktree `alloc`/`publish`/`merge_to_main`/`cleanup` on `task/<id>` branches (60 s per-git-op timeout) |
| `graph.py` | Generic async DAG engine: named nodes, conditional edges (`when=`), gather nodes, `max_steps` bound |
| `main.py` | CLI entry point: `run`, `once`, `status`, `graph`, `build`, `serve`, and `code {plan,run,status}` |
| `pool.py` | `AsyncOpenAI` request pool for the research workload: per-family semaphores, retry/backoff, token accounting |
| `scheduler.py` | `Supervisor`: runs research rounds continuously (pipeline concurrency, round cooldown, periodic stats) |
| `store.py` | sqlite persistence: `rounds`, `items`, `answers`, `seeds`, `builds`, `build_modules`, `harness_runs`, `code_tasks` |
| `work.py` | Research round graph (questions → synthesize → verify → seeds) and the `Roles` family-rotation |

Everything else at the top level:

| Path | Role |
|---|---|
| `static/index.html` | Dashboard web UI (desktop) |
| `static/usage.html` | Dashboard usage/tokens view |
| `static/phone.html` | Small-screen dashboard page (add `/phone.html` to the URL) |
| `start.sh` / `stop.sh` | Start/stop the dashboard (`nohup .venv/bin/python main.py serve` → `logs/server.log`; `pkill -f "main\.py serve"` — never touches an orchestrator process) |
| `docs/` | Detail reference docs — see [Links](#links) |
| `deploy/` | systemd units: `arc-orchestrator.service`, `arc-dashboard.service` |
| `production/minecraft` | Build-workload output dir (`config.BUILD_OUTPUT_DIR`) |
| `requirements.txt` | Python dependencies (openai, python-dotenv) — install into `.venv`; the system `python3` lacks them |

State and external directories (not in git):

| Path | Role |
|---|---|
| `~/tasks` | Taskfiles (`config.TASKS_DIR`, override `ARC_TASKS_DIR`): written by `main.py code plan`, read/edited by hand, executed by `main.py code run` |
| `~/worktrees` | Per-task git worktrees (`config.WORKTREE_ROOT`, override `ARC_WORKTREE_ROOT`): `<project>/<task-id>` on branch `task/<task-id>` |
| `~/repos` | Blessed clones (`gitstore`): the pristine `main` each project merges into |
| `logs/` | `events.jsonl` (all events), `harness/` (live per-run transcripts), `server.log` (dashboard) |
| `orchestrator.db` | sqlite state (`config.DB_PATH`, override `ARC_DB_PATH`): code-task statuses, harness runs, research rounds/builds |

---

## 5. Workflow (short version)

The full operator runbook, with troubleshooting, is
[docs/runbook.md](docs/runbook.md). The loop:

1. **Plan** — `.venv/bin/python main.py code plan "<goal>" /path/to/repo`
   (Kimi-K3 drafts a taskfile into `~/tasks/<slug>.json` and prints the
   resolved DAG).
2. **Review the taskfile** — read and hand-edit `~/tasks/<slug>.json`:
   check tier routing, reviewer pairing, deps, and that every `verify_cmd` is
   honest. Schema reference: [docs/taskfile-schema.md](docs/taskfile-schema.md).
3. **Dry-run** — `.venv/bin/python main.py code run <taskfile> --dry-run`
   (validates everything; no models, no git — Rule 8).
4. **Run** — `.venv/bin/python main.py code run <taskfile>`; check
   `main.py code status` for task and harness-run stats.
5. **Watch** — `./start.sh`, then open `http://localhost:8787`
   (`http://<lan-ip>:8787` from laptop/phone, `/phone.html` on phones).

---

## Links

- [docs/orchestration-contract.md](docs/orchestration-contract.md) — the
  end-to-end per-task pipeline contract (alloc → implement → gate → review →
  publish → merge, fix loops, failure semantics)
- [docs/model-tiers.md](docs/model-tiers.md) — model fleet, tiers, and
  allowed roles in full
- [docs/concurrency-limits.md](docs/concurrency-limits.md) — the two layers
  of concurrency caps and their env overrides
- [docs/taskfile-schema.md](docs/taskfile-schema.md) — taskfile JSON reference
  and validation rules
- [docs/runbook.md](docs/runbook.md) — operator runbook: dashboard,
  plan → dry-run → run, troubleshooting
- [README.md](README.md) — project overview, dashboard quick start, 24/7
  setup