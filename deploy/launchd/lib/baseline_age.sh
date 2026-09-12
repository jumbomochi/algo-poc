# Age and usability of the divergence baseline of record — KAN-71, corrected.
#
# WHAT THIS JUDGES, AND WHY IT CHANGED
# ------------------------------------
# The first version of this file picked the newest output/backtest_multi_*.json
# by mtime. On 2026-09-10 the weekly refresh succeeded for the first time in
# three weeks and the check went quiet — "backtest_multi_20260910_235624.json is
# 0d old", no alert — while the baseline the divergence monitor actually grades
# against was still 23 days old, and the artifact that silenced the alert
# carried coverage.state = BLOCKED at 17.39% excluded and could never be pinned.
#
# scripts/ops/baseline_pin.py already said so in its own docstring: the baseline
# of record is a configuration fact, divergence.baseline_pin, not whatever
# output/backtest_multi_*.json happens to sort last. The seam existed and was
# not used. So the pin is what gets judged here, resolved through that same
# script rather than reimplemented.
#
# "Did the weekly refresh run" is a different question and is no longer asked
# here: ALGO_DEADMAN_REFRESH_URL answers it properly and received its first ping
# on 2026-09-10. Two mechanisms answering one question is how one of them stops
# being read.
#
# STATE VERSUS EVENT
# ------------------
# The pin's age is a STATE: reported daily, alerting once it passes the
# monitor's comparison window, past which live and backtest stop overlapping
# meaningfully (the monitor already hints at this with "Only 22 overlapping days
# available (requested 30)").
#
# An unusable refresh is an EVENT: it alerts on the day it is produced, and is
# reported as state thereafter. Alerting every morning that the newest artifact
# is BLOCKED would fire until a data vendor exists for delisted history
# (KAN-59/KAN-60) — which is exactly how an alert gets ignored.
#
# A newer, USABLE artifact sitting unpinned is not an alert at all. Re-pinning
# is a deliberate act (KAN-51) and that is the normal state after any refresh.
#
# Sourced by run_pipeline_report.sh. Sets, and never exits non-zero:
#   ALGO_BASELINE_STATUS   ok | stale | unusable | missing | unresolved
#   ALGO_BASELINE_DETAIL   one line carrying BOTH the pin and the newest
#
# Either number alone misleads: "pin 23d" hides that a refresh ran, and
# "newest 0d" hides that it cannot be used.

# Days before the PIN is called stale. The monitor's comparison window, not the
# weekly cadence — 8 days was the old value and answered the dead-man's
# question, not this one.
ALGO_BASELINE_PIN_MAX_DAYS="${ALGO_BASELINE_PIN_MAX_DAYS:-30}"
# How new an artifact must be for "this refresh produced something unusable" to
# still be news rather than a standing condition.
ALGO_BASELINE_FRESH_DAYS="${ALGO_BASELINE_FRESH_DAYS:-1}"

ALGO_BASELINE_STATUS=""
ALGO_BASELINE_DETAIL=""

# Portable mtime, validated by SHAPE rather than exit code. `stat -f %m` on GNU
# coreutils means "filesystem status": it prints a four-line dump to stdout
# while exiting 1, so a `stat -f %m || stat -c %Y` chain hands back that dump
# with the real mtime appended and every later comparison reads as garbage.
# Guarded because gateway_watchdog.sh defines the same helper.
if ! command -v algo_mtime >/dev/null 2>&1; then
algo_mtime() {
    local m
    m="$(stat -f %m "$1" 2>/dev/null)"                                   # BSD / macOS
    case "$m" in ''|*[!0-9]*) m="$(stat -c %Y "$1" 2>/dev/null)" ;; esac # GNU
    case "$m" in ''|*[!0-9]*) m=0 ;; esac
    printf '%s\n' "$m"
}
fi

_algo_baseline_age_days() {
    local now; now="${ALGO_NOW_EPOCH:-$(date +%s)}"
    local d=$(( (now - $(algo_mtime "$1")) / 86400 ))
    [ "$d" -lt 0 ] && d=0
    printf '%s\n' "$d"
}

# Coverage verdict from an artifact, WITHOUT parsing it.
#
# These files are ~240MB. json.load on one costs seconds and gigabytes in a job
# that runs every morning, and the coverage block sits in the first ~12KB, so a
# bounded read is both cheaper and sufficient. Prints "<state> <excluded_pct>",
# or nothing if either is unreadable — an unreadable artifact must not render as
# a usable one.
_algo_baseline_coverage() {
    [ -f "$1" ] || return 0
    local state pct
    state="$(head -c 262144 "$1" 2>/dev/null \
             | awk '/"coverage"/{f=1} f && /"state"/{gsub(/[^A-Z_]/,""); print; exit}')"
    # Rounded: the raw value is a full float (11.284998021159765) and an alert
    # body is read by a human at 04:52.
    pct="$(head -c 262144 "$1" 2>/dev/null \
           | awk '/"coverage"/{f=1} f && /"excluded_pct"/{gsub(/[^0-9.]/,""); printf "%.2f", $0; exit}')"
    [ -n "$state" ] || return 0
    printf '%s %s\n' "$state" "${pct:-?}"
}

# The pinned baseline, resolved through the one seam that owns that question.
# cd first: the config value is relative and resolve_pin makes it absolute
# against the working directory, which is what makes `output/...` mean the
# deployed tree's output/ rather than wherever the job was standing.
algo_baseline_pin_path() {
    local py="${ALGO_PYTHON:-${ALGO_DIR:-.}/.venv/bin/python}"
    local cfg_arg=()
    [ -n "${ALGO_BASELINE_CONFIG:-}" ] && cfg_arg=(--config "$ALGO_BASELINE_CONFIG")
    (cd "${ALGO_DIR:-.}" 2>/dev/null \
     && "$py" "${ALGO_DIR:-.}/scripts/ops/baseline_pin.py" \
              ${cfg_arg[@]+"${cfg_arg[@]}"} 2>/dev/null)
}

# Newest backtest_multi_* by mtime. Still computed — it answers "a refresh
# produced something", which belongs in the body — but it no longer decides
# anything on its own.
algo_baseline_newest() {
    local dir="${ALGO_BASELINE_DIR:-${ALGO_DIR:-.}/output}"
    local newest="" newest_m=0 f m
    for f in "$dir"/backtest_multi_*.json; do
        [ -f "$f" ] || continue
        m="$(algo_mtime "$f")"
        if [ "$m" -gt "$newest_m" ]; then newest_m="$m"; newest="$f"; fi
    done
    printf '%s\n' "$newest"
}

algo_baseline_age_check() {
    ALGO_BASELINE_STATUS=""
    ALGO_BASELINE_DETAIL=""

    local pin newest newest_part="" ncov nstate npct nage
    pin="$(algo_baseline_pin_path)"

    # Describe the newest artifact first: it is reported in every branch,
    # because "a refresh ran but cannot be used" is invisible otherwise.
    newest="$(algo_baseline_newest)"
    if [ -n "$newest" ]; then
        nage="$(_algo_baseline_age_days "$newest")"
        ncov="$(_algo_baseline_coverage "$newest")"
        nstate="${ncov%% *}"; npct="${ncov##* }"
        if [ -n "$nstate" ] && [ "$nstate" != "OK" ]; then
            newest_part="; newest is $(basename "$newest") (${nage}d, coverage ${nstate} at ${npct}% excluded — cannot be pinned)"
        else
            newest_part="; newest is $(basename "$newest") (${nage}d, coverage ${nstate:-unknown})"
        fi
    else
        newest_part="; no backtest_multi_* artifact in ${ALGO_BASELINE_DIR:-${ALGO_DIR:-.}/output}"
    fi

    if [ -z "$pin" ]; then
        ALGO_BASELINE_STATUS="unresolved"
        ALGO_BASELINE_DETAIL="NO divergence.baseline_pin resolved — the monitor has nothing to grade against and will exit 3 (BLIND)${newest_part}"
        return 0
    fi

    if [ ! -f "$pin" ]; then
        ALGO_BASELINE_STATUS="missing"
        ALGO_BASELINE_DETAIL="pinned baseline $(basename "$pin") is MISSING from disk — the monitor will exit 3 (BLIND) on its next run${newest_part}"
        return 0
    fi

    local page pcov pstate ppct
    page="$(_algo_baseline_age_days "$pin")"
    pcov="$(_algo_baseline_coverage "$pin")"
    pstate="${pcov%% *}"; ppct="${pcov##* }"

    local pin_part="pin is $(basename "$pin") (${page}d old, coverage ${pstate:-unknown}"
    [ -n "$pstate" ] && [ "$pstate" != "OK" ] && pin_part="$pin_part at ${ppct}% excluded"
    pin_part="$pin_part)"

    if [ "$page" -ge "$ALGO_BASELINE_PIN_MAX_DAYS" ]; then
        ALGO_BASELINE_STATUS="stale"
        ALGO_BASELINE_DETAIL="${pin_part} — past the ${ALGO_BASELINE_PIN_MAX_DAYS}d comparison window, so live and backtest no longer overlap meaningfully${newest_part}"
        return 0
    fi

    # The event: a refresh produced something that cannot become the baseline.
    # Only while it is still news — see the header on state versus event.
    if [ -n "$newest" ] && [ -n "$nstate" ] && [ "$nstate" != "OK" ] \
       && [ "${nage:-999}" -le "$ALGO_BASELINE_FRESH_DAYS" ]; then
        ALGO_BASELINE_STATUS="unusable"
        ALGO_BASELINE_DETAIL="${pin_part}${newest_part}"
        return 0
    fi

    ALGO_BASELINE_STATUS="ok"
    ALGO_BASELINE_DETAIL="${pin_part}${newest_part}"
    return 0
}

# The message to escalate, or empty when there is nothing to page about. Kept
# separate from the check so the caller's alerting stays a two-line if, matching
# the launchd-wiring and branch-guard sections.
algo_baseline_alert_body() {
    case "$ALGO_BASELINE_STATUS" in
        stale)      printf '🚨 divergence baseline PIN STALE — %s\n' "$ALGO_BASELINE_DETAIL" ;;
        missing)    printf '🚨 divergence baseline PIN MISSING — %s\n' "$ALGO_BASELINE_DETAIL" ;;
        unresolved) printf '🚨 divergence baseline UNPINNED — %s\n' "$ALGO_BASELINE_DETAIL" ;;
        unusable)   printf '🚨 the weekly refresh produced an UNUSABLE baseline — %s\n' "$ALGO_BASELINE_DETAIL" ;;
        *)          : ;;
    esac
}
