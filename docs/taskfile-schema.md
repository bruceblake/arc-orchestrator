# Taskfile Reference (`~/tasks/*.json`)

A **taskfile** is a JSON document describing a multi-task coding project for
the code workload in `code_tasks.py`: a set of tasks, each implemented by an
AI agent in its own git worktree, behind a deterministic verify gate and a
cross-harness review. Taskfiles live in `config.TASKS_DIR` (default `~/tasks`,
override with `ARC_TASKS_DIR`). They are either written by hand or drafted by
the planner — `main.py code plan "<goal>" <repo>` (Kimi-K3) — and executed by
`main.py code run <taskfile>`.

Every task runs through this pipeline (full contract in
[orchestration-contract.md](orchestration-contract.md)):

```
alloc → implement → gate ──pass──▶ review ──pass──▶ publish → merge to main
           ▲            │                │
           └──── fail ◀─┴───── fail ◀────┘   (≤ ARC_MAX_FIX_ROUNDS = 3 fix rounds)
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

## 2. Per-task fields

| Field | Type | Default | Meaning |
|---|---|---|---|
| `id` | string | — (required) | Unique task id, kebab-case `[a-z0-9][a-z0-9-]{0,60}` (1–61 chars, lowercase digits/hyphens, starts with a lowercase letter or digit). The canonical key everything else references: `deps` entries, branch `task/<id>`, worktree `~/worktrees/<repo>/<id>`, commit trailer `Task-Id`, harness transcripts `<id>-x<attempt>-<role>-<n>.jsonl`. Must be unique within the file. |
| `title` | string | `id` | One-line human summary; becomes the merge commit message `task(<id>): <title>` and appears in `describe()`/status output. |
| `prompt` | string | — (required) | The complete task spec. **The implementing agent sees ONLY its own prompt** — prefixed with the task id/title and `files_hint`, plus standard boilerplate ("make only the changes this task requires; do not git-commit; keep changes minimal and working"). No other tasks, no project context. The reviewer also judges the diff against exactly this prompt. |
| `model` | string | `""` (rejected) | Implementer model, one of the four configured models, tier-routed — see table below. `Kimi-K3` runs via the `kimi` CLI; the other three via `opencode`. |
| `reviewer` | string | `""` (rejected) | `"kimi"` (Kimi-K3 via the `kimi` CLI) or `"glm"` (GLM-5.3 via `opencode`) — the only two reviewers, subject to the cross-harness rule below. |
| `verify_cmd` | string | `""` (gate skipped) | Deterministic honesty gate: a shell command run in the task's worktree that **must fail when the work is wrong**. Exit 0 = pass. See "The verify gate" below. |
| `files_hint` | list of strings | `[]` | Repo-relative paths the task expects to touch. Injected into the implementer prompt as `Files you are expected to touch: ...`. Informational (not enforced), but keep **disjoint between dep-independent (parallel) tasks** — two agents editing the same file is the main cause of `conflict` merge failures. |
| `probe_cmd` | string | `""` | Optional. A shell command run in the task's worktree **after its gate passes**; the last top-level JSON object it prints is the task's **verdict**, stored on the row (`code_tasks.verdict`) and emitted as `task.verdict`. A probe that exits non-zero or prints no JSON object fails the gate — a branch skipped because the probe crashed would be a bug disguised as a decision. Dependents read the verdict through `when`. |
| `when` | object | absent | Optional. Run this task **only if** a dependency's verdict satisfies a condition; otherwise the task and everything downstream of it are recorded `skipped` (a terminal status that counts as complete). Shape: `{"dep": "<id, also in deps>", "key": "<verdict field, dotted ok>", <one operator>}` with operator one of `"equals": v`, `"not_equals": v`, `"in": [..]`, `"truthy": bool`, `"exists": bool`. The named dep must have a `probe_cmd`. Wired as `Edge(when=)` on the release edge (`pr_merge_<dep>` or the join → `alloc_<task>`), with a `skip_<task>` node on the complement. This is the one-taskfile **router** (see [graph-patterns.md](graph-patterns.md) § 4). |
| `deps` | list of strings | `[]` | Ids of tasks whose merged output this task needs (the id is the canonical key). One dep: the runner wires `pr_merge_<dep> → alloc_<task>`. Two or more: a gather node (`join_<task>`) waits for **every** listed dep's PR to merge before the task allocates its worktree — a real join, order irrelevant (`code_tasks.build_code_graph`, `wire_deps`). The worktree always branches from `main`, so it inherits every earlier merge. |

### Model routing (tier table, `config.IMPLEMENT_TIERS`)

| Tier | Model | Use for |
|---|---|---|
| basic | `gpt-oss-120b` | Very basic/mechanical tasks only |
| medium | `DeepSeek-V4-Flash` | Medium tasks only |
| hard | `GLM-5.3` or `Kimi-K3` | Hard tasks: deep understanding, large refactors |

The planner prompt adds: *SPREAD work across all four models so independent
tasks run in parallel*, and tasks should be small (<30 min for one agent).
Per-model harness concurrency is capped at Kimi-K3 ≤ 2, GLM-5.3 ≤ 3,
gpt-oss-120b ≤ 8, DeepSeek-V4-Flash ≤ 8 (see
[concurrency-limits.md](concurrency-limits.md)).

### Reviewer and the cross-harness rule

| Implementer | Allowed `reviewer` |
|---|---|
| `Kimi-K3` | `"glm"` only |
| `GLM-5.3` | `"kimi"` only |
| `gpt-oss-120b`, `DeepSeek-V4-Flash` | either — split reviews between `"kimi"` and `"glm"` so neither idles nor saturates |

A reviewer never shares a model family with the implementer it reviews
(loader-enforced for the two strong models; for gpt-oss/deepseek
implementations both reviewers are cross-family by construction).

### The verify gate (`verify_cmd`)

- Run via `asyncio.create_subprocess_shell` with **cwd = the task's worktree**;
  stdout and stderr are combined.
- **Exit 0 = pass**; any other exit code fails the gate. On failure the last
  2000 chars of output are fed back to the implementer
  ("verify gate failed, output: ...") and the task loops back to `implement`
  (up to `ARC_MAX_FIX_ROUNDS` = 3 rounds, then the task fails).
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
should say so explicitly. After the pre-PR review, `config.PR_REVIEWERS` (2)
independent reviewers read the pull request and are instructed to **reject a
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
that the gate (`verify_cmd`) already passed, and the **full working-tree diff
vs `main`** (capped at 24,000 chars). The reviewer replies strict JSON
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
5. **Unknown/missing model** — the model must be one of the four
   `config.IMPLEMENTER_MODELS` (a missing `model` defaults to `""` and is
   rejected here too):
   `ValueError: task {tid}: model {model!r} must be an implementer (['DeepSeek-V4-Flash', 'GLM-5.3', 'Kimi-K3', 'gpt-oss-120b'])`
6. **Reviewer must be kimi/glm** (missing defaults to `""`, rejected):
   `ValueError: task {tid}: reviewer must be 'kimi' or 'glm', got {reviewer!r}`
7. **Cross-harness rule** — only when the implementer is `Kimi-K3` or
   `GLM-5.3` (families `kimi`/`glm`); the reviewer must be the other one:
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

- **Tier routing** — the loader rejects models outside the four, but does not
  check basic/medium/hard suitability; tier assignment is an authoring rule
  from `config.IMPLEMENT_TIERS` and the planner prompt.
- **Field types beyond the above** — `deps`/`files_hint` are only wrapped with
  `list(...)`; keep them JSON arrays of strings (a bare string gets split into
  characters and then fails rule 8 with `unknown dep`).
- **Prompt quality / verify_cmd honesty** — the gate only checks exit codes;
  only you (or the planner) can make them meaningful.

## 4. Complete example

One hard task first (`notes-schema`, Kimi-K3 implements, glm reviews), then
three tasks fanning out in parallel once it merges — medium, hard, and basic —
with disjoint `files_hint` and Kimi-K3 ↔ GLM-5.3 reviewing each other's work
(glm reviews Kimi's `notes-schema`; kimi reviews GLM's `notes-export`):

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
        "model": "Kimi-K3",
        "reviewer": "glm",
        "verify_cmd": "python -m py_compile src/notes/schema.py && grep -q 'def load_notes' src/notes/schema.py && grep -q 'def save_notes' src/notes/schema.py",
        "files_hint": ["src/notes/__init__.py", "src/notes/schema.py"],
        "deps": []
      },
      {
        "id": "notes-cli",
        "title": "add and list CLI commands",
        "prompt": "src/notes/schema.py already exists (merged by a previous task) and provides Note, load_notes(path), save_notes(path, notes) — read it first. Create src/notes/cli.py with an argparse CLI runnable as python -m notes.cli: subcommand add \"<text>\" appends a Note (id=str(uuid4()), created=now ISO-8601) using save_notes to notes.json in the current directory; subcommand list prints one '<created>  <text>' line per note via load_notes. Acceptance: py_compile passes and both subcommands are registered. Do not modify schema.py or any other file.",
        "model": "DeepSeek-V4-Flash",
        "reviewer": "kimi",
        "verify_cmd": "python -m py_compile src/notes/cli.py && grep -q '\"add\"' src/notes/cli.py && grep -q '\"list\"' src/notes/cli.py",
        "files_hint": ["src/notes/cli.py"],
        "deps": ["notes-schema"]
      },
      {
        "id": "notes-export",
        "title": "Markdown and JSON export module",
        "prompt": "src/notes/schema.py already exists (merged by a previous task) and provides Note and load_notes — read it first. Create src/notes/export.py with export_notes(notes: list[Note], fmt: str) -> str: fmt='md' returns one '- <created> <text>' bullet per note; fmt='json' returns a JSON array of {id, text, created}; any other fmt raises ValueError. Acceptance: py_compile passes, the function exists with that signature, unknown fmt raises. Do not modify schema.py, cli.py, or any other file.",
        "model": "GLM-5.3",
        "reviewer": "kimi",
        "verify_cmd": "python -m py_compile src/notes/export.py && grep -q 'def export_notes' src/notes/export.py",
        "files_hint": ["src/notes/export.py"],
        "deps": ["notes-schema"]
      },
      {
        "id": "usage-docs",
        "title": "README usage section",
        "prompt": "Add a '## Usage' section to README.md (create the file if missing) documenting two commands, each on its own code-formatted line: python -m notes.cli add \"buy milk\" and python -m notes.cli list. The CLI itself is being built in parallel in src/notes/cli.py — do NOT create, modify, or reference-check any source file; only edit README.md. Acceptance: README.md contains both command lines verbatim.",
        "model": "gpt-oss-120b",
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
  notes-schema: implement=Kimi-K3 review=glm(cross-family) deps=[] base=main verify=python -m py_compile ...
  notes-cli: implement=DeepSeek-V4-Flash review=kimi(cross-family) deps=['notes-schema'] base=main verify=...
  notes-export: implement=GLM-5.3 review=kimi(cross-family) deps=['notes-schema'] base=main verify=...
  usage-docs: implement=gpt-oss-120b review=glm(cross-family) deps=['notes-schema'] base=main verify=...
```

Why it is shaped this way:

- **Fanout**: `notes-schema` starts at t=0; the other three all depend only on
  it and run concurrently once it merges.
- **Tier routing**: hard → Kimi-K3 and GLM-5.3, medium → DeepSeek-V4-Flash,
  basic → gpt-oss-120b; all four implementers used.
- **Cross-review**: Kimi-K3 implements `notes-schema` → glm reviews it;
  GLM-5.3 implements `notes-export` → kimi reviews it. The gpt-oss/deepseek
  tasks take whichever reviewer balances the load.
- **Disjoint `files_hint`** across the three parallel tasks (`cli.py`,
  `export.py`, `README.md`) so merges cannot collide.
- **Single-dep lists**: each dependent lists exactly one dep; with
  `deps: [a, b]` only `b` gates the start (see section 2).

## 4b. What happens to your task file

```
alloc (worktree on task/<id>, branched from the base branch — main by default)
  └─ implement ─ gate ─ review ─ publish (commit, sync with base, push, OPEN PR)
                                    └─ pr_review (2 reviewers, unanimous)
                                         ├─ approved → pr_merge (squash into the base branch)
                                         └─ rejected → back to implement, same PR, max 3 rounds
```

Nothing is merged locally. The pull request is the gate, so a reviewer's
rejection genuinely withholds the change. A task with `deps` starts only once
its dependency's PR has **merged**, not merely opened.

The base branch is `main` unless `ARC_BASE_BRANCH` says otherwise. With
`ARC_BASE_BRANCH=development`, `development` reaches `main` only through
`main.py code promote`, which opens a PR for a human to merge.

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
  orchestrator's job, done by a *different* harness in a separate graph node —
  the loader rejects `Kimi-K3`+`kimi` and `GLM-5.3`+`glm` outright. Don't try
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
- **Size tasks for one agent sitting** (<30 min) and spread them across all
  four models so parallel slots fill.

## Cross-references

- [../AGENTS.md](../AGENTS.md) — repo-level agent conventions
- [orchestration-contract.md](orchestration-contract.md) — what the
  orchestrator decides vs. what the runner guarantees
- [model-tiers.md](model-tiers.md) — tier table and cross-review matrix
- [concurrency-limits.md](concurrency-limits.md) — family and driver caps
- [runbook.md](runbook.md) — operating, monitoring, and recovering runs