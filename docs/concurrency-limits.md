# Concurrency limits (how many agents at once)

This is the reference for "how many harness instances run at the same time"
in the multi-harness code workload. It is checked against `config.py`,
`drivers.py`, `scheduler.py`, `pool.py`, `graph.py` and `code_tasks.py`.

There are **two independent layers** of limits, and they mean different
things:

| Layer | Where it is enforced | Ceiling | Applies to |
|---|---|---|---|
| Per-account API caps | The ARC API itself (server-side, per API key) | 17 across all models | **All** processes sharing the key |
| Per-process driver semaphores | `drivers.py` in this process | 10 across all models | One orchestrator run |

The two are not the same number on purpose. The account caps are the hard
ceiling the API will reject you for exceeding. The driver semaphores are what
this process actually enforces, and they sit **below** the account caps.

## Measured caps (2026-09-10)

The configured limits were a guess and two of them were wrong. Measured by
ramping concurrent requests per model until ARC rejected, counting the fleet's
own in-flight usage:

| model | measured concurrent | was configured | verdict |
| --- | --- | --- | --- |
| gpt-oss-120b | **5** | 10 account / 8 drivers | over-subscribed by 3 |
| DeepSeek-V4-Flash | **5** | 10 account / 8 drivers | over-subscribed by 3 |
| GLM-5.3 | **4** | 4 account / 3 drivers | one slot wasted |
| Kimi-K3 | **3** | 3 account / 2 drivers | one slot wasted |

Over-subscription is not harmless: the fleet generated its own
`400 concurrent session limit reached`, and the capacity backoff then treated
it as the provider being busy. Under-subscription silently wasted capacity.

`config._MEASURED_CONCURRENCY` now holds these numbers and both the account
limit and the driver cap derive from them, so they cannot drift apart again.
`tests/test_config.py` fails if any model's driver cap exceeds its account cap.

**Since that measurement** (2026-09-12): DeepSeek-V4.1-Flash-thinking-max
joined the roster with a **provider-published** limit of 10 concurrent (ARC
docs updated 2026-09-12) — a published figure, not a ramped measurement like
the ones above, so re-measure it if observed rejections disagree. The two
retired rows stay above as history: gpt-oss-120b left the fleet on 2026-09-11,
and DeepSeek-V4-Flash was retired on 2026-09-12 when the provider removed the
model from the API.

**`ARC_DRIVER_HEADROOM`** (default 0) reserves slots per model for interactive
use of the same ARC account. Set it to `1` if you want to run an interactive
`kimi` alongside the fleet without contending; at 0 the fleet uses everything
and an interactive session competes with it — a rejection there is retried on
the capacity backoff, not fatal, but it will feel slow.

To re-measure after a plan change, ramp concurrency per model and find where
`400 concurrent session limit reached` starts.


## 0. The THIRD layer: the harness pool

Before the per-model layers below, there is a limit that is easy to miss and is
frequently the binding one: **every opencode-backed model shares ONE local
binary and ONE ~240MB sqlite store** in `~/.local/share/opencode`. The
per-model driver caps permit GLM 2 + DeepSeek 5 = **7** concurrent opencode
processes against it — still above the pool of 5.

Measured 2026-09-10, identical prompt, warm cache:

| concurrent | 3 | 4 | 5 | 6 | 10 |
|---|---|---|---|---|---|
| succeeded | 3/3 | 4/4 | 5/5 | 4/6 | 4/10 |

Past five it fails fast with an **empty stderr**. The fleet logged that as
`opencode exited 1: ` — a message that says a run died and nothing about why —
and its retry ladder repeated it four times per task, so self-inflicted
contention looked like a provider outage. 42 such errors in half an hour.

`config.harness_limit(harness)` caps it (opencode 5, dsh 5), enforced by
`drivers._harness_gate` in-process and by a `harness:<name>` row in the same
`driver_leases` table across processes.

| Harness | Cap | Override |
|---|---|---|
| opencode (GLM-5.3) | 5 | `ARC_HARNESS_LIMIT_OPENCODE` |
| dsh (DeepSeek-V4.1-Flash-thinking-max) | 5 | `ARC_HARNESS_LIMIT_DSH` |

dsh (DeepSeek's own harness, swapped in 2026-09-12) keeps its state as
per-profile JSON files under `$DSH_HOME` — no central store like opencode's
sqlite — so the opencode cliff does not obviously transfer; 5 mirrors it
until a load test says otherwise.

Acquisition order is **model gate → model lease → harness gate → harness
lease**, always. One global order means no circular wait, and the scarce
harness slot is never held while queueing for a plentiful model slot.

A consequence worth internalising: **a model under its own cap is not
available if its harness is full.** `code_tasks._reviewer_pressure` therefore
scores a candidate reviewer on whichever ceiling binds first. Scoring on the
model alone sent reviews to GLM and DeepSeek while the opencode pool they
share sat at 5/5 with seven reviewers queued behind it — and the kimi harness
idle at 1/3.

Graph admission — bounding fanout WITHIN a run
----------------------------------------------

The caps above bound each model and harness, but nothing used to stop one run
from STARTING more work than they could serve: the graph engine began every
root node at once, and the excess queued inside `drivers._lease_acquire`
holding a worktree and a DB row while doing nothing. `Graph(max_in_flight=...)`
bounds how many nodes execute concurrently within one run; the rest wait on
their per-node queues holding no driver slot. `code_tasks.build_code_graph`
sets it from `config.max_tasks_in_flight()` — default the total harness
capacity (opencode 5 + kimi 3 = 8), override **`ARC_MAX_TASKS_IN_FLIGHT`**.
The default exceeds the root count of every taskfile measured so far, so
unconfigured runs behave exactly as before.

## 1. Two layers of limits

### Layer 1 — per-account API caps (`config.FAMILIES`)

`config.FAMILIES` is the model registry. Each family's `limit` is the number
of concurrent requests ARC allows for that family on one account key
(`pool.py` builds a per-family semaphore from it, but the account cap is
really server-side):

| Model (family) | Account cap |
|---|---|
| DeepSeek-V4.1-Flash-thinking-max (`deepseek`) | 10 |
| GLM-5.3 (`glm`) | 4 |
| Kimi-K3 (`kimi`) | 3 |

For the **research workload** `pool.py` enforces these client-side as an
`asyncio.Semaphore(config.family_limit(f))` per family (`pool.py:79`), so a
single process never exceeds the per-family caps. Those semaphores live on the
`ArcPool` instance (`self.sems`), so they are **process-local** — each
`main.py` process builds its own set. Across processes the account cap is the
real shared ceiling, and it is enforced by the ARC API itself: an over-limit
request is rejected with HTTP 400, which `pool._is_session_limit` recognises by
"session limit" / "concurrent" (`pool.py:50-55`).

### Layer 2 — per-process driver semaphores (`drivers._MODEL_DRIVER_CAP`)

The **code workload** does not call the API through `pool.chat`. It shells out
to the `kimi` and `opencode` CLIs (`drivers.py`), each of which makes its own
API calls. Concurrent harness instances are bounded by a **per-model**
semaphore, not a per-family one:

```python
# config.py
# Derived from the live ROSTER rows — DeepSeek-V4.1-Flash-thinking-max's 10
# is the provider-published figure (2026-09-12), the rest are measured.
_MEASURED_CONCURRENCY = {"Kimi-K3": 3, "GLM-5.3": 4,
                         "DeepSeek-V4.1-Flash-thinking-max": 10}
_SESSIONS_PER_PROCESS = {"opencode": 2, "kimi": 1}
DRIVER_HEADROOM = int(os.getenv("ARC_DRIVER_HEADROOM", "0"))
_MODEL_DRIVER_CAP = {m: max(1, n // _SESSIONS_PER_PROCESS[harness_of(m)]
                            - DRIVER_HEADROOM)
                     for m, n in _MEASURED_CONCURRENCY.items()}
```

| Model | ARC sessions | Sessions per process | Driver cap |
|---|---|---|---|
| DeepSeek-V4.1-Flash-thinking-max | 10 | 2 (opencode) | 5 |
| GLM-5.3 | 4 | 2 (opencode) | 2 |
| Kimi-K3 | 3 | 1 (kimi CLI) | 3 |

A harness PROCESS is not an ARC SESSION. An opencode run issues parallel tool
calls and holds about two sessions at once, so a driver cap equal to the
session limit asks for twice the budget. Measured over four hours: 23 capacity
rejections, GLM-5.3 refused with as few as TWO drivers live against a ceiling
of four.

Remember the harness pool above sits UNDER these: the two opencode models
share five slots between them, so their per-model caps are reached only when
the other is idle.

`drivers._gate(model)` lazily creates an `asyncio.Semaphore(config.driver_limit(model))`
per model (`drivers.py:60`). `Driver.run` does `await gate.acquire()` before
launching the subprocess and releases it on every exit path (`drivers.py:605`,
`drivers.py:686`).
These semaphores are process-local (a module-level dict), so **each** `main.py
code run` process gets its own set.

### Layer 2b — cross-process driver leases (`store.driver_leases`)

Process-local semaphores have a hole: a terminal queue (`run-queue.sh`), a
`main.py code run` spawned from the dashboard's Run buttons, and a bench run
each hold their own full set of caps, so N processes can stack to N× the
account limit. The lease table closes it: after taking the semaphore,
`Driver.run` calls `drivers._lease_acquire`, which inserts a row into
`driver_leases` (model, pid, task, acquired_at) only if the model currently
has fewer live rows than `config.driver_limit(model)`. Over cap, the task
**waits**, polling every 20 s and emitting `driver.cap_wait {model, task,
in_use, cap}` about once a minute — that event is the fleet's "concurrency
limit reached" warning. Rows are reaped when older than
`config.DRIVER_LEASE_TTL` (default 3300 s, `ARC_DRIVER_LEASE_TTL`) or owned by
a dead pid, so a killed run frees its slots within seconds and a crashed one
within the TTL. All of this is in addition to — never instead of — the
semaphore: the semaphore is the fast in-process path, the lease is the
cross-process truth.

Summing the driver caps: **5 + 2 + 3 = 10**. This is the maximum number
of harness instances one orchestrator run can have in flight at once.

```
DeepSeek-V4.1-max  5   ← hard implementers + planner/reviewers (opencode)
GLM-5.3            2   ← medium implementers + reviewers (opencode)
Kimi-K3            3   ← hard implementers + reviewers (kimi CLI)
────────────────
                  10   per-process ceiling
```

## 2. Why driver caps sit below account caps

Two mechanisms keep a driver cap below its account cap.

The first is the sessions-per-process factor: an opencode run holds about two
ARC sessions at once (parallel tool calls), so its caps are the account cap
**divided by two** — DeepSeek 10 → 5, GLM 4 → 2. The kimi CLI holds one
session per process, so Kimi-K3's cap equals its account cap of 3. Setting an
opencode cap equal to the session limit asks for twice the budget and the
fleet generates its own 400s. On top of the division, `ARC_DRIVER_HEADROOM`
(default 0) subtracts a flat reserve per model.

The second is politeness. The account key is shared with the user's own
interactive sessions, so the orchestrator must leave room for them:

- `config.driver_limit(model, interactive=False)` gives batch callers on the
  planner model (DeepSeek-V4.1-Flash-thinking-max) one slot **fewer**
  (`INTERACTIVE_RESERVE`, default 1, giving 4 in batch), so an interactive
  chat always has somewhere to land instead of queueing behind a full fleet.
  It is reserved only on the planner model — applying it to every model
  halved GLM and DeepSeek to protect a path neither of them serves.
- With `ARC_DRIVER_HEADROOM=1` every model's cap drops by one, leaving a
  measured slot for your own `kimi` or `opencode` session during a run.

So on a machine where `kimi-code` and `opencode` are also used by hand, a run
does not starve the interactive agents.

## 3. Semaphore queueing — what actually happens

When more tasks become runnable than there are free slots, **the graph does
not reorder tasks**; the driver semaphore arbitrates. The behaviour comes
from `graph.py` and `code_tasks.py`:

`scheduler.py` and `pool.py` bound a *different* workload (the research
pipeline): `scheduler.Supervisor` launches at most `PIPELINE_ROUNDS`
concurrent *rounds* (`scheduler.py:50`, default 2), and `pool.py` bounds
concurrent API *requests* per family. Neither throttles the code fleet — the
code workload has no central scheduler. Its parallelism is decided entirely by
the DAG in `graph.py` plus the per-model driver semaphores in `drivers.py`.

1. **Runnability is DAG-driven, not slot-driven.** `build_code_graph`
   (`code_tasks.py:150`) wires each task's chain
   `alloc → implement → gate → review → publish`, and a task with `deps` is
   *not* a start node — instead there is an edge
   `publish_<dep> → alloc_<task>` (`code_tasks.py:266-269`). A task (and the
   rest of its chain) only becomes runnable once its dependency has been
   published/merged. Tasks with no `deps` are graph start nodes.
2. **Start nodes all launch at once.** `_Execution.run` seeds every start
   node immediately (`graph.py:118-120`); each node runs in its own worker
   coroutine pulling from its own `asyncio.Queue`. The graph will happily
   dispatch *every* runnable node's function concurrently — it has no global
   concurrency cap of its own (the guard is `MAX_GRAPH_STEPS`, fired-node
   count, not parallelism).
3. **The model semaphore is the real throttle.** A runnable `implement` /
   `review` node calls `driver.run`, which does `await gate.acquire()` on
   that model's semaphore. If the model already has `driver_limit` harnesses
   in flight, the acquire **blocks** — the node's coroutine parks there, its
   slot in the graph stays "in flight", and the task simply waits.
4. **Which task gets the slot is opportunistic.** Waiters are woken in the
   order they called `acquire()` (asyncio's semaphore is fair, FIFO), but the
   order in which node workers reach `acquire` is the order the DAG makes them
   runnable plus asyncio scheduling — not any explicit task priority. There is
   no "queue the leftover tasks in a list and run the most important first".

Net effect: with a big task batch, **all** runnable no-dep tasks are dispatched
and the extra ones park on the GLM semaphore (the tightest cap at 2). That is
why the effective parallelism of a run is governed by the driver caps — in
practice **GLM-5.3 (2)** bottlenecks a medium-heavy batch, while the review
firings for GLM and Kimi work all land on `deepseek` and compete with the
implementations for the shared opencode pool of 5.

## 4. Environment overrides

Both layers can be overridden per family with env vars. The suffixes match the
**family** name from `config.FAMILIES`, not the model name.

### `ARC_LIMIT_<FAMILY>` — override the account cap layer

`config.family_limit` builds the name by uppercasing the family key and
replacing `-` with `_` (`config.py:112-124`):

```python
override = os.getenv(f"ARC_LIMIT_{name.upper().replace('-', '_')}")
```

| Family | Env var |
|---|---|
| deepseek | `ARC_LIMIT_DEEPSEEK` |
| glm | `ARC_LIMIT_GLM` |
| kimi | `ARC_LIMIT_KIMI` |

Example: `ARC_LIMIT_KIMI=1`. This raises/lowers the **account-cap** layer
(used by `pool.py`'s families and reported as `capacity`). It does **not**
change the driver semaphores.

### `ARC_DRIVER_LIMIT_<FAMILY>` — override the driver semaphore layer

`config.driver_limit` maps the model to its family first, then builds the same
suffix (`config.py:708-721`):

```python
override = os.getenv(f"ARC_DRIVER_LIMIT_{MODEL_FAMILY[model].upper().replace('-', '_')}")
```

`MODEL_FAMILY` is derived from the live roster (`config.ROSTER`) and maps
`DeepSeek-V4.1-Flash-thinking-max → deepseek`, `GLM-5.3 → glm`,
`Kimi-K3 → kimi`, so:

| Model (family) | Env var |
|---|---|
| DeepSeek-V4.1-Flash-thinking-max (`deepseek`) | `ARC_DRIVER_LIMIT_DEEPSEEK` |
| GLM-5.3 (`glm`) | `ARC_DRIVER_LIMIT_GLM` |
| Kimi-K3 (`kimi`) | `ARC_DRIVER_LIMIT_KIMI` |

Example: `ARC_DRIVER_LIMIT_KIMI=1` (the README's example).

### When to raise them

| Situation | What to do |
|---|---|
| **Dedicated box** — no interactive `kimi`/`opencode` sessions share the key | Raise the driver caps toward the account caps (e.g. `ARC_DRIVER_LIMIT_GLM=4`, `ARC_DRIVER_LIMIT_KIMI=3`) to run the fleet flat-out. |
| You have a **higher account tier** | Raise `ARC_LIMIT_<FAMILY>` *and* the matching `ARC_DRIVER_LIMIT_<FAMILY>`. The account cap is server-side, so raising only the driver cap can hit the API's 400 "session limit" rejection. |
| **Shared box** (you also use `kimi-code` / `opencode` by hand) | Keep defaults. The whole point of the driver caps is to leave headroom for your own sessions. |

Never set a driver cap above the account cap for a family — you would only
trade "polite headroom" for hard API rejections and retry churn.

## 5. Related knobs

These bound a *single* task, not the parallelism. All live in `config.py` and
are overridable from `.env`.

| Knob | Default | What it does |
|---|---|---|
| `DRIVER_TIMEOUT` | 2700 s | Per-harness-subprocess runtime cap. `drivers.Driver._once` sets `deadline = t0 + config.DRIVER_TIMEOUT`; if the child is still producing output past it, it is killed and the run raises `DriverError` (`drivers.py:799`). |
| `GATE_TIMEOUT` | 180 s | Cap on the deterministic verify gate. `code_tasks.gate` runs `verify_cmd` via `asyncio.wait_for(proc.communicate(), config.GATE_TIMEOUT)`; on timeout it kills the child and returns `passed=False` (`code_tasks.py:924-947`). |
| `MAX_FIX_ROUNDS` | 8 | Bounded (re)implement↔review fix loop per task. `code_tasks.py` re-fires `implement_<tid>` after a failed gate/review while `runs["implement_<tid>"] <= config.MAX_FIX_ROUNDS`, else it escalates one tier up `config.ESCALATION_PATH` with a fresh fix budget; the task fails only when the last tier exhausts. |
| `MAX_RETRIES` | 12 | Per-firing retry budget in `drivers.Driver.run`: an attempt that raises `DriverError` retries (exponential backoff `2^attempt`, capped at 30 s; capacity errors use the longer `DRIVER_CAPACITY_BACKOFF` ladder) until `attempt > config.MAX_RETRIES`, then re-raises and fails the attempt (`drivers.py:666`). |

Note `config.MAX_RETRIES` is also used by `pool.py` for API request retries
in the research workload; in the code workload the driver retry loop above is
what governs a single harness firing.

### `ARC_DSH_BIN` — where the dsh CLI lives

`config.dsh_bin()` resolves the DeepSeek harness binary: `$ARC_DSH_BIN` when
set, otherwise `dsh` found on PATH, otherwise the npm-global install location
recorded on 2026-09-12 (`~/.local/opt/node/bin/dsh`).

### `ARC_ALLOW_SAME_FAMILY_REVIEW` — TEMPORARY review-policy override

Operator-authorized 2026-09-12 while GLM-5.3's provider backend is unstable:
setting this to `1` lets a task's pre-merge review and its PR reviewers come
from the implementer's own family (DeepSeek reviewing DeepSeek). Consumed by
`code_tasks.load_taskfile` and `code_tasks._eligible_pr_reviewers`. It
suspends the cross-review requirement — reviews are no longer an independent
reading by a different harness — so take it back out as soon as GLM-5.3 is
stable again.

## 6. Worked example — 12 tasks, first wave

A task file has 12 tasks: 8 medium (GLM-5.3) and 4 hard (2 routed to
DeepSeek-V4.1-Flash-thinking-max, 2 to Kimi-K3), all with no `deps` (so all
are graph start nodes and become runnable at once). Driver caps: GLM-5.3 = 2,
DeepSeek-V4.1-Flash-thinking-max = 5, Kimi-K3 = 3 — and the opencode harness
pool = 5 shared by the two opencode models.

**First wave — the implementers (12 of them) all start.** This time it is a
per-model cap, not the harness pool, that bites first:

| Implementer | Need | Cap | Runs? |
|---|---|---|---|
| GLM-5.3 | 8 | 2 | 2 run, 6 queue on the model semaphore |
| DeepSeek-V4.1-Flash-thinking-max | 2 | 5 | ✓ |
| Kimi-K3 | 2 | 3 | ✓ (own harness) |

GLM's own cap of 2 binds before the opencode pool: DS-max 2 + GLM 2 = 4
processes against a pool of 5, so all four opencode processes run and six
medium tasks park on `drivers._gate("GLM-5.3")` in FIFO order. Kimi runs
immediately because the `kimi` CLI is a separate harness. **All 12
implementations do not run concurrently** in the first wave.

**What queues next — the reviews.** Cross-review is family-based: the 8 GLM
tasks and the 2 Kimi tasks are all reviewed by `deepseek`
(DeepSeek-V4.1-Flash-thinking-max), and the 2 DS-max tasks are reviewed by
`kimi`. That is ten review firings on DS-max against a driver cap of 5 —
and those reviews compete with the medium implementations still draining
through GLM for the same five opencode harness slots. The kimi reviews (2)
fit under Kimi's own cap of 3.

**Why this is the bottleneck.** GLM-5.3 (cap 2) paces any medium-heavy batch,
and the opencode pool (5) then has to serve the remaining medium
implementations AND the deepseek review storm at once — the harness layer,
not any model cap, is what throttles the reviews. The kimi CLI keeps its own
three slots beside all that. The per-process ceiling of 10 is only approached
when a batch is heavy on both hard models and medium work at the same time.

## Cross-references

- [../AGENTS.md](../AGENTS.md)
- [orchestration-contract.md](orchestration-contract.md)
- [model-tiers.md](model-tiers.md)
- [taskfile-schema.md](taskfile-schema.md)
- [runbook.md](runbook.md)

## 7. Waiting for a slot: push, with the poll as the backstop

`drivers._lease_acquire` used to sleep up to 20 s between attempts. A slot
freed one second after a poll went unnoticed for the remaining nineteen — and
because PR reviewers are the scarcest resource in the fleet, that delay landed
on precisely the handoffs that matter most.

`workqueue.py` adds a push channel. `_lease_release(model, task)` notifies the
topic `slot:<model>` (and `slot:harness:<name>` for a harness lease, the same
key the lease itself uses); a waiter subscribes to that topic and its wait
returns the moment a slot opens. Measured end to end through the real driver
path: **0.5 s instead of up to 20 s.**

It is deliberately **not** load-bearing. Notification is an AF_UNIX datagram,
which can be dropped; a subscriber can die without unregistering; a notify can
race a subscribe. So every wait still takes a timeout, the poll loop is still
what guarantees progress, and a box where the socket or the database cannot be
opened runs the fleet exactly as before, only slower. Push makes the answer
arrive sooner; it is not the thing that makes the answer correct.

Releasing never raises. It runs in a `finally`, and an exception there would
replace whatever error the attempt was already unwinding with a sqlite
traceback from a cleanup path. A release that fails is recoverable on its own —
the lease has a TTL and the reaper also drops rows whose pid is gone.

## 8. The durable work queue (`workqueue.py`)

The same module backs a general queue, used where work must survive the process
that scheduled it. `run-queue.sh` keeps its pending list in the shell's argv, so
killing the queue loses every task file that had not started yet.

| Property | How |
|---|---|
| Durable | Rows in the same sqlite database as everything else |
| Idempotent enqueue | `UNIQUE(topic, dedupe_key)` as a **partial** index over `pending`/`claimed` only — live work cannot be enqueued twice, and a finished key is free for a legitimate re-run later |
| At-least-once delivery | `claim` takes a **lease**; `reclaim` returns expired ones, so a dead worker's item is not lost |
| Exactly-once *effect* | The caller's job. `complete` returns False if the item was already finished by whoever took over its reclaimed lease, so a superseded worker is told rather than silently accepted |
| Push | `notify(topic)` / `subscribe(topic)`, with `wait(timeout)` and `wait_async(timeout)` |

Named `workqueue`, not `queue`: this repo's modules live in the root and every
entrypoint puts that root on `sys.path`, so a `queue.py` here shadows the
standard library's for the whole process — `concurrent.futures` imports it.
