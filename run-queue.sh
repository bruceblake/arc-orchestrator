#!/usr/bin/env bash
# Serial fleet queue: run task files one at a time, never two at once.
#
# Serialization is a flock on a lock file, not a pgrep scan. The pgrep version
# this replaces had two defects that cost a live fleet run: every waiting queue
# instance saw the same process exit and started simultaneously (on 2026-09-09
# that put four task files in flight within two seconds, 9 requests against
# a per-model cap of 3, and every retry came back as an instant 400), and its
# `rc=$?` was always 0 because the `$(date)` in the same echo reset $?.
#
# Usage:  ./run-queue.sh [taskfile-stem ...]      (default: the list below)
set -uo pipefail
cd "$(dirname "$0")" || exit 1

PY=.venv/bin/python
LOG=logs/run-queue.log
LOCK=logs/.run-queue.lock
TASKS_DIR="${ARC_TASKS_DIR:-$HOME/tasks}"

DEFAULT_QUEUE=(dashboard-ui-recovery dag-chaining github-ops-and-heartbeat projects-ui-and-patterns)
QUEUE=("$@")
[ ${#QUEUE[@]} -eq 0 ] && QUEUE=("${DEFAULT_QUEUE[@]}")

mkdir -p logs
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "another run-queue.sh already holds $LOCK — not starting a second one." >&2
    exit 1
fi

# How many task files may be in flight at once. Was effectively 1: this queue
# ran them strictly one after another after a 09-09 incident where four
# started together and the dashboard reported one model at 9/3.
#
# That reading was wrong. driver.start was emitted BEFORE a driver acquired
# its semaphore and lease, so queued drivers were counted as running. With
# that fixed, the measured data says the caps were never the constraint:
# ZERO driver.cap_wait events in 6047, and queue time totalling 1.2% of 33
# driver-hours. What the serialization actually bought was a fleet running at
# 2.13 concurrent drivers against 21 slots, with exactly ONE driver live 58%
# of the time.
#
# The real guard against over-subscription is the per-model lease in
# store.driver_leases, which is enforced across processes and was not what
# failed. This bounds task-file concurrency instead, so one wedged file cannot
# monopolise the fleet.
MAX_PARALLEL="${ARC_QUEUE_PARALLEL:-3}"

say() { printf '=== %s %s ===\n' "$(date '+%H:%M:%S')" "$*" | tee -a "$LOG"; }

# A queue killed mid-flight leaves 'running' rows, driver leases and worktrees
# behind; they throttle the fleet against ghosts until reaped.
$PY main.py code reconcile >>"$LOG" 2>&1

trap 'say "QUEUE INTERRUPTED"; exit 130' INT TERM

FAILFILE=$(mktemp)
trap 'rm -f "$FAILFILE"' EXIT
failed=0
for stem in "${QUEUE[@]}"; do
    tf="$TASKS_DIR/$stem.json"
    if [ ! -f "$tf" ]; then
        say "SKIP $stem (no such task file: $tf)"
        continue
    fi
    # Another process may already be running this exact file — the dashboard's
    # Run button, or a terminal. Two runs of one task file share task ids and
    # worktrees, so wait it out rather than racing or reporting a false failure.
    waited=0
    while $PY - "$tf" <<'PYEOF' >/dev/null 2>&1
import sys, pathlib, reconcile
tf = str(pathlib.Path(sys.argv[1]).resolve())
sys.exit(0 if any(r.get("taskfile") and str(pathlib.Path(r["taskfile"]).resolve()) == tf
                  for r in reconcile.live_runs()) else 1)
PYEOF
    do
        [ "$waited" -eq 0 ] && say "WAIT $stem (already running elsewhere)"
        waited=1
        sleep 20
    done
    [ "$waited" -eq 1 ] && say "RESUME $stem (the other run finished)"

    # Hold at MAX_PARALLEL task files in flight; start the next as one frees.
    while [ "$(jobs -rp | wc -l)" -ge "$MAX_PARALLEL" ]; do
        wait -n 2>/dev/null || true
    done

    say "START $stem"
    (
        $PY main.py code run "$tf" >>"$LOG" 2>&1
        rc=$?                   # captured BEFORE any other command runs
        if [ "$rc" -eq 0 ]; then
            say "END $stem rc=0"
        else
            say "END $stem rc=$rc (FAILED)"
            # Failures are recorded in a file, not a shell variable: each run
            # is a background subshell, so a variable it increments dies with
            # it, and `wait -n || ...` swallows the status it would have
            # reported. The queue would then always claim success.
            echo "$stem rc=$rc" >> "$FAILFILE"
        fi
    ) &
done

# Drain: every remaining task file must finish before the queue reports.
while [ "$(jobs -rp | wc -l)" -gt 0 ]; do
    wait -n 2>/dev/null || true
done
failed=$(wc -l < "$FAILFILE" 2>/dev/null || echo 0)

if [ "$failed" -gt 0 ]; then
    say "QUEUE COMPLETE — ${#QUEUE[@]} task file(s), $failed failed:"
    sed 's/^/    /' "$FAILFILE" | tee -a "$LOG"
else
    say "QUEUE COMPLETE — ${#QUEUE[@]} task file(s), all succeeded"
fi
exit $(( failed > 0 ? 1 : 0 ))
