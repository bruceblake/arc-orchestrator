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
- **Re-running this command is also the retry/resume path** — see §5 "Retrying
  / resuming": already-`merged` tasks are skipped, `failed` tasks resume one
  escalation tier up, `conflict` tasks try repair first.
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

### Retrying / resuming

**Retrying anything is always: re-run the same task file.**

```bash
cd /home/proxyie/arc-orchestrator
.venv/bin/python main.py code run ~/tasks/<taskfile>.json
```

A `code run` on a task file that already has rows in the `code_tasks` table
resumes the same project (`code_tasks.build_code_graph`) — it never starts a
new one:

- `merged` tasks are **skipped** — their subgraph is replaced by a stub
  publish node returning `merged`, so dependents treat them as satisfied and
  no models or git ops are wasted.
- `failed` tasks are **retried one escalation tier higher** than the model in
  their `code_tasks` row (`config.ESCALATION_PATH`, default
  `gpt-oss-120b → DeepSeek-V4-Flash → GLM-5.3 → Kimi-K3`) with a full fresh
  fix budget — the old run already proved that model insufficient. Within a
  run the same tier-escalation happens live: a task that exhausts
  `MAX_FIX_ROUNDS` at one model escalates instead of failing, and the final
  failure message names the last model tried (`exhausted escalation up to
  Kimi-K3`).
- `conflict` tasks are **retried at the same model** (a merge conflict is not
  a capability signal) and their publish node first tries **conflict repair**:
  if branch `task/<task-id>` still has commits ahead of `main` — the reviewed,
  gate-passing commit survived the failed run (`gitstore.branch_ahead`) — it
  merges that branch directly under the merge lock instead of re-running
  implement+review. Only if repair fails does the task fall back to full
  re-execution.
- stale `running` rows (crashed-run leftovers) are marked `failed` at
  startup, **scoped to this task file** — it is safe to start a run while
  other projects' rows are idle.

Startup prints a resume plan (skipped / retried / escalated lists) and emits a
`run.resume` event `{skipped_merged, retried, escalated_on_resume}`.

Re-running is safe because `gitstore.alloc` always **resets branch
`task/<task-id>` to the base ref** on (re)alloc — a failed attempt's rejected
work never leaks into a retry — and merges are stash-tolerant (see
"Task conflict"). Never hand-write a reduced task file to retry a subset;
that was the old workaround, it is no longer needed.

### Task failed (`exhausted escalation`)

The implementer failed the verify gate or review `MAX_FIX_ROUNDS` (default 3)
times **at every tier of `config.ESCALATION_PATH`**, so the task is marked
`failed` — the failure message names the last model tried (`exhausted
escalation up to Kimi-K3`). To debug:

```bash
ls -t /home/proxyie/arc-orchestrator/logs/harness/<task>-x*-implementer-*.jsonl
```

Read the latest implementer transcript and the reviewer verdict (each tier
records its own `harness_runs` rows with the model actually used). Fix the
task's `prompt`/`verify_cmd` in `~/tasks/<taskfile>.json`, then **re-run the
same task file** — it resumes (see "Retrying / resuming"): merged tasks are
skipped and this task restarts one escalation tier higher than its recorded
model with a fresh fix budget.

### Task conflict

When a merge collides, `publish` marks the task `conflict` (`error` holds the
merge error; event `task.conflict` — distinct from `task.failed`). The
worktree and `task/<task-id>` branch are **left in place**, so the next
`code run` of the same task file tries **repair** first: if
`task/<task-id>` still has commits ahead of `main` — the reviewed,
gate-passing commit survived (`gitstore.branch_ahead`) — the publish node
merges that branch directly under the merge lock instead of re-running
implement+review. Only if repair fails does the task fall back to a full
re-execution (alloc resets the branch to `main`).

Merges also tolerate a **dirty blessed-repo working tree** (the blessed repo
is usually your working copy too): `gitstore.merge_to_main` path-scoped
`git stash push`'s files that the merge would update and that have
uncommitted local edits, then `git stash pop`'s them after. If the pop
conflicts, the merge stays landed and the error tells you to resolve the
stash (`git stash list` / `git stash show -p`) — no more "Your local
changes ... would be overwritten" aborts just because someone was mid-edit.

You can still resolve on disk:

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
  the worktree as-is, and `gitstore.alloc` **resets its `task/<task-id>`
  branch to the base ref** on a re-run, so any manual edit is either clobbered
  or races the agent.
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
