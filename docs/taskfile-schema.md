# Taskfile Reference (`~/tasks/*.json`)

A **taskfile** is a JSON document describing a multi-task coding project for
the code workload in `code_tasks.py`: a set of tasks, each implemented by an
AI agent in its own git worktree, behind a deterministic verify gate and a
cross-family review. Taskfiles live in `config.TASKS_DIR` (default `~/tasks`,
override with `ARC_TASKS_DIR`). They are either written by hand or drafted by
the planner — `main.py code plan "<goal>" <repo>`
(GLM-5.3) — and executed by
`main.py code run <taskfile>`.

Every task runs through this pipeline (full contract in
[orchestration-contract.md](orchestration-contract.md)):

```
alloc → implement → gate ──pass──▶ review ──pass──▶ publish → PR review → merge
           ▲            │                │
           └──── fail ◀─┴───── fail ◀────┘   (≤ ARC_MAX_FIX_ROUNDS = 8 fix rounds)
```

Before any of that, a taskfile declaring `project.after` holds at a
`chain_wait` gate until every upstream taskfile's every task is merged
(see "Project chaining" in section 1).

Validate a taskfile and print the resolved DAG without calling any model or
touching git:

```bash
.venv/bin/python main.py code run ~/tasks/my-project.json --dry-run
```

`load_taskfile` (section 3) runs before anything else in `code run`; a
validation error aborts before any model call or git mutation. A real run
also exits with `repo not found: <path>` if `project.repo` does not exist
(`--repo PATH` overrides the taskfile's repo; `--dry-run` skips the check).

## 1. Top-level shape

```json
{"project": {"repo": "/absolute/path/to/repo", "title": "short label",
             "tasks": [ /* task objects, section 2 */ ]}}
```

| Key | Required | Meaning |
|---|---|---|
| `project.repo` | yes | Absolute path to the blessed clone (`Path(...).resolve()`d by the loader). Worktrees are allocated under `~/worktrees/<repo-name>/<task-id>` (`ARC_WORKTREE_ROOT`); reviewed merges land on its `main`. |
| `project.title` | no | Short informational label; defaults to `""`. Never shown to the implementing agents. |
| `project.tasks` | yes | JSON array of task objects. `load_taskfile` imposes no count limit and accepts an empty array (such a run is a no-op); the 1–50 requirement lives only in the dashboard's create-project endpoint, and the planner aims for 2–6. |
| `project.after` | no | List of taskfile paths this project chains on (see "Project chaining" below). `[]`/absent = start immediately. |
| `project.human_review` | no | `true` = every task's pull request, once the fleet's reviewers approve it, WAITS for a human decision before it merges (a human checkpoint; see "Human checkpoints" below). `false` = never wait. Absent = the fleet-wide `ARC_PR_MANUAL_REVIEW` decides. Must be a boolean (`ValueError` otherwise). |
| `project.pattern` | no | The planner's label for the shape of the graph between tasks: a `graph_shapes.PATTERNS` id (`single`, `chain`, `fanout`, `diamond`, `router`, `debate`, `hierarchical`). Aliases such as `fan-out-fan-in` are normalized by the loader; an unknown name is kept and logged. A label only — `deps` are the graph; `code_tasks.describe` reports the shape the deps actually form and flags a mismatch. See [graph-patterns.md](graph-patterns.md). |

### Project chaining (`project.after`)

Per-task `deps` order tasks **inside** one taskfile; `after` orders whole
**taskfiles**: this file's run holds at the `chain_wait` gate until every
task of every listed upstream taskfile is merged, and only then allocates
its first worktree.

```json
{"project": {"repo": "/home/proxyie/repos/notes", "title": "notes: web UI",
             "after": ["/home/proxyie/tasks/notes-cli.json"],
             "tasks": [ ... ]}}
```

- **Path resolution**: an absolute path is used as-is; a bare filename
  resolves against `config.TASKS_DIR` (`~/tasks`); a multi-component
  relative path resolves against `~/tasks` if it exists there, else against
  the cwd. The resolved path is the canonical key — the same form
  `code run` records in the `code_tasks` table.
- **Readiness is per task id, parsed from the upstream taskfile on disk** —
  not per row. A dep run whose process died after 3 of 4 tasks merged
  leaves 3 `merged` rows and no evidence the 4th ever ran; row-counting
  would call that project done and branch this one from a base missing
  task 4.
- **Waiting vs. blocked**: a dep taskfile not on disk yet, a dep with
  unfinished tasks, or no rows at all = *waiting* (chains may be declared
  before the upstream project is even planned). A dep task row with status
  `failed` or `conflict` = *blocked*: the run ends immediately with exit
  code 1 and a `chain.blocked` event.
- **While it waits nothing exists** — no worktree, branch, or task row for
  this taskfile — so a cancelled wait leaves nothing behind to clean up.
- **Budget**: the gate polls every 10 s and gives up after
  `ARC_CHAIN_TIMEOUT` (default 6 h), ending the run with exit code 1.
  Events: `chain.wait` on entry, `chain.ready` on release,
  `chain.blocked` on failure/timeout.
- **Cycles** (`a.json` after `b.json` after `a.json`) are rejected when the
  graph is built: `ValueError: project.after cycle detected: ...`.
- **`--no-wait`** turns the gate into a pre-flight check: instead of
  waiting, `code run` exits with code **2** when the chain is not ready
  (and 0 when it is) — the signal a queue wrapper uses to requeue. It
  writes no rows — the check is read-only — and is safe to run while
  another process owns the upstream run.
- `code status` reports every taskfile in `~/tasks` that declares `after`
  under a `chains` key: `{taskfile, after, ready, waiting, failed}`.

### Human checkpoints (`human_review`)

A game is judged by playing it, so a project can make a person the last
gate. With `human_review` on, `pr_review` (after the fleet's reviewers all
approve) calls `code_tasks._await_manual_review`, which records a hold in the
`manual_reviews` table (`manual_review.py`) and waits — no model time is
spent — until a human decides:

- **Dashboard → "Needs you"** (desktop tab, and the phone page's Review
  view): each held PR with its evidence inline (before | after | difference
  per camera, screenshots, flythrough and playtest videos, evidence
  warnings), the fleet reviewers' verdicts, **▶ Play this build** (and main)
  on the PC's display with a scene picker, and **Approve** / **Request
  changes**. The request-changes comment is required and becomes the
  implementer's feedback exactly like a reviewer's issue; a new PR round
  follows. Routes: `GET /api/reviews`, `POST /api/reviews/decide`
  (`review_routes.py`, behind `_refuse_post` and the dashboard token).
- **GitHub labels**: `manual-approved`, or `manual-rejected` plus a comment.

Resolution per task: the task's own `human_review`, else
`project.human_review`, else `ARC_PR_MANUAL_REVIEW`. A decision made while
the run is down is kept and applied when it resumes to that PR round.
`ARC_PR_MANUAL_TIMEOUT` (default 0 = wait forever) turns into a
*rejection*, never a merge.

```json
{"project": {"name": "prison-escape-test", "repo": "...", "human_review": true,
             "tasks": [{"id": "docs-readme", "human_review": false, ...}, ...]}}
```

## 2. Per-task fields

| Field | Type | Default | Meaning |
|---|---|---|---|
| `id` | string | — (required) | Unique task id, kebab-case `[a-z0-9][a-z0-9-]{0,60}` (1–61 chars, lowercase digits/hyphens, starts with a lowercase letter or digit). The canonical key everything else references: `deps` entries, branch `task/<id>`, worktree `~/worktrees/<repo>/<id>`, commit trailer `Task-Id`, harness transcripts `<id>-x<attempt>-<role>-<n>.jsonl`. Must be unique within the file. |
| `title` | string | `id` | One-line human summary; becomes the merge commit message `task(<id>): <title>` and appears in `describe()`/status output. |
| `prompt` | string | — (required) | The complete task spec. **The implementing agent sees ONLY its own prompt** — prefixed with the task id/title and `files_hint`, plus standard boilerplate ("make only the changes this task requires; do not git-commit; keep changes minimal and working"). No other tasks, no project context. The reviewer also judges the diff against exactly this prompt. |
| `model` | string | `""` (rejected) | Implementer model, one of today's live implementers, tier-routed — see table below. `DeepSeek-V4.1-Flash-thinking-max` runs via the `reasonix` harness; `GLM-5.3` via `opencode`. |
| `reviewer` | string | `""` (rejected) | `"glm"` (GLM-5.3 via `opencode`) or `"deepseek"` (DeepSeek via `reasonix`) — the only two review families today, subject to the cross-family rule below. |
| `verify_cmd` | string | `""` (gate skipped) | Deterministic honesty gate: a shell command run in the task's worktree that **must fail when the work is wrong**. Exit 0 = pass. See "The verify gate" below. |
| `files_hint` | list of strings | `[]` | Repo-relative paths the task expects to touch. Injected into the implementer prompt as `Files you are expected to touch: ...`. Informational (not enforced), but keep **disjoint between dep-independent (parallel) tasks** — two agents editing the same file is the main cause of `conflict` merge failures. |
| `probe_cmd` | string | `""` | Optional. A shell command run in the task's worktree **after its gate passes**; the last top-level JSON object it prints is the task's **verdict**, stored on the row (`code_tasks.verdict`) and emitted as `task.verdict`. A probe that exits non-zero or prints no JSON object fails the gate — a branch skipped because the probe crashed would be a bug disguised as a decision. Dependents read the verdict through `when`. |
| `when` | object | absent | Optional. Run this task **only if** a dependency's verdict satisfies a condition; otherwise the task and everything downstream of it are recorded `skipped` (a terminal status that counts as complete). Shape: `{"dep": "<id, also in deps>", "key": "<verdict field, dotted ok>", <one operator>}` with operator one of `"equals": v`, `"not_equals": v`, `"in": [..]`, `"truthy": bool`, `"exists": bool`. The named dep must have a `probe_cmd`. Wired as `Edge(when=)` on the release edge (`pr_merge_<dep>` or the join → `alloc_<task>`), with a `skip_<task>` node on the complement. This is the one-taskfile **router** (see [graph-patterns.md](graph-patterns.md) § 4). |
| `human_review` | bool | absent | Optional. Overrides `project.human_review` for this task: `true` holds its fleet-approved PR for a human decision, `false` lets it merge on the fleet's approval. Must be a boolean. |
| `deps` | list of strings | `[]` | Ids of tasks whose merged output this task needs (the id is the canonical key). One dep: the runner wires `pr_merge_<dep> → alloc_<task>`. Two or more: a gather node (`join_<task>`) waits for **every** listed dep's PR to merge before the task allocates its worktree — a real join, order irrelevant (`code_tasks.build_code_graph`, `wire_deps`). The worktree always branches from `main`, so it inherits every earlier merge. |

### Model routing (tier table, `config.IMPLEMENT_TIERS`)

The roster is DATED (`config.ROSTER`) and validated against what the API
actually serves, so this table is a snapshot — `main.py code run <file>
--dry-run` prints today's. As of 2026-09-12:

| Tier | Model | Use for |
|---|---|---|
| medium | `DeepSeek-V4.1-Flash-thinking-max` | Documentation, mechanical edits, one self-contained feature — the fast workhorse that carries the implementation load |
| hard | `GLM-5.3` | Deep understanding, multi-file reasoning, large refactors — the fleet's strongest model, which also plans and is the last escalation stage |

There is no basic tier (gpt-oss-120b was retired 2026-09-11). Kimi-K3 was
retired from this repo on 2026-09-12, so `medium` and `hard` are the only
tiers. The planner
prompt adds: *SPREAD work across the models so independent tasks run in
parallel*, and tasks should be small (<30 min for one agent). Per-model
driver concurrency follows provider/measured ARC ceilings minus an interactive
reserve (`config.driver_limit`; see
[concurrency-limits.md](concurrency-limits.md)).

### Reviewer and the cross-family rule

`reviewer` names a FAMILY from `config.REVIEW_FAMILIES` — today `"glm"` or
`"deepseek"` — and the loader rejects any that shares a family with
the implementer:

| Implementer | Allowed `reviewer` |
|---|---|
| `DeepSeek-V4.1-Flash-thinking-max` | `"glm"` |
| `GLM-5.3` | `"deepseek"` |

With two families the pairing is forced; the scheduler still load-balances PR
reviewers at run time (`_reviewer_pressure`).
A taskfile that names a reviewer family which is not live today is
remapped by `config.cross_family_reviewer` (code_tasks.py:94), not rejected —
the remap tests the REVIEWER family, so it covers `"kimi"` on any task, not
only tasks whose `model` came off `code_tasks.RETIRED_MODELS` (that table
remaps the task's `model` field alone). The 2026-09-12 same-family override
(an `ARC_*` env var, for GLM-5.3's backend instability) was
removed 2026-09-14; cross-family review is unconditional again.

### The verify gate (`verify_cmd`)

- Run via `asyncio.create_subprocess_shell` with **cwd = the task's worktree**;
  stdout and stderr are combined.
- **Exit 0 = pass**; any other exit code fails the gate. On failure the last
  2000 chars of output are fed back to the implementer
  ("verify gate failed, output: ...") and the task loops back to `implement`
  (up to `ARC_MAX_FIX_ROUNDS` = 8 rounds, then the task escalates a tier).
- Killed at `config.GATE_TIMEOUT` (default **180s**, override `ARC_GATE_TIMEOUT`)
  → fails with `gate timed out after 180.0s`. Keep the command fast.
- Empty string **skips** the gate (auto-pass) — avoid this: the gate is the
  only deterministic check; review alone is weaker.

Typical honest gates:

| Kind | Example |
|---|---|
| Python syntax | `python -m py_compile src/app.py` |
| JS syntax | `node --check src/app.js` |
| Test suite | `npm test --silent` / `python -m pytest tests/` |
| Contract grep | `test -f src/index.html && grep -q Hello src/index.html` |

### Two rules a gate must follow

**Use `./py`, never `.venv/bin/python`.** A gate runs inside the task's git
worktree, and worktrees have no `.venv` — it is gitignored, so one is never
checked out. `.venv/bin/python` there is "No such file or directory" no matter
how correct the work is. That cost 26 implement attempts and 6 escalations
across two tasks before it was found, then was reintroduced in four more task
files. `./py` resolves the interpreter from the main worktree. `check.sh`
fails any gate that still names `.venv/bin/python`.

**Grep alone proves nothing.** A grep says a string is present, not that the
change works — and it cannot tell a working config from a corrupted one. One
task rewrote `.gitignore` into `*.pyclogs/*.lock`, which still contains the
substring its gate grepped for, so the gate passed and the corruption landed.
Make `./check.sh` the FIRST clause of any gate that touches code:

```
"verify_cmd": "./check.sh && test -f humanize.py && ./py -m unittest discover -s tests -t tests -k Humanize 2>&1 | grep -q 'OK'"
```

**A gate must FAIL before the task is done.** A gate that already passes on an
unchanged tree tests nothing. Before running a task file, check that each
gate's task-specific clause currently fails — and that it greps for something
the task will CREATE, not for a symbol that has since been renamed. One gate
grepped `miniDag` after that function had been consolidated into `taskDag`; it
could never pass, and the task looped through three implement attempts before
anyone noticed.

### Tests are mandatory

Every task that changes code must add or update tests, and the task prompt
should say so explicitly. The PR review follows the pre-PR review: two readers
are wanted (`config.PR_REVIEWERS_WANTED`), but the two-family fleet fields
exactly ONE cross-family reader per PR — recorded by `task.pr_review_thin` —
and that reader is instructed to **reject a
code change that ships no test which would fail without it**. A task with no
tests does not merge slowly — it loops through `PR_MAX_ROUNDS` and fails.

Keep each task's tests in their own file where possible: two parallel tasks
editing one test file will conflict.

Documentation-only tasks are exempt.

### What the implementer sees (exactly)

`code_tasks._impl_prompt` builds, verbatim:

```
You are implementing one task in this repository.

TASK <id>: <title>

<prompt>

Project contract: read AGENTS.md ...               ← when the worktree has
                                                       AGENTS.md, CLAUDE.md,
                                                       or .cursor/rules
                                                       (project_contract.role_block)

Files you are expected to touch: <files_hint>        ← only if non-empty

Rules: make only the changes this task requires; do not git-commit
(the orchestrator handles git); keep changes minimal and working.
```

On fix-loop retries it appends the reviewer's issues or the failed gate
output. So `prompt` must be **self-contained**: name the repo-relative file
paths, the exact functions/behavior expected, acceptance criteria, and what
NOT to touch. "See the other task" or "as discussed" is invisible to the agent.

### What the reviewer sees

The review prompt contains the task id/title, the `prompt` (as SPEC), a note
that the gate (`verify_cmd`) already passed, the **full working-tree diff
vs `main`** (capped at 24,000 chars), and the project-contract line for
that worktree when one is passed in. The reviewer replies strict JSON
`{"pass": true}` or `{"pass": false, "issues": [...]}`; rejections loop back
to the implementer with the issue list.

## 3. Validation rules (`code_tasks.load_taskfile`)

`load_taskfile(path)` parses the file (UTF-8) and enforces, in order:

1. **JSON parses** — otherwise `json.JSONDecodeError`.
2. **Required keys** — `project`, `project.repo`, `project.tasks` and
   per-task `id` are read up front; per-task `prompt` is read when the task
   record is built (after checks 4–7 for that task). A missing key raises
   `KeyError` (not `ValueError`), e.g. `KeyError: 'prompt'`.
3. **Id shape** — the id must match `[a-z0-9][a-z0-9-]{0,60}` (it becomes a
   branch name, a worktree path, a ref fragment, and a log filename):
   `ValueError: task id {tid!r} must match [a-z0-9][a-z0-9-]{0,60} (it becomes a worktree path and a git-ref fragment)`
4. **Duplicate ids** — `ValueError: duplicate task id: {tid}`
5. **Unknown/missing model** — the model must be one of today's
   `config.IMPLEMENTER_MODELS` (a missing `model` defaults to `""` and is
   rejected here too; a RETIRED name is remapped, not rejected):
   `ValueError: task {tid}: model {model!r} must be an implementer (['DeepSeek-V4.1-Flash-thinking-max', 'GLM-5.3'])`
6. **Reviewer must be a live review family** (missing defaults to `""`, rejected):
   `ValueError: task {tid}: reviewer must be one of deepseek|glm, got {reviewer!r}`
7. **Cross-family rule** — the reviewer must not share the implementer's
   family (`config.cross_family_reviewer`), enforced unconditionally (the
   2026-09-12..14 same-family-review override was removed):
   `ValueError: task {tid}: reviewer {reviewer!r} must not be the harness that implemented ({model}); use the other one`
8. **Unknown deps** — every `deps` entry must be a task id in this file:
   `ValueError: task {tid}: unknown dep {d!r}`
9. **No cycles** — topological sort over deps:
   `ValueError: dependency cycle at {tid}`
10. **`project.after` shape** — must be a list of non-empty strings:
    `ValueError: project.after must be a list of taskfile paths`. Entries are
    deduplicated and resolved to canonical absolute keys; a taskfile listing
    **itself** is rejected
    (`ValueError: project.after lists this taskfile itself: ...`). Upstream
    files need not exist yet — waiting is a runtime state, not a validation
    error — but a cycle among existing files is rejected at graph build
    (`ValueError: project.after cycle detected: ...`).

Not checked by the loader (know where these live):

- **Tier routing** — the loader rejects models outside today's implementers,
  but does not
  check medium/hard suitability; tier assignment is an authoring rule
  from `config.IMPLEMENT_TIERS` and the planner prompt.
- **Field types beyond the above** — `deps`/`files_hint` are only wrapped with
  `list(...)`; keep them JSON arrays of strings (a bare string gets split into
  characters and then fails rule 8 with `unknown dep`).
- **Prompt quality / verify_cmd honesty** — the gate only checks exit codes;
  only you (or the planner) can make them meaningful.

## 4. Complete example

One hard task first (`notes-schema`, GLM-5.3 implements, deepseek reviews),
then three tasks fanning out in parallel once it merges — all medium, all
implemented by DeepSeek-V4.1-Flash-thinking-max and reviewed by glm —
with disjoint `files_hint` (glm reviews the DeepSeek work; the reverse pairing
is deepseek reviewing GLM-5.3):

```json
{
  "project": {
    "repo": "/home/proxyie/repos/notes",
    "title": "notes CLI: schema, commands, export, docs",
    "tasks": [
      {
        "id": "notes-schema",
        "title": "Note model and JSON persistence",
        "prompt": "This repo is a small note-taking CLI. Create the package dir src/notes/ (with an empty __init__.py) and src/notes/schema.py defining: a Note dataclass with fields id: str, text: str, created: str (ISO-8601); load_notes(path) -> list[Note] that reads a JSON array of note objects (missing file returns []); save_notes(path, notes) that writes the same shape back. Acceptance: python -m py_compile passes and both functions exist with exactly these names. Do not add a CLI, tests, or touch any other file.",
        "model": "GLM-5.3",
        "reviewer": "deepseek",
        "verify_cmd": "python -m py_compile src/notes/schema.py && grep -q 'def load_notes' src/notes/schema.py && grep -q 'def save_notes' src/notes/schema.py",
        "files_hint": ["src/notes/__init__.py", "src/notes/schema.py"],
        "deps": []
      },
      {
        "id": "notes-cli",
        "title": "add and list CLI commands",
        "prompt": "src/notes/schema.py already exists (merged by a previous task) and provides Note, load_notes(path), save_notes(path, notes) — read it first. Create src/notes/cli.py with an argparse CLI runnable as python -m notes.cli: subcommand add \"<text>\" appends a Note (id=str(uuid4()), created=now ISO-8601) using save_notes to notes.json in the current directory; subcommand list prints one '<created>  <text>' line per note via load_notes. Acceptance: py_compile passes and both subcommands are registered. Do not modify schema.py or any other file.",
        "model": "DeepSeek-V4.1-Flash-thinking-max",
        "reviewer": "glm",
        "verify_cmd": "python -m py_compile src/notes/cli.py && grep -q '\"add\"' src/notes/cli.py && grep -q '\"list\"' src/notes/cli.py",
        "files_hint": ["src/notes/cli.py"],
        "deps": ["notes-schema"]
      },
      {
        "id": "notes-export",
        "title": "Markdown and JSON export module",
        "prompt": "src/notes/schema.py already exists (merged by a previous task) and provides Note and load_notes — read it first. Create src/notes/export.py with export_notes(notes: list[Note], fmt: str) -> str: fmt='md' returns one '- <created> <text>' bullet per note; fmt='json' returns a JSON array of {id, text, created}; any other fmt raises ValueError. Acceptance: py_compile passes, the function exists with that signature, unknown fmt raises. Do not modify schema.py, cli.py, or any other file.",
        "model": "DeepSeek-V4.1-Flash-thinking-max",
        "reviewer": "glm",
        "verify_cmd": "python -m py_compile src/notes/export.py && grep -q 'def export_notes' src/notes/export.py",
        "files_hint": ["src/notes/export.py"],
        "deps": ["notes-schema"]
      },
      {
        "id": "usage-docs",
        "title": "README usage section",
        "prompt": "Add a '## Usage' section to README.md (create the file if missing) documenting two commands, each on its own code-formatted line: python -m notes.cli add \"buy milk\" and python -m notes.cli list. The CLI itself is being built in parallel in src/notes/cli.py — do NOT create, modify, or reference-check any source file; only edit README.md. Acceptance: README.md contains both command lines verbatim.",
        "model": "DeepSeek-V4.1-Flash-thinking-max",
        "reviewer": "glm",
        "verify_cmd": "grep -q 'notes.cli add' README.md && grep -q 'notes.cli list' README.md",
        "files_hint": ["README.md"],
        "deps": ["notes-schema"]
      }
    ]
  }
}
```

`code run --dry-run` prints the resolved DAG (verify commands elided here):

```
repo: /home/proxyie/repos/notes
  notes-schema: implement=GLM-5.3 review=deepseek(cross-family) deps=[] base=main verify=python -m py_compile ...
  notes-cli: implement=DeepSeek-V4.1-Flash-thinking-max review=glm(cross-family) deps=['notes-schema'] base=main verify=...
  notes-export: implement=DeepSeek-V4.1-Flash-thinking-max review=glm(cross-family) deps=['notes-schema'] base=main verify=...
  usage-docs: implement=DeepSeek-V4.1-Flash-thinking-max review=glm(cross-family) deps=['notes-schema'] base=main verify=...
```

Why it is shaped this way:

- **Fanout**: `notes-schema` starts at t=0; the other three all depend only on
  it and run concurrently once it merges.
- **Tier routing**: hard → GLM-5.3 (the schema everything depends on, and the
  fleet's strongest model); medium → DeepSeek-V4.1-Flash-thinking-max for the
  CLI, the export module and the README; both implementers used.
- **Cross-review**: every reviewer is a different family from its
  implementer — GLM-5.3's work goes to deepseek, DeepSeek's work goes to
  glm. With two families there is exactly one valid reviewer per task.
- **Disjoint `files_hint`** across the three parallel tasks (`cli.py`,
  `export.py`, `README.md`) so merges cannot collide.
- **Single-dep lists**: each dependent lists exactly one dep; with
  `deps: [a, b]` only `b` gates the start (see section 2).

## 4b. What happens to your task file

```
alloc (worktree on task/<id>, branched from the base branch — main by default)
  └─ implement ─ gate ─ review ─ publish (commit, sync with base, push, OPEN PR)
                                    └─ pr_review (ONE cross-family reviewer in the
                                       two-family fleet; task.pr_review_thin records it)
                                         ├─ approved → pr_merge (squash into the base branch)
                                         └─ rejected → back to implement, same PR,
                                            max 8 rounds (ARC_PR_MAX_ROUNDS)
```

Nothing is merged locally. The pull request is the gate, so a reviewer's
rejection genuinely withholds the change. A task with `deps` starts only once
its dependency's PR has **merged**, not merely opened.

The base branch is `main` unless `ARC_BASE_BRANCH` says otherwise. With
`ARC_BASE_BRANCH=development`, `development` reaches `main` only through
`main.py code promote`, which opens a PR for a human to merge.

## 4c. The plan is a living document — agent amendments (`plan_amend.py`)

A planner decomposes once, but the best information about the plan arrives
*inside* the run: an implementer discovers its task is really two tasks, a
reviewer sees the verify gate does not test the spec. Any agent
(implementer, pre-merge reviewer, PR reviewer) may therefore propose changes
to the taskfile it is running against, by appending **one JSON object per
line** to:

```
.arc/plan_proposals.jsonl        (inside its own worktree; create .arc/)
```

The graph nodes read and **immediately delete** this file right after every
agent run (implement, review, PR-review — success and crash paths alike) and
once more in publish before `git add -A` runs, so the file can **never be
committed into a PR**. Each proposal is then validated by the *same loader*
the taskfile came from (`code_tasks.load_taskfile`, policy included — Rules
1/2/8 hold for agents exactly as for planners) and the survivors are applied
by rewriting the taskfile atomically.

### Proposal kinds

```jsonc
{"kind":"note","task":"<id>","note":"observation, risk, or follow-up"}
{"kind":"edit_scope","task":"<id>","title":"...","prompt":"...","files_hint":["..."]}
{"kind":"change_verify","task":"<id>","verify_cmd":"<shell command>"}
{"kind":"change_model","task":"<id>","model":"<model>"}   // reviewer auto-flips
                                                          // if the pairing goes same-family
{"kind":"add_task","taskspec":{"id":"<new-id>","title":"...","prompt":"...",
        "model":"<model>","verify_cmd":"<shell command>","deps":["<id>"]}}
{"kind":"split_task","task":"<id>","into":[{"id":"<new-id>","title":"...",
        "prompt":"...","verify_cmd":"<shell command>","deps":["<id>"]}, ...]}
```

`"task"` always names an **existing** task in the same file. New ids go in
`taskspec` (add) or `into` (split, 2–4 pieces). A split's pieces inherit the
target's `model` and `verify_cmd` unless overridden, the first piece **always
carries the target's `deps`** (any deps it declares are unioned in, never a
replacement — dropping the target's upstream would start the piece before its
input exists), and every task that depended on the target re-points to **all**
pieces. A `when`-conditional task cannot be split, and neither can a task
another `when` reads — re-point the condition by hand first.

Two things v1 deliberately has no kind for: **adding a dep to an existing
task** (write an `add_task` whose `deps` name it, or attach a `note` for the
planner/operator), and **conditional new tasks** (`add_task` /
`split_task` produce plain unconditional tasks — no `when` / `probe_cmd` in
proposals).

### The honesty boundary (v1)

- **Only tasks that have not started may be mutated.** `merged` is history;
  `running` / `in_review` / `conflict` mean a live agent or an open PR is
  acting on the old text right now. Both reject. `failed` / `skipped` tasks
  MAY be amended — resume re-reads the file. A `note` lands on any task
  regardless.
- **The in-flight DAG never rewires.** This run's graph was built from the
  file as it was; amendments take effect when the run **resumes** (`code run
  <taskfile>` re-parses it), for tasks with no row yet, and for downstream
  chain gates that parse this file when their wait ends.
- Never removes or renames a task, never resurrects an id that has a
  `code_tasks` row (a new task filed under a merged id would be skipped by
  resume as already done), never empties a verify gate (Rule 4), never
  touches project-level keys.
- A proposal that leaves the whole file invalid under the loader is rolled
  back and rejected; its siblings still apply.

### The trail

Every proposal — applied, rejected, or merely noted — is recorded in the
`plan_proposals` table (`taskfile, target, proposer, role, model, kind,
action, reason, payload`) and emitted as a `plan.amend` event. Read it with:

```
GET /api/plan-proposals[?file=<taskfile.json>][&limit=N]   (dashboard, read-only)
```


## 5. Authoring tips

- **Write `verify_cmd` first.** It is the contract: a command that fails when
  the work is wrong. Then write the prompt so a correct implementation passes
  it. A gate that always exits 0 (or an empty one) lets bad code through to
  review, and reviewers are softer judges than `grep`.
- **One file area per task.** A task confined to one module is easy to
  specify, gate, review, and merge. Sprawling tasks blow the 3-round fix loop.
- **Keep parallel tasks' `files_hint` disjoint.** Dep-independent tasks run
  concurrently in separate worktrees branched from the same `main`; overlapping
  edits collide at merge time and end in `conflict`.
- **Never chain write + self-review in one task.** Reviewing is the
  orchestrator's job, done by a *different* family in a separate graph node —
  the loader rejects `DeepSeek-V4.1-Flash-thinking-max`+`deepseek` and
  `GLM-5.3`+`glm` outright. Don't try
  to have a task "also check the previous task's output"; add a dep and let
  the reviewer do it.
- **Make every prompt self-contained.** The agent sees only its own prompt:
  include repo-relative paths, exact names/signatures, acceptance criteria,
  and an explicit "do not touch" list. Point dependent tasks at the merged
  files they should read first.
- **Use `deps` only when one task truly reads another's output.** Chains
  serialize; stylistic ordering wastes parallelism. If a task needs two
  parallel tasks' output, chain them (A→B, then `deps: [B]`) rather than
  listing both — only the last dep gates the alloc.
- **Use `after` only between taskfiles, never within one.** `deps` orders
  tasks inside a file (fine-grained, per-task PR merges); `after` orders
  whole files (one gate, zero worktrees until every upstream task merged).
  Chaining two files that could interleave wastes the parallelism the DAG
  exists to exploit — but chaining files whose tasks genuinely read the
  upstream's merged code is exactly what `after` is for.
- **Keep `verify_cmd` under the 180s gate timeout** and deterministic (no
  network, no flaky suites).
- **Size tasks for one agent sitting** (<30 min) and spread them across both
  models — and across both harnesses — so parallel slots fill.

## Cross-references

- [../AGENTS.md](../AGENTS.md) — repo-level agent conventions
- [orchestration-contract.md](orchestration-contract.md) — what the
  orchestrator decides vs. what the runner guarantees
- [model-tiers.md](model-tiers.md) — tier table and cross-review matrix
- [concurrency-limits.md](concurrency-limits.md) — family and driver caps
- [runbook.md](runbook.md) — operating, monitoring, and recovering runs