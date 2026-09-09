#!/usr/bin/env bash
# Stop the ARC dashboard server.
set -uo pipefail
cd "$(dirname "$0")"

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
