#!/bin/bash
# Daily paper trading runner for algo-poc
# Runs after US market close (4:15 AM SGT / 4:15 PM ET)
# Signals are computed on finalized daily bars to avoid intraday noise

ALGO_DIR="/Users/huiliang/GitHub/algo-poc"
VENV="$ALGO_DIR/.venv/bin/python"
LOG_DIR="$HOME/ibc/logs"
LOG_FILE="$LOG_DIR/paper_trading_$(date +%Y%m%d).log"

# The paper DB and redis are the dockerized instances, published on
# machine-local ports (see docker-compose.override.yml). config/default.yaml's
# localhost defaults point at nothing on this machine.
export ALGO_DATABASE_URL="postgresql://algo:algo@localhost:55432/algo_poc"
export ALGO_REDIS_URL="redis://localhost:56379/0"

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

# Run paper trading. --publish bridges the signals into the service
# pipeline (risk -> execution -> real IB paper orders) for gates 4-6
# evidence; the simulated book commits regardless.
cd "$ALGO_DIR"

# Fail loudly if the paper DB schema is behind the code's migrations.
# Without this, a migration landing without `alembic upgrade head` surfaces
# mid-run as a cryptic psycopg2 UndefinedColumn error (2026-07-25 incident).
ALEMBIC="$ALGO_DIR/.venv/bin/alembic"
DB_REV=$("$ALEMBIC" current 2>/dev/null | grep -oE '[0-9a-f]{12}' | head -1)
HEAD_REV=$("$ALEMBIC" heads 2>/dev/null | grep -oE '[0-9a-f]{12}' | head -1)
if [ -z "$HEAD_REV" ]; then
    echo "$(date): ERROR - could not determine alembic head revision" >> "$LOG_FILE"
    exit 1
fi
if [ "$DB_REV" != "$HEAD_REV" ]; then
    echo "$(date): ERROR - paper DB schema out of date (DB at '${DB_REV:-none}', head '$HEAD_REV'); run '.venv/bin/alembic upgrade head' with ALGO_DATABASE_URL set" >> "$LOG_FILE"
    exit 1
fi

"$VENV" scripts/run_paper.py --publish >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

echo "$(date): Paper trading run completed (exit code: $EXIT_CODE)" >> "$LOG_FILE"

# Clean up logs older than 30 days
find "$LOG_DIR" -name "paper_trading_*.log" -mtime +30 -delete 2>/dev/null

exit $EXIT_CODE
