# Dream-RSI: improving how a run explores, offline

`dream_rsi.py` implements the mechanism of **Dream-RSI** (*Recursive
Self-Improvement through Evolving Worlds*, arXiv:2609.14858) as an offline
learner for the orchestrator's own decision-making. It answers a narrow
question with recorded data and **no model calls and no git**:

> Given the runs this fleet has already done, which *exploration policy* —
> the rule for which task or fix-round to open next and when to stop — would
> have scored best on that same history?

The answer never runs code online by itself. `main.py code dream` scores the
built-in policies plus any candidate it is handed, prints the replay score of
each, and records the argmax. Only a **scored** policy can be selected
(paper §5.1: reuse history as a *scored simulator*, never as prose guidance —
prose guidance measurably hurts).

## The two graphs, and why this is a separate one

[AGENTS.md §1](../AGENTS.md) already names two graphs: the static per-task
pipeline, and the per-project graph the planner designs. Dream-RSI works on a
**third, observed** graph — the *discovery tree* a run actually produced —
reconstructed after the fact from the rows the orchestrator already writes.

## The discovery tree (a replay world)

`build_tree(name, task_rows, run_rows, deps_by_task=...)` assembles one tree
per recorded taskfile from `store.code_tasks` + `store.harness_runs`:

- **A node is one generation–evaluation attempt.** In this repo that is every
  harness run sharing one `(task, attempt)` cell — the implementer run, its
  pre-merge `reviewer` run, and any `pr-reviewer` runs — folded into a single
  node whose *score* is one scalar.
- **A branch is a task.** Within a task, attempts form a chain (`a-x2` hangs
  off `a-x1`: a fix round continues the previous attempt).
- **Dependencies place branches.** A task that declares `deps` hangs off its
  last dependency's best recorded attempt (`deps_by_task`, read from the
  taskfile on disk when it still exists); without a recorded dep it is a root
  child. Tasks are topologically ordered first, because the store returns rows
  newest-first, so a dependency is always *seen after* its dependent.
- **An unrun task is a node with no completion credit.** A task with no runs
  becomes one attempt node scored with `exit_code=None`, which earns no
  "harness ran" bonus — silence is not success.

### Scoring one attempt

`attempt_score(exit_code, role, verdict, task_status, escalations=0)` is the
explicit, replaceable derivation of the paper's per-attempt `s_v`, bounded to
roughly `[-1.5, 3]` (an escalation penalty is unbounded below) so `β1` stays
interpretable:

| signal | effect |
|---|---|
| harness ran to completion (`exit_code == 0`) | `+0.5` |
| review verdict passed | `+1.0` |
| review verdict failed | `-0.1` per real issue (capped at `-1.0`) |
| task `merged` | `+1.5` |
| task `failed` | `-0.5` |
| each escalation taken | `-0.5` |

A verdict is read in **both** shapes the repo records: the pre-merge reviewer
writes `{"pass": bool, "issues": [...]}` (`code_tasks._parse_verdict`) and the
PR reviewer writes `{"approve": bool, "issues": [...]}`
(`code_tasks._parse_approval`). Several review verdicts of one attempt fold
into one (`_combine_verdicts`): it passes only if every parseable verdict
passed, and its issues are the union.

## The replay simulator and the objective

`replay(tree, policy, W, max_rounds)` evaluates one policy on one recorded
tree. Revealing a selected node deterministically returns its **recorded**
children (paper §3) — replay generates no new candidates, so it costs only
CPU. The rollout ends on an empty batch, the round limit, or a full reveal.

The score is Eq.1, `V = Q - β1·N + β2·(N / max(1, k))`:

| symbol | meaning |
|---|---|
| `Q` | best revealed node score (quality) |
| `N` | revealed attempts (the cost the policy paid) |
| `k` | rounds used |
| `β1` | cost per attempt — `config.DREAM_BETA1` |
| `β2` | parallelism bonus weight — `config.DREAM_BETA2` |

## Exploration policies

`ExplorationPolicy` is the paper's shared decision interface. `choose(tree,
eligible, W)` sees ONLY the revealed prefix (prefix-observable) and returns a
batch of at most `W` eligible node ids; an empty batch stops the rollout.
Built-ins (`BUILTIN_POLICIES`): `RecursiveFixed`, `GreedyBest`, `BreadthBatch`,
`AdaptiveEffort`.

`score_policies(trees, policies, ...)` evaluates exactly the policies it is
given across every tree and returns an `ImprovementResult` whose `.selected`
is the **argmax** policy name; `improve(...)` prepends the built-in incumbent
to the candidate list and delegates to it. Because the incumbent is always a
candidate, selection is monotone non-worsening on the fixed history (paper
§3: `V* ≥ V⁰`).

## The policy-development ("dreaming") agent hook

`propose_source(agent, context)` lets an LLM author a *candidate* policy as
Python source; `compile_policy(source)` compiles it into an `ExplorationPolicy`
under a **restricted** namespace (`_SAFE_BUILTINS` — no imports, no file I/O,
no subprocess) and returns `None` on any failure, so a bad proposal is dropped,
never fatal. Agent-authored source is **never executed online**: it only
competes in `score_policies`' argmax like every other candidate.

## Commands

Score the built-ins over every recorded run (no models, no git):

```bash
.venv/bin/python main.py code dream
```

Add a candidate policy from a file, widen the batch, and cap the rounds:

```bash
.venv/bin/python main.py code dream --taskfile myproj.json --workers 6 --rounds 32 --policy /tmp/cand.py
```

Print the result as JSON (for a dashboard or a script):

```bash
.venv/bin/python main.py code dream --json
```

The run writes a report under `logs/replay/<stamp>.jsonl` and emits a
`dream.completed` event (Rule 7).

## Environment knobs

| var | default | meaning |
|---|---|---|
| `ARC_DREAM_BETA1` | `0.06` | Eq.1 cost per revealed attempt (`β1`) |
| `ARC_DREAM_BETA2` | `0.5` | Eq.1 parallelism-bonus weight (`β2`) |
| `ARC_DREAM_WORKERS` | `0` | replay batch width `W`; `0` derives it from the live in-flight cap (`config.max_tasks_in_flight()`) |
| `ARC_DREAM_MAX_ROUNDS` | `24` | replay round limit `K2` (the `--rounds` default) |

## Sources

- Dream-RSI — *Recursive Self-Improvement through Evolving Worlds*,
  arXiv:2609.14858.
- [docs/graph-patterns.md](graph-patterns.md) — the graph between tasks, which
  the discovery tree is reconstructed from.
- [docs/orchestration-contract.md](orchestration-contract.md) — the per-task
  pipeline whose `code_tasks` / `harness_runs` rows are the replay world.
