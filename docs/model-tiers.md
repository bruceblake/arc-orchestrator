# Model Tiers (tier routing reference)

The multi-model code fleet is tiered by task difficulty and role. Every
routing decision below is made at plan time — by Kimi-K3 in
`main.py code plan`, or by whoever writes a task file by hand — and is
enforced again by `code_tasks.load_taskfile`, which rejects a task file
that violates it. There is no runtime triage.

## Model fleet

| Model | Harness | Tier | Allowed roles | Per-account API cap | Driver semaphore cap |
|---|---|---|---|---|---|
| Kimi-K3 | `kimi` CLI (`KimiDriver`) | hard | Implement, Plan, Review, PR-review | 3 | 3 |
| GLM-5.3 | `opencode` (`OpencodeDriver`) | hard | Implement, Plan, Review, PR-review | 4 | 4 |
| gpt-oss-120b | `opencode` (`OpencodeDriver`) | basic | Implement only | 10 | 5 |
| DeepSeek-V4-Flash | `opencode` (`OpencodeDriver`) | medium | Implement, PR-review | 10 | 5 |

- **Per-account API cap** — `config.FAMILIES[*].limit` (`gpt-oss` 10,
  `glm` 4, `kimi` 3, `deepseek` 10). These are per-account ARC limits, not
  per-process; overridable per family via `ARC_LIMIT_<FAMILY>`.
- **Driver semaphore cap** — `config._MODEL_DRIVER_CAP`, the max concurrent
  harness instances per model via `drivers._gate` → `config.driver_limit`.
  Kept below the account cap to reserve headroom for interactive use.
- Kimi-K3 runs in the `kimi` CLI; GLM-5.3, gpt-oss-120b, and DeepSeek-V4-Flash
  run in `opencode` via `OpencodeDriver`. The driver caps apply per model.

## Tier definitions (loader-enforced)

Tiers come from `config.IMPLEMENT_TIERS`. A model implements only the tier
it is assigned to (an implementer model in a task file must be in
`config.IMPLEMENTER_MODELS`; the plan must route it to a suitable tier).

| Tier | Work | Model |
|---|---|---|
| basic | very basic implementation ONLY | gpt-oss-120b |
| medium | moderate implementation ONLY | DeepSeek-V4-Flash |
| hard | complex / multi-file / architectural | GLM-5.3 or Kimi-K3 |

Examples of basic: boilerplate, renames, simple utilities, config edits.
Examples of medium: single-file features, straightforward wiring, docs
(markdown reference files like this one). Hard is anything that needs
multi-file reasoning, delicate design, or architectural judgment.

## Planning and review

- **Only GLM-5.3 and Kimi-K3 may plan or review.** In the task file schema
  the reviewer field is `kimi` or `glm`; `code_tasks.load_taskfile` rejects
  any other value.
- gpt-oss-120b and DeepSeek-V4-Flash **implement only**. `OpencodeDriver`
  raises if these models are given a `planner` or `reviewer` role, and their
  output **always** requires review by GLM-5.3 or Kimi-K3 before merge.
- Kimi-K3 is the main orchestrator/planner: `main.py code plan` invokes
  `plan_tasks`, which runs `KimiDriver("planner")`.

## Cross-review matrix

Implementer → required / allowed reviewer. A task's reviewer never shares a
harness with its implementer (i.e. no same-harness self-review).

| Implementer | Required reviewer |
|---|---|
| Kimi-K3 | GLM-5.3 |
| GLM-5.3 | Kimi-K3 |
| gpt-oss-120b | GLM-5.3 or Kimi-K3 |
| DeepSeek-V4-Flash | GLM-5.3 or Kimi-K3 |

`code_tasks.load_taskfile` raises `ValueError` if a task sets `reviewer` to
the same harness family as the implementer (`kimi` ↔ `kimi`, `glm` ↔ `glm`),
so Kimi-K3 work must be reviewed by glm and GLM-5.3 work by kimi. Work by
gpt-oss/DeepSeek may be reviewed by either. Split reviews between kimi and
glm so neither idles nor saturates.

## How to pick a tier

- **Start at the lowest tier** that plausibly fits; escalate only when
  evidence says the task is bigger than the tier.
- Escalate one tier when the task **touches many files**, needs **design
  judgment** (an API shape, a schema, a cross-cutting refactor), or has
  **failed review once**.
- Do not assign a hard task to a basic/medium model; it will fail review and
  burn fix rounds. Do not over-assign a trivial task to a hard model — it
  wastes the fleet and the accounts.
- A planner re-plans (or a hand-authored task file is edited) whenever a task
  crosses a tier boundary; routing is fixed at load time.

## Cross-references

- [../AGENTS.md](../AGENTS.md)
- [orchestration-contract.md](orchestration-contract.md)
- [concurrency-limits.md](concurrency-limits.md)
- [taskfile-schema.md](taskfile-schema.md)
- [runbook.md](runbook.md)
