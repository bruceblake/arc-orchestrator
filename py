#!/usr/bin/env bash
# The project interpreter, resolvable from a git WORKTREE.
#
# Worktrees have no .venv (it is gitignored), so a verify gate that runs
# `.venv/bin/python` inside one fails with "No such file or directory" no
# matter how correct the task's work is. That cost 26 implement attempts and
# 6 escalations across two tasks before it was found, and it was reintroduced
# in every gate written afterwards. Use `./py` in gates, never `.venv/bin/python`.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for cand in "$here/.venv/bin/python" \
            "$(git -C "$here" rev-parse --git-common-dir 2>/dev/null | xargs -r dirname)/.venv/bin/python"; do
    if [ -x "$cand" ]; then exec "$cand" "$@"; fi
done
exec python3 "$@"
