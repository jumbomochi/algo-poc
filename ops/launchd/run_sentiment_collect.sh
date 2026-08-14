#!/bin/bash
# launchd wrapper for com.algopoc.sentiment-collect — sets up the working
# directory and DB wiring the plist itself doesn't carry (secrets stay in
# the plist's EnvironmentVariables block; environment/DB wiring lives here,
# same split as deploy/launchd/run_paper.sh and run_divergence.sh).
#
# bash, not zsh: the shared secret loader distinguishes "sourced" from
# "executed" via ${BASH_SOURCE[0]}, which zsh does not define — under zsh the
# guard collapses to $0 == $0 and the loader would run its CLI instead of
# defining functions. Nothing here needs zsh.
ALGO_DIR="/Users/huiliang/GitHub/algo-poc"
cd "$ALGO_DIR" || exit 1

# The paper DB is the dockerized postgres on a machine-local port (see
# docker-compose.override.yml); config/default.yaml's localhost:5432 default
# points at nothing on this machine. Postgres now requires auth (T3
# message-bus lockdown). Secrets come from the macOS login keychain via the
# shared loader — see deploy/launchd/secrets.sh for why not a plaintext .env.
ALGO_SECRETS_ENV_FILE="$ALGO_DIR/.env"   # regular-file fallback only
# shellcheck source=deploy/launchd/secrets.sh
. "$ALGO_DIR/deploy/launchd/secrets.sh"

if ! algo_load_secrets POSTGRES_PASSWORD; then
    echo "$(date): ERROR - $ALGO_SECRETS_ERROR" >&2
    algo_alert_local "sentiment collect aborted — $ALGO_SECRETS_ERROR"
    exit 1
fi
export ALGO_DATABASE_URL="postgresql://algo:${POSTGRES_PASSWORD}@localhost:55432/algo_poc"
exec "$ALGO_DIR/.venv/bin/python" scripts/collect_sentiment.py --aggregate-days 5
