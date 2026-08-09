#!/bin/zsh
# launchd wrapper for com.algopoc.sentiment-collect — sets up the working
# directory and DB wiring the plist itself doesn't carry (secrets stay in
# the plist's EnvironmentVariables block; environment/DB wiring lives here,
# same split as deploy/launchd/run_paper.sh and run_divergence.sh).
cd /Users/huiliang/GitHub/algo-poc
# The paper DB is the dockerized postgres on a machine-local port (see
# docker-compose.override.yml); config/default.yaml's localhost:5432 default
# points at nothing on this machine. Postgres now requires auth (T3
# message-bus lockdown) — credentials live in the repo's gitignored `.env`,
# same split as the Telegram credentials in deploy/launchd/gateway_watchdog.sh.
ENV_FILE=/Users/huiliang/GitHub/algo-poc/.env
DB_PASSWORD=$(grep '^POSTGRES_PASSWORD=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)
if [ -z "$DB_PASSWORD" ]; then
    echo "$(date): ERROR - POSTGRES_PASSWORD not set in $ENV_FILE" >&2
    exit 1
fi
export ALGO_DATABASE_URL="postgresql://algo:${DB_PASSWORD}@localhost:55432/algo_poc"
exec /Users/huiliang/GitHub/algo-poc/.venv/bin/python scripts/collect_sentiment.py --aggregate-days 5
