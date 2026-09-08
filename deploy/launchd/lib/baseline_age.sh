# Age of the divergence baseline artifact — KAN-71.
#
# WHY THIS IS NOT PART OF THE REFRESH JOB
# ---------------------------------------
# run_backtest_refresh.sh already says "divergence baseline is getting stale"
# when it fails. That line is emitted BY THE FAILING RUN, which makes it
# structurally unable to fire in the case that matters most: a run that never
# starts. 2026-08-11 is on the record — the host booted after the 05:00 slot,
# launchd does not re-fire a missed StartCalendarInterval, and nothing said a
# word. The dead-man switch is the mechanism designed for exactly that, and it
# cannot help either: ALGO_DEADMAN_REFRESH_URL only pings on a SUCCESSFUL
# refresh, so it cannot arm itself until the job it watches is already healthy,
# and a healthchecks.io check that has never been pinged never alerts (KAN-65).
#
# The result was a baseline that aged from 2026-08-25 to 2026-09-08 — fourteen
# days — with no page, and no line in the daily report an operator reads.
#
# So this check is deliberately independent of the weekly job: it looks at the
# artifact on disk, every day, and does not care why it is old.
#
# Sourced by run_pipeline_report.sh. Sets, and never exits non-zero:
#   ALGO_BASELINE_FILE       basename of the newest artifact, or ""
#   ALGO_BASELINE_AGE_DAYS   whole days since its mtime, or ""
#   ALGO_BASELINE_STATUS     fresh | stale | absent
#   ALGO_BASELINE_DETAIL     one line for the report body

# Days before the baseline is called stale. Weekly job, so one missed Tuesday
# plus a day of slack — the same figure as the dead-man period, deliberately.
ALGO_BASELINE_STALE_DAYS="${ALGO_BASELINE_STALE_DAYS:-8}"

ALGO_BASELINE_FILE=""
ALGO_BASELINE_AGE_DAYS=""
ALGO_BASELINE_STATUS=""
ALGO_BASELINE_DETAIL=""

# Portable mtime, validated by SHAPE rather than by exit code. `stat -f %m` on
# GNU coreutils means "filesystem status": it prints a four-line filesystem dump
# to stdout while exiting 1, so a plain `stat -f %m || stat -c %Y` chain hands
# back that dump with the real mtime appended and every later comparison reads
# as garbage. Guarded because gateway_watchdog.sh defines the same helper and a
# script may source both; identical semantics either way.
if ! command -v algo_mtime >/dev/null 2>&1; then
algo_mtime() {
    local m
    m="$(stat -f %m "$1" 2>/dev/null)"                                   # BSD / macOS
    case "$m" in ''|*[!0-9]*) m="$(stat -c %Y "$1" 2>/dev/null)" ;; esac # GNU
    case "$m" in ''|*[!0-9]*) m=0 ;; esac
    printf '%s\n' "$m"
}
fi

# Newest backtest_multi_*.json in the output directory, by mtime.
#
# By mtime and not by the date in the filename: the two disagree whenever a
# refresh is re-run by hand, and the question here is "when was a baseline last
# produced", which is a fact about the file rather than about its name.
algo_baseline_newest() {
    local dir="${ALGO_BASELINE_DIR:-${ALGO_DIR:-.}/output}"
    local newest="" newest_m=0 f m
    for f in "$dir"/backtest_multi_*.json; do
        [ -f "$f" ] || continue
        m="$(algo_mtime "$f")"
        if [ "$m" -gt "$newest_m" ]; then
            newest_m="$m"
            newest="$f"
        fi
    done
    printf '%s\n' "$newest"
}

algo_baseline_age_check() {
    ALGO_BASELINE_FILE=""
    ALGO_BASELINE_AGE_DAYS=""
    ALGO_BASELINE_STATUS=""
    ALGO_BASELINE_DETAIL=""

    local newest
    newest="$(algo_baseline_newest)"

    # No artifact at all is reported distinctly from a stale one and alerts just
    # as loudly. Absence of evidence must never render as freshness — that is
    # the whole failure mode this check exists to break.
    if [ -z "$newest" ]; then
        ALGO_BASELINE_STATUS="absent"
        ALGO_BASELINE_DETAIL="NO baseline artifact in ${ALGO_BASELINE_DIR:-${ALGO_DIR:-.}/output} — the divergence monitor has nothing to score against"
        return 0
    fi

    local now age_days
    now="${ALGO_NOW_EPOCH:-$(date +%s)}"
    age_days=$(( (now - $(algo_mtime "$newest")) / 86400 ))
    [ "$age_days" -lt 0 ] && age_days=0

    ALGO_BASELINE_FILE="$(basename "$newest")"
    ALGO_BASELINE_AGE_DAYS="$age_days"
    if [ "$age_days" -ge "$ALGO_BASELINE_STALE_DAYS" ]; then
        ALGO_BASELINE_STATUS="stale"
        ALGO_BASELINE_DETAIL="$ALGO_BASELINE_FILE is ${age_days}d old (stale at ${ALGO_BASELINE_STALE_DAYS}d) — the weekly refresh has not produced one since"
    else
        ALGO_BASELINE_STATUS="fresh"
        ALGO_BASELINE_DETAIL="$ALGO_BASELINE_FILE is ${age_days}d old"
    fi
    return 0
}

# The message to escalate, or empty when there is nothing to say. Kept separate
# from the check so the caller's alerting stays a two-line if — the same shape
# the launchd-wiring reconciliation uses, for the same reason: a failure mode
# whose nature is silence cannot be reported into a file nobody opens (KAN-64).
algo_baseline_alert_body() {
    case "$ALGO_BASELINE_STATUS" in
        stale)  printf '🚨 divergence baseline STALE — %s\n' "$ALGO_BASELINE_DETAIL" ;;
        absent) printf '🚨 divergence baseline MISSING — %s\n' "$ALGO_BASELINE_DETAIL" ;;
        *)      : ;;
    esac
}
