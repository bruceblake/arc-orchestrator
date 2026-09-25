# Shared by start.sh / stop.sh / restart.sh (sourced, not executed).
#
# When the arc-dashboard systemd user unit is installed it is THE dashboard:
# it carries the token (EnvironmentFile=~/.config/arc-dashboard.env), the
# shared db/log paths and the restart policy. A copy started with nohup has
# none of that. On 2026-09-24 such a copy held port 8787 token-less all day
# while the unit crash-looped 2700+ times on "port already in use", and after
# the next reboot the unit won and the operator was suddenly asked for a token.
# So these scripts drive the unit whenever it exists.
#
#   ARC_DASHBOARD_UNIT     unit name (default arc-dashboard.service)
#   ARC_DASHBOARD_NO_UNIT  1 = ignore the unit (e.g. a second copy on another port)

DASHBOARD_UNIT="${ARC_DASHBOARD_UNIT:-arc-dashboard.service}"
DEFAULT_PORT=8787

# 0 when the unit is installed and should own the dashboard on $1.
use_unit() {
    local port="${1:-$DEFAULT_PORT}"
    [ "${ARC_DASHBOARD_NO_UNIT:-0}" = "1" ] && return 1
    [ "$port" = "$DEFAULT_PORT" ] || return 1
    command -v systemctl >/dev/null 2>&1 || return 1
    systemctl --user cat "$DASHBOARD_UNIT" >/dev/null 2>&1
}

unit_pid() {
    systemctl --user show -p MainPID --value "$DASHBOARD_UNIT" 2>/dev/null || echo 0
}

# PIDs of `main.py serve` processes that are NOT the unit's process.
stray_pids() {
    local main; main=$(unit_pid)
    local pid
    for pid in $(pgrep -f "main\.py serve" 2>/dev/null); do
        [ "$pid" = "$main" ] && continue
        [ "$pid" = "$$" ] && continue
        if grep -q "$DASHBOARD_UNIT" "/proc/$pid/cgroup" 2>/dev/null; then
            continue
        fi
        echo "$pid"
    done
}

stop_strays() {
    local pids; pids=$(stray_pids)
    [ -z "$pids" ] && return 0
    echo "Stopping dashboard copies started outside systemd: $pids"
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
    for _ in $(seq 1 20); do
        pids=$(stray_pids)
        [ -z "$pids" ] && return 0
        sleep 0.5
    done
    # shellcheck disable=SC2086
    kill -9 $pids 2>/dev/null || true
}

wait_up() {
    local port="$1" tries="${2:-40}"
    for _ in $(seq 1 "$tries"); do
        curl -s -m 2 -o /dev/null "http://localhost:$port/api/health" && return 0
        sleep 0.5
    done
    return 1
}

unit_failed_help() {
    echo "The $DASHBOARD_UNIT unit did not come up. Its last log lines:" >&2
    journalctl --user -u "$DASHBOARD_UNIT" -n 15 --no-pager >&2 2>/dev/null \
        || systemctl --user status "$DASHBOARD_UNIT" --no-pager >&2
}
