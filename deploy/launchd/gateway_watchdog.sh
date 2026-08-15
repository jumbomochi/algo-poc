#!/bin/bash
# IB Gateway watchdog for algo-poc — hardened version.
#
# Checks the API port; if it's been down for TWO consecutive runs, kickstarts
# the IBC gateway launchd job. Two-strike logic rides over the legitimate
# ~1-min nightly auto-restart (23:55) and weekly cold-restart (Sun 08:00).
#
# HARDENING (2026-07-05): before any kickstart, the newest IBC gateway log is
# checked for authentication failures. If the Gateway is being REJECTED
# ("Unrecognized Username or Password" / "Too many failed login attempts"),
# restarting would loop failed logins into an IB rate-limit or lockout — the
# 2026-07-01 incident (30 rejected logins). In that state the watchdog
# refuses to act, sends ONE Telegram alert, and waits for a human.
#
# Wire via launchd with StartInterval (300s). See deploy/launchd/.

set -uo pipefail

PORT=7497                      # paper API port (7496 for live)
GW_LABEL="local.ibc-gateway"
LOG_DIR="$HOME/ibc/logs"
LOG_FILE="$LOG_DIR/gateway_watchdog_$(date +%Y%m%d).log"
MARKER="$HOME/ibc/.gateway_down_marker"
AUTH_MARKER="$HOME/ibc/.gateway_auth_failure_alerted"
# Written by the execution service on IB Error 1100 (server-connectivity lost);
# the API port stays OPEN during a 1100, so the port check above is blind to it.
CONN_MARKER="$HOME/ibc/state/gateway_connectivity_lost"
CONN_ALERT_MARKER="$HOME/ibc/.gateway_connectivity_alerted"
CONN_SUSTAINED_SECS=180        # ignore a 1100 that self-heals within one cycle
ALGO_DIR="/Users/huiliang/GitHub/algo-poc"
# Secrets come from the macOS login keychain via the shared loader. Sourced by
# path from the repo (never from the deployed ~/ibc copy) so there is exactly
# one implementation of the lookup and it cannot drift.
ALGO_SECRETS_ENV_FILE="$ALGO_DIR/.env"   # regular-file fallback only
# shellcheck source=deploy/launchd/secrets.sh
. "$ALGO_DIR/deploy/launchd/secrets.sh"
# Shared best-effort Telegram sender (KAN-43), sourced by path for the same
# reason: one copy of the credential-reading logic that cannot drift.
ALGO_JOB_LABEL="watchdog"
# shellcheck source=deploy/launchd/lib/telegram.sh
. "$ALGO_DIR/deploy/launchd/lib/telegram.sh"

mkdir -p "$LOG_DIR"
ts() { date '+%Y-%m-%d %H:%M:%S'; }

# Drift guard: warn loudly if this deployed copy has fallen behind the repo
# canonical. The 2026-08-11 cold-boot auth failure was a stale ~/ibc copy still
# using the pre-T3 default DB password. Warn-only — a legitimately newer
# deployed copy must not block the run. Resync with deploy/launchd/deploy.sh.
CANON="$ALGO_DIR/deploy/launchd/$(basename "$0")"
if [ -f "$CANON" ] && ! cmp -s "$0" "$CANON"; then
    echo "$(date): WARNING - $(basename "$0") differs from repo canonical ($CANON); run deploy/launchd/deploy.sh to resync" >> "$LOG_FILE"
fi

if nc -z -G 3 127.0.0.1 "$PORT" 2>/dev/null; then
    # Up — clear pending strikes. Only log/alert on a state change.
    if [ -f "$MARKER" ] || [ -f "$AUTH_MARKER" ]; then
        echo "$(ts): port $PORT recovered" >> "$LOG_FILE"
        [ -f "$AUTH_MARKER" ] && telegram "✅ IB Gateway recovered: port $PORT is up again after the login problem."
        rm -f "$MARKER" "$AUTH_MARKER"
    fi

    # Port is up, but is IB *server* connectivity up? The execution service
    # drops CONN_MARKER (epoch of the loss) on Error 1100 and removes it on
    # recovery (1101/1102). Alert on a SUSTAINED 1100 — no kickstart, since a
    # 1100 usually self-heals and restarting would disrupt the session.
    if [ -f "$CONN_MARKER" ]; then
        LOST_AT=$(cat "$CONN_MARKER" 2>/dev/null)
        [ -n "$LOST_AT" ] || LOST_AT=0          # unreadable → treat as very old
        AGE=$(( $(date +%s) - LOST_AT ))
        if [ "$AGE" -ge "$CONN_SUSTAINED_SECS" ]; then
            REALERT_SECS=$((12 * 3600))
            NEED_ALERT=0
            if [ ! -f "$CONN_ALERT_MARKER" ]; then
                NEED_ALERT=1
            else
                LAST_ALERT=$(stat -f %m "$CONN_ALERT_MARKER" 2>/dev/null || echo 0)
                [ $(( $(date +%s) - LAST_ALERT )) -ge "$REALERT_SECS" ] && NEED_ALERT=1
            fi
            if [ "$NEED_ALERT" -eq 1 ]; then
                echo "$(ts): IB Error 1100 sustained ${AGE}s (port $PORT up) — alerting; NOT restarting" >> "$LOG_FILE"
                telegram "🚨 IB Gateway lost server connectivity (Error 1100) for ~$((AGE / 60)) min — port $PORT is up but no market data / order routing. Watchdog will NOT restart (a 1100 usually self-heals). Check the Gateway. (Repeats every 12h until it recovers.)"
                touch "$CONN_ALERT_MARKER"
            fi
        fi
    elif [ -f "$CONN_ALERT_MARKER" ]; then
        # 1100 cleared (execution removed CONN_MARKER on 1102) after we alerted.
        echo "$(ts): IB connectivity restored (Error 1100 cleared)" >> "$LOG_FILE"
        telegram "✅ IB Gateway server connectivity restored (Error 1100 cleared)."
        rm -f "$CONN_ALERT_MARKER"
    fi
    exit 0
fi

# Port is down. FIRST: is the Gateway stuck on a login rejection? Restarting
# into that state hammers IB with failed logins — refuse and alert instead.
LATEST_GW_LOG=$(ls -t "$LOG_DIR"/ibc-*.txt 2>/dev/null | head -1)
if [ -n "$LATEST_GW_LOG" ] && tail -80 "$LATEST_GW_LOG" 2>/dev/null \
        | grep -qE "Unrecognized Username or Password|Too many failed login attempts"; then
    # Alert immediately, then RE-ALERT every 12h while unresolved — a single
    # missed message cost two paper-record days (2026-07-07..09).
    REALERT_SECS=$((12 * 3600))
    NEED_ALERT=0
    if [ ! -f "$AUTH_MARKER" ]; then
        NEED_ALERT=1
    else
        LAST_ALERT=$(stat -f %m "$AUTH_MARKER" 2>/dev/null || echo 0)
        NOW_EPOCH=$(date +%s)
        [ $((NOW_EPOCH - LAST_ALERT)) -ge "$REALERT_SECS" ] && NEED_ALERT=1
    fi
    if [ "$NEED_ALERT" -eq 1 ]; then
        echo "$(ts): AUTH FAILURE in $LATEST_GW_LOG — refusing to kickstart; alerted operator" >> "$LOG_FILE"
        telegram "🚨 IB Gateway login is being REJECTED (port $PORT down). Watchdog will NOT restart it — repeated failed logins risk an IB lockout. Manual re-login needed. (This repeats every 12h until fixed.)"
        touch "$AUTH_MARKER"
    fi
    rm -f "$MARKER"
    exit 0
fi

# No auth failure in evidence — treat as a dead/stuck process. Two strikes.
if [ -f "$MARKER" ]; then
    echo "$(ts): port $PORT down 2 consecutive checks — kickstarting $GW_LABEL" >> "$LOG_FILE"
    launchctl kickstart -k "gui/$(id -u)/$GW_LABEL" >> "$LOG_FILE" 2>&1
    rm -f "$MARKER"            # reset; next run confirms recovery (or strikes again)
    telegram "⚠️ IB Gateway watchdog: port $PORT was down ~10 min with no auth error in the log — kickstarted the Gateway. Will confirm recovery next check."
else
    echo "$(ts): port $PORT down (1st check) — grace before action" >> "$LOG_FILE"
    touch "$MARKER"
fi

# Prune old logs
find "$LOG_DIR" -name "gateway_watchdog_*.log" -mtime +30 -delete 2>/dev/null
exit 0
