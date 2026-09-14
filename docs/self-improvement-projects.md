# Self-improvement projects

Task files in `~/tasks/` that point the fleet at this repository. Run one with

```bash
.venv/bin/python main.py code run ~/tasks/<name>.json
```

or from the dashboard's project card. Always `--dry-run` first after editing one.

## The catalogue

| project | tasks | what it adds | shape |
| --- | --- | --- | --- |
| `selftest-untested-modules.json` | 4 | unit tests for `pool.py`, `scheduler.py`, `events.py`, `bench_data.py` — the modules with no coverage | fully parallel, 4 disjoint new files |
| `operator-cli.json` | 3 | `main.py code list` and `main.py doctor` (pre-flight for plan mode, API key, harness binaries, timeout invariants, stale rows), plus docs | serial chain |
| `fleet-observability.json` | 3 | `/api/metrics` rollup, live agent progress in the Fleet panel, docs | 2 parallel then 1 |
| `github-pr-flow.json` | 3 | `gitstore.github_status()`, PR readiness in the detail view, and how to turn PRs on | serial chain |
| `engine-hardening.json` | 3 | `task.budget` events (what a task actually cost), persisted verify-gate output | serial chain |

Start with `selftest-untested-modules.json`. It is the safest — four
independent tasks that only ADD test files, so nothing existing can regress,
and it exercises the full fan-out path.

## Queued roadmap (bigger than one taskfile)

These are multi-project efforts, executed in order. Each becomes its own
taskfile written when its turn comes; this list is the durable queue.

1. **Serve-migration** — move the fleet drivers off one-shot harness
   processes onto `opencode serve`-mode sessions (session binding via
   `x-opencode-directory`, `prompt_async`, SSE). The capacity-400 that
   arrives as a structured `session.error` with `isRetryable:false` must be
   special-cased retryable (it means account-cap full, not task failure);
   `/instance/dispose` ends sessions instantly. Chained so nothing else
   builds on the old driver shape.
2. **Lloyd-style loop orchestrator** — a manager agent with a tickets table
   (sqlite, its own db), a mission note (who/what/how), a pulse loop over
   logs + recent merges + backlog, and a plan-review gate before non-trivial
   builds. Taskfile declares `project.after` the serve-migration taskfile.
3. **Frontend/backend decoupling (2026-09-13 request)** — split the
   monolithic `dashboard.py` (static files + JSON APIs + process spawning in
   one http.server) into a dedicated **backend API service** (read APIs,
   mutating routes, process supervision, event log, one bind address; clean
   route allowlist per Rule 6b) and a **separate frontend service** serving
   the SPA (`static/`), talking to the backend over versioned endpoints.
   Replace timer polling with server-push (SSE/websocket) for events, agents
   and transcripts. Searchable API surface from day one: today the frontend
   calls ~15 ad-hoc routes; the split is when they get a schema. Must keep
   `start.sh`/`stop.sh` and the deploy/ systemd units working (they gain one
   more unit), and Rule 6b's unauthenticated-LAN posture stays intact. Chain
   `after` serve-migration.

## How these are shaped, and why

Every rule below came from a failure on 2026-09-09. See
[audit-2026-09-09.md](audit-2026-09-09.md).

- **No task asks for a whole-file rewrite.** Long responses are what ARC
  terminates: a task prompted to rewrite a 504-line file lost 33% of its
  requests against an 8.3% baseline, and burned 30 of its 45 minutes on
  retries. Prompts that touch a big file say "make a TARGETED edit" and name
  the function.
- **`files_hint` is disjoint** between tasks with no dependency, or their
  parallel merges collide.
- **Every `verify_cmd` starts with `./check.sh`**, so a task cannot merge a
  change that breaks the engine running it. The clause after it is specific to
  the task and *fails today* — a gate that already passes proves nothing.
- **Routing follows real difficulty.** `DeepSeek-V4.1-Flash-thinking-max` for
  documentation,
  mechanical edits, and one self-contained feature; `GLM-5.3` where
  multi-file reasoning is
  genuinely needed. GLM-5.3 is the fleet's
  strongest model and its planner — do not spend it on prose.
- **Prompts are self-contained.** The implementer sees only its own prompt and
  the repo, never the project goal, so each one names paths, functions and
  acceptance criteria.

## Writing your own

`main.py code plan "<goal>" <repo>` drafts one with GLM-5.3, or use the
dashboard's **+ New project** (JSON tab) to write tasks directly. Either way,
`--dry-run` it and check the resolved routing before spending tokens.

Schema reference: [taskfile-schema.md](taskfile-schema.md).
