#!/usr/bin/env bash
# Sync the canonical launchd wrappers + plists from this repo to their live
# locations on the operator's host. Replaces the error-prone manual per-file
# `cp` that let ~/ibc/run_divergence.sh drift to a pre-T3 revision (old default
# creds) and fail auth on the 2026-08-11 cold boot.
#
#   *.sh    in deploy/launchd/ -> ~/ibc/<name>            (chmod +x)
#   *.plist in deploy/launchd/ -> ~/Library/LaunchAgents/<name>
#
# Idempotent: unchanged files are skipped. For every file that WOULD change it
# prints a diff. It performs the file copies (safe) but never runs launchctl —
# (re)loading a job is `launchctl bootout/bootstrap`, which CLAUDE.md reserves
# for a human. For any plist that changed, it prints the exact reload commands
# for you to run.
#
# Usage:
#   deploy/launchd/deploy.sh            # apply (copy changed files)
#   deploy/launchd/deploy.sh --dry-run  # show what would change, copy nothing
set -uo pipefail

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

ALGO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC="$ALGO_DIR/deploy/launchd"
IBC="$HOME/ibc"
LA="$HOME/Library/LaunchAgents"

# launchd wiring reconciliation (KAN-64), so the reload hint below can name the
# jobs that are ACTUALLY unloaded rather than printing a generic list of the
# ones whose file happened to change. The lib only ever reads `launchctl list`;
# bootout/bootstrap remains a human step, which is why this script prints those
# commands instead of running them.
ALGO_LAUNCH_AGENTS_DIR="$LA"
# shellcheck source=deploy/launchd/lib/launchd_wiring.sh
. "$SRC/lib/launchd_wiring.sh"

[ "$DRY_RUN" = "1" ] && echo "== deploy.sh (dry-run: no files will be written) ==" \
                     || echo "== deploy.sh (applying) =="
echo "  source: $SRC"

# AC#16 (iron rule): --dry-run must leave the filesystem untouched. This mkdir
# used to run unconditionally, so previewing a deploy on a fresh host silently
# created ~/ibc and ~/Library/LaunchAgents — a "read-only" command with a
# write side effect. Only create the destinations when actually applying.
if [ "$DRY_RUN" = "0" ]; then
    mkdir -p "$IBC" "$LA" 2>/dev/null || true
fi

changed=0
reload_labels=()

sync_one() {
    # $1 = source path, $2 = destination path, $3 = "exec" to chmod +x
    local src="$1" dst="$2" mode="${3:-}"
    if [ ! -f "$src" ]; then
        return
    fi
    if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
        return  # identical — nothing to do
    fi
    changed=$((changed + 1))
    if [ -f "$dst" ]; then
        echo ""
        echo "CHANGED: $dst"
        diff -u "$dst" "$src" 2>/dev/null | sed 's/^/    /' || true
    else
        echo ""
        echo "NEW:     $dst"
    fi
    if [ "$DRY_RUN" = "0" ]; then
        cp "$src" "$dst"
        [ "$mode" = "exec" ] && chmod +x "$dst"
    fi
    # If this is a plist, remember its label for the reload hint.
    case "$dst" in
        *.plist)
            reload_labels+=("$(basename "$dst" .plist)")
            ;;
    esac
}

# Wrappers -> ~/ibc (executable)
for f in "$SRC"/*.sh; do
    [ -e "$f" ] || continue
    [ "$(basename "$f")" = "deploy.sh" ] && continue   # don't deploy the deployer
    # secrets.sh is SOURCED BY PATH from the repo, so a copy in ~/ibc would
    # never be executed. Deploying it would plant exactly the stale-copy trap
    # that broke the 2026-08-11 cold boot: an operator edits ~/ibc/secrets.sh,
    # sees no effect, and the real logic silently stays behind.
    [ "$(basename "$f")" = "secrets.sh" ] && continue
    # deadman.sh, same reason: sourced by path from the repo, never executed
    # from ~/ibc. A deployed copy would only ever be a decoy.
    [ "$(basename "$f")" = "deadman.sh" ] && continue
    sync_one "$f" "$IBC/$(basename "$f")" exec
done

# launchd job definitions -> ~/Library/LaunchAgents
for f in "$SRC"/*.plist; do
    [ -e "$f" ] || continue
    sync_one "$f" "$LA/$(basename "$f")"
done

# What is installed but never bootstrapped? This is the check that was missing
# on 2026-08-17: local.algo-evidence-digest.plist was copied here, the suite was
# green, and the job never ran for four days because nobody ran the commands
# this script printed. Reported whether or not anything changed — an in-sync
# tree with an unloaded job is exactly the state that hid it.
algo_launchd_wiring_check
if [ -n "$ALGO_LAUNCHD_UNLOADED" ] || [ -n "$ALGO_LAUNCHD_ORPHANED" ]; then
    echo ""
    echo "== launchd wiring =="
    printf '%s' "$ALGO_LAUNCHD_REPORT"
fi
if [ -n "$ALGO_LAUNCHD_UNLOADED" ]; then
    echo ""
    algo_launchd_bootstrap_hint
fi

echo ""
if [ "$changed" = "0" ]; then
    echo "Everything is already in sync. Nothing to do."
    exit 0
fi

if [ "$DRY_RUN" = "1" ]; then
    echo "$changed file(s) would change. Re-run without --dry-run to apply."
    exit 0
fi

echo "$changed file(s) synced."
if [ "${#reload_labels[@]}" -gt 0 ]; then
    echo ""
    echo "A plist changed. Reload each affected job yourself (launchctl is a"
    echo "human step — CLAUDE.md):"
    for label in "${reload_labels[@]}"; do
        echo "    launchctl bootout   gui/\$(id -u)/$label 2>/dev/null; \\"
        echo "    launchctl bootstrap gui/\$(id -u) $LA/$label.plist; \\"
        echo "    launchctl list | grep $label"
    done
fi
