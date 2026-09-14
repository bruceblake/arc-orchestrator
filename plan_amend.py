"""The taskfile is a living document: agents propose plan amendments from
inside their worktrees.

The planner's decomposition is fixed the moment `code plan` writes the
taskfile, but the best information about the plan arrives LATER, inside a run:
an implementer elbow-deep in t3 discovers its scope is really two tasks; a
reviewer reading the diff sees the verify gate doesn't test the spec. Before
this module, that knowledge had exactly one channel — the verdict JSON,
bounded to the agent's own task — so an agent that could see the plan was
wrong either forced the work into the wrong shape anyway or shipped a plan
bug it had already diagnosed.

The channel is `.arc/plan_proposals.jsonl` inside the per-task worktree: the
one filesystem an agent can definitely write and the orchestrator definitely
reads. The implement/review prompts offer the schema (`prompt_block`); graph
nodes HARVEST the file right after every agent run — read, then delete
immediately, because publish commits with `git add -A` and a proposal file
must never become PR content — validate each proposal against the same rules
the loader enforces on hand-written taskfiles (the caller injects the loader
as `validate`), and apply the survivors by rewriting the taskfile on disk
atomically.

What "apply" means in v1 — the honesty boundary:

  * Only tasks that have NOT started may be mutated. Merged is history, and
    an in-flight task has a live agent whose session already contains the old
    text; both reject (attach a `note` instead — it always lands). A failed
    or skipped task MAY be amended: its resume re-reads the file.
  * The in-flight DAG does NOT rewire. This run's graph was built from the
    taskfile as it was; amendments take effect when the run RESUMES
    (`code run <taskfile>` re-parses the file), for whichever tasks have no
    row yet, and for any downstream chain whose gate parses this file at wait
    time.
  * A proposal never removes or renames a task, never resurrects an id that
    has a row (a new task filed under a merged id would be skipped by resume
    as already done), never touches project-level keys, and never leaves a
    task without a verify gate (Rule 4 holds for agents exactly as for
    planners).

Every proposal — applied, rejected, or merely noted — lands in the
plan_proposals table and as a `plan.amend` event, so "who wanted to change
the plan and what happened" is always answerable.
"""
import copy
import json
import logging
import os
import re
import tempfile
from pathlib import Path

import config
import events

log = logging.getLogger("plan-amend")

# The one filesystem contract between agent and orchestrator. Under .arc/ so
# the file can never collide with anything a task is actually building.
PROPOSALS_REL = ".arc/plan_proposals.jsonl"

KINDS = ("note", "edit_scope", "change_verify", "change_model",
         "add_task", "split_task")

# A runaway agent appending forever is a bug to be surfaced, not a backlog.
MAX_PER_BATCH = 25
_MAX_BYTES = 256 * 1024

# Row statuses under which a task MAY be mutated — an allowlist, not a
# denylist: anything not named here (merged, running, in_review, conflict,
# pending, and any status the future adds) freezes the task, because the
# burden of proof is on mutation. No row at all is also safe — the resume
# path re-reads the file.
_MUTABLE = ("failed", "skipped")

# The loader's own id rule (code_tasks.load_taskfile); a proposal's new id
# becomes a worktree path and a git-ref fragment exactly like a planned one.
_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,60}")


def prompt_block(roster):
    """The opt-in amendment instruction grafted onto implement/review prompts.

    `roster` is [(task_id, title), ...] for the whole taskfile: a proposer
    needs sibling ids to name dep targets, and a guessed id costs a
    rejection, so the real ids are handed over.
    """
    ids = ", ".join(tid for tid, _ in roster) or "(none)"
    models = ", ".join(config.IMPLEMENTER_MODELS)
    return (
        "\nPLAN AMENDMENTS (optional): if the PLAN itself is wrong — a task "
        "that should be split, a verify gate that does not test its spec, a "
        "missing task, a misrouted model — do not just work around it: "
        f"append ONE JSON object per line to {PROPOSALS_REL} (create the .arc "
        "directory). One of these kinds per line:\n"
        '  {"kind":"note","task":"<id>","note":"observation, risk, or follow-up"}\n'
        '  {"kind":"edit_scope","task":"<id>","title":"...","prompt":"...",'
        '"files_hint":["..."]}\n'
        '  {"kind":"change_verify","task":"<id>","verify_cmd":"<shell command>"}\n'
        '  {"kind":"change_model","task":"<id>","model":"<model>"}\n'
        '  {"kind":"add_task","taskspec":{"id":"<new-id>","title":"...",'
        '"prompt":"...","model":"<model>","verify_cmd":"<shell command>",'
        '"deps":["<id>"]}}\n'
        '  {"kind":"split_task","task":"<id>","into":[{"id":"<new-id>",'
        '"title":"...","prompt":"...","verify_cmd":"<shell command>",'
        '"deps":["<id>"]}]}\n'
        f"This plan's task ids: {ids}\n"
        f"Legal models: {models}\n"
        "Amendments are validated by the same loader the taskfile came from "
        "(legal model, cross-family reviewer pair, deps must exist, no "
        "cycles) and can only touch tasks that have not started; a merged or "
        "in-flight task rejects the amendment, so attach a note to it "
        "instead. Applied amendments take effect when the run resumes, never "
        "mid-graph. The orchestrator reads and DELETES this file when your "
        "session ends — it is never committed to the PR.\n"
    )


def read_proposals(wt):
    """Read the proposals file from a worktree and delete it in the same breath.

    Deletion is not housekeeping: publish commits with `git add -A`, so any
    proposal file still on disk when publish runs would be committed into the
    PR. Nodes harvest right after every agent run, and publish sweeps once
    more before committing, so the file never survives to the commit. Never
    raises: a harness killed mid-write leaves torn JSON behind, and that must
    not take the node down with it.
    """
    p = Path(wt) / PROPOSALS_REL
    try:
        with p.open(encoding="utf-8", errors="replace") as f:
            raw = f.read(_MAX_BYTES)
            truncated = bool(f.read(1))
    except OSError:
        return []
    try:
        p.unlink()
    except OSError as exc:
        # The anti-`git add -A` guarantee rests on this delete; its failure
        # must be visible, and harvest() retries it after applying.
        log.warning("plan-amend: could not delete %s after reading: %s", p, exc)
    lines = [ln for ln in (l.strip() for l in raw.splitlines()) if ln]
    entries = []
    for line in lines[:MAX_PER_BATCH]:
        try:
            entries.append(json.loads(line))
        except ValueError:
            entries.append({"_malformed": line[:300]})
    if len(lines) > MAX_PER_BATCH:
        truncated = True
    if truncated:
        # Dropping proposals silently would make a runaway agent invisible.
        # The sentinel is itself recorded (rejected), so the batch's tail
        # being cut is part of the trail.
        entries.append({"_truncated": True})
    return entries


def harvest(store, taskfile, wt, *, proposer, role, model, validate):
    """read_proposals + apply. Called right after every agent run by the graph
    nodes, and once more by publish as a pre-commit sweep (a run process can
    die between an agent run and the harvest that follows it, and publish's
    `git add -A` would otherwise commit whatever the agent left)."""
    entries = read_proposals(wt)
    if not entries:
        return
    counts = apply(store, taskfile, entries, proposer=proposer, role=role,
                   model=model, validate=validate)
    log.info("plan proposals from %s/%s on %s: %d applied, %d rejected, %d noted",
             proposer, role, Path(taskfile).name,
             counts["applied"], counts["rejected"], counts["noted"])
    # The whole anti-`git add -A` guarantee hangs on the file being GONE by
    # commit time. read_proposals deletes on read; if that delete failed it
    # already warned, so retry once here — and if the channel STILL survives,
    # say so on the event log where a human will actually see it.
    leftover = Path(wt) / PROPOSALS_REL
    if leftover.exists():
        try:
            leftover.unlink()
        except OSError as exc:
            log.error("plan-amend: %s survived TWO deletes: %s", leftover, exc)
            events.emit("plan.amend.channel_survives",
                        taskfile=Path(taskfile).name, proposer=proposer,
                        path=str(leftover), error=str(exc)[:200])


def apply(store, taskfile, entries, *, proposer, role, model, validate):
    """Validate each entry in order and apply the survivors to the taskfile.

    Entries are processed against the file as amended so far; one that leaves
    the file invalid under the injected loader is rolled back and rejected
    while its siblings still apply. The amended file is written once at the
    end, atomically (same-dir temp + os.replace), so a concurrent reader —
    the dashboard, a downstream chain gate, a hand-editing operator — never
    sees half a taskfile.

    Synchronous on purpose, with no await between read-modify-write: a run's
    nodes harvest on one event loop and cannot interleave, and two RUN
    processes racing one taskfile is a configuration the rest of the system
    already refuses (run start resets the other run's rows).
    """
    if not entries:
        return {"applied": 0, "rejected": 0, "noted": 0}
    try:
        data = json.loads(Path(taskfile).read_text(encoding="utf-8"))
        tasks = data["project"]["tasks"]
        if not isinstance(tasks, list):
            raise TypeError("project.tasks is not a list")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        for entry in entries:
            _record(store, taskfile, "", proposer, role, model, "?",
                    "rejected", f"taskfile unreadable: {exc}"[:300], entry)
        return {"applied": 0, "rejected": len(entries), "noted": 0}

    try:
        statuses = {r["id"]: r["status"] for r in store.code_tasks_for(taskfile)}
    except Exception:
        # Statuses are the freeze boundary, so the safe fallback is to treat
        # nothing as frozen rather than to freeze everything... no — it is to
        # reject mutations: a store we cannot read cannot prove a task hasn't
        # started. Notes still land.
        log.warning("plan-amend: cannot read code_tasks for %s", taskfile)
        statuses = None

    counts = {"applied": 0, "rejected": 0, "noted": 0}
    dirty = False
    for entry in entries:
        kind = entry.get("kind") if isinstance(entry, dict) else "?"
        snap = copy.deepcopy(tasks)
        target, action, reason = _apply_one(data, entry, statuses)
        if action == "applied":
            try:
                validate(data)
            except Exception as exc:
                # The proposal mutated the plan into something the loader
                # would refuse from a human planner too. Roll THIS entry back;
                # its siblings still apply.
                tasks[:] = snap
                action = "rejected"
                reason = f"invalid plan after applying: {exc}"[:300]
        counts[action] += 1
        _record(store, taskfile, target, proposer, role, model,
                str(kind or "?"), action, reason, entry)
        events.emit("plan.amend", taskfile=Path(taskfile).name,
                    task=target or "", proposer=proposer, role=role,
                    model=model, kind=str(kind or "?"), action=action,
                    reason=(reason or "")[:240])
        if action == "applied":
            dirty = True
    if dirty:
        _write_taskfile(taskfile, data)
    return counts


# --- per-kind mutation ---------------------------------------------------------


def _apply_one(data, entry, statuses):
    """One proposal -> (target_id, action, reason), mutating
    data["project"]["tasks"] in place only when action == "applied". Whole-file
    validation is the caller's job (apply() rolls back on failure).

    `statuses` maps task id -> code_tasks row status, or None when the store
    could not be read (mutations then reject; notes still land).
    """
    if not isinstance(entry, dict):
        return "", "rejected", "entry is not a JSON object"
    if "_malformed" in entry:
        return "", "rejected", "line is not valid JSON"
    if "_truncated" in entry:
        return "", "rejected", (f"batch truncated: at most {MAX_PER_BATCH} entries "
                                f"/ {_MAX_BYTES // 1024} KiB are kept per harvest; "
                                "surplus proposals were dropped — re-propose them "
                                "next round")
    kind = entry.get("kind")
    if kind not in KINDS:
        return "", "rejected", f"unknown kind {kind!r} (one of {', '.join(KINDS)})"
    tasks = data["project"]["tasks"]
    by_id = {t.get("id"): t for t in tasks if isinstance(t, dict)}

    def frozen(tid):
        if statuses is None:
            return "cannot verify the task has not started (store unreadable)"
        st = statuses.get(tid)
        if st is None:
            return None  # no row — unstarted, mutable
        if st not in _MUTABLE:
            return (f"task {tid} is {st} — only unstarted (or failed/skipped) "
                    "tasks may be amended")
        return None

    if kind == "note":
        note = str(entry.get("note", "")).strip()
        target = entry.get("task", "")
        if not note:
            return str(target), "rejected", "note is empty"
        return str(target), "noted", note[:240]

    if kind == "add_task":
        spec = entry.get("taskspec")
        if not isinstance(spec, dict):
            return "", "rejected", "add_task needs a \"taskspec\" object"
        tid = spec.get("id")
        reason = _check_new_id(tid, by_id, statuses)
        if reason is None:
            reason = _check_spec_fields(spec, require_model=True, require_verify=True)
        if reason:
            return str(tid or ""), "rejected", reason
        new = _clean_spec(spec)
        if "reviewer" not in new:
            cf = config.cross_family_reviewer(new["model"])
            if cf:
                new["reviewer"] = cf
        tasks.append(new)
        return new["id"], "applied", None

    target = entry.get("task")
    target = target if isinstance(target, str) else ""
    t = by_id.get(target)
    if t is None:
        return target, "rejected", f"unknown task {target!r}"
    reason = frozen(target)
    if reason:
        return target, "rejected", reason

    if kind == "edit_scope":
        fields = {}
        for k in ("title", "prompt"):
            if k in entry:
                v = entry[k]
                if not isinstance(v, str) or not v.strip():
                    return target, "rejected", f"{k} must be a non-empty string"
                fields[k] = v
        if "files_hint" in entry:
            v = entry["files_hint"]
            if not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
                return target, "rejected", "files_hint must be a list of strings"
            fields["files_hint"] = v
        if not fields:
            return target, "rejected", "edit_scope needs title, prompt, or files_hint"
        t.update(fields)
        return target, "applied", None

    if kind == "change_verify":
        v = entry.get("verify_cmd")
        if not isinstance(v, str) or not v.strip():
            # A proposal may REPLACE a gate but never remove one: a taskfile
            # task with no honest verify_cmd is a bug (Rule 4), whoever wrote it.
            return target, "rejected", "verify_cmd must be a non-empty command"
        t["verify_cmd"] = v
        return target, "applied", None

    if kind == "change_model":
        m = entry.get("model")
        if m not in config.IMPLEMENTER_MODELS:
            return target, "rejected", (f"model {m!r} is not an implementer "
                                        f"({', '.join(config.IMPLEMENTER_MODELS)})")
        t["model"] = m
        if entry.get("reviewer") is not None:
            t["reviewer"] = entry["reviewer"]
        elif (t.get("reviewer") or "") == config.MODEL_FAMILY.get(m, ""):
            # The old pairing is now same-family, which the loader refuses:
            # flip to a cross-family reviewer rather than let validation do it.
            # (A taskfile's reviewer: is a FAMILY token — "glm", "deepseek" —
            # so compare it against the new model's family, not MODEL_FAMILY
            # of the token: that lookup is keyed by model name and never hits.)
            cf = config.cross_family_reviewer(m)
            if cf:
                t["reviewer"] = cf
        return target, "applied", None

    # split_task
    if t.get("when"):
        # Conditional semantics cannot be distributed honestly over pieces:
        # which piece inherits the `when` reads on the upstream verdict?
        return target, "rejected", ("split of a conditional (`when`) task — "
                                    "re-point the condition by hand")
    for other in tasks:
        w = other.get("when") if isinstance(other, dict) else None
        if other is not t and w and w.get("dep") == target:
            return target, "rejected", (f"{other.get('id')}'s `when` reads "
                                        f"{target}'s verdict — re-point it by hand first")
    pieces = entry.get("into")
    if not (isinstance(pieces, list) and 2 <= len(pieces) <= 4):
        return target, "rejected", "split_task needs 2 to 4 pieces in \"into\""
    ids = [p.get("id") if isinstance(p, dict) else None for p in pieces]
    if len(set(ids)) != len(ids):
        return target, "rejected", "duplicate piece id"
    for pid in ids:
        reason = _check_new_id(pid, by_id, statuses)
        if reason:
            return target, "rejected", reason
    built = []
    for i, p in enumerate(pieces):
        if not isinstance(p, dict):
            return target, "rejected", f"piece {i} is not an object"
        reason = _check_spec_fields(p, require_model=False, require_verify=False)
        if reason:
            return target, "rejected", f"piece {p.get('id', i)}: {reason}"
        new = _clean_spec(p)
        new.setdefault("model", t["model"])
        new.setdefault("verify_cmd", t.get("verify_cmd", ""))
        if not new["verify_cmd"].strip():
            return target, "rejected", (f"piece {new['id']}: would inherit an "
                                        "empty gate — give it its own verify_cmd")
        if "reviewer" not in new:
            cf = config.cross_family_reviewer(new["model"])
            if cf:
                new["reviewer"] = cf
        if i == 0:
            # The FIRST piece ALWAYS carries the target's upstream — the
            # split's combined upstream is exactly the target's upstream, so
            # deps the piece declares are unioned IN, never a replacement:
            # dropping the target's upstream would start the piece before its
            # input exists. Later pieces must say what they need (usually: an
            # earlier piece).
            upstream = list(t.get("deps", []))
            new["deps"] = upstream + [d for d in new["deps"] if d not in upstream]
        built.append(new)
    piece_ids = [n["id"] for n in built]
    idx = tasks.index(t)
    tasks[idx:idx + 1] = built
    for other in tasks:
        if other.get("id") in piece_ids:
            continue
        deps = other.get("deps")
        if isinstance(deps, list) and target in deps:
            # Dependents re-point to ALL pieces: guessing a subset (first
            # piece only, last piece only) either starts the dependent before
            # its input exists or ties it to work it never needed. The join
            # over every piece is the only answer that is always safe.
            other["deps"] = ([d for d in deps if d != target]
                             + [p for p in piece_ids if p not in deps])
    return target, "applied", None


def _check_new_id(tid, by_id, statuses):
    if not isinstance(tid, str) or not _ID_RE.fullmatch(tid):
        return f"task id {tid!r} must match {_ID_RE.pattern}"
    if statuses is None:
        # A new id lands on resume; a store we cannot read cannot prove the
        # id has no row, and a spent id keys new work against old state.
        return "cannot verify the task has not started (store unreadable)"
    if tid in by_id or tid in statuses:
        # An id with a ROW is spent even if the task left the file:
        # resurrecting it keys the new task against the old row, and a merged
        # row would make resume skip the new work as already done.
        return f"task id {tid!r} is already used"
    return None


def _check_spec_fields(spec, *, require_model, require_verify):
    """Shape rules for a NEW task spec (piece or addition). Existing-dep
    existence and cycles are the loader's job, run over the whole file."""
    for k in ("title", "prompt"):
        v = spec.get(k)
        if not isinstance(v, str) or not v.strip():
            return f"{k} must be a non-empty string"
    m = spec.get("model")
    if m is not None and m not in config.IMPLEMENTER_MODELS:
        return (f"model {m!r} is not an implementer "
                f"({', '.join(config.IMPLEMENTER_MODELS)})")
    if require_model and m is None:
        return "model is required (inheritance only exists for split pieces)"
    v = spec.get("verify_cmd")
    if v is not None and (not isinstance(v, str) or not v.strip()):
        return "verify_cmd must be a non-empty command (Rule 4)"
    if require_verify and v is None:
        return "verify_cmd is required (Rule 4 — no new task without a gate)"
    deps = spec.get("deps", [])
    if not (isinstance(deps, list) and all(isinstance(d, str) for d in deps)):
        return "deps must be a list of task ids"
    fh = spec.get("files_hint", [])
    if not (isinstance(fh, list) and all(isinstance(x, str) for x in fh)):
        return "files_hint must be a list of strings"
    return None


def _clean_spec(spec):
    new = {"id": spec["id"], "title": spec["title"].strip(),
           "prompt": spec["prompt"]}
    for k in ("model", "reviewer", "verify_cmd"):
        if k in spec:
            new[k] = spec[k]
    new["deps"] = list(spec.get("deps", []))
    if "files_hint" in spec:
        new["files_hint"] = list(spec["files_hint"])
    return new


# --- persistence ----------------------------------------------------------------


def _write_taskfile(path, data):
    """Atomic rewrite: same-dir temp file + os.replace, so no reader ever
    observes a partially written plan."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".plan-amend-",
                               suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _record(store, taskfile, target, proposer, role, model, kind, action,
            reason, entry):
    try:
        payload = json.dumps(entry)[:1000]
    except (TypeError, ValueError):
        payload = str(entry)[:1000]
    store.save_plan_proposal(taskfile, target or "", proposer, role, model,
                             kind, action, (reason or "")[:300], payload)
