#!/bin/bash
# Daily earnings refresh for algo-poc — 04:45 SGT, EVERY day. KAN-110.
#
# Runs scripts/fetch_earnings.py, which pulls Alpha Vantage's earnings calendar
# and the EARNINGS history of whichever tickers are due, within a per-run call
# budget, and merges them atomically into data.cache_dir/earnings.json. The
# design — what is fetched in which order, and exactly when the cache's
# fetched_at is advanced — is in that script's docstring.
#
# WHY DAILY, WEEKENDS INCLUDED
# ----------------------------
# KAN-109 marks earnings_drift DATA-DEGRADED once earnings.json's fetched_at is
# more than 2 days old (data.earnings.max_fetch_age_days, = the lookup window).
# A Tue-Sat job would leave the Tuesday paper run reading a Saturday fetch —
# ~3 days — and degrade the sleeve every single week. Every day, one missed run
# is still inside the bound; two are not.
#
# WHY 04:45 SGT
# -------------
# 04:45 SGT is 16:45 ET under EDT and 15:45 ET under EST, i.e. the US
# afternoon of the session the 05:15 paper run is about to price. By then:
#   * that session's PRE-market reports (~07:00 ET) are ~9 hours old, so the
#     paper run 30 minutes later can act on them on their own report date —
#     the bar the backtest enters on. A 07:00 SGT slot (19:00 ET) would only
#     reach the paper run a day later, at lookup offset 1;
#   * the PREVIOUS session's post-market reports (~16:05 ET) are a full day
#     old, so Alpha Vantage has certainly published their actuals;
#   * nothing else in the chain is running: it precedes the paper run (05:15),
#     divergence (05:45), report (05:52), backup (06:15), the Tuesday refresh
#     (06:30) and the Monday digest (08:00), and it touches neither IB nor the
#     database, so it competes with none of them. The run is ~5 min on the free
#     tier (21 calls, 13 s apart) and is bounded at 25 min, so it always ends
#     before 05:15. If it does not finish, the paper run reads the previous
#     file — the write is atomic, never a torn one.
# A post-market report from the session itself (16:05 ET) is usually NOT in
# yet (it is before the release under EST); it is fetched the next morning and
# reaches the paper run at offset 1, inside the 2-day window.
#
# Exit-code contract (scripts/fetch_earnings.py's, passed through):
#   0  = CURRENT: fetched_at advanced — every live recent reporter fetched.
#   1  = could not run (no API key, another refresh holds the lock, a crash,
#        an unreadable earnings.json it refuses to overwrite).
#   2  = FAILED: Alpha Vantage refused, rate-limited or errored before the live
#        universe was current. Progress is saved; fetched_at is NOT advanced.
#   3  = INCOMPLETE: the budget ran out first (the first days of a backfill, or
#        an earnings-season day with more reporters than budget).
# 124  = killed by the timeout below.
#
# DEAD-MAN SWITCH
# ---------------
# Pings $ALGO_DEADMAN_EARNINGS_URL only on exit 0 — the one outcome that
# advances fetched_at. Every other exit sends a Telegram from here; the switch
# is what notices the job not running at all (host asleep or off at 04:45 —
# launchd does not re-fire a missed calendar slot). Configure the check with
# cron `45 4 * * *` (Asia/Singapore); see docs/operations/dead-man-switches.md.

set -uo pipefail

# ALGO_DIR is overridable only so the tests can drive this wrapper against a
# stub tree — launchd starts jobs with an empty environment, so production
# always takes the default. Never export it in a login shell.
ALGO_DIR="${ALGO_DIR:-/Users/huiliang/algo-poc-deploy}"
VENV="$ALGO_DIR/.venv/bin/python"
LOG_DIR="$HOME/ibc/logs"
LOG_FILE="$LOG_DIR/earnings_refresh_$(date +%Y%m%d).log"
RETENTION_DAYS=30
# 25 min: a free-tier run is ~5 min, and the paper run starts at 05:15.
EARNINGS_TIMEOUT="${ALGO_EARNINGS_TIMEOUT_SECONDS:-1500}"
# Secrets come from the macOS login keychain via the shared loader, sourced by
# path from the repo (never from the deployed ~/ibc copy) so there is exactly
# one implementation of the lookup and it cannot drift.
ALGO_SECRETS_ENV_FILE="$ALGO_DIR/.env"   # regular-file fallback only
# shellcheck source=deploy/launchd/secrets.sh
. "$ALGO_DIR/deploy/launchd/secrets.sh"
ALGO_JOB_LABEL="earnings refresh"
# shellcheck source=deploy/launchd/lib/telegram.sh
. "$ALGO_DIR/deploy/launchd/lib/telegram.sh"
# shellcheck source=deploy/launchd/deadman.sh
. "$ALGO_DIR/deploy/launchd/deadman.sh"

mkdir -p "$LOG_DIR"
ts() { date '+%Y-%m-%d %H:%M:%S'; }

# Hold a power assertion for the life of this run (KAN-77). The host was found
# idle-sleeping after one minute on 2026-09-09; a sleep between two spaced
# requests would stretch a 5-minute run past the paper run.
#
# The missing-lib branch is not defensive padding: `. missing.sh` fails without
# aborting under `set -uo pipefail`, so the next line would be a bare
# "command not found" and the run would continue with no assertion and no
# record of why.
# shellcheck source=deploy/launchd/lib/power.sh
. "$ALGO_DIR/deploy/launchd/lib/power.sh" 2>/dev/null
if command -v algo_hold_power_assertion >/dev/null 2>&1; then
    algo_hold_power_assertion "$@"
else
    echo "$(date): WARNING - $ALGO_DIR/deploy/launchd/lib/power.sh could not be sourced;" \
         "this run holds no power assertion (KAN-77). Running anyway." >> "$LOG_FILE"
fi

echo "$(ts): Starting daily earnings refresh (Alpha Vantage)" >> "$LOG_FILE"

# Drift guard: warn loudly if this deployed copy has fallen behind the repo
# canonical. Warn-only — a legitimately newer deployed copy must not block the
# run. Resync with deploy/launchd/deploy.sh.
CANON="$ALGO_DIR/deploy/launchd/$(basename "$0")"
if [ -f "$CANON" ] && ! cmp -s "$0" "$CANON"; then
    echo "$(ts): WARNING - $(basename "$0") differs from repo canonical ($CANON); run deploy/launchd/deploy.sh to resync" >> "$LOG_FILE"
fi

finish() {
    find "$LOG_DIR" -name "earnings_refresh_*.log" -mtime "+$RETENTION_DAYS" -delete 2>/dev/null
    algo_deadman_ping "$1" ALGO_DEADMAN_EARNINGS_URL
    echo "$(ts): dead-man switch: $ALGO_DEADMAN_STATUS" >> "$LOG_FILE"
    exit "$1"
}

# The key travels to Python in the environment (algo_load_secrets exports it)
# and is never echoed: it is a query parameter on every request, and the
# fetcher scrubs it from everything it prints.
if ! algo_load_secrets ALPHAVANTAGE_API_KEY; then
    echo "$(ts): ERROR - $ALGO_SECRETS_ERROR" >> "$LOG_FILE"
    algo_alert_local "earnings refresh aborted — $ALGO_SECRETS_ERROR"
    telegram "❌ Earnings refresh ABORTED: $ALGO_SECRETS_ERROR. earnings_drift goes DATA-DEGRADED once earnings.json is 2 days old. The dead-man check was NOT pinged."
    finish 1
fi

# --universe pit: the live sleeve universe always comes first; any budget it
# does not need trickle-backfills the point-in-time universe for the backtests.
# The run's own lines start at this offset, so an earlier same-day run's
# summary is never mistaken for this one's.
LOG_OFFSET=$(wc -c < "$LOG_FILE" | tr -d ' ')
cd "$ALGO_DIR" || finish 1
# shellcheck source=deploy/launchd/lib/bounded.sh
. "$ALGO_DIR/deploy/launchd/lib/bounded.sh"
algo_run_bounded "$EARNINGS_TIMEOUT" \
    "$VENV" scripts/fetch_earnings.py --universe pit >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

RUN_OUTPUT=$(tail -c "+$((LOG_OFFSET + 1))" "$LOG_FILE" 2>/dev/null)
SUMMARY=$(printf '%s\n' "$RUN_OUTPUT" | grep '^EARNINGS_REFRESH ' | tail -1)
SUMMARY="${SUMMARY:-no summary line (crashed before reporting)}"
LOG_REF="~/ibc/logs/$(basename "$LOG_FILE")"

case "$EXIT_CODE" in
    0)
        echo "$(ts): earnings refresh OK — $SUMMARY" >> "$LOG_FILE"
        # Current, but a live ticker Alpha Vantage has nothing for is a name
        # earnings_drift silently cannot trade. Say so; still a healthy beat.
        NO_DATA=$(printf '%s\n' "$RUN_OUTPUT" | grep '^LIVE_NO_DATA: ' | tail -1)
        if [ -n "$NO_DATA" ]; then
            telegram "⚠️ Earnings refresh: ${NO_DATA#LIVE_NO_DATA: }. See $LOG_REF."
        fi
        ;;
    3)
        echo "$(ts): earnings refresh INCOMPLETE — $SUMMARY" >> "$LOG_FILE"
        telegram "⚠️ Earnings refresh INCOMPLETE: the call budget ran out before every live recent reporter was fetched, so earnings.json's fetched_at was NOT advanced (earnings_drift degrades once it is 2 days old). Expected for the first days of a backfill. $SUMMARY. See $LOG_REF."
        ;;
    2)
        echo "$(ts): earnings refresh FAILED — $SUMMARY" >> "$LOG_FILE"
        telegram "❌ Earnings refresh FAILED: Alpha Vantage refused or errored (rate limit?) before the live universe was current. Progress saved; fetched_at NOT advanced. $SUMMARY. See $LOG_REF. The dead-man check was NOT pinged."
        ;;
    124)
        echo "$(ts): earnings refresh TIMED OUT after ${EARNINGS_TIMEOUT}s" >> "$LOG_FILE"
        algo_alert_local "earnings refresh timed out after ${EARNINGS_TIMEOUT}s"
        telegram "⏱️ Earnings refresh TIMED OUT after ${EARNINGS_TIMEOUT}s and was killed (progress up to the kill is saved). See $LOG_REF. The dead-man check was NOT pinged."
        ;;
    *)
        echo "$(ts): earnings refresh ERROR (exit $EXIT_CODE) — $SUMMARY" >> "$LOG_FILE"
        telegram "❌ Earnings refresh could not run (exit $EXIT_CODE): $SUMMARY. See $LOG_REF. The dead-man check was NOT pinged."
        ;;
esac

finish "$EXIT_CODE"
