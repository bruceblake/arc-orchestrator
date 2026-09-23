# Orchestration roadmap

Living document, dated **2026-09-15**. It records the strategic directions the
operator named over one long working session — directions that are **not yet
task files**. Each "Next" item below becomes a planned task file once a planner
model is available; until GLM-5.3's backend recovers, `main.py code plan`
cannot run, and the fleet is running DeepSeek-V4.1-Flash-thinking-max for every
role, so items here are advanced by hand-written task files. Nothing here is a
commitment to a date or an effort estimate.

---

## Now — in flight

Reconciled against `~/tasks/*.json` and the `code_tasks` table on 2026-09-15.
Per-task status is from `orchestrator.db` (`main.py code status`) — a snapshot,
since the fleet keeps merging while this file sits still. Re-read the table
before trusting a row.

| Task file | What it is | State |
|---|---|---|
| `fleet-ops-overhaul-of-the-arc-orchestrat.json` | "Fleet-ops overhaul: close the empty-diff publish hole, scope-lock reviews, budget roles per-task, and make fleet activity visible" (hierarchical) | `empty-diff-publish`, `role-budgets`, `usage-hourly`, `chat-sessions`, `review-scope-lock` merged; `degraded-review-policy` failed; `activity-feed` still running |
| `migrate-the-arc-orchestrator-s-opencode.json` | "Migrate OpencodeDriver to persistent opencode serve sessions" (chain) | `ocserve-client` running — the first slice of the serve migration |
| `audit-backup-generator-fix.json` | "Audit: one DB backup per day + a real action on the missing-log finding" (single) | `audit-backup-generator-fix` running |
| `dynamic-dag-structural-plan-amendments.json` | "Dynamic DAG: finish the structural plan-amendment channel (cancel_task, bounds, mutation ledger, DAG rendering)" (hierarchical) | no `code_tasks` rows yet — **chain-gated on `migrate-the-arc-orchestrator-s-opencode.json`** (`project.after`), so it allocates no worktree until that whole chain has merged |
| `agents-tab-phantom-rows.json` | "Agents tab: stop showing dead runs as stalled live agents" (single) | merged (PR #68) — but a single-task file is done at merge, so this row is history |
| `orchestration-roadmap.json` | "Docs: write the orchestration roadmap capturing the operator's queued directions" (single) | `orchestration-roadmap-doc` running — this file |

Two notes on the reconcile:

- `degraded-review-policy` in the fleet-ops overhaul failed and was not
  re-run; the same-family review hatch it would have promoted was instead
  re-added directly in `config.py` (PR #65, commit `e44a37a`). Anything that
  wants a *policy* rather than an env hatch is still open.
- `migrate-the-arc-orchestrator-s-opencode.json` is the head of a chain:
  several items below (harness concurrency, dynamic DAG) are correct to wait
  for it, because they build on the post-migration driver shape.

## Next — specified, awaiting a planner

Each item below is one task file (or one project of a few) waiting for a
planner. The **acceptance shape** is a sketch of what the task file's
`verify_cmd` could honestly assert — the fleet's own rule is that a gate must
*fail today* (see [self-improvement-projects.md](self-improvement-projects.md)),
so these are written as things a command can check, not as prose goals.

### 1. Pulse-loop orchestrator ("Lloyd" pattern)

**What the operator asked for.** An always-on supervisor agent, pinged on an
interval (roughly 90 minutes), that re-reads a persistent *mission note* and
acts on it. The mission note has three parts:

- **Who** — the agent's identity and expertise, so a fresh process is the same
  operator-facing agent every pulse.
- **What** — the checks performed on every pulse: new failures in the logs,
  work stranded in a non-terminal state, recently merged PRs whose
  documentation is now stale, backlog items nobody has been staffed onto.
- **How** — the working-style laws it holds itself to.

It keeps its own state in a SQLite **tickets** table it manages itself, files
tickets for what it finds, **staffs them autonomously** (turns a ticket into
work rather than waiting for a human), and escalates only genuine problems.
Reference model named by the operator: a Reddit-described setup in which one
orchestrator manages ~16 child sessions and 800+ tickets.

**Why.** The fleet can run for hours with nobody knowing whether it is making
progress; the operator's recurring question is "is anything running, and is it
going anywhere?" Today that answer is assembled by a human reading a feed.

**What already exists here** — this item is mostly composition, not new
machinery:

- `store.py` — SQLite persistence, one table per concern, already the pattern a
  tickets table would follow (`project_state`, `queue_items` show the shape).
- `scheduler.py` `Supervisor` — a long-running loop that keeps work in flight,
  with backoff, stats and clean signal handling. That is the pulse clock and
  the lifecycle; it is not agent-driven today.
- `main.py audit` (`audit.py`) — already answers "what broke" (grouped
  defects) and "what is rotting" (stranded tasks, orphaned worktrees, leases
  held by dead processes, log growth), and **every finding already carries a
  severity and a concrete next action**. Those findings are the raw material
  for tickets.
- `daily-audit.sh` + its cron entry — the scheduling precedent: silent on a
  good day, mails only on a non-zero exit.

**What is genuinely new:** agent-authored ticketing (a table the agent owns and
writes to, not one the code defines), the mission note as a durable artifact
re-read every pulse, autonomous staffing of what it files, and an escalation
policy deciding what is worth a human. Everything else is already built and
unused for this purpose.

**Acceptance shape.** A gate could run one pulse offline (no model call)
against a seeded fixture — a stranded task row and a stale doc — and assert
that ticket rows were written, that each carries an owner and a severity, that
a second pulse does not duplicate them (idempotency), and that `pulse.*` events
landed in `logs/events.jsonl`; plus `./check.sh`, since this touches
`store.py`/`scheduler.py`.

### 2. Self-organizing DAG beyond static task files

**What the operator asked for.** Today the graph is fixed at load time
(`code_tasks.build_code_graph` builds it from the file; `graph.py` runs it
verbatim). The operator wants graphs that **grow and edit themselves while
running** — a main orchestrator agent, or the agents themselves, amending
structure mid-run — with shared state carried through a **mailbox/threads**
system so nodes can talk to each other rather than only to the engine.

**Why.** A plan written before any code exists is systematically wrong about
the middle of the job, and the information that should change the graph arrives
while it is executing.

**Where this stands.**

- `dynamic-dag-structural-plan-amendments.json` (**in flight**, chain-gated on
  the serve migration) is the first slice: structural plan amendments —
  `cancel_task`, bounds, a mutation ledger, DAG rendering.
- What exists today is Rule 4b, `.arc/plan_proposals.jsonl`: agents may propose
  edits to the task file they are running against, and nodes harvest that file
  after every agent run. The channel is deliberately narrow — amendments are
  re-validated through the real loader, only tasks with no row (or a
  `failed`/`skipped` one) may be mutated, the in-flight DAG never rewires, and
  amendments take effect on resume.
- **A shared board exists** (`board.py`, `.arc/board.jsonl` plus
  `logs/boards/<project>.jsonl`). It carries handoffs, results and review
  notes across harnesses, which is what a usage swap needs when the next
  model cannot resume the previous session. It is not addressed mail and
  it does not spawn tasks. The fix loop's feedback edge and the
  plan-proposal channel are still the other two paths.

**This item is the follow-on**, in two pieces:

1. **Agent-to-agent messaging** — a mailbox/threads channel extending the
   `.arc/` plan-proposal pattern, so nodes publish and consume addressed
   messages: one task can tell another what it found (an interface it settled
   on, a symbol it renamed) instead of encoding it in the diff and hoping.
2. **Dynamic node creation during a run** — a task that spawns tasks. This is
   explicitly *not implemented* today: `graph.py` has `Spawn` for runtime
   fan-out and `Graph.subgraph`, but a task cannot add tasks to its own
   taskfile at runtime, and the loader would have to validate spawned specs
   (Rules 1–2) when they are created rather than at load. See
   [graph-patterns.md](graph-patterns.md) § "Toward more complex graphs".

**Acceptance shape.** A run in which one task, mid-graph, sends a message that
another task reads and acts on — asserted from the event log (`message.sent` /
`message.received` with sender, recipient, node, run_id), not from a
transcript — and a spawned task that was validated, allocated a worktree, gated
and reviewed exactly like a planned one. Delivery must be durable (written
before it is read, surviving a killed run) and the existing freeze boundary
must hold: no mutation of a task whose row has started.

### 3. Research phase before planning

**What the operator asked for.** A stage that runs *before* planning, in which
a fresh agent investigates the problem space — codebase recon plus web research
— and whose findings feed the planner prompt, so the planner plans against
evidence rather than against the goal sentence alone.

**Why.** `code plan` today hands the planner a one-line goal and the repo. On a
slow model in the fleet's scarcest slot, it spends its first minutes orienting
itself with `ls`-and-`grep` behaviour, and it has no way to learn what the
outside world says about the problem (a library's real API, a known pitfall) —
so it plans from memory.

**Nearest existing analogues** (both partial):

- `main.py gh triage <repo>` (`gh_ops.issue-triager`) already *is* a
  recon-then-plan step: a model reads open issues and writes a ready-to-run
  task file with correct cross-review pairing. It is repo-scoped and runs
  outside the governed pipeline.
- The planner already consults a catalogue: `docs/graph-patterns.md`, rendered
  into the prompt from `graph_shapes.planner_prose`, plus `graft map` for the
  directory/hub/hotspot view of the repo. That is a *static* research input;
  this item makes it dynamic and per-goal.
- The research workload (`work.py` + `pool.py`) has web research wired
  (`-legacy-tool-calling` variants with `server:websearch`) — the capability
  exists in the repo, just not on the code planner's path.

**Acceptance shape.** A task file whose head task is research and whose plan
task takes `deps` on it; the plan prompt (`code_tasks.plan_tasks`) carrying a
findings block; and a test that a run with a recorded findings artifact yields
a task file whose tasks reference what the research found (a named library or
file, say), plus `./check.sh`. The findings artifact must be a file the planner
reads, not text smuggled through a prompt string.

### 4. Integration-review (merge-train) stage

**What the operator asked for.** A **new stage, after several PRs have
merged**, in which a fresh agent with no implementation ownership reviews the
**integrated `main` branch**: cross-feature interactions, architectural
invariants, regressions, stale assumptions, test gaps, and inconsistency
between docs and backlog — did individually correct changes still compose? In
the operator's framing: *a senior engineer arriving after the merge train
asking: each carriage passed inspection — does the whole train run?*

**Why.** Every existing gate is per-task and therefore structurally blind to
composition. The pre-merge review (Rule 2) sees one bounded diff; the PR review
(Rule 5) sees the same diff again from the other family; the verify gate runs
one task's `verify_cmd` in one worktree. Nothing anywhere looks at the
*product* of a batch of merges — which is exactly where the interesting defects
live, and exactly where the operator's "it merged but is the system still
coherent?" question points.

**Explicitly not:** a re-review of one PR. It runs *after* merges, on the
tree, not on a diff — and it does **not** reopen completed work.

**Material findings become new tracked remediation items** — new task-file
rows written into the backlog, which then run through the ordinary governed
pipeline (alloc → implement → gate → cross-family review → PR → merge). Never a
silent reopening of a completed task: a merged row is history and stays
history, so the remediation is a new id with the finding as its spec.

**Acceptance shape.** A stage that can be triggered once N PRs have merged and
that emits a structured findings artifact (each finding: area, evidence as
`file:line`, severity), plus a step that turns each material finding into a
task-file row with an honest gate — a test could seed a deliberately incoherent
pair of merges, run the stage, and assert that `integration.*` events were
emitted and that remediation rows were produced in the backlog. Not a check
that "an agent was asked to look": the gate must assert on the artifact.

### 5. Frontend/backend decoupling

**What the operator asked for.** Split the single `dashboard.py` — server,
JSON APIs and static-file serving in one stdlib `http.server` — into a
dedicated **backend service** and a dedicated **frontend service** with a real
API boundary between them. The operator explicitly wants **better architecture
design here, as a project** — the shape of the boundary is part of the work,
not a foregone conclusion.

**Why.** `dashboard.py` is ~4,100 lines doing three jobs at once, and every
mutating route, every read route and every static file share one process and
one bind. Its API surface grew ad-hoc (the frontend calls roughly fifteen
routes) and there is no schema anywhere.

**What is known today, for whoever designs this:**

- `dashboard.py` is ~4,100 lines: read APIs over `orchestrator.db`, the event
  log and live transcripts, mutating routes (`do_POST` serves
  `/api/projects/create`, `/api/projects/run`, plus the repo/chat routes), and
  static-file serving.
- `static/` holds `index.html`, `phone.html`, `usage.html`, `common.js` and
  `static/panels/*` (agents, chat, detail, fleet, github, keyboard, projects,
  slots, state, summary, transcript) — the panel split from
  `dashboard-modularise` is what makes a frontend service plausible at all.
- **The unauthenticated-LAN posture is a deliberate operator decision**
  (AGENTS.md Rule 6b). Any decoupling must either preserve that documented
  stance or replace it with an explicitly re-decided one — it may not quietly
  change it, and it must not widen it (Rule 6b's rule: nothing that takes a
  path, a command or a git ref from a request body without an allowlist).
- `start.sh` / `stop.sh` and `deploy/`'s systemd units are the operational
  contract; a split means an extra unit and a start/stop that still works.
- `docs/self-improvement-projects.md` already scoped this as a queued
  multi-project effort, chained `after` the serve migration. Note the tension
  with `docs/self-improvement-wave.md`'s "deliberately not proposed: a frontend
  framework" — splitting services is not adding a bundler, and that line stands.

**Acceptance shape.** Backend tests that exercise the read and mutating routes
against the new boundary (including the allowlist behaviour Rule 6b requires),
a frontend that renders with the backend restarted independently of it, and
`start.sh` / `stop.sh` / the systemd units still bringing the whole thing up.
Plus `./check.sh` — which already checks every dashboard page's DOM references,
so the split must keep passing it.

### 6. Usage observability detail

**What the operator asked for.** The usage page (`/usage.html`) must show
**hour-of-day-per-day breakdowns** with **less clutter** — the shape they asked
for is a per-day view broken out by hour, not another aggregate table.

**Why.** "How many tokens went where, and when" is the only lever the operator
has on cost and on capacity pressure, and the current page answers it at the
wrong resolution: the daily row and the hourly buckets are separate views that
have twice disagreed, rather than one drill-down.

**Known defect — treat this as the first child item.** The operator reports
that the **hourly code-usage panel still renders red/broken after the last fix
round**. A remediation task file should be filed as the first child of this
item, before the new breakdown work: `GET /api/usage/hourly` plus the
date-picker UI shipped as `usage-hourly` (merged in the fleet-ops overhaul),
and the panel is still broken, so the honest next step is to reproduce that
against a live server and fix it, not to build on top of it.

**Acceptance shape.** For the defect: a test or a live fetch that renders the
hourly panel for a date with data and asserts it renders without an error state
(the page currently has no such check). For the feature: an `/api/usage/hourly`
response whose buckets for one day sum **exactly** to that day's row in
`/api/usage` — the repo has already paid for this: `dashboard.py`'s
`_usage` docstring records that the daily and hourly views came to disagree
once, and mandates that both walk the same aggregation. A gate that asserts the
two agree is the right gate. Plus `./check.sh`.

### 7. Important-events stream

**What the operator asked for.** A first-class log surface of **important
events only**: who is reviewing what, who failed and why, what each agent is
actually doing — **not heartbeat noise**. A curated feed a human can read, as
opposed to the append-only firehose.

**Why.** The operator could not tell what an agent had been doing for hours.
The transcripts are hard to follow (raw harness JSONL, thousands of lines of
stream frames), and `logs/events.jsonl` is complete but flat: driver starts,
lease waits and token receipts sit at the same level as "this task was rejected
by review, and why".

**What already shipped (do not rebuild):**

- The event vocabulary already carries most of the semantics: `task.gate`,
  `task.reviewed`, `task.pr_opened` / `task.pr_reviewed` / `task.resynced`,
  `task.merged`, `task.failed`, `task.escalated`, `task.pr_review_thin`,
  `chain.wait` / `chain.ready` / `chain.blocked`, and `driver.cap_wait` — each
  with the task, model, attempt and verdict attached (AGENTS.md Rule 7).
- PR #64 (commit `de87fde`) put each node's fix-loop state on the Projects DAG
  (`↺ xN (⬆M) — gate failed | review rejected (k issues)`), and PR #60
  (commit `36fcc6c`) gave transcripts a readable activity view.
- `events.py` tags every event with workload context, and Rule 7b fingerprints
  errors, so "who failed and why" already has a durable home.

**What is missing** is the surface itself: a curated, human-readable
important-events feed — a classifier/allowlist that promotes the subset worth
reading into one view (dashboard panel plus an API), keeping the full log
intact underneath for forensics. The transcripts being hard to follow is part
of this gap, not a separate complaint.

**Acceptance shape.** A read API (say `GET /api/events/important`) plus a
panel; a test that seeds a fixture event log mixing important events with noise
(heartbeats, per-request receipts) and asserts the feed returns exactly the
important ones, each with a human-readable summary line, and that a task's
failure reason appears verbatim rather than as an event name. Plus `./check.sh`.

### 8. Chat/planning overhaul

**What the operator asked for.** A way to start a **new chat** from the UI, and
the chat + planning flow reworked as **one system** rather than two features
that happen to share a page.

**Why.** Chat is how the operator turns intent into a plan, and today the two
halves do not compose: the chat path and `code plan` are separate entry points
into the same outcome (a task file). Starting over is possible since PR #54
(the panel's "＋ new chat" opens a fresh session), but getting *from* that
session *to* a plan is still not one flow — the chat turn and the planner are
still two systems.

**Reconcile `orchestrator-chat.json`** (titled "Orchestrator chat: plan
projects with Kimi-K3 from dashboard and phone (text + speech)"). Its tasks
have since merged — `orch-chat-backend` (chat engine: `main.py chat` +
`orchchat.py` session/plan finalizer), `orch-chat-api` (`/api/repos`,
`/api/repos/create`, `/api/chat/start`, `/api/chat/poll`), `orch-chat-panel`
(desktop panel) and `orch-chat-phone` — and `chat-sessions` (merged, PR #54)
added list/create/switch beyond the single fixed session, **including the
new-chat action itself**: the desktop panel's `＋ new chat`
(`static/index.html`, wired by `chatNewSession` in `static/panels/chat.js`)
and the phone's `New chat` (`static/phone.html`) already open a fresh session
with an empty transcript, and `tests/chat_ui.test.mjs` covers it. So this item
is **not** greenfield: what remains is unifying chat and planning into one
flow. Note that the task file's own title
still names Kimi-K3, a retired model — it is history and must not be re-run
as-is.

**Acceptance shape.** A flow test that a chat turn can produce a task file
through the same validation path as `code plan` (`load_taskfile` plus the
`describe` summary) rather than through a second, parallel code path, and that
`code plan`'s output and the chat's land in the same place — a chat turn whose
plan is listed by `main.py code status` (or the taskfile directory) exactly as
a planned one is. The already-shipped new-chat action is covered by
`tests/chat_ui.test.mjs`, so that test is a regression guard, not the gate.
Plus `./check.sh`.

### 9. Harness concurrency topology

**What the operator asked for.** Raise the **effective** ceiling on concurrent
work past the measured 5-process limit of `opencode`. Options the operator
surfaced:

- **Multiple isolated opencode server instances** — per-project ports with
  isolated DB/session directories (the shared `~/.local/share/opencode` sqlite
  store is what makes one pool one pool), or one container per instance so the
  isolation is real rather than nominal.
- **The serve migration** (`migrate-the-arc-orchestrator-s-opencode.json`, in
  flight) — persistent `opencode serve` sessions with a lifecycle and an
  HTTP/SSE client, instead of one-shot harness processes.
- **The `reasonix` harness for DeepSeek** — already a separate pool with its
  own measured ceiling, so DeepSeek work does not compete for opencode slots.

**Why.** Fleet throughput is bounded by the *lowest* ceiling in the chain, and
today that is the opencode harness pool, not the account limit.

**The measured numbers** (AGENTS.md Rule 6: identical prompt, warm cache, on
the shared opencode pool):

| concurrent | 3 | 4 | 5 | 6 | 10 |
|---|---|---|---|---|---|
| succeeded | 3/3 | 4/4 | 5/5 | 4/6 | 4/10 |

Past five it fails fast with an **empty stderr** — the fleet logged
`opencode exited 1: ` and burned retry ladders on self-inflicted contention.
The reasonix pool measured 3/3, 6/6 and 7/7 clean on 2026-09-14, with no cliff
up to 7 where opencode's was at 6. The caps are separate layers: GLM-5.3's cap
of 4 belongs to its **account**, while opencode's 5 belongs to the **binary and
its store** — so raising the harness ceiling alone does not raise GLM's account
ceiling.

**The goal** is maximizing *concurrent, verified* work within the ARC
per-account API caps — and those caps are **per API KEY, not per process**
(`config.py`), shared with other consumers of the same key. A topology that
buys parallelism by exceeding the account cap just converts work into 400s and
backoff, so the target is the headroom between the harness ceiling and the
account ceiling, verified by measured concurrent runs rather than asserted.

**Acceptance shape.** A load test in the fleet's own style: N concurrent
isolated instances on one prompt, reporting succeeded/total at each N, and a
gate asserting that the new topology reaches **strictly more than 5** concurrent
opencode runs clean while `driver.cap_wait` and capacity-rejection events stay
at zero — plus a check that per-account caps are still respected (the fleet
must not win concurrency by over-subscribing the key). Plus `./check.sh`.

## Later — ideas named once, not yet specified

Named in the same session but not worked out; they need a conversation, not a
planner, before they become task files.

- **Graft-style faster file-reading for agents** (operator tooling
  preference). Not a new feature request so much as a standing preference: an
  agent should be handed the exact `file:line` spans it needs rather than
  discovering them. Graft is already wired into the pipeline (`graft build` /
  `ask` for implementers, `blast` for reviewers, `map` for the planner —
  see README § "Code-graph context"); the open question is where else the same
  treatment applies (the research workload, chat, `gh_ops`, the Minecraft
  build workload).
- **Per-model max-context enforcement matching the ARC API docs.** The
  operator's question: should every model's max context come from the ARC
  docs? Today only some context values are recorded (the 2026-09-15 docs read
  for GLM-5.3: 128k context, concurrency 4) and nothing enforces a per-model
  context budget at call time; the runbook's § "Context budget (per harness)"
  describes the *harnesses* failing differently near their limits. Deciding
  this means deciding whether `config.py` becomes the single source for
  context windows and whether a call that would exceed one is refused
  pre-flight rather than terminated mid-stream.
- **Doubling all timeouts and rounds during the unlimited-tokens window.**
  Partially done via env overrides — `ARC_DRIVER_TIMEOUT` and the per-role
  `ARC_*_TIMEOUT` values exist, total budgets are unlimited by default since
  2026-09-14, and `ARC_MAX_FIX_ROUNDS` / `ARC_PR_MAX_ROUNDS` /
  `ARC_MAX_REVIEW_CRASHES` / `ARC_PR_MAX_INCONCLUSIVE` are all overridable. A
  config-level revisit is pending: whether the *defaults* should change for the
  duration of the window, or stay as they are with the env overrides carrying
  it.

---

This file is edited by hand and by governance tasks; when an item becomes a
taskfile, move it to Now with the taskfile path.
