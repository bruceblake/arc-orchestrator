#!/usr/bin/env bash
# Gracefully restart the ARC dashboard server.
#
# If running: sends POST /api/restart to trigger an in-process graceful re-exec.
# If not running: runs ./start.sh to launch it.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${1:-}"
if [ -z "$PORT" ] && [ -f .env ]; then
    ENV_PORT=$(grep -E '^ARC_DASHBOARD_PORT=' .env | tail -1 | cut -d= -f2 | tr -d '[:space:]' || true)
    [ -n "${ENV_PORT:-}" ] && PORT="$ENV_PORT"
fi
PORT="${PORT:-8787}"

# Check if dashboard is currently running on PORT
if ! curl -s -m 2 -o /dev/null "http://localhost:$PORT/"; then
    echo "Dashboard is not currently running on port $PORT. Starting..."
    ./start.sh "$PORT"
    exit 0
fi

echo "Sending graceful restart request to dashboard on port $PORT..."
TOKEN=""
if [ -f .env ]; then
    TOKEN=$(grep -E '^ARC_DASHBOARD_TOKEN=' .env | tail -1 | cut -d= -f2- | tr -d '[:space:]' || true)
fi

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
