#!/bin/bash
# Daily paper trading runner for algo-poc
# Runs after US market close (4:15 AM SGT / 4:15 PM ET)
# Signals are computed on finalized daily bars to avoid intraday noise

# Overridable only so tests/deploy/test_deadman_ping.py can drive this wrapper
# end-to-end against a stub tree — launchd starts jobs with an empty
# environment, so production always takes the default. Never export ALGO_DIR
# in a login shell: a manual run would then use whatever tree that points at.
ALGO_DIR="${ALGO_DIR:-/Users/huiliang/GitHub/algo-poc}"
VENV="$ALGO_DIR/.venv/bin/python"
LOG_DIR="$HOME/ibc/logs"
LOG_FILE="$LOG_DIR/paper_trading_$(date +%Y%m%d).log"
# Secrets come from the macOS login keychain via the shared loader. Sourced by
# path from the repo (never from the deployed ~/ibc copy) so there is exactly
# one implementation of the lookup and it cannot drift.
ALGO_SECRETS_ENV_FILE="$ALGO_DIR/.env"   # regular-file fallback only
# shellcheck source=deploy/launchd/secrets.sh
. "$ALGO_DIR/deploy/launchd/secrets.sh"
# KAN-15: the external dead-man switch. Sourced by path for the same reason.
# shellcheck source=deploy/launchd/deadman.sh
. "$ALGO_DIR/deploy/launchd/deadman.sh"

# AC#17: create the log directory before the first write. Every other wrapper
# does this; run_paper.sh did not, so on a fresh host the opening line (and the
# credential/gateway errors that follow) vanished into a failed redirect — the
# run died with no log at all to explain why.
mkdir -p "$LOG_DIR"

echo "$(date): Starting daily paper trading run" >> "$LOG_FILE"

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
# canonical. The 2026-08-11 cold-boot auth failure was a stale ~/ibc copy still
# using the pre-T3 default DB password. Warn-only — a legitimately newer
# deployed copy must not block the run. Resync with deploy/launchd/deploy.sh.
CANON="$ALGO_DIR/deploy/launchd/$(basename "$0")"
if [ -f "$CANON" ] && ! cmp -s "$0" "$CANON"; then
    echo "$(date): WARNING - $(basename "$0") differs from repo canonical ($CANON); run deploy/launchd/deploy.sh to resync" >> "$LOG_FILE"
fi

# Bounded wait for a TCP port to accept connections. On a cold boot the docker
# stack and IB Gateway can lag the 04:15 trigger; the old behaviour hard-failed
# on the first probe and forced a manual rerun. Retries let it self-heal.
# $1=host $2=port $3=label $4=timeout_sec
wait_for_port() {
    local host="$1" port="$2" label="$3" timeout="${4:-600}"
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

# The paper DB and redis are the dockerized instances, published on
# machine-local ports (see docker-compose.override.yml). config/default.yaml's
# localhost defaults point at nothing on this machine. Both now require auth
# (T3 message-bus lockdown).
if ! algo_load_secrets POSTGRES_PASSWORD REDIS_PASSWORD; then
    echo "$(date): ERROR - $ALGO_SECRETS_ERROR" >> "$LOG_FILE"
    # Telegram needs a credential we may not have; alert through the
    # secret-free channel too, so a locked keychain is still noticed today
    # rather than in two days.
    algo_alert_local "paper run aborted 04:15 — $ALGO_SECRETS_ERROR"
    telegram "🚨 Paper trading run ABORTED: $ALGO_SECRETS_ERROR"
    exit 1
fi
export ALGO_DATABASE_URL="postgresql://algo:${POSTGRES_PASSWORD}@localhost:55432/algo_poc"
export ALGO_REDIS_URL="redis://:${REDIS_PASSWORD}@localhost:56379/0"

# Wait up to 10 min for IB Gateway. The watchdog kickstarts it within ~10 min
# of a cold boot, and the 04:45 divergence job leaves 30 min of headroom.
if ! wait_for_port 127.0.0.1 7497 "IB Gateway" 600; then
    algo_alert_local "paper run aborted — IB Gateway never came up on 7497"
    telegram "🚨 Paper trading run ABORTED: IB Gateway not reachable on 7497 after 10 min."
    exit 1
fi

# Wait up to 5 min for the dockerized paper DB (docker compose stack coming up).
if ! wait_for_port 127.0.0.1 55432 "paper DB (docker compose up?)" 300; then
    algo_alert_local "paper run aborted — paper DB never came up on 55432"
    telegram "🚨 Paper trading run ABORTED: paper DB not reachable on 55432 after 5 min (docker compose up?)."
    exit 1
fi

# Run paper trading. --publish bridges the signals into the service
# pipeline (risk -> execution -> real IB paper orders) for gates 4-6
# evidence; the simulated book commits regardless. --no-entries-disabled
# opts entries in (the CLI flag defaults True; the config gate + fail-closed
# reconciliation still block buys unless reconciliation is `ok`).
cd "$ALGO_DIR"

# Fail loudly if the paper DB schema is behind the code's migrations.
# Without this, a migration landing without `alembic upgrade head` surfaces
# mid-run as a cryptic psycopg2 UndefinedColumn error (2026-07-25 incident).
ALEMBIC="$ALGO_DIR/.venv/bin/alembic"
DB_REV=$("$ALEMBIC" current 2>/dev/null | grep -oE '[0-9a-f]{12}' | head -1)
HEAD_REV=$("$ALEMBIC" heads 2>/dev/null | grep -oE '[0-9a-f]{12}' | head -1)
if [ -z "$HEAD_REV" ]; then
    echo "$(date): ERROR - could not determine alembic head revision" >> "$LOG_FILE"
    algo_alert_local "paper run aborted — could not determine alembic head revision"
    telegram "🚨 Paper trading run ABORTED: could not determine alembic head revision."
    exit 1
fi
if [ "$DB_REV" != "$HEAD_REV" ]; then
    echo "$(date): ERROR - paper DB schema out of date (DB at '${DB_REV:-none}', head '$HEAD_REV'); run '.venv/bin/alembic upgrade head' with ALGO_DATABASE_URL set" >> "$LOG_FILE"
    algo_alert_local "paper run aborted — DB schema at '${DB_REV:-none}', head '$HEAD_REV'"
    telegram "🚨 Paper trading run ABORTED: paper DB schema out of date (DB '${DB_REV:-none}' vs head '$HEAD_REV'). Run: .venv/bin/alembic upgrade head"
    exit 1
fi

"$VENV" scripts/run_paper.py --publish --no-entries-disabled >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

echo "$(date): Paper trading run completed (exit code: $EXIT_CODE)" >> "$LOG_FILE"

# A non-zero exit means no signals were committed for the day. Say so out loud:
# the 2026-08-13/14 aborts were written to this log and to the 04:52 pipeline
# report, and still went unnoticed for two days because nothing pushed.
if [ "$EXIT_CODE" != "0" ]; then
    algo_alert_local "paper run FAILED (exit $EXIT_CODE) — see $LOG_FILE"
    telegram "🚨 Paper trading run FAILED (exit $EXIT_CODE). No signals committed today. See $LOG_FILE"
fi

# KAN-15: tell the OUTSIDE world the run happened. Every alert above this line
# is sent by this host about this host, so none of them can fire when the Mac
# is off, asleep, or off the network — which is exactly what "no run at all"
# looks like, and exactly what nobody noticed on 2026-08-13/14. Pinged only on
# success; a failed run must stay silent so the external check pages.
#
# Deliberately AFTER the failure alerts and deliberately incapable of failing
# (see deploy/launchd/deadman.sh) — this reports on the run, it must never be
# able to change its outcome.
algo_deadman_ping "$EXIT_CODE"
echo "$(date): dead-man switch: $ALGO_DEADMAN_STATUS" >> "$LOG_FILE"

# Clean up logs older than 30 days
find "$LOG_DIR" -name "paper_trading_*.log" -mtime +30 -delete 2>/dev/null

exit $EXIT_CODE
