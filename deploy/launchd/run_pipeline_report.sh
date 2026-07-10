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

set -uo pipefail

# launchd's default PATH lacks /usr/local/bin, where the docker CLI lives.
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

ALGO_DIR="/Users/huiliang/GitHub/algo-poc"
VENV="$ALGO_DIR/.venv/bin/python"
LOG_DIR="$HOME/ibc/logs"
TODAY=$(date +%Y%m%d)
LOG_FILE="$LOG_DIR/pipeline_report_${TODAY}.log"
PAPER_LOG="$LOG_DIR/paper_trading_${TODAY}.log"
ENV_FILE="$ALGO_DIR/.env"

ts() { date "+%Y-%m-%d %H:%M:%S %Z"; }

telegram() {
    [ -f "$ENV_FILE" ] || return 0
    local token chat
    token=$(grep '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2-)
    chat=$(grep '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | head -1 | cut -d= -f2-)
    [ -n "$token" ] && [ -n "$chat" ] || return 0
    curl -s -m 10 "https://api.telegram.org/bot${token}/sendMessage" \
        -d chat_id="$chat" --data-urlencode text="$1" >/dev/null 2>&1 || true
}

mkdir -p "$LOG_DIR"
cd "$ALGO_DIR"

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
BUYS=$(grep -c '  BUY ' "$PAPER_LOG" 2>/dev/null || true)
SELLS=$(grep -c '  SELL' "$PAPER_LOG" 2>/dev/null || true)
SKIPS=$(grep -c '  SKIP' "$PAPER_LOG" 2>/dev/null || true)
DIV=$(grep -oE "Divergence monitor OK|BREACH|hard error" \
      "$LOG_DIR/divergence_${TODAY}.log" 2>/dev/null | tail -1)
RESTING=$(grep -oE "[0-9]+ resting orders" "$LOG_FILE" | tail -1)
SNAP=$(grep -q "$(date +%Y-%m-%d)" "$LOG_FILE" && echo "today's snapshot ✓" \
       || echo "today's snapshot MISSING")

telegram "$RUN_STATUS — B:${BUYS:-0} S:${SELLS:-0} skip:${SKIPS:-0} | divergence: ${DIV:-no log} | ${RESTING:-IB check failed} | $SNAP"

# Prune report logs older than 30 days.
find "$LOG_DIR" -name "pipeline_report_*.log" -mtime +30 -delete 2>/dev/null

exit 0
