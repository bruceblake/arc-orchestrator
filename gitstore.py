"""Blessed-clone + worktree lifecycle for the multi-harness code workload.

The blessed clone (~/repos/<project>) keeps main clean; every task runs in
~/worktrees/<project>/<task-id> on branch task/<task-id>. The orchestrator is
the only git actor — harnesses only write files inside their worktree.
"""
import asyncio
import fcntl
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

import config
import events

log = logging.getLogger("gitstore")

GIT_TIMEOUT = 120


class GitError(RuntimeError):
    pass


class _RepoLock:
    """Serialize git mutations against one blessed clone.

    alloc, cleanup and the base fast-forward all rewrite refs and worktrees
    in the same .git. Parallel tasks (the diamond's three dependents) used to
    run those at once. A checkout that loses that race leaves a directory of
    0-byte files and an empty .git pointer; the next resume then dies with
    "already exists" because `worktree remove` does not recognize it.
    """

    def __init__(self, repo):
        git = Path(repo).resolve() / ".git"
        if git.is_file():
            line = git.read_text(errors="replace").strip()
            if line.startswith("gitdir:"):
                git = Path(line.split(":", 1)[1].strip())
        self.path = git / "arc-orchestrator.lock"
        self._fh = None

    async def __aenter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        try:
            # Keep the event loop responsive without a thread whose shutdown
            # can stall short asyncio.run() gates after the lock is released.
            while True:
                try:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except BlockingIOError:
                    await asyncio.sleep(0.05)
        except BaseException:
            self._fh.close()
            self._fh = None
            raise

    async def __aexit__(self, *_exc):
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


def _null_split(raw):
    """Split NUL-delimited git output (-z) into path strings.

    fsdecode, not decode: a filename on this fleet can hold bytes that are not
    valid UTF-8, and fsdecode's surrogateescape round-trips them, so the string
    still names the real file when it is passed back to git. `errors="replace"`
    would hand back a path that exists nowhere — the same class of bug as the
    C-quoting this replaced.
    """
    if not raw:
        return []
    return [os.fsdecode(p) for p in raw.split(b"\0")]


async def checkpoint(repo, task_id, wt, label, *, model=None, attempt=None):
    """Save the worktree's work as a patch; returns its Path, or None.

    A worktree is state, like a long-running server process. alloc RESETS
    task/<id> to base on every (re)alloc and discards both uncommitted edits
    and unpublished commits; on 2026-09-24 a resume after a reboot threw away
    the reviewed work of three tasks that way and it survived only because
    their branches had been pushed. This is the checkpoint: a patch of
    everything the worktree holds against its MERGE BASE — commits, staged and
    unstaged edits, untracked files — minus the .arc channel files (the
    orchestrator's, never PR content) and .reasonix.

    It is binary-safe in the literal sense: the diffs are captured as BYTES and
    written unchanged. `--binary` is not enough on its own, because git only
    classifies a blob as binary when it contains a NUL byte — a latin-1 source
    file is "text" to git, and decoding it would rewrite 0xE9 into the three
    bytes of U+FFFD while the restore still reported success.

    NEVER raises: it runs on reset, drain and cancel paths, where turning a
    handled interruption into an unhandled exception is worse than losing the
    checkpoint. Returns None when there is nothing to save or git refuses.

    It also leaves the worktree EXACTLY as it found it. It never stages, so it
    cannot hand a live agent a staged copy of its own work, and it can be run
    on a sibling still being implemented without racing index.lock.
    """
    try:
        wt = Path(wt)
        if not (wt / ".git").exists():
            return None
        base = config.BASE_BRANCH
        rc, mb, _ = await _git(["merge-base", base, "HEAD"], cwd=wt, check=False)
        ref = mb.strip() if rc == 0 and mb.strip() else "HEAD"
        _, head, _ = await _git(["rev-parse", "HEAD"], cwd=wt, check=False)
        # READ-ONLY capture. This deliberately does NOT run `git add -N` to make
        # untracked files visible: that mutates the index and takes index.lock,
        # and a drain fires a sweep while sibling tasks are still being
        # implemented — the add would race a live agent's own git commands and
        # hand it a staged copy of work it never staged. The tracked half is one
        # `diff`; each untracked file is diffed against /dev/null, which git
        # treats as a new file and which touches nothing at all. Both halves
        # apply with `git apply --3way`.
        excl = [*(f":!{p}" for p in CHANNEL_FILES), *NEVER_STAGE]
        # -z, NOT newline-split. With core.quotePath on (the default) git
        # C-quotes any path that is not plain ASCII, so `ls-files --others`
        # yields `"caf\303\251.txt"` — and that QUOTED string names no file on
        # disk. Fed to `diff --no-index` it makes git exit 1 with EMPTY stdout
        # ("Could not access …"), and 1 is also the exit code for "the files
        # differ", so the empty blob was appended and the quoted name recorded:
        # a checkpoint that reported success while silently dropping the file.
        # -z emits raw bytes and never quotes. _git_BYTES, not _git: the name is
        # a path, and _git decodes with errors="replace", which rewrites a
        # non-UTF-8 tracked name into a string that names no file on disk —
        # the same defect one layer down.
        _, files_raw, _ = await _git_bytes(
            ["diff", "--name-only", "-z", ref, "--", ".", *excl],
            cwd=wt, check=False)
        # BYTES, not text — see _git_bytes. A latin-1 or otherwise non-UTF-8
        # source file is not "binary" to git (it has no NUL byte), so --binary
        # alone does not protect it: decoding would rewrite 0xE9 into the three
        # bytes of U+FFFD, and the checkpoint would restore a corrupted file
        # while still reporting success.
        rc, patch, _ = await _git_bytes(
            ["diff", "--binary", ref, "--", ".", *excl], cwd=wt, check=False)
        rc_others, others_raw, _ = await _git_bytes(
            ["ls-files", "-z", "--others", "--exclude-standard", "--", ".",
             *excl], cwd=wt, check=False)
        if rc_others > 1:
            return None
        # Each -z field is a raw path; fsdecode round-trips ANY bytes
        # (surrogateescape), so an undecodable name still names the real file.
        listed = [f for f in _null_split(files_raw) if f]
        for rel in _null_split(others_raw):
            if not rel:
                continue
            rc2, one, err2 = await _git_bytes(
                ["diff", "--no-index", "--binary", "--", "/dev/null", rel],
                cwd=wt, check=False)
            # --no-index exits 1 when the files differ, which is the normal
            # case here; only a real failure (2) discards the capture. Exit 1
            # with NO patch is the third case: git could not access the path it
            # was handed. Trusting the exit code alone is what recorded it.
            if rc2 > 1:
                return None
            if rc2 == 1 and not one.strip():
                log.warning("checkpoint %s: skipped unreadable untracked path "
                            "%r (%s)", task_id, rel, err2.strip()[:120])
                continue
            patch += one
            listed.append(rel)
        if rc != 0 or (not listed and not patch.strip()):
            return None
        out = (Path(config.CHECKPOINT_DIR) / Path(repo).resolve().name
               / str(task_id))
        out.mkdir(parents=True, exist_ok=True)
        # Subsecond stamp AND mtime ordering. A whole-second stamp made two
        # checkpoints in the same second sort on their LABEL, which is worse
        # than arbitrary: "20260924T120000-interrupted-1" sorts BEFORE
        # "…-interrupted", and "pre-reset" before "x1". checkpoint_files reads
        # that order as newest-first and _prune_checkpoints drops its FRONT, so
        # a resume restored an older patch than the one it had just written.
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        stamp += f".{int(time.time() * 1000) % 1000:03d}"
        dest = out / f"{stamp}-{label}.patch"
        n = 0
        while dest.exists():
            n += 1
            dest = out / f"{stamp}-{label}.{n}.patch"
        # Local checkpoint writes finish before the next git step. Offloading
        # them left a default-executor thread that stalled test-run shutdown.
        dest.write_bytes(patch)
        meta = {"task": str(task_id), "label": str(label), "head": head.strip(),
                "merge_base": ref, "files": listed, "model": model,
                "attempt": attempt, "patch": dest.name, "bytes": len(patch)}
        rc, ahead, _ = await _git(["rev-list", "--count", f"{ref}..HEAD"],
                                  cwd=wt, check=False)
        meta["commits"] = int(ahead.strip()) if (rc == 0
                                                 and ahead.strip().isdigit()) else None
        dest.with_suffix(".json").write_text(
            json.dumps(meta, indent=2, default=str), encoding="utf-8")
        events.emit("task.checkpointed", task=str(task_id), label=str(label),
                    path=str(dest), files=len(listed), commits=meta["commits"],
                    model=model)
        _prune_checkpoints(out)
        return dest
    except Exception as exc:                                   # noqa: BLE001
        try:
            events.emit("task.checkpoint_failed", task=str(task_id),
                        label=str(label), error=str(exc)[:200])
        except Exception:                                      # noqa: BLE001
            pass
        log.warning("checkpoint %s (%s) failed: %s", task_id, label, exc)
        return None


async def checkpoint_stopping(repo, task_ids=None, label="interrupted"):
    """Checkpoint the worktrees of tasks that have STOPPED; returns how many.

    This is the drain/cancel sweep, and its scope is the point. A drain fires
    it from the tail node of one task while its siblings are still being
    implemented: checkpointing one of THOSE would run git in a worktree a live
    agent is writing to. Callers pass the tasks that have genuinely stopped —
    main.py the rows it just marked interrupted, the graph the task whose merge
    or failure has already fired.

    AWAIT this; it needs the event loop the run is still holding.

    A `code_tasks` row dict is accepted as well as a bare id, because that is
    what main.py has in hand at the moment it needs this. It must be: the
    first version took `str(t)` of whatever it was given, so passing the row
    dicts from `store.running_code_tasks` looked up a directory literally named
    "{'id': 'x', …}" — every task missed, the sweep saved nothing, and because
    a missing directory is an ordinary skip there was not even an error. A
    lookup that finds no worktree now SAYS SO, so the next version of that
    mistake is visible in the event log instead of silent.
    """
    saved, root = 0, Path(config.WORKTREE_ROOT) / Path(repo).resolve().name
    seen = set()
    for raw in (task_ids or []):
        # A row dict, a path, a plain id: take the id out of whatever it is.
        tid = str(raw.get("id") if isinstance(raw, dict) else raw) if raw else ""
        if not tid or tid in seen:
            continue
        seen.add(tid)
        wt = root / tid
        if not (wt / ".git").exists():
            events.emit("task.checkpoint_skipped", task=tid, label=label,
                        path=str(wt), reason="no worktree")
            continue
        try:
            events.emit("task.checkpoint_sweep", task=tid, label=label)
            if await checkpoint(repo, tid, wt, label):
                saved += 1
        except Exception as exc:                               # noqa: BLE001
            log.warning("checkpoint sweep: %s failed: %s", tid, exc)
    return saved


def checkpoint_worktrees(repo, task_ids=None, label="interrupted"):
    """`checkpoint_stopping` for a caller with NO event loop left.

    A wrapper, not the primary path: awaiting is what the run itself must do
    (it still holds its loop), and `asyncio.run` inside a running loop raises
    RuntimeError — which the caller's `except Exception` would swallow into a
    log line, quietly losing the checkpoint a cancel was supposed to write.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(checkpoint_stopping(repo, task_ids, label))
    raise RuntimeError(
        "checkpoint_worktrees needs no running loop — await "
        "checkpoint_stopping instead")


def _prune_checkpoints(directory):
    """Keep only the newest config.CHECKPOINT_KEEP checkpoints of one task."""
    try:
        keep = max(1, int(config.CHECKPOINT_KEEP))
    except (TypeError, ValueError):
        keep = 10
    try:
        for old in _ordered_patches(directory)[:-keep]:
            for p in (old, old.with_suffix(".json")):
                try:
                    p.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def _ordered_patches(directory):
    """This task's checkpoints, OLDEST first, ordered by WRITE TIME.

    By mtime, not by name. A name is a timestamp plus a label, so two
    checkpoints written in the same second used to sort on the LABEL — and
    "…-interrupted-1" sorts before "…-interrupted", "pre-reset" before "x1".
    Since checkpoint_files reads that order as newest-first and
    _prune_checkpoints drops the front of it, the fleet restored a patch older
    than the one it had just written and could prune the newest away. mtime is
    what "newest" actually meant, and it never depends on a label.
    """
    try:
        return sorted(directory.glob("*.patch"),
                      key=lambda p: (p.stat().st_mtime_ns, p.name))
    except OSError:
        return []


def checkpoint_files(repo, task_id):
    """Every checkpoint of this task, newest first, as (path, metadata)."""
    out = (Path(config.CHECKPOINT_DIR) / Path(repo).resolve().name
           / str(task_id))
    rows = []
    for p in reversed(_ordered_patches(out)):
        try:
            meta = json.loads(p.with_suffix(".json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        rows.append((p, meta))
    return rows


async def restore_checkpoint(repo, task_id, wt, path=None):
    """Apply the latest (or a named) checkpoint onto a worktree; report conflicts.

    This runs after alloc, which has already reset the branch to base, so the
    patch applies against the merge base it was taken from. `--3way` is what
    lets a partially overlapping patch recover its non-conflicting parts; a
    genuinely conflicting file is then REPORTED and the whole apply rolled back
    rather than left half-applied — a conflicted tree would otherwise be
    published as if it were the attempt's own work.
    """
    wt = Path(wt)
    meta = {}
    if path is None:
        rows = checkpoint_files(repo, task_id)
        if not rows:
            return {"restored": False, "path": None, "files": [],
                    "conflicts": [], "meta": {},
                    "reason": "no checkpoint for this task"}
        path, meta = rows[0]
    else:
        path = Path(path)
        try:
            meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
    if not Path(path).exists():
        return {"restored": False, "path": str(path), "files": [],
                "conflicts": [], "meta": meta,
                "reason": f"checkpoint not found: {path}"}
    rc, _, _ = await _git(["apply", "--3way", "--whitespace=nowarn",
                           str(Path(path).resolve())], cwd=wt, check=False)
    # A conflicting `--3way` apply leaves conflict markers in the files AND
    # unmerged entries in the index (exit 1, "Applied patch ... with
    # conflicts"). Neither the exit code alone nor the message may be the test:
    # the index is the honest one, and `ls-files -u` prints a bare path per
    # stage — its `--name-only` is silently ignored.
    # -z here too: the conflict list is what an operator reads and what the
    # event records, and a C-quoted `"caf\303\251.txt"` names nothing.
    _, unmerged_raw, _ = await _git_bytes(["ls-files", "-u", "-z"], cwd=wt,
                                          check=False)
    conflicts = sorted({f.split("\t")[-1]
                        for f in _null_split(unmerged_raw) if f})
    _, applied_raw, _ = await _git_bytes(
        ["diff", "--name-only", "-z", config.BASE_BRANCH, "--", "."],
        cwd=wt, check=False)
    files = sorted({p for p in _null_split(applied_raw)
                    if p and p not in conflicts})
    if conflicts or rc != 0:
        # Leave nothing half-applied: a tree carrying conflict markers, or one
        # where only part of the patch landed, would be published as though it
        # were the attempt's own work. Worse than not restoring at all.
        await _git(["reset", "-q", "--hard", "HEAD"], cwd=wt, check=False)
        events.emit("task.checkpoint_conflict", task=str(task_id),
                    path=str(path), conflicts=conflicts, exit=rc)
        return {"restored": False, "path": str(path), "files": [], "meta": meta,
                "conflicts": conflicts,
                "reason": (f"{len(conflicts)} conflicting file(s)" if conflicts
                           else "git apply refused the patch")}
    # Unstage. `--3way` implies `--index`, so a clean apply stages everything it
    # wrote, and an implementer that starts work on a tree with a pre-staged
    # index publishes a diff it did not choose (publish's `git add -A` would
    # have masked this, but a reviewer reading `git diff` would not). The work
    # belongs in the FILES, exactly as the checkpoint recorded it.
    await _git(["reset", "-q", "--mixed", "HEAD"], cwd=wt, check=False)
    events.emit("task.checkpoint_restored", task=str(task_id), path=str(path),
                files=len(files), conflicts=0)
    return {"restored": True, "path": str(path), "files": files,
            "conflicts": [], "meta": meta, "reason": ""}


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


async def _git_bytes(args, cwd, check=True):
    """Like `_git`, but returns stdout as BYTES, undecoded and unreplaced.

    A patch is data, not text. `_git` decodes with errors="replace" and the
    caller re-encodes, which silently rewrites every byte that is not valid
    UTF-8 (0xE9 in a latin-1 source file becomes EF BF BD) — and `--binary`
    does not protect that file, because git only calls a file binary when it
    contains a NUL byte. Tolerable for a filename or a status line; corruption
    for a checkpoint. Only the stderr is decoded, for the error message.
    """
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
    if check and proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args[:4])} failed ({proc.returncode}) in {cwd}: "
            f"{err.decode(errors='replace').strip()[:400]}"
        )
    return proc.returncode, out, err.decode(errors="replace")


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
    wrong for this system: the local base is what pr_merge fast-forwards
    after each merge, and it can legitimately be ahead of origin (an
    operator's unpushed commit, a push that failed). Branching from a stale
    origin would silently revert that work the next time a task published.

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

    A re-alloc resets task/<task_id> to the base when the branch has no open
    pull request: a failed attempt's rejected work must not leak into the
    retry. A branch whose tip is not an ancestor of base AND whose PR is
    still open is reused instead — resetting it discards reviewed commits.
    A gh failure is not an open PR, so alloc then resets as before."""
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
    # n > 0 means the branch tip is not an ancestor of base. Resetting it
    # throws away commits. If an open PR still points at the branch, reuse it.
    if n:
        number = url = state = None
        try:
            number, url, state = await find_pr(
                repo, task_id, state="open", wait_quota=False)
        except Exception:
            number = None
        if number is not None and (state or "").upper() == "OPEN":
            events.emit("task.branch_kept", task=task_id, branch=branch,
                        pr=number, url=url, commits=n,
                        reason="open pull request")
            log.warning(
                "alloc %s: open PR #%s — reusing %s (%d commit(s)), not resetting",
                task_id, number, branch, n)
            async with _RepoLock(repo):
                await _reuse_branch_worktree(repo, wt, branch)
            return wt
    # A re-alloc is about to discard this worktree. Checkpoint it first, so a
    # resume can restore instead of starting over — uncommitted edits count as
    # work too, and the checkpoint never raises, so the reset still happens.
    prev = await existing_worktree(repo, task_id)
    if prev is not None:
        _, dirty, _ = await _git(["status", "--porcelain"], cwd=prev, check=False)
        if n or dirty.strip():
            await checkpoint(repo, task_id, prev, "pre-reset")
    if n:
        events.emit("task.branch_reset", task=task_id, branch=branch,
                    commits_discarded=n, base=base_ref)
        log.warning("alloc %s: resetting %s to %s discards %d commit(s)",
                    task_id, branch, base_ref, n)
    async with _RepoLock(repo):
        await _replace_worktree(repo, wt, branch, base_ref)
        if not await _checkout_intact(repo, wt, base_ref):
            # A parallel checkout on this clone has been observed to return
            # success and leave every file, including .git, at 0 bytes. git
            # then refuses the next add ("already exists") and the resume
            # dies before a model starts. One rebuild is enough; a second
            # hollow tree is a real failure, not something to paper over.
            log.warning("alloc %s: checkout at %s was empty; recreating it",
                        task_id, wt)
            events.emit("worktree.hollow", task=task_id, path=str(wt))
            await _replace_worktree(repo, wt, branch, base_ref)
            if not await _checkout_intact(repo, wt, base_ref):
                raise GitError(f"worktree {wt} checked out empty")
    return wt


def _untracked_overwrite_paths(err):
    """Paths from `untracked working tree files would be overwritten by merge`.

    Git indents each path. The header and the "Please move..." trailer are
    not indented, so they never become candidates.
    """
    if "untracked working tree files would be overwritten" not in (err or ""):
        return []
    paths = []
    for line in err.splitlines():
        if not line[:1].isspace():
            continue
        rel = line.strip()
        if not rel or ".." in Path(rel).parts:
            continue
        paths.append(rel)
    return paths


async def _remove_untracked(repo, paths):
    """Delete untracked files that are blocking a fast-forward. Returns paths.

    Only `??` lines. A tracked local edit must survive — that is the
    operator's work, and the fast-forward is supposed to refuse it.
    """
    removed = []
    root = Path(repo).resolve()
    for rel in paths:
        path = (root / rel).resolve()
        try:
            inside = path.is_relative_to(root)
        except ValueError:
            inside = False
        if not inside or not path.is_file():
            continue
        rc, out, _ = await _git(["status", "--porcelain", "--", rel],
                                cwd=repo, check=False)
        if rc != 0 or not out.startswith("??"):
            continue
        path.unlink()
        removed.append(rel)
    if removed:
        events.emit("git.untracked_cleared", repo=str(root),
                    files=removed[:20], count=len(removed))
        log.warning("removed %d untracked file(s) so %s can fast-forward: %s",
                    len(removed), root.name, ", ".join(removed[:8]))
    return removed


async def _drop_worktree_dir(repo, wt):
    """Make `wt` absent so `worktree add` can create it.

    `git worktree remove` only deletes a path git still recognizes. The
    prison-escape resume died here: the directory was on disk, its .git
    file was empty, prune called the gitdir invalid, remove said "not a
    working tree", and add --force still exited 128 with "already exists".
    """
    wt = Path(wt)
    if wt.exists():
        await _git(["worktree", "remove", "--force", str(wt)], cwd=repo, check=False)
    if wt.exists():
        rc, listed, _ = await _git(["worktree", "list", "--porcelain"],
                                   cwd=repo, check=False)
        if rc == 0 and str(wt.resolve()) in listed:
            raise GitError(
                f"cannot replace {wt}: git still tracks it and worktree remove failed")
        shutil.rmtree(wt)
    await _git(["worktree", "prune"], cwd=repo, check=False)


async def _reuse_branch_worktree(repo, wt, branch):
    """Check out `branch` as it stands. Never uses -B, which resets the tip."""
    live = wt if await existing_worktree(repo, Path(wt).name) else None
    if live is None:
        await _drop_worktree_dir(repo, wt)
        await _git(["worktree", "add", "--force", str(wt), branch], cwd=repo)
        live = wt
    if await _checkout_intact(repo, live, branch):
        return live
    log.warning("alloc %s: checkout at %s was empty; recreating it on %s",
                Path(wt).name, live, branch)
    events.emit("worktree.hollow", task=Path(wt).name, path=str(live))
    await _drop_worktree_dir(repo, live)
    await _git(["worktree", "add", "--force", str(wt), branch], cwd=repo)
    if not await _checkout_intact(repo, wt, branch):
        raise GitError(f"worktree {wt} checked out empty")
    return wt


async def _replace_worktree(repo, wt, branch, base_ref):
    await _drop_worktree_dir(repo, wt)
    await _git(["worktree", "add", "--force", "-B", branch, str(wt), base_ref],
               cwd=repo)


async def _checkout_intact(repo, wt, base_ref):
    """False when a non-empty blob landed as a 0-byte file, or .git is empty."""
    gitfile = Path(wt) / ".git"
    try:
        if not gitfile.is_file() or gitfile.stat().st_size == 0:
            return False
    except OSError:
        return False
    rc, out, _ = await _git(["ls-tree", "-r", "--name-only", base_ref],
                            cwd=repo, check=False)
    if rc != 0:
        return False
    checked = 0
    for rel in out.splitlines():
        rel = rel.strip()
        if not rel:
            continue
        rc, size_s, _ = await _git(["cat-file", "-s", f"{base_ref}:{rel}"],
                                   cwd=repo, check=False)
        if rc != 0 or not (size_s.strip() or "").isdigit():
            continue
        if int(size_s.strip()) <= 0:
            continue
        path = Path(wt) / rel
        try:
            actual = path.stat().st_size if path.is_file() else -1
        except OSError:
            return False
        if actual <= 0:
            return False
        checked += 1
        if checked >= 3:
            break
    return True


async def diff_stat(wt):
    _, st, _ = await _git(["status", "--porcelain"], cwd=wt)
    _, ds, _ = await _git(["diff", "--stat", "HEAD"], cwd=wt, check=False)
    parts = [p for p in (st.strip(), ds.strip()) if p]
    return "\n".join(parts) or "(clean)"


# The .arc channel files are ignored in this repo, but not in every project
# repo. Naming an ignored path in `git add` makes Git 2.55 fail ("The
# following paths are ignored"), so unstage them after adding. `git reset`
# of a path that is neither on disk nor in the index errors, so only those
# that exist or are already indexed are reset. Reasonix state
# (.reasonix/tasks/<run>/events.jsonl, snapshot.json, task.lock) must never
# be published: thirty-three such files reached main before this exclusion.
# Path exclusions for `git diff`, where the `:!` form is required. NEVER_STAGE
# is NOT usable with `git add`: naming an ignored path that way makes the add
# exit 1 ("The following paths are ignored by one of your .gitignore files"),
# and .reasonix is ignored in this repo.
CHANNEL_FILES = (".arc/plan_proposals.jsonl", ".arc/board.jsonl",
                 ".arc/handoff.md")
NEVER_STAGE = (":!.reasonix",)
# The same exclusion as a plain path, for the `git reset` that unstages it.
RUNTIME_PATHS = (".reasonix",)


async def _stage_without_runtime_files(wt, *, intent=False):
    args = ["add", "-A"]
    if intent:
        args.append("-N")
    # NEVER_STAGE is a pathspec EXCLUSION, and git refuses that shape outright
    # when the path it names is .gitignore'd ("The following paths are ignored
    # by one of your .gitignore files: .reasonix", exit 1) — it is ignored in
    # this very repo. So the add takes everything and the exclusions are
    # applied by unstaging afterwards, which works whether or not the path is
    # ignored and whether or not it exists.
    await _git([*args, "--", "."], cwd=wt)
    await _git(["reset", "-q", "--", *RUNTIME_PATHS], cwd=wt, check=False)
    root = Path(wt)
    present = [p for p in CHANNEL_FILES if (root / p).exists()]
    _, indexed, _ = await _git(
        ["ls-files", "--", *CHANNEL_FILES], cwd=wt, check=False)
    for name in indexed.splitlines():
        if name and name not in present:
            present.append(name)
    # Also clears a channel file staged by an earlier interrupted run.
    if present:
        await _git(["reset", "-q", "--", *present], cwd=wt)


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
    # Same channel exclusion as publish(): a surviving proposals file must
    # not leak into the diff reviewers read either.
    await _stage_without_runtime_files(wt, intent=True)
    rc, mb, _ = await _git(["merge-base", base, "HEAD"], cwd=wt, check=False)
    ref = mb.strip() if rc == 0 and mb.strip() else "HEAD"
    _, diff, _ = await _git(
        ["diff", ref, "--", ".", *(f":!{path}" for path in CHANNEL_FILES)],
        cwd=wt, check=False)
    if len(diff) > max_chars:
        diff = diff[:max_chars] + f"\n... [truncated at {max_chars} chars]"
    return diff or "(empty diff)"


async def publish(wt, message, trailers=None):
    """Commit all changes in the worktree; returns commit hash, None if clean."""
    _, st, _ = await _git(["status", "--porcelain"], cwd=wt)
    if not st.strip():
        return None
    # `.arc/plan_proposals.jsonl` is the plan-amendment channel (plan_amend.py)
    # — an orchestrator<->agent runtime file, never PR content. Harvest deletes
    # it before publish runs; unstaging afterwards is the belt for the day a
    # delete fails (plan.amend.channel_survives events are the alarm).
    await _stage_without_runtime_files(wt)
    # The board is excluded on purpose and is left in the worktree. If it is
    # the only dirty path, commit would exit 1 with "nothing added to commit"
    # and a resume that only posted to the board would fail publish.
    _, staged, _ = await _git(["diff", "--cached", "--name-only"], cwd=wt, check=False)
    if not staged.strip():
        return None
    args = ["commit", "-q", "-m", message]
    if trailers:
        args += ["-m", "\n".join(f"{k}: {v}" for k, v in trailers.items())]
    await _git(args, cwd=wt)
    _, head, _ = await _git(["rev-parse", "HEAD"], cwd=wt)
    return head.strip()


async def branch_ahead(repo, task_id, base=None):
    """True if task/<task_id> exists and has commits `base` does not.

    Defaults to config.BASE_BRANCH, never a literal "main". When the fleet
    merged into a separate development branch, comparing against main meant
    a branch fully merged into development still counted as unmerged, so
    reconcile KEPT its worktree forever and the cleanup it exists to perform
    never happened (task/graph-admission-control: 0 commits ahead of
    development, 30 ahead of main). The two are one branch by default now;
    the rule stands for anyone who sets ARC_BASE_BRANCH back.
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


async def github_status(repo):
    """Best-effort GitHub-readiness probe; never raises or hangs.

    Returns {'remote': <origin url or None>, 'gh_installed': bool,
    'gh_authed': bool, 'ready': bool, 'reason': <short text or None>}.
    The dashboard shows it per project, so an operator sees BEFORE a run
    why publish would fail to push or open a pull request."""
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
    async with _RepoLock(repo):
        await _drop_worktree_dir(repo, wt)
        if delete_branch:
            await _git(["branch", "-d", f"task/{task_id}"], cwd=repo, check=False)


# --- pull-request flow -------------------------------------------------------
# The PR is the gate. A task branch is pushed and a pull request opened against
# config.BASE_BRANCH; reviewers read the real PR diff; a merger merges it only
# once every reviewer approves. Nothing is merged locally, so "send it back"
# actually withholds the change instead of commenting on history.

_RATE_LIMITED = ("rate limit", "secondary rate", "abuse detection")


def is_rate_limited(text):
    """GitHub refused for API quota, not for anything about the request."""
    low = (text or "").lower()
    return any(m in low for m in _RATE_LIMITED)


async def _quota_reset_in(cwd):
    """Seconds until the exhausted GitHub quota resets; 60 when unknown.

    `gh api rate_limit` is free: it does not count against either quota."""
    import time
    rc, out, _ = await _gh_raw(["api", "rate_limit"], cwd, timeout=30)
    if rc != 0:
        return 60.0
    try:
        res = json.loads(out).get("resources") or {}
    except ValueError:
        return 60.0
    now = time.time()
    waits = [float(r.get("reset", now)) - now for r in res.values()
             if isinstance(r, dict) and r.get("remaining", 1) == 0]
    return max(5.0, min(max(waits), 3600.0)) if waits else 60.0


async def _gh(args, cwd, timeout=180, wait_quota=True):
    """Run gh; returns (rc, stdout, stderr). Never raises.

    A refusal for GitHub API quota waits for the reset and retries, bounded
    by config.GH_QUOTA_MAX_WAIT, so parallel runs that drain the shared
    5000/h GraphQL quota stall briefly instead of failing reviewed tasks at
    `gh pr create` (three did on 2026-09-24)."""
    rc, out, err = await _gh_raw(args, cwd, timeout)
    budget = config.GH_QUOTA_MAX_WAIT if wait_quota else 0
    while rc != 0 and budget > 0 and is_rate_limited(err + out):
        wait = min(await _quota_reset_in(cwd) + 5, budget)
        budget -= wait
        events.emit("git.quota_wait", op=" ".join(args[:2]), wait_s=round(wait),
                    error=(err or out).strip()[:200])
        await asyncio.sleep(wait)
        rc, out, err = await _gh_raw(args, cwd, timeout)
    return rc, out, err


async def _gh_raw(args, cwd, timeout=180):
    """Run gh once; returns (rc, stdout, stderr). Never raises."""
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


async def ensure_remote(repo, name=None, private=True):
    """Make sure `origin` exists, creating the GitHub repo with gh if needed.

    Returns (ok, url_or_reason) and NEVER raises — callers surface the reason
    as a note or a refusal line, not a stack trace. The 09-12 minecraft-test
    run threw away eight minutes of model work because publish found no
    remote on a machine where `gh auth status` was green the whole time: the
    dead end was pure bookkeeping. If gh created the GitHub repo but its
    trailing `--push` failed, the remote still exists — report success and
    let the run's own push retry.
    """
    import shutil
    repo = Path(repo).resolve()
    rc, out, _ = await _git(["remote", "get-url", "origin"], cwd=repo, check=False)
    if rc == 0 and out.strip():
        return True, out.strip()
    if not shutil.which("gh"):
        return False, "gh CLI not installed"
    rc, _, _ = await _gh(["auth", "status"], cwd=repo)
    if rc != 0:
        return False, "gh not authenticated"
    name = name or repo.name
    visibility = "--private" if private else "--public"
    rc, out, err = await _gh(
        ["repo", "create", name, visibility,
         f"--source={repo}", "--remote=origin", "--push"],
        cwd=repo, timeout=90)
    # gh exits non-zero when its --push fails even though the GitHub repo and
    # the origin remote were created — which is all a run needs to push itself.
    rc2, url, _ = await _git(["remote", "get-url", "origin"], cwd=repo, check=False)
    if rc2 == 0 and url.strip():
        return True, url.strip()
    return False, (err.strip() or out.strip() or f"gh repo create exited {rc}")[:200]


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


async def origin_ahead(repo, base=None):
    """Commits origin/<base> has that the local base does not. 0 if unknown."""
    repo = Path(repo).resolve()
    base = base or config.BASE_BRANCH
    rc, out, _ = await _git(["rev-list", "--count", f"{base}..origin/{base}"],
                            cwd=repo, check=False)
    if rc != 0 or not (out.strip() or "").isdigit():
        return 0
    return int(out.strip())


async def fast_forward_base(repo, base=None):
    """Move the local base branch to what origin has. Returns (ok, note).

    `git update-ref refs/heads/<base>` is only safe while <base> is NOT the
    checked-out branch: it moves the pointer without touching the index or
    working tree, so doing it to the current branch makes every file in the
    repo appear massively modified or deleted. That was fine while the
    operator's checkout was always `main` and the base was always
    `development`, and it becomes a foot-gun the moment anyone works ON the
    integration branch — which the branch model actively encourages.

    A checked-out base also refuses to fast-forward when untracked files
    would be overwritten. Godot writes `*.uid` beside every script, and the
    foundation PR then commits those same paths. The blessed clone still had
    the untracked copies, so the merge aborted, local main stayed on the
    scaffold, and every dependent branched without the code it depends on.
    Those untracked copies are deleted and the fast-forward is retried.
    A tracked local edit is left alone — that refusal is the point.
    """
    repo = Path(repo).resolve()
    base = base or config.BASE_BRANCH
    async with _RepoLock(repo):
        rc, remotes, _ = await _git(["remote"], cwd=repo, check=False)
        if "origin" not in remotes.split():
            return True, "no origin remote; local base stands"
        await _git(["fetch", "origin", base], cwd=repo, check=False)
        rc, _, _ = await _git(["rev-parse", "--verify", f"origin/{base}"],
                              cwd=repo, check=False)
        if rc != 0:
            return True, "origin has no such branch yet; local base stands"
        rc, cur, _ = await _git(["rev-parse", "--abbrev-ref", "HEAD"],
                                cwd=repo, check=False)
        if rc == 0 and cur.strip() == base:
            return await _ff_checked_out(repo, base)
        rc, _, err = await _git(
            ["update-ref", f"refs/heads/{base}", f"origin/{base}"],
            cwd=repo, check=False)
        return rc == 0, "updated" if rc == 0 else err.strip()[:400]


async def _ff_checked_out(repo, base):
    rc, _, err = await _git(["merge", "--ff-only", f"origin/{base}"],
                            cwd=repo, check=False)
    if rc == 0:
        return True, "fast-forwarded the checked-out base"
    removed = await _remove_untracked(repo, _untracked_overwrite_paths(err))
    if removed:
        rc, _, err = await _git(["merge", "--ff-only", f"origin/{base}"],
                                cwd=repo, check=False)
        if rc == 0:
            return True, (
                "fast-forwarded the checked-out base after removing "
                f"{len(removed)} untracked file(s) the incoming commit "
                "already contains")
    return False, ("base is checked out and not fast-forwardable: "
                   f"{err.strip()[:400]}")


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


async def head(wt):
    """The worktree's current HEAD sha, or None."""
    rc, out, _ = await _git(["rev-parse", "HEAD"], cwd=Path(wt), check=False)
    return out.strip() if rc == 0 and out.strip() else None


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


# Network weather, not a verdict on the work. One of these on a push used to
# mark a finished, reviewed task FAILED on the first attempt — and a failed
# row blocks every project chained after it (Rule 9): interactables-framework
# died that way on "unable to access 'https://github.com/...'" (2026-09-23),
# and the multiplayer batch waiting on it stopped with it.
_TRANSIENT_NET = ("unable to access", "could not resolve host", "connection timed out",
                  "connection reset", "operation timed out", "timed out after",
                  "early eof", "rpc failed", "ssl", "tls", "temporary failure",
                  "failed to connect", "http 502", "http 503", "http 504",
                  "the remote end hung up", "connection refused", "error connecting")


def is_transient_network_error(text):
    low = (text or "").lower()
    return any(m in low for m in _TRANSIENT_NET)


async def _retry_transient(label, attempt_fn):
    """Run attempt_fn() -> (ok, note); retry network failures with backoff.

    A real refusal (a stale lease, auth, "no commits between") is returned
    at once: retrying it only delays the same answer."""
    ok, note = await attempt_fn()
    for i, delay in enumerate(config.NET_RETRY_DELAYS):
        if ok or not is_transient_network_error(note):
            break
        events.emit("git.retry", op=label, attempt=i + 2, wait_s=delay,
                    error=(note or "")[:200])
        await asyncio.sleep(delay)
        ok, note = await attempt_fn()
    return ok, note


async def push_task_branch(repo, task_id):
    """Push task/<id> to origin. Returns (ok, note)."""
    repo = Path(repo).resolve()
    rc, remotes, _ = await _git(["remote"], cwd=repo, check=False)
    if not remotes.strip():
        return False, "no git remote configured"

    async def once():
        try:
            await _git(["push", "-u", "--force-with-lease", "origin",
                        f"task/{task_id}"], cwd=repo)
        except GitError as exc:
            return False, f"push failed: {exc}"[:400]
        return True, "pushed"
    ok, note = await _retry_transient(f"push task/{task_id}", once)
    return ok, note[:200]


async def open_pr(repo, task_id, title, body, base=None):
    """Open (or find) the PR for task/<id>. Returns (number, url, note)."""
    repo = Path(repo).resolve()
    base = base or config.BASE_BRANCH
    branch = f"task/{task_id}"
    rc, out, err = await _gh(["pr", "list", "--head", branch, "--state", "open",
                              "--json", "number,url"], cwd=repo, wait_quota=False)
    if rc != 0 and is_rate_limited(err + out):
        # GraphQL is spent; REST has its own quota. Same question, asked there.
        rc, out, _ = await _gh(
            ["api", f"repos/{{owner}}/{{repo}}/pulls?head={{owner}}:{branch}&state=open",
             "--jq", "[.[] | {number: .number, url: .html_url}]"],
            cwd=repo, wait_quota=False)
    if rc == 0 and out.strip():
        try:
            existing = json.loads(out)
        except ValueError:
            existing = []
        if existing:
            return existing[0]["number"], existing[0]["url"], "already open"
    created = {}

    async def once():
        rc, out, err = await _gh(
            ["pr", "create", "--base", base, "--head", branch,
             "--title", title, "--body", body], cwd=repo, wait_quota=False)
        if rc != 0 and is_rate_limited(err + out):
            # Open it through REST instead of waiting out the GraphQL reset;
            # only if REST is spent too does the quota wait apply.
            rc, out, err = await _gh(
                ["api", "repos/{owner}/{repo}/pulls", "-f", f"title={title}",
                 "-f", f"head={branch}", "-f", f"base={base}", "-f", f"body={body}",
                 "--jq", ".html_url"], cwd=repo)
            if rc == 0:
                events.emit("git.rest_fallback", op="pr create", branch=branch)
        created["out"] = out
        return rc == 0, err.strip()
    ok, err = await _retry_transient(f"pr create {branch}", once)
    if not ok:
        return None, None, f"gh pr create failed: {err[:200]}"
    out = created.get("out") or ""
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


async def find_pr(repo, task_id, state="open", *, wait_quota=True):
    """PR for task/<id> — (number, url, state) or (None, None, None).

    `state` is open | closed | merged | all. Used on resume to notice a PR
    that already merged (or closed) while the run that would have settled the
    row was dead: the row can then be marked terminal instead of re-imploding
    through publish → alloc, which would reset a branch whose work is already
    on main.

    `wait_quota=False` is the resume probe: a rate-limit wait must not stall
    planning. A non-zero gh exit is "no PR", and the caller keeps today's path.
    """
    rc, out, _ = await _gh(
        ["pr", "list", "--head", f"task/{task_id}", "--state", state,
         "--json", "number,url,state"], cwd=Path(repo).resolve(),
        wait_quota=wait_quota)
    if rc != 0 or not out.strip():
        return None, None, None
    try:
        rows = json.loads(out)
    except ValueError:
        return None, None, None
    if not rows:
        return None, None, None
    pr = rows[0]
    return pr.get("number"), pr.get("url"), pr.get("state")


async def merge_pr(repo, number, method="squash"):
    """Merge a PR. Returns (ok, note). Only ever called after approvals."""
    rc, out, err = await _gh(
        ["pr", "merge", str(number), f"--{method}", "--delete-branch"],
        cwd=Path(repo).resolve(), timeout=240)
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
