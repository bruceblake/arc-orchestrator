# Fleet efficiency: where throughput is being lost

Analysis date: 2026-09-10. Read-only study of recorded history. No source files changed.

## 0. Data, windows, sample sizes

| source | rows | window |
|---|---|---|
| `logs/events.jsonl` | 6047 lines (1 unparseable) | 2026-09-07 22:07 → 2026-09-10 04:29 UTC (54.2 h) |
| reconstructed driver runs (`driver.start` ↔ terminal event) | 416 closed, 3 open at log end | same |
| `orchestrator.db harness_runs` | 167 (103 implementer, 64 reviewer) | 2026-09-08 21:44 → 2026-09-10 04:16 |
| `orchestrator.db code_tasks` | 42 rows; 31 distinct tasks with harness runs | 14 taskfiles |
| `/home/proxyie/tasks/*.json` | 18 files, 52 tasks, 21 dep edges | — |

Pairing method: `driver.start` matched to `driver.done` / `driver.error` / `driver.cancelled` /
`driver.timeout` / `driver.stale` on `(harness, model, task, attempt)`. 413/416 paired; 16
`driver.error` rows were unpairable (13 emitted from `code_tasks.review()` without harness/model
keys, 3 with no preceding start). 12 intervals ending in `driver.stale` or a duplicate start were
capped at `DRIVER_TIMEOUT` (2700 s) because their true end is unrecorded.

**Active time** below means the union of stretches with ≥1 driver in flight, merging gaps ≤5 min:
**15.55 h across 12 windows**. The other 38.7 h of the log is operator downtime and is excluded
from utilisation figures.

Two counts are load-bearing and worth stating up front:

- **`driver.cap_wait` events in the entire log: 0.**
- **`inflight.over_cap` events: 7**, all `family=kimi`, all on 09-09 between 18:29 and 21:50.

---

## 1. Concurrency — MEASURED

Time-weighted distribution of simultaneous in-flight drivers per model, over the 15.55 h of
active time. `util` = mean in-flight ÷ `config.driver_limit(model)`.

| model | cap | runs | idle | exactly 1 | at/over cap | mean in-flight | util |
|---|---|---|---|---|---|---|---|
| Kimi-K3 | 2 | 169 | 28.1% | 54.2% | 17.7% | 1.01 | **50.6%** |
| GLM-5.3 | 3 | 107 | 55.3% | 26.2% | 8.4% | 0.73 | **24.4%** |
| DeepSeek-V4-Flash | 8 | 58 | 85.7% | 6.9% | 0.0% | 0.26 | **3.2%** |
| gpt-oss-120b | 8 | 82 | 88.3% | 10.7% | 0.0% | 0.13 | **1.6%** |

Fleet total, same window:

| metric | value |
|---|---|
| mean in-flight drivers, all models | **2.13** of a 21-slot cap (**10.1%**) |
| active time with 0 drivers | 4.7% |
| active time with **exactly 1** driver | **58.2%** |
| active time with ≥5 drivers | 12.0% |
| peak observed | 14 (09-09 15:00–18:00 only) |
| total driver-hours consumed | 33.10 h against 326.6 slot-hours available |

Per-window means show the shape clearly:

| window (UTC) | h | driver runs | mean in-flight | distinct taskfiles |
|---|---|---|---|---|
| 09-08 23:32–23:59 | 0.45 | 8 | 1.00 | 1 |
| 09-09 12:56–13:17 | 0.35 | 10 | 1.00 | 1 |
| 09-09 13:44–14:43 | 0.98 | 20 | 1.88 | 1 (arc-governance-docs, 6 tasks) |
| 09-09 14:53–18:45 | 3.87 | 195 | **3.61** | 4 concurrent taskfiles |
| 09-09 19:42–04:21 | 8.64 | 134 | 1.79 | 1 at a time (post-flock) |

Distinct taskfiles with a driver in flight, time-weighted over the 14.26 h where at least one was
live: **1 taskfile 80.3% of the time**, 2+ taskfiles 19.7%.

### Is any cap binding? No.

Direct measurement of queueing: for every completed run, `(driver.done.ts − driver.start.ts) −
reported run seconds` isolates time spent waiting on the in-process semaphore
(`drivers._gate`) plus the cross-process lease (`drivers._lease_acquire`), since `driver.start`
is emitted *before* `_guarded_once` acquires either.

| model / role | n | median wait | p90 | max | total |
|---|---|---|---|---|---|
| GLM-5.3 reviewer | 37 | 0.6 s | 39.1 s | 165.9 s | 0.16 h |
| Kimi-K3 reviewer | 43 | 0.0 s | 28.9 s | 43.9 s | 0.08 h |
| GLM-5.3 implementer | 24 | 3.0 s | 23.8 s | 46.2 s | 0.06 h |
| all others | 112 | 0.0 s | ≤35 s | 58.4 s | 0.09 h |
| **total** | **216** | **0.0 s** | — | 165.9 s | **0.39 h** |

0.39 h of queueing against 33.10 driver-hours = **1.2% of driver time**. Combined with zero
`cap_wait` events, the per-model caps cost approximately nothing. The apparent "over cap"
percentages in the first table (Kimi 17.7% at/over 2) are an artefact: `driver.start` fires before
the semaphore, so a queued driver is counted as in-flight. Recomputed over only cleanly-terminated
intervals, Kimi-K3 exceeds its cap for 1.06% of active time and GLM-5.3 for 0.41%.

**Bottleneck model: Kimi-K3, but by a wide margin the least-loaded resource is the fleet as a
whole.** Kimi-K3 is the only model above 50% utilisation; the 16 slots on gpt-oss-120b and
DeepSeek-V4-Flash carry 18.0% of driver-hours while holding 76% of the slots.

---

## 2. Routing — MEASURED

Implement-attempt outcomes, from `harness_runs` ordered by time within each task. An implement run
is classified `gate_pass` when the next run in that task is a reviewer run (the graph only edges
`gate → review` on `passed`), `gate_fail` otherwise. n = 103 implement runs across 31 tasks.

| model | implement attempts | gate pass | gate fail | gate pass % | review pass \| gate passed | end-to-end % |
|---|---|---|---|---|---|---|
| gpt-oss-120b | 44 | 26 | 18 | **59.1%** | 11/26 = 42.3% | **25.0%** |
| DeepSeek-V4-Flash | 31 | 22 | 9 | **71.0%** | 16/22 = 72.7% | **51.6%** |
| GLM-5.3 | 19 | 14 | 4 | 77.8% | 6/14 = 42.9% | 31.6% |
| Kimi-K3 | 9 | 2 | 7 | 22.2% (n=9, thin) | 2/2 = 100% | 22.2% |

Per **task**, grouped by the model the task *started* on:

| starting model | tasks | escalated | solved at that tier | implement runs | runs/task | harness s/task |
|---|---|---|---|---|---|---|
| DeepSeek-V4-Flash | 13 | **0 (0%)** | 13 | 22 | **1.7** | **588** |
| gpt-oss-120b | 11 | **4 (36%)** | 7 | 61 | **5.5** | **1243** |
| GLM-5.3 | 5 | 0 | 3 (2 failed on process kill) | 11 | 2.2 | 1376 |
| Kimi-K3 | 2 | 0 | 1 | 9 | 4.5 | 2001 |

One-shot success (task merged after a single implement run): DeepSeek 7/13 (54%), gpt-oss 3/11 (27%).

### The cleanest natural experiment in the data

`selftest-untested-modules.json` contains 4 structurally identical tasks ("write unit tests for
module X", one file each, same `verify_cmd` shape). Two were routed to gpt-oss-120b, two to
DeepSeek-V4-Flash:

| task | routed to | trace | outcome |
|---|---|---|---|
| test-scheduler | DeepSeek | I×3 → R+ | merged, 702 s |
| test-pool | DeepSeek | I×3, R−, I, R+ | merged, 1112 s |
| test-events | gpt-oss | I×4 (all discarded) → DeepSeek ×3 → R+ | escalated, 1109 s |
| test-bench-data | gpt-oss | I×4 → DeepSeek ×3 → R− → GLM ×3 → R+ | escalated, 3301 s |

2/2 gpt-oss tasks escalated; 0/2 DeepSeek tasks did. n=4 — thin, but it is a controlled comparison,
and it agrees with the 11-vs-13-task aggregate above.

### The specific case: `test-bench-data`

Confirmed in `harness_runs`: 10 implement attempts, gpt-oss-120b ×4 (169 s, 131 s, 68 s, 84 s),
DeepSeek-V4-Flash ×3 (134 s, 150 s, 179 s), then a GLM-5.3 review at 604 s that failed on a scope
violation, then GLM-5.3 ×3 implements (745 s, 284 s, 362 s) and a passing Kimi-K3 review at 391 s.
3301 harness-seconds, 56 min wall clock, for one test file. The first 7 attempts (916 s) were
discarded wholesale.

**Answer to the question posed: yes, but narrowly.** gpt-oss-120b is not incapable — it merged 7 of
its 11 tasks, 3 of them first try (`create-stylesheet`, `p01-docs-oss`, `readme-link-governance`,
all single-file trivial edits). It fails on anything with a real verify gate. The measured cost of
that failure is not gpt-oss time (which is free — 1.6% utilisation) but **review time on the capped
models**:

| task started on | tasks | reviewer runs consumed | share of all reviews | reviewer seconds | share |
|---|---|---|---|---|---|
| gpt-oss-120b | 11 (35%) | **35** | **54.7%** | 7301 | **45.7%** |
| DeepSeek-V4-Flash | 13 (42%) | 19 | 29.7% | 4479 | 28.0% |
| GLM-5.3 | 5 | 8 | 12.5% | 2834 | 17.7% |
| Kimi-K3 | 2 | 2 | 3.1% | 1376 | 8.6% |

Every review is executed by Kimi-K3 or GLM-5.3 — the two capped models. Attributing *all*
Kimi+GLM harness-seconds (6.94 h) to the model each task started on: gpt-oss-started tasks consume
**38.5%** of the scarce-model budget while being 35% of tasks and 18% of tasks that finish
first-try.

Escalation path cost: 4 tasks escalated, burning **2655 s (0.74 h) of implement work at tiers that
were abandoned** = 31.1% of those tasks' harness time, 8.2% of all harness seconds.

---

## 3. Cost — MEASURED

From `harness_runs` (completed, persisted runs only — n=167):

| model | role | n | median | p90 | max | total |
|---|---|---|---|---|---|---|
| Kimi-K3 | reviewer | 38 | 214 s | 447 s | 569 s | 2.43 h |
| GLM-5.3 | reviewer | 26 | 184 s | 623 s | **939 s** | 2.01 h |
| GLM-5.3 | implementer | 19 | 291 s | 659 s | 745 s | 1.77 h |
| DeepSeek-V4-Flash | implementer | 31 | 145 s | 276 s | 480 s | 1.23 h |
| gpt-oss-120b | implementer | 44 | **57 s** | 131 s | 173 s | 0.78 h |
| Kimi-K3 | implementer | 9 | 0 s* | 806 s | 1455 s | 0.73 h |

\* five of the nine Kimi-K3 implement rows are `DriverError` rows saved with `seconds=0.0`.

By role: implementer 4.50 h (50.3%), reviewer 4.44 h (49.7%). **Review is half the wall clock and
100% of it lands on the two capped models.**

From the event log, which also covers runs that crashed or were killed (n=416, all workloads):

| model | role | n | median | p90 | max | driver-hours |
|---|---|---|---|---|---|---|
| Kimi-K3 | implementer | 91 | 125 s | 1165 s | 2779 s | **11.74** |
| GLM-5.3 | implementer | 47 | 332 s | 2700 s | 2777 s | 8.17 |
| DeepSeek-V4-Flash | implementer | 58 | 58 s | 334 s | 2700 s | 4.00 |
| GLM-5.3 | reviewer | 60 | 34 s | 577 s | 2700 s | 3.21 |
| Kimi-K3 | reviewer | 72 | 83 s | 423 s | 710 s | 3.10 |
| gpt-oss-120b | implementer | 82 | 14 s | 129 s | 2700 s | 1.97 |
| Kimi-K3 | planner | 6 | 466 s | 966 s | 976 s | 0.91 |

Kimi-K3 accounts for **15.75 of 33.10 driver-hours (47.6%)** on the model with a cap of 2.

### "Kimi-K3 reviews are slow (600s+)" — REFUTED

| reviewer | n (events) | median | p90 | max | runs ≥600 s |
|---|---|---|---|---|---|
| Kimi-K3 | 72 | 83 s | 423 s | 710 s | **1** (a duplicate-start artefact) |
| GLM-5.3 | 60 | 34 s | 577 s | 2700 s | 4 |

From the DB (completed runs): Kimi-K3 reviewer max is **569 s; 0 of 38 runs reached 600 s**.
GLM-5.3 reviewer has 4 of 26 at ≥600 s and a 939 s maximum. **GLM-5.3 is the slow reviewer, not
Kimi-K3.** The seven review runs in the whole history that exceeded 600 s were: GLM 962 s, GLM
901 s, Kimi 710 s (restart artefact), GLM 699 s (restart), GLM 694 s, Kimi 613 s, GLM 604 s.

What *is* true is that Kimi-K3 review time is expensive **per slot**: a 214 s Kimi review occupies
half the Kimi fleet, while a 184 s GLM review occupies a third of GLM's. Normalised to cap-hours
over the 15.55 h active window: Kimi 7.88 cap-hours consumed of 15.55 available (50.6%), GLM 3.79
of 15.55 (24.4%).

### Rework

| metric | value |
|---|---|
| implement runs per merged/attempted task | 103 / 31 = **3.32** |
| implement runs that were superseded | 72 of 103 = **70%** |
| implement seconds on superseded attempts | 11 827 s of 16 212 s = **73%** |
| reviews per task | 64 / 30 = **2.13** |
| tasks whose **first** review passed | 16 of 30 = **53%** |
| reviewer seconds spent on reviews that failed | 8018 s of 15 991 s = **50%** |
| failed reviews citing a scope violation / unrelated file edit | **7 of 29 (24%)**, 2429 s (0.67 h) |

---

## 4. Graph shape — MEASURED

18 taskfiles, 52 tasks, 21 dependency edges.

| metric | value |
|---|---|
| tasks with **no** deps | **32 / 52 = 61.5%** |
| mean tasks per taskfile | **2.9** |
| mean max level width (best-case simultaneous tasks in one file) | **1.8** |
| max width in any single file | 5 (`arc-governance-docs.json`) |
| files whose graph is a pure chain (width 1) | 8 of 18 |

The graph engine is **not** the constraint. Traced launch of `arc-governance-docs.json` (6 tasks, 5
with no deps): all five `alloc` nodes and five `driver.start`s fired within **0.1 s** of each other.
`graph.py::_Execution.run` creates one worker per node, so independent tasks fan out fully.
Achieved parallelism (harness-seconds ÷ makespan) on the three files that ran cleanly end to end:

| taskfile | tasks | work | makespan | parallelism |
|---|---|---|---|---|
| selftest-untested-modules.json | 4 | 1.73 h | 0.93 h | 1.85 |
| arc-governance-docs.json | 6 | 1.66 h | 0.98 h | 1.69 |
| dashboard-ui-polish.json | 3 | 1.09 h | 0.66 h | 1.64 |

Even a perfectly-fanned-out 6-task file only sustains 1.7 concurrent drivers, because task
durations are wildly unequal (in `arc-governance-docs`: implements finished at 75 s, 75 s, 157 s,
300 s and 696 s) so the tail is serial.

### Dependency edges that are not needed

Using `files_hint` overlap as the test for a genuine write conflict: **10 of 21 edges (48%) join
tasks that share no file at all.**

| taskfile | edge | shared files |
|---|---|---|
| github-pr-flow.json | pr-preflight → pr-status-ui | none |
| github-pr-flow.json | pr-status-ui → pr-docs | none |
| engine-hardening.json | gate-output-capture → gitignore-gates | none |
| fleet-observability.json | api-metrics → metrics-doc | none |
| operator-cli.json | cli-doctor → cli-docs | none |
| smoke-tasks.json | t01-html → t02-js | none |
| add-a-src-style-css… | create-stylesheet → link-stylesheet | none |
| arc-governance-docs.json | docs-model-tiers → agents-md | none |
| arc-governance-docs.json | docs-taskfile-schema → agents-md | none |
| dashboard-ui-polish.json | backend-fleet-api → index-graph-polish | none |
| minecraft-* (×8 edges) | richer-terrain → … → world-persistence | `js/main.js` (real) |
| operator-cli / engine-hardening / github-ops (×3) | — | `main.py` / `code_tasks.py` / `docs/runbook.md` (real) |

Dropping only the zero-overlap edges raises the summed max width across the 18 files from **32 to
41 (+28%)** and collapses three files from a 3-deep chain to width 3
(`github-pr-flow.json` goes depth 3 → 1). The two `minecraft-*` files stay 5-deep chains: every
edge there is a real `js/main.js` conflict.

Note: several of the zero-overlap edges are *semantic* (docs describing an API that another task
adds; a page linking a stylesheet another task creates), not conflicts. Those need
content-availability at merge time, not serialized execution — see recommendation #2.

---

## 5. The single biggest throughput constraint

**Not enough work is admitted to the fleet at once. The fleet averages 2.13 concurrent drivers
against 21 slots (10.1%), and runs exactly one driver 58.2% of active time.**

Evidence, in order of strength:

1. **Queueing is 1.2% of driver time** (0.39 h of 33.10 h; median wait 0.0 s across 216 runs) and
   there are **zero `driver.cap_wait` events in 6047 events**. The caps are not being hit.
2. **gpt-oss-120b and DeepSeek-V4-Flash sit idle 88.3% and 85.7% of active time**, at 1.6% and 3.2%
   of their 8-slot caps. Sixteen of the fleet's twenty-one slots are effectively unused.
3. **One taskfile is live 80.3% of the time** that any taskfile is live. `run-queue.sh` enforces
   this deliberately with a `flock` (line 26), and the header comment records why: on 2026-09-09
   four taskfiles started within two seconds and put "9 Kimi requests against a cap of 3". The
   event log confirms that episode — 7 `inflight.over_cap` events on 09-09 (one reading
   `inflight: 9, limit: 3`) and **75 of the 78** `provider.api_error: 400` fast-fails clustered in the
   15:00 and 17:00 hours.
4. **One taskfile is not enough work**: mean 2.9 tasks per file, mean max width 1.8, and unequal
   task durations mean the best-observed file-level parallelism is 1.85.

So the fleet is serialized at the *taskfile* level by a global lock that exists to work around a
per-model cap breach — while the per-model caps, measured, are never contended. The lock is
solving a problem the leases already solve; the cost is the other 90% of the fleet.

Secondary constraint, worth naming because it is where the *capped* models' time actually goes:
**review is 49.7% of harness seconds, is executed exclusively by the two capped models, and 50% of
review seconds are spent on reviews that fail** — 24% of those on scope violations a mechanical
check could have caught before a reviewer was ever launched.

---

## 6. Recommended changes, ranked by impact ÷ effort

### 1. Let more than one taskfile run at a time — `run-queue.sh` (the `flock` at line 26) plus a global admission gate in `drivers.Driver._guarded_once`

Replace the whole-queue `flock` with a bounded window: run *N* taskfiles concurrently (start with
N=3) and keep the existing per-file guard that refuses to run the *same* file twice. The cap
breach the flock was added to prevent is already prevented by `store.acquire_driver_lease`
(pid-liveness checked, cross-process) and `drivers._gate`; the measured contention on both is
0.39 h total.

Expected effect: fleet mean in-flight from 2.13 toward 5–6. The one window in the history where
several taskfiles overlapped (09-09 14:53–18:45) measured **mean 3.61 in-flight with a peak of 14**
— that is the only direct evidence available for the size of the gain, and it came from an
uncontrolled accident, so treat 3.6 as a floor rather than a forecast.

Before raising N, confirm the leases actually hold: the 09-09 breach reached `inflight: 9` against
a limit of 3, which either the leases failed to stop or the in-flight *accounting* mis-reported
(see #4). Ship #4 first if you want the answer rather than the symptom.

### 2. Stop routing new tasks to gpt-oss-120b; make DeepSeek-V4-Flash the entry tier — `config.ESCALATION_PATH` and `config.IMPLEMENT_TIERS`

Set `ESCALATION_PATH = "DeepSeek-V4-Flash,GLM-5.3,Kimi-K3"` and fold the `basic` tier into
`medium`. Keep gpt-oss-120b for tasks with an empty or trivial `verify_cmd`.

Measured basis: gpt-oss 5.5 implement runs/task, 36% escalation, 1243 s/task, and 54.7% of all
reviewer runs for 35% of tasks; DeepSeek 1.7 runs/task, 0% escalation, 588 s/task, 3.2% slot
utilisation (so it has capacity to absorb the work for free). Both `selftest` tasks routed to
gpt-oss escalated; neither DeepSeek one did.

Expected effect (INFERRED, see §7): if gpt-oss's 11 tasks had run at DeepSeek's measured
review-per-task rate (1.46 vs 3.18), they would have consumed ~16 reviewer runs instead of 35 —
roughly **1.3 h of Kimi/GLM slot time returned**, plus the 0.74 h of abandoned-tier implement work.

### 3. Move scope enforcement out of review and into the gate — `code_tasks.py::make_chain.gate()`

Before running `verify_cmd`, diff the worktree against `base` and fail the gate when the changed
paths fall outside `files_hint` (plus a small allowlist). Today this check is performed by a capped
model reading a 24 000-char diff.

Measured basis: **7 of 29 failed reviews (24%) cite a scope violation or an unrelated file edit,
costing 2429 s (0.67 h) of Kimi/GLM time** — e.g. `test-bench-data` (GLM, 604 s: "the diff creates
two files besides the single permitted test file"), `phone-ui-polish` (GLM, 642 s: an unrelated
deletion of `orchestrator.db-wal`), `worktree-docs-note` (GLM, twice: an unrelated `config.py`
edit). Each of those also costs a full implement retry.

Expected effect: removes ~0.67 h of capped-model time and ~7 implement retries per 30 tasks, and
catches the violation in <1 s instead of 10 minutes.

### 4. Emit `driver.queued` before the semaphore and `driver.start` after it — `drivers.py::Driver.run` / `Driver._guarded_once`

`driver.start` is emitted in `Driver.run` *before* `_guarded_once` acquires `_gate` and the lease.
Every consumer therefore counts queued drivers as running. This is why Kimi-K3 appears to exceed
its cap of 2 for 17.7% of active time (peak 6) when the clean-interval measurement says 1.06%, and
it is very likely why the `inflight.over_cap` alarm fired `inflight: 9, limit: 3` — the number that
motivated the throughput-destroying flock in #1.

Move the `driver.start` emit inside `_guarded_once` after `gate.acquire()` and
`_lease_acquire(...)` return, and emit `driver.queued` at the top of `Driver.run`.

Expected effect: no throughput on its own, but it makes #1 safe to ship and turns the in-flight
gauge and `inflight.over_cap` into something trustworthy. Low effort, high leverage.

### 5. Attach the task id to `task.gate` and `task.reviewed` — `code_tasks.py::make_chain.gate()` and `.review()`

Both emits rely on `events.set_context(module=tid)` called inside `alloc()`. `graph.py` creates one
`asyncio.Task` per node up front, and a contextvar set inside one task does not propagate to
siblings — so the module tag is lost. Measured: `task.gate` carries a module on **15 of 125**
events, `task.reviewed` on **3 of 79**. Pass `task=tid` explicitly.

While there: `store.save_harness_run(..., verdict=json.dumps(verdict)[:500])` truncates the verdict
mid-string. **21 of 64 stored verdicts are unparseable JSON** as a result (all of them `pass: false`,
recoverable only by prefix-matching). Raise the limit or store `{"pass": ..., "issues": [...]}`
with the issues truncated individually.

Expected effect: per-model gate and review rates become directly measurable from the event log
instead of inferred from run ordering, and the failure reasons stop being destroyed at 500 bytes.

### 6. Split the zero-conflict dependency edges — `code_tasks.py::plan_tasks` prompt, plus a lint in `load_taskfile`

Ten of 21 edges join tasks with no `files_hint` overlap. Add to the planner prompt: *a dep is for a
write conflict on a shared file; if two tasks touch disjoint files they must not be chained*. Add a
warning (not an error) in `load_taskfile` when an edge's endpoints share no `files_hint`.

Expected effect: summed max width across the current 18 files rises **32 → 41 (+28%)**;
`github-pr-flow.json` goes from a 3-deep chain to 3 parallel tasks. Note this only pays off once #1
lands — at one taskfile at a time, a wider file still saturates at ~2 drivers. Ranked last for that
reason, not because the edges are correct.

**Not recommended:** raising `driver_limit` for any model. Nothing is waiting on those caps.

---

## 7. MEASURED vs INFERRED

**Measured** (directly counted from `orchestrator.db` or `logs/events.jsonl`):

- All concurrency distributions, idle/at-cap percentages and means (§1). n=416 intervals, 15.55 h.
- Queue-time table and the 0.39 h total (§1). n=216 completed runs.
- `driver.cap_wait` = 0, `inflight.over_cap` = 7 (§0).
- All gate/review outcome rates and per-task escalation counts (§2). n=103 implement runs, 64
  reviewer runs, 31 tasks.
- All latency percentiles (§3). n=167 DB rows, n=416 event intervals.
- The Kimi-K3 review-latency refutation (§3). n=72 events / 38 DB rows.
- Rework, first-review-pass and scope-violation counts (§3). n=103 / 30 / 29.
- Task counts, dep edges, `files_hint` overlap and widths (§4). n=18 files, 52 tasks, 21 edges.
- The 0.1 s fan-out trace and the 1.85/1.69/1.64 achieved-parallelism figures (§4).

**Inferred** (reasoning on top of the measurements — state these as hypotheses):

- *Gate pass/fail per implement run* is inferred from run ordering (an implement followed by a
  reviewer run ⇒ its gate passed), because `task.gate` events carry no task id. The inference is
  sound given the graph edges in `code_tasks.py` (`gate → review when passed`) and is
  self-consistent: the 64 inferred gate-passes exactly equal the 64 reviewer runs.
- *gpt-oss-120b is mis-routed rather than unlucky.* The aggregate (11 vs 13 tasks) does not control
  for task difficulty, which is not recorded anywhere. The `selftest-untested-modules` comparison
  does control for it but has **n=4 tasks (2 per arm)**. Treat the direction as well-supported and
  the magnitude as an estimate.
- *Expected effect of running 3 taskfiles concurrently.* Extrapolated from one 3.87 h window
  (09-09 14:53–18:45) that was not a controlled experiment and coincided with a cap breach and a
  400-error storm. Only a floor, not a forecast.
- *Savings from re-routing gpt-oss work to DeepSeek* (~1.3 h of capped-model time) assume the
  re-routed tasks behave like DeepSeek's existing tasks. Unverified.
- *The `inflight: 9, limit: 3` breach was an accounting artefact* rather than a real breach. The
  `driver.start`-before-semaphore ordering makes this very likely, but the leases could genuinely
  have failed. Recommendation #4 is what distinguishes the two.

**Too thin to support a claim:**

- **Kimi-K3 as an implementer.** 9 DB rows, of which 5 have `seconds = 0.0` (crash paths), and
  2/9 gate passes. Both Kimi-implemented tasks in the DB come from one taskfile. Nothing about
  Kimi-K3's implement quality can be concluded. Its 91 event-log implement runs and 11.74
  driver-hours are real and dominated by a single pathological task (`index-graph-polish`).
- **GLM-5.3's 42.9% review-pass rate as an implementer** (n=14 gate-passing runs). Directionally
  worse than DeepSeek, but the confidence interval is wide.
- **`driver.stale` costs.** 23 events, median age 2940 s, up to 10 138 s, 28.3 h of nominal
  in-flight time. These are runs whose orchestrator process died, so the interval end is unknown
  and the "cost" is not real capacity — `store.acquire_driver_lease` reaps dead pids on every call
  and the `driver_leases` table is currently empty. It pollutes the in-flight gauge, nothing more.
- **The idle-stall kills.** 36 kills, but 34 occurred under an older configuration
  (`DRIVER_IDLE_TIMEOUT` of 120 s or 300 s and `DRIVER_TIMEOUT` of 900 s); the current values are
  420 s and 2700 s. Only two kills happened at the current settings — and both look wrong:
  `index-graph-polish` killed at 2700 s with 115.8 s idle and 123 KB written, and `dag-chaining`
  killed at 2700 s with **51 s idle and 1.92 MB written**. Both were demonstrably still producing
  output. n=2 is not enough to justify raising `DRIVER_TIMEOUT` again, but it is enough to watch.

**One finding outside the four questions, worth flagging:** of the 9 tasks in `code_tasks` with
`status='failed'`, **only 1 failed for capability reasons** (`docs-dashboard-ui`, "exhausted fix
rounds"). Seven failed with "interrupted: run process exited before the task finished" or
"reset-stale: owning run process died", and one with "run crashed: kimi driver 400". Combined with
25 `run.resume` events and 23 `driver.stale` events, the dominant cause of lost work in this
history is **the orchestrator process dying mid-run**, not the models. That is a reliability
problem, not a throughput one, so it is outside this report's scope — but it is larger than
anything in §6.
