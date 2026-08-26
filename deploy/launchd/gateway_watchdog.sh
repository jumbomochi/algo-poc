#!/bin/bash
# IB Gateway + docker-stack watchdog for algo-poc — hardened version.
#
# Checks the API port; if it's been down for TWO consecutive runs, kickstarts
# the IBC gateway launchd job. Two-strike logic rides over the legitimate
# ~1-min nightly auto-restart and weekly cold-restart (Sun 08:00).
#
# HARDENING (2026-07-05): before any kickstart, the newest IBC gateway log is
# checked for authentication failures. If the Gateway is being REJECTED
# ("Unrecognized Username or Password" / "Too many failed login attempts"),
# restarting would loop failed logins into an IB rate-limit or lockout — the
# 2026-07-01 incident (30 rejected logins). In that state the watchdog
# refuses to act, sends ONE Telegram alert, and waits for a human.
#
# KAN-62 (2026-08-26): the refusal above is right; the alert cadence around it
# was not. On 2026-08-20 the 23:55 auto-restart's re-login was rejected, the
# watchdog alerted once at 23:59:56, and its flat 12h re-alert then meant
# silence until the following noon — so the operator's last message arrived
# 4h16m before the 04:15 run, which aborted. The re-alert interval now TIGHTENS
# as the paper run approaches, and the auth branch no longer clears the
# two-strike marker it does not own (that reset cost an extra cycle of downtime
# every time the auth condition cleared). The 23:55 half of KAN-62 is the
# AutoRestartTime move in ~/ibc/config.ini, recorded in README.md.
#
# KAN-63 (2026-08-26): the Error 1100 latch is written by the execution service
# and can only be removed by it — and execution runs in a container. When the
# docker engine died on 2026-08-20 the latch froze, and this watchdog spent 20h
# re-alerting a growing outage that had already ended, across a Gateway cold
# restart that fixed everything. A latch is now trusted only for the lifetime of
# the Gateway session that raised it and only while its writer is running.
#
# KAN-66 (2026-08-26): nothing watched the docker engine. It died with the
# Electron GUI alive on 2026-08-20 and took the divergence monitor, the DB
# backup and every service container down; three alerts fired, all accurate
# about their own symptom, none naming the cause. Stack liveness is now checked
# on this job's 300s cycle. It ALERTS ONLY — never restarts docker.
#
# Wire via launchd with StartInterval (300s). See deploy/launchd/.
#
# NO DEAD-MAN: covered by others, at both ends (KAN-56 coverage review). This
# is a StartInterval job, not a calendar one, so it has no slot to miss — after
# a boot launchd starts it within 300s. And its failure is not silent by
# nature: if the watchdog stops running, the Gateway it guards eventually goes
# unreachable, and *that* is what the 04:15 paper run and the Tuesday refresh
# report (both alert on an unreachable 7497 and both hold their own dead-man
# check). A ping every 5 minutes would also be the noisiest check in the
# account while adding a signal that arrives strictly later than the ones
# already wired. The wider "this host stopped monitoring itself" case belongs
# to $DEADMAN_WATCHDOG_URL, pinged by Alertmanager from the always-firing
# Watchdog rule — a different switch for a different blind spot.

set -uo pipefail

# launchd's default PATH lacks /usr/local/bin, where the docker CLI lives — and
# since KAN-66 this job runs `docker info`. $ALGO_PATH_PREFIX exists only so the
# test can put its stubs ahead of the real binaries; this assignment REPLACES
# the inherited PATH, so prepending from outside would otherwise have no effect.
# Unset in production, where launchd supplies an empty environment.
export PATH="${ALGO_PATH_PREFIX:+$ALGO_PATH_PREFIX:}/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

PORT=7497                      # paper API port (7496 for live)
GW_LABEL="local.ibc-gateway"
LOG_DIR="$HOME/ibc/logs"
LOG_FILE="$LOG_DIR/gateway_watchdog_$(date +%Y%m%d).log"
MARKER="$HOME/ibc/.gateway_down_marker"
AUTH_MARKER="$HOME/ibc/.gateway_auth_failure_alerted"
# Written by the execution service on IB Error 1100 (server-connectivity lost);
# the API port stays OPEN during a 1100, so the port check above is blind to it.
# Line 1 is the epoch second of the loss; subsequent `key=value` lines carry the
# Gateway session identity this watchdog stamps on first observation (KAN-63).
CONN_MARKER="$HOME/ibc/state/gateway_connectivity_lost"
CONN_ALERT_MARKER="$HOME/ibc/.gateway_connectivity_alerted"
CONN_SUSTAINED_SECS=180        # ignore a 1100 that self-heals within one cycle
# An unbounded, ever-growing number in an alert is a signal that nothing is
# measuring it: on 2026-08-21 the watchdog reported "~1207 min" for a 1100 that
# had ended hours earlier. Past this the duration is reported as a floor, not a
# measurement (KAN-63 part 3).
CONN_AGE_CAP_SECS=$((24 * 3600))
DOCKER_ALERT_MARKER="$HOME/ibc/.docker_stack_alerted"
DOCKER_STRIKE_MARKER="$HOME/ibc/.docker_stack_strike"
REALERT_MAX_SECS=$((12 * 3600))
# ALGO_DIR is overridable only so tests can point this wrapper at a checkout;
# launchd starts jobs with an empty environment, so production takes the
# default. Never export it in a login shell.
ALGO_DIR="${ALGO_DIR:-/Users/huiliang/GitHub/algo-poc}"
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
# Docker engine + compose-stack liveness (KAN-66). Alert-only by construction.
# shellcheck source=deploy/launchd/lib/docker_health.sh
. "$ALGO_DIR/deploy/launchd/lib/docker_health.sh"

# launchctl is reached through a variable so the suite can inject a fake job
# table, in the same shape as secrets.sh's $ALGO_SECURITY_BIN. Production takes
# the default; never export this in a login shell.
ALGO_LAUNCHCTL_BIN="${ALGO_LAUNCHCTL_BIN:-/bin/launchctl}"

mkdir -p "$LOG_DIR"
ts() { date '+%Y-%m-%d %H:%M:%S'; }

# Injectable clock. Only the tests set $ALGO_NOW_EPOCH; the escalating re-alert
# schedule below has to be asserted by driving the clock rather than by waiting
# four hours for 04:15 to come round.
now_epoch() { echo "${ALGO_NOW_EPOCH:-$(date +%s)}"; }

# stat(1) and date(1) diverge between BSD and GNU, and the test suite runs on
# ubuntu-latest while production is macOS. Both spellings are tried so the
# shipped code is the code under test rather than a paraphrase of it.
algo_mtime() {
    stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo 0
}
algo_date_fmt() {  # $1 = epoch, $2 = strftime format
    date -r "$1" "+$2" 2>/dev/null || date -d "@$1" "+$2" 2>/dev/null || echo ""
}

# Drift guard: warn loudly if this deployed copy has fallen behind the repo
# canonical. The 2026-08-11 cold-boot auth failure was a stale ~/ibc copy still
# using the pre-T3 default DB password. Warn-only — a legitimately newer
# deployed copy must not block the run. Resync with deploy/launchd/deploy.sh.
CANON="$ALGO_DIR/deploy/launchd/$(basename "$0")"
if [ -f "$CANON" ] && ! cmp -s "$0" "$CANON"; then
    echo "$(date): WARNING - $(basename "$0") differs from repo canonical ($CANON); run deploy/launchd/deploy.sh to resync" >> "$LOG_FILE"
fi

# --------------------------------------------------------------------------
# Re-alert throttling
# --------------------------------------------------------------------------
# True when $1 (an alert marker) is absent or older than $2 seconds. Shared by
# every re-alerting condition so they cannot drift apart the way the six
# hand-copied telegram() bodies did.
need_alert() {
    local marker="$1" interval="$2"
    [ -f "$marker" ] || return 0
    [ $(( $(now_epoch) - $(algo_mtime "$marker") )) -ge "$interval" ]
}

# The daily paper run this watchdog exists to protect. Kept as constants rather
# than parsed out of the plist (this is a 300s job; it should not be reading
# and parsing XML 288 times a day) — tests/deploy/test_gateway_watchdog.py
# asserts they still match local.algo-paper-trading.plist.
PAPER_RUN_HOUR=4
PAPER_RUN_MIN=15

# Seconds from now until the next local 04:15. Computed from the local
# time-of-day rather than by parsing a date string, because `date -j -f` is
# BSD-only and this has to run under the Linux CI that tests it.
secs_to_paper_run() {
    local hms h m s sod run delta
    hms="$(algo_date_fmt "$(now_epoch)" '%H %M %S')"
    [ -n "$hms" ] || { echo "$REALERT_MAX_SECS"; return 0; }
    read -r h m s <<< "$hms"
    sod=$(( 10#$h * 3600 + 10#$m * 60 + 10#$s ))
    run=$(( PAPER_RUN_HOUR * 3600 + PAPER_RUN_MIN * 60 ))
    delta=$(( run - sod ))
    [ "$delta" -lt 0 ] && delta=$(( delta + 86400 ))
    echo "$delta"
}

# KAN-62 part 2: while the Gateway is unusable, how often to re-alert. A flat
# 12h meant the operator's last warning before the 04:15 run was 4h16m stale on
# 2026-08-21, and the run aborted. The interval tightens as the run approaches,
# so the final message is recent enough to act on.
#
# The 15-minute floor inside the last hour is what makes the guarantee hold:
# this job runs every 300s, so within the final 60 minutes an alert is always
# either newer than 15 minutes or re-sent — an alert therefore always lands
# within 60 minutes of the run.
auth_realert_secs() {
    local remaining
    remaining="$(secs_to_paper_run)"
    if   [ "$remaining" -le 3600 ];  then echo 900          # < 1h  → every 15m
    elif [ "$remaining" -le 10800 ]; then echo 1800         # < 3h  → every 30m
    elif [ "$remaining" -le 21600 ]; then echo 3600         # < 6h  → hourly
    else echo "$REALERT_MAX_SECS"; fi                       # else  → every 12h
}

# --------------------------------------------------------------------------
# KAN-66: docker engine + compose stack liveness
# --------------------------------------------------------------------------
# Runs before the port check, because a dead engine is upstream of almost
# everything else this host does and its symptoms surface elsewhere. Alert
# only: no restart, kill, or open — see lib/docker_health.sh.
algo_docker_check
case "$ALGO_DOCKER_STATUS" in
    daemon-down|services-down)
        # TWO STRIKES, exactly like the port check above and for the same
        # reason: a `docker compose up` legitimately shows containers at
        # "(health: starting)" for a few seconds, and a Docker Desktop restart
        # makes `docker info` fail for about half a minute. Paging for either
        # teaches the operator to ignore this alert, which is the one failure
        # mode it cannot afford. The 2026-08-20 outage lasted roughly fifteen
        # hours, so one extra 300s cycle of confirmation costs nothing real.
        if [ ! -f "$DOCKER_STRIKE_MARKER" ]; then
            echo "$(ts): docker stack unhealthy (1st check) - $ALGO_DOCKER_DETAIL" >> "$LOG_FILE"
            touch "$DOCKER_STRIKE_MARKER"
        elif need_alert "$DOCKER_ALERT_MARKER" "$REALERT_MAX_SECS"; then
            echo "$(ts): DOCKER STACK UNHEALTHY - $ALGO_DOCKER_DETAIL" >> "$LOG_FILE"
            algo_alert_local "docker stack unhealthy: $ALGO_DOCKER_DETAIL"
            telegram "🚨 algo-poc docker stack: $ALGO_DOCKER_DETAIL. Nothing is being restarted automatically — a dead engine needs process kills and an app relaunch, which is not safe to automate on a 5-minute timer. (Repeats every 12h until it recovers.)"
            touch "$DOCKER_ALERT_MARKER"
        fi
        ;;
    ok)
        rm -f "$DOCKER_STRIKE_MARKER"
        if [ -f "$DOCKER_ALERT_MARKER" ]; then
            echo "$(ts): docker stack recovered - $ALGO_DOCKER_DETAIL" >> "$LOG_FILE"
            telegram "✅ algo-poc docker stack recovered: daemon up and all compose services running."
            rm -f "$DOCKER_ALERT_MARKER"
        fi
        ;;
    *)
        # Cannot ask, so cannot judge. Not a strike: "unknown" must never
        # accumulate into a page, or a host without docker alerts forever.
        echo "$(ts): docker stack state UNKNOWN - $ALGO_DOCKER_DETAIL" >> "$LOG_FILE"
        ;;
esac

# --------------------------------------------------------------------------
# KAN-63: is the Error 1100 latch still describing something real?
# --------------------------------------------------------------------------
# The running Gateway's identity: "<pid> <start-epoch>". Derived from `ps -o
# etime=` rather than `ps -o lstart=` because etime needs no locale-dependent
# date parsing and its [[DD-]HH:]MM:SS shape is identical on BSD and GNU.
gateway_identity() {
    local pid etime days rest a b c secs
    pid="$("$ALGO_LAUNCHCTL_BIN" list 2>/dev/null | awk -v l="$GW_LABEL" '$NF == l { print $1 }' | head -1)"
    case "$pid" in ''|*[!0-9]*) return 1 ;; esac
    etime="$(ps -p "$pid" -o etime= 2>/dev/null | tr -d ' ')"
    [ -n "$etime" ] || return 1
    days=0; rest="$etime"
    case "$etime" in *-*) days="${etime%%-*}"; rest="${etime#*-}" ;; esac
    IFS=: read -r a b c <<< "$rest"
    if [ -n "${c:-}" ]; then
        secs=$(( 10#$days * 86400 + 10#$a * 3600 + 10#$b * 60 + 10#$c ))
    else
        secs=$(( 10#$days * 86400 + 10#$a * 60 + 10#$b ))
    fi
    echo "$pid $(( $(now_epoch) - secs ))"
}

# `key=value` out of the marker's tail, or "" if absent.
conn_marker_field() {
    sed -n "s/^$1=//p" "$CONN_MARKER" 2>/dev/null | head -1
}

# Stamp the current Gateway identity onto a marker that does not carry one.
# APPENDED, never rewritten: line 1 must stay the bare loss epoch so an older
# deployed copy of this script — or any other reader — still parses it with a
# plain `head -1`, and the writer's own `writer=` / `gateway_endpoint=` lines
# are diagnostics worth keeping. Only ever called when no stamp is present, so
# it cannot double up.
stamp_conn_marker() {
    local pid="$1" started="$2"
    printf 'gateway_pid=%s\ngateway_started_at=%s\nstamped_by=watchdog\n' \
        "$pid" "$started" >> "$CONN_MARKER" 2>/dev/null || true
}

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
        LOST_AT=$(head -1 "$CONN_MARKER" 2>/dev/null | tr -d ' ')
        case "$LOST_AT" in ''|*[!0-9]*) LOST_AT=0 ;; esac   # unreadable → very old

        # Is this latch still describing the Gateway session that raised it?
        # A 1100 cannot outlive the connection it belongs to, and on 2026-08-21
        # this one survived a full Gateway process replacement while its writer
        # was dead in a stopped container.
        STALE_REASON=""
        if IDENT="$(gateway_identity)"; then
            read -r GW_PID GW_STARTED <<< "$IDENT"
            MARK_PID="$(conn_marker_field gateway_pid)"
            MARK_STARTED="$(conn_marker_field gateway_started_at)"
            if [ -n "$MARK_PID" ] && [ -n "$MARK_STARTED" ]; then
                # ±5s tolerance: the start epoch is derived from second-resolution
                # elapsed time sampled at two different moments, so an unchanged
                # process legitimately re-derives one second either side.
                DRIFT=$(( MARK_STARTED - GW_STARTED )); [ "$DRIFT" -lt 0 ] && DRIFT=$(( -DRIFT ))
                if [ "$MARK_PID" != "$GW_PID" ] || [ "$DRIFT" -gt 5 ]; then
                    STALE_REASON="it was recorded for Gateway pid $MARK_PID (started $MARK_STARTED), and the running Gateway is pid $GW_PID (started $GW_STARTED)"
                fi
            elif [ "$GW_STARTED" -gt $(( LOST_AT + 5 )) ]; then
                # Unstamped or legacy bare-epoch marker: the Gateway started
                # after the loss was recorded, so the loss belongs to a session
                # that no longer exists. This is the backstop for a latch this
                # watchdog never got to stamp.
                STALE_REASON="the running Gateway started at $GW_STARTED, after the loss was recorded at $LOST_AT"
            fi
        else
            echo "$(ts): WARNING - could not determine the running Gateway's identity; not judging the Error 1100 latch this cycle" >> "$LOG_FILE"
        fi

        if [ -n "$STALE_REASON" ]; then
            echo "$(ts): STALE Error 1100 latch dropped — $STALE_REASON" >> "$LOG_FILE"
            rm -f "$CONN_MARKER"
            if [ -f "$CONN_ALERT_MARKER" ]; then
                telegram "✅ IB Gateway server connectivity: the Error 1100 latch was STALE and has been dropped — $STALE_REASON. No outage is in progress."
                rm -f "$CONN_ALERT_MARKER"
            fi
            exit 0
        fi

        AGE=$(( $(now_epoch) - LOST_AT ))
        if [ "$AGE" -ge "$CONN_SUSTAINED_SECS" ]; then
            # Do not trust a latch whose writer is not running. Execution is the
            # only thing that can clear it, so with execution down the honest
            # alert is "execution is down", which is both true and actionable —
            # not a 1100 duration that nothing is maintaining.
            EXEC_STATE="unknown"
            case "$ALGO_DOCKER_STATUS" in
                daemon-down)
                    EXEC_STATE="down" ;;
                ok|services-down)
                    if algo_docker_service_running execution; then
                        EXEC_STATE="up"
                    else
                        EXEC_STATE="down"
                    fi ;;
            esac

            if need_alert "$CONN_ALERT_MARKER" "$REALERT_MAX_SECS"; then
                if [ "$EXEC_STATE" = "down" ]; then
                    echo "$(ts): Error 1100 latch present but the execution service is DOWN — reporting execution, not a 1100 duration" >> "$LOG_FILE"
                    algo_alert_local "execution service is down; the Error 1100 latch cannot be trusted or cleared"
                    telegram "🚨 The execution service is DOWN. An IB Error 1100 latch is present, but execution is the only thing that writes or clears it, so its age means nothing — not reporting a 1100 duration. Fix the execution service. (Repeats every 12h.)"
                else
                    if [ "$AGE" -gt "$CONN_AGE_CAP_SECS" ]; then
                        AGE_TEXT="over $((CONN_AGE_CAP_SECS / 3600))h — the latch is older than this cap, so treat the duration as a floor, not a measurement"
                    else
                        AGE_TEXT="~$((AGE / 60)) min"
                    fi
                    echo "$(ts): IB Error 1100 sustained ${AGE}s (port $PORT up, execution $EXEC_STATE) — alerting; NOT restarting" >> "$LOG_FILE"
                    telegram "🚨 IB Gateway lost server connectivity (Error 1100) for $AGE_TEXT — port $PORT is up but no market data / order routing. Watchdog will NOT restart (a 1100 usually self-heals). Check the Gateway. (Repeats every 12h until it recovers.)"
                fi
                touch "$CONN_ALERT_MARKER"
            fi

            # Stamp an unstamped latch so the NEXT cycle can tell a live 1100
            # from one that outlived its Gateway. Done after the alert decision
            # so a first observation still alerts on a genuine 1100.
            if [ -z "$(conn_marker_field gateway_pid)" ] && IDENT="$(gateway_identity)"; then
                read -r GW_PID GW_STARTED <<< "$IDENT"
                stamp_conn_marker "$GW_PID" "$GW_STARTED"
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
    # Alert immediately, then RE-ALERT on the escalating schedule while
    # unresolved — a single missed message cost two paper-record days
    # (2026-07-07..09), and a flat 12h cadence cost the 2026-08-21 session.
    REALERT_SECS="$(auth_realert_secs)"
    if need_alert "$AUTH_MARKER" "$REALERT_SECS"; then
        MINS_TO_RUN=$(( $(secs_to_paper_run) / 60 ))
        echo "$(ts): AUTH FAILURE in $LATEST_GW_LOG — refusing to kickstart; alerted operator (re-alert ${REALERT_SECS}s, ${MINS_TO_RUN} min to the ${PAPER_RUN_HOUR}:$(printf '%02d' "$PAPER_RUN_MIN") run)" >> "$LOG_FILE"
        telegram "🚨 IB Gateway login is being REJECTED (port $PORT down). Watchdog will NOT restart it — repeated failed logins risk an IB lockout. Manual re-login needed. The ${PAPER_RUN_HOUR}:$(printf '%02d' "$PAPER_RUN_MIN") paper run is in ${MINS_TO_RUN} min and will abort unless this is fixed."
        touch "$AUTH_MARKER"
    fi
    # NOTE (KAN-62): $MARKER is deliberately NOT removed here. It is the
    # two-strike counter belonging to the *kickstart* path below, and this
    # branch does not own it. Clearing it meant that when the auth condition
    # finally cleared on 2026-08-21 at 08:19 the watchdog restarted counting
    # from zero — "port 7497 down (1st check)" at 08:20:09 — adding a whole
    # extra cycle of downtime on top of the outage it had just waited out.
    exit 0
fi

# No auth failure in evidence — treat as a dead/stuck process. Two strikes.
if [ -f "$MARKER" ]; then
    echo "$(ts): port $PORT down 2 consecutive checks — kickstarting $GW_LABEL" >> "$LOG_FILE"
    "$ALGO_LAUNCHCTL_BIN" kickstart -k "gui/$(id -u)/$GW_LABEL" >> "$LOG_FILE" 2>&1
    rm -f "$MARKER"            # reset; next run confirms recovery (or strikes again)
    telegram "⚠️ IB Gateway watchdog: port $PORT was down ~10 min with no auth error in the log — kickstarted the Gateway. Will confirm recovery next check."
else
    echo "$(ts): port $PORT down (1st check) — grace before action" >> "$LOG_FILE"
    touch "$MARKER"
fi

# Prune old logs
find "$LOG_DIR" -name "gateway_watchdog_*.log" -mtime +30 -delete 2>/dev/null
exit 0
