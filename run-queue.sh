#!/bin/bash
# Serial fleet queue: one `code run` at a time, waiting for any active one first.
cd /home/proxyie/arc-orchestrator || exit 1
PY=.venv/bin/python
log=logs/run-queue.log

wait_free() {
  while pgrep -f "main\.py code run /home" >/dev/null; do sleep 15; done
}

for tf in escalation-and-nav dashboard-ui-recovery dag-chaining github-ops-and-heartbeat projects-ui-and-patterns; do
  wait_free
  echo "=== $(date '+%H:%M:%S') START $tf ===" | tee -a "$log"
  $PY main.py code run "/home/proxyie/tasks/$tf.json" >> "$log" 2>&1
  echo "=== $(date '+%H:%M:%S') END $tf rc=$? ===" | tee -a "$log"
done
echo "=== $(date '+%H:%M:%S') QUEUE COMPLETE ===" | tee -a "$log"
