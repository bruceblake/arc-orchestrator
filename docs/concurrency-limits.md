# Concurrency limits (how many agents at once)

This is the reference for "how many harness instances run at the same time"
in the multi-harness code workload. It is checked against `config.py`,
`drivers.py`, `scheduler.py`, `pool.py`, `graph.py` and `code_tasks.py`.

There are **two independent layers** of limits, and they mean different
things:

| Layer | Where it is enforced | Ceiling | Applies to |
|---|---|---|---|
| Per-account API caps | The ARC API itself (server-side, per API key) | 27 across all models | **All** processes sharing the key |
| Per-process driver semaphores | `drivers.py` in this process | 21 across all models | One orchestrator run |

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
per-model caps permit GLM 4 + DeepSeek 5 + gpt-oss 5 = **14** concurrent
opencode processes against it.

Measured 2026-09-10, identical prompt, warm cache:

| concurrent | 3 | 4 | 5 | 6 | 10 |
|---|---|---|---|---|---|
| succeeded | 3/3 | 4/4 | 5/5 | 4/6 | 4/10 |

Past five it fails fast with an **empty stderr**. The fleet logged that as
`opencode exited 1: ` — a message that says a run died and nothing about why —
and its retry ladder repeated it four times per task, so self-inflicted
contention looked like a provider outage. 42 such errors in half an hour.

`config.harness_limit(harness)` caps it (opencode 5, kimi 3), enforced by
`drivers._harness_gate` in-process and by a `harness:<name>` row in the same
`driver_leases` table across processes.

| Harness | Cap | Override |
|---|---|---|
| opencode (GLM, DeepSeek, gpt-oss) | 5 | `ARC_HARNESS_LIMIT_OPENCODE` |
| kimi (Kimi-K3 only) | 3 | `ARC_HARNESS_LIMIT_KIMI` |

The kimi CLI keeps no shared store, so its cap is simply Kimi-K3's own and
raising it buys nothing.

Acquisition order is **model gate → model lease → harness gate → harness
lease**, always. One global order means no circular wait, and the scarce
harness slot is never held while queueing for a plentiful model slot.

A consequence worth internalising: **a model under its own cap is not
available if its harness is full.** `code_tasks._reviewer_pressure` therefore
scores a candidate reviewer on whichever ceiling binds first. Scoring on the
model alone sent reviews to GLM and DeepSeek while the opencode pool they
share sat at 5/5 with seven reviewers queued behind it — and the kimi harness
idle at 1/3.

## 1. Two layers of limits

### Layer 1 — per-account API caps (`config.FAMILIES`)

`config.FAMILIES` is the model registry. Each family's `limit` is the number
of concurrent requests ARC allows for that family on one account key
(`pool.py` builds a per-family semaphore from it, but the account cap is
really server-side):

| Model (family) | Account cap |
|---|---|
| gpt-oss-120b (`gpt-oss`) | 10 |
| DeepSeek-V4-Flash (`deepseek`) | 10 |
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
_MEASURED_CONCURRENCY = {"Kimi-K3": 3, "GLM-5.3": 4,
                         "gpt-oss-120b": 5, "DeepSeek-V4-Flash": 5}
DRIVER_HEADROOM = int(os.getenv("ARC_DRIVER_HEADROOM", "0"))
_MODEL_DRIVER_CAP = {m: max(1, n - DRIVER_HEADROOM)
                     for m, n in _MEASURED_CONCURRENCY.items()}
```

| Model | Driver semaphore cap |
|---|---|
| gpt-oss-120b | 5 |
| DeepSeek-V4-Flash | 5 |
| GLM-5.3 | 4 |
| Kimi-K3 | 3 |

Remember the harness pool above sits UNDER these: the three opencode models
share five slots between them, so their per-model caps are reached only when
the other two are idle.

`drivers._gate(model)` lazily creates an `asyncio.Semaphore(config.driver_limit(model))`
per model (`drivers.py:44-50`). `Driver.run` does `await gate.acquire()` before
launching the subprocess and `gate.release()` when it finishes (`drivers.py:123,137`).
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

Summing the driver caps: **8 + 8 + 3 + 2 = 21**. This is the maximum number
of harness instances one orchestrator run can have in flight at once.

```
gpt-oss-120b  8   ← basic implementers (opencode)
DeepSeek-V4   8   ← medium implementers (opencode)
GLM-5.3       3   ← hard implementers + planners/reviewers (opencode)
Kimi-K3       2   ← hard implementers + planners/reviewers (kimi CLI)
────────────────
             21   per-process ceiling
```

## 2. Why driver caps sit below account caps

The driver caps are the account cap **minus headroom**. The account key is
shared with the user's own interactive sessions, so the orchestrator must stay
polite. `config.driver_limit` is documented as "Max concurrent harness
instances for a model (ARC cap minus headroom)" (`config.py:128`), and the
module docstring in `drivers.py` says ARC rejects over-limit requests per
model, so the per-model semaphores cap concurrent harness instances *below*
the account limits.

The headroom matters because:

- **Kimi-K3** — you run the `kimi` CLI interactively. Account cap 3, driver
  cap 2 leaves exactly 1 slot for your own session during a run.
- **GLM-5.3** — account cap 4, driver cap 3 leaves 1 slot.
- **gpt-oss-120b / DeepSeek-V4-Flash** — account cap 10, driver cap 8 leaves
  headroom, and also absorbs some of the burst/retry load.

So on a machine where `kimi-code` and `opencode` are also used by hand, a run
does not starve the interactive agents. This headroom is the *reason* driver
caps are deliberately lower than the account caps.

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
and the extra ones park on the GLM/Kimi semaphore (the tightest caps). That is
why the effective parallelism of a run is governed by the driver caps — in
practice the **GLM-5.3 + Kimi-K3** pair (3 + 2 = 5) is the bottleneck, since
every review (and every hard implementation) needs one of them.

## 4. Environment overrides

Both layers can be overridden per family with env vars. The suffixes match the
**family** name from `config.FAMILIES`, not the model name.

### `ARC_LIMIT_<FAMILY>` — override the account cap layer

`config.family_limit` builds the name by uppercasing the family key and
replacing `-` with `_` (`config.py:88-100`):

```python
override = os.getenv(f"ARC_LIMIT_{name.upper().replace('-', '_')}")
```

| Family | Env var |
|---|---|
| gpt-oss | `ARC_LIMIT_GPT_OSS` |
| deepseek | `ARC_LIMIT_DEEPSEEK` |
| glm | `ARC_LIMIT_GLM` |
| kimi | `ARC_LIMIT_KIMI` |

Example: `ARC_LIMIT_KIMI=1`. This raises/lowers the **account-cap** layer
(used by `pool.py`'s families and reported as `capacity`). It does **not**
change the driver semaphores.

### `ARC_DRIVER_LIMIT_<FAMILY>` — override the driver semaphore layer

`config.driver_limit` maps the model to its family first, then builds the same
suffix (`config.py:127-135`):

```python
override = os.getenv(f"ARC_DRIVER_LIMIT_{MODEL_FAMILY[model].upper().replace('-', '_')}")
```

`MODEL_FAMILY` (`config.py:118-123`) maps `gpt-oss-120b → gpt-oss`,
`DeepSeek-V4-Flash → deepseek`, `GLM-5.3 → glm`, `Kimi-K3 → kimi`, so:

| Model (family) | Env var |
|---|---|
| gpt-oss-120b (`gpt-oss`) | `ARC_DRIVER_LIMIT_GPT_OSS` |
| DeepSeek-V4-Flash (`deepseek`) | `ARC_DRIVER_LIMIT_DEEPSEEK` |
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
| `DRIVER_TIMEOUT` | 2700 s | Per-harness-subprocess runtime cap. `drivers.Driver._once` sets `deadline = t0 + config.DRIVER_TIMEOUT`; if the child is still producing output past it, it is killed and the run raises `DriverError` (`drivers.py:166-180`). |
| `GATE_TIMEOUT` | 180 s | Cap on the deterministic verify gate. `code_tasks.gate` runs `verify_cmd` via `asyncio.wait_for(proc.communicate(), config.GATE_TIMEOUT)`; on timeout it kills the child and returns `passed=False` (`code_tasks.py:199-203`). |
| `MAX_FIX_ROUNDS` | 3 | Bounded (re)implement↔review fix loop per task. `code_tasks.py` re-fires `implement_<tid>` after a failed gate/review while `runs["implement_<tid>"] <= config.MAX_FIX_ROUNDS`, else it fires `fail_<tid>` (`code_tasks.py:253-265`). |
| `MAX_RETRIES` | 4 | Per-firing retry budget in `drivers.Driver.run`: an attempt that raises `DriverError` retries (exponential backoff `2^attempt`, capped at 30 s) until `attempt > config.MAX_RETRIES`, then re-raises and fails the task (`drivers.py:126-136`). |

Note `config.MAX_RETRIES` is also used by `pool.py` for API request retries
in the research workload; in the code workload the driver retry loop above is
what governs a single harness firing.

## 6. Worked example — 12 tasks, first wave

A task file has 12 tasks: 2 hard (GLM-5.3 / Kimi-K3 implementers), 4 medium
(DeepSeek-V4-Flash), 6 basic (gpt-oss-120b), all with no `deps` (so all are
graph start nodes and become runnable at once). Driver caps: GLM-5.3 = 3,
Kimi-K3 = 3, DeepSeek-V4-Flash = 5, gpt-oss-120b = 5 — and the opencode
harness pool = 5 across all three of the opencode models.

**First wave — the implementers (12 of them) all start.** The caps are far
from binding on implementers:

| Implementer | Need | Cap | Runs? |
|---|---|---|---|
| gpt-oss-120b | 6 | 5 | 5 run, 1 queues |
| DeepSeek-V4-Flash | 4 | 5 | queues behind the harness |
| GLM-5.3 | 1 (one hard task) | 4 | queues behind the harness |
| Kimi-K3 | 1 (the other hard task) | 3 | ✓ (own harness) |

The per-model caps are NOT what binds here. gpt-oss, DeepSeek and GLM want
6 + 4 + 1 = 11 opencode processes against a harness pool of 5, so only five of
them run at a time regardless of model headroom. Kimi runs immediately because
the `kimi` CLI is a separate harness. **All 12 implementations do not run
concurrently** in the first wave. The per-model guards only bite when a model
needs *more* than its cap.

**What queues — the reviews.** Every task must then be reviewed by GLM-5.3 or
Kimi-K3 (a reviewer never shares a family with its implementer). That is 12
review firings, plus the hard-tasks' own implementations already counted. The
GLM + Kimi caps together allow only **3 + 2 = 5** concurrent harness
instances, and GLM/Kimi are also still busy with the hard implementations.
So once the implementations start finishing, the review firings pile up on the
`GLM-5.3` and `Kimi-K3` semaphores (`drivers._gate`): the first five reach
`gate.acquire()` and run, the rest block in FIFO order until a reviewer frees
its slot.

**Why this is the bottleneck.** DeepSeek (cap 8) and gpt-oss (cap 8) never
saturate with 4 and 6 tasks; the run is limited by the hard-model pair
(GLM 3 + Kimi 2 = 5). Even though the *implementers* all ran, throughput is
capped by review slots, so the total in-flight never reaches the 21 ceiling
with only 12 tasks — the theoretical 21 only takes over when a batch has
many more hard/review work items than 5.

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
