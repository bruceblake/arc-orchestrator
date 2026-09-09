# Operator runbook

This is the operator's runbook for the ARC multi-model orchestrator at
`/home/proxyie/arc-orchestrator`. All commands assume you are on that machine,
in a shell, as the user that owns the repo. Everything below is checked
against `main.py`, `dashboard.py`, `config.py`, `code_tasks.py`, `gitstore.py`,
`drivers.py`, `start.sh` and `stop.sh`.

## 1. Starting and stopping the dashboard

The dashboard is a separate process from the orchestrator. To (re)start it on
port 8787:

```bash
cd /home/proxyie/arc-orchestrator && ./stop.sh && ./start.sh
```

- `start.sh` reads `ARC_DASHBOARD_PORT` from `.env` and defaults to `8787`; it
  uses `nohup .venv/bin/python main.py serve --port "$PORT" >> logs/server.log`
  and prints the exact LAN/Tailscale URLs to open.
- `stop.sh` is `pkill -f "main\.py serve"` (it will NOT kill a `main.py run`
  orchestrator process — only the `serve` one).
- Always use the project venv when invoking Python directly, e.g.
  `cd /home/proxyie/arc-orchestrator && .venv/bin/python main.py serve`.
  The **system `python3` lacks the dependencies** (`openai`, `python-dotenv`)
  used by this repo, so do not use it for anything here.
- **NEVER touch ports `4096`** (the opencode web server) **or `7681`**
  (ttyd). Those are for other agents; the dashboard owns port 8787.

Manual equivalent of `start.sh`:

```bash
cd /home/proxyie/arc-orchestrator
nohup .venv/bin/python main.py serve --port 8787 >> logs/server.log 2>&1 &
```

The dashboard is **read-only**: it only reads `orchestrator.db` and
`logs/events.jsonl`, plus the harness transcripts under `logs/harness/`.

## 2. Plan -> run workflow

The code workload is a DAG of coding-agent tasks described in a JSON task
file. Two entry points build one: Kimi-K3 as the planner (CLI) or the
dashboard's "Plan with Kimi-K3" tab.

### 2.1 Plan

```bash
cd /home/proxyie/arc-orchestrator
.venv/bin/python main.py code plan "<goal>" /path/to/repo
```

- `code plan` asks Kimi-K3 (the planner) to break the goal into 2–8 small
  tasks and writes the task file to `~/tasks/<goal-slug>.json`, then prints its
  path plus a `describe(...)` summary of the resolved DAG
  (implement/review pairing, deps, verify gates).
- Review the generated file in `~/tasks/` and edit it by hand if needed
  (schema rules are in `docs/taskfile-schema.md`).

### 2.2 Always dry-run first

```bash
cd /home/proxyie/arc-orchestrator
.venv/bin/python main.py code run ~/tasks/<taskfile>.json --dry-run
```

- `--dry-run` **loads and validates the task file** (`code_tasks.load_taskfile`
  rejects bad model/reviewer/unknown-deps/cycles), prints the resolved DAG via
  `describe(...)`, and then prints `dry-run ok`. It makes **no model calls and
  no git mutations**.
- Read the validation output carefully before running for real: mismatch in the
  printed DAG (wrong implementer, wrong reviewer, missing deps) is exactly what
  causes a failing run later.

### 2.3 Run

```bash
cd /home/proxyie/arc-orchestrator
.venv/bin/python main.py code run ~/tasks/<taskfile>.json
```

- This builds the graph and executes every task: alloc worktree -> implement ->
  verify gate -> cross-family review -> publish/merge -> cleanup.
- Watch progress with `.venv/bin/python main.py code status` (dumps the
  `code_tasks` and `harness_runs` tables as JSON), and the dashboard live at
  `http://localhost:8787/`.

## 3. Dashboard map

Open `http://localhost:8787/` (or the LAN/Tailscale URL `start.sh` prints).

| Page/API | What it shows |
|---|---|
| `/` | Projects console: create a project, run a project, repo grouping, and live DAG nodes colored by status (`pending` / `running` / `merged` / `conflict` / `failed`). |
| `/usage.html` | Usage page with ranges `1h` / `24h` / `7d` / `all`. |
| `/api/projects` | List of project task files with statuses, per-task DAG + progress. |
| `/api/project?file=<name>.json` | Single project detail: tasks, `code_tasks` rows, harness runs, event tail, git info. |
| `/api/agents` | Live agents across all layers (pool requests, `driver:*` harness runs, kimi-code sessions) plus the launched-run registry. |
| `/api/transcript?file=<name>.jsonl&tail=N` | Tail of one harness transcript streamed live from `logs/harness/*.jsonl`. |
| `/api/usage?range=...` | Usage aggregates (models, families, totals, series, inflight). |
| `/api/graphs` | Static round/build graph topologies. |

Clicking a running agent (a `driver:*` row under `/api/agents`) shows its live
transcript via `/api/transcript`.

## 4. File and log locations

All paths are under `/home/proxyie/arc-orchestrator` unless shown absolute.

| Path | Purpose |
|---|---|
| `~/tasks` | Task files (taskfile JSON) incl. planner output. |
| `~/worktrees/<project>/<task-id>` | Per-task git worktrees; `gitstore.py` branches `task/<task-id>` from `main` and removes them after a clean merge. |
| `logs/events.jsonl` | Append-only event log the dashboard tails. |
| `logs/harness/` | One JSONL transcript per harness firing, named `<task-id>-<role>-<attempt>.jsonl` (e.g. `foo-x2-implementer-1.jsonl`). |
| `logs/server.log` | Dashboard stdout/stderr. |
| `orchestrator.db` | SQLite store; the code workload lives in tables `code_tasks` and `harness_runs`. |
| `config.py` | All the knobs: `DRIVER_TIMEOUT`, `GATE_TIMEOUT`, `MAX_FIX_ROUNDS`, `WORKTREE_ROOT`, `TASKS_DIR`, driver/model caps. |

## 5. Recovery

### Dry-run validation error

If `code run --dry-run` fails to print `dry-run ok`, the task file is invalid.
Fix the task file per `docs/taskfile-schema.md` (valid `model`, reviewer
`kimi`/`glm`, cross-family reviewer, known deps, no cycles), re-run the
dry-run, then re-run.

### Task failed (`exhausted fix rounds`)

The implementer failed the verify gate or review `MAX_FIX_ROUNDS` (default 3)
times, so the task is marked `failed`. To debug:

```bash
ls -t /home/proxyie/arc-orchestrator/logs/harness/<task>-x*-implementer-*.jsonl
```

Read the latest implementer transcript and the reviewer verdict. Fix the task's
`prompt`/`verify_cmd` in `~/tasks/<taskfile>.json`, then re-run.

**Actual re-run behaviour (verified in `main.py` + `code_tasks.py`):** re-running
a task file does **NOT** resume or retry only the previously-failed tasks.
`code_tasks.build_code_graph` rebuilds the whole graph from the file and
`gitstore.alloc` **force-removes and recreates every worktree** for every task
in the file, so a re-run re-executes the **entire task set from scratch** —
including tasks that already `merged`. There is no "skip already-merged/skip
failed" resume logic. If you only want to re-run a subset, hand-write a
reduced task file listing just those tasks (and their deps) and run that.

### Task conflict

When a merge collides, `publish` marks the task `conflict` (`error` holds the
merge error). The worktree and `task/<task-id>` branch are **left in place**,
so you can resolve on disk:

```bash
ls ~/worktrees/<project>/<conflicted-task>     # inspect the worktree
cd <repo>                                      # the blessed clone named by the task file's project.repo
git checkout main && git merge task/<conflicted-task>   # finish the merge manually
```

Resolve the conflict, commit the merge, and (optionally) `git worktree remove`
the worktree and `git branch -d task/<conflicted-task>`. After that, either
treat the task as done (it now lives on `main`) or delete it from the task
file before re-running the rest.

### Stuck running

Check for orphaned harness processes:

```bash
ps aux | grep -E 'kimi|opencode'
```

The orchestrator reaps a hung driver itself: `drivers.py` kills the child
after `DRIVER_TIMEOUT` (default **900s**) and reports a `driver.error`. A
`driver.error` counts against `MAX_RETRIES` (default 4) before the task fails.
If a `kimi`/`opencode` process is still alive past that, it is either running
a fresh attempt, retrying with backoff, or genuinely orphaned — kill it with
`kill <pid>` only after confirming it is not the current active attempt on the
dashboard.

## 6. Safety rules

- **Never hand-edit a worktree mid-run.** The orchestrator is the only git
  actor and harnesses write files inside their worktree; the implementer reads
  the worktree as-is, and `gitstore.alloc` **force-removes** it on a re-run, so
  any manual edit is either clobbered or races the agent.
- **Publish/merge lock (verified in `gitstore.py` + `code_tasks.py`):**
  `gitstore.py` itself holds **no lock**. The serialization is the in-process
  `_merge_lock = asyncio.Lock()` in `code_tasks.py`, taken around
  `gitstore.merge_to_main` + `cleanup`. It serializes merges **within a single
  orchestrator process only**; two concurrent `main.py code run` processes
  targeting the same repo do **not** share it and can interleave `git checkout
  main` / `merge --no-ff`. The dashboard guards only against launching two runs
  of the **same** task file at once (`/api/projects/run` returns 409 if that
  task file already has a running process). **Rule: run at most one
  `main.py code run` against a given repo at a time.**
- **Dry-run first, always.** `code run --dry-run` is free (no model calls, no
  git) and catches schema/DAG errors before they burn a merge conflict or a fix
  round.

## Cross-references

- [`../AGENTS.md`](../AGENTS.md) — the agent tasking/behaviour contract.
- [`orchestration-contract.md`](orchestration-contract.md) — how Kimi-K3 plans and the graph runner executes it.
- [`model-tiers.md`](model-tiers.md) — model routing tiers and the cross-review matrix.
- [`concurrency-limits.md`](concurrency-limits.md) — per-model caps and the driver semaphores.
- [`taskfile-schema.md`](taskfile-schema.md) — exact task-file schema and validation rules.
