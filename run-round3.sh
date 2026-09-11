#!/usr/bin/env bash
# Round 3 waits for ui-shared-helpers (it rewrites all three pages), then runs
# the three projects with bounded concurrency.
cd /home/proxyie/arc-orchestrator || exit 1
while [ "$(.venv/bin/python -c 'import reconcile;print(len(reconcile.live_runs()))')" != "0" ]; do sleep 30; done
exec ./run-queue.sh card-density wire-efficiency data-correctness
