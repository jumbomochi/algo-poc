#!/bin/bash
# Daily backup of the dockerized paper DB (RPO <= 1 day).
# Runs at 05:15 SGT every day — after the 04:15 paper run and 04:45 divergence
# monitor have written the day's rows, so each dump contains that day's state.
#
# Dumps pg_dump custom format (compressed, pg_restore-able) via docker exec,
# verifies the archive is readable, prunes dumps older than 30 days, and
# Telegram-alerts on any failure. Success is logged, not alerted.
#
# Restore runbook: docs/operations/backups.md

set -uo pipefail

# launchd's default PATH lacks /usr/local/bin, where the docker CLI lives.
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

CONTAINER="algo-poc-postgres-1"
DB_USER="algo"
DB_NAME="algo_poc"
BACKUP_DIR="$HOME/ibc/backups"
LOG_DIR="$HOME/ibc/logs"
LOG_FILE="$LOG_DIR/db_backup_$(date +%Y%m%d).log"
DUMP_FILE="$BACKUP_DIR/algo_poc_$(date +%Y%m%d_%H%M%S).dump"
RETENTION_DAYS=30
ALGO_DIR="/Users/huiliang/GitHub/algo-poc"
# Secrets come from the macOS login keychain via the shared loader. Sourced by
# path from the repo (never from the deployed ~/ibc copy) so there is exactly
# one implementation of the lookup and it cannot drift.
ALGO_SECRETS_ENV_FILE="$ALGO_DIR/.env"   # regular-file fallback only
# shellcheck source=deploy/launchd/secrets.sh
. "$ALGO_DIR/deploy/launchd/secrets.sh"
# Shared best-effort Telegram sender (KAN-43), sourced by path for the same
# reason: one copy of the credential-reading logic that cannot drift.
ALGO_JOB_LABEL="db backup"
# shellcheck source=deploy/launchd/lib/telegram.sh
. "$ALGO_DIR/deploy/launchd/lib/telegram.sh"

ts() { date "+%Y-%m-%d %H:%M:%S %Z"; }

fail() {
    echo "$(ts): ERROR - $1" >> "$LOG_FILE"
    telegram "❌ Paper-DB backup FAILED: $1 — RPO clock is running. See ~/ibc/logs/$(basename "$LOG_FILE")."
    exit 1
}

mkdir -p "$BACKUP_DIR" "$LOG_DIR"
echo "$(ts): Starting daily paper-DB backup" >> "$LOG_FILE"

# Drift guard: warn loudly if this deployed copy has fallen behind the repo
# canonical. The 2026-08-11 cold-boot auth failure was a stale ~/ibc copy still
# using the pre-T3 default DB password. Warn-only — a legitimately newer
# deployed copy must not block the run. Resync with deploy/launchd/deploy.sh.
CANON="$ALGO_DIR/deploy/launchd/$(basename "$0")"
if [ -f "$CANON" ] && ! cmp -s "$0" "$CANON"; then
    echo "$(date): WARNING - $(basename "$0") differs from repo canonical ($CANON); run deploy/launchd/deploy.sh to resync" >> "$LOG_FILE"
fi

# The DB lives in the docker volume; if the container is down there is nothing
# to dump from and the RPO guarantee is at risk — alert, don't silently skip.
docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true \
    || fail "postgres container $CONTAINER is not running"

docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" --format=custom \
    > "$DUMP_FILE" 2>>"$LOG_FILE" \
    || { rm -f "$DUMP_FILE"; fail "pg_dump exited non-zero"; }

# Verify the archive is a readable pg_dump and non-trivial in size.
[ "$(stat -f %z "$DUMP_FILE")" -ge 1024 ] \
    || fail "dump suspiciously small: $(stat -f %z "$DUMP_FILE") bytes ($DUMP_FILE)"
docker exec -i "$CONTAINER" pg_restore --list < "$DUMP_FILE" >/dev/null 2>>"$LOG_FILE" \
    || fail "pg_restore --list cannot read $DUMP_FILE"

SIZE=$(du -h "$DUMP_FILE" | cut -f1)
echo "$(ts): Backup OK: $DUMP_FILE ($SIZE)" >> "$LOG_FILE"

# Retention: prune local dumps and job logs older than RETENTION_DAYS.
find "$BACKUP_DIR" -name "algo_poc_*.dump" -mtime "+$RETENTION_DAYS" -delete 2>/dev/null
find "$LOG_DIR" -name "db_backup_*.log" -mtime "+$RETENTION_DAYS" -delete 2>/dev/null

exit 0
