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
- **Routing follows real difficulty.** `gpt-oss-120b` for documentation and
  mechanical edits, `DeepSeek-V4-Flash` for one self-contained feature,
  `GLM-5.3`/`Kimi-K3` only where multi-file reasoning is genuinely needed.
  Kimi is the scarcest tier (driver cap 2) — do not spend it on prose.
- **Prompts are self-contained.** The implementer sees only its own prompt and
  the repo, never the project goal, so each one names paths, functions and
  acceptance criteria.

## Writing your own

`main.py code plan "<goal>" <repo>` drafts one with Kimi, or use the
dashboard's **+ New project** (JSON tab) to write tasks directly. Either way,
`--dry-run` it and check the resolved routing before spending tokens.

Schema reference: [taskfile-schema.md](taskfile-schema.md).
