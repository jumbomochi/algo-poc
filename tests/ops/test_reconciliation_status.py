"""The reconciliation section of the 04:52 daily report — KAN-86.

On 2026-08-28 a single ``missing_in_ib`` discrepancy (LLY, con_id 9160) made
``reconciliation_reports`` read ``status=major, entries_allowed=false``, and
``scripts/run_paper.py`` turns that into a book-wide halt: every buy, in all six
sleeves. It stayed that way for 17 days and **nothing said so**. The Prometheus
gauge is scraped by nobody, the one log line scrolls past, the run exits 0 so
the dead-man switch stays correctly green, and the daily report had no
reconciliation section at all.

The failure is indistinguishable from a quiet market — a book that places no
buys for 17 days looks exactly like a book whose signals said hold — so these
tests assert on what reaches a human, not on what is computed.

The rendering is split from the collection for the same reason
``pipeline_report_summary`` splits them: the message content is proved by
inserting rows, not by grepping a shell script.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from scripts.ops.reconciliation_status import (
    SENTINEL,
    STALE_AFTER_HOURS,
    ReconciliationFacts,
    alert_body,
    collect_facts,
    main,
    render_section,
)
from shared.models import Base
from shared.models.order_ledger import ReconciliationReport

NOW = datetime(2026, 9, 14, 20, 52, tzinfo=timezone.utc)

LLY = {
    "type": "missing_in_ib",
    "con_id": 9160,
    "symbol": "LLY",
    "ib_quantity": None,
    "db_quantity": 12.0,
    "portfolio": "quality_value",
    "auto_correct": False,
}


@pytest.fixture()
def session(tmp_path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'recon.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _report(session, *, at, allowed=True, discrepancies=(), mode="paper"):
    """One row as ``scripts/reconcile_paper.py`` writes it.

    ``status`` and ``entries_allowed`` are stored as columns and *also* inside
    ``result``; the columns are what ``run_paper.py`` reads, so they are what
    this reads too.
    """
    severity = "ok" if allowed else "major"
    session.add(
        ReconciliationReport(
            account_id="DUN551088",
            mode=mode,
            status=severity,
            entries_allowed=allowed,
            result={
                "account_id": "DUN551088",
                "severity": severity,
                "entries_allowed": allowed,
                "matched": [],
                "discrepancies": list(discrepancies),
            },
            created_at=at,
        )
    )
    session.commit()


def _facts(session, *, now=NOW, mode="paper"):
    return collect_facts(session, mode=mode, now=now)


# ---------------------------------------------------------------------------
# 1. A healthy book renders the section and does not escalate
# ---------------------------------------------------------------------------


def test_a_healthy_book_renders_the_section(session):
    """Rendered whether good or bad — a silent section is evidence, absence of
    one is not. Same rule the launchd-wiring and baseline-age sections follow."""
    _report(session, at=NOW - timedelta(minutes=37))
    facts = _facts(session)

    assert facts.status == "ok"
    section = render_section(facts)
    assert "ok" in section
    assert "entries: allowed" in section
    assert "discrepancies: 0" in section


def test_a_healthy_book_does_not_escalate(session):
    """An alarm that fires on a healthy day is one the operator stops reading."""
    _report(session, at=NOW - timedelta(minutes=37))
    assert alert_body(_facts(session)) == ""


# ---------------------------------------------------------------------------
# 2/3. The escalation threshold: two consecutive sessions, not one
# ---------------------------------------------------------------------------


def test_one_disabled_session_renders_but_does_not_escalate(session):
    """One session is a transient — a position opened after the snapshot, an IB
    hiccup. Paging on it trains the operator to ignore the page."""
    _report(session, at=NOW - timedelta(days=1), allowed=True)
    _report(session, at=NOW - timedelta(minutes=37), allowed=False,
            discrepancies=[LLY])
    facts = _facts(session)

    assert facts.disabled_sessions == 1
    section = render_section(facts)
    assert "entries: DISABLED" in section
    assert alert_body(facts) == ""


def test_two_consecutive_disabled_sessions_escalate(session):
    """Two is a halt nobody has noticed."""
    _report(session, at=NOW - timedelta(days=2), allowed=True)
    _report(session, at=NOW - timedelta(days=1), allowed=False,
            discrepancies=[LLY])
    _report(session, at=NOW - timedelta(minutes=37), allowed=False,
            discrepancies=[LLY])
    facts = _facts(session)

    assert facts.disabled_sessions == 2
    assert "ENTRIES DISABLED" in alert_body(facts)


def test_the_seventeen_day_case_counts_every_consecutive_session(session):
    """The case this story was filed for. The count is the headline, because
    "disabled" alone reads as today's news rather than as a standing halt."""
    _report(session, at=NOW - timedelta(days=17), allowed=True)
    for day in range(16, 0, -1):
        _report(session, at=NOW - timedelta(days=day), allowed=False,
                discrepancies=[LLY])
    # This morning's own run, 37 minutes ago. Without it the newest reading is
    # a day old and correctly reads as stale rather than as a live halt.
    _report(session, at=NOW - timedelta(minutes=37), allowed=False,
            discrepancies=[LLY])
    facts = _facts(session)

    assert facts.disabled_sessions == 17
    assert "17 consecutive sessions" in alert_body(facts)


def test_a_recovered_session_resets_the_count(session):
    """Consecutive means consecutive. An older halt that was repaired must not
    inflate today's count and re-page for a problem already fixed."""
    _report(session, at=NOW - timedelta(days=5), allowed=False,
            discrepancies=[LLY])
    _report(session, at=NOW - timedelta(days=4), allowed=False,
            discrepancies=[LLY])
    _report(session, at=NOW - timedelta(days=3), allowed=True)
    _report(session, at=NOW - timedelta(minutes=37), allowed=False,
            discrepancies=[LLY])
    facts = _facts(session)

    assert facts.disabled_sessions == 1
    assert alert_body(facts) == ""


def test_several_readings_in_one_day_are_one_session(session):
    """A catch-up run writes a second report the same day. Counting rows rather
    than sessions would escalate on a single day's transient."""
    _report(session, at=NOW - timedelta(days=1), allowed=True)
    _report(session, at=NOW - timedelta(hours=3), allowed=False,
            discrepancies=[LLY])
    _report(session, at=NOW - timedelta(minutes=37), allowed=False,
            discrepancies=[LLY])
    facts = _facts(session)

    assert facts.disabled_sessions == 1
    assert alert_body(facts) == ""


def test_another_mode_is_not_read(session):
    """``run_paper.py`` halts on the reading for its own mode. A live-book row
    must not silence, or trigger, the paper book's section."""
    _report(session, at=NOW - timedelta(minutes=37), allowed=False,
            discrepancies=[LLY], mode="live")
    facts = _facts(session, mode="paper")

    assert facts.status == "missing"
    assert "NO reconciliation reading" in alert_body(facts)


# ---------------------------------------------------------------------------
# 4. The escalation names the discrepancy and the remedy
# ---------------------------------------------------------------------------


def test_the_escalation_names_the_discrepancy_and_the_remedy(session):
    """"Entries disabled" without "and here is the thing to run" produces a
    second silent week while someone works out what to do."""
    _report(session, at=NOW - timedelta(days=1), allowed=False,
            discrepancies=[LLY])
    _report(session, at=NOW - timedelta(minutes=37), allowed=False,
            discrepancies=[LLY])
    body = alert_body(_facts(session))

    assert "missing_in_ib" in body
    assert "LLY" in body
    assert "9160" in body
    assert "quality_value" in body
    assert "scripts/reconcile_paper.py --report" in body


def test_the_threshold_is_stated_so_it_can_be_argued_with(session):
    _report(session, at=NOW - timedelta(days=1), allowed=False,
            discrepancies=[LLY])
    _report(session, at=NOW - timedelta(minutes=37), allowed=False,
            discrepancies=[LLY])
    assert "2 consecutive" in alert_body(_facts(session))


def test_a_discrepancy_missing_its_fields_still_names_what_it_can(session):
    """A row written by an older writer, or one whose portfolio is unknown,
    must not crash the only channel that reports the halt."""
    bare = {"type": "missing_in_db", "con_id": 42}
    _report(session, at=NOW - timedelta(days=1), allowed=False,
            discrepancies=[bare])
    _report(session, at=NOW - timedelta(minutes=37), allowed=False,
            discrepancies=[bare])
    body = alert_body(_facts(session))

    assert "missing_in_db" in body
    assert "42" in body


def test_many_discrepancies_are_capped_so_the_send_is_not_dropped(session):
    """Telegram rejects a body over 4096 chars with HTTP 400, and the wrapper's
    fire-and-forget send discards curl's status — an over-long message is
    silently dropped, which is the failure this story exists to end."""
    many = [dict(LLY, con_id=9000 + i, symbol=f"SYM{i}") for i in range(40)]
    _report(session, at=NOW - timedelta(days=1), allowed=False,
            discrepancies=many)
    _report(session, at=NOW - timedelta(minutes=37), allowed=False,
            discrepancies=many)
    body = alert_body(_facts(session))

    assert "40" in body
    assert len(body) < 1000, len(body)
    assert "more" in body


# ---------------------------------------------------------------------------
# 5. A stale reading is unknown-and-escalates, never OK (the KAN-71 rule)
# ---------------------------------------------------------------------------


def test_a_stale_reading_is_unknown_not_healthy(session):
    """Absence of evidence must not render as OK. A reading from before the
    bound says nothing about whether this morning's book is halted."""
    _report(session, at=NOW - timedelta(hours=STALE_AFTER_HOURS + 2),
            allowed=True)
    facts = _facts(session)

    assert facts.status == "stale"
    section = render_section(facts)
    assert "unknown" in section
    assert "UNKNOWN" in alert_body(facts)


def test_a_stale_reading_escalates_even_though_it_said_entries_were_allowed(session):
    """The dangerous shape: the last thing we heard was good news, and it is
    old. Rendering that as healthy is exactly the KAN-71 defect."""
    _report(session, at=NOW - timedelta(days=4), allowed=True)
    assert alert_body(_facts(session)) != ""


def test_no_reading_at_all_escalates(session):
    facts = _facts(session)
    assert facts.status == "missing"
    assert "unknown" in render_section(facts)
    assert alert_body(facts) != ""


def test_an_unreadable_database_is_unknown_not_healthy(session):
    """The collection failed; the section must say so rather than omit itself.
    A missing section reproduces the silence this story is about."""
    facts = ReconciliationFacts(status="unreadable", error="OperationalError")
    assert "unknown" in render_section(facts)
    assert "UNREADABLE" in alert_body(facts)
    assert "OperationalError" in alert_body(facts)


def test_a_fresh_reading_inside_the_bound_is_not_stale(session):
    _report(session, at=NOW - timedelta(hours=STALE_AFTER_HOURS - 1),
            allowed=True)
    assert _facts(session).status == "ok"


# ---------------------------------------------------------------------------
# 6. The section never changes a job's exit code
# ---------------------------------------------------------------------------


def _run_main(db_path, *extra) -> tuple[int, str]:
    import io
    import contextlib

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(["--database-url", f"sqlite:///{db_path}", *extra])
    return code, out.getvalue()


def test_main_exits_zero_on_a_healthy_book(tmp_path, session):
    _report(session, at=datetime.now(timezone.utc))
    code, out = _run_main(tmp_path / "recon.db")
    assert code == 0
    assert SENTINEL in out


def test_main_exits_zero_on_a_halted_book(tmp_path, session):
    _report(session, at=datetime.now(timezone.utc) - timedelta(days=1),
            allowed=False, discrepancies=[LLY])
    _report(session, at=datetime.now(timezone.utc), allowed=False,
            discrepancies=[LLY])
    code, out = _run_main(tmp_path / "recon.db")
    assert code == 0
    section, _, alert = out.partition(SENTINEL)
    assert "DISABLED" in section
    assert "ENTRIES DISABLED" in alert


def test_main_exits_zero_when_the_database_cannot_be_read(tmp_path):
    """A reporting problem must never become a missed session: missed sessions
    are permanent holes in the gate evidence (2026-08-13, 08-18, 09-01)."""
    code, out = _run_main("/nonexistent/dir/recon.db")
    assert code == 0
    section, _, alert = out.partition(SENTINEL)
    assert "unknown" in section
    assert alert.strip() != ""


def test_main_never_prints_the_password_from_a_bad_dsn():
    """The DSN carries the live Postgres password and this output goes to the
    report log verbatim."""
    import io
    import contextlib

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main([
            "--database-url",
            "postgresql://algo:sup3r-s3cret@127.0.0.1:1/algo_poc",
        ])
    assert code == 0
    assert "sup3r-s3cret" not in out.getvalue() + err.getvalue()


# ---------------------------------------------------------------------------
# Sessions are SGT trading days, judged by their newest reading
# ---------------------------------------------------------------------------
# Both the 04:15 paper run and the 04:52 report are scheduled in SGT, so the SGT
# date is what "session" means. The UTC date rolls at 08:00 SGT, which is inside
# the trading day, and grouping on it was wrong in both directions.
#
# NOW is 2026-09-14 20:52 UTC = 2026-09-15 04:52 SGT, so "this session" is the
# SGT date 2026-09-15.

SGT = ZoneInfo("Asia/Singapore")


def _sgt(day: int, hour: int, minute: int = 0) -> datetime:
    """A moment on 2026-09-<day> in SGT, as the writer would store it."""
    return datetime(2026, 9, day, hour, minute, tzinfo=SGT).astimezone(timezone.utc)


def test_a_catch_up_reconcile_later_the_same_trading_day_is_one_session(session):
    """The operator's own investigation re-run. The message tells them to run
    `reconcile_paper.py --report`; doing so must not produce the second page
    that teaches them to ignore the first. Under UTC-date grouping the 09:00
    SGT re-run crossed into a new session and escalated."""
    _report(session, at=_sgt(14, 4, 15), allowed=True)
    _report(session, at=_sgt(15, 4, 15), allowed=False, discrepancies=[LLY])
    _report(session, at=_sgt(15, 9, 0), allowed=False, discrepancies=[LLY])
    facts = collect_facts(session, mode="paper", now=_sgt(15, 10, 0))

    assert facts.disabled_sessions == 1
    assert alert_body(facts) == ""


def test_a_late_evening_run_is_its_own_session_not_the_next_mornings(session):
    """The converse. A manual run at 23:30 SGT shares a UTC date with the next
    morning's 04:15 SGT run, and merging them hid a real second session."""
    _report(session, at=_sgt(14, 23, 30), allowed=False, discrepancies=[LLY])
    _report(session, at=_sgt(15, 4, 15), allowed=False, discrepancies=[LLY])
    facts = collect_facts(session, mode="paper", now=_sgt(15, 4, 52))

    assert facts.disabled_sessions == 2
    assert "ENTRIES DISABLED" in alert_body(facts)


def test_a_session_is_judged_by_its_newest_reading_not_by_all_of_them(session):
    """`run_paper.py` halts on the newest row, so the newest row is what
    governed trading. Requiring every reading in a session to have blocked
    entries rendered "blocked for 0 consecutive session(s)" — nonsense on its
    face — whenever a session contained one allowing reading."""
    _report(session, at=_sgt(15, 4, 15), allowed=True)
    _report(session, at=_sgt(15, 5, 0), allowed=False, discrepancies=[LLY])
    facts = collect_facts(session, mode="paper", now=_sgt(15, 5, 30))

    assert facts.status == "disabled"
    assert facts.disabled_sessions == 1
    assert "0 consecutive" not in render_section(facts)


def test_a_long_halt_is_not_suppressed_by_one_allowing_reading_today(session):
    """The dangerous half of the same bug: under the all-must-be-disabled rule
    the count broke at zero, so a halt of ANY length paged not at all."""
    for day in range(1, 15):
        _report(session, at=_sgt(day, 4, 15), allowed=False, discrepancies=[LLY])
    _report(session, at=_sgt(15, 4, 15), allowed=True)
    _report(session, at=_sgt(15, 5, 0), allowed=False, discrepancies=[LLY])
    facts = collect_facts(session, mode="paper", now=_sgt(15, 5, 30))

    assert facts.disabled_sessions == 15
    assert "ENTRIES DISABLED" in alert_body(facts)


def test_a_repaired_session_still_stops_the_count(session):
    """The rule the newest-reading change must not break."""
    _report(session, at=_sgt(12, 4, 15), allowed=False, discrepancies=[LLY])
    _report(session, at=_sgt(13, 4, 15), allowed=False, discrepancies=[LLY])
    _report(session, at=_sgt(14, 4, 15), allowed=True)
    _report(session, at=_sgt(15, 4, 15), allowed=False, discrepancies=[LLY])
    facts = collect_facts(session, mode="paper", now=_sgt(15, 4, 52))

    assert facts.disabled_sessions == 1
    assert alert_body(facts) == ""


def test_a_missed_session_does_not_render_in_the_healthy_shape(session):
    """The bound is under 24h on purpose. At 26 a run that never wrote left
    yesterday's reading at ~24.6h and rendered as `status: ok`, which is
    exactly "the last thing we heard was good news and it is old"."""
    _report(session, at=_sgt(14, 4, 15), allowed=True)
    facts = collect_facts(session, mode="paper", now=_sgt(15, 4, 52))

    assert facts.status == "stale"
    assert "status: ok ·" not in render_section(facts)
    assert alert_body(facts) != ""


def test_todays_own_reading_is_fresh(session):
    """The normal morning: the 04:15 run wrote 37 minutes ago."""
    _report(session, at=_sgt(15, 4, 15), allowed=True)
    facts = collect_facts(session, mode="paper", now=_sgt(15, 4, 52))
    assert facts.status == "ok"


def test_an_overlong_body_is_capped_so_the_send_is_not_dropped(session):
    """The count cap bounds how many discrepancies are named, not how long each
    one is. Telegram rejects over 4096 chars and the send discards curl's
    status, so the ceiling has to be unconditional."""
    huge = dict(LLY, symbol="X" * 5000)
    _report(session, at=_sgt(14, 4, 15), allowed=False, discrepancies=[huge])
    _report(session, at=_sgt(15, 4, 15), allowed=False, discrepancies=[huge])
    body = alert_body(collect_facts(session, mode="paper", now=_sgt(15, 4, 52)))

    assert len(body) <= 3500
    assert body.endswith("…")
