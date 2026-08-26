#!/bin/bash
# Docker engine + compose-stack liveness for the algo-poc launchd wrappers.
#
# WHY THIS EXISTS (KAN-66)
# ------------------------
# On 2026-08-20 the Docker *engine* died while Docker Desktop's Electron GUI
# stayed alive. `docker ps` failed, every container was gone, and the app looked
# healthy: com.docker.backend, com.docker.virtualization, vpnkit and dockerd were
# all absent while /Applications/Docker.app and vmnetd were still RUNNING, and
# the socket file still existed (dated 10 days earlier). Three scheduled jobs
# failed the next morning — the divergence monitor on port 55432, the DB backup
# on a missing postgres container, and the watchdog on a 1100 latch nothing was
# left to clear — each alerting accurately about its own symptom and none naming
# the cause.
#
# So the check is `docker info` SUCCEEDING, never a process-name match: the
# process was present and the daemon was not. That is the exact 2026-08-21 state
# and any liveness check built on `pgrep Docker` would have called it healthy.
#
# `docker info` succeeding is necessary and not sufficient. After the 08-21
# restart, 10 of 11 algo-poc containers came up healthy and portfolio-accounting
# was left crash-looping (KAN-61). A daemon-only check calls that healthy too, so
# the expected compose services are compared against what is actually running.
#
# IT NEVER REMEDIATES. A dead engine needed process kills and an app relaunch,
# which is not something to automate against a live trading host on a 5-minute
# timer. This file contains no restart, kill, or open invocation, and
# tests/deploy/test_gateway_watchdog.py asserts that it stays that way. If
# auto-restart is ever wanted it belongs in its own story with its own guardrails.
#
# SOURCED BY PATH FROM THE REPO — never from ~/ibc — for the same reason as
# secrets.sh and lib/telegram.sh: exactly one copy of the logic, which therefore
# cannot drift. Living under lib/ keeps it out of deploy.sh's `"$SRC"/*.sh` glob
# so no decoy copy can be planted in ~/ibc.
#
# Expects: $ALGO_DIR — repo root (for `docker compose`)
#          $LOG_FILE — optional; diagnostics are appended when set
#
# Usage:
#   . "$ALGO_DIR/deploy/launchd/lib/docker_health.sh"
#   algo_docker_check
#   case "$ALGO_DOCKER_STATUS" in daemon-down|services-down) ... ;; esac

# Compose project name. Containers are named algo-poc-<service>-1, so this is
# the label every one of them carries.
ALGO_COMPOSE_PROJECT="${ALGO_COMPOSE_PROJECT:-algo-poc}"

# Compose services that RUN ONCE AND EXIT, and whose `exited` state is therefore
# healthy. `migrate` runs `alembic upgrade head` at stack start and sits at
# "Exited (0)" for the rest of the stack's life; without this it would be
# reported as a fault on literally every cycle, and an alert that fires every
# five minutes forever is worse than no alert at all.
#
# A NON-ZERO exit is still a fault even here — a failed migration is exactly the
# thing worth paging about. And a one-shot added later and not listed here
# alerts until it is added, which is the safe direction to be wrong in.
ALGO_COMPOSE_ONESHOT_SERVICES="${ALGO_COMPOSE_ONESHOT_SERVICES:-migrate}"

# The docker CLI, resolved once at source time. launchd starts jobs with a PATH
# that lacks /usr/local/bin, where Docker Desktop installs the client, and not
# every wrapper that needs this lib rewrites its PATH — so the fallbacks are
# spelled out rather than assumed. Overridable for tests, which also get picked
# up by the `command -v` branch when they stub via PATH.
#
# Setting it to the EMPTY STRING explicitly (as opposed to leaving it unset)
# disables the docker checks entirely and makes every verdict "unknown" — the
# right behaviour on a host with no container runtime, which must not be paged
# for an outage it cannot have. Hence `${VAR+set}` rather than `-z "$VAR"`.
if [ -z "${ALGO_DOCKER_BIN+set}" ]; then
    for _algo_docker_candidate in \
        "$(command -v docker 2>/dev/null || true)" \
        /usr/local/bin/docker \
        /opt/homebrew/bin/docker
    do
        if [ -n "$_algo_docker_candidate" ] && [ -x "$_algo_docker_candidate" ]; then
            ALGO_DOCKER_BIN="$_algo_docker_candidate"
            break
        fi
    done
    unset _algo_docker_candidate
fi
ALGO_DOCKER_BIN="${ALGO_DOCKER_BIN:-}"

# Set by algo_docker_check:
#   ALGO_DOCKER_STATUS  ok | daemon-down | services-down | unknown
#   ALGO_DOCKER_DETAIL  one-line human summary, safe to put in a Telegram message
#   ALGO_DOCKER_BAD     space-separated service names that are not healthy
ALGO_DOCKER_STATUS="unknown"
ALGO_DOCKER_DETAIL=""
ALGO_DOCKER_BAD=""

_algo_docker_log() {
    [ -n "${LOG_FILE:-}" ] || return 0
    if declare -F ts >/dev/null 2>&1; then
        echo "$(ts): $*" >> "$LOG_FILE"
    else
        echo "$(date): $*" >> "$LOG_FILE"
    fi
}

# Is the daemon answering? `docker info` does a round trip to dockerd; `docker
# --version` and a socket stat() both pass while the daemon is dead (the socket
# file outlived the engine by ten days on 08-21).
algo_docker_daemon_ok() {
    [ -n "$ALGO_DOCKER_BIN" ] || return 1
    "$ALGO_DOCKER_BIN" info >/dev/null 2>&1
}

# Services docker-compose.yml declares, one per line. Best-effort: the compose
# file interpolates ${POSTGRES_PASSWORD:?} / ${REDIS_PASSWORD:?}, so this fails
# when the keychain is locked. A failure degrades to "compare nothing", never to
# a false healthy — the observed-container scan below still runs.
algo_docker_expected_services() {
    [ -n "$ALGO_DOCKER_BIN" ] || return 0
    (cd "${ALGO_DIR:-.}" 2>/dev/null && "$ALGO_DOCKER_BIN" compose config --services 2>/dev/null) | sed '/^$/d'
}

# Every container in the compose project: "<service>|<state>|<status>".
# `docker ps` (not `docker compose ps`) because its Go-template --format is
# stable across compose versions and needs no env interpolation. --all so a
# crash-looping or exited container is seen rather than silently absent.
algo_docker_observed_containers() {
    [ -n "$ALGO_DOCKER_BIN" ] || return 0
    "$ALGO_DOCKER_BIN" ps --all \
        --filter "label=com.docker.compose.project=$ALGO_COMPOSE_PROJECT" \
        --format '{{.Label "com.docker.compose.service"}}|{{.State}}|{{.Status}}' \
        2>/dev/null | sed '/^|/d;/^$/d'
}

# Populate ALGO_DOCKER_STATUS / _DETAIL / _BAD. Always returns 0 — the caller
# decides what an unhealthy stack means for it.
algo_docker_check() {
    ALGO_DOCKER_STATUS="unknown"
    ALGO_DOCKER_DETAIL=""
    ALGO_DOCKER_BAD=""

    if [ -z "$ALGO_DOCKER_BIN" ]; then
        ALGO_DOCKER_STATUS="unknown"
        ALGO_DOCKER_DETAIL="docker CLI not found (not on PATH, /usr/local/bin or /opt/homebrew/bin)"
        return 0
    fi

    if ! algo_docker_daemon_ok; then
        ALGO_DOCKER_STATUS="daemon-down"
        ALGO_DOCKER_DETAIL="docker daemon is not responding (\`docker info\` failed) — every container is down"
        return 0
    fi

    local observed expected seen="" bad="" svc state status
    observed="$(algo_docker_observed_containers)"

    while IFS='|' read -r svc state status; do
        [ -n "$svc" ] || continue
        seen="$seen $svc"
        # `restarting` is the crash loop; `exited`/`dead`/`created`/`paused` are
        # all not-running. Only `running` is a candidate for healthy — except
        # for a one-shot that has already done its job and exited cleanly.
        if [ "$state" != "running" ]; then
            case " $ALGO_COMPOSE_ONESHOT_SERVICES " in
                *" $svc "*)
                    case "$status" in
                        *"Exited (0)"*) continue ;;   # ran, succeeded, done
                    esac
                    ;;
            esac
            bad="$bad $svc($state)"
            continue
        fi
        # Health, when the image declares a healthcheck, rides in .Status as
        # "Up 2 hours (healthy)" / "(unhealthy)" / "(health: starting)". No
        # parenthesis at all means no healthcheck — running is all we can ask.
        case "$status" in
            *"(unhealthy)"*)       bad="$bad $svc(unhealthy)" ;;
            *"(health: starting)"*) bad="$bad $svc(health-starting)" ;;
        esac
    done <<< "$observed"

    # A service with no container at all is invisible to the scan above, and is
    # exactly what "10 of 11 came up" looks like.
    expected="$(algo_docker_expected_services)"
    if [ -n "$expected" ]; then
        while read -r svc; do
            [ -n "$svc" ] || continue
            case " $seen " in
                *" $svc "*) ;;
                *) bad="$bad $svc(no-container)" ;;
            esac
        done <<< "$expected"
    else
        _algo_docker_log "WARNING - could not read \`docker compose config --services\`; a missing container cannot be detected this cycle"
    fi

    ALGO_DOCKER_BAD="${bad# }"
    if [ -n "$ALGO_DOCKER_BAD" ]; then
        ALGO_DOCKER_STATUS="services-down"
        ALGO_DOCKER_DETAIL="docker daemon is up but these compose services are not healthy: $ALGO_DOCKER_BAD"
    else
        ALGO_DOCKER_STATUS="ok"
        ALGO_DOCKER_DETAIL="docker daemon up; all compose services running"
    fi
    return 0
}

# True when the named compose service has a *running* container. Used by the
# Error 1100 latch check (KAN-63): the latch's only clearer is the execution
# service, so a latch is untrustworthy while execution is not running.
# Returns 2 (neither true nor false) when the daemon cannot be asked at all, so
# a caller can tell "execution is down" from "we cannot know".
algo_docker_service_running() {
    local want="$1" svc state status
    [ -n "$ALGO_DOCKER_BIN" ] || return 2
    algo_docker_daemon_ok || return 2
    while IFS='|' read -r svc state status; do
        [ "$svc" = "$want" ] || continue
        [ "$state" = "running" ] && return 0
    done <<< "$(algo_docker_observed_containers)"
    return 1
}

# Why a bounded wait on a docker-published port timed out, in one clause the
# operator can act on (KAN-66 item 4). run_paper.sh and run_divergence.sh both
# wait on 55432 and both aborted correctly on 2026-08-21 — but they named the
# PORT, and "paper DB not reachable on 55432 after 300s" pointed the operator at
# postgres when the whole hypervisor was gone. Naming the daemon is what
# separates "this one container did not come up" from "nothing is running".
algo_docker_wait_hint() {
    if [ -z "$ALGO_DOCKER_BIN" ]; then
        echo "the docker CLI could not be found, so the stack's state is unknown"
    elif ! algo_docker_daemon_ok; then
        echo "the docker daemon is NOT RESPONDING (\`docker info\` failed) — the whole stack is down, not just this port"
    else
        echo "the docker daemon is up, so this is the container publishing the port, not the engine"
    fi
}
