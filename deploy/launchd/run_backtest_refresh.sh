#!/bin/bash
# Weekly backtest refresh for algo-poc — Tuesdays 05:00 SGT.
#
# Re-runs the full 10yr backtest so the divergence monitor's baseline stays
# current (it auto-picks the newest output/backtest_multi_*.json; without a
# refresh the live equity dates never overlap the baseline and every
# portfolio reads NO_DATA forever).
#
# Tuesday, not the runbook's original Monday: IBKR's historical-data farm is
# routinely dead through Monday pre-market (observed 2026-07-05/06 — down
# from Saturday night until Monday's 21:30 SGT open). By Tuesday 05:00 SGT
# the US Monday session has closed and the farms are warm.
#
# Exit-code contract:
#   0 = refresh OK
#   1 = IB Gateway unreachable, nothing run
#   2 = point-in-time membership snapshot missing, nothing run (KAN-23)
#   * = the backtest's own exit code

set -uo pipefail

# ALGO_DIR / ALGO_PYTHON / ALGO_MEMBERSHIP_SNAPSHOT are overridable only so
# tests/deploy/test_backtest_refresh_snapshot.py can drive this wrapper
# end-to-end against stubs — launchd starts jobs with an empty environment, so
# production always takes the defaults. Never export any of the three in a
# login shell: a manual run would then backtest whatever tree, interpreter or
# membership file they point at, and the success path prunes $ALGO_DIR/output.
ALGO_DIR="${ALGO_DIR:-/Users/huiliang/GitHub/algo-poc}"
VENV="${ALGO_PYTHON:-$ALGO_DIR/.venv/bin/python}"
# Point-in-time index membership. Without it the backtest ranks only the names
# that survived to today, which is the survivorship bias the rebaseline exists
# to remove (KAN-23).
MEMBERSHIP_SNAPSHOT="${ALGO_MEMBERSHIP_SNAPSHOT:-$ALGO_DIR/data/universe/sp500_membership.json}"
LOG_DIR="$HOME/ibc/logs"
LOG_FILE="$LOG_DIR/backtest_refresh_$(date +%Y%m%d).log"
# Secrets come from the macOS login keychain via the shared loader. Sourced by
# path from the repo (never from the deployed ~/ibc copy) so there is exactly
# one implementation of the lookup and it cannot drift.
ALGO_SECRETS_ENV_FILE="$ALGO_DIR/.env"   # regular-file fallback only
# shellcheck source=deploy/launchd/secrets.sh
. "$ALGO_DIR/deploy/launchd/secrets.sh"
# Shared best-effort Telegram sender (KAN-43), sourced by path for the same
# reason: one copy of the credential-reading logic that cannot drift.
ALGO_JOB_LABEL="backtest refresh"
# shellcheck source=deploy/launchd/lib/telegram.sh
. "$ALGO_DIR/deploy/launchd/lib/telegram.sh"

mkdir -p "$LOG_DIR"
ts() { date '+%Y-%m-%d %H:%M:%S'; }

echo "$(ts): Starting weekly backtest refresh" >> "$LOG_FILE"

# Drift guard: warn loudly if this deployed copy has fallen behind the repo
# canonical. The 2026-08-11 cold-boot auth failure was a stale ~/ibc copy still
# using the pre-T3 default DB password. Warn-only — a legitimately newer
# deployed copy must not block the run. Resync with deploy/launchd/deploy.sh.
CANON="$ALGO_DIR/deploy/launchd/$(basename "$0")"
if [ -f "$CANON" ] && ! cmp -s "$0" "$CANON"; then
    echo "$(date): WARNING - $(basename "$0") differs from repo canonical ($CANON); run deploy/launchd/deploy.sh to resync" >> "$LOG_FILE"
fi

# Checked BEFORE the gateway, and fatal rather than best-effort. The monitor
# auto-selects the newest output/backtest_multi_*.json, so a refresh that ran
# without --universe-snapshots would write a survivorship-biased artifact that
# supersedes the rebaselined one and revert the monitor to exit 3 (BLIND) —
# undoing KAN-23 within a week, silently. Producing nothing is strictly better
# than producing that, and a local misconfiguration must not be masked by
# whatever IB happens to be doing at 05:00. Exit 2 keeps it distinct from the
# gateway's exit 1 in the launchd log.
if [ ! -f "$MEMBERSHIP_SNAPSHOT" ]; then
    echo "$(ts): ERROR - membership snapshot missing at $MEMBERSHIP_SNAPSHOT;" \
         "refusing to write a survivorship-biased baseline" >> "$LOG_FILE"
    algo_alert_local "backtest refresh aborted — membership snapshot missing"
    telegram "🚨 Weekly backtest refresh ABORTED: point-in-time membership snapshot missing at data/universe/sp500_membership.json. Refusing to write a survivorship-biased baseline (it would supersede the rebaselined one and blind the divergence monitor). Regenerate with scripts/ops/build_membership_snapshot.py."
    exit 2
fi

if ! nc -z 127.0.0.1 7497 2>/dev/null; then
    echo "$(ts): ERROR - IB Gateway not reachable on 7497" >> "$LOG_FILE"
    telegram "❌ Weekly backtest refresh SKIPPED: IB Gateway not reachable on 7497."
    exit 1
fi

cd "$ALGO_DIR"
"$VENV" scripts/run_backtest.py --years 10 --capital 100000 \
    --universe-snapshots "$MEMBERSHIP_SNAPSHOT" >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

if [ "$EXIT_CODE" -eq 0 ]; then
    NEWEST=$(ls -t "$ALGO_DIR"/output/backtest_multi_*.json 2>/dev/null | head -1)
    SUMMARY=$(grep -A6 "AGGREGATE" "$LOG_FILE" | grep -E "Total Return|Sharpe|Max Drawdown" | head -3 | tr -s ' ' | tr '\n' ' ')
    echo "$(ts): refresh OK -> $NEWEST" >> "$LOG_FILE"
    telegram "✅ Weekly backtest refreshed: $(basename "${NEWEST:-unknown}") — ${SUMMARY:-see log}. Divergence monitor baseline is now current."
    # Prune baselines older than 90 days (~64MB each); the monitor only uses the newest.
    find "$ALGO_DIR/output" -name "backtest_multi_*.json" -mtime +90 -delete 2>/dev/null
else
    echo "$(ts): refresh FAILED (exit $EXIT_CODE)" >> "$LOG_FILE"
    telegram "❌ Weekly backtest refresh FAILED (exit $EXIT_CODE) — divergence baseline is getting stale. See ~/ibc/logs/$(basename "$LOG_FILE")."
fi

find "$LOG_DIR" -name "backtest_refresh_*.log" -mtime +90 -delete 2>/dev/null
exit $EXIT_CODE
