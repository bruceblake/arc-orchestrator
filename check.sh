#!/usr/bin/env bash
# Repo self-check: the gate every task that edits this repo should pass.
#
# Task verify_cmd greps ("does the file mention X?") cannot tell that a change
# still WORKS. An agent refactoring drivers.py or dashboard.py can satisfy
# every grep and still break the engine, and the merge lands regardless. This
# runs the real checks and is meant to be the first clause of such a gate:
#
#     ./check.sh && grep -q 'my-new-thing' drivers.py
#
# Exits non-zero on the first failure, so a task bounces back to its
# implementer with the failing output as feedback.
set -uo pipefail
cd "$(dirname "$0")" || exit 1
# Find an interpreter that actually has the dependencies.
#
# This runs inside a task's git WORKTREE, and a worktree has no .venv — it is
# gitignored, so `git worktree add` never creates one. The old fallback to
# bare `python3` picked an interpreter without dotenv/openai, so every module
# import failed and EVERY gate beginning with ./check.sh failed no matter what
# the agent wrote. Two documentation tasks escalated all the way to the top
# tier and died that way, never once reaching review.
#
# git-common-dir points at the main checkout's .git from any worktree, so its
# parent is where the real virtualenv lives.
PY="${PY:-}"
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
    PY=".venv/bin/python"
fi
if [ ! -x "$PY" ]; then
    MAIN_WT=$(dirname "$(git rev-parse --git-common-dir 2>/dev/null || echo .)")
    [ -x "$MAIN_WT/.venv/bin/python" ] && PY="$MAIN_WT/.venv/bin/python"
fi
if [ ! -x "$PY" ]; then
    PY=$(command -v python3 || echo python3)
fi
if ! "$PY" -c "import dotenv" >/dev/null 2>&1; then
    echo "FAIL: $PY cannot import the project dependencies (no virtualenv found)."
    echo "      Set PY=/path/to/.venv/bin/python, or create one in the main checkout."
    exit 1
fi
echo "interpreter: $PY"
rc=0

step() { printf '\n--- %s ---\n' "$1"; }

step "python syntax"
if ! "$PY" -m compileall -q $(ls *.py) >/dev/null; then
    echo "FAIL: a module does not compile"; rc=1
fi

step "imports"
for m in config store graph events drivers gitstore code_tasks reconcile dashboard; do
    # No pipe here: `cmd | tail || rc=1` tests TAIL's status, which is always
    # 0, so import failures were reported and then silently forgiven.
    if ! out=$("$PY" -c "import $m" 2>&1); then
        echo "FAIL: import $m"
        echo "$out" | tail -3
        rc=1
    fi
done

step "unit tests"
# Unset ARC_ESCALATION_PATH for the suite: it is live fleet-run state, not test
# state. A run launched during a capacity outage exports
# ARC_ESCALATION_PATH=<the surviving model> (AGENTS.md Rule 2's documented
# pairing with ARC_ALLOW_SAME_FAMILY_REVIEW), and config.py reads it at import,
# collapsing the two-tier default to one model. The tests below assert the
# DEFAULT routing invariants ("every model below the top has a successor",
# "resume escalates one tier up"), so the suite passed or failed with the
# operator's shell rather than with the diff — measured 2026-09-15 on a PRISTINE
# tree: 15 failures with the var set, 0 without. The tests are right; the gate
# must run them with the defaults in effect.
#
# ARC_ALLOW_SAME_FAMILY_REVIEW is deliberately left set: the "taskfile validity"
# step validates the operator's real ~/tasks files, authored for whichever
# review mode the fleet is running.
TESTENV=(env -u ARC_ESCALATION_PATH)
if ! "${TESTENV[@]}" "$PY" -m unittest discover -s tests -t tests 2>&1 | tail -20; then
    echo "FAIL: unit tests"; rc=1
fi
"${TESTENV[@]}" "$PY" -m unittest discover -s tests -t tests >/dev/null 2>&1 || rc=1

step "dashboard javascript"
if command -v node >/dev/null 2>&1; then
    tmp=$(mktemp -d)
    for f in static/*.html; do
        # A one-line external tag (<script src="..."></script>) is not inline
        # JS: it opens and closes a sed range on the same line, so the range
        # extraction below would hand node --check a stray "<script>". Drop
        # complete external tags first; pages without them are unaffected.
        sed '/^[[:space:]]*<script[^>]*src=[^>]*><\/script>[[:space:]]*$/d' "$f" \
          | sed -n '/<script[^>]*>/,/<\/script>/p' | sed '1d;$d' > "$tmp/$(basename "$f").js"
        if ! node --check "$tmp/$(basename "$f").js" 2>&1; then
            echo "FAIL: $f has a JavaScript syntax error"; rc=1
        fi
    done
    # index.html's inline script now lives in per-panel files under
    # static/panels/ — check them directly, the pages no longer hold it.
    for f in static/panels/*.js; do
        if ! node --check "$f" 2>&1; then
            echo "FAIL: $f has a JavaScript syntax error"; rc=1
        fi
    done
    # A call to a function that does not exist is valid syntax and fails at
    # RUNTIME, on click — node --check cannot see it, and neither could the
    # DOM-reference check below. loadProjects() shipped that way.
    if ! node tests/undefined_calls.mjs static/*.html; then
        echo "FAIL: a page calls a function that is never defined"; rc=1
    fi

    # Editing a page can ship a control that is unusable to a keyboard or
    # screen-reader user while every syntax/ID check still passes: a clickable
    # div with no role="button", a control with no accessible name, an <img>
    # with no alt, an <input> with no label, or a page that never draws a
    # focus outline. The scanner is the gate for those.
    if ! node tests/a11y_check.mjs static/*.html; then
        echo "FAIL: a page is missing an accessibility requirement"; rc=1
    fi

    # Every $("#id") the script reaches for must exist in the markup.
    "$PY" - <<'PYEOF' || rc=1
import re, pathlib, sys
bad = 0
for p in sorted(pathlib.Path("static").glob("*.html")):
    s = p.read_text()
    ids = set(re.findall(r'\bid="([\w-]+)"', s))
    script = s[s.find("<script"):]
    if p.name == "index.html":  # its JS lives in static/panels/*.js now
        script += "\n".join(q.read_text() for q in sorted(pathlib.Path("static/panels").glob("*.js")))
    refs = set(re.findall(r'\$\(["\']#([\w-]+)["\']\)', script))
    refs |= set(re.findall(r'getElementById\(["\']([\w-]+)["\']\)', script))
    missing = sorted(refs - ids)
    if missing:
        print(f"FAIL: {p} JavaScript references missing element(s): {missing}")
        bad = 1
sys.exit(bad)
PYEOF
    rm -rf "$tmp"
    # Parsing is not running: render the real page against live API payloads.
    if [ -f tests/render_check.mjs ] && curl -s -m 2 -o /dev/null "http://localhost:8787/api/health"; then
        d=$(mktemp -d)
        curl -s -m 5 "http://localhost:8787/api/health"   > "$d/h.json"
        curl -s -m 5 "http://localhost:8787/api/projects" > "$d/p.json"
        f=$(curl -s -m 5 "http://localhost:8787/api/projects" | "$PY" -c "import json,sys;ps=json.load(sys.stdin)['projects'];print(ps[0]['file'] if ps else '')" 2>/dev/null)
        curl -s -m 5 "http://localhost:8787/api/project?file=$f" > "$d/d.json"
        if ! node tests/render_check.mjs "$d/h.json" "$d/p.json" "$d/d.json" 2>&1 | tail -12; then
            echo "FAIL: the dashboard JS throws on real data"; rc=1
        fi
        rm -rf "$d"
    else
        echo "(dashboard not running — skipping the live render check)"
    fi
    # Behavioural unit checks of the render functions; needs no running server.
    if ! node tests/ui_render.test.mjs; then
        echo "FAIL: dashboard render functions misbehave"; rc=1
    fi
    # Same contract for the small-screen page.
    if ! node tests/phone_render.test.mjs; then
        echo "FAIL: phone page render functions misbehave"; rc=1
    fi
    # The dashboard chat panel: session picker, new chat, speech, send/poll.
    # Both chat suites landed with the feature and guarded nothing until they
    # got a line here — the explicit list is what actually runs them.
    if ! node tests/chat_ui.test.mjs; then
        echo "FAIL: dashboard chat panel misbehaves"; rc=1
    fi
    # And the phone page's Plan chat, which has its own session controls.
    if ! node tests/phone_chat.test.mjs; then
        echo "FAIL: phone plan chat misbehaves"; rc=1
    fi
    if [ -f tests/usage_visibility.test.mjs ] && ! node tests/usage_visibility.test.mjs; then
        echo "FAIL: usage.html keeps polling a tab nobody is looking at"; rc=1
    fi
    # The hourly view: the date picker, the stacked bar chart and the
    # per-model table. Landing the feature without this entry is how a page
    # ships a control that renders nothing.
    if [ -f tests/usage_hourly.test.mjs ] && ! node tests/usage_hourly.test.mjs; then
        echo "FAIL: usage.html hourly view misbehaves"; rc=1
    fi
    # The projects list: compact cards, the filter bar, the URL-hash round
    # trip, and the workload DAGs. This file landed with the feature (#10)
    # and was never added here, so it passed on the author's machine and
    # guarded nothing after that.
    if ! node tests/projects_ui.test.mjs; then
        echo "FAIL: projects-list UI misbehaves"; rc=1
    fi
    # The fleet activity feed: newest first, the type badges, relative
    # timestamps and the click-through to a project. Same explicit list — a
    # suite that is not named here does not run.
    if [ -f tests/activity_ui.test.mjs ] && ! node tests/activity_ui.test.mjs; then
        echo "FAIL: fleet activity feed misbehaves"; rc=1
    fi
else
    echo "(node not installed — skipping JavaScript checks)"
fi

step "every imported module is actually tracked"
"$PY" - <<'PYEOF' || rc=1
import ast, pathlib, subprocess, sys

# A module that exists on THIS machine but is not committed makes a fresh
# clone crash on import, and nothing else in this suite would notice: the
# tests import it happily from the working tree. bench.py, bench_data.py and
# orchbench.py sat untracked for days while main.py imported all three, so
# every `main.py code bench ...` command was broken on any fresh checkout.
root = pathlib.Path(__file__).resolve().parent if "__file__" in dir() else pathlib.Path(".")
tracked = set(subprocess.run(["git", "ls-files", "*.py"], capture_output=True,
                             text=True).stdout.split())
local = {p.name for p in pathlib.Path(".").glob("*.py")}
bad = 0
for name in sorted(tracked):
    try:
        tree = ast.parse(pathlib.Path(name).read_text())
    except (OSError, SyntaxError):
        continue
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            mods.add(n.module.split(".")[0])
    for m in sorted(mods):
        if f"{m}.py" in local and f"{m}.py" not in tracked:
            print(f"FAIL: {name} imports {m}, but {m}.py is NOT tracked — "
                  f"a fresh clone would crash on import")
            bad = 1
sys.exit(bad)
PYEOF

step "task gates are worktree-safe"
"$PY" - <<'PYEOF' || rc=1
import json, pathlib, sys
bad = 0
d = pathlib.Path.home() / "tasks"
for f in sorted(d.glob("*.json")) if d.is_dir() else []:
    try:
        proj = json.loads(f.read_text()).get("project") or {}
    except Exception:
        continue
    if "arc-orchestrator" not in str(proj.get("repo", "")):
        continue
    for t in proj.get("tasks") or []:
        cmd = t.get("verify_cmd") or ""
        # A gate runs inside a git WORKTREE, which has no .venv (gitignored).
        # `.venv/bin/python` there is "No such file or directory" no matter how
        # correct the work is — 26 wasted implement attempts before it was
        # found. ./py resolves the interpreter from the main worktree.
        if ".venv/bin/python" in cmd:
            print(f"FAIL: {f.name}:{t['id']} gate uses .venv/bin/python; "
                  f"use ./py (worktrees have no .venv)")
            bad = 1
sys.exit(bad)
PYEOF

step "task ids are unique per repo"
"$PY" - <<'PYEOF' || rc=1
import collections, json, pathlib, sys
import config

# A task id is not a label, it is the KEY: branch task/<id>, worktree
# ~/worktrees/<repo>/<id>, commit trailer Task-Id. The schema only requires it
# to be unique WITHIN a file, which is not enough — two files sharing an id for
# one repo share a branch and a directory. gitstore.alloc does
# `git worktree remove --force` then resets the branch, so running both means
# one run deletes the directory the other's agent is editing and throws away
# its commits. Nothing detected that.
seen = collections.defaultdict(list)
d = pathlib.Path(config.TASKS_DIR)
for f in sorted(d.glob("*.json")) if d.is_dir() else []:
    try:
        proj = json.loads(f.read_text())["project"]
    except Exception:
        continue
    repo = str(proj.get("repo", ""))
    for t in proj.get("tasks") or []:
        if t.get("id"):
            seen[(repo, t["id"])].append(f.name)
bad = 0
for (repo, tid), files in sorted(seen.items()):
    if len(files) > 1:
        print(f"FAIL: task id {tid!r} is declared by {len(files)} taskfiles for "
              f"one repo ({', '.join(sorted(files))}) — they share branch "
              f"task/{tid} and one worktree, so running both destroys work")
        bad = 1
sys.exit(bad)
PYEOF

step "taskfile validity"
# Each taskfile is validated under the fleet it was PLANNED for
# (project.fleet): a studio taskfile's models exist only on the studio roster,
# and loading it under the default local fleet reported a perfectly good plan
# as broken. Each fleet gets its own interpreter because config derives the
# roster at import time.
"$PY" - <<'PYEOF' || rc=1
import json, os, pathlib, subprocess, sys
import config
d = pathlib.Path(config.TASKS_DIR)
by_fleet = {}
for f in sorted(d.glob("*.json")) if d.is_dir() else []:
    try:
        fleet = (json.loads(f.read_text(encoding="utf-8")).get("project") or {}).get("fleet")
    except (OSError, ValueError):
        fleet = None
    by_fleet.setdefault(fleet or os.environ.get("ARC_FLEET") or "local", []).append(str(f))
bad = 0
for fleet, files in sorted(by_fleet.items()):
    code = ("import sys, pathlib, code_tasks\n"
            "bad = 0\n"
            "for f in sys.argv[1:]:\n"
            "    try:\n"
            "        code_tasks.load_taskfile(f)\n"
            "    except Exception as exc:\n"
            "        print(f'FAIL: {pathlib.Path(f).name}: {exc}')\n"
            "        bad = 1\n"
            "sys.exit(bad)\n")
    r = subprocess.run([sys.executable, "-c", code, *files],
                       env=dict(os.environ, ARC_FLEET=fleet))
    bad = bad or r.returncode
sys.exit(1 if bad else 0)
PYEOF

printf '\n=== check.sh %s ===\n' "$([ $rc -eq 0 ] && echo PASS || echo FAIL)"
exit $rc
