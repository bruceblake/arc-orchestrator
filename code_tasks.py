"""Multi-harness code workload: JSON task files -> task graph -> worktrees.

Per task: alloc worktree -> implement (opencode: gpt-oss-120b / DeepSeek-V4-Flash)
-> deterministic verify gate (verify_cmd) -> cross-family review (Kimi-K3 via
kimi CLI, or GLM-5.3 via opencode) -> bounded fix loop -> publish commit ->
merge to main (serialized) -> cleanup. Reviews are mandatory and cross-family
by default; an explicit bench `policy` (see orchbench.py) may relax
routing/review rules to measure what the governance defaults buy.
"""
import asyncio
import json
import logging
import re
import time
from pathlib import Path

import config
import events
import gitstore
from drivers import DriverError, KimiDriver, OpencodeDriver, transcript_tokens
from graph import Graph

log = logging.getLogger("code-tasks")

_merge_lock = asyncio.Lock()


def load_taskfile(path, policy=None):
    """Load + validate a taskfile. `policy` (bench variant overrides) may widen
    the allowed implementers/reviewers, permit self-review, or disable review;
    with policy=None the governance defaults apply byte-for-byte."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    repo = Path(data["project"]["repo"]).resolve()
    pol = policy or {}
    models = set(config.IMPLEMENTER_MODELS) | set(pol.get("implementers", []))
    reviewers = tuple(pol.get("reviewers", ("kimi", "glm")))
    review_on = pol.get("review", True)
    allow_self = bool(pol.get("allow_self_review"))
    tasks = {}
    for t in data["project"]["tasks"]:
        tid = t["id"]
        if tid in tasks:
            raise ValueError(f"duplicate task id: {tid}")
        model = t.get("model", "")
        if model not in models:
            raise ValueError(
                f"task {tid}: model {model!r} must be an implementer ({sorted(models)})"
            )
        reviewer = t.get("reviewer", "")
        if review_on and reviewer not in reviewers:
            raise ValueError(f"task {tid}: reviewer must be one of {reviewers}, got {reviewer!r}")
        impl_family = config.MODEL_FAMILY[model]
        rev_family = config.MODEL_FAMILY.get(reviewer, reviewer)
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
        }
    for tid, t in tasks.items():
        for d in t["deps"]:
            if d not in tasks:
                raise ValueError(f"task {tid}: unknown dep {d!r}")
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
    return {"repo": repo, "tasks": tasks, "title": data.get("project", {}).get("title", ""),
            "policy": pol, "after": after}


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


def describe(taskset):
    lines = [f"repo: {taskset['repo']}"]
    if taskset.get("after"):
        lines.append("after: " + ", ".join(
            Path(k).name for k in taskset["after"]))
    for tid in _topo(taskset["tasks"]):
        t = taskset["tasks"][tid]
        impl_fam = config.MODEL_FAMILY[t["model"]]
        rev_fam = config.MODEL_FAMILY.get(t["reviewer"], t["reviewer"])
        cross = "cross-family" if impl_fam != rev_fam else "SAME-FAMILY(!)"
        lines.append(
            f"  {tid}: implement={t['model']} review={rev_fam}({cross}) "
            f"deps={t['deps'] or '[]'} base={config.BASE_BRANCH} "
            f"verify={t['verify_cmd'] or '(none)'}"
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


def _impl_prompt(t, feedback):
    p = (
        f"You are implementing one task in this repository.\n\n"
        f"TASK {t['id']}: {t['title']}\n\n{t['prompt']}\n"
    )
    if t["files_hint"]:
        p += f"\nFiles you are expected to touch: {', '.join(t['files_hint'])}\n"
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
        "- Locate code with grep/search FIRST; read only the line ranges you "
        "need, never a whole large file.\n"
        "- Do not re-read a file you have already seen; rely on what is "
        "already in the conversation.\n"
        "- Work in several small edits, each one verified, rather than one "
        "sweeping change. Many short requests succeed where one long one "
        "does not.\n"
    )
    if feedback:
        p += f"\nPrevious attempt was rejected. Fix these issues:\n{feedback}\n"
    return p


def _review_prompt(t, diff):
    return (
        f"You are reviewing an implementation produced by another AI agent.\n\n"
        f"TASK {t['id']}: {t['title']}\n\nSPEC:\n{t['prompt']}\n\n"
        f"The implementation already passed its automated verify gate "
        f"({t['verify_cmd'] or 'none'}). Here is the full diff:\n\n{diff}\n\n"
        "Review for: spec compliance, correctness, and scope discipline "
        "(nothing unrelated). Reply with STRICT JSON only, no prose, of the form:\n"
        '{"pass": true}  or  {"pass": false, "issues": ["specific issue 1", ...]}\n'
        "Pass only if the change fully and correctly implements the spec."
    )


def _parse_verdict(text):
    """Last balanced span carrying a "pass" key wins.

    The flat regex could not span braces inside quoted code in the verdict
    prose (e.g. "{WORLD_X,WORLD_Z,WORLD_H}"), silently dropping real verdicts.
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
            return {"pass": bool(obj["pass"]),
                    "issues": [str(i) for i in obj.get("issues", [])]}
    return {"pass": False, "issues": ["reviewer returned no parseable verdict"]}


def _pr_review_prompt(t, diff, n_reviewers, round_n, prior_issues):
    """Prompt for a reviewer reading a real pull request.

    Deliberately different from the pre-PR review: this reviewer can BLOCK the
    change, so it is told what it owns, that tests are mandatory, and that
    deferring to a colleague is not its job.
    """
    p = (
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
    p += f"\nTHE PULL REQUEST DIFF:\n\n{diff}\n\nReview for, in order:\n"
    p += ("1. CORRECTNESS — does it do what the spec says, without bugs? Trace "
          "the logic; do not assume it works because it looks plausible.\n"
          "2. REGRESSIONS — could this break existing behaviour? Consider what "
          "calls the changed functions.\n")
    if config.REQUIRE_TESTS:
        p += ("3. TESTS — a code change MUST come with tests that would FAIL "
              "without it. Reject if there are none, if they only assert the "
              "code runs, or if they miss the behaviour the spec describes. "
              "Documentation-only changes are exempt.\n")
    p += ("4. SCOPE — nothing unrelated to the spec.\n\n"
          "Review independently: do not assume another reviewer checked "
          "something. Be specific — name the file and line, say what is wrong "
          "and what would fix it. Vague objections waste a whole round.\n\n"
          "Reply with STRICT JSON only, no prose:\n"
          '{"approve": true}  or  '
          '{"approve": false, "issues": ["file.py:42 — problem and fix", ...]}')
    return p


def _parse_approval(text):
    """Last balanced span carrying an "approve" key wins; fails closed."""
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
            return {"approve": bool(obj["approve"]),
                    "issues": [str(i) for i in obj.get("issues", [])]}
    return {"approve": False, "issues": ["reviewer returned no parseable verdict"]}


def _driver(model, role, policy):
    """Implementer/reviewer driver. policy['harness'] maps model -> kimi|opencode
    (bench variants); default keeps the governed routing (Kimi-K3 -> kimi CLI)."""
    pol = policy or {}
    harness = pol.get("harness", {}).get(model)
    if harness is None:
        harness = "kimi" if model == "Kimi-K3" else "opencode"
    if harness == "kimi":
        return KimiDriver(role, bench=bool(pol))
    return OpencodeDriver(model, role, bench=bool(pol))


def _reviewer_driver(t, policy):
    token = t["reviewer"]
    if token == "kimi":
        return KimiDriver("reviewer", bench=bool(policy))
    if token == "glm":
        return OpencodeDriver("GLM-5.3", "reviewer", bench=bool(policy))
    return _driver(token, "reviewer", policy)


# Failure reasons that mean "this model could not do the task" and so justify
# resuming one tier higher. Anything else (a killed run process, a cancelled
# graph, a harness crash, a merge conflict) is infrastructure noise: the task
# resumes at the SAME tier, because escalating on it wastes the scarcest models.
_CAPABILITY_FAILURES = ("exhausted escalation", "exhausted fix rounds",
                        "review rejected", "gate failed")


def _is_capability_failure(error):
    low = (error or "").lower()
    if not low:
        # Pre-existing rows written before failures carried a reason: treat an
        # unexplained failure as a capability signal, matching the old behaviour.
        return True
    return any(m in low for m in _CAPABILITY_FAILURES)


def build_code_graph(store, taskset, taskfile="", policy=None):
    repo = taskset["repo"]
    tasks = taskset["tasks"]
    pol = policy if policy is not None else taskset.get("policy") or None
    mfr = (pol or {}).get("max_fix_rounds", config.MAX_FIX_ROUNDS)
    review_on = (pol or {}).get("review", True)
    escalate_on = (pol or {}).get("escalate", True)
    g = Graph("code-tasks", max_steps=config.MAX_GRAPH_STEPS)
    # Task nodes with no in-task deps ("heads") start the graph — directly
    # when there is no `after`, else behind the chain_wait gate.
    heads = []

    # --- resume: statuses recorded by earlier runs of THIS taskfile ----------
    # Re-running `code run <taskfile>` is a resume of the same project: merged
    # tasks collapse into skip stubs, failed/conflict/stale/pending tasks run
    # again — failed ones one tier higher, conflict ones keeping their model.
    prior = {}
    if taskfile and store is not None:
        try:
            prior = {r["id"]: r for r in store.code_tasks_for(taskfile)}
        except Exception:
            prior = {}

    def _tier_index(model):
        try:
            return config.ESCALATION_PATH.index(model)
        except ValueError:
            return None

    def _next_tier(model):
        """The next stronger model for `model`, or None at the top.

        A model that is not ON the escalation path (a taskfile may still route
        explicitly to gpt-oss-120b for mechanical work) counts as below the
        entry tier, so it escalates INTO the path rather than being stuck
        unable to escalate at all.
        """
        idx = _tier_index(model)
        if idx is None:
            return config.ESCALATION_PATH[0] if config.ESCALATION_PATH else None
        if idx + 1 < len(config.ESCALATION_PATH):
            return config.ESCALATION_PATH[idx + 1]
        return None

    def reviewer_for(t, model):
        """Cross-review preserved under escalation: a strong model's work is
        reviewed by the other strong harness; basic/medium keep the taskfile
        reviewer."""
        fam = config.MODEL_FAMILY.get(model)
        if fam == "kimi":
            return "glm"
        if fam == "glm":
            return "kimi"
        return t["reviewer"]

    def start_model(tid):
        """Model a (possibly resumed) run starts this task at.

        Only a genuine capability failure escalates. A row that says the model
        exhausted its fix rounds is evidence the tier was too weak; a row the
        stale-reset wrote because its run process was killed says nothing about
        the model at all. Escalating the latter used to send every interrupted
        task straight to Kimi-K3 — the scarcest, most stall-prone tier — so one
        killed queue turned into four tasks piled on a cap of two.
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
        events.emit("run.resume", taskfile=taskfile, skipped_merged=skipped,
                    retried=retried, escalated_on_resume=higher)

    async def _pr_hook(tid, t):
        """Best-effort push + PR after a successful merge; never raises."""
        try:
            url, note = await gitstore.push_and_open_pr(
                repo, tid, t["title"], taskfile)
            if url:
                events.emit("task.pr_opened", task=tid, url=url)
            else:
                events.emit("task.pr_skipped", task=tid, reason=note)
        except Exception as exc:  # publish must never fail on the PR hook
            events.emit("task.pr_skipped", task=tid, reason=f"pr hook: {exc}"[:200])

    def make_skip(t):
        """Merged task: collapse to a stub publish so dependents see it as done."""
        tid = t["id"]

        async def publish(ctx):
            return {"merged": True, "skipped": True, "head": None}

        async def pr_merge(ctx):
            return {"merged": True, "skipped": True}

        g.node(f"publish_{tid}", publish)
        g.node(f"pr_merge_{tid}", pr_merge)
        g.edge(f"publish_{tid}", f"pr_merge_{tid}")
        if t["deps"]:
            g.edge(f"pr_merge_{t['deps'][-1]}", f"publish_{tid}")
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

        def cur_model(ctx):
            esc = ctx.get("results", {}).get(f"escalate_{tid}")
            return esc["to_model"] if esc else model0

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

        async def alloc(ctx):
            wt = await gitstore.alloc(repo, tid, base)
            store.upsert_code_task(taskfile, tid, t["title"], model0,
                                   reviewer_for(t, model0), "running",
                                   branch=f"task/{tid}", worktree=str(wt))
            events.set_context(module=tid)
            events.emit("worktree.alloc", path=str(wt), base=base)
            return {"worktree": str(wt)}

        async def implement(ctx):
            results = ctx.get("results", {})
            feedback = ""
            rev = results.get(f"review_{tid}")
            if rev and not rev.get("pass"):
                feedback = "\n".join(f"- {i}" for i in rev.get("issues", []))
            gate = results.get(f"gate_{tid}")
            if not feedback and gate and not gate.get("passed"):
                feedback = f"verify gate failed, output:\n{gate.get('output', '')}"
            attempt = ctx.get("runs", {}).get(f"implement_{tid}", 0) + 1
            model = cur_model(ctx)
            driver = _driver(model, "implementer", pol)
            try:
                res = await driver.run(
                    _impl_prompt(t, feedback), Path(results[f"alloc_{tid}"]["worktree"]),
                    task_id=f"{tid}-x{attempt}")
            except DriverError as exc:
                if not (pol or {}).get("tolerate_driver_error", True):
                    raise
                store.save_harness_run(tid, driver.harness, model, "implementer",
                                       attempt, 1, "", 0.0)
                events.emit("driver.error", task=tid, role="implementer",
                            error=str(exc)[:200])
                return {"crashed": True, "error": str(exc)[:200],
                        "harness": driver.harness}
            store.save_harness_run(tid, driver.harness, model, "implementer",
                                   attempt, res.exit_code, res.transcript_path, res.seconds)
            return {"session_id": res.session_id, "harness": driver.harness}

        async def gate(ctx):
            cmd = t["verify_cmd"]
            prev = ctx.get("results", {}).get(f"implement_{tid}", {})
            if prev.get("crashed"):
                return {"passed": False,
                        "output": f"implementer crashed: {prev.get('error', '')}"}
            if not cmd:
                return {"passed": True, "output": ""}
            wt = ctx["results"][f"alloc_{tid}"]["worktree"]
            proc = await asyncio.create_subprocess_shell(
                cmd, cwd=wt,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), config.GATE_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return {"passed": False, "output": f"gate timed out after {config.GATE_TIMEOUT}s",
                        "log_path": None}
            output = out.decode(errors="replace")[-2000:]
            attempt = ctx.get("runs", {}).get(f"implement_{tid}", 0)
            log_path = None
            try:
                log_dir = Path(config.ROOT) / "logs" / "gates"
                log_dir.mkdir(parents=True, exist_ok=True)
                log_path = str(log_dir / f"{tid}-x{attempt}.log")
                Path(log_path).write_text(out.decode(errors="replace"))
            except OSError:
                log_path = None
            events.emit("task.gate", passed=proc.returncode == 0, log=log_path)
            return {"passed": proc.returncode == 0, "output": output, "log_path": log_path}

        async def review(ctx):
            if not review_on:
                events.emit("task.reviewed", passed=True, reviewer="none",
                            skipped=True)
                return {"pass": True, "issues": [], "skipped": True}
            wt = Path(ctx["results"][f"alloc_{tid}"]["worktree"])
            diff = await gitstore.diff_full(wt, base)
            rev_tok = reviewer_for(t, cur_model(ctx))
            driver = _reviewer_driver({"reviewer": rev_tok}, pol)
            attempt = ctx.get("runs", {}).get(f"review_{tid}", 0) + 1
            try:
                res = await driver.run(_review_prompt(t, diff), wt, task_id=f"{tid}-x{attempt}")
            except DriverError as exc:
                if not (pol or {}).get("tolerate_driver_error", True):
                    raise
                store.save_harness_run(tid, driver.harness, driver.model, "reviewer",
                                       attempt, 1, "", 0.0,
                                       verdict='{"pass": false, "issues": ["reviewer crashed"]}')
                events.emit("driver.error", task=tid, role="reviewer",
                            error=str(exc)[:200])
                return {"pass": False, "issues": [f"reviewer crashed: {exc}"[:200]]}
            verdict = _parse_verdict(res.text)
            store.save_harness_run(tid, driver.harness, driver.model, "reviewer",
                                   attempt, res.exit_code, res.transcript_path,
                                   res.seconds, verdict=json.dumps(verdict)[:500])
            events.emit("task.reviewed", passed=verdict["pass"], reviewer=rev_tok)
            return verdict

        async def escalate(ctx):
            src = cur_model(ctx)
            nxt = _next_tier(src)
            rev = reviewer_for(t, nxt)
            store.upsert_code_task(taskfile, tid, t["title"], nxt, rev, "running")
            events.emit("task.escalated", task=tid, from_model=src, to_model=nxt,
                        n=esc_n(ctx) + 1)
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
            if alloc_res is None:
                return {"published": False, "reason": "no worktree"}
            wt = Path(alloc_res["worktree"])
            impl = results.get(f"implement_{tid}", {})
            model, rev = cur_model(ctx), reviewer_for(t, cur_model(ctx))
            head = await gitstore.publish(
                wt, f"task({tid}): {t['title']}",
                {"Harness": impl.get("harness", "?"), "Model": model,
                 "Reviewer": rev, "Task-Id": tid})
            if head is None:
                store.upsert_code_task(taskfile, tid, t["title"], model, rev,
                                       "failed", error="implementer produced no changes",
                                       finished=True)
                events.emit("task.failed", task=tid, reason="no changes to publish")
                return {"published": False, "reason": "no changes"}
            ok, note = await gitstore.push_task_branch(repo, tid)
            if not ok:
                store.upsert_code_task(taskfile, tid, t["title"], model, rev,
                                       "failed", error=f"push failed: {note}",
                                       finished=True)
                events.emit("task.failed", task=tid, reason=f"push failed: {note}")
                return {"published": False, "reason": note}
            body = (f"Task `{tid}` from `{Path(taskfile).name if taskfile else '?'}`\n\n"
                    f"{t['prompt'][:1500]}\n\n---\n"
                    f"Implemented by **{model}**, pre-review by **{rev}**.\n"
                    f"Verify gate: `{t['verify_cmd'] or '(none)'}`\n\n"
                    f"{config.PR_REVIEWERS} independent reviewers must approve "
                    f"before this merges.")
            number, url, note = await gitstore.open_pr(
                repo, tid, f"task({tid}): {t['title']}", body, base)
            if number is None:
                store.upsert_code_task(taskfile, tid, t["title"], model, rev,
                                       "failed", error=f"could not open PR: {note}",
                                       finished=True)
                events.emit("task.failed", task=tid, reason=f"pr: {note}")
                return {"published": False, "reason": note}
            store.upsert_code_task(taskfile, tid, t["title"], model, rev,
                                   "in_review", branch=f"task/{tid}")
            events.emit("task.pr_opened", task=tid, url=url, number=number,
                        head=head, note=note)
            return {"published": True, "pr": number, "url": url, "head": head}

        async def pr_review(ctx):
            """N independent reviewers read the real PR diff. All must approve."""
            pub = ctx.get("results", {}).get(f"publish_{tid}") or {}
            number = pub.get("pr")
            if not number:
                return {"approved": False, "issues": ["no pull request to review"]}
            round_n = ctx.get("runs", {}).get(f"pr_review_{tid}", 0) + 1
            prior_r = ctx.get("results", {}).get(f"pr_review_{tid}") or {}
            diff = await gitstore.pr_diff(repo, number)
            # Reviewers differ from the implementer's family AND from each
            # other, so two approvals mean two genuinely separate readings.
            impl_fam = config.MODEL_FAMILY.get(cur_model(ctx))
            pool = [m for m in ("Kimi-K3", "GLM-5.3", "DeepSeek-V4-Flash")
                    if config.MODEL_FAMILY.get(m) != impl_fam]
            chosen = pool[:max(1, config.PR_REVIEWERS)]

            async def one(model):
                drv = _driver(model, "reviewer", pol)
                try:
                    res = await drv.run(
                        _pr_review_prompt(t, diff, len(chosen), round_n,
                                          prior_r.get("issues") or []),
                        Path(ctx["results"][f"alloc_{tid}"]["worktree"]),
                        task_id=f"{tid}-pr{round_n}")
                except DriverError as exc:
                    return model, {"approve": False,
                                   "issues": [f"reviewer {model} crashed: {exc}"[:200]]}
                verdict = _parse_approval(res.text)
                store.save_harness_run(tid, drv.harness, model, "pr-reviewer",
                                       round_n, res.exit_code, res.transcript_path,
                                       res.seconds, verdict=json.dumps(verdict)[:500])
                return model, verdict

            outcomes = await asyncio.gather(*[one(m) for m in chosen])
            issues, approvals = [], []
            for model, v in outcomes:
                if v["approve"]:
                    approvals.append(model)
                else:
                    issues.extend(f"[{model}] {i}" for i in v["issues"])
            approved = len(approvals) == len(chosen)
            events.emit("task.pr_reviewed", task=tid, pr=number, round=round_n,
                        approved=approved, approvals=approvals,
                        reviewers=chosen, n_issues=len(issues))
            # Post each verdict AS A GITHUB REVIEW, not just internally. The
            # approvals existed only in our event log, so a PR merged by two
            # AI reviewers showed "0 reviews" on GitHub — the trail was
            # invisible exactly where a human would look for it.
            for model, v in outcomes:
                body = (f"**{model}** (round {round_n}) — "
                        + ("approved." if v["approve"] else "changes requested:\n\n"
                           + "\n".join(f"- {i}" for i in v["issues"][:20])))
                # A bot cannot formally approve its own repo's PR, so an
                # approval is posted as a comment and a rejection uses
                # --request-changes where permitted; both fall back to a plain
                # comment so the verdict is never lost.
                rc, _, _ = await gitstore._gh(
                    ["pr", "review", str(number),
                     "--approve" if v["approve"] else "--request-changes",
                     "--body", body], cwd=repo)
                if rc != 0:
                    await gitstore._gh(["pr", "comment", str(number),
                                        "--body", body], cwd=repo)
            if not approved:
                await gitstore._gh(
                    ["pr", "comment", str(number), "--body",
                     "**Changes requested** (round %d) — returning to the "
                     "implementer.\n\n%s" % (
                         round_n, "\n".join(f"- {i}" for i in issues[:20]))],
                    cwd=repo)
            return {"approved": approved, "issues": issues,
                    "approvals": approvals, "pr": number, "round": round_n}

        async def pr_merge(ctx):
            """Merge the PR — reached only once every reviewer approved."""
            rv = ctx.get("results", {}).get(f"pr_review_{tid}") or {}
            number = rv.get("pr")
            model, rev = cur_model(ctx), reviewer_for(t, cur_model(ctx))
            state = await gitstore.pr_state(repo, number)
            if state.get("mergeable") == "CONFLICTING":
                store.upsert_code_task(taskfile, tid, t["title"], model, rev,
                                       "conflict",
                                       error=f"PR #{number} conflicts with {base}",
                                       finished=True)
                events.emit("task.conflict", task=tid, pr=number,
                            reason="PR conflicts with the base branch")
                return {"merged": False, "reason": "conflict"}
            ok, note = await gitstore.merge_pr(repo, number)
            if not ok:
                store.upsert_code_task(taskfile, tid, t["title"], model, rev,
                                       "conflict", error=note, finished=True)
                events.emit("task.conflict", task=tid, pr=number, reason=note)
                return {"merged": False, "reason": note}
            # Fast-forward the local integration branch to what GitHub merged.
            await gitstore._git(["fetch", "origin", base], cwd=repo, check=False)
            await gitstore._git(["update-ref", f"refs/heads/{base}",
                                 f"origin/{base}"], cwd=repo, check=False)
            await gitstore.cleanup(repo, tid)
            store.upsert_code_task(taskfile, tid, t["title"], model, rev,
                                   "merged", finished=True)
            events.emit("task.merged", task=tid, pr=number,
                        approvals=rv.get("approvals"))
            return {"merged": True, "pr": number}

        async def fail(ctx):
            reason = ctx.get("results", {}).get(f"gate_{tid}", {})
            last = cur_model(ctx)
            store.upsert_code_task(taskfile, tid, t["title"], last,
                                   reviewer_for(t, last), "failed",
                                   error=f"exhausted escalation up to {last}",
                                   finished=True)
            events.emit("task.failed", task=tid,
                        reason=f"exhausted escalation up to {last}")
            emit_budget(ctx)
            return {"failed": True, "gate": reason}

        chain = {"alloc": alloc, "implement": implement, "gate": gate,
                 "review": review, "escalate": escalate, "publish": publish,
                 "pr_review": pr_review, "pr_merge": pr_merge, "fail": fail}
        for suffix, fn in chain.items():
            g.node(f"{suffix}_{tid}", fn)
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

        g.edge(f"publish_{tid}", f"pr_review_{tid}",
               when=lambda r, c: bool(r.get("published")))
        g.edge(f"pr_review_{tid}", f"pr_merge_{tid}",
               when=lambda r, c: bool(r.get("approved")))
        g.edge(f"pr_review_{tid}", f"implement_{tid}",
               when=lambda r, c: not r.get("approved")
               and pr_rounds(c) < config.PR_MAX_ROUNDS)
        g.edge(f"pr_review_{tid}", f"fail_{tid}",
               when=lambda r, c: not r.get("approved")
               and pr_rounds(c) >= config.PR_MAX_ROUNDS)
        for src in ("gate", "review"):
            key = "passed" if src == "gate" else "pass"
            g.edge(f"{src}_{tid}", f"implement_{tid}",
                   when=lambda r, c, k=key: not r[k] and within_budget(c))
            g.edge(f"{src}_{tid}", f"escalate_{tid}",
                   when=lambda r, c, k=key: not r[k]
                   and not within_budget(c) and can_escalate(c))
            g.edge(f"{src}_{tid}", f"fail_{tid}",
                   when=lambda r, c, k=key: not r[k]
                   and not within_budget(c) and not can_escalate(c))
        g.edge(f"escalate_{tid}", f"implement_{tid}")
        # Conflict-repair fallthrough: only when the repair-mode publish ran
        # (no alloc in results yet) and could not merge the old branch.
        g.edge(f"publish_{tid}", f"alloc_{tid}",
               when=lambda r, c, i=tid: not r.get("published")
               and f"alloc_{i}" not in c.get("results", {}))
        first = "publish" if prior_status == "conflict" else "alloc"
        if t["deps"]:
            # Wait for the dep's PR to MERGE into the base branch, not just to
            # open — otherwise a dependent branches from a base that lacks the
            # code it depends on.
            g.edge(f"pr_merge_{t['deps'][-1]}", f"{first}_{tid}")
        else:
            heads.append(f"{first}_{tid}")

    for tid in _topo(tasks):
        if (prior.get(tid) or {}).get("status") == "merged":
            make_skip(tasks[tid])
        else:
            make_chain(tasks[tid])

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
    return g


PLAN_SCHEMA_HINT = """\
{"project": {"repo": "<abs path>", "title": "<short>",
 "tasks": [{"id": "<kebab-id>", "title": "...", "prompt": "<detailed spec>",
            "model": "gpt-oss-120b" | "DeepSeek-V4-Flash" | "GLM-5.3" | "Kimi-K3",
            "reviewer": "kimi" | "glm",
            "verify_cmd": "<shell cmd run in the worktree, empty ok>",
            "files_hint": ["path/..."], "deps": ["<id>", ...]}]}}"""


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
    """Assistant message texts from a harness transcript, oldest first."""
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


async def plan_tasks(goal, repo, out_path=None):
    prompt = (
        "You are the ORCHESTRATOR of a multi-model coding fleet. Your output "
        "is the execution graph itself: the runner below you executes exactly "
        "what you emit — you decide the task breakdown, the fanout "
        "(which tasks run in parallel), the dependencies (which must wait), "
        "the model routing (who implements), the reviewer, and the verify "
        "gate for every task. Design it well; there is no later triage.\n\n"
        f"GOAL: {goal}\nTARGET REPO: {repo}\n\n"
        "ROUTING TIERS (enforced — a task file that violates these is rejected):\n"
        "- gpt-oss-120b: very basic, mechanical tasks (rename, small HTML/CSS, "
        "append a function, wiring, config).\n"
        "- DeepSeek-V4-Flash: medium tasks (a self-contained feature, a new "
        "endpoint, moderate refactor of one file).\n"
        "- GLM-5.3 or Kimi-K3: hard tasks that need deep understanding, "
        "multi-file reasoning, delicate architecture, or subtle debugging.\n"
        "- reviewer is kimi or glm. Cross-review rule: work by Kimi-K3 is "
        "reviewed by glm; work by GLM-5.3 is reviewed by kimi; gpt-oss/"
        "DeepSeek work may be reviewed by either. Split reviews between "
        "kimi and glm so neither idles nor saturates.\n\n"
        "GRAPH DESIGN (maximize safe parallelism):\n"
        "- 2-8 tasks. Fan out: every task that does NOT consume another "
        "task's output gets no deps and starts immediately at t=0.\n"
        "- Add a dep ONLY when a task truly reads code another task writes "
        "(e.g. uses a new API). Never chain tasks for stylistic order — "
        "chains serialize and waste the fleet.\n"
        "- Keep files_hint disjoint across independent tasks so parallel "
        "implementers never edit the same file (merge conflicts fail tasks).\n"
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
    res = await KimiDriver("planner").run(prompt, Path(repo), task_id="plan")
    raw = _plan_json_from_run(res)
    if raw is None:
        raise RuntimeError(f"planner produced no usable JSON; transcript: {res.transcript_path}")
    json.loads(raw)  # validate
    Path(config.TASKS_DIR).mkdir(parents=True, exist_ok=True)
    out = Path(out_path or Path(config.TASKS_DIR) / (re.sub(r"[^a-z0-9]+", "-", goal.lower())[:40].strip("-") + ".json"))
    out.write_text(raw + "\n", encoding="utf-8")
    return out
