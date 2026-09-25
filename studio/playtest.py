"""Human playtesting: play any build of a studio game, log what you find.

Every other check in the studio is a machine's: the scripted playtest, the
perf gate, the blind judge. This module is the one place a HUMAN plays the
game — pick a build (the base branch, or an in-flight `task/<id>` branch),
launch it on the desktop, press F8 in game to log a finding with a screenshot,
answer a three-question survey afterwards, and triage what was found.

Three rules shape it:

  ISOLATED FROM THE PIPELINE. A build is a SNAPSHOT: a `git clone --local`
  of the game's blessed repo, checked out detached at one sha, under
  studio_run_dir(project)/playtest/builds/<sha12>. Never `git worktree add` on
  the blessed clone — a second worktree holding `task/<id>` makes
  gitstore.alloc's `checkout -B` fail for that task — and never anything under
  config.WORKTREE_ROOT, which `reconcile --apply` reaps as orphaned. The
  blessed clone is only ever READ. The F8 overlay is injected into the
  snapshot's project.godot, never into the game repo.

  FINDINGS REACH THE FLEET ONE WAY. Nothing here starts work. A finding the
  operator ACCEPTS (or reopens) is appended to the studio planner's goal on the
  next `studio plan` (open_for_planner / planner_block) — human-triaged,
  explicit, and visible in the taskfile's goal.

  CONTAINED. The dashboard has no authentication (AGENTS.md Rule 6b). Every
  value a request names — project, build, session, finding, screenshot file —
  is checked against a list this module computes; no path is ever taken from a
  request, and the launch argv is fixed.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import config
import errors
import events

PLAYTEST = "playtest"
READY_MARKER = ".arc_playtest_ready"
OVERLAY_SRC = Path(__file__).resolve().parent / "playtest_overlay.gd"
OVERLAY_DIR = "arc_playtest"
AUTOLOAD_NAME = "ArcPlaytest"

CATEGORIES = ("bug", "feel", "balance", "ux", "visual", "perf", "other")
SEVERITIES = (1, 2, 3, 4)          # 1 blocker, 2 major, 3 minor, 4 polish
STATES = ("new", "accepted", "wontfix", "duplicate", "fixed", "verified",
          "reopened")
OPEN_STATES = ("accepted", "reopened")      # what the planner is handed
SURVEY_KEYS = ("fun", "clarity", "difficulty")
MAX_NOTE = 2000
MAX_LINK = 200
SESSIONS_SHOWN = 20
BUILD_CACHE_S = 10.0

SESSION_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")
FINDING_RE = re.compile(r"f-[0-9a-f]{8}")
SHOT_RE = re.compile(r"[A-Za-z0-9._-]+\.png")


class Unavailable(RuntimeError):
    """The machine cannot run a playtest right now (no Godot, no display, or
    the build could not be prepared). The dashboard maps this to 409."""


# --- paths and atomic JSON ----------------------------------------------------
def _root(project, create=False):
    d = config.studio_run_dir(project, create=create) / PLAYTEST
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def _sessions_dir(project):
    return _root(project) / "sessions"


def _read_json(path, default):
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return doc if isinstance(doc, type(default)) else default


def _write_json(path, doc):
    """tmp + os.replace, so a reader never sees half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


# The dashboard serves requests on threads and the CLI is another process, so
# findings.json and session.json are guarded twice: a thread lock (flock is
# per open file description, and two threads opening the lock file would each
# get their own) and an flock for the other processes.
# Re-entrant within a thread: a second flock on a new descriptor of the same
# file would wait on the first forever.
_THREAD_LOCK = threading.RLock()
_HELD = threading.local()


@contextlib.contextmanager
def _locked(project):
    with _THREAD_LOCK:
        if getattr(_HELD, "depth", 0):
            _HELD.depth += 1
            try:
                yield
            finally:
                _HELD.depth -= 1
            return
        lock = _root(project, create=True) / ".lock"
        with open(lock, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            _HELD.depth = 1
            try:
                yield
            finally:
                _HELD.depth = 0
                fcntl.flock(fh, fcntl.LOCK_UN)


# --- builds ---------------------------------------------------------------------
def _git(repo, *args, timeout=30):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, timeout=timeout)


def _repo(project):
    from studio import status
    return status.repo_for(project)


def base_branch(repo):
    """The branch builds are cut from: config.BASE_BRANCH when the game repo
    has it, else main, else whatever HEAD is on."""
    for name in (config.BASE_BRANCH, "main"):
        if name and _git(repo, "rev-parse", "--verify", "-q",
                         f"refs/heads/{name}").returncode == 0:
            return name
    head = _git(repo, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
    return head or "main"


_BUILD_CACHE = {}          # project -> (ts, repo, [build dicts without "snapshot"])
_SCENES_CACHE = {}         # (repo, sha) -> (main_scene, [res:// scene paths]); a sha is immutable
MAX_SCENES = 200


def scenes_at(repo, sha):
    """(main_scene, scenes) of the build at `sha`, read from git — no checkout.

    `scenes` is every .tscn in the tree as a res:// path, main scene first.
    It is the allowlist a launch's `scene` is checked against. The dashboard
    offers a picker because a game's run/main_scene is not always the scene a
    human wants to play: prison-escape-test's is the bare cell-wing builder,
    while the assembled prison with its HUD is scenes/graybox_prison.tscn.
    """
    key = (str(repo), sha)
    hit = _SCENES_CACHE.get(key)
    if hit is not None:
        return hit
    main = ""
    pg = _git(repo, "show", f"{sha}:project.godot")
    if pg.returncode == 0:
        m = re.search(r'(?m)^run/main_scene="([^"]+)"', pg.stdout)
        main = m.group(1) if m else ""
    out = _git(repo, "ls-tree", "-r", "--name-only", sha)
    found = sorted("res://" + f for f in out.stdout.splitlines()
                   if f.endswith(".tscn") and not f.startswith((".", "addons/")))
    scenes = ([main] if main else []) + [s for s in found if s != main]
    res = (main, scenes[:MAX_SCENES])
    if out.returncode == 0:
        _SCENES_CACHE[key] = res
    return res


def _snapshot_dir(project, sha):
    return _root(project) / "builds" / sha[:12]


def builds(project):
    """Every playable build: the base branch first, then task branches newest
    first. Cached for BUILD_CACHE_S because the dashboard polls every 5 s."""
    repo = _repo(project)
    now = time.time()
    hit = _BUILD_CACHE.get(project)
    if hit and hit[1] == repo and now - hit[0] < BUILD_CACHE_S:
        rows = hit[2]
    else:
        rows = []
        if repo and (Path(repo) / ".git").exists():
            base = base_branch(repo)
            # A task branch that exists only on the remote (its worktree was
            # reaped, or it was pushed from another checkout) is still a build
            # someone may need to play. It is listed under the same task/<id>
            # name, and a local branch of that name wins.
            out = _git(repo, "for-each-ref", "--sort=-committerdate",
                       "--format=%(refname)%1f%(objectname)%1f"
                       "%(committerdate:unix)%1f%(subject)",
                       f"refs/heads/{base}", "refs/heads/task/",
                       "refs/remotes/origin/task/").stdout
            seen = set()
            lines = sorted(out.splitlines(), key=lambda ln: ln.startswith("refs/remotes/"))
            for line in lines:
                parts = line.split("\x1f")
                if len(parts) < 4:
                    continue
                ref, sha, ts, subject = parts[0], parts[1], parts[2], parts[3]
                bid = (ref[len("refs/remotes/origin/"):] if ref.startswith("refs/remotes/")
                       else ref[len("refs/heads/"):])
                if bid in seen:
                    continue
                seen.add(bid)
                try:
                    ts = int(ts)
                except ValueError:
                    ts = 0
                rows.append({"id": bid, "ref": ref, "sha": sha,
                             "subject": subject[:200], "ts": ts})
            rows.sort(key=lambda b: (b["id"] != base, -b["ts"]))
        _BUILD_CACHE[project] = (now, repo, rows)
    out = []
    for b in rows:
        main, scenes = scenes_at(repo, b["sha"]) if repo else ("", [])
        out.append(dict(b, main_scene=main, scenes=scenes,
                        snapshot=(_snapshot_dir(project, b["sha"]) / READY_MARKER).exists()))
    return out


def _build(project, build_id):
    for b in builds(project):
        if b["id"] == build_id:
            return b
    return None


def _base_build(project):
    rows = builds(project)
    return rows[0] if rows and not rows[0]["id"].startswith("task/") else None


def inject_overlay(snap):
    """Copy the overlay into the SNAPSHOT and register it as an autoload."""
    snap = Path(snap)
    (snap / OVERLAY_DIR).mkdir(exist_ok=True)
    shutil.copyfile(OVERLAY_SRC, snap / OVERLAY_DIR / "overlay.gd")
    pg = snap / "project.godot"
    text = pg.read_text(encoding="utf-8") if pg.exists() else ""
    line = f'{AUTOLOAD_NAME}="*res://{OVERLAY_DIR}/overlay.gd"'
    if re.search(rf"(?m)^{AUTOLOAD_NAME}=", text):
        text = re.sub(rf"(?m)^{AUTOLOAD_NAME}=.*$", line, text)
    elif re.search(r"(?m)^\[autoload\]\s*$", text):
        text = re.sub(r"(?m)^\[autoload\]\s*$", "[autoload]\n\n" + line, text, count=1)
    else:
        text = text.rstrip("\n") + ("\n\n" if text else "") + f"[autoload]\n\n{line}\n"
    pg.write_text(text, encoding="utf-8")


def ensure_snapshot(project, build_id, *, do_import=True):
    """The snapshot directory for one build, created if needed.

    Immutable per sha: an existing snapshot with the ready marker is reused.
    One without the marker is a half-made leftover and is rebuilt. The clone is
    made in a temporary sibling and renamed into place, so two launches of the
    same build cannot interleave.
    """
    b = _build(project, build_id)
    if b is None:
        raise KeyError(f"unknown build {build_id!r}")
    repo = _repo(project)
    snap = _snapshot_dir(project, b["sha"])
    if (snap / READY_MARKER).exists():
        # The game snapshot stays pinned to its sha. The overlay is ours, and
        # a ready snapshot must pick up a newer overlay or a lighting fix
        # never reaches a build the operator already played.
        inject_overlay(snap)
        return snap
    snap.parent.mkdir(parents=True, exist_ok=True)
    if snap.exists():
        shutil.rmtree(snap, ignore_errors=True)
    tmp = snap.with_name(f"{snap.name}.tmp-{secrets.token_hex(3)}")
    try:
        clone = ["git", "clone", "-q", "--local", "--no-checkout", str(repo), str(tmp)]
        p = subprocess.run(clone, capture_output=True, text=True, timeout=300)
        if p.returncode != 0 and "cross-device" in (p.stderr or "").lower():
            # --local hardlinks the objects, which cannot cross filesystems
            # (ARC_STUDIO_DIR on another mount than the game repo). Copy.
            shutil.rmtree(tmp, ignore_errors=True)
            p = subprocess.run(clone[:4] + ["--no-hardlinks"] + clone[4:],
                               capture_output=True, text=True, timeout=600)
        if p.returncode != 0:
            raise Unavailable(f"could not snapshot {build_id}: "
                              f"{(p.stderr or p.stdout).strip()[:500]}")
        steps = (["git", "-C", str(tmp), "checkout", "-q", "--detach", b["sha"]],
                 # The snapshot must never be able to push back to the blessed clone.
                 ["git", "-C", str(tmp), "remote", "remove", "origin"])
        for argv in steps:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=300)
            if p.returncode != 0:
                raise Unavailable(f"could not snapshot {build_id}: "
                                  f"{(p.stderr or p.stdout).strip()[:500]}")
        if not (tmp / "project.godot").exists():
            raise Unavailable(f"build {build_id} has no project.godot at its root")
        inject_overlay(tmp)
        if do_import:
            _import(tmp)
        (tmp / READY_MARKER).write_text(json.dumps(
            {"build": build_id, "sha": b["sha"], "ts": time.time()}), encoding="utf-8")
        try:
            os.rename(tmp, snap)
        except OSError:
            # Another launch finished the same sha first; theirs is as good.
            if not (snap / READY_MARKER).exists():
                raise
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
    return snap


def _import(snap):
    """Populate the snapshot's import cache. A failed import is NOT fatal: the
    human is about to look at the game anyway, and a partly-imported build
    that runs is more useful than a refusal. The log stays in the snapshot."""
    from studio.engine import godot
    if not godot.godot_bin():
        return
    try:
        out = godot.import_assets(str(snap), timeout=600)
    except godot.GodotError as exc:
        out = f"IMPORT FAILED (continuing):\n{exc}"
    (Path(snap) / ".arc_playtest_import.log").write_text(str(out)[-20000:],
                                                         encoding="utf-8")


# --- sessions -------------------------------------------------------------------
_PROCS = {}                # pid -> Popen, for children this process must reap


def session_ids(project):
    d = _sessions_dir(project)
    if not d.is_dir():
        return []
    return sorted((p.name for p in d.iterdir()
                   if p.is_dir() and SESSION_RE.fullmatch(p.name)), reverse=True)


def _session_path(project, sid):
    return _sessions_dir(project) / sid / "session.json"


def _alive(pid, sess_dir):
    """Is `pid` still the Godot this session launched?"""
    if not pid:
        return False
    proc = _PROCS.get(pid)
    if proc is not None:
        return proc.poll() is None     # reaps; _refresh reads the return code
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    # A pid can be reused, and a dead child of another process lingers as a
    # zombie; /proc says which (when there is a /proc).
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        if stat.rsplit(")", 1)[-1].split()[0] == "Z":
            return False
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
        return f"--arc-playtest-dir={sess_dir}" in cmd
    except (OSError, IndexError):
        return True


def _refresh(project, sid):
    """Load a session, settling its status: a running session whose process
    is gone becomes ended (or failed on a non-zero exit we saw) and its
    findings are ingested. Returns the session dict or None."""
    path = _session_path(project, sid)
    s = _read_json(path, {})
    if not s:
        return None
    if (s.get("status") == "preparing" and sid not in _PREPARING
            and time.time() - float(s.get("started") or 0) > PREPARE_STALE):
        with _locked(project):
            s = _read_json(path, {}) or s
            if s.get("status") == "preparing":
                s.update(status="failed", ended=time.time(),
                         error="preparation was interrupted (the process making "
                               "the snapshot exited)")
                _write_json(path, s)
    if s.get("status") == "running" and not _alive(s.get("pid"), path.parent):
        with _locked(project):
            s = _read_json(path, {}) or s
            if s.get("status") == "running":
                proc = _PROCS.pop(s.get("pid"), None)
                rc = proc.returncode if proc is not None else None
                s["status"] = "failed" if rc not in (None, 0) and not s.get("stopped") else "ended"
                s["exit_code"] = rc
                s["ended"] = time.time()
                _write_json(path, s)
        ingest(project, sid)
    return s


def sessions(project, limit=SESSIONS_SHOWN):
    """The newest sessions, each settled (see _refresh)."""
    out = []
    for sid in session_ids(project)[:limit]:
        s = _refresh(project, sid)
        if s:
            out.append(s)
    return out


def launch(project, build_id, *, wait=True, scene=None):
    """Start one human playtest session of `build_id`. Returns the session.

    Raises KeyError for an unknown build or scene, Unavailable when this
    machine cannot run it. The argv is fixed; nothing in it comes from a
    request except the build id, which must be one builds() lists, and the
    optional `scene`, which must be one of that build's scenes (scenes_at).
    No scene = the game's own run/main_scene.

    The first play of a sha clones and imports it, which can take minutes. With
    wait=False (the dashboard) that work runs on a thread and the session comes
    back "preparing" at once, so a request thread is never held for an import;
    the session turns "running" or "failed" when the thread finishes. A build
    whose snapshot is already made starts inline either way.
    """
    from studio.engine import godot
    exe = godot.godot_bin()
    if not exe:
        raise Unavailable("Godot is not installed here (install it or set ARC_GODOT_BIN)")
    display = config.STUDIO_DISPLAY
    if not display:
        raise Unavailable("no display to play on (set DISPLAY or ARC_STUDIO_DISPLAY)")
    b = _build(project, build_id)
    if b is None:
        raise KeyError(f"unknown build {build_id!r}")
    if scene and scene not in (b.get("scenes") or []):
        raise KeyError(f"unknown scene {scene!r} for build {build_id!r}")
    b = dict(b, launch_scene=scene or "")
    base = _sessions_dir(project)
    base.mkdir(parents=True, exist_ok=True)
    while True:
        sid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        sess = base / sid
        try:
            sess.mkdir()
            break
        except FileExistsError:
            continue
    (sess / "userdata").mkdir()
    s = {"id": sid, "build": build_id, "sha": b["sha"], "started": time.time(),
         "ended": None, "pid": None, "status": "preparing", "survey": None,
         "scene": scene or b.get("main_scene") or ""}
    _write_json(sess / "session.json", s)
    ready = (_snapshot_dir(project, b["sha"]) / READY_MARKER).exists()
    if wait or ready:
        return _start(project, sid, build_id, b, exe, display, raise_errors=True)
    t = threading.Thread(target=_start, args=(project, sid, build_id, b, exe, display),
                         name=f"playtest-{sid}", daemon=True)
    _PREPARING[sid] = t
    t.start()
    return s


def play_env(display, userdata):
    """The environment a played build runs in.

    DISPLAY is always set explicitly: the dashboard usually runs under systemd,
    which starts services with no DISPLAY at all, so inheriting it launched
    nothing (config.STUDIO_DISPLAY finds WSLg's :0 on its own). WSLg's audio
    server is wired in the same way when the caller's environment lacks it.
    """
    env = dict(os.environ, DISPLAY=display, XDG_DATA_HOME=str(userdata))
    if not env.get("PULSE_SERVER") and Path(WSLG_PULSE).exists():
        env["PULSE_SERVER"] = "unix:" + WSLG_PULSE
    return env


WSLG_PULSE = "/mnt/wslg/PulseServer"
_PREPARING = {}            # session id -> thread making its snapshot (this process)
PREPARE_STALE = 900        # a "preparing" session nobody here owns, older than this, died


def _start(project, sid, build_id, b, exe, display, *, raise_errors=False):
    """Make the snapshot and start Godot for a "preparing" session.

    The thread leaves _PREPARING only after the session's final status is
    written, so a reader never sees "preparing" with no owner mid-flight.
    """
    try:
        return _start_session(project, sid, build_id, b, exe, display,
                              raise_errors=raise_errors)
    finally:
        _PREPARING.pop(sid, None)


def _start_session(project, sid, build_id, b, exe, display, *, raise_errors):
    sess = _sessions_dir(project) / sid
    path = sess / "session.json"
    try:
        snap = ensure_snapshot(project, build_id)
        argv = [exe, "--path", str(snap), "--log-file", str(sess / "godot.log")]
        if b.get("launch_scene"):
            argv.append(b["launch_scene"])
        argv += ["--", f"--arc-playtest-dir={sess}", f"--arc-build={b['sha']}"]
        env = play_env(display, sess / "userdata")
        try:
            with open(sess / "stdout.log", "wb") as log:
                proc = subprocess.Popen(argv, cwd=str(snap), env=env, stdout=log,
                                        stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL,
                                        start_new_session=True)
        except OSError as exc:
            raise Unavailable(f"could not start Godot: {exc}") from exc
    except Exception as exc:
        if not isinstance(exc, (Unavailable, KeyError)):
            errors.capture(exc, node="playtest.launch", project=project, session=sid)
        with _locked(project):
            s = _read_json(path, {})
            s.update(status="failed", ended=time.time(), error=str(exc)[:500])
            _write_json(path, s)
        if raise_errors:
            if isinstance(exc, (Unavailable, KeyError)):
                raise
            raise Unavailable(str(exc)[:500]) from exc
        return s
    _PROCS[proc.pid] = proc
    with _locked(project):
        s = _read_json(path, {})
        s.update(pid=proc.pid, status="running")
        _write_json(path, s)
    events.emit("playtest.launch", project=project, session=sid, build=build_id,
                sha=b["sha"])
    return s


def stop(project, sid):
    """Stop a running session (SIGTERM to its process group)."""
    if sid not in session_ids(project):
        raise KeyError(f"unknown session {sid!r}")
    path = _session_path(project, sid)
    s = _read_json(path, {})
    pid = s.get("pid")
    if s.get("status") == "running" and _alive(pid, path.parent):
        with contextlib.suppress(OSError):
            os.killpg(pid, signal.SIGTERM)
        proc = _PROCS.get(pid)
        if proc is not None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
    with _locked(project):
        s = _read_json(path, {}) or s
        if s.get("status") == "running":
            s["stopped"] = True
            _write_json(path, s)
    events.emit("playtest.stop", project=project, session=sid)
    return _refresh(project, sid)


def survey(project, sid, *, fun, clarity, difficulty, note=""):
    """Record the post-session survey: three 1-5 scores and a note."""
    if sid not in session_ids(project):
        raise KeyError(f"unknown session {sid!r}")
    scores = {"fun": fun, "clarity": clarity, "difficulty": difficulty}
    for k, v in scores.items():
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 5:
            raise ValueError(f"{k} must be an integer 1..5")
    path = _session_path(project, sid)
    with _locked(project):
        s = _read_json(path, {})
        if not s:
            raise KeyError(f"unknown session {sid!r}")
        s["survey"] = dict(scores, note=str(note or "")[:MAX_NOTE], ts=time.time())
        _write_json(path, s)
    return s


# --- findings -------------------------------------------------------------------
def _findings_path(project):
    return _root(project) / "findings.json"


def load_findings(project):
    return _read_json(_findings_path(project), {})


def _clean_category(v):
    v = str(v or "").strip().lower()
    return v if v in CATEGORIES else None


def _clean_severity(v):
    if isinstance(v, bool):
        return None
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return v if v in SEVERITIES else None


def _new_finding(fid, *, session, build, sha, source, ts, category, severity,
                 note, scene="", screenshot=None, **extra):
    return {"id": fid, "session": session, "build": build, "sha": sha,
            "source": source, "ts": ts, "category": category,
            "severity": severity, "note": str(note or "")[:MAX_NOTE],
            "scene": str(scene or "")[:300], "screenshot": screenshot,
            "state": "new", "triage_note": "", "link": "", "fixed_in": None,
            "history": [{"state": "new", "ts": ts, "note": ""}], **extra}


def ingest(project, sid):
    """Fold a session's in-game findings.jsonl into the store. Idempotent: a
    line's finding id is derived from (session, line index), and a trailing
    line without its newline is still being written, so it waits."""
    path = _sessions_dir(project) / sid / "findings.jsonl"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").split("\n")[:-1]
    except OSError:
        return 0
    if not lines:
        return 0
    s = _read_json(_session_path(project, sid), {})
    added = 0
    with _locked(project):
        store = load_findings(project)
        for i, line in enumerate(lines):
            fid = "f-" + hashlib.sha1(f"{sid}:{i}".encode()).hexdigest()[:8]
            if fid in store:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            shot = d.get("screenshot")
            shot = shot if isinstance(shot, str) and SHOT_RE.fullmatch(shot) else None
            try:
                ts = float(d.get("ts") or 0) or time.time()
            except (TypeError, ValueError):
                ts = time.time()
            store[fid] = _new_finding(
                fid, session=sid, build=s.get("build", ""), sha=s.get("sha", ""),
                source="game", ts=ts,
                category=_clean_category(d.get("category")) or "other",
                severity=_clean_severity(d.get("severity")) or 3,
                note=d.get("note", ""), scene=d.get("scene", ""), screenshot=shot,
                line=i, game_time=d.get("game_time"), fps=d.get("fps"))
            added += 1
        if added:
            _write_json(_findings_path(project), store)
    return added


def add_finding(project, *, category, severity, note, session=None):
    """A finding typed in the dashboard (or CLI). With a session it belongs to
    that session's build; without one, to the current base build."""
    cat, sev = _clean_category(category), _clean_severity(severity)
    if cat is None:
        raise ValueError(f"category must be one of {list(CATEGORIES)}")
    if sev is None:
        raise ValueError("severity must be 1 (blocker) .. 4 (polish)")
    note = str(note or "").strip()
    if not note:
        raise ValueError("a finding needs a note")
    if session:
        if session not in session_ids(project):
            raise KeyError(f"unknown session {session!r}")
        s = _read_json(_session_path(project, session), {})
        build, sha = s.get("build", ""), s.get("sha", "")
    else:
        b = _base_build(project)
        build, sha = (b["id"], b["sha"]) if b else ("main", "")
    with _locked(project):
        store = load_findings(project)
        fid = "f-" + secrets.token_hex(4)
        while fid in store:
            fid = "f-" + secrets.token_hex(4)
        store[fid] = _new_finding(fid, session=session or None, build=build, sha=sha,
                                  source="dashboard", ts=time.time(), category=cat,
                                  severity=sev, note=note)
        _write_json(_findings_path(project), store)
    return store[fid]


def legal_next(state):
    """The states a finding in `state` may move to."""
    out = [s for s in ("accepted", "wontfix", "duplicate", "fixed") if s != state]
    if state == "fixed":
        out.append("verified")
    if state in ("fixed", "verified", "wontfix"):
        out.append("reopened")
    return out


def triage(project, finding_id, state, *, note="", link=""):
    """Move a finding to `state`. `fixed` records the base build's sha."""
    if state not in STATES or state == "new":
        raise ValueError(f"state must be one of {list(STATES[1:])}")
    with _locked(project):
        store = load_findings(project)
        f = store.get(finding_id)
        if f is None:
            raise KeyError(f"unknown finding {finding_id!r}")
        prev = f.get("state", "new")
        if state not in legal_next(prev):
            raise ValueError(f"cannot move a {prev} finding to {state}")
        now = time.time()
        f["state"] = state
        f["triage_note"] = str(note or "")[:MAX_NOTE]
        if link:
            f["link"] = str(link)[:MAX_LINK]
        if state == "fixed":
            b = _base_build(project)
            f["fixed_in"] = b["sha"] if b else None
        elif state == "reopened":
            f["fixed_in"] = None
        f.setdefault("history", []).append(
            {"state": state, "ts": now, "note": f["triage_note"]})
        _write_json(_findings_path(project), store)
    events.emit("playtest.triage", project=project, finding=finding_id,
                **{"from": prev, "to": state})
    return f


def open_for_planner(project):
    """Accepted and reopened findings, most severe first."""
    rows = [f for f in load_findings(project).values() if f.get("state") in OPEN_STATES]
    return sorted(rows, key=lambda f: (f.get("severity") or 4, f.get("ts") or 0))


def planner_block(project):
    """The text appended to the studio planner's goal, or "" when nothing is open."""
    rows = open_for_planner(project)
    if not rows:
        return ""
    lines = ["", "", "--- OPEN HUMAN PLAYTEST FINDINGS (triaged by the operator; "
             "address where in scope) ---"]
    for f in rows:
        where = ", ".join(x for x in ((f.get("sha") or "")[:7], f.get("scene") or "") if x)
        link = f" [{f['link']}]" if f.get("link") else ""
        lines.append(f"- [sev {f.get('severity')} {f.get('category')}] "
                     f"{' '.join(str(f.get('note', '')).split())}"
                     + (f" ({where})" if where else "") + link)
    return "\n".join(lines)


def shot_path(project, sid, name):
    """A session screenshot, or None. Every part is allowlisted."""
    from studio import status
    if project not in status.projects() or sid not in session_ids(project):
        return None
    if not isinstance(name, str) or not SHOT_RE.fullmatch(name):
        return None
    base = (_sessions_dir(project) / sid).resolve()
    try:
        target = (base / name).resolve()
        target.relative_to(base)
    except (ValueError, OSError):
        return None
    return target if target.is_file() else None


def _shot_url(project, f):
    if not f.get("screenshot") or not f.get("session"):
        return None
    return (f"/api/studio/playtest/shot?project={project}"
            f"&session={f['session']}&file={f['screenshot']}")


# --- the dashboard's view -------------------------------------------------------
def snapshot(project):
    """Everything the Studio → Playtest view shows. Never raises.

    Not strictly read-only like the rest of studio.status: settling a session
    whose Godot has exited writes its session.json and ingests its findings,
    once. It emits no events, so polling does not flood the log.
    """
    try:
        from studio.engine import godot
        rows = sessions(project)
        for s in rows:
            # Idempotent and cheap (a few small files): covers findings logged
            # while still playing, and a settle interrupted before its ingest.
            ingest(project, s["id"])
        store = load_findings(project)
        per = {}
        for f in store.values():
            per[f.get("session")] = per.get(f.get("session"), 0) + 1
        findings = sorted(store.values(), key=lambda f: f.get("ts") or 0, reverse=True)
        counts = {s: 0 for s in STATES}
        for f in findings:
            counts[f.get("state", "new")] = counts.get(f.get("state", "new"), 0) + 1
        counts["open"] = counts["new"] + counts["accepted"] + counts["reopened"]
        return {
            "godot": bool(godot.godot_bin()),
            "display": config.STUDIO_DISPLAY or "",
            "builds": builds(project),
            "sessions": [dict(s, findings=per.get(s["id"], 0)) for s in rows],
            "findings": [dict(f, screenshot_url=_shot_url(project, f),
                              next=legal_next(f.get("state", "new")))
                         for f in findings],
            "counts": counts,
        }
    except Exception as exc:                                 # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:500]}
