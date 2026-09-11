# Scheduling the daily audit

`daily-audit.sh` is designed to be silent on a good day: it writes
`logs/audit/<date>.txt` either way and exits **2** only when the audit found
something critical. That exit code is the alarm — a scheduler that mails on
failure then mails only when it means something.

Verified: the calendar spec normalises to `*-*-* 07:17:00`, next elapse 07:17
local.

## systemd (this machine — units are in this directory)

They are already copied to `~/.config/systemd/user/`. Enabling them needs a
user session bus, which a non-login shell does not have, so run this from a
normal terminal:

```bash
sudo loginctl enable-linger "$USER"   # once, so the timer runs without a login
systemctl --user daemon-reload
systemctl --user enable --now arc-audit.timer
systemctl --user list-timers arc-audit.timer
```

Check a run:

```bash
systemctl --user start arc-audit.service
journalctl --user -u arc-audit.service -n 40
```

`Persistent=true` means a machine that was asleep at 07:17 runs the audit on
its next boot rather than silently skipping the day.

## cron (if you install it instead)

`crontab` is not present on this box. If you add it:

```
17 7 * * *  /home/proxyie/arc-orchestrator/daily-audit.sh
```

## Running it by hand

```bash
./py main.py audit                 # triage + codebase, runs check.sh
./py main.py audit --no-health     # skip check.sh (the slow part)
./py main.py audit --json          # machine-readable
./py main.py audit --fix           # + reconcile's reversible cleanups
```

`--fix` skips entirely while any run is in flight: reaping worktrees and leases
out from under a live run turns a cleanup into an outage.
