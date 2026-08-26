"""The registry of sessions on which no graded paper run happened.

These tests pin the two things KAN-67 decided: which dates are accepted as
permanently absent, and that Option A stayed chosen — no dated replay was
built, so no synthetic row can be produced for them.
"""

from __future__ import annotations

import argparse
import pathlib
from datetime import date

from shared.market_calendar import MarketCalendar
from shared.absent_sessions import (
    DECISION,
    KNOWN_ABSENT_SESSIONS,
    absent_session,
    absent_sessions_in,
    known_absent_dates,
)


def _run_paper_parser() -> argparse.ArgumentParser:
    from scripts.run_paper import _parser

    return _parser(
        default_db_url="postgresql://algo:algo@localhost:5432/algo_poc",
        default_redis_url="redis://localhost:6379/0",
    )


def test_the_registry_records_both_accepted_gaps() -> None:
    assert known_absent_dates() == {date(2026, 8, 13), date(2026, 8, 18)}


def test_every_entry_carries_a_cause_and_a_reference() -> None:
    for entry in KNOWN_ABSENT_SESSIONS:
        assert entry.cause.strip(), f"{entry.session_date} has no recorded cause"
        assert entry.reference.strip(), f"{entry.session_date} has no reference"


def test_absent_session_looks_one_date_up() -> None:
    entry = absent_session(date(2026, 8, 18))
    assert entry is not None
    assert "IB" in entry.cause
    assert absent_session(date(2026, 8, 19)) is None


def test_absent_sessions_in_is_range_bounded_and_ascending() -> None:
    found = absent_sessions_in(date(2026, 8, 11), date(2026, 8, 21))
    assert [entry.session_date for entry in found] == [
        date(2026, 8, 13),
        date(2026, 8, 18),
    ]

    assert absent_sessions_in(date(2026, 8, 14), date(2026, 8, 17)) == []


def test_the_decision_record_names_option_a_and_both_dates() -> None:
    assert "2026-08-13" in DECISION
    assert "2026-08-18" in DECISION
    assert "Option A" in DECISION


def test_the_decision_record_states_the_window_correctly() -> None:
    """The count in the prose has to match the calendar it describes.

    An artifact whose whole job is to be the record cannot be wrong about how
    many sessions it is talking about.
    """
    sessions = MarketCalendar().trading_sessions(
        date(2026, 8, 11), date(2026, 8, 21)
    )
    assert len(sessions) == 9
    assert f"{len(sessions)} NYSE sessions" in DECISION


def test_run_paper_offers_no_dated_replay_flag() -> None:
    """Option A, asserted executably: the backfill path does not exist.

    Building ``--as-of`` would put look-ahead risk into the very runner that
    produces the gate evidence, and would stamp a simulated row into a table
    where every other row is a live paper session. If someone adds dated
    replay later it must be scoped as a capability and this test updated
    deliberately — not as a side effect of a backfill.
    """
    parser = _run_paper_parser()

    flags = {
        option for action in parser._actions for option in action.option_strings
    }
    assert "--as-of" not in flags
    assert "--as-of-date" not in flags
    assert "--date" not in flags


def test_no_equity_snapshot_backfill_script_exists() -> None:
    """The other shape Option B could take: a standalone backfill script.

    ``scripts/ops/`` already contains ``backfill_model_hashes.py``, so the
    pattern is available and a flag check alone would not catch it.
    """
    ops = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "ops"
    writers = [
        path.name
        for path in ops.glob("*.py")
        if "EquitySnapshot(" in path.read_text()
    ]
    assert writers == [], (
        "a script under scripts/ops now constructs EquitySnapshot rows; if this "
        "is dated replay it must be scoped as a capability, not a backfill "
        "(KAN-67 chose Option A)"
    )


def test_every_absent_date_is_a_real_nyse_session() -> None:
    """A holiday or weekend entry would be silently inert.

    ``blindness`` only ever asks the calendar about sessions it yields, so a
    registered non-session is never consulted — the operator would believe a
    gap was accepted while nothing had changed. Checked against the real
    calendar, not ``weekday()``, because a Thursday can be Thanksgiving.
    """
    calendar = MarketCalendar()
    for entry in KNOWN_ABSENT_SESSIONS:
        assert calendar.is_trading_day(entry.session_date), entry.session_date


def test_the_registry_is_sorted_and_has_no_duplicates() -> None:
    dates = [entry.session_date for entry in KNOWN_ABSENT_SESSIONS]
    assert dates == sorted(dates)
    assert len(dates) == len(set(dates))
