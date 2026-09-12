# Orchestration Contract (Kimi-K3 is the orchestrator)

Kimi-K3 is the **brain** of the code fleet. When a goal arrives — via
`main.py code plan "<goal>" <repo>` or the dashboard's *Plan with Kimi-K3*
tab — Kimi-K3 designs the entire execution plan. The graph runner
(`code_tasks.build_code_graph` + `graph.py`) executes it **verbatim**.
There is no runtime triage: every routing decision below is made by the
Kimi-K3 plan (or by whoever writes a task file by hand, under the same
rules, enforced by `code_tasks.load_taskfile`).

## What the orchestrator decides

1. **Task break** — 2–8 small tasks (`<30 min` each). Prefer more parallel
   small tasks over few serial big ones.
2. **Fanout** — every task that does not consume another task's output has
   **no `deps`** and starts at `t=0`. Width at layer 0 should typically be
   half or more of the task count.
3. **Dependencies** — add `deps: [X]` *only* when this task reads code that
   task X writes. Never chain for stylistic ordering; chains serialize.
   Dep edges run through `main`: `publish_X → alloc_Y`, so Y always builds
   on a `main` that already contains X (and all earlier merges).
4. **Model routing** — the tier table in
   [model-tiers.md](model-tiers.md). Hard rule, loader-enforced:
   `config.IMPLEMENTER_MODELS` × tier suitability.
5. **Reviewer** — `kimi` or `glm` per task, **cross-harness** when a strong
   model implements (see the cross-review matrix in
   [model-tiers.md](model-tiers.md#cross-review-matrix)). Split reviews
   between the two so neither idles nor saturates.
6. **Verify gate** — a deterministic `verify_cmd` per task (tests, build,
   `node --check`, a `grep` contract). It runs in the task's worktree
   *before* review; failure loops the task back to the implementer
   (max `config.MAX_FIX_ROUNDS` = 3 rounds per tier, then the task
   **escalates** to the next model in `config.ESCALATION_PATH` — default
   `gpt-oss-120b → DeepSeek-V4-Flash → GLM-5.3 → Kimi-K3`, env
   `ARC_ESCALATION_PATH` / `ARC_MAX_ESCALATIONS` — with a fresh fix budget,
   instead of failing).
7. **Collision avoidance** — `files_hint` must be disjoint across
   dep-independent tasks; two parallel agents editing the same file is the
   main cause of `conflict` failures at merge time.
8. **Project chaining** — `project.after`: whole taskfiles that must be
   fully merged before this taskfile starts. Use it when this project's
   tasks read another project's merged output; never inside one taskfile
   (`deps` does that, per task).

## What the runner guarantees

- **Worktree isolation** (`gitstore.py`): every task implements in its own
  `git worktree` under `~/worktrees/`, branched from `main`.
- **Green-main invariant**: merges are serialized through a single process
  lock; `main` always contains only reviewed, gate-passed work.
- **Graph semantics** (`graph.py`): each node is an async worker; tokens
  flow over edges; conditional edges implement the fix loops; `in_flight`
  hitting 0 ends the run.
- **Concurrency caps** (`drivers.py` semaphores): see
  [concurrency-limits.md](concurrency-limits.md). Queued tasks wait
  politely — they never push an account over its ARC limit.
- **Chain gating** (`code_tasks._make_chain_wait`): a taskfile declaring
  `after` holds at the `chain_wait` graph node — before **any** worktree
  alloc — until every task id of every upstream taskfile (parsed from the
  file on disk, not from rows) has a `merged` row. While it waits, nothing
  exists: no branch, no worktree, no task rows. A `failed`/`conflict`
  upstream row or the `ARC_CHAIN_TIMEOUT` (6 h) budget ends the run
  (exit 1, `chain.blocked`); a missing upstream file is simply waited on.
  `chain_wait` → every head node, edge gated on `{"ok": true}`.
- **Retry safety** (`gitstore.alloc`, `gitstore.merge_to_main`): alloc always
  resets branch `task/<tid>` to the base ref on (re)alloc, so a failed
  attempt's rejected work never leaks into a retry; merges tolerate a dirty
  blessed-repo working tree (path-scoped `git stash push` of conflicting
  local edits before the merge, `stash pop` after — the merge stays landed
  even if the pop conflicts).
- **Publish hook** (`gitstore.push_and_open_pr`): after a successful local
  merge, best-effort push of `task/<tid>` + `main` to origin and a GitHub PR
  via `gh pr create` — emits `task.pr_opened` `{url}` or
  `task.pr_skipped` `{reason}`, never fails the task.
- **Evidence**: every run lands in `logs/harness/*.jsonl` (streamed live),
  `logs/events.jsonl`, and the SQLite tables `code_tasks` / `harness_runs`.
  Escalations, conflicts, resume plans, and the publish hook surface as
  `task.escalated` / `task.conflict` / `run.resume` / `task.pr_opened` /
  `task.pr_skipped` events. The dashboard reads all of it — see
  [runbook.md](runbook.md).

## Task-chain shape (per task)

```
alloc → implement → gate ──pass──▶ review ──pass──▶ publish(merge + PR hook)
         ▲            │                │
         └──── fail ◀─┴───── fail ◀────┘   (≤ MAX_FIX_ROUNDS fix rounds per tier)
         │
         │   fix rounds exhausted
         ▼
      escalate_<tid>  ── next model in config.ESCALATION_PATH
         │                 (gpt-oss-120b → DeepSeek-V4-Flash → GLM-5.3 → Kimi-K3),
         │                 fresh fix budget, latest failure carried as feedback;
         │                 reviewer flips when the new implementer is kimi/glm
         │                 family (glm→kimi, kimi→glm), basic/medium keep theirs
         │
         └──▶ implement (again)      ... until the last tier exhausts:
                                         task failed, message names the last
                                         model tried
```

Statuses recorded in `code_tasks`: `pending → running → merged |
conflict | failed`. `conflict` = merge collision; `failed` = fix rounds
exhausted on the final escalation tier.

## Project chains (before the per-task pipeline)

A taskfile with `project.after` prefixes the whole shape above with one
gate: `chain_wait` is the graph's only start node, and every head (first
task of the chain, or the skip/repair stub of a merged/conflicted one)
waits on it. The gate releases only when every upstream taskfile's every
task is merged — so a dependent project branches from a base that already
contains the code it depends on, the same invariant `deps` provides
inside a file. Full semantics, exit codes, and events:
[taskfile-schema.md](taskfile-schema.md) § "Project chaining".

## The pull-request gate

```
alloc ─► implement ─► gate ─► review ─► publish ──► pr_review ──► pr_merge
          ▲             │        │         │            │ ▲ ▲   (all approve)  │
          │             │        │         │            │ │ │                  │
          └─────────────┴────────┴─────────┘            │ │ └── resynced ──────┘
             fix loop (gate/review reject)              │ │     (base moved on;
          ▲                                             │ │      re-review)
          │                                             │ └──── inconclusive
          └─────────────────────────────────────────────┘       (a reviewer
                    PR reviewers request changes                  crashed; retry
                    (a REAL objection, not a crash)                the review)
```

`publish` commits, pushes `task/<id>`, and opens a PR against
`config.BASE_BRANCH` (`development`). **Nothing has merged at this point.**

`pr_review` runs `config.PR_REVIEWERS` reviewers in parallel on the real
`gh pr diff`. They come from families other than the implementer's and from
each other. Every one must approve. A rejection posts the issues as a PR
comment and returns the task to `implement`, whose next commit updates the
same PR; `config.PR_MAX_ROUNDS` (3) bounds that loop.

A reviewer that CRASHED did not review. If nobody objected but one never ran,
the round is **inconclusive**: nothing is posted as `--request-changes`, the
task returns to `pr_review` rather than to `implement`, and the retry budget is
`config.PR_MAX_INCONCLUSIVE` — separate from `PR_MAX_ROUNDS` so an
infrastructure failure cannot consume a round reserved for real disagreement.
A genuine objection still beats a crash.

`pr_merge` runs only after unanimous approval: `gh pr merge --squash
--delete-branch`, then fast-forwards the local base branch and removes the
worktree.

A PR GitHub reports as `CONFLICTING` is **resynced before being given up on**:
`gitstore.sync_with_base` merges the current base into the task branch (and
aborts on failure, so a genuine overlap never leaves a half-merged worktree),
pushes, and routes back to `pr_review` — the diff changed, so the approval it
already holds no longer covers it. `config.PR_MAX_RESYNCS` (2) bounds it. Only
a real textual conflict records status `conflict`, and it records which files
disagree.

**Resuming.** A task in `in_review` or `conflict` restarts at `publish`, which
re-attaches to its existing worktree and open PR. It must NOT restart at
`alloc`: alloc resets `task/<id>` to the base and would discard the branch the
PR was opened from.

**Draining.** When a sibling task fails, the graph stops scheduling new work,
but the `publish -> pr_review -> pr_merge` edges are marked `on_drain=True` and
keep firing, so an already-open PR still gets reviewed and merged instead of
being orphaned. The rework edge is deliberately not marked: draining must still
refuse to commit fresh model time.

Reviewers are instructed that a code change must ship tests that would fail
without it (`config.REQUIRE_TESTS`), and to look for regressions in callers of
the changed code. Documentation-only changes are exempt.

**Branches.** `task/<id>` → `development` → (manual promotion PR) → `main`.
The fleet never writes to `main`; `main.py code promote` opens the promotion
PR for a human to merge.

**How many reviewers.** `PR_REVIEWERS_WANTED` (`ARC_PR_REVIEWERS`, default 2) is
what the operator asked for. `PR_REVIEWERS` is what today's roster can deliver:
an implementer's PR can only be read by the *other* review-capable families,
so the ceiling is `families - 1` — 2 with three families, **1 after Kimi-K3
leaves on 2026-09-19**. The effective value is the smaller, so the config never
promises a gate the fleet cannot staff; the daily audit reports delivered
against wanted, and `task.pr_review_thin` fires on every PR that got fewer
readers than asked.

**Statuses** gain `in_review`: the PR is open and awaiting approvals.

**Resume**: re-running `main.py code run` on the same taskfile resumes the
project (`code_tasks.build_code_graph`) — it never starts a new one. Tasks at
`merged` are skipped wholesale (their subgraph collapses to a stub publish
node returning `merged`; dependents treat them as satisfied, no models/git
spent). Tasks at `failed` re-execute one escalation tier higher than their
recorded model with a full fresh fix budget **only when the recorded failure
reason is a capability failure** (`code_tasks._is_capability_failure`: the
model exhausted its fix rounds or escalation path). A failure that records an
infrastructure reason — the run process was killed, the graph cancelled, the
harness crashed — re-executes at the **same** tier: being interrupted is no
evidence the model was too weak, and escalating on it funnels every
interrupted task onto the scarcest tier at once. Tasks at `conflict`
re-execute at the **same** model, and their publish node first tries repair:
if `task/<tid>` is still ahead of `main` (`gitstore.branch_ahead` — the
reviewed, gate-passing commit survived), it merges that branch directly under
the merge lock instead of re-running implement+review; only a failed repair
falls back to full re-execution. Stale `running` rows are marked `failed` at
startup, scoped to that taskfile. Startup prints a resume plan (skipped /
retried / escalated) and emits `run.resume`
`{skipped_merged, retried, escalated_on_resume}`, where `escalated_on_resume`
is measured against where each task **last ran**, not the taskfile's routing.

**Review diffs** are taken against the **merge base** of the task branch and
the base ref, never the live `main` (`gitstore.diff_full`). Merges are
serialized but tasks run in parallel, so `main` advances while a task works;
diffing against it presents every file a sibling merged since alloc as a
deletion by this task, and the reviewer rejects the scope violation.

**Run shutdown** is a contract too: on every exit path — clean finish, node
crash, `SIGINT`, `SIGTERM` — a run cancels in-flight work, kills its harness
child processes, marks its unfinished tasks `failed` with an infrastructure
reason, and releases the driver leases it held. `main.py code reconcile`
reaps orphans from runs that died before this was true.

**Standalone PR review (gh_ops).** The `pr-reviewer` agent (`main.py gh
pr-review <repo> <N>`) reviews an arbitrary open pull request under the exact
verdict contract of the internal reviewers above — strict JSON
`{"pass": true}` or `{"pass": false, "issues": [...]}`, parsed by the same
`code_tasks._parse_verdict` — so a verdict from either path means the same
thing. It is print-only unless `--post` submits the verdict via `gh pr
review` (approve on pass, otherwise a comment listing the issues), and it
plays no role in the governed merge gate described here.
