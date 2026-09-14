# Is the book fail-closed, and for how long — KAN-86.
#
# WHAT THIS EXISTS FOR
# --------------------
# On 2026-08-28 one missing_in_ib discrepancy (LLY, con_id 9160) made
# reconciliation_reports read status=major, entries_allowed=false, and
# run_paper.py:1758 turns that single boolean into a book-wide halt — every
# buy, in all six sleeves. It stayed that way for 17 days and nothing said so.
#
# Every channel built to break this kind of silence was quiet and CORRECT to be
# quiet: the run happened, so the dead-man switch pinged; the run exited 0, so
# the external check stayed green; no alert was raised, so Telegram and
# algo_alert_local had nothing to send. The Prometheus gauge at run_paper.py:633
# is written to a scraper that does not exist and read by a rule that was never
# written. The only trace of a 17-day trading halt was one line in a log.
#
# STATE VERSUS EVENT, AND WHY THE THRESHOLD IS TWO
# ------------------------------------------------
# Severity is a STATE: rendered every morning whether good or bad, so a healthy
# section is evidence rather than absence. A section that appears only when
# something is wrong teaches the reader that its absence means "fine", which is
# the assumption that cost 17 days.
#
# The ESCALATION needs a threshold, because one disabled session is a transient
# — a position opened after the snapshot, an IB hiccup, a catch-up run — and
# paging on it trains the operator to ignore the page. Two consecutive sessions
# is a halt nobody has noticed. The threshold is printed in the message so it
# can be argued with rather than reverse engineered.
#
# NOT DONE HERE: a rule for the Prometheus gauge. No Prometheus is deployed to
# evaluate one, so it would add a fifth detector with no consumer in order to
# fix the fourth.
#
# Sourced by run_pipeline_report.sh. Sets, and never exits non-zero:
#   ALGO_RECONCILIATION_DETAIL  the report section, always non-empty
#   ALGO_RECONCILIATION_ALERT   the escalation body, empty when healthy
#
# The split, and the sentinel between them, exist so the database is read once:
# the section and the alert are two renderings of one reading, and two
# invocations could disagree with each other across the 04:15 run's write.

# Bounded execution lives in one place (KAN-75). Resolved relative to THIS
# file, not $ALGO_DIR, for the reason branch_guard.sh records: a lib knows
# where its own sibling lives, and $ALGO_DIR is the tree under inspection.
# shellcheck source=deploy/launchd/lib/bounded.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bounded.sh"

# Wall-clock bound on the database read. WITHOUT THIS THE SECTION CAN END THE
# REPORT PERMANENTLY: create_engine has no connect_timeout, so against a
# half-open localhost:55432 — docker-proxy alive, container wedged, a real
# state on this host — the read blocks forever. This check runs inside the
# report's `{ ... } >> "$LOG_FILE"` block, so the whole job hangs, and launchd
# will not start a second instance of local.algo-pipeline-report while one is
# running. Every subsequent morning's report would then never run, and the job
# argues it needs no dead-man precisely because it sends daily. A section added
# to end silence must not become a permanent silence.
ALGO_RECONCILIATION_TIMEOUT="${ALGO_RECONCILIATION_TIMEOUT:-60}"

ALGO_RECONCILIATION_SENTINEL="===RECONCILIATION-ALERT==="
ALGO_RECONCILIATION_DETAIL=""
ALGO_RECONCILIATION_ALERT=""

algo_reconciliation_check() {
    ALGO_RECONCILIATION_DETAIL=""
    ALGO_RECONCILIATION_ALERT=""

    local py out rc=0 cause
    py="${ALGO_PYTHON:-${ALGO_DIR:-.}/.venv/bin/python}"

    # The script owns every failure it can name — a bad DSN, an unreachable
    # database — and renders those as "unknown" itself, so it exits 0 even
    # then. What is left for this layer is the interpreter or file being
    # absent, and the deadline above firing. cd first so a relative ALGO_DIR
    # resolves the same way the rest of the wrapper resolves it.
    out="$( (cd "${ALGO_DIR:-.}" 2>/dev/null \
             && algo_run_bounded "$ALGO_RECONCILIATION_TIMEOUT" \
                    "$py" "${ALGO_DIR:-.}/scripts/ops/reconciliation_status.py" \
                    --mode "${ALGO_MODE:-paper}" 2>/dev/null) )" || rc=$?

    # A body carrying the sentinel is usable whatever the exit code said.
    # Discarding good output because the process died after printing it would
    # replace a precise reading with "could not be run" — the safe direction,
    # but the wrong cause, and cause is what the operator acts on.
    case "$out" in
        *"$ALGO_RECONCILIATION_SENTINEL"*) rc=0 ;;
    esac

    if [ "$rc" -ne 0 ] || [ -z "$out" ]; then
        # Never a reassuring default. A wrapper that cannot run the check knows
        # less than nothing about the book, and "absence of evidence is not
        # evidence of no halt" is the rule the whole section implements.
        if [ "$rc" = "124" ]; then
            cause="it did not finish within ${ALGO_RECONCILIATION_TIMEOUT}s (the database read is most likely wedged)"
        elif [ "$rc" -ne 0 ]; then
            cause="it exited $rc without a usable reading"
        else
            cause="missing interpreter or script"
        fi
        ALGO_RECONCILIATION_DETAIL="status: unknown — the reconciliation check could not be run ($cause); this says nothing about whether entries are blocked"
        ALGO_RECONCILIATION_ALERT="🚨 reconciliation: CHECK DID NOT RUN ($cause) — nothing can say whether entries are blocked, and a halted book looks exactly like a quiet market. Remedy: python scripts/reconcile_paper.py --report"
        return 0
    fi

    # Split on the sentinel line. awk rather than parameter expansion because
    # both halves are multi-line and ${var%%...} would need the whole body in
    # one pattern.
    ALGO_RECONCILIATION_DETAIL="$(printf '%s\n' "$out" \
        | awk -v s="$ALGO_RECONCILIATION_SENTINEL" '$0==s{exit} {print}')"
    ALGO_RECONCILIATION_ALERT="$(printf '%s\n' "$out" \
        | awk -v s="$ALGO_RECONCILIATION_SENTINEL" 'f{print} $0==s{f=1}')"

    # A non-empty body with no sentinel means the script printed something
    # unexpected — a stub on PATH, a half-written change. Report what it said
    # rather than swallowing it, and page: an unrecognised reading is still no
    # reading.
    case "$out" in
        *"$ALGO_RECONCILIATION_SENTINEL"*) : ;;
        *)
            ALGO_RECONCILIATION_ALERT="🚨 reconciliation: CHECK OUTPUT UNRECOGNISED — nothing can say whether entries are blocked. Remedy: python scripts/reconcile_paper.py --report"
            ;;
    esac
    return 0
}

# The message to escalate, or empty when there is nothing to page about. Kept
# separate from the check so the caller's alerting stays a two-line if, matching
# the launchd-wiring, baseline-age and branch-guard sections.
algo_reconciliation_alert_body() {
    [ -n "$ALGO_RECONCILIATION_ALERT" ] && printf '%s\n' "$ALGO_RECONCILIATION_ALERT"
}
