# Self-improvement wave — decomposition and run order

Six projects, ten tasks, produced from an audit against published practice for
[DAG/workflow engines](https://www.jongwow.kr/en/data-engineering/airflow-dag-design-principles)
and [operational dashboard UX](https://www.uxpin.com/studio/blog/dashboard-design-principles/).
Every taskfile passes `main.py code run <file> --dry-run`.

## The constraint that shaped this

`docs/taskfile-schema.md` is explicit: keep `files_hint` **disjoint between
dep-independent tasks** — two agents editing one file is the main cause of
`conflict`. That is not theoretical. On 2026-09-10 two tasks conflicted on
`static/index.html` and `code_tasks.py` and neither could land until an agent
resolved the merge by hand.

So the decomposition is driven by file ownership, not by topic:

- No two parallel tasks in a project share a file. Verified mechanically.
- Tasks that must share a file are serialised with `deps`.
- Projects that share a file go in different waves.

## Waves

**Wave 1 — mutually disjoint, run together**

| Project | Tasks | Owns |
|---|---|---|
| `usage-truth` | 2 | `dashboard.py`, `static/usage.html` |
| `phone-ui` | 1 | `static/phone.html` |
| `dashboard-modularise` | 1 | `static/index.html`, `static/panels/` |
| `graph-engineering` | 2 | `graph.py`, `config.py`, `store.py` |

```bash
ARC_QUEUE_PARALLEL=4 ./run-queue.sh usage-truth phone-ui dashboard-modularise graph-engineering
```

**Wave 2 — after wave 1 merges**

| Project | Tasks | Blocked by |
|---|---|---|
| `dashboard-ux` | 2 | `dashboard-modularise` — both own `static/index.html`, and both UX tasks assume the split |
| `fleet-insight` | 2 | `usage-truth` (`dashboard.py`) and `graph-engineering` (`config.py`) |

```bash
ARC_QUEUE_PARALLEL=2 ./run-queue.sh dashboard-ux fleet-insight
```

`dashboard-modularise` is the enabler and the reason it runs alone in its lane:
until `static/index.html` is split, every UI task collides on one 1458-line
file. Both wave-2 UX tasks begin by checking the split happened and stopping if
it did not, rather than rewriting the monolith.

## What the audit found

**Graph engine.** Measured, not assumed:

- *No admission control.* `graph.py` contains no concurrency bound. Of the
  taskfiles in `~/tasks`, ten have 2 root tasks, two have 4, one has 5 — all
  start the instant the run does. `run-queue.sh` bounds task FILES; nothing
  bounds tasks within a file. Work started past the model/harness ceilings does
  not run, it queues holding a worktree and a DB row. → `graph-admission-control`
- *No durable state.* `ctx['results']` and `ctx['runs']` are in memory only.
  A killed run — routine; the log is full of "interrupted: run process exited"
  — loses fix-round counts, escalation history and paid-for reviewer verdicts,
  because resume rebuilds from the coarse `status` column alone.
  → `graph-resumable-state`
- *Idempotency* is the one to be careful with, not to chase: `alloc` resets a
  branch, `publish` commits. Re-running them is not free. The state task
  persists only pure verdicts, and says so.

**Dashboard UX.**

- *No summary tier.* Nine panels of equal weight, all expanded. Practice is
  Summary → Context → Details with 3-5 figures answering "is everything OK?"
  before anything else. → `ux-summary-tier`
- *Mouse-only*, for a tool an operator watches for hours. → `ux-keyboard-and-focus`
- *Phone page is a stub*: 312 lines, two panels, largest interactive padding
  9px against a 44px minimum, no PRs, no capacity, and it redefines helpers
  `/common.js` already exports. → `phone-shell`

**Usage page.** The range filter is decorative — `/api/usage?range=` returns
byte-identical totals for `1h`, `24h`, `7d` and `all` (3893 requests,
306,870,616 tokens each). Root cause: `_usage()` validates `range_key` on its
first line and never references it again. → `usage-range-filter`

The page also renders none of the data that would answer an operator's actual
question: `failed_attempts` and `daily` are in the payload and unused, and
there is no success rate, no waste figure and no baseline to compare against.
→ `usage-informative`

**Cost.** 306M tokens have gone through this fleet and nothing anywhere
converts them to money. → `cost-attribution`

**Progress.** "Is it actually doing anything?" is the recurring question, and
answering it means reading a feed and inferring. A run whose drivers are all
queued behind a saturated harness looks identical to one that is working.
→ `progress-watchdog`

## Deliberately not proposed

- *Dashboard authentication.* The exposure is real and documented (AGENTS.md
  Rule 6b); the operator has accepted it. Not a task.
- *Rewriting the graph engine around an external orchestrator.* The engine is
  small, tested and understood. Its gaps are two features, not a rewrite.
- *A frontend framework.* The pages are served by a stdlib `http.server` with
  no build step. Adding a bundler to fix a 1458-line file is a bigger change
  than splitting the file.
