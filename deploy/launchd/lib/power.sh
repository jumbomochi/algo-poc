# Power assertion for scheduled runs — KAN-77.
#
# WHY
# ---
# On 2026-09-09 the host was found with `pmset sleep 1` — idle sleep after ONE
# minute — and 57 sleep events in a day, 37 of them 'Dark Wake Thermal
# Emergency'. launchd wakes the machine to fire a StartCalendarInterval job, but
# nothing kept it awake once the job was running, and these jobs run for minutes
# to hours. A single 04:15 paper run logged 39 x Error 1100 (IB connectivity
# lost) against 38 x Error 1102 (restored), 69 x Error 322, and 87 consecutive
# zero-bar fetches at ~61s each. It reached ticker 88 of 140 in fifteen hours
# with nothing usable, so the 04:47 divergence monitor exited 3 (BLIND).
#
# `sudo pmset -a sleep 0` fixes the host and has been applied. This exists
# because that is a HOST setting: it does not survive a new machine, a restored
# backup, or someone changing power policy for an unrelated reason. The manual
# runbook already invokes long jobs under `caffeinate -is`; the scheduled ones
# should not be weaker than the manual ones.
#
# WHY RE-EXEC AND NOT A BACKGROUND CAFFEINATE
# -------------------------------------------
# `exec caffeinate -is "$0"` makes caffeinate the PARENT of the run, so the
# assertion is bound to the process and released by exit. There is no cleanup
# path to forget and no way for it to outlive the job. A backgrounded
# `caffeinate &` would have to be released on every abort path — and on
# 2026-09-09 three orphaned caffeinate processes were found still holding
# assertions after their runs had been killed, which is exactly that failure.
#
# caffeinate propagates the child's exit status verbatim (verified for
# 0/1/2/143), so every wrapper's exit-code contract and its single dead-man
# decision are unaffected by the re-exec.
#
# Sourced by the long-running wrappers and called immediately after their log
# directory exists and before they announce themselves — an assertion taken
# after the work begins is one the sleep has already got past.
#
# gateway_watchdog.sh deliberately does NOT use this: it is a StartInterval job
# that runs for a couple of seconds, and re-execing it 288 times a day is cost
# with no benefit.

# Re-execute the calling script under a power assertion. Call as:
#     algo_hold_power_assertion "$@"
# Returns normally (without re-execing) when the assertion is already held or
# when no caffeinate binary exists.
algo_hold_power_assertion() {
    # Already inside the re-exec. Without this the exec would recurse forever.
    [ -n "${ALGO_POWER_HELD:-}" ] && return 0

    # Overridable only so tests can substitute a recording stub; production
    # always takes the default. Never export it in a login shell.
    local bin="${ALGO_CAFFEINATE_BIN:-caffeinate}"

    if ! command -v "$bin" >/dev/null 2>&1; then
        # NOT fatal. A missing power tool must never stop a trading run — that
        # would be monitoring causing the outage it exists to prevent. The same
        # rule the dead-man ping follows.
        echo "$(date): WARNING - '$bin' not found, so this run holds no power assertion;" \
             "if the host sleeps mid-run the IB connection is severed and historical" \
             "data requests time out (KAN-77). Running anyway." >> "${LOG_FILE:-/dev/null}"
        return 0
    fi

    export ALGO_POWER_HELD=1
    # -i prevents idle sleep, -s prevents system sleep. -i alone still allows a
    # maintenance or thermal wake to put the machine down, which is where 37 of
    # the 57 sleeps on 2026-09-09 came from.
    exec "$bin" -is "$0" "$@"
}
