# Is the deploy tree running promoted code? — KAN-73.
#
# WHY THIS EXISTS
# ---------------
# On 2026-09-08 the deploy tree was found on `develop`, four commits ahead of
# `main`; then, mid-correction, on a `main` ref that was 38 commits stale, which
# silently rolled the live host back 62 files. Neither state produced a line
# anywhere. Every other drift of this shape already has a guard — the schema
# guard in run_paper.sh, the `cmp -s "$0" "$CANON"` wrapper guard, secrets.sh's
# non-regular-file .env refusal, the plist reconciliation in KAN-64 — and all of
# them were correctly silent throughout, because each was true. The code was
# self-consistent. It just was not the promoted code.
#
# WHY ANCESTRY AND NOT THE BRANCH NAME
# ------------------------------------
# "Am I on main?" would have called the second, worse state healthy: the tree WAS
# on main, 38 commits behind it. The question that catches both is whether HEAD
# is contained in origin/main's history, and how far behind it is.
#
# WHY ls-remote AND NOT fetch
# ---------------------------
# The comparison is only as good as the local origin/main ref, and a stale ref
# is exactly what went wrong — nothing fetches on this host, so deciding from
# that ref alone would have called the 38-commit rollback healthy. So the remote
# tip is the authority, not a footnote.
#
# But `git fetch` takes locks, and a monitor killed mid-fetch leaves a
# .git/FETCH_HEAD.lock that breaks every later fetch — trading a silent problem
# for a louder one. `git ls-remote` answers the same question, writes nothing,
# and takes no locks. When it cannot reach origin the check falls back to the
# last known ref and says so: a degraded answer beats silence.
#
# Sourced by run_pipeline_report.sh. Sets, and never exits non-zero:
#   ALGO_BRANCH          current branch name, or "" if unreadable
#   ALGO_BRANCH_STATUS   promoted | behind | unpromoted | unknown
#   ALGO_BRANCH_DETAIL   one line for the report body
#
# Deliberately NOT fatal to any job. Refusing to trade because the checkout is
# on the wrong branch converts a reporting problem into a missed session, and
# missed sessions are permanent holes in the gate evidence (2026-08-13, 08-18,
# 09-01). Loud, daily and non-blocking is the right severity.

ALGO_BRANCH=""
ALGO_BRANCH_STATUS=""
ALGO_BRANCH_DETAIL=""

# Seconds before the read-only remote probe is abandoned. Small: it is a
# nice-to-have caveat, not the verdict.
ALGO_BRANCH_PROBE_TIMEOUT="${ALGO_BRANCH_PROBE_TIMEOUT:-10}"

# The tree to inspect. Defaults to the deploy tree, which is the only thing
# production ever wants. Overridable ONLY so tests can drive the wrapper against
# a throwaway repo — without it every pipeline-report test would `ls-remote` to
# GitHub (75s instead of 15s locally), and in CI, where actions/checkout leaves
# a detached HEAD and often no origin/main ref at all, this check would report
# "unknown", fire an extra alert, and break the message-count assertions. Never
# export it in a login shell.
ALGO_BRANCH_DIR="${ALGO_BRANCH_DIR:-${ALGO_DIR:-.}}"

# Bounded execution lives in one place now (KAN-75).
# Resolved relative to THIS file, not $ALGO_DIR. A lib knows where its own
# sibling lives; $ALGO_DIR is the tree under inspection, which is not the same
# thing and is deliberately pointed at a throwaway repo by the tests. Conflating
# them made the bounded probe silently undefined, and the fallback path — "could
# not reach origin" — looks exactly like being offline.
# shellcheck source=deploy/launchd/lib/bounded.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bounded.sh"


_algo_git() {
    git -C "$ALGO_BRANCH_DIR" "$@" 2>/dev/null
}

algo_branch_check() {
    ALGO_BRANCH=""
    ALGO_BRANCH_STATUS=""
    ALGO_BRANCH_DETAIL=""

    # A tree whose git state cannot be read is reported as unknown and alerts.
    # Absence of evidence must not render as promoted — the KAN-71 rule.
    if ! _algo_git rev-parse --git-dir >/dev/null; then
        ALGO_BRANCH_STATUS="unknown"
        ALGO_BRANCH_DETAIL="cannot read git state at $ALGO_BRANCH_DIR — unable to tell whether the deploy is running promoted code"
        return 0
    fi

    ALGO_BRANCH="$(_algo_git rev-parse --abbrev-ref HEAD)"
    local head_short; head_short="$(_algo_git rev-parse --short HEAD)"

    if ! _algo_git rev-parse --verify --quiet origin/main >/dev/null; then
        ALGO_BRANCH_STATUS="unknown"
        ALGO_BRANCH_DETAIL="no origin/main ref in $ALGO_BRANCH_DIR — nothing to compare $ALGO_BRANCH ($head_short) against"
        return 0
    fi

    # Read-only, lock-free, bounded. This is the AUTHORITY on where main is, not
    # a caveat: the local origin/main ref is stale precisely when nobody has
    # fetched, which is the state that produced the 38-commit rollback. Deciding
    # from the local ref alone would have called that tree healthy.
    local caveat="" remote_sha head_full target
    remote_sha="$(algo_run_bounded "$ALGO_BRANCH_PROBE_TIMEOUT" \
                    git -C "$ALGO_BRANCH_DIR" ls-remote origin refs/heads/main \
                    | awk 'NR==1{print $1}')"
    head_full="$(_algo_git rev-parse HEAD)"

    if [ -z "$remote_sha" ]; then
        # Offline: fall back to the last known ref and say so, rather than
        # refusing to answer. A degraded answer beats silence.
        caveat=" (could not reach origin; compared against the last known ref)"
        target="$(_algo_git rev-parse origin/main)"
    elif _algo_git cat-file -e "${remote_sha}^{commit}"; then
        # We have the real tip locally — every comparison below is exact.
        target="$remote_sha"
    else
        # The remote tip is not in this repo at all, so it is definitionally
        # ahead and the count cannot be computed. The promoted/unpromoted call
        # still can be, from the last known ref.
        caveat=" (origin/main is at ${remote_sha:0:7}, not present locally — run git fetch)"
        target="$(_algo_git rev-parse origin/main)"
        if _algo_git merge-base --is-ancestor HEAD "$target"; then
            ALGO_BRANCH_STATUS="behind"
            ALGO_BRANCH_DETAIL="$ALGO_BRANCH ($head_short) is behind origin/main — the deploy is running older code than main${caveat}"
            return 0
        fi
    fi

    if [ "$head_full" = "$target" ]; then
        ALGO_BRANCH_STATUS="promoted"
        ALGO_BRANCH_DETAIL="$ALGO_BRANCH ($head_short) is origin/main${caveat}"
    elif _algo_git merge-base --is-ancestor HEAD "$target"; then
        # Normal for the hours between a promotion and the pull, so this is a
        # warning in the body rather than a page.
        local behind; behind="$(_algo_git rev-list --count "HEAD..$target")"
        ALGO_BRANCH_STATUS="behind"
        ALGO_BRANCH_DETAIL="$ALGO_BRANCH ($head_short) is ${behind} commit(s) behind origin/main — the deploy is running older code than main${caveat}"
    else
        ALGO_BRANCH_STATUS="unpromoted"
        ALGO_BRANCH_DETAIL="$ALGO_BRANCH ($head_short) is NOT contained in origin/main — the deploy is running code that was never promoted${caveat}"
    fi
    return 0
}

# The message to escalate, or empty when there is nothing to page about. Kept
# separate from the check so the caller's alerting stays a two-line if, matching
# the launchd-wiring and baseline-age sections.
algo_branch_alert_body() {
    case "$ALGO_BRANCH_STATUS" in
        unpromoted) printf '🚨 deploy is running UNPROMOTED code — %s\n' "$ALGO_BRANCH_DETAIL" ;;
        unknown)    printf '🚨 deploy branch state UNKNOWN — %s\n' "$ALGO_BRANCH_DETAIL" ;;
        *)          : ;;
    esac
}
