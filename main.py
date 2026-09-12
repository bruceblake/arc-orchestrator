#!/usr/bin/env python3
import argparse
import asyncio
import logging
import os
import signal
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
    import gitstore
    from code_tasks import (build_code_graph, chain_status, describe,
                        load_taskfile, pending_chains, plan_tasks)

    if args.code_cmd == "status":
        store = Store(args.db or config.DB_PATH)
        if args.reset_stale:
            n = store.reset_stale_code_tasks()
            print(f"reset {n} stale 'running' task(s) -> failed")
        out = store.code_status()
        out["chains"] = pending_chains(store)
        print(json.dumps(out, indent=2, default=str))
        return
    if args.code_cmd == "bench":
        cmd_code_bench(args)
        return
    if args.code_cmd == "promote":
        import gitstore
        repo = Path(args.repo or config.ROOT).resolve()

        async def go():
            await gitstore.ensure_base_branch(repo)
            n, url, note = await gitstore.open_promotion_pr(repo)
            if url:
                print(f"promotion PR: {url}\n  {note}")
                print(f"\nReview and merge it yourself — the fleet never "
                      f"touches {config.PROD_BRANCH}.")
            else:
                print(f"no promotion PR opened: {note}")
        asyncio.run(go())
        return
    if args.code_cmd == "reconcile":
        import reconcile as _rec
        store = Store(args.db or config.DB_PATH)
        rep = asyncio.run(_rec.reconcile(store, apply=not args.dry_run,
                                         force=args.force))
        if args.dry_run and not rep["skipped"]:
            print("(dry run — nothing was changed)")
        print(_rec.format_report(rep))
        return
    if args.code_cmd == "list":
        cmd_code_list(args)
        return

    async def run():
        if args.code_cmd == "plan":
            path = await plan_tasks(args.goal, Path(args.repo).resolve(),
                                    store=Store(db_path(args, False)))
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
        # --no-wait: a read-only pre-flight of the chain gate. Instead of
        # sitting in chain_wait until the upstream projects merge, fail fast
        # with exit code 2 — the signal a queue/CI wrapper uses to requeue.
        # Placed before the live-run guard on purpose: it mutates no rows
        # (the check only reads the deps' code_tasks rows), so it is safe
        # to run even while another process owns this file.
        if args.no_wait and taskset.get("after"):
            st = chain_status(store, taskset["after"])
            if not st["ok"]:
                bits = []
                for d in st["deps"]:
                    n = Path(d["taskfile"]).name
                    if d["failed"]:
                        bits.append(f"{n}: failed {', '.join(d['failed'])}")
                    elif not d["readable"]:
                        bits.append(f"{n}: taskfile not on disk yet")
                    elif d["n_tasks"]:
                        bits.append(f"{n}: {d['merged']}/{d['n_tasks']} merged")
                    else:
                        bits.append(f"{n}: no rows yet")
                print(f"chain not ready ({'; '.join(bits)}); "
                      "run again without --no-wait to wait for it",
                      file=sys.stderr)
                sys.exit(2)
        # Two processes on the SAME task file would share task ids, worktrees
        # and branches and fight over them; the stale-reset each performs at
        # startup would also clobber the other's live rows. Driver leases keep
        # the account within its caps, but they cannot make this coherent.
        # (Different task files in parallel are fine and expected.)
        import reconcile as _rec
        others = [r["pid"] for r in _rec.live_runs()
                  if r.get("taskfile")
                  and str(Path(r["taskfile"]).resolve()) == tf]
        if others and not args.force:
            sys.exit(
                f"{Path(tf).name} is already being run by pid "
                f"{', '.join(map(str, others))}.\n"
                f"Wait for it, stop it (dashboard Stop, or `kill {others[0]}`), "
                f"or pass --force to run a second one anyway.")
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
        log = logging.getLogger("code-cmd")
        import gitstore as _gs
        await _gs.ensure_base_branch(Path(args.repo or taskset["repo"]).resolve())
        if config.kimi_plan_mode_on():
            log.error(
                "kimi is configured with default_plan_mode = true (%s).\n"
                "Headless agents will research and propose instead of editing: "
                "leaving plan mode needs ExitPlanMode approved, and no one is "
                "there to approve it. Set default_plan_mode = false, or pass "
                "--force to run anyway.", config.KIMI_CONFIG)
            events.emit("run.refused", taskfile=tf, reason="kimi default_plan_mode is true")
            if not args.force:
                sys.exit(1)
        # The PR gate needs somewhere to push. Checked HERE, before a model is
        # spent, because the alternative is what happened to minecraft-test on
        # 09-12: the scaffold was written, passed its gate, passed review, and
        # was then thrown away at publish with "push failed: no git remote
        # configured". Roughly eight minutes of model time to discover a fact
        # `git remote` answers instantly.
        gh = await gitstore.github_status(taskset["repo"])
        if not gh.get("ready"):
            log.error(
                "%s cannot complete a task: %s.\n"
                "Every task ends by pushing a branch and opening a pull request "
                "— without that, work is written, reviewed, and then discarded. "
                "Fix it, or pass --force to run anyway.",
                taskset["repo"], gh.get("reason") or "not ready for pull requests")
            events.emit("run.refused", taskfile=tf,
                        reason=f"repo not PR-ready: {gh.get('reason')}")
            if not args.force:
                sys.exit(1)
        graph = build_code_graph(store, taskset, taskfile=tf)
        # SIGTERM (the dashboard's Stop button, systemd, `kill <pid>`) and
        # SIGINT must unwind through the cleanup below rather than killing the
        # process outright: the default action skipped the finally block, so a
        # stopped run left its rows 'running' and its leases held, and the
        # cancellation never reached the drivers to kill their harness children.
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, ValueError):
                pass
        graph_task = asyncio.create_task(graph.run({}))
        stop_task = asyncio.create_task(stop.wait())
        final = {}
        try:
            done, _pending = await asyncio.wait(
                {graph_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            if graph_task in done:
                final = graph_task.result()
            else:
                log.warning("stop requested — cancelling in-flight tasks")
                events.emit("run.stopped", taskfile=tf)
                graph_task.cancel()
                await asyncio.gather(graph_task, return_exceptions=True)
        finally:
            stop_task.cancel()
            # Whatever happened — clean finish, node crash, Ctrl-C, SIGTERM —
            # this process is about to stop owning these rows and leases. Left
            # behind, 'running' rows make the dashboard lie and the next resume
            # mistake them for failures, and lease rows throttle the fleet
            # against a process that no longer exists.
            import reconcile as _rec
            leaked = store.running_code_tasks(taskfile=tf)
            if leaked:
                store.reset_stale_code_tasks(taskfile=tf,
                                             reason=_rec.INTERRUPTED_REASON)
                log.warning("marked %d unfinished task(s) as failed: %s",
                            len(leaked), ", ".join(r["id"] for r in leaked))
                events.emit("run.interrupted", taskfile=tf,
                            tasks=[r["id"] for r in leaked])
            freed = store.release_leases_for_pid(os.getpid())
            if freed:
                log.info("released %d driver lease(s)", freed)
        results = final.get("results", {})
        merged = sorted(k for k, v in results.items()
                        if k.startswith("publish_") and isinstance(v, dict) and v.get("merged"))
        log.info("done — merged: %s", ", ".join(merged) or "none")
        # The chain gate failing is a clean graph end, not an exception —
        # surface it as a non-zero exit so wrappers can tell it apart from
        # success. (1 = chain blocked/timed out at runtime; --no-wait uses 2.)
        cw = results.get("chain_wait")
        if isinstance(cw, dict) and not cw.get("ok"):
            log.error("chain blocked: %s", cw.get("reason", "unknown"))
            sys.exit(1)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


def cmd_code_list(args):
    import json
    from datetime import datetime, timezone

    from store import Store

    store = Store(args.db or config.DB_PATH)
    try:
        rows_all = store.code_tasks_all()
    except Exception:
        rows_all = []
    tdir = Path(config.TASKS_DIR)
    entries = []
    for f in sorted(tdir.glob("*.json")) if tdir.is_dir() else []:
        entry = {"file": f.name, "n_tasks": 0, "merged": 0,
                 "statuses": {}, "last_activity": None}
        try:
            data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            entry["error"] = "parse error"
            entries.append(entry)
            continue
        proj = data.get("project") or {}
        ids = [t.get("id") for t in (proj.get("tasks") or [])
               if isinstance(t, dict) and t.get("id")]
        idset = set(ids)
        entry["n_tasks"] = len(ids)
        statuses = {}
        last = None
        for r in rows_all:
            if not (r.get("taskfile")
                    and (r["taskfile"] == str(f) or r["taskfile"].endswith("/" + f.name))):
                continue
            if r.get("id") not in idset:
                continue
            st = r.get("status") or "pending"
            statuses[st] = statuses.get(st, 0) + 1
            for k in ("created_at", "finished_at"):
                v = r.get(k)
                if v and (last is None or v > last):
                    last = v
        entry["statuses"] = statuses
        entry["merged"] = statuses.get("merged", 0)
        if last is None:
            try:
                last = datetime.fromtimestamp(
                    f.stat().st_mtime, tz=timezone.utc).isoformat()
            except OSError:
                last = None
        entry["last_activity"] = last
        entries.append(entry)

    entries.sort(key=lambda p: p["last_activity"] or "", reverse=True)

    if args.json:
        print(json.dumps(entries, indent=2, default=str))
        return

    for p in entries:
        if p.get("error"):
            print(f"{p['file']:24}  error: {p['error']}")
            continue
        summary = " ".join(f"{k}={v}" for k, v in sorted(p["statuses"].items()))
        print(f"{p['file']:24}  {p['n_tasks']} tasks  "
              f"{p['merged']}/{p['n_tasks']} merged  "
              f"{summary}  {p['last_activity'] or ''}")


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


def cmd_audit(args):
    """Daily audit. Exit code is the alarm: 2 if anything critical, else 0.

    A non-zero exit is what lets this be scheduled without anybody reading it
    on a quiet day — cron mails only on failure, and 'critical' is defined
    narrowly enough that a mail means something.
    """
    import audit
    import json
    import store as _store
    st = None
    try:
        st = _store.Store(config.DB_PATH)
    except Exception:
        pass
    if args.fix:
        # reconcile() is async and its reporter is format_report — guessed
        # wrong once and the scheduled audit crashed on its first real run.
        import asyncio
        import reconcile
        if st is None:
            print("--fix needs the database; skipping cleanup")
        elif reconcile.live_runs():
            # Reaping worktrees and leases out from under a LIVE run is how a
            # cleanup becomes an outage. The audit still reports; it just does
            # not touch anything while the fleet is working.
            print("--fix skipped: runs are in flight")
        else:
            print(reconcile.format_report(
                asyncio.run(reconcile.reconcile(st, apply=True))))
    report = audit.run(st, since_s=args.since, with_health=not args.no_health,
                       snapshot=args.fix)
    print(json.dumps(report, indent=2, default=str) if args.json
          else audit.render(report))
    return 2 if report["counts"]["critical"] else 0


def cmd_gh(args):
    import gh_ops

    try:
        if args.gh_cmd == "triage":
            rc = asyncio.run(gh_ops.triage(args.repo, model=args.model,
                                           apply_labels=args.apply_labels))
        elif args.gh_cmd == "issue":
            rc = asyncio.run(gh_ops.make_issue(args.desc, args.repo,
                                               model=args.model, create=args.create))
        else:  # pr-review
            rc = asyncio.run(gh_ops.pr_review(args.repo, args.number,
                                              model=args.model, post=args.post))
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}")
        sys.exit(1)
    sys.exit(rc)


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


def cmd_doctor(args):
    import shutil

    import reconcile
    from store import Store

    failures = 0

    def report(name, ok, detail=""):
        nonlocal failures
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f" — {detail}"))
        if not ok:
            failures += 1

    if config.kimi_plan_mode_on():
        report("kimi plan mode off", False,
               f"default_plan_mode = true in {config.KIMI_CONFIG}; plan mode makes "
               "headless agents research and propose instead of edit — leaving it "
               "needs ExitPlanMode approved, which nothing does in a headless run. "
               "This silently wasted most fleet runs before it was found; set "
               f"default_plan_mode = false in {config.KIMI_CONFIG}")
    else:
        report("kimi plan mode off", True)

    key = config.API_KEY
    report("ARC_API_KEY set", bool(key) and "PASTE-YOUR-KEY" not in key,
           "ARC_API_KEY is missing or still the placeholder — add your key from "
           "llm.arc.vt.edu to the .env file")

    for harness in ("kimi", "opencode"):
        report(f"'{harness}' on PATH", shutil.which(harness) is not None,
               f"the {harness} harness binary was not found on PATH")

    for label, path in (("WORKTREE_ROOT", config.WORKTREE_ROOT),
                        ("TASKS_DIR", config.TASKS_DIR)):
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
            ok, detail = True, ""
        except OSError as exc:
            ok, detail = False, f"cannot create {path}: {exc}"
        report(f"{label} exists or can be created ({path})", ok, detail)

    report("timeouts: DRIVER_LEASE_TTL > DRIVER_TIMEOUT > DRIVER_IDLE_TIMEOUT",
           config.DRIVER_LEASE_TTL > config.DRIVER_TIMEOUT > config.DRIVER_IDLE_TIMEOUT,
           f"DRIVER_LEASE_TTL={config.DRIVER_LEASE_TTL}s, "
           f"DRIVER_TIMEOUT={config.DRIVER_TIMEOUT}s, "
           f"DRIVER_IDLE_TIMEOUT={config.DRIVER_IDLE_TIMEOUT}s — leases must outlive "
           "a harness run and the idle timeout must stay under the total cap")

    if reconcile.live_runs():
        report("no stale 'running' code_tasks", True)
    else:
        stale = [r["id"] for r in
                 Store(args.db or config.DB_PATH).running_code_tasks()]
        report("no stale 'running' code_tasks", not stale,
               f"{len(stale)} task(s) marked 'running' but no `main.py code run` "
               f"process is alive ({', '.join(stale)}) — run `main.py code reconcile` "
               "or `main.py code status --reset-stale`")

    if failures:
        sys.exit(1)


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
    doc_p = sub.add_parser("doctor", help="check for misconfiguration that silently breaks runs")
    doc_p.add_argument("--db", default=None, help="sqlite database path")
    code_p = sub.add_parser("code", help="multi-harness code workload (worktrees + reviews)")
    code_sub = code_p.add_subparsers(dest="code_cmd", required=True)
    cr_p = code_sub.add_parser("run", help="run a task file")
    cr_p.add_argument("taskfile", help="path to task JSON")
    cr_p.add_argument("--repo", default=None, help="override repo path from task file")
    cr_p.add_argument("--dry-run", action="store_true", help="print the resolved DAG, no models, no git")
    cr_p.add_argument("--force", action="store_true",
                      help="run even if another process is already running this task file")
    cr_p.add_argument("--no-wait", action="store_true",
                      help="exit code 2 instead of waiting when the task file's "
                           "`after` dependencies are not all merged yet")
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
    cpr = code_sub.add_parser(
        "promote",
        help=f"open a {config.BASE_BRANCH} -> {config.PROD_BRANCH} PR for you to merge")
    cpr.add_argument("--repo", default=None, help="repo path (default: this one)")
    cpr.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    crec = code_sub.add_parser(
        "reconcile",
        help="reap orphans a killed run left behind (stale rows, leases, worktrees)")
    crec.add_argument("--dry-run", action="store_true",
                      help="report what would be reaped, change nothing")
    crec.add_argument("--force", action="store_true",
                      help="reconcile even while a code-run process is alive")
    crec.add_argument("--db", default=None, help="sqlite database path")
    crec.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    cl_p = code_sub.add_parser("list", help="list every task file in the tasks dir")
    cl_p.add_argument("--json", action="store_true",
                      help="emit the same data as JSON instead of a table")
    cl_p.add_argument("--db", default=None, help="sqlite database path")
    cl_p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
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
    au_p = sub.add_parser("audit", help="daily audit: triage defects + check the codebase")
    au_p.add_argument("--since", type=float, default=86400,
                      help="seconds of error history to triage (default 24h)")
    au_p.add_argument("--json", action="store_true", help="emit the report as JSON")
    au_p.add_argument("--no-health", action="store_true",
                      help="skip running check.sh (it is the slow part)")
    au_p.add_argument("--fix", action="store_true",
                      help="also run the reversible cleanups (reconcile --apply)")

    gh_p = sub.add_parser("gh", help="GitHub ops agents (gh CLI): triage, issue, pr-review")
    gh_sub = gh_p.add_subparsers(dest="gh_cmd", required=True)
    gt_p = gh_sub.add_parser("triage", help="classify open issues; writes a taskfile to ~/tasks")
    gt_p.add_argument("repo", help="owner/name or a local checkout path")
    gt_p.add_argument("--apply-labels", action="store_true",
                      help="apply triage labels via gh issue edit (writes to GitHub)")
    gi_p = gh_sub.add_parser("issue", help="draft an issue from a description (prints a preview)")
    gi_p.add_argument("desc", help="free-form goal or bug description")
    gi_p.add_argument("repo", help="owner/name or a local checkout path")
    gi_p.add_argument("--create", action="store_true",
                      help="file the issue via gh issue create (default: print preview)")
    gp_p = gh_sub.add_parser("pr-review", help="review a PR (verdict contract of internal review)")
    gp_p.add_argument("repo", help="owner/name or a local checkout path")
    gp_p.add_argument("number", type=int, help="pull request number")
    gp_p.add_argument("--post", action="store_true",
                      help="submit via gh pr review (default: print only)")
    for gp in (gt_p, gi_p, gp_p):
        gp.add_argument("--model", choices=["Kimi-K3", "GLM-5.3"], default=None,
                        help=f"agent model (default {config.GH_MODEL})")
        gp.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    chat_p = sub.add_parser(
        "chat", help="conversational planning with the fleet's planner")
    chat_p.add_argument("--session", required=True,
                        help="session id, ^[a-z0-9][a-z0-9-]{0,39}$")
    chat_p.add_argument("--repo", required=True,
                        help="absolute repo path under /home/proxyie")
    chat_p.add_argument("-v", "--verbose", action="store_true", help="debug logging")

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
    elif args.cmd == "audit":
        # The exit code IS the alarm. Without propagating it, a scheduled audit
        # exits 0 no matter what it found and cron never says a word.
        sys.exit(cmd_audit(args))
    elif args.cmd == "gh":
        cmd_gh(args)
    elif args.cmd == "serve":
        cmd_serve(args)
    elif args.cmd == "doctor":
        cmd_doctor(args)
    elif args.cmd == "bench":
        cmd_bench(args)
    elif args.cmd == "chat":
        import orchchat
        sys.exit(asyncio.run(orchchat.run_turn(args.session, args.repo)))


if __name__ == "__main__":
    main()