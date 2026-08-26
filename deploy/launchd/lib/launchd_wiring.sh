#!/bin/bash
# launchd wiring reconciliation for the algo-poc jobs.
#
# WHY THIS EXISTS (KAN-64)
# ------------------------
# The repo guarantees a plist is version-controlled and copied to
# ~/Library/LaunchAgents. Nothing checked that the job was actually LOADED.
# local.algo-evidence-digest sat on disk from 2026-08-17 and never ran once,
# because it was never bootstrapped — and both existing guards stayed green
# throughout:
#
#   test_every_plist_is_version_controlled  — passes, the plist is tracked
#   test_deploy_script_exists_...           — passes, and deliberately requires
#                                             that deploy.sh NOT run launchctl,
#                                             because bootout/bootstrap is a
#                                             human step (CLAUDE.md)
#
# So deploy.sh copies the file and prints the reload commands; if the operator
# does not run them the plist looks correct, the suite is green, and the job
# never fires. The job whose purpose was surfacing evidence gaps was itself an
# invisible gap for four days, across two missed Monday digests.
#
# WHERE THE CHECK CAN AND CANNOT LIVE
# -----------------------------------
# Not in pytest: CI runs on GitHub Actions where `launchctl list` is
# meaningless, and the suite is deliberately self-contained and unit-level
# (CLAUDE.md). A test that shelled out to launchctl would fail in CI or be
# skipped there — the same blind spot in a new costume. And not in the evidence
# digest, for the obvious reason: the digest is the job that was not loaded, and
# a check that runs only inside the thing being checked cannot detect its own
# absence.
#
# It therefore lives here and is called from a job that is already reliably
# running on the host — the 04:52 pipeline report, which already reads job logs
# and already alerts. deploy.sh calls it too, so its reload hint names the
# labels that are actually outstanding.
#
# SCOPE is `local.algo-*`. local.ibc-gateway is deliberately excluded: its plist
# is not in this repo (it is IBC's own), and its failure mode is not silent —
# an unloaded Gateway job means port 7497 goes unreachable, which the watchdog,
# the 04:15 paper run and the Tuesday refresh all already alert on.
#
# THIS FILE NEVER LOADS OR UNLOADS A JOB. It only reads `launchctl list` and
# prints the commands a human should run. bootout/bootstrap stays a human step.
#
# SOURCED BY PATH FROM THE REPO — never from ~/ibc — like secrets.sh and
# lib/telegram.sh, so there is one copy that cannot drift. Under lib/ so
# deploy.sh's `"$SRC"/*.sh` glob cannot plant a decoy copy.
#
# Expects: $ALGO_DIR — repo root (to find the canonical plists)
#
# Usage:
#   . "$ALGO_DIR/deploy/launchd/lib/launchd_wiring.sh"
#   algo_launchd_wiring_check
#   [ -n "$ALGO_LAUNCHD_UNLOADED" ] && telegram "..."

# Overridable so tests can inject a fake `launchctl list`, in the same shape as
# secrets.sh's $ALGO_SECURITY_BIN / $ALGO_OSASCRIPT_BIN. Production always takes
# the default; never export this in a login shell.
ALGO_LAUNCHCTL_BIN="${ALGO_LAUNCHCTL_BIN:-/bin/launchctl}"

# Where the live copies land. Overridable for the same reason.
ALGO_LAUNCH_AGENTS_DIR="${ALGO_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"

ALGO_LAUNCHD_LABEL_PREFIX="${ALGO_LAUNCHD_LABEL_PREFIX:-local.algo-}"

# Set by algo_launchd_wiring_check:
#   ALGO_LAUNCHD_UNLOADED  installed in LaunchAgents, absent from launchctl list
#   ALGO_LAUNCHD_ORPHANED  loaded, but no canonical plist in deploy/launchd/
#   ALGO_LAUNCHD_LOADED    installed and loaded (the healthy set)
#   ALGO_LAUNCHD_REPORT    multi-line "<label>: loaded|NOT LOADED" listing
ALGO_LAUNCHD_UNLOADED=""
ALGO_LAUNCHD_ORPHANED=""
ALGO_LAUNCHD_LOADED=""
ALGO_LAUNCHD_REPORT=""

# Labels launchd currently has bootstrapped, one per line. `launchctl list`
# prints "PID Status Label" with a header row; the label is the last field.
algo_launchd_loaded_labels() {
    "$ALGO_LAUNCHCTL_BIN" list 2>/dev/null \
        | awk 'NR > 1 { print $NF }' \
        | grep "^${ALGO_LAUNCHD_LABEL_PREFIX}" || true
}

# Labels whose plist is installed in ~/Library/LaunchAgents, one per line.
algo_launchd_installed_labels() {
    local f
    for f in "$ALGO_LAUNCH_AGENTS_DIR/${ALGO_LAUNCHD_LABEL_PREFIX}"*.plist; do
        [ -e "$f" ] || continue
        basename "$f" .plist
    done
}

# Labels the repo declares as canonical, one per line.
algo_launchd_canonical_labels() {
    local f
    for f in "${ALGO_DIR:-.}/deploy/launchd/${ALGO_LAUNCHD_LABEL_PREFIX}"*.plist; do
        [ -e "$f" ] || continue
        basename "$f" .plist
    done
}

# Populate the ALGO_LAUNCHD_* variables. Always returns 0; the caller decides
# what a mismatch means for it.
algo_launchd_wiring_check() {
    ALGO_LAUNCHD_UNLOADED=""
    ALGO_LAUNCHD_ORPHANED=""
    ALGO_LAUNCHD_LOADED=""
    ALGO_LAUNCHD_REPORT=""

    local loaded installed canonical label
    loaded="$(algo_launchd_loaded_labels)"
    installed="$(algo_launchd_installed_labels)"
    canonical="$(algo_launchd_canonical_labels)"

    while read -r label; do
        [ -n "$label" ] || continue
        case $'\n'"$loaded"$'\n' in
            *$'\n'"$label"$'\n'*)
                ALGO_LAUNCHD_LOADED="$ALGO_LAUNCHD_LOADED $label"
                ALGO_LAUNCHD_REPORT="$ALGO_LAUNCHD_REPORT$label: loaded"$'\n'
                ;;
            *)
                ALGO_LAUNCHD_UNLOADED="$ALGO_LAUNCHD_UNLOADED $label"
                ALGO_LAUNCHD_REPORT="$ALGO_LAUNCHD_REPORT$label: NOT LOADED"$'\n'
                ;;
        esac
    done <<< "$installed"

    # The reverse drift: a job launchd is still running whose definition has
    # left the repo. The per-wrapper `cmp -s "$0" "$CANON"` guard catches this
    # for scripts; nothing caught it for jobs.
    while read -r label; do
        [ -n "$label" ] || continue
        case $'\n'"$canonical"$'\n' in
            *$'\n'"$label"$'\n'*) ;;
            *)
                ALGO_LAUNCHD_ORPHANED="$ALGO_LAUNCHD_ORPHANED $label"
                ALGO_LAUNCHD_REPORT="$ALGO_LAUNCHD_REPORT$label: loaded but NOT IN REPO (deploy/launchd/)"$'\n'
                ;;
        esac
    done <<< "$loaded"

    ALGO_LAUNCHD_UNLOADED="${ALGO_LAUNCHD_UNLOADED# }"
    ALGO_LAUNCHD_ORPHANED="${ALGO_LAUNCHD_ORPHANED# }"
    ALGO_LAUNCHD_LOADED="${ALGO_LAUNCHD_LOADED# }"
    return 0
}

# Print the bootout/bootstrap pair a human must run for each unloaded label.
# PRINTS ONLY — see the header. Call after algo_launchd_wiring_check.
algo_launchd_bootstrap_hint() {
    [ -n "$ALGO_LAUNCHD_UNLOADED" ] || return 0
    local label
    echo "These jobs are installed but NOT LOADED — launchd will never run them."
    echo "Bootstrap each one yourself (launchctl is a human step — CLAUDE.md):"
    for label in $ALGO_LAUNCHD_UNLOADED; do
        echo "    launchctl bootout   gui/\$(id -u)/$label 2>/dev/null; \\"
        echo "    launchctl bootstrap gui/\$(id -u) $ALGO_LAUNCH_AGENTS_DIR/$label.plist; \\"
        echo "    launchctl list | grep $label"
    done
}
