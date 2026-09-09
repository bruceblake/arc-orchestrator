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
| `deps` | list of strings | `[]` | Ids of tasks whose merged output this task needs (the id is the canonical key). The runner wires `publish_<last dep> → alloc_<task>`: a dependent task allocates its worktree only after the **last listed** dep has merged to `main` (merges are serialized; the worktree always branches from `main`, so it inherits every earlier merge). |

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
   record is built (after checks 3–6 for that task). A missing key raises
   `KeyError` (not `ValueError`), e.g. `KeyError: 'prompt'`.
3. **Duplicate ids** — `ValueError: duplicate task id: {tid}`
4. **Unknown/missing model** — the model must be one of the four
   `config.IMPLEMENTER_MODELS` (a missing `model` defaults to `""` and is
   rejected here too):
   `ValueError: task {tid}: model {model!r} must be an implementer (['DeepSeek-V4-Flash', 'GLM-5.3', 'Kimi-K3', 'gpt-oss-120b'])`
5. **Reviewer must be kimi/glm** (missing defaults to `""`, rejected):
   `ValueError: task {tid}: reviewer must be 'kimi' or 'glm', got {reviewer!r}`
6. **Cross-harness rule** — only when the implementer is `Kimi-K3` or
   `GLM-5.3` (families `kimi`/`glm`); the reviewer must be the other one:
   `ValueError: task {tid}: reviewer {reviewer!r} must not be the harness that implemented ({model}); use the other one`
7. **Unknown deps** — every `deps` entry must be a task id in this file:
   `ValueError: task {tid}: unknown dep {d!r}`
8. **No cycles** — topological sort over deps:
   `ValueError: dependency cycle at {tid}`

Not checked by the loader (know where these live):

- **id format** — the kebab-case pattern is enforced by the dashboard's
  create-project endpoint (`dashboard.py`: `task {i}: id must match [a-z0-9][a-z0-9-]{0,60}`);
  hand-written files should follow the same pattern since ids become branch
  names, log filenames, and `deps` keys. The loader itself only enforces
  uniqueness.
- **Tier routing** — the loader rejects models outside the four, but does not
  check basic/medium/hard suitability; tier assignment is an authoring rule
  from `config.IMPLEMENT_TIERS` and the planner prompt.
- **Field types beyond the above** — `deps`/`files_hint` are only wrapped with
  `list(...)`; keep them JSON arrays of strings (a bare string gets split into
  characters and then fails rule 7 with `unknown dep`).
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