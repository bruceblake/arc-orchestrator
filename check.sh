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
PY="${PY:-.venv/bin/python}"
[ -x "$PY" ] || PY=python3
rc=0

step() { printf '\n--- %s ---\n' "$1"; }

step "python syntax"
if ! "$PY" -m compileall -q $(ls *.py) >/dev/null; then
    echo "FAIL: a module does not compile"; rc=1
fi

step "imports"
for m in config store graph events drivers gitstore code_tasks reconcile dashboard; do
    "$PY" -c "import $m" 2>&1 | tail -3 || { echo "FAIL: import $m"; rc=1; }
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
        sed -n '/<script[^>]*>/,/<\/script>/p' "$f" | sed '1d;$d' > "$tmp/$(basename "$f").js"
        if ! node --check "$tmp/$(basename "$f").js" 2>&1; then
            echo "FAIL: $f has a JavaScript syntax error"; rc=1
        fi
    done
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
else
    echo "(node not installed — skipping JavaScript checks)"
fi

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
