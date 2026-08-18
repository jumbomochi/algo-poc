#!/bin/bash
# Daily pipeline report — 04:52 SGT Tue-Sat, after the 04:15 paper run and the
# 04:45 divergence monitor. Collects the whole pipeline's state (paper run,
# risk-gate activity, divergence, execution service, resting IB orders, equity
# snapshot continuity) into one log and sends a compact Telegram summary.
#
# The daily message doubles as a liveness heartbeat: on 2026-07-07 a single
# missed alert cost two paper-record days, so silence from this job IS a
# signal. Replaces the ad-hoc scratchpad watcher (watch_sat_run.sh) that did
# not survive reboots.
#
# NO DEAD-MAN: this job is already one (KAN-56 coverage review). Its entire
# output is a message that arrives every morning, so a run that never happens
# is visible as a missing report rather than as a quiet success — the failure
# mode an external check exists to expose is the only mode this job has. The
# jobs it reports ON are separately covered: run_paper.sh, run_divergence.sh,
# run_backtest_refresh.sh and run_db_backup.sh each ping their own check, so a
# reader who misses the absence of this report is still paged by theirs. Adding
# a fifth external check here would page for the same outage twice and buy
# nothing. If this ever stops sending unconditionally — a digest that is
# skipped on quiet days, say — that reasoning expires and it needs a switch.

set -uo pipefail

# launchd's default PATH lacks /usr/local/bin, where the docker CLI lives.
# $ALGO_PATH_PREFIX exists only so the test can put its `docker`/`curl` stubs
# ahead of the real ones — this assignment REPLACES the inherited PATH, so
# prepending to $PATH from outside would otherwise have no effect. Unset in
# production, where launchd supplies an empty environment.
export PATH="${ALGO_PATH_PREFIX:+$ALGO_PATH_PREFIX:}/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# ALGO_DIR / ALGO_PYTHON / ALGO_DATABASE_URL are overridable only so
# tests/deploy/test_pipeline_report.py can drive this wrapper end-to-end
# against stubs — launchd starts jobs with an empty environment, so production
# always takes the defaults. Never export any of the three in a login shell: a
# manual run would then use whatever tree, interpreter or database they point
# at.
ALGO_DIR="${ALGO_DIR:-/Users/huiliang/GitHub/algo-poc}"
VENV="${ALGO_PYTHON:-$ALGO_DIR/.venv/bin/python}"
LOG_DIR="$HOME/ibc/logs"
TODAY=$(date +%Y%m%d)
# Lower bound for "this run's" fills and rejections. Local midnight, stamped
# with its offset so the summariser does no timezone guessing: at 04:52 SGT it
# brackets the 04:15 paper run and excludes yesterday's. A UTC calendar date
# would be wrong here — 04:52 SGT is 20:52 UTC the *previous* day, so the
# headline would read zero fills on a day that traded.
SINCE=$(date +%Y-%m-%dT00:00:00%z)
LOG_FILE="$LOG_DIR/pipeline_report_${TODAY}.log"
PAPER_LOG="$LOG_DIR/paper_trading_${TODAY}.log"
# Secrets come from the macOS login keychain via the shared loader. Sourced by
# path from the repo (never from the deployed ~/ibc copy) so there is exactly
# one implementation of the lookup and it cannot drift.
ALGO_SECRETS_ENV_FILE="$ALGO_DIR/.env"   # regular-file fallback only
# shellcheck source=deploy/launchd/secrets.sh
. "$ALGO_DIR/deploy/launchd/secrets.sh"
# Shared best-effort Telegram sender (KAN-43), sourced by path for the same
# reason: one copy of the credential-reading logic that cannot drift.
ALGO_JOB_LABEL="pipeline report"
# shellcheck source=deploy/launchd/lib/telegram.sh
. "$ALGO_DIR/deploy/launchd/lib/telegram.sh"

ts() { date "+%Y-%m-%d %H:%M:%S %Z"; }

mkdir -p "$LOG_DIR"
cd "$ALGO_DIR"

# Drift guard: warn loudly if this deployed copy has fallen behind the repo
# canonical. The 2026-08-11 cold-boot auth failure was a stale ~/ibc copy still
# using the pre-T3 default DB password. Warn-only — a legitimately newer
# deployed copy must not block the run. Resync with deploy/launchd/deploy.sh.
CANON="$ALGO_DIR/deploy/launchd/$(basename "$0")"
if [ -f "$CANON" ] && ! cmp -s "$0" "$CANON"; then
    echo "$(date): WARNING - $(basename "$0") differs from repo canonical ($CANON); run deploy/launchd/deploy.sh to resync" >> "$LOG_FILE"
fi

# `docker compose` interpolates ${POSTGRES_PASSWORD:?} / ${REDIS_PASSWORD:?} in
# docker-compose.yml by reading `.env` ITSELF — it never goes through the loader
# above. So on 2026-08-13/14, with .env a 1Password FIFO nothing was serving,
# every compose call in this report died with:
#   "required variable POSTGRES_PASSWORD is missing a value"
# Exporting them first fixes it for good: compose resolves ${VAR} from the
# process environment in preference to `.env`, so the file is never consulted.
# Non-fatal — the report still has value without its compose-backed sections.
if ! algo_load_secrets POSTGRES_PASSWORD REDIS_PASSWORD; then
    echo "$(ts): WARNING - docker compose sections will fail: $ALGO_SECRETS_ERROR" >> "$LOG_FILE"
fi

# The paper DB is the dockerized postgres on a machine-local port (see
# docker-compose.override.yml); config/default.yaml's localhost:5432 default
# points at nothing on this machine. Same DSN run_divergence.sh builds.
export ALGO_DATABASE_URL="${ALGO_DATABASE_URL:-postgresql://algo:${POSTGRES_PASSWORD:-}@localhost:55432/algo_poc}"

{
    echo "$(ts): ===== daily pipeline report ====="

    echo; echo "===== paper run tail ====="
    tail -8 "$PAPER_LOG" 2>/dev/null || echo "MISSING"

    echo; echo "===== gate activity ====="
    # NB: grep -c prints 0 itself on no-match (exiting 1), so the fallback is
    # for a missing file only — `|| echo 0` here would double-print.
    BUYS=$(grep -c '  BUY ' "$PAPER_LOG" 2>/dev/null || true)
    SELLS=$(grep -c '  SELL' "$PAPER_LOG" 2>/dev/null || true)
    SKIPS=$(grep -c '  SKIP' "$PAPER_LOG" 2>/dev/null || true)
    echo "BUYs: ${BUYS:-0}  SELLs: ${SELLS:-0}  SKIPs: ${SKIPS:-0}"

    echo; echo "===== divergence tail ====="
    tail -4 "$LOG_DIR/divergence_${TODAY}.log" 2>/dev/null || echo "MISSING"

    echo; echo "===== execution service: last 2h ====="
    docker compose logs execution --since 2h 2>&1 \
        | grep -iE "skipped|rounded|submitted|error|Cancelled|Fill" | tail -12

    echo; echo "===== resting orders at IB ====="
    "$VENV" - <<'PYEOF'
import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())
from ib_insync import IB
ib = IB()
try:
    ib.connect("127.0.0.1", 7497, clientId=54, timeout=15)
    ib.reqAllOpenOrders(); ib.sleep(2)
    trades = ib.openTrades()
    print(f"{len(trades)} resting orders")
    for t in trades[:12]:
        print(" ", t.order.orderId, t.contract.symbol, t.order.action,
              t.order.totalQuantity, "@", getattr(t.order, "lmtPrice", "MKT"),
              t.orderStatus.status)
    ib.disconnect()
except Exception as e:
    print("IB check failed:", e)
PYEOF

    echo; echo "===== equity snapshots (record continuity) ====="
    docker compose exec -T postgres psql -U algo -d algo_poc -t -c \
      "SELECT date, COUNT(*), ROUND(SUM(equity)::numeric,2) FROM equity_snapshots WHERE portfolio NOT LIKE '\_%' GROUP BY date ORDER BY date DESC LIMIT 7;" 2>&1
} >> "$LOG_FILE" 2>&1

# Compact Telegram summary from the values just gathered.
if grep -q "exit code: 0" "$PAPER_LOG" 2>/dev/null; then
    RUN_STATUS="✅ paper run OK"
elif [ -f "$PAPER_LOG" ]; then
    RUN_STATUS="❌ paper run FAILED"
else
    RUN_STATUS="❌ paper run MISSING"
fi
# Halt state, fills and rejections come from the ledger tables, not from log
# text. A grep count can report a healthy number of orders while the pipeline
# never received one (KAN-31), and a halt leaves no line in the paper log at
# all — so the operator could be halted and never told. The BUY/SELL/SKIP
# greps stay in the log body above for diagnosis; they are just no longer the
# headline number.
#
# A read failure degrades to a named marker, never to a reassuring
# "halt: clear" we cannot substantiate: absence of evidence is not evidence
# that nothing is halted.
SUMMARY=$("$VENV" "$ALGO_DIR/scripts/ops/pipeline_report_summary.py" \
              --since "$SINCE" --mode "${ALGO_MODE:-paper}" \
              2>>"$LOG_FILE") || SUMMARY=""
[ -n "$SUMMARY" ] || SUMMARY="⚠️ halt/fills/rejections UNKNOWN (DB read failed)"
DIV=$(grep -oE "Divergence monitor OK|BREACH|hard error" \
      "$LOG_DIR/divergence_${TODAY}.log" 2>/dev/null | tail -1)
RESTING=$(grep -oE "[0-9]+ resting orders" "$LOG_FILE" | tail -1)
SNAP=$(grep -q "$(date +%Y-%m-%d)" "$LOG_FILE" && echo "today's snapshot ✓" \
       || echo "today's snapshot MISSING")

telegram "$SUMMARY | $RUN_STATUS | divergence: ${DIV:-no log} | ${RESTING:-IB check failed} | $SNAP"

# Prune report logs older than 30 days.
find "$LOG_DIR" -name "pipeline_report_*.log" -mtime +30 -delete 2>/dev/null

exit 0
