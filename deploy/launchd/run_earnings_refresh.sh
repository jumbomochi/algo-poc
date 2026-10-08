#!/bin/bash
# Daily earnings refresh for algo-poc — 04:45 SGT full run and 05:05 SGT
# top-up (`--top-up`), EVERY day. KAN-110.
#
# Runs scripts/fetch_earnings.py, which pulls Alpha Vantage's earnings calendar
# and the EARNINGS history of whichever tickers are due, within a per-run call
# budget, and merges them atomically into data.cache_dir/earnings.json. The
# design — what is fetched in which order, and exactly when the cache's
# fetched_at is advanced — is in that script's docstring.
#
# Two launchd jobs run this one wrapper:
#   local.algo-earnings-refresh  04:45  full run: calendar + up to 16 EARNINGS
#                                       requests; the ONLY run that can advance
#                                       fetched_at, and the one the dead-man
#                                       switch watches.
#   local.algo-earnings-topup    05:05  `--top-up`: no calendar call, at most 4
#                                       requests, live tickers whose report is
#                                       dated today/yesterday (ET) and still
#                                       lacks an actual. Never moves fetched_at.
#
# WHY DAILY, WEEKENDS INCLUDED
# ----------------------------
# KAN-109 marks earnings_drift DATA-DEGRADED once earnings.json's fetched_at is
# more than 2 days old (data.earnings.max_fetch_age_days, = the lookup window).
# A Tue-Sat job would leave the Tuesday paper run reading a Saturday fetch —
# ~3 days — and degrade the sleeve every single week. Every day, one missed run
# is still inside the bound; two are not.
#
# WHY 04:45, AND WHY A TOP-UP AT 05:05
# ------------------------------------
# The paper run (05:15 SGT, Tue-Sat) prices the US session that has just
# closed. 04:45 SGT is 16:45 ET under EDT and 15:45 ET under EST:
#   * that session's PRE-market reports (~07:00 ET) are ~9 hours old, so the
#     paper run can act on them on their own report date — the bar the
#     backtest enters on. A 07:00 SGT slot (19:00 ET) would reach the paper run
#     a day later;
#   * the PREVIOUS session's post-market reports (~16:05 ET) are a day old.
# What 04:45 cannot have is that session's own POST-market report: it is
# released ~16:05 ET, 20 minutes after 04:45 under EDT and AFTER it under EST.
# The backtest decides on close[t] with the report dated t, so live would enter
# a session late — and a Friday after-market report would be lost outright:
# Saturday's run misses it and Tuesday's run is at offset 3, outside the 0..+2
# lookup window. The 05:05 top-up (17:05 EDT) catches the ones Alpha Vantage
# has published by then. Under EST 05:05 SGT is 16:05 ET — the report is not
# out — so in EST months after-market reports still enter a day late and
# Friday after-market reports are still lost; docs/strategy.md and
# docs/operations/divergence-monitor.md record that as an expected divergence.
#
# Nothing else is running at either slot (paper 05:15, divergence 05:45, report
# 05:52, backup 06:15, Tuesday refresh 06:30, Monday digest 08:00), and neither
# run touches IB or the database. Both end before 05:15: the full run is ~4
# min on the free tier (17 requests, 13 s apart), bounded at 15 min (+20 s kill
# grace → by 05:00:20, before the top-up); the top-up is under a minute,
# bounded at 8 min (→ by 05:13:20). If either is cut short the paper run reads
# the previous file — every write is atomic, never a torn one. Should the full
# run still hold the lock at 05:05, the top-up skips (exit 75, logged only).
#
# Exit-code contract (scripts/fetch_earnings.py's, passed through):
#   0   = full: CURRENT, fetched_at advanced. top-up: done (perhaps nothing due).
#   1   = could not run (no API key, a crash, an unreadable earnings.json).
#   2   = FAILED: Alpha Vantage refused, rate-limited or errored. Progress is
#         saved; fetched_at is NOT advanced.
#   3   = INCOMPLETE (full only): the budget ran out first (the first days of a
#         backfill, or an earnings-season day with more reporters than budget).
#   75  = another refresh held the lock; nothing was done.
#   124 = killed by the timeout below (progress saved; the fetcher exits 124
#         itself on SIGTERM, and lib/bounded.sh maps a hard kill to 124 too).
#
# DEAD-MAN SWITCH
# ---------------
# The full run pings $ALGO_DEADMAN_EARNINGS_URL only on exit 0 — the one
# outcome that advances fetched_at. The top-up never pings: it cannot move
# fetched_at, so its health says nothing about whether the sleeve will degrade,
# and a second external check would page at 05:05 for at most one session of
# entry latency. Its failures (exit 1/2/124) still Telegram. One check, cron
# `45 4 * * *` (Asia/Singapore); see docs/operations/dead-man-switches.md.

set -uo pipefail

# ALGO_DIR is overridable only so the tests can drive this wrapper against a
# stub tree — launchd starts jobs with an empty environment, so production
# always takes the default. Never export it in a login shell.
ALGO_DIR="${ALGO_DIR:-/Users/huiliang/algo-poc-deploy}"
VENV="$ALGO_DIR/.venv/bin/python"
LOG_DIR="$HOME/ibc/logs"
LOG_FILE="$LOG_DIR/earnings_refresh_$(date +%Y%m%d).log"
RETENTION_DAYS=30
TOP_UP=0
[ "${1:-}" = "--top-up" ] && TOP_UP=1
if [ "$TOP_UP" = "1" ]; then
    MODE="top-up"
    FETCH_ARGS="--top-up"
    # 8 min: under a minute of work, and the paper run starts 10 min later.
    EARNINGS_TIMEOUT="${ALGO_EARNINGS_TOPUP_TIMEOUT_SECONDS:-480}"
else
    MODE="full"
    FETCH_ARGS="--universe pit"
    # 15 min: ~4 min of work, and it must be done before the 05:05 top-up.
    EARNINGS_TIMEOUT="${ALGO_EARNINGS_TIMEOUT_SECONDS:-900}"
fi
# Secrets come from the macOS login keychain via the shared loader, sourced by
# path from the repo (never from the deployed ~/ibc copy) so there is exactly
# one implementation of the lookup and it cannot drift.
ALGO_SECRETS_ENV_FILE="$ALGO_DIR/.env"   # regular-file fallback only
# shellcheck source=deploy/launchd/secrets.sh
. "$ALGO_DIR/deploy/launchd/secrets.sh"
ALGO_JOB_LABEL="earnings refresh ($MODE)"
# shellcheck source=deploy/launchd/lib/telegram.sh
. "$ALGO_DIR/deploy/launchd/lib/telegram.sh"
# shellcheck source=deploy/launchd/deadman.sh
. "$ALGO_DIR/deploy/launchd/deadman.sh"

mkdir -p "$LOG_DIR"
ts() { date '+%Y-%m-%d %H:%M:%S'; }

# Hold a power assertion for the life of this run (KAN-77). The host was found
# idle-sleeping after one minute on 2026-09-09; a sleep between two spaced
# requests would stretch the run past the paper run.
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

echo "$(ts): Starting daily earnings refresh ($MODE, Alpha Vantage)" >> "$LOG_FILE"

# Drift guard: warn loudly if this deployed copy has fallen behind the repo
# canonical. Warn-only — a legitimately newer deployed copy must not block the
# run. Resync with deploy/launchd/deploy.sh.
CANON="$ALGO_DIR/deploy/launchd/$(basename "$0")"
if [ -f "$CANON" ] && ! cmp -s "$0" "$CANON"; then
    echo "$(ts): WARNING - $(basename "$0") differs from repo canonical ($CANON); run deploy/launchd/deploy.sh to resync" >> "$LOG_FILE"
fi

finish() {
    find "$LOG_DIR" -name "earnings_refresh_*.log" -mtime "+$RETENTION_DAYS" -delete 2>/dev/null
    if [ "$TOP_UP" = "1" ]; then
        echo "$(ts): dead-man switch: not pinged (top-up runs never ping; the 04:45 full run owns the check)" >> "$LOG_FILE"
    else
        algo_deadman_ping "$1" ALGO_DEADMAN_EARNINGS_URL
        echo "$(ts): dead-man switch: $ALGO_DEADMAN_STATUS" >> "$LOG_FILE"
    fi
    exit "$1"
}

# The key travels to Python in the environment (algo_load_secrets exports it)
# and is never echoed: it is a query parameter on every request, and the
# fetcher scrubs it from everything it prints.
if ! algo_load_secrets ALPHAVANTAGE_API_KEY; then
    echo "$(ts): ERROR - $ALGO_SECRETS_ERROR" >> "$LOG_FILE"
    algo_alert_local "earnings refresh ($MODE) aborted — $ALGO_SECRETS_ERROR"
    telegram "❌ Earnings refresh ($MODE) ABORTED: $ALGO_SECRETS_ERROR. earnings_drift goes DATA-DEGRADED once earnings.json is 2 days old. The dead-man check was NOT pinged."
    finish 1
fi

# Full run: --universe pit — the live sleeve universe always comes first; any
# budget it does not need trickle-backfills the point-in-time universe for the
# backtests. The run's own lines start at this offset, so an earlier same-day
# run's summary (the other mode's, or a hand-run's) is never mistaken for this
# one's.
LOG_OFFSET=$(wc -c < "$LOG_FILE" | tr -d ' ')
cd "$ALGO_DIR" || finish 1
# shellcheck source=deploy/launchd/lib/bounded.sh
. "$ALGO_DIR/deploy/launchd/lib/bounded.sh"
# shellcheck disable=SC2086  # FETCH_ARGS is two fixed words, split on purpose
algo_run_bounded "$EARNINGS_TIMEOUT" \
    "$VENV" scripts/fetch_earnings.py $FETCH_ARGS >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

RUN_OUTPUT=$(tail -c "+$((LOG_OFFSET + 1))" "$LOG_FILE" 2>/dev/null)
SUMMARY=$(printf '%s\n' "$RUN_OUTPUT" | grep '^EARNINGS_REFRESH ' | tail -1)
SUMMARY="${SUMMARY:-no summary line (crashed before reporting)}"
LOG_REF="~/ibc/logs/$(basename "$LOG_FILE")"

if [ "$TOP_UP" = "1" ]; then
    case "$EXIT_CODE" in
        0)
            echo "$(ts): earnings top-up OK — $SUMMARY" >> "$LOG_FILE" ;;
        75)
            # The 04:45 run (or a hand-run) still holds the lock: it is doing
            # the work. Not a failure; nothing to page about.
            echo "$(ts): earnings top-up SKIPPED (another refresh holds the lock) — $SUMMARY" >> "$LOG_FILE" ;;
        2)
            echo "$(ts): earnings top-up FAILED — $SUMMARY" >> "$LOG_FILE"
            telegram "⚠️ Earnings top-up FAILED (Alpha Vantage refused or errored). Today's after-market actuals arrive with tomorrow's 04:45 run, a session later than the backtest; fetched_at is unaffected. $SUMMARY. See $LOG_REF." ;;
        124)
            echo "$(ts): earnings top-up TIMED OUT after ${EARNINGS_TIMEOUT}s" >> "$LOG_FILE"
            algo_alert_local "earnings top-up timed out after ${EARNINGS_TIMEOUT}s"
            telegram "⏱️ Earnings top-up TIMED OUT after ${EARNINGS_TIMEOUT}s and was killed (progress saved). See $LOG_REF." ;;
        *)
            echo "$(ts): earnings top-up ERROR (exit $EXIT_CODE) — $SUMMARY" >> "$LOG_FILE"
            telegram "❌ Earnings top-up could not run (exit $EXIT_CODE): $SUMMARY. See $LOG_REF." ;;
    esac
    finish "$EXIT_CODE"
fi

# Standing alerts a CURRENT run can still carry, sent every run until fixed.
NO_DATA=$(printf '%s\n' "$RUN_OUTPUT" | grep '^LIVE_NO_DATA: ' | tail -1)
REFUSED=$(printf '%s\n' "$RUN_OUTPUT" | grep '^LIVE_REFUSED: ' | tail -1)

case "$EXIT_CODE" in
    0)
        echo "$(ts): earnings refresh OK — $SUMMARY" >> "$LOG_FILE"
        # Current, but a live ticker Alpha Vantage has nothing for is a name
        # earnings_drift silently cannot trade. Say so; still a healthy beat.
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
    75)
        echo "$(ts): earnings refresh SKIPPED — another refresh holds the lock" >> "$LOG_FILE"
        telegram "⚠️ Earnings refresh SKIPPED: another refresh (a hand-run?) held the cache lock at 04:45, so fetched_at was not advanced by this run. $SUMMARY. See $LOG_REF. The dead-man check was NOT pinged."
        ;;
    124)
        echo "$(ts): earnings refresh TIMED OUT after ${EARNINGS_TIMEOUT}s" >> "$LOG_FILE"
        algo_alert_local "earnings refresh timed out after ${EARNINGS_TIMEOUT}s"
        telegram "⏱️ Earnings refresh TIMED OUT after ${EARNINGS_TIMEOUT}s and was killed (progress up to the kill is saved; fetched_at NOT advanced). $SUMMARY. See $LOG_REF. The dead-man check was NOT pinged."
        ;;
    *)
        echo "$(ts): earnings refresh ERROR (exit $EXIT_CODE) — $SUMMARY" >> "$LOG_FILE"
        telegram "❌ Earnings refresh could not run (exit $EXIT_CODE): $SUMMARY. See $LOG_REF. The dead-man check was NOT pinged."
        ;;
esac
# A ticker Alpha Vantage keeps refusing is excluded from the freshness gate so
# the sleeve is not held degraded — which makes it silent unless this says so,
# every day, until symbol_overrides maps it.
if [ -n "$REFUSED" ]; then
    telegram "⚠️ Earnings refresh: ${REFUSED#LIVE_REFUSED: }. See $LOG_REF."
fi

finish "$EXIT_CODE"
