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
  orchestrator (the only git actor) commit, push, and open a pull request against `config.BASE_BRANCH` (`main` by default) under a
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
| Kimi-K3 | `kimi` CLI (`KimiDriver`) | hard | Implement, Plan, Review, PR-review | 3 | 3 |
| GLM-5.3 | `opencode` (`OpencodeDriver`) | hard | Implement, Plan, Review, PR-review | 4 | 4 |
| gpt-oss-120b | `opencode` (`OpencodeDriver`) | basic | **Implement only** | 10 | 5 |
| DeepSeek-V4-Flash | `opencode` (`OpencodeDriver`) | medium | **Implement, PR-review** | 10 | 5 |

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
- The review node instantiates `KimiDriver("reviewer")` or
  `OpencodeDriver("GLM-5.3", "reviewer")` accordingly and sends the full
  diff (`gitstore.diff_full`) with the original spec; the verdict must be
  JSON: `{"pass": true}` or `{"pass": false, "issues": [...]}`.
- **`reviewer` and `pr_reviewer` are different roles.** `reviewer` is this
  pre-merge gate. `pr_reviewer` reviews an already-open pull request (Rule 5),
  and `DeepSeek-V4-Flash` may hold it even though it may not hold `reviewer`:
  judging a bounded diff against a spec is a much smaller job than authoring
  the change, and with three cross-family-eligible models a two-reviewer merge
  gate is otherwise unreachable whenever Kimi or GLM implemented — which is
  most tasks. `gpt-oss-120b` stays implement-only.
- **Never keep a second list of who may review.** Eligibility is decided by
  CONSTRUCTING the driver (`code_tasks._eligible_pr_reviewers`). A hand-kept
  pool and the drivers' own role rules drifted apart once and it cost seven
  pull requests: the pool offered DeepSeek, `OpencodeDriver` refused the role,
  and the `ValueError` killed `pr_review` one second after each PR opened.
- A failed review sends the issues back to the implementer as feedback
  (bounded fix loop, Rule 4); nothing merges without `pass: true`
  (edge `review_<tid> -> publish_<tid>`, code_tasks.py:257).

- **A reviewer that CRASHED did not review.** A pre-merge reviewer that dies —
  a capacity error, a harness fault — returns `crashed`, and the task retries
  the REVIEW rather than going back to the implementer. Spending a fix round on
  it sends the implementer to repair code nobody criticised. Bounded by
  `config.MAX_REVIEW_CRASHES` (`ARC_MAX_REVIEW_CRASHES`, default 3), kept
  separate from the fix budget for the same reason `PR_MAX_INCONCLUSIVE` is
  separate from `PR_MAX_ROUNDS`.

  This cost a real task. graph-admission-control's verify gate passed FOUR
  times while its reviewer hit 18 consecutive capacity errors; each crash was
  recorded as a rejection, the implementer was sent to fix nothing, and the
  task finally died as "exhausted escalation" on work that was never rejected.

Full pipeline contract: [docs/orchestration-contract.md](docs/orchestration-contract.md).

### Rule 3 — Every task runs in its own git worktree off the repo's `main` branch

- `gitstore.alloc` (gitstore.py:47) creates `~/worktrees/<project>/<task-id>`
  on branch `task/<task-id>` from base `main` (`config.WORKTREE_ROOT`,
  override `ARC_WORKTREE_ROOT`). A blessed clone at `~/repos/<project>` keeps
  `main` clean. On (re)alloc `gitstore.alloc` always **resets branch
  `task/<task-id>` to the base ref** — a failed attempt's branch holds
  rejected work and never leaks into a retry (Rules 4–5).
- Tasks with no `deps` are graph start nodes and run in parallel; a dependent
  task is wired `publish_<last-dep> -> alloc_<tid>` (code_tasks.py:267) so it
  allocates only after its dependency has merged. The same ordering holds
  **between projects**: a taskfile declaring `project.after` allocates **no
  worktree at all** until its whole chain is merged (Rule 9) — while it
  waits, no branch, worktree, or task row exists.
- **The orchestrator is the only git actor** (gitstore.py docstring). Harnesses
  only write files inside their worktree; the implementation prompt
  (code_tasks.py:107) tells every implementer: *"do not git-commit (the
  orchestrator handles git); keep changes minimal and working."* The same
  applies to you: **NEVER `git commit`, `git merge`, or `git push` in a task
  worktree** — publish/merge is done for you by Rule 5.
- **This worktree discipline is universal: ALL multi-agent work in this
  ecosystem — code, docs, and these very governance files (AGENTS.md,
  `docs/`) — goes through per-task git worktrees** at
  `~/worktrees/<project>/<task-id>` on `task/<task-id>` branches, never
  direct edits on `main` (or any shared branch) by harnesses. A task that
  edits documentation is planned, routed, gated, reviewed and merged exactly
  like one that edits code — the pipeline is path-agnostic, and every
  implementer harness runs with cwd = its worktree (drivers.py:484), so
  there is no "too small for a worktree" path, not even for a one-line doc
  fix.

### Rule 4 — Every task MUST define an honest `verify_cmd` gate, run before review

- The gate node (code_tasks.py:190) runs the task's `verify_cmd` as a shell
  command **in the worktree**, under `config.GATE_TIMEOUT` = **180 s**
  (override `ARC_GATE_TIMEOUT`); a timeout kills the process and fails the
  gate. Only its stdout/stderr tail (last 2000 chars) is kept.
- The gate MUST pass before review happens (edge `gate_<tid> ->
  review_<tid>` fires only `when r["passed"]`, code_tasks.py:256).
- A gate or review failure loops back to `implement` with the failure output
  as feedback while `runs <= config.MAX_FIX_ROUNDS` (3, override
  `ARC_MAX_FIX_ROUNDS`). Exhausting the fix rounds does **not** fail the task
  yet: it **escalates one tier up `config.ESCALATION_PATH`** (default
  `gpt-oss-120b → DeepSeek-V4-Flash → GLM-5.3 → Kimi-K3`, overrides
  `ARC_ESCALATION_PATH` / `ARC_MAX_ESCALATIONS`) — an `escalate_<tid>` graph
  node routes back to `implement_<tid>` with a **fresh fix budget**, carrying
  the latest gate/review failure as feedback. Cross-review holds on
  escalation (`code_tasks.build_code_graph`): when the new implementer's
  family is `kimi` or `glm` the reviewer token flips to the other one
  (glm implementer → kimi reviewer, kimi → glm); basic/medium models keep
  the taskfile's reviewer. Each escalation emits `task.escalated`
  `{from_model, to_model, n}`; the `code_tasks` row is updated with the
  current model/reviewer and `harness_runs` rows record the model actually
  used. On **resume**, escalation is conditional: only a row whose recorded
  failure reason is a capability failure (exhausted fix rounds / escalation)
  starts a tier higher. A row written because the run process was killed or
  the graph was cancelled restarts at the **same** tier — an interrupted run
  is not evidence the model was too weak, and escalating on it sends every
  interrupted task to the scarcest tier simultaneously.
  Only when the last tier exhausts is the task marked `failed`, and
  the failure message names the last model tried (`exhausted escalation up
  to Kimi-K3`, code_tasks.py:243). Concurrency footnote: worst-case harness
  runs per task multiply by tier count (fix rounds × tiers); all caps of
  Rule 6 still apply.
- The loader permits an empty `verify_cmd` (it then passes trivially,
  code_tasks.py:192) — which is exactly why this rule is normative: a
  taskfile with no honest gate is a bug. The planner prompt requires a
  "meaningful verify_cmd"; hand-written taskfiles get one too. Run the tests,
  build, or a targeted check — something whose exit code actually depends on
  the change being correct.

### Rule 5 — The pull request is the gate; nothing merges without approvals

**No task merges locally. Ever.** `publish` (code_tasks.py) commits the
worktree on `task/<tid>` with message `task(<tid>): <title>` and trailers
`Harness`, `Model`, `Reviewer`, `Task-Id`, pushes the branch, and opens a pull
request against `config.BASE_BRANCH` (default `main`). At that moment
nothing has landed.

This replaced a flow that merged into `main` and opened the PR afterwards. A
reviewer could then only object to work that had already shipped — "send it
back" withheld nothing. Worse, `gh pr create` was run *after* pushing main, so
GitHub saw no commits between the refs and the hook had **never once opened a
PR** in the life of the repo.

**The review loop** (`pr_review` -> `pr_merge` | `implement`):

- `config.PR_REVIEWERS` (default 2) reviewers read the **real PR diff** via
  `gh pr diff`, in parallel, each with its own prompt and no knowledge of the
  others' verdicts.
- Reviewers are chosen from families **other than the implementer's**, and
  differ from each other, so two approvals mean two independent readings.
- **Unanimous approval is required.** Any rejection posts the blocking issues
  as a PR comment and sends the task back to `implement`; the next commit
  updates the same PR and a new round begins.
- The loop is bounded by `config.PR_MAX_ROUNDS` (default 3); exhausting it
  fails the task rather than looping forever.
- **A reviewer that crashed did not review.** If no reviewer objects but one
  never ran, the round is *inconclusive*, not a rejection: nothing is posted
  as `--request-changes`, the task goes back to `pr_review` rather than to the
  implementer, and the retry comes from `config.PR_MAX_INCONCLUSIVE` — kept
  separate from `PR_MAX_ROUNDS` so infrastructure failures cannot eat the
  rounds reserved for real disagreement about the code (`ARC_PR_MAX_INCONCLUSIVE`,
  default 3). A genuine objection still beats a crash. Before this, a crash was posted to a public PR as
  "changes requested: reviewer crashed" and sent the implementer to fix issues
  that did not exist.
- **A conflicting PR is resynced, not abandoned.** `pr_merge` merges the
  current base into the task branch (`gitstore.sync_with_base`, which ABORTS
  on failure so a genuine overlap never leaves a half-merged worktree for the
  next publish to commit), pushes, and routes back to `pr_review` — the diff
  changed, so the approval it already has no longer covers it. Bounded by
  `config.PR_MAX_RESYNCS` (`ARC_PR_MAX_RESYNCS`, default 2). A real textual conflict still stops,
  recording which files disagree.
- **Resuming a task whose PR is open re-attaches to it.** `in_review` and
  `conflict` tasks restart at `publish`, which finds the existing worktree and
  the open PR and hands it straight to review. Do not "fix" this by starting
  at `alloc`: alloc RESETS `task/<id>` to the base and discards the branch the
  PR was opened from.
- **A sibling's failure must not orphan an open PR.** The `publish ->
  pr_review -> pr_merge` edges are marked `on_drain=True`, so they keep firing
  after the graph starts draining. The rework edge deliberately is not:
  draining must still refuse to start fresh model time.
- Reviewers are told that a code change **must** ship tests that would fail
  without it (`config.REQUIRE_TESTS`), and to check for regressions in what
  calls the changed code. Documentation-only changes are exempt.
- Only `pr_merge` merges, via `gh pr merge --squash --delete-branch`, and only
  after a unanimous `pr_review`. It then fast-forwards the local integration
  branch to what GitHub merged and cleans up the worktree.

**A dependent task waits for its dependency's PR to MERGE**, not merely to
open (`pr_merge_<dep> -> alloc_<tid>`) — otherwise it would branch from a base
that does not yet contain the code it depends on.

**Branch model.** `task/<id>` branches from `config.BASE_BRANCH` and its
pull request merges back into it. That is ONE branch by default — `main`.
It was `development` with `main` promoted to by hand, and the split cost
more than it bought: the operator's checkout, the fleet's base and the
promotion target were three moving refs, and several bugs came straight out
of that (`config.py` records them). Nothing was removed: set
`ARC_BASE_BRANCH=development` (keeping `ARC_PROD_BRANCH=main`) and
`main.py code promote` opens a `development -> main` PR for a human to
merge. `config.promotion_configured()` — true only when the two branches
differ — is what switches the promote command and the dashboard button on.

`gitstore._base_ref` always resolves to the **local** base branch. It once
preferred `origin/<base>` whenever a remote existed, which meant the first
skipped push would have every new task branch from a stale origin and revert
merged work.

### Rule 6 — Concurrency caps are THREE-layer; know all three before launching anything

| Layer | Where | gpt-oss | deepseek | glm | kimi | Override |
|---|---|---|---|---|---|---|
| Per-account API caps | `config.FAMILIES[*].limit` (ARC rejects over-limit per model) | 10 | 10 | 4 | 3 | `ARC_LIMIT_<FAMILY>` |
| Driver semaphores + leases | `config._MODEL_DRIVER_CAP` — ARC **sessions** divided by how many one harness process holds at once | 2 | 2 | 2 | 3 | `ARC_DRIVER_LIMIT_<FAMILY>` |
| **Harness pool** | `config.harness_limit` via `drivers._harness_gate` + a `harness:<name>` lease | opencode: **5** total | ← shared | ← shared | kimi: 3 | `ARC_HARNESS_LIMIT_<HARNESS>` |

**A harness process is not one ARC session.** The session ceilings measured
on this fleet are gpt-oss 5, DeepSeek 5, GLM 4, Kimi 3 — but an opencode run
issues parallel tool calls and holds about TWO sessions at once, so a driver
cap set equal to the session limit over-subscribes by that factor. Measured
from the event log: 23 capacity rejections in four hours, GLM-5.3 refused with
as few as TWO of our drivers live against a ceiling of four. Driver caps are
therefore sessions // sessions-per-process; the kimi CLI holds one, opencode
two. gpt-oss and DeepSeek were previously configured at 8 against a real
ceiling of 5, so the fleet generated its own 400s under load and blamed the
provider.

**The harness layer is the one people forget, and it is often the binding
one.** Every opencode-backed model runs through ONE local binary backed by ONE
~240MB sqlite store in `~/.local/share/opencode`. The per-model caps permit
GLM 4 + DeepSeek 5 + gpt-oss 5 = **14** concurrent opencode processes against
it. Measured with an identical prompt and a warm cache:

| concurrent | 3 | 4 | 5 | 6 | 10 |
|---|---|---|---|---|---|
| succeeded | 3/3 | 4/4 | 5/5 | 4/6 | 4/10 |

Past five it fails fast with an EMPTY stderr, which the fleet logged as
`opencode exited 1: ` and retried four times per task — burning the retry
ladder on self-inflicted contention. A model sitting under its own cap is NOT
available if its harness is full, which is why `code_tasks._reviewer_pressure`
scores a reviewer on whichever ceiling binds first.

- The **account caps are per API key, not per process** — other agents and
  interactive sessions share them (config.py:88).
- The **driver semaphores bound concurrent harness instances of one MODEL in
  this process**; `ARC_DRIVER_HEADROOM` reserves slots for interactive use of
  the same account.
- Acquisition order is always model gate → model lease → harness gate →
  harness lease. One global order means no circular wait, and the scarce
  harness slot is never held while queueing for a plentiful model slot.
- The **driver leases close the cross-process hole**: semaphores alone let a
  terminal queue AND dashboard-launched runs each hold their own cap and stack
  to 2× the account limit. Before spawning a harness, `drivers._lease_acquire`
  takes a row in the shared `driver_leases` table under that model's driver
  cap; over cap the task **waits** (poll every 20 s) and emits
  `driver.cap_wait {model, task, in_use, cap}` about once a minute — that event
  is the warning surface for "a new task is about to exceed concurrency".
  Leases are reaped when older than `config.DRIVER_LEASE_TTL` (3300 s) or when
  the owning pid is dead, so killed runs never deadlock the fleet.
- For the research workload, `pool.py` additionally enforces per-family
  `asyncio.Semaphore(config.family_limit(f))` client-side.
- Full explanation, including how the ARC API rejects over-limit requests:
  [docs/concurrency-limits.md](docs/concurrency-limits.md).

### Rule 6b — The dashboard is UNAUTHENTICATED and this is a deliberate choice

`main.py serve` binds `0.0.0.0:8787` with no authentication of any kind. That
is not an oversight; the operator was shown the following and chose to keep it,
on the basis that the network is trusted.

**What it means concretely.** `POST /api/projects/create` accepts a task whose
`verify_cmd` is an arbitrary shell string, and `code_tasks.gate` runs that
string through `asyncio.create_subprocess_shell`. So:

```
POST /api/projects/create   {"tasks":[{... "verify_cmd":"<anything>"}]}
POST /api/projects/run      {"file":"..."}
```

is remote code execution as the operator, for anyone who can reach port 8787.
Verified live on 2026-09-10: an unauthenticated POST created a taskfile. The
other mutating routes (`run`, `stop`, `archive`, `retry-task`, `promote`) spawn
processes and move git refs on the same terms.

**Therefore:**

- Do NOT expose port 8787 beyond a trusted LAN. No port-forwarding, no tunnel,
  no reverse proxy to the public internet.
- Do NOT add a route that widens this — nothing that takes a path, a command,
  or a git ref from the request body without an allowlist.
- If the trust assumption ever stops holding, the two mechanisms already
  designed for it are: bind the loopback address instead of `0.0.0.0` (which
  would need a new bind-address setting in `main.py serve`), and require a
  shared-secret header on POST only, so read-only access from `phone.html`
  keeps working. **Neither exists** — do not go looking for an env var.

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
  `task.merged`, `task.conflict`, `task.failed`, `task.escalated`
  (Rule 4), `task.pr_opened` / `task.pr_reviewed` / `task.resynced` (Rule 5),
  and `run.resume`
  (Rule 5 resume plan); project chains add `chain.wait` / `chain.ready` /
  `chain.blocked` (Rule 9).
- The dashboard Projects DAG view renders the loops: fix-loop attempts as
  dashed amber self-arcs with xN counts, `conflict` nodes in **orange**
  (distinct from `failed` red), and the last review verdict on each node.
- Harness-level resilience: `config.DRIVER_TIMEOUT` = 2700 s per harness
  invocation (override `ARC_DRIVER_TIMEOUT`) as a total-runtime backstop, and
  `config.DRIVER_IDLE_TIMEOUT` = 420 s (override `ARC_DRIVER_IDLE_TIMEOUT`) as
  a **stall detector**: a harness that produces no stdout for that long is
  waiting on a request that is not coming back, so it is killed and retried
  rather than waited out. Every stall records forensics first — process state,
  CPU delta, last tool calls, and whether an API request is outstanding — see
  [docs/runbook.md](docs/runbook.md) § "A harness stalled".
  (An earlier version of this rule said ARC "holds rejected/queued requests
  open instead of erroring". That is **wrong**: measured 2026-09-09, ARC
  rejects over-cap requests in ~0.2 s with
  `400 {"detail": "concurrent session limit reached"}`. Those 400s are what
  the capacity backoff exists for; the long silences are a separate,
  still-unexplained failure.)
  Retries use exponential
  backoff (capped at 30 s) up to `config.MAX_RETRIES` = 4. If every retry
  fails, the attempt is recorded as a harness run (exit 1) and treated as a
  failed attempt — the task re-enters the bounded fix loop and, if the
  outage persists through the fix rounds and escalation tiers, ends `failed`
  (Rule 4); the rest of the run continues.
- If you cannot show a transcript or an event for a claim about a run, do not
  make the claim.

### Rule 7b — Never discard an exception; capture it

`str(exc)[:300]` was all that survived a failure anywhere in this repo — no
file, no line, no frame. Debugging meant guessing which of several call paths
produced a message like `opencode exited 1:`.

- **Catch sites call `errors.capture(exc, task=..., model=..., node=...)`** and
  put the returned fingerprint on the event. The event stays short; the
  traceback, the exception chain and the caller's context go to `error_events`.
- **Errors are grouped by FINGERPRINT, not by message.** The fingerprint is the
  exception type plus the names of the frames inside this repo — deliberately
  not line numbers (they shift on every edit) and not the message (it carries
  worktree paths, task ids and durations that make every occurrence unique).
  A hundred occurrences of one bug must count as one bug.
- **`errors.capture` never raises.** It runs inside `except` and `finally`
  blocks, and instrumentation that can turn a handled error into an unhandled
  one is worse than none.
- **Capacity errors are not captured.** They are expected weather and would
  swamp the triage list; they already have their own events.
- **Every event carries a `run_id`** so one run can be reassembled afterwards
  from events spread across the run process, its drivers and the dashboard.

Tests must never write to the operator's error table — `tests/helpers.py`
redirects `config.DB_PATH` for the same reason it redirects `EVENTS_LOG`. One
unguarded suite run put 43 synthetic defects into the production triage list.

### Rule 7c — The daily audit

`main.py audit` answers two questions a green dashboard cannot: what broke
(distinct defects, worst first) and what is rotting (work stranded in a
non-terminal state, worktrees and branches left by dead runs, leases pinning
capacity for processes that no longer exist, log growth, and whether
`check.sh` still passes).

- Every finding carries a severity AND a concrete next action.
- **The exit code is the alarm**: 2 when anything is critical, else 0. That is
  what makes `daily-audit.sh` schedulable — cron mails only on a non-zero exit,
  so a mail means something.
- `--fix` performs only the reversible cleanups `reconcile` already implements,
  and **skips entirely while any run is in flight**: reaping worktrees and
  leases out from under a live run turns a cleanup into an outage.

### Rule 8 — Dry-run before every run

- Before executing a taskfile for real, run
  `main.py code run <taskfile> --dry-run` (main.py:244). It runs the full
  loader validation (Rule 1/2 rules, unknown deps, dependency cycles via
  `_topo`) and prints the resolved DAG (`describe`), calling **no models and
  touching no git**; it even uses a separate `dry-run.db` (main.py:21).
- `code plan` also prints the `describe` summary of the file it just wrote —
  read it. A dry-run that surprises you is a taskfile bug; fix the taskfile,
  not the dry-run.

### Rule 9 — Project chains gate on whole-DAG completion

`deps` orders tasks **within** a taskfile. When one *project* needs another
project's merged output, the taskfile declares `project.after`: a list of
upstream taskfile paths (bare filenames resolve under `~/tasks`). Never use
`after` inside one taskfile — that is what `deps` is for.

- `code_tasks.load_taskfile` (code_tasks.py:28) validates the shape: a list
  of non-empty strings, deduped, never the taskfile itself; an `after` cycle
  is rejected with `ValueError` at graph build (`_after_cycle`,
  code_tasks.py:222). A dep taskfile that does not exist yet is legal — it
  is simply waited on, so a chain may be declared before the upstream is
  planned.
- `code_tasks.build_code_graph` (code_tasks.py:471) prefixes the whole DAG
  with a single `chain_wait` node (`_make_chain_wait`, code_tasks.py:256):
  it is the only start node, and every head (first task, or the skip/repair
  stub of a merged/conflicted one) hangs off it, gated `when r.get("ok")`.
- The gate releases only when **every task id of every upstream taskfile,
  parsed from the file on disk, has a `merged` row** (`chain_status`,
  code_tasks.py:172). A killed upstream run with 3 of 4 tasks merged is
  therefore *not* done — rows alone can't prove a DAG finished, because a
  killed run never writes rows for the rest. Waiting costs nothing: no
  worktree, branch, or task row exists until the chain is ready.
- A `failed`/`conflict` upstream row blocks the chain; the
  `ARC_CHAIN_TIMEOUT` budget (default **6 h**, poll every 10 s) bounds the
  wait. Either ends the run with exit code 1 and a `chain.blocked` event
  (`chain.wait`/`chain.ready` bracket the gate — Rule 7).
- CLI surface: `main.py code run <taskfile> --no-wait` pre-flights the
  chain and exits **2** when not ready (0 when ready) so queue wrappers can
  requeue instead of blocking a slot; `main.py code status` reports per-file
  chain readiness under `chains`. Full semantics:
  [docs/taskfile-schema.md](docs/taskfile-schema.md) § "Project chaining".

### Benchmarking exception — the bench `policy` escape hatch

`code_tasks.load_taskfile` / `code_tasks.build_code_graph` accept an optional
`policy` dict that widens the rules above **for benchmark variant runs
only**. The default (`policy=None`) enforces Rules 1–9 byte-for-byte, and
every normal path (`code plan`, `code run`, dashboard project runs) passes
no policy.

`orchbench.py` (`main.py code bench`) declares the benchmark matrix: each
entry in `orchbench.VARIANTS` is an explicit, named policy — e.g.
`glm-implement-only` (GLM implements, never reviews), `kimi-implement-only`,
`deepseek-reviews` / `gptoss-reviews` (implement-only models reviewing),
`self-review`, `no-review`, `kimi-via-opencode` (harness swap),
`all-glm` / `all-deepseek` / `all-kimi` (flat routing), `misroute`
(tier-inverted routing), `no-fixloop` / `fixloop-1` (`max_fix_rounds` 0/1) —
so we can measure which governance options actually matter. Two policy keys
also affect runtime behavior:

- `tolerate_driver_error` — **default on since 2026-09-09, in every path.**
  A harness that exhausts its `MAX_RETRIES` retries fails the attempt
  through the normal fix loop/`fail` node (gate: "implementer crashed";
  review: a crash verdict) instead of raising `DriverError` out of the
  graph — one crashed model no longer aborts an entire run. The policy
  key remains only as an opt-out (`False` restores the old abort).
- `review` / `max_fix_rounds` — gate toggles for the corresponding
  variants.

Every variant run stamps a fresh `filetoolkit` repo at
`~/repos/orchbench-<stamp>-<code>`, runs the full governed DAG (alloc →
implement → gate → review → publish → serialized merge) on six tiered
tasks, records harness sessions to its own db
(`logs/orchbench/<stamp>/orchbench.db`), and scores merged `main` with
`orchbench.integration_score` (the repo's own test suite). Results append
to `logs/orchbench/<stamp>/results.jsonl`; the table is
`main.py code bench report [--stamp <stamp>]`. Transcripts, events, and
`harness_runs` rows are produced exactly as in normal runs (Rule 7 holds
for benchmarks too).

### GitHub operations agents (gh_ops.py)

Standalone `gh`-CLI agents for GitHub housekeeping — NOT part of the governed
code pipeline (no worktree, no gate, no publish; Rules 1–8 do not apply):

- **`issue-triager`** — `main.py gh triage <repo> [--apply-labels] [--model
  Kimi-K3|GLM-5.3]`: classifies open issues (kind bug|feature|question|docs,
  size S|M|L, recommended tier per `config.IMPLEMENT_TIERS`), prints a triage
  table, and writes a ready-to-run taskfile to `~/tasks/<repo>-issues.json`
  with correct cross-review pairing (fill in each `verify_cmd` and dry-run
  before executing — Rules 4/8 apply once it becomes a taskfile).
- **`issue-maker`** — `main.py gh issue "<desc>" <repo> [--create]`: drafts a
  structured issue (title; body with context/repro/acceptance) and prints it.
- **`pr-reviewer`** — `main.py gh pr-review <repo> <N> [--post]`: reviews a PR
  under the same verdict JSON contract as internal review (it reuses
  `code_tasks._parse_verdict`; see docs/orchestration-contract.md).

Only **Kimi-K3** and **GLM-5.3** may hold these three roles —
`drivers.KimiDriver.__init__` / `drivers.OpencodeDriver.__init__` raise
`ValueError` if gpt-oss-120b or DeepSeek-V4-Flash is given one (same
enforcement pattern as Rule 2). Default model `config.GH_MODEL`
(`ARC_GH_MODEL`, default Kimi-K3); each `gh` subprocess is bounded by
`config.GH_TIMEOUT` (`ARC_GH_TIMEOUT`, default 60 s).

**Preview by default.** `--apply-labels`, `--create`, and `--post` are the
ONLY paths that write to GitHub; without them every command is read-only.
Every gh-touching command checks `gh auth status` first and exits with
`run: gh auth login` when unauthenticated (drafting/printing need no gh).
These agents do not change publishing: project publishes still merge via
`gitstore` exactly as Rule 5 defines; a gh_ops PR-publishing mode is a future
policy hook and is deliberately not implemented here.

---

## 4. Repo file map

Top-level Python modules (one role each):

| File | Role |
|---|---|
| `build_work.py` | Minecraft-style browser-game build workload: planner → 6 parallel module producers (each an implement → syntax gate → contract check → cross-model review → fix gauntlet) → assemble → bounded integration-review cycle |
| `bench.py` / `bench_data.py` | Single-model micro benchmark (top-level `main.py bench`): 31-task dataset × models × harness solvers (direct/fanout/fixloop/review/opencode/kimi), pass@k scoring — measures models and harnesses in isolation |
| `code_tasks.py` | The multi-harness code workload: taskfile loader/validation, the Kimi-K3 planner prompt (`plan_tasks`), per-task chain `alloc → implement → gate → review → publish/fail` with fix-loop and `escalate_<tid>` escalation edges, project-level `after` chain gating (`chain_wait`), resume of re-run taskfiles |
| `config.py` | Single source of truth: model families + caps, tier maps, driver caps, timeouts, paths — every `ARC_*` env override lives here |
| `dashboard.py` | Dashboard server (`main.py serve`, default port 8787): static UI + JSON APIs over `orchestrator.db`, `logs/events.jsonl` and live harness transcripts — **not read-only**: `do_POST` (dashboard.py:999) serves `/api/projects/create`, which spawns `main.py code plan` (goal mode) or writes taskfiles into `~/tasks` directly (dashboard.py:840-842), and `/api/projects/run`, which launches `main.py code run` (optionally `--dry-run`) subprocesses via `subprocess.Popen` (dashboard.py:768-770) |
| `drivers.py` | Headless CLI harness drivers: `KimiDriver` (`kimi` CLI) and `OpencodeDriver` (`opencode`); per-model semaphores, retries, timeouts, live transcript streaming to `logs/harness/` |
| `events.py` | Append-only JSONL event log `logs/events.jsonl` with contextvars attribution (`workload`/`round`/`iteration`/`module`) and 100 MiB rotation |
| `gh_ops.py` | GitHub operations agents over the `gh` CLI (`main.py gh …`): `issue-triager`, `issue-maker`, `pr-reviewer` — standalone tools outside the governed pipeline; preview by default, only `--apply-labels`/`--create`/`--post` write to GitHub |
| `gitstore.py` | The only git actor: worktree `alloc`/`publish`/`sync_with_base`/`push_task_branch`/`open_pr`/`merge_pr`/`fast_forward_base`/`cleanup` on `task/<id>` branches (60 s per-git-op timeout); nothing merges locally |
| `graph.py` | Generic async DAG engine: named nodes, conditional edges (`when=`), gather nodes, `max_steps` bound |
| `main.py` | CLI entry point: `run`, `once`, `status`, `graph`, `build`, `serve`, `bench` (micro), and `code {plan,run,status,bench}` |
| `orchbench.py` | Orchestration variant benchmark (`main.py code bench`): 14 named policy variants of the governed code DAG (routing, reviewer, harness, fix-loop) on a fresh `filetoolkit` repo per variant, with merge/integration scoring — benchmarks the orchestration options set, not single models |
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
| `docs/` | Detail reference docs — see [Links](#links); includes `graph-patterns.md`, the pattern library the planner consults |
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
   `main.py code status` for task and harness-run stats. A taskfile with
   `project.after` holds at the chain gate until its upstream projects are
   merged (Rule 9); `--no-wait` pre-flights and exits 2 when not ready.
   **Re-running the
   same command is also the retry/resume path** (Rule 5): merged tasks are
   skipped, failed tasks resume one escalation tier up, conflicts try
   repair — never write a reduced taskfile to retry a subset.
5. **Watch** — `./start.sh`, then open `http://localhost:8787`
   (`http://<lan-ip>:8787` from laptop/phone, `/phone.html` on phones).

---

## Links

- [docs/orchestration-contract.md](docs/orchestration-contract.md) — the
  end-to-end per-task pipeline contract (alloc → implement → gate → review →
  publish → merge, fix loops, tier escalation, resume semantics, failure
  semantics)
- [docs/model-tiers.md](docs/model-tiers.md) — model fleet, tiers, and
  allowed roles in full
- [docs/concurrency-limits.md](docs/concurrency-limits.md) — the two layers
  of concurrency caps and their env overrides
- [docs/taskfile-schema.md](docs/taskfile-schema.md) — taskfile JSON reference
  and validation rules
- [docs/graph-patterns.md](docs/graph-patterns.md) — the graph-pattern library
  for multi-agent work (chain, fan-out/fan-in, diamond, router,
  orchestrator-workers, …); the `code plan` planner
  (`code_tasks.plan_tasks`) consults it and records its choice as
  `"pattern"` in the taskfile
- [docs/runbook.md](docs/runbook.md) — operator runbook: dashboard,
  plan → dry-run → run, troubleshooting
- [docs/audit-2026-09-09.md](docs/audit-2026-09-09.md) — reliability audit
  of 2026-09-09: the twelve coupled defects behind stalled fleet runs
  (moving-ref review diffs, escalation on interrupted runs, leaked driver
  slots, uncleaned shutdown) and the reasoning behind each fix
- [README.md](README.md) — project overview, dashboard quick start, 24/7
  setup