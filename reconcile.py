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
import json
import os
from pathlib import Path

import config
import gitstore

INTERRUPTED_REASON = "interrupted: run process exited before the task finished"


def _ancestry(pid, limit=12):
    """{pid} plus its parents, walking /proc up to init."""
    out, seen = {pid}, 0
    while pid > 1 and seen < limit:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            pid = int(stat[stat.rindex(")") + 2:].split()[1])   # ppid
        except (OSError, ValueError, IndexError):
            break
        out.add(pid)
        seen += 1
    return out


def live_runs():
    """[{'pid', 'taskfile'}] for every `main.py code run` alive on this machine.

    Matches on real argv token ORDER, not a substring of the whole command
    line: a shell wrapper or this very process can easily contain the words
    "main.py", "code" and "run" scattered across unrelated arguments.
    """
    me = os.getpid()
    # A process in our own ANCESTRY is not a competing run: `timeout 60 python
    # main.py code run <file>`, `nohup`, `nice` and friends keep the whole
    # command in their argv, so the wrapper that launched THIS run matches the
    # pattern and the run refuses to start against itself. Verified: a launch
    # under `timeout` reported "already being run by pid <the timeout>".
    mine = _ancestry(me)
    runs = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) in mine:
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
                cwd = None
                try:
                    cwd = os.readlink(entry / "cwd")
                except OSError:
                    pass
                runs.append({"pid": int(entry.name),
                             "taskfile": rest[0] if rest else None,
                             "cwd": cwd})
            break
    return runs


def _proc_cwd(pid):
    """Process working directory, or None if it cannot be read."""
    if not pid:
        return None
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


class LiveTaskfileMatcher:
    """Matches taskfile paths against currently live code-run processes,
    resolving relative argv against each process's cwd from /proc/<pid>/cwd."""

    def __init__(self, runs=None):
        if runs is None:
            runs = live_runs()
        self.runs = runs
        self.live_resolved = set()
        self.unresolved_tokens = set()
        self.unresolved_basenames = set()
        self.live_raw_tokens = set()
        self.live_taskfiles = set()

        for r in runs:
            tf_arg = r.get("taskfile")
            if not tf_arg:
                continue
            self.live_raw_tokens.add(tf_arg)
            pid = r.get("pid")
            cwd = r.get("cwd") or _proc_cwd(pid)
            if cwd:
                try:
                    resolved = str((Path(cwd) / tf_arg).resolve())
                    self.live_resolved.add(resolved)
                    self.live_taskfiles.add(resolved)
                except OSError:
                    self.live_taskfiles.add(tf_arg)
            else:
                # Cannot read /proc/<pid>/cwd: do not guess, do not reset or settle that pid's taskfile.
                self.unresolved_tokens.add(tf_arg)
                self.unresolved_basenames.add(Path(tf_arg).name)
                self.live_taskfiles.add(tf_arg)
                try:
                    if Path(tf_arg).is_absolute():
                        self.live_resolved.add(str(Path(tf_arg).resolve()))
                except OSError:
                    pass

    def is_live(self, tf):
        """Return True if tf matches an active code-run process."""
        if not tf:
            return False
        tf_str = str(tf)
        try:
            if str(Path(tf).resolve()) in self.live_resolved:
                return True
        except OSError:
            pass
        if tf_str in self.live_raw_tokens or tf_str in self.unresolved_tokens:
            return True
        try:
            if Path(tf).name in self.unresolved_basenames:
                return True
        except OSError:
            pass
        return False

    def matching_pids(self, tf):
        """Return list of pids for live runs matching taskfile tf."""
        if not tf:
            return []
        tf_str = str(tf)
        try:
            target_resolved = str(Path(tf).resolve())
            target_name = Path(tf).name
        except OSError:
            target_resolved = tf_str
            target_name = tf_str

        pids = []
        for r in self.runs:
            tf_arg = r.get("taskfile")
            if not tf_arg:
                continue
            pid = r.get("pid")
            cwd = r.get("cwd") or _proc_cwd(pid)
            matched = False
            if cwd:
                try:
                    run_resolved = str((Path(cwd) / tf_arg).resolve())
                    if run_resolved == target_resolved or tf_arg == tf_str:
                        matched = True
                except OSError:
                    if tf_arg == tf_str:
                        matched = True
            else:
                tf_arg_p = Path(tf_arg)
                if (tf_arg == tf_str or
                        tf_arg_p.name == target_name or
                        (tf_arg_p.is_absolute() and
                         str(tf_arg_p.resolve()) == target_resolved)):
                    matched = True
            if matched and pid is not None and pid not in pids:
                pids.append(pid)
        return pids

    def __contains__(self, tf):
        return self.is_live(tf)

    def __call__(self, tf):
        return self.is_live(tf)

    def __len__(self):
        return len(self.live_taskfiles)


def live_taskfile_matcher(runs=None):
    return LiveTaskfileMatcher(runs)


def is_live_taskfile(taskfile, runs=None):
    return LiveTaskfileMatcher(runs).is_live(taskfile)


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


def _taskfile_exists(path):
    """Check if taskfile exists either directly or under TASKS_DIR."""
    if not path:
        return False
    p = Path(path)
    try:
        if p.exists():
            return True
        if not p.is_absolute() and (Path(config.TASKS_DIR) / p).exists():
            return True
    except OSError:
        return False
    return False


async def reconcile(store, *, repos=None, apply=True, force=False):
    """Return a report dict; mutates only when `apply`.

    While a `code run` process is alive, destructive git operations (worktree
    deletion and branch cleanup) are skipped, but safe bookkeeping is performed
    for taskfiles that are not the live run's taskfile: dead-pid driver leases
    are reaped, running rows whose taskfile does not exist on disk are reset to
    interrupted, and in_review/conflict rows whose PR is already merged on GitHub
    are settled without deleting worktrees. Pass `force=True` to run the full
    sweep including worktree cleanup even when live runs are detected.
    """
    report = {"live_runs": live_run_pids(), "rows": [], "rows_kept": [],
              "merged_settled": [], "leases": 0, "worktrees": [], "kept": [],
              "skipped": False}
    is_live = bool(report["live_runs"] and not force)
    if is_live:
        report["skipped"] = True

    if apply:
        report["leases"] = store.reap_driver_leases(
            float("inf") if is_live else config.DRIVER_LEASE_TTL
        )
    else:
        now_dead = 0
        for r in store.driver_lease_rows():
            try:
                os.kill(r["pid"], 0)
            except OSError:
                now_dead += 1
        report["leases"] = now_dead

    matcher = LiveTaskfileMatcher(live_runs())
    _is_live = matcher.is_live

    running = store.running_code_tasks()
    orphaned = []
    kept = []
    for r in running:
        tf = r.get("taskfile")
        if _is_live(tf):
            kept.append(r)
        elif is_live and _taskfile_exists(tf):
            kept.append(r)
        else:
            orphaned.append(r)

    report["rows"] = [{"id": r["id"], "taskfile": Path(r["taskfile"]).name if r.get("taskfile") else "",
                       "model": r["model"], "worktree": r.get("worktree"),
                       "_taskfile": r.get("taskfile")}
                      for r in orphaned]
    report["rows_kept"] = [
        {"id": r["id"], "taskfile": Path(r["taskfile"]).name if r.get("taskfile") else ""}
        for r in kept]
    if apply:
        # Group by the RAW taskfile string the row stores — a resolved
        # absolute path would not match the WHERE clause.
        for tf in sorted({r["taskfile"] for r in orphaned if r.get("taskfile")}):
            store.reset_stale_code_tasks(taskfile=tf, reason=INTERRUPTED_REASON)
    for r in report["rows"]:
        r.pop("_taskfile", None)

    # Settle in_review and conflict rows whose PR is already MERGED on GitHub: the run
    # that would have written 'merged' died first, and a resume alone would
    # need a live model just to do bookkeeping. Best-effort — no gh / no
    # remote means leave the row for the normal resume path.
    report["merged_settled"] = []
    for status in ("in_review", "conflict"):
        for row in store.code_tasks_with_status(status):
            tf = row.get("taskfile")
            if _is_live(tf):
                continue  # a live run owns this row's taskfile
            wt = row.get("worktree")
            repo = _find_repo(Path(wt).parent.name) if wt else None
            if repo is None and tf and _taskfile_exists(tf):
                try:
                    data = json.loads(Path(tf).read_text(encoding="utf-8"))
                    repo_path = (data.get("project") or {}).get("repo")
                    if repo_path:
                        repo = _find_repo(Path(repo_path).name)
                except Exception:
                    pass
            if repo is None:
                continue
            number, url, pr_st = await gitstore.find_pr(repo, row["id"],
                                                        state="all")
            if (pr_st or "").upper() != "MERGED":
                continue
            if apply:
                store.upsert_code_task(row["taskfile"], row["id"], row["title"],
                                       row["model"], row["reviewer"], "merged",
                                       finished=True)
                if not is_live:
                    await gitstore.cleanup(repo, row["id"])
            report["merged_settled"].append(
                {"id": row["id"], "pr": number, "url": url})

    if is_live:
        return report

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
        actions_taken = bool(rep["leases"] or rep["rows"] or rep.get("merged_settled"))
        if actions_taken:
            out.append(f"SKIPPED (destructive sweep) — {len(rep['live_runs'])} code-run process(es) still "
                       f"alive: {rep['live_runs']}")
            out.append("safe bookkeeping completed (worktrees kept); pass --force to sweep worktrees.")
        else:
            out.append(f"SKIPPED — {len(rep['live_runs'])} code-run process(es) still "
                       f"alive: {rep['live_runs']}")
            out.append("stop them first, or pass --force if you know they are wedged.")
    out.append(f"driver leases reaped : {rep['leases']}")
    out.append(f"stale 'running' rows : {len(rep['rows'])}")
    for r in rep["rows"]:
        out.append(f"    {r['id']:<28} {r['taskfile']:<38} {r['model']}")
    if rep.get("rows_kept"):
        reason = ("their taskfile still has a live run or exists on disk"
                  if rep["skipped"] else
                  "their taskfile still has a live run")
        out.append(f"'running' left alone : {len(rep['rows_kept'])} — {reason}")
        for r in rep["rows_kept"]:
            out.append(f"    {r['id']:<28} {r['taskfile']}")
    if rep.get("merged_settled"):
        out.append(f"in_review/conflict settled : {len(rep['merged_settled'])} — "
                   "PR already merged on GitHub")
        for m in rep["merged_settled"]:
            out.append(f"    {m['id']:<28} PR #{m['pr']}")
    if not rep["skipped"]:
        out.append(f"worktrees removed    : {len(rep['worktrees'])}")
        for w in rep["worktrees"]:
            out.append(f"    {w['repo']}/{w['task']}  (branch {w['branch']})")
        if rep["kept"]:
            out.append(f"worktrees KEPT       : {len(rep['kept'])} — unmerged work, "
                       f"resume the taskfile to land it")
            for w in rep["kept"]:
                out.append(f"    {w['repo']}/{w['task']}  ({w['reason']})")
    return "\n".join(out)
