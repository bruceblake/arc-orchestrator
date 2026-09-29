"""Every task is a GitHub issue: opened at start, updated at every step,
closed by its PR.

Before this, a fleet task left no public record until its pull request opened
at the very end — gate failures, rejections, swaps and escalations lived only
in local logs. Now each taskfile gets one tracking (epic) issue with the DAG
as a checklist, and each task gets an issue that the pipeline comments on at
every state transition (never on progress heartbeats) and that its PR closes
with `Closes #N`.

Everything goes through the REST API (`gh api`), which has its own quota,
separate from the GraphQL one `gh pr`/`gh issue` spend, and through
gitstore._gh so the quota wait applies. Functions here RAISE GhIssueError on
a gh failure; the pipeline wiring (code_tasks) is what makes them
best-effort. Mapping (repo, taskfile, task) -> issue lives in `task_issues`
in config.DB_PATH (or the run's --db, use_db); task '' is the epic.
"""
import datetime
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import urllib.parse
from contextlib import closing
from pathlib import Path

import config
import gitstore

SCHEMA = """CREATE TABLE IF NOT EXISTS task_issues(
  repo TEXT NOT NULL,
  taskfile TEXT NOT NULL,
  task TEXT NOT NULL,
  issue INTEGER NOT NULL,
  epic INTEGER,
  created_at TEXT NOT NULL,
  PRIMARY KEY (repo, taskfile, task)
)"""

STATUSES = ("pending", "implementing", "in-review", "merged", "failed",
            "conflict", "skipped")
BODY_CAP = 6000
_LABEL_COLORS = {"arc-task": "5319e7", "arc:": "c5def5", "model:": "fbca04"}
_labels_done = set()          # (repo, name) created or seen this process
_enabled_cache = {}
_PER_PAGE = 100
_MAX_PAGES = 100              # 10k open issues: past that, fail loudly
_DB = None                    # the run's selected database (use_db)
_slug_cache = {}


class GhIssueError(RuntimeError):
    pass


# --- storage ------------------------------------------------------------------

def use_db(path):
    """Point issue storage and status reads at the run's own database (the
    one `code run --db` selected); None = config.DB_PATH."""
    global _DB
    _DB = str(path) if path and str(path) != ":memory:" else None


def _db():
    return _DB or config.DB_PATH


def _connect():
    conn = sqlite3.connect(_db(), timeout=30, isolation_level=None)
    conn.execute(SCHEMA)
    return conn


def _key(repo, taskfile):
    return str(Path(repo).resolve()), str(Path(taskfile).resolve()) if taskfile else ""


def issue_for(repo, taskfile, task):
    """The recorded issue number for a task ('' = the epic), or None."""
    r, tf = _key(repo, taskfile)
    with closing(_connect()) as conn:
        row = conn.execute("SELECT issue FROM task_issues WHERE repo=? AND "
                           "taskfile=? AND task=?", (r, tf, task)).fetchone()
    return row[0] if row else None


def _record(repo, taskfile, task, issue, epic=None):
    r, tf = _key(repo, taskfile)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    with closing(_connect()) as conn:
        conn.execute("INSERT INTO task_issues(repo, taskfile, task, issue, epic, "
                     "created_at) VALUES (?,?,?,?,?,?) ON CONFLICT(repo, taskfile, "
                     "task) DO UPDATE SET issue=excluded.issue, epic=excluded.epic",
                     (r, tf, task, int(issue), epic, now))


def rows_for_task(task):
    """Every recorded issue for a task id, across repos/taskfiles (CLI show)."""
    with closing(_connect()) as conn:
        cur = conn.execute("SELECT repo, taskfile, task, issue, epic, created_at "
                           "FROM task_issues WHERE task=? ORDER BY created_at",
                           (task,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def recorded(taskfile):
    """{task id: {issue, epic, repo}} for one taskfile. ``''`` is the epic."""
    if not taskfile:
        return {}
    path = str(Path(taskfile).resolve())
    try:
        with closing(_connect()) as conn:
            rows = conn.execute(
                "SELECT task, issue, epic, repo FROM task_issues WHERE taskfile=?",
                (path,)).fetchall()
    except sqlite3.Error:
        return {}
    return {task: {"issue": issue, "epic": epic, "repo": repo}
            for task, issue, epic, repo in rows}


def row_for_task(task, taskfile=None):
    """The task_issues row for one task, or None when it is ambiguous.

    An explicit taskfile that matches no row is no match. It does not
    fall through to the sole row for that task id, which may belong to
    another project. With no taskfile, a single row is returned.
    """
    if not task:
        return None
    rows = rows_for_task(task)
    if taskfile:
        want = str(Path(taskfile).resolve())
        match = [r for r in rows if r["taskfile"] == want]
        return match[-1] if match else None
    return rows[-1] if len(rows) == 1 else None


def link_for_task(task, taskfile=None):
    """(issue number, url) for one task, or (None, '')."""
    if taskfile:
        rec = recorded(taskfile).get(task)
        if rec:
            return rec["issue"], issue_url(rec.get("repo"), rec["issue"])
    row = row_for_task(task, taskfile)
    if not row:
        return None, ""
    return row["issue"], issue_url(row.get("repo"), row["issue"])


def epic_for_name(name):
    """The epic whose repo directory or taskfile stem is ``name``, or None.

    The last match wins, including an inactive taskfile that only shares the
    repo name. Standup comments use ``epics_for_taskfiles`` instead."""
    if not name:
        return None
    try:
        with closing(_connect()) as conn:
            rows = conn.execute(
                "SELECT repo, taskfile, issue FROM task_issues WHERE task='' "
                "ORDER BY created_at").fetchall()
    except sqlite3.Error:
        return None
    hit = None
    for repo, tf, issue in rows:
        if Path(repo).name == name or Path(tf).stem == name:
            hit = {"repo": repo, "taskfile": tf, "issue": issue}
    return hit


def epics_for_taskfiles(taskfiles):
    """Epic rows for these taskfiles, one per taskfile that has one.

    A project can hold several taskfiles in one repo. Callers pass the
    active taskfiles and get only those epics."""
    out, seen = [], set()
    for tf in taskfiles or []:
        if not isinstance(tf, str) or not tf.strip():
            continue
        rec = recorded(tf).get("")
        if not rec or not rec.get("issue"):
            continue
        key = (str(rec.get("repo") or ""), int(rec["issue"]))
        if key in seen:
            continue
        seen.add(key)
        out.append({"repo": rec.get("repo") or "",
                    "taskfile": str(Path(tf).resolve()),
                    "issue": int(rec["issue"])})
    return out


def _slug_from_remote(url):
    url = (url or "").strip()
    if url.endswith(".git"):
        url = url[:-4]
    m = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+)$", url)
    return m.group(1) if m else ""


def github_slug(repo):
    """``owner/name`` from the repo's origin, or '' . Cached, including misses."""
    if not repo:
        return ""
    try:
        key = str(Path(repo).resolve())
    except OSError:
        return ""
    if key in _slug_cache:
        return _slug_cache[key]
    slug = ""
    try:
        cp = subprocess.run(
            ["git", "-C", key, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5, env=config.child_env())
        if cp.returncode == 0:
            slug = _slug_from_remote(cp.stdout)
    except (OSError, subprocess.SubprocessError):
        slug = ""
    _slug_cache[key] = slug
    return slug


def issue_url(repo, number):
    """https://github.com/<owner>/<name>/issues/<n>, or '' when unknown."""
    if number in (None, ""):
        return ""
    slug = github_slug(repo)
    if not slug:
        return ""
    return f"https://github.com/{slug}/issues/{int(number)}"


def _task_rows(taskfile):
    """{task id: {status, model}} for a taskfile; {} when unknown.

    `model` is the implementer the row recorded (after a swap or escalation).
    A table created without that column — older tests — still yields status."""
    if not taskfile:
        return {}

    def read(conn, key):
        try:
            return conn.execute(
                "SELECT id, status, model FROM code_tasks WHERE taskfile=?",
                (key,)).fetchall()
        except sqlite3.OperationalError:
            return [(i, s, None) for i, s in conn.execute(
                "SELECT id, status FROM code_tasks WHERE taskfile=?", (key,))]

    try:
        with closing(sqlite3.connect(_db(), timeout=30)) as conn:
            rows = read(conn, str(taskfile))
            if not rows and str(taskfile) != str(Path(taskfile).resolve()):
                rows = read(conn, str(Path(taskfile).resolve()))
    except sqlite3.Error:
        return {}
    return {i: {"status": s, "model": m} for i, s, m in rows}


def _task_statuses(taskfile):
    """{task id: code_tasks status} for a taskfile; {} when unknown."""
    return {k: v["status"] for k, v in _task_rows(taskfile).items()}


# --- enablement ---------------------------------------------------------------

def enabled(repo):
    """config.GH_ISSUES: off | on | auto (= origin is on GitHub)."""
    mode = config.GH_ISSUES
    if mode in ("off", "0", "false", "no"):
        return False
    if mode in ("on", "1", "true", "yes"):
        return True
    key = str(Path(repo).resolve())
    if key not in _enabled_cache:
        try:
            url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=key,
                                 capture_output=True, text=True, timeout=10,
                                 env=config.child_env()).stdout
        except (OSError, subprocess.SubprocessError):
            url = ""
        _enabled_cache[key] = "github.com" in url
    return _enabled_cache[key]


# --- redaction ----------------------------------------------------------------

_SECRET_NAME = re.compile(r"TOKEN|KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL", re.I)
_SECRET_SHAPES = re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|"
                            r"sk-[A-Za-z0-9_-]{16,})")


def redact(text, repo=None):
    """Repo-relative paths only; no home-directory paths, no env secrets."""
    text = str(text or "")
    for name, value in os.environ.items():
        if value and len(value) >= 8 and _SECRET_NAME.search(name):
            text = text.replace(value, "[redacted]")
    text = _SECRET_SHAPES.sub("[redacted]", text)
    roots = [re.escape(str(Path(config.WORKTREE_ROOT).expanduser())) + r"/[^/\s]+/[^/\s]+"]
    if repo:
        roots.append(re.escape(str(Path(repo).resolve())))
        roots.append(re.escape(str(repo).rstrip("/")))
    for root in roots:
        text = re.sub(root + r"/", "", text)
        text = re.sub(root + r"(?=[\s'\":,)]|$)", ".", text)
    home = re.escape(str(Path.home()))
    # Anything else under $HOME: keep the file name, drop the directories.
    text = re.sub(home + r"(?:/[^\s'\":,)]*)?",
                  lambda m: "…/" + m.group(0).rstrip("/").rsplit("/", 1)[-1]
                  if m.group(0) != str(Path.home()) else "~", text)
    return text


def _cap(text, cap=BODY_CAP):
    if len(text) <= cap:
        return text
    note = f"\n… [truncated at {cap} chars]"
    return text[:cap - len(note)] + note


# --- gh -----------------------------------------------------------------------

async def _api(repo, method, path, fields=(), jq=None):
    """`gh api` (REST). fields: (key, value) pairs; `key[]` builds arrays.
    Returns parsed JSON (or the jq text); raises GhIssueError."""
    args = ["api", "-X", method, path]
    for k, v in fields:
        args += ["-f", f"{k}={v}"]
    if jq:
        args += ["--jq", jq]
    rc, out, err = await gitstore._gh(args, cwd=Path(repo).resolve())
    if rc != 0:
        # gh puts "Validation Failed (HTTP 422)" on stderr and the JSON body
        # (errors[].code) on stdout. `err or out` dropped already_exists.
        raise GhIssueError(f"gh api {method} {path}: "
                           f"{_api_error_text(err, out)[:300]}")
    if jq:
        return out.strip()
    try:
        return json.loads(out) if out.strip() else None
    except ValueError:
        return None


def _api_error_text(err, out):
    """Both gh streams, stderr first. Empty and duplicate chunks are dropped."""
    parts = []
    for raw in (err, out):
        text = (raw or "").strip()
        if text and text not in parts:
            parts.append(text)
    return "\n".join(parts)


def _duplicate_label(text):
    """True when a label POST failed because that name is already there."""
    low = str(text).lower()
    return "already_exists" in low or "already exists" in low


def _color(name):
    for prefix, color in _LABEL_COLORS.items():
        if name.startswith(prefix):
            return color
    return "ededed"


async def ensure_labels(repo, names):
    """Create missing labels once per process; 'already exists' is fine."""
    key = str(Path(repo).resolve())
    for name in names:
        if (key, name) in _labels_done:
            continue
        try:
            await _api(repo, "POST", "repos/{owner}/{repo}/labels",
                       [("name", name), ("color", _color(name))])
        except GhIssueError as exc:
            if not _duplicate_label(exc):
                raise
        _labels_done.add((key, name))


# --- rendering ----------------------------------------------------------------

def label_status(status):
    """code_tasks status -> the arc:* label suffix."""
    return {"running": "implementing", "in_review": "in-review"}.get(
        status or "pending", (status or "pending").replace("_", "-"))


# Human attention, not a pipeline status. ``swap_labels`` replaces every
# other ``arc:*`` label; this one stays until a person clears it.
HUMAN_LABEL = "arc:needs-human"


def _project(taskfile, project=None):
    """(name, project dict) from the raw taskfile; tolerant of a missing file."""
    try:
        data = json.loads(Path(taskfile).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        data = {}
    proj = data.get("project") or {}
    name = project or proj.get("name") or proj.get("title") or (
        Path(taskfile).stem if taskfile else "project")
    return name, proj


def dashboard_link(taskfile):
    """The dashboard's project detail view for this taskfile (the `x` hash
    parameter opens it; `q` would only filter the list), or '' when
    config.DASHBOARD_PUBLIC_URL is unset."""
    if not config.DASHBOARD_PUBLIC_URL or not taskfile:
        return ""
    return (f"{config.DASHBOARD_PUBLIC_URL}/#"
            + urllib.parse.urlencode({"x": Path(taskfile).name}))


def closes_refs(issue, epic=None):
    """The PR-body lines that make a merge close the task issue."""
    return f"Closes #{issue}" + (f"\nPart of #{epic}" if epic else "")


async def ensure_pr_closes(repo, pr, issue, epic=None):
    """Make an open PR's body carry `Closes #<issue>`. A resumed task
    re-attaches to a PR that may predate issues (or was opened with an empty
    body); without the keyword its merge would leave the issue open.
    Returns True when the body was changed."""
    got = await _api(repo, "GET", f"repos/{{owner}}/{{repo}}/pulls/{pr}") or {}
    body = got.get("body") or ""
    if re.search(rf"(?i)\b(close[sd]?|fix(e[sd])?|resolve[sd]?) #{issue}\b", body):
        return False
    new = (body.rstrip() + "\n\n" if body.strip() else "") + closes_refs(issue, epic)
    await _api(repo, "PATCH", f"repos/{{owner}}/{{repo}}/pulls/{pr}", [("body", new)])
    return True


def project_name(taskfile):
    """The name issue titles use: project.name, else the taskfile stem."""
    return _project(taskfile)[0]


def _raw_tasks(proj, data_tasks=None):
    tasks = data_tasks if data_tasks is not None else proj.get("tasks") or []
    if isinstance(tasks, dict):
        return [dict(v, id=k) for k, v in tasks.items()]
    return list(tasks)


def epic_body(repo, project, taskfile, tasks=None):
    """The tracking issue body: goal, the DAG as a checklist, the pattern."""
    name, proj = _project(taskfile, project)
    statuses = _task_statuses(taskfile)
    lines = [marker(taskfile, ""),
             f"Tracking issue for the fleet project **{name}**.", ""]
    goal = proj.get("goal") or proj.get("description") or proj.get("title")
    if goal:
        lines += ["## Goal", "", str(goal), ""]
    lines += ["## Tasks", ""]
    for t in _raw_tasks(proj, tasks):
        tid = t.get("id", "")
        n = issue_for(repo, taskfile, tid)
        ref = f"#{n} " if n else ""
        box = "x" if statuses.get(tid) == "merged" else " "
        lines.append(f"- [{box}] {ref}{t.get('title', tid)} (`{tid}`, "
                     f"{t.get('model', '?')} → {t.get('reviewer', '?')})")
    if proj.get("pattern"):
        lines += ["", f"Pattern: `{proj['pattern']}`"]
    if config.DASHBOARD_PUBLIC_URL:
        lines += ["", f"Dashboard: {dashboard_link(taskfile)}"]
    lines += ["", "_Maintained by the ARC orchestrator; edits are overwritten._"]
    return _cap(redact("\n".join(lines), repo))


def task_body(repo, project, taskfile, task, epic=None):
    deps = []
    for d in task.get("deps") or []:
        n = issue_for(repo, taskfile, d)
        deps.append(f"#{n} (`{d}`)" if n else f"`{d}`")
    lines = [marker(taskfile, task.get("id", "")),
             f"Fleet task `{task.get('id', '')}` of **{project}**.", "",
             "## Spec", "", str(task.get("prompt") or "(no prompt)"), "",
             f"**Model:** {task.get('model', '?')} · **Reviewer:** "
             f"{task.get('reviewer', '?')}",
             f"**Depends on:** {', '.join(deps) if deps else 'nothing'}"]
    if task.get("verify_cmd"):
        lines += ["", "**Verify gate:**", "", "```sh", str(task["verify_cmd"]), "```"]
    if task.get("files_hint"):
        lines += ["", "**Files:** " + ", ".join(f"`{f}`" for f in task["files_hint"])]
    if epic:
        lines += ["", f"Part of #{epic}"]
    return _cap(redact("\n".join(lines), repo))


def task_title(project, task):
    return f"[{project}] {task.get('title') or task.get('id')}"


# --- issues -------------------------------------------------------------------

def _taskfile_id(taskfile):
    """A stable, path-free id for a taskfile: two taskfiles with the same
    project name and task titles must never share an issue."""
    key = str(Path(taskfile).resolve()) if taskfile else ""
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def marker(taskfile, task):
    """Hidden first line of every body we write: which taskfile and task
    the issue belongs to ('epic' for the tracking issue)."""
    return f"<!-- arc: taskfile={_taskfile_id(taskfile)} task={task or 'epic'} -->"


_MARKER_RE = re.compile(r"<!-- arc: taskfile=\w+ task=\S+ -->")


def _ours(body, taskfile, task):
    """May an issue with this body be adopted for (taskfile, task)? Yes when
    it carries our marker, or no arc marker at all (a human opened it). An
    issue marked for another taskfile or task is someone else's."""
    found = _MARKER_RE.search(body or "")
    return found is None or found.group(0) == marker(taskfile, task)


async def _find_open(repo, title, taskfile=None, task=""):
    """An open issue with exactly this title (pull requests excluded) that
    belongs to this taskfile and task, searched across EVERY page — a match
    on page 2 must not be duplicated."""
    for page in range(1, _MAX_PAGES + 1):
        found = await _api(repo, "GET", "repos/{owner}/{repo}/issues?state=open"
                           f"&per_page={_PER_PAGE}&page={page}") or []
        for it in found:
            if (it.get("title") == title and "pull_request" not in it
                    and _ours(it.get("body"), taskfile, task)):
                return it.get("number")
        if len(found) < _PER_PAGE:
            return None
    raise GhIssueError(f"more than {_MAX_PAGES * _PER_PAGE} open issues; "
                       f"refusing to guess whether {title!r} exists")


async def _create(repo, title, body, labels=()):
    fields = [("title", title), ("body", body)] + [("labels[]", l) for l in labels]
    made = await _api(repo, "POST", "repos/{owner}/{repo}/issues", fields)
    if not made or not made.get("number"):
        raise GhIssueError(f"issue create returned no number for {title!r}")
    return int(made["number"])


async def ensure_epic(repo, project, taskfile, tasks=None):
    """One tracking issue per taskfile, titled '[arc] <project>'; body kept
    current in place. Returns its number."""
    name, _ = _project(taskfile, project)
    title = f"[arc] {name}"
    body = epic_body(repo, name, taskfile, tasks)
    n = issue_for(repo, taskfile, "")
    if n is None:
        n = await _find_open(repo, title, taskfile, "")
        if n is None:
            await ensure_labels(repo, ["arc-epic"])
            return _record_new(repo, taskfile, "", await _create(
                repo, title, body, ["arc-epic"]), None)
        _record(repo, taskfile, "", n)
    await _api(repo, "PATCH", f"repos/{{owner}}/{{repo}}/issues/{n}",
               [("body", body)])
    return n


def _record_new(repo, taskfile, task, n, epic):
    _record(repo, taskfile, task, n, epic)
    return n


async def ensure_task_issue(repo, project, taskfile, task, status="pending",
                            epic=None):
    """Issue for one task: DB row, else an open issue with the same title,
    else a new one. Returns its number."""
    tid = task.get("id", "")
    n = issue_for(repo, taskfile, tid)
    if n is not None:
        return n
    epic = epic if epic is not None else issue_for(repo, taskfile, "")
    title = task_title(project, task)
    labels = ["arc-task", f"arc:{label_status(status)}",
              f"model:{task.get('model', 'unknown')}"]
    n = await _find_open(repo, title, taskfile, tid)
    if n is not None:
        # Adopted (a human opened it, or our row was lost): give it the same
        # spec, epic link and labels a fresh issue would have, keeping any
        # labels it already carries except a stale arc:* status.
        await ensure_labels(repo, labels)
        have = await _api(repo, "GET",
                          f"repos/{{owner}}/{{repo}}/issues/{n}/labels") or []
        keep = [l.get("name") for l in have
                if l.get("name") and not l["name"].startswith(("arc:", "model:"))]
        merged = keep + [l for l in labels if l not in keep]
        await _api(repo, "PATCH", f"repos/{{owner}}/{{repo}}/issues/{n}",
                   [("body", task_body(repo, project, taskfile, task, epic))]
                   + [("labels[]", l) for l in merged])
        return _record_new(repo, taskfile, tid, n, epic)
    await ensure_labels(repo, labels)
    n = await _create(repo, title, task_body(repo, project, taskfile, task, epic),
                      labels)
    return _record_new(repo, taskfile, tid, n, epic)


def comment_text(kind, body="", *, attempt=None, model=None, repo=None):
    meta = ", ".join(x for x in (f"attempt {attempt}" if attempt else "",
                                 str(model) if model else "") if x)
    head = f"**{kind}**" + (f" ({meta})" if meta else "")
    body = redact(body, repo).strip()
    return _cap(head + ("\n\n" + body if body else ""))


async def comment(repo, issue, kind, body="", *, attempt=None, model=None):
    """One structured comment: an emoji-free bold header, then the body
    (capped, paths redacted to repo-relative). ``model`` is the header
    identity — a board mirror passes the author there."""
    text = comment_text(kind, body, attempt=attempt, model=model, repo=repo)
    await _api(repo, "POST", f"repos/{{owner}}/{{repo}}/issues/{issue}/comments",
               [("body", text)])


def run_sync(coro):
    """Drive one gh_issues coroutine from the board or the captain.

    Bounded by ``GH_ISSUES_TIMEOUT``. ``asyncio.wait_for`` cancels the
    coroutine, so a quota sleep inside ``gitstore._gh`` cannot outlive the
    budget. A running loop cannot nest ``asyncio.run``; that path uses a
    daemon thread and does not join it after the timeout. Waiting on a
    ``ThreadPoolExecutor`` worker after ``Future.result`` expires blocked
    the caller for the rest of the quota wait."""
    import asyncio
    timeout = config.GH_ISSUES_TIMEOUT

    async def bounded():
        return await asyncio.wait_for(coro, timeout)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(bounded())

    import threading
    box, done = {}, threading.Event()

    def worker():
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            box["result"] = loop.run_until_complete(bounded())
        except BaseException as exc:  # noqa: BLE001 — re-raised in the caller
            box["error"] = exc
        finally:
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass
            done.set()

    threading.Thread(target=worker, daemon=True).start()
    # One second past the budget so wait_for can cancel and the thread can
    # record the error. A stuck non-cancellable call does not hold the caller.
    if not done.wait(timeout + 1):
        raise TimeoutError(f"gh_issues exceeded {timeout}s")
    if "error" in box:
        raise box["error"]
    return box.get("result")


async def swap_labels(repo, issue, status=None, model=None):
    """Swap the issue's arc:* label for arc:<status> and/or its model:* label
    for model:<model>, keeping every other label. One GET, at most one PUT."""
    wanted = {}
    if status:
        wanted["arc:"] = f"arc:{label_status(status)}"
    if model:
        wanted["model:"] = f"model:{model}"
    if not wanted:
        return
    await ensure_labels(repo, list(wanted.values()))
    have = await _api(repo, "GET", f"repos/{{owner}}/{{repo}}/issues/{issue}/labels")
    names = [l.get("name") for l in have or [] if l.get("name")]
    prefixes = tuple(wanted)
    # arc:needs-human starts with "arc:" but it is not the status label.
    keep = [n for n in names if n == HUMAN_LABEL or not n.startswith(prefixes)]
    for n in wanted.values():
        if n not in keep:
            keep.append(n)
    if sorted(keep) == sorted(names):
        return
    await _api(repo, "PUT", f"repos/{{owner}}/{{repo}}/issues/{issue}/labels",
               [("labels[]", n) for n in keep])


async def add_label(repo, issue, name):
    """Add one label, keeping every label the issue already carries."""
    await ensure_labels(repo, [name])
    have = await _api(repo, "GET", f"repos/{{owner}}/{{repo}}/issues/{issue}/labels")
    names = [l.get("name") for l in have or [] if l.get("name")]
    if name in names:
        return
    await _api(repo, "PUT", f"repos/{{owner}}/{{repo}}/issues/{issue}/labels",
               [("labels[]", n) for n in names + [name]])


async def set_status(repo, issue, status):
    """Swap the issue's arc:* label for arc:<status>, keeping every other."""
    await swap_labels(repo, issue, status=status)


async def set_model(repo, issue, model):
    """Swap the model:* label to the implementer now working the task
    (a usage swap or an escalation changes it)."""
    await swap_labels(repo, issue, model=model)


def _pr_marker(pr_number):
    return f"<!-- arc: pr={pr_number} -->"


async def _has_comment(repo, issue, needle):
    for page in range(1, _MAX_PAGES + 1):
        got = await _api(repo, "GET",
                         f"repos/{{owner}}/{{repo}}/issues/{issue}/comments"
                         f"?per_page={_PER_PAGE}&page={page}") or []
        if any(needle in (c.get("body") or "") for c in got):
            return True
        if len(got) < _PER_PAGE:
            return False
    return False


async def link_pr(repo, issue, pr_number, url=""):
    """Comment the PR link once per (issue, PR). Idempotent by reading the
    issue's own comments, so a backfilled issue or a link whose first post
    failed still gets it on the next publish. Returns True when posted."""
    if await _has_comment(repo, issue, _pr_marker(pr_number)):
        return False
    await comment(repo, issue, "pull request opened",
                  f"#{pr_number} {url}".strip() + " — merging it closes this issue.\n"
                  + _pr_marker(pr_number))
    return True


async def close_failed(repo, issue, reason):
    """Comment the reason and label arc:failed. The issue stays OPEN: a
    failed task needs a human, and a closed issue hides it."""
    await comment(repo, issue, "task failed", reason)
    await set_status(repo, issue, "failed")


async def close_merged(repo, issue, reason=None):
    """Label arc:merged. With a PR, GitHub closes the issue itself through
    `Closes #N`, so closing here too would race it for nothing. Without one
    (an empty diff counts as merged) no keyword ever fires: `reason` is then
    commented and the issue closed here."""
    await set_status(repo, issue, "merged")
    if reason:
        await comment(repo, issue, "merged without a pull request", reason)
        await _api(repo, "PATCH", f"repos/{{owner}}/{{repo}}/issues/{issue}",
                   [("state", "closed"), ("state_reason", "completed")])


# --- backfill -----------------------------------------------------------------

async def sync_taskfile(taskfile):
    """Backfill the epic and every task issue of an existing taskfile from its
    code_tasks rows. Returns {task id: issue number, '': epic}."""
    from code_tasks import load_taskfile
    ts = load_taskfile(taskfile)
    repo = ts["repo"]
    name, _ = _project(taskfile)
    rows = _task_rows(taskfile)
    epic = await ensure_epic(repo, name, taskfile)
    out = {"": epic}
    for tid in topo_ids(ts["tasks"]):
        t = dict(ts["tasks"][tid], id=tid)
        row = rows.get(tid) or {}
        st = row.get("status") or "pending"
        recorded = row.get("model")
        if recorded:
            t["model"] = recorded
        fresh = issue_for(repo, taskfile, tid) is None
        n = await ensure_task_issue(repo, name, taskfile, t, st, epic)
        if not fresh or st != "pending":
            await set_status(repo, n, st)
        if not fresh and recorded:
            await set_model(repo, n, recorded)
        if st == "merged":
            await _api(repo, "PATCH", f"repos/{{owner}}/{{repo}}/issues/{n}",
                       [("state", "closed")])
        if st in ("in_review", "conflict"):
            number, url, _state = await gitstore.find_pr(repo, tid, state="open")
            if number:
                await ensure_pr_closes(repo, number, n, epic)
                await link_pr(repo, n, number, url or "")
        out[tid] = n
    await ensure_epic(repo, name, taskfile)      # now with every #N ticked
    return out


def topo_ids(tasks):
    """Deps before dependents, so a body can link its deps' issues."""
    seen, order = set(), []

    def visit(tid):
        if tid in seen or tid not in tasks:
            return
        seen.add(tid)
        for d in tasks[tid].get("deps") or []:
            visit(d)
        order.append(tid)
    for tid in tasks:
        visit(tid)
    return order
