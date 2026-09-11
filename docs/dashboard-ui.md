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
| `/api/metrics` | none | Per-model run/token/stall rollup plus a code-task status rollup — see [Metrics endpoint](#metrics-endpoint) |
| `/api/queue` | none | LIVE capacity: `running` and `waiting` attempt rows (task, role, model, seconds, why), one `models` row per model and one `harnesses` row per harness with `cap`/`running`/`waiting`/`free`, and `totals` including `reviewers_waiting` |
| `/api/events` | `after` = 0-based line offset into `logs/events.jsonl`; `since` = epoch seconds (seeds the cursor, first request only) | Batch of parsed events plus `next` offset, `total`, and `reset` flag |
| `/api/graphs` | none | Static node/edge topology of the research-round and build graphs |
| `/api/code` | `file` = path relative to the build output dir | Contents of one generated build file (404 outside the build dir) |

## POST API (JSON body)

| Route | Body | Returns |
|---|---|---|
| `/api/projects/create` | `repo` (required, absolute path under `ARC_REPO_ROOT`, default the operator home), plus either `goal` (3..2000 chars, Kimi-K3 plans it) or `title` + `tasks` list; optional `overwrite` | `mode: plan` with pid/log/taskfile, or `mode: tasks` with written file name |
| `/api/projects/run` | `file` = taskfile name, optional `dry_run` | Spawns `main.py code run [--dry-run]`; pid, log name, `dry_run` flag; 409 if already running |

## Metrics endpoint

`GET /api/metrics` (no params) rolls up all `harness_runs` rows and the
`driver.stalled` / `driver.timeout` events in `logs/events.jsonl` per model,
plus a status rollup over `code_tasks`. Fields:

- `now` — server time (epoch seconds).
- `models[]` — one entry per model, sorted by `runs` descending:
  `model` / `pretty` (raw and display name), `runs`, `ok`, `failed`
  (split by exit code), `avg_seconds` (mean run duration),
  `total_tokens` and `avg_tokens_per_run` (parsed from the run transcripts),
  `stall_count` (`driver.stalled` events — idle-timeout kills),
  `termination_count` (`driver.timeout` events — total-runtime kills).
- `tasks` — `{total, merged, failed, conflict, merge_rate}` over all
  `code_tasks` rows; `merge_rate` is `merged / total`, rounded to 4 places.

```console
$ curl -s localhost:8787/api/metrics | python3 -m json.tool
{
    "now": 1789000000.42,
    "models": [
        {
            "model": "GLM-5.3",
            "pretty": "GLM-5.3",
            "runs": 41,
            "ok": 38,
            "failed": 3,
            "avg_seconds": 512.407,
            "total_tokens": 1834200,
            "avg_tokens_per_run": 44736.6,
            "stall_count": 1,
            "termination_count": 0
        },
        ...
    ],
    "tasks": {"total": 57, "merged": 49, "failed": 5, "conflict": 3, "merge_rate": 0.8596}
}
```

(sample trimmed to one model row.)

## Model slots panel

Answers two questions the rest of the UI could not: **what is holding a model
slot right now, and who is queued behind them.**

- **Capacity cards** — one per harness *and* one per model, pips for held vs
  free. Harness rows come first because the harness is frequently the binding
  ceiling: every opencode model can sit under its own cap while the single
  opencode pool is saturated, which reads as "idle" exactly when it is most
  wrong. Amber = at cap, red = a queue behind it.
- **Attempt rows** — each running and queued attempt with its task, role,
  model, wait time, and *why* it is waiting: `fleet at cap (4/4)`,
  `opencode pool full`, or `waiting for a slot in its own run`.
- **PR reviewers are separated out** in purple, with their own count and a
  "reviewers only" filter. They are the fleet's bottleneck — `PR_REVIEWERS`
  scarce cross-family models per round — and averaging them into one
  "N waiting" number hid which queue was actually stuck.

Data comes from `dashboard._queue`: **running** from the `driver_leases` table
(that IS the definition of occupying a slot, and it carries a pid so a killed
run cannot pin phantom capacity), **waiting** from pairing each
`driver.queued` with its terminal event. An attempt holding its model lease
while queued for the harness lease counts as waiting, not running — it is not
running until it holds every slot it needs.

## Notes

- Live transcripts tail `logs/harness/*.jsonl`; the drawer polls a run's
  transcript every ~3 s and the project list every ~5 s.
- Token totals exclude dry-run events (model/family `dry-run` is skipped).
- `/api/events` returns at most `MAX_EVENTS_PER_RESPONSE` lines per call; use
  the returned `next` offset to poll incrementally.
- Each page is a self-contained single HTML file (no build step, no external
  assets).