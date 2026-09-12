"""Run the daily audit from inside the dashboard process.

Why here and not cron: this fleet lives in WSL2, which has no cron daemon, a
degraded systemd, and — the part that makes both moot — goes to sleep when
idle. No scheduler on this box fires unless something is awake. The dashboard
server is the process that is awake whenever the operator is using the system,
so it is the process that can be trusted to run the audit.

Semantics chosen to survive restarts:

* The last run time is PERSISTED (logs/audit/last-run), so restarting the
  dashboard ten times a day does not run ten audits, and a dashboard that was
  down when the audit was due runs it on the next start rather than waiting
  another 24 hours.
* Reports are written to logs/audit/<stamp>.json and the newest is served at
  /api/audit, so the report is a page, not a log line nobody reads.
* The audit runs with snapshot=True (taskfile + database backups) and
  with_health=False: check.sh takes minutes and would block the dashboard's
  request threads. Health is checked by the fleet's own gate on every task.
* It NEVER runs reconcile --apply from here. Reaping worktrees from a
  background thread while an operator is mid-launch is how a cleanup becomes
  an outage; --fix stays a deliberate command.
"""
import json
import logging
import threading
import time
from pathlib import Path

import config

log = logging.getLogger("audit.scheduler")

INTERVAL_S = 24 * 3600
CHECK_EVERY_S = 300
KEEP_REPORTS = 30


def _dir():
    d = Path(config.ROOT) / "logs" / "audit"
    d.mkdir(parents=True, exist_ok=True)
    return d


def last_run():
    try:
        return float((_dir() / "last-run").read_text().strip())
    except (OSError, ValueError):
        return None


def due(now=None, interval=INTERVAL_S):
    now = now if now is not None else time.time()
    last = last_run()
    return last is None or now - last >= interval


def run_once(store=None, snapshot=True):
    """Run the audit, persist the report, mark the time. Returns the report."""
    import audit
    report = audit.run(store, since_s=INTERVAL_S, with_health=False, snapshot=snapshot)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    d = _dir()
    (d / f"{stamp}.json").write_text(json.dumps(report, indent=1, default=str))
    (d / "latest.json").write_text(json.dumps(report, indent=1, default=str))
    (d / "last-run").write_text(str(report["ts"]))
    for old in sorted(d.glob("2*.json"))[:-KEEP_REPORTS]:
        old.unlink(missing_ok=True)
    c = report["counts"]
    log.warning("daily audit: %d critical, %d warning, %d info -> %s",
                c["critical"], c["warning"], c["info"], d / f"{stamp}.json")
    try:
        import events
        events.emit("audit.ran", critical=c["critical"], warning=c["warning"],
                    info=c["info"], report=str(d / f"{stamp}.json"))
    except Exception:
        pass
    return report


def latest():
    try:
        return json.loads((_dir() / "latest.json").read_text())
    except (OSError, ValueError):
        return None


def start(store=None, interval=INTERVAL_S, check_every=CHECK_EVERY_S):
    """Background thread: run when due, then every `interval`. Daemon, so it
    never keeps the process alive on its own."""
    def loop():
        while True:
            try:
                if due(interval=interval):
                    run_once(store)
            except Exception as exc:  # the scheduler must outlive a bad audit
                log.error("daily audit failed: %s", exc)
                try:
                    import errors
                    errors.capture(exc, node="audit.scheduler")
                except Exception:
                    pass
            time.sleep(check_every)
    t = threading.Thread(target=loop, name="daily-audit", daemon=True)
    t.start()
    return t
