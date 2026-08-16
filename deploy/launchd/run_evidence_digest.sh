#!/bin/bash
# Weekly evidence digest for algo-poc — Mondays 08:00 SGT. KAN-29.
#
# Sends one Telegram message summarising the week from the evidence store:
# epoch progress, per-sleeve divergence, equity, DLQ depth, alerts and drills.
# The message content, its ordering and its failure semantics all live in
# scripts/ops/evidence_digest.py; this wrapper only supplies credentials and
# runs it.
#
# WHY THE PYTHON SENDS, AND NOT THIS SCRIPT
# -----------------------------------------
# Every other wrapper here renders its own text and calls the shared bash
# telegram() helper. This one deliberately does not: the digest sends through
# services/notifications/channels.py's TelegramChannel so that the message and
# the dead-man ping are decided by the same code that knows whether the send
# actually succeeded. A wrapper cannot tell a delivered digest from a rendered
# one, and the dead-man switch's whole value is that it only fires on delivery.
#
# telegram() is still sourced, for the one case Python cannot report: the
# script failing before or during startup.

set -uo pipefail

ALGO_DIR="/Users/huiliang/GitHub/algo-poc"
VENV="$ALGO_DIR/.venv/bin/python"
LOG_DIR="$HOME/ibc/logs"
LOG_FILE="$LOG_DIR/evidence_digest_$(date +%Y%m%d).log"
# Secrets come from the macOS login keychain via the shared loader, sourced by
# path from the repo (never from the deployed ~/ibc copy) so there is exactly
# one implementation of the lookup and it cannot drift.
ALGO_SECRETS_ENV_FILE="$ALGO_DIR/.env"   # regular-file fallback only
# shellcheck source=deploy/launchd/secrets.sh
. "$ALGO_DIR/deploy/launchd/secrets.sh"
ALGO_JOB_LABEL="evidence digest"
# shellcheck source=deploy/launchd/lib/telegram.sh
. "$ALGO_DIR/deploy/launchd/lib/telegram.sh"
# shellcheck source=deploy/launchd/deadman.sh
. "$ALGO_DIR/deploy/launchd/deadman.sh"

mkdir -p "$LOG_DIR"
ts() { date '+%Y-%m-%d %H:%M:%S'; }

echo "$(ts): Starting weekly evidence digest" >> "$LOG_FILE"

# Drift guard: warn loudly if this deployed copy has fallen behind the repo
# canonical. Warn-only — a legitimately newer deployed copy must not block the
# run. Resync with deploy/launchd/deploy.sh.
CANON="$ALGO_DIR/deploy/launchd/$(basename "$0")"
if [ -f "$CANON" ] && ! cmp -s "$0" "$CANON"; then
    echo "$(ts): WARNING - $(basename "$0") differs from repo canonical ($CANON); run deploy/launchd/deploy.sh to resync" >> "$LOG_FILE"
fi

# Postgres and Redis to read the evidence store; Telegram to deliver it.
# A missing Telegram credential is NOT fatal here: the digest still runs, and
# TelegramChannel raises on send, which is reported as a failed send and
# withholds the dead-man ping — the outcome an undelivered digest should have.
if ! algo_load_secrets POSTGRES_PASSWORD REDIS_PASSWORD; then
    echo "$(ts): ERROR - $ALGO_SECRETS_ERROR" >> "$LOG_FILE"
    # Telegram needs a credential we may not have; use the secret-free channel
    # too, so a locked keychain is noticed today rather than in two days.
    algo_alert_local "evidence digest aborted — $ALGO_SECRETS_ERROR"
    telegram "❌ Weekly evidence digest ABORTED: $ALGO_SECRETS_ERROR"
    exit 1
fi
export ALGO_DATABASE_URL="postgresql://algo:${POSTGRES_PASSWORD}@localhost:55432/algo_poc"
export ALGO_REDIS_URL="redis://:${REDIS_PASSWORD}@localhost:56379/0"

# algo_load_secrets exports each name, which is how TelegramChannel reads them.
if ! algo_load_secrets TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID; then
    echo "$(ts): WARNING - $ALGO_SECRETS_ERROR; the digest will render but cannot be delivered" >> "$LOG_FILE"
fi

# Resolve the dead-man URL here and hand it to Python in the environment: the
# keychain lookup stays in KAN-15's loader rather than being reimplemented in
# the digest. The ping itself is Python's, because only Python knows whether
# the send succeeded.
if algo_deadman_url_for ALGO_DEADMAN_DIGEST_URL; then
    export ALGO_DEADMAN_DIGEST_URL="$_ALGO_DEADMAN_URL"
else
    echo "$(ts): WARNING - ALGO_DEADMAN_DIGEST_URL is in neither the environment nor the keychain; nothing outside this host will notice a missing digest. Import it: deploy/launchd/secrets.sh --import" >> "$LOG_FILE"
fi

cd "$ALGO_DIR"
"$VENV" scripts/ops/evidence_digest.py >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

if [ "$EXIT_CODE" -eq 0 ]; then
    echo "$(ts): digest sent" >> "$LOG_FILE"
else
    echo "$(ts): digest FAILED (exit $EXIT_CODE)" >> "$LOG_FILE"
    # The digest could not deliver itself, so this is the only channel left.
    telegram "❌ Weekly evidence digest FAILED (exit $EXIT_CODE) — see ~/ibc/logs/$(basename "$LOG_FILE"). The dead-man check was NOT pinged."
fi

find "$LOG_DIR" -name "evidence_digest_*.log" -mtime +90 -delete 2>/dev/null
exit $EXIT_CODE
