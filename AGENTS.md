# AGENTS.md — governance for the ARC multi-model orchestrator

This is the main governance document for this repository. **Every agent (human
or AI) that works in this repo reads this file first.** It states what this
repo is, which models are allowed to do what, and the hard rules the code
enforces. Detail lives in the docs under `docs/` (see [Links](#links)); this
file is normative where it says MUST/NEVER, and every rule carries the code
that enforces it. Everything here is true of the code as it exists today.

---

## 1. Mission

This repo is a **multi-model coding orchestrator**. A fleet of **two models**
builds software — and this repo itself — as a directed graph of small tasks:

- **GLM-5.3** — the fleet's strongest model, running in **`opencode`**
  (`drivers.OpencodeDriver`) — is the main orchestrator/planner
  (`config.PLANNER_MODEL`): it breaks a project goal into 2–6 small tasks,
  decides the graph fanout (which tasks run in parallel), the dependency
  order, the model routing (who implements what tier), and the reviewer
  pairing, via `main.py code plan` (`code_tasks.plan_tasks`).
- All models implement. **GLM-5.3** takes the hard tier (multi-file reasoning,
  delicate design, architectural judgment) on top of planning and reviewing;
  **DeepSeek-V4.1-Flash-thinking-max** (in **`reasonix`**, Reasonix — the
  DeepSeek-native cache-first agent, `drivers.ReasonixDriver`; replaced `dsh`
  by operator decision 2026-09-13) takes the medium/mechanical tier. DeepSeek is much
  faster and carries the implementation load; GLM-5.3 is the planner and the
  last escalation stage.
- **Every implementation is gated and cross-reviewed before merge**: a
  deterministic `verify_cmd` gate runs first, then a reviewer model from a
  *different model family* reviews the full diff, and only then does the
  orchestrator (the only git actor) commit, push, and open a pull request against `config.BASE_BRANCH` (`main` by default) under a
  process-wide lock.

### Two graphs

Every project is two graphs, and they are owned by different parties:

1. **The per-task pipeline — static, in code.** Every task runs
   `alloc → implement → gate(verify_cmd + visual evidence, Rule 7d) →
   cross-family review → publish → PR (+ evidence comment) → PR review
   (ONE cross-family reviewer today, Rule 5) → merge`, with
   the bounded fix loop (Rule 4)
   and tier escalation (`config.ESCALATION_PATH`) as conditional edges. It
   is built by `code_tasks.build_code_graph`, documented per node by
   `pipeline_doc.py` (dashboard: "How a task moves through the pipeline"),
   and **nobody chooses it per project** — not the planner, not a taskfile.
2. **The graph between tasks — designed per project by the planner.** Which
   tasks exist, which run in parallel (`deps: []`), which wait (`deps`),
   which run only if a dependency's probe verdict says so (`when` +
   `probe_cmd` — a branch whose condition fails is recorded `skipped`, with
   everything downstream), and which whole projects wait on others
   (`project.after`, Rule 9). The
   planner picks a shape for it from the catalogue in `graph_shapes.PATTERNS`
   (single, chain, fanout, diamond, router, debate, hierarchical; the
   evaluator and escalate loops are built into graph 1 and never chosen) by
   the kind of work, records it as `project.pattern`, and writes `deps` that
   form it. `graph_shapes.classify` derives the shape a taskfile's `deps`
   actually form; `code_tasks.describe` (printed after `code plan` and on
   every dry run) reports both and flags a **mismatch**. The dashboard serves
   the same view at `GET /api/graph-shapes`.

The planner prompt (`code_tasks.plan_tasks`) is generated from that
catalogue plus today's roster and driver caps (`graph_shapes.planner_prose`)
and lists the repo's existing projects with their merge state, so a plan
that builds on unmerged work declares `after` instead of assuming code that
is still on a branch. Prose reference: [docs/graph-patterns.md](docs/graph-patterns.md).

The code workload is the primary occupant of this repo, but the same
orchestrator core (`graph.py`, `pool.py`, `events.py`, `store.py`) also runs
a 24/7 research-question round workload (`work.py` + `scheduler.py`,
`main.py run`). Godot Studio (`studio/`, `main.py studio`) is the sole game
workload and uses the governed code-task pipeline. The retired Minecraft
builder has no CLI or dashboard route; historical data is retained.
All workloads share the event log and the dashboard.

---

## 2. Model fleet and roles

Routing is decided at plan time (by GLM-5.3 in
`main.py code plan`, or by whoever writes a taskfile by hand) and is
**enforced again by the loader**,
`code_tasks.load_taskfile`. There is no runtime triage. Full reference:
[docs/model-tiers.md](docs/model-tiers.md).

| Model | Harness | Tier | Allowed roles | Per-account API cap | Driver semaphore cap |
|---|---|---|---|---|---|
| GLM-5.3 | `opencode` (`OpencodeDriver`) | hard | Implement, Plan, Review, PR-review | 4 | 4 |
| DeepSeek-V4.1-Flash-thinking-max | `reasonix` (`ReasonixDriver`) | medium | Implement, Review, PR-review | 10 | 10 |

**GLM-5.3 is the fleet's strongest model** — operator decision 2026-09-12:
hard tier, the planner, and the last escalation stage. Its cap of 4 is the
official ARC docs value (docs.arc.vt.edu model table, checked 2026-09-15:
GLM-5.3 = 128k context, concurrency 4), adopted per operator directive. Live
rejections twice deviated from the table — "max 3 in flight per user on this
backend" on 2026-09-14 (the basis of a one-day pin to 3, since reverted) and
"max 5 in flight" on 2026-09-15 — both recorded as dated observations, since
other consumers of the key share the account cap; the lease + capacity
backoff absorb the dips. **DeepSeek-V4.1-Flash-thinking-max
(DS-max below) is the medium-tier workhorse**: much faster than GLM-5.3, it
carries the implementation load and reviews, and it **NEVER plans**. Its cap
of 10 is the provider-published figure for V4.1 (provider docs updated
2026-09-12), not a measurement of ours.

- **Tiers** (`config.IMPLEMENT_TIERS`): `medium` → DeepSeek-V4.1-Flash-thinking-max
  (moderate / mechanical work), `hard` → GLM-5.3 (multi-file reasoning,
  delicate design, architectural judgment — on top of its planning/reviewing
  duties). There is no `basic` tier: gpt-oss-120b was retired with it.
- **Role enforcement is roster-driven** (`config.MODEL_ROLES` / `model_may`),
  not a per-model if-chain: both live models may implement, review, and
  PR-review; only GLM-5.3 may plan, and the driver constructors raise
  `ValueError` for a model off today's roster or a role outside `MODEL_ROLES`
  (`DeepseekDriver` refuses the planner role intrinsically; gh_ops roles
  require planner permission).
- **Retired models still load.** gpt-oss-120b left the fleet before this
  change; DeepSeek-V4-Flash was retired 2026-09-12 (the provider removed it
  from the API; the DeepSeek-V4.1 line replaced it); **Kimi-K3 was retired
  into history the same day by operator decision** — its ROSTER row was
  deleted, so no role, tier, or cap lookup can name it, and it is gone from
  `config.FAMILIES` and every live role. A taskfile naming a retired model is
  remapped onto the escalation path by `code_tasks.RETIRED_MODELS`
  (Kimi-K3 → the strongest live tier), so old taskfiles still run.
- `main.py code plan` uses **GLM-5.3** as the planner
  (`config.PLANNER_MODEL`). The planner prompt (`code_tasks.plan_tasks`)
  instructs it to spread work across both models so independent tasks run in
  parallel, keep tasks small (<30 min for one agent), add `deps` only when
  one task truly needs another's output, and give every task a meaningful
  `verify_cmd`. GLM-5.3 planning is slow on big goals — see
  [docs/runbook.md](docs/runbook.md) § "Planning a large goal".
- Thinking variants (`*-thinking-low/high/max`) and the
  `*-legacy-tool-calling` websearch models registered in `config.FAMILIES`
  belong to the research workload; the code workload routes only the
  roster names above (its one thinking variant is DS-max itself).

---

## 3. Governance rules

These rules are normative. Each is enforced by the code cited; the pointers
are where to look when a rule surprises you.

### Rule 1 — Tier routing is mandatory and loader-enforced

A task's `model` MUST be one of `config.IMPLEMENTER_MODELS`
(`GLM-5.3`, `DeepSeek-V4.1-Flash-thinking-max`) and MUST match the
difficulty tier it was planned for (`config.IMPLEMENT_TIERS`).

- `code_tasks.load_taskfile` (code_tasks.py:85) raises `ValueError` if a
  task's model is not in `config.IMPLEMENTER_MODELS` and is not a retired
  model remappable by `code_tasks.RETIRED_MODELS` (Rule §2: retired names are
  remapped onto the escalation path).
- `drivers.OpencodeDriver.__init__` (drivers.py:967) and
  `drivers.DeepseekDriver.__init__` raise `ValueError` if a model is off the
  roster or is given a role outside its `config.MODEL_ROLES` entry
  (`model_may`) — role enforcement is roster-driven, not a per-model
  if-chain. DeepSeek is refused the planner role by its roster row, which has
  no `planner`.
- The planner prompt (`code_tasks._routing_tiers_prose`, code_tasks.py:1565)
  assigns implementers by tier:
  DeepSeek-V4.1-Flash-thinking-max for medium/mechanical tasks, GLM-5.3 for
  hard.

Never route a medium-tier task up to the hard tier or a hard task down to the
medium tier. Tier reference: [docs/model-tiers.md](docs/model-tiers.md).

### Rule 2 — Cross-review is mandatory and loader-enforced; never same-family self-review

Every task MUST be reviewed, and the reviewer MUST NOT be from the same model
family as the implementer it reviews — family-based, not harness-based (the
two families run different harnesses today anyway: GLM on `opencode`, DeepSeek
on `reasonix`).

- `code_tasks.load_taskfile` (code_tasks.py:89-115) REQUIRES a cross-family
  reviewer, but it normalizes an off-roster token rather than rejecting it. A
  `reviewer` naming a family that is not live today (e.g. `"kimi"` on an
  otherwise-valid GLM-5.3 task) is silently remapped to
  `config.cross_family_reviewer(model)` at code_tasks.py:94 — that branch tests
  the REVIEWER family, not the task's model, so it fires for any taskfile whose
  reviewer is outside `config.REVIEW_FAMILIES`; code_tasks.py:96 raises only
  when no cross-family reviewer exists at all. The raise at code_tasks.py:101
  is reached only under a bench `policy` reviewers override (benchmarking
  exception, below), not on the normal path, and the same-family `ValueError`
  at code_tasks.py:114-115 is what a hand-written same-family `reviewer` hits.
  The cross-family flip at code_tasks.py:104-105 is narrower still: it is gated
  on `model != planned_model`, so it applies only to a task whose model was
  remapped off a retired one — a normal taskfile with a same-family reviewer is
  rejected, not flipped. With two families the cross-review pairing
  is exact: **GLM-5.3 work is reviewed by deepseek; DeepSeek-V4.1-Flash-thinking-max
  work is reviewed by glm** (`config.cross_family_reviewer`: the strongest
  review-capable family that is not the implementer's).
- The taskfile `reviewer` token stays the deterministic plan. The review
  node resolves it through `config.REVIEW_FAMILIES` and, **before**
  instantiating a driver, asks `code_tasks._select_reviewer` whether that
  model has driver or harness headroom (`_reviewer_pressure` < 1). When it
  does, that model reviews. When it does not, the node picks another model
  whose driver can be constructed for `reviewer`, from a family other than
  the model that **actually implemented** (`wrote_the_code`), at the same or
  a stronger tier, and with real headroom right now — least contended, then
  stronger. Otherwise it keeps the planned reviewer and waits. It never
  allows a same-family review, never drops to a weaker tier, and never skips
  the review. A bench `policy` and `ARC_ALLOW_SAME_FAMILY_REVIEW` do not
  swap: those runs measure or replace the named reviewer on purpose. A
  usage-window substitution inside `driver.run` is unchanged
  (`avoid_families` is still the implementer's family); the harness run,
  `task.reviewer_selected` / `task.reviewed`, the review result, and the PR
  trailer record the model and family that actually read the diff. The
  verdict must be JSON: `{"pass": true}` or `{"pass": false, "issues": [...]}`.
- **Cross-family review is the default and only relaxed by one documented
  escape hatch.** A same-family review is a second pass by the same model,
  NOT an independent reading, so the loader rejects it — except under the
  capacity hatch below (removed 2026-09-14, re-added 2026-09-15).
- **`reviewer` and `pr_reviewer` are different roles.** `reviewer` is this
  pre-merge gate. `pr_reviewer` reviews an already-open pull request (Rule 5):
  judging a bounded diff against a spec is a much smaller job than authoring
  the change. On today's roster both live models may hold either role; the
  constraint is the cross-family pairing, so with two families exactly ONE
  reviewer per PR is possible (Rule 5). (This bullet once documented a
  narrower roster where DeepSeek-V4-Flash could PR-review but never
  gate-review; that exception retired with the model.)
- **Never keep a second list of who may review.** Eligibility is decided by
  CONSTRUCTING the driver (`code_tasks._eligible_pr_reviewers`). A hand-kept
  pool and the drivers' own role rules drifted apart once and it cost seven
  pull requests: the pool offered DeepSeek, `OpencodeDriver` refused the role,
  and the `ValueError` killed `pr_review` one second after each PR opened.
- A failed review sends the issues back to the implementer as feedback
  (bounded fix loop, Rule 4); nothing merges without `pass: true`
  (edge `review_<tid> -> publish_<tid>`, code_tasks.py:257).

- **A reviewer that CRASHED did not review.** A pre-merge reviewer that dies —
  a capacity error, a harness fault — or whose session ends **without emitting
  a parseable verdict** (e.g. stops mid-analysis with a question) returns
  `crashed`, and the task retries
  the REVIEW rather than going back to the implementer. Spending a fix round on
  it sends the implementer to repair code nobody criticised. Bounded by
  `config.MAX_REVIEW_CRASHES` (`ARC_MAX_REVIEW_CRASHES`, default 20), kept
  separate from the fix budget for the same reason `PR_MAX_INCONCLUSIVE` is
  separate from `PR_MAX_ROUNDS`.

  This cost a real task. graph-admission-control's verify gate passed FOUR
  times while its reviewer hit 18 consecutive capacity errors; each crash was
  recorded as a rejection, the implementer was sent to fix nothing, and the
  task finally died as "exhausted escalation" on work that was never rejected.
  The no-verdict variant cost a round too: a GLM reviewer analyzed PR #50 for
  47 minutes, then ended its session with a question instead of the verdict
  JSON — the missing verdict fail-closed to a rejection, bouncing the
  implementer to fix issues that were never delivered.

Full pipeline contract: [docs/orchestration-contract.md](docs/orchestration-contract.md).

**Capacity escape hatch (off by default).** When the whole cross-family
reviewer backend is hard-down *server-side* (not contention — a jammed
counter nothing local holds), a run may be launched with
`ARC_ALLOW_SAME_FAMILY_REVIEW=1` (`config.ALLOW_SAME_FAMILY_REVIEW`): the
loader then accepts a same-family reviewer, `_reviewer_for` keeps it, and
the PR-review pool inverts to same-family only. First used 2026-09-12..14,
removed when GLM-5.3 stabilised, re-added 2026-09-15 when GLM-5.3's session
counter jammed at 5 for 3.5+ hours with no local holder (verified with a
bare probe). Pair it with `ARC_ESCALATION_PATH=<the surviving model>` so
exhaustion cannot escalate into the dead family; turn it off when the
backend recovers — it weakens exactly the independence this rule exists
for.

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
  implementer harness runs with cwd = its worktree (the shared
  `Driver._once`, drivers.py:920, for opencode and reasonix alike), so
  there is no "too small for a worktree" path, not even for a one-line doc
  fix.

### Rule 4 — Every task MUST define an honest `verify_cmd` gate, run before review

- The gate node (code_tasks.py:190) runs the task's `verify_cmd` as a shell
  command **in the worktree**, under `config.GATE_TIMEOUT` = **360 s**
  (override `ARC_GATE_TIMEOUT`); a timeout kills the process and fails the
  gate. Only its stdout/stderr tail (last 2000 chars) is kept.
- The gate MUST pass before review happens (edge `gate_<tid> ->
  review_<tid>` fires only `when r["passed"]`, code_tasks.py:256).
- A gate or review failure loops back to `implement` with the failure output
  as feedback while `runs <= config.MAX_FIX_ROUNDS` (16, override
  `ARC_MAX_FIX_ROUNDS`). Exhausting the fix rounds does **not** fail the task
  yet: it **escalates one tier up `config.ESCALATION_PATH`** (default
  `DeepSeek-V4.1-Flash-thinking-max → GLM-5.3`, overrides
  `ARC_ESCALATION_PATH` / `ARC_MAX_ESCALATIONS`) — an `escalate_<tid>` graph
  node routes back to `implement_<tid>` with a **fresh fix budget**, carrying
  the latest gate/review failure as feedback. There is no `basic` tier, so a
  medium task's first escalation is the hard tier, GLM-5.3. Cross-review holds on
  escalation (`code_tasks.build_code_graph`): the reviewer token flips to the
  family-paired reviewer of the new implementer (deepseek implementer → glm,
  glm → deepseek). Each escalation emits `task.escalated`
  `{from_model, to_model, n}`; the `code_tasks` row is updated with the
  current model/reviewer and `harness_runs` rows record the model actually
  used. On **resume**, escalation is conditional: only a row whose recorded
  failure reason is a capability failure (exhausted fix rounds / escalation)
  starts a tier higher. A row written because the run process was killed or
  the graph was cancelled restarts at the **same** tier — an interrupted run
  is not evidence the model was too weak, and escalating on it sends every
  interrupted task to the scarcest tier simultaneously.
  Only when the last tier exhausts is the task marked `failed`, and
  the failure message names how many escalations were taken and the model
  they ended on (`exhausted escalation: N escalation(s), ended on <last>`,
  code_tasks.py:1409-1410; the older "up to <model>" wording was removed
  because it mislabelled PR-round and cancelled-run failures as escalation
  failures — code_tasks.py:1378-1381). Concurrency footnote: worst-case harness
  runs per task multiply by tier count (fix rounds × tiers); all caps of
  Rule 6 still apply.
- The loader permits an empty `verify_cmd` (it then passes trivially,
  code_tasks.py:192) — which is exactly why this rule is normative: a
  taskfile with no honest gate is a bug. The planner prompt requires a
  "meaningful verify_cmd"; hand-written taskfiles get one too. Run the tests,
  build, or a targeted check — something whose exit code actually depends on
  the change being correct.

### Rule 4b — The plan is a living document; agents may amend it

Any implementer or reviewer (pre-merge or PR) MAY propose changes to the
taskfile it is running against — the best information about the plan arrives
inside the run, not at plan time. The channel is
`.arc/plan_proposals.jsonl` in the per-task worktree (one JSON per line;
kinds `note | edit_scope | change_verify | change_model | add_task |
split_task`), offered in every implement/review prompt with the real task
ids and legal models (`plan_amend.prompt_block`).

- A second file, `.arc/board.jsonl` (`board.py`), is the thread agents
  share across harnesses. The orchestrator posts a handoff when a usage
  swap moves an attempt, and a result or review note after each run.
  Sibling tasks read the project copy at `logs/boards/<project>.jsonl`
  (`ARC_BOARD_DIR`). A `session_id` on a post resumes only on the harness
  named in that post — a Cursor chat id is never passed to `codex exec
  resume`. The file is excluded from `git add -A` and is not harvested
  away: the next fix round in the same worktree still needs it.

- Graph nodes HARVEST the file right after every agent run — crash paths
  included — and publish sweeps once more before committing (publish uses
  `git add -A`; the channel file must never land in a PR). Harvest = read +
  immediately delete, then validate + apply + record (`plan_amend.harvest`).
- Validation re-parses the WHOLE candidate file through the real loader
  (`code_tasks.load_taskfile`, policy included — Rules 1/2/8 hold for agents
  exactly as for planners); a proposal that makes the file invalid is rolled
  back and rejected while its siblings still apply.
- **The freeze boundary is the `code_tasks` row status** (v1): only tasks
  with no row or a `failed`/`skipped` row may be mutated — `merged`,
  `running`, `in_review` and `conflict` reject (attach a `note` instead; it
  always lands). **The in-flight DAG never rewires**: amendments take effect
  on resume (re-running `code run <taskfile>` re-parses the file), for tasks
  with no row yet, and for downstream chain gates that parse the file when
  their wait ends.
- Never: remove/rename tasks, resurrect an id with a row (resume would skip
  new work keyed to a merged row), empty a verify gate (Rule 4), touch
  project-level keys.
- Every proposal lands in the `plan_proposals` table and as a `plan.amend`
  event regardless of outcome; read it via the read-only
  `GET /api/plan-proposals` route. Full schema and boundary:
  [docs/taskfile-schema.md](docs/taskfile-schema.md) § "The plan is a living
  document".

### Rule 4c — Every agent run hands off through the task dossier

A task outlives any one session: restarts, plan-window swaps and
escalations hand it to a fresh — often different — model. The durable
record is `dossier.py` (table `task_dossier` in `config.DB_PATH`), never a
chat summary.

- **Write the handoff.** Before finishing, an implementer MUST write
  `.arc/handoff.md` in its worktree with `## Done`, `## Remaining`,
  `## Decisions` (with reasons), `## Dead ends` (what failed and why),
  `## Gotchas`, `## Next step` (the implement prompt says so,
  `dossier.HANDOFF_PROMPT`). The node harvests it right after every agent
  run — crash paths included — and deletes it; it is a
  `gitstore.CHANNEL_FILES` entry, so it never reaches a commit or a review
  diff.
- **Read the dossier.** Every implement, review and PR-review prompt starts
  with `dossier.render(...)` once the task has history. Decisions listed
  there are not relitigated and Dead ends are not retried without a new
  reason.
- The orchestrator records every outcome (`dossier.record_attempt`) and
  every model change with its cause (escalation, usage swap). Operators
  read it with `main.py code context <task>` and add notes with `--note`.
  Contract: [docs/orchestration-contract.md](docs/orchestration-contract.md)
  § "The task dossier".

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

- `config.PR_REVIEWERS_WANTED` (default 2) is the *wanted* number of
  reviewers reading the **real PR diff** via `gh pr diff`, each with its own
  prompt and no knowledge of the others' verdicts. Reviewers must come from
  families other than the implementer's; `config.PR_REVIEWERS` is the effective
  `max(1, min(wanted, families - 1))`. With only two families it resolves to
  1, so exactly one cross-family reviewer reads each PR.
- **This WEAKENS the gate, and the code says so out loud.** When the
  roster cannot field the wanted number, `pr_fanout` emits
  `task.pr_review_thin {task, pr, wanted, got, reviewers, implementer}`
  so a thin review is visible in the event log and the
  dashboard, not silent. The **compensating control is the pre-merge review
  of Rule 2**: a bounded diff is judged there by the opposite family first,
  and the PR review is the second read of a change that already passed a
  cross-family gate. Do not describe the PR gate as "two independent
  readings" any more — with two families it is one.
- **Unanimous approval is required** among the reviewers who did run. Any
  rejection posts the blocking issues as a PR comment and sends the task back
  to `implement`; the next commit updates the same PR and a new round begins.
- The loop is bounded by `config.PR_MAX_ROUNDS` (default 16); exhausting it
  fails the task rather than looping forever.
- The pool only holds families other than the implementer's — except while
  the Rule 2 capacity hatch (`ARC_ALLOW_SAME_FAMILY_REVIEW`) is on, when it
  deliberately inverts to the implementer's own family. Otherwise unanimity,
  the round budget, and `task.pr_review_thin` always apply on a genuinely
  independent reading.
- **A reviewer that crashed did not review.** If no reviewer objects but one
  never ran — or its session ended without a parseable verdict — the round is
  *inconclusive*, not a rejection: nothing is posted
  as `--request-changes`, the task goes back to `pr_review` rather than to the
  implementer, and the retry comes from `config.PR_MAX_INCONCLUSIVE` — kept
  separate from `PR_MAX_ROUNDS` so infrastructure failures cannot eat the
  rounds reserved for real disagreement about the code (`ARC_PR_MAX_INCONCLUSIVE`,
  default 20). On an inconclusive retry, `pr_fanout` remembers crashed models
  for this task and pairs each crashed reviewer with a healthy eligible
  reviewer of the same or a stronger tier, so a hard-tier crash does not
  block a healthy medium reviewer from replacing a medium-tier crash. A
  crashed model that still looks idle loses to that replacement. It records
  the selection and reason in `task.pr_review_selected`. If no such reviewer
  is eligible, it retries the crashed reviewer; it does not substitute a weaker
  reviewer or waive cross-family review. A genuine objection still beats a
  crash and clears this retry preference. Before this, a crash was posted to
  a public PR as "changes requested: reviewer crashed" and sent the
  implementer to fix issues that did not exist.
- **A conflicting PR is resynced, not abandoned.** `pr_merge` merges the
  current base into the task branch (`gitstore.sync_with_base`, which ABORTS
  on failure so a genuine overlap never leaves a half-merged worktree for the
  next publish to commit), pushes, and routes back to `pr_review` — the diff
  changed, so the approval it already has no longer covers it. Bounded by
  `config.PR_MAX_RESYNCS` (`ARC_PR_MAX_RESYNCS`, default 12). A real textual conflict still stops,
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

| Layer | Where | deepseek | glm | Override |
|---|---|---|---|---|
| Per-account API caps | `config.FAMILIES[*].limit` (ARC rejects over-limit per model) | 10 | 4 | `ARC_LIMIT_<FAMILY>` |
| Driver semaphores + leases | `config._MODEL_DRIVER_CAP` — ARC **sessions** divided by how many one harness process holds at once (GLM is pinned at its full account budget, `config._DRIVER_CAP_PIN`) | 10 | 4 | `ARC_DRIVER_LIMIT_<FAMILY>` |
| **Harness pool** | `config.harness_limit` via `drivers._harness_gate` + a `harness:<name>` lease | opencode (glm): **5** total | reasonix (deepseek): **10** total | `ARC_HARNESS_LIMIT_<HARNESS>` |

**A harness process is not one ARC session.** The session ceilings measured
on this fleet were gpt-oss 5, DeepSeek(V4-Flash) 5, GLM 4, Kimi 3 — that was
the retired fleet: gpt-oss-120b and Kimi-K3 have since left it, and
DeepSeek's current 10 is the provider-published figure for V4.1 (provider
docs updated 2026-09-12), not a measurement of ours. The retired one-shot
opencode run (opencode, and reasonix by the same assumption)
issued parallel tool calls and held about TWO sessions at once, so a driver
cap set equal to the session limit over-subscribed by that factor. Since
2026-09-16 that per-attempt spawn is gone for opencode: it runs through ONE
persistent `opencode serve` server, with ONE session per run — the "two
sessions per process" factor is a property of the retired spawn, not of the
serve path, and reasonix keeps the assumed-2 factor until measured. Measured
from the event log: 23 capacity rejections in four hours, GLM-5.3 refused with
as few as TWO of our drivers live against a ceiling of four — and the
backend's rejection text has twice contradicted the official table: "max 3 in
flight per user on this backend" on 2026-09-14 (once while ZERO fleet drivers
were alive, so other consumers of the key held slots) and "max 5 in flight" on
2026-09-15. Both are dated observations of a shared account cap, not the
stated value: GLM's ceiling is the official docs value 4 (docs.arc.vt.edu,
adopted per operator directive 2026-09-15).
Driver caps are
therefore sessions // sessions-per-process for the non-pinned models;
opencode holds two
(`config._SESSIONS_PER_PROCESS`); reasonix is counted as ONE session per
process since 2026-09-24 (operator directive: use all 10 DeepSeek seats; the
reasonix load test saw no capacity rejections), so DeepSeek 10 // 1 = 10. GLM-5.3's driver
cap is PINNED at the account's full 4 (`config._DRIVER_CAP_PIN`, operator
directive 2026-09-15): in-flight GLM sessions tracked one per harness, so
halving threw away half the slots the account grants — an over-cap burst
surfaces as 400s the capacity backoff retries.
GLM's four driver slots are shared across processes through the lease table
(`driver.cap_wait`), so dips below 4 in the account cap surface as waits and
retried 400s the backoff absorbs rather than self-inflicted failures; batch
callers see one slot fewer (3) while `INTERACTIVE_RESERVE` holds one back for
chat, and `ARC_INTERACTIVE_RESERVE=0` hands batch all four.
`ARC_DRIVER_LIMIT_GLM=1` re-serialises GLM harnesses if the backend tightens
persistently.
gpt-oss and DeepSeek were previously configured at 8 against a real
ceiling of 5, so the fleet generated its own 400s under load and blamed the
provider.

**The harness layer is the one people forget, and it is often the binding
one.** Each harness now has its OWN pool, because the two models no longer
share a binary: **opencode (GLM-5.3) is capped at 5 and reasonix (DeepSeek) at 10**
(`config._HARNESS_CAP`). Since 2026-09-16 every opencode-backed model runs
through ONE persistent `opencode serve` server (one process per orchestrator,
one session per run, bound to the worktree via the `x-opencode-directory`
header and ended via the `instance/dispose` route), backed by ONE ~240MB
sqlite store in `~/.local/share/opencode`, so
GLM's driver cap of 4 sits just under the opencode pool — the retired
three-model fleet's GLM 2 + DeepSeek 5 = 7 opencode processes is history.
reasonix keeps per-workspace session files under its own home
(`config.REASONIX_FLEET_HOME`), with no central store, and its load test ran
2026-09-14: 3/3, 6/6 and 7/7 concurrent one-shot runs exited clean with no
ARC capacity rejections — no cliff up to 7, where opencode's was at 6.
Measured on the shared opencode pool, with an identical prompt and a warm cache:

| concurrent | 3 | 4 | 5 | 6 | 10 |
|---|---|---|---|---|---|
| succeeded | 3/3 | 4/4 | 5/5 | 4/6 | 4/10 |

Past five it fails fast with an EMPTY stderr, which the fleet logged as
`opencode exited 1: ` and retried four times per task — burning the retry
ladder on self-inflicted contention. A model sitting under its own cap is NOT
available if its harness is full, which is why `code_tasks._reviewer_pressure`
scores a reviewer on whichever ceiling binds first.

- The **account caps are per API key, not per process** — other agents and
  interactive sessions share them (config.py:125).
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
  Leases are reaped when older than `config.DRIVER_LEASE_TTL` (derived as
  `longest_total_timeout() + DRIVER_CAPACITY_BACKOFF_CAP + 300` when total
  budgets are finite, a fixed 24 h when they are unlimited — the default) or
  when the owning pid is dead, so killed runs never deadlock the fleet.
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
- `/api/repos/create`, `/api/repos/remote` and `/api/chat/*` (and the
  read-only `/api/repos`) already live inside that discipline: `/api/chat/start`
  accepts a `repo` only byte-identical to an entry of `GET /api/repos` and
  spawns one fixed argv (`main.py chat ...`); `/api/repos/create` makes a
  local git checkout under `~/repos` (git init + one commit) and then
  best-effort asks `gh repo create` for a remote — a machine without gh
  keeps its local repo, with the reason as a note; `/api/repos/remote` does
  the same for an existing checkout and accepts a `repo` only
  byte-identical to a `/api/repos` entry. The only body values that reach
  `gh` are the repo name and a private flag — the argv stays fixed.
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
  (distinct from `failed` red), the last review verdict on each node, and —
  on any node that has been sent back to code — a third status line
  `↺ xN (⬆M) — gate failed | review rejected (k issues)` in amber while the
  latest bounce is unresolved (muted grey once the newest gate/review passed
  again), with the full reason tail on the tooltip.
- Harness-level resilience: there is **no total wall-clock budget** by
  default (`config.DRIVER_TIMEOUT` and the per-role `config.ROLE_TIMEOUT`
  map are all 0 = unlimited since 2026-09-14; opt a cap back in via
  `ARC_DRIVER_TIMEOUT` / `ARC_PLANNER_TIMEOUT` / `ARC_REVIEWER_TIMEOUT` /
  `ARC_IMPLEMENTER_TIMEOUT`). Total budgets kept killing healthy work —
  GLM-5.3 reads for 60–85 min before its first edit on a hard task, and
  every total cap tried (900/2700/5400 s) killed it mid-task with zero
  edits, the last at the moment it had located every edit site. The one
  kill that remains is `config.DRIVER_IDLE_TIMEOUT` = 840 s (override
  `ARC_DRIVER_IDLE_TIMEOUT`, planner 3000 s via `ARC_PLANNER_IDLE_TIMEOUT`),
  a **stall detector**: a harness that produces no stdout for that long is
  waiting on a request that is not coming back, so it is killed and retried
  rather than waited out. A working harness streams constantly and the
  measured time-to-first-token tail is 308.9 s, far under the budget, so
  silence is the only honest "dead" signal. Every stall records forensics first — process state,
  CPU delta, last tool calls, and whether an API request is outstanding — see
  [docs/runbook.md](docs/runbook.md) § "A harness stalled".
  (An earlier version of this rule said ARC "holds rejected/queued requests
  open instead of erroring". That is **wrong**: measured 2026-09-09, ARC
  rejects over-cap requests in ~0.2 s with
  `400 {"detail": "concurrent session limit reached"}`. Those 400s are what
  the capacity backoff exists for; the long silences are a separate,
  still-unexplained failure.)
  Retries use exponential
  backoff (capped at 60 s) up to `config.MAX_RETRIES` = 24. If every retry
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

### Rule 7d — Every game change is SEEN: screenshots and video, everywhere a decision is made

A green gate proves what the tests measure. A game is judged by looking at
it, so for every task whose worktree is a Godot project (`project.godot`),
the gate node runs `evidence.capture` right after `verify_cmd` passes:

- **Screenshots** from the project's fixed anchor cameras
  (`studio/evaluation/camera_system.py`, overridable by a committed
  `studio_cameras.json`) — the same viewpoints every attempt, so they compare.
- **A flythrough video** through those anchors, recorded with Godot's movie
  writer (`--write-movie`), kept as `.mp4` plus a small looping `.gif`.
- **The scripted playtest recorded** (`tools/playtest.gd`) when the game has
  one, plus the screenshots it takes along its route.
- **Before | after | difference** per camera against the SAME cameras
  rendered at the task's **merge base** (never the live base branch — the
  `gitstore.diff_full` rule; siblings' merges are not this task's change),
  with the share of pixels changed. Near-solid frames are flagged as blank
  renders.

That evidence goes to every place a decision is made:

- the **pre-merge reviewer** and **every PR reviewer** get the images attached
  (drivers' `images`) and an evidence block in the prompt that makes a visible
  regression a blocking issue, exactly like a failing test;
- the **pull request** gets a comment with the comparisons, screenshots and
  videos inline — pushed to the game repo's orphan `arc-evidence` branch
  (`ARC_EVIDENCE_BRANCH`) so a private repo renders them for its viewers;
- the **agent board** gets a `kind: "evidence"` post (`board.py`);
- the files stay under `logs/evidence/<project>/<task>/x<attempt>/`.

Rules the code holds to:

- **Nothing is written into the worktree.** The capture harnesses run from a
  temp directory (Godot takes an absolute `--script` path) and every untracked
  file the capture creates is deleted — publish's `git add -A` would ship it.
- **`ARC_EVIDENCE=required` (default): a project that will not render fails
  the gate** with the Godot errors as feedback. `best-effort` downgrades that
  to a warning; `off` disables capture. A task opts out with
  `"evidence": false` (documentation-only work).
- **A machine that cannot capture never fails a task** — no display, Godot or
  ffmpeg is `evidence.unavailable`, an infrastructure gap, not the
  implementer's bug. Rendering needs a display: WSLg provides `:0`; elsewhere
  run Xvfb and set `ARC_STUDIO_DISPLAY`.
- A publish or comment failure never fails publish (`evidence.publish_failed`
  with a fingerprint): the reviewers already had the images.
- Events: `evidence.captured`, `evidence.failed`, `evidence.unavailable`,
  `evidence.error`, `evidence.posted`, `evidence.publish_failed`.

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
  `ARC_CHAIN_TIMEOUT` budget (default **12 h**, poll every 10 s) bounds the
  wait. Either ends the run with exit code 1 and a `chain.blocked` event
  (`chain.wait`/`chain.ready` bracket the gate — Rule 7).
- CLI surface: `main.py code run <taskfile> --no-wait` pre-flights the
  chain and exits **2** when not ready (0 when ready) so queue wrappers can
  requeue instead of blocking a slot; `main.py code status` reports per-file
  chain readiness under `chains`. Full semantics:
  [docs/taskfile-schema.md](docs/taskfile-schema.md) § "Project chaining".
- The dashboard shows the chain in both directions (`_project_chain`,
  dashboard.py): a chained project sits under "waiting on another project",
  its DAG starts with a dashed ⛓ gate node per upstream taskfile (click =
  that project), and a run holding at the gate is labelled so — never "live".

### Rule 10 — A product's structure lives in that repo, not in this file

This `AGENTS.md` governs the fleet. The repo a taskfile names
(`project.repo`) has its own contract: `AGENTS.md` and `CLAUDE.md` at its
root, plus `*.md` / `*.mdc` files in `.cursor/rules`. Harnesses already run
with that worktree as their working directory. The prompts name the files
so they are read, and so fleet rules are not copied into the product.

- `project_contract.discover` lists those files and ignores a symlink that
  resolves outside the repo. `status_line` is the one line `describe` prints
  on `code plan` and `code run --dry-run`.
- `project_contract.planner_block` is inserted into `code_tasks.plan_tasks`,
  `orchchat.build_prompt`, and `studio.planner.system_prompt`. When a
  contract exists, the planner gets a capped excerpt and must bake the
  rules into every task prompt. When it does not, a goal that starts or
  reshapes the project must make the first task add `AGENTS.md`; a small
  change to an existing codebase must not invent that task.
- `project_contract.role_block` is passed into `code_tasks._impl_prompt`,
  `_review_prompt`, and `_pr_review_prompt` from the task worktree.
  Implementers are told to read the contract and to ignore this file.
  Reviewers may reject a diff that breaks a stated rule the task did not
  override, and must not invent a violation when no contract file exists.
- `project_contract.captain_block` is inserted into `captain.build_prompt`.
  The captain tells the operator when the bound repo has no contract. It
  does not edit either repository.
- A missing contract does **not** fail `load_taskfile`. Existing repos keep
  running. The absence is reported, not gated.

Talk to the fleet from this checkout (captain, `code plan`, or a session
whose job is the orchestrator). Put layout, test commands, and "do not
touch" rules in the product repo's own `AGENTS.md`.

### Benchmarking exception — the bench `policy` escape hatch

`code_tasks.load_taskfile` / `code_tasks.build_code_graph` accept an optional
`policy` dict that widens the rules above **for benchmark variant runs
only**. The default (`policy=None`) enforces Rules 1–9 byte-for-byte, and
every normal path (`code plan`, `code run`, dashboard project runs) passes
no policy.

`orchbench.py` (`main.py code bench`) declares the benchmark matrix: each
entry in `orchbench.VARIANTS` is an explicit, named policy — e.g.
`glm-implement-only` (GLM implements, never reviews),
`deepseek-reviews` (an implement-only model reviewing),
`self-review`, `no-review`, `kimi-via-opencode` (harness swap),
`all-glm` / `all-deepseek` (flat routing), `misroute`
(tier-inverted routing), `no-fixloop` / `fixloop-1` (`max_fix_rounds` 0/1) —
so we can measure which governance options actually matter. Some variant
names still carry models the fleet has retired (`kimi-implement-only`,
`gptoss-reviews`, `all-kimi`): `orchbench.VARIANTS` is a dated benchmark
config of historical comparisons, not a live roster, and no normal run can
reach those models through it. Two policy keys
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
  <roster-model>]`: classifies open issues (kind bug|feature|question|docs,
  size S|M|L, recommended tier per `config.IMPLEMENT_TIERS`), prints a triage
  table, and writes a ready-to-run taskfile to `~/tasks/<repo>-issues.json`
  with correct cross-review pairing (fill in each `verify_cmd` and dry-run
  before executing — Rules 4/8 apply once it becomes a taskfile).
- **`issue-maker`** — `main.py gh issue "<desc>" <repo> [--create]`: drafts a
  structured issue (title; body with context/repro/acceptance) and prints it.
- **`pr-reviewer`** — `main.py gh pr-review <repo> <N> [--post]`: reviews a PR
  under the same verdict JSON contract as internal review (it reuses
  `code_tasks._parse_verdict`; see docs/orchestration-contract.md).

Any model **trusted to plan on today's roster** may hold these three roles —
gh roles require planner permission, and today only **GLM-5.3** has it, so
`config.GH_MODEL` resolves to GLM-5.3 (DeepSeek-V4.1-Flash-thinking-max has
no `planner` role and is refused);
`drivers.OpencodeDriver.__init__` / `drivers.DeepseekDriver.__init__` raise
`ValueError` for a model off the roster or one lacking the permission (same
roster-driven enforcement as Rule 2). Default model `config.GH_MODEL`
(`ARC_GH_MODEL` env), falling back to `config.PLANNER_MODEL` =
GLM-5.3; each `gh` subprocess is bounded by
`config.GH_TIMEOUT` (`ARC_GH_TIMEOUT`, default 120 s). Pipeline pushes and
`gh pr create` retry NETWORK failures only (`gitstore.is_transient_network_error`)
with backoff `ARC_NET_RETRY_DELAYS` (default `5,15,45,90,180` s, `git.retry`
events); a lease, auth or no-commits refusal is never retried. A GitHub
API-quota refusal (`gitstore.is_rate_limited`: the 5000/h GraphQL quota is
shared by every run, the dashboard and any other tool on the account) is not
network weather: pipeline `gh` calls wait for the reset `gh api rate_limit`
reports, bounded per call by `ARC_GH_QUOTA_MAX_WAIT` (default 3600 s,
`git.quota_wait` events), and `gh pr create` first falls back to the REST
API, which has its own quota (`git.rest_fallback`).

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
| `bench.py` / `bench_data.py` | Single-model micro benchmark (top-level `main.py bench`): 31-task dataset × models × harness solvers (direct/fanout/fixloop/review/opencode/kimi), pass@k scoring — measures models and harnesses in isolation |
| `code_tasks.py` | The multi-harness code workload: taskfile loader/validation, the GLM-5.3 planner prompt (`plan_tasks`, model `config.PLANNER_MODEL`), per-task chain `alloc → implement → gate → review → publish/fail` with fix-loop and `escalate_<tid>` escalation edges, project-level `after` chain gating (`chain_wait`), resume of re-run taskfiles |
| `captain.py` | The conversational supervisor (`main.py captain`, the dashboard Captain panel): gathers LIVE fleet state (`fleet_state` — task rows by status, the three concurrency layers via `capacity_snapshot`, recent events), runs one captain turn on `config.PLANNER_MODEL`, and executes a CLOSED action set (`parse_actions` / `execute_actions` — `plan`, `run`, `resume`, `status`, `amend`) as fixed `main.py` argv. `plan_pressure` is the capacity-aware admission gate: a `run`/`resume` whose implementer models have no free driver slot is QUEUED (`logs/captain/queue.jsonl`) instead of launched into capacity refusals. Sessions live under `logs/captain/` (`ARC_CAPTAIN_DIR`), separate from chat. It never edits code or touches git — all governance stays in the pipeline. |
| `config.py` | Single source of truth: model families + caps, tier maps, driver caps, timeouts, paths — every `ARC_*` env override lives here |
| `graph_shapes.py` | The graph BETWEEN tasks (§1 "Two graphs"): the pattern catalogue as data (`PATTERNS`, with a drawable sketch each), `normalize_pattern` (label aliases → catalogue id, used by the loader), `classify` (the shape a taskfile's `deps` actually form: single/chain/fanout/fanin/diamond/hierarchical/mixed, width, depth, declared-vs-detected mismatch), `planner_prose` (the GRAPH DESIGN block of the planner prompt, from the catalogue and today's caps), `describe` (→ `GET /api/graph-shapes`: patterns, every taskfile classified, what the engine can and cannot express) |
| `dashboard.py` | Dashboard server (`main.py serve`, default port 8787): static UI + JSON APIs over `orchestrator.db`, `logs/events.jsonl` and live harness transcripts — **not read-only**: `do_POST` (dashboard.py:999) serves `/api/projects/create`, which spawns `main.py code plan` (goal mode) or writes taskfiles into `~/tasks` directly (dashboard.py:840-842), and `/api/projects/run`, which launches `main.py code run` (optionally `--dry-run`) subprocesses via `subprocess.Popen` (dashboard.py:768-770). It also serves the orchestrator-chat routes: `GET /api/repos` (repo allowlist scanned from the repos root, default `~/repos`), `POST /api/repos/create` (local `git init` + one commit, then best-effort gh remote creation), `POST /api/repos/remote` (gh remote for an existing allowlisted checkout), `POST /api/chat/start` (appends the user turn to the session jsonl, spawns `main.py chat`, rejects any repo not on the `/api/repos` allowlist), and `GET /api/chat/poll` (turns from an index + running flag + newest taskfile) |
| `drivers.py` | Headless CLI harness drivers: `OpencodeDriver` (`opencode`, GLM-5.3 — since 2026-09-16 the ONLY path is the persistent `opencode serve` server reached through `ocserve.py`: one server per orchestrator, one session per run over `x-opencode-directory`, prompts on the async route, results over SSE, dispose on exit; the one-shot `opencode run` spawn is retired) and `ReasonixDriver` (`reasonix`, DeepSeek-V4.1-Flash-thinking-max since 2026-09-13 — `reasonix run --output-format stream-json`: every tool call, text delta and token receipt on stdout, final `{"type":"result"}` object carries the answer and session id; a private `REASONIX_HOME` generated by `reasonix_fleet_home`); `DeepseekDriver` (`dsh`, 2026-09-12..13, historical — streams reasoning on stderr, prints only the final message on stdout, pumps both pipes for the stall clock, no session resume, 0 tokens reported); `KimiDriver` still exists for historical transcripts only (no live model runs the kimi harness); per-model semaphores, retries, timeouts, live transcript streaming to `logs/harness/` |
| `dream_rsi.py` | Dream-RSI offline policy improvement (`main.py code dream`, arXiv:2609.14858): rebuilds the *discovery tree* a run produced from `code_tasks` + `harness_runs` (`build_tree`), re-scores attempts (`attempt_score`), replays exploration policies against recorded history with Eq.1 (`replay`, `score_policies`, `improve`), and offers an LLM policy-development hook (`propose_source`, `compile_policy` — sandboxed) — no model calls, no git. Prose: [docs/dream-rsi.md](docs/dream-rsi.md) |
| `dossier.py` | The task dossier (Rule 4c): durable per-task handoff record (`task_dossier` table) — attempt outcomes, harvested `.arc/handoff.md` sections, model changes, operator notes; `render` is the prompt block every agent run starts with, `main.py code context` prints it |
| `evidence.py` | Visual evidence (Rule 7d): screenshots from the fixed anchor cameras, a flythrough video (Godot movie writer → mp4 + gif), the scripted playtest recorded, before/after/diff against the task's merge base, blank-render detection; publishes to the game repo's `arc-evidence` branch and renders the PR comment and reviewer prompt block. Never writes into the worktree |
| `events.py` | Append-only JSONL event log `logs/events.jsonl` with contextvars attribution (`workload`/`round`/`iteration`/`module`) and 100 MiB rotation |
| `fleetwatch.py` | Fleet watchdog (`deploy/arc-watchdog.service`, always on): records the argv/cwd/env of every live `code run`, re-runs any that died with unfinished work (the normal resume path), holds chained taskfiles until `chain_status` is ready, never overrules a `run.stopped` (operator Stop), and parks a taskfile after repeated quick exits. Never kills a run or touches git. State: `logs/watchdog/` |
| `gh_ops.py` | GitHub operations agents over the `gh` CLI (`main.py gh …`): `issue-triager`, `issue-maker`, `pr-reviewer` — standalone tools outside the governed pipeline; preview by default, only `--apply-labels`/`--create`/`--post` write to GitHub |
| `gitstore.py` | The only git actor: worktree `alloc`/`publish`/`sync_with_base`/`push_task_branch`/`open_pr`/`merge_pr`/`fast_forward_base`/`cleanup` on `task/<id>` branches (120 s per-git-op timeout); nothing merges locally |
| `graph.py` | Generic async DAG engine: named nodes, conditional edges (`when=`), gather nodes, `max_steps` bound |
| `main.py` | CLI entry point: `run`, `once`, `status`, `graph`, `studio`, `serve`, `bench` (micro), `chat` (one conversational planner turn over a session jsonl — module `orchchat.py`), `captain` (one state-aware supervisor turn — module `captain.py`), and `code {plan,run,status,dream,bench}` |
| `orchbench.py` | Orchestration variant benchmark (`main.py code bench`): 14 named policy variants of the governed code DAG (routing, reviewer, harness, fix-loop) on a fresh `filetoolkit` repo per variant, with merge/integration scoring — benchmarks the orchestration options set, not single models |
| `board.py` | Shared agent board: `.arc/board.jsonl` in the task worktree plus `logs/boards/<project>.jsonl`. A session id resumes only on the harness that posted it |
| `plan_amend.py` | The living-plan channel (Rule 4b): prompt schema, `.arc/plan_proposals.jsonl` harvest (read + delete before `git add -A`), loader-validated amendment of the taskfile with per-entry rollback, `plan_proposals` recording |
| `project_contract.py` | The target repo's agent contract (Rule 10): discovers `AGENTS.md`, `CLAUDE.md`, and `.cursor/rules` inside the product repo, and supplies the planner, implementer, reviewer, captain, and chat prompts. This file stays fleet-only |
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
| `docs/` | Detail reference docs — see [Links](#links); includes `graph-patterns.md`, the prose behind `graph_shapes.PATTERNS` (the planner is handed the catalogue from code, not the doc) |
| `deploy/` | systemd units: `arc-orchestrator.service`, `arc-dashboard.service` |
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
[docs/runbook.md](docs/runbook.md).

Two directories, two contracts (Rule 10). This checkout is where you talk
to the fleet: the captain (`main.py captain --repo /path/to/product`), the
chat panel, or `code plan`. The product checkout (`~/repos/<project>`,
passed as `--repo` or as the plan path) is where that project's `AGENTS.md`
lives. A session opened in this directory loads *this* file and does not
see the product's layout unless the plan step reads the product repo.

The loop:

1. **Plan** — `.venv/bin/python main.py code plan "<goal>" /path/to/repo`
   (GLM-5.3 drafts a taskfile into
   `~/tasks/<slug>.json` and prints the
   resolved DAG). Total budgets are unlimited by default, so a large goal
   needs no timeout env var (the planner idle budget is 3000 s;
   [docs/runbook.md](docs/runbook.md) § "Planning a large goal").
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
- [docs/concurrency-limits.md](docs/concurrency-limits.md) — the three layers
  of concurrency caps (account, driver, harness) and their env overrides
- [docs/taskfile-schema.md](docs/taskfile-schema.md) — taskfile JSON reference
  and validation rules
- [docs/graph-patterns.md](docs/graph-patterns.md) — the graph-pattern library
  for multi-agent work (chain, fan-out/fan-in, diamond, router,
  orchestrator-workers, …); the `code plan` planner
  (`code_tasks.plan_tasks`) consults it and records its choice as
  `"pattern"` in the taskfile
- [docs/dream-rsi.md](docs/dream-rsi.md) — Dream-RSI: offline improvement of
  *how* a run explores, by replaying recorded history (`main.py code dream`,
  module `dream_rsi.py`); no model calls, no git
- [docs/runbook.md](docs/runbook.md) — operator runbook: dashboard,
  plan → dry-run → run, troubleshooting
- [docs/audit-2026-09-09.md](docs/audit-2026-09-09.md) — reliability audit
  of 2026-09-09: the twelve coupled defects behind stalled fleet runs
  (moving-ref review diffs, escalation on interrupted runs, leaked driver
  slots, uncleaned shutdown) and the reasoning behind each fix
- [README.md](README.md) — project overview, dashboard quick start, 24/7
  setup

<!-- graft:start -->
## Graft — repo context graph

When this worktree has a code graph (the orchestrator builds one before every
implement attempt and exports its location as `GRAFT_DIR` — always pass it:
`graft --dir "$GRAFT_DIR" <command>`), get context from it before grepping or
opening source files — each call is one exact answer where a grep ladder is
several turns:

- `graft --dir "$GRAFT_DIR" ask "<question>" --source` — ranked definitions
  with the relevant lines inlined; reuse literal identifiers (symbol, error
  string, file name) as the query. For "every occurrence" tasks use
  `graft --dir "$GRAFT_DIR" grep "<literal>"` (exhaustive, grouped by
  enclosing symbol) instead of ranked results.
- `graft --dir "$GRAFT_DIR" skeleton <file>` — every signature with its line
  span, far cheaper than reading the file; skim an API surface this way.
- `graft --dir "$GRAFT_DIR" callers <symbol>` — exact callers; `--direction
  out` for callees, `-d 2` for the transitive blast radius. Run it before
  changing a signature.
- `graft --dir "$GRAFT_DIR" map` — directory clusters, hubs and hotspots.

Open a source file only at the exact `file:line` range a hit names; never
re-read whole files. The graph refreshes itself before each query, so after
your own edits the answers are current. Without the binary these commands
do not exist and the task prompt says so — fall back to grep.
<!-- graft:end -->
