"""Conversational planning: the engine behind `main.py chat`.

The dashboard appends the operator's turn to $ARC_CHAT_DIR/<session>.jsonl
and runs `main.py chat --session <id> --repo <path>`. run_turn loads the
session, runs the fleet's planner (config.PLANNER_MODEL) exactly once over the whole
conversation, appends the assistant turn, and — when the reply carries a
```taskfile fenced block — validates that block through
code_tasks.load_taskfile (every governance rule applies: tier routing,
cross-harness review, ids, deps) and writes it atomically under
config.TASKS_DIR.

Exit codes: 0 for every completed turn — a repo problem, a driver failure
or an invalid taskfile is recorded IN the appended turn, because the
dashboard polls this process and must never wedge on a failure it can show
the operator. 1 only when the session file itself cannot be read or
written (including an invalid session id).
"""

import contextlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import code_tasks
import config
import drivers
import errors

SESSION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")

# Conversation older than this is dropped, oldest turns first, so a long
# refinement session cannot price itself out of the planner's context.
HISTORY_CHARS = 30000

PLANNER_PERSONA = (
    f"You are the ARC fleet's planning orchestrator, running on "
    f"{config.PLANNER_MODEL}. "
    "The operator describes a goal conversationally; you refine it into a "
    "governed code project for this fleet. Ask at most 2-3 sharp questions "
    "when the goal is ambiguous; once the goal is concrete, reply with a "
    "short plan summary (tasks, who implements, how it's verified) and "
    "exactly ONE ```taskfile fenced block with the complete taskfile JSON. "
    "Taskfile rules: {\"project\": {\"repo\": <stated path>, \"title\": str, "
    "\"tasks\": [...]}}; 2-6 tasks, each <30 min for one agent; per-task "
    "model is exactly one of "
    f"{' or '.join(sorted(config.IMPLEMENTER_MODELS))} — today's two-model "
    "fleet is GLM-5.3 (hard tier, the fleet's strongest, and the planner) and "
    "DeepSeek-V4.1-Flash-thinking-max (medium tier, the fast workhorse that "
    "implements and reviews but NEVER plans); "
    f"reviewer is exactly one of "
    f"{', '.join(chr(34) + f + chr(34) for f in sorted(config.REVIEW_FAMILIES))} "
    "and MUST NOT share a family with the implementer (never self-review); "
    "every task gets an honest verify_cmd — when code changes make "
    "./check.sh the first clause, use ./py NEVER .venv/bin/python (worktrees "
    "have no venv), and the command must FAIL on the untouched tree and pass "
    "only when the work is done (e.g. a new test it creates); prompts fully "
    "self-contained (the implementer sees only its own prompt: exact "
    "repo-relative paths, function names, acceptance criteria, an explicit "
    "do-not-touch list); files_hint of parallel (dep-independent) tasks "
    "disjoint; deps only when a task truly reads another task's merged "
    "output; ids kebab-case ^[a-z0-9][a-z0-9-]{0,60}$."
)


def chat_dir():
    """Sessions live in $ARC_CHAT_DIR, default <cwd>/logs/chat.

    Resolved at call time (not import time) so tests and the dashboard can
    redirect it via the environment regardless of import order.
    """
    return Path(os.getenv("ARC_CHAT_DIR") or Path.cwd() / "logs" / "chat")


def _session_path(session):
    return chat_dir() / f"{session}.jsonl"


def load_session(path):
    """Every turn of a session file, oldest first."""
    turns = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            turns.append(json.loads(line))
    return turns


def append_turn(path, turn):
    """Append one turn; a turn is one JSON object per line."""
    chat_dir().mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(turn) + "\n")


def _safe_append(path, turn):
    """Append a turn, returning the OSError instead of raising it.

    Every failure path ends in an assistant turn, and each of those writes
    can itself fail — the one condition that must return exit 1.
    """
    try:
        append_turn(path, turn)
    except OSError as exc:
        print(f"chat: cannot write session {path}: {exc}", file=sys.stderr)
        return exc
    return None


def _repo_problem(repo):
    """Why this repo cannot host a plan, or None when it can.

    Checked BEFORE the planner runs: an invalid repo discovered only at
    finalization would waste the fleet's scarcest model on a taskfile that
    can never execute. Same rule as dashboard._valid_repo — under
    /home/proxyie (the operator's trees), an existing directory, a git
    checkout.
    """
    if not isinstance(repo, str) or not repo.startswith("/home/proxyie/"):
        return f"repo must be an absolute path under /home/proxyie, got {repo!r}"
    path = Path(repo)
    if not path.is_dir():
        return f"repo is not a directory: {repo}"
    if not (path / ".git").exists():
        return f"repo is not a git checkout (no .git): {repo}"
    return None


def _history(turns, limit=HISTORY_CHARS):
    """'role: text' blocks, oldest turns dropped once past limit chars."""
    blocks = [f"{t.get('role')}: {t.get('text', '')}".strip()
              for t in turns
              if t.get("role") in ("user", "assistant") and t.get("text")]
    kept, size = [], 0
    for block in reversed(blocks):  # newest first; stop at the budget
        if kept and size + len(block) > limit:
            break
        kept.append(block)
        size += len(block)
    return "\n\n".join(reversed(kept))


def _planner_driver():
    """The planner for CHAT, which is interactive and therefore different.

    Roster-aware through drivers.driver_for: GLM-5.3 (opencode) on the
    two-model roster pinned 2026-09-12. Hardcoding a driver class here would
    fail the morning the roster's harness for the planner moves — this call
    site was missed once already when the governed pipeline's was fixed.

    `interactive=True` is the real point. A batch planner queueing behind three
    implementers is fine; a HUMAN waiting on a chat reply behind them is not.
    Interactive work jumps the driver queue (see drivers.Driver.run).
    """
    model = config.PLANNER_MODEL
    if model is None:
        raise RuntimeError("no planner-capable model on today's roster")
    return drivers.driver_for(model, "planner", interactive=True)


def build_prompt(repo, turns):
    return (PLANNER_PERSONA
            + f"\n\nTARGET REPO: {repo}\n\n"
            + "CONVERSATION WITH THE OPERATOR (oldest first). Reply as the "
              "planning orchestrator:\n\n"
            + _history(turns))


def _reply_text(res):
    """The planner's full reply text.

    DriverResult.text is capped at its LAST 3000 characters — fatal here,
    where the ```taskfile block is the payload and real taskfiles run
    6-11KB. Read the transcript's assistant messages instead (the same fix
    code_tasks applies to planner output) and fall back to the capped text
    only when no transcript survived.
    """
    msgs = code_tasks._transcript_assistant_messages(res.transcript_path)
    if msgs:
        return msgs[-1]
    return res.text or ""


def _taskfile_blocks(text):
    """The contents of every ```taskfile fenced block, in order."""
    return re.findall(r"```taskfile[^\n]*\n(.*?)```", text, re.DOTALL)


def _slug(text):
    """dashboard._task_slug's rule, restated: importing the whole HTTP
    server for one regex would drag it into every chat process."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower())[:40].strip("-")


def _unique_taskfile(slug):
    """First free <slug>.json in TASKS_DIR, with -2, -3... on collision."""
    tdir = Path(config.TASKS_DIR)
    cand, n = tdir / f"{slug}.json", 2
    while cand.exists():
        cand = tdir / f"{slug}-{n}.json"
        n += 1
    return cand


def finalize_taskfile(text, repo):
    """(filename, error) — validate the LAST ```taskfile block, write it.

    No file is written unless code_tasks.load_taskfile accepts the whole
    document, so every governance rule (tier routing, cross-review pairing,
    ids, deps) holds for chat-planned projects exactly as for `code plan`.
    The block's project.repo is overwritten with the --repo argument: the
    operator's CLI input is the truth, not what the model remembered.
    """
    blocks = _taskfile_blocks(text)
    if not blocks:
        return None, None
    try:
        data = json.loads(blocks[-1].strip())
    except ValueError as exc:
        return None, f"taskfile block is not valid JSON: {exc}"
    if not isinstance(data, dict) or not isinstance(data.get("project"), dict):
        return None, "taskfile block must be a JSON object with a 'project' object"
    data["project"]["repo"] = repo
    title = data["project"].get("title")
    if not isinstance(title, str) or not _slug(title):
        return None, "project.title is missing or has no slug characters"
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", suffix=".json", encoding="utf-8", delete=False) as f:
            json.dump(data, f)
            tmp = f.name
        code_tasks.load_taskfile(tmp)
    except Exception as exc:
        return None, f"taskfile rejected: {exc}"
    finally:
        if tmp:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
    dest = _unique_taskfile(_slug(title))
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / (dest.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, dest)  # atomic: no reader ever sees a half-written file
    return dest.name, None


async def run_turn(session, repo):
    """One assistant turn for a session whose user turn is already appended.

    Returns the process exit code: 0 for every completed turn, 1 only when
    the session file itself cannot be read or written.
    """
    if not session or not SESSION_RE.fullmatch(session):
        print(f"chat: invalid session id {session!r} "
              f"(expected ^[a-z0-9][a-z0-9-]{{0,39}}$)", file=sys.stderr)
        return 1
    path = _session_path(session)
    try:
        turns = load_session(path)
    except (OSError, ValueError) as exc:
        print(f"chat: cannot read session {path}: {exc}", file=sys.stderr)
        return 1

    problem = _repo_problem(repo)
    if problem is not None:
        # The planner is never invoked for a repo it could not plan for —
        # the taskfile could not be finalized anyway. Still a completed
        # turn (exit 0): the poll loop must show the operator the problem,
        # not hang on it.
        err = _safe_append(path, {
            "role": "assistant", "ts": time.time(),
            "text": "I can't plan for that repository — "
                    "please give me a valid one.",
            "error": problem})
        return 1 if err else 0

    # plan_tasks runs as task_id "plan"; chat transcripts must not
    # interleave with planner runs in logs/harness/.
    task_id = f"chat-{session}"
    try:
        res = await _planner_driver().run(
            build_prompt(repo, turns), Path(repo), task_id=task_id)
    except Exception as exc:
        # A crashed planner did not answer. Same containment as the
        # governed pipeline: capture a fingerprint for triage, keep the
        # turn loop alive.
        fp = errors.capture(exc, task=task_id, model=config.PLANNER_MODEL, node="chat")
        err = _safe_append(path, {
            "role": "assistant", "ts": time.time(),
            "text": "Sorry — the planner failed on that request. "
                    "Please try again.",
            "error": f"{fp}: {exc}"})
        return 1 if err else 0

    text = _reply_text(res)
    turn = {"role": "assistant", "ts": time.time(), "text": text}
    if not text.strip():
        turn["error"] = "planner returned an empty reply"
    else:
        try:
            name, ferr = finalize_taskfile(text, repo)
        except Exception as exc:
            # finalize_taskfile reports its own expected failures above;
            # reaching here means a genuine surprise in our own code.
            fp = errors.capture(exc, task=task_id, node="chat.finalize")
            name, ferr = None, f"{fp}: {exc}"
        if name:
            turn["taskfile"] = name
            print(f"chat: taskfile written: {name}")
        elif ferr:
            turn["error"] = ferr
    err = _safe_append(path, turn)
    return 1 if err else 0