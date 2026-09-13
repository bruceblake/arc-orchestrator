"""Benchmark runner: which model x harness x option set solves coding tasks.

Suites come from bench_data.py. Every (task, model-spec, harness, sample) job
writes its candidate files into logs/bench/<run_id>/<job-slug>/, runs the
task's pre-written tests in a subprocess, and records one row per sample in
the bench_results SQLite table.

Published methodology (HumanEval/Codex, aider, SWE-bench, Terminal-Bench)
says the *harness is the unit of measurement*: scaffold choice swings scores
by 10-25 points for the same model. So every row and every report keeps
(model, effort, harness, temperature, n) explicit, and reports show both
pass@1 (mean over samples), pass@k (unbiased estimator), any-of-n coverage,
and pass^n reliability.
"""
import asyncio
import json
import logging
import math
import os
import re
import shutil
import sys
import time
from pathlib import Path

import config

log = logging.getLogger("bench")

OUTPUT_ROOT = Path(config.ROOT) / "logs" / "bench"
# "kimi" is deliberately NOT a default: Kimi-K3 was retired 2026-09-12 and no
# live family resolves to that harness any more. SOLVERS keeps it so an
# explicit historical invocation still runs, but a default bench run must
# not spawn a retired CLI.
DEFAULT_HARNESSES = ["direct", "fanout", "fixloop", "review", "opencode"]
FUNCTION_FILE = "solution.py"


# ---------------------------------------------------------------------------
# code extraction from model text
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:python|py|javascript|js|html|json)?\s*\n(.*?)```", re.S)


def _blocks(text):
    return [m.group(1) for m in _FENCE.finditer(text)]


def extract_code(text, entry):
    """Single-file extraction: prefer the fenced block defining `entry`."""
    candidates = _blocks(text)
    if entry:
        for b in reversed(candidates):
            if f"def {entry}(" in b or f"class {entry}" in b:
                return b.strip() + "\n"
    if candidates:
        for b in reversed(candidates):
            if "def " in b or "class " in b:
                return b.strip() + "\n"
        return candidates[-1].strip() + "\n"
    if "def " in text or "class " in text:
        return text.strip() + "\n"
    return None


def extract_files(text, task):
    """Candidate files for a task. Package tasks expect `file: path` markers."""
    if task["kind"] == "function":
        code = extract_code(text, task["entry"])
        return {FUNCTION_FILE: code} if code else {}
    files = {}
    pos = 0
    for m in _FENCE.finditer(text):
        prefix = text[pos:m.start()].rsplit("\n", 3)[-3:]
        marker = re.search(r"file:\s*([A-Za-z0-9_./-]+)\s*$", prefix.strip())
        if marker:
            path = marker.group(1).lstrip("./")
            if path and not path.startswith("test_"):
                files[path] = m.group(1).strip() + "\n"
        pos = m.end()
    return files


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------

def solve_prompt(task):
    if task["kind"] == "function":
        return (
            "Implement this Python function. Reply with ONE ```python code "
            "block containing the complete implementation (imports allowed). "
            "No prose.\n\n" + task["prompt"]
        )
    return (
        "Implement the following project spec in the current working "
        "directory. The tests and fixtures listed already exist. Output each "
        "file you create as a fenced code block immediately preceded by a "
        "line of the form `file: relative/path.py`. No other prose.\n\n"
        + task["prompt"]
    )


def fix_prompt(task, tail):
    return (
        "Your previous attempt failed the tests. Failing test output "
        f"(tail):\n```\n{tail}\n```\nFix the problem and output the "
        + ("corrected function in ONE ```python block."
           if task["kind"] == "function"
           else "corrected files, each as `file: path` + fenced block.")
    )


def review_prompt(task, files_map, test_tail):
    body = "\n\n".join(f"### {p}\n```\n{c}\n```" for p, c in files_map.items())
    return (
        "You are reviewing another AI agent's code submission for this "
        f"spec:\n\n{task['prompt']}\n\nIts code:\n\n{body}\n\n"
        + (f"The tests failed with:\n```\n{test_tail}\n```\n\n" if test_tail
           else "The automated tests PASSED.\n\n")
        + "Reply with STRICT JSON only: {\"pass\": true} if the code fully "
        "and correctly implements the spec, else {\"pass\": false, \"issues\": "
        "[\"specific fixes needed\"]}.")


def parse_verdict(text):
    for m in re.finditer(r"\{[^{}]*\"pass\"[^{}]*\}", text, re.S):
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            continue
        if isinstance(obj, dict) and "pass" in obj:
            return {"pass": bool(obj["pass"]),
                    "issues": [str(i) for i in obj.get("issues", [])]}
    return {"pass": False, "issues": ["unparseable verdict"]}


# ---------------------------------------------------------------------------
# workdir + test execution
# ---------------------------------------------------------------------------

def _test_files(task):
    return [f for f in task["files"] if f.startswith("test_")]


def write_workdir(base, task, candidate):
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True, exist_ok=True)
    for rel, content in task["files"].items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    for rel, content in candidate.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return base


async def run_tests(workdir, task, timeout=None):
    """(passed, output tail). Each test file must exit 0."""
    outs = []
    env = dict(os.environ, PYTHONPATH=str(workdir))
    for tf in _test_files(task):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, tf, cwd=str(workdir), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(
                proc.communicate(), timeout or task.get("timeout", 20))
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False, f"{tf}: timed out after {timeout or task.get('timeout')}s"
        outs.append(out.decode(errors="replace"))
        if proc.returncode != 0:
            return False, f"--- {tf} (exit {proc.returncode}) ---\n" + "\n".join(outs)[-1500:]
    return True, "\n".join(outs)[-1500:]


# ---------------------------------------------------------------------------
# solvers — one Attempt {passed, rounds, seconds, tokens, ptok, ctok, error}
# ---------------------------------------------------------------------------

def _meta_totals(metas):
    return (
        sum(m.get("tokens", 0) for m in metas),
        sum(m.get("prompt_tokens", 0) for m in metas),
        sum(m.get("completion_tokens", 0) for m in metas),
    )


def _retry_family(family):
    order = [f for f in config.FAMILY_ORDER if f != family]
    return order[0] if order else family


async def solve_direct(pool, task, spec, workdir, *, temperature=None, **_kw):
    """One-shot: single chat call -> extract files -> run tests."""
    t0 = time.monotonic()
    meta = {}
    try:
        text = await pool.chat(spec[0], [{"role": "user", "content": solve_prompt(task)}],
                               effort=spec[1], purpose="bench", temperature=temperature,
                               meta=meta)
    except Exception as exc:
        return {"passed": False, "rounds": 1, "seconds": round(time.monotonic() - t0, 1),
                "tokens": 0, "ptok": 0, "ctok": 0, "error": str(exc)[:300]}
    files = extract_files(text, task)
    if not files:
        return {"passed": False, "rounds": 1, "seconds": round(time.monotonic() - t0, 1),
                "tokens": meta.get("tokens", 0), "ptok": meta.get("prompt_tokens", 0),
                "ctok": meta.get("completion_tokens", 0), "error": "no code extracted"}
    write_workdir(workdir, task, files)
    passed, tail = await run_tests(workdir, task)
    return {"passed": passed, "rounds": 1, "seconds": round(time.monotonic() - t0, 1),
            "tokens": meta.get("tokens", 0), "ptok": meta.get("prompt_tokens", 0),
            "ctok": meta.get("completion_tokens", 0),
            "error": None if passed else f"tests failed: {tail[-200:]}"}


async def solve_fixloop(pool, task, spec, workdir, *, temperature=None, max_rounds=3, **_kw):
    """aider-style: implement -> run tests -> feed failures back, <= max_rounds."""
    t0 = time.monotonic()
    metas = []
    messages = [{"role": "user", "content": solve_prompt(task)}]
    tail = ""
    for rnd in range(1, max_rounds + 1):
        meta = {}
        text = await pool.chat(spec[0], messages, effort=spec[1],
                               purpose="bench", temperature=temperature, meta=meta)
        metas.append(meta)
        files = extract_files(text, task)
        if not files:
            tail = "no code block extracted from reply"
        else:
            write_workdir(workdir, task, files)
            passed, tail = await run_tests(workdir, task)
            if passed:
                tok, ptok, ctok = _meta_totals(metas)
                return {"passed": True, "rounds": rnd,
                        "seconds": round(time.monotonic() - t0, 1), "tokens": tok,
                        "ptok": ptok, "ctok": ctok, "error": None}
        messages += [{"role": "assistant", "content": text},
                     {"role": "user", "content": fix_prompt(task, tail[-1200:])}]
    tok, ptok, ctok = _meta_totals(metas)
    return {"passed": False, "rounds": max_rounds,
            "seconds": round(time.monotonic() - t0, 1), "tokens": tok,
            "ptok": ptok, "ctok": ctok, "error": f"tests failed: {tail[-200:]}"}


async def solve_review(pool, task, spec, workdir, *, temperature=None, max_rounds=3,
                       reviewer=None, **_kw):
    """implementer + cross-family reviewer loop; tests remain ground truth."""
    t0 = time.monotonic()
    metas = []
    rev_family = reviewer or _retry_family(spec[0])
    prompt = solve_prompt(task)
    files, tail, verdict = {}, "", None
    for rnd in range(1, max_rounds + 1):
        meta = {}
        text = await pool.chat(spec[0], [{"role": "user", "content": prompt}],
                               effort=spec[1], purpose="bench",
                               temperature=temperature, meta=meta)
        metas.append(meta)
        files = extract_files(text, task)
        if files:
            write_workdir(workdir, task, files)
            tests_passed, tail = await run_tests(workdir, task)
        else:
            tests_passed, tail = False, "no code block extracted from reply"
        meta_r = {}
        vtext = await pool.chat(rev_family,
                                [{"role": "user", "content": review_prompt(task, files, tail)}],
                                effort="default", purpose="bench", meta=meta_r)
        metas.append(meta_r)
        verdict = parse_verdict(vtext)
        if tests_passed and verdict["pass"]:
            tok, ptok, ctok = _meta_totals(metas)
            return {"passed": True, "rounds": rnd,
                    "seconds": round(time.monotonic() - t0, 1), "tokens": tok,
                    "ptok": ptok, "ctok": ctok, "error": None}
        prompt = fix_prompt(task, tail[-900:]) + "\nReviewer issues:\n- " + "\n- ".join(
            verdict["issues"][:5])
    tok, ptok, ctok = _meta_totals(metas)
    return {"passed": False, "rounds": max_rounds,
            "seconds": round(time.monotonic() - t0, 1), "tokens": tok,
            "ptok": ptok, "ctok": ctok,
            "error": f"tests {'passed' if tests_passed else 'failed'}; review {verdict}"[:200]}


async def solve_fanout(pool, task, spec, workdir, *, temperature=None,
                       fanout_n=4, **kw):
    """Best-of-n: run fanout_n direct samples in parallel, keep the first
    whose tests pass (test-based selection, disclosed). Total token cost of
    all samples is reported."""
    t0 = time.monotonic()
    subs = [workdir.parent / (workdir.name + f"_f{i}") for i in range(fanout_n)]
    attempts = await asyncio.gather(*(
        solve_direct(pool, task, spec, subs[i], temperature=temperature)
        for i in range(fanout_n)))
    tok = sum(a["tokens"] for a in attempts)
    ptok = sum(a["ptok"] for a in attempts)
    ctok = sum(a["ctok"] for a in attempts)
    sec = round(time.monotonic() - t0, 1)
    for i, a in enumerate(attempts):
        if a["passed"]:
            write_workdir(workdir, task, {
                p: c for p, c in _read_candidate(subs[i], task).items()})
            return {"passed": True, "rounds": fanout_n, "seconds": sec,
                    "tokens": tok, "ptok": ptok, "ctok": ctok,
                    "error": None}
    tail = attempts[0]["error"] or "tests failed"
    return {"passed": False, "rounds": fanout_n, "seconds": sec,
            "tokens": tok, "ptok": ptok, "ctok": ctok,
            "error": f"0/{fanout_n} samples passed; first: {tail[-160:]}"}


def _read_candidate(workdir, task):
    tests = set(_test_files(task)) | set(task["files"])
    out = {}
    for p in sorted(workdir.rglob("*")):
        if p.is_file() and p.name not in ("PROMPT.md",):
            rel = str(p.relative_to(workdir))
            if rel not in tests and "__pycache__" not in rel:
                try:
                    out[rel] = p.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    pass
    return out


_CLI_PROMPT = (
    "Read PROMPT.md in the current directory and implement the spec. "
    "Write all files it requires into the current directory "
    "{hint}. The provided test_*.py tests are the ground truth: run them "
    "with `python3 <testfile>` and iterate until they pass. Work "
    "autonomously and do not ask questions.")


async def solve_cli(pool, task, spec, workdir, *, harness, max_rounds=3, **kw):
    """Drive a real CLI agent (opencode, or the historical kimi) in the
    task workdir."""
    t0 = time.monotonic()
    import drivers
    if pool.dry_run:
        write_workdir(workdir, task, {"solution.py": "# dry-run\n"})
        return {"passed": False, "rounds": 1, "seconds": 0.1,
                "tokens": 0, "ptok": 0, "ctok": 0,
                "error": "dry-run: CLI solver simulated"}
    write_workdir(workdir, task, {})
    (workdir / "PROMPT.md").write_text(task["prompt"], encoding="utf-8")
    if task["kind"] == "function":
        hint = f"(module `{FUNCTION_FILE}` defining `{task['entry']}`)"
    else:
        hint = "(paths as given in the spec)"
    prompt = _CLI_PROMPT.format(hint=hint)
    if harness == "kimi":
        if spec[0] != "kimi":
            raise ValueError("kimi harness only applies to the kimi family")
        driver = drivers.KimiDriver("implementer", bench=True)
    else:
        driver = drivers.OpencodeDriver(
            pool.resolve_model(spec[0], spec[1]), "implementer", bench=True)
    try:
        res = await driver.run(prompt, str(workdir), task_id="bench")
    except Exception as exc:
        return {"passed": False, "rounds": 1,
                "seconds": round(time.monotonic() - t0, 1),
                "tokens": 0, "ptok": 0, "ctok": 0, "error": str(exc)[:300]}
    passed, tail = await run_tests(workdir, task)
    return {"passed": passed, "rounds": max_rounds, "seconds": res.seconds,
            "tokens": res.tokens, "ptok": res.prompt_tokens,
            "ctok": res.completion_tokens,
            "error": None if passed else f"tests failed: {tail[-200:]}"}


# ---------------------------------------------------------------------------
# job expansion + runner
# ---------------------------------------------------------------------------

SOLVERS = {
    "direct": "solve_direct",
    "fanout": "solve_fanout",
    "fixloop": "solve_fixloop",
    "review": "solve_review",
    "opencode": "solve_cli",
    "kimi": "solve_cli",
}

_MODEL_COLUMN = {"gpt-oss": "gpt-oss-120b", "glm": "GLM-5.3",
                 "kimi": "Kimi-K3", "deepseek": "DeepSeek-V4.1-Flash-thinking-max"}


def expand_jobs(tasks, specs, harnesses, *, n=1):
    """jobs = task x (family, effort) x harness x sample index.

    `direct` gets `n` samples (pass@1 = mean over samples); `fanout` is one
    attempt that internally samples fanout_n; the rest run once (cost)."""
    jobs = []
    for task in tasks:
        for spec in specs:
            for harness in harnesses:
                if harness == "kimi" and spec[0] != "kimi":
                    log.info("matrix: skip kimi harness for family %s", spec[0])
                    continue
                reps = n if harness == "direct" else 1
                for s in range(reps):
                    jobs.append({"task": task, "spec": spec,
                                 "harness": harness, "sample": s})
    return jobs


async def run_jobs(store, pool, run_id, jobs, *, temperature=None,
                   max_rounds=3, fanout_n=4):
    sem = asyncio.Semaphore(int(os.getenv("ARC_BENCH_JOBS", "24")))
    done = [0]

    async def one(job):
        task, spec, harness, i = (job["task"], job["spec"],
                                  job["harness"], job["sample"])
        slug = f"{task['task_id']}__{spec[0]}-{spec[1]}__{harness}__s{i}"
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", slug)
        workdir = OUTPUT_ROOT / str(run_id) / slug
        solver = globals()[SOLVERS[harness]]
        kwargs = {"temperature": temperature, "max_rounds": max_rounds,
                  "fanout_n": fanout_n, "harness": harness}
        async with sem:
            try:
                r = await solver(pool, task, spec, workdir, **kwargs)
            except Exception as exc:
                r = {"passed": False, "rounds": 0, "seconds": 0.0,
                     "tokens": 0, "ptok": 0, "ctok": 0,
                     "error": f"runner: {exc}"[:300]}
        store.save_bench_result(
            run_id, task["task_id"], task["suite"], task["tier"],
            _MODEL_COLUMN.get(spec[0], spec[0]), spec[1], harness,
            temperature, fanout_n if harness == "fanout" else 1, i,
            r["passed"], r["rounds"], r["seconds"], r["tokens"],
            r["ptok"], r["ctok"], r["error"], str(workdir))
        done[0] += 1
        log.info("[%d/%d] %s %s", done[0], len(jobs), slug,
                 "PASS" if r["passed"] else "fail")

    await asyncio.gather(*(one(j) for j in jobs))
    return done[0]


# ---------------------------------------------------------------------------
# scoring + report
# ---------------------------------------------------------------------------

def pass_at_k(n, c, k):
    """Unbiased pass@k (Chen et al., Codex): 1 - C(n-c, k) / C(n, k)."""
    if n < k:
        return float(c > 0)
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def _group(rows, keys):
    out = {}
    for r in rows:
        g = tuple(r[k] for k in keys)
        out.setdefault(g, []).append(r)
    return out


def _cell_stats(rows):
    ns = len(rows)
    c = sum(1 for r in rows if r["passed"])
    sec = sum(r["seconds"] or 0 for r in rows) / ns
    tok = sum(r["tokens"] or 0 for r in rows) / ns
    return ns, c, sec, tok


def report(store, run_ids):
    rows = store.bench_results(run_ids)
    if not rows:
        return "no bench results for those run ids"
    lines = []
    runs = {r["id"]: r for r in store.bench_runs_list()}
    hdr = "runs: " + ", ".join(
        f"#{i} {runs.get(i, {}).get('label', '')}".rstrip() for i in run_ids)
    lines += [hdr, f"samples: {len(rows)}", ""]

    # --- leaderboard: model x effort x harness
    lines.append("=== leaderboard (pass@1 = mean over samples; "
                 "coverage = any sample passed) ===")
    lines.append(f"{'model':<16}{'effort':<9}{'harness':<10}"
                 f"{'n':>4}{'pass@1':>8}{'any':>6}{'sec':>7}{'tok':>8}")
    cells = _group(rows, ("model", "effort", "harness"))
    table = []
    for g, rs in cells.items():
        ns, c, sec, tok = _cell_stats(rs)
        table.append((c / ns, c, ns, g, sec, tok))
    for rate, c, ns, (m, e, h), sec, tok in sorted(table, reverse=True):
        lines.append(f"{m:<16}{e:<9}{h:<10}{ns:>4}{rate * 100:>7.0f}%"
                     f"{(c > 0) * 100:>5.0f}%{sec:>7.1f}{tok:>8.0f}")
    lines.append("")

    # --- suite x model pass rates
    lines.append("=== suite x model (pass@1 %, direct harness only) ===")
    direct = [r for r in rows if r["harness"] == "direct"]
    suites = sorted({r["suite"] for r in direct})
    models = sorted({r["model"] for r in direct})
    lines.append(f"{'model':<16}" + "".join(f"{s:>10}" for s in suites))
    for m in models:
        line = f"{m:<16}"
        for s in suites:
            rs = [r for r in direct if r["model"] == m and r["suite"] == s]
            line += f"{sum(r['passed'] for r in rs) / len(rs) * 100:>9.0f}%" if rs else f"{'-':>10}"
        lines.append(line)
    lines.append("")

    # --- tier breakdown
    lines.append("=== tier x harness (pass@1 %) ===")
    tiers = sorted({r["tier"] for r in rows})
    harnesses = sorted({r["harness"] for r in rows})
    lines.append(f"{'tier':<8}" + "".join(f"{h:>10}" for h in harnesses))
    for t in tiers:
        line = f"{t:<8}"
        for h in harnesses:
            rs = [r for r in rows if r["tier"] == t and r["harness"] == h]
            line += f"{sum(r['passed'] for r in rs) / len(rs) * 100:>9.0f}%" if rs else f"{'-':>10}"
        lines.append(line)
    lines.append("")

    # --- pass@k / pass^n for cells with multiple samples
    lines.append("=== sampling (cells with n>1 direct samples): "
                 "pass@k unbiased / pass^n reliability ===")
    lines.append(f"{'model':<16}{'task-set':<10}{'n':>4}{'c':>4}"
                 + "".join(f"{'pass@' + str(k):>8}" for k in (1, 2, 4)) + f"{'pass^n':>8}")
    cells = _group([r for r in rows if r["harness"] == "direct"],
                   ("model", "effort", "suite"))
    for (m, e, s), rs in sorted(cells.items()):
        ns, c, _, _ = _cell_stats(rs)
        if ns < 2:
            continue
        line = f"{m:<16}{s:<10}{ns:>4}{c:>4}"
        for k in (1, 2, 4):
            line += f"{pass_at_k(ns, c, min(k, ns)) * 100:>7.0f}%"
        line += f"{(c / ns) ** ns * 100:>7.0f}%"
        lines.append(line)
    lines.append("")
    lines.append("methodology: harness is the unit of measurement "
                 "(SWE-bench/aider finding: same model swings 10-25 pts "
                 "across scaffolds). fixloop = aider-style test feedback "
                 "(max 3 rounds); fanout = best-of-n with test-based "
                 "selection; review = cross-family reviewer over test "
                 "results; opencode/kimi = full CLI agents with shell access.")
    return "\n".join(lines)
