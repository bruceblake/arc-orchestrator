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
    """The sqlite file this command records into.

    Real runs use config.DB_PATH, which honours ARC_DB_PATH. This used to
    build `<dir of main.py>/orchestrator.db` itself and ignore the override,
    so a run launched from a second checkout (a git worktree) wrote its task
    rows into THAT checkout's database while its leases, errors and the
    dashboard used the shared one: on 2026-09-22 a studio run merged a task
    that the dashboard and the next resume could not see at all. With
    ARC_DB_PATH unset the two paths are identical. Dry runs keep their own
    file next to main.py, as before.
    """
    if args.db:
        return args.db
    if dry_run:
        return str(Path(__file__).resolve().parent / "dry-run.db")
    return str(config.DB_PATH)


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

    pool = ArcPool(dry_run=True)
    store = Store(":memory:")
    for g in (
        build_round_graph(pool, store, Roles(), {}),
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
            # Only rows whose taskfile has NO live run are stale. A blanket
            # reset used to flip every 'running' row — including the ones a
            # concurrent `code run` was actively executing — because the help
            # text promised a guard the code never had.
            import reconcile as _rec
            matcher = _rec.LiveTaskfileMatcher()
            rows = store.running_code_tasks()
            n = 0
            seen = set()
            for r in rows:
                raw = r.get("taskfile") or ""
                if not raw or matcher.is_live(raw) or raw in seen:
                    continue
                seen.add(raw)
                n += store.reset_stale_code_tasks(taskfile=raw)
            print(f"reset {n} stale 'running' task(s) -> failed "
                  f"({len(matcher)} live taskfile(s) left alone)")
        out = store.code_status()
        out["chains"] = pending_chains(store)
        print(json.dumps(out, indent=2, default=str))
        return
    if args.code_cmd == "dream":
        cmd_code_dream(args)
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
    if args.code_cmd in ("checkpoints", "restore"):
        asyncio.run(cmd_code_checkpoints(args))
        return
    if args.code_cmd == "context":
        sys.exit(cmd_code_context(args))
    if args.code_cmd == "issues":
        sys.exit(cmd_code_issues(args))

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
        others = _rec.LiveTaskfileMatcher().matching_pids(tf)
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
        repo_path = Path(args.repo or taskset["repo"]).resolve()
        await _gs.ensure_base_branch(repo_path)
        # Dependents branch from the LOCAL base. A PR that merged on GitHub
        # while this checkout failed to fast-forward (Godot's untracked
        # *.uid files did exactly that to cell-wing-foundation) leaves origin
        # ahead, and a resume skips the already-merged task so nothing else
        # advances the branch. Tasks then rebuild the scaffold and miss the
        # code they depend on. Refuse unless the operator passes --force.
        ok_ff, ff_note = await _gs.fast_forward_base(repo_path)
        if not ok_ff and await _gs.origin_ahead(repo_path):
            log.error(
                "local %s is behind origin/%s (%s).\n"
                "Tasks branch from the local base, so a merge that landed on "
                "GitHub but not here is invisible to every dependent. "
                "Fix the checkout, or pass --force to branch from it anyway.",
                config.BASE_BRANCH, config.BASE_BRANCH, ff_note)
            events.emit("run.refused", taskfile=tf,
                        reason=f"base behind origin: {ff_note[:300]}")
            if not args.force:
                sys.exit(1)
        # Only when a live model actually runs the kimi harness (Kimi-K3 was
        # retired 2026-09-12; nothing does today, so this is a no-op and the
        # check stays for a future kimi-harness model).
        if "kimi" in config.MODEL_HARNESS.values() and config.kimi_plan_mode_on():
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
        if not gh.get("ready") and gh.get("reason") == "no git remote configured":
            # The missing remote is the one refusal gh can cure on its own —
            # same machine, same auth, one `gh repo create`. Only if that too
            # fails is the run really stuck, and the refusal then says what
            # ensure_remote said instead of the bare git fact.
            ok, url_or_reason = await gitstore.ensure_remote(taskset["repo"])
            if ok:
                events.emit("repo.remote_created", taskfile=tf, url=url_or_reason)
                log.info("created GitHub remote for %s: %s",
                         taskset["repo"], url_or_reason)
                gh = await gitstore.github_status(taskset["repo"])
            else:
                gh = {"ready": False, "reason": url_or_reason}
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
        # Every task is a GitHub issue: the epic plus one issue per unmerged
        # task, before any work starts (best-effort; gh_issues.py).
        from code_tasks import open_task_issues
        await open_task_issues(store, taskset, tf)
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
            # Drain or cancel — this is the moment, whatever ended the run.
            # A worktree is state: the NEXT alloc resets task/<id> to base, so
            # whatever the attempts left in theirs is checkpointed here, before
            # anything can discard it. AWAITED, not asyncio.run: this finally is
            # still inside async def run(), and asyncio.run raises RuntimeError
            # on a live loop — which the except below would swallow into a log
            # line, losing the very checkpoint a cancel exists to write.
            # The TASK IDS, not the row dicts: `leaked` holds dicts (the log
            # line above reads r["id"]), and checkpoint_stopping resolves each
            # entry to WORKTREE_ROOT/<repo>/<id>. Passing the dicts made every
            # lookup miss, so a cancel saved nothing and said nothing — a
            # non-existent directory is an ordinary skip in the sweep.
            try:
                import gitstore as _gs
                n = await _gs.checkpoint_stopping(
                    taskset["repo"], [r["id"] for r in leaked])
                if n:
                    log.info("checkpointed %d interrupted worktree(s)", n)
            except Exception as exc:                           # noqa: BLE001
                import errors as _errors
                _errors.capture(exc, node="checkpoint_stopping")
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


def cmd_code_dream(args):
    """`code dream` — Dream-RSI offline policy improvement (arXiv:2609.14858).

    Replays recorded history (store.code_tasks + harness_runs) against the
    built-in exploration policies, plus any candidate a `--policy <file>` or a
    policy-development agent supplies, and reports the argmax. NO models, NO
    git: replay reads recorded outcomes only (the paper's "dreaming").
    """
    import json

    from store import Store

    import dream_rsi
    import events

    store = Store(args.db or config.DB_PATH)
    trees = dream_rsi.load_trees(store, taskfile=args.taskfile)
    if not trees:
        print("no recorded history to replay "
              "(nothing in code_tasks/harness_runs for that selection)")
        return

    extra = []
    if args.policy:
        try:
            src = Path(args.policy).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"could not read policy file {args.policy}: {exc}")
            src = ""
        pol = dream_rsi.compile_policy(src) if src else None
        if pol is None:
            print(f"policy file {args.policy} did not compile to an "
                  "ExplorationPolicy subclass; ignoring it")
        else:
            extra.append(pol)

    result = dream_rsi.improve(trees, extra_policies=extra,
                               W=args.workers, max_rounds=args.rounds)
    path = dream_rsi.save_run(result)
    events.emit("dream.completed", history_size=len(trees),
                selected=result.selected, means=result.mean_by_policy,
                report=path)

    if args.json:
        print(json.dumps({"selected": result.selected,
                          "means": result.mean_by_policy,
                          "per_tree": result.per_tree,
                          "report": path}, indent=2, default=str))
        return

    print(f"dreamed over {len(trees)} recorded run(s); "
          f"W={dream_rsi.workers() if args.workers is None else args.workers}, "
          f"K2={args.rounds or config.DREAM_MAX_ROUNDS}")
    print(f"  beta1={dream_rsi.beta1()} (attempt cost) "
          f"beta2={dream_rsi.beta2()} (parallelism bonus)")
    print("  mean replay score by policy (higher is better):")
    for name, score in sorted(result.mean_by_policy.items(),
                              key=lambda kv: -kv[1]):
        mark = "  <- selected" if name == result.selected else ""
        print(f"    {name:18s} {score:8.4f}{mark}")
    print(f"  selected policy: {result.selected}")
    print(f"  report: {path}")


async def cmd_code_checkpoints(args):
    """`code checkpoints` / `code restore`: inspect and apply an attempt's work.

    A worktree is state (gitstore.checkpoint): every implement attempt, every
    reset that would discard work, and every drain is checkpointed. These are
    the operator's handles on them — list what exists, and put one back.
    """
    import json

    import gitstore
    from store import Store

    store = Store(args.db or config.DB_PATH)
    repo, tf = _resolve_task_repo(args, store)
    tid = args.task
    rows = gitstore.checkpoint_files(repo, tid)
    if args.code_cmd == "checkpoints":
        if args.json:
            print(json.dumps([{"path": str(p), **m} for p, m in rows],
                             indent=2, default=str))
            return
        if not rows:
            print(f"no checkpoints for {tid} in {repo}")
            return
        print(f"{len(rows)} checkpoint(s) for {tid} in {repo}:")
        for p, m in rows:
            print(f"  {p.name:44} {str(m.get('label') or '?'):12} "
                  f"{len(m.get('files') or []):3} file(s)  "
                  f"{m.get('commits') if m.get('commits') is not None else '?'}"
                  f" commit(s)  {m.get('model') or ''}")
            files = m.get("files") or []
            if files:
                print(f"      {', '.join(files[:8])}"
                      + (" ..." if len(files) > 8 else ""))
        print(f"\nrestore with: main.py code restore {tid} "
              f"[--checkpoint PATH]")
        return

    wt = gitstore.worktree_for(repo, tid)
    if not (wt / ".git").exists():
        print(f"no worktree at {wt}. Run the task's alloc first "
              f"(`main.py code run <taskfile>`), then restore.")
        sys.exit(1)
    res = await gitstore.restore_checkpoint(repo, tid, wt, path=args.checkpoint)
    if res.get("restored"):
        print(f"restored {len(res.get('files') or [])} file(s) from "
              f"{res.get('path')} into {wt}")
        for f in res.get("files") or []:
            print(f"  {f}")
        return
    print(f"not restored: {res.get('reason')}")
    for f in res.get("conflicts") or []:
        print(f"  conflict: {f}")
    sys.exit(1)


def _resolve_task_repo(args, store):
    """(repo, taskfile) for a checkpoint command.

    `--taskfile` names the project outright. Without it the task id is looked
    up in the recorded rows, because an operator restoring work at 3am should
    not have to remember which file planned it. What the lookup finds is the
    REPO the row recorded, never a path from the command line.
    """
    from code_tasks import load_taskfile

    tf = getattr(args, "taskfile", None)
    if tf:
        return Path(load_taskfile(tf)["repo"]).resolve(), str(tf)
    try:
        rows = [r for r in store.code_tasks_all() if r.get("id") == args.task]
    except Exception:                                          # noqa: BLE001
        rows = []
    for r in sorted(rows, key=lambda r: r.get("created_at") or "", reverse=True):
        raw = r.get("taskfile") or ""
        if raw and Path(raw).is_file():
            try:
                return Path(load_taskfile(raw)["repo"]).resolve(), raw
            except Exception:                                  # noqa: BLE001
                continue
    sys.exit(f"cannot tell which repo task {args.task!r} belongs to — "
             f"pass --taskfile <taskfile>")


def cmd_code_context(args):
    """Print a task's dossier (context OUT); --note injects context IN."""
    import dossier
    if args.db:
        config.DB_PATH = args.db
    if args.taskfile:
        from code_tasks import load_taskfile
        project = Path(load_taskfile(args.taskfile)["repo"]).name
    else:
        project = dossier.find_project(args.task)
    if not project:
        print(f"no dossier for task {args.task!r} (pass --taskfile)",
              file=sys.stderr)
        return 1
    if args.note:
        dossier.import_notes(project, args.task, args.note, args.author)
    print(dossier.export(project, args.task, "json" if args.json else "md"))
    return 0


def cmd_code_issues(args):
    """`code issues sync <taskfile>` backfills GitHub issues and the epic from
    code_tasks rows; `code issues show <task>` prints the recorded issues."""
    import gh_issues
    if args.db:
        config.DB_PATH = args.db
    if args.issues_cmd == "sync":
        out = asyncio.run(gh_issues.sync_taskfile(str(Path(args.taskfile).resolve())))
        print(f"epic #{out.pop('', None)}")
        for tid, n in out.items():
            print(f"  #{n}  {tid}")
        return 0
    rows = gh_issues.rows_for_task(args.task)
    if not rows:
        print(f"no issue recorded for task {args.task!r}", file=sys.stderr)
        return 1
    for r in rows:
        print(f"#{r['issue']}  {r['task']}  epic #{r['epic'] or '-'}  "
              f"{Path(r['taskfile']).name}  {r['repo']}  {r['created_at']}")
    return 0


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

    if "kimi" in config.MODEL_HARNESS.values() and config.kimi_plan_mode_on():
        report("kimi plan mode off", False,
               f"default_plan_mode = true in {config.KIMI_CONFIG}; plan mode makes "
               "headless agents research and propose instead of edit — leaving it "
               "needs ExitPlanMode approved, which nothing does in a headless run. "
               "This silently wasted most fleet runs before it was found; set "
               f"default_plan_mode = false in {config.KIMI_CONFIG}")
    else:
        report("kimi plan mode off", True,
               "no live model runs the kimi harness (Kimi-K3 retired 2026-09-12)")

    key = config.API_KEY
    report("ARC_API_KEY set", bool(key) and "PASTE-YOUR-KEY" not in key,
           "ARC_API_KEY is missing or still the placeholder — add your key from "
           "llm.arc.vt.edu to the .env file")

    # Every harness a LIVE roster model runs must be on PATH: opencode for
    # GLM-5.3, reasonix for DeepSeek-V4.1-Flash-thinking-max (2026-09-12 fleet).
    # Derived, not a literal list — the doctor once kept demanding the retired
    # kimi binary and never checked dsh.
    # Resolved the way the drivers resolve it (config.harness_bin): dsh and
    # reasonix live in the npm prefix, which the service's shell does not
    # have on PATH, and the doctor once failed a harness the fleet was
    # running fine.
    for harness in sorted(set(config.MODEL_HARNESS.values())):
        exe = config.harness_bin(harness)
        report(f"'{harness}' executable found", os.access(exe, os.X_OK),
               f"the {harness} harness binary was not found (looked for {exe})")

    # graft is optional (graft.py: the fleet runs unchanged without it), so
    # its absence is INFO, not FAIL — but an operator should see whether the
    # code-graph hints are actually on, since they are the difference
    # between a harness reading three spans and grepping for ten minutes.
    import graft
    if not config.GRAFT_ENABLED:
        print("INFO  graft code-graph hints: off (ARC_GRAFT=0)")
    elif graft.available():
        print(f"PASS  graft code-graph hints: on ({graft.binary()})")
    else:
        print("INFO  graft code-graph hints: off — `graft` not found; install "
              "with deploy/install-graft.sh (npm i -g @nanonets/graft) to cut "
              "harness search tokens")

    for label, path in (("WORKTREE_ROOT", config.WORKTREE_ROOT),
                        ("TASKS_DIR", config.TASKS_DIR)):
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
            ok, detail = True, ""
        except OSError as exc:
            ok, detail = False, f"cannot create {path}: {exc}"
        report(f"{label} exists or can be created ({path})", ok, detail)

    longest_total = config.longest_total_timeout()
    role_budgets = "/".join(f"{role}={config.ROLE_TIMEOUT[role]}"
                            for role in ("planner", "reviewer", "implementer")
                            if role in config.ROLE_TIMEOUT)
    if longest_total > 0:
        timeouts_ok = (config.DRIVER_LEASE_TTL
                       > longest_total > config.DRIVER_IDLE_TIMEOUT)
        timeouts_why = ("leases must outlive the longest harness run and the "
                        "idle timeout must stay under the total cap")
    else:
        # Total budgets are unlimited (0): no finite cap to compare against,
        # so the lease TTL just needs to be the fixed generous bound (24h).
        timeouts_ok = config.DRIVER_LEASE_TTL >= 86400
        timeouts_why = ("total budgets are unlimited (0); the lease TTL falls "
                        "back to a fixed 24h bound (pid liveness is checked "
                        "before it condemns a row)")
    report("timeouts: DRIVER_LEASE_TTL outlives the longest harness run",
           timeouts_ok,
           f"DRIVER_LEASE_TTL={config.DRIVER_LEASE_TTL}s, "
           f"longest role budget={longest_total}s ({role_budgets}), "
           f"DRIVER_TIMEOUT={config.DRIVER_TIMEOUT}s, "
           f"DRIVER_IDLE_TIMEOUT={config.DRIVER_IDLE_TIMEOUT}s — {timeouts_why}")

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


def cmd_board(args):
    """`main.py board post|read|claims` — the agent coordination board."""
    import agentboard
    if getattr(args, "db", None):
        config.DB_PATH = args.db
    project = args.project or agentboard.infer_project()[0]
    if not project:
        print("board: --project is required outside ~/worktrees/<project>/<task>",
              file=sys.stderr)
        return 2
    if args.board_cmd == "post":
        if args.kind not in agentboard.KINDS:
            print(f"board: unknown kind {args.kind!r}; one of {', '.join(agentboard.KINDS)}",
                  file=sys.stderr)
            return 2
        if not agentboard.valid_channel(args.channel):
            print(f"board: invalid channel {args.channel!r}", file=sys.stderr)
            return 2
        mid = agentboard.post(project, author=args.author, channel=args.channel,
                              kind=args.kind, body=args.body, mentions=args.mention,
                              reply_to=args.reply_to)
        if agentboard.stored(project, mid):
            print(mid)
            return 0
        # The DB was not writable from here — a harness sandbox (Codex's
        # workspace-write mounts everything outside the worktree read-only).
        # The id alone used to be printed with exit 0 and the post was lost.
        # Queue it in the worktree's .arc/board.jsonl, which the orchestrator
        # harvests while the run is live and again when it ends.
        wt = agentboard.worktree_root()
        if wt is None:
            print(f"board: could not write the board DB ({config.DB_PATH}) and "
                  "this is not a worktree to queue the post in", file=sys.stderr)
            return 1
        try:
            path = agentboard.spool(wt, channel=args.channel, kind=args.kind,
                                    body=args.body, mentions=args.mention,
                                    reply_to=args.reply_to, msg_id=mid)
        except OSError as exc:
            print(f"board: could not write the board DB or queue the post: {exc}",
                  file=sys.stderr)
            return 1
        print(mid)
        print(f"board: DB not writable from this sandbox; queued in {path} — "
              "the orchestrator delivers it within a minute while your run is "
              "live (under your own agent ID).", file=sys.stderr)
        return 0
    if args.board_cmd == "claims":
        for c in agentboard.claims(project):
            left = int(c["expires_at"] - time.time())
            print(f"{c['author']}  {', '.join(c['paths'])}  ({left}s left){'  ' + c['note'] if c['note'] else ''}")
        return 0
    if args.reader:
        rows = agentboard.inbox(project, args.reader, since_ts=args.since)
    else:
        rows = agentboard.thread(project, channel=args.channel, since_ts=args.since)

    def show(m, depth=0):
        print(f"{'  ' * depth}[{m['kind']} #{m['id']} {m['channel']} "
              f"{time.strftime('%m-%d %H:%M', time.localtime(m['ts']))}] "
              f"{m['author']}: {m['body']}")
        for r in m.get("replies", ()):
            show(r, depth + 1)
    for m in rows:
        show(m)
    return 0


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
    cp_p = code_sub.add_parser("plan", help="ask the planner model to draft a task file for a goal")
    cp_p.add_argument("goal", help="project goal in one sentence")
    cp_p.add_argument("repo", help="target repo path")
    cp_p.add_argument("--db", default=None, help="sqlite database path")
    cp_p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    cs_p = code_sub.add_parser("status", help="show code-task and harness-run stats")
    cs_p.add_argument("--db", default=None, help="sqlite database path")
    cs_p.add_argument("--reset-stale", action="store_true",
                      help="mark 'running' tasks 'failed' when their taskfile "
                           "has no live run (concurrent runs are left alone)")
    cck = code_sub.add_parser(
        "checkpoints", help="list a task's worktree checkpoints (attempt work saved on reset/drain)")
    cck.add_argument("task", help="task id")
    cck.add_argument("--taskfile", default=None,
                     help="the taskfile that planned it (default: look the id up in the database)")
    cck.add_argument("--json", action="store_true", help="emit the list as JSON")
    cck.add_argument("--db", default=None, help="sqlite database path")
    cck.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    crs = code_sub.add_parser(
        "restore", help="apply a task's latest (or named) checkpoint into its worktree")
    crs.add_argument("task", help="task id")
    crs.add_argument("--checkpoint", default=None,
                     help="a specific checkpoint .patch (default: the latest)")
    crs.add_argument("--taskfile", default=None,
                     help="the taskfile that planned it (default: look the id up in the database)")
    crs.add_argument("--db", default=None, help="sqlite database path")
    crs.add_argument("-v", "--verbose", action="store_true", help="debug logging")
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
    cx_p = code_sub.add_parser(
        "context", help="print a task's durable dossier (handoff context)")
    cx_p.add_argument("task", help="task id")
    cx_p.add_argument("--taskfile", default=None,
                      help="taskfile the task belongs to (default: newest dossier for the id)")
    cx_p.add_argument("--json", action="store_true", help="emit the raw dossier JSON")
    cx_p.add_argument("--note", default=None,
                      help="inject an operator/captain note before printing")
    cx_p.add_argument("--author", default="operator", help="author of --note")
    cx_p.add_argument("--db", default=None, help="sqlite database path")
    ci_p = code_sub.add_parser(
        "issues", help="GitHub issue per task: backfill a taskfile or show one task")
    ci_sub = ci_p.add_subparsers(dest="issues_cmd", required=True)
    cis = ci_sub.add_parser("sync", help="backfill issues + the epic from code_tasks rows")
    cis.add_argument("taskfile")
    cis.add_argument("--db", default=None, help="sqlite database path")
    cish = ci_sub.add_parser("show", help="print the issues recorded for a task id")
    cish.add_argument("task")
    cish.add_argument("--db", default=None, help="sqlite database path")
    cl_p = code_sub.add_parser("list", help="list every task file in the tasks dir")
    cl_p.add_argument("--json", action="store_true",
                      help="emit the same data as JSON instead of a table")
    cl_p.add_argument("--db", default=None, help="sqlite database path")
    cl_p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    cd_p = code_sub.add_parser(
        "dream", help="Dream-RSI: score exploration policies against recorded history (no models)")
    cd_p.add_argument("--taskfile", default=None,
                      help="a specific taskfile's recorded run to replay (default: every recorded run)")
    cd_p.add_argument("--workers", type=int, default=None,
                      help="batch width W a policy may open per decision (default: the graph cap)")
    cd_p.add_argument("--rounds", type=int, default=None, help="replay round limit K2")
    cd_p.add_argument("--policy", default=None,
                      help="a Python file defining `class Policy(ExplorationPolicy)` to add as a candidate")
    cd_p.add_argument("--json", action="store_true", help="emit the replay result as JSON")
    cd_p.add_argument("--db", default=None, help="sqlite database path")
    cd_p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
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
        # Planner-capable live models only: gh roles need planner permission
        # (GLM-5.3 today; DeepSeek-V4.1-Flash-thinking-max does not plan), and a
        # retired name must not be offered at all.
        gp.add_argument("--model", choices=[m for m, roles in config.MODEL_ROLES.items()
                                            if "planner" in roles], default=None,
                        help=f"agent model (default {config.GH_MODEL})")
        gp.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    # --- studio: the 3D multiplayer game workload (ARC_FLEET=studio) -------
    st_studio = sub.add_parser(
        "studio",
        help="3D game workload: phases, visual judge, 3D operator, fuzz swarm")
    ss = st_studio.add_subparsers(dest="studio_cmd", required=True)

    sd = ss.add_parser("doctor", help="what the studio can and cannot do here")
    sd.add_argument("--json", action="store_true")
    sd.add_argument("--no-probe", action="store_true",
                    help="skip the OpenRouter model probe (works offline)")

    sp = ss.add_parser("provision",
                       help="add the studio models to the opencode config")
    sp.add_argument("--write", action="store_true",
                    help="apply the change (the config is backed up first)")

    sc = ss.add_parser("scaffold", help="write a Godot graybox starter project")
    sc.add_argument("repo")
    sc.add_argument("--project", default="prison-escape")
    sc.add_argument("--force", action="store_true",
                    help="overwrite files that already exist")

    spl = ss.add_parser("plan", help="plan one phase's tasks into a taskfile")
    spl.add_argument("goal")
    spl.add_argument("repo")
    spl.add_argument("--project", default="prison-escape")
    spl.add_argument("--phase", default="",
                     help="default: the project's current phase")
    spl.add_argument("--out", default="")

    for name, helptext in (("status", "phase, gate and round summary"),
                           ("gate", "run the current phase gate"),
                           ("budget", "studio spend so far")):
        q = ss.add_parser(name, help=helptext)
        if name != "budget":
            q.add_argument("project")
            q.add_argument("repo")
        if name == "gate":
            q.add_argument("--phase", default="")

    spr = ss.add_parser("promote", help="advance to the next phase if the gate passes")
    spr.add_argument("project")
    spr.add_argument("repo")
    spr.add_argument("--force", action="store_true",
                     help="promote over a FAILING gate; recorded forever")
    spr.add_argument("--reason", default="")

    sr = ss.add_parser("render", help="render a round's cameras (needs a display)")
    sr.add_argument("project")
    sr.add_argument("repo")
    sr.add_argument("--round", type=int, default=0)
    sr.add_argument("--phase", default="")
    sr.add_argument("--scene", default="", help="scene to render (default: main)")
    sr.add_argument("--resolution", default="1600x900")

    sj = ss.add_parser("judge", help="score the latest rendered round (blind)")
    sj.add_argument("project")
    sj.add_argument("repo")
    sj.add_argument("--round", type=int, default=0)
    sj.add_argument("--phase", default="")
    sj.add_argument("--model", default="",
                    help="override the round's rotation pick")

    sf = ss.add_parser("fuzz", help="run the headless multiplayer fuzz swarm")
    sf.add_argument("project")
    sf.add_argument("repo")
    sf.add_argument("--bots", type=int, default=None)
    sf.add_argument("--seconds", type=float, default=None)

    spt = ss.add_parser("playtest", help="run the scripted playtest (measure + look)")
    spt.add_argument("project")
    spt.add_argument("repo")

    spp = ss.add_parser("play", help="launch a build for a HUMAN playtest (F8 = finding)")
    spp.add_argument("project")
    spp.add_argument("--build", default="",
                     help="main (default) or task/<id>, as the dashboard lists them")

    sfi = ss.add_parser("findings", help="human playtest findings for a project")
    sfi.add_argument("project")
    sfi.add_argument("--state", default="", choices=("", "new", "accepted", "wontfix",
                                                      "duplicate", "fixed", "verified",
                                                      "reopened"))

    sap = ss.add_parser("approve", help="approve (or --reject) a workbench asset")
    sap.add_argument("project")
    sap.add_argument("asset", help="asset file name, as the workbench lists it")
    sap.add_argument("--reject", default="", metavar="REASON")

    srp = ss.add_parser("review-pack",
                        help="write a paste-ready review of a PR for Antigravity/Cursor")
    srp.add_argument("repo")
    srp.add_argument("pr", type=int)
    srp.add_argument("--out", default="")

    sa = ss.add_parser("astra", help="run the 3D/animation operator on a goal")
    sa.add_argument("goal")
    sa.add_argument("--project", default="prison-escape")
    sa.add_argument("--max-steps", type=int, default=24, dest="max_steps")

    chat_p = sub.add_parser(
        "chat", help="conversational planning with the fleet's planner")
    chat_p.add_argument("--session", required=True,
                        help="session id, ^[a-z0-9][a-z0-9-]{0,39}$")
    chat_p.add_argument("--repo", required=True,
                        help="absolute repo path under ARC_REPO_ROOT (default: your home)")
    chat_p.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    cap_p = sub.add_parser(
        "captain", help="supervise the fleet conversationally (state-aware, bounded actions)")
    cap_p.add_argument("--session",
                       help="session id, ^[a-z0-9][a-z0-9-]{0,39}$ (conversational turn)")
    cap_p.add_argument("--repo",
                       help="absolute repo path under ARC_REPO_ROOT (default: your home)")
    cap_p.add_argument("--autopilot", action="store_true",
                       help="run the autonomous project manager (captain_autopilot.py)")
    cap_p.add_argument("--interval", type=int, default=None,
                       help="autopilot: seconds between ticks (default ARC_CAPTAIN_INTERVAL=600)")
    cap_p.add_argument("--once", action="store_true", help="autopilot: one tick, then exit")
    cap_p.add_argument("--dry-run", action="store_true",
                       help="autopilot: log decisions without acting")
    cap_p.add_argument("--no-llm", action="store_true",
                       help="autopilot: playbooks only, never call the model")
    cap_p.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    board_p = sub.add_parser(
        "board", help="agent coordination board: post, read, claims (docs/agent-board.md)")
    board_sub = board_p.add_subparsers(dest="board_cmd", required=True)
    bpo = board_sub.add_parser("post", help="post one message to the board")
    bpo.add_argument("--project", help="default: inferred from a ~/worktrees/<project>/<task> cwd")
    bpo.add_argument("--as", dest="author", required=True,
                     help="'<task_id>/<role>' or a bare role (captain, operator, planner)")
    bpo.add_argument("--channel", default="project",
                     help="project | task:<id> | dm:<agent> | captain | operator")
    bpo.add_argument("--kind", default="note", help="one of agentboard.KINDS")
    bpo.add_argument("--mention", action="append", default=[], help="repeatable")
    bpo.add_argument("--reply-to", dest="reply_to")
    bpo.add_argument("body")
    brd = board_sub.add_parser("read", help="print a channel thread or an agent's inbox")
    brd.add_argument("--project")
    brd.add_argument("--channel")
    brd.add_argument("--for", dest="reader", help="an agent: print its inbox instead")
    brd.add_argument("--since", type=float)
    bcl = board_sub.add_parser("claims", help="list live path claims")
    bcl.add_argument("--project")
    for bp in (bpo, brd, bcl):
        # Agents run this from a task worktree, where config.DB_PATH would
        # resolve to <worktree>/orchestrator.db. The prompt passes the fleet's.
        bp.add_argument("--db", default=None, help="sqlite database path")

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
    elif args.cmd == "studio":
        from studio import cli as studio_cli
        sys.exit(studio_cli.run(args))
    elif args.cmd == "chat":
        import orchchat
        sys.exit(asyncio.run(orchchat.run_turn(args.session, args.repo)))
    elif args.cmd == "board":
        sys.exit(cmd_board(args))
    elif args.cmd == "captain":
        if args.autopilot:
            import captain_autopilot
            sys.exit(captain_autopilot.run(
                interval=args.interval or captain_autopilot.INTERVAL_S,
                once=args.once, dry_run=args.dry_run, llm=not args.no_llm))
        if not args.session or not args.repo:
            ap.error("captain: --session and --repo are required without --autopilot")
        import captain
        sys.exit(asyncio.run(captain.run_turn(args.session, args.repo)))


if __name__ == "__main__":
    main()