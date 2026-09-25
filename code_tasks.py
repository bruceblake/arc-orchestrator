"""Multi-harness code workload: JSON task files -> task graph -> worktrees.

Per task: alloc worktree -> implement (the model's roster harness; since the
2026-09-12 two-model fleet: DeepSeek-V4.1-Flash-thinking-max on reasonix, GLM-5.3 on
opencode) -> deterministic verify gate (verify_cmd) -> cross-family review
(the other family's reviewer) -> bounded fix loop -> publish commit ->
merge to main (serialized) -> cleanup. Reviews are mandatory and cross-family
by default; an explicit bench `policy` (see orchbench.py) may relax
routing/review rules to measure what the governance defaults buy.
"""
import asyncio
import functools
import json
import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import agentboard
import board
import config
import errors
import events
import evidence
import gitstore
import graft
import manual_review
import drivers
import gh_issues
import plan_amend
import dossier as dossier_mod
import project_contract
import ui_evidence
from drivers import (DeepseekDriver, DriverError, KimiDriver, OpencodeDriver,
                     ReasonixDriver,
                     driver_for, transcript_tokens)
from graph import Graph, GraphError

log = logging.getLogger("code-tasks")



# Where a retired model's work goes now. Callables so they read the roster at
# call time, not at import (the roster is dated).
RETIRED_MODELS = {
    "gpt-oss-120b":      lambda: config.ESCALATION_PATH[0],                 # no basic tier
    # ...or, the day the provider serves no DeepSeek at all (09-12, 12:22),
    # the entry tier — never None: a retired name must always land somewhere.
    "DeepSeek-V4-Flash": lambda: next((m for m in config.ESCALATION_PATH
                                       if m.startswith("DeepSeek")),
                                      config.ESCALATION_PATH[0]),
    "Kimi-K3":           lambda: config.ESCALATION_PATH[-1],                # strongest live
    # RETIRED EARLY by operator decision 2026-09-12 (the provider had scheduled
    # its withdrawal for 2026-09-19; the operator moved first). Old taskfiles
    # naming it still run, remapped onto the strongest live tier.
    # Union-Alpha RETIRED EARLY 2026-09-17 by operator decision: its free
    # OpenRouter preview ended (every call returns "Thank you for participating
    # in the Stealth Union Alpha testing period"), before its scheduled
    # 2026-09-23 end date. It was a MEDIUM-tier implementer/reviewer, so its
    # work lands on the medium tier's live model — never the tier-0 default,
    # which would silently promote it to the hard tier.
    "Union-Alpha":       lambda: next((m for m in config.ESCALATION_PATH
                                       if m in config.IMPLEMENT_TIERS.get("medium", ())),
                                      config.ESCALATION_PATH[0]),
    # Former hard-tier Studio subscription harnesses. Keep existing taskfiles
    # runnable after the active Studio profile moved to Claude and Codex.
    "Cursor-Grok-4.7":   lambda: config.ESCALATION_PATH[-1],
    "Antigravity-Gemini": lambda: config.ESCALATION_PATH[-1],
}


def load_taskfile(path, policy=None):
    """Load + validate a taskfile. `policy` (bench variant overrides) may widen
    the allowed implementers/reviewers, permit self-review, or disable review;
    with policy=None the governance defaults apply byte-for-byte."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    # A taskfile planned by the studio names models that exist only on the
    # studio roster. Loaded under another fleet it failed with "model X must
    # be an implementer ([GLM, DeepSeek])", which reads like a bad plan rather
    # than the wrong fleet. Say which fleet it needs instead.
    fleet = (data.get("project") or {}).get("fleet")
    if fleet and fleet != config.FLEET:
        raise ValueError(
            f"{Path(path).name} was planned for ARC_FLEET={fleet}, but this "
            f"process runs ARC_FLEET={config.FLEET}; run it with "
            f"ARC_FLEET={fleet}")
    repo = Path(data["project"]["repo"]).resolve()
    pol = policy or {}
    models = set(config.IMPLEMENTER_MODELS) | set(pol.get("implementers", []))
    # Review-capable families, from the roster: two today (deepseek, glm),
    # since DeepSeek-V4.1-Flash-thinking-max gained reviewer on 2026-09-12.
    # A taskfile written for a family that has since left is remapped below
    # rather than rejected — the plan is still good.
    reviewers = tuple(pol.get("reviewers", tuple(config.REVIEW_FAMILIES)))
    review_on = pol.get("review", True)
    # Self-review is a bench-variant knob only (policy["allow_self_review"]),
    # plus ONE fleet-wide escape hatch: config.ALLOW_SAME_FAMILY_REVIEW
    # (ARC_ALLOW_SAME_FAMILY_REVIEW), for when the cross-family reviewer is
    # hard-down server-side — see config.py. It ran 2026-09-12..14, was
    # removed when GLM-5.3 stabilised, and was re-added 2026-09-15 when
    # GLM-5.3's session counter jammed for hours with nothing local holding
    # it.
    allow_self = bool(pol.get("allow_self_review")) or \
        config.ALLOW_SAME_FAMILY_REVIEW
    # Human checkpoints (Rule 5, manual review): project.human_review makes
    # every task's fleet-approved PR wait for a person; a task's own
    # human_review overrides it either way. Absent = the fleet-wide
    # ARC_PR_MANUAL_REVIEW decides at run time (manual_review.wanted).
    project_human = data["project"].get("human_review")
    if project_human is not None and not isinstance(project_human, bool):
        raise ValueError("project.human_review must be true or false")
    tasks = {}
    for t in data["project"]["tasks"]:
        tid = t["id"]
        task_human = t.get("human_review")
        if task_human is not None and not isinstance(task_human, bool):
            raise ValueError(f"task {tid}: human_review must be true or false")
        if not isinstance(tid, str) or \
                not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,60}", tid):
            raise ValueError(
                f"task id {tid!r} must match [a-z0-9][a-z0-9-]{{0,60}} "
                "(it becomes a worktree path and a git-ref fragment)")
        if tid in tasks:
            raise ValueError(f"duplicate task id: {tid}")
        model = t.get("model", "")
        planned_model = model
        if model not in models and model in RETIRED_MODELS and not pol.get("implementers"):
            # A model that LEFT the roster — gpt-oss retired 09-11, DeepSeek-V4
            # replaced 09-12, Kimi-K3 retired EARLY 09-12. The decomposition is
            # still good; only the label is stale. Remap to where that tier's work
            # goes now rather than failing every taskfile written before the
            # transition.
            model = RETIRED_MODELS[model]() or config.ESCALATION_PATH[0]
        if model not in models:
            raise ValueError(
                f"task {tid}: model {model!r} must be an implementer ({sorted(models)})"
            )
        reviewer = t.get("reviewer", "")
        if review_on and reviewer not in reviewers and not pol.get("reviewers"):
            # The named family is not review-capable TODAY — most likely it left
            # the roster (kimi on 09-12) or was never one (gpt-oss). Remap to
            # the strongest cross-family reviewer instead of failing a taskfile
            # whose decomposition is still perfectly good.
            remapped = config.cross_family_reviewer(model)
            if remapped is None:
                raise ValueError(
                    f"task {tid}: reviewer must be one of {reviewers}, got "
                    f"{reviewer!r}, and no cross-family reviewer exists for {model}")
            reviewer = remapped
        elif review_on and reviewer not in reviewers:
            raise ValueError(f"task {tid}: reviewer must be one of {reviewers}, got {reviewer!r}")
        impl_family = config.MODEL_FAMILY[model]
        rev_family = config.MODEL_FAMILY.get(reviewer, reviewer)
        if (review_on and not allow_self and impl_family == rev_family
                and model != planned_model):
            # The remap above moved a retired model's task onto a family that
            # happens to be its own reviewer (DeepSeek → GLM with reviewer
            # "glm", the morning the provider stopped serving DeepSeek). The
            # plan was cross-family when written; keep it cross-family now by
            # flipping the reviewer, not by failing every old taskfile.
            flipped = config.cross_family_reviewer(model)
            if flipped is not None:
                reviewer, rev_family = flipped, config.MODEL_FAMILY.get(flipped, flipped)
        if review_on and not allow_self and impl_family == rev_family:
            raise ValueError(
                f"task {tid}: reviewer {reviewer!r} must not be the harness that "
                f"implemented ({model}); use another one"
            )
        tasks[tid] = {
            "id": tid,
            "title": t.get("title", tid),
            "prompt": t["prompt"],
            "model": model,
            "reviewer": reviewer,
            "verify_cmd": t.get("verify_cmd", ""),
            "deps": list(t.get("deps", [])),
            "files_hint": list(t.get("files_hint", [])),
            "probe_cmd": t.get("probe_cmd", "") or "",
            "when": _load_when(tid, t.get("when")),
            "human_review": task_human if task_human is not None else project_human,
        }
        if not isinstance(tasks[tid]["probe_cmd"], str):
            raise ValueError(f"task {tid}: probe_cmd must be a string")
        # Rule 7d switches, read at run time (evidence.enabled_for and the
        # reviewer's blocking rule). Kept only when written, so an absent key
        # still means "the default".
        for flag in ("evidence", "visual"):
            if flag in t:
                if not isinstance(t[flag], bool):
                    raise ValueError(f"task {tid}: {flag} must be true or false")
                tasks[tid][flag] = t[flag]
    for tid, t in tasks.items():
        for d in t["deps"]:
            if d not in tasks:
                raise ValueError(f"task {tid}: unknown dep {d!r}")
        w = t["when"]
        if w:
            if w["dep"] not in t["deps"]:
                raise ValueError(
                    f"task {tid}: when.dep {w['dep']!r} must also be listed in deps "
                    "(a condition is read from a dependency's verdict)")
            if not tasks[w["dep"]]["probe_cmd"]:
                raise ValueError(
                    f"task {tid}: when reads {w['dep']}'s verdict, but {w['dep']} has no "
                    "probe_cmd — nothing would ever write one")
    _topo(tasks)  # raises on cycles
    # Project chaining: `after` names whole taskfiles whose EVERY task must be
    # merged before this project allocates its first worktree. Existence of
    # the dep files is deliberately NOT required here — a chain is often
    # declared before the upstream project is even planned; a missing dep
    # simply reads as "not done yet" at wait time (and bounded by
    # config.CHAIN_TIMEOUT). Cross-file cycles cannot be checked here (the
    # deps may not exist yet); build_code_graph checks them at run time.
    after_raw = data.get("project", {}).get("after", [])
    if not isinstance(after_raw, list) or not all(
            isinstance(a, str) and a.strip() for a in after_raw):
        raise ValueError("project.after must be a list of taskfile paths")
    me = str(Path(path).resolve())
    after = []
    for a in after_raw:
        k = _dep_key(a)
        if k == me:
            raise ValueError(f"project.after lists this taskfile itself: {a!r}")
        if k not in after:
            after.append(k)
    # The planner's label for the graph between tasks. Normalized to the
    # catalogue id (fan-out-fan-in, orchestrator-workers -> fanout); an
    # unknown name is kept as written and logged, never rejected — the deps
    # are the graph, the label is what the planner MEANT, and describe()
    # says whether the two agree.
    import graph_shapes
    raw_pattern = data.get("project", {}).get("pattern", "") or ""
    pattern = graph_shapes.normalize_pattern(raw_pattern) or raw_pattern
    if raw_pattern and pattern == raw_pattern and raw_pattern not in graph_shapes.PATTERN_IDS:
        log.warning("%s: pattern %r is not in the catalogue (%s)",
                    Path(path).name, raw_pattern, ", ".join(graph_shapes.PATTERN_IDS))
    return {"repo": repo, "tasks": tasks, "title": data.get("project", {}).get("title", ""),
            "name": data.get("project", {}).get("name") or repo.name,
            "human_review": project_human,
            "pattern": pattern,
            "policy": pol, "after": after}


# --- conditional deps: task.when ---------------------------------------------
#
# The graph between tasks used to be unconditional: every task written runs.
# `when` lets a task run only if a dependency's VERDICT says so. The verdict is
# the JSON a task's `probe_cmd` prints in its worktree after its gate passes
# (stored on the row, emitted as task.verdict); the predicate is evaluated on
# the edge that would release the dependent, exactly like the gate/review
# branches inside a task (graph.Edge when=). A dependent whose condition does
# not hold is SKIPPED — a terminal status, recorded like merged or failed — and
# so is everything downstream of it. This is what makes a one-taskfile router
# possible: probe → {fix-frontend if area == frontend, fix-backend otherwise}.

_WHEN_OPS = ("equals", "not_equals", "in", "truthy", "exists")


def _load_when(tid, raw):
    """Validate a task's `when` block; None when absent."""
    if raw in (None, "", {}):
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"task {tid}: when must be an object")
    dep, key = raw.get("dep"), raw.get("key")
    if not isinstance(dep, str) or not dep:
        raise ValueError(f"task {tid}: when.dep must name a dependency")
    if not isinstance(key, str) or not key:
        raise ValueError(f"task {tid}: when.key must name a verdict field")
    ops = [o for o in _WHEN_OPS if o in raw]
    if len(ops) != 1:
        raise ValueError(
            f"task {tid}: when needs exactly one of {_WHEN_OPS}, got {ops or 'none'}")
    op = ops[0]
    val = raw[op]
    if op == "in" and not isinstance(val, list):
        raise ValueError(f"task {tid}: when.in must be a list")
    if op in ("truthy", "exists") and not isinstance(val, bool):
        raise ValueError(f"task {tid}: when.{op} must be true or false")
    return {"dep": dep, "key": key, "op": op, "value": val}


def _verdict_get(verdict, key):
    """`key` may be dotted (a.b.c) into a nested verdict; missing → None."""
    cur = verdict
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None, False
        cur = cur[part]
    return cur, True


def when_holds(when, verdict):
    """Does `verdict` (the dep's probe JSON, or None) satisfy `when`?"""
    got, present = _verdict_get(verdict if isinstance(verdict, dict) else {}, when["key"])
    op, val = when["op"], when["value"]
    if op == "exists":
        return present == val
    if op == "truthy":
        return bool(got) == val
    if op == "equals":
        return present and got == val
    if op == "not_equals":
        return not present or got != val
    if op == "in":
        return present and got in val
    return False


def when_text(when):
    op, v = when["op"], when["value"]
    sym = {"equals": "==", "not_equals": "!=", "in": "in"}.get(op)
    if sym:
        return f"{when['dep']}.{when['key']} {sym} {json.dumps(v)}"
    return f"{when['dep']}.{when['key']} {'is' if v else 'is not'} {op}"


def _topo(tasks):
    order, seen = [], set()

    def visit(tid, stack):
        if tid in seen:
            return
        if tid in stack:
            raise ValueError(f"dependency cycle at {tid}")
        for d in tasks[tid]["deps"]:
            visit(d, stack | {tid})
        seen.add(tid)
        order.append(tid)

    for tid in tasks:
        visit(tid, set())
    return order


def _downstream(tasks, tid):
    """Every task that (transitively) depends on `tid`, in topological order."""
    out, frontier = [], [tid]
    while frontier:
        cur = frontier.pop(0)
        for other, t in tasks.items():
            if cur in t["deps"] and other not in out and other != tid:
                out.append(other)
                frontier.append(other)
    return out


def describe(taskset):
    import graph_shapes
    lines = [f"repo: {taskset['repo']}",
             project_contract.status_line(taskset["repo"])]
    if taskset.get("after"):
        lines.append("after: " + ", ".join(
            Path(k).name for k in taskset["after"]))
    # The graph between tasks, as declared and as the deps actually form it.
    # A plan that says "diamond" and wires a chain has a bug worth one line
    # here — the planner reads this after `code plan`, the operator on a
    # dry run.
    shape = graph_shapes.classify({"tasks": list(taskset["tasks"].values()),
                                   "pattern": taskset.get("pattern") or ""})
    decl = taskset.get("pattern") or "(none declared)"
    note = " — MISMATCH: the deps do not form the declared pattern" if shape["mismatch"] else ""
    lines.append(f"graph: {shape['shape']} (declared {decl}; width {shape['width']}, "
                 f"depth {shape['depth']}) — {shape['reason']}{note}")
    for tid in _topo(taskset["tasks"]):
        t = taskset["tasks"][tid]
        impl_fam = config.MODEL_FAMILY[t["model"]]
        rev_fam = config.MODEL_FAMILY.get(t["reviewer"], t["reviewer"])
        cross = "cross-family" if impl_fam != rev_fam else "SAME-FAMILY(!)"
        cond = f" when={when_text(t['when'])}" if t.get("when") else ""
        probe = f" probe={t['probe_cmd']}" if t.get("probe_cmd") else ""
        lines.append(
            f"  {tid}: implement={t['model']} review={rev_fam}({cross}) "
            f"deps={t['deps'] or '[]'}{cond} base={config.BASE_BRANCH} "
            f"verify={t['verify_cmd'] or '(none)'}{probe}"
        )
    return "\n".join(lines)


# --- project chaining: taskfile -> taskfile dependencies --------------------
#
# A taskfile may declare `"after": ["<taskfile>", ...]`: its project then
# waits for EVERY task of each named upstream taskfile to reach 'merged'
# before its own first worktree allocates. This is the cross-project analog
# of per-task `deps` — DAG-of-DAGs chaining.


def _dep_key(p):
    """Canonical key for an `after` entry: the resolved absolute path — the
    same form main.py stores in code_tasks.taskfile."""
    q = Path(str(p)).expanduser()
    if not q.is_absolute():
        cand = Path(config.TASKS_DIR) / q
        q = cand if len(q.parts) == 1 or cand.exists() else Path.cwd() / q
    return str(q.resolve())


def _taskfile_ids(key):
    """Task ids of a taskfile on disk, or None when missing/unreadable."""
    try:
        data = json.loads(Path(key).read_text(encoding="utf-8"))
        return [t["id"] for t in data["project"]["tasks"]]
    except Exception:
        return None


def _read_after(key):
    """The resolved `after` keys of a taskfile on disk ([] when unreadable)."""
    try:
        data = json.loads(Path(key).read_text(encoding="utf-8"))
    except Exception:
        return []
    raw = data.get("project", {}).get("after", [])
    if not isinstance(raw, list):
        return []
    return [_dep_key(a) for a in raw if isinstance(a, str) and a.strip()]


def chain_status(store, after_keys):
    """Readiness of a run's `after` dependencies.

    A dependency is done when EVERY task id in its taskfile has a 'merged'
    row — rows-only would complete prematurely: a dep run killed after 3 of
    4 tasks leaves 3 merged rows and no evidence the 4th ever existed, so
    the ids are parsed from disk. A dep with rows failed/conflict BLOCKS the
    chain; anything else (rows missing, rows still running, taskfile not on
    disk yet) is waiting, not failing — chains are declared before upstream
    projects are planned.
    """
    st = {"ok": True, "waiting": [], "failed": {}, "deps": []}
    for key in after_keys:
        ids = _taskfile_ids(key)
        rows = {r["id"]: r["status"] for r in store.code_tasks_for(key)}
        bad = sorted(i for i, s in rows.items() if s in ("failed", "conflict"))
        st["deps"].append({
            "taskfile": key,
            "readable": ids is not None,
            "n_tasks": len(ids) if ids is not None else None,
            "merged": sum(1 for s in rows.values() if s == "merged"),
            "unmerged": [i for i in (ids or []) if rows.get(i) != "merged"],
            "failed": bad,
        })
        if bad:
            st["failed"][key] = bad
        elif ids is None or any(rows.get(i) != "merged" for i in ids):
            st["waiting"].append(key)
    st["ok"] = not st["failed"] and not st["waiting"]
    return st


def pending_chains(store):
    """Chain readiness for every taskfile in TASKS_DIR that declares `after`
    (feeds `main.py code status`). Light parse only — a broken taskfile must
    not take the status command down with it."""
    out = []
    d = Path(config.TASKS_DIR)
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.json")):
        raw = _read_after(f)
        if not raw:
            continue
        st = chain_status(store, raw)
        out.append({"taskfile": str(f), "after": raw, "ready": st["ok"],
                    "waiting": st["waiting"], "failed": st["failed"]})
    return out


def _after_cycle(own_key, after_keys, max_depth=8):
    """A taskfile-key path from an `after` entry back to `own_key`, or None.

    Depth-bounded DFS over the `after` edges found on disk; unreadable files
    simply have no outgoing edges (they may not exist yet). Depth-bounded
    because a chain loop among files that all exist is the only error this
    needs to catch, not full graph theory on a directory of taskfiles.
    """
    seen = set(after_keys)

    def walk(key, path):
        if len(path) >= max_depth:
            return None
        for dep in _read_after(key):
            if dep == own_key:
                return path + [dep]
            if dep in seen:
                continue
            seen.add(dep)
            hit = walk(dep, path + [dep])
            if hit:
                return hit
        return None

    for k in after_keys:
        hit = walk(k, [k])
        if hit:
            return hit
    return None


_CHAIN_POLL_S = 10.0


def _make_chain_wait(store, taskfile, after_keys):
    """The chain gate node: poll `after` deps until every one is fully
    merged, one of them fails, or config.CHAIN_TIMEOUT expires. It runs
    before any alloc, so while it waits there is no worktree, branch or
    task row for this taskfile — a cancelled wait leaves nothing behind."""

    async def chain_wait(ctx):
        t0 = time.monotonic()
        warned = set()
        events.emit("chain.wait", taskfile=taskfile, deps=after_keys)
        while True:
            st = chain_status(store, after_keys)
            if st["ok"]:
                waited = round(time.monotonic() - t0, 1)
                events.emit("chain.ready", taskfile=taskfile,
                            deps=after_keys, waited_s=waited)
                return {"ok": True, "waited_s": waited}
            if st["failed"]:
                reason = "dependency failed: " + "; ".join(
                    f"{Path(k).name}: {', '.join(v)}"
                    for k, v in st["failed"].items())
                events.emit("chain.blocked", taskfile=taskfile,
                            failed_tasks=st["failed"])
                return {"ok": False, "reason": reason, "failed": st["failed"]}
            for d in st["deps"]:
                if d["taskfile"] in warned:
                    continue
                warned.add(d["taskfile"])
                if not d["readable"]:
                    log.info("chain: waiting for %s (taskfile not on disk "
                             "yet or unreadable)", Path(d["taskfile"]).name)
                elif d["n_tasks"]:
                    log.info("chain: waiting for %s (%d/%d merged)",
                             Path(d["taskfile"]).name, d["merged"], d["n_tasks"])
                else:
                    log.info("chain: waiting for %s (no tasks parsed)",
                             Path(d["taskfile"]).name)
            waited = time.monotonic() - t0
            if waited > config.CHAIN_TIMEOUT:
                names = ", ".join(Path(k).name for k in st["waiting"])
                reason = (f"chain wait timed out after {waited:.0f}s "
                          f"(waiting: {names})")
                events.emit("chain.blocked", taskfile=taskfile,
                            reason="timeout", waited_s=round(waited, 1),
                            waiting=st["waiting"])
                return {"ok": False, "reason": reason, "timeout": True,
                        "waiting": st["waiting"]}
            await asyncio.sleep(min(_CHAIN_POLL_S, config.CHAIN_TIMEOUT))

    return chain_wait


BOARD_ETIQUETTE = (
    "COORDINATE ON THE BOARD — one JSON line per post in .arc/board.jsonl\n"
    "- claim shared files before editing (others' live claims on your files "
    "are listed above): {\"kind\":\"claim\",\"body\":\"claiming audit.py\","
    "\"refs\":{\"paths\":[\"audit.py\"]}}; a CLAIM CONFLICTS block above means "
    "ASK first.\n"
    "- ask instead of guessing an interface: {\"kind\":\"question\",\"channel\":"
    "\"task:<id>\",\"body\":\"@<id> is plan_tasks() sync?\",\"mentions\":[\"<id>\"]}\n"
    "- answer with evidence, not prose: {\"kind\":\"answer\",\"reply_to\":\"<id>\","
    "\"body\":\"async — code_tasks.py:1564\"}\n"
    "- post a result with the files you changed: {\"kind\":\"result\",\"body\":"
    "\"validation added\",\"refs\":{\"files\":[\"agentboard.py\"]}}\n"
    "- blocked past 2 fix rounds: {\"kind\":\"blocker\",\"body\":\"...\"} — the "
    "captain acts on blockers.\n"
    "Mentions must name a real task id, model, or @all/@captain/@operator; a "
    "bare copy of this prompt is rejected as an 'error' post.\n")


def _impl_prompt(t, feedback, hints="", roster=None, board="", contract="",
                 dossier=""):
    """The implementer's whole world: the task, where its code is, the rules.

    `hints` is graft.hints_block output — the file:line spans the code graph
    ranks for this task — or "" when there is no graph. With hints the agent
    is told to READ those ranges first; without them it is told to search,
    which is what it would do anyway, just more expensively. `roster` (every
    task id + title in the plan) turns the plan-amendment channel on: without
    the real ids a proposal can only guess dep targets, and a guess costs a
    rejection. `dossier` (dossier.render) is the task's durable history; it
    leads the prompt so a restarted or swapped-in model boots from it.
    """
    p = (
        (dossier + "\n" if dossier else "") +
        f"You are implementing one task in this repository.\n\n"
        f"TASK {t['id']}: {t['title']}\n\n{t['prompt']}\n"
    )
    if contract:
        p += "\n" + contract.strip() + "\n"
    if t["files_hint"]:
        p += f"\nFiles you are expected to touch: {', '.join(t['files_hint'])}\n"
    if hints:
        p += "\n" + hints
    tooling = graft.tooling_prose()
    if tooling:
        p += "\n" + tooling
    if hints:
        locate = ("- Start from the WHERE TO LOOK spans above; use the graft "
                  "commands for anything else. ")
    elif tooling:
        locate = "- Locate code with the graft commands above FIRST. "
    else:
        locate = "- Locate code with grep/search FIRST; "
    p += (
        "\nRules: make only the changes this task requires; do not git-commit "
        "(the orchestrator handles git); keep changes minimal and working.\n"
        "\nKeep every request small — BOTH what you send and what you write. "
        "The API terminates connections on long-running requests: measured on "
        "this fleet, tasks doing whole-file rewrites fail 33% of their "
        "requests versus 8% for tasks making targeted edits, and each failure "
        "costs about five minutes of retry. Long answers are what get killed. "
        "So:\n"
        "- NEVER rewrite a whole file. Edit the specific lines that must "
        "change, even when the task description sounds like a rewrite. A "
        "500-line file emitted in one response is the single most likely way "
        "to fail this task.\n"
        + locate +
        "keep every read to just the line ranges you need — never open a "
        "whole large file.\n"
        "- Do not re-read a file you have already seen; rely on what is "
        "already in the conversation.\n"
        "- Work in several small edits, each one verified, rather than one "
        "sweeping change. Many short requests succeed where one long one "
        "does not.\n"
    )
    if feedback:
        p += f"\nPrevious attempt was rejected. Fix these issues:\n{feedback}\n"
    p += dossier_mod.HANDOFF_PROMPT
    if board:
        p += "\n" + board + "\n" + BOARD_ETIQUETTE
    if roster:
        p += plan_amend.prompt_block(roster)
    return p


async def _changed_files(wt, base):
    """Paths this task changed vs its merge base, untracked included."""
    try:
        rc, mb, _ = await gitstore._git(["merge-base", base, "HEAD"], cwd=wt,
                                        check=False)
        ref = mb.strip() if rc == 0 and mb.strip() else "HEAD"
        _, diff, _ = await gitstore._git(["diff", "--name-only", ref], cwd=wt,
                                         check=False)
        _, new, _ = await gitstore._git(
            ["ls-files", "--others", "--exclude-standard"], cwd=wt, check=False)
    except Exception:
        return []
    return sorted({p for p in (diff + "\n" + new).splitlines()
                   if p and not p.startswith((".arc/", ".reasonix/"))})


def _harness_of(model):
    """The local harness that runs this model, from the roster."""
    return config.MODEL_HARNESS.get(model, "opencode")


def _seat_blocked(model, usage):
    """True when this harness has a recent driver.usage_limit still in force."""
    harness = _harness_of(model)
    if usage.get(f"usage_limit:{harness}"):
        return True
    return drivers._usage_blocked_until.get(harness, 0) > time.time()


def _reviewer_rank(model, usage):
    """Lower sorts first: DeepSeek, GLM, other seats, then Claude.

    A full or usage-blocked seat sorts after every free seat.
    Among subscription seats the least contended (most headroom) wins.
    """
    fam = config.MODEL_FAMILY.get(model)
    pressure = _reviewer_pressure(model, usage)
    order = {"deepseek": 0, "glm": 1, "anthropic": 3}.get(fam, 2)
    return (
        1 if _seat_blocked(model, usage) or pressure >= 1.0 else 0,
        order,
        pressure,
        -_tier_rank(model),
    )


def _reviewer_pressure(model, usage):
    """How contended this reviewer is, 0.0 (idle) to 1.0+ (at a ceiling).

    Whichever ceiling binds FIRST wins: a model comfortably under its own cap
    is not actually available if the harness it shares with another model is
    full. Scoring on the model alone once sent every review to the opencode
    models while their single local pool sat at 5/5 with seven reviewers
    queued behind it and another harness idle at 1/3.
    """
    h = _harness_of(model)
    return max(usage.get(model, 0) / max(1, config.driver_limit(model)),
               usage.get(f"harness:{h}", 0) / max(1, config.harness_limit(h)))


def _eligible_pr_reviewers(impl_fam, pol):
    """Cross-family models that may actually review an open PR.

    Eligibility is decided by CONSTRUCTING the driver, not by a second list
    kept alongside the drivers' own rules. Those two drifted apart once and it
    cost seven pull requests: the pool named DeepSeek, the driver refused the
    role, and the ValueError — which the reviewer wrapper did not catch —
    killed pr_review one second after each PR opened, leaving the branch and
    the PR stranded with nobody coming back for them.
    """
    out = []
    for m in config.ESCALATION_PATH[::-1]:  # strongest first, from the roster
        # Default: cross-family only. Under ARC_ALLOW_SAME_FAMILY_REVIEW the
        # cross-family reviewer is the thing that is down, so the pool
        # INVERTS to only the implementer's own family.
        same = config.MODEL_FAMILY.get(m) == impl_fam
        if same != config.ALLOW_SAME_FAMILY_REVIEW:
            continue
        try:
            _driver(m, "pr_reviewer", pol)
        except ValueError:
            continue
        out.append(m)
    return out


_FLEET_COMMENT_PREFIXES = ("**Changes requested**", "Noted, non-blocking")


def _is_fleet_comment(body):
    """Comments the fleet itself posts: never a human's review feedback."""
    b = (body or "").lstrip()
    if b.startswith(_FLEET_COMMENT_PREFIXES):
        return True
    # Per-reviewer verdicts are "**<model>** (round N) — ..."
    return bool(re.match(r"\*\*[^*]+\*\* \(round \d+\)", b))


MANUAL_LOCAL_POLL = 2.0     # seconds between reads of the manual_reviews table


async def _await_manual_review(repo, tid, number, round_n, *, project="",
                               url="", title="", reviewers=None):
    """Hold a fleet-approved PR until a human decides (manual_review.wanted).

    Two ways to decide, whichever comes first:
      * the dashboard's "Needs you" queue (desktop or phone), which writes the
        decision to the manual_reviews table this loop polls;
      * a GitHub label on the PR (manual-approved / manual-rejected + comment).

    Returns {"decision": "approved"|"rejected"|"timeout", "issues": [...]}.
    A timeout is reported as REJECTED with a clear issue rather than merged:
    the gate exists because a person wanted the last word, so running out of
    patience must never turn into a merge nobody approved.
    """
    started = time.time()
    try:
        held = manual_review.request(tid, number, round_n, project=project,
                                     repo=str(repo), url=url, title=title,
                                     reviewers=reviewers)
    except Exception as exc:                        # noqa: BLE001
        # The GitHub label path still works without the table.
        errors.capture(exc, task=tid, node="manual_review.request")
        held = None
    events.emit("task.pr_awaiting_manual", task=tid, pr=number, round=round_n,
                project=project, url=url,
                approve_label=config.PR_MANUAL_APPROVED_LABEL,
                reject_label=config.PR_MANUAL_REJECTED_LABEL)
    if not (held and held["status"] != "waiting"):
        await gitstore._gh(["pr", "comment", str(number), "--body",
                            f"**Awaiting manual review** (round {round_n}). The fleet "
                            "approved this PR. Decide in the dashboard's **Needs you** "
                            f"queue, or label it `{config.PR_MANUAL_APPROVED_LABEL}` "
                            f"to merge, or `{config.PR_MANUAL_REJECTED_LABEL}` and leave "
                            "a comment saying what to change."], cwd=repo)
    last_gh = 0.0
    while True:
        try:
            held = manual_review.get(tid, number, round_n)
        except Exception:                           # noqa: BLE001
            held = None
        if held and held["status"] in ("approved", "rejected"):
            manual_review.settle(tid, number, round_n, held["status"])
            by = held.get("decided_by") or "dashboard"
            comment = (held.get("comment") or "").strip()
            if held["status"] == "approved":
                body = f"**Manual review** (round {round_n}) — approved by a human ({by})."
                if comment:
                    body += "\n\n" + comment
                await gitstore._gh(["pr", "comment", str(number), "--body", body], cwd=repo)
                events.emit("task.pr_manual", task=tid, pr=number, decision="approved",
                            via=by, waited_s=round(time.time() - started))
                return {"decision": "approved", "issues": []}
            issues = [comment or "Rejected in manual review (no comment was left; "
                                 "ask the reviewer what to change)."]
            await gitstore._gh(["pr", "comment", str(number), "--body",
                                f"**Manual review** (round {round_n}) — changes "
                                f"requested by a human ({by}):\n\n{issues[0]}"], cwd=repo)
            events.emit("task.pr_manual", task=tid, pr=number, decision="rejected",
                        via=by, n_issues=1, waited_s=round(time.time() - started))
            return {"decision": "rejected", "issues": issues}
        # The table is local and cheap, so it is read every couple of seconds
        # (a click in the dashboard takes effect at once); GitHub only every
        # PR_MANUAL_POLL, since its API quota is shared by the whole fleet.
        rc, out = 1, ""
        if time.time() - last_gh >= config.PR_MANUAL_POLL:
            last_gh = time.time()
            rc, out, _ = await gitstore._gh(
                ["pr", "view", str(number), "--json", "labels,comments,state"], cwd=repo)
        if rc == 0:
            try:
                doc = json.loads(out)
            except ValueError:
                doc = {}
            labels = {l.get("name") for l in doc.get("labels") or []}
            if doc.get("state") == "MERGED" or config.PR_MANUAL_APPROVED_LABEL in labels:
                manual_review.settle(tid, number, round_n, "approved", by="github label")
                events.emit("task.pr_manual", task=tid, pr=number, decision="approved",
                            via="github label", waited_s=round(time.time() - started))
                return {"decision": "approved", "issues": []}
            if config.PR_MANUAL_REJECTED_LABEL in labels:
                issues = []
                for c in doc.get("comments") or []:
                    body = (c.get("body") or "").strip()
                    try:
                        at = datetime.fromisoformat(c.get("createdAt", "").replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        at = 0
                    if body and at >= started - 5 and not _is_fleet_comment(body):
                        issues.append(body[:2000])
                if not issues:
                    issues = ["Rejected in manual review (no comment was left; "
                              "ask the reviewer what to change)."]
                # Clear the label so the NEXT round waits for a fresh decision
                # instead of being rejected again by this one.
                await gitstore._gh(["pr", "edit", str(number), "--remove-label",
                                    config.PR_MANUAL_REJECTED_LABEL], cwd=repo)
                manual_review.settle(tid, number, round_n, "rejected", by="github label",
                                     comment="\n\n".join(issues))
                events.emit("task.pr_manual", task=tid, pr=number, decision="rejected",
                            via="github label", n_issues=len(issues),
                            waited_s=round(time.time() - started))
                return {"decision": "rejected", "issues": issues}
        if config.PR_MANUAL_TIMEOUT and time.time() - started > config.PR_MANUAL_TIMEOUT:
            manual_review.settle(tid, number, round_n, "timeout", by="timeout")
            events.emit("task.pr_manual", task=tid, pr=number, decision="timeout")
            return {"decision": "rejected",
                    "issues": [f"No manual review decision within "
                               f"{int(config.PR_MANUAL_TIMEOUT)}s "
                               "(ARC_PR_MANUAL_TIMEOUT)."]}
        await asyncio.sleep(min(config.PR_MANUAL_POLL, MANUAL_LOCAL_POLL))


def _tally_reviews(outcomes):
    """(issues, approvals, crashed, approved, inconclusive) for one PR round.

    Module-level so it is TESTED rather than re-implemented in a test. A
    previous version of this logic was verified by a copy of itself living in
    the test file, which mutation testing showed catches nothing: breaking the
    real code left the suite green.

    `inconclusive` is the distinction that matters — nobody objected, but a
    reviewer never ran, so the round reached no verdict. That is a review to
    retry, not a change to request: the diff has not been read.
    """
    issues, approvals, crashed = [], [], []
    for model, v in outcomes:
        if v.get("crashed"):
            crashed.append(model)
        elif v["approve"]:
            approvals.append(model)
        else:
            issues.extend(f"[{model}] {i}" for i in v["issues"])
    approved = bool(outcomes) and len(approvals) == len(outcomes)
    return issues, approvals, crashed, approved, bool(crashed) and not issues


def _collect_follow_ups(outcomes):
    """`[model] text` for every PRE-EXISTING finding the round filed.

    Module-level for the same reason `_tally_reviews` is: the join's feedback
    path must be tested against the real thing. These are recorded on
    `task.pr_reviewed` and never become PR-requested changes — a reviewer
    reads the full diff, so a wart the task never touched used to block a
    merge and burn one of the implementer's fix rounds on work it did not do.
    """
    out = []
    for model, v in outcomes:
        if v.get("crashed"):
            continue
        out.extend(f"[{model}] {f}" for f in v.get("follow_ups") or [])
    return out


def _tier_index_m(model):
    try:
        return config.ESCALATION_PATH.index(model)
    except ValueError:
        return None

def _next_tier_m(model):
    """The next stronger model for `model`, or None at the top.

    A model that is not ON the escalation path (a taskfile may still route
    explicitly to gpt-oss-120b for mechanical work) counts as below the
    entry tier, so it escalates INTO the path rather than being stuck
    unable to escalate at all.
    """
    idx = _tier_index_m(model)
    if idx is None:
        return config.ESCALATION_PATH[0] if config.ESCALATION_PATH else None
    if idx + 1 < len(config.ESCALATION_PATH):
        return config.ESCALATION_PATH[idx + 1]
    return None


# Public names for the dashboard's manual escalation, so it applies the SAME
# tier arithmetic the graph applies rather than a second copy of it.
_tier_index = _tier_index_m
_next_tier = _next_tier_m


def _reviewer_for(t, model):
    """Cross-review preserved under escalation, from the roster.

    Keep the taskfile's reviewer when it is still review-capable and still a
    different family from the implementer; otherwise take the strongest other
    review-capable family. Module-level so the dashboard's manual escalation
    applies the same rule the graph does — the reviewer must follow the
    implementer, and it must never be the implementer's own family — unless
    ARC_ALLOW_SAME_FAMILY_REVIEW is on (the cross-family reviewer is down)."""
    current = t.get("reviewer")
    fam = config.MODEL_FAMILY.get(model)
    if current in config.REVIEW_FAMILIES and (
            current != fam or config.ALLOW_SAME_FAMILY_REVIEW):
        return current
    return config.cross_family_reviewer(model) or current


_GATE_FAILURE = re.compile(
    r"^(FAIL|ERROR): |^AssertionError|^Traceback \(most recent call last\)"
    r"|^FAILED\b|^not ok\b|^✗|^_{2,}.+_{2,}$|^E\s+(AssertionError|\w+Error\b)")


def _gate_failures(output, limit=12):
    """The gate-output lines that NAME what failed, first occurrence order.

    The fix loop's feedback window is small, and on a check.sh run that
    window used to be the unittest summary and the shell's own echo, which
    say THAT something failed, not WHAT (code_tasks.gate_feedback now
    extracts the failing sections ahead of that tail). empty-diff-publish
    failed its worktree gate on exactly one of 1046 tests, the failing line
    ("FAIL: test_every_edge_condition_reads_as_english ...") sat a hundred
    lines above the cut, and the implementer was told only "unit tests
    failed". The full log is on disk (log_path) for a human; the model
    needs the names IN the feedback.

    Covers unittest `FAIL:`/`ERROR:`, pytest short summaries (`FAILED x::y`,
    needs `-rf`), pytest's default FAILURES-section underlines
    (`_____ test_x _____`) and its `E`-prefixed exception lines, TAP
    (`not ok`), and bare tracebacks (`^Traceback`, `^AssertionError`).
    Pytest without a FAILURES section (xdist summary-only modes, `-q`
    pass-with-warnings) and TAP `# TODO` expected failures yield lines a
    green run could print — so the caller prepends the names block only
    when the gate actually failed (see gate()).
    """
    hits, seen = [], set()
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line in seen or not _GATE_FAILURE.search(line):
            continue
        seen.add(line)
        hits.append(line[:200])
        if len(hits) >= limit:
            break
    return hits


_GATE_FAIL_LINE = re.compile(r"^(FAIL|ERROR):")
_GATE_FULL_LIST_HEADER = "--- failing checks (full list) ---"


def _gate_fail_lines(output, limit=40):
    """EVERY `FAIL:`/`ERROR:` line in the output, not just the ones near the cut.

    Rule 4's feedback window is small, and a check.sh
    run puts the unittest summary and the shell's echo under that cut: on
    2026-09-15 a gate kept 1 of 15 failing test names, and three worktrees
    spent a fix round hunting for the other fourteen. _gate_failures() covers
    more formats but stops at 12 names; this is the narrower, longer list that
    goes beside the tail in the log file
    (`logs/gates/<project>/<task>/x<attempt>.log`, code_tasks.gate_log_path)
    and in the `task.gate` event, so the names survive outside the cut.
    """
    hits, seen = [], set()
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line in seen or not _GATE_FAIL_LINE.match(line):
            continue
        seen.add(line)
        hits.append(line[:200])
        if len(hits) >= limit:
            break
    return hits


def _gate_full_list_block(lines):
    """Header plus lines, or "" — shared by the log file and the event."""
    if not lines:
        return ""
    return _GATE_FULL_LIST_HEADER + "\n" + "\n".join(lines)


_TRUNCATION_MARKER = "\n\n... [%d bytes omitted] ...\n\n"


def cap_log(text, max_bytes=None):
    """Head + tail of `text`, with the dropped middle named in BYTES.

    Rule 4 keeps the gate's full output on disk, and an unbounded file is how
    a runaway test run fills the disk. Bytes, not characters: the cap is a
    disk budget, and a harness transcript is full of multi-byte text (a
    len(str)-based cap would let a 5 MB file be several times that).

    Head AND tail, deliberately: for a test run the failure is usually in the
    middle (the traceback) but the SUMMARY is at the end, and for a build the
    first error is at the top. Cutting either end loses something a human
    opens the log to find. Decoding is errors="replace" so a cut cannot land
    mid-codepoint and make the file unreadable.
    """
    limit = config.GATE_LOG_MAX_BYTES if max_bytes is None else int(max_bytes)
    raw = text.encode("utf-8", errors="replace")
    if limit <= 0 or len(raw) <= limit:
        return text
    marker = _TRUNCATION_MARKER % (len(raw) - limit)
    mlen = len(marker.encode("utf-8"))
    # The marker is part of the budget: `limit` is the cap on the FILE, which
    # has already been overrun by omitting anything at all.
    room = max(limit - mlen, 0)
    head, tail = room // 2, room - room // 2
    # Move both cuts off any UTF-8 continuation byte. `errors="replace"` turns
    # a split codepoint into a 3-byte U+FFFD — the file then comes out OVER
    # the cap it was capped to (measured: 305 bytes for a 300-byte cap), which
    # defeats the one guarantee this function makes. `raw` is valid UTF-8 by
    # construction (it was encoded with the same handler), so aligning the two
    # cuts is enough.
    head_end, tail_start = head, len(raw) - tail
    while head_end and raw[head_end] & 0xC0 == 0x80:
        head_end -= 1
    while tail_start < len(raw) and raw[tail_start] & 0xC0 == 0x80:
        tail_start += 1
    return (raw[:head_end].decode("utf-8", errors="replace") + marker
            + raw[tail_start:].decode("utf-8", errors="replace"))


# The failing BLOCKS, not just their first lines. `_gate_failures` gives the
# implementer the NAMES of what failed; that is enough to know where to look
# and not enough to know what broke. The lines under a `FAIL:` header are the
# test's own traceback, and under untittest that is the entire answer.
_SECTION_START = re.compile(
    r"^(FAIL|ERROR): "                      # unittest and Godot both use this
    r"|^_{2,}\s*\S.*_{2,}$"                 # pytest FAILURES-section separator
    r"|^SCRIPT ERROR"                       # Godot: a script blew up
    r"|^Traceback \(most recent call last\)")
# Where a block ends: the run's SUMMARY, not its fences. `----` fences open
# unittest's failure sections (right under the `FAIL:` header) as well as
# closing them, so treating a fence as an end kept only the header line and
# threw the traceback away — the exact loss this function exists to prevent.
_SECTION_END = re.compile(r"^Ran \d+ tests?\b|^OK$|^FAILED\b")
# What may START the next block. `Traceback` is deliberately NOT here: inside
# a unittest `FAIL:` block it is the block's OWN traceback, so breaking on it
# truncated every block to its header line.
_SECTION_BREAK = re.compile(
    r"^(FAIL|ERROR): |^_{2,}\s*\S.*_{2,}$|^SCRIPT ERROR")
# What CONTINUES a block on an unindented line. `Traceback (most recent call
# last)` is here and NOT in _SECTION_BREAK: inside a unittest `FAIL:` block it
# is that block's own traceback, and treating it as the next block's start
# split every failure in two (header+fence, then a headerless traceback) and
# burned one of the three section slots on the fragment.
_SECTION_CONT = re.compile(
    r"^Traceback \(most recent call last\)"
    r"|^\w*(Error|Exception|Warning)\b"
    r"|^E\s+\w*(Error|Exception)\b"
    r"|^\s*at: "                            # Godot stack frame
    r"|^(res://|godot|Godot)"               # Godot echo / version banner
    r"|^-{10,}$|^=+$")                      # unittest section fences


def _failing_sections(output, limit=3, block_bytes=1500):
    """The FAILING sections of a gate log, each headed by its own failure line.

    The tail (and even the names list) tells the implementer WHAT failed; the
    traceback under the name tells it WHY. On a check.sh run the tracebacks
    sit exactly where the 2000-char cut throws them away — the summary and the
    shell's echo are what survive — so the fix round starts by re-running the
    test to see the error the gate already had in hand.

    Covers unittest (`FAIL:`/`ERROR:` then its own traceback), pytest
    (`____ test_x ____` separators) and Godot (`FAIL:`/`SCRIPT ERROR`, whose
    stack and echo lines the block keeps), all bounded by the run's summary.

    Bounded on both axes (few sections, bounded bytes each): this goes into a
    PROMPT, so an extractor that can emit the whole log is no better than the
    tail it replaced.
    """
    lines = output.splitlines()
    out, i = [], 0
    while i < len(lines) and len(out) < limit:
        if not _SECTION_START.match(lines[i].strip()):
            i += 1
            continue
        start = i
        i += 1
        # Inside a unittest traceback every line is indented or blank, and the
        # block ends at the first unindented line that is neither the next
        # failure's header nor a Godot stack/echo line. Capped so one runaway
        # traceback cannot consume the whole prompt budget.
        while i < len(lines) and i - start < 80:
            probe = lines[i].strip()
            if _SECTION_END.match(probe):
                break
            if probe and not lines[i][:1].isspace():
                if _SECTION_BREAK.match(probe):
                    break
                # An unindented line belongs to this block only when it is one
                # of the block's own continuation shapes.
                if not _SECTION_CONT.match(probe):
                    break
            i += 1
        block = "\n".join(lines[start:i]).strip()
        out.append(block.encode("utf-8")[:block_bytes].decode("utf-8", "replace"))
    return out


_FEEDBACK_SEP = "\n\n"
_FEEDBACK_SEP_B = len(_FEEDBACK_SEP.encode("utf-8"))
# The tail's guaranteed share. The tail is what makes the block elastic, but
# "elastic" must not mean "evicted": a feedback block that explains the
# failures and then shows NO output is worse than a slightly shorter one.
_TAIL_FLOOR = 600


def _clip_bytes(text, limit, from_end=False):
    """`text` cut to at most `limit` BYTES, never mid-codepoint.

    Byte-exact, like `cap_log` and for the same reason: the budget is a prompt
    budget, and a cut that lands inside a multi-byte character decodes to a
    3-byte U+FFFD and puts the result over the limit it was cut to.
    """
    raw = text.encode("utf-8")
    if limit <= 0:
        return ""
    if len(raw) <= limit:
        return text
    if from_end:
        start = len(raw) - limit
        while start < len(raw) and raw[start] & 0xC0 == 0x80:
            start += 1
        return raw[start:].decode("utf-8", "replace")
    end = limit
    while end and raw[end] & 0xC0 == 0x80:
        end -= 1
    return raw[:end].decode("utf-8", "replace")


def gate_feedback(output, log_path=None, names=None, budget=6000):
    """What the implementer is handed after a failed gate: the failing SECTIONS,
    then the tail, and the path to the full log.

    Replaces the blind `output[-2000:]` that Rule 4 used to specify. Every
    part is bounded, so the whole thing fits a prompt the way the 2000-char
    window did.

    The allocation order is the whole point, and it is: **path, sections,
    tail** — the reverse of how this was first written. The path was appended
    LAST and the assembled block was sliced from the FRONT, so it was the
    first thing evicted: with three sections at their cap the block came out
    at 6000 bytes with no path, and a long name list at 6221 with neither
    path nor tail — over its own budget as well as missing the one line a
    reader cannot reconstruct. So the path is reserved first, the head is
    clipped to what is left after the tail's floor, and only the tail absorbs
    the remainder.
    """
    budget = int(budget)
    path_part = f"full log: {log_path}" if log_path else ""

    sections = _failing_sections(output)
    head = []
    if sections:
        head.append("failing sections:\n" + "\n\n".join(sections))
    if names:
        head.append("failing checks:\n" + "\n".join(f"  {n}" for n in names))
    raw_head = _FEEDBACK_SEP.join(head)

    tail = output[-2000:].strip()
    tail_header = "gate output (tail):\n"

    # Set aside the path and the tail's floor, then clip the HEAD to what is
    # left. An unbounded head is what pushed the block over budget; the
    # sections lead the head, so a clip takes the names first.
    reserve = len(path_part.encode("utf-8")) + _FEEDBACK_SEP_B if path_part else 0
    floor = len(tail_header.encode("utf-8")) + _TAIL_FLOOR if tail else 0
    head_text = _clip_bytes(raw_head, max(budget - reserve - floor - _FEEDBACK_SEP_B, 0))
    if head_text != raw_head and "\n" in head_text:
        # A clipped head must not end mid-line: half a test name reads as a
        # real one, and the name list is the only line-shaped part here.
        head_text = head_text.rsplit("\n", 1)[0]

    parts = (1 if head_text else 0) + (1 if tail else 0) + (1 if path_part else 0)
    fixed = (len(head_text.encode("utf-8")) + len(path_part.encode("utf-8"))
             + _FEEDBACK_SEP_B * max(parts - 1, 0))
    room = max(budget - fixed, 0)
    # The tail keeps its END — the run's summary is the newest line, and the
    # header is dropped only when it cannot fit at all.
    body = _clip_bytes(tail, max(room - len(tail_header.encode("utf-8")), 0),
                       from_end=True)
    tail_text = (tail_header + body) if body else ""
    return _FEEDBACK_SEP.join(
        p for p in (head_text, tail_text, path_part) if p)


def _slug(s):
    """A task/project id as one safe path segment (no separators, no '..')."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(s or "x")).strip("-.") or "x"


def gate_log_path(project, tid, attempt):
    """Where a gate's full output goes: logs/gates/<project>/<task>/x<n>.log.

    Returns the path as a string. The directory is NOT created here — the
    caller writes the file immediately and treats an OSError as "no log"
    rather than failing the gate over a diagnostic.

    Mirrors `save_review_log`'s layout on purpose: one directory per task
    holds the gate log and the raw reviewer output of every attempt, so an
    operator asking "what did round 3 actually see?" opens one folder.
    """
    return str(Path(config.ROOT) / "logs" / "gates"
               / _slug(project) / _slug(tid) / f"x{int(attempt)}.log")


def save_review_log(project, tid, attempt, model, text, kind="review"):
    """The raw reviewer output, kept when no verdict could be parsed.

    A reviewer that ends its session without the verdict JSON is recorded as
    `crashed` and the review is simply retried — so the ONE artefact that says
    why (it asked a question? it wrote prose? its JSON was truncated?) was
    previously thrown away, and the retry started with no more information
    than the first attempt had. Written under
    logs/gates/<project>/<task>/ beside the gate logs: same evidence trail,
    and never inside the worktree (publish's `git add -A` would ship it).

    Returns the path, or None if it could not be written — a diagnostic write
    must never be the thing that fails a review.
    """
    try:
        d = Path(config.ROOT) / "logs" / "gates" / _slug(project) / _slug(tid)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{_slug(kind)}-x{int(attempt)}.txt"
        path.write_text(cap_log(
            f"model: {model}\nrole: reviewer\nattempt: {attempt}\n\n{text}"))
        return str(path)
    except (OSError, ValueError, TypeError):
        return None


def _resume_session(results, tid, model, harness=None):
    """The harness session to continue for this fix round, or None.

    A rework continues only when the SAME model on the SAME harness is
    mending the worktree it just wrote. A session id is a file in that
    harness's own store (a Codex rollout, a Cursor chat). Handing a Cursor
    chat id to `codex exec resume` exits immediately with "no rollout found"
    and burns the retry ladder. A usage swap records the harness that
    actually ran; a mismatch starts fresh and the shared board carries what
    the other harness did. A recorded session with no harness cannot be
    proven to belong to this one, so it is not resumed either.
    """
    prev = (results or {}).get(f"implement_{tid}") or {}
    if prev.get("model") != model or not prev.get("session_id"):
        return None
    prev_h = prev.get("harness")
    if harness:
        if not prev_h or prev_h != harness:
            return None
    return prev["session_id"]


def wrote_the_code(ctx, tid, assigned, store=None):
    """The model whose diff the reviewers must not share a family with.

    cur_model is the assigned seat. A spent plan can move the attempt onto
    another harness, and implement() records that model on its result. A
    resume that starts at publish has no implement result in the graph, so
    the last successful implementer row in harness_runs is the same fact.
    Falling back to the assigned seat is only for a run that never recorded
    one.
    """
    ran = ((ctx or {}).get("results", {}).get(f"implement_{tid}") or {}).get("model")
    if ran in config.MODEL_FAMILY:
        return ran
    if store is not None:
        try:
            rows = store.harness_runs_prefix(tid)
        except Exception:
            rows = []
        # harness_runs_prefix is a LIKE prefix, so "t1" also returns "t10".
        # Only this task's own rows count.
        wrote = [r for r in rows
                 if r.get("task_id") == tid
                 and r.get("role") == "implementer" and r.get("exit_code") == 0
                 and r.get("model") in config.MODEL_FAMILY]
        if wrote:
            return wrote[-1]["model"]
    return assigned


def _rework_feedback(tid, results):
    """Why this task is being implemented again, most authoritative first.

    pr_review USED TO BE MISSING from this: reviewers rejected an open PR, the
    edge fired back into implement, and the implementer was handed an empty
    feedback string — because review_ and gate_ had both PASSED, which is how
    the task reached publish in the first place. It re-read its own finished
    work, correctly concluded there was nothing left to do, exited in two
    minutes with no commit, and the task died as "no changes to publish" with
    the reviewers' objections never delivered to anyone. The whole
    send-it-back path was inert.
    """
    parts = []
    pub = results.get(f"publish_{tid}") or {}
    if pub.get("resolve"):
        files = "\n".join(f"  - {f}" for f in pub.get("conflicts") or [])
        parts.append(
            f"This branch CONFLICTS with `{pub.get('base')}` and cannot be "
            f"merged as it stands.\n\n"
            f"A `git merge` is ALREADY IN PROGRESS in your worktree, with "
            f"conflict markers (<<<<<<<, =======, >>>>>>>) written into these "
            f"files:\n{files}\n\n"
            f"Resolve every one of them by EDITING the files: keep your task's "
            f"change AND the incoming change from the base branch — the base "
            f"moved on for its own reasons and reverting it is not a fix. "
            f"Remove every conflict marker. Do NOT run `git merge --abort`, "
            f"`git checkout --ours/--theirs` wholesale, or `git commit`; the "
            f"orchestrator commits for you once the verify gate passes.")
    pr = results.get(f"pr_review_{tid}")
    if pr and not pr.get("approved"):
        who = ", ".join(pr.get("reviewers") or []) or "the reviewers"
        parts.append(
            f"Your pull request was REVIEWED AND REJECTED by {who}. "
            f"The code you already wrote is on the branch and is NOT "
            f"acceptable as-is — you must change it. Do not conclude "
            f"the task is already done.\n"
            + "\n".join(f"- {i}" for i in pr.get("issues", [])))
    rev = results.get(f"review_{tid}")
    if rev and not rev.get("pass"):
        parts.append("Pre-merge review rejected it:\n"
                     + "\n".join(f"- {i}" for i in rev.get("issues", [])))
    gate = results.get(f"gate_{tid}")
    if gate and not gate.get("passed"):
        parts.append(f"The verify gate failed, output:\n{gate.get('output', '')}")
    return "\n\n".join(parts)


SCOPE_BLOCKING = "introduced-by-this-diff"
SCOPE_PRE_EXISTING = "pre-existing"


def _split_by_scope(raw_issues, raw_follow_ups=()):
    """(blocking, follow_ups) for one reviewer verdict.

    Reviewers read the FULL diff, so a wart the task never touched used to
    block a merge and burn one of the implementer's fix rounds on work it did
    not do. Every issue is now labelled, and the LABEL decides whether it may
    block:

      * `introduced-by-this-diff` — the change itself causes it. Blocks.
      * `pre-existing` — already there before this task touched anything.
        Collected as a follow-up: recorded and surfaced, never a merge
        blocker, never a fix round.

    An UNLABELLED issue (the old flat `{"issues": ["plain text"]}` format)
    counts as blocking: a reviewer that said something was wrong must not have
    it quietly dropped by a parser upgrade.
    """
    blocking, follow = [], []
    for it in raw_issues or []:
        if isinstance(it, dict):
            label = str(it.get("label") or "").strip().lower()
            text = str(it.get("text") or it.get("issue") or "").strip()
            if label == SCOPE_PRE_EXISTING:
                follow.append(text or str(it))
                continue
            blocking.append(text or str(it))
        else:
            blocking.append(str(it))
    for f in raw_follow_ups or []:
        text = f.get("text") if isinstance(f, dict) else f
        if str(text or "").strip():
            follow.append(str(text))
    return blocking, follow


def _scope_lock_prose(flag):
    """The SCOPE LOCK block both reviewer prompts end with.

    Written once: the pre-merge reviewer and the PR reviewer must hold the
    same line about what may block, or a change bounced for a pre-existing
    reason is re-implemented against nothing.
    """
    return (
        "SCOPE LOCK — an issue may only BLOCK this change if THIS DIFF "
        "introduced it. Label every issue you report:\n"
        f'- "{SCOPE_BLOCKING}" — this change causes it. These block.\n'
        f'- "{SCOPE_PRE_EXISTING}" — it was already there before this task '
        "touched anything (a flaky test, a wart in a file this diff merely "
        "passes through, an unrelated defect). These do NOT block: list them "
        "under follow_ups, where they are recorded and tracked separately.\n"
        "Do not silently drop a real pre-existing finding — labelling it is "
        "what keeps it visible without holding this change hostage.\n\n"
        "Reply with STRICT JSON only, no prose:\n"
        f'{{"{flag}": true, "issues": [], "follow_ups": []}}  or  '
        f'{{"{flag}": false, "issues": [{{"label": "{SCOPE_BLOCKING}", '
        f'"text": "file.py:42 — problem and what fixes it"}}], '
        '"follow_ups": ["file.py:9 — pre-existing, not this diff\'s job"]}\n'
        f'Set "{flag}" false ONLY when at least one issue is labelled '
        f'"{SCOPE_BLOCKING}". A "{SCOPE_PRE_EXISTING}" issue goes in follow_ups '
        f'and never sets "{flag}" false.'
    )


def _evidence_block(shown, driver):
    """The reviewer's visual-evidence block for a gate capture, or "".

    A dashboard capture (Rule 7e) says whether THIS reviewer can see the
    attached images — a blind model is told so, never asked to judge pixels
    it cannot see. A game capture (Rule 7d) keeps evidence.py's block."""
    if not shown:
        return ""
    if shown.get("kind") == ui_evidence.KIND:
        return ui_evidence.prompt_block(shown, drivers.sees_images(driver))
    return evidence.prompt_block(shown)


def _visual_review_prose(diff):
    """The VISUAL clause for a diff that touches the dashboard UI, else "".

    A passing DOM unit test can still ship a page that looks broken, so a UI
    diff is judged by looking at it. Non-UI diffs get nothing: their prompt
    stays byte-identical."""
    ui = ui_evidence.diff_touches_ui(diff)
    if not ui:
        return ""
    return ("VISUAL — this diff changes the dashboard UI ("
            + ", ".join(ui[:6]) + "). A UI change needs visual evidence: the "
            "VISUAL EVIDENCE block (before/after screenshots rendered by the "
            "orchestrator) and, when the look changes on purpose, updated golden "
            "screenshots under tests/visual/golden/ in this diff — the visual "
            "regression check in ./check.sh fails until they match. A visible "
            "regression (broken layout, clipped or overlapping text, lost "
            "contrast, a phone layout that overflows, a new JavaScript error) "
            "is BLOCKING, exactly like a failing test. Only phone.html has "
            "dark-mode styles; the other pages' -dark screenshots match their "
            "-light ones and do not show dark mode. "
            "If there is no VISUAL EVIDENCE block and no golden update for a "
            "change that alters what a page shows, reject it for missing "
            "visual evidence.\n")


def _review_prompt(t, diff, impact="", roster=None, board="", contract="",
                   dossier=""):
    p = (
        (dossier + "\n" if dossier else "") +
        f"You are reviewing an implementation produced by another AI agent.\n\n"
        f"TASK {t['id']}: {t['title']}\n\nSPEC:\n{t['prompt']}\n\n"
        f"The implementation already passed its automated verify gate "
        f"({t['verify_cmd'] or 'none'}). Here is the full diff:\n\n{diff}\n\n"
        + graft.impact_block(impact) +
        "Review for: spec compliance, correctness, and scope discipline "
        "(nothing unrelated).\n"
    )
    if config.REQUIRE_TESTS:
        p += ("A code change MUST come with tests that would FAIL without it. "
              "Documentation-only changes are exempt.\n")
    p += _visual_review_prose(diff)
    if board:
        p += "\n" + board + "\n" + BOARD_ETIQUETTE
    if roster:
        p += plan_amend.prompt_block(roster)
    if contract:
        p += "\n" + contract.strip() + "\n"
    p += "\n" + _scope_lock_prose("pass")
    return p


def _verdict_dict(obj, flag):
    """Scope-locked verdict built from a parsed reviewer JSON object.

    The LABELS decide, not the flag the model typed: the rule is that the flag
    is false ONLY when at least one issue is `introduced-by-this-diff`. A
    reviewer that wrote `false` while filing nothing but pre-existing findings
    was voting to hold this change hostage for work it did not do, so the
    label wins and the round passes with those findings kept as follow-ups.
    The reverse stays fail-closed: a blocking issue blocks even if the model
    typed true.
    """
    blocking, follow = _split_by_scope(obj.get("issues"), obj.get("follow_ups"))
    if not blocking and not follow and not obj.get(flag):
        # A bare rejection naming nothing: fail closed, but say so — a merge
        # must never rest on a verdict with no readable reason.
        blocking = [f'reviewer rejected it ("{flag}": false) without naming '
                    "an issue this diff introduced"]
    out = {flag: not blocking, "issues": blocking}
    if follow:
        out["follow_ups"] = follow
    return out


_MERGE_WAIT_STATUSES = frozenset({"BLOCKED", "UNSTABLE", "UNKNOWN"})


def _merge_should_wait(note, status, mergeable):
    """True when GitHub is not ready, rather than the files overlapping.

    A blocked or still-calculating pull request used to be stored as
    ``conflict``. That stops the task and everything waiting on it, for a
    check that has not finished. ``--auto`` in the gh message is the same
    case. An auth failure is a real stop, even if the status is still unknown.
    """
    low = (note or "").lower()
    if ("not logged" in low or "authentication" in low
            or "resource not accessible" in low or "http 401" in low
            or "http 403" in low):
        return False
    if ((status or "").upper() in _MERGE_WAIT_STATUSES
            or (mergeable or "").upper() == "UNKNOWN"):
        return True
    return ("--auto" in low or "required status check" in low
            or "still being calculated" in low)


def _parse_verdict(text):
    """Last balanced span carrying a "pass" key wins.

    The flat regex could not span braces inside quoted code in the verdict
    prose (e.g. "{WORLD_X,WORLD_Z,WORLD_H}"), silently dropping real verdicts.

    Scope-locked (review-scope-lock): `issues` are the BLOCKING ones and
    `follow_ups`, present only when a reviewer filed one, carries the
    pre-existing findings. The old flat format still parses, and its
    unlabelled issues are blocking.
    """
    spans = []
    for m in re.finditer(r"\{", text):
        span = _balanced_span(text, m.start())
        if span is not None:
            spans.append(span)
    for span in reversed(spans):
        try:
            obj = json.loads(span)
        except ValueError:
            continue
        if isinstance(obj, dict) and "pass" in obj:
            return _verdict_dict(obj, "pass")
    # Last-ditch salvage for a verdict whose JSON never loads — reasonix
    # renders review prose into `.result` and an unbalanced quote in it
    # truncates the payload (measured 2026-09-15: json error "Expecting ','
    # delimiter" around char 837 of two real reviews). Fail-safe polarity:
    # a salvaged `false` is honoured as a rejection, because the reviewer did
    # say no; a salvaged `true` is NOT trusted, because the unreadable span
    # may have carried blocking issues the reviewer wrote down — so the
    # truncated/crash fallback stays exactly as it was.
    last = None
    for m in re.finditer(r'"pass"\s*:\s*(true|false)', text):
        last = m
    if last is not None and last.group(1) == "false":
        return {"pass": False, "salvaged": True,
                "issues": ["verdict JSON malformed; reviewer indicated failure"]}
    return {"pass": False, "truncated": True,
            "issues": ["reviewer returned no parseable verdict"]}


def _pr_review_prompt(t, diff, n_reviewers, round_n, prior_issues, impact="",
                      roster=None, board="", contract="", dossier=""):
    """Prompt for a reviewer reading a real pull request.

    Deliberately different from the pre-PR review: this reviewer can BLOCK the
    change, so it is told what it owns, that tests are mandatory, and that
    deferring to a colleague is not its job.
    """
    p = (
        (dossier + "\n" if dossier else "") +
        f"You are one of {n_reviewers} independent reviewers on a PULL REQUEST "
        f"opened by another AI agent. Your approval is REQUIRED to merge — "
        f"nothing has landed yet, and if you reject it, it goes back to the "
        f"implementer.\n\n"
        f"TASK {t['id']}: {t['title']}\n\nSPEC:\n{t['prompt']}\n\n"
        f"This is review round {round_n}.\n"
    )
    if prior_issues:
        p += ("\nIssues raised last round, which the implementer was asked to "
              "fix — verify each is actually resolved:\n"
              + "\n".join(f"- {i}" for i in prior_issues) + "\n")
    p += f"\nTHE PULL REQUEST DIFF:\n\n{diff}\n\n"
    p += graft.impact_block(impact)
    p += "Review for, in order:\n"
    p += ("1. CORRECTNESS — does it do what the spec says, without bugs? Trace "
          "the logic; do not assume it works because it looks plausible.\n"
          "2. REGRESSIONS — could this break existing behaviour? Consider what "
          "calls the changed functions.\n")
    if config.REQUIRE_TESTS:
        p += ("3. TESTS — a code change MUST come with tests that would FAIL "
              "without it. Reject if there are none, if they only assert the "
              "code runs, or if they miss the behaviour the spec describes. "
              "Documentation-only changes are exempt.\n")
    visual = _visual_review_prose(diff)
    if visual:
        p += "3b. " + visual
    p += ("4. SCOPE — nothing unrelated to the spec. An issue this diff did "
          "not introduce is not yours to block on.\n\n"
          "Review independently: do not assume another reviewer checked "
          "something. Be specific — name the file and line, say what is wrong "
          "and what would fix it. Vague objections waste a whole round.\n\n")
    if board:
        p += "\n" + board + "\n" + BOARD_ETIQUETTE
    if roster:
        p += plan_amend.prompt_block(roster)
    if contract:
        p += "\n" + contract.strip() + "\n"
    p += _scope_lock_prose("approve")
    return p


def _parse_approval(text):
    """Last balanced span carrying an "approve" key wins; fails closed.

    Scope-locked like `_parse_verdict` (review-scope-lock): `issues` are the
    BLOCKING ones, `follow_ups` the pre-existing findings, and an old flat
    verdict's unlabelled issues are blocking."""
    spans = []
    for m in re.finditer(r"\{", text):
        span = _balanced_span(text, m.start())
        if span is not None:
            spans.append(span)
    for span in reversed(spans):
        try:
            obj = json.loads(span)
        except ValueError:
            continue
        if isinstance(obj, dict) and "approve" in obj:
            return _verdict_dict(obj, "approve")
    return {"approve": False, "truncated": True,
            "issues": ["reviewer returned no parseable verdict"]}


def _driver(model, role, policy):
    """Implementer/reviewer driver. policy['harness'] maps model -> opencode|dsh|reasonix
    (bench variants); default follows the roster row's harness, so a model
    whose ROSTER row moves harness moves with it."""
    pol = policy or {}
    harness = pol.get("harness", {}).get(model)
    if harness is None:
        return driver_for(model, role, bench=bool(pol))
    # A bench variant may pin a harness the roster does not name (orchbench's
    # kimi-via-opencode swap); that path is explicit and never implicit.
    if harness == "kimi":
        return KimiDriver(role, bench=bool(pol))
    if harness == "dsh":
        return DeepseekDriver(model, role, bench=bool(pol))
    if harness == "reasonix":
        return ReasonixDriver(model, role, bench=bool(pol))
    return OpencodeDriver(model, role, bench=bool(pol))


def _reviewer_driver(t, policy):
    """The driver for the taskfile's `reviewer:` family token.

    Resolved through config.REVIEW_FAMILIES, so "glm" means GLM-5.3 and
    "deepseek" means whichever DeepSeek is live. A token that has left the
    roster ("kimi", retired 2026-09-12) is remapped in load_taskfile before it
    ever reaches this; the .get fallback never builds a withdrawn driver.
    """
    token = t["reviewer"]
    model = config.REVIEW_FAMILIES.get(token, token)
    return _driver(model, "reviewer", policy)


def _tier_rank(model):
    """Position in config.TIER_ORDER (weakest 0); -1 for an unknown model."""
    tier = config.MODEL_TIER.get(model)
    return config.TIER_ORDER.index(tier) if tier in config.TIER_ORDER else -1


def _select_reviewer(planned_tok, impl_model, pol, usage):
    """(model, reason) for the pre-merge review — capacity-aware, never weaker.

    The taskfile's reviewer token stays the deterministic plan. Only when its
    model has no driver or harness headroom (`_reviewer_pressure` >= 1) is
    another review family considered: never the implementer's family, never a
    weaker tier than the planned reviewer, and only one whose driver can be
    CONSTRUCTED for the role (Rule 2: no second list of who may review) and
    that has real headroom right now. Otherwise the planned reviewer is kept
    and the review waits for it — a review is never skipped. Under the
    ARC_ALLOW_SAME_FAMILY_REVIEW hatch the cross-family backend is the thing
    that is down, so no fallback is attempted.
    """
    planned = config.REVIEW_FAMILIES.get(planned_tok, planned_tok)
    if _reviewer_pressure(planned, usage) < 1.0:
        return planned, "planned"
    if config.ALLOW_SAME_FAMILY_REVIEW:
        return planned, "planned_full_hatch"
    if pol:
        # A bench variant measures the reviewer it names; never swap it.
        return planned, "planned_full_bench"
    impl_fam = config.MODEL_FAMILY.get(impl_model)
    floor = _tier_rank(planned)
    fit = []
    # Every review-capable model, not the one name REVIEW_FAMILIES pins per
    # family: a local two-family roster still yields to a stronger seat that
    # the taskfile did not name, and a same-family model is never that seat.
    for m in config.MODEL_ROLES:
        if m == planned or config.MODEL_FAMILY.get(m) == impl_fam:
            continue
        if not config.model_may(m, "reviewer") or _tier_rank(m) < floor:
            continue
        try:
            if config.driver_limit(m) <= 0:
                continue
        except (KeyError, ValueError):
            continue
        if _reviewer_pressure(m, usage) >= 1.0 or _seat_blocked(m, usage):
            continue
        try:
            _driver(m, "reviewer", pol)
        except ValueError:
            continue
        fit.append(m)
    if not fit:
        return planned, "planned_full_no_alternative"
    # Free ARC seats, then the subscription seat with the most headroom.
    # Claude sorts last so the smallest plan is not spent on routine review.
    fit.sort(key=lambda m: _reviewer_rank(m, usage))
    return fit[0], "planned_full_fallback"


def _reviewer_that_ran(ctx, tid, store, fallback):
    """(model or None, family token) of the pre-merge review that passed.

    The taskfile token stays the plan. This is who actually read the diff:
    this graph's review result, or on a publish resume the last successful
    reviewer harness run. `fallback` is the planned family token.
    """
    reviewed = ((ctx or {}).get("results", {}).get(f"review_{tid}") or {})
    model = reviewed.get("reviewer_model")
    fam = reviewed.get("reviewer_family")
    if model or fam:
        return model, fam or fallback
    if store is not None:
        try:
            rows = store.harness_runs_prefix(tid)
        except Exception:
            rows = []
        did = [r for r in rows
               if r.get("task_id") == tid and r.get("role") == "reviewer"
               and r.get("exit_code") == 0
               and r.get("model") in config.MODEL_FAMILY]
        if did:
            model = did[-1]["model"]
            return model, config.MODEL_FAMILY.get(model) or fallback
    return None, fallback


# Failure reasons that mean "this model could not do the task" and so justify
# resuming one tier higher. Anything else (a killed run process, a cancelled
# graph, a harness crash, a merge conflict) is infrastructure noise: the task
# resumes at the SAME tier, because escalating on it wastes the scarcest models.
_CAPABILITY_FAILURES = (
    "exhausted escalation",
    # fail()'s real wording. The list used to hold "gate failed" and
    # "review rejected" — words fail() NEVER writes — so a gate- or
    # review-rejected task was classified as infrastructure noise.
    "verify gate still failing",         # "verify gate still failing after N…"
    "pre-merge review still rejecting",  # "pre-merge review still rejecting…"
    "rejected after",                    # "PR #N rejected after M round(s)…"
    # publish(): a rework told to fix rejected issues produced nothing. The
    # SIBLING string ("implementer produced no changes", the first publish) is
    # deliberately NOT here: the runbook's plan-mode failure produces the same
    # words from an infrastructure cause, so it is not reliably a capability
    # signal.
    "rework after pr rejection produced no changes",
    # Legacy / hand-written rows.
    "exhausted fix rounds",
    "review rejected",
    "gate failed",
)

# The reasons that POSITIVELY say the run was interrupted before the model
# could finish — the only ones a checkpoint may be restored on. An ALLOWLIST,
# deliberately, and the difference matters: the restore test used to be
# `not _is_capability_failure(...)`, a denylist, so every new string publish()
# or fail() grew defaulted to RESTORING. Restoring a refused diff re-submits
# exactly what a gate or a reviewer rejected, and three review rounds each
# found one more string leaking through ("verify gate still failing…",
# "rework after PR rejection produced no changes", …). Unknown reasons now
# start CLEAN. Both entries are the module constants that write them
# (reconcile.INTERRUPTED_REASON, store.STALE_REASON); a test asserts that.
_INTERRUPTION_REASONS = (
    "interrupted: run process exited before the task finished",
    "reset-stale: owning run process died",
)


def _is_capability_failure(error):
    low = (error or "").lower()
    if not low:
        # Pre-existing rows written before failures carried a reason: treat an
        # unexplained failure as a capability signal, matching the old behaviour.
        return True
    return any(m in low for m in _CAPABILITY_FAILURES)


def _was_interrupted(error):
    """True only for the reasons that say the run was cut off mid-flight.

    The allowlist half of the pair above, and the one a checkpoint RESTORE is
    decided on. An unrecognised reason is False: starting clean is the safe
    error, because restoring work a gate or a reviewer refused re-submits it.
    """
    low = (error or "").lower()
    if not low:
        return False
    return any(m in low for m in _INTERRUPTION_REASONS)


async def _run_probe(cmd, wt):
    """Run a task's probe_cmd in its worktree; (verdict, None) on success —
    the LAST JSON object in stdout — or (None, reason) on any failure."""
    try:
        proc = await drivers.spawn(
            ["/bin/sh", "-c", cmd], cwd=str(wt),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), config.GATE_TIMEOUT)
        except asyncio.TimeoutError:
            await drivers._terminate(proc)
            return None, f"probe_cmd timed out after {config.GATE_TIMEOUT}s"
    except OSError as exc:
        return None, f"probe_cmd could not start: {exc}"
    text = out.decode(errors="replace")
    if proc.returncode != 0:
        return None, f"probe_cmd exited {proc.returncode}: {text.strip()[-400:]}"
    # The last TOP-LEVEL object: scan forward, and after a balanced span
    # parses, continue past it — so `{"n": {"k": 2}}` yields the outer object,
    # not the nested one a reverse search would find first.
    found, i = None, 0
    while True:
        i = text.find("{", i)
        if i == -1:
            break
        span = _balanced_span(text, i)
        if not span:
            i += 1
            continue
        try:
            obj = json.loads(span)
        except ValueError:
            i += 1
            continue
        if isinstance(obj, dict):
            found = obj
        i += len(span)
    if found is not None:
        return found, None
    return None, f"probe_cmd printed no JSON object: {text.strip()[-400:]}"


def _amendment_validator(taskfile, pol):
    """Whole-file validation for proposed plan amendments.

    The candidate plan is piped through the SAME loader a hand-written
    taskfile goes through — policy included — so an agent proposal cannot
    sneak a routing, reviewer-pairing, or dependency shape past Rules 1/2/8
    just because it arrived through a worktree instead of a planner.
    """
    def validate(data):
        fd, tmp = tempfile.mkstemp(dir=str(Path(taskfile).parent),
                                   prefix=".amend-check-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            load_taskfile(tmp, policy=pol)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return validate


def _published_shot_urls(manifest, web):
    """Public URLs of the screenshots evidence.publish just pushed.

    `web` is the directory URL publish returns (`.../blob/<branch>/<task>/xN`).
    Each shot is a file under that directory."""
    shots = (manifest or {}).get("shots") or []
    if not shots or not web:
        return []
    root = Path(shots[0]).parent.parent
    urls = []
    base = web.rstrip("/")
    for shot in shots:
        try:
            rel = Path(shot).resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            rel = Path(shot).name
        urls.append(f"{base}/{rel}?raw=true")
    return urls


async def open_task_issues(store, taskset, taskfile):
    """Run start: the taskfile's tracking issue plus an issue for every task
    not yet merged. Best-effort and bounded. One GitHub failure is a
    gh.issue_error event and the rest of the tasks are still opened. {} only
    when issues are off, or when the whole call times out."""
    repo = taskset["repo"]
    try:
        if not taskfile or not Path(taskfile).is_file() or not gh_issues.enabled(repo):
            return {}
        gh_issues.use_db(getattr(store, "path", None))

        async def go():
            name = gh_issues.project_name(taskfile)
            rows = {r["id"]: r for r in store.code_tasks_for(taskfile)}
            out = {}

            async def one(op, tid, coro):
                """One GitHub call. A failure is reported and skipped so the
                next task still gets an issue."""
                try:
                    return await coro
                except Exception as exc:                   # noqa: BLE001
                    fp = errors.capture(exc, node="gh_issues_open", task=tid or None,
                                        taskfile=str(taskfile))
                    events.emit("gh.issue_error", op=op, task=tid or None,
                                taskfile=str(taskfile), error=str(exc)[:200],
                                fingerprint=fp)
                    return None

            epic = await one("epic", "", gh_issues.ensure_epic(repo, name, taskfile))
            if epic is not None:
                out[""] = epic
            for tid in gh_issues.topo_ids(taskset["tasks"]):
                row = rows.get(tid) or {}
                if row.get("status") == "merged":
                    continue
                task = dict(taskset["tasks"][tid], id=tid)
                # A resume that starts at publish never re-implements, so the
                # issue must wear the model the row recorded after a swap or
                # escalation, not the taskfile's planned one.
                recorded = row.get("model")
                if recorded:
                    task["model"] = recorded
                had = gh_issues.issue_for(repo, taskfile, tid)
                n = await one("task", tid, gh_issues.ensure_task_issue(
                    repo, name, taskfile, task, row.get("status") or "pending", epic))
                if n is None:
                    continue
                out[tid] = n
                if had and recorded:
                    await one("model", tid, gh_issues.set_model(repo, n, recorded))
                # A resume that re-attaches to an already-open PR may never
                # publish again (no worktree, or nothing new to commit). The
                # keyword has to be on the PR body or a merge leaves the issue
                # open. sync_taskfile already does this; run start must too.
                if row.get("status") in ("in_review", "conflict"):
                    found = await one(
                        "find_pr", tid,
                        gitstore.find_pr(repo, tid, state="open"))
                    if found and found[0]:
                        number, url, _state = found
                        await one("pr_closes", tid, gh_issues.ensure_pr_closes(
                            repo, number, n, epic))
                        await one("link_pr", tid, gh_issues.link_pr(
                            repo, n, number, url or ""))
            refreshed = await one("epic", "", gh_issues.ensure_epic(repo, name, taskfile))
            if refreshed is not None:
                out[""] = refreshed
            return out
        return await asyncio.wait_for(
            go(), config.GH_ISSUES_TIMEOUT * (2 + len(taskset["tasks"])))
    except Exception as exc:                                   # noqa: BLE001
        fp = errors.capture(exc, node="gh_issues_open", taskfile=str(taskfile))
        events.emit("gh.issue_error", op="open", taskfile=str(taskfile),
                    error=str(exc)[:200], fingerprint=fp)
        return {}


def _resume_pr_start(repo, tid, *, known_open=False):
    """Choose the safe resume node for an open PR: gate if edits are pending.

    Any gh failure returns None: resume then follows today's alloc path.
    Called from build_code_graph, which production invokes inside a running
    loop, so the probe runs on a private loop when one is already going.
    """
    async def _check():
        if not known_open:
            number, _url, state = await gitstore.find_pr(
                repo, tid, state="open", wait_quota=False)
            if not number or (state or "").upper() != "OPEN":
                return None
        try:
            wt = await gitstore.existing_worktree(repo, tid)
        except Exception:
            return "gate"  # the PR was found, but cleanliness is unknown
        if wt is None:
            return "publish"  # publish handles a missing worktree
        try:
            rc, dirty, _ = await gitstore._git(
                ["status", "--porcelain", "--untracked-files=all", "--", ".",
                 *(f":(exclude){p}" for p in
                   (*gitstore.CHANNEL_FILES, *gitstore.RUNTIME_PATHS))],
                cwd=wt, check=False)
        except Exception:
            return "gate"  # cannot prove the worktree is clean
        return "gate" if rc != 0 or dirty.strip() else "publish"

    try:
        try:
            asyncio.get_running_loop()
            in_loop = True
        except RuntimeError:
            in_loop = False
        if not in_loop:
            return asyncio.run(_check())
        box = {}

        def _thread():
            try:
                box["v"] = asyncio.run(_check())
            except Exception as exc:
                box["e"] = exc

        th = threading.Thread(target=_thread, daemon=True)
        th.start()
        th.join()
        if box.get("e") is not None:
            return "gate" if known_open else None
        return box.get("v")
    except Exception:
        return "gate" if known_open else None


def build_code_graph(store, taskset, taskfile="", policy=None):
    repo = taskset["repo"]
    tasks = taskset["tasks"]
    project_slug = Path(repo).name
    pol = policy if policy is not None else taskset.get("policy") or None
    mfr = (pol or {}).get("max_fix_rounds", config.MAX_FIX_ROUNDS)
    review_on = (pol or {}).get("review", True)
    escalate_on = (pol or {}).get("escalate", True)
    g = Graph("code-tasks", max_steps=config.MAX_GRAPH_STEPS,
              max_in_flight=config.max_tasks_in_flight())
    # Task nodes with no in-task deps ("heads") start the graph — directly
    # when there is no `after`, else behind the chain_wait gate.
    heads = []

    # The plan-amendment channel (plan_amend.py): agents may propose changes
    # to the plan from inside their worktree. The roster is what makes
    # proposals actionable — dep targets and edit targets are named by id, so
    # the prompts carry the real ids rather than leave agents to guess them.
    # Bench passes a virtual key (`orchbench:<variant>:<stamp>` — no file on
    # disk): no roster, no harvest. Bench prompts must not drift from their
    # fixed form, and a proposal can never apply to a file that is not there.
    roster = ([(tid, t["title"]) for tid, t in tasks.items()]
              if taskfile and Path(taskfile).is_file() else None)

    def harvest_proposals(tid, wt, role, model):
        """Collect any plan proposals an agent left in its worktree.

        Runs after EVERY agent session (implement, review, PR review — the
        crash paths too: a harness that died mid-run may still have written
        the file) and once more inside publish as a pre-commit sweep, because
        publish's `git add -A` would otherwise commit a leftover. A harvest
        failure must never take a node down — but it must never vanish
        either (Rule 7b).
        """
        if not taskfile or store is None or not Path(taskfile).is_file():
            return
        try:
            plan_amend.harvest(store, taskfile, Path(wt), proposer=tid,
                               role=role, model=model,
                               validate=_amendment_validator(taskfile, pol))
        except Exception as exc:
            fp = errors.capture(exc, task=tid, model=model,
                                node=f"plan_amend_{tid}", role=role)
            log.warning("plan-amendment harvest failed for %s (%s): %s",
                        tid, fp, exc)

    # The task dossier (dossier.py): durable handoff OUT of every agent run
    # (recorded outcome + the agent's .arc/handoff.md) and IN to every prompt
    # (render). Like the proposal harvest, it must never take a node down.
    def dossier_block(tid, role):
        try:
            return dossier_mod.render(project_slug, tid, role=role)
        except Exception as exc:
            errors.capture(exc, task=tid, node=f"dossier_{tid}", role=role)
            return ""

    # The agent board (agentboard.py, Rule 4c): digests IN to every prompt,
    # agents' .arc/board.jsonl lines OUT after every run, claims on the
    # task's files, and the orchestrator's own status/result/question posts.
    # Every helper swallows its errors: the board coordinates, it never gates.
    def board_digest(tid, wt, role, model):
        """The reader's digest; the old raw tail only when the digest is
        empty. Marks the inbox read, so a mention or broadcast posted after
        this prompt is delivered to the NEXT one — nothing interrupts a
        running harness."""
        text = ""
        try:
            text = agentboard.digest_for(
                project_slug, task=tid, role=role, model=model,
                files_hint=tasks[tid].get("files_hint") or (), mark_seen=True)
        except Exception as exc:
            errors.capture(exc, task=tid, model=model, node=f"board_{tid}",
                           role=role)
        return text or board.prompt_block(wt, project=project_slug, task=tid)

    def note_prompt(tid, role, prompt):
        """Remember the prompt so ingest can refuse a board line that is a
        bare copy of it (`agentboard.record_prompt` / `_copies_prompt`)."""
        try:
            agentboard.record_prompt(project_slug, task=tid, role=role,
                                     text=prompt)
        except Exception as exc:
            errors.capture(exc, task=tid, node=f"board_{tid}", role=role)

    def board_ingest(tid, wt, role, model):
        """Land what the agent wrote to .arc/board.jsonl on the board. Runs
        next to every plan-proposal harvest, crash paths included."""
        if wt is None:
            return
        try:
            agentboard.ingest_file(project_slug, wt, task=tid, role=role,
                                   model=model)
        except Exception as exc:
            errors.capture(exc, task=tid, model=model, node=f"board_{tid}",
                           role=role)

    async def harvesting(tid, wt, role, model, run):
        """Await ``run`` (a harness run) while harvesting its
        .arc/board.jsonl every config.BOARD_LIVE_INGEST_S seconds.

        Without this a line an agent wrote — including a post the board CLI
        had to queue there because a sandbox made the DB read-only — reached
        nobody until the run ended, which is an hour or more on a hard task.
        ingest_file is offset-based and idempotent, so the harvest after the
        run still lands only what is new."""
        interval = config.BOARD_LIVE_INGEST_S
        if wt is None or not interval or interval <= 0:
            return await run

        async def loop():
            while True:
                await asyncio.sleep(interval)
                board_ingest(tid, wt, role, model)

        poll = asyncio.create_task(loop())
        try:
            return await run
        finally:
            poll.cancel()
            try:
                await poll
            except asyncio.CancelledError:
                pass

    def board_post(tid, kind, body, **kw):
        kw.setdefault("author_task", tid)
        try:
            return agentboard.post(project_slug, author=f"{tid}/orchestrator",
                                   channel=kw.pop("channel", f"task:{tid}"),
                                   kind=kind, body=body, **kw)
        except Exception as exc:
            errors.capture(exc, task=tid, node=f"board_{tid}")

    def claim_ttl():
        budget = config.total_timeout_for("implementer")
        return float(budget) if budget and budget > 0 else 4 * 3600.0

    def claim_files(tid, touched=()):
        """(Re)take the implementer's lease on files_hint plus `touched`.
        Returns the prompt text naming overlapping claims ("" when none) and
        pings each other task, mentioning both, so the two coordinate instead
        of colliding."""
        author = f"{tid}/implementer"
        paths = list(tasks[tid].get("files_hint") or [])
        paths += [p for p in touched if p not in paths]
        try:
            agentboard.release(project_slug, tid, author)
            cid = agentboard.claim(project_slug, task=tid, author=author,
                                   paths=paths, ttl_s=claim_ttl(),
                                   note="implementer lease on files_hint")
        except Exception as exc:
            errors.capture(exc, task=tid, node=f"board_{tid}")
            return ""
        lines = []
        for c in getattr(cid, "conflicts", ()) or ():
            other = c.get("task") or agentboard.split_agent(c["author"])[0]
            shared = ", ".join(c.get("paths") or [])
            lines.append(f"- {c['author']} holds {shared}")
            if other and other != tid:
                board_post(tid, "ping", channel=f"task:{other}",
                           body=(f"@{other} @{tid}: both tasks claim "
                                 f"overlapping files ({shared}). Coordinate "
                                 f"on the board before editing them."),
                           mentions=[other, tid])
        if not lines:
            return ""
        return ("CLAIM CONFLICTS — other live tasks hold files you are "
                "expected to touch. Ask them (@mention) before editing:\n"
                + "\n".join(lines) + "\n")

    def release_files(tid):
        try:
            agentboard.release(project_slug, tid, f"{tid}/implementer")
        except Exception as exc:
            errors.capture(exc, task=tid, node=f"board_{tid}")

    def releasing_on_cancel(tid, fn):
        """Wrap a task's node: a cancelled graph releases the implementer's
        claim wherever the task is (gate, publish, merge, ...) — nobody will
        edit those files any more. The cancellation still propagates."""
        @functools.wraps(fn)
        async def node(ctx):
            try:
                return await fn(ctx)
            except asyncio.CancelledError:
                release_files(tid)
                raise
        return node

    def ask_implementer(tid, source, issues):
        """A rejection as an OPEN question addressed to the implementer, so
        the next round's digest lists it until it is answered."""
        who = f"{tid}/implementer"
        body = (f"@{who} {source} rejected this change. Fix or answer "
                "(kind=answer, reply_to=<this id>):\n"
                + "\n".join(f"- {i}" for i in (issues or ["(no issues listed)"])[:20]))
        board_post(tid, "question", body, mentions=[who])

    def dossier_after(tid, wt, *, attempt, model, role, outcome=None,
                      harness="", summary="", failure_excerpt="", files=(),
                      session_id=None):
        """Harvest the handoff file (if wt) and record the outcome (if any)."""
        try:
            if wt is not None:
                dossier_mod.harvest_handoff(project_slug, tid, wt,
                                            attempt=attempt, model=model,
                                            role=role)
            if outcome:
                dossier_mod.record_attempt(
                    project_slug, tid, attempt=attempt, model=model,
                    harness=harness, role=role, outcome=outcome,
                    summary=summary, failure_excerpt=failure_excerpt,
                    files_changed=files, session_id=session_id)
        except Exception as exc:
            errors.capture(exc, task=tid, model=model, node=f"dossier_{tid}",
                           role=role)

    def dossier_call(tid, fn, *args):
        try:
            fn(project_slug, tid, *args)
        except Exception as exc:
            errors.capture(exc, task=tid, node=f"dossier_{tid}")

    # Every task is a GitHub issue (gh_issues.py). Each state transition makes
    # at most one comment; every call is bounded and best-effort — a gh
    # failure is a gh.issue_error event with a fingerprint, never a failed or
    # blocked task. Bench keys (no file on disk) never touch GitHub.
    issues_on = bool(taskfile) and Path(taskfile).is_file() and \
        gh_issues.enabled(repo)
    issue_project = gh_issues.project_name(taskfile) if issues_on else ""
    if issues_on:
        # The run's own database (`code run --db`), not config.DB_PATH.
        gh_issues.use_db(getattr(store, "path", None))

    async def issue_step(tid, op, *, status=None, kind=None, body="",
                         attempt=None, model=None, pr=None, failed=None,
                         epic=False, closed=None, pr_closes=None,
                         impl_model=None):
        if not issues_on:
            return

        async def go():
            n = gh_issues.issue_for(repo, taskfile, tid)
            if n is None:
                n = await gh_issues.ensure_task_issue(
                    repo, issue_project, taskfile, dict(tasks[tid], id=tid))

            async def step(name, coro):
                """One GitHub call. A label or comment failure must not skip
                the closing keyword (or the other way around)."""
                try:
                    await coro
                except Exception as exc:                   # noqa: BLE001
                    fp = errors.capture(exc, task=tid, node=f"gh_issue_{tid}",
                                        op=f"{op}:{name}")
                    events.emit("gh.issue_error", task=tid, op=f"{op}:{name}",
                                error=str(exc)[:200], fingerprint=fp)

            if pr_closes:
                # Independent of labels and comments: a reattached PR whose
                # body lacks `Closes #N` must still gain it.
                await step("pr_closes", gh_issues.ensure_pr_closes(
                    repo, pr_closes, n, gh_issues.issue_for(repo, taskfile, "")))
            if failed is not None:
                await step("close_failed", gh_issues.close_failed(repo, n, failed))
            if status or impl_model:
                await step("labels", gh_issues.swap_labels(
                    repo, n, status=status, model=impl_model))
            if pr:
                await step("link_pr", gh_issues.link_pr(repo, n, *pr))
            if kind:
                await step("comment", gh_issues.comment(
                    repo, n, kind, body, attempt=attempt, model=model))
            if closed:
                await step("close_merged", gh_issues.close_merged(repo, n, closed))
            if epic:
                await step("epic", gh_issues.ensure_epic(
                    repo, issue_project, taskfile))
        try:
            await asyncio.wait_for(go(), config.GH_ISSUES_TIMEOUT)
        except Exception as exc:                               # noqa: BLE001
            fp = errors.capture(exc, task=tid, node=f"gh_issue_{tid}", op=op)
            events.emit("gh.issue_error", task=tid, op=op,
                        error=str(exc)[:200], fingerprint=fp)

    def issue_refs(tid):
        """'Closes #N' (+ the epic) for a PR body; '' when issues are off."""
        if not issues_on:
            return ""
        try:
            n = gh_issues.issue_for(repo, taskfile, tid)
            epic = gh_issues.issue_for(repo, taskfile, "")
        except Exception as exc:                               # noqa: BLE001
            errors.capture(exc, task=tid, node=f"gh_issue_{tid}")
            return ""
        if n is None:
            return ""
        return "\n\n" + gh_issues.closes_refs(n, epic)

    def with_issue(stage, tid, fn, t, cur):
        """Wrap a pipeline node so its transition lands on the task issue."""
        if not issues_on:
            return fn

        async def node(ctx):
            runs = ctx.get("runs", {})
            before = None
            if stage == "implement":
                before = cur(ctx)
                await issue_step(
                    tid, stage, status="implementing", kind="implementing",
                    impl_model=before,
                    body=f"{before} via {_harness_of(before)}",
                    attempt=runs.get(f"implement_{tid}", 0) + 1, model=before)
            r = await fn(ctx)
            try:
                await issue_after(stage, tid, t, ctx, r, before, cur)
            except Exception as exc:                           # noqa: BLE001
                fp = errors.capture(exc, task=tid, node=f"gh_issue_{tid}")
                events.emit("gh.issue_error", task=tid, op=stage,
                            error=str(exc)[:200], fingerprint=fp)
            return r
        return node

    async def issue_after(stage, tid, t, ctx, r, before, cur):
        r = r if isinstance(r, dict) else {}
        attempt = ctx.get("runs", {}).get(f"implement_{tid}", 0) or None
        if stage == "implement":
            if r.get("model") and r["model"] != before:
                await issue_step(tid, "usage_swap", kind="usage swap",
                                 impl_model=r["model"],
                                 body=f"{before} → {r['model']}: the plan "
                                      f"window of {before} is spent.",
                                 attempt=attempt, model=r["model"])
        elif stage == "gate":
            if r.get("passed"):
                await issue_step(tid, stage, kind="gate passed",
                                 body=f"`{(t.get('verify_cmd') or '(none)')[:300]}`",
                                 attempt=attempt)
            else:
                await issue_step(tid, stage, kind="gate failed",
                                 body="```\n" + (r.get("output") or "")[-3000:]
                                      + "\n```", attempt=attempt)
        elif stage == "review" and not r.get("skipped"):
            who = r.get("reviewer_model") or reviewer_for(t, cur(ctx))
            issues = r.get("issues") or []
            listing = "\n".join(f"- {i}" for i in issues[:30])
            kind = ("review crashed" if r.get("crashed") else
                    "review passed" if r.get("pass") else "review rejected")
            await issue_step(tid, stage, kind=kind, body=listing,
                             attempt=attempt, model=who)
        elif stage == "escalate":
            await issue_step(tid, stage, kind="escalated",
                             impl_model=r.get("to_model"),
                             body=f"{r.get('from_model')} → {r.get('to_model')}: "
                                  f"fix rounds exhausted on {r.get('from_model')}.",
                             model=r.get("to_model"))
        elif stage == "publish" and r.get("published") and r.get("pr"):
            # link_pr is idempotent per (issue, PR) — it reads the issue's
            # comments — so a reattached PR still links a backfilled issue,
            # or one whose first link comment failed.
            await issue_step(tid, stage, status="in-review", pr_closes=r["pr"],
                             pr=(r["pr"], r.get("url") or ""))
        elif (stage == "publish" and not r.get("published")
              and not r.get("resolve") and not r.get("merged")
              and f"alloc_{tid}" in ctx.get("results", {})):
            # Terminal: the same condition under which no publish edge fires
            # (push rejected, PR not opened, no changes) — `fail` is never
            # reached, so the issue is failed here.
            await issue_step(tid, stage, failed=f"publish failed: "
                             f"{r.get('reason') or 'unknown'}", epic=True)
        elif stage == "pr_review" and r.get("pr"):
            if r.get("inconclusive"):
                kind = "pull request review inconclusive"
            else:
                kind = ("pull request approved" if r.get("approved")
                        else "pull request changes requested")
            issues = r.get("issues") or []
            await issue_step(tid, stage, kind=f"{kind} (round {r.get('round')})",
                             body="\n".join(f"- {i}" for i in issues[:30]))
        elif stage == "pr_merge":
            if r.get("merged") and not r.get("pr"):
                # No PR, so no `Closes #N` will ever fire: close it here.
                await issue_step(tid, stage, epic=True,
                                 closed="The change was empty, so it was "
                                        "recorded as merged without a PR.")
            elif r.get("merged"):
                await issue_step(tid, stage, status="merged", epic=True)
            elif r.get("reason") and not r.get("resynced"):
                row = next((x for x in store.code_tasks_for(taskfile)
                            if x["id"] == tid), None) or {}
                await issue_step(tid, stage, status="conflict", kind="conflict",
                                 body=str(row.get("error") or r["reason"])[:1500])
        elif stage == "fail":
            await issue_step(tid, stage, failed=str(r.get("reason") or "failed"),
                             epic=True)

    # --- resume: statuses recorded by earlier runs of THIS taskfile ----------
    # Re-running `code run <taskfile>` is a resume of the same project: merged
    # tasks collapse into skip stubs, failed/conflict/stale/pending tasks run
    # again — failed ones one tier higher, conflict ones keeping their model.
    prior = {}
    resume_start = {}
    if taskfile and store is not None:
        try:
            prior = {r["id"]: r for r in store.code_tasks_for(taskfile)}
        except Exception:
            prior = {}

    _tier_index, _next_tier = _tier_index_m, _next_tier_m
    reviewer_for = _reviewer_for

    def start_model(tid):
        """Model a (possibly resumed) run starts this task at.

        Only a genuine capability failure escalates. A row that says the model
        exhausted its fix rounds is evidence the tier was too weak; a row the
        stale-reset wrote because its run process was killed says nothing about
        the model at all. Escalating the latter used to send every interrupted
        task straight to the scarcest top tier — so one killed queue turned
        into four tasks piled on one small cap.
        """
        t = tasks[tid]
        r = prior.get(tid)
        if not r or not escalate_on:
            return t["model"]
        if r["status"] == "failed" and _is_capability_failure(r.get("error")):
            nxt = _next_tier(r.get("model") or t["model"])
            if nxt:
                return nxt
        # conflict resumes at the same model — a merge conflict is not a
        # model-capability signal — and so does an interrupted run.
        return r.get("model") or t["model"]

    if prior:
        skipped = sorted(tid for tid, r in prior.items()
                         if r["status"] == "merged" and tid in tasks)
        retried = sorted(tid for tid, r in prior.items()
                         if r["status"] != "merged" and tid in tasks)
        # "Escalated" means higher than where this task LAST ran, not higher
        # than the taskfile's original routing. Comparing against the taskfile
        # reported every resume of an already-escalated task as a fresh
        # escalation, which is exactly the signal an operator reads to decide
        # whether the fleet is about to pile onto a scarce model.
        def _baseline(tid):
            r = prior.get(tid)
            return (r.get("model") if r else None) or tasks[tid]["model"]

        higher = {tid: m for tid in retried
                  if (m := start_model(tid)) != _baseline(tid)}
        # A reboot leaves the row 'running' (then stale-reset writes 'failed')
        # while the PR is still open. Re-attach, but verify unfinished local
        # edits before publish can commit them.
        for tid in retried:
            known_open = prior[tid].get("status") in ("in_review", "conflict")
            try:
                start = _resume_pr_start(repo, tid, known_open=known_open)
                if start:
                    resume_start[tid] = start
            except Exception:
                log.warning("resume %s: open-PR probe failed", tid)
        events.emit("run.resume", taskfile=taskfile, skipped_merged=skipped,
                    retried=retried, escalated_on_resume=higher,
                    reattached_open_pr=sorted(tid for tid in resume_start
                                              if prior[tid].get("status") not in
                                              ("in_review", "conflict")))

    def wire_deps(t, target):
        """Gate `target` on EVERY dependency merging, not just the last one.

        This used to be `g.edge(f"pr_merge_{t['deps'][-1]}", target)` — the
        LAST dep only. A task declaring deps ["a", "b"] waited for b and
        started the moment b merged, whether or not a had; if a was the slower
        of the two, the dependent branched from a base missing the code it
        depended on. That is not a join, and the engine has had a real one
        (gather=True) the whole time — the research and build graphs use it,
        the code graph never did.

        One dep keeps the direct edge. Two or more get a gather node that
        waits for all of them.
        """
        deps = t["deps"]
        when = t.get("when")
        if len(deps) == 1:
            src = f"pr_merge_{deps[0]}"
        else:
            src = f"join_{t['id']}"

            async def joined(ctx):
                # The dependents' `when` reads verdicts off this result, so
                # carry every dep's along (pr_merge returns its own).
                res = ctx.get("results", {})
                return {"joined": list(deps),
                        "verdicts": {d: (res.get(f"pr_merge_{d}") or {}).get("verdict")
                                     for d in deps}}

            g.node(src, joined, gather=True)
            for d in deps:
                g.edge(f"pr_merge_{d}", src)
        if not when:
            g.edge(src, target)
            return

        def verdict_of(r):
            if len(deps) == 1:
                return r.get("verdict")
            return (r.get("verdicts") or {}).get(when["dep"])

        g.edge(src, target, when=lambda r, c: when_holds(when, verdict_of(r)))
        g.edge(src, f"skip_{t['id']}",
               when=lambda r, c: not when_holds(when, verdict_of(r)))

    def make_skip_node(t):
        """Terminal node for a task whose `when` did not hold: it and every
        task downstream of it are recorded as `skipped`, so the project can
        finish (skipped counts as complete, like merged) and the page says
        why the branch was not taken."""
        tid = t["id"]
        downstream = _downstream(tasks, tid)

        async def skip(ctx):
            reason = f"when {when_text(t['when'])} did not hold"
            for sid in [tid] + downstream:
                st = tasks[sid]
                store.upsert_code_task(taskfile, sid, st["title"], st["model"],
                                       st["reviewer"], "skipped",
                                       error=reason if sid == tid else f"depends on skipped {tid}",
                                       finished=True)
                events.emit("task.skipped", task=sid, reason=reason,
                            because=None if sid == tid else tid)
            return {"skipped": True, "merged": False, "reason": reason,
                    "downstream": downstream}

        async def skip_node(ctx):
            r = await skip(ctx)
            # The public lifecycle too: every skipped task is arc:skipped
            # with its reason, not left reading arc:pending forever.
            for sid in [tid] + downstream:
                await issue_step(
                    sid, "skip", status="skipped", kind="skipped",
                    body=r["reason"] if sid == tid else
                    f"depends on skipped `{tid}` ({r['reason']})",
                    epic=sid == ([tid] + downstream)[-1])
            return r

        g.node(f"skip_{tid}", skip_node if issues_on else skip)

    def make_skip(t):
        """Merged task: collapse to a stub publish so dependents see it as done."""
        tid = t["id"]

        row = prior.get(tid) or {}
        try:
            prior_verdict = json.loads(row["verdict"]) if row.get("verdict") else None
        except (ValueError, TypeError):
            prior_verdict = None

        async def publish(ctx):
            return {"merged": True, "skipped": True, "head": None}

        async def pr_merge(ctx):
            # The verdict this task recorded when it really ran, so a
            # dependent's `when` reads the same answer on a resume.
            return {"merged": True, "skipped": True, "verdict": prior_verdict}

        g.node(f"publish_{tid}", publish)
        g.node(f"pr_merge_{tid}", pr_merge)
        g.edge(f"publish_{tid}", f"pr_merge_{tid}")
        if t["deps"]:
            wire_deps(t, f"publish_{tid}")
        else:
            heads.append(f"publish_{tid}")

    def make_chain(t):
        tid = t["id"]
        # Branch from main even with deps: merges are serialized and the
        # publish_<dep> -> alloc_<tid> edge orders us after the dep's merge, so
        # main already contains every dep (dep branches are deleted at
        # cleanup, before any dependent allocs).
        base = config.BASE_BRANCH
        model0 = start_model(tid)
        prior_status = (prior.get(tid) or {}).get("status")

        async def worktree(ctx):
            """This task's worktree, whether alloc ran in THIS graph or not.

            Every node used to index ctx["results"][f"alloc_{tid}"] directly,
            which is absent on a resume that starts at publish — so the first
            node to look raised KeyError and drained the graph, stranding the
            very PR the resume existed to finish.
            """
            res = (ctx.get("results", {}).get(f"alloc_{tid}") or {}).get("worktree")
            if res:
                return Path(res)
            wt = await gitstore.existing_worktree(repo, tid)
            if wt is None:
                raise GraphError(f"{tid}: no worktree — nothing to resume")
            return wt

        def cur_model(ctx):
            """Which model implements this task RIGHT NOW.

            Three sources, and the HIGHEST tier among them wins, because
            escalation is monotonic: the operator's manual override (set from
            the dashboard, read fresh from the database at every node boundary
            so a running task moves up at its next step), the graph's own
            escalate node, and the taskfile's declared model.
            """
            esc = ctx.get("results", {}).get(f"escalate_{tid}")
            auto = esc["to_model"] if esc else model0
            manual = None
            if store is not None and taskfile:
                try:
                    manual = store.get_model_override(taskfile, tid)
                except Exception:
                    manual = None
            if not manual or manual == auto:
                return auto
            ti, tm = _tier_index(auto), _tier_index(manual)
            # An off-path model (gpt-oss) counts as below the entry tier.
            ti = -1 if ti is None else ti
            tm = -1 if tm is None else tm
            return manual if tm >= ti else auto

        def esc_n(ctx):
            return ctx.get("runs", {}).get(f"escalate_{tid}", 0)

        def within_budget(ctx):
            """Fix budget at the current tier: mfr rounds per tier, refreshed
            by every escalation (implement runs are counted globally)."""
            runs = ctx.get("runs", {})
            return runs.get(f"implement_{tid}", 0) <= mfr * (esc_n(ctx) + 1)

        def can_escalate(ctx):
            if not escalate_on or esc_n(ctx) >= config.MAX_ESCALATIONS:
                return False
            return _next_tier(cur_model(ctx)) is not None

        def emit_budget(ctx):
            """One task.budget event tying the whole retry/escalation cost of
            this task together; seconds come from its harness_runs rows and
            tokens from parsing those runs' transcripts (derived, not stored)."""
            try:
                rows = store.harness_runs_prefix(tid)
            except Exception:
                rows = []
            toks = 0
            for r in rows:
                if not r.get("transcript"):
                    continue
                try:
                    raw = Path(r["transcript"]).read_text(encoding="utf-8",
                                                          errors="replace")
                except OSError:
                    continue
                toks += transcript_tokens(raw)[0]
            events.emit("task.budget", task=tid, model=cur_model(ctx),
                        implement_attempts=ctx.get("runs", {}).get(
                            f"implement_{tid}", 0),
                        escalations=esc_n(ctx),
                        total_driver_seconds=round(
                            sum(r.get("seconds") or 0.0 for r in rows), 1),
                        total_tokens=toks)

        async def restore_interrupted(tid, wt):
            """Continue an interrupted attempt instead of restarting it.

            alloc has just reset task/<id> to base. The previous row says WHY
            the task ended: "interrupted: run process exited before the task
            finished" (main.py on SIGTERM/Ctrl-C, reconcile on a dead pid) or a
            cancelled graph — the reasons Rule 4 already treats as NOT a
            capability failure, because the model was never given a chance to
            fail. The work it wrote is the work to continue from, so its latest
            checkpoint is applied back onto the fresh worktree and the board is
            told, in the same words the implementer reads.

            A genuine capability failure starts CLEAN on purpose: a gate or a
            reviewer rejected that work, and restoring it would re-submit the
            very thing that was refused.

            Which is which is an ALLOWLIST (`_INTERRUPTION_REASONS`), not
            "not a capability failure". The old test was a DENYLIST, so every
            failure string publish() or fail() grew later defaulted to
            RESTORING — three review rounds each found one more leaking through
            ("verify gate still failing…", "rework after PR rejection produced
            no changes", …). An unrecognised reason is not evidence the model
            was interrupted; it starts clean.
            """
            row = prior.get(tid) or {}
            if row.get("status") != "failed":
                return
            if not _was_interrupted(row.get("error")):
                return
            res = await gitstore.restore_checkpoint(repo, tid, wt)
            if not res.get("restored"):
                return
            files = res.get("files") or []
            events.emit("task.checkpoint_restored", task=tid,
                        path=res.get("path"), files=len(files),
                        previous_failure=row.get("error"),
                        attempt=(res.get("meta") or {}).get("attempt"))
            board.post(wt, task=tid, role="orchestrator", model="",
                       harness="checkpoint", kind="note", project=project_slug,
                       body=(f"restored {len(files)} file(s) from checkpoint "
                             f"{Path(str(res.get('path') or '')).name}: the "
                             f"previous attempt was interrupted, not rejected — "
                             f"continue it instead of starting over: "
                             f"{', '.join(files[:12])}"))

        async def alloc(ctx):
            wt = await gitstore.alloc(repo, tid, base)
            store.upsert_code_task(taskfile, tid, t["title"], model0,
                                   reviewer_for(t, model0), "running",
                                   branch=f"task/{tid}", worktree=str(wt))
            events.set_context(module=tid)
            events.emit("worktree.alloc", path=str(wt), base=base)
            await restore_interrupted(tid, wt)
            return {"worktree": str(wt)}

        async def implement(ctx):
            results = ctx.get("results", {})
            # Status is set to "running" at alloc and at escalate — and NOWHERE
            # else. A task that resumes at publish (conflict repair, or
            # in_review with a PR open) and lands here still reads as
            # "conflict" in the database while an agent is actively editing its
            # worktree. That cost me a near-miss: the status said conflict, the
            # task was mid-rework, and acting on the status would have raced a
            # live agent through the same merge.
            store.upsert_code_task(taskfile, tid, t["title"], cur_model(ctx),
                                   reviewer_for(t, cur_model(ctx)), "running",
                                   branch=f"task/{tid}")
            feedback = _rework_feedback(tid, results)
            attempt = ctx.get("runs", {}).get(f"implement_{tid}", 0) + 1
            model = cur_model(ctx)
            driver = _driver(model, "implementer", pol)
            wt = await worktree(ctx)
            # Graph hints are recomputed per attempt: a rework's spans should
            # point at the code as it is NOW, after the previous attempt.
            hints = await graft.hints(t, wt)
            # A rework continues the previous attempt's harness session when
            # the model is unchanged: the alternative is re-reading the whole
            # repo per round (60-85 min measured). Both harnesses express
            # this as `-c` (continue the newest session in the workspace, and
            # the worktree is per-task): drivers.py argv treats a truthy
            # session_id as exactly that flag.
            resume = _resume_session(results, tid, model, driver.harness)
            # Lease files_hint for this round (re-taken every fix round, so
            # the lease never lapses under a live agent), then the digest.
            conflicts = claim_files(tid)
            board_post(tid, "status",
                       f"implementing: attempt {attempt} on {model}")
            thread = conflicts + board_digest(tid, wt, "implementer", model)
            # Which model this attempt actually RAN on. A spent plan window can
            # substitute another driver (drivers.usage_substitute), and the
            # checkpoint written in the `finally` below must name the model
            # that ran — on the crash path exactly as on the success path.
            ran = {"model": model}
            prompt = _impl_prompt(t, feedback, hints, roster, thread,
                                  project_contract.role_block(wt, "implementer"),
                                  dossier=dossier_block(tid, "implementer"))
            note_prompt(tid, "implementer", prompt)
            try:
                res = await harvesting(tid, wt, "implementer", model, driver.run(
                    prompt, wt,
                    session_id=resume, task_id=f"{tid}-x{attempt}",
                    avoid_families={reviewer_for(t, model)}))
                ran["model"] = getattr(res, "model", None) or model
            except DriverError as exc:
                if not (pol or {}).get("tolerate_driver_error", True):
                    raise
                store.save_harness_run(tid, driver.harness, model, "implementer",
                                       attempt, 1, "", 0.0)
                # An implementer crash is the single most consequential failure
                # in the pipeline and it was the one path still throwing its
                # traceback away — the capture wired into drivers.py does not
                # reach here, because this except is what catches what THAT
                # one re-raises.
                fp = errors.capture(exc, task=tid, model=model,
                                    node=f"implement_{tid}", role="implementer",
                                    attempt=attempt, harness=driver.harness)
                events.emit("driver.error", task=tid, role="implementer",
                            model=model, attempt=attempt,
                            error=str(exc)[:200], fingerprint=fp)
                harvest_proposals(tid, wt, "implementer", model)
                board_ingest(tid, wt, "implementer", model)
                dossier_after(tid, wt, attempt=attempt, model=model,
                              role="implementer", outcome="crashed",
                              harness=driver.harness,
                              failure_excerpt=str(exc)[:600])
                board.post(wt, task=tid, role="implementer", model=model,
                           harness=driver.harness, kind="error",
                           body=str(exc)[:400], project=project_slug)
                return {"crashed": True, "error": str(exc)[:200],
                        "harness": driver.harness}
            except asyncio.CancelledError:
                # A cancelled graph releases the lease: nobody is editing.
                board_ingest(tid, wt, "implementer", model)
                release_files(tid)
                raise
            finally:
                # A worktree is state: save what this attempt produced before
                # anything — a reset, a cancel, a reboot — can discard it.
                # A `finally`, not a line after the try, because the CRASH path
                # returns from its except and used to skip the checkpoint
                # entirely: an attempt that wrote three files and then died
                # saved nothing, and a reboot loses it — there is no fail tail
                # to fall back on when the process is gone. Never raises.
                await gitstore.checkpoint(repo, tid, wt, f"x{attempt}",
                                          model=ran["model"], attempt=attempt)
            # A spent plan window may have moved this attempt to another
            # model (drivers.usage_substitute): record the one that RAN.
            ran_model = ran["model"]
            ran_harness = getattr(res, "harness", None) or driver.harness
            store.save_harness_run(tid, ran_harness, ran_model, "implementer",
                                   attempt, res.exit_code, res.transcript_path, res.seconds)
            harvest_proposals(tid, wt, "implementer", ran_model)
            board_ingest(tid, wt, "implementer", ran_model)
            # Most taskfiles carry no files_hint, so the pre-run lease above
            # claimed NO paths (measured: every prison-escape claim had
            # paths=[]) and a sibling was never warned off anything. Lease
            # what this attempt actually changed, so the next digest of every
            # sibling that touches those files shows the claim.
            try:
                touched = await _changed_files(wt, base)
            except Exception:                                  # noqa: BLE001
                touched = []
            if [p for p in touched
                    if p not in (tasks[tid].get("files_hint") or [])]:
                claim_files(tid, touched)
            # The outcome of this attempt is the gate's to record; here only
            # the agent's handoff is harvested — and a plan-window swap is
            # written down, so the next model knows why it changed.
            dossier_after(tid, wt, attempt=attempt, model=ran_model,
                          role="implementer")
            if ran_model != model:
                dossier_call(tid, dossier_mod.note_model_change,
                             f"attempt {attempt}: usage swap {model} -> "
                             f"{ran_model} (plan window spent)")
                dossier_after(tid, None, attempt=attempt, model=ran_model,
                              role="implementer", outcome="usage_swap",
                              harness=ran_harness,
                              summary=f"plan window moved {model} -> {ran_model}",
                              session_id=res.session_id)
            board.post(wt, task=tid, role="implementer", model=ran_model,
                       harness=ran_harness, session_id=res.session_id,
                       kind="result", body=(getattr(res, "text", "") or "")[:400],
                       project=project_slug)
            return {"session_id": res.session_id, "harness": ran_harness,
                    "model": ran_model}

        async def capture_evidence(wt, attempt):
            """(manifest, gate_error). Rule 7d; see evidence.py.

            A machine that cannot capture (no display/Godot/ffmpeg) never
            fails the task. A project that will not render does, in the
            default `required` mode. Anything else is a bug in the capture
            itself: recorded with a fingerprint, never blamed on the task."""
            from studio.engine import godot as _godot
            out = evidence.run_dir(project_slug, tid, attempt)
            try:
                # Into the written manifest, not just this dict: a resumed
                # review reads the manifest back from disk.
                m = await asyncio.to_thread(evidence.capture, wt, out, repo=repo,
                                            base=base, project=project_slug,
                                            non_visual=evidence.non_visual(t))
            except evidence.EvidenceUnavailable as exc:
                evidence.emit("unavailable", task=tid, reason=str(exc)[:300])
                return None, None
            except (evidence.EvidenceError, _godot.GodotError) as exc:
                evidence.emit("failed", task=tid, attempt=attempt, error=str(exc)[:300])
                if config.EVIDENCE_MODE == "required":
                    return None, ("visual evidence: the project did not render "
                                  f"for its screenshots/video:\n{str(exc)[:1500]}")
                return None, None
            except Exception as exc:                           # noqa: BLE001
                fp = errors.capture(exc, task=tid, node=f"gate_{tid}")
                evidence.emit("error", task=tid, error=str(exc)[:300], fingerprint=fp)
                return None, None
            m["attempt"] = attempt
            evidence.emit("captured", task=tid, attempt=attempt,
                          shots=len(m.get("shots") or []),
                          videos=sorted(m.get("videos") or {}),
                          warnings=len(m.get("warnings") or []), dir=str(out))
            board.post(wt, task=tid, role="orchestrator", model="", harness="evidence",
                       kind="evidence", body=evidence.board_body(m),
                       project=project_slug)
            return m, None

        async def capture_ui_evidence(wt, attempt, changed):
            """(manifest, gate_error) for a diff that touches the dashboard UI
            (Rule 7e; see ui_evidence.py): before/after screenshots of every
            dashboard view, rendered from the merge base and the worktree.

            Same failure contract as capture_evidence: no browser on this
            machine never fails the task; a dashboard that will not start
            from this worktree does, in `required` mode."""
            out = evidence.run_dir(project_slug, tid, attempt)
            try:
                m = await asyncio.to_thread(ui_evidence.capture, wt, out, base=base,
                                            project=project_slug, changed=changed)
            except evidence.EvidenceUnavailable as exc:
                evidence.emit("unavailable", task=tid, surface="ui", reason=str(exc)[:300])
                return None, None
            except evidence.EvidenceError as exc:
                evidence.emit("failed", task=tid, surface="ui", attempt=attempt,
                              error=str(exc)[:300])
                if config.EVIDENCE_MODE == "required":
                    return None, ("visual evidence: the dashboard did not render "
                                  f"for its screenshots:\n{str(exc)[:1500]}")
                return None, None
            except Exception as exc:                           # noqa: BLE001
                fp = errors.capture(exc, task=tid, node=f"gate_{tid}")
                evidence.emit("error", task=tid, surface="ui", error=str(exc)[:300],
                              fingerprint=fp)
                return None, None
            m["attempt"] = attempt
            evidence.emit("captured", task=tid, surface="ui", attempt=attempt,
                          shots=len(m.get("shots") or []),
                          changed=m.get("changed_views") or [],
                          warnings=len(m.get("warnings") or []), dir=str(out))
            board.post(wt, task=tid, role="orchestrator", model="", harness="evidence",
                       kind="evidence", body=ui_evidence.board_body(m),
                       project=project_slug)
            return m, None

        async def post_pr_evidence(ctx, number):
            """Push this attempt's capture to the game repo's evidence branch
            and comment it onto the PR. Never fails publish: a PR without its
            evidence comment is still a PR, and the reviewers already had the
            images attached."""
            shown = gate_evidence(ctx)
            if not shown or shown.get("posted_pr") == number:
                return
            try:
                web = await asyncio.to_thread(evidence.publish, repo, project_slug,
                                              tid, shown.get("attempt", 0), shown)
                if not web:
                    return
                body = ui_evidence.presenter(shown).pr_markdown(
                    shown, web, task_id=tid, attempt=shown.get("attempt", 0))
                rc, out, err = await gitstore._gh(
                    ["pr", "comment", str(number), "--body", body], cwd=repo)
                if rc != 0:
                    raise RuntimeError(f"gh pr comment: {err.strip()[:200]}")
                shown["posted_pr"] = number
                comment_url = next((ln.strip() for ln in reversed((out or "").splitlines())
                                    if ln.strip().startswith("http")), "")
                shots = _published_shot_urls(shown, web)
                evidence.emit("posted", task=tid, pr=number, url=comment_url or web)
                lines = []
                if comment_url:
                    lines.append(f"Evidence comment on pull request #{number}: {comment_url}")
                if shots:
                    lines.append("Screenshots:")
                    lines.extend(f"- {u}" for u in shots[:12])
                elif not comment_url:
                    lines.append(f"Pull request #{number}: {web}")
                await issue_step(tid, "evidence", kind="visual evidence",
                                 body="\n".join(lines))
            except Exception as exc:                           # noqa: BLE001
                fp = errors.capture(exc, task=tid, node=f"publish_{tid}")
                evidence.emit("publish_failed", task=tid, pr=number,
                              error=str(exc)[:300], fingerprint=fp)

        def gate_evidence(ctx):
            """The latest capture for this task in this graph run, or None."""
            return (ctx.get("results", {}).get(f"gate_{tid}") or {}).get("evidence")

        def review_evidence(ctx):
            """What a reviewer is shown: this run's capture, or — when the run
            resumed past its gate (a restart re-attaching to an open PR) — the
            newest capture on disk, so a resumed review is not blind. A gate
            that DID run this time and captured nothing is not overridden by
            an older attempt's images: those would show code that changed."""
            gated = ctx.get("results", {}).get(f"gate_{tid}")
            if gated is not None:
                return gated.get("evidence")
            return evidence.latest_manifest(project_slug, tid)

        async def gate(ctx):
            cmd = t["verify_cmd"]
            prev = ctx.get("results", {}).get(f"implement_{tid}", {})
            if prev.get("crashed"):
                return {"passed": False,
                        "output": f"implementer crashed: {prev.get('error', '')}"}
            if not cmd:
                try:
                    files = await _changed_files(str(await worktree(ctx)), base)
                except Exception:
                    files = []
                dossier_after(tid, None,
                              attempt=ctx.get("runs", {}).get(f"implement_{tid}", 0),
                              model=prev.get("model") or cur_model(ctx),
                              role="implementer", outcome="passed",
                              harness=prev.get("harness", ""),
                              summary="no verify gate (passes trivially)",
                              files=files, session_id=prev.get("session_id"))
                return {"passed": True, "output": ""}
            wt = str(await worktree(ctx))
            # The gate is a shell pipeline (`./check.sh && ...`), and the
            # shell is the least of what it starts: a unittest run, node, git.
            # It is spawned like a harness — its own process group, no stdin
            # — so a timeout kills the whole tree, not just /bin/sh. Killing
            # only the shell left the test runner alive in the worktree,
            # blocked on a stdout pipe nobody was reading any more.
            proc = await drivers.spawn(
                ["/bin/sh", "-c", cmd], cwd=wt,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), config.GATE_TIMEOUT)
            except asyncio.TimeoutError:
                await drivers._terminate(proc)
                dossier_after(tid, None,
                              attempt=ctx.get("runs", {}).get(f"implement_{tid}", 0),
                              model=prev.get("model") or cur_model(ctx),
                              role="implementer", outcome="gate_failed",
                              harness=prev.get("harness", ""),
                              summary=f"gate timed out after {config.GATE_TIMEOUT}s")
                board_post(tid, "status", f"gate failed: gate timed out after "
                           f"{config.GATE_TIMEOUT}s: {cmd[:200]}"[:300])
                return {"passed": False, "output": f"gate timed out after {config.GATE_TIMEOUT}s",
                        "log_path": None}
            full = out.decode(errors="replace")
            passed = proc.returncode == 0
            names = _gate_failures(full)
            attempt = ctx.get("runs", {}).get(f"implement_{tid}", 0)
            # The FULL `FAIL:`/`ERROR:` list, not just the window the 2000-char
            # cut keeps: on 2026-09-15 a gate kept 1 of 15 failing test names
            # and three worktrees spent a fix round hunting the other
            # fourteen. Failure-only, the same discipline as the names block
            # above — on a pass a FAIL:-shaped line is a caught exception
            # printed by an expected-error test, and a "failing checks"
            # header over it would be a lie.
            fail_block = "" if passed else _gate_full_list_block(_gate_fail_lines(full))
            log_path = None
            try:
                log_path = gate_log_path(project_slug, tid, attempt)
                Path(log_path).parent.mkdir(parents=True, exist_ok=True)
                # Rule 4: the FULL output, not the 2000-char window the fix
                # loop hands the implementer — a failing line a hundred lines
                # above that cut is the usual reason a fix round starts by
                # re-running the test. Capped, head and tail, so a runaway
                # run cannot fill the disk.
                Path(log_path).write_text(
                    cap_log(full + ("\n\n" + fail_block if fail_block else "")))
            except OSError:
                log_path = None
            # What the implementer is handed. Sections first (the traceback of
            # each failure — the part the blind tail threw away), then the
            # names, then the tail, then where the full log is. Failure-only
            # for the sections and names: on a pass a FAIL:-shaped line is a
            # caught exception printed by an expected-error test, and a
            # "failing checks" header over it would be a lie.
            output = (gate_feedback(full, log_path,
                                    names if not passed else None)
                      if not passed else full[-2000:])
            # Attribution and a reason, not just a boolean: a bare
            # {"passed": false} in the log cannot be tied to a task or acted
            # on, and this is the per-node progress signal the dashboard reads.
            if passed:
                tail = None
            elif names:
                tail = ("failing: " + "; ".join(names))[-400:]
            else:
                # The raw output's own tail, not `output`'s: `output` is the
                # feedback block now, so slicing it would put the log path in
                # a field meant to be the last lines the gate printed.
                tail = full.strip()[-400:]
            if fail_block:
                # The log-tail field carries the names too: the tail alone is
                # a 400-char window of the output that just hid them, so the
                # list rides beside it (bounded — 40 names, 200 chars each).
                tail = ((tail or "") + "\n" + fail_block)[:9000]
            events.emit("task.gate", task=tid, attempt=attempt, passed=passed,
                        log=log_path, cmd=cmd[:120], tail=tail)
            verdict = None
            if passed and t.get("probe_cmd"):
                verdict, perr = await _run_probe(t["probe_cmd"], wt)
                if perr:
                    # A probe that yields no verdict is a gate failure: the
                    # dependents' conditions would be evaluated on nothing,
                    # and "the branch was skipped because the probe crashed"
                    # is a bug hidden as a decision.
                    passed = False
                    output = (output + "\n" + perr)[-2000:]
                    events.emit("task.gate", task=tid, attempt=attempt, passed=False,
                                log=log_path, cmd=t["probe_cmd"][:120], tail=perr[-400:])
                else:
                    store.set_code_task_verdict(taskfile, tid, verdict)
                    events.emit("task.verdict", task=tid, attempt=attempt, verdict=verdict)
            shown = None
            changed = await _changed_files(wt, base)
            eerr = None
            if passed and evidence.enabled_for(wt, t):
                # Rule 7d: a game change is SEEN before anyone judges it. A
                # project that will not render fails the gate like a test.
                shown, eerr = await capture_evidence(wt, attempt)
            elif passed and ui_evidence.enabled_for(wt, t, changed):
                # Rule 7e: so is a change to the orchestrator's own dashboard.
                shown, eerr = await capture_ui_evidence(wt, attempt, changed)
            if eerr:
                passed = False
                output = (eerr + "\n...\n" + output)[-2400:]
                events.emit("task.gate", task=tid, attempt=attempt, passed=False,
                            log=log_path, cmd="visual evidence", tail=eerr[-400:])
            dossier_after(tid, None, attempt=attempt,
                          model=prev.get("model") or cur_model(ctx),
                          role="implementer", harness=prev.get("harness", ""),
                          outcome="passed" if passed else "gate_failed",
                          summary=("gate passed" if passed else
                                   f"gate failed: {cmd[:120]}"),
                          failure_excerpt="" if passed else (tail or output)[-1200:],
                          files=changed,
                          session_id=prev.get("session_id"))
            if not passed:
                board_post(tid, "status", "gate failed: "
                           + " ".join((tail or output or cmd).split())[:300])
            return {"passed": passed, "output": output, "log_path": log_path,
                    "verdict": verdict, "evidence": shown}

        async def review(ctx):
            if not review_on:
                events.emit("task.reviewed", task=tid, passed=True, reviewer="none",
                            skipped=True)
                return {"pass": True, "issues": [], "skipped": True}
            wt = await worktree(ctx)
            diff = await gitstore.diff_full(wt, base)
            impact = await graft.blast(wt, task=tid)   # uncommitted: tree vs HEAD
            rev_tok = reviewer_for(t, wrote_the_code(
                ctx, tid, cur_model(ctx), store))
            impl_now = wrote_the_code(ctx, tid, cur_model(ctx), store)
            try:
                usage = store.lease_usage()
            except Exception:
                usage = {}
            planned_rev = config.REVIEW_FAMILIES.get(rev_tok, rev_tok)
            rev_model, rev_reason = _select_reviewer(rev_tok, impl_now, pol, usage)
            driver = (_reviewer_driver({"reviewer": rev_tok}, pol)
                      if rev_model == planned_rev
                      else _driver(rev_model, "reviewer", pol))
            attempt = ctx.get("runs", {}).get(f"review_{tid}", 0) + 1
            events.emit("task.reviewer_selected", task=tid, round=attempt,
                        planned=planned_rev, planned_token=rev_tok,
                        model=rev_model,
                        family=config.MODEL_FAMILY.get(rev_model),
                        implementer=impl_now, reason=rev_reason)
            shown = review_evidence(ctx)
            if shown:
                driver.images = ui_evidence.presenter(shown).review_images(shown)
            try:
                prompt = _review_prompt(t, diff, impact, roster,
                                        board_digest(tid, wt, "reviewer", driver.model)
                                        + _evidence_block(shown, driver),
                                        project_contract.role_block(wt, "reviewer"),
                                        dossier=dossier_block(tid, "reviewer"))
                note_prompt(tid, "reviewer", prompt)
                res = await harvesting(tid, wt, "reviewer", driver.model, driver.run(
                    prompt,
                    wt, task_id=f"{tid}-x{attempt}",
                    avoid_families={config.MODEL_FAMILY[wrote_the_code(
                        ctx, tid, cur_model(ctx), store)]}))
            except asyncio.CancelledError:
                # A cancelled graph: land what the reviewer wrote, and end the
                # implementer's lease — nobody will edit these files now.
                board_ingest(tid, wt, "reviewer", driver.model)
                release_files(tid)
                raise
            except DriverError as exc:
                if not (pol or {}).get("tolerate_driver_error", True):
                    raise
                store.save_harness_run(tid, driver.harness, driver.model, "reviewer",
                                       attempt, 1, "", 0.0,
                                       verdict='{"pass": false, "issues": ["reviewer crashed"]}')
                fp = errors.capture(exc, task=tid, model=driver.model,
                                    node=f"review_{tid}", role="reviewer",
                                    attempt=attempt, harness=driver.harness)
                events.emit("driver.error", task=tid, role="reviewer",
                            fingerprint=fp, model=driver.model,
                            error=str(exc)[:200])
                # A reviewer that CRASHED did not review. Returning pass:False
                # sent the task back to the implementer to fix issues nobody
                # raised, and burned one of its fix rounds doing it.
                # graph-admission-control died exactly this way: its gate passed
                # FOUR times while the reviewer hit 18 consecutive capacity
                # errors, and it was recorded as "exhausted escalation" on work
                # that was never rejected. Same distinction pr_review already
                # makes — the diff has not been read, so retry the REVIEW.
                harvest_proposals(tid, wt, "reviewer", driver.model)
                board_ingest(tid, wt, "reviewer", driver.model)
                dossier_after(tid, wt, attempt=attempt, model=driver.model,
                              role="reviewer", outcome="crashed",
                              harness=driver.harness,
                              failure_excerpt=str(exc)[:600])
                return {"pass": False, "crashed": True,
                        "reviewer_model": driver.model,
                        "issues": [f"reviewer crashed: {exc}"[:200]]}
            verdict = _parse_verdict(res.text)
            # A usage-window swap moves the attempt onto another harness.
            # Record the model that RAN, the same way implement does.
            ran_model = getattr(res, "model", None) or driver.model
            ran_harness = getattr(res, "harness", None) or driver.harness
            store.save_harness_run(tid, ran_harness, ran_model, "reviewer",
                                   attempt, res.exit_code, res.transcript_path,
                                   res.seconds, verdict=json.dumps(verdict)[:500])
            harvest_proposals(tid, wt, "reviewer", ran_model)
            board_ingest(tid, wt, "reviewer", ran_model)
            if ran_model != driver.model:
                dossier_call(tid, dossier_mod.note_model_change,
                             f"review round {attempt}: usage swap "
                             f"{driver.model} -> {ran_model} (plan window spent)")
                await issue_step(tid, "usage_swap", kind="reviewer usage swap",
                                 body=f"{driver.model} → {ran_model}: the plan "
                                      f"window of {driver.model} is spent.",
                                 attempt=attempt, model=ran_model)
            dossier_after(
                tid, wt, attempt=attempt, model=ran_model, role="reviewer",
                harness=ran_harness,
                outcome=("crashed" if verdict.get("truncated") else
                         "passed" if verdict.get("pass") else "review_rejected"),
                summary=("review ended without a verdict"
                         if verdict.get("truncated") else ""),
                failure_excerpt=("" if verdict.get("pass") else "\n".join(
                    f"- {i}" for i in verdict.get("issues") or [])[:1200]))
            if verdict.get("truncated"):
                # The session ended without a verdict (e.g. stopped mid-analysis
                # with a question). Same rule as a crash: the diff was never
                # judged, so retry the REVIEW — don't bounce the implementer to
                # fix issues that were never delivered.
                # The raw output is the ONLY evidence of what the reviewer did
                # instead of voting (asked a question, wrote prose, emitted
                # truncated JSON), and this path used to discard it — the
                # retry then repeated the same failure blind.
                rlog = save_review_log(project_slug, tid, attempt, ran_model,
                                       res.text)
                events.emit("driver.error", task=tid, role="reviewer",
                            fingerprint="reviewer.no_verdict",
                            model=ran_model, log=rlog,
                            error="review ended without a parseable verdict")
                return {"pass": False, "crashed": True,
                        "reviewer_model": ran_model,
                        "reviewer_family": config.MODEL_FAMILY.get(ran_model),
                        "review_log": rlog,
                        "issues": ["reviewer session ended without a verdict"
                                   + (f" (raw output: {rlog})" if rlog else "")]}
            issues = verdict.get("issues") or []
            board.post(wt, task=tid, role="reviewer", model=ran_model,
                       harness=ran_harness, kind="note",
                       body=("pass" if verdict.get("pass") else
                             "reject: " + "; ".join(str(i) for i in issues)[:300]),
                       project=project_slug)
            if not verdict.get("pass"):
                ask_implementer(tid, f"pre-merge review (round {attempt}, "
                                f"{ran_model})", issues)
            events.emit("task.reviewed", task=tid, passed=verdict["pass"],
                        # The family that ACTUALLY reviewed; the planned
                        # token rides beside it when capacity moved the review.
                        reviewer=config.MODEL_FAMILY.get(ran_model, rev_tok),
                        planned_reviewer=rev_tok,
                        selection=rev_reason,
                        # The reviewer's MODEL, not just its family token: the
                        # activity feed names who read the diff, and the token
                        # ("glm") is not a model name.
                        model=ran_model,
                        harness=ran_harness,
                        # The fix-loop round this verdict belongs to, so the
                        # feed can say "round 3" instead of a bare timestamp.
                        round=attempt,
                        n_issues=len(verdict.get("issues") or []),
                        # A verdict SALVAGED from malformed JSON (a bare
                        # `"pass": false` whose object never loads) is a real
                        # rejection with no readable issue list — surfaced so
                        # a thin rejection is visible rather than mysterious.
                        salvaged=verdict.get("salvaged", False),
                        # The raw output of a salvaged verdict: its issue list
                        # is unreadable by definition, so without this file the
                        # recorded rejection points at nothing a human (or the
                        # next round) can inspect.
                        log=(save_review_log(project_slug, tid, attempt,
                                             ran_model, res.text, kind="review")
                             if verdict.get("salvaged") else None),
                        # Pre-existing findings a reviewer filed while reading
                        # the full diff: recorded so they are not lost, and
                        # deliberately NOT in `issues` — they must never block
                        # the merge or cost the implementer a fix round.
                        follow_ups=(verdict.get("follow_ups") or [])[:10],
                        n_follow_ups=len(verdict.get("follow_ups") or []))
            verdict["reviewer_model"] = ran_model
            verdict["reviewer_family"] = config.MODEL_FAMILY.get(ran_model)
            return verdict

        async def escalate(ctx):
            src = cur_model(ctx)
            nxt = _next_tier(src)
            rev = reviewer_for(t, nxt)
            store.upsert_code_task(taskfile, tid, t["title"], nxt, rev, "running")
            events.emit("task.escalated", task=tid, from_model=src, to_model=nxt,
                        n=esc_n(ctx) + 1)
            board_post(tid, "status", f"escalated {src} -> {nxt} (escalation "
                       f"{esc_n(ctx) + 1}: fix rounds exhausted on {src})")
            dossier_call(tid, dossier_mod.note_model_change,
                         f"escalation {esc_n(ctx) + 1}: {src} -> {nxt} "
                         f"(fix rounds exhausted on {src})")
            return {"from_model": src, "to_model": nxt, "n": esc_n(ctx) + 1}

        async def publish(ctx):
            """Commit, push the branch, open the PR. Merges NOTHING locally.

            The pull request is the gate: reviewers read this diff and their
            approval is what merges it. This used to merge into main and open
            the PR afterwards, so a reviewer could only object to work that
            had already landed.
            """
            results = ctx.get("results", {})
            alloc_res = results.get(f"alloc_{tid}")
            if alloc_res is not None:
                wt = Path(alloc_res["worktree"])
            else:
                # publish is the START node: this is a resume of a task that
                # already has a branch (conflict repair, or in_review with a PR
                # open). It used to bail out with "no worktree" here, which fired
                # the fallthrough edge into alloc — and alloc RESETS task/<id> to
                # base, so every such resume silently threw away the very work it
                # was resuming and re-implemented from scratch. Re-attach to the
                # existing worktree instead; only fall through when there really
                # is not one.
                wt = await gitstore.existing_worktree(repo, tid)
                if wt is None:
                    # No worktree on an in_review/conflict resume: the PR may
                    # already be MERGED (the run that would have written
                    # 'merged' died first). Re-imploding through alloc would
                    # reset a branch whose work is on main and burn a full
                    # implement cycle re-deriving it. Check GitHub first.
                    if prior_status in ("in_review", "conflict"):
                        number, url, pr_st = await gitstore.find_pr(
                            repo, tid, state="all")
                        if pr_st == "MERGED":
                            store.upsert_code_task(
                                taskfile, tid, t["title"],
                                cur_model(ctx), reviewer_for(t, cur_model(ctx)),
                                "merged", finished=True)
                            events.emit("task.merged", task=tid, pr=number,
                                        url=url,
                                        note="PR already merged; row settled "
                                             "on resume without re-implement")
                            await gitstore.cleanup(repo, tid)
                            return {"published": False, "merged": True,
                                    "empty": True, "head": None,
                                    "pr": number, "url": url}
                    return {"published": False, "reason": "no worktree"}
                events.emit("task.resumed", task=tid, worktree=str(wt),
                            prior_status=prior_status)
            # Belt-and-suspenders harvest: implement/review collect proposals
            # right after their runs, but a crash between write and collect
            # leaves .arc/plan_proposals.jsonl in the worktree — and publish
            # does `git add -A`, so without this sweep the channel file would
            # land in the PR. Also catches proposals from PR-review rework.
            harvest_proposals(tid, wt, "publish-sweep", "")
            board_ingest(tid, wt, "publish-sweep", "")
            dossier_after(tid, wt, attempt=0, model="", role="publish-sweep")
            impl = results.get(f"implement_{tid}", {})
            model = cur_model(ctx)
            # The row and the PR name who read the diff. The taskfile token
            # stays the plan; `rev` is that reader's family.
            rev_model, rev = _reviewer_that_ran(
                ctx, tid, store, reviewer_for(t, model))
            # COMMIT FIRST, then sync. `git merge` refuses to run over local
            # modifications it would overwrite, and at this point the agent's
            # entire output is uncommitted in the worktree.
            fresh_head = await gitstore.publish(
                wt, f"task({tid}): {t['title']}",
                {"Harness": impl.get("harness", "?"),
                 "Model": impl.get("model") or model,
                 "Reviewer": rev_model or rev, "Task-Id": tid})

            # Sync with the base on EVERY publish, not only on a resume.
            #
            # A task branches from base at alloc and opens its PR a median of
            # 101 minutes later — 13.5 hours at the extreme, measured over this
            # fleet. Other tasks merge throughout. Nothing reconciled the two
            # until GitHub refused the merge, by which point two reviewers had
            # already read a diff against a base that no longer existed.
            #
            # Syncing here means the PR is opened against the CURRENT base, so
            # drift alone can no longer cause a conflict, and a genuine overlap
            # surfaces before any reviewer is spent on it.
            ok, conflicts, note = await gitstore.sync_with_base(
                wt, base, keep_conflicts=True)
            resynced = ok and bool(note and "already" not in note.lower())
            if not ok and conflicts:
                # The merge is left in progress with its markers. Hand it to an
                # implementer to resolve by editing files; publishing anyway
                # would spend two reviewers on a diff that cannot merge and
                # then land back here unchanged.
                events.emit("task.resync_failed", task=tid, base=base,
                            note=note, files=conflicts[:20])
                return {"published": False, "resolve": True,
                        "conflicts": conflicts, "base": base}
            if resynced:
                events.emit("task.resynced", task=tid, base=base, note=note)
            if fresh_head is None:
                # No new commit. On a RESUME the branch is already pushed and
                # its PR already open, so re-attach rather than re-implementing
                # and throwing that diff away. But if a PR review round has
                # already run in THIS graph, "no changes" means the rework
                # produced nothing — re-reviewing an identical diff would just
                # burn reviewers to reach the same verdict, so let it fail.
                reworked = f"pr_review_{tid}" in ctx.get("results", {})
                if not reworked and not await gitstore.branch_ahead(
                        repo, tid, base):
                    store.upsert_code_task(taskfile, tid, t["title"], model, rev,
                                           "merged", finished=True)
                    events.emit("task.merged", task=tid,
                                note="empty diff: nothing to publish")
                    await gitstore.cleanup(repo, tid)
                    return {"published": False, "merged": True, "empty": True,
                            "head": None}
                if resynced:
                    # The merge commit only exists locally until this runs, and
                    # the re-attach path below does no pushing of its own.
                    await gitstore.push_task_branch(repo, tid)
                number, url, note = (None, None, None) if reworked else \
                    await gitstore.open_pr(repo, tid,
                                           f"task({tid}): {t['title']}", "", base)
                if number is not None:
                    store.upsert_code_task(taskfile, tid, t["title"], model, rev,
                                           "in_review", branch=f"task/{tid}")
                    events.emit("task.pr_reattached", task=tid, pr=number,
                                url=url, note=note)
                    dossier_call(tid, dossier_mod.set_pr, number, url)
                    return {"published": True, "pr": number, "url": url,
                            "head": None, "reattached": True}
                store.upsert_code_task(
                    taskfile, tid, t["title"], model, rev, "failed",
                    error=("rework after PR rejection produced no changes"
                           if reworked else "implementer produced no changes"),
                    finished=True)
                release_files(tid)
                events.emit("task.failed", task=tid,
                            reason=("rework produced no changes" if reworked
                                    else "no changes to publish"))
                return {"published": False, "merged": False,
                        "reason": "no changes"}
            ok, note = await gitstore.push_task_branch(repo, tid)
            if not ok:
                store.upsert_code_task(taskfile, tid, t["title"], model, rev,
                                       "failed", error=f"push failed: {note}",
                                       finished=True)
                release_files(tid)
                events.emit("task.failed", task=tid, reason=f"push failed: {note}")
                return {"published": False, "reason": note}
            body = (f"Task `{tid}` from `{Path(taskfile).name if taskfile else '?'}`\n\n"
                    f"{t['prompt'][:1500]}\n\n---\n"
                    f"Implemented by **{model}**, pre-review by **{rev_model or rev}**.\n"
                    f"Verify gate: `{t['verify_cmd'] or '(none)'}`\n\n"
                    f"{config.PR_REVIEWERS} independent reviewers must approve "
                    f"before this merges.{issue_refs(tid)}")
            number, url, note = await gitstore.open_pr(
                repo, tid, f"task({tid}): {t['title']}", body, base)
            if number is None:
                store.upsert_code_task(taskfile, tid, t["title"], model, rev,
                                       "failed", error=f"could not open PR: {note}",
                                       finished=True)
                release_files(tid)
                events.emit("task.failed", task=tid, reason=f"pr: {note}")
                return {"published": False, "reason": note}
            store.upsert_code_task(taskfile, tid, t["title"], model, rev,
                                   "in_review", branch=f"task/{tid}")
            events.emit("task.pr_opened", task=tid, url=url, number=number,
                        head=fresh_head, note=note)
            dossier_call(tid, dossier_mod.set_pr, number, url)
            await post_pr_evidence(ctx, number)
            return {"published": True, "pr": number, "url": url, "head": fresh_head}

        async def pr_fanout(ctx):
            """Pick the reviewers and SPAWN one graph node per reviewer.

            The reviewers used to fan out inside a single node via
            asyncio.gather, which made them invisible to the graph: not in
            the diagram, not checkpointed, not individually retryable, and a
            crashed reviewer surfaced only as a field on its parent's result.
            Each is now a real node — pr_reviewer_<tid> — with its own retry
            policy and timeout, joined at pr_review_<tid>. Width is decided
            here at runtime from the eligible pool, which is what dynamic
            fan-out is for.
            """
            from graph import Spawn
            pub = ctx.get("results", {}).get(f"publish_{tid}") or {}
            number = pub.get("pr")
            if not number:
                return {"no_pr": True, "approved": False,
                        "issues": ["no pull request to review"]}
            round_n = ctx.get("runs", {}).get(f"pr_review_{tid}", 0) + 1
            prior_r = ctx.get("results", {}).get(f"pr_review_{tid}") or {}
            diff = await gitstore.pr_diff(repo, number)
            # Reviewers differ from the implementer's family AND from each
            # other, so two approvals mean two genuinely separate readings.
            impl_fam = config.MODEL_FAMILY.get(
                wrote_the_code(ctx, tid, cur_model(ctx), store))
            pool = _eligible_pr_reviewers(impl_fam, pol)
            # Free DeepSeek, then GLM, then the subscription seat with the
            # most headroom; full or usage-blocked seats sort last.
            try:
                usage = store.lease_usage()
            except Exception:
                usage = {}
            pool.sort(key=lambda m: _reviewer_rank(m, usage))
            crashed_before = set(prior_r.get("crashed_models") or prior_r.get("crashed") or []) \
                if prior_r.get("inconclusive") else set()
            if crashed_before:
                def tier_rank(model):
                    tier = config.MODEL_TIER.get(model)
                    return config.TIER_ORDER.index(tier) if tier in config.TIER_ORDER else -1

                # Each healthy reviewer replaces one crashed reviewer of equal
                # or weaker tier. Strongest crashes are matched first, and the
                # weakest sufficient healthy model is spent, so one hard crash
                # does not keep a crashed medium reviewer ahead of a healthy
                # medium reviewer that can fill the other slot.
                slots = max(1, config.PR_REVIEWERS)
                crashes = sorted((m for m in pool if m in crashed_before),
                                 key=tier_rank, reverse=True)
                free = [m for m in pool if m not in crashed_before]
                replaced, unreplaced = [], []
                for crash in crashes:
                    fit = [m for m in free if tier_rank(m) >= tier_rank(crash)]
                    if not fit:
                        unreplaced.append(crash)
                        continue
                    pick = min(fit, key=lambda m: (tier_rank(m), free.index(m)))
                    replaced.append(pick)
                    free.remove(pick)
                if not crashes:
                    chosen = pool[:slots]
                else:
                    chosen = list(replaced)
                    if len(chosen) < slots:
                        chosen.extend(unreplaced[:slots - len(chosen)])
                    if len(chosen) < slots:
                        floor = min(tier_rank(m) for m in crashes)
                        extra = [m for m in free if tier_rank(m) >= floor]
                        chosen.extend(extra[:slots - len(chosen)])
                    chosen = chosen[:slots]
            else:
                chosen = pool[:max(1, config.PR_REVIEWERS)]
            healthy = [m for m in chosen if m not in crashed_before] if crashed_before else []
            reason = ("healthy_same_or_stronger_after_crash" if healthy and crashed_before
                      else "retry_crashed_reviewer" if crashed_before
                      else "least_loaded")
            pre_model, pre_fam = _reviewer_that_ran(
                ctx, tid, store, reviewer_for(t, cur_model(ctx)))
            events.emit("task.pr_review_selected", task=tid, pr=number,
                        round=round_n, reviewers=chosen,
                        crashed_before=sorted(crashed_before), reason=reason,
                        pre_reviewer=pre_model, pre_reviewer_family=pre_fam)
            if len(chosen) < config.PR_REVIEWERS_WANTED:
                # The roster cannot field PR_REVIEWERS cross-family readers for
                # this implementer — the two-model fleet of 2026-09-12 has two
                # families, so every task gets exactly one. The gate still
                # requires unanimity among those who review; one genuine
                # cross-family read beats a same-family pair for the property
                # cross-review protects. But it is a weaker gate than the
                # config asked for, and that must be visible, not silent.
                events.emit("task.pr_review_thin", task=tid, pr=number,
                            wanted=config.PR_REVIEWERS_WANTED, got=len(chosen),
                            reviewers=chosen, implementer=cur_model(ctx))
                # The same fact in the activity feed's own vocabulary. A thin
                # review is the one thing an operator must not have to go
                # digging for: `task.pr_review_thin` is the documented record,
                # and this is what the feed badges as DEGRADED so a weakened
                # gate is visible on the console instead of only in the log.
                events.emit("task.review_degraded", task=tid, pr=number,
                            wanted=config.PR_REVIEWERS_WANTED, got=len(chosen),
                            reviewers=chosen, models=[],
                            implementer=cur_model(ctx),
                            note=(f"thin review: {len(chosen)} of "
                                  f"{config.PR_REVIEWERS_WANTED} reviewer(s) "
                                  f"the roster wanted"))
            items = [{"model": m, "pr": number, "round": round_n, "diff": diff,
                      "n_reviewers": len(chosen),
                      "prior_issues": prior_r.get("issues") or []} for m in chosen]
            return Spawn(f"pr_reviewer_{tid}", items, f"pr_review_{tid}",
                         result={"spawned": len(chosen), "reviewers": chosen,
                                 "pr": number, "round": round_n})

        async def pr_reviewer(ctx):
            """ONE reviewer reads the PR diff. A graph node, so it is visible,
            checkpointed and retryable on its own."""
            it = ctx["spawn"]
            model = it["model"]
            wt = None
            try:
                drv = _driver(model, "pr_reviewer", pol)
                wt = await worktree(ctx)
                impact = await graft.blast(wt, base, task=tid)
                shown = review_evidence(ctx)
                if shown:
                    drv.images = ui_evidence.presenter(shown).review_images(shown)
                prompt = _pr_review_prompt(
                    t, it["diff"], it["n_reviewers"], it["round"],
                    it["prior_issues"], impact, roster,
                    board_digest(tid, wt, "pr-reviewer", model)
                    + _evidence_block(shown, drv),
                    project_contract.role_block(wt, "reviewer"),
                    dossier=dossier_block(tid, "pr-reviewer"))
                note_prompt(tid, "pr-reviewer", prompt)
                res = await harvesting(tid, wt, "pr-reviewer", model, drv.run(
                    prompt,
                    wt, task_id=f"{tid}-pr{it['round']}",
                    avoid_families={config.MODEL_FAMILY[wrote_the_code(
                        ctx, tid, cur_model(ctx), store)]}))
            except asyncio.CancelledError:
                board_ingest(tid, wt, "pr-reviewer", model)
                release_files(tid)
                raise
            except (DriverError, ValueError) as exc:
                # A reviewer that crashed did NOT review. Reported as such —
                # never as a rejection — so the join retries the review rather
                # than sending the implementer to fix nothing.
                if wt is not None:
                    harvest_proposals(tid, wt, "pr-reviewer", model)
                    board_ingest(tid, wt, "pr-reviewer", model)
                dossier_after(tid, wt, attempt=it["round"], model=model,
                              role="pr-reviewer", outcome="crashed",
                              failure_excerpt=str(exc)[:600])
                return {"model": model, "approve": False, "crashed": True,
                        "issues": [f"reviewer {model} crashed: {exc}"[:200]]}
            verdict = _parse_approval(res.text)
            store.save_harness_run(tid, drv.harness, model, "pr-reviewer",
                                   it["round"], res.exit_code, res.transcript_path,
                                   res.seconds, verdict=json.dumps(verdict)[:500])
            harvest_proposals(tid, wt, "pr-reviewer", model)
            board_ingest(tid, wt, "pr-reviewer", model)
            ran_model = getattr(res, "model", None) or model
            ran_harness = getattr(res, "harness", None) or drv.harness
            if ran_model != model:
                dossier_call(tid, dossier_mod.note_model_change,
                             f"PR review round {it['round']}: usage swap "
                             f"{model} -> {ran_model} (plan window spent)")
                await issue_step(tid, "usage_swap", kind="PR reviewer usage swap",
                                 body=f"{model} → {ran_model}: the plan window "
                                      f"of {model} is spent (PR #{it.get('pr')}, "
                                      f"round {it['round']}).", model=ran_model)
            dossier_after(
                tid, wt, attempt=it["round"], model=ran_model, role="pr-reviewer",
                harness=ran_harness,
                outcome=("crashed" if verdict.get("truncated") else
                         "passed" if verdict.get("approve") else "review_rejected"),
                summary=f"PR #{it.get('pr')} round {it['round']}",
                failure_excerpt=("" if verdict.get("approve") else "\n".join(
                    f"- {i}" for i in verdict.get("issues") or [])[:1200]))
            if verdict.get("truncated"):
                # Session ended without a verdict — a reviewer that never
                # reviewed. Crashed, not a rejection: the join retries the
                # round from PR_MAX_INCONCLUSIVE, not the implementer's rounds.
                # Its raw output is kept for the same reason as the pre-merge
                # one: an inconclusive round retried blind just repeats itself.
                rlog = save_review_log(project_slug, tid, it["round"],
                                       ran_model, res.text, kind="pr-review")
                events.emit("driver.error", task=tid, role="pr-reviewer",
                            fingerprint="reviewer.no_verdict", model=ran_model,
                            log=rlog,
                            error="PR reviewer session ended without a verdict")
                return {"model": model, "approve": False, "crashed": True,
                        "review_log": rlog,
                        "issues": [f"reviewer {model} session ended without "
                                   "a verdict"
                                   + (f" (raw output: {rlog})" if rlog else "")]}
            verdict["model"] = model
            return verdict

        async def pr_review(ctx):
            """The JOIN: every reviewer is in. Tally, post to GitHub, decide."""
            fan = ctx.get("results", {}).get(f"pr_fanout_{tid}") or {}
            if fan.get("no_pr"):
                return {"approved": False, "issues": fan["issues"], "approvals": [],
                        "reviewers": [], "crashed": [], "inconclusive": False,
                        "inconclusive_n": 0, "pr": None, "round": 0}
            number, round_n = fan.get("pr"), fan.get("round", 1)
            chosen = fan.get("reviewers") or []
            prior_r = ctx.get("results", {}).get(f"pr_review_{tid}") or {}
            verdicts = ctx.get("results", {}).get(f"pr_reviewer_{tid}") or []
            outcomes = [(v.get("model"), v) for v in verdicts]
            issues, approvals, crashed, approved, inconclusive = \
                _tally_reviews(outcomes)
            follow_ups = _collect_follow_ups(outcomes)
            prior_incon = (prior_r or {}).get("inconclusive_n", 0)
            inconclusive_n = prior_incon + 1 if inconclusive else prior_incon
            crashed_models = sorted(set(prior_r.get("crashed_models") or [])
                                    | set(crashed)) if inconclusive else []
            events.emit("task.pr_reviewed", task=tid, pr=number, round=round_n,
                        approved=approved, approvals=approvals,
                        reviewers=chosen, n_issues=len(issues),
                        # Who actually read the diff. `reviewers` holds the
                        # FAMILY TOKENS the pool chose ("glm"); the feed names
                        # models, so the resolved ones ride beside them.
                        models=[m for m, _ in outcomes],
                        models_ran=[m for m, v in outcomes
                                    if not v.get("crashed")],
                        issues=[i[:400] for i in issues[:10]],
                        # Pre-existing findings, recorded and never blocking:
                        # they do not reach the implementer and cost no round.
                        follow_ups=[f[:400] for f in follow_ups[:10]],
                        n_follow_ups=len(follow_ups),
                        crashed=crashed, inconclusive=inconclusive)
            # Post each verdict AS A GITHUB REVIEW so the trail is visible where
            # a human looks for it. A crashed reviewer gets a neutral note, never
            # a formal rejection: it did not read the diff.
            for model, v in outcomes:
                if v.get("crashed"):
                    body = (f"**{model}** (round {round_n}) — review could not "
                            f"run: {'; '.join(v['issues'])[:400]}")
                else:
                    body = (f"**{model}** (round {round_n}) — "
                            + ("approved." if v["approve"] else "changes requested:\n\n"
                               + "\n".join(f"- {i}" for i in v["issues"][:20])))
                    if v.get("follow_ups"):
                        body += ("\n\nNoted, non-blocking (pre-existing, not "
                                 "introduced by this diff):\n"
                                 + "\n".join(f"- {f}" for f in v["follow_ups"][:10]))
                rc = 1
                if not v.get("crashed"):
                    rc, _, _ = await gitstore._gh(
                        ["pr", "review", str(number),
                         "--approve" if v["approve"] else "--request-changes",
                         "--body", body], cwd=repo)
                if rc != 0:
                    await gitstore._gh(["pr", "comment", str(number),
                                        "--body", body], cwd=repo)
            manual = None
            if approved and manual_review.wanted(t):
                # The fleet agreed; now the human's word. Rejection here goes
                # down the same path as a reviewer's: the comments become the
                # implementer's feedback and a new PR round begins. Whether
                # this task wants a human is the taskfile's call
                # (human_review), else ARC_PR_MANUAL_REVIEW.
                pub = ctx.get("results", {}).get(f"publish_{tid}") or {}
                manual = await _await_manual_review(
                    repo, tid, number, round_n,
                    project=taskset.get("name") or Path(str(repo)).name,
                    url=(pub.get("url") if pub.get("pr") == number else "") or "",
                    title=t.get("title", tid),
                    reviewers=manual_review.summarize_reviewers(outcomes))
                if manual["decision"] == "rejected":
                    approved = False
                    issues = manual["issues"]
            if not approved and not inconclusive:
                ask_implementer(tid, f"PR #{number} review (round {round_n})",
                                issues)
                await gitstore._gh(
                    ["pr", "comment", str(number), "--body",
                     "**Changes requested** (round %d) — returning to the "
                     "implementer.\n\n%s" % (
                         round_n, "\n".join(f"- {i}" for i in issues[:20]))],
                    cwd=repo)
            return {"approved": approved, "issues": issues,
                    "manual": manual,
                    "follow_ups": follow_ups,
                    "approvals": approvals, "reviewers": chosen,
                    "crashed": crashed, "inconclusive": inconclusive,
                    "crashed_models": crashed_models,
                    "inconclusive_n": inconclusive_n,
                    "pr": number, "round": round_n}

        async def pr_merge(ctx):
            """Merge the PR — reached only once every reviewer approved."""
            rv = ctx.get("results", {}).get(f"pr_review_{tid}") or {}
            number = rv.get("pr")
            model, rev = cur_model(ctx), reviewer_for(t, cur_model(ctx))
            pub = ctx.get("results", {}).get(f"publish_{tid}") or {}
            if pub.get("empty") and pub.get("merged"):
                gate_res = ctx.get("results", {}).get(f"gate_{tid}") or {}
                release_files(tid)
                # No diff reached main here (or the PR was merged before this
                # run): the result still lands, with no files.
                pr_ref = pub.get("url") or (
                    f"PR #{pub['pr']}" if pub.get("pr") is not None else "")
                board_post(tid, "result",
                           f"merged {pr_ref or '(no PR: empty diff)'}: "
                           f"{t['title']} (0 file(s) changed)",
                           refs={"files": [], "pr": pr_ref})
                return {"merged": True, "pr": None,
                        "verdict": gate_res.get("verdict")}
            state = await gitstore.pr_state(repo, number)
            status = (state.get("mergeStateStatus") or "").upper()
            mergeable = (state.get("mergeable") or "").upper()
            # DIRTY is a real overlap; BEHIND is a base that moved. Both are
            # what sync_with_base is for. CONFLICTING is the older field for
            # the same overlap. BLOCKED / UNSTABLE / UNKNOWN are checks or
            # GitHub still calculating — not a file conflict.
            if mergeable == "CONFLICTING" or status in ("DIRTY", "BEHIND"):
                # Try to resolve it before giving up. Most conflicts here are
                # not a disagreement about the code at all — they are a base
                # branch that moved on under a task that took twenty minutes,
                # and they merge cleanly with no model involved. This used to
                # be a dead end: mark conflict, finish, wait for a human.
                resyncs = (ctx.get("results", {}).get(f"pr_merge_{tid}") or {}
                           ).get("resyncs", 0)
                ok, conflicts, note = await gitstore.sync_with_base(
                    await worktree(ctx), base)
                if ok and resyncs < config.PR_MAX_RESYNCS:
                    pushed, pnote = await gitstore.push_task_branch(repo, tid)
                    if pushed:
                        events.emit("task.resynced", task=tid, pr=number,
                                    base=base, resyncs=resyncs + 1)
                        # The diff on the PR just changed, so the approval it
                        # already has no longer covers it: review it again.
                        return {"merged": False, "resynced": True,
                                "resyncs": resyncs + 1, "pr": number}
                    note = f"resynced but push failed: {pnote}"
                elif ok:
                    note = f"still conflicting after {resyncs} resync(s)"
                store.upsert_code_task(
                    taskfile, tid, t["title"], model, rev, "conflict",
                    error=f"PR #{number} conflicts with {base}: {note}",
                    finished=True)
                events.emit("task.conflict", task=tid, pr=number, reason=note,
                            files=conflicts[:20])
                return {"merged": False, "reason": "conflict"}
            # The files this PR changes, read BEFORE the merge moves the base
            # (after it the merge base can equal HEAD and the diff is empty).
            try:
                merged_files = await _changed_files(await worktree(ctx), base)
            except Exception:
                merged_files = []
            if state.get("state") == "MERGED":
                # Someone merged it while the fleet was still reviewing — an
                # operator from the GitHub UI, or a hand merge of a backlog.
                # That is the outcome this node exists to reach, not a
                # failure: `gh pr merge` on a merged PR exits non-zero, and
                # treating that as a conflict marked the task failed and
                # stalled every task that depended on it.
                events.emit("task.merged_externally", task=tid, pr=number)
                ok, note = True, "already merged"
            else:
                ok, note = await gitstore.merge_pr(repo, number)
            if not ok and _merge_should_wait(note, status, mergeable):
                waits = int((ctx.get("results", {}).get(f"pr_merge_{tid}") or {}
                             ).get("waits") or 0) + 1
                if waits <= config.PR_MAX_RESYNCS:
                    if waits > 1:
                        await asyncio.sleep(min(60, 15 * (waits - 1)))
                    events.emit("task.merge_wait", task=tid, pr=number,
                                waits=waits, reason=note[:200])
                    return {"merged": False, "waiting": True, "waits": waits,
                            "pr": number}
                note = (f"still not mergeable after {waits - 1} wait(s): "
                        f"{note}")
            if not ok:
                store.upsert_code_task(taskfile, tid, t["title"], model, rev,
                                       "conflict", error=note, finished=True)
                events.emit("task.conflict", task=tid, pr=number, reason=note)
                return {"merged": False, "reason": note}
            # Fast-forward the local integration branch to what GitHub merged.
            ok_ff, ff_note = await gitstore.fast_forward_base(repo, base)
            if not ok_ff:
                events.emit("task.base_not_advanced", task=tid, base=base,
                            reason=ff_note)
            await gitstore.cleanup(repo, tid)
            store.upsert_code_task(taskfile, tid, t["title"], model, rev,
                                   "merged", finished=True)
            events.emit("task.merged", task=tid, pr=number,
                        approvals=rv.get("approvals"))
            release_files(tid)
            pr_url = (pub.get("url") if pub.get("pr") == number else None) or (
                f"PR #{number}" if number is not None else "")
            board_post(tid, "result",
                       f"merged {pr_url}: {t['title']} "
                       f"({len(merged_files)} file(s) changed)",
                       refs={"files": merged_files, "pr": pr_url})
            dossier_after(tid, None, attempt=0, model=model, role="orchestrator",
                          outcome="merged", summary=f"PR #{number} merged")
            dossier_call(tid, dossier_mod.set_pr, None)
            gate_res = ctx.get("results", {}).get(f"gate_{tid}") or {}
            return {"merged": True, "pr": number, "verdict": gate_res.get("verdict")}

        async def fail(ctx):
            """Terminal failure. Must say WHY, because several paths land here.

            It used to report "exhausted escalation up to <model>" no matter how
            it was reached. A task that ran out of PR REVIEW ROUNDS was
            therefore filed as an escalation failure — and pause-when-hidden
            was recorded as "exhausted escalation up to DeepSeek-V4-Flash" with
            ZERO escalations, two still permitted and a next tier available.
            That message sent the reader to audit the escalation config for a
            bug that was never there.
            """
            results = ctx.get("results", {})
            gate_res = results.get(f"gate_{tid}") or {}
            rev_res = results.get(f"review_{tid}") or {}
            pr_res = results.get(f"pr_review_{tid}") or {}
            last = cur_model(ctx)
            escalations = esc_n(ctx)
            attempts = ctx.get("runs", {}).get(f"implement_{tid}", 0)

            if pr_res and not pr_res.get("approved"):
                if pr_res.get("inconclusive"):
                    why = (f"PR #{pr_res.get('pr')} never reached a verdict: "
                           f"{config.PR_MAX_INCONCLUSIVE} inconclusive round(s), "
                           f"reviewers kept crashing")
                else:
                    why = (f"PR #{pr_res.get('pr')} rejected after "
                           f"{config.PR_MAX_ROUNDS} review round(s); last had "
                           f"{len(pr_res.get('issues') or [])} unresolved issue(s)")
            elif not gate_res.get("passed", True):
                why = (f"verify gate still failing after {attempts} attempt(s) "
                       f"on {last}")
            elif rev_res and not rev_res.get("pass", True):
                why = f"pre-merge review still rejecting after {attempts} attempt(s)"
            elif escalations:
                why = (f"exhausted escalation: {escalations} escalation(s), "
                       f"ended on {last}")
            else:
                why = (f"no path forward on {last} after {attempts} attempt(s) "
                       f"(no escalation was taken)")

            store.upsert_code_task(taskfile, tid, t["title"], last,
                                   reviewer_for(t, last), "failed",
                                   error=why[:400], finished=True)
            # `reason` says WHY the task died; `detail` says WHAT was wrong
            # with it. Without it the activity feed could only repeat "verify
            # gate still failing", and the operator had to open the project to
            # learn which assertion, or which reviewer objection, killed the
            # work. Gate output tail first (it names the failing tests), then
            # the blocking review issues, then the escalation wording.
            detail = ""
            if not gate_res.get("passed", True):
                # gate() returns {"passed", "output", "log_path", "verdict"} —
                # `output` is the feedback block (failing sections, names,
                # tail, log path), bounded by gate_feedback.
                detail = (gate_res.get("output") or "")[-800:]
            elif rev_res and not rev_res.get("pass", True):
                detail = "; ".join(str(i) for i in
                                   (rev_res.get("issues") or [])[:5])[:800]
            elif pr_res and not pr_res.get("approved"):
                detail = "; ".join(str(i) for i in
                                   (pr_res.get("issues") or [])[:5])[:800]
            if not detail:
                detail = why
            release_files(tid)
            board_post(tid, "status", f"failed: {why}"[:300])
            events.emit("task.failed", task=tid, reason=why[:400],
                        detail=detail[:800], model=last,
                        escalations=escalations,
                        implement_attempts=attempts)
            emit_budget(ctx)
            return {"failed": True, "gate": gate_res, "reason": why}

        chain = {"alloc": alloc, "implement": implement, "gate": gate,
                 "review": review, "escalate": escalate, "publish": publish,
                 "pr_fanout": pr_fanout, "pr_review": pr_review,
                 "pr_merge": pr_merge, "fail": fail}
        for suffix, fn in chain.items():
            g.node(f"{suffix}_{tid}",
                   with_issue(suffix, tid, releasing_on_cancel(tid, fn), t, cur_model))
        # One reviewer per node, with the per-node policy the fan-out makes
        # possible: a harness that dies on the way in is retried HERE, and the
        # join only ever sees crashes that survived the retries. The timeout is
        # a backstop above the driver's own when a total budget is configured
        # (None when budgets are unlimited — the driver's idle kill still
        # bounds silence), so a reviewer cannot hold the join open indefinitely.
        from graph import Retry
        _rev_total = config.total_timeout_for("reviewer")
        g.node(f"pr_reviewer_{tid}", releasing_on_cancel(tid, pr_reviewer),
               retry=Retry(attempts=3, backoff=20.0, max_backoff=120.0,
                           on=(DriverError,)),
               timeout=(_rev_total + 600) if _rev_total > 0 else None)
        # Declared so validate() sees them; at runtime Spawn and the join do
        # the routing and these two edges never fire on their own.
        g.edge(f"pr_fanout_{tid}", f"pr_reviewer_{tid}")
        g.edge(f"pr_reviewer_{tid}", f"pr_review_{tid}")
        # A fanout with no PR to review goes straight to the join's rejection.
        g.edge(f"pr_fanout_{tid}", f"pr_review_{tid}",
               when=lambda r, c: bool(r.get("no_pr")), on_drain=True)
        g.edge(f"alloc_{tid}", f"implement_{tid}")
        g.edge(f"implement_{tid}", f"gate_{tid}")
        g.edge(f"gate_{tid}", f"review_{tid}", when=lambda r, c: r["passed"])
        g.edge(f"review_{tid}", f"publish_{tid}", when=lambda r, c: r["pass"])

        # --- the pull request IS the gate -----------------------------------
        # publish pushes the branch and opens the PR; nothing has merged yet.
        # config.PR_REVIEWERS reviewers read the real PR diff. Unanimous
        # approval merges it; anything else sends it back to the implementer,
        # whose next commit updates the same PR.
        def pr_rounds(c):
            return c.get("runs", {}).get(f"pr_review_{tid}", 0)

        # on_drain: once a branch is pushed and a PR is open, the model time is
        # already spent. If a SIBLING task fails and drains the graph, these two
        # edges still fire so the PR gets reviewed and merged instead of being
        # orphaned on GitHub. The rework edge below is deliberately not marked —
        # draining must not start a fresh implementer.
        g.edge(f"publish_{tid}", f"pr_fanout_{tid}",
               when=lambda r, c: bool(r.get("published")), on_drain=True)
        g.edge(f"pr_review_{tid}", f"pr_merge_{tid}",
               when=lambda r, c: bool(r.get("approved")), on_drain=True)
        # A resync rewrote the branch, so the approval the PR already has no
        # longer covers what is on it. Back to review, not straight to merge.
        g.edge(f"pr_merge_{tid}", f"pr_fanout_{tid}",
               when=lambda r, c: bool(r.get("resynced")), on_drain=True)
        # Checks still running, or GitHub has not computed mergeability.
        # Retry the merge; do not record a file conflict.
        g.edge(f"pr_merge_{tid}", f"pr_merge_{tid}",
               when=lambda r, c: bool(r.get("waiting")), on_drain=True)
        # An inconclusive round reached no verdict: every reviewer crashed and
        # nobody read the diff. Retry the REVIEW — sending the implementer back
        # to fix issues that do not exist wastes a model and burns a real round.
        # on_drain, because the PR is already open and this is still landing it.
        g.edge(f"pr_review_{tid}", f"pr_fanout_{tid}",
               when=lambda r, c: r.get("inconclusive")
               and r.get("inconclusive_n", 0) < config.PR_MAX_INCONCLUSIVE,
               on_drain=True)
        g.edge(f"pr_review_{tid}", f"implement_{tid}",
               when=lambda r, c: not r.get("approved")
               and not r.get("inconclusive")
               and pr_rounds(c) < config.PR_MAX_ROUNDS)
        g.edge(f"pr_review_{tid}", f"fail_{tid}",
               when=lambda r, c: not r.get("approved")
               and (r.get("inconclusive_n", 0) >= config.PR_MAX_INCONCLUSIVE
                    if r.get("inconclusive")
                    else pr_rounds(c) >= config.PR_MAX_ROUNDS))
        # A crashed reviewer retries the REVIEW; it must not consume a fix
        # round, because no one objected to the code.
        def review_crashes(c):
            return c.get("runs", {}).get(f"review_{tid}", 0)

        g.edge(f"review_{tid}", f"review_{tid}",
               when=lambda r, c: r.get("crashed")
               and review_crashes(c) < config.MAX_REVIEW_CRASHES)
        g.edge(f"review_{tid}", f"fail_{tid}",
               when=lambda r, c: r.get("crashed")
               and review_crashes(c) >= config.MAX_REVIEW_CRASHES)
        for src in ("gate", "review"):
            key = "passed" if src == "gate" else "pass"
            g.edge(f"{src}_{tid}", f"implement_{tid}",
                   when=lambda r, c, k=key: not r[k] and not r.get("crashed")
                   and within_budget(c))
            g.edge(f"{src}_{tid}", f"escalate_{tid}",
                   when=lambda r, c, k=key: not r[k] and not r.get("crashed")
                   and not within_budget(c) and can_escalate(c))
            g.edge(f"{src}_{tid}", f"fail_{tid}",
                   when=lambda r, c, k=key: not r[k] and not r.get("crashed")
                   and not within_budget(c) and not can_escalate(c))
        g.edge(f"escalate_{tid}", f"implement_{tid}")
        # Conflict-repair fallthrough: only when the repair-mode publish ran
        # (no alloc in results yet) and could not merge the old branch.
        # A conflict that needs a human-shaped fix goes to the implementer, NOT
        # to alloc — alloc resets the branch and would discard the very work
        # that conflicts. This edge must therefore exclude the resolve case.
        g.edge(f"publish_{tid}", f"implement_{tid}",
               when=lambda r, c: bool(r.get("resolve")))
        g.edge(f"publish_{tid}", f"alloc_{tid}",
               when=lambda r, c, i=tid: not r.get("published")
               and not r.get("resolve")
               and not r.get("merged")
               and f"alloc_{i}" not in c.get("results", {}))
        g.edge(f"publish_{tid}", f"pr_merge_{tid}",
               when=lambda r, c: bool(r.get("empty")), on_drain=True)
        # A clean open PR resumes at publish. Pending local edits first run
        # through the verify gate and cross-family review; publish must not
        # commit an interrupted PR rework without those checks.
        first = resume_start.get(tid) or (
            "publish" if prior_status in ("conflict", "in_review") else "alloc")
        if t["deps"]:
            # Wait for EVERY dep's PR to MERGE into the base branch, not just
            # to open — otherwise a dependent branches from a base that lacks
            # the code it depends on.
            wire_deps(t, f"{first}_{tid}")
        else:
            heads.append(f"{first}_{tid}")

    for tid in _topo(tasks):
        if (prior.get(tid) or {}).get("status") == "merged":
            make_skip(tasks[tid])
        else:
            make_chain(tasks[tid])
        if tasks[tid].get("when"):
            make_skip_node(tasks[tid])

    # --- the chain gate -------------------------------------------------
    # With `after`, every head (including conflict-repair publishes) runs
    # only once every upstream task is merged; the gate is a plain node with
    # conditional edges so a blocked chain ENDS the run with a clean result
    # (main.py turns it into exit code 1) instead of a raised exception, and
    # no head allocates a worktree while it waits.
    after_keys = list(taskset.get("after") or [])
    if after_keys:
        cyc = _after_cycle(taskfile, after_keys)
        if cyc:
            raise ValueError(
                "project.after cycle detected: "
                + " -> ".join(Path(k).name for k in cyc))
        g.node("chain_wait", _make_chain_wait(store, taskfile, after_keys))
        g.start("chain_wait")
        for h in heads:
            g.edge("chain_wait", h, when=lambda r, c: r.get("ok"))
    else:
        for h in heads:
            g.start(h)
    for tid in tasks:
        # ONE tail node PER TASK, edged from that task's FAILURE, not its
        # merge. The merge path is useless for this: pr_merge calls
        # gitstore.cleanup(repo, tid) before it returns, so by the time a node
        # downstream of it runs the worktree is already gone and there is
        # nothing left to save. `fail` is where work actually survives — the
        # task stopped mid-flight with its worktree intact, which is exactly
        # the state the next alloc would reset. Not a gathered sweep either: a
        # task still mid-pipeline when a sibling drains never reaches fail at
        # all, so a gather waiting on it hangs the drain ("unreachable
        # sources"), and reading a tree somebody is writing is wrong anyway.
        # A merged task has only a skip stub, so neither node exists for it.
        if f"fail_{tid}" in g.nodes:
            g.node(f"checkpoint_{tid}", _make_checkpoint_one(tid, repo))
            g.edge(f"fail_{tid}", f"checkpoint_{tid}", on_drain=True)
    return g


def _make_checkpoint_one(tid, repo):
    """Checkpoint THIS task's worktree, the moment the task fails.

    The implement node checkpoints after every attempt already; this is the
    second belt for the path BETWEEN attempts — a task that died during a gate,
    a review or a PR round left work in its worktree that the next alloc would
    reset, and nothing else saves it before the run ends. Only ever this one
    worktree, so a drain never touches a sibling that is still running. Never
    raises.
    """

    async def checkpoint_one(ctx):
        results = ctx.get("results", {})
        wt = (results.get(f"alloc_{tid}") or {}).get("worktree")
        if not wt:
            # A resume that started at publish never ran alloc in THIS graph,
            # so the worktree is not in the results — ask git for it, the same
            # way every other node does.
            try:
                found = await gitstore.existing_worktree(repo, tid)
            except Exception:                                   # noqa: BLE001
                found = None
            if found is None:
                return {"saved": None}
            wt = str(found)
        try:
            p = await gitstore.checkpoint(repo, tid, Path(wt), "interrupted")
        except Exception as exc:                                # noqa: BLE001
            errors.capture(exc, task=tid, node=f"checkpoint_{tid}")
            return {"saved": None}
        return {"saved": str(p) if p else None}

    return checkpoint_one


def _routing_tiers_prose():
    """The planner's routing instructions, written from today's roster."""
    tiers = config.IMPLEMENT_TIERS
    lines = ["ROUTING TIERS (enforced — a task file that violates these is rejected):\n"]
    if tiers.get("medium"):
        medium = tiers["medium"]
        lines.append(f"- {' or '.join(medium)}: medium tasks (a self-contained "
                     "feature, a new endpoint, moderate refactor of one file) AND the "
                     "mechanical ones (rename, small HTML/CSS, wiring, config) — there is "
                     "no lower tier.\n")
        if len(medium) > 1:
            # The tier has more than one implementer, so say so: without this the
            # planner may route EVERY medium task to one of them and leave the
            # other idle. The operator asked for a MIX (2026-09-16), and the
            # medium models run on different harnesses (deepseek on reasonix,
            # union on opencode), so spreading them also spreads the local
            # harness load instead of stacking every medium task on one pool.
            lines.append(f"- Spread medium tasks across {' and '.join(medium)} — "
                         "give independent tasks DIFFERENT models so they run in "
                         "parallel and neither sits idle; do NOT send every medium "
                         "task to the same one.\n")
    if tiers.get("hard"):
        lines.append(f"- {' or '.join(tiers['hard'])}: hard tasks that need deep "
                     "understanding, multi-file reasoning, delicate architecture, or "
                     "subtle debugging.\n")
    lines.append(f"- {config.ESCALATION_PATH[-1]} is the fleet's strongest model "
                 "(the last escalation stage) — prefer it for the hardest tasks.\n")
    fams = list(config.REVIEW_FAMILIES)
    pairs = []
    for m in config.ESCALATION_PATH:
        r = config.cross_family_reviewer(m)
        if r:
            pairs.append(f"work by {m} is reviewed by {r}")
    lines.append(f"- reviewer is one of {' or '.join(fams)}. Cross-review rule: "
                 + "; ".join(pairs) + ". Spread reviews across the review-capable "
                 "families so none idles or saturates.\n\n")
    return "".join(lines)


def plan_schema_hint():
    """The taskfile schema the planner is shown, with TODAY'S models in it.

    A string literal here named gpt-oss and Kimi-K3 long after both had left.
    Generated from the roster so the planner is never told to route work to a
    model that retired (gpt-oss 09-11, Kimi-K3 09-12)."""
    models = " | ".join(f'"{m}"' for m in config.ESCALATION_PATH)
    reviewers = " | ".join(f'"{f}"' for f in config.REVIEW_FAMILIES)
    return (
        '{"project": {"repo": "<abs path>", "title": "<short>",\n'
        ' "pattern": "<catalogue id: single|chain|fanout|diamond|router|debate|hierarchical>",\n'
        ' "after": ["<taskfile name this whole plan must wait for; omit when none>"],\n'
        ' "tasks": [{"id": "<kebab-id>", "title": "...", "prompt": "<detailed spec>",\n'
        f'            "model": {models},\n'
        f'            "reviewer": {reviewers},\n'
        '            "verify_cmd": "<shell cmd run in the worktree, empty ok>",\n'
        '            "probe_cmd": "<optional: prints a JSON verdict after the gate passes>",\n'
        '            "when": {"dep": "<a dep id>", "key": "<verdict field>", "equals": "<value>"},\n'
        '            "files_hint": ["path/..."], "deps": ["<id>", ...]}]}}')


PLAN_SCHEMA_HINT = plan_schema_hint()


def _balanced_span(text, start):
    """text[start:closing] of the balanced {...} at start, str-aware; None if unbalanced."""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _plan_is_substantive(obj):
    """Reject a plan whose tasks are placeholders.

    Planners sometimes emit an abbreviated copy first — same schema, but with
    task prompts literally "...". Taking the first schema match would accept
    that decoy and hand the fleet unrunnable work, which is a far worse
    failure than a loud one.
    """
    tasks = (obj.get("project") or {}).get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return False
    for t in tasks:
        if not isinstance(t, dict) or not t.get("id"):
            return False
        if len(str(t.get("prompt") or "").strip()) < 40:
            return False
    return True


def _extract_plan_json(text):
    """Best balanced JSON span that parses as a real plan; None if absent.

    Planners echo the JSON more than once (fenced copy + bare copy) or append
    prose, so a greedy regex over-spans and nested fragments under-match —
    accept only a balanced span carrying the project/tasks schema.

    The LAST substantive span wins, matching _parse_verdict: what a model says
    last is its answer, and an earlier copy is often an abbreviated sketch.
    A substantive span is preferred over a placeholder one at any position.
    """
    fallback = None
    best = None
    for m in re.finditer(r"\{", text):
        span = _balanced_span(text, m.start())
        if span is None:
            continue
        try:
            obj = json.loads(span)
        except ValueError:
            continue
        if not (isinstance(obj.get("project"), dict) and "tasks" in obj["project"]):
            continue
        if _plan_is_substantive(obj):
            best = span
        elif fallback is None:
            fallback = span
    return best or fallback


def _transcript_assistant_messages(path):
    """Assistant message texts from a harness transcript, oldest first.

    Reads BOTH transcript dialects. kimi stream-json carries
    {"role": "assistant", "content": ...} lines. opencode session logs carry
    NO role lines at all — assistant text lives in
    {"type": "text", "part": {"text": ...}} records, next to step_finish
    bookkeeping and synthetic compaction markers. Reading only the role
    dialect means a successful GLM-via-opencode plan is parsed as empty and
    thrown away with "produced no usable JSON": the res.text fallback is
    parse_transcript's last 3000 chars and a real plan is 6.5-11.5KB, so the
    fallback can never hold one. Exactly this killed the first opencode-GLM
    plan attempt on 2026-09-13.
    """
    out = []
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("type") == "text":  # opencode event dialect
            part = obj.get("part")
            if isinstance(part, dict) and not part.get("synthetic"):
                t = part.get("text")
                if isinstance(t, str) and t.strip():
                    out.append(t)
            continue
        if obj.get("role") != "assistant":
            continue
        content = obj.get("content")
        if isinstance(content, str) and content.strip():
            out.append(content)
        elif isinstance(content, list):  # opencode-style content parts
            text = "".join(c.get("text", "") for c in content
                           if isinstance(c, dict))
            if text.strip():
                out.append(text)
    return out


def _plan_json_from_run(res):
    """The plan JSON from a finished planner run, or None.

    Reads the TRANSCRIPT rather than DriverResult.text. drivers.parse_transcript
    caps that text at the last 3000 characters — harmless for reviewer verdicts,
    which are small and scanned backwards, but fatal for a plan: real ones run
    6.5-11.5KB, so the opening `{"project":` is always cut off and no balanced
    span can ever match. The planner would complete normally, exit 0, and have
    its work thrown away with "produced no usable JSON".
    """
    for msg in reversed(_transcript_assistant_messages(res.transcript_path)):
        span = _extract_plan_json(msg)
        if span is not None:
            return span
    return _extract_plan_json(res.text or "")


def _existing_projects_prose(repo, store=None):
    """Other taskfiles for this repo, with their state, so the planner can
    chain a new plan `after` one that has not merged yet instead of writing
    tasks that assume code which is still on a branch."""
    repo_key = str(Path(repo).resolve())
    d = Path(config.TASKS_DIR)
    rows = []
    files = sorted(d.glob("*.json"), key=lambda f: f.stat().st_mtime) if d.is_dir() else []
    for f in files[-12:]:  # the dozen most recent — the ones a new plan may build on
        try:
            proj = json.loads(f.read_text(encoding="utf-8")).get("project") or {}
        except Exception:
            continue
        if str(Path(str(proj.get("repo") or "")).resolve()) != repo_key:
            continue
        ids = [t.get("id") for t in proj.get("tasks") or [] if isinstance(t, dict)]
        state = "state unknown"
        if store is not None:
            try:
                st = {r["id"]: r["status"] for r in store.code_tasks_for(str(f.resolve()))}
                merged = sum(1 for i in ids if st.get(i) == "merged")
                state = ("merged" if ids and merged == len(ids) else
                         "not started" if not st else f"{merged}/{len(ids)} merged")
            except Exception:
                pass
        rows.append(f"    {f.name}: {proj.get('title') or f.stem} ({len(ids)} tasks, {state})")
    if not rows:
        return ""
    return ('EXISTING PROJECTS FOR THIS REPO (name one in "after" if this plan '
            "builds on it and it is not merged yet):\n" + "\n".join(rows) + "\n\n")


def _orientation_prose(repo_map):
    """The planner's first look at the repo, from the code graph.

    Directory clusters, hub symbols and hotspots in ~900 tokens — what the
    planner would otherwise spend its first (slowest-model) minutes deriving
    with ls and grep. Also tells it the implementers have the same graph, so
    task prompts can name symbols and trust they will be found.
    """
    if not repo_map:
        return ""
    return ("REPO ORIENTATION (from the code graph: directory clusters, hub "
            "symbols with their caller counts, hotspots). Use it to name the "
            "exact files and symbols each task touches — implementers get "
            "graph-ranked file:line hints for whatever you name:\n\n"
            + repo_map + "\n\n")


async def plan_tasks(goal, repo, out_path=None, store=None):
    import graph_shapes
    prompt = (
        "You are the ORCHESTRATOR of a multi-model coding fleet. Your output "
        "is the execution graph itself: the runner below you executes exactly "
        "what you emit — you decide the task breakdown, the fanout "
        "(which tasks run in parallel), the dependencies (which must wait), "
        "the model routing (who implements), the reviewer, and the verify "
        "gate for every task. Design it well; there is no later triage.\n\n"
        f"GOAL: {goal}\nTARGET REPO: {repo}\n\n"
        + _orientation_prose(await graft.repo_map(repo))
        + _routing_tiers_prose()
        + graph_shapes.planner_prose()
        + _existing_projects_prose(repo, store)
        + project_contract.planner_block(repo) +
        "TASK DESIGN:\n"
        "- Each task prompt must be fully self-contained: the implementer "
        "sees ONLY its prompt and the repo, never this goal. Include file "
        "paths, function names, acceptance criteria.\n"
        "- Small tasks (<30 min each). Prefer one more parallel small task "
        "over one big serial one.\n"
        "- SIZE IS A CORRECTNESS CONCERN, not just speed. A task that has to "
        "emit a long response is the most likely to fail: the API terminates "
        "long-running requests, and tasks asked for whole-file rewrites fail "
        "33% of their requests against an 8% baseline. Never write a prompt "
        "that implies rewriting a large file — name the function or the lines "
        "to change.\n"
        "- EVERY task that changes code must also add or update TESTS. Say so "
        "in the prompt and require it in verify_cmd. Two independent reviewers "
        "read the pull request afterwards and are instructed to REJECT a code "
        "change that ships no test which would fail without it. A task with no "
        "tests will simply loop and then fail.\n"
        "- Keep each task's tests in their own file where possible, so two "
        "parallel tasks do not both edit one test file and conflict.\n"
        "- verify_cmd: a deterministic shell check run in the task's "
        "worktree (tests, build, node --check, grep). Leave empty only for "
        "purely cosmetic tasks. A failing gate bounces the task back to the "
        "implementer (max 3 fix rounds), so make it honest. A grep only "
        "proves a string is present, never that the change WORKS — if the "
        "repo has a test suite or a self-check script (e.g. ./check.sh), make "
        "it the FIRST clause of every gate that touches code: "
        "'./check.sh && grep -q ...'. A task that edits the orchestrator "
        "itself must not be able to merge a change that breaks it.\n\n"
        "WHAT HAPPENS TO YOUR PLAN (design for it):\n"
        "- Each task gets its own git worktree on branch task/<id>, branched "
        f"from `{config.BASE_BRANCH}`.\n"
        "- implement -> verify gate -> one cross-family review -> commit, "
        "push, and OPEN A PULL REQUEST. Nothing is merged locally.\n"
        f"- {config.PR_REVIEWERS} further independent reviewers then read the "
        "real PR diff. ALL must approve or the task goes back to the "
        "implementer with their issues, up to "
        f"{config.PR_MAX_ROUNDS} rounds, then it fails.\n"
        f"- Only then is the PR merged into `{config.BASE_BRANCH}`. A task "
        "with deps starts only after its dependency's PR has MERGED.\n"
        "- So: a task that is vague, untestable, or too large does not merely "
        "run slowly — it gets rejected repeatedly and fails. Write each task "
        "so a reviewer who sees only the diff and the prompt can tell whether "
        "it is correct.\n\n"
        "Reply with STRICT JSON only, matching exactly this shape:\n"
        + PLAN_SCHEMA_HINT
    )
    # The strongest live model that may plan — GLM-5.3 on the two-model
    # roster pinned 2026-09-12 (Kimi-K3 retired that day).
    #
    # A planner can exit 0 with NO usable JSON: reasoning models burn the
    # whole opencode output budget on thinking and finish reason=length with
    # zero emitted text. That threw away a 40-minute GLM plan on 2026-09-13,
    # unnoticed until the traceback. One tightened retry costs little against
    # losing the run; the retry gets its own task_id so the first attempt's
    # transcript survives instead of being overwritten.
    raw = None
    res = None
    for attempt in (1, 2):
        res = await _driver(config.PLANNER_MODEL, "planner", None).run(
            prompt, Path(repo), task_id="plan" if attempt == 1 else "plan-r2")
        raw = _plan_json_from_run(res)
        if raw is not None:
            break
        prompt += (
            "\n\nPREVIOUS ATTEMPT LOST: your run exhausted its output budget "
            "on investigation notes and produced NO taskfile JSON. Keep the "
            "investigation tight and emit the final taskfile JSON EARLY — "
            "the JSON is the deliverable.\n")
    if raw is None:
        raise RuntimeError(f"planner produced no usable JSON; transcript: {res.transcript_path}")
    json.loads(raw)  # validate
    Path(config.TASKS_DIR).mkdir(parents=True, exist_ok=True)
    out = Path(out_path or Path(config.TASKS_DIR) / (re.sub(r"[^a-z0-9]+", "-", goal.lower())[:40].strip("-") + ".json"))
    out.write_text(raw + "\n", encoding="utf-8")
    return out
