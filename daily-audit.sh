#!/usr/bin/env bash
# Daily audit. Designed to be silent on a good day.
#
# Exits 2 when the audit found something critical, which is what makes it
# schedulable: cron mails only on a non-zero exit, so a mail arriving means
# something rather than being the thing you filter away.
#
#   crontab -e
#   17 7 * * *  /home/proxyie/arc-orchestrator/daily-audit.sh
set -uo pipefail
cd "$(dirname "$0")" || exit 1

STAMP=$(date +%Y-%m-%d)
OUT="logs/audit/$STAMP.txt"
mkdir -p logs/audit

# --fix runs only the reversible cleanups reconcile already implements: reaping
# leases whose process is gone and worktrees whose run is gone. Nothing here
# touches a branch, a PR, or anything a human has not already lost.
.venv/bin/python main.py audit --fix > "$OUT" 2>&1
rc=$?

# Keep a month. The point of history is spotting a defect that keeps coming
# back, which a single overwritten file cannot show.
find logs/audit -name '*.txt' -mtime +31 -delete 2>/dev/null

if [ "$rc" -ne 0 ]; then
    echo "ARC audit $STAMP: CRITICAL findings"
    cat "$OUT"
    exit "$rc"
fi
# Quiet success: the report is on disk either way.
exit 0
