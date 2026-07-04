#!/bin/bash
# Daily paper trading runner for algo-poc
# Runs after US market close (4:15 AM SGT / 4:15 PM ET)
# Signals are computed on finalized daily bars to avoid intraday noise

ALGO_DIR="/Users/huiliang/GitHub/algo-poc"
VENV="$ALGO_DIR/.venv/bin/python"
LOG_DIR="$HOME/ibc/logs"
LOG_FILE="$LOG_DIR/paper_trading_$(date +%Y%m%d).log"

# The paper DB is the dockerized postgres, published on a machine-local port
# (see docker-compose.override.yml). config/default.yaml's localhost:5432
# default points at nothing on this machine.
export ALGO_DATABASE_URL="postgresql://algo:algo@localhost:55432/algo_poc"

echo "$(date): Starting daily paper trading run" >> "$LOG_FILE"

# Check IB Gateway is running
if ! nc -z 127.0.0.1 7497 2>/dev/null; then
    echo "$(date): ERROR - IB Gateway not reachable on port 7497" >> "$LOG_FILE"
    exit 1
fi

# Check the paper DB is reachable (docker compose stack must be up)
if ! nc -z 127.0.0.1 55432 2>/dev/null; then
    echo "$(date): ERROR - paper DB not reachable on port 55432 (is docker compose up?)" >> "$LOG_FILE"
    exit 1
fi

# Run paper trading
cd "$ALGO_DIR"
"$VENV" scripts/run_paper.py >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

echo "$(date): Paper trading run completed (exit code: $EXIT_CODE)" >> "$LOG_FILE"

# Clean up logs older than 30 days
find "$LOG_DIR" -name "paper_trading_*.log" -mtime +30 -delete 2>/dev/null

exit $EXIT_CODE
