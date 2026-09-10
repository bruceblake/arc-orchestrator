# Planner diagnosis — `main.py code plan` produces no task file

Investigated read-only on 2026-09-10. No source file was modified. No `main.py code plan`
was executed; all conclusions come from existing logs, transcripts and static reading.

**One-line verdict:** the planner is not hanging and the model is not failing. The model
returns a complete, valid plan; the orchestrator then throws it away, because
`drivers.parse_transcript()` truncates the harness reply to its **last 3000 characters**
before `_extract_plan_json()` ever sees it, and a real plan is 6.5–11.5 KB. Extraction
returns `None` and `plan_tasks()` raises `RuntimeError`, writing no file.

---

## (a) What the evidence shows happened

### The run did terminate — with an exception, not a hang

`logs/plan-add-a-api-version-endpoint-to-dashboard.log` is not empty. It contains a
traceback:

```
  File "/home/proxyie/arc-orchestrator/code_tasks.py", line 641, in plan_tasks
    raise RuntimeError(f"planner produced no usable JSON; transcript: {res.transcript_path}")
RuntimeError: planner produced no usable JSON; transcript: /home/proxyie/arc-orchestrator/logs/harness/plan-planner-1.jsonl
```

The earlier attempt, `logs/plan-please-polish-the-new-project-page-and-m.log`, ends with the
identical error against `plan-planner-2.jsonl`. Both plan runs on record failed the same way.

### The driver reported success

From `logs/events.jsonl` (local time), the 23:13 failure:

```
09-09 23:07:05  driver.start   harness=kimi model=Kimi-K3 role=planner task=plan attempt=1 resume=false
09-09 23:08:05  driver.progress bytes=13422 idle_s=7.4   elapsed_s=60.0  state=S cpu_s=2.31 sockets=1
09-09 23:09:06  driver.progress bytes=13422 idle_s=67.5  elapsed_s=120.1 cpu_delta_s=0.64
09-09 23:10:06  driver.progress bytes=17309 idle_s=57.1  elapsed_s=180.1 cpu_delta_s=0.56
09-09 23:11:06  driver.progress bytes=18448 idle_s=12.9  elapsed_s=240.1 cpu_delta_s=0.63
09-09 23:12:06  driver.progress bytes=18448 idle_s=73.0  elapsed_s=300.2 cpu_delta_s=0.54
09-09 23:13:06  driver.progress bytes=18448 idle_s=133.0 elapsed_s=360.3 cpu_delta_s=0.49
09-09 23:13:31  driver.done     seconds=385.7
```

`driver.done`, exit code 0, 385.7 s. Peak idle was 133 s against a 420 s
`DRIVER_IDLE_TIMEOUT` — never close to a stall. The failure is downstream of the driver.

### The kimi session log shows a completely clean run

`~/.kimi-code/sessions/wd_arc-orchestrator_586a9cff578e/session_af6a8349-9c23-4e89-9be1-4e651851abc3/logs/kimi-code.log`
is 13 lines, all `INFO`, no `WARN`, no `ERROR`:

```
2026-09-10T03:07:06.808Z INFO  llm config    provider=openai model=Kimi-K3 modelAlias=arc/kimi-k3-fleet thinkingEffort=off systemPromptChars=38247 toolCount=25
2026-09-10T03:07:50.944Z INFO  llm response  turnStep=0.1 ttftMs=1998 streamDurationMs=42134  outputTokens=1205
2026-09-10T03:07:58.491Z INFO  llm response  turnStep=0.2 ttftMs=845  streamDurationMs=6645   outputTokens=197
2026-09-10T03:09:08.913Z INFO  llm response  turnStep=0.3 ttftMs=853  streamDurationMs=69549  outputTokens=1760
2026-09-10T03:10:32.260Z INFO  llm response  turnStep=0.4 ttftMs=1743 streamDurationMs=81585  outputTokens=1751
2026-09-10T03:10:53.179Z INFO  llm response  turnStep=0.5 ttftMs=514  streamDurationMs=20366  outputTokens=519
2026-09-10T03:13:30.443Z INFO  llm response  turnStep=0.6 ttftMs=574  streamDurationMs=156679 outputTokens=3380
```

Six requests, six answers, zero retries, zero API errors. `state.json` for that session
records `"lastTurnReason":"completed"`. (Log timestamps are UTC; 03:07:06Z = 23:07:06 local.)

The elapsed time is fully accounted for by decode speed: 8812 output tokens across
6 sequential agent turns at ~21 tok/s. The final answer alone took 156.7 s to stream. The
"~9 s direct API request" comparison is not comparable — that is one turn with a small
prompt; this is a 6-turn agent loop with a 38 KB system prompt and 25 tools.

### The model DID emit a valid plan

`logs/harness/plan-planner-1.jsonl` record 15 is the final assistant message, 6858 chars,
ending:

```
..."files_hint": ["static/index.html"], "deps": []}\n]}}
```

Running the repo's own `_extract_plan_json()` (lifted verbatim out of `code_tasks.py`)
against that message content:

```
EXTRACT ON FINAL MSG: FOUND len=6555
tasks: ['version-endpoint', 'version-header-ui']
```

Valid JSON, correct schema, two well-formed tasks. **The model did its job.**

### The orchestrator threw it away — measured

Running the repo's own `parse_transcript()` (lifted verbatim out of `drivers.py`) against the
transcript bytes:

```
sid  = session_af6a8349-9c23-4e89-9be1-4e651851abc3
len(text) = 3000
head: 'le": "Show deployed version in static/index.html header",\n   "prompt": ...'
tail: '..."deps": []}\n]}}To resume this session: kimi -r session_af6a8349-...'
extract on res.text -> None
```

The returned `res.text` starts **mid-word, mid-JSON-string** (`le": "Show deployed...`).
The opening `{"project":` is gone. There is no balanced `{...}` span left, so
`_extract_plan_json` returns `None`, and line 641 raises.

For contrast, on the untruncated concatenation of the same transcript:

```
len(full joined) = 21677
extract on FULL -> FOUND len=6555
```

The single line responsible, `drivers.py:312`:

```python
return sid_holder[0], ("".join(texts) or raw)[-3000:]
```

### Confirming datapoint: small plans DO work

`~/tasks/add-a-src-style-css-to-the-multi-harness.json` is **1609 bytes** and its mtime is
`2026-09-08 18:44:14`, matching exactly `09-08 18:44:14 driver.done ... role=planner seconds=150.0`.
A plan that fits inside the 3000-char window survives; a 6555-char one does not. This is
consistent with the bug being a size threshold rather than a general planner breakage.

### The 12:06 run: same root cause, plus a second latent defect

`09-09 11:33:51 driver.start` → `09-09 11:50:07 driver.error "kimi timed out after 900.0s"`
→ `09-09 11:50:09 driver.start attempt=2` → `09-09 12:06:04 driver.done seconds=897.5`
→ same `RuntimeError`. Total ~33 min. `DRIVER_TIMEOUT` was 900 s then; it is 2700 s now
(`config.py:120`). Attempt 1's session
(`session_5a762de7-.../logs/kimi-code.log`) shows the process was mid-generation when killed:
its last line is `15:47:07.052Z INFO llm request turnStep=0.9` with no matching response
before the 15:50:07Z kill. That session had one 442 s response (8565 tokens) — slow, not broken.

Attempt 2's transcript (`plan-planner-2.jsonl`) exposes a **second** bug. That run went
through kimi plan mode (record 25 is a tool result: `"Exited plan mode. Plan mode deactivated."`),
so the transcript contains *two* plan-shaped JSON blobs: an abbreviated one inside a
```` ```json ```` fence in the plan-mode markdown, and the real one in the final message.
`_extract_plan_json` takes the **first** match:

```
FIRST-match on joined :  len=825   ids=[fs-browse-api, new-project-modal-overhaul, docs-fs-picker]  promptlens=[3, 3, 3]
LAST-match  on joined :  len=11476 ids=[fs-browse-api, new-project-modal-overhaul, docs-fs-picker]  promptlens=[2536, 4170, 1875]
```

`promptlens=[3,3,3]` — the first match's task prompts are the literal string `"..."`. Had the
truncation bug not fired first, this run would have written a **silently useless task file**
whose implementer prompts are three dots. (Plan mode is now off — `~/.kimi-code/config.toml`
line 3 `default_plan_mode = false`, set 09-09 22:09 — so this path is currently dormant,
but the extraction preference is still wrong.)

---

## (b) Root cause

**MEASURED (high confidence):**

- `drivers.parse_transcript()` truncates the harness reply to the last 3000 characters
  (`drivers.py:312`). `DriverResult.text` is therefore a tail, not a reply.
- The planner's output is 6555 and 11476 characters in the two runs on record — both far
  over that window — so the opening `{` of the plan is always cut off.
- `code_tasks._extract_plan_json()` requires a *balanced* span, so a headless tail can never
  match. It returns `None`.
- `code_tasks.plan_tasks()` line 640-641 turns `None` into an unconditional `RuntimeError`
  and returns before writing anything. No file is created; nothing is retried; the raw model
  output is not preserved anywhere except the transcript.
- The model produced correct, complete, schema-valid JSON in both failed runs.

**MEASURED, secondary:** `_extract_plan_json` returns the **first** schema-matching span.
When the transcript contains an earlier abbreviated copy (plan-mode summaries, worked
examples), the wrong one wins and would be written to disk unvalidated.

**INFERRED:** the reason review works and planning does not is the asymmetry between the two
parsers. `_parse_verdict` (`code_tasks.py:155`) iterates `reversed(spans)` — last match wins
— and a reviewer verdict is a few hundred bytes, so it comfortably fits the 3000-char tail.
`_extract_plan_json` takes the first match on a payload an order of magnitude larger. The
3000-char cap was almost certainly sized for verdicts and log-tail display and was never
reconsidered when the planner started reusing the same field. (`git log -S'[-3000:]' -- drivers.py`
shows the line dates to the original commit `9fdd909`.)

**INFERRED:** the "15+ minutes with no output" the operator observed is the 11:33–12:06 run
(2 × ~15 min attempts). The most recent failure (23:07–23:13) was 6.4 min. Either way the
user-visible symptom is identical because of a UI gap: `static/index.html doPlan()` polls for
the task file for 240 s and then shows *"still planning… see logs/… — close this and check
back."* The process is already dead by then. Nothing in the dashboard ever surfaces the
`RuntimeError`; `_prune_registry()` just silently drops the dead pid from `_launch_registry`.

---

## (c) Concrete minimal fix (NOT implemented)

**Primary — one function, `code_tasks.plan_tasks()` (`code_tasks.py:638-641`).**

Stop consuming the truncated `res.text`. `DriverResult` already carries
`transcript_path`, which holds the full untruncated stdout. Replace the extraction source
with the **last non-empty assistant message** read back from that file:

- After `res = await KimiDriver("planner").run(...)`, add a small helper (next to
  `_extract_plan_json`) that reads `res.transcript_path` line by line, JSON-parses each line,
  keeps the last record with `role == "assistant"` and a non-empty string `content`, and
  returns that string.
- Feed that string to `_extract_plan_json`. Fall back to `res.text` only if the transcript is
  unreadable or has no assistant message, so behaviour never regresses for tiny plans.

Verified offline against both failing transcripts: `FIRST-match on last-assistant-msg`
yields `len=6555` (planner-1, correct) and `len=11476` with `promptlens=[2536, 4170, 1875]`
(planner-2, correct — it also sidesteps the plan-mode decoy). This one change fixes both
defects without touching `drivers.py`, whose 3000-char cap is load-bearing for the reviewer
path and for log display.

**Do not** simply delete the `[-3000:]` in `drivers.py:312`: on `plan-planner-2.jsonl` that
alone yields the 825-char decoy with `"prompt": "..."`, i.e. it converts a loud failure into
a silent one. If `drivers.py` is changed anyway, `_extract_plan_json` must switch to
last-match-wins (`for span in reversed(spans)`, mirroring `_parse_verdict`) in the same commit.

**Secondary, cheap, recommended alongside:**

1. `plan_tasks()` — on extraction failure, write the raw reply to
   `logs/plan-raw-<slug>.txt` before raising, and include the reply length in the error.
   A 15-minute Kimi run currently leaves nothing but "no usable JSON".
2. `dashboard._create_project()` / `doPlan()` — the poll loop should notice the pid is gone
   and surface the tail of `logs/plan-<slug>.log`, instead of "still planning… check back"
   for a process that died minutes ago.
3. `drivers.Driver._once` — the transcript path is `f"{task_id or 'adhoc'}-{self.role}-{attempt}.jsonl"`
   and `plan_tasks` hardcodes `task_id="plan"`, so every plan run overwrites
   `plan-planner-1.jsonl`. Two concurrent plans clobber each other's evidence.

---

## (d) Checked and RULED OUT

| Hypothesis | Verdict | Evidence |
|---|---|---|
| ARC's ~310 s `APIConnectionError: terminated` | **Ruled out** for these runs | `grep -rn "terminated\|APIConnectionError" ~/.kimi-code/sessions/wd_arc-orchestrator_586a9cff578e/*/logs/kimi-code.log` → no matches. The failing session's 6 responses took 42/7/70/82/20/157 s, all under 310 s. |
| kimi-code retrying internally at ~5 min/retry | **Ruled out** | Failing session log is 13 lines with zero `WARN`/retry entries. (Other sessions in the same dir *do* show `WARN llm request failed ... 400 status code (no body)` — so the log would have recorded retries had they occurred.) |
| The planner has no timeout | **Ruled out** | `plan_tasks` calls `KimiDriver.run()`, the same `Driver.run` → `_guarded_once` → `_once` → `_pump` path as implement and review. `_pump` (`drivers.py:466-...`) enforces `deadline = t0 + config.DRIVER_TIMEOUT` (2700 s) **and** `config.DRIVER_IDLE_TIMEOUT` (420 s) on every read, and `Driver.run` caps retries at `config.MAX_RETRIES` (4). The planner is bounded identically to every other role. Demonstrated live: `driver.error "kimi timed out after 900.0s"` at 09-09 11:50:07 is that machinery firing on the planner. |
| The planner hangs forever | **Ruled out for the request phase.** Worst case ≈ 5 attempts × 2700 s + backoffs ≈ 3.9 h, then the `DriverError` propagates. **One genuine exception:** `_lease_acquire` (`drivers.py:68-83`) is `while True:` with `await asyncio.sleep(20)` and no deadline, and it runs *before* `_once` sets `t0`. If Kimi-K3 sits at its cap of 2 (`config._MODEL_DRIVER_CAP`), a plan can queue indefinitely, emitting only `driver.cap_wait` ~every 60 s. That did **not** happen in the runs examined (first `driver.progress` at `elapsed_s=60.0`, one minute after `driver.start` — no cap wait). |
| Model failed to produce JSON / hit output limits / stream cut off | **Ruled out** | Final assistant message is 6858 chars ending `]}}`; parses cleanly; two schema-valid tasks. `lastTurnReason: "completed"`. |
| Bad/unwritable `TASKS_DIR` | **Ruled out** | Execution never reaches the write — the exception is at line 641, the `mkdir`/`write_text` are 643-645. `~/tasks/` is writable and holds 25+ files, several written the same day. |
| kimi plan mode | **Not the cause of the current failure**; historically a contributing factor to the 12:06 run only. `~/.kimi-code/config.toml:3` is now `default_plan_mode = false` (set 09-09 22:09), and `plan-planner-1.jsonl` shows no `ExitPlanMode`. Note the guard `config.kimi_plan_mode_on()` sits at `main.py:250`, inside the taskfile-run branch **after** the `code_cmd == "plan"` branch returns at ~line 203 — so `code plan` is not covered by it. |
| Prompt too long / prompt malformed | **Ruled out** | The prompt is ~2.6 KB and the model answered it correctly. `systemPromptChars=38247` is kimi-code's own system prompt, unchanged across working and failing runs. |
| Dashboard `_create_project` passing bad args | **Ruled out** | `dashboard.py:1214` spawns `[py, "main.py", "code", "plan", goal, str(repo)]` and the traceback shows `plan_tasks` reached with a resolved repo path and a working session. |

---

## Ambiguities / things I could not settle

- I could not reconstruct which run produced `~/tasks/minecraft-web-voxel-extensions.json`
  (11523 bytes, mtime 09-08 19:31:09). The nearest planner `driver.done` is 09-08 19:26:33
  (546 s) — five minutes earlier — so it is more likely hand-authored or pasted than planner
  output. If it *was* planner output, an 11.5 KB plan surviving the 3000-char window would
  contradict the size hypothesis. I have no transcript for it (`plan-planner-1.jsonl` has
  since been overwritten), so this is unresolved rather than supporting or refuting.
- The operator's "15+ minutes" does not match the most recent failure (6.4 min). I assume it
  refers to the 11:33–12:06 run, but I cannot prove the two reports describe the same attempt.
  It does not change the diagnosis: both attempts failed at the same line for the same reason.
