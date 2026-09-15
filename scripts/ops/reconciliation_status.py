#!/usr/bin/env python3
"""Report whether the book is fail-closed, and page when it has been for days.

KAN-86. ``reconciliation_reports`` read ``status=major, entries_allowed=false``
from 2026-08-28 for 17 days on a single ``missing_in_ib`` discrepancy (LLY,
con_id 9160), and ``scripts/run_paper.py:1758`` turns that one boolean into a
book-wide halt::

    if not preparation.reconciliation.entries_allowed:
        entries_disabled = True

Not one sleeve — every buy, in all six. Nothing reported it. The Prometheus
gauge at ``run_paper.py:633`` is written to a scraper that does not exist and
read by a rule that was never written; the one log line scrolls past in a file
nobody opens on a healthy-looking day; the run exits 0, so the dead-man switch
pings and the external check stays correctly green.

That is the fourth instance of one pattern in five weeks — a correct detector
whose output nothing consumes (KAN-64, KAN-71, KAN-80, KAN-84) — and the most
expensive, because **the failure is indistinguishable from a quiet market**. A
book that places no buys for 17 days looks exactly like a book whose signals
said hold. Nothing in the equity curve, the exit code or the dead-man switch
separates the two.

So this module produces two things for the 04:52 report:

* a **section**, rendered whether the book is healthy or not, so a quiet
  section is evidence rather than absence; and
* an **escalation body**, empty unless there is something to page about.

Both go to stdout, split by :data:`SENTINEL`, because the launchd wrapper needs
both from one database read. Collection is split from rendering so the message
is proved by inserting rows rather than by grepping the shell script — the same
split ``pipeline_report_summary.py`` makes, for the same reason.

**This script always exits 0.** Refusing to report, or reporting a failure
upward, would convert a reporting problem into a missed session, and missed
sessions are permanent holes in the gate evidence (2026-08-13, 08-18, 09-01).
Every failure path renders as *unknown* and escalates instead.

Deliberately NOT here: a rule for the Prometheus gauge. No Prometheus is
deployed to evaluate one, so that would add a fifth detector with no consumer
in order to fix the fourth. The gauge stays as it is for the day the stack
lands.

Usage:
    python scripts/ops/reconciliation_status.py --mode paper
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

# Executed by path from the launchd wrapper, which puts `scripts/ops/` — not
# the repo root — on sys.path. Pin the repo root explicitly so the imports
# resolve to THIS checkout rather than to whatever tree an editable install
# happens to point at.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from shared.models.order_ledger import ReconciliationReport  # noqa: E402

#: Splits the section from the escalation body on stdout. A sentinel rather
#: than two invocations: the wrapper needs both halves and the database should
#: be read once.
SENTINEL = "===RECONCILIATION-ALERT==="

#: The clock the trading day is named by. Both the 04:15 paper run and this
#: 04:52 report are scheduled in SGT, so the SGT date is what "session" means
#: here. Grouping on the UTC date instead would be wrong in both directions:
#: the UTC date rolls at 08:00 SGT, so a catch-up reconcile at 09:00 SGT would
#: land in a *different* session from that morning's 04:15 run — escalating on
#: one bad day plus the operator's own investigation re-run — while a manual
#: run at 23:30 SGT would merge into the NEXT morning's session.
SESSION_TZ = ZoneInfo("Asia/Singapore")

#: How old a reading may be before it stops being evidence about this morning.
#: The only legitimate age at 04:52 is ~37 minutes: the paper run writes at
#: 04:15 the same morning, and the report shares its Tue–Sat schedule, so there
#: is no weekend gap to accommodate — a Tuesday whose own run wrote nothing
#: leaves Saturday's reading at 72h and SHOULD say unknown.
#:
#: 20 hours therefore tolerates a late or catch-up run within the same day
#: while still calling a missed session what it is. It is deliberately under
#: 24: at 26 a run that never wrote left yesterday's reading at ~24.6h and
#: rendered in the HEALTHY shape — "the last thing we heard was good news and
#: it is old", which is the KAN-71 defect this module quotes.
STALE_AFTER_HOURS = 20.0

#: One session is a transient — a position opened after the snapshot, an IB
#: hiccup, a catch-up run. Two is a halt nobody has noticed. Stated in the
#: message itself so the threshold can be argued with rather than reverse
#: engineered.
ESCALATE_AFTER_SESSIONS = 2

#: How many readings back to look. Comfortably past the 17-day case that
#: prompted this, and bounded so a long-running halt cannot make the query grow
#: without limit. A halt longer than this saturates the reported COUNT — the
#: headline number stops growing — but never the escalation, which only needs
#: the count to reach two.
LOOKBACK_READINGS = 120

#: Discrepancies named individually before the rest are summarised. Telegram
#: rejects a body over 4096 chars with HTTP 400 and the wrapper's
#: fire-and-forget send discards curl's status, so an over-long message is
#: silently dropped — the exact failure this story exists to end.
MAX_NAMED_DISCREPANCIES = 3

#: Hard ceiling on the escalation body, applied last and unconditionally.
#: MAX_NAMED_DISCREPANCIES bounds the *count* of discrepancies named, not their
#: length — three rows carrying a pathological symbol would still build a body
#: Telegram drops on the floor. The cap makes "this message is deliverable" a
#: guarantee rather than a probability, which for the only channel reporting a
#: trading halt is the difference that matters.
MAX_BODY_CHARS = 3500

REMEDY = "python scripts/reconcile_paper.py --report"

# A bad DSN surfaces verbatim in SQLAlchemy's ArgumentError, and the DSN carries
# the live Postgres password. Runs to the LAST '@' on purpose: the password may
# itself contain '@' or whitespace, and over-redacting is the safe direction.
# A third copy of the pattern rather than an import — divergence_alert.py pulls
# in the backtest package and pipeline_report_summary.py pulls in the halt
# repository, and this module has no business loading either — so, like those,
# it carries its own test.
_DSN_CREDENTIAL = re.compile(r"(?P<prefix>[a-zA-Z][\w+.-]*://[^\s/@]*:).*@")


def _redact(text: str) -> str:
    return _DSN_CREDENTIAL.sub(r"\g<prefix>***@", text)


@dataclass(frozen=True)
class ReconciliationFacts:
    """What the newest reading says, and how much of a standing condition it is.

    ``status`` is the one field the renderers branch on:

    ``ok``
        a fresh reading, entries allowed.
    ``disabled``
        a fresh reading, entries blocked. ``disabled_sessions`` says for how
        long, and decides whether it escalates.
    ``stale``
        a reading older than :data:`STALE_AFTER_HOURS`. It said something once;
        it says nothing about this morning.
    ``missing``
        no reading at all for this mode.
    ``unreadable``
        the database could not be read.

    The last three all render as *unknown* and all escalate. Absence of
    evidence must not render as OK — the rule KAN-71 was closed on.
    """

    status: str
    severity: str | None = None
    entries_allowed: bool | None = None
    discrepancies: tuple[dict, ...] = ()
    reading_at: datetime | None = None
    age_hours: float | None = None
    disabled_sessions: int = 0
    error: str | None = None
    stale_after_hours: float = STALE_AFTER_HOURS
    escalate_after_sessions: int = ESCALATE_AFTER_SESSIONS

    @property
    def known(self) -> bool:
        return self.status in {"ok", "disabled"}


def _session_key(moment: datetime) -> str:
    """The trading session a reading belongs to, as an SGT date.

    A catch-up run writes a second report on the same trading day, and counting
    rows rather than sessions would escalate on a single day's transient. See
    :data:`SESSION_TZ` for why the SGT date and not the UTC one.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(SESSION_TZ).date().isoformat()


def _consecutive_disabled_sessions(rows) -> int:
    """Sessions, newest first, whose LAST reading blocked entries.

    Each session is judged by its newest reading, because that is the reading
    that governed trading: ``run_paper.py`` reads the newest row and halts the
    book on it. Requiring *every* reading in a session to have blocked entries
    instead produced two bugs — a newest session containing one allowing
    reading rendered as "blocked for 0 consecutive session(s)", and, worse,
    silently suppressed the page for a halt of any length behind it.

    Stops at the first session whose last reading allowed entries. An older
    halt that was repaired must not inflate today's count and re-page for a
    problem already fixed.
    """
    newest_by_session: dict[str, bool] = {}
    order: list[str] = []
    for row in rows:  # newest first
        key = _session_key(row.created_at)
        if key not in newest_by_session:
            order.append(key)
            newest_by_session[key] = bool(row.entries_allowed)

    count = 0
    for key in order:
        if newest_by_session[key]:
            break
        count += 1
    return count


def collect_facts(
    session: Session,
    *,
    mode: str,
    now: datetime | None = None,
    stale_after_hours: float = STALE_AFTER_HOURS,
    escalate_after_sessions: int = ESCALATE_AFTER_SESSIONS,
) -> ReconciliationFacts:
    """Read the newest reconciliation reading for ``mode``, and its history.

    Scoped by mode because ``run_paper.py`` halts on the reading for its own
    mode: a live-book row must neither silence nor trigger the paper book's
    section.
    """
    now = now or datetime.now(timezone.utc)
    rows = list(
        session.scalars(
            select(ReconciliationReport)
            .where(ReconciliationReport.mode == mode)
            .order_by(
                ReconciliationReport.created_at.desc(),
                # Same tiebreak gate_data_source.py uses over this table: two
                # rows can share a timestamp, and "newest" must be total.
                ReconciliationReport.id.desc(),
            )
            .limit(LOOKBACK_READINGS)
        )
    )
    common = dict(
        stale_after_hours=stale_after_hours,
        escalate_after_sessions=escalate_after_sessions,
    )
    if not rows:
        return ReconciliationFacts(status="missing", **common)

    newest = rows[0]
    at = newest.created_at
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    age_hours = (now - at).total_seconds() / 3600.0
    # A clock skew that puts the reading in the future is not freshness we can
    # rely on, but it is also not staleness; clamp rather than invent either.
    age_hours = max(age_hours, 0.0)

    result = newest.result if isinstance(newest.result, dict) else {}
    discrepancies = tuple(
        d for d in (result.get("discrepancies") or []) if isinstance(d, dict)
    )

    if age_hours > stale_after_hours:
        return ReconciliationFacts(
            status="stale",
            severity=newest.status,
            entries_allowed=bool(newest.entries_allowed),
            discrepancies=discrepancies,
            reading_at=at,
            age_hours=age_hours,
            **common,
        )

    return ReconciliationFacts(
        status="ok" if newest.entries_allowed else "disabled",
        severity=newest.status,
        entries_allowed=bool(newest.entries_allowed),
        discrepancies=discrepancies,
        reading_at=at,
        age_hours=age_hours,
        disabled_sessions=(
            0 if newest.entries_allowed else _consecutive_disabled_sessions(rows)
        ),
        **common,
    )


def _describe(discrepancy: dict) -> str:
    """One discrepancy, naming whatever fields it carries.

    A row written by an older writer, or one whose portfolio is unknown, must
    not crash the only channel that reports the halt — so every field is
    optional and missing ones are simply left out.
    """
    parts = [str(discrepancy.get("type") or "unknown")]
    symbol = discrepancy.get("symbol")
    if symbol:
        parts.append(str(symbol))
    con_id = discrepancy.get("con_id")
    if con_id is not None:
        parts.append(f"con_id {con_id}")
    portfolio = discrepancy.get("portfolio")
    if portfolio:
        parts.append(f"portfolio {portfolio}")
    return " ".join(parts)


def _discrepancy_phrase(facts: ReconciliationFacts) -> str:
    total = len(facts.discrepancies)
    if not total:
        return "no discrepancies recorded"
    named = [_describe(d) for d in facts.discrepancies[:MAX_NAMED_DISCREPANCIES]]
    phrase = f"{total} discrepancy: " if total == 1 else f"{total} discrepancies: "
    phrase += "; ".join(named)
    remaining = total - len(named)
    if remaining > 0:
        phrase += f"; +{remaining} more"
    return phrase


def _bare_age_phrase(facts: ReconciliationFacts) -> str:
    """"3.2h ago", with no leading verb, for sentences that supply their own."""
    if facts.age_hours is None:
        return "of unknown age"
    if facts.age_hours < 1:
        return f"{facts.age_hours * 60:.0f}m ago"
    return f"{facts.age_hours:.1f}h ago"


def _age_phrase(facts: ReconciliationFacts) -> str:
    return f"read {_bare_age_phrase(facts)}"


def render_section(facts: ReconciliationFacts) -> str:
    """The body section, rendered on every run whatever the state.

    A section that appears only when something is wrong teaches the reader that
    its absence means "fine", which is the assumption that cost 17 days here.
    """
    if facts.status == "unreadable":
        return (
            f"status: unknown — the reconciliation reading could not be read "
            f"({facts.error or 'no detail'}); this says nothing about whether "
            f"entries are blocked\n"
            f"  remedy: {REMEDY}"
        )
    if facts.status == "missing":
        return (
            "status: unknown — NO reconciliation reading exists for this mode; "
            "absence of a reading is not evidence that entries are allowed\n"
            f"  remedy: {REMEDY}"
        )
    if facts.status == "stale":
        entries = "allowed" if facts.entries_allowed else "DISABLED"
        return (
            f"status: unknown — newest reading is {_bare_age_phrase(facts)} "
            f"(bound {facts.stale_after_hours:.0f}h), so it says nothing about "
            f"this morning\n"
            f"  last heard: {facts.severity} · entries: {entries} · "
            f"{_discrepancy_phrase(facts)} · at "
            f"{facts.reading_at.isoformat() if facts.reading_at else 'unknown'}\n"
            f"  remedy: {REMEDY}"
        )

    entries = "allowed" if facts.entries_allowed else "DISABLED"
    lines = [
        f"status: {facts.severity or 'unknown'} · entries: {entries} · "
        f"discrepancies: {len(facts.discrepancies)} · {_age_phrase(facts)} "
        f"({facts.reading_at.isoformat() if facts.reading_at else 'unknown'})"
    ]
    if facts.status == "disabled":
        lines.append(
            f"  entries have been blocked for {facts.disabled_sessions} "
            f"consecutive session(s) — this blocks every buy in every sleeve, "
            f"not one sleeve"
        )
        lines.append(f"  escalates at {facts.escalate_after_sessions} consecutive")
    for discrepancy in facts.discrepancies[:MAX_NAMED_DISCREPANCIES]:
        lines.append(f"  {_describe(discrepancy)}")
    remaining = len(facts.discrepancies) - MAX_NAMED_DISCREPANCIES
    if remaining > 0:
        lines.append(f"  +{remaining} more")
    if facts.status == "disabled":
        lines.append(f"  remedy: {REMEDY}")
    return "\n".join(lines)


def alert_body(facts: ReconciliationFacts) -> str:
    """The message to escalate, or empty when there is nothing to page about.

    Kept separate from the section so the caller's alerting stays a two-line
    ``if``, matching the launchd-wiring, baseline-age and branch-guard sections
    that were added by exactly this class of story.
    """
    return _capped(_alert_body(facts))


def _capped(body: str) -> str:
    if len(body) <= MAX_BODY_CHARS:
        return body
    return body[: MAX_BODY_CHARS - 1] + "…"


def _alert_body(facts: ReconciliationFacts) -> str:
    if facts.status == "unreadable":
        return (
            f"🚨 reconciliation: reading UNREADABLE ({facts.error or 'no detail'}) "
            f"— nothing can say whether entries are blocked, and a halted book "
            f"looks exactly like a quiet market. Remedy: {REMEDY}"
        )
    if facts.status == "missing":
        return (
            f"🚨 reconciliation: NO reconciliation reading for this mode — "
            f"absence of a reading is not evidence that entries are allowed. "
            f"Remedy: {REMEDY}"
        )
    if facts.status == "stale":
        entries = "allowed" if facts.entries_allowed else "DISABLED"
        return (
            f"🚨 reconciliation: reading is UNKNOWN — newest is "
            f"{_bare_age_phrase(facts)}, past the "
            f"{facts.stale_after_hours:.0f}h bound, so it says nothing about "
            f"this morning (last heard: {facts.severity}, entries {entries}). "
            f"Remedy: {REMEDY}"
        )
    if facts.status == "disabled" and (
        facts.disabled_sessions >= facts.escalate_after_sessions
    ):
        return (
            f"🚨 reconciliation: ENTRIES DISABLED for "
            f"{facts.disabled_sessions} consecutive sessions — every buy in "
            f"every sleeve is blocked, and a book that places no buys looks "
            f"exactly like a book whose signals said hold. "
            f"status={facts.severity}, {_discrepancy_phrase(facts)}. "
            f"Remedy: {REMEDY} (then --apply-plan). "
            f"Escalates at {facts.escalate_after_sessions} consecutive sessions."
        )
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render the daily report's reconciliation section."
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("ALGO_DATABASE_URL"),
        help="defaults to $ALGO_DATABASE_URL",
    )
    parser.add_argument("--mode", default=os.environ.get("ALGO_MODE", "paper"))
    parser.add_argument(
        "--stale-after-hours", type=float, default=STALE_AFTER_HOURS
    )
    args = parser.parse_args(argv)

    if not args.database_url:
        facts = ReconciliationFacts(
            status="unreadable", error="no --database-url and no $ALGO_DATABASE_URL"
        )
    else:
        try:
            engine = create_engine(args.database_url)
            try:
                with Session(engine) as session:
                    facts = collect_facts(
                        session,
                        mode=args.mode,
                        stale_after_hours=args.stale_after_hours,
                    )
            finally:
                engine.dispose()
        except Exception as exc:  # noqa: BLE001 — never sink the report
            # One redacted line, not a traceback: the DSN carries the live
            # Postgres password and this goes to the report log verbatim.
            detail = f"{_redact(type(exc).__name__)}: {_redact(str(exc))}"
            print(detail, file=sys.stderr)
            facts = ReconciliationFacts(
                status="unreadable", error=_redact(type(exc).__name__)
            )

    print(render_section(facts))
    print(SENTINEL)
    body = alert_body(facts)
    if body:
        print(body)
    # Always 0: see the module docstring. A reporting problem must not become a
    # missed session.
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
