# Model Tiers (tier routing reference)

The multi-model code fleet is tiered by task difficulty and role. Every
routing decision below is made at plan time — by
DeepSeek-V4.1-Flash-thinking-max in `main.py code plan`
(`config.PLANNER_MODEL`), or by whoever writes a task file by hand — and is
enforced again by `code_tasks.load_taskfile`, which remaps retired model
names and rejects anything else that violates it. There is no runtime
triage.

## Model fleet

| Model | Harness | Tier | Allowed roles | Per-account API cap | Driver semaphore cap |
|---|---|---|---|---|---|
| GLM-5.3 | `opencode` (`OpencodeDriver`) | medium | Implement, Plan, Review, PR-review | 4 | 2 |
| Kimi-K3 | `kimi` CLI (`KimiDriver`) | hard | Implement, Plan, Review, PR-review | 3 | 3 |
| DeepSeek-V4.1-Flash-thinking-max | `opencode` (`OpencodeDriver`) | hard | Implement, Plan, Review, PR-review | 10 | 5 |

- **Per-account API cap** — `config.FAMILIES[*].limit` (`glm` 4,
  `kimi` 3, `deepseek` 10). The deepseek 10 is **provider-published**
  (ARC docs, 2026-09-12), not a ramped measurement like the retired
  fleet's figures — re-measure it if observed rejections disagree.
  These are per-account ARC limits, not per-process; overridable per
  family via `ARC_LIMIT_<FAMILY>`.
- **Driver semaphore cap** — `config._MODEL_DRIVER_CAP`, the max concurrent
  harness instances per model via `drivers._gate` → `config.driver_limit`.
  It is the account cap divided by sessions-per-process (an opencode run
  holds about two ARC sessions at once; the kimi CLI holds one), so 10 ÷ 2 = 5
  and 4 ÷ 2 = 2. `ARC_DRIVER_HEADROOM` subtracts further, and batch callers
  on the planner model see one slot fewer (`INTERACTIVE_RESERVE`) so an
  interactive chat always has somewhere to land.
- Kimi-K3 runs in the `kimi` CLI; GLM-5.3 and
  DeepSeek-V4.1-Flash-thinking-max run in `opencode` via `OpencodeDriver`.
  The driver caps apply per model.
- Kimi-K3 is scheduled for withdrawal on **2026-09-19** (see the roster
  section below for what changes that morning).

## Tier definitions (loader-enforced)

Tiers come from `config.IMPLEMENT_TIERS`. A model implements only the tier
it is assigned to (an implementer model in a task file must be in
`config.IMPLEMENTER_MODELS`; the plan must route it to a suitable tier).
A task file naming a **retired** model still loads: `code_tasks.RETIRED_MODELS`
remaps the stale name onto today's escalation path (e.g. `DeepSeek-V4-Flash`,
removed from the provider's API on 2026-09-12, remaps to the live DeepSeek)
rather than rejecting a decomposition that is still good.

| Tier | Work | Model |
|---|---|---|
| medium | moderate AND very basic/mechanical implementation | GLM-5.3 |
| hard | complex / multi-file / architectural | Kimi-K3 or DeepSeek-V4.1-Flash-thinking-max |

There is no basic tier: with gpt-oss-120b retired (2026-09-11) the medium
tier is the floor and GLM-5.3 absorbs the mechanical work. Within hard,
DeepSeek-V4.1-Flash-thinking-max takes the hardest tasks — it is the fleet's
strongest model (operator decision, 2026-09-12).

Examples of medium: boilerplate, renames, simple utilities, config edits,
single-file features, straightforward wiring, docs (markdown reference files
like this one). Hard is anything that needs multi-file reasoning, delicate
design, or architectural judgment.

## Planning and review

- **All three live models may plan and review.** In the task file schema the
  reviewer field names a review *family* — `kimi`, `glm`, or `deepseek`
  (`config.REVIEW_FAMILIES`, which maps each token to the live model that
  reviews for it); `code_tasks.load_taskfile` rejects any other value. A
  family token that has left the roster (as `kimi` will on 2026-09-19) is
  remapped to the strongest cross-family reviewer at load time, not rejected.
- **Role eligibility is roster-driven.** `config.MODEL_ROLES` (derived from
  `config.ROSTER`) says which of implementer / planner / reviewer /
  pr_reviewer each model may hold; `config.model_may` answers the question,
  and the driver constructors enforce it — `KimiDriver` and `OpencodeDriver`
  raise `ValueError` when constructed with a role the model's roster row does
  not allow. There is no per-model if-chain.
- DeepSeek-V4.1-Flash-thinking-max is the main orchestrator/planner:
  `main.py code plan` invokes `plan_tasks`, which runs
  `OpencodeDriver(config.PLANNER_MODEL, "planner")`.

## Cross-review matrix

Implementer → required reviewer. Cross-review is **family**-based: a task's
reviewer must come from a model family other than the implementer's own
(`config.cross_family_reviewer` picks the strongest review-capable family
that qualifies). With today's roster that resolves to:

| Implementer | Required reviewer (family → model) |
|---|---|
| GLM-5.3 | `deepseek` → DeepSeek-V4.1-Flash-thinking-max |
| Kimi-K3 | `deepseek` → DeepSeek-V4.1-Flash-thinking-max |
| DeepSeek-V4.1-Flash-thinking-max | `kimi` → Kimi-K3 |

`code_tasks.load_taskfile` raises `ValueError` if a task's reviewer family is
the implementer's own family. PR review (`pr_reviewer`) works the same way
but chooses `config.PR_REVIEWERS` (default 2) reviewers from families other
than the implementer's, differing from each other, and requires unanimous
approval — with three review families the fleet can always staff two
independent readings.

## How to pick a tier

- **Start at the lowest tier** that plausibly fits; escalate only when
  evidence says the task is bigger than the tier.
- Escalate one tier when the task **touches many files**, needs **design
  judgment** (an API shape, a schema, a cross-cutting refactor), or has
  **failed review once**.
- Do not assign a hard task to a medium model; it will fail review and
  burn fix rounds. Do not over-assign a trivial task to a hard model — it
  wastes the fleet and the accounts.
- A planner re-plans (or a hand-authored task file is edited) whenever a task
  crosses a tier boundary; routing is fixed at load time.

## Cost attribution

The dashboard prices each model run from per-million-token rates in
`config.MODEL_PRICING` (USD). Prompt and completion are priced separately
`config.cost_of(model, prompt_tokens, completion_tokens)`; a model with no
entry prices at 0.0 rather than inventing a rate. Because the telemetry does
not distinguish a cached prompt read from a fresh one, and because
no-split/live tokens are priced at the completion rate, any figure is an
upper bound, not an exact charge.

Rates are overridable per model via `ARC_PRICE_<MODEL>_PROMPT` and
`ARC_PRICE_<MODEL>_COMPLETION`, where `<MODEL>` is the model name uppercased
with non-alphanumerics turned to `_` — e.g. `ARC_PRICE_KIMI_K3_PROMPT`. A
partial override replaces only the half it names, falling back to the table
for the other; a junk override value is ignored rather than crashing.

## Cross-references

- [../AGENTS.md](../AGENTS.md)
- [orchestration-contract.md](orchestration-contract.md)
- [concurrency-limits.md](concurrency-limits.md)
- [taskfile-schema.md](taskfile-schema.md)
- [runbook.md](runbook.md)

## The roster is dated (`config.ROSTER`)

Models arrive and leave on dates the provider sets. Every model constant in
`config` — `IMPLEMENTER_MODELS`, `IMPLEMENT_TIERS`, `MODEL_FAMILY`,
`MODEL_HARNESS`, `MODEL_ROLES`, `REVIEW_FAMILIES`, `PLANNER_MODEL`, the
default `ESCALATION_PATH`, the measured concurrency caps — is derived from the
rows of `ROSTER` that are live **today**, so a transition is a date in one
table, not an edit in six places on the morning it happens.

| model | tier | roles | live |
|---|---|---|---|
| DeepSeek-V4-Flash | medium | implement, PR-review | until 2026-09-12 |
| GLM-5.3 | medium | all | — |
| Kimi-K3 | hard | all | until 2026-09-19 |
| DeepSeek-V4.1-Flash-thinking-max | hard | all | from 2026-09-12 |

gpt-oss-120b was retired on 2026-09-11 (operator decision). There is no
"basic" tier: mechanical work routes to the medium tier.

DeepSeek-V4-Flash was retired on 2026-09-12 because the provider removed it
from the API — a request today returns "Model not found". The replacement
line is DeepSeek-V4.1-Flash; the code fleet runs its thinking-max variant,
which the roster row above names (operator decision 2026-09-12: it is the
fleet's strongest model, the planner, and the last escalation stage). The
research/build workloads address the family through
`config.FAMILIES["deepseek"]`: DeepSeek-V4.1-Flash (base),
DeepSeek-V4.1-Flash-thinking-low, DeepSeek-V4.1-Flash-thinking-max, and the
websearch name DeepSeek-V4.1-Flash-thinking-max-legacy-tool-calling.

**Preview any date** with `ARC_ROSTER_DATE=YYYY-MM-DD` — the whole test suite
is run under both transition dates before they arrive:

```bash
ARC_ROSTER_DATE=2026-09-19 ./py -m unittest discover -s tests -t tests
```

What changes on **2026-09-19** when Kimi-K3 leaves: the fleet is GLM-5.3 +
DeepSeek-V4.1-Flash-thinking-max, the escalation path shrinks to two tiers
(`MAX_ESCALATIONS` becomes 1), the planner stays
DeepSeek-V4.1-Flash-thinking-max, DS-max's own work is now reviewed by glm
(GLM's reviewer stays deepseek — the glm ↔ deepseek pairing is the only
cross-family review left, which is why the 4.1 row carries the full role
set: a fleet with one reviewable family has no cross-review at all), the PR
gate drops to one reviewer (`PR_REVIEWERS` is capped at families − 1), and
`KimiDriver` refuses to construct. Taskfiles that still say
`"reviewer": "kimi"` are remapped to the strongest cross-family reviewer at
load time rather than rejected; their decomposition is still good.

Prices live in `config.MODEL_PRICING` and can be overridden per model with
`ARC_PRICE_<MODEL>_PROMPT` / `ARC_PRICE_<MODEL>_COMPLETION` (USD per million
tokens; the key is the model name upper-cased with non-alphanumerics as `_`,
e.g. `ARC_PRICE_DEEPSEEK_V4_1_FLASH_PROMPT` and `ARC_PRICE_DEEPSEEK_V4_1_FLASH_COMPLETION`). DeepSeek-V4.1-Flash is priced the
same as V4 until the provider publishes a rate; gpt-oss-120b stays in the table
because the event log prices history, not just today's roster.
