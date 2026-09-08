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


def cmd_serve(args):
    from dashboard import serve

    serve(port=args.port, db_path=args.db or config.DB_PATH)


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
    elif args.cmd == "serve":
        cmd_serve(args)


if __name__ == "__main__":
    main()