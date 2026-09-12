# Graph patterns for multi-agent work

The pattern library for planning code runs in this repo — the prose behind
`graph_shapes.PATTERNS`. The `code plan` planner (`code_tasks.plan_tasks`,
`config.PLANNER_MODEL`) is handed the catalogue **from code**
(`graph_shapes.planner_prose`, so a planner working on another repo does not
need this file), picks **one** pattern per goal by the kind of work, designs
the taskfile's `deps` graph to match it, and records the choice as
`"pattern": "<id>"` in the project object. The loader normalizes the label
to a catalogue id (`fan-out-fan-in` → `fanout`; unknown names pass through
with a warning). The `deps` are still the whole graph: `graph_shapes.classify`
derives the shape they actually form, and `code_tasks.describe` — printed
after `code plan` and on every dry run — reports declared vs detected and
flags a mismatch. The dashboard's "Graph shapes" view (`GET /api/graph-shapes`)
draws every pattern and classifies every taskfile in `~/tasks`.

Classification rules (deps between the file's own ids only): **single** —
one task; **chain** — one path, every task ≤ 1 dep and ≤ 1 dependent;
**fanout** — no task has two deps (parallel heads, single-dep tails may hang
off them); **fanin** — several heads and one join (the catalogue's
fan-out/fan-in); **diamond** — one root, a split, and one join whose sides all
descend from the root (a join that redundantly lists the root still counts);
**hierarchical** — several joins or fan-out under fan-out; **mixed** —
anything else.

Grounding first, then nine patterns, then a decision table, then what the
engine can and cannot express yet. Sources at the bottom.

## Ground rules of this system (the same for every pattern)

Two facts from the literature shape everything below:

- **Workflows, not free agents.** Anthropic ("Building effective agents") and
  LangGraph both split agentic systems into *workflows* (LLMs moved along
  predefined code paths) and *agents* (LLMs directing themselves at runtime).
  OpenAI's Agents SDK doc calls the same split "orchestrating via code" vs.
  "orchestrating via LLM". This repo is deliberately a **workflow**: the
  taskfile fixes the graph, `load_taskfile` enforces routing/review rules,
  and the only runtime freedom is inside each harness session. All sources
  agree: prefer the simplest deterministic structure that can work; add
  dynamic orchestration only when subtasks cannot be known in advance.
- **The per-task gauntlet is fixed; patterns shape the graph BETWEEN tasks.**
  Every taskfile task already runs
  `alloc → implement → gate(verify_cmd) → cross-review → publish → PR →
  unanimous PR review → merge` with a bounded fix loop and tier escalation.
  A "pattern" here is a recipe for task ids, `deps`, model tiers, and
  reviewer pairing — never a way to skip the gauntlet.

### Model tiers and driver caps (fan-out arithmetic)

The roster is dated and API-validated (`config.ROSTER`); this is the
2026-09-12 snapshot — `main.py capacity` prints today's effective caps.

| Model | Tier | Roles | Measured cap | Driver slots |
|---|---|---|---|---|
| GLM-5.3 | medium | implement/plan/review/PR-review | 4 | 2 |
| Kimi-K3 (until 2026-09-19) | hard | implement/plan/review/PR-review | 3 | 3 |
| DeepSeek-V4.1-Flash-thinking-max | hard | implement/plan/review/PR-review; planner | 5 | 2 |

- Reviewer is always cross-family: `reviewer` names a family in
  `config.REVIEW_FAMILIES` (`deepseek`, `kimi`, `glm`) other than the
  implementer's. Split reviews so no family idles.
- Fan-out wider than a family's driver slots simply **queues** (leases are
  cross-process; over-cap tasks wait, they do not fail), and every opencode
  model (GLM, DeepSeek) also shares ONE harness cap of 5. Width beyond the
  cap costs latency, not correctness — but a fan-out of 6 tasks all routed
  to GLM is really a chain of 3 batches, so plan for it or spread tiers.
- Keep `files_hint` disjoint across parallel tasks. Two implementers editing
  one file = merge conflict = failed task.
- `deps` joins are real: a task with two or more deps gets a gather node
  (`code_tasks.build_code_graph`, `wire_deps`) that waits for **every** dep's
  PR to merge. Order in the list does not matter. (It used to be the last
  entry only — a dependent could branch from a base missing the code it
  depended on; that is gone.)

Taskfile task shape (see docs/taskfile-schema.md):

```json
{"id": "kebab-id", "title": "...", "prompt": "<self-contained spec>",
 "model": "GLM-5.3|Kimi-K3|DeepSeek-V4.1-Flash-thinking-max",
 "reviewer": "kimi|glm", "verify_cmd": "./check.sh && ...",
 "files_hint": ["..."], "deps": ["other-id"]}
```

---

## 1. Chain / pipeline (sequential)

```
A --> B --> C
```

Each task consumes the previous task's merged output. Anthropic's "prompt
chaining", generalized: each link is a full implementer + gate + review.

- **When:** staged migrations (schema → callers → cleanup), refactors that
  must land in order (introduce API → migrate call sites → delete old API),
  docs that must follow the code they describe.
- **Express it:** `deps` chains. Route each link by its own tier — a chain
  often mixes tiers.

```json
{"project": {"repo": "...", "title": "...", "pattern": "chain",
 "tasks": [
  {"id": "intro-api", "model": "GLM-5.3", "reviewer": "kimi",
   "prompt": "...", "verify_cmd": "./check.sh", "deps": []},
  {"id": "migrate-callers", "model": "DeepSeek-V4.1-Flash-thinking-max", "reviewer": "glm",
   "prompt": "... uses the new API from ...", "verify_cmd": "./check.sh",
   "deps": ["intro-api"]},
  {"id": "delete-old", "model": "GLM-5.3", "reviewer": "kimi",
   "prompt": "...", "verify_cmd": "./check.sh", "deps": ["migrate-callers"]}]}}
```

- **Pitfalls:** never chain for stylistic order — every link pays the full
  gate/review/PR/merge gauntlet, and a dependent starts only after its dep's
  PR has *merged* (not merely opened). If two links don't actually read each
  other's code, unchain them and get parallelism back. Keep chains ≤ 4
  links; longer means the plan is hiding fan-out.

## 2. Fan-out / fan-in (scatter-gather)

```
      +--> B --+
  --> +--> C --+ --> E
      +--> D --+
```

Independent subtasks in parallel (Anthropic's "sectioning"; map-reduce's
map), then one integration/merge task (reduce). LangGraph's Send API is the
same shape with dynamic width.

- **When:** greenfield multi-module builds, repo-wide mechanical changes
  split by directory (one task per slice), research batches, per-criterion
  evaluation passes.
- **Express it:**

```json
{"project": {"repo": "...", "title": "...", "pattern": "fan-out-fan-in",
 "tasks": [
  {"id": "slice-a", "model": "GLM-5.3", "reviewer": "kimi",
   "files_hint": ["a/"], "deps": [], "...": "..."},
  {"id": "slice-b", "model": "DeepSeek-V4.1-Flash-thinking-max", "reviewer": "glm",
   "files_hint": ["b/"], "deps": []},
  {"id": "slice-c", "model": "DeepSeek-V4.1-Flash-thinking-max", "reviewer": "kimi",
   "files_hint": ["c/"], "deps": []},
  {"id": "integrate", "model": "GLM-5.3", "reviewer": "kimi",
   "prompt": "Verify the slices work TOGETHER on merged base; fix seams.",
   "verify_cmd": "./check.sh && full test suite",
   "deps": ["slice-a", "slice-b", "slice-c"]}]}}
```

- **Fan-out width:** 2–4 is the sweet spot (planner rule: 2–8 tasks total).
  Spread the width across families — 2 GLM + 2 DeepSeek + 3 Kimi truly run
  at once; 5 GLM tasks run 2-2-1.
- **Pitfalls:** the fan-in task owns integration honesty — its `verify_cmd`
  must run the *whole* suite against the merged base, not grep for the slice
  files. Respect the `deps[-1]` wait-edge rule: list the slowest/hardest
  slice last. The fan-in task should budget for seam fixes; if slices share
  an interface, land the interface in a chain link *before* the fan-out (see
  diamond).

## 3. Diamond (split → verify → merge)

```
      +--> B --+
A --> +        + --> V --> M
      +--> C --+
```

Fan out two candidate/complementary implementations, then a single strong
**verify** task runs the combined result on the merged base, then the merge
is settled. "Split-verify-merge": the branches may be two halves of one
feature *or* two rival implementations of the same spec; V is what turns a
coin-flip into a decision.

- **When:** risky core changes where one implementation's failure mode is
  hard to foresee; review-heavy work (security-sensitive parsing, protocol
  code); pairs of interacting modules (producer/consumer) whose seam needs a
  dedicated check.
- **Express it:** B and C disjoint `files_hint`, V routed hard with an
  integration `verify_cmd`:

```json
{"project": {"repo": "...", "title": "...", "pattern": "diamond",
 "tasks": [
  {"id": "contract", "model": "GLM-5.3", "reviewer": "kimi", "deps": [],
   "prompt": "Land the shared interface/types both sides will build on."},
  {"id": "side-a", "model": "GLM-5.3", "reviewer": "kimi",
   "files_hint": ["impl/a*"], "deps": ["contract"]},
  {"id": "side-b", "model": "Kimi-K3", "reviewer": "glm",
   "files_hint": ["impl/b*"], "deps": ["contract"]},
  {"id": "verify-integration", "model": "GLM-5.3", "reviewer": "kimi",
   "prompt": "Run the full suite against the merged a+b; repair the seam.",
   "verify_cmd": "./check.sh && pytest tests/integration -x",
   "deps": ["side-a", "side-b"]}]}}
```

- **Width:** 2 branches is the norm; 3 maximum (GLM cap 3, and each rival
  past 2 mostly costs API budget, not information).
- **Pitfalls:** two tasks implementing the *same* files will merge-conflict —
  rivals must write to separate paths (or use debate/vote, §7). V is not a
  substitute for the per-task gates; it exists to test what no branch could
  test alone: the combination. If the contract link is skipped, both sides
  invent their own interface and V becomes a rewrite.

## 4. Router

```
classify --+--(medium)---> GLM task
           +--(hard)-----> Kimi/DeepSeek task
```

Classify the work, send it down exactly one branch. Anthropic/OpenAI
runtime routers pick the branch *while running*; **this system routes at
plan time** — the planner is the classifier, and tier routing is the router
(`load_taskfile` rejects a task routed to the wrong kind of model). Where
routing cannot be decided from the goal text, use a two-phase plan: a cheap
probe taskfile first, then the real one.

- **When:** bugfix vs. feature vs. docs triage; a bug whose location is
  unknown (phase 1 = probe: locate + write a failing reproduction test with
  GLM; phase 2 = fix with the tier the probe justifies);
  migrations scoped by an inventory pass.
- **Express it:** one taskfile per phase — phase 1 explores, phase 2
  executes. Never emit "all branches and let the graph choose": task-level
  edges are unconditional, so every task you emit **will run**.

```json
{"project": {"repo": "...", "title": "probe: where does X break",
 "pattern": "router",
 "tasks": [{"id": "locate", "model": "DeepSeek-V4.1-Flash-thinking-max", "reviewer": "kimi",
   "prompt": "Locate the fault; land a FAILING reproduction test only.",
   "verify_cmd": "! ./py -m unittest discover -s tests -t tests -k Repro",
   "deps": []}]}}
```

- **Pitfalls:** a router whose probe lands code other than the reproduction
  test contaminates phase 2's base. Keep probes read-only-plus-test. The
  second phase is a *new* taskfile; declare `"after": ["<probe taskfile>"]`
  in its project object so it holds at the chain gate until the probe has
  merged (Rule 9) — that is the cross-taskfile edge.

## 5. Orchestrator-workers (supervisor)

```
            planner (Kimi-K3, code plan)
                 |  taskfile
        +--------+--------+
        v        v        v
     worker1  worker2  worker3   (each: implement → gate → review → PR)
        +--------+--------+
                 v
     orchestrator merges (only git actor), reports status
```

The system's default shape, and what Anthropic calls orchestrator-workers
(with the decomposition done at plan time instead of runtime) and
Magentic-One's Orchestrator with its task/progress ledgers (our ledger is
`~/tasks/<slug>.json` + `orchestrator.db` + `logs/events.jsonl`;
Magentic-One's "stall → replan" is our resume/escalate, §9).

- **When:** greenfield builds and any goal whose subtasks are *not* fully
  known up front — the planner's job is exactly to discover them. This is
  the default `main.py code plan` flow; choose it when no narrower pattern
  obviously fits.
- **Express it:** nothing special — 2–8 tasks, deps only where real, the
  planner synthesizes; the orchestrator process is the supervisor (only it
  commits/pushes/merges). Typical skeleton is the fan-out-fan-in one above
  with `"pattern": "orchestrator-workers"`.
- **Pitfalls:** the planner sees only the goal and the repo, so every task
  prompt must be self-contained (paths, function names, acceptance criteria)
  — workers never see the goal. One orchestrator = one planning pass; if the
  plan is wrong the fix is editing the taskfile, not hoping workers
  improvise. Delegation depth is one level — for deeper splits see
  hierarchical (§8).

## 6. Evaluator-optimizer (reflection loop)

```
   +--------------------- fix loop (≤ MAX_FIX_ROUNDS=3) --------------------+
   v                                                                        |
implement → gate(verify_cmd) → cross-review(other harness) → publish → PR
   ^            |fail             |fail                       |2 PR reviewers
   +------------+-----------------+---------------------------+ any reject
                     exhaust → escalate tier up (§9)          (≤3 rounds)
```

Generator + evaluator in a bounded loop until quality is met — Anthropic's
evaluator-optimizer. In this system it is **built into every task**, so you
never wire it by hand; the pattern to *design* is the strength of the
evaluator: an honest `verify_cmd` plus a reviewer from the other harness.

- **When:** all code work (it is mandatory here). Design consequence: for
  tasks where "correct" is subtle (performance, layout, parsing edge cases),
  put the effort into the *evaluator* — a stricter gate (property tests,
  golden files) converts review rounds into cheap gate rounds.
- **Express it:** every taskfile task; tune with `verify_cmd`. Gate feedback
  and review issues both flow back to the implementer verbatim, so a crisp
  failing command = a crisp fix prompt.
- **Pitfalls:** a vacuous gate (`verify_cmd: ""`, or a grep that "proves a
  string is present") pushes all evaluation onto human-visible review rounds
  and PR rounds — slow, and capped (3+3) before the task fails for real.
  Evaluation criteria must exist *before* generation: writing the test after
  the code is optimizing towards your own bug.

## 7. Debate / vote (N implementers + judge)

```
      +--> candidate A (GLM-5.3)  --+
  --> +--> candidate B (Kimi-K3)    + --> judge (hard tier) --> one winner lands
      +--> candidate C (DeepSeek) --+
```

Run the same (or the same-shaped) task N times for diverse answers and
aggregate — Anthropic's "voting" parallelization. Note this system already
votes on **every** task: `PR_REVIEWERS` (default 2) independent reviewers
must unanimously approve the PR. This pattern adds voting at the
*generation* stage for the few tasks that warrant it.

- **When:** design/approach decisions (API shape, schema, algorithm choice)
  where wrong-answer cost ≫ generation cost; tricky bugfixes with multiple
  plausible root causes; research questions. Expensive — reserve for
  genuinely open problems.
- **Express it:** candidates write **disjoint** artifacts (never the same
  code file), a hard-tier judge compares and either merges one or writes the
  synthesis, judged by the other harness:

```json
{"project": {"repo": "...", "title": "...", "pattern": "debate-vote",
 "tasks": [
  {"id": "cand-a", "model": "GLM-5.3", "reviewer": "kimi",
   "files_hint": ["proposals/a.md"], "deps": [], "...": "..."},
  {"id": "cand-b", "model": "Kimi-K3", "reviewer": "glm",
   "files_hint": ["proposals/b.md"], "deps": []},
  {"id": "judge", "model": "GLM-5.3", "reviewer": "kimi",
   "prompt": "Compare proposals a/b against the criteria in ...; implement "
             "the winner in src/...",
   "verify_cmd": "./check.sh", "deps": ["cand-a", "cand-b"]}]}}
```

- **Width:** N=2 candidates is usually enough; N=3 max, and mind that Kimi
  runs at most 2 concurrent sessions, GLM 3. Cost is N+1 full gauntlets.
- **Pitfalls:** two candidate PRs touching the same file cannot both merge —
  either both write proposals and the judge implements, or expect one PR to
  be closed by hand (the orchestrator does not auto-close losers). Juries
  inherit the biases of the judge model; give the judge explicit criteria in
  the prompt, not "pick the best one".

## 8. Hierarchical (planner → sub-planners → workers)

```
planner(Kimi-K3)
  |-- sub-plan A (taskfile A: 2-8 tasks) --> code run A
  |-- sub-plan B (taskfile B: 2-8 tasks) --> code run B
  +-- sub-plan C ...
```

Recurse the orchestrator: a top-level plan whose results seed further plans.
This repo's planner is single-level by design (2–8 focused tasks), so
hierarchy is expressed as **ordered planning rounds**: land contracts first,
then plan each module against the merged base (the dashboard does this as
separate projects; with a two-branch flow, `ARC_BASE_BRANCH=development`,
`code promote` separates levels).

- **When:** large greenfield systems (> 8 natural tasks), multi-subsystem
  migrations, "build a game/app" goals where each module is itself a plan.
- **Express it:** round 1 = a chain/diamond that lands the shared skeleton
  (interfaces, directory layout, CI). Then one `code plan` per module, each
  with its own taskfile, run in sequence or in parallel across repos.
  Levels > 2 are a smell — flatten or the integration risk grows faster
  than the parallelism wins.
- **Pitfalls:** no cross-taskfile deps exist, so a human (or the operator
  script) sequences the rounds — do not start level 2 before level 1's PRs
  merge. Each sub-planner re-reads the repo, so sub-plans are always against
  current merged code, never against the top planner's stale memory. Cost
  multiplies per level; keep the top level to contracts, not prose.

## 9. Retry + escalate (per-task resilience, built-in)

```
implement → gate ─fail→ implement (fix round, ≤ MAX_FIX_ROUNDS=8)
                └─budget out→ escalate tier up (fresh fix budget,
                    ≤ MAX_ESCALATIONS=2 per run)
                    path (config.ESCALATION_PATH): GLM → Kimi → DeepSeek-4.1 → fail
                    a retired model's task hops in at its remap (RETIRED_MODELS)
```

The saga/circuit-breaker analog, already wired by `code_tasks.build_code_graph`
(Rule 4): bounded local retry, then escalate along `config.ESCALATION_PATH`
(default GLM-5.3 → Kimi-K3 → DeepSeek-V4.1-Flash-thinking-max) with the failure as
feedback, the reviewer re-chosen as the implementer's family changes;
compensation = reallocating the worktree **resets the task branch to base**,
so a rejected attempt never leaks into a retry. A taskfile that still names a
retired model (gpt-oss-120b, DeepSeek-V4-Flash) is remapped onto the path by
`code_tasks.RETIRED_MODELS` rather than rejected. With `MAX_ESCALATIONS` = 2
(one less than the path length) a GLM-planned task can reach the top tier
within a single run; a task planned higher up tops out sooner, and continues
only via the resume path
(re-running a capability-failed row resumes one tier up). Resume escalates
only on capability failures — an interrupted run restarts at the same
tier.

- **When:** automatic for every task; nothing to design. Know it when
  estimating latency: worst case per task is fix_rounds × tiers.
- **Pitfalls:** escalation is predicated on the failure being *the model's
  fault*. A task whose prompt is unknowable ("make it better") will climb
  all the way to Kimi and fail there burning the scarcest capacity — write
  verifiable tasks instead. Gate output is truncated to 2000 chars, so make
  `verify_cmd` print the diagnosis early (or tail-filter it), or every fix
  round starts from a useless feedback blob.

---

## Decision table

| Work type | Recommended pattern(s) | Typical task count |
|---|---|---|
| Greenfield build (multi-module) | orchestrator-workers → fan-out-fan-in (+ diamond seam check) | 4–8 |
| Small feature / endpoint | single task (evaluator-optimizer is free) | 1–2 |
| Repo-wide mechanical change | fan-out-fan-in sliced by directory, all GLM | 3–6 |
| Delicate refactor / architecture | chain of hard-tier links; diamond if a seam is risky | 2–4 |
| Docs | chain (write → link/render check) or one task | 1–3 |
| Bugfix, known location | router→single task; gate = failing repro test now passing | 1–2 |
| Bugfix, unknown location / spike | router two-phase (probe taskfile, then real plan) | 1 + 2–8 |
| Review-heavy / risky change (security, protocol) | diamond or debate-vote with strict gate | 3–5 |
| Research / approach selection | debate-vote on proposals, or fan-out-fan-in with synthesis task | 3–5 |
| Migration | chain staged by layer (inventory → migrate → delete) | 3–5 |
| Very large build (> 8 tasks) | hierarchical: contracts round, then per-module plans | 2–4 + per-module 2–8 |

Sizing notes: totals count implement tasks (plan/merge are free). Every
number above already includes what the gauntlet adds — more tasks is always
more gate/PR rounds, so prefer the narrowest pattern that isolates the real
risk. When torn between two patterns, take the cheaper one; the
evaluator-optimizer loop around every task absorbs small misjudgments for
free.

## Toward more complex graphs

What the engine (`graph.py`) has today, and what a taskfile can say with it.
`graph_shapes._engine` checks each of these against the code at call time
and the dashboard renders the result; keep this list in step with it.

**The engine has:**

- *Conditional edges* — `Edge(when=...)`. The per-task pipeline's
  gate/review/escalate branches are these.
- *Joins* — `Node(gather=True)`. A task with several deps gets one.
- *Dynamic fan-out* — `Spawn`: one node spawning N children decided at
  runtime. The pipeline's `pr_fanout` (one child per PR reviewer) uses it.
- *Retry and timeout per node* — `Retry`, `Node(timeout=)`; the fix loop and
  tier escalation are built on them.
- *Subgraphs* — `Graph.subgraph`: a whole graph as one node of another.
- *Taskfile chaining* — `project.after` → the `chain_wait` gate.

**What a taskfile cannot yet express** (each marked *not implemented*):

- *Conditional deps between tasks.* Every task you write runs; a dep cannot
  say "only if the probe found X". Today: route at plan time, or a second
  taskfile `after` the first (the router pattern). A task-level `when` on a
  dep would map onto `Edge(when=)` reading the upstream task's result — the
  predicate would need a vocabulary (gate output? a JSON verdict file the
  task writes?) and the dashboard would need to draw an edge that may never
  fire. *Not implemented.*
- *Tasks spawning tasks.* A task cannot add tasks to its own taskfile at
  runtime; the planner decides the set once. Hierarchical work is rounds of
  taskfiles. A `"kind": "spawn"` task whose result is a list of task specs
  would map onto `graph.Spawn` with the per-task pipeline as the target —
  the loader would have to validate the spawned specs (Rules 1–2) at
  runtime rather than at load. *Not implemented.*
- *Loops between tasks.* `deps` must be acyclic; the only loops are inside a
  task (gate → fix, review → fix) with a round budget. A bounded
  evaluator loop between two tasks (build ↔ audit) would be an `Edge` from
  the audit task's `pr_merge` back to the build task's `alloc` with a
  budget like `MAX_FIX_ROUNDS` — and a branch policy for the re-run (reset
  to base, or continue on the merged result). *Not implemented.*

The order to add them, if wanted: conditional deps first (smallest change,
unlocks a one-file router), then task-level loops (reuses the fix-loop
budget), then spawning (needs runtime validation).

## Sources

- Anthropic — "Building effective agents" (workflows vs. agents; prompt
  chaining; routing; parallelization: sectioning vs. voting;
  orchestrator-workers; evaluator-optimizer):
  https://www.anthropic.com/engineering/building-effective-agents
- LangGraph — "Workflows and agents" (same five patterns as graph
  primitives; Send API dynamic fan-out / map-reduce):
  https://docs.langchain.com/oss/python/langgraph/workflows-agents
- OpenAI Agents SDK — "Agent orchestration" (orchestration via code vs. via
  LLM; agents-as-tools/manager; handoffs; chaining; evaluator loop):
  https://openai.github.io/openai-agents-python/multi_agent/
- OpenAI Cookbook — "Orchestrating Agents: Routines and Handoffs" (routine /
  triage-handoff model behind the practical guide's decentralized pattern):
  https://cookbook.openai.com/examples/orchestrating_agents
- Microsoft Research — "Magentic-One: a generalist multi-agent system"
  (orchestrator with task & progress ledgers; stall → replan; specialist
  worker agents; irreversibility risks):
  https://www.microsoft.com/en-us/research/blog/magentic-one-a-generalist-multi-agent-system-for-solving-complex-tasks/

(Attempted, not citable: Google's "Agents companion" whitepaper on Kaggle is
CAPTCHA-walled to fetchers — OpenAI's "A practical guide to building agents"
PDF exceeds fetch size limits; the two OpenAI web sources above cover the
same pattern vocabulary.)

## Applied audit: the code-tasks pipeline against these principles (2026-09-11)

The patterns above are a menu. This section is the inventory — what the
pipeline that actually runs does and does not do, measured from the event log
rather than asserted. Re-run the numbers before trusting them: they are a
snapshot.

### The Wait Test, edge by edge

*Walk the graph and at every edge ask: does this step need the RESULT of the
one before it? If yes, serial is correct. If no, the edge is a wait for
nothing and the steps should run at once.*

| edge | verdict | why, with the number that decides it |
|---|---|---|
| `alloc -> implement` | serial | needs the worktree path |
| `implement -> gate` | serial | needs the written code |
| `gate -> review` | serial | the gate fails **18%** of the time; running the 7-minute review alongside it would waste a reviewer that often to save ~14 s |
| `review -> publish` | serial | the pre-merge review rejects **39%** of implementations that passed the gate — publish must wait for that verdict. *This edge looked redundant (pr_review re-reads the diff from scratch) and the data says it is not.* |
| `publish -> pr_fanout` | serial | reviewers need the PR number and the pushed diff |
| `pr_fanout -> N × pr_reviewer` | **parallel** | reviewers are independent; each is a `Spawn`'d node (`graph.Spawn`), not a coroutine inside one node |
| `pr_reviewer -> pr_review` | **join** | the verdict needs every reviewer; the join fires once with the list |
| `pr_review -> pr_merge` | serial | merge needs unanimous approval |
| independent tasks | **parallel** | every task without `deps` is a graph head and starts at once |
| `deps: [a, b] -> c` | **join** | was wired as `deps[-1]` only — a race dressed as a dependency; now a `gather=True` node over every dep |

Nothing in the serial column is a wait for nothing. The cost of the pipeline is
in the model calls, not in the edges between them.

### Per-node reliability (the "every node must ship on its own" rule)

| node | runs | errors | reliability | median |
|---|---|---|---|---|
| alloc | 145 | 4 | 97% | 0 s |
| implement | 256 | 3 | 99% | 4 min |
| gate | 255 | 0 | 100% | 14 s |
| review | 186 | 0 | 100% | 7 min |
| publish | 159 | 0 | 100% | 0 s |
| **pr_review** | 59 | **9** | **87%** | 14 min |
| pr_merge | 52 | 0 | 100% | 4 s |

The one weak node is the one that fanned out inside itself: its nine errors are
reviewer crashes that took the whole node down. Per-node `Retry` on
`pr_reviewer` now absorbs those before the join ever sees them.

### Primitives the engine has, and what each one is called elsewhere

| here | LangGraph | Temporal / Prefect | status |
|---|---|---|---|
| multiple edges from one node | fan-out | — | ✅ |
| `gather=True` | list-form edges / `defer` | fan-in | ✅ (now used by `deps`) |
| `Spawn(target, items, join)` | `Send()` | `.map()` | ✅ (reviewers) |
| `when=` | conditional edge | branch | ✅ |
| back-edges, self-edges | loops + recursion limit | — | ✅ (`max_steps`) |
| `Retry(attempts, backoff, on)` | `RetryPolicy` | `RetryOptions` | ✅ per node |
| `timeout=` | node `timeout` | activity timeout | ✅ per node |
| `on_error="handler"` | error handler node | compensation | ✅ |
| `g.subgraph(name, inner)` | subgraph | child workflow | ✅ |
| `on_drain=True` | — | graceful cancellation | ✅ (no equivalent in LangGraph) |
| `Persist` + `_seed_from_store` | checkpointer | durable history | ✅ |
| human-in-the-loop interrupt | `interrupt()` | signal / update | ❌ — the promotion PR is the only human gate, and it is outside the graph |

### Principles from the graph-engineering material, checked

- **Narrow node** (one job, only the tools it needs) — holds. Reviewers cannot
  write; implementers do not review; the gate is a shell command.
- **Separable work** (only graph what genuinely splits) — holds at the task
  level; the planner is told to decompose by tier and by file ownership.
- **Deterministic edges for mandatory steps** (hooks, not model choice) — holds.
  `gate` always runs; `publish` always syncs; no transition is left to a model.
- **Over-spawn caution** — `PR_REVIEWERS` bounds the fan-out; the pool is the
  eligible cross-family set, not "every model".
- **Every node must ship on its own** — measured above; one node was below
  95% and has been given its own retry.
