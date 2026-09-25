#!/usr/bin/env bash
# Stop the ARC dashboard server.
#
# With the arc-dashboard systemd user unit installed, `pkill` alone is wrong:
# Restart=always brings the unit back 15 s later. Stop the unit through
# systemd, then remove any copy started outside it.
set -uo pipefail
cd "$(dirname "$0")"
# shellcheck source=deploy/dashboard-unit.sh
. deploy/dashboard-unit.sh

if use_unit "${1:-$DEFAULT_PORT}"; then
    if systemctl --user is-active --quiet "$DASHBOARD_UNIT"; then
        systemctl --user stop "$DASHBOARD_UNIT"
        echo "Stopped $DASHBOARD_UNIT (it starts again at login/boot; 'systemctl --user disable $DASHBOARD_UNIT' to prevent that)."
    fi
    stop_strays
    if pgrep -f "main\.py serve" > /dev/null; then
        echo "Could not stop it. Run: pgrep -af 'main.py serve' and kill the pid manually." >&2
        exit 1
    fi
    echo "Dashboard stopped."
    exit 0
fi

if ! pgrep -f "main\.py serve" > /dev/null; then
    echo "The dashboard is not running."
    exit 0
fi

pkill -f "main\.py serve"
sleep 1

if pgrep -f "main\.py serve" > /dev/null; then
    pkill -9 -f "main\.py serve"
    sleep 1
fi

if pgrep -f "main\.py serve" > /dev/null; then
    echo "Could not stop it. Run: pgrep -af 'main.py serve' and kill the pid manually." >&2
    exit 1
fi

echo "Dashboard stopped."
