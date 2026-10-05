"""KAN-103: the evidence readers key by the US session valued.

Covers ``shared/session_dating.equity_by_session``, the evidence store's
``equity_series``, the digest's window end, and the shadow fingerprint's
dating version.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backtest.shadow_artifact import (
    SHADOW_DATING_VERSION,
    SHADOW_ID_PREFIX,
    shadow_id_for,
)
from scripts.ops.evidence_digest import resolve_window
from shared.evidence_store import blindness, equity_series, session_snapshots
from shared.market_calendar import MarketCalendar
from shared.models.base import Base
from shared.models.equity_snapshot import EquitySnapshot
from shared.models.evidence import DivergenceDaily, DivergenceStatus
from shared.session_dating import equity_by_session, unstamped_run_dates

SGT = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _snap(db, portfolio, run, session, equity, market_value=0.0):
    db.add(EquitySnapshot(
        portfolio=portfolio, date=run, session_date=session, equity=equity,
        cash=equity - market_value, market_value=market_value, created_at=NOW,
    ))
    db.flush()


# --------------------------------------------------------- equity_by_session


def test_equity_by_session_keys_by_session_and_keeps_the_later_run():
    rows = [
        {"date": "2026-09-05", "session_date": "2026-09-04", "equity": 100.0},
        {"date": "2026-09-08", "session_date": "2026-09-04", "equity": 101.0},
        {"date": "2026-09-09", "session_date": None, "equity": 999.0},
        {"date": "2026-09-10", "session_date": "2026-09-09", "equity": 102.0},
    ]
    assert equity_by_session(rows) == {
        date(2026, 9, 4): 101.0,
        date(2026, 9, 9): 102.0,
    }
    assert unstamped_run_dates(rows) == [date(2026, 9, 9)]


def test_equity_by_session_is_order_independent():
    rows = [
        {"date": date(2026, 9, 8), "session_date": date(2026, 9, 4), "equity": 2.0},
        {"date": date(2026, 9, 5), "session_date": date(2026, 9, 4), "equity": 1.0},
    ]
    assert equity_by_session(rows) == {date(2026, 9, 4): 2.0}


# ------------------------------------------------------------- equity_series


def test_equity_series_is_dated_by_session_one_row_per_sleeve(db):
    # Labor Day: Sat 09-05 and Tue 09-08 both value Fri 09-04.
    _snap(db, "momentum", date(2026, 9, 5), date(2026, 9, 4), 1_000.0, 100.0)
    _snap(db, "momentum", date(2026, 9, 8), date(2026, 9, 4), 1_000.0, 100.0)
    _snap(db, "sector_rotation", date(2026, 9, 5), date(2026, 9, 4), 500.0)
    _snap(db, "sector_rotation", date(2026, 9, 8), date(2026, 9, 4), 500.0)
    _snap(db, "momentum", date(2026, 9, 9), date(2026, 9, 8), 1_010.0, 100.0)
    _snap(db, "sector_rotation", date(2026, 9, 9), date(2026, 9, 8), 505.0)
    # A partial-bar row: no session, not in the series.
    _snap(db, "momentum", date(2026, 9, 10), None, 9_999.0, 9_999.0)
    # A drill: never account equity.
    _snap(db, "__drill__", date(2026, 9, 9), date(2026, 9, 8), 50_000.0)

    rows = equity_series(db, start=date(2026, 9, 1), end=date(2026, 9, 30))

    assert rows == [
        (date(2026, 9, 4), 1_500.0, 100.0),  # not 3_000: counted once
        (date(2026, 9, 8), 1_515.0, 100.0),
    ]


def test_equity_series_bounds_by_session_not_run_date(db):
    # Run Tue 09-29 values Mon 09-28: in a window ending 09-28.
    _snap(db, "momentum", date(2026, 9, 29), date(2026, 9, 28), 1_000.0)
    _snap(db, "momentum", date(2026, 9, 30), date(2026, 9, 29), 1_001.0)

    rows = equity_series(db, start=date(2026, 9, 28), end=date(2026, 9, 28))

    assert rows == [(date(2026, 9, 28), 1_000.0, 0.0)]


def test_session_snapshots_takes_the_later_run_of_a_revalued_session(db):
    _snap(db, "momentum", date(2026, 8, 8), date(2026, 8, 7), 1_000.0)
    _snap(db, "momentum", date(2026, 8, 9), date(2026, 8, 7), 1_000.5)

    by_session = session_snapshots(db, start=date(2026, 8, 1), end=date(2026, 8, 31))

    assert by_session[date(2026, 8, 7)]["momentum"].date == date(2026, 8, 9)


# ------------------------------------------------------------ digest window


def test_the_monday_digest_stops_at_fridays_close():
    """Mon 2026-10-05 08:00 SGT: the US Monday session has not opened."""
    now = datetime(2026, 10, 5, 8, 0, tzinfo=SGT)

    as_of, window_start = resolve_window(None, window_days=7, now=now)

    assert as_of == date(2026, 10, 2)
    assert window_start == date(2026, 9, 28)
    sessions = MarketCalendar().trading_sessions(window_start, as_of)
    assert len(sessions) == 5  # Mon 09-28 .. Fri 10-02, not six


def test_an_explicit_past_as_of_is_left_alone():
    now = datetime(2026, 10, 5, 8, 0, tzinfo=SGT)

    assert resolve_window(date(2026, 8, 17), window_days=7, now=now) == (
        date(2026, 8, 17), date(2026, 8, 10),
    )


def test_an_explicit_future_as_of_is_clamped_to_the_last_close():
    now = datetime(2026, 10, 5, 8, 0, tzinfo=SGT)

    as_of, _ = resolve_window(date(2026, 10, 9), window_days=7, now=now)

    assert as_of == date(2026, 10, 2)


def test_the_2026_10_05_digest_is_blind_on_one_session_not_three(db):
    """The decision doc's worked example, after the fix.

    Verdicts dated by session: Mon 09-28 graded (by Tue 09-29's run), Tue 09-29,
    Thu 10-01 (by the 10-02 08:43 catch-up), Fri 10-02. Wed 09-30 was never
    valued — the SGT 10-01 run did not happen.
    """
    for day in (date(2026, 9, 28), date(2026, 9, 29), date(2026, 10, 1),
                date(2026, 10, 2)):
        db.add(DivergenceDaily(
            sleeve="momentum", session_date=day,
            status=DivergenceStatus.OK.value, baseline_id="shadow:x",
            window_sessions=30, threshold=0.2, created_at=NOW,
        ))
    db.flush()
    as_of, window_start = resolve_window(
        None, window_days=7, now=datetime(2026, 10, 5, 8, 0, tzinfo=SGT)
    )

    result = blindness(
        db, start=window_start, end=as_of, sleeves=["momentum"],
        baseline_id="shadow:x", calendar=MarketCalendar(),
    )

    assert result.blind_sessions == [date(2026, 9, 30)]


# ------------------------------------------------------------ shadow identity


class _Sleeve:
    def __init__(self, params):
        self.shadow_params = params


def _pre_kan103_id(portfolios, *, whole_shares=False) -> str:
    """``shadow_id_for`` exactly as it was before the dating version."""
    fingerprint = sorted(
        (name, json.dumps(s.shadow_params, sort_keys=True, default=str))
        for name, s in portfolios.items()
    )
    if whole_shares:
        fingerprint.append(("__sizing__", "whole_shares"))
    digest = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True).encode()
    ).hexdigest()[:16]
    return f"{SHADOW_ID_PREFIX}{digest}"


@pytest.mark.parametrize("whole_shares", [False, True])
def test_session_dated_verdicts_file_under_a_new_baseline_id(whole_shares):
    """Run-date-keyed and session-keyed verdicts must never share a streak."""
    roster = {"momentum": _Sleeve({"top_n": 5}), "tail": _Sleeve({"k": 1})}

    new = shadow_id_for(roster, whole_shares=whole_shares)

    assert new != _pre_kan103_id(roster, whole_shares=whole_shares)
    assert new.startswith(SHADOW_ID_PREFIX)
    assert new == shadow_id_for(dict(reversed(list(roster.items()))),
                                whole_shares=whole_shares)
    assert SHADOW_DATING_VERSION == "session_date"


# ------------------------------------------------------------- pre-flight


def test_unstamped_snapshots_counts_by_run_date_and_skips_drills(db):
    from shared.evidence_store import unstamped_snapshots

    _snap(db, "momentum", date(2026, 9, 29), None, 1.0)
    _snap(db, "momentum", date(2026, 9, 30), None, 1.0)
    _snap(db, "momentum", date(2026, 10, 1), date(2026, 9, 30), 1.0)
    _snap(db, "__drill__", date(2026, 9, 30), None, 1.0)
    _snap(db, "momentum", date(2026, 8, 1), None, 1.0)  # before the span

    counted = unstamped_snapshots(
        db, start=date(2026, 9, 28), end=date(2026, 10, 3)
    )

    assert (counted.unstamped, counted.total) == (2, 3)
    assert counted.alarming
    assert "backfill not applied?" in counted.describe("this week")


def test_one_partial_bar_row_is_not_an_alarm(db):
    from shared.evidence_store import unstamped_snapshots

    for i in range(4):
        _snap(db, "momentum", date(2026, 9, 29) + timedelta(days=i),
              date(2026, 9, 28) + timedelta(days=i), 1.0)
    _snap(db, "momentum", date(2026, 10, 3), None, 1.0)

    assert not unstamped_snapshots(db, start=date(2026, 9, 28)).alarming


def test_the_digest_leads_with_an_unapplied_backfill(db):
    from scripts.ops.evidence_digest import (
        build_sources,
        collect_snapshot,
        render_digest,
    )

    for run in (date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 2)):
        _snap(db, "momentum", run, None, 1_000.0)
    as_of, window_start = date(2026, 10, 2), date(2026, 9, 28)

    snapshot = collect_snapshot(
        build_sources(
            db, redis_factory=lambda: None, as_of=as_of,
            window_start=window_start, calendar=MarketCalendar(),
        ),
        as_of=as_of, window_start=window_start,
    )
    body = render_digest(snapshot)

    first = body.splitlines()[0]
    assert first.startswith("🚨 UNSTAMPED — 3 of 3 equity snapshots this week")
    assert "backfill_snapshot_sessions.py" in first
    assert snapshot.equity is None  # the reason the equity line is empty
