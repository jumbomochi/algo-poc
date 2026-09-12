# Bounded execution — KAN-75.
#
# One implementation of "run this with a deadline", because there were three and
# each had its own bugs:
#
#   docker_health.sh   _algo_docker_bounded      (KAN-70)
#   branch_guard.sh    _algo_bounded             a `command -v` guarded copy
#   run_backtest_refresh.sh                      an inline sleep-counting loop
#
# THE WALL CLOCK IS THE POINT
# ---------------------------
# The refresh bounded its backtest at 6h by counting its own sleeps:
#
#     waited=0
#     while [ "$waited" -lt "$REFRESH_TIMEOUT" ]; do
#         kill -0 "$BACKTEST_PID" || exit 0
#         sleep 5
#         waited=$((waited + 5))
#     done
#
# `sleep` is suspended when the machine suspends, so on a host that sleeps
# `waited` counts AWAKE seconds while the deadline it defends is a wall-clock
# one. On 2026-09-08 a refresh started 06:30:27 with a 6h bound was still
# running at 19:12 — 6h42m past its deadline, watchdog alive, never fired. It
# had 16.29s of CPU across 12h42m and was stalled at ticker 473 of 826 with each
# fetch returning 0 bars after ~25 minutes: unrecoverable, and the bound existed
# to end it.
#
# The sleeping host has its own fix (KAN-77). This is separate: a deadline is a
# promise about wall time, and the same drift accumulates with no suspension at
# all — 4,320 iterations of `sleep 5` plus a `kill -0` and loop overhead is
# strictly more than 21600s, just slower to matter.
#
# THREE TRAPS THIS ENCODES SO THEY ARE NOT REDISCOVERED
# -----------------------------------------------------
# 1. BOTH background jobs must redirect stdout. Command substitution does not
#    return until every inherited descriptor is closed, so a killer holding the
#    pipe makes `$(algo_run_bounded ...)` wait out the entire bound — turning
#    the guard into the stall it exists to prevent. This bit while writing
#    KAN-70's fix and is why the redirections are not optional.
#
# 2. SIGTERM then SIGKILL. A process wedged on a socket read may not take a
#    polite signal, and the whole point is that it stops.
#
# 3. THE WHOLE PROCESS GROUP, NOT JUST THE CHILD. Signalling the child pid alone
#    is not enough, and "the call sites all run a single executable" is not true:
#    `git ls-remote https://...` forks a `git-remote-https` helper, and
#    `docker compose` forks too. The helper inherits the same stdout, so killing
#    only the parent leaves the pipe held open — and a `$(algo_run_bounded ...)`
#    then waits for the helper's own network timeout (~75s against a blackholed
#    address) with the bound long since fired. The bound looked like it worked;
#    the caller still hung. So the child is started in its own process group
#    (`set -m`) and the group is signalled.
#
# Not used by power.sh, deliberately: that re-execs under caffeinate, so the
# assertion is released by process exit and there is nothing to bound.

#: Seconds between liveness checks. Small enough that a finished child is reaped
#: promptly, large enough that a 6h bound is not 4,320 wakeups of pure overhead.
ALGO_BOUNDED_POLL_SECONDS="${ALGO_BOUNDED_POLL_SECONDS:-5}"
#: Grace between SIGTERM and SIGKILL.
ALGO_BOUNDED_KILL_GRACE_SECONDS="${ALGO_BOUNDED_KILL_GRACE_SECONDS:-20}"

# algo_run_bounded <seconds> <command> [args...]
#
# Runs the command with a hard wall-clock deadline. Stdout is passed through.
# Returns the command's own exit status, or 124 on expiry — timeout(1)'s
# conventional code, and one the commands used here cannot produce themselves.
#
# Implemented with a killer subshell rather than timeout(1): production is macOS,
# which does not ship it, and CI is ubuntu, which does. A chain that silently
# behaves differently on the two is how the `stat -f %m` bug happened.
algo_run_bounded() {
    local secs="$1"; shift

    # The child INHERITS stdout and stderr; the caller decides where they go.
    # Deliberately not buffered to a temp file and dumped at the end: the
    # backtest streams hours of progress into its log and an operator greps it
    # live for "[473/826]". Buffering would make a six-hour job silent until it
    # finished, which is the opposite of what a bound is for.
    #
    # Inheriting is also safe for `$(algo_run_bounded ...)`: the substitution
    # returns once every writer to the pipe closes. The killer below has its
    # descriptors redirected away, and anything the child forked is killed with
    # it — those are the only other writers.
    #
    # Job control gives the child its own process group, so the killer can
    # signal the group and reach helper processes the child forked. Restored
    # immediately: `set -m` in a script also changes how later background jobs
    # are reaped and reported, and this is a sourced lib — it does not get to
    # leave the caller's shell modified.
    local had_monitor=0
    case "$-" in *m*) had_monitor=1 ;; esac
    set -m
    "$@" &
    local pid=$!
    [ "$had_monitor" = "1" ] || set +m

    # The deadline is read from the clock on every pass, so a suspended host
    # resumes with the deadline where it always was rather than where the
    # accumulated sleeps think it is.
    (
        local start now
        start="$(date +%s)"
        while :; do
            kill -0 "$pid" 2>/dev/null || exit 0
            now="$(date +%s)"
            [ $(( now - start )) -ge "$secs" ] && break
            sleep "$ALGO_BOUNDED_POLL_SECONDS"
        done
        kill -0 "$pid" 2>/dev/null || exit 0
        # `-$pid` is the process group. The fallback matters: if job control was
        # unavailable the child shares our group, and `kill -TERM -$$` would
        # take down the job itself.
        kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
        sleep "$ALGO_BOUNDED_KILL_GRACE_SECONDS"
        kill -KILL -"$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null
    ) >/dev/null 2>&1 &
    local killer=$!

    local rc=0
    wait "$pid" 2>/dev/null || rc=$?

    # Reap the killer so a child that finished early leaves nothing behind: a
    # stray killer would fire at an unrelated pid later, and this runs every
    # five minutes.
    kill "$killer" 2>/dev/null
    wait "$killer" 2>/dev/null

    # 128+n means killed by signal n. Either signal here means the deadline
    # fired, and the caller wants one code for that, not two.
    [ "$rc" -ge 128 ] && return 124
    return "$rc"
}
