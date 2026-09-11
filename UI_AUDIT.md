# ARC Orchestrator — UI audit and improvement backlog

**Scope:** `static/index.html` (Projects console), `static/usage.html`, `static/phone.html`, and the
`dashboard.py` endpoints that feed them.
**Method:** read the three pages line by line; fetched every API the pages use (and several they
don't) with `curl` against the live server on :8787; measured payload sizes and timings; computed
SVG geometry and WCAG contrast from the real payloads.
**Audited:** 2026-09-10, 10:20–10:35 local.

### Read this first — the files moved while I audited them

`static/index.html` grew from 946 → 1031 lines during the audit (10:23 → 10:32) and `dashboard.py`
changed twice. Three task files are **in flight right now** and already cover part of this backlog:

| task file | covers |
|---|---|
| `~/tasks/ui-accessibility.json` | keyboard/ARIA/focus on `index.html`; `@media` for `usage.html` |
| `~/tasks/task-level-actions.json` | `POST /api/projects/retry-task` + a retry button on failed rows |
| `~/tasks/ui-shared-helpers.json` | extracting duplicated JS helpers into `static/common.js` |

Items below that overlap are marked **[IN FLIGHT]** with the part that is *not* covered. Line numbers
are from the 10:32 snapshot of `index.html` (1031 lines) and the 10:30 snapshot of `dashboard.py`
(1785 lines) — verify before editing.

Everything stated as a number was measured. Where I am inferring, the item says so.

---

## QUICK WINS

### 1. `loadProjects()` is called five times and never defined — S
**File:** `static/index.html:497, 499, 505, 512, 945`
Every one of the new archive/phase controls calls `loadProjects()`. There is no such function
(`grep -n "function loadProjects"` → nothing; the poller is `pollProjects()`). Clicking a phase
filter, the *show/hide done* toggle, *archive all done*, a per-card *archive*, or *show archived*
throws `ReferenceError` and the list never updates.
**Do:** `const loadProjects = pollProjects;` next to the other helpers, or rename the five call
sites. Then click each of the five controls once.

### 2. Dead repo-grouping code left inside `renderProjects()`, and the header count is wrong — S
**File:** `static/index.html:448–465` (`visibleProjects()`, `vis`, `groups`, `ordered`)
The phase rewrite left the old repo-grouping block in place: `vis`, `groups` and `ordered` are
computed on every 5s render and thrown away. Worse, `#proj-meta` is filled from `vis` (the old
project/repo filter) while the list renders `visible` (the phase/archive filter), so the count in
the panel header disagrees with the cards below it — right now it reads `(21)` over 8 visible cards.
**Do:** delete `visibleProjects()`, `vis`, `groups`, `ordered`; set `#proj-meta` from `visible.length`
and `PROJECTS.filter(p => !p.archived).length`.

### 3. `prow()` and the compact/comfort pills are now unreachable — S
**File:** `static/index.html:155–156` (buttons), `288, 315, 321–330` (`DENSE`), `406–419` (`prow`)
The phase renderer always calls `card()`. `prow()` is dead, so the two pills in the filter bar do
nothing visible, and the `#d=comfort` hash parameter is a no-op — a user who clicks "compact" gets no
feedback at all.
**Do:** either honour `DENSE` inside the phase render (`DENSE ? prow(...) : card(...)`) — which is
the cheapest fix for the clutter complaint — or delete the pills, `prow`, `DENSE`, and the hash key.
Do not leave both.

### 4. The Agents panel shows the wrong attempt number and leaks `-xN` task ids — S
**File:** `static/index.html:526` (`attemptOf`), `577–590` (`renderAgents`)
`attemptOf()` parses `-(\d+)\.jsonl$` from the *transcript filename*, which is the per-attempt run
counter, not the attempt. Observed in the live `/api/agents` payload: transcript
`pr-docs-x5-reviewer-1.jsonl` is attempt **5**, and the panel would label it "attempt 1".
Separately, `renderAgents` prints `a.task` raw, so the operator sees `api-retry-task-x1` —
`phone.html:128` already strips this with `baseOf()`.
**Do:** copy phone's `attOf`/`baseOf` (`/-x(\d+)$/` on `a.task`) into index; show `task` + `×N`.

### 5. Fleet "recent problems" never clears and has no age window — S
**File:** `dashboard.py:1456–1475` (`_health`), rendered at `static/index.html:606+`
`problems` is "the last 25 matching events", with no timestamp cutoff and no acknowledgement. Right
now the panel permanently reads **recent problems (2)** and lists a `task.failed f1` and a
`graph.draining: 'files_hint'` from **8.7 hours ago**, both from an unrelated smoke run. A badge that
is never zero is a badge nobody reads.
**Do:** filter to a window (2h is a reasonable default), keep an "older problems" disclosure, and
render the count as `0` (dimmed) when the window is clean. Add the taskfile/project link so a problem
is clickable (see item 23).

### 6. `.pill` gives `cursor: pointer` to read-only readouts — S
**File:** `static/index.html:21`
`#tot-tokens`, `#tot-split`, `#tot-today` and `#health-pill` are static `<span class="pill">`
readouts, but `.pill` sets `cursor:pointer` and `:hover` recolours them, so four header elements
advertise a click that does not exist.
**Do:** split the rule — `.pill` (static) and `.pill.act`/`button.pill` (interactive, keeps the
cursor and hover). Then make the *today* pill actually clickable (→ filter to `attention`), because
that is the click people will try.

### 7. Dead CSS and dead markup — S
**Files:** `static/index.html:33` `.projgrid` (never used; the grid class is `.repogrid` at :81),
`:11` `--run: #e3b341` (declared, zero `var(--run)` references in the file), `:157`
`<span class="hint" id="f-hint">` (never written to by any code path).
**Do:** delete all three. `--run` in particular is a trap: it looks like the "running" status colour
but the running colour is `--accent` (`STATUSC.running = "#58a6ff"`).

### 8. `/api/usage` ships a 69 KB time series that nothing draws — S (backend) / M (chart)
**File:** `dashboard.py:493` (`_usage`), consumed by `static/usage.html:366`
Measured: `/api/usage?range=24h` = **76,734 B**, of which `series` is **69,153 B (90.1 %)** —
1,445 buckets across 5 families, mostly all-zero — plus `daily` (30 entries, 3,360 B). `usage.html`
contains **zero** `<svg>`/`<canvas>` elements; it never touches `series` or `daily`. Only
`phone.html:293` reads `series`, and only for `range=1h`, only to sum one number.
At the page's 5s poll that is ~15 KB/s ≈ **55 MB/hour** of data nothing renders.
**Do:** gate it — `?series=1` (default off), and have `phone.html` ask for the sum instead. Then
either draw the chart (item 32) or drop `daily` entirely.

### 9. Static HTML is served `no-store` — S
**File:** `dashboard.py:1616–1625` (`Handler._file`)
`index.html` is 62,366 B and every navigation between Projects / Usage / Phone re-downloads it in
full, because `_file()` sets `Cache-Control: no-store` the same as the JSON handler.
**Do:** send an `ETag` (hash of bytes) + `Cache-Control: no-cache` and answer `304` on match. Keep
`no-store` for `/api/*` only.

### 10. `/api/graphs` rebuilds an `ArcPool` and a `Store` on every request — S
**File:** `dashboard.py:1585–1596` (`_build_graph_topologies`), polled at `static/index.html:1028`
Measured 385 ms on the first call, ~1.4 ms warm. The topology is static for the process lifetime, yet
the page re-fetches it every 30 s forever.
**Do:** compute once into a module-level cache; fetch once on the client (drop
`setInterval(pollGraphs, 30000)`).

### 11. Nothing pauses polling when the tab is hidden — S
**Files:** all three (`grep -c visibilitychange` → 0, 0, 0)
`index.html` runs up to 8 timers: projects 5 s, fleet 5 s, agents 4.5 s, health 6 s, events 8 s,
graphs 30 s, detail 5 s, transcript 3 s, plus a 1 s elapsed ticker. Measured steady state with the
console open and the fleet busy: `/api/projects` 38 KB/5 s + `/api/agents` 12 KB/5 s + fleet + health
≈ **10 KB/s ≈ 36 MB/hour, per open tab**, continuing in background tabs.
**Do:** one `visibilitychange` handler that clears the intervals on hide and re-polls immediately on
show. Same three lines in all three pages (put it in `common.js` — item 29).

### 12. `index.html` has no media queries at all, and its grid overflows below 392 px — S
**File:** `static/index.html` (`grep -c "@media"` → **0**), `.repogrid` at `:81`
`repeat(auto-fill, minmax(360px, 1fr))` cannot shrink below 360 px; with `main`'s `14px 16px`
padding the page scrolls horizontally on any viewport under ~392 px. The sticky `<header>` also
wraps to 5–6 rows on a phone (nav + h1 + 4 pills + agent chips + the filter bar) and eats the screen.
`usage.html` has a `@media (max-width: 640px)` block; `index.html` has nothing.
**Do:** `minmax(min(360px, 100%), 1fr)`, and a `@media (max-width: 700px)` block that hides the
secondary header pills, drops the filter bar to one row, and unsticks the header.
**[IN FLIGHT — partially]** `responsive-usage` fixes `usage.html` only; `index.html` and `phone.html`
are not covered by any queued task. That task **landed at 10:36, mid-audit** — `usage.html` now has a
`.tblwrap { overflow-x:auto }` around the families table and a `@media (max-width: 700px)` block. Two
follow-ups on it: (a) it duplicates `.modelgrid { grid-template-columns: 1fr }`, which the existing
`@media (max-width: 640px)` block at `:95` already sets — merge the two blocks or they will drift;
(b) `body { overflow-x: hidden }` at `:103` *masks* horizontal overflow rather than removing it,
which hides the symptom of any future wide element.

### 13. There are no focus styles anywhere — S
**Files:** `index.html`, `usage.html`, `phone.html` — `grep -c ":focus"` → **0, 0, 0**
Every interactive element (real `<button>`s included) is invisible to a keyboard user.
**Do:** one rule per page (or in `common.js`'s stylesheet): `:focus-visible { outline: 2px solid
var(--accent); outline-offset: 2px; }`.
**[IN FLIGHT]** `a11y-index` covers `index.html`. **Not covered:** `usage.html` and `phone.html`.

### 14. The closed transcript drawer stays in the tab order — S
**File:** `static/index.html:66–71` (`.drawer` / `.drawer.open`)
The drawer is hidden with `transform: translateX(100%)`, never `display:none`. Its close button and
`<pre>` remain focusable and screen-reader-visible at all times, so tabbing off the header lands in
an off-screen panel.
**Do:** add `inert` (or `visibility:hidden` + `pointer-events:none`) when `.open` is absent, and
restore focus to the trigger on close.

### 15. Colour-only status at failing contrast, plus sub-11px type — S
**Measured (WCAG 2.1):**
- pending `#484f58` on `#0d1117` = **2.28:1** — this is the fill of the status dots
  (`STATUSC.pending`, `.dot`) and the border of `.chip.pending`; non-text UI needs 3:1 (1.4.11).
- `usage.html:80` `.capflag.over` white on `--err` = **3.35:1** — fails AA. (The 10:36 responsive
  change raised it from 10 px to 12 px under 700 px only; 12 px is still "small text" for AA, so the
  ratio still needs fixing, and above 700 px it is still 10 px.)
- `phone.html` uses 9.5 px (`.s-label`, `.tstate`) and 10 px (six rules) as its *primary* labels.
- Everything else is fine: `--dim` 5.6–6.2:1, `--ok` 7.45:1, `--accent` 7.49:1, `--err` 5.65:1.
**Do:** lift `pending` to ≈`#6e7681`, darken `.capflag.over` text to `#000` or raise it to 12 px,
floor phone type at 11 px.

### 16. Every mutation goes through `alert()`/`confirm()`, and the run log path is then lost — S/M
**File:** `static/index.html:504, 512, 941, 974, 976, 983, 985`
Six blocking dialogs. `alert()` freezes the polling loop, is unstyled, and on the Phone view is a
system modal over a 12 px UI. Worse: starting a run reports `started (pid N) log: logs/<name>.log`
in an alert — dismiss it and there is no way to find that log again anywhere in the UI.
**Do:** replace with an inline confirm row inside the panel header, and keep a persistent "last
action" line on the detail panel with the pid, the log name, and a link to a `/api/log?file=` tail
(the transcript drawer already does exactly this for harness transcripts).

---

## STRUCTURAL

### 17. The project card states the same fact four times — M
**File:** `static/index.html:420–447` (`card()`)
For every card the status counts are encoded as: (a) the three-segment `.pbar`, (b) the text line
`3/3 merged`, (c) the chip row `merged 3`, and (d) the colours of the mini-DAG node borders. Four
encodings, one fact. `p.n_tasks` is also printed twice (`3/3` and `3 tasks`).
**Do:** keep the progress bar plus one text line; drop the chip row (it is the least legible) and
let the phase heading carry the state. This alone removes ~2 rows from 21 cards.

### 18. The mini-DAG is the clutter engine — M
**File:** `static/index.html:354–401` (`taskDag`, `mini` branch), called from `card()` at `:425`
Computed from the real `/api/projects` payload, at a 336 px card interior:

| project | cols | viewBox W | rendered height |
|---|---|---|---|
| `selftest-untested-modules` | 1 | 144 | **420 px** |
| `dag-chaining`, `projects-ui-and-patterns`, `parallel-proof` | 1 | 144 | 224 px |
| `dashboard-selftest` (1 task!) | 1 | 144 | 126 px |
| `arc-governance-docs` | 2 | 322 | 232 px |
| `minecraft-web-voxel-extensions` | 5 | 856 | 21 px, labels scaled 9px → **3.5px** |

Total mini-DAG height across 18 cards: **≈2,310 px**. Because the SVG is `width:100%` with a
`viewBox`, a narrow graph is *scaled up* (one task becomes a 300 px-wide box) and a wide graph is
scaled down past legibility. It also makes every grid row as tall as its tallest card.
**Do:** fix the band — render the mini-DAG into a constant-height strip (~56 px) with
`preserveAspectRatio="xMinYMid slice"` inside an `overflow-x:auto` wrapper, or drop it below 3 tasks
and keep only the `.dots` strip that `prow()` already builds. This is the single highest-value change
for "the Projects section looks too cluttered".

### 19. Card element diet: 15 element types, ~13 rendered per card — M
**File:** `static/index.html:420–447`
Inventory: title, LIVE badge, progress bar, progress text, mini-DAG, status chips (1–3), token stat,
agent-time stat, task-count stat, model tags (1–3), reviewer tags (1–2), error note, filename,
relative time, "running (pid N)", archive button. A typical done project renders 13 of them.
Of these, the operator scanning for "what needs me" uses: title, phase, progress, age. The filename
(`engine-hardening.json` under the title "Engine hardening") and the reviewer tags are pure noise at
list level.
**Do:** target six elements — title, LIVE/phase, progress bar + count, one cost stat, age, actions.
Move models/reviewers/filename/agent-time to the detail view, which has room.

### 20. `statuses` counts rows that are not in the task file — S/M
**File:** `dashboard.py:1000–1010` (`_projects`)
`statuses` is built from every `code_tasks` row whose `taskfile` matches, while `progress` is built
from the task ids in the file. Observed: `dashboard-ui-recovery.json` has `n_tasks: 1` and
`statuses: {"merged": 2}`, and `escalation-and-nav.json` has `n_tasks: 1` with
`{"merged": 1, "failed": 1}` — the card shows "1/1 merged" beside chips claiming 2 tasks, and the
`failed` count in `card()` comes from the wrong set, so the red bar segment can exceed 100 %.
**Do:** count `statuses` only for `r["id"] in idset` (the variable already exists two lines above).

### 21. Duplicate task files double-count tokens — M
**Observed in `/api/projects`:** `minecraft-web-voxel-extensions.json` and
`minecraft-web-voxel-extensions-fix1.json` cover the same 5 task ids and both report exactly
**3,599,940 tokens / 5 merged**. Per-task stats are keyed by task id
(`dashboard.py:938 ev_stats[base]`), so any repo- or fleet-level sum over projects counts that work
twice. The repo header ("N projects · N tasks · N tok") is therefore wrong for that repo.
**Do:** detect task-id overlap in `_projects` and mark the older file `superseded_by`; render it as a
sub-line of the newer card rather than a peer, and exclude it from sums.

### 22. Pool-level agents (the planner) are invisible everywhere — M
**File:** `static/index.html:531` — `AGENTS = (d.agents || []).filter(a => a.task)`
`/api/agents` returns two kinds of row (`dashboard.py:392–470`): driver rows (harness runs, with
`task`) and pool rows (`task: null`, carrying `purpose`, e.g. the Kimi planner behind
"+ New project", research and critique calls). Index drops the second kind, and the fleet gauges
count `drivers` only. So after clicking *Plan project* — a step the modal itself says "takes a
minute" — the console shows no agent, no chip, and no gauge movement. The only feedback is a line of
text inside the modal.
**Do:** render pool rows in the Agents panel with `purpose` in place of `task`, styled as secondary;
include them in the header chips.

### 23. The finished-agents view needs filters and links, not just a list — M
**File:** `static/index.html:552–571` (`renderRecent`), fed by `dashboard.py:1195–1201`
(`_agents` → `recent`, currently 40 rows, 12 KB)
This is the operator's stated ask and it has just landed, so treat this as the next iteration rather
than a defect. As built, the *finished* tab is a flat 40-row list with no grouping, no outcome
filter, no project column, no "load more", and no way to get from a failed run to the project it
belongs to. The model filter (`FILT.m`) applies, but the project filter (`FILT.p`) does not.
**Do:** group by task id (so 5 attempts of `pr-docs` collapse to one expandable row), add
pass/fail/all segments, add a project column that opens the detail view, and honour `FILT.p`. Cap and
paginate server-side with `?limit=&before=`.

### 24. Three endpoints are served and rendered nowhere — M
**Files:** `dashboard.py:1693` `/api/metrics`, `:1704` `/api/summary`, `:1731` `/api/code`
No page fetches any of them (verified by grepping all three HTML files for `/api/`). `/api/metrics`
is 958 B and carries exactly the numbers the console lacks: `tasks.merge_rate` (**0.8333** right
now), and per-model `runs / ok / failed / avg_seconds / stall_count / termination_count`. A
first-time operator asking "is the fleet doing well?" cannot find out from any page.
**Do:** add a one-line fleet scoreboard to the Fleet panel header (merge rate, tasks today,
worst-performing model) sourced from `/api/metrics` — it is cheap (2.5 ms) and small. Delete
`/api/code` and `/api/summary`, or give them a page.

### 25. "Happening now" is empty for most projects, and says the wrong thing about it — M
**File:** `dashboard.py:1127–1140` (`_project_detail` event scan); `static/index.html:839`
(`renderFeed`)
The feed is built by substring-matching task ids against `logs/events.jsonl`. That file has been
pruned (45 KB now; `logs/events.jsonl.prefilter-bak` is 1.3 MB), so it contains **14 distinct task
ids covering 4 of 21 projects**. Every other project's detail view shows "No events recorded for this
project yet" — which reads as "this never ran" for a project that merged 6 tasks. The match is also a
raw substring test over the whole JSON line, so a short id (`t1`, `f1`, `c1` all exist in the log)
matches unrelated events.
*(Note: the scan itself is cheap — I benchmarked the full 1.3 MB backup at 2.9 ms to read and 2.8 ms
to scan. This is a correctness/copy problem, not a performance one.)*
**Do:** match on parsed `task`/`module` fields with `_xkey()` rather than substring; when no events
survive, fall back to a timeline built from `runs` (which *is* complete — it comes from the DB) and
say "no event history retained for this project".

### 26. Every 5 s re-render destroys the state the operator is using — M
**File:** `static/index.html:847` (`renderTasks`), `:311–320` (`rebuildFilterOptions`/`setOptions`),
`:895` (`pollTranscript`)
- `renderTasks()` clears `#tasks tbody` and rebuilds every row with `sr.style.display = "none"`, and
  `refreshDetail()` runs every 5 s — so an expanded run-history sub-row **closes itself while you
  read it**.
- `rebuildFilterOptions()` rewrites the three `<select>` elements' `innerHTML` on every projects and
  agents poll; an open dropdown closes.
- `pollTranscript()` replaces `pre.textContent` wholesale every 3 s, destroying any text selection
  (you cannot copy an error out of the drawer).
**Do:** keep a `Set` of expanded task ids and re-apply after render (cheapest fix); rebuild the
`<select>`s only when the option list actually changes; append to the `<pre>` instead of replacing
it (or skip the update when `document.getSelection()` is inside it).

### 27. The detail poll runs four `git` subprocesses and a `gh auth status` every 5 s — M
**File:** `dashboard.py:1079–1099` (`_git_block`), `:1141–1144` (`github_status`),
`gitstore.py:242–281`; poll at `static/index.html:746`
`_git_block` shells out to `git branch`, `git log`, `git worktree list` and `git status --short` on
every detail refresh, and `_project_detail` additionally runs `asyncio.run(gitstore.github_status())`.
Today that measures 7–8 ms only because **no repo here has an `origin`**, so `github_status` returns
after one call. On a repo with a remote it runs `gh auth status` with a **60 s timeout**
(`gitstore.py:267`) on every 5 s poll. This is a latency landmine, not a current defect.
**Do:** cache `_git_block` per repo for ~15 s and `github_status` per repo for ~5 min (it changes
approximately never); both keyed on repo path.

### 28. Payloads carry large fields the UI immediately truncates — S/M
**Files:** `dashboard.py:1145–1150` (`_project_detail` returns `tasks` verbatim);
`static/index.html:874` (renders `t.prompt.slice(0, 260)`)
Measured on `dashboard-ui-polish.json`: 27,422 B total, of which task **prompts are 9,570 B (35 %)**
and `verify_cmd` another 1,155 B — for a UI that shows the first 260 characters of one prompt inside
a collapsed sub-row. `/api/projects` (38 KB) likewise ships `task_ids` (2.9 %) duplicating
`dag.nodes[].id`, and both `done_tokens/done_seconds` and `live_tokens/live_seconds` alongside the
`tokens`/`seconds` totals — no page reads the splits.
**Do:** truncate `prompt` server-side to ~400 chars with a `?full=1` escape hatch for a future
"show prompt" action; drop `task_ids` and the four split fields. Roughly halves the detail payload.

### 29. The three pages have drifted apart — M
**Observed differences:**

| thing | index.html | usage.html | phone.html |
|---|---|---|---|
| `$` | `querySelector` (:277) | `querySelector` (:163) | **`getElementById`** (:112) |
| `esc` | replace ×3 (:296) | replace ×3 (:175) | regex+map (:113) |
| token format | `fmtK` → `177.0M` (:294) | `fmt` → `177.01M` (:170) | `fmt` → `177.01M` (:114) |
| conflict colour | `#f0883e` (:281 `STATUSC`) | n/a | **`#d29922`** (:118 `DOT`) |
| model names | `Kimi K3`, `GLM 5.3` (:278) | `m.pretty` from API | **`K3`, `GLM`, `120B`, `V4`** (:115) |
| "/" is called | "Projects" | "Projects" | "Projects" in nav, **"flows"** in the footer (:107) |
| page title | `ARC Orchestrator · Projects` | `ARC · Usage` | `ARC Phone` |

Same helper names, different behaviour, is the dangerous kind of duplication: a future copy-paste
between pages will compile and be wrong.
**[IN FLIGHT]** `ui-shared-helpers.json` extracts `esc`, `short`, `tick`, `fmtK/fmt` and the family
`COLORS`. **Make sure it also covers:** the status colour map (`STATUSC` vs `DOT` — the conflict
colour genuinely differs), the model short-name map, the nav markup, and the page-title scheme.

### 30. The "Orchestration graphs" panel is the wrong subsystem in the wrong place, and it crops — M
**File:** `static/index.html:712–724` (`renderGraph`), panel at `:186–189`
It renders the static `round` and `build` topologies — the *research/build* pipeline, not code
tasks — as the fourth panel of the projects console, below the fold, refreshed every 30 s, with no
interaction and no live state. A first-time operator reasonably assumes it describes their project.
It is also the only SVG in the codebase with `width`/`height` attributes and `max-width:100%` but
**no `viewBox`**, inside a `#graphs` div with no `overflow-x` — so below ~1,240 px it is *cropped*,
not scaled. Measured widths: `round` = 1,040 px (6 columns), `build` = 870 px (5 columns).
**Do:** add `viewBox="0 0 W H"` and wrap in `overflow-x:auto`; then collapse the panel by default
(`<details>`) or move it to its own `/graphs` page linked from the nav.

### 31. Archiving can bury a project that still needs attention — S/M
**File:** `dashboard.py:1518–1539` (`_archive_project`), filter at `static/index.html:473`
`_archive_project` refuses only while a run owns the task file. The list filter is
`SHOW_ARCHIVED ? p.archived : !p.archived` — archived hides regardless of phase. Live payload right
now: `dag-chaining.json` is `archived: true` **and** `phase: "attention"` (1 failed task), so an
unresolved failure has silently left the console. With "archive all done" as a one-click bulk
action, this will happen more.
**Do:** refuse (or confirm with the failure count) when `phase === "attention"`, and badge the
*show archived* button when the archive contains anything not `done`.

---

## NICE TO HAVE

### 32. Draw the usage chart the data already exists for — M
`usage.html` is a numbers page with no chart at all. `series` (per-family buckets — 60 s for 1h,
300 s for 24h) and `daily` (30 days) are already on the wire (item 8). A stacked area of tokens/min
by family, plus a 30-day bar strip, is the page's whole reason to exist.

### 33. First-run orientation — S
Concrete things a first-time operator cannot find: what the dot colours mean (encoded only in `title`
tooltips — 14 of them in `index.html`, invisible on touch and to keyboard); that a card is clickable
(cursor only); what "Dry run" does (explained only inside a `confirm()` after you click it); where
task files live (mentioned only in the empty state they will never see); what `x2 ⬆1` on a DAG node
means. **Do:** a one-line legend under the Projects header and a `?` disclosure in the Fleet panel.

### 34. Cross-page and cross-panel links — M
Every view is a dead end at its edges: a usage model card does not link to the projects that spent
those tokens; a project detail does not link to usage for its window; the Phone page cannot open a
project; a fleet problem does not link to its task; the Agents panel does not link to a project.

### 35. Driver leases are invisible — S
`/api/health` returns `leases` (3 rows right now) and `runs`; `renderHealth` uses `runs.length` for a
count and ignores `leases` entirely — yet a stale lease is precisely what wedges the fleet at cap.
Show them under the capacity gauges with an age, since the gauges alone cannot explain a stuck fleet.

### 36. The stale badge does not say what is stale — S
`static/index.html:301–306`: `markFail()` maintains a `FAILS` set of the failing poll names and then
discards it, showing only "stale (retrying…)". Put the names in the `title` (and in the badge text
when there is one failure).

### 37. Mutating POSTs are unauthenticated on `0.0.0.0` — S to note, M to fix
`dashboard.py:1824` binds `0.0.0.0:8787`; `/api/projects/{create,run,stop,archive}` spawn processes,
kill pids and write files with no auth, and the Phone page actively invites LAN use. Out of scope for
a UI backlog, but it belongs on someone's list — a shared-secret header or a bind to the LAN
interface with a token in the URL would be enough.

---

## Appendix A — measurements

All from `curl` against localhost:8787, 2026-09-10 10:20–10:33, 3 runs each, warm.

| endpoint | size | time | polled every | notes |
|---|---|---|---|---|
| `/api/projects` | 38.4 KB | 8–10 ms | 5 s (index), 5 s (phone) | `dag` = **66 %** of it |
| `/api/project?file=` | 26–27 KB | 7–8 ms | 5 s while detail open | prompts = **35 %**; 4 git subprocesses |
| `/api/agents` | 12.1 KB | ~4 ms | 4.5 s | 3 live + 40 recent |
| `/api/usage?range=1h` | 17.7 KB | 9 ms | 5 s | `series` 65 % |
| `/api/usage?range=24h` | **76.7 KB** | 65 ms | 5 s | `series` **90 %**, unrendered |
| `/api/usage?range=7d` | 47.8 KB | 70 ms | 5 s | |
| `/api/usage?range=all` | 8.7 KB | 12 ms | 5 s | (was 178 ms on the earlier process) |
| `/api/events?after=0` | 39.7 KB | 1.7 ms | 8 s | full log on first load |
| `/api/graphs` | 3.2 KB | **385 ms cold**, 1.4 ms warm | 30 s | rebuilt per request |
| `/api/health` | 1.1 KB | 5 ms | 6 s | |
| `/api/fleet` | 1.6 KB | 11 ms | 5 s | 2 s server cache |
| `/api/metrics` | 958 B | 2.5 ms | never | unused |
| `/api/summary` | 2.2 KB | 2.3 ms | never | unused |
| `index.html` | 62.4 KB | — | every navigation | `no-store` |

Nothing exceeds 200 ms warm. Two things exceed 50 KB: `/api/usage?range=24h` (76.7 KB) and
`index.html` (62.4 KB). Aggregate console traffic ≈ **10 KB/s per open tab**, unpaused when hidden.

## Appendix B — what I could not observe, and what I am inferring

- **`gh auth status` latency (item 27):** no repo in this environment has an `origin`, so the slow
  path never executes. The 60 s timeout on a 5 s poll is read from the code, not measured.
- **Live agent rendering:** at the start of the audit `/api/agents` was empty; by the end three
  drivers were running, so I verified the payload shape but did not watch the panel across a full
  task lifecycle (start → stall → escalate → merge). The stalled/`quiet`/`stuck` styling
  (`index.html:118–121`, `.agentrow.stuck`) is unexercised in my observation.
- **Rendered pixel geometry (items 12, 18, 30):** computed from the SVG/CSS maths and the real
  payloads, not from a browser screenshot. The card interior is assumed to be 336 px (360 px track
  minus 12 px padding each side); a different grid width shifts the numbers proportionally, not the
  conclusion.
- **`dag-chaining.json` being archived while in `attention` (item 31):** I observed the state, not
  the action that produced it. It may have been archived individually before its phase changed
  rather than by "archive all done" — the gap in `_archive_project` is real either way.
- **Item 3 (`prow` dead):** true of the 10:32 snapshot. Given the pace of edits, re-check before
  deleting anything.
- I did not run the pages in a browser (no display), did not execute `main.py code run`, and made no
  change to any source file. This report is the only file I wrote.
