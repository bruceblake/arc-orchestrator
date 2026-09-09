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
   (max `config.MAX_FIX_ROUNDS` = 3 rounds, then the task fails).
7. **Collision avoidance** — `files_hint` must be disjoint across
   dep-independent tasks; two parallel agents editing the same file is the
   main cause of `conflict` failures at merge time.

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
- **Evidence**: every run lands in `logs/harness/*.jsonl` (streamed live),
  `logs/events.jsonl`, and the SQLite tables `code_tasks` / `harness_runs`.
  The dashboard reads all of it — see [runbook.md](runbook.md).

## Task-chain shape (per task)

```
alloc → implement → gate ──pass──▶ review ──pass──▶ publish(merge)
         ▲            │                │
         └──── fail ◀─┴───── fail ◀────┘   (≤3 fix rounds)
                          │ exhausted
                          ▼
                         fail
```

Statuses recorded in `code_tasks`: `pending → running → merged |
conflict | failed`. `conflict` = merge collision (needs a human or a
re-plan with disjoint files); `failed` = fix rounds exhausted.
