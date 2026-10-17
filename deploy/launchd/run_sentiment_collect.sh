#!/bin/bash
# Hourly sentiment collector for algo-poc (research only; nothing trades on it).
# Spec: docs/superpowers/specs/2026-08-02-social-sentiment-research-design.md
# Runbook: docs/operations/sentiment-research.md
#
# Collects every enabled source that has its credentials, scores the messages,
# and rebuilds the last 5 sessions of sentiment_daily. The evaluation clock
# (scripts/sentiment_eval.py) runs from the first cycle that stores messages,
# so a job that is installed but not collecting costs calendar time silently.
#
# CREDENTIALS
# Each source's key is a per-job credential in the login keychain (service
# algo-poc), like the earnings refresh's ALPHAVANTAGE_API_KEY, and never lives
# in the plist: secrets.sh --import --only FINNHUB_API_KEY. A source whose key
# is absent is skipped and named in the log; the collector itself refuses
# (exit 1, "no_sources_enabled") only when no source is left. Absent keys are
# not an alert: this job is optional research and runs 24 times a day.
#
# EXIT CODES
#   0 = a cycle completed (a failing source logs source_fetch_failed and keeps
#       its cursor, so its gap stays visible to the evaluation)
#   1 = no paper DB password, or no source has its credentials
#
# NO DEAD-MAN: research only, and hourly. A missed cycle loses nothing (each
# cycle re-reads from the source's cursor with an hour of overlap), and a long
# outage is caught where it matters: scripts/sentiment_eval.py exits 2 when a
# source's gaps exceed 10% of the window, and the daily pipeline report's
# launchd wiring check names this job if it is installed but not loaded.
#
# bash, not zsh: the shared secret loader distinguishes "sourced" from
# "executed" via ${BASH_SOURCE[0]}, which zsh does not define.

# The deploy clone, not the dev checkout — see deploy/launchd/README.md (KAN-72).
ALGO_DIR="${ALGO_DIR:-/Users/huiliang/algo-poc-deploy}"
LOG_DIR="$HOME/ibc/logs"
mkdir -p "$LOG_DIR"
cd "$ALGO_DIR" || exit 1

ALGO_SECRETS_ENV_FILE="$ALGO_DIR/.env"   # regular-file fallback only
# shellcheck source=deploy/launchd/secrets.sh
. "$ALGO_DIR/deploy/launchd/secrets.sh"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

# Logged, not alerted: a locked keychain already pages through the paper run,
# and this job would repeat the desktop alert 24 times a day.
if ! algo_load_secrets POSTGRES_PASSWORD; then
    echo "$(ts): ERROR - $ALGO_SECRETS_ERROR" >&2
    exit 1
fi
# The paper DB is the dockerized postgres on a machine-local port (see
# docker-compose.override.yml).
export ALGO_DATABASE_URL="postgresql://algo:${POSTGRES_PASSWORD}@localhost:55432/algo_poc"

# Source keys: exported when present, named when absent, never printed. An
# absent key is normal (that source is skipped); any other reason a key could
# not be read, such as a locked keychain, is logged as itself.
keyed="" missing=""
for name in FINNHUB_API_KEY REDDIT_CLIENT_ID REDDIT_CLIENT_SECRET; do
    if algo_load_secrets "$name"; then
        keyed="$keyed $name"
    else
        missing="$missing $name"
        case "$ALGO_SECRETS_ERROR" in
            *"has no item for"*) ;;   # not imported (with or without a .env)
            *) echo "$(ts): WARNING - $name unreadable: $ALGO_SECRETS_ERROR" >&2 ;;
        esac
    fi
done
echo "$(ts): sentiment collect — keys present:${keyed:- none}; absent:${missing:- none}"

exec "$ALGO_DIR/.venv/bin/python" scripts/collect_sentiment.py --aggregate-days 5
