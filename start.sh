#!/usr/bin/env bash
# Start the ARC dashboard and print the exact addresses to open on your laptop/phone.
# Safe to run any time: if the server is already up, it just prints the addresses.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${1:-}"
if [ -z "$PORT" ] && [ -f .env ]; then
    ENV_PORT=$(grep -E '^ARC_DASHBOARD_PORT=' .env | tail -1 | cut -d= -f2 | tr -d '[:space:]' || true)
    [ -n "${ENV_PORT:-}" ] && PORT="$ENV_PORT"
fi
PORT="${PORT:-8787}"

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
            *)  label="same wifi/network" ;;
        esac
        echo "  laptop or phone:  http://$ip:$PORT   ($label)"
    done < <(addresses)
    echo
    echo "On your phone, add /phone.html to the address for the small-screen page."
}

if curl -s -m 2 -o /dev/null "http://localhost:$PORT/"; then
    echo "The dashboard is ALREADY RUNNING."
    echo "(That 'Address already in use' error just means there is nothing new to start.)"
    print_urls
    exit 0
fi

echo "Starting the dashboard on port $PORT ..."
mkdir -p logs
nohup .venv/bin/python main.py serve --port "$PORT" >> logs/server.log 2>&1 &
NEW_PID=$!

for _ in $(seq 1 20); do
    if curl -s -m 2 -o /dev/null "http://localhost:$PORT/"; then
        echo "Started (pid $NEW_PID, log: logs/server.log)"
        print_urls
        exit 0
    fi
    sleep 0.5
done

echo "FAILED to start. Last log lines:" >&2
tail -20 logs/server.log >&2
exit 1
