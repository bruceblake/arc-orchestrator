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

### 1.1 Pre-flight checks (`main.py doctor`)

```bash
cd /home/proxyie/arc-orchestrator
.venv/bin/python main.py doctor
```

- Runs the passive checks that catch misconfiguration *before* a run burns
  model calls on it: kimi plan mode is off (plan mode silently leaves agents
  researching instead of editing), `ARC_API_KEY` is set and not the
  placeholder, the `kimi` and `opencode` harness binaries are on `PATH`, the
  worktree and tasks directories are creatable, the timeout invariants hold
  (`DRIVER_LEASE_TTL > DRIVER_TIMEOUT > DRIVER_IDLE_TIMEOUT`), and there are
  no stale `running` task rows.
- Exits **non-zero on any failure**, so it works in scripts and as a habit:
  run it after installing, after editing `.env` or `config.py`, and as the
  first step whenever "runs fail for no obvious reason".

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

### 2.4 Seeing every project at a glance (`code list`)

```bash
cd /home/proxyie/arc-orchestrator
.venv/bin/python main.py code list            # or: code list --json
```

- Prints **one row per task file in `~/tasks/`**: how many tasks it declares,
  how many are `merged`, the status counts, and the last activity time — the
  per-project merge progress across everything you have ever run or planned.
- Reach for it when you come back after a break and want to know which
  projects are done, half-merged, or stuck before deciding what to resume.
- `--json` emits the same data as JSON for scripting (e.g. feeding a status
  check into another tool); `--db` points it at a non-default database.

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

### Enabling GitHub PR flow

To have the orchestrator open a GitHub Pull Request after a successful merge, enable the PR flow:

1. **Create a GitHub repository** for the project (or use an existing one).
2. Add the remote to the local blessed clone:
   ```bash
   git -C ~/repos/<project> remote add origin <url>
   ```
3. Install the GitHub CLI if not already present (`apt install gh` or similar).
4. Authenticate the CLI:
   ```bash
   gh auth login
   ```
   Follow the prompts to log in with your GitHub account and grant access.
5. Verify that the CLI is authenticated:
   ```bash
   gh auth status
   ```
   It should report a logged-in user.
   Verify the git remote with:
   ```bash
   git -C ~/repos/<project> remote -v
   ```
6. Confirm it is on: the project detail page shows a **PR readiness** hint line on the git block — `PRs on: <remote>` when ready, or `PRs off: <reason>` otherwise.
7. Run a task as usual. On success, the orchestrator will emit a `task.pr_opened` event and the PR URL appears in the logs. If the remote is missing or the CLI is unauthenticated, the run will emit `task.pr_skipped` but the local merge still lands.

The PR flow is additive — it never blocks the local merge. Enabling it simply adds a best-effort push and PR creation after the merge lock releases.

## 4. File and log locations

All paths are under `/home/proxyie/arc-orchestrator` unless shown absolute.

| Path | Purpose |
|---|---|
| `~/tasks` | Task files (taskfile JSON) incl. planner output. |
| `~/worktrees/<project>/<task-id>` | Per-task git worktrees; `gitstore.py` branches `task/<task-id>` from `main` and removes them after a clean merge. |
| `logs/events.jsonl` | Append-only event log the dashboard tails. |
| `logs/harness/` | One JSONL transcript per harness firing, named `<task-id>-<role>-<attempt>.jsonl` (e.g. `foo-x2-implementer-1.jsonl`). |
| `logs/gates/` | Verify‑gate output (`<task>-x<attempt>.log`) kept out of version control. |
| `logs/server.log` | Dashboard stdout/stderr. |
| `orchestrator.db` | SQLite store; the code workload lives in tables `code_tasks` and `harness_runs`. |
| `config.py` | All the knobs: `DRIVER_TIMEOUT`, `GATE_TIMEOUT`, `MAX_FIX_ROUNDS`, `WORKTREE_ROOT`, `TASKS_DIR`, driver/model caps. |

Verify‑gate output is written to `logs/gates/<task>-x<attempt>.log`. These logs capture the stdout/stderr of each gate run and are intentionally excluded from version control via `.gitignore`.

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
- `failed` tasks are retried, and **whether they escalate depends on why they
  failed** (`code_tasks._is_capability_failure`):
  - A **capability failure** — the row's `error` says the model exhausted its
    fix rounds or escalation path — retries **one tier higher** in
    `config.ESCALATION_PATH` (default
    `gpt-oss-120b → DeepSeek-V4-Flash → GLM-5.3 → Kimi-K3`) with a fresh fix
    budget, because the old run proved that model insufficient.
  - An **infrastructure failure** — the run process was killed, the graph was
    cancelled, the harness crashed — retries at the **same tier**. Being
    interrupted says nothing about the model. Escalating on it used to send
    every interrupted task to Kimi-K3, the scarcest tier (driver cap 2): one
    killed queue put four tasks there at once, exceeded the account cap, and
    every request came back as an instant `provider.api_error: 400`.

  Within a run the same tier-escalation happens live: a task that exhausts
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
  other projects' rows are idle. The reason recorded is an infrastructure
  one, so these rows retry at the same tier (above).

Startup prints a resume plan (skipped / retried / escalated lists) and emits a
`run.resume` event `{skipped_merged, retried, escalated_on_resume}`.
`escalated_on_resume` reports tiers higher than **where the task last ran**,
not higher than the task file's original routing.

A run now also cleans up after itself on every exit path — clean finish, node
crash, Ctrl-C, or SIGTERM: unfinished tasks are marked `failed`, the driver
leases it held are released, and its harness child processes are killed. See
"Reaping orphans" below for wreckage left by runs that predate this.

Re-running is safe because `gitstore.alloc` always **resets branch
`task/<task-id>` to the base ref** on (re)alloc — a failed attempt's rejected
work never leaks into a retry — and merges are stash-tolerant (see
"Task conflict"). Never hand-write a reduced task file to retry a subset;
that was the old workaround, it is no longer needed.

### Per-task retry

Re-running the task file is the normal retry path above, but when a project has
already merged most of its tasks it re-runs the whole DAG anyway. To re-execute
**one** task without touching the rest of the project, reset that task to
`pending`:

```bash
cd /home/proxyie/arc-orchestrator
curl -s -X POST localhost:8787/api/projects/retry-task \
  -H 'Content-Type: application/json' \
  -d '{"file": "<taskfile>.json", "task": "<task-id>"}'
```

- It resets **only that task's** `code_tasks` row to `pending`; every other
  task keeps its recorded status, so the next `code run` of the same task file
  re-executes just that task (and its dependencies, which are already
  satisfied) instead of re-running the whole project.
- Use it when you have looked at one task's failure, fixed the underlying
  cause (a bad taskfile, a flaky dependency, a one-off crash), and want a
  clean single-task re-run — the same effect as hand-writing a reduced task
  file, without the workaround.
- It is **refused while a run owns the task file**: if any `code run` process
  is live for that task file the endpoint returns `409` and tells you to stop
  the run first (`/api/projects/stop`). Reset a task before the run starts,
  never mid-run.
- It emits a `task.reset` event `{taskfile, task}` in `logs/events.jsonl`, and
  the dashboard project detail shows the task back at `pending` immediately.

### Agents produce output but change no files (plan mode)

The single most damaging misconfiguration found so far, and it looks exactly
like a stall from the outside: the agent runs for minutes, writes 100KB+ of
transcript, and leaves the worktree completely clean.

**Cause.** `~/.kimi-code/config.toml` with:

```toml
default_plan_mode = true
```

Plan mode makes an agent research and propose rather than edit. Leaving it
requires approving `ExitPlanMode` — and a headless `kimi -p` run has nobody to
approve anything. Measured on this box: **182 of 206 sessions entered plan
mode; only 44 ever left.** Most fleet agents were researching, writing plan
files, and changing nothing.

**Fix.** `default_plan_mode = false`. There is no CLI override (`--plan`
enables it; there is no `--no-plan`) and no config-path env var — kimi ships as
a compiled binary — so this is a global setting. Interactive plan mode is
still available on demand with `kimi --plan`.

`main.py code run` now refuses to start when it detects plan mode is on,
naming the config file (`--force` overrides), and `tests/test_config.py` fails
if this box is ever reconfigured back.

**How to spot it** without reading config: the implementer transcript shows
`ExitPlanMode` among the tool calls, or the worktree has zero dirty files
after a long run:

```bash
git -C ~/worktrees/<repo>/<task> status --porcelain | wc -l
```

### A harness went quiet (`driver.stalled`)

**Read this before shortening `ARC_DRIVER_IDLE_TIMEOUT`.** Silence is not a
hang. ARC *queues* requests rather than refusing them, and time-to-first-token
is exactly what stdout silence measures.

Measured over 1927 completed steps from the session wire logs:

| input tokens | median TTFT | p90 | max |
| --- | --- | --- | --- |
| 0-20k | 1.0s | 3.5s | 14.1s |
| 35-50k | 1.0s | 7.4s | 238.1s |
| 65-90k | 1.1s | 15.8s | 262.5s |
| 90k+ | 1.0s | 16.4s | **308.9s** |

The median never moves. The **tail** grows with context — and those slow steps
then stream their response normally in ~0.3s. They were healthy all along.

So an idle timeout below that tail destroys finished work. Per-task
probability of killing a healthy request at >=40k context:

| idle timeout | per step | median task | p90 task |
| --- | --- | --- | --- |
| 60s | 0.91% | 5.3% | 18.1% |
| 120s | 0.41% | 2.4% | 8.7% |
| 300s | 0.08% | 0.5% | 1.8% |
| **420s** (default) | 0.00% | 0.0% | 0.0% |

These are **lower bounds**: a step we killed leaves no `step.end`, so it is
absent from the telemetry entirely and the real tail is worse.

`config.DRIVER_TIMEOUT` (2700s) bounds the total cost, so a genuinely dead
request costs one idle window, not the whole budget.

**Forensics.** Every stall event still records, gathered from `/proc` before
the kill:

| field | meaning |
| --- | --- |
| `state` / `cpu_delta_s` | `S`/`D` with near-zero CPU = waiting, not spinning |
| `blocked` | the above, as a boolean |
| `wire.awaiting_api` / `waiting_s` | a request is outstanding, and for how long |
| `last_activity` | the last few tool calls before it went quiet |
| `bytes` / `records` | how much it produced first |

Note what these can and cannot tell you. `blocked: true` with
`awaiting_api: true` means the harness is waiting on the server — it does
**not** distinguish "queued and about to answer" from "never coming back", and
a harness we SIGKILL also ends on an unanswered request. `waiting_s` against
the table above is the only real discriminator.

What ARC does at its concurrency cap is **reject instantly**: 5 concurrent
Kimi-K3 requests against a cap of 3 gave 3 successes and 2
`400 {"detail": "concurrent session limit reached"}` in 0.2s. Earlier notes in
this repo claiming ARC "holds rejected requests open indefinitely" are wrong.
Those 400s are real and are what the capacity backoff is for; the long
silences are queueing, which is a different thing.

`driver.progress` heartbeats (every `ARC_DRIVER_PROGRESS_INTERVAL`, default
60s) carry the same `/proc` sample while an agent is healthy, so the Fleet
panel can show "quiet 90s" on a live agent without it meaning trouble.

### Terminated requests (the real failure mode)

ARC terminates long-running requests. kimi-code's session log records them —
the wire log does not, which is why this went unexplained for so long:

```
~/.kimi-code/sessions/<session>/logs/kimi-code.log
  WARN llm request failed turnStep=0.12 model=arc/kimi-k3-fleet
       errorName=APIConnectionError errorMessage=terminated
```

They arrive after roughly 310s in flight. It is **not** a hard ceiling — 27
responses succeeded with decode times over 300s, one at 828.9s — so treat it
as a probability that rises with how long a request stays open, not a cutoff.

**Response length is the variable you control**, and it dominates:

| workload | terminated |
| --- | --- |
| all sessions baseline | 179 / 2144 = **8.3%** |
| a task prompted to rewrite a 504-line file | 6 / 18 = **33%** |

Each termination costs ~5 minutes of retry. That task burned 30 of its
45-minute budget on them and never finished. Median decode is ~52 ms/token, so
a 9000-token response is already ~8 minutes in flight.

Mitigations, in order of effect:

1. **Never ask for a whole-file rewrite.** Write task prompts that name the
   specific change. `code_tasks._impl_prompt` now instructs the implementer to
   make targeted edits even when the task sounds like a rewrite.
2. Keep tasks small enough that no single response needs to be long.
3. `ARC_DRIVER_TIMEOUT` (2700s) bounds a task that is retrying productively;
   it is a backstop, not a cure — raising it buys time at ~5 min per retry.

Note kimi requests `maxTokens = 131072` (derived from `max_context_size`; no
separate output cap exists in its config — `max_output_tokens`, `max_tokens`
and `output_tokens` were all tested and none change it). So generation length
is bounded only by the model deciding to stop.

### Context budget (per harness — they fail differently)

Both harnesses ship configured for a **131072-token** context and compact near
that ceiling — kimi at `max_context_size - reserved_context_size`, opencode at
`limit.context * compaction.threshold`. Neither reached it before requests grew
too large to come back. But the right response differs, because their
compaction differs (measured 2026-09-09):

| harness | compaction | budget | why |
| --- | --- | --- | --- |
| opencode | **works** — fired twice inside one GLM-5.3 run, which then carried on to 621KB (vs ~350KB at the default, where it never compacted) | `ARC_OPENCODE_CONTEXT`, default **65536** | a smaller budget keeps each request small enough to come back |
| kimi | **never completes** — 20 `full_compaction.begin` across the whole session history, 0 `full_compaction.end`, interactive sessions included | `ARC_KIMI_CONTEXT`, default **131072** | lowering it only reaches that dead end sooner (tried, measured, reverted) |

How each budget is applied — interactive sessions keep the harness defaults:

| harness | mechanism |
| --- | --- |
| kimi | alias `arc/kimi-k3-fleet` in `~/.kimi-code/config.toml` (same `model = "Kimi-K3"`, its own `max_context_size`), passed as `-m` |
| opencode | generated `~/.config/opencode/opencode-fleet.json`, selected per-process via `$OPENCODE_CONFIG` |

opencode needs the whole-file approach because it sends the model **key** to
the API — a differently-keyed alias comes back `{"detail":"Model not found"}`.
Its fleet config is regenerated from your own `opencode.json` whenever that
changes, so provider settings and API keys stay in one place. If the kimi alias
is missing, the driver falls back to the default model rather than failing.
`ARC_USE_FLEET_ALIASES=0` disables both.

**None of this is a cure.** Compaction lets an opencode task run roughly twice
as far; it still stalled eventually. The lever that actually reduces risk is
not growing the context, which is why implement prompts tell the agent to grep
before reading, read line ranges rather than whole files, and not re-read what
it has already seen.

### The pull-request flow

Every task now ends in a pull request, and the PR is what merges it.

```
task/<id>  ──push──►  PR into development  ──2 approvals──►  merged
                              │
                              └── rejected → back to the implementer,
                                  same PR, new commits, new round (max 3)
```

- Watch it: `gh pr list`, or the project detail view, which links each task's
  PR. A task sitting at status `in_review` is waiting on reviewers.
- Reviewers post their blocking issues as a PR comment, so the reasoning is on
  the PR itself, not only in the event log.
- **Nothing reaches `main`.** When `development` is where you want it:

```bash
.venv/bin/python main.py code promote
```

That opens a `development → main` PR and stops. You merge it.

**Setup on a fresh box:** `gh auth login`, then
`git remote add origin <url>`, then run anything — `ensure_base_branch`
creates `development` if it is missing. Without a remote, `publish` fails the
task with `push failed: no git remote configured` rather than pretending to
merge.

### Reaping orphans (`code reconcile`)

A run that died before it could clean up leaves three kinds of orphan, all of
which make the fleet behave worse until reaped:

| orphan | effect |
| --- | --- |
| `code_tasks` rows stuck at `running` | dashboard shows work that is not happening; the resume planner reads them as failures |
| `driver_leases` rows | count against the per-model cap until their TTL, so the fleet throttles itself against ghosts |
| worktrees + `task/<id>` branches | accumulate under `~/worktrees` |

```bash
.venv/bin/python main.py code reconcile --dry-run   # report only
.venv/bin/python main.py code reconcile             # reap
```

It **refuses to run while a `code run` process is alive** (pass `--force` only
if you know that process is wedged), and it **never deletes a worktree holding
uncommitted agent edits or a branch with unmerged commits** — the orchestrator
only commits at publish, so an interrupted implement's entire output lives in
its worktree as uncommitted files. Those are reported under "worktrees KEPT";
resume the task file to land them.

`run-queue.sh` reconciles automatically before its first task file.

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

### Merge landed but local edits stayed stashed (`merge.stash_retained`)

Not a failure. The blessed repo doubles as the operator's working copy, so
`merge_to_main` path-scoped stashes any locally-dirty file the merge needs to
touch, merges, then pops. The pop can fail when the branch **adds** a path the
operator also has as an untracked local file (a log, a transcript): git will
not restore it over the merged copy.

The merge has already landed. The event names the paths and the stash is left
intact — recover with:

```bash
git -C <repo> stash list
git -C <repo> stash pop
```

This used to raise, which marked a **successfully merged task `conflict`**.
It no longer does; only a genuinely failed merge does that.

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
