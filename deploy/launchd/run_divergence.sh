#!/bin/bash
# Daily divergence monitor for algo-poc
# Runs at 4:45 AM SGT, ~30 min after run_paper.sh (4:15) has written the day's
# equity_snapshots row. Compares live paper equity to the latest backtest.
#
# Exit-code contract (from scripts/divergence_monitor.py):
#   0 = all portfolios OK or WARNING       -> no action
#   1 = at least one portfolio BREACH      -> alert
#   2 = hard error (DB/backtest/args)      -> page
#   3 = baseline backtest not comparable   -> alert; the monitor is BLIND
#       (same-bar fills, no commission floor, or a survivorship-biased
#        universe: every report is forced to NO_DATA). Regenerate the baseline
#        per docs/operations/backtest-baseline.md. Do NOT read this as OK.
# NOTE: deliberately NOT using `set -e` around the python call, because exit
# codes 1 and 2 are meaningful signals we branch on, not failures to abort on.

set -uo pipefail

ALGO_DIR="/Users/huiliang/GitHub/algo-poc"
VENV="$ALGO_DIR/.venv/bin/python"
LOG_DIR="$HOME/ibc/logs"
METRICS_DIR="$HOME/ibc/metrics"
LOG_FILE="$LOG_DIR/divergence_$(date +%Y%m%d).log"
PROM_FILE="$METRICS_DIR/divergence.prom"
# Secrets come from the macOS login keychain via the shared loader. Sourced by
# path from the repo (never from the deployed ~/ibc copy) so there is exactly
# one implementation of the lookup and it cannot drift.
ALGO_SECRETS_ENV_FILE="$ALGO_DIR/.env"   # regular-file fallback only
# shellcheck source=deploy/launchd/secrets.sh
. "$ALGO_DIR/deploy/launchd/secrets.sh"

mkdir -p "$LOG_DIR" "$METRICS_DIR"

echo "$(date): Starting daily divergence monitor" >> "$LOG_FILE"

# Best-effort Telegram alert. A missing credential is LOGGED, never silently
# swallowed — muting this is what hid the 2026-08-13/14 outage for two days.
telegram() {
    local token chat
    if ! algo_secret_into TELEGRAM_BOT_TOKEN; then
        echo "$(date): WARNING - cannot send alert: $ALGO_SECRETS_ERROR" >> "$LOG_FILE"
        return 0
    fi
    token="$_ALGO_SECRET_VALUE"
    if ! algo_secret_into TELEGRAM_CHAT_ID; then
        echo "$(date): WARNING - cannot send alert: $ALGO_SECRETS_ERROR" >> "$LOG_FILE"
        return 0
    fi
    chat="$_ALGO_SECRET_VALUE"
    curl -s -m 10 "https://api.telegram.org/bot${token}/sendMessage" \
        -d chat_id="$chat" --data-urlencode text="$1" >/dev/null 2>&1 || true
}

# Drift guard: warn loudly if this deployed copy has fallen behind the repo
# canonical. On 2026-08-11 the deployed copy was a stale pre-T3 revision (old
# default DB password) missing the exit-3 handler, so a BLIND baseline logged
# as "UNEXPECTED exit code 3". Warn-only. Resync with deploy/launchd/deploy.sh.
CANON="$ALGO_DIR/deploy/launchd/$(basename "$0")"
if [ -f "$CANON" ] && ! cmp -s "$0" "$CANON"; then
    echo "$(date): WARNING - $(basename "$0") differs from repo canonical ($CANON); run deploy/launchd/deploy.sh to resync" >> "$LOG_FILE"
fi

# Bounded wait for a TCP port to accept connections. $1=host $2=port $3=label
# $4=timeout_sec. Lets a cold boot self-heal instead of paging (exit 2) when
# the docker stack is still coming up.
wait_for_port() {
    local host="$1" port="$2" label="$3" timeout="${4:-300}"
    local waited=0
    until nc -z "$host" "$port" 2>/dev/null; do
        if [ "$waited" -ge "$timeout" ]; then
            echo "$(date): ERROR - $label not reachable on $host:$port after ${timeout}s" >> "$LOG_FILE"
            return 1
        fi
        [ "$waited" = "0" ] && echo "$(date): waiting for $label on $host:$port ..." >> "$LOG_FILE"
        sleep 15
        waited=$((waited + 15))
    done
    return 0
}

# The paper DB is the dockerized postgres on a machine-local port (see
# docker-compose.override.yml); config/default.yaml's localhost:5432 default
# points at nothing on this machine. Postgres now requires auth (T3
# message-bus lockdown).
if ! algo_load_secrets POSTGRES_PASSWORD; then
    echo "$(date): ERROR - $ALGO_SECRETS_ERROR" >> "$LOG_FILE"
    algo_alert_local "divergence monitor aborted 04:45 — $ALGO_SECRETS_ERROR"
    telegram "🚨 Divergence monitor ABORTED: $ALGO_SECRETS_ERROR"
    exit 2
fi
export ALGO_DATABASE_URL="postgresql://algo:${POSTGRES_PASSWORD}@localhost:55432/algo_poc"

# Wait up to 5 min for the dockerized paper DB before hard-erroring (exit 2).
if ! wait_for_port 127.0.0.1 55432 "paper DB (docker compose up?)" 300; then
    algo_alert_local "divergence monitor aborted — paper DB never came up on 55432"
    telegram "🚨 Divergence monitor ABORTED: paper DB not reachable on 55432 after 5 min (docker compose up?)."
    exit 2
fi

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
        telegram "🚨 Divergence BREACH ($(date +%F)) — live paper equity has diverged from the backtest baseline. See $LOG_FILE"
        ;;
    2)
        echo "$(date): PAGE - divergence monitor hard error (exit 2)" >> "$LOG_FILE"
        algo_alert_local "divergence monitor hard error (exit 2) — see $LOG_FILE"
        telegram "🚨 Divergence monitor HARD ERROR (exit 2) on $(date +%F). See $LOG_FILE"
        ;;
    3)
        echo "$(date): ALERT - divergence monitor BLIND: baseline backtest is not" \
             "comparable to live (exit 3). No drift detection is running until the" \
             "baseline is regenerated - see docs/operations/backtest-baseline.md" \
             >> "$LOG_FILE"
        # A blind monitor is an outage, not a pass.
        telegram "⚠️ Divergence monitor is BLIND (exit 3): the baseline backtest is not comparable to live, so every report is forced to NO_DATA. No drift detection is running. Regenerate per docs/operations/backtest-baseline.md"
        ;;
    *)
        echo "$(date): UNEXPECTED exit code $EXIT_CODE from divergence monitor" >> "$LOG_FILE"
        algo_alert_local "divergence monitor returned unexpected exit $EXIT_CODE"
        telegram "🚨 Divergence monitor returned UNEXPECTED exit code $EXIT_CODE. See $LOG_FILE"
        ;;
esac

# Clean up logs older than 30 days
find "$LOG_DIR" -name "divergence_*.log" -mtime +30 -delete 2>/dev/null

exit $EXIT_CODE
