# Dashboard services

The projects console is one process (`main.py serve`). The browser is
already a separate frontend (`static/`). Pagination, conditional
refresh, and skeleton loading are **in-process services** with a small
HTTP contract, not extra network services.

A separate process per concern would add a hop on every 3–5 s poll, a
second failure domain, and another listener on the unauthenticated
dashboard port (Rule 6b). That is the opposite of a fast first paint.
The split that does help is a stable boundary:

| Service | Backend | Frontend | Contract |
|---|---|---|---|
| Page | `dashboard_services/page.py` | `static/services/page.js` | `limit` + `offset` opt in. Absent both, the list is unchanged. Response gains `page: {key, limit, offset, total, next_offset}`. |
| Refresh | `dashboard_services/refresh.py` | `static/services/refresh.js` | `ETag` / `If-None-Match`. A match is **304** and an empty body. Top-level `now`/`ts` and duration fields `idle_s`, `last_event_s`, `seconds` are not part of the hash. Nested event `ts` values are. |
| Skeleton | (no server state) | `static/services/skeleton.js` plus `.sk` CSS | First paint only. A later poll never puts the skeleton back. A failed poll keeps the last good render and raises the stale badge. |

## What is paged

- `GET /api/github?limit=&offset=` slices `prs`. The panel starts at 10 and asks for 10 more. `open_count` is the full list, so the header does not shrink with the page.
- `GET /api/activity?limit=&offset=` skips `offset` matching events, newest first.
- `GET /api/projects?limit=&offset=` slices `projects`. The console does not send this: phase filters need the full list.
- `GET /api/agents?limit=&offset=` slices `recent` only. Live `agents` stays complete so a running harness cannot fall off the page.

## What is conditional

`/api/queue`, `/api/projects`, `/api/work-status`, `/api/agents`, `/api/github`, `/api/activity`.

Clients that do not send `If-None-Match` (the phone page, tests, `curl`) still receive the full JSON. The desktop console sends the validator and, on 304, leaves the DOM alone. Ages move on a one-second clock (`[data-since]`) so a quiet timer does not require a refetch.

Polls pause while the tab is hidden and run once when it becomes visible.

## Why the live task map is not paged

The map and the project list answer "what is running". A page that hid a stalled task would be a faster wrong answer. Those payloads skip the DOM rewrite when the template is unchanged, and they 304 when only the clock moved.
