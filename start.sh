#!/usr/bin/env bash
# Start the ARC dashboard and print the exact addresses to open on your laptop/phone.
# Safe to run any time: if the server is already up, it just prints the addresses.
#
# When the arc-dashboard systemd user unit is installed, this starts THE UNIT
# (deploy/dashboard-unit.sh explains why a nohup copy must never own the port).
# ARC_DASHBOARD_UNIT names the unit (default arc-dashboard.service);
# ARC_DASHBOARD_NO_UNIT=1 ignores it (e.g. a second copy for development).
set -euo pipefail
# Game development uses the Godot Studio subscription fleet by default.
export ARC_FLEET="${ARC_FLEET:-studio}"
cd "$(dirname "$0")"
# shellcheck source=deploy/dashboard-unit.sh
. deploy/dashboard-unit.sh

PORT="${1:-}"
if [ -z "$PORT" ] && [ -f .env ]; then
    ENV_PORT=$(grep -E '^ARC_DASHBOARD_PORT=' .env | tail -1 | cut -d= -f2 | tr -d '[:space:]' || true)
    [ -n "${ENV_PORT:-}" ] && PORT="$ENV_PORT"
fi
PORT="${PORT:-$DEFAULT_PORT}"

# All non-loopback IPv4 addresses of this machine, one per line.
addresses() {
    ip -4 -o addr show scope global 2>/dev/null \
        | awk '$2 != "lo" { sub(/\/.*/, "", $4); print $4 }' \
        | awk '!seen[$0]++'
}

print_urls() {
    echo
    echo "Open in a browser:"
    echo "  on this computer:   http://localhost:$PORT"
    local ip label
    while read -r ip; do
        [ -z "$ip" ] && continue
        case "$ip" in
            100.6[4-9].*|100.[7-9][0-9].*|100.1[0-1][0-9].*|100.12[0-7].*)
                label="tailscale - works even away from home" ;;
            172.1[6-9].*|172.2[0-9].*|172.3[0-1].*)
                label="WSL-internal - changes every reboot; the token is saved per address, prefer localhost" ;;
            *)  label="same wifi/network" ;;
        esac
        echo "  laptop or phone:  http://$ip:$PORT   ($label)"
    done < <(addresses)
    echo
    echo "On your phone, add /phone.html to the address for the small-screen page."
}

if use_unit "$PORT"; then
    if [ -n "$(stray_pids)" ]; then
        echo "A dashboard started outside systemd is running (pid $(stray_pids | tr '\n' ' '))."
        echo "It serves without the unit's token and environment; replacing it with the unit."
        stop_strays
    fi
    if systemctl --user is-active --quiet "$DASHBOARD_UNIT" && wait_up "$PORT" 4; then
        echo "The dashboard is ALREADY RUNNING ($DASHBOARD_UNIT)."
        print_urls
        exit 0
    fi
    echo "Starting $DASHBOARD_UNIT ..."
    systemctl --user reset-failed "$DASHBOARD_UNIT" 2>/dev/null || true
    systemctl --user start "$DASHBOARD_UNIT"
    if wait_up "$PORT" 40; then
        echo "Started ($DASHBOARD_UNIT, logs: journalctl --user -u $DASHBOARD_UNIT)"
        print_urls
        exit 0
    fi
    unit_failed_help
    exit 1
fi

if curl -s -m 2 -o /dev/null "http://localhost:$PORT/"; then
    echo "The dashboard is ALREADY RUNNING."
    print_urls
    exit 0
fi

echo "Starting the dashboard on port $PORT ..."
mkdir -p logs
# main.py serve reads ARC_DASHBOARD_TOKEN from ~/.config/arc-dashboard.env
# itself (ARC_DASHBOARD_ENV_FILE), so this copy has the same token as the unit.
nohup .venv/bin/python main.py serve --port "$PORT" >> logs/server.log 2>&1 &
NEW_PID=$!

if wait_up "$PORT" 20; then
    echo "Started (pid $NEW_PID, log: logs/server.log)"
    print_urls
    exit 0
fi

echo "FAILED to start. Last log lines:" >&2
tail -20 logs/server.log >&2
exit 1
