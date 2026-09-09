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
from drivers import DriverError, KimiDriver, OpencodeDriver
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
            f"deps={t['deps'] or '[]'} base=main verify={t['verify_cmd'] or '(none)'}"
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
            idx = _tier_index(r.get("model") or t["model"])
            if idx is not None and idx + 1 < len(config.ESCALATION_PATH):
                return config.ESCALATION_PATH[idx + 1]
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

        g.node(f"publish_{tid}", publish)
        if t["deps"]:
            g.edge(f"publish_{t['deps'][-1]}", f"publish_{tid}")
        else:
            g.start(f"publish_{tid}")

    def make_chain(t):
        tid = t["id"]
        # Branch from main even with deps: merges are serialized and the
        # publish_<dep> -> alloc_<tid> edge orders us after the dep's merge, so
        # main already contains every dep (dep branches are deleted at
        # cleanup, before any dependent allocs).
        base = "main"
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
            idx = _tier_index(cur_model(ctx))
            return idx is not None and idx + 1 < len(config.ESCALATION_PATH)

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
                return {"passed": False, "output": f"gate timed out after {config.GATE_TIMEOUT}s"}
            output = out.decode(errors="replace")[-2000:]
            events.emit("task.gate", passed=proc.returncode == 0)
            return {"passed": proc.returncode == 0, "output": output}

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
            nxt = config.ESCALATION_PATH[_tier_index(src) + 1]
            rev = reviewer_for(t, nxt)
            store.upsert_code_task(taskfile, tid, t["title"], nxt, rev, "running")
            events.emit("task.escalated", task=tid, from_model=src, to_model=nxt,
                        n=esc_n(ctx) + 1)
            return {"from_model": src, "to_model": nxt, "n": esc_n(ctx) + 1}

        async def publish(ctx):
            results = ctx.get("results", {})
            alloc_res = results.get(f"alloc_{tid}")
            if alloc_res is None:
                # conflict repair: the reviewed, gate-passing commit survived
                # on task/<tid> — merge it directly instead of re-running
                # implement+review. Falls through to a full re-execution (via
                # the publish -> alloc edge below) only when repair fails.
                if not await gitstore.branch_ahead(repo, tid, base):
                    return {"merged": False, "repair": "no-branch"}
                async with _merge_lock:
                    try:
                        await gitstore.merge_to_main(repo, tid)
                        await gitstore.cleanup(repo, tid)
                    except gitstore.GitError as exc:
                        store.upsert_code_task(taskfile, tid, t["title"], model0,
                                               reviewer_for(t, model0), "conflict",
                                               error=str(exc)[:300], finished=True)
                        events.emit("task.conflict", task=tid, reason=str(exc)[:200])
                        return {"merged": False, "repair": str(exc)[:200]}
                store.upsert_code_task(taskfile, tid, t["title"], model0,
                                       reviewer_for(t, model0), "merged", finished=True)
                events.emit("task.merged", task=tid, repaired=True)
                await _pr_hook(tid, t)
                return {"merged": True, "repaired": True}
            wt = Path(alloc_res["worktree"])
            impl = results.get(f"implement_{tid}", {})
            head = await gitstore.publish(
                wt, f"task({tid}): {t['title']}",
                {"Harness": impl.get("harness", "?"), "Model": cur_model(ctx),
                 "Reviewer": reviewer_for(t, cur_model(ctx)), "Task-Id": tid})
            async with _merge_lock:
                try:
                    await gitstore.merge_to_main(repo, tid)
                    await gitstore.cleanup(repo, tid)
                except gitstore.GitError as exc:
                    store.upsert_code_task(taskfile, tid, t["title"], cur_model(ctx),
                                           reviewer_for(t, cur_model(ctx)), "conflict",
                                           error=str(exc)[:300], finished=True)
                    events.emit("task.conflict", task=tid, reason=str(exc)[:200])
                    return {"merged": False, "head": head}
            store.upsert_code_task(taskfile, tid, t["title"], cur_model(ctx),
                                   reviewer_for(t, cur_model(ctx)), "merged", finished=True)
            events.emit("task.merged", task=tid, head=head)
            await _pr_hook(tid, t)
            return {"merged": True, "head": head}

        async def fail(ctx):
            reason = ctx.get("results", {}).get(f"gate_{tid}", {})
            last = cur_model(ctx)
            store.upsert_code_task(taskfile, tid, t["title"], last,
                                   reviewer_for(t, last), "failed",
                                   error=f"exhausted escalation up to {last}",
                                   finished=True)
            events.emit("task.failed", task=tid,
                        reason=f"exhausted escalation up to {last}")
            return {"failed": True, "gate": reason}

        chain = {"alloc": alloc, "implement": implement, "gate": gate,
                 "review": review, "escalate": escalate, "publish": publish,
                 "fail": fail}
        for suffix, fn in chain.items():
            g.node(f"{suffix}_{tid}", fn)
        g.edge(f"alloc_{tid}", f"implement_{tid}")
        g.edge(f"implement_{tid}", f"gate_{tid}")
        g.edge(f"gate_{tid}", f"review_{tid}", when=lambda r, c: r["passed"])
        g.edge(f"review_{tid}", f"publish_{tid}", when=lambda r, c: r["pass"])
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
               when=lambda r, c, i=tid: not r.get("merged")
               and f"alloc_{i}" not in c.get("results", {}))
        first = "publish" if prior_status == "conflict" else "alloc"
        if t["deps"]:
            g.edge(f"publish_{t['deps'][-1]}", f"{first}_{tid}")
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


def _extract_plan_json(text):
    """First balanced JSON span that parses as a plan dict; None if absent.

    Planners echo the JSON more than once (fenced copy + bare copy) or append
    prose, so a greedy regex over-spans and nested fragments under-match —
    accept only a balanced span carrying the project/tasks schema.
    """
    for m in re.finditer(r"\{", text):
        span = _balanced_span(text, m.start())
        if span is None:
            continue
        try:
            obj = json.loads(span)
        except ValueError:
            continue
        if isinstance(obj.get("project"), dict) and "tasks" in obj["project"]:
            return span
    return None


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
        "- verify_cmd: a deterministic shell check run in the task's "
        "worktree (tests, build, node --check, grep). Leave empty only for "
        "purely cosmetic tasks. A failing gate bounces the task back to the "
        "implementer (max 3 fix rounds), so make it honest.\n\n"
        "Reply with STRICT JSON only, matching exactly this shape:\n"
        + PLAN_SCHEMA_HINT
    )
    res = await KimiDriver("planner").run(prompt, Path(repo), task_id="plan")
    raw = _extract_plan_json(res.text)
    if raw is None:
        raise RuntimeError(f"planner produced no usable JSON; transcript: {res.transcript_path}")
    json.loads(raw)  # validate
    Path(config.TASKS_DIR).mkdir(parents=True, exist_ok=True)
    out = Path(out_path or Path(config.TASKS_DIR) / (re.sub(r"[^a-z0-9]+", "-", goal.lower())[:40].strip("-") + ".json"))
    out.write_text(raw + "\n", encoding="utf-8")
    return out
