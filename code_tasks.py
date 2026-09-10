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
from pathlib import Path

import config
import events
import gitstore
from drivers import DriverError, KimiDriver, OpencodeDriver, transcript_tokens
from graph import Graph, GraphError

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
    return {"repo": repo, "tasks": tasks, "title": data.get("project", {}).get("title", ""),
            "policy": pol}


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


def _harness_of(model):
    """The local harness that runs this model. Mirrors _driver()'s routing."""
    return "kimi" if model == "Kimi-K3" else "opencode"


def _reviewer_pressure(model, usage):
    """How contended this reviewer is, 0.0 (idle) to 1.0+ (at a ceiling).

    Whichever ceiling binds FIRST wins: a model comfortably under its own cap
    is not actually available if the harness it shares with two other models is
    full. Scoring on the model alone sent every review to GLM and DeepSeek while
    the single opencode pool they share sat at 5/5 with seven reviewers queued
    behind it and the kimi harness idle at 1/3.
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
    for m in ("Kimi-K3", "GLM-5.3", "DeepSeek-V4-Flash"):
        if config.MODEL_FAMILY.get(m) == impl_fam:
            continue
        try:
            _driver(m, "pr_reviewer", pol)
        except ValueError:
            continue
        out.append(m)
    return out


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
            g.start(f"publish_{tid}")

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
            feedback = _rework_feedback(tid, results)
            attempt = ctx.get("runs", {}).get(f"implement_{tid}", 0) + 1
            model = cur_model(ctx)
            driver = _driver(model, "implementer", pol)
            try:
                res = await driver.run(
                    _impl_prompt(t, feedback), await worktree(ctx),
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
            wt = str(await worktree(ctx))
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
            passed = proc.returncode == 0
            # Attribution and a reason, not just a boolean: a bare
            # {"passed": false} in the log cannot be tied to a task or acted
            # on, and this is the per-node progress signal the dashboard reads.
            events.emit("task.gate", task=tid, attempt=attempt, passed=passed,
                        log=log_path, cmd=cmd[:120],
                        tail=None if passed else output.strip()[-400:])
            return {"passed": passed, "output": output, "log_path": log_path}

        async def review(ctx):
            if not review_on:
                events.emit("task.reviewed", task=tid, passed=True, reviewer="none",
                            skipped=True)
                return {"pass": True, "issues": [], "skipped": True}
            wt = await worktree(ctx)
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
            events.emit("task.reviewed", task=tid, passed=verdict["pass"],
                        reviewer=rev_tok,
                        n_issues=len(verdict.get("issues") or []))
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
                    return {"published": False, "reason": "no worktree"}
                events.emit("task.resumed", task=tid, worktree=str(wt),
                            prior_status=prior_status)
            impl = results.get(f"implement_{tid}", {})
            model, rev = cur_model(ctx), reviewer_for(t, cur_model(ctx))
            head = await gitstore.publish(
                wt, f"task({tid}): {t['title']}",
                {"Harness": impl.get("harness", "?"), "Model": model,
                 "Reviewer": rev, "Task-Id": tid})
            if head is None:
                # No new commit. On a RESUME the branch is already pushed and
                # its PR already open, so re-attach rather than re-implementing
                # and throwing that diff away. But if a PR review round has
                # already run in THIS graph, "no changes" means the rework
                # produced nothing — re-reviewing an identical diff would just
                # burn reviewers to reach the same verdict, so let it fail.
                reworked = f"pr_review_{tid}" in ctx.get("results", {})
                number, url, note = (None, None, None) if reworked else \
                    await gitstore.open_pr(repo, tid,
                                           f"task({tid}): {t['title']}", "", base)
                if number is not None:
                    store.upsert_code_task(taskfile, tid, t["title"], model, rev,
                                           "in_review", branch=f"task/{tid}")
                    events.emit("task.pr_reattached", task=tid, pr=number,
                                url=url, note=note)
                    return {"published": True, "pr": number, "url": url,
                            "head": None, "reattached": True}
                store.upsert_code_task(
                    taskfile, tid, t["title"], model, rev, "failed",
                    error=("rework after PR rejection produced no changes"
                           if reworked else "implementer produced no changes"),
                    finished=True)
                events.emit("task.failed", task=tid,
                            reason=("rework produced no changes" if reworked
                                    else "no changes to publish"))
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
            pool = _eligible_pr_reviewers(impl_fam, pol)
            # Pick the LEAST CONTENDED eligible models, not a fixed order.
            # The fixed order sent every review to Kimi and GLM while DeepSeek
            # sat idle, so tasks waited 30 minutes for a slot another model
            # could have served at once (260 cap_wait events in one hour).
            #
            # Contention is whichever ceiling binds FIRST — the model's own cap
            # or its harness's. Sorting on the model alone sent reviews to GLM
            # and DeepSeek while the single opencode pool they share sat at 5/5
            # with seven reviewers queued behind it and the kimi harness idle at
            # 1/3. A model under its own cap is not available if its harness
            # is full.
            try:
                usage = store.lease_usage()
            except Exception:
                usage = {}

            pool.sort(key=lambda m: (_reviewer_pressure(m, usage),
                                     usage.get(m, 0)))
            chosen = pool[:max(1, config.PR_REVIEWERS)]

            async def one(model):
                # Never let one reviewer take the whole graph down with it: a
                # crashed or unbuildable reviewer is a rejection with a reason,
                # not an exception that orphans an open PR.
                # "pr_reviewer", not "reviewer": these are the gate on an open
                # PR and they are the scarcest thing in the fleet (PR_REVIEWERS
                # cross-family models per round). The dashboard separates them
                # from the pre-PR gate reviewer so a reviewer queue is legible.
                try:
                    drv = _driver(model, "pr_reviewer", pol)
                    res = await drv.run(
                        _pr_review_prompt(t, diff, len(chosen), round_n,
                                          prior_r.get("issues") or []),
                        await worktree(ctx),
                        task_id=f"{tid}-pr{round_n}")
                except (DriverError, ValueError) as exc:
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
                    "approvals": approvals, "reviewers": chosen,
                    "pr": number, "round": round_n}

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

        # on_drain: once a branch is pushed and a PR is open, the model time is
        # already spent. If a SIBLING task fails and drains the graph, these two
        # edges still fire so the PR gets reviewed and merged instead of being
        # orphaned on GitHub. The rework edge below is deliberately not marked —
        # draining must not start a fresh implementer.
        g.edge(f"publish_{tid}", f"pr_review_{tid}",
               when=lambda r, c: bool(r.get("published")), on_drain=True)
        g.edge(f"pr_review_{tid}", f"pr_merge_{tid}",
               when=lambda r, c: bool(r.get("approved")), on_drain=True)
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
        # in_review resumes at publish, which finds the already-open PR and
        # hands it straight to pr_review — restarting at alloc would discard a
        # pushed branch and an open pull request.
        first = "publish" if prior_status in ("conflict", "in_review") else "alloc"
        if t["deps"]:
            # Wait for the dep's PR to MERGE into the base branch, not just to
            # open — otherwise a dependent branches from a base that lacks the
            # code it depends on.
            g.edge(f"pr_merge_{t['deps'][-1]}", f"{first}_{tid}")
        else:
            g.start(f"{first}_{tid}")

    for tid in _topo(tasks):
        if (prior.get(tid) or {}).get("status") == "merged":
            make_skip(tasks[tid])
        else:
            make_chain(tasks[tid])
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
