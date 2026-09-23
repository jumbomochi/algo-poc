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
#   deploy/launchd/deploy.sh                  # apply (copy changed files)
#   deploy/launchd/deploy.sh --dry-run        # show what would change, copy nothing
#   deploy/launchd/deploy.sh --from-any-tree  # override the tree guard (logged)
set -uo pipefail

DRY_RUN=0
FROM_ANY_TREE=0
for arg in "$@"; do
    case "$arg" in
        --dry-run)       DRY_RUN=1 ;;
        --from-any-tree) FROM_ANY_TREE=1 ;;
        *) echo "deploy.sh: unknown argument: $arg" >&2; exit 2 ;;
    esac
done

ALGO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
SRC="$ALGO_DIR/deploy/launchd"
IBC="$HOME/ibc"
LA="$HOME/Library/LaunchAgents"

# TREE GUARD — KAN-89. This script installs whatever tree it lives in, so one
# run from the dev checkout or a .worktrees/<key> tree used to put develop or
# feature-branch wrappers into production and report success — bypassing the
# promotion gate KAN-72 exists to restore. So it installs only when:
#   1. its own tree IS the deploy clone — the wrappers' ALGO_DIR default, read
#      from run_paper.sh rather than restated here so the two cannot drift; and
#   2. that tree is on promoted code (lib/branch_guard.sh says "promoted").
#      "behind" refuses too: installing older code than main is the 2026-09-08
#      rollback. "unknown" refuses: absence of evidence is not promoted.
# --dry-run copies nothing, so it never refuses; it prints the reason instead.
# --from-any-tree is the explicit escape hatch (tests, genuine emergencies); it
# says, and records in ~/ibc/logs/deploy.log, exactly what it installed from.
guard_reason=""
expected_clone="$(sed -n 's/^ALGO_DIR="\${ALGO_DIR:-\([^}]*\)}"$/\1/p' "$SRC/run_paper.sh" 2>/dev/null | head -n1)"
expected_resolved="$( [ -n "$expected_clone" ] && cd "$expected_clone" 2>/dev/null && pwd -P )"
if [ -z "$expected_clone" ]; then
    guard_reason="cannot read the deploy clone path (ALGO_DIR default) from $SRC/run_paper.sh"
elif [ "$ALGO_DIR" != "$expected_resolved" ]; then
    guard_reason="running from $ALGO_DIR, which is not the deploy clone $expected_clone — pull and deploy there"
else
    # Only the clone pays for the (bounded, read-only) ls-remote probe. It runs
    # under --from-any-tree too, so the override record says whether it put
    # unpromoted code into production.
    ALGO_BRANCH_DIR="$ALGO_DIR"
    # shellcheck source=deploy/launchd/lib/branch_guard.sh
    . "$SRC/lib/branch_guard.sh"
    algo_branch_check
    [ "$ALGO_BRANCH_STATUS" = "promoted" ] || \
        guard_reason="the deploy clone is not on promoted code ($ALGO_BRANCH_STATUS): $ALGO_BRANCH_DETAIL"
fi

if [ "$FROM_ANY_TREE" = "1" ]; then
    src_branch="$(git -C "$ALGO_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
    src_head="$(git -C "$ALGO_DIR" rev-parse --short HEAD 2>/dev/null || echo '?')"
    override_line="--from-any-tree: installing from $ALGO_DIR on $src_branch ($src_head)${guard_reason:+ — guard would have refused: $guard_reason}"
    echo "WARNING: $override_line" >&2
    if [ "$DRY_RUN" = "0" ]; then
        mkdir -p "$IBC/logs" 2>/dev/null || true
        printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$override_line" \
            >> "$IBC/logs/deploy.log" 2>/dev/null || true
    fi
elif [ -n "$guard_reason" ]; then
    if [ "$DRY_RUN" = "1" ]; then
        echo "WARNING: a real deploy would refuse: $guard_reason" >&2
    else
        echo "deploy.sh: REFUSED, nothing copied: $guard_reason" >&2
        echo "  (override, for tests and genuine emergencies only: --from-any-tree)" >&2
        exit 1
    fi
fi

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
failed=0
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
        # ATOMIC INSTALL — do not replace with a plain `cp "$src" "$dst"`.
        #
        # cp truncates and rewrites the SAME inode, and bash does not read a
        # script into memory: it reads incrementally and remembers a byte
        # offset. A shell already executing $dst therefore resumes at its old
        # offset inside the new content. On 2026-09-08 a backtest refresh that
        # started at 06:30 took a deploy at 07:54 (+7 lines) and, on its next
        # read, died with "line 248: ith: command not found" — seven lines
        # adrift, mid-word. That skipped the whole tail of the wrapper, which is
        # where refresh_exit's single dead-man decision, the TIMEOUT_FLAG branch
        # and the output/ prune protection live. run_paper.sh runs for minutes
        # from 04:15, so the same hazard applies to a live trading run.
        #
        # rename(2) is atomic and leaves the old inode readable until the last
        # descriptor closes, so a running job finishes against the version it
        # started with and the next invocation gets the new one. The mode is set
        # on the staged file, before the rename, so $dst never exists with the
        # wrong permissions.
        local tmp="$dst.deploy.$$"
        if ! cp "$src" "$tmp" 2>/dev/null; then
            rm -f "$tmp"
            echo "    ERROR: could not stage $dst — leaving the existing copy in place" >&2
            failed=$((failed + 1))
            return 1
        fi
        if [ "$mode" = "exec" ]; then
            chmod 755 "$tmp"
        else
            chmod 644 "$tmp"
        fi
        if ! mv -f "$tmp" "$dst" 2>/dev/null; then
            rm -f "$tmp"
            echo "    ERROR: could not install $dst — leaving the existing copy in place" >&2
            failed=$((failed + 1))
            return 1
        fi
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

if [ "$failed" -gt 0 ]; then
    echo ""
    echo "$failed file(s) FAILED to install; the previous copies are untouched." >&2
    echo "Fix the cause and re-run — nothing was left half-written." >&2
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

# A partial deploy must not report success: some wrappers would be new and some
# stale, which is the hardest state to reason about during an incident.
[ "$failed" -gt 0 ] && exit 1
exit 0
