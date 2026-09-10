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
# the agent wrote. Two documentation tasks escalated all the way to Kimi-K3
# and died that way, never once reaching review.
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
if ! "$PY" -m unittest discover -s tests -t tests 2>&1 | tail -20; then
    echo "FAIL: unit tests"; rc=1
fi
"$PY" -m unittest discover -s tests -t tests >/dev/null 2>&1 || rc=1

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
else
    echo "(node not installed — skipping JavaScript checks)"
fi

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

step "taskfile validity"
"$PY" - <<'PYEOF' || rc=1
import sys, pathlib, config, code_tasks
bad = 0
d = pathlib.Path(config.TASKS_DIR)
for f in sorted(d.glob("*.json")) if d.is_dir() else []:
    try:
        code_tasks.load_taskfile(f)
    except Exception as exc:
        print(f"FAIL: {f.name}: {exc}")
        bad = 1
sys.exit(bad)
PYEOF

printf '\n=== check.sh %s ===\n' "$([ $rc -eq 0 ] && echo PASS || echo FAIL)"
exit $rc
