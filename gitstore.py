"""Blessed-clone + worktree lifecycle for the multi-harness code workload.

The blessed clone (~/repos/<project>) keeps main clean; every task runs in
~/worktrees/<project>/<task-id> on branch task/<task-id>. The orchestrator is
the only git actor — harnesses only write files inside their worktree.
"""
import asyncio
from pathlib import Path

import config

GIT_TIMEOUT = 60


class GitError(RuntimeError):
    pass


async def _git(args, cwd, check=True):
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), GIT_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise GitError(f"git {' '.join(args[:3])} timed out in {cwd}")
    text_out, text_err = out.decode(errors="replace"), err.decode(errors="replace")
    if check and proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args[:4])} failed ({proc.returncode}) in {cwd}: "
            f"{text_err.strip()[:400]}"
        )
    return proc.returncode, text_out, text_err


async def _ensure_identity(repo):
    for key, val in (("user.name", "arc-orchestrator"),
                     ("user.email", "arc-orchestrator@localhost")):
        rc, out, _ = await _git(["config", "--get", key], cwd=repo, check=False)
        if rc != 0 or not out.strip():
            await _git(["config", key, val], cwd=repo)


async def alloc(repo, task_id, base="main"):
    """Create (or recreate, on retry) the task worktree; returns its Path."""
    repo = Path(repo).resolve()
    wt = Path(config.WORKTREE_ROOT) / repo.name / task_id
    branch = f"task/{task_id}"
    await _ensure_identity(repo)
    if wt.exists():
        await _git(["worktree", "remove", "--force", str(wt)], cwd=repo, check=False)
    rc, remotes, _ = await _git(["remote"], cwd=repo, check=False)
    base_ref = "origin/main" if remotes.strip() else base
    rc, _, _ = await _git(["rev-parse", "--verify", branch], cwd=repo, check=False)
    if rc == 0:
        await _git(["worktree", "add", "--force", str(wt), branch], cwd=repo)
    else:
        await _git(["worktree", "add", "-b", branch, str(wt), base_ref], cwd=repo)
    return wt


async def diff_stat(wt):
    _, st, _ = await _git(["status", "--porcelain"], cwd=wt)
    _, ds, _ = await _git(["diff", "--stat", "HEAD"], cwd=wt, check=False)
    parts = [p for p in (st.strip(), ds.strip()) if p]
    return "\n".join(parts) or "(clean)"


async def diff_full(wt, base, max_chars=24000):
    """Working-tree diff vs base for review; includes untracked files."""
    await _git(["add", "-A", "-N"], cwd=wt, check=False)  # intent-to-add
    _, diff, _ = await _git(["diff", base], cwd=wt, check=False)
    if len(diff) > max_chars:
        diff = diff[:max_chars] + f"\n... [truncated at {max_chars} chars]"
    return diff or "(empty diff)"


async def publish(wt, message, trailers=None):
    """Commit all changes in the worktree; returns commit hash, None if clean."""
    _, st, _ = await _git(["status", "--porcelain"], cwd=wt)
    if not st.strip():
        return None
    await _git(["add", "-A"], cwd=wt)
    args = ["commit", "-q", "-m", message]
    if trailers:
        args += ["-m", "\n".join(f"{k}: {v}" for k, v in trailers.items())]
    await _git(args, cwd=wt)
    _, head, _ = await _git(["rev-parse", "HEAD"], cwd=wt)
    return head.strip()


async def merge_to_main(repo, task_id):
    repo = Path(repo).resolve()
    _, cur, _ = await _git(["branch", "--show-current"], cwd=repo)
    if cur.strip() != "main":
        await _git(["checkout", "main"], cwd=repo)
    rc, _, err = await _git(
        ["merge", "--no-ff", "-m", f"merge task/{task_id}", f"task/{task_id}"],
        cwd=repo, check=False,
    )
    if rc != 0:
        raise GitError(f"merge task/{task_id} failed: {err.strip()[:300]}")


async def cleanup(repo, task_id, delete_branch=True):
    repo = Path(repo).resolve()
    wt = Path(config.WORKTREE_ROOT) / repo.name / task_id
    if wt.exists():
        await _git(["worktree", "remove", "--force", str(wt)], cwd=repo, check=False)
    if delete_branch:
        await _git(["branch", "-d", f"task/{task_id}"], cwd=repo, check=False)
