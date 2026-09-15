# Model Tiers (tier routing reference)

The multi-model code fleet is tiered by task difficulty and role. Every
routing decision below is made at plan time — by
GLM-5.3 in `main.py code plan`
(`config.PLANNER_MODEL`), or by whoever writes a task file by hand — and is
enforced again by `code_tasks.load_taskfile`, which remaps retired model
names and rejects anything else that violates it. There is no runtime
triage.

## Model fleet

Two models, two harnesses (operator decision 2026-09-12):

| Model | Harness | Tier | Allowed roles | Per-account API cap | Driver semaphore cap |
|---|---|---|---|---|---|
| GLM-5.3 | `opencode` (`OpencodeDriver`) | hard | Implement, Plan, Review, PR-review | 4 | 2 |
| DeepSeek-V4.1-Flash-thinking-max | `reasonix` (`ReasonixDriver`) | medium | Implement, Review, PR-review | 10 | 5 |

**GLM-5.3 is the fleet's strongest model** — hard tier, the planner, the last
escalation stage. **DeepSeek-V4.1-Flash-thinking-max (DS-max) is the
medium-tier workhorse**: much faster, carries the implementation load,
reviews, and **never plans**.

- **Per-account API cap** — `config.FAMILIES[*].limit` (`glm` 3,
  `deepseek` 10). GLM's 3 is the ceiling the backend itself reported on
  2026-09-14 (`max 3 in flight per user`, seen with zero fleet drivers alive;
  it revises the historical measured 4); the
  deepseek 10 is **provider-published**
  (ARC docs, 2026-09-12), not a ramped measurement like the retired
  fleet's figures — re-measure it if observed rejections disagree.
  These are per-account ARC limits, not per-process; overridable per
  family via `ARC_LIMIT_<FAMILY>`.
- **Driver semaphore cap** — `config._MODEL_DRIVER_CAP`, the max concurrent
  harness instances per model via `drivers._gate` → `config.driver_limit`.
  It is the account cap divided by sessions-per-process (both harnesses hold
  about two ARC sessions at once: `config._SESSIONS_PER_PROCESS` is
  `{"opencode": 2, "reasonix": 2}`), so 10 ÷ 2 = 5
  and 3 ÷ 2 = 1 (GLM's account cap 4 → 3 on 2026-09-14 per the
  backend's own rejection text, above). `ARC_DRIVER_HEADROOM` subtracts further;
  batch callers on the
  planner model would see one slot fewer (`INTERACTIVE_RESERVE`), but
  `_apply_reserve` skips the reserve while it would leave batch under
  `MIN_BATCH_SLOTS` = 2 — and GLM-5.3's cap **is** 1, so no reserve is applied
  there (an interactive chat queues instead; at one slot there is nothing
  left to cut).
- GLM-5.3 runs in `opencode`; DeepSeek-V4.1-Flash-thinking-max runs in
  **`reasonix`** (`drivers.ReasonixDriver`, binary via `config.reasonix_bin()`).
  The driver caps apply per model; the **harness** pools are separate —
  5 opencode, 7 reasonix (`config._HARNESS_CAP`) — so neither model can
  starve the other's binary. reasonix 7 was measured 2026-09-14: 3/3, 6/6
  and 7/7 concurrent one-shot runs all exited 0 cleanly (opencode's
  equivalent cliff was at 6).
- **Kimi-K3 was retired from this repo on 2026-09-12** by operator decision:
  its ROSTER row was deleted, it is gone from `config.FAMILIES` and from every
  live role, and no table above can name it as current. `drivers.KimiDriver`
  still exists so historical transcripts can be re-read; it refuses to
  construct for a model off today's roster.

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
| medium | moderate AND very basic/mechanical implementation | DeepSeek-V4.1-Flash-thinking-max |
| hard | complex / multi-file / architectural | GLM-5.3 |

There is no basic tier: with gpt-oss-120b retired (2026-09-11) the medium
tier is the floor. Within the fleet,
GLM-5.3 takes the hardest tasks — it is the fleet's
strongest model (operator decision, 2026-09-12), and there is no tier above
it to escalate to.

Examples of medium: boilerplate, renames, simple utilities, config edits,
single-file features, straightforward wiring, docs (markdown reference files
like this one). Hard is anything that needs multi-file reasoning, delicate
design, or architectural judgment.

## Planning and review

- **Both live models may review; only GLM-5.3 may plan.** In the task file
  schema the
  reviewer field names a review *family* — `glm`, or `deepseek`
  (`config.REVIEW_FAMILIES`, which maps each token to the live model that
  reviews for it); `code_tasks.load_taskfile` rejects any other value. A
  family token that has left the roster (e.g. `kimi`, retired 2026-09-12) is
  remapped to the strongest cross-family reviewer at load time, not rejected.
- **Role eligibility is roster-driven.** `config.MODEL_ROLES` (derived from
  `config.ROSTER`) says which of implementer / planner / reviewer /
  pr_reviewer each model may hold; `config.model_may` answers the question,
  and the driver constructors enforce it — `OpencodeDriver` and
  `DeepseekDriver`
  raise `ValueError` when constructed with a role the model's roster row does
  not allow. There is no per-model if-chain; DeepSeek's row simply has no
  `planner`.
- GLM-5.3 is the main orchestrator/planner:
  `main.py code plan` invokes `plan_tasks`, which runs
  `OpencodeDriver(config.PLANNER_MODEL, "planner")`. GLM-5.3 planning is slow
  on big goals — fine: total budgets are unlimited by default and the planner
  idle budget is 3000 s. See [runbook.md](runbook.md) § "Planning a large
  goal".

## Cross-review matrix

Implementer → required reviewer. Cross-review is **family**-based: a task's
reviewer must come from a model family other than the implementer's own
(`config.cross_family_reviewer` picks the strongest review-capable family
that qualifies). With today's roster that resolves to:

| Implementer | Required reviewer (family → model) |
|---|---|
| GLM-5.3 | `deepseek` → DeepSeek-V4.1-Flash-thinking-max |
| DeepSeek-V4.1-Flash-thinking-max | `glm` → GLM-5.3 |

`code_tasks.load_taskfile` raises `ValueError` if a task's reviewer family is
the implementer's own family. With two families the pairing is exact and
there is no choice to make.

PR review (`pr_reviewer`) works the same way, but the fleet has only ONE
family other than the implementer's, so exactly one cross-family reviewer
reads each PR. `config.PR_REVIEWERS_WANTED` is still 2 and
`config.PR_REVIEWERS` resolves to `max(1, min(wanted, families - 1))` = 1;
the miss is not silent — `pr_review` emits
`task.pr_review_thin {task, pr, wanted, got, reviewers, implementer}`. The
pre-merge review above is the compensating control: the PR review is the
second read of a change that already passed a cross-family gate.

**History:** a temporary `ARC_*` same-family-review override
(operator-authorized 2026-09-12, GLM-5.3's provider backend unstable)
suspended the cross-family half of both rules; it was removed 2026-09-14
once GLM-5.3 stabilised. A
same-family review is a second pass by the same model, not an independent
reading, and is rejected again.

## How to pick a tier

- **Start at the lowest tier** that plausibly fits; escalate only when
  evidence says the task is bigger than the tier.
- Escalate one tier when the task **touches many files**, needs **design
  judgment** (an API shape, a schema, a cross-cutting refactor), or has
  **failed review once**.
- Do not assign a hard task to the medium model; it will fail review and
  burn fix rounds. Do not over-assign a trivial task to GLM-5.3 — it
  wastes the scarcest model and the planner's own capacity.
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
with non-alphanumerics turned to `_` — e.g. `ARC_PRICE_DEEPSEEK_V4_1_FLASH_PROMPT`. The
same knob still prices retired models in history (`ARC_PRICE_KIMI_K3_PROMPT`
for old Kimi-K3 rows), because the event log prices the past, not just today's
roster. A
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

| model | harness | tier | roles | live |
|---|---|---|---|---|
| DeepSeek-V4-Flash | opencode | medium | implement, PR-review | until 2026-09-12 |
| Kimi-K3 | kimi | hard | all | retired 2026-09-12 (row deleted) |
| DeepSeek-V4.1-Flash-thinking-max | reasonix (dsh 2026-09-12..13) | medium | implement, review, PR-review | from 2026-09-12 |
| GLM-5.3 | opencode | hard | all | — |

gpt-oss-120b was retired on 2026-09-11 (operator decision). There is no
"basic" tier: mechanical work routes to the medium tier.

DeepSeek-V4-Flash was retired on 2026-09-12 because the provider removed it
from the API — a request today returns "Model not found". The replacement
line is DeepSeek-V4.1-Flash; the code fleet runs its thinking-max variant,
which the roster row above names (operator decision 2026-09-12). The
research/build workloads address the family through
`config.FAMILIES["deepseek"]`: DeepSeek-V4.1-Flash (base),
DeepSeek-V4.1-Flash-thinking-low, DeepSeek-V4.1-Flash-thinking-max, and the
websearch name DeepSeek-V4.1-Flash-thinking-max-legacy-tool-calling.

**Kimi-K3's retirement (2026-09-12, operator decision).** Its ROSTER row was
deleted outright — the scheduled 2026-09-19 provider withdrawal was not waited
for — so it is gone from `config.FAMILIES`, from `MODEL_ROLES`, from
`REVIEW_FAMILIES`, and from every live table in these docs. Historical
`harness_runs` rows still price through `MODEL_PRICING`, and `KimiDriver`
still refuses to construct loudly for old transcripts. A taskfile that still
names it loads: `code_tasks.RETIRED_MODELS` remaps `Kimi-K3` onto the
strongest live tier (GLM-5.3), and a taskfile naming a retired reviewer family
is remapped by `config.cross_family_reviewer`.

**Preview any date** with `ARC_ROSTER_DATE=YYYY-MM-DD` — the whole test suite
is run under both transition dates before they arrive:

```bash
ARC_ROSTER_DATE=2026-09-19 ./py -m unittest discover -s tests -t tests
```

Prices live in `config.MODEL_PRICING` and can be overridden per model with
`ARC_PRICE_<MODEL>_PROMPT` / `ARC_PRICE_<MODEL>_COMPLETION` (USD per million
tokens; the key is the model name upper-cased with non-alphanumerics as `_`,
e.g. `ARC_PRICE_DEEPSEEK_V4_1_FLASH_PROMPT` and `ARC_PRICE_DEEPSEEK_V4_1_FLASH_COMPLETION`). DeepSeek-V4.1-Flash is priced the
same as V4 until the provider publishes a rate; gpt-oss-120b and Kimi-K3 stay
in the table
because the event log prices history, not just today's roster.
