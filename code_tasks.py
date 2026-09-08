"""Multi-harness code workload: JSON task files -> task graph -> worktrees.

Per task: alloc worktree -> implement (opencode: gpt-oss-120b / DeepSeek-V4-Flash)
-> deterministic verify gate (verify_cmd) -> cross-family review (Kimi-K3 via
kimi CLI, or GLM-5.3 via opencode) -> bounded fix loop -> publish commit ->
merge to main (serialized) -> cleanup. Reviews are mandatory; a reviewer never
shares a model family with the implementer it reviews.
"""
import asyncio
import json
import logging
import re
from pathlib import Path

import config
import events
import gitstore
from drivers import KimiDriver, OpencodeDriver
from graph import Graph

log = logging.getLogger("code-tasks")

_merge_lock = asyncio.Lock()


def load_taskfile(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    repo = Path(data["project"]["repo"]).resolve()
    tasks = {}
    for t in data["project"]["tasks"]:
        tid = t["id"]
        if tid in tasks:
            raise ValueError(f"duplicate task id: {tid}")
        model = t.get("model", "")
        if model not in config.IMPLEMENTER_MODELS:
            raise ValueError(
                f"task {tid}: model {model!r} must be an implementer "
                f"({sorted(config.IMPLEMENTER_MODELS)})"
            )
        reviewer = t.get("reviewer", "")
        if reviewer not in ("kimi", "glm"):
            raise ValueError(f"task {tid}: reviewer must be 'kimi' or 'glm', got {reviewer!r}")
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
    return {"repo": repo, "tasks": tasks, "title": data.get("project", {}).get("title", "")}


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
        rev_fam = "kimi" if t["reviewer"] == "kimi" else "glm"
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
    for m in reversed(list(re.finditer(r"\{[^{}]*\}", text))):
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            continue
        if "pass" in obj:
            return {"pass": bool(obj["pass"]),
                    "issues": [str(i) for i in obj.get("issues", [])]}
    return {"pass": False, "issues": ["reviewer returned no parseable verdict"]}


def build_code_graph(store, taskset, taskfile=""):
    repo = taskset["repo"]
    tasks = taskset["tasks"]
    g = Graph("code-tasks", max_steps=config.MAX_GRAPH_STEPS)

    def make_chain(t):
        tid = t["id"]
        # Branch from main even with deps: merges are serialized and the
        # publish_<dep> -> alloc_<tid> edge orders us after the dep's merge, so
        # main already contains every dep (dep branches are deleted at
        # cleanup, before any dependent allocs).
        base = "main"

        async def alloc(ctx):
            wt = await gitstore.alloc(repo, tid, base)
            store.upsert_code_task(taskfile, tid, t["title"], t["model"], t["reviewer"],
                                   "running", branch=f"task/{tid}", worktree=str(wt))
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
            res = await OpencodeDriver(t["model"], "implementer").run(
                _impl_prompt(t, feedback), Path(results[f"alloc_{tid}"]["worktree"]),
                task_id=f"{tid}-x{attempt}")
            store.save_harness_run(tid, "opencode", t["model"], "implementer",
                                   attempt, res.exit_code, res.transcript_path, res.seconds)
            return {"session_id": res.session_id}

        async def gate(ctx):
            cmd = t["verify_cmd"]
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
            wt = Path(ctx["results"][f"alloc_{tid}"]["worktree"])
            diff = await gitstore.diff_full(wt, base)
            driver = (KimiDriver("reviewer") if t["reviewer"] == "kimi"
                      else OpencodeDriver("GLM-5.3", "reviewer"))
            attempt = ctx.get("runs", {}).get(f"review_{tid}", 0) + 1
            res = await driver.run(_review_prompt(t, diff), wt, task_id=f"{tid}-x{attempt}")
            verdict = _parse_verdict(res.text)
            store.save_harness_run(tid, driver.harness, driver.model, "reviewer",
                                   attempt, res.exit_code, res.transcript_path,
                                   res.seconds, verdict=json.dumps(verdict)[:500])
            events.emit("task.reviewed", passed=verdict["pass"], reviewer=t["reviewer"])
            return verdict

        async def publish(ctx):
            wt = Path(ctx["results"][f"alloc_{tid}"]["worktree"])
            head = await gitstore.publish(
                wt, f"task({tid}): {t['title']}",
                {"Harness": "opencode", "Model": t["model"],
                 "Reviewer": t["reviewer"], "Task-Id": tid})
            async with _merge_lock:
                try:
                    await gitstore.merge_to_main(repo, tid)
                    await gitstore.cleanup(repo, tid)
                except gitstore.GitError as exc:
                    store.upsert_code_task(taskfile, tid, t["title"], t["model"],
                                           t["reviewer"], "conflict", error=str(exc)[:300],
                                           finished=True)
                    events.emit("task.failed", reason=str(exc)[:200])
                    return {"merged": False, "head": head}
            store.upsert_code_task(taskfile, tid, t["title"], t["model"], t["reviewer"],
                                   "merged", finished=True)
            events.emit("task.merged", head=head)
            return {"merged": True, "head": head}

        async def fail(ctx):
            reason = ctx.get("results", {}).get(f"gate_{tid}", {})
            store.upsert_code_task(taskfile, tid, t["title"], t["model"], t["reviewer"],
                                   "failed", error="exhausted fix rounds", finished=True)
            events.emit("task.failed", reason="exhausted fix rounds")
            return {"failed": True, "gate": reason}

        chain = {"alloc": alloc, "implement": implement, "gate": gate,
                 "review": review, "publish": publish, "fail": fail}
        for suffix, fn in chain.items():
            g.node(f"{suffix}_{tid}", fn)
        g.edge(f"alloc_{tid}", f"implement_{tid}")
        g.edge(f"implement_{tid}", f"gate_{tid}")
        g.edge(f"gate_{tid}", f"review_{tid}", when=lambda r, c: r["passed"])
        g.edge(f"review_{tid}", f"publish_{tid}", when=lambda r, c: r["pass"])
        for src in ("gate", "review"):
            key = "passed" if src == "gate" else "pass"
            g.edge(f"{src}_{tid}", f"implement_{tid}",
                   when=lambda r, c, k=key, i=tid: not r[k]
                   and c.get("runs", {}).get(f"implement_{i}", 0) <= config.MAX_FIX_ROUNDS)
            g.edge(f"{src}_{tid}", f"fail_{tid}",
                   when=lambda r, c, k=key, i=tid: not r[k]
                   and c.get("runs", {}).get(f"implement_{i}", 0) > config.MAX_FIX_ROUNDS)
        if t["deps"]:
            g.edge(f"publish_{t['deps'][-1]}", f"alloc_{tid}")
        else:
            g.start(f"alloc_{tid}")

    for tid in _topo(tasks):
        make_chain(tasks[tid])
    return g


PLAN_SCHEMA_HINT = """\
{"project": {"repo": "<abs path>", "title": "<short>",
 "tasks": [{"id": "<kebab-id>", "title": "...", "prompt": "<detailed spec>",
            "model": "gpt-oss-120b" | "DeepSeek-V4-Flash",
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
        "You are planning a small multi-task coding project for a fleet of AI "
        "implementers. Break this GOAL into 2-6 ordered tasks.\n\n"
        f"GOAL: {goal}\nTARGET REPO: {repo}\n\n"
        "Rules: implementer models are ONLY gpt-oss-120b and DeepSeek-V4-Flash; "
        "assign each a reviewer, alternating kimi/glm; tasks must be small "
        "(<30 min for one agent), disjoint where possible, ordered via deps "
        "when one needs another's output; give each a meaningful verify_cmd. "
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
