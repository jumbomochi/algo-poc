#!/bin/bash
# Daily backup of the dockerized paper DB (RPO <= 1 day).
# Runs at 05:15 SGT every day — after the 04:15 paper run and 04:45 divergence
# monitor have written the day's rows, so each dump contains that day's state.
#
# Dumps pg_dump custom format (compressed, pg_restore-able) via docker exec,
# verifies the archive is readable, prunes dumps older than 30 days, and
# Telegram-alerts on any failure. Success is logged, not alerted.
#
# Each verified dump is also rsynced to iCloud Drive for offsite coverage.
# Deliberately NOT rsync --delete: a wipe of the local backup dir must not
# propagate offsite. Both sides are pruned independently by age instead.
#
# Restore runbook: docs/operations/backups.md

set -uo pipefail

# launchd's default PATH lacks /usr/local/bin, where the docker CLI lives.
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

CONTAINER="algo-poc-postgres-1"
DB_USER="algo"
DB_NAME="algo_poc"
BACKUP_DIR="$HOME/ibc/backups"
OFFSITE_DIR="$HOME/Library/Mobile Documents/com~apple~CloudDocs/algo-poc-backups"
LOG_DIR="$HOME/ibc/logs"
LOG_FILE="$LOG_DIR/db_backup_$(date +%Y%m%d).log"
DUMP_FILE="$BACKUP_DIR/algo_poc_$(date +%Y%m%d_%H%M%S).dump"
RETENTION_DAYS=30
ENV_FILE="/Users/huiliang/GitHub/algo-poc/.env"

ts() { date "+%Y-%m-%d %H:%M:%S %Z"; }

telegram() {
    # Best-effort Telegram alert; never fails the backup job.
    [ -f "$ENV_FILE" ] || return 0
    local token chat
    token=$(grep '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2-)
    chat=$(grep '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | head -1 | cut -d= -f2-)
    [ -n "$token" ] && [ -n "$chat" ] || return 0
    curl -s -m 10 "https://api.telegram.org/bot${token}/sendMessage" \
        -d chat_id="$chat" --data-urlencode text="$1" >/dev/null 2>&1 || true
}

fail() {
    echo "$(ts): ERROR - $1" >> "$LOG_FILE"
    telegram "❌ Paper-DB backup FAILED: $1 — RPO clock is running. See ~/ibc/logs/$(basename "$LOG_FILE")."
    exit 1
}

mkdir -p "$BACKUP_DIR" "$LOG_DIR"
echo "$(ts): Starting daily paper-DB backup" >> "$LOG_FILE"

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

# Offsite copy to iCloud Drive. Upload to Apple's servers is asynchronous —
# the file lands in the local iCloud folder immediately and syncs when the
# machine is online. A failure here breaks the offsite guarantee: alert, but
# exit 0 (the local backup — the primary RPO layer — already succeeded).
mkdir -p "$OFFSITE_DIR"
if rsync -a "$BACKUP_DIR"/algo_poc_*.dump "$OFFSITE_DIR"/ 2>>"$LOG_FILE"; then
    echo "$(ts): Offsite copy OK -> $OFFSITE_DIR" >> "$LOG_FILE"
    find "$OFFSITE_DIR" -name "algo_poc_*.dump" -mtime "+$RETENTION_DAYS" -delete 2>/dev/null
else
    echo "$(ts): ERROR - offsite rsync to iCloud failed" >> "$LOG_FILE"
    telegram "⚠️ Paper-DB backup: local dump OK but offsite rsync to iCloud FAILED. See ~/ibc/logs/$(basename "$LOG_FILE")."
fi

# Retention: prune dumps and job logs older than RETENTION_DAYS.
find "$BACKUP_DIR" -name "algo_poc_*.dump" -mtime "+$RETENTION_DAYS" -delete 2>/dev/null
find "$LOG_DIR" -name "db_backup_*.log" -mtime "+$RETENTION_DAYS" -delete 2>/dev/null

exit 0
