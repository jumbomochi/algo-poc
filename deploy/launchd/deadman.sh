#!/bin/bash
# KAN-15 (P1-12) — the one check nothing inside this host can make.
#
# WHY THIS EXISTS
# ---------------
# Every other check in this repo runs on the machine it is monitoring:
# Prometheus, Alertmanager, the container healthchecks, the launchd wrappers'
# own Telegram alerts. All of them share one blind spot — if the Mac is
# asleep, off, or has lost its network, the thing that was supposed to shout
# is the thing that is gone. A monitor cannot report its own absence.
#
# On 2026-08-13 and 2026-08-14 the 04:15 paper run aborted silently and it
# took two days to notice (see deploy/launchd/secrets.sh for the FIFO root
# cause). KAN-16 fixed the silence for the case where the wrapper *runs* and
# fails. This file covers the case where the wrapper never runs at all —
# which looks, from inside, exactly like a quiet healthy night.
#
# The mechanism is a dead-man's switch: a successful run pings an external
# checker (healthchecks.io or equivalent), and the CHECKER pages when the
# ping does not arrive by its deadline. Nothing here has to detect anything;
# the absence of a message is the message.
#
#   ALGO_DEADMAN_PAPER_URL — pinged by run_paper.sh on a successful run.
#     Configure the external check with a period of ~26h: one missed trading
#     day pages, and a normal weekend does not (see "weekends" below).
#
# There is a second, wider dead-man switch that is NOT this file's job:
# config/alert_rules.yml's always-firing Watchdog alert, which Alertmanager
# pings out every 5 minutes and which covers "the whole observability stack
# stopped". This one is narrower and more specific: "the host is up, but the
# trading run did not happen".
#
# FIRE AND FORGET, ALWAYS
# -----------------------
# Every function here returns 0. A dead-man ping is a report *about* the run;
# it must never be able to change the run's outcome. A flaky network, an
# expired check URL or a missing curl must not turn a successful trading day
# into a failed one — that would be monitoring causing the outage it exists
# to detect. Callers log $ALGO_DEADMAN_STATUS instead of branching on a
# return code.
#
# WEEKENDS AND HOLIDAYS
# ---------------------
# run_paper.sh runs every day, including weekends, and exits 0 on a
# non-trading day (it simply commits no signals). So the ping arrives daily
# and a ~26h external period is correct — deliberately NOT gated on the NYSE
# calendar here, because a calendar bug would silence the dead-man switch,
# which is the one failure mode it must not have.
#
# Usage (sourced, after secrets.sh):
#   . "$ALGO_DIR/deploy/launchd/deadman.sh"
#   algo_deadman_ping "$EXIT_CODE"
#   echo "$(date): dead-man: $ALGO_DEADMAN_STATUS" >> "$LOG_FILE"

# Human-readable outcome of the last algo_deadman_ping, for the caller to log.
ALGO_DEADMAN_STATUS=""

# Resolved URL from the last lookup. Never logged: a healthchecks.io URL is a
# bearer capability — anyone holding it can forge a healthy ping and turn the
# switch off permanently.
_ALGO_DEADMAN_URL=""

# Redact a check URL down to something safe to write to a log file: scheme,
# host, and the fact that a path was present. Enough to tell two checks apart
# and to spot a typo'd host; not enough to impersonate the run.
_algo_deadman_redact() {
    printf '%s' "$1" | sed -E 's#^(https?://[^/]+)/.*#\1/<redacted>#'
}

# Resolve the ping URL for $1 (a variable name), setting $_ALGO_DEADMAN_URL.
#
# Environment first, then the keychain/.env loader. The env-var branch exists
# so an operator (or the test suite) can point a single invocation at a
# different check without touching the keychain; under launchd the
# environment is empty, so production always takes the keychain path.
# Requires deploy/launchd/secrets.sh to have been sourced already.
algo_deadman_url_for() {
    local name="$1"
    _ALGO_DEADMAN_URL=""
    eval "local from_env=\"\${$name:-}\""
    if [ -n "$from_env" ]; then
        _ALGO_DEADMAN_URL="$from_env"
        return 0
    fi
    if command -v algo_secret_into >/dev/null 2>&1 && algo_secret_into "$name"; then
        _ALGO_DEADMAN_URL="$_ALGO_SECRET_VALUE"
        return 0
    fi
    return 1
}

# algo_deadman_ping EXIT_CODE [URL_VAR_NAME]
#
# Pings the dead-man URL if and only if EXIT_CODE is 0. A failed run must NOT
# ping: the external checker's whole job is to page when a healthy signal
# stops arriving, and a wrapper that pinged unconditionally would report a
# crashed run as a healthy one — a monitor that lies is worse than no monitor.
# (The wrapper's own Telegram/local alerts cover the "ran and failed" case.)
algo_deadman_ping() {
    local exit_code="${1:-1}" name="${2:-ALGO_DEADMAN_PAPER_URL}" url

    if [ "$exit_code" != "0" ]; then
        ALGO_DEADMAN_STATUS="not pinged (run exited $exit_code) — the external check will page when its deadline passes"
        return 0
    fi

    if ! algo_deadman_url_for "$name"; then
        ALGO_DEADMAN_STATUS="NOT CONFIGURED: $name is in neither the environment nor the keychain, so nothing outside this host can tell that the run happened. Import it: deploy/launchd/secrets.sh --import"
        return 0
    fi
    url="$_ALGO_DEADMAN_URL"

    case "$url" in
        http://*|https://*) ;;
        *)
            ALGO_DEADMAN_STATUS="NOT PINGED: $name is not an http(s) URL"
            return 0
            ;;
    esac

    # --retry covers the cold-boot case where the run finishes before the
    # network is fully up. -f so an HTTP error is a failure, not a silent
    # success against a deleted check.
    if curl -fsS -m 10 --retry 3 --retry-delay 2 -o /dev/null "$url" >/dev/null 2>&1; then
        ALGO_DEADMAN_STATUS="pinged $(_algo_deadman_redact "$url")"
    else
        ALGO_DEADMAN_STATUS="PING FAILED to $(_algo_deadman_redact "$url") — the external check will page as if the run had not happened"
    fi
    return 0
}
