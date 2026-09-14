"""Code-graph context for harness runs, via Graft (github.com/trailhq/Graft).

WHY. Before a harness edits anything it has to FIND the code, and the way it
does that by default is grep -> read -> grep again, each turn re-sending the
whole conversation to the API. On this fleet that search is paid for twice:
once in ARC tokens, and once in wall-clock against per-model concurrency caps
that are shared with the rest of campus (see config.py, "concurrency"). The
implementer prompt already begs the model to "locate code with grep FIRST"
and "never read a whole file" — this module makes those instructions cheap to
follow instead of merely stern.

WHAT. Graft parses the worktree with tree-sitter into a symbol graph (who
defines what, who calls whom) — deterministic, no model, no API key, ~2 s cold
for this repo and ~0.2 s incremental. Against that graph:

  * `hints(task, worktree)`  -> the top-N file:line spans a task's title and
    prompt point at, injected into the implementer prompt as "WHERE TO LOOK",
    so the agent's first tool call is a targeted read, not a search.
  * `blast(worktree, base)`  -> what depends on the changed symbols, and which
    tests reach them, injected into the reviewer prompts. The PR reviewer is
    told to "consider what calls the changed functions"; now it is handed the
    list instead of grepping for it on the fleet's clock.
  * `repo_map(repo)`         -> a token-budgeted orientation for the planner,
    which otherwise spends its first minutes (on the slowest model) reading
    the tree to decide what the tasks are.
  * `tooling_prose()`        -> tells the harness the `graft ask / skeleton /
    callers / grep` commands exist in its worktree, so its remaining searches
    are one precise call rather than a grep ladder.

Graft's published numbers (162-run sweep): 46% fewer tool calls, 42% fewer
tokens, 60% less wall-clock, equal correctness. Treat them as an upper bound;
the fleet's own before/after comes from the event log (`graft.*` events).

WHERE THE GRAPH LIVES. Never inside the worktree. `graft build` run in a
worktree adds `graft/` to `.gitignore` and drops a `.ignore` file — two
unrelated files in the task's diff, which the SCOPE rule tells reviewers to
reject. The graph goes under `WORKTREE_ROOT/.graft/<repo>/<task>/` instead
(measured: the worktree stays `git status`-clean). Every call passes it as
the global `--dir` flag — `build` and `ask` honour the `GRAFT_DIR` env var,
`map` and `blast` do not (graft 0.x), and the flag is honoured by all four.
The harness gets the same path as `GRAFT_DIR` and is told to pass it.

FAILURE MODE. Optional, always. No binary, a parse error, a timeout, an odd
JSON shape — every entry point returns its empty value and the run proceeds
exactly as before this module existed. A context helper must never be the
reason a task fails.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

import config
import events

log = logging.getLogger("arc.graft")

# Graft prefixes every query with a "[graft] tokens saved ≈ ..." banner meant
# for an interactive Claude session ("tell the user the total ... this
# turn"). Nothing in a fleet prompt should ask a headless reviewer to report
# token savings to a user who is not there.
_BANNER = re.compile(r"^\[graft\].*$", re.M)
# `graft blast --format markdown` is a PR-comment: it carries a mermaid
# diagram and a git-blame "who knows this code" table. A reviewer needs the
# dependents and the test signal, not a picture or a list of people to tag.
_MERMAID = re.compile(r"```mermaid.*?```\s*", re.S)
_WHO_KNOWS = re.compile(
    r"<details>\s*<summary><strong>Who knows this code</strong>.*?</details>\s*", re.S)

_bin_cache = {"checked": False, "path": None}

# Not tunables: a graph that takes longer than this to build is a repo graft
# cannot help with in a fleet's time budget, and a query past this is hung.
BUILD_TIMEOUT = 120.0
QUERY_TIMEOUT = 60.0
MAP_CHARS = 3500      # planner orientation; ~900 tokens
BLAST_CHARS = 6000    # reviewer impact report; ~1500 tokens on top of the diff


def binary():
    """Path of the graft CLI, or None. Cached: PATH does not change mid-run.

    Looks past PATH into the node prefix `dsh` already lives in
    (config.dsh_bin does the same for the same reason: the service that
    spawns harnesses is not a login shell).
    """
    if _bin_cache["checked"]:
        return _bin_cache["path"]
    path = None
    if config.GRAFT_ENABLED:
        cand = config.GRAFT_BIN or "graft"
        path = shutil.which(cand)
        if path is None and not os.path.isabs(cand):
            fallback = Path.home() / ".local" / "opt" / "node" / "bin" / cand
            if os.access(fallback, os.X_OK):
                path = str(fallback)
        elif path is None and os.access(cand, os.X_OK):
            path = cand
    _bin_cache.update(checked=True, path=path)
    return path


def available():
    return binary() is not None


def graph_dir(worktree):
    """Where this worktree's graph lives — beside the worktrees, never inside.

    Keyed by the worktree's own path segments under WORKTREE_ROOT (repo name
    and task id) so a retry of the same task reuses its cache and two tasks on
    the same repo never share one. A repo path that is not a fleet worktree
    (the planner runs in the repo itself) keys on its resolved path instead.
    """
    wt = Path(worktree).resolve()
    root = Path(config.WORKTREE_ROOT).resolve()
    if wt.is_relative_to(root):
        rel = wt.relative_to(root)
    else:
        rel = Path(*[p for p in wt.parts if p not in ("/", "")])
    return root / ".graft" / rel


def env_for(worktree, base=None):
    """Environment additions for a process that should see this graph."""
    env = dict(base or {})
    exe = binary()
    if exe is None:
        return env
    env["GRAFT_DIR"] = str(graph_dir(worktree))
    env["DO_NOT_TRACK"] = "1"        # graft's telemetry; the fleet is headless
    # binary() looks PAST PATH (the node-prefix fallback above): the run
    # process resolves graft, but the harness child's shell inherits a PATH
    # without that directory and the model's own `graft ...` calls die with
    # "command not found" — empty-diff-publish burned three implementer
    # timeouts re-reading the repo on grep ladders because of exactly this.
    # Put the binary's directory on the child's PATH; graft's env-node
    # shebang also finds the node binary that lives beside it there.
    bindir = os.path.dirname(exe)
    path = env.get("PATH") or os.environ.get("PATH", os.defpath)
    if bindir and bindir not in path.split(os.pathsep):
        env["PATH"] = bindir + os.pathsep + path
    return env


async def _run(args, cwd, timeout, env=None):
    """(rc, stdout, stderr) of `graft --dir <graph> <args>`; rc None if it could not run."""
    exe = binary()
    if exe is None:
        return None, "", "graft not installed"
    full_env = dict(os.environ, **env_for(cwd))
    full_env.update(env or {})
    try:
        proc = await asyncio.create_subprocess_exec(
            exe, "--dir", str(graph_dir(cwd)), *args, cwd=str(cwd), env=full_env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except (OSError, ValueError) as exc:
        return None, "", str(exc)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None, "", f"graft {args[0]} timed out after {timeout}s"
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


_WIRING = re.compile(r"wiring:\s*(\d+)\s*nodes.*?(\d+)\s*edges", re.S)


async def build(worktree, task=None):
    """Build or refresh the graph for a worktree. True on success; never raises.

    Incremental: graft hashes files and re-parses only what changed, so
    calling this before every implement attempt costs ~0.2 s when nothing
    moved and keeps the hints honest after a rework.
    """
    if not available():
        return False
    t0 = time.monotonic()
    rc, out, err = await _run(["build", "."], worktree, BUILD_TIMEOUT)
    secs = round(time.monotonic() - t0, 2)
    if rc != 0:
        log.warning("graft build failed in %s (rc=%s): %s", worktree, rc,
                    (err or out).strip()[-300:])
        events.emit("graft.build", task=task, ok=False, seconds=secs,
                    error=(err or out).strip()[-200:])
        return False
    m = _WIRING.search(out + err)
    nodes, edges = (int(m.group(1)), int(m.group(2))) if m else (None, None)
    events.emit("graft.build", task=task, ok=True, seconds=secs,
                nodes=nodes, edges=edges)
    return True


def _parse_hits(raw):
    """Normalise `graft ask --json` hits; tolerate any shape we did not expect."""
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    hits = data.get("hits") if isinstance(data, dict) else data
    out = []
    for h in hits or []:
        if not isinstance(h, dict):
            continue
        pointer = str(h.get("pointer") or "").strip()
        if not pointer:
            continue
        out.append({
            "pointer": pointer,
            "title": str(h.get("title") or "").strip(),
            "snippet": str(h.get("snippet") or "").strip(),
            "score": float(h.get("score") or 0.0),
        })
    return out


def task_query(task):
    """What to ask the graph for a task: its title, files, and the prompt's head.

    Ranking is lexical, so identifiers in the title and prompt are what land;
    the prompt's tail is acceptance criteria and process rules that only add
    noise. Capped so a long prompt does not become a long argv.
    """
    parts = [task.get("title") or ""]
    parts += list(task.get("files_hint") or [])
    parts.append((task.get("prompt") or "")[:400])
    q = " ".join(p for p in parts if p)
    return re.sub(r"\s+", " ", q).strip()[:600]


async def ask(query, worktree, k=None):
    """Top-k graph hits for a query: [{pointer, title, snippet, score}]."""
    if not available() or not query.strip():
        return []
    k = k or config.GRAFT_HINTS
    rc, out, err = await _run(["ask", query, "--json", "."], worktree,
                              QUERY_TIMEOUT)
    if rc != 0:
        log.info("graft ask failed (rc=%s): %s", rc, (err or out).strip()[-200:])
        return []
    return _parse_hits(_BANNER.sub("", out))[:k]


async def hints(task, worktree):
    """The implementer's "WHERE TO LOOK" block for a task, or "".

    Builds (incrementally) first so the spans are for THIS worktree's files,
    then asks. Emits `graft.hints` with the hit count so the event log can
    tell, per task, whether the graph had anything to say.
    """
    if not available():
        return ""
    tid = task.get("id")
    if not await build(worktree, task=tid):
        return ""
    hits = await ask(task_query(task), worktree)
    events.emit("graft.hints", task=tid, hits=len(hits),
                pointers=[h["pointer"] for h in hits])
    return hints_block(hits)


def hints_block(hits):
    if not hits:
        return ""
    lines = ["WHERE TO LOOK (from the repo's code graph — exact file:line "
             "spans matching this task, verified against the current tree; "
             "start by reading THESE ranges, do not search for them):"]
    for h in hits:
        what = h["title"] or h["snippet"]
        lines.append(f"- {h['pointer']}" + (f" — {what}" if what else ""))
    return "\n".join(lines) + "\n"


def tooling_prose():
    """Harness-facing instructions for the graft CLI, or "" when absent."""
    if not available():
        return ""
    return (
        "This worktree has a code graph (its directory is in $GRAFT_DIR — "
        "always pass it: `graft --dir \"$GRAFT_DIR\" <command>`). Use it "
        "instead of grep/find/cat ladders — each of these is ONE call with an "
        "exact answer:\n"
        "- `graft --dir \"$GRAFT_DIR\" ask \"<what you need>\" --source` — "
        "ranked definitions with the relevant lines inlined; reuse literal "
        "identifiers as the query.\n"
        "- `graft --dir \"$GRAFT_DIR\" skeleton <file>` — every signature in "
        "a file with line spans, ~10x cheaper than reading it. Skim APIs this "
        "way.\n"
        "- `graft --dir \"$GRAFT_DIR\" callers <symbol>` — exact callers (add "
        "`--direction out` for callees, `-d 2` for the blast radius) before "
        "changing a signature.\n"
        "- `graft --dir \"$GRAFT_DIR\" grep \"<regex>\"` — exhaustive search "
        "grouped by enclosing symbol, for every-occurrence tasks.\n"
        "Open a source file only at the exact range a hit names.\n"
    )


async def repo_map(repo, max_chars=None):
    """`graft map` orientation for a repo (planner), banner stripped, capped."""
    if not available():
        return ""
    max_chars = max_chars or MAP_CHARS
    if not await build(repo, task="plan"):
        return ""
    rc, out, err = await _run(["map", "."], repo, QUERY_TIMEOUT)
    if rc != 0 or not out.strip():
        return ""
    text = _BANNER.sub("", out).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n… (map truncated)"
    return text


async def blast(worktree, base=None, task=None, max_chars=None):
    """Blast radius of a worktree's changes, as markdown.

    What the reviewers are asked to reason about — regressions in dependents,
    tests that reach the change — is precomputed here from the graph edges.
    Stripped of the diagram and the git-blame table; capped so a sweeping
    change does not swamp the diff it is meant to annotate.

    `base=None` diffs the WORKING TREE against HEAD: the pre-PR review runs
    before the publish commit, when HEAD is still the branch point and the
    change is all uncommitted (gitstore.diff_full has run `git add -N`, so
    new files count). `base=<branch>` diffs merge-base(base, HEAD)..HEAD —
    committed changes only — which is what the PR reviewer sees after
    publish.
    """
    if not available():
        return ""
    max_chars = max_chars or BLAST_CHARS
    if not await build(worktree, task=task):
        return ""
    args = ["blast", "--format", "markdown", "--no-owners"]  # no people to tag
    if base:
        args += ["--base", base]
    args.append(".")
    rc, out, err = await _run(args, worktree, QUERY_TIMEOUT)
    if rc != 0 or not out.strip():
        return ""
    text = _BANNER.sub("", out)
    text = _MERMAID.sub("", text)
    text = _WHO_KNOWS.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n… (impact report truncated)"
    return text


def impact_block(report):
    """Reviewer-prompt section wrapping a blast report, or ""."""
    if not report:
        return ""
    return ("IMPACT (from the repo's code graph — which symbols depend on the "
            "changed ones and whether any test reaches them; use it for the "
            "REGRESSIONS and TESTS checks instead of searching):\n\n"
            + report + "\n\n")
