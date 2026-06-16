#!/bin/bash
# Daily divergence monitor for algo-poc
# Runs at 4:45 AM SGT, ~30 min after run_paper.sh (4:15) has written the day's
# equity_snapshots row. Compares live paper equity to the latest backtest.
#
# Exit-code contract (from scripts/divergence_monitor.py):
#   0 = all portfolios OK or WARNING   -> no action
#   1 = at least one portfolio BREACH  -> alert
#   2 = hard error (DB/backtest/args)  -> page
# NOTE: deliberately NOT using `set -e` around the python call, because exit
# codes 1 and 2 are meaningful signals we branch on, not failures to abort on.

set -uo pipefail

ALGO_DIR="/Users/huiliang/GitHub/algo-poc"
VENV="$ALGO_DIR/.venv/bin/python"
LOG_DIR="$HOME/ibc/logs"
METRICS_DIR="$HOME/ibc/metrics"
LOG_FILE="$LOG_DIR/divergence_$(date +%Y%m%d).log"
PROM_FILE="$METRICS_DIR/divergence.prom"

mkdir -p "$LOG_DIR" "$METRICS_DIR"

echo "$(date): Starting daily divergence monitor" >> "$LOG_FILE"

cd "$ALGO_DIR"
"$VENV" scripts/divergence_monitor.py \
    --prometheus-textfile "$PROM_FILE" \
    >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

case "$EXIT_CODE" in
    0)
        echo "$(date): Divergence monitor OK (exit 0)" >> "$LOG_FILE"
        ;;
    1)
        echo "$(date): ALERT - divergence BREACH (exit 1)" >> "$LOG_FILE"
        # Notifications are disabled in config/default.yaml today. When enabled,
        # wire a real alert here, e.g.:
        #   "$VENV" -m scripts.ops.notify --priority high --msg "divergence breach $(date +%F)"
        ;;
    2)
        echo "$(date): PAGE - divergence monitor hard error (exit 2)" >> "$LOG_FILE"
        # Page on-call here once a channel exists.
        ;;
    *)
        echo "$(date): UNEXPECTED exit code $EXIT_CODE from divergence monitor" >> "$LOG_FILE"
        ;;
esac

# Clean up logs older than 30 days
find "$LOG_DIR" -name "divergence_*.log" -mtime +30 -delete 2>/dev/null

exit $EXIT_CODE
