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

ALGO_RECONCILIATION_SENTINEL="===RECONCILIATION-ALERT==="
ALGO_RECONCILIATION_DETAIL=""
ALGO_RECONCILIATION_ALERT=""

algo_reconciliation_check() {
    ALGO_RECONCILIATION_DETAIL=""
    ALGO_RECONCILIATION_ALERT=""

    local py out
    py="${ALGO_PYTHON:-${ALGO_DIR:-.}/.venv/bin/python}"

    # The script owns every failure it can name — a bad DSN, an unreachable
    # database — and renders those as "unknown" itself, so it exits 0 even
    # then. This `|| out=""` covers only the case it cannot: the interpreter
    # or the file being absent. cd first so a relative ALGO_DIR resolves the
    # same way the rest of the wrapper resolves it.
    out="$( (cd "${ALGO_DIR:-.}" 2>/dev/null \
             && "$py" "${ALGO_DIR:-.}/scripts/ops/reconciliation_status.py" \
                      --mode "${ALGO_MODE:-paper}" 2>/dev/null) )" || out=""

    if [ -z "$out" ]; then
        # Never a reassuring default. A wrapper that cannot run the check knows
        # less than nothing about the book, and "absence of evidence is not
        # evidence of no halt" is the rule the whole section implements.
        ALGO_RECONCILIATION_DETAIL="status: unknown — the reconciliation check could not be run (missing interpreter or script); this says nothing about whether entries are blocked"
        ALGO_RECONCILIATION_ALERT="🚨 reconciliation: CHECK DID NOT RUN — nothing can say whether entries are blocked, and a halted book looks exactly like a quiet market. Remedy: python scripts/reconcile_paper.py --report"
        return 0
    fi

    # Split on the sentinel line. awk rather than parameter expansion because
    # both halves are multi-line and ${var%%...} would need the whole body in
    # one pattern.
    ALGO_RECONCILIATION_DETAIL="$(printf '%s\n' "$out" \
        | awk -v s="$ALGO_RECONCILIATION_SENTINEL" '$0==s{exit} {print}')"
    ALGO_RECONCILIATION_ALERT="$(printf '%s\n' "$out" \
        | awk -v s="$ALGO_RECONCILIATION_SENTINEL" 'f{print} $0==s{f=1}')"

    # A body with no sentinel means the script printed something unexpected.
    # Report what it said rather than swallowing it, and treat the missing
    # sentinel as a reason to page — same rule as an empty body above.
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
