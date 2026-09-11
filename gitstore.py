"""Blessed-clone + worktree lifecycle for the multi-harness code workload.

The blessed clone (~/repos/<project>) keeps main clean; every task runs in
~/worktrees/<project>/<task-id> on branch task/<task-id>. The orchestrator is
the only git actor — harnesses only write files inside their worktree.
"""
import asyncio
import re
import json
import logging
from pathlib import Path

import config
import events

log = logging.getLogger("gitstore")

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
            try:
                await _git(["config", key, val], cwd=repo)
            except GitError:
                # parallel allocs on a fresh repo race to write .git/config;
                # re-check instead of failing — another writer may have won
                rc, out, _ = await _git(["config", "--get", key],
                                        cwd=repo, check=False)
                if rc != 0 or not out.strip():
                    raise


async def _base_ref(repo, base="main"):
    """The ref a task branches from — the LOCAL base, always.

    This used to prefer origin/<base> whenever any remote existed. That is
    wrong for this system: merge_to_main merges into the LOCAL branch and the
    push is best-effort, so the moment a push is skipped or fails, local main
    is ahead and every new task would branch from a stale origin — silently
    reverting merged work the next time it published.

    origin is a publishing target here, not the source of truth. If origin is
    genuinely ahead (someone pushed elsewhere), that is a real divergence and
    it is reported rather than silently preferred either way.
    """
    rc, out, _ = await _git(["rev-parse", "--verify", base], cwd=repo, check=False)
    if rc != 0:
        return base
    rc, remotes, _ = await _git(["remote"], cwd=repo, check=False)
    if remotes.strip():
        rc, ahead, _ = await _git(
            ["rev-list", "--count", f"{base}..origin/{base}"], cwd=repo, check=False)
        if rc == 0 and (ahead.strip() or "0").isdigit() and int(ahead.strip() or 0):
            import events
            events.emit("git.remote_ahead", repo=str(repo), base=base,
                        commits=int(ahead.strip()),
                        note="origin is ahead of local; tasks still branch from "
                             "local — pull before running to avoid diverging")
    return base


def worktree_for(repo, task_id):
    """Where alloc() puts this task's worktree, whether or not it exists yet."""
    return Path(config.WORKTREE_ROOT) / Path(repo).resolve().name / task_id


async def existing_worktree(repo, task_id):
    """The task's worktree if it is on disk AND git still tracks it, else None.

    Resuming a task whose PR is already open must reuse the branch that PR was
    opened from. A bare directory check is not enough: `git worktree remove`
    leaves nothing behind, but a killed run can leave a directory git no longer
    lists, and committing in one of those fails in a confusing way later.
    """
    wt = worktree_for(repo, task_id)
    if not (wt / ".git").exists():
        return None
    rc, out, _ = await _git(["worktree", "list", "--porcelain"],
                            cwd=Path(repo).resolve(), check=False)
    if rc != 0 or str(wt) not in out:
        return None
    return wt


async def alloc(repo, task_id, base="main"):
    """Create (or recreate, on retry) the task worktree; returns its Path.

    A re-alloc always resets task/<task_id> to the base: a failed/crashed
    attempt's branch holds rejected work and must not leak into the retry.
    (The conflict-repair path in code_tasks.publish merges the old branch
    BEFORE alloc runs, so reviewed commits are never discarded silently.)"""
    repo = Path(repo).resolve()
    wt = Path(config.WORKTREE_ROOT) / repo.name / task_id
    if not wt.resolve().is_relative_to(
            Path(config.WORKTREE_ROOT).resolve() / repo.name):
        raise ValueError(f"task id {task_id!r} escapes WORKTREE_ROOT")
    branch = f"task/{task_id}"
    await _ensure_identity(repo)
    base_ref = await _base_ref(repo, base)
    # Resetting task/<id> to base DISCARDS whatever is on it. That is correct
    # for a retry of rejected work, and catastrophic for a branch whose PR is
    # open and reviewed — which is what happened when a resume fell through to
    # alloc: four reviewed PR branches were reset to base in one run, and only
    # survived because origin had not been force-pushed over yet. It stays
    # silent no longer.
    rc, ahead, _ = await _git(["rev-list", "--count", f"{base_ref}..{branch}"],
                              cwd=repo, check=False)
    n = int(ahead.strip()) if rc == 0 and ahead.strip().isdigit() else 0
    if n:
        events.emit("task.branch_reset", task=task_id, branch=branch,
                    commits_discarded=n, base=base_ref)
        log.warning("alloc %s: resetting %s to %s discards %d commit(s)",
                    task_id, branch, base_ref, n)
    if wt.exists():
        await _git(["worktree", "remove", "--force", str(wt)], cwd=repo, check=False)
    await _git(["worktree", "add", "--force", "-B", branch, str(wt), base_ref],
               cwd=repo)
    return wt


async def diff_stat(wt):
    _, st, _ = await _git(["status", "--porcelain"], cwd=wt)
    _, ds, _ = await _git(["diff", "--stat", "HEAD"], cwd=wt, check=False)
    parts = [p for p in (st.strip(), ds.strip()) if p]
    return "\n".join(parts) or "(clean)"


async def diff_full(wt, base, max_chars=24000):
    """Working-tree diff vs the point this task branched from; untracked included.

    Diffed against the MERGE BASE, not the live `base` ref. Merges are
    serialized but tasks run in parallel, so `main` moves forward while a task
    is still working: diffing against it made every file a sibling had merged
    since alloc appear as a deletion by THIS task. Reviewers then correctly
    rejected the change as an out-of-scope deletion, burning a fix round every
    time — the more the fleet parallelized, the more often correct work was
    rejected and escalated to a stronger model for no reason.
    """
    await _git(["add", "-A", "-N"], cwd=wt, check=False)  # intent-to-add
    rc, mb, _ = await _git(["merge-base", base, "HEAD"], cwd=wt, check=False)
    ref = mb.strip() if rc == 0 and mb.strip() else "HEAD"
    _, diff, _ = await _git(["diff", ref], cwd=wt, check=False)
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


async def _dirty_paths(repo):
    """Paths with uncommitted (tracked or untracked) changes in the repo.

    `--untracked-files=all` matters: plain --porcelain collapses a wholly
    untracked directory into one entry ("?? logs/"), so a branch adding
    "logs/harness/x.jsonl" found nothing to stash and the merge then died on
    "untracked working tree files would be overwritten by merge". In this repo
    that was 26 reported paths versus 416 actual ones.
    """
    _, st, _ = await _git(["status", "--porcelain", "--untracked-files=all"],
                          cwd=repo)
    out = set()
    for line in st.splitlines():
        if not line:
            continue
        p = line[3:]
        if " -> " in p:
            p = p.split(" -> ", 1)[1]
        out.add(p)
    return out


async def branch_ahead(repo, task_id, base=None):
    """True if task/<task_id> exists and has commits `base` does not.

    Defaults to config.BASE_BRANCH, not "main". The fleet merges into
    development; main is promoted to separately and lags it — 52 commits behind
    as this was written. Comparing against main meant a branch fully merged into
    development still counted as unmerged, so reconcile KEPT its worktree
    forever and the cleanup it exists to perform never happened. Measured:
    task/graph-admission-control was 0 commits ahead of development and 30
    ahead of main.
    """
    base = base or config.BASE_BRANCH
    # If the base branch does not resolve, we cannot know whether this branch
    # holds unmerged work — and the caller (reconcile) treats False as "merged,
    # safe to delete". Unknown must therefore report AHEAD: keeping a worktree
    # that could have been removed costs disk, deleting one that held commits
    # costs the work.
    rc, _, _ = await _git(["rev-parse", "--verify", base], cwd=repo, check=False)
    if rc != 0:
        return True
    repo = Path(repo).resolve()
    branch = f"task/{task_id}"
    rc, _, _ = await _git(["rev-parse", "--verify", branch], cwd=repo, check=False)
    if rc != 0:
        return False
    base_ref = await _base_ref(repo, base)
    rc, out, _ = await _git(["rev-list", "--count", f"{base_ref}..{branch}"],
                            cwd=repo, check=False)
    return rc == 0 and (out.strip() or "0").isdigit() and int(out.strip() or 0) > 0


async def merge_to_main(repo, task_id):
    """Merge --no-ff task branch into main; tolerates a dirty working tree.

    The blessed repo doubles as the operator's working copy, so uncommitted
    edits may exist. Files the merge needs to update that are locally
    modified are path-scoped stashed first, then restored after the merge —
    a merge no longer fails just because someone was editing an unrelated
    file the branch also touched."""
    repo = Path(repo).resolve()
    _, cur, _ = await _git(["branch", "--show-current"], cwd=repo)
    if cur.strip() != "main":
        await _git(["checkout", "main"], cwd=repo)
    branch = f"task/{task_id}"
    _, names, _ = await _git(["diff", "--name-only", f"main...{branch}"], cwd=repo)
    changed = {n.strip() for n in names.splitlines() if n.strip()}
    blocking = sorted(changed & await _dirty_paths(repo))
    stashed = False
    if blocking:
        rc, _, _ = await _git(
            ["stash", "push", "-q", "-u", "-m", f"arc-pre-merge {task_id}",
             "--", *blocking], cwd=repo, check=False)
        stashed = rc == 0
    rc, _, err = await _git(
        ["merge", "--no-ff", "-m", f"merge task/{task_id}", branch],
        cwd=repo, check=False,
    )
    if rc != 0:
        if stashed:
            await _git(["stash", "pop", "-q"], cwd=repo, check=False)
        raise GitError(f"merge {branch} failed: {err.strip()[:300]}")
    if stashed:
        rc, _, err = await _git(["stash", "pop", "-q"], cwd=repo, check=False)
        if rc != 0:
            # The merge LANDED. Failing here marked a successfully merged task
            # 'conflict', which is a lie — and the common trigger is benign:
            # the branch adds a path the operator also has as an untracked
            # local file (a log, a transcript), so git refuses to restore it
            # over the merged copy. Leave the stash for the operator and say
            # so; do not fail work that actually succeeded.
            events.emit("merge.stash_retained", task=task_id, paths=blocking,
                        error=err.strip()[:200],
                        note="merge landed; local edits are still in the stash "
                             "— inspect with `git stash list` / `git stash pop`")
            log.warning(
                "merged %s, but local edits to %s stayed in the stash (%s) — "
                "recover them with `git stash pop`",
                branch, blocking, err.strip()[:120])


async def push_and_open_pr(repo, task_id, title, taskfile=""):
    """Best-effort GitHub publish: push the branch, open a PR, then push main.

    Never raises; returns (pr_url_or_None, note). With no remote / no gh /
    no auth it reports a skip note via the events emitted by the caller —
    local merge has already landed, so this is purely additive."""
    import shutil
    repo = Path(repo).resolve()
    branch = f"task/{task_id}"
    rc, remotes, _ = await _git(["remote"], cwd=repo, check=False)
    if not remotes.strip():
        return None, "no git remote configured"
    if not shutil.which("gh"):
        return None, "gh CLI not installed"
    # ORDER MATTERS. The branch goes up first and the PR is opened while
    # origin/main still LACKS these commits; main is pushed afterwards, which
    # marks the PR merged. Pushing main first — which is what this did — left
    # GitHub with nothing between the two refs, so every `gh pr create`
    # failed with "No commits between main and task/<id>" and this hook had
    # never once opened a PR.
    try:
        await _git(["push", "-u", "origin", branch], cwd=repo)
    except GitError as exc:
        return None, f"push failed: {exc}"[:200]
    proc = await asyncio.create_subprocess_exec(
        "gh", "pr", "create", "--base", "main", "--head", branch,
        "--title", f"task({task_id}): {title}",
        "--body", f"Task `{task_id}` from `{taskfile or '?'}`\n\n"
        "Merged locally into main by the orchestrator; pushing main marks "
        "this PR merged.", cwd=str(repo),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), 60)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None, "gh pr create timed out"
    if proc.returncode != 0:
        # main still needs publishing even when the PR could not be opened.
        await _git(["push", "origin", "main"], cwd=repo, check=False)
        return None, f"gh pr create failed: {err.decode(errors='replace').strip()[:200]}"
    url = out.decode(errors="replace").strip()
    # Now publish main; GitHub sees the branch's commits land and marks the PR
    # merged, leaving a reviewable diff and the review trail behind it.
    await _git(["push", "origin", "main"], cwd=repo, check=False)
    return url, "opened"


async def github_status(repo):
    """Best-effort GitHub-readiness probe; never raises or hangs.

    Returns {'remote': <origin url or None>, 'gh_installed': bool,
    'gh_authed': bool, 'ready': bool, 'reason': <short text or None>}.
    Explains why push_and_open_pr skipped, e.g. for a pr_skipped event."""
    import shutil
    repo = Path(repo).resolve()
    info = {"remote": None, "gh_installed": False,
            "gh_authed": False, "ready": False, "reason": None}
    rc, out, _ = await _git(["remote", "get-url", "origin"], cwd=repo, check=False)
    if rc == 0 and out.strip():
        info["remote"] = out.strip()
    else:
        info["reason"] = "no git remote configured"
        return info
    if not shutil.which("gh"):
        info["reason"] = "gh CLI not installed"
        return info
    info["gh_installed"] = True
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "auth", "status", cwd=str(repo),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            await asyncio.wait_for(proc.communicate(), 60)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            info["reason"] = "gh auth status timed out"
            return info
    except OSError as exc:
        info["reason"] = f"gh auth status failed: {exc}"[:200]
        return info
    if proc.returncode != 0:
        info["reason"] = "gh not authenticated"
        return info
    info["gh_authed"] = True
    info["ready"] = True
    return info


async def cleanup(repo, task_id, delete_branch=True):
    repo = Path(repo).resolve()
    wt = Path(config.WORKTREE_ROOT) / repo.name / task_id
    if wt.exists():
        await _git(["worktree", "remove", "--force", str(wt)], cwd=repo, check=False)
    if delete_branch:
        await _git(["branch", "-d", f"task/{task_id}"], cwd=repo, check=False)


# --- pull-request flow -------------------------------------------------------
# The PR is the gate. A task branch is pushed and a pull request opened against
# config.BASE_BRANCH; reviewers read the real PR diff; a merger merges it only
# once every reviewer approves. Nothing is merged locally, so "send it back"
# actually withholds the change instead of commenting on history.

async def _gh(args, cwd, timeout=90):
    """Run gh; returns (rc, stdout, stderr). Never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", *args, cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return 124, "", f"gh {' '.join(args[:3])} timed out after {timeout}s"
    except OSError as exc:
        return 127, "", str(exc)


async def ensure_base_branch(repo, base=None, prod=None):
    """Make sure the integration branch exists locally and on origin."""
    repo = Path(repo).resolve()
    base = base or config.BASE_BRANCH
    prod = prod or config.PROD_BRANCH
    rc, _, _ = await _git(["rev-parse", "--verify", base], cwd=repo, check=False)
    if rc != 0:
        await _git(["branch", base, prod], cwd=repo)
    rc, remotes, _ = await _git(["remote"], cwd=repo, check=False)
    if remotes.strip():
        await _git(["push", "-u", "origin", base], cwd=repo, check=False)
    return base


async def fast_forward_base(repo, base=None):
    """Move the local base branch to what origin has. Returns (ok, note).

    `git update-ref refs/heads/<base>` is only safe while <base> is NOT the
    checked-out branch: it moves the pointer without touching the index or
    working tree, so doing it to the current branch makes every file in the
    repo appear massively modified or deleted. That was fine while the
    operator's checkout was always `main` and the base was always
    `development`, and it becomes a foot-gun the moment anyone works ON the
    integration branch — which the branch model actively encourages.
    """
    repo = Path(repo).resolve()
    base = base or config.BASE_BRANCH
    await _git(["fetch", "origin", base], cwd=repo, check=False)
    rc, cur, _ = await _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, check=False)
    if rc == 0 and cur.strip() == base:
        rc, _, err = await _git(["merge", "--ff-only", f"origin/{base}"],
                                cwd=repo, check=False)
        if rc == 0:
            return True, "fast-forwarded the checked-out base"
        return False, f"base is checked out and not fast-forwardable: {err.strip()[:160]}"
    rc, _, err = await _git(["update-ref", f"refs/heads/{base}", f"origin/{base}"],
                            cwd=repo, check=False)
    return rc == 0, "updated" if rc == 0 else err.strip()[:160]


async def merge_in_progress(wt):
    """(True, unmerged_paths) when this worktree is mid-merge.

    Distinguishes "the agent resolved every conflict and staged them" from
    "there are still conflicts to fix", which are the same MERGE_HEAD state to
    git but opposite outcomes for the pipeline.
    """
    wt = Path(wt)
    if not (wt / ".git").exists():
        return False, []
    rc, out, _ = await _git(["rev-parse", "--verify", "MERGE_HEAD"],
                            cwd=wt, check=False)
    if rc != 0:
        return False, []
    _, un, _ = await _git(["diff", "--name-only", "--diff-filter=U"],
                          cwd=wt, check=False)
    return True, [ln.strip() for ln in un.splitlines() if ln.strip()]


async def sync_with_base(wt, base=None, keep_conflicts=False):
    """Merge the current base into this task's branch, inside its worktree.

    Returns (ok, conflicts, note). A PR that conflicts with the base branch was
    a dead end: the task was marked "conflict", finished, and left for a human.
    With every task merging into one integration branch that is not an edge
    case, it is the normal cost of parallelism — and most of it is not a real
    disagreement at all, just a base that moved on under a long-running task.
    Those merge cleanly with no model involved.

    `conflicts` is the list of paths git could not reconcile; it is empty when
    ok is True. On failure the merge is ABORTED, so the worktree is left exactly
    as it was rather than half-merged.
    """
    base = base or config.BASE_BRANCH
    wt = Path(wt)
    # A merge left in progress by a previous pass is not a new merge to start.
    # If the agent resolved everything, say so and let publish's commit conclude
    # it; if conflicts remain, report exactly those.
    in_merge, unmerged = await merge_in_progress(wt)
    if in_merge:
        if unmerged:
            return False, unmerged, f"{len(unmerged)} conflict(s) still unresolved"
        return True, [], "merge already resolved — pending commit"
    await _git(["fetch", "origin", base], cwd=wt, check=False)
    rc, ref, _ = await _git(["rev-parse", "--verify", f"origin/{base}"],
                            cwd=wt, check=False)
    target = f"origin/{base}" if rc == 0 and ref.strip() else base
    rc, _, err = await _git(
        ["merge", "--no-edit", "-m", f"merge {base} into task branch", target],
        cwd=wt, check=False)
    if rc == 0:
        return True, [], "merged cleanly"
    rc2, out, _ = await _git(["diff", "--name-only", "--diff-filter=U"],
                             cwd=wt, check=False)
    conflicts = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if keep_conflicts and conflicts:
        # Leave the merge in progress WITH its markers so an implementer can
        # resolve it by editing files — the thing agents are actually good at.
        # publish's `git add -A && git commit` then concludes the merge, and
        # the verify gate catches any marker left behind (nothing compiles).
        return False, conflicts, f"{len(conflicts)} conflicting file(s), left for resolution"
    await _git(["merge", "--abort"], cwd=wt, check=False)
    return False, conflicts, (f"{len(conflicts)} conflicting file(s)"
                              if conflicts else f"merge failed: {err.strip()[:200]}")


async def push_task_branch(repo, task_id):
    """Push task/<id> to origin. Returns (ok, note)."""
    repo = Path(repo).resolve()
    rc, remotes, _ = await _git(["remote"], cwd=repo, check=False)
    if not remotes.strip():
        return False, "no git remote configured"
    try:
        await _git(["push", "-u", "--force-with-lease", "origin",
                    f"task/{task_id}"], cwd=repo)
    except GitError as exc:
        return False, f"push failed: {exc}"[:200]
    return True, "pushed"


async def open_pr(repo, task_id, title, body, base=None):
    """Open (or find) the PR for task/<id>. Returns (number, url, note)."""
    repo = Path(repo).resolve()
    base = base or config.BASE_BRANCH
    branch = f"task/{task_id}"
    rc, out, _ = await _gh(["pr", "list", "--head", branch, "--state", "open",
                            "--json", "number,url"], cwd=repo)
    if rc == 0 and out.strip():
        try:
            existing = json.loads(out)
        except ValueError:
            existing = []
        if existing:
            return existing[0]["number"], existing[0]["url"], "already open"
    rc, out, err = await _gh(
        ["pr", "create", "--base", base, "--head", branch,
         "--title", title, "--body", body], cwd=repo)
    if rc != 0:
        return None, None, f"gh pr create failed: {err.strip()[:200]}"
    url = out.strip().splitlines()[-1] if out.strip() else ""
    number = None
    m = re.search(r"/pull/(\d+)", url)
    if m:
        number = int(m.group(1))
    return number, url, "opened"


async def pr_diff(repo, number, max_chars=60000):
    """The PR's diff, for a reviewer to read. Bounded."""
    rc, out, err = await _gh(["pr", "diff", str(number)], cwd=Path(repo).resolve())
    if rc != 0:
        return f"(could not read PR diff: {err.strip()[:200]})"
    if len(out) > max_chars:
        return out[:max_chars] + f"\n... [diff truncated at {max_chars} chars]"
    return out or "(empty diff)"


async def pr_state(repo, number):
    """{state, mergeable, checks} for a PR, or {} when unknown."""
    rc, out, _ = await _gh(["pr", "view", str(number), "--json",
                            "state,mergeable,mergeStateStatus,isDraft"],
                           cwd=Path(repo).resolve())
    if rc != 0:
        return {}
    try:
        return json.loads(out)
    except ValueError:
        return {}


async def merge_pr(repo, number, method="squash"):
    """Merge a PR. Returns (ok, note). Only ever called after approvals."""
    rc, out, err = await _gh(
        ["pr", "merge", str(number), f"--{method}", "--delete-branch"],
        cwd=Path(repo).resolve(), timeout=120)
    if rc != 0:
        return False, f"gh pr merge failed: {err.strip()[:200]}"
    return True, "merged"


async def open_promotion_pr(repo, base=None, prod=None, title=None):
    """Open a development -> main PR for a human to merge. Never merges it."""
    repo = Path(repo).resolve()
    base = base or config.BASE_BRANCH
    prod = prod or config.PROD_BRANCH
    if base == prod:
        # One-branch flow: there is nothing to promote INTO. GitHub rejects a
        # PR whose head and base are the same ref, and the error it returns
        # ("No commits between main and main") reads like a bug rather than a
        # configuration choice.
        return None, None, (f"promotion is not configured: BASE_BRANCH and "
                            f"PROD_BRANCH are both {base!r}")
    await _git(["push", "origin", base], cwd=repo, check=False)
    rc, ahead, _ = await _git(["rev-list", "--count", f"{prod}..{base}"],
                              cwd=repo, check=False)
    n = int(ahead.strip() or 0) if rc == 0 and ahead.strip().isdigit() else 0
    if not n:
        return None, None, f"{base} has nothing {prod} does not"
    rc, out, _ = await _gh(["pr", "list", "--head", base, "--base", prod,
                            "--state", "open", "--json", "number,url"], cwd=repo)
    if rc == 0 and out.strip():
        try:
            ex = json.loads(out)
        except ValueError:
            ex = []
        if ex:
            return ex[0]["number"], ex[0]["url"], f"already open ({n} commits)"
    rc, out, err = await _gh(
        ["pr", "create", "--base", prod, "--head", base,
         "--title", title or f"promote {base} -> {prod} ({n} commits)",
         "--body", f"{n} commit(s) on `{base}` ready for `{prod}`.\n\n"
                   "Every commit here already passed its own PR review."],
        cwd=repo)
    if rc != 0:
        return None, None, f"gh pr create failed: {err.strip()[:200]}"
    url = out.strip().splitlines()[-1] if out.strip() else ""
    m = re.search(r"/pull/(\d+)", url)
    return (int(m.group(1)) if m else None), url, f"opened ({n} commits)"
