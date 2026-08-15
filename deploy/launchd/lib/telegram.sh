#!/bin/bash
# Shared best-effort Telegram sender for the algo-poc launchd wrappers.
#
# WHY THIS EXISTS
# ---------------
# Six wrappers each carried a hand-copied `telegram()`. They had already
# drifted: four raised `algo_alert_local` when no credential resolved, two did
# not — so on a locked keychain those two failed silently, which is the exact
# hole KAN-16 was opened to close. A fix applied to one copy (adding the
# `-m 10` timeout, say) silently misses the other five.
#
# SOURCED BY PATH FROM THE REPO — `. "$ALGO_DIR/deploy/launchd/lib/telegram.sh"`
# — never from ~/ibc, for the same reason as secrets.sh: exactly one copy of
# the logic, and it cannot drift. It lives under lib/ so deploy.sh's
# `"$SRC"/*.sh` glob does not reach it and cannot plant a decoy copy an
# operator could edit with no effect.
#
# Requires: secrets.sh sourced first (algo_secret_into, algo_alert_local).
# Expects:  $LOG_FILE        — where the "cannot alert" line goes (optional:
#                              falls back to /dev/null, because a helper whose
#                              contract is "never fails a caller" must not die
#                              on an unset variable under `set -u`)
#           $ALGO_JOB_LABEL  — this job's name, e.g. "divergence monitor"
#
# Usage:
#   ALGO_JOB_LABEL="divergence monitor"
#   . "$ALGO_DIR/deploy/launchd/lib/telegram.sh"
#   telegram "🚨 something happened"

ALGO_JOB_LABEL="${ALGO_JOB_LABEL:-algo-poc job}"

# Wrappers that want second-resolution stamps define ts(); the rest get date.
# Resolved at call time, so it does not matter whether ts() is defined before
# or after this file is sourced.
_algo_telegram_ts() {
    if declare -F ts >/dev/null 2>&1; then ts; else date; fi
}

# Best-effort Telegram alert. A missing credential is LOGGED and raised
# locally, never silently swallowed — the old `[ -f "$ENV_FILE" ]` guard was
# FALSE for the 1Password FIFO that replaced .env on 2026-08-12, so every
# alert path returned *success* and stayed quiet for two days.
#
# Always returns 0: the job's own verdict matters more than the delivery of its
# notification, and no caller's exit code may depend on Telegram.
telegram() {
    local token chat
    if ! algo_secret_into TELEGRAM_BOT_TOKEN; then
        echo "$(_algo_telegram_ts): WARNING - cannot send alert: $ALGO_SECRETS_ERROR" >> "${LOG_FILE:-/dev/null}"
        algo_alert_local "$ALGO_JOB_LABEL cannot alert: $ALGO_SECRETS_ERROR"
        return 0
    fi
    token="$_ALGO_SECRET_VALUE"
    if ! algo_secret_into TELEGRAM_CHAT_ID; then
        echo "$(_algo_telegram_ts): WARNING - cannot send alert: $ALGO_SECRETS_ERROR" >> "${LOG_FILE:-/dev/null}"
        algo_alert_local "$ALGO_JOB_LABEL cannot alert: $ALGO_SECRETS_ERROR"
        return 0
    fi
    chat="$_ALGO_SECRET_VALUE"
    curl -s -m 10 "https://api.telegram.org/bot${token}/sendMessage" \
        -d chat_id="$chat" --data-urlencode text="$1" >/dev/null 2>&1 || true
    return 0
}
