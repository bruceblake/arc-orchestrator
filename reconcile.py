"""Reap the wreckage a killed run leaves behind.

A `code run` that dies (killed queue, Ctrl-C, OOM, crash) leaves three kinds of
orphan the next run trips over:

  * code_tasks rows still marked 'running' — they make the dashboard show work
    that is not happening, and the resume planner treats them as failures;
  * driver_leases rows — they count against the per-model cap until their TTL
    expires, so the fleet throttles itself against ghosts;
  * git worktrees + task branches — they accumulate under ~/worktrees and hold
    a checkout of every interrupted attempt.

Worktrees whose branch still has commits main does not are NEVER removed: that
is reviewed work the conflict-repair path in code_tasks.publish can still land.
"""
import os
from pathlib import Path

import config
import gitstore

INTERRUPTED_REASON = "interrupted: run process exited before the task finished"


def live_runs():
    """[{'pid', 'taskfile'}] for every `main.py code run` alive on this machine.

    Matches on real argv token ORDER, not a substring of the whole command
    line: a shell wrapper or this very process can easily contain the words
    "main.py", "code" and "run" scattered across unrelated arguments.
    """
    me = os.getpid()
    runs = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            argv = [a for a in (entry / "cmdline").read_bytes()
                    .decode(errors="replace").split("\0") if a]
        except OSError:
            continue
        for i, arg in enumerate(argv):
            if Path(arg).name != "main.py":
                continue
            if argv[i + 1:i + 3] == ["code", "run"]:
                rest = [a for a in argv[i + 3:] if not a.startswith("-")]
                runs.append({"pid": int(entry.name),
                             "taskfile": rest[0] if rest else None})
            break
    return runs


def live_run_pids():
    return [r["pid"] for r in live_runs()]


def _repos_from_rows(rows):
    """Distinct repo paths implied by worktree paths recorded on task rows."""
    repos = set()
    for r in rows:
        wt = r.get("worktree")
        if wt:
            repos.add(Path(wt).parent.name)
    return repos


async def _worktree_state(repo, task_id, wt):
    """What a worktree still holds: 'dirty' | 'ahead' | 'merged' | 'absent'.

    'dirty' comes first and outranks everything. An interrupted implement
    leaves the agent's edits UNCOMMITTED in the worktree — the orchestrator
    only commits at publish — so a branch can be level with main and the
    worktree still hold every line the agent wrote. Counting commits alone
    would have called that "merged" and deleted the work.
    """
    rc, st, _ = await gitstore._git(["status", "--porcelain"], cwd=wt, check=False)
    if rc == 0 and st.strip():
        return "dirty"
    branch = f"task/{task_id}"
    rc, _, _ = await gitstore._git(["rev-parse", "--verify", branch],
                                   cwd=repo, check=False)
    if rc != 0:
        return "absent"
    return "ahead" if await gitstore.branch_ahead(repo, task_id) else "merged"


async def reconcile(store, *, repos=None, apply=True, force=False):
    """Return a report dict; mutates only when `apply`.

    Refuses to touch anything while a `code run` process is alive unless
    `force`, because its rows and worktrees are legitimately in use.
    """
    report = {"live_runs": live_run_pids(), "rows": [], "leases": 0,
              "worktrees": [], "kept": [], "skipped": False}
    if report["live_runs"] and not force:
        report["skipped"] = True
        return report

    if apply:
        report["leases"] = store.reap_driver_leases(config.DRIVER_LEASE_TTL)
    else:
        now_dead = 0
        for r in store.driver_lease_rows():
            try:
                os.kill(r["pid"], 0)
            except OSError:
                now_dead += 1
        report["leases"] = now_dead

    running = store.running_code_tasks()
    report["rows"] = [{"id": r["id"], "taskfile": Path(r["taskfile"]).name,
                       "model": r["model"], "worktree": r.get("worktree")}
                      for r in running]
    if apply and running:
        store.reset_stale_code_tasks(reason=INTERRUPTED_REASON)

    # Worktree sweep: every directory under <root>/<repo-name>/<task-id>.
    root = Path(config.WORKTREE_ROOT)
    if not root.is_dir():
        return report
    wanted = set(repos or [])
    for repo_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if wanted and repo_dir.name not in wanted:
            continue
        repo = _find_repo(repo_dir.name)
        if repo is None:
            continue
        for wt in sorted(p for p in repo_dir.iterdir() if p.is_dir()):
            tid = wt.name
            state = await _worktree_state(repo, tid, wt)
            if state in ("dirty", "ahead"):
                report["kept"].append({
                    "task": tid, "repo": repo.name, "path": str(wt),
                    "reason": ("worktree has uncommitted agent edits"
                               if state == "dirty" else
                               "branch has unmerged commits")})
                continue
            report["worktrees"].append({"task": tid, "repo": repo.name,
                                        "branch": state})
            if apply:
                await gitstore.cleanup(repo, tid)
    return report


def _find_repo(name):
    """Locate the blessed clone for a worktree directory name."""
    if not name or Path(name).name != name:
        return None  # refuse anything but a plain directory name (no traversal)
    for cand in (Path(config.ROOT).parent / name, Path.home() / "repos" / name,
                 Path(config.ROOT) if Path(config.ROOT).name == name else None):
        if cand and (cand / ".git").exists():
            return cand.resolve()
    return None


def format_report(rep):
    out = []
    if rep["skipped"]:
        out.append(f"SKIPPED — {len(rep['live_runs'])} code-run process(es) still "
                   f"alive: {rep['live_runs']}")
        out.append("stop them first, or pass --force if you know they are wedged.")
        return "\n".join(out)
    out.append(f"driver leases reaped : {rep['leases']}")
    out.append(f"stale 'running' rows : {len(rep['rows'])}")
    for r in rep["rows"]:
        out.append(f"    {r['id']:<28} {r['taskfile']:<38} {r['model']}")
    out.append(f"worktrees removed    : {len(rep['worktrees'])}")
    for w in rep["worktrees"]:
        out.append(f"    {w['repo']}/{w['task']}  (branch {w['branch']})")
    if rep["kept"]:
        out.append(f"worktrees KEPT       : {len(rep['kept'])} — unmerged work, "
                   f"resume the taskfile to land it")
        for w in rep["kept"]:
            out.append(f"    {w['repo']}/{w['task']}  ({w['reason']})")
    return "\n".join(out)
