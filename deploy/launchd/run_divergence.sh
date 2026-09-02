#!/bin/bash
# Daily divergence monitor for algo-poc
# Runs at 4:45 AM SGT, ~30 min after run_paper.sh (4:15) has written the day's
# equity_snapshots row AND the rolling shadow series. Compares live paper equity
# to that shadow: the model replayed over the bars live actually saw, whose last
# session is today.
#
# It used to compare against a PINNED 10-year backtest artifact. That artifact
# cannot score sessions past its own last bar, so the comparison window froze —
# six consecutive runs in August 2026 all reported window_end=2026-08-14 and
# rewrote the same evidence row, while a coverage figure measured over 2016-2020
# forced every verdict to NO_DATA. The pin still exists and is still the
# baseline of record for edge evidence (run_sleeve_evaluation.py); it is simply
# no longer the daily operational feed.
#
# Exit-code contract (from scripts/divergence_monitor.py):
#   0 = all portfolios OK or WARNING       -> no action
#   1 = at least one portfolio BREACH      -> alert
#   2 = hard error (DB/backtest/args)      -> page
#   3 = no shadow series to grade against  -> alert; the monitor is BLIND
#       (the 04:15 paper run did not produce one). The fault is upstream in
#       the paper run. Historically this also covered a non-comparable pinned
#       baseline; that path is gone with the pin. Regenerate the baseline
#        per docs/operations/backtest-baseline.md. Do NOT read this as OK.
#   4 = baseline artifact is STALE (KAN-56) -> alert; the verdicts are real
#       but were scored against expectations the weekly refresh stopped
#       updating. Distinct from 3: the monitor is not blind, it is comparing
#       against old numbers. Between 2026-07-28 and 2026-08-18 that was true
#       for three weeks and nothing said so.
# NOTE: deliberately NOT using `set -e` around the python call, because exit
# codes 1 and 2 are meaningful signals we branch on, not failures to abort on.
#
# THE FEED IS NAMED, NEVER DISCOVERED (KAN-51, then the shadow migration)
# -----------------------------------------------------------------------
# The monitor is invoked with an explicit --shadow. Two earlier shapes failed,
# and the invariant that survives both is that this script NAMES the feed:
#
#   1. Neither flag. Every run took the recency path
#      (find_latest_backtest_json), so the artifact the gate evidence was
#      measured against was silently replaced by the Tuesday refresh. Recency
#      selection is not a pin: it makes the baseline a filesystem accident.
#   2. --backtest <pin> --pinned. A pinned 10-year artifact cannot score
#      sessions past its own last bar, so the window froze: six consecutive
#      runs in August 2026 all reported window_end=2026-08-14 and rewrote the
#      same evidence row, while a coverage figure measured over 2016-2020
#      forced every verdict to NO_DATA.
#
# The shadow is written by the 04:15 paper run: each sleeve replayed over the
# bars live actually saw, seeded at live's NAV, so its last session is today.
# The path is dated and absolute rather than discovered — "newest shadow in
# output/" would let a stale curve from a failed morning grade today's book.
#
# If it is absent the path is passed through DELIBERATELY and the monitor exits
# 3 and this script alerts, which keeps one authority on whether the run can
# mean anything. Skipping the run here, or quietly dropping the flag, is the
# same silent fallback in different clothes — and a job that ran and meant
# nothing is the 2026-08-13 failure pattern this whole cluster of stories
# exists to remove.
#
# The pin is NOT gone: divergence.baseline_pin remains the baseline of record
# for edge evidence (scripts/run_sleeve_evaluation.py) and D18's accepted
# coverage bias still rests on it. It is simply no longer the daily feed.
#
# DEAD-MAN SWITCH (KAN-56)
# ------------------------
# Every alert this script sends requires this script to be running, and the
# 04:45 job is the one that has actually gone missing: 2026-08-13 and 08-14
# produced no run at all and 08-13 is a permanent hole in the gate evidence.
# So a run that reaches a VERDICT pings $ALGO_DEADMAN_DIVERGENCE_URL and an
# external checker pages when the pings stop. A verdict — not a clean bill of
# health — is the healthy beat here: exits 1, 3 and 4 all mean the monitor ran,
# judged, and reported through its own Telegram, and withholding the ping for
# them would saturate the external check for the whole duration of a real drift
# episode, which is precisely when telling "did not run" from "ran and found
# something" matters most. Only exit 2 (nothing could be judged) stays silent.
# Configure the external check with a period of ~26h. See
# docs/operations/dead-man-switches.md.

set -uo pipefail

# ALGO_DIR / ALGO_PYTHON / ALGO_DIVERGENCE_REPORT are overridable only so
# tests/deploy/test_divergence_alerting.py can drive this wrapper end-to-end
# against stubs — launchd starts jobs with an empty environment, so production
# always takes the defaults. Never export any of the three in a login shell: a
# manual run would then use whatever tree, interpreter or report path they
# point at. ALGO_PYTHON swaps the interpreter for the monitor too, not just for
# the alert renderer.
ALGO_DIR="${ALGO_DIR:-/Users/huiliang/GitHub/algo-poc}"
VENV="${ALGO_PYTHON:-$ALGO_DIR/.venv/bin/python}"
LOG_DIR="$HOME/ibc/logs"
METRICS_DIR="$HOME/ibc/metrics"
LOG_FILE="$LOG_DIR/divergence_$(date +%Y%m%d).log"
PROM_FILE="$METRICS_DIR/divergence.prom"
# Passed to the monitor explicitly (it would default to the same path) so the
# alert renderer knows exactly which report to read back.
REPORT_FILE="${ALGO_DIVERGENCE_REPORT:-$ALGO_DIR/output/divergence_$(date +%Y%m%d).json}"
# Stamped just before the monitor runs, so the renderer can tell this run's
# artifacts from an earlier same-day run's. Initialised here because `set -u`
# is on and the early-abort paths below exit before they are stamped.
RUN_STARTED=0
LOG_OFFSET=0
# The baseline of record. Resolved below, after the cd, so a relative config pin
# means this checkout's output/ rather than wherever launchd started us.
# Secrets come from the macOS login keychain via the shared loader. Sourced by
# path from the repo (never from the deployed ~/ibc copy) so there is exactly
# one implementation of the lookup and it cannot drift.
ALGO_SECRETS_ENV_FILE="$ALGO_DIR/.env"   # regular-file fallback only
# shellcheck source=deploy/launchd/secrets.sh
. "$ALGO_DIR/deploy/launchd/secrets.sh"
# Shared best-effort Telegram sender (KAN-43), sourced by path for the same
# reason: one copy of the credential-reading logic that cannot drift.
ALGO_JOB_LABEL="divergence monitor"
# shellcheck source=deploy/launchd/lib/telegram.sh
. "$ALGO_DIR/deploy/launchd/lib/telegram.sh"
# Dead-man ping helper (KAN-15), likewise sourced by path and never deployed.
# shellcheck source=deploy/launchd/deadman.sh
. "$ALGO_DIR/deploy/launchd/deadman.sh"
# Docker engine liveness (KAN-66), so a failed port wait can say whether the
# daemon is gone or only this container. Alert-only: the lib never restarts
# anything.
# shellcheck source=deploy/launchd/lib/docker_health.sh
. "$ALGO_DIR/deploy/launchd/lib/docker_health.sh"

# Render the alert body from the monitor's own JSON report, so the message
# names the breaching sleeves / the unmet baseline requirement rather than just
# saying "something happened". A rendering failure must degrade to the generic
# text, never to silence — being told nothing is the failure mode this job
# exists to remove. $1 = exit code, $2 = fallback text.
divergence_alert_text() {
    local text
    text=$("$VENV" "$ALGO_DIR/scripts/ops/divergence_alert.py" \
               --exit-code "$1" --report "$REPORT_FILE" --log "$LOG_FILE" \
               --not-before "$RUN_STARTED" --log-offset "$LOG_OFFSET" \
               2>>"$LOG_FILE") || text=""
    [ -n "$text" ] || text="$2"
    printf '%s' "$text"
}

mkdir -p "$LOG_DIR" "$METRICS_DIR"

echo "$(date): Starting daily divergence monitor" >> "$LOG_FILE"

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

# Redis is NOT required to run the monitor: it carries only the alert the
# monitor raises if it cannot write its verdicts to the evidence store (KAN-27).
# That failure leaves the exit code untouched by design, so this is the sole
# operator-visible signal for it — but making the credential mandatory would
# let an alert-path dependency abort drift detection, which is strictly worse
# than the outage it reports. Warn and carry on.
if algo_load_secrets REDIS_PASSWORD; then
    export ALGO_REDIS_URL="redis://:${REDIS_PASSWORD}@localhost:56379/0"
else
    echo "$(date): WARNING - $ALGO_SECRETS_ERROR; an evidence-store write failure cannot be alerted" >> "$LOG_FILE"
fi

# Wait up to 5 min for the dockerized paper DB before hard-erroring (exit 2).
if ! wait_for_port 127.0.0.1 55432 "paper DB (docker compose up?)" 300; then
    # Name the daemon, not just the port (KAN-66) — this is the exact abort the
    # dead docker engine produced on 2026-08-21, and it named postgres.
    DOCKER_HINT="$(algo_docker_wait_hint)"
    echo "$(date): ERROR - $DOCKER_HINT" >> "$LOG_FILE"
    algo_alert_local "divergence monitor aborted — paper DB never came up on 55432: $DOCKER_HINT"
    telegram "🚨 Divergence monitor ABORTED: paper DB not reachable on 55432 after 5 min — $DOCKER_HINT."
    exit 2
fi

cd "$ALGO_DIR"

# The rolling shadow the 04:15 run wrote. Named explicitly rather than
# discovered: the monitor must never fall back to "whatever shadow is newest in
# output/", which would let a stale curve from a failed morning grade today's
# book. A shadow that is absent is the blind signal, and the monitor exits 3
# for it — it is not this script's job to decide that.
SHADOW_FILE="${ALGO_DIVERGENCE_SHADOW:-$ALGO_DIR/output/shadow_$(date +%Y%m%d).json}"
if [ -f "$SHADOW_FILE" ]; then
    echo "$(date): shadow series: $SHADOW_FILE" >> "$LOG_FILE"
else
    echo "$(date): WARNING - no shadow series at $SHADOW_FILE;" \
         "the 04:15 paper run did not produce one, so the monitor will exit 3" \
         "(blind). The fault is in the paper run, not in divergence." \
         >> "$LOG_FILE"
fi

# Everything the renderer reads back must be attributable to THIS run: a report
# left by an earlier run today would otherwise be narrated as this one's, and
# exit 1 does not prove a breach (the monitor ends in `sys.exit(main())`, so an
# uncaught exception exits 1 too).
RUN_STARTED=$(date +%s)
LOG_OFFSET=$(wc -c < "$LOG_FILE" 2>/dev/null | tr -d ' ')
[ -n "$LOG_OFFSET" ] || LOG_OFFSET=0

"$VENV" scripts/divergence_monitor.py \
    --shadow "$SHADOW_FILE" \
    --output "$REPORT_FILE" \
    --prometheus-textfile "$PROM_FILE" \
    >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

case "$EXIT_CODE" in
    0)
        echo "$(date): Divergence monitor OK (exit 0)" >> "$LOG_FILE"
        ;;
    1)
        echo "$(date): ALERT - divergence BREACH (exit 1)" >> "$LOG_FILE"
        telegram "$(divergence_alert_text 1 "🚨 Divergence BREACH ($(date +%F)) — live paper equity has diverged from the backtest baseline. See $LOG_FILE")"
        ;;
    2)
        echo "$(date): PAGE - divergence monitor hard error (exit 2)" >> "$LOG_FILE"
        algo_alert_local "divergence monitor hard error (exit 2) — see $LOG_FILE"
        telegram "$(divergence_alert_text 2 "🚨 Divergence monitor HARD ERROR (exit 2) on $(date +%F). See $LOG_FILE")"
        ;;
    3)
        echo "$(date): ALERT - divergence monitor BLIND: baseline backtest is not" \
             "comparable to live (exit 3). No drift detection is running until the" \
             "baseline is regenerated - see docs/operations/backtest-baseline.md" \
             >> "$LOG_FILE"
        # A blind monitor is an outage, not a pass.
        telegram "$(divergence_alert_text 3 "⚠️ Divergence monitor is BLIND (exit 3): the baseline backtest is not comparable to live, so every report is forced to NO_DATA. No drift detection is running. Regenerate per docs/operations/backtest-baseline.md")"
        ;;
    4)
        echo "$(date): ALERT - divergence baseline is STALE (exit 4): the verdicts" \
             "above are real but were scored against a baseline the weekly refresh" \
             "stopped updating. Check ~/ibc/logs/backtest_refresh_*.log" >> "$LOG_FILE"
        telegram "$(divergence_alert_text 4 "⚠️ Divergence baseline is STALE (exit 4): the weekly backtest refresh has not produced a newer baseline, so drift is being measured against old expectations. See ~/ibc/logs/backtest_refresh_*.log")"
        ;;
    *)
        echo "$(date): UNEXPECTED exit code $EXIT_CODE from divergence monitor" >> "$LOG_FILE"
        algo_alert_local "divergence monitor returned unexpected exit $EXIT_CODE"
        telegram "🚨 Divergence monitor returned UNEXPECTED exit code $EXIT_CODE. See $LOG_FILE"
        ;;
esac

# A verdict is the healthy beat, not a clean one — see the header. Mapped here
# rather than branched on inside the case above so the rule is stated once and
# a future exit code has to be classified deliberately.
case "$EXIT_CODE" in
    0|1|3|4) DEADMAN_EXIT=0 ;;
    *)       DEADMAN_EXIT="$EXIT_CODE" ;;
esac
algo_deadman_ping "$DEADMAN_EXIT" ALGO_DEADMAN_DIVERGENCE_URL
echo "$(date): dead-man switch: $ALGO_DEADMAN_STATUS" >> "$LOG_FILE"

# Clean up logs older than 30 days
find "$LOG_DIR" -name "divergence_*.log" -mtime +30 -delete 2>/dev/null

exit $EXIT_CODE
