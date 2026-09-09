# Dashboard UI + API reference

Quick reference for the dashboard server (`dashboard.py`, `main.py serve`,
default port 8787). Routes listed are exactly what `Handler.do_GET` /
`Handler.do_POST` implement.

## Pages

| Path | Served from | Purpose |
|---|---|---|
| `/` | `static/index.html` | Projects console: taskfile list, DAG status, drawer with events/git/transcript, plan/run controls |
| `/usage.html` | `static/usage.html` | Usage view: per-model/family requests, tokens, latency, time series |
| `/phone.html` | `static/phone.html` | Small-screen variant of the projects console for phones |

## GET API

| Route | Params | Returns |
|---|---|---|
| `/api/usage` | `range` = `1h` (default) \| `24h` \| `7d` \| `all` | Totals, per-model and per-family rows, daily buckets, time series |
| `/api/fleet` | none | All-history code-fleet totals (`requests`, `ok`, `errors`, `tokens`, `prompt_tokens`, `completion_tokens`) plus one row per model with `account_cap` and `driver_cap`; cached ~2 s |
| `/api/projects` | none | All taskfiles in `~/tasks` with task DAG, statuses, progress, token/second totals |
| `/api/project` | `file` = taskfile name | Taskfile detail: task defs, `code_tasks` rows, harness runs, recent events, git block |
| `/api/agents` | none | In-flight agent runs plus dashboard-launched processes from the launch registry |
| `/api/transcript` | `file` = `<task>-<role>-<attempt>.jsonl`, `tail` = 1..1000 (default 200) | Last N lines of a harness transcript in `logs/harness/` |
| `/api/summary` | none | Research/build stats, critique matrix, family limits, event-log and build-dir paths |
| `/api/events` | `after` = 0-based line offset into `logs/events.jsonl` | Batch of parsed events plus `next` offset and `reset` flag |
| `/api/graphs` | none | Static node/edge topology of the research-round and build graphs |
| `/api/code` | `file` = path relative to the build output dir | Contents of one generated build file (404 outside the build dir) |

## POST API (JSON body)

| Route | Body | Returns |
|---|---|---|
| `/api/projects/create` | `repo` (required, absolute path under `/home/proxyie/`), plus either `goal` (3..2000 chars, Kimi-K3 plans it) or `title` + `tasks` list; optional `overwrite` | `mode: plan` with pid/log/taskfile, or `mode: tasks` with written file name |
| `/api/projects/run` | `file` = taskfile name, optional `dry_run` | Spawns `main.py code run [--dry-run]`; pid, log name, `dry_run` flag; 409 if already running |

## Notes

- Live transcripts tail `logs/harness/*.jsonl`; the drawer polls a run's
  transcript every ~3 s and the project list every ~5 s.
- Token totals exclude dry-run events (model/family `dry-run` is skipped).
- `/api/events` returns at most `MAX_EVENTS_PER_RESPONSE` lines per call; use
  the returned `next` offset to poll incrementally.
- Each page is a self-contained single HTML file (no build step, no external
  assets).