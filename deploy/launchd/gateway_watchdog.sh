#!/bin/bash
# IB Gateway watchdog for algo-poc.
# Checks the API port; if it's been down for TWO consecutive runs, kickstarts
# the IBC gateway launchd job (the fix that resolved the stuck-login-modal issue
# on 2026-06-25). Two-strike logic rides over the legitimate ~1-min nightly
# auto-restart (23:55) and cold-restart (08:00) blips instead of fighting them.
#
# Wire via launchd with StartInterval (e.g. every 300s). See deploy/launchd/.

set -uo pipefail

PORT=7497                      # paper API port (7496 for live)
GW_LABEL="local.ibc-gateway"
LOG_DIR="$HOME/ibc/logs"
LOG_FILE="$LOG_DIR/gateway_watchdog_$(date +%Y%m%d).log"
MARKER="$HOME/ibc/.gateway_down_marker"

mkdir -p "$LOG_DIR"
ts() { date '+%Y-%m-%d %H:%M:%S'; }

if nc -z -G 3 127.0.0.1 "$PORT" 2>/dev/null; then
    # Up — clear any pending strike. Only log on recovery to keep the log quiet.
    if [ -f "$MARKER" ]; then
        echo "$(ts): port $PORT recovered" >> "$LOG_FILE"
        rm -f "$MARKER"
    fi
    exit 0
fi

# Port is down.
if [ -f "$MARKER" ]; then
    echo "$(ts): port $PORT down 2 consecutive checks — kickstarting $GW_LABEL" >> "$LOG_FILE"
    launchctl kickstart -k "gui/$(id -u)/$GW_LABEL" >> "$LOG_FILE" 2>&1
    rm -f "$MARKER"            # reset; next run confirms recovery (or strikes again)
else
    echo "$(ts): port $PORT down (1st check) — grace before action" >> "$LOG_FILE"
    touch "$MARKER"
fi

# Prune old logs
find "$LOG_DIR" -name "gateway_watchdog_*.log" -mtime +30 -delete 2>/dev/null
exit 0
