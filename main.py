#!/usr/bin/env python3
import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import config


def setup_logging(verbose):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def db_path(args, dry_run):
    if args.db:
        return args.db
    root = Path(__file__).resolve().parent
    return str(root / ("dry-run.db" if dry_run else "orchestrator.db"))


def add_common(p, once=False):
    p.add_argument("--dry-run", action="store_true", help="simulate all model calls")
    p.add_argument("--questions", type=int, default=None, help="questions per round")
    p.add_argument("--db", default=None, help="sqlite database path")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    if not once:
        p.add_argument("--pipeline", type=int, default=None, help="concurrent rounds (default 2)")
        p.add_argument("--rounds", type=int, default=None, help="stop after N rounds (default: forever)")


def cmd_graph(_args):
    from pool import ArcPool
    from store import Store
    from work import Roles, build_round_graph
    from build_work import build_build_graph

    pool = ArcPool(dry_run=True)
    store = Store(":memory:")
    for g in (
        build_round_graph(pool, store, Roles(), {}),
        build_build_graph(pool, store, build_id=0, iteration=1, mode="create",
                          out_dir=Path("."), current_files={}),
    ):
        print(f"graph '{g.name}' (max_steps={g.max_steps})")
        print("start: " + ", ".join(g.starts))
        print("nodes:")
        for name, node in g.nodes.items():
            print(f"  {name}{' [gather]' if node.gather else ''}")
        print("edges:")
        for e in g.edges:
            print(f"  {e.src} -> {e.dst}{'' if e.when is None else '  (conditional)'}")
        print()


def cmd_status(args):
    from store import Store

    path = args.db or str(Path(__file__).resolve().parent / "orchestrator.db")
    st = Store(path).stats()
    avg = "n/a" if st["avg_score"] is None else f"{st['avg_score']:.2f}"
    rate = "n/a" if st["pass_rate"] is None else f"{st['pass_rate'] * 100:.0f}%"
    print(f"rounds by status : {st['rounds']}")
    print(f"items done       : {st['items_done']}")
    print(f"avg verify score : {avg}")
    print(f"pass rate        : {rate}")
    print(f"answers by family: {st['answers']}")
    print(f"seeds unused     : {st['seeds_unused']}")
    if st["recent_topics"]:
        print("recent topics    :")
        for t in st["recent_topics"]:
            print(f"  - {t}")


def cmd_run(args, once):
    from pool import ArcPool
    from scheduler import Supervisor
    from store import Store

    try:
        pool = ArcPool(dry_run=args.dry_run)
    except RuntimeError as exc:
        sys.exit(str(exc))
    store = Store(db_path(args, args.dry_run))
    kwargs = {
        "questions": args.questions,
        "max_rounds": 1 if once else getattr(args, "rounds", None),
    }
    if not once:
        kwargs["pipeline"] = args.pipeline
    sup = Supervisor(pool, store, **kwargs)
    try:
        asyncio.run(sup.run())
    except KeyboardInterrupt:
        pass


def cmd_build(args):
    import build_work
    import events
    from pool import ArcPool
    from store import Store

    try:
        pool = ArcPool(dry_run=args.dry_run)
    except RuntimeError as exc:
        sys.exit(str(exc))
    store = Store(db_path(args, args.dry_run))
    if args.dry_run and not os.getenv("ARC_BUILD_OUTPUT_DIR"):
        out_dir = Path(str(config.BUILD_OUTPUT_DIR) + "-dry-run")
    else:
        out_dir = Path(config.BUILD_OUTPUT_DIR)
    iterations = args.iterations or 1
    log = logging.getLogger("build-cmd")
    log.info("build start — iterations=%d output=%s dry_run=%s", iterations, out_dir, args.dry_run)
    stale = store.fail_stale_builds()
    if stale:
        log.warning("marked %d orphaned build(s) from a previous run as failed", stale)

    async def run():
        for it in range(1, iterations + 1):
            events.set_context(workload="minecraft-build", iteration=it)
            mode = "create" if not (out_dir / "index.html").exists() else "improve"
            current = {}
            if mode == "improve":
                for m in build_work.MODULES:
                    p = out_dir / m["file"]
                    if p.exists():
                        current[m["name"]] = p.read_text(encoding="utf-8", errors="replace")
            bid = store.start_build(it, mode)
            events.emit("iteration_start", build_id=bid, mode=mode)
            t0 = time.monotonic()
            try:
                graph = build_work.build_build_graph(
                    pool, store, build_id=bid, iteration=it, mode=mode,
                    out_dir=out_dir, current_files=current,
                )
                final = await graph.run({"iteration": it, "integration_round": 1})
                ms = final.get("results", {}).get("metrics_store", {})
                store.finish_build(bid, "ok",
                                   passed=1 if ms.get("integration_passed") else 0,
                                   integration_rounds=ms.get("integration_rounds"))
                events.emit("iteration_end", build_id=bid, status="ok",
                            seconds=round(time.monotonic() - t0, 1),
                            integration_rounds=ms.get("integration_rounds"),
                            integration_passed=ms.get("integration_passed"),
                            total_tokens=ms.get("total_tokens"))
                log.info("build iteration %d ok in %.1fs — %s", it, time.monotonic() - t0, ms)
            except asyncio.CancelledError:
                store.finish_build(bid, "interrupted", error="cancelled during shutdown")
                events.emit("iteration_end", build_id=bid, status="interrupted",
                            seconds=round(time.monotonic() - t0, 1))
                raise
            except Exception as exc:
                store.finish_build(bid, "failed", error=str(exc)[:500])
                events.emit("iteration_end", build_id=bid, status="failed",
                            seconds=round(time.monotonic() - t0, 1), error=str(exc)[:300])
                log.error("build iteration %d failed after %.1fs: %s", it, time.monotonic() - t0, exc)
                raise

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


def cmd_code(args):
    import json
    import events
    from store import Store
    from code_tasks import build_code_graph, describe, load_taskfile, plan_tasks

    if args.code_cmd == "status":
        store = Store(args.db or config.DB_PATH)
        if args.reset_stale:
            n = store.reset_stale_code_tasks()
            print(f"reset {n} stale 'running' task(s) -> failed")
        out = store.code_status()
        print(json.dumps(out, indent=2, default=str))
        return
    if args.code_cmd == "bench":
        cmd_code_bench(args)
        return

    async def run():
        if args.code_cmd == "plan":
            path = await plan_tasks(args.goal, Path(args.repo).resolve())
            print(f"task file written: {path}")
            print(describe(load_taskfile(path)))
            return
        taskset = load_taskfile(args.taskfile)
        if args.dry_run:
            print(describe(taskset))
            print(f"\ndry-run ok — {len(taskset['tasks'])} task(s), roles validated, "
                  "no models called, no git mutations")
            return
        if not Path(args.repo or taskset["repo"]).exists():
            sys.exit(f"repo not found: {taskset['repo']}")
        store = Store(db_path(args, False))
        tf = str(Path(args.taskfile).resolve())
        # Resume semantics: this process just started, so any 'running' rows
        # for THIS taskfile belong to a dead attempt — reset and report the
        # plan before the graph re-executes (merged tasks are skipped inside
        # build_code_graph, failed ones resume one tier higher).
        n_stale = store.reset_stale_code_tasks(taskfile=tf)
        prior_rows = {r["id"]: r["status"] for r in store.code_tasks_for(tf)}
        if prior_rows:
            skipped = sorted(i for i, s in prior_rows.items() if s == "merged")
            retried = sorted(i for i, s in prior_rows.items() if s != "merged")
            print(f"resume {tf}:")
            if skipped:
                print(f"  merged (skipped): {', '.join(skipped)}")
            if retried:
                print(f"  re-executing:     {', '.join(retried)}")
            if n_stale:
                print(f"  stale 'running' rows reset to failed: {n_stale}")
        events.set_context(workload="code-tasks")
        graph = build_code_graph(store, taskset, taskfile=tf)
        final = await graph.run({})
        results = final.get("results", {})
        merged = sorted(k for k, v in results.items()
                        if k.startswith("publish_") and isinstance(v, dict) and v.get("merged"))
        log = logging.getLogger("code-cmd")
        log.info("done — merged: %s", ", ".join(merged) or "none")

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


def cmd_code_bench(args):
    import json

    import orchbench
    from store import Store

    if args.orch_bench_cmd == "report":
        stamp = args.stamp
        if stamp is None:
            root = orchbench.OUT_ROOT
            found = sorted(root.glob("*/results.jsonl"), reverse=True) if root.exists() else []
            if not found:
                sys.exit("no orchbench results yet — run: main.py code bench run")
            stamp = found[0].parent.name
        path = orchbench.OUT_ROOT / stamp / "results.jsonl"
        if not path.exists():
            sys.exit(f"no results for stamp {stamp!r} under {orchbench.OUT_ROOT}")
        results = [json.loads(line) for line in
                   path.read_text(encoding="utf-8").splitlines() if line.strip()]
        print(orchbench.report(results))
        return

    names = (list(orchbench.VARIANTS) if args.variants == "all"
             else [n.strip() for n in args.variants.split(",") if n.strip()])
    for n in names:
        if n not in orchbench.VARIANTS:
            sys.exit(f"unknown variant {n!r}; choices: {sorted(orchbench.VARIANTS)} | all")
    stamp = args.stamp or time.strftime("%Y%m%d-%H%M%S")
    db = args.db or str(orchbench.OUT_ROOT / stamp / "orchbench.db")
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    store = Store(db)
    print(f"orchbench stamp={stamp} variants={names} db={db}")

    async def go():
        results = []
        for n in names:
            r = await orchbench.run_variant(store, n, stamp, plan_only=args.plan)
            results.append(r)
            if args.plan:
                print(f"--- {n} ---")
                print(r["describe"])
        if not args.plan and results:
            print(orchbench.report(results))

    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        pass


def cmd_serve(args):
    from dashboard import serve

    serve(port=args.port, db_path=args.db or config.DB_PATH)


def cmd_bench(args):
    import json

    import bench
    import bench_data
    from pool import ArcPool
    from store import Store

    root = Path(__file__).resolve().parent
    store = Store(args.db or str(root / "orchestrator.db"))

    if args.bench_cmd == "list":
        suites = args.suites.split(",") if args.suites else list(bench_data.SUITES)
        for name in suites:
            s = bench_data.SUITES[name]
            print(f"[{name}] {s['desc']}")
            for t in s["tasks"]:
                print(f"  {t['tier']:<7}{t['task_id']:<22}{t['kind']}")
        return
    if args.bench_cmd == "runs":
        for r in store.bench_runs_list():
            print(f"#{r['id']:<4}{r['status']:<10}{r['label']:<32}"
                  f"{r['suites']:<28}{r['started_at']}")
        return
    if args.bench_cmd == "report":
        ids = [args.run_id] if args.run_id else None
        if ids is None:
            runs = store.bench_runs_list(limit=1)
            if not runs:
                sys.exit("no bench runs yet")
            ids = [runs[0]["id"]]
        print(bench.report(store, ids))
        return

    # bench run -----------------------------------------------------------
    suites = args.suites.split(",") if args.suites else list(bench_data.SUITES)
    tiers = args.tier.split(",") if args.tier else None
    tasks = bench_data.tasks_for(suites, tiers=tiers, limit=args.limit)
    efforts = (args.efforts or "default").split(",")
    specs = []
    for m in args.models.split(","):
        fam, _, eff = m.partition(":")
        fams = list(config.FAMILIES) if fam == "all" else [fam]
        for f in fams:
            if f not in config.FAMILIES:
                sys.exit(f"unknown family {f!r}; choices: {sorted(config.FAMILIES)} | all")
            for e in (eff.split(";") if eff else efforts):
                if e not in config.FAMILIES[f].models:
                    sys.exit(f"unknown effort {e!r} for {f}; "
                             f"choices: {sorted(config.FAMILIES[f].models)}")
                specs.append((f, e))
    harnesses = args.harnesses.split(",")
    for h in harnesses:
        if h not in bench.SOLVERS:
            sys.exit(f"unknown harness {h!r}; choices: {sorted(bench.SOLVERS)}")
    jobs = bench.expand_jobs(tasks, specs, harnesses, n=args.n)
    calls = sum(1 if j["harness"] in ("opencode", "kimi")
                else args.n if j["harness"] == "direct"
                else args.fanout_n if j["harness"] == "fanout"
                else 2 * args.max_rounds if j["harness"] == "review"
                else args.max_rounds for j in jobs)
    plan = (f"{len(tasks)} tasks x {len(specs)} model-specs x "
            f"{len(harnesses)} harnesses = {len(jobs)} jobs "
            f"(~{calls} model calls max, suites={suites}, specs={specs}, "
            f"harnesses={harnesses}, n={args.n}, temp={args.temperature})")
    if args.plan:
        print(plan)
        return
    print(plan)

    async def go():
        pool = ArcPool(dry_run=args.dry_run)
        store.fail_stale_bench_runs()
        cfg = {"specs": specs, "harnesses": harnesses, "n": args.n,
               "temperature": args.temperature, "max_rounds": args.max_rounds,
               "fanout_n": args.fanout_n, "dry_run": args.dry_run}
        rid = store.start_bench_run(",".join(suites), args.label or "",
                                    json.dumps(cfg))
        try:
            await bench.run_jobs(store, pool, rid, jobs,
                                 temperature=args.temperature,
                                 max_rounds=args.max_rounds,
                                 fanout_n=args.fanout_n)
            store.finish_bench_run(rid, "completed")
        except Exception:
            store.finish_bench_run(rid, "failed")
            raise
        print(bench.report(store, [rid]))

    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        pass


def main():
    ap = argparse.ArgumentParser(
        description="24/7 multi-model graph orchestrator for https://llm-api.arc.vt.edu"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="run continuously (24/7)")
    add_common(run_p)
    once_p = sub.add_parser("once", help="run a single round")
    add_common(once_p, once=True)
    st_p = sub.add_parser("status", help="show database statistics")
    st_p.add_argument("--db", default=None, help="sqlite database path")
    sub.add_parser("graph", help="print the round graph topology")
    build_p = sub.add_parser("build", help="run the minecraft build workload")
    build_p.add_argument("--iterations", type=int, default=1, help="sequential build iterations (later ones improve)")
    add_common(build_p, once=True)
    serve_p = sub.add_parser("serve", help="run the dashboard web server")
    serve_p.add_argument("--port", type=int, default=None, help=f"port (default {8787})")
    serve_p.add_argument("--db", default=None, help="sqlite database path")
    serve_p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    code_p = sub.add_parser("code", help="multi-harness code workload (worktrees + reviews)")
    code_sub = code_p.add_subparsers(dest="code_cmd", required=True)
    cr_p = code_sub.add_parser("run", help="run a task file")
    cr_p.add_argument("taskfile", help="path to task JSON")
    cr_p.add_argument("--repo", default=None, help="override repo path from task file")
    cr_p.add_argument("--dry-run", action="store_true", help="print the resolved DAG, no models, no git")
    cr_p.add_argument("--db", default=None, help="sqlite database path")
    cr_p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    cp_p = code_sub.add_parser("plan", help="ask Kimi-K3 to draft a task file for a goal")
    cp_p.add_argument("goal", help="project goal in one sentence")
    cp_p.add_argument("repo", help="target repo path")
    cp_p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    cs_p = code_sub.add_parser("status", help="show code-task and harness-run stats")
    cs_p.add_argument("--db", default=None, help="sqlite database path")
    cs_p.add_argument("--reset-stale", action="store_true",
                      help="mark stale 'running' tasks 'failed' (only when no run process is alive)")
    cb_p = code_sub.add_parser("bench", help="orchestration benchmark: full governed DAG per policy variant")
    cb_sub = cb_p.add_subparsers(dest="orch_bench_cmd", required=True)
    cbr = cb_sub.add_parser("run", help="run the variant matrix (orchbench.VARIANTS)")
    cbr.add_argument("--variants", default="all",
                     help="comma list of variant names, or 'all' (default)")
    cbr.add_argument("--stamp", default=None,
                     help="run stamp grouping taskfiles/db/repos (default: timestamp)")
    cbr.add_argument("--plan", action="store_true",
                     help="validate all variant taskfiles and print resolved DAGs; no models, no git")
    cbr.add_argument("--db", default=None,
                     help="sqlite db (default: logs/orchbench/<stamp>/orchbench.db)")
    cbr.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    cbrep = cb_sub.add_parser("report", help="score table for a finished stamp")
    cbrep.add_argument("--stamp", default=None, help="default: latest stamp with results")
    bench_p = sub.add_parser("bench", help="benchmark models x harnesses on coding tasks")
    b_sub = bench_p.add_subparsers(dest="bench_cmd", required=True)
    bl_p = b_sub.add_parser("list", help="list benchmark suites and tasks")
    bl_p.add_argument("--suites", default=None, help="comma list (default: all)")
    br_p = b_sub.add_parser("runs", help="list past benchmark runs")
    for bp in (bl_p, br_p):
        bp.add_argument("--db", default=None, help="sqlite database path")
    bp_p = b_sub.add_parser("report", help="score tables for a run")
    bp_p.add_argument("--run-id", type=int, default=None, help="default: latest")
    bp_p.add_argument("--db", default=None, help="sqlite database path")
    bun_p = b_sub.add_parser("run", help="run benchmark jobs")
    bun_p.add_argument("--suites", default=None, help="comma list (default: all)")
    bun_p.add_argument("--tier", default=None, help="easy,medium,hard filter")
    bun_p.add_argument("--limit", type=int, default=None, help="first N tasks")
    bun_p.add_argument("--models", default="all",
                       help="comma list of fam[:eff1;eff2] entries, or 'all'")
    bun_p.add_argument("--efforts", default=None,
                       help="efforts for models without a :suffix (default: default)")
    bun_p.add_argument("--harnesses", default="direct",
                       help="comma list: direct,fanout,fixloop,review,opencode,kimi")
    bun_p.add_argument("--n", type=int, default=1,
                       help="samples per cell for the direct harness")
    bun_p.add_argument("--fanout-n", type=int, default=4,
                       help="inner samples for the fanout harness")
    bun_p.add_argument("--temperature", type=float, default=None)
    bun_p.add_argument("--max-rounds", type=int, default=3,
                       help="fix rounds for fixloop/review")
    bun_p.add_argument("--label", default=None, help="run label")
    bun_p.add_argument("--plan", action="store_true", help="print the matrix, run nothing")
    bun_p.add_argument("--dry-run", action="store_true", help="simulate all model calls")
    bun_p.add_argument("--db", default=None, help="sqlite database path")
    bun_p.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    args = ap.parse_args()
    setup_logging(getattr(args, "verbose", False))

    if args.cmd == "graph":
        cmd_graph(args)
    elif args.cmd == "status":
        cmd_status(args)
    elif args.cmd == "run":
        cmd_run(args, once=False)
    elif args.cmd == "once":
        cmd_run(args, once=True)
    elif args.cmd == "build":
        cmd_build(args)
    elif args.cmd == "code":
        cmd_code(args)
    elif args.cmd == "serve":
        cmd_serve(args)
    elif args.cmd == "bench":
        cmd_bench(args)


if __name__ == "__main__":
    main()