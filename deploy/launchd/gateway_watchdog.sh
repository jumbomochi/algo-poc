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
# KAN-118 (2026-10-16): a 1100 does not always self-heal. On 2026-10-14 the
# Gateway sat on "Connecting to server..." for 7h11m with one page. A validated
# 1100 that lasts CONN_RESTART_SECS, with execution up, now gets ONE guarded
# kickstart. It never runs into a rejected login, IBC's own restarts, the paper
# slot or a running IB-client job, and it never runs twice for one outage.
# While a 1100 lasts, the re-alert is hourly during US market hours.
#
# Wire via launchd with StartInterval (300s). See deploy/launchd/.
#
# NO DEAD-MAN: covered by others, at both ends (KAN-56 coverage review). This
# is a StartInterval job, not a calendar one, so it has no slot to miss — after
# a boot launchd starts it within 300s. And its failure is not silent by
# nature: if the watchdog stops running, the Gateway it guards eventually goes
# unreachable, and *that* is what the 05:15 paper run and the Tuesday refresh
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
# KAN-118: a 1100 that outlives this is not going to self-heal. On 2026-10-14
# the Gateway sat on "Connecting to server..." for 7h11m and recovered only when
# a human restarted it. Past this, with the latch validated and execution up,
# the watchdog restarts the Gateway ONCE (see conn_restart_decision).
CONN_RESTART_SECS=1800
# The restart record: which Gateway was restarted, when, and how it turned out.
# Never deleted; its rules are self-limiting (see conn_restart_decision).
CONN_RESTART_MARKER="$HOME/ibc/.gateway_connectivity_restarted"
# A Gateway that started within this long after our kickstart is the one the
# kickstart produced. A sustained 1100 on it is not restarted again.
CONN_RESTART_BORN_SECS=900
# How long a restarted Gateway gets to reopen its port before the outcome is
# paged as "still down".
CONN_RESTART_VERIFY_SECS=900
# A 1100 recorded within this long after our restart, on the Gateway the
# restart produced, is the same outage coming back. Later than this it is a new
# outage on that Gateway and may be restarted for.
CONN_RESTART_SAME_OUTAGE_SECS=3600
# launchd jobs that hold an IB API session of their own. A restart under them
# would break a paper run mid-decision or a 5h refresh fetch.
IB_CLIENT_JOBS="local.algo-paper-trading local.algo-pipeline-report local.algo-backtest-refresh"
# Scheduled Gateway restarts the watchdog must not race. They mirror
# ~/ibc/config.ini (AutoRestartTime=2:00 PM, ColdRestartTime=08:00 on Sundays),
# recorded in deploy/launchd/README.md and asserted by the test suite.
IBC_AUTO_RESTART_HOUR=14
IBC_AUTO_RESTART_MIN=0
IBC_COLD_RESTART_WDAY=0
IBC_COLD_RESTART_HOUR=8
IBC_COLD_RESTART_MIN=0
DOCKER_ALERT_MARKER="$HOME/ibc/.docker_stack_alerted"
DOCKER_STRIKE_MARKER="$HOME/ibc/.docker_stack_strike"
REALERT_MAX_SECS=$((12 * 3600))
# ALGO_DIR is overridable only so tests can point this wrapper at a checkout;
# launchd starts jobs with an empty environment, so production takes the
# default. Never export it in a login shell.
ALGO_DIR="${ALGO_DIR:-/Users/huiliang/algo-poc-deploy}"
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
# four hours for the paper run's slot to come round.
now_epoch() { echo "${ALGO_NOW_EPOCH:-$(date +%s)}"; }

# stat(1) and date(1) diverge between BSD and GNU, and the test suite runs on
# ubuntu-latest while production is macOS. Both spellings are tried so the
# shipped code is the code under test rather than a paraphrase of it.
#
# THE OUTPUT IS VALIDATED, NOT THE EXIT CODE. `stat -f %m` on GNU coreutils
# means "filesystem status" and prints a FOUR-LINE filesystem dump to stdout
# while exiting 1 — so a plain `stat -f %m || stat -c %Y` chain appends the real
# mtime to that dump and hands back garbage. Every arithmetic comparison on it
# then fails, and because `need_alert` reads that comparison as "not due yet",
# EVERY THROTTLED ALERT SILENTLY STOPS FIRING. A helper that fails by returning
# nonsense instead of failing loudly is how a monitor goes quiet, so these two
# check the shape of what they got and only then trust it.
algo_mtime() {
    local m
    m="$(stat -f %m "$1" 2>/dev/null)"                       # BSD / macOS
    case "$m" in ''|*[!0-9]*) m="$(stat -c %Y "$1" 2>/dev/null)" ;; esac  # GNU
    case "$m" in ''|*[!0-9]*) m=0 ;; esac
    printf '%s\n' "$m"
}

# Local wall-clock "HH MM SS" for an epoch, or non-zero if it cannot be read.
# Deliberately narrower than a general strftime wrapper: a fixed output shape is
# what makes the validation above possible.
algo_local_hms() {
    local out
    for out in "$(date -r "$1" '+%H %M %S' 2>/dev/null)" \
               "$(date -d "@$1" '+%H %M %S' 2>/dev/null)"; do
        case "$out" in
            [0-9][0-9]" "[0-9][0-9]" "[0-9][0-9]) printf '%s\n' "$out"; return 0 ;;
        esac
    done
    return 1
}

# Local "W HH MM SS" for an epoch, W from %w (0 = Sunday … 6 = Saturday, which
# is launchd's Weekday numbering), or non-zero if it cannot be read. Same two
# spellings and the same shape check as algo_local_hms, for the same reason
# (KAN-99).
algo_local_wday_hms() {
    local out
    for out in "$(date -r "$1" '+%w %H %M %S' 2>/dev/null)" \
               "$(date -d "@$1" '+%w %H %M %S' 2>/dev/null)"; do
        case "$out" in
            [0-6]" "[0-9][0-9]" "[0-9][0-9]" "[0-9][0-9]) printf '%s\n' "$out"; return 0 ;;
        esac
    done
    return 1
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
# asserts they still match local.algo-paper-trading.plist. 05:15 since KAN-104
# (04:15 fell before the NYSE close every EST winter).
PAPER_RUN_HOUR=5
PAPER_RUN_MIN=15
# launchd Weekday numbering: 0 or 7 = Sunday, 1 = Monday … 6 = Saturday. The
# job runs Tue–Sat SGT, i.e. after each US Mon–Fri session (KAN-99).
PAPER_RUN_WEEKDAYS="2 3 4 5 6"

# "SECS W": seconds from now until the next scheduled paper run, and that run's
# weekday (0-6), or "SECS -" on the fallback below. Computed from the local
# weekday and time of day rather than by parsing a date string, because
# `date -j -f` is BSD-only and this has to run under the Linux CI that tests it.
#
# KAN-99: the countdown used to target the next 04:15 on ANY day, so a weekend
# login failure escalated toward a Sunday and a Monday 04:15 that launchd never
# fires, and every page said that run "will abort". It now steps forward a day
# at a time to the first weekday in PAPER_RUN_WEEKDAYS. Each step is a flat
# 86400s: the host is SGT, which has no daylight saving.
paper_run_countdown() {
    local whms w h m s sod run delta off cand day rd
    # Cannot read the clock → fall back to the slowest cadence rather than
    # inventing an urgency, and say so in the log.
    if ! whms="$(algo_local_wday_hms "$(now_epoch)")"; then
        echo "$(ts): WARNING - could not read the local time of day; re-alert cadence falls back to ${REALERT_MAX_SECS}s" >> "$LOG_FILE"
        echo "$REALERT_MAX_SECS -"; return 0
    fi
    read -r w h m s <<< "$whms"
    sod=$(( 10#$h * 3600 + 10#$m * 60 + 10#$s ))
    run=$(( PAPER_RUN_HOUR * 3600 + PAPER_RUN_MIN * 60 ))
    delta=$(( run - sod ))
    # Offset 7 is today's weekday again, for when today is the only run day
    # and its run has already passed.
    for off in 0 1 2 3 4 5 6 7; do
        cand=$(( delta + off * 86400 ))
        [ "$cand" -ge 0 ] || continue
        day=$(( (w + off) % 7 ))
        for rd in $PAPER_RUN_WEEKDAYS; do
            if [ $(( rd % 7 )) -eq "$day" ]; then
                echo "$cand $day"; return 0
            fi
        done
    done
    # Unreachable unless PAPER_RUN_WEEKDAYS is empty or garbage.
    echo "$(ts): WARNING - PAPER_RUN_WEEKDAYS='${PAPER_RUN_WEEKDAYS}' names no weekday; re-alert cadence falls back to ${REALERT_MAX_SECS}s" >> "$LOG_FILE"
    echo "$REALERT_MAX_SECS -"
}

# Seconds from now until the next scheduled paper run.
secs_to_paper_run() {
    local secs _day
    read -r secs _day <<< "$(paper_run_countdown)"
    echo "$secs"
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

# KAN-118: true while a US regular session can be open, in SGT. The union of
# both seasons (21:30–04:00 under EDT, 22:30–05:00 under EST), so it needs no
# DST table: an evening on Mon–Fri, or the small hours of Tue–Sat. Exchange
# holidays are not modelled; an extra hourly page on one is harmless.
in_us_market_hours() {
    local whms w h m s sod
    whms="$(algo_local_wday_hms "$(now_epoch)")" || return 1
    read -r w h m s <<< "$whms"
    sod=$(( 10#$h * 3600 + 10#$m * 60 ))
    if [ "$sod" -ge $(( 21 * 3600 + 30 * 60 )) ]; then
        [ "$w" -ge 1 ] && [ "$w" -le 5 ]
    elif [ "$sod" -lt $(( 5 * 3600 )) ]; then
        [ "$w" -ge 2 ] && [ "$w" -le 6 ]
    else
        return 1
    fi
}

# KAN-118: how often to re-alert while the Gateway cannot route orders but its
# port is up. One page in seven hours (2026-10-14) is too quiet for an outage
# that overlaps a session; outside one, the 12h cadence is unchanged.
conn_realert_secs() {
    if in_us_market_hours; then echo 3600; else echo "$REALERT_MAX_SECS"; fi
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

# True when the newest IBC log shows IB rejecting the login. Any kickstart into
# that state loops failed logins toward a lockout (2026-07-01, 30 rejections),
# so both restart paths ask this first.
ibc_login_rejected() {
    local latest
    latest=$(ls -t "$LOG_DIR"/ibc-*.txt 2>/dev/null | head -1)
    [ -n "$latest" ] && tail -80 "$latest" 2>/dev/null \
        | grep -qE "Unrecognized Username or Password|Too many failed login attempts"
}

# --------------------------------------------------------------------------
# KAN-118: restart a Gateway stuck in a sustained Error 1100, once
# --------------------------------------------------------------------------
# `key=value` out of the restart record, or "" if absent.
restart_marker_field() {
    sed -n "s/^$1=//p" "$CONN_RESTART_MARKER" 2>/dev/null | head -1
}

# Set one field of the restart record in place, keeping the others.
set_restart_marker_field() {
    local key="$1" value="$2" tmp
    [ -f "$CONN_RESTART_MARKER" ] || return 0
    tmp="$CONN_RESTART_MARKER.tmp.$$"
    { grep -v "^$key=" "$CONN_RESTART_MARKER" 2>/dev/null; printf '%s=%s\n' "$key" "$value"; } \
        > "$tmp" && mv "$tmp" "$CONN_RESTART_MARKER"
}

# "HH:MM" of an epoch, for pages. Falls back to the bare epoch.
hhmm_of() {
    local hms h m _s
    if hms="$(algo_local_hms "$1")"; then
        read -r h m _s <<< "$hms"; echo "$h:$m"
    else
        echo "$1"
    fi
}

# Is any launchd job that holds its own IB session running right now? A job is
# running when `launchctl list` shows a numeric PID for it.
# The table is captured before it is parsed: piping it into an awk that exits
# on the first match can SIGPIPE launchctl (its real output is larger than a
# pipe buffer), and under pipefail that reads as "nothing running".
ib_client_job_running() {
    local table
    table="$("$ALGO_LAUNCHCTL_BIN" list 2>/dev/null)"
    awk -v jobs=" $IB_CLIENT_JOBS " '
        !found && index(jobs, " " $NF " ") && $1 ~ /^[0-9]+$/ { print $NF; found = 1 }
        END { exit !found }' <<< "$table"
}

# Prints why a restart must wait right now, or nothing (exit 1) when it may go
# ahead. Deferral, not refusal: the next cycle asks again. An unreadable clock
# defers, because every window below is a time-of-day.
restart_blackout_reason() {
    local whms w h m s sod job win rd
    if ! whms="$(algo_local_wday_hms "$(now_epoch)")"; then
        echo "the local time of day cannot be read"; return 0
    fi
    read -r w h m s <<< "$whms"
    sod=$(( 10#$h * 3600 + 10#$m * 60 + 10#$s ))

    # IBC's own daily restart, and its weekly cold restart: 10 min before to
    # 15 min after, so the two can never stack.
    win=$(( IBC_AUTO_RESTART_HOUR * 3600 + IBC_AUTO_RESTART_MIN * 60 ))
    if [ "$sod" -ge $(( win - 600 )) ] && [ "$sod" -lt $(( win + 900 )) ]; then
        echo "IBC's daily auto-restart ($(printf '%02d:%02d' "$IBC_AUTO_RESTART_HOUR" "$IBC_AUTO_RESTART_MIN")) is due or in progress"; return 0
    fi
    win=$(( IBC_COLD_RESTART_HOUR * 3600 + IBC_COLD_RESTART_MIN * 60 ))
    if [ "$w" -eq "$IBC_COLD_RESTART_WDAY" ] && [ "$sod" -ge $(( win - 600 )) ] && [ "$sod" -lt $(( win + 900 )) ]; then
        echo "IBC's weekly cold restart (Sun $(printf '%02d:%02d' "$IBC_COLD_RESTART_HOUR" "$IBC_COLD_RESTART_MIN")) is due or in progress"; return 0
    fi

    # The paper run's slot: 5 min before it to 15 min after, on the days it
    # runs. The running-job check below covers a run that goes long.
    win=$(( PAPER_RUN_HOUR * 3600 + PAPER_RUN_MIN * 60 ))
    for rd in $PAPER_RUN_WEEKDAYS; do
        if [ $(( rd % 7 )) -eq "$w" ] && [ "$sod" -ge $(( win - 300 )) ] && [ "$sod" -lt $(( win + 900 )) ]; then
            echo "the $(printf '%02d:%02d' "$PAPER_RUN_HOUR" "$PAPER_RUN_MIN") paper run's slot is open"; return 0
        fi
    done

    if job="$(ib_client_job_running)"; then
        echo "$job is running and holds its own IB session"; return 0
    fi
    return 1
}

# Decide what to do about a VALIDATED, sustained 1100 with execution up. Prints
# one of:
#   restart            kickstart now
#   wait <text>        not yet (under CONN_RESTART_SECS, or a blackout)
#   refuse <text>      never for this outage (already restarted, or login
#                      rejected); the alert says a human is needed
# Arguments: latch age, latch loss epoch, running Gateway pid, its start epoch.
#
# Every refusal is asked only once the 1100 has lasted CONN_RESTART_SECS, so a
# short blip never pages "a human needs to look".
conn_restart_decision() {
    local age="$1" lost_at="$2" gw_pid="$3" gw_started="$4" r_pid r_at born since reason
    if [ "$age" -lt "$CONN_RESTART_SECS" ]; then
        echo "wait it will restart the Gateway if this lasts $((CONN_RESTART_SECS / 60)) min"; return 0
    fi
    r_pid="$(restart_marker_field restarted_pid)"
    r_at="$(restart_marker_field restarted_at)"
    case "$r_at" in ''|*[!0-9]*) r_at="" ;; esac

    if [ -n "$r_at" ]; then
        # Same process, alive since before our kickstart: the kickstart did not
        # replace it. The start-time bound keeps a recycled pid from matching
        # an old record.
        if [ "$r_pid" = "$gw_pid" ] && [ "$gw_started" -le $(( r_at + 5 )) ]; then
            echo "refuse the watchdog already restarted this Gateway (pid $gw_pid) at $(hhmm_of "$r_at") and the same process is still in Error 1100"; return 0
        fi
        # The Gateway our kickstart produced, losing the servers again soon
        # after: the same outage, which the restart did not fix. A loss
        # recorded later than CONN_RESTART_SAME_OUTAGE_SECS after the restart
        # is a new outage and may be restarted for.
        born=$(( gw_started - r_at ))
        since=$(( lost_at - r_at ))
        if [ "$born" -ge -5 ] && [ "$born" -le "$CONN_RESTART_BORN_SECS" ] \
            && [ "$since" -ge -5 ] && [ "$since" -le "$CONN_RESTART_SAME_OUTAGE_SECS" ]; then
            echo "refuse the watchdog restarted the Gateway at $(hhmm_of "$r_at") and the new one (pid $gw_pid) went back into Error 1100 at $(hhmm_of "$lost_at")"; return 0
        fi
    fi
    if ibc_login_rejected; then
        echo "refuse the IBC log shows IB rejecting the login, and a restart would loop failed logins toward a lockout"; return 0
    fi
    if reason="$(restart_blackout_reason)"; then
        echo "wait restart deferred: $reason"; return 0
    fi
    echo "restart"
}

# Kickstart the Gateway for a sustained 1100 and record it. The record is
# written BEFORE the kickstart, so a watchdog killed mid-call can never restart
# the same outage twice. A kickstart launchctl refuses is not retried: the
# record already names this pid, so the next cycle refuses, and this page says
# so now rather than claiming a restart.
conn_restart_gateway() {
    local age="$1" gw_pid="$2" lost_at="$3" now rc
    now="$(now_epoch)"
    printf 'restarted_pid=%s\nrestarted_at=%s\nlost_at=%s\noutcome=pending\n' \
        "$gw_pid" "$now" "$lost_at" > "$CONN_RESTART_MARKER"
    echo "$(ts): IB Error 1100 sustained ${age}s (port $PORT up, execution up, latch validated for pid $gw_pid) — kickstarting $GW_LABEL (KAN-118, once per outage)" >> "$LOG_FILE"
    "$ALGO_LAUNCHCTL_BIN" kickstart -k "gui/$(id -u)/$GW_LABEL" >> "$LOG_FILE" 2>&1
    rc=$?
    if [ "$rc" -ne 0 ]; then
        set_restart_marker_field outcome failed
        echo "$(ts): KAN-118 kickstart FAILED (launchctl exit $rc) — not retrying for this outage" >> "$LOG_FILE"
        telegram "🚨 IB Gateway has been in Error 1100 for ~$((age / 60)) min, and the watchdog's restart FAILED: launchctl kickstart exited $rc. It will not retry for this outage. A human needs to restart the Gateway."
        return 0
    fi
    telegram "⚠️ IB Gateway was in Error 1100 (no server connectivity) for ~$((age / 60)) min with port $PORT up — the watchdog has RESTARTED it, once for this outage. It will report whether it came back."
}

# KAN-118: page how a restart turned out, once. $1 = recovered | failed,
# $2 = the page. No-op unless a restart is awaiting its outcome.
conn_restart_outcome() {
    [ "$(restart_marker_field outcome)" = "pending" ] || return 1
    set_restart_marker_field outcome "$1"
    echo "$(ts): KAN-118 restart outcome: $1" >> "$LOG_FILE"
    telegram "$2"
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
    # recovery (1101/1102) or on any reconnect. Alert on a SUSTAINED 1100; a
    # short one usually self-heals, but one that outlives CONN_RESTART_SECS
    # gets ONE guarded restart (KAN-118).
    if [ -f "$CONN_MARKER" ]; then
        LOST_AT=$(head -1 "$CONN_MARKER" 2>/dev/null | tr -d ' ')
        case "$LOST_AT" in ''|*[!0-9]*) LOST_AT=0 ;; esac   # unreadable → very old

        # Is this latch still describing the Gateway session that raised it?
        # A 1100 cannot outlive the connection it belongs to, and on 2026-08-21
        # this one survived a full Gateway process replacement while its writer
        # was dead in a stopped container.
        STALE_REASON=""
        # Set only when the latch carries a stamp that matches the running
        # Gateway: the one state in which a restart may be considered.
        LATCH_VALIDATED=""
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
                else
                    LATCH_VALIDATED=1
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
            # After a KAN-118 restart this is the expected way the old latch
            # goes: the Gateway it named was replaced, and the port is back.
            if [ -n "$MARK_PID" ] && [ "$MARK_PID" = "$(restart_marker_field restarted_pid)" ] \
                && conn_restart_outcome recovered "✅ IB Gateway is back after the watchdog's Error 1100 restart: port $PORT is up on a new Gateway session (pid $GW_PID), and the old session's latch has been dropped. Execution reconnects on its own timer."; then
                rm -f "$CONN_ALERT_MARKER"
            elif [ -f "$CONN_ALERT_MARKER" ]; then
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

            # KAN-118: only a latch validated against the running Gateway, with
            # its writer up and a readable loss time, is ever restarted for.
            # Every other state keeps the alert-only behaviour.
            if [ -n "$LATCH_VALIDATED" ] && [ "$EXEC_STATE" = "up" ] && [ "$LOST_AT" -gt 0 ]; then
                DECISION="$(conn_restart_decision "$AGE" "$LOST_AT" "$GW_PID" "$GW_STARTED")"
            elif [ "$EXEC_STATE" = "up" ] && [ -n "$LATCH_VALIDATED" ]; then
                DECISION="wait it will not restart the Gateway: the latch's loss time is unreadable"
            elif [ "$EXEC_STATE" = "up" ]; then
                DECISION="wait it will restart the Gateway only once this latch is confirmed against the running Gateway"
            else
                DECISION="wait execution is $EXEC_STATE"
            fi
            if [ "$DECISION" = "restart" ]; then
                conn_restart_gateway "$AGE" "$GW_PID" "$LOST_AT"
                touch "$CONN_ALERT_MARKER"
                exit 0
            fi
            RESTART_NOTE="${DECISION#* }"

            REALERT_SECS="$(conn_realert_secs)"
            if [ "$REALERT_SECS" -lt "$REALERT_MAX_SECS" ]; then
                REPEAT_TEXT="Repeats hourly during US market hours, otherwise every 12h"
            else
                REPEAT_TEXT="Repeats every 12h, hourly during US market hours"
            fi
            # A refusal while a restart awaits its outcome IS that outcome:
            # page it now, not after the throttle the restart page started.
            FORCE_ALERT=""
            case "$DECISION" in
                refuse\ *) [ "$(restart_marker_field outcome)" = "pending" ] && FORCE_ALERT=1 ;;
            esac
            if [ -n "$FORCE_ALERT" ] || need_alert "$CONN_ALERT_MARKER" "$REALERT_SECS"; then
                if [ "$EXEC_STATE" = "down" ]; then
                    echo "$(ts): Error 1100 latch present but the execution service is DOWN — reporting execution, not a 1100 duration" >> "$LOG_FILE"
                    algo_alert_local "execution service is down; the Error 1100 latch cannot be trusted or cleared"
                    telegram "🚨 The execution service is DOWN. An IB Error 1100 latch is present, but execution is the only thing that writes or clears it, so its age means nothing — not reporting a 1100 duration. Fix the execution service. ($REPEAT_TEXT.)"
                else
                    if [ "$AGE" -gt "$CONN_AGE_CAP_SECS" ]; then
                        AGE_TEXT="over $((CONN_AGE_CAP_SECS / 3600))h — the latch is older than this cap, so treat the duration as a floor, not a measurement"
                    else
                        AGE_TEXT="~$((AGE / 60)) min"
                    fi
                    case "$DECISION" in
                        refuse\ *)
                            # A restart already failed, or would hammer a
                            # rejected login: say plainly that it is a human's.
                            echo "$(ts): IB Error 1100 sustained ${AGE}s (port $PORT up, execution $EXEC_STATE) — alerting; NOT restarting: $RESTART_NOTE" >> "$LOG_FILE"
                            # This page IS the outcome page; record it so no
                            # later branch reports this restart again.
                            [ "$(restart_marker_field outcome)" = "pending" ] && set_restart_marker_field outcome failed
                            telegram "🚨 IB Gateway lost server connectivity (Error 1100) for $AGE_TEXT — port $PORT is up but no market data / order routing. NOT restarting: $RESTART_NOTE. A human needs to look at the Gateway. ($REPEAT_TEXT until it recovers.)" ;;
                        *)
                            echo "$(ts): IB Error 1100 sustained ${AGE}s (port $PORT up, execution $EXEC_STATE) — alerting; not restarting yet: $RESTART_NOTE" >> "$LOG_FILE"
                            telegram "🚨 IB Gateway lost server connectivity (Error 1100) for $AGE_TEXT — port $PORT is up but no market data / order routing. Watchdog: $RESTART_NOTE. ($REPEAT_TEXT until it recovers.)" ;;
                    esac
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
    elif conn_restart_outcome recovered "✅ IB Gateway is back after the watchdog's Error 1100 restart (restarted at $(hhmm_of "$(restart_marker_field restarted_at)")): port $PORT is up and execution has cleared the latch."; then
        # KAN-118: execution reconnected to the restarted Gateway and cleared
        # the latch. That message replaces the generic all-clear below.
        rm -f "$CONN_ALERT_MARKER"
    elif [ -f "$CONN_ALERT_MARKER" ]; then
        # 1100 cleared (execution removed CONN_MARKER on 1102) after we alerted.
        echo "$(ts): IB connectivity restored (Error 1100 cleared)" >> "$LOG_FILE"
        telegram "✅ IB Gateway server connectivity restored (Error 1100 cleared)."
        rm -f "$CONN_ALERT_MARKER"
    fi
    exit 0
fi

# KAN-118: a restarted Gateway that never reopened its port. Paged once; the
# two-strike port-down path below still applies on its own terms.
R_AT="$(restart_marker_field restarted_at)"
case "$R_AT" in ''|*[!0-9]*) R_AT="" ;; esac
if [ -n "$R_AT" ] && [ $(( $(now_epoch) - R_AT )) -ge "$CONN_RESTART_VERIFY_SECS" ]; then
    conn_restart_outcome failed "🚨 The watchdog restarted the IB Gateway at $(hhmm_of "$R_AT") for a sustained Error 1100, and port $PORT is still DOWN $(( ($(now_epoch) - R_AT) / 60 )) min later. The port-down path takes over from here; check the Gateway." || true
fi

# Port is down. FIRST: is the Gateway stuck on a login rejection? Restarting
# into that state hammers IB with failed logins — refuse and alert instead.
LATEST_GW_LOG=$(ls -t "$LOG_DIR"/ibc-*.txt 2>/dev/null | head -1)
if ibc_login_rejected; then
    # Alert immediately, then RE-ALERT on the escalating schedule while
    # unresolved — a single missed message cost two paper-record days
    # (2026-07-07..09), and a flat 12h cadence cost the 2026-08-21 session.
    REALERT_SECS="$(auth_realert_secs)"
    if need_alert "$AUTH_MARKER" "$REALERT_SECS"; then
        read -r SECS_TO_RUN RUN_WDAY <<< "$(paper_run_countdown)"
        RUN_HHMM="$(printf '%02d:%02d' "$PAPER_RUN_HOUR" "$PAPER_RUN_MIN")"
        if [ "$SECS_TO_RUN" -ge 86400 ] && [ "$RUN_WDAY" != "-" ]; then
            # KAN-99: a day or more out, say WHICH run and how far in days —
            # "in 3438 min" to an unnamed 4:15 is how a weekend page came to
            # point at a Sunday run that does not exist.
            RUN_DAY_NAMES=(Sun Mon Tue Wed Thu Fri Sat)
            RUN_NAME="${RUN_DAY_NAMES[$RUN_WDAY]} ${RUN_HHMM}"
            RUN_IN="$(( SECS_TO_RUN / 86400 ))d $(( SECS_TO_RUN % 86400 / 3600 ))h"
            LOG_RUN="${RUN_IN} to the next paper run (${RUN_NAME})"
            MSG_RUN="The next paper run (${RUN_NAME}) is in ${RUN_IN}"
        else
            MINS_TO_RUN=$(( SECS_TO_RUN / 60 ))
            LOG_RUN="${MINS_TO_RUN} min to the ${PAPER_RUN_HOUR}:$(printf '%02d' "$PAPER_RUN_MIN") run"
            MSG_RUN="The ${PAPER_RUN_HOUR}:$(printf '%02d' "$PAPER_RUN_MIN") paper run is in ${MINS_TO_RUN} min"
        fi
        echo "$(ts): AUTH FAILURE in $LATEST_GW_LOG — refusing to kickstart; alerted operator (re-alert ${REALERT_SECS}s, ${LOG_RUN})" >> "$LOG_FILE"
        telegram "🚨 IB Gateway login is being REJECTED (port $PORT down). Watchdog will NOT restart it — repeated failed logins risk an IB lockout. Manual re-login needed. ${MSG_RUN} and will abort unless this is fixed."
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
if [ -f "$MARKER" ] && [ -n "$R_AT" ] && [ "$(restart_marker_field outcome)" = "pending" ] \
        && [ $(( $(now_epoch) - R_AT )) -lt "$CONN_RESTART_VERIFY_SECS" ]; then
    # KAN-118: the Gateway was kickstarted for a 1100 under
    # CONN_RESTART_VERIFY_SECS ago and may still be logging in. A second
    # kickstart now would restart one outage twice; the strike is kept, so the
    # first cycle past the window acts on it.
    echo "$(ts): port $PORT down 2 consecutive checks, but the Gateway was restarted for Error 1100 at $(hhmm_of "$R_AT") — holding the kickstart until $((CONN_RESTART_VERIFY_SECS / 60)) min after it" >> "$LOG_FILE"
elif [ -f "$MARKER" ]; then
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
