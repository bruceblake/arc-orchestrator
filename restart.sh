#!/usr/bin/env bash
# Gracefully restart the ARC dashboard server.
#
# If running: sends POST /api/restart to trigger an in-process graceful re-exec.
# If not running: runs ./start.sh to launch it.
#
# With the arc-dashboard systemd user unit installed this is simply
# `systemctl --user restart`: the unit carries the token and environment, and
# a nohup copy started by the old fallback below is exactly what once held the
# port token-less while the unit crash-looped (deploy/dashboard-unit.sh).
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=deploy/dashboard-unit.sh
. deploy/dashboard-unit.sh

PORT="${1:-}"
if [ -z "$PORT" ] && [ -f .env ]; then
    ENV_PORT=$(grep -E '^ARC_DASHBOARD_PORT=' .env | tail -1 | cut -d= -f2 | tr -d '[:space:]' || true)
    [ -n "${ENV_PORT:-}" ] && PORT="$ENV_PORT"
fi
PORT="${PORT:-$DEFAULT_PORT}"

if use_unit "$PORT"; then
    stop_strays
    echo "Restarting $DASHBOARD_UNIT ..."
    systemctl --user reset-failed "$DASHBOARD_UNIT" 2>/dev/null || true
    systemctl --user restart "$DASHBOARD_UNIT"
    if wait_up "$PORT" 40; then
        echo "Dashboard is up and running on port $PORT ($DASHBOARD_UNIT)."
        exit 0
    fi
    unit_failed_help
    exit 1
fi

# Check if dashboard is currently running on PORT
if ! curl -s -m 2 -o /dev/null "http://localhost:$PORT/"; then
    echo "Dashboard is not currently running on port $PORT. Starting..."
    ./start.sh "$PORT"
    exit 0
fi

echo "Sending graceful restart request to dashboard on port $PORT..."
TOKEN="${ARC_DASHBOARD_TOKEN:-}"
# The same places `main.py serve` reads it from: the unit's EnvironmentFile
# first, then .env.
for f in "${ARC_DASHBOARD_ENV_FILE:-$HOME/.config/arc-dashboard.env}" .env; do
    [ -n "$TOKEN" ] && break
    [ -f "$f" ] || continue
    TOKEN=$(grep -E '^(export )?ARC_DASHBOARD_TOKEN=' "$f" | tail -1 | cut -d= -f2- \
            | tr -d '[:space:]' | sed -e "s/^[\"']//" -e "s/[\"']$//" || true)
done

AUTH_HEADER=()
if [ -n "$TOKEN" ]; then
    AUTH_HEADER=(-H "Authorization: Bearer $TOKEN")
fi

RESP=$(curl -s -w "\n%{http_code}" -X POST \
    -H "Content-Type: application/json" \
    "${AUTH_HEADER[@]}" \
    -d '{"force": false}' \
    "http://localhost:$PORT/api/restart" || true)

HTTP_CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | head -n -1)

if [ "$HTTP_CODE" = "409" ]; then
    echo "Warning: an interactive session is currently in progress."
    read -r -p "Force restart anyway? [y/N] " confirm
    if [[ "$confirm" =~ ^[Yy]$ ]]; then
        RESP=$(curl -s -w "\n%{http_code}" -X POST \
            -H "Content-Type: application/json" \
            "${AUTH_HEADER[@]}" \
            -d '{"force": true}' \
            "http://localhost:$PORT/api/restart" || true)
        HTTP_CODE=$(echo "$RESP" | tail -1)
        BODY=$(echo "$RESP" | head -n -1)
    else
        echo "Restart cancelled."
        exit 0
    fi
fi

if [ "$HTTP_CODE" != "200" ]; then
    echo "Restart API returned $HTTP_CODE: $BODY"
    echo "Falling back to ./stop.sh && ./start.sh..."
    ./stop.sh
    ./start.sh "$PORT"
    exit 0
fi

echo "Restart initiated. Waiting for dashboard to become ready on port $PORT..."
for _ in $(seq 1 30); do
    if curl -s -m 2 -o /dev/null "http://localhost:$PORT/"; then
        echo "Dashboard is up and running on port $PORT."
        exit 0
    fi
    sleep 0.5
done

echo "Server did not respond within 15 seconds. Checking status with ./start.sh..."
./start.sh "$PORT"
