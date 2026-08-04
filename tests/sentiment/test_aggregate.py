from __future__ import annotations

import statistics
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from sentiment.aggregate import best_score, rebuild_daily, session_date_for
from shared.market_calendar import MarketCalendar
from shared.models import Base, SentimentDaily, SentimentMessage

CAL = MarketCalendar()


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def add_message(session, ticker, posted_at, provider=None, local=None, likes=0, author="a", source_id=None):
    session.add(
        SentimentMessage(
            source="stocktwits",
            source_id=source_id or f"{ticker}-{posted_at.isoformat()}-{author}",
            ticker=ticker,
            author=author,
            text="x",
            posted_at=posted_at,
            collected_at=posted_at,
            provider_score=provider,
            local_score=local,
            score_model="vader",
            meta={"likes": likes},
        )
    )
    session.commit()


def test_session_date_intraday():
    # Tuesday 2026-08-04 14:00 UTC (10:00 ET, market open) -> same session
    dt = datetime(2026, 8, 4, 14, 0, tzinfo=timezone.utc)
    assert session_date_for(dt, CAL) == date(2026, 8, 4)


def test_session_date_after_close_rolls_forward():
    # Tuesday 2026-08-04 21:00 UTC (17:00 ET, after close) -> Wednesday
    dt = datetime(2026, 8, 4, 21, 0, tzinfo=timezone.utc)
    assert session_date_for(dt, CAL) == date(2026, 8, 5)


def test_session_date_weekend_rolls_to_monday():
    # Saturday 2026-08-01 -> Monday 2026-08-03
    dt = datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc)
    assert session_date_for(dt, CAL) == date(2026, 8, 3)


def test_best_score_prefers_provider():
    assert best_score(1.0, 0.2) == 1.0
    assert best_score(None, 0.2) == 0.2
    assert best_score(None, None) is None


def test_rebuild_daily_aggregates(session):
    morning = datetime(2026, 8, 4, 14, 0, tzinfo=timezone.utc)
    add_message(session, "AAPL", morning, provider=1.0, likes=4, author="a")
    add_message(session, "AAPL", morning, provider=None, local=-1.0, likes=0, author="b")
    n = rebuild_daily(session, CAL, date(2026, 8, 4), date(2026, 8, 4))
    assert n == 1
    row = session.query(SentimentDaily).one()
    assert row.ticker == "AAPL"
    assert row.session_date == date(2026, 8, 4)
    assert row.message_count == 2
    assert row.unique_authors == 2
    assert row.mean_score == pytest.approx(0.0)  # (1.0 + -1.0) / 2
    # weights: 1+4=5 for +1.0, 1+0=1 for -1.0 -> (5 - 1) / 6
    assert row.weighted_score == pytest.approx(4 / 6)
    assert row.sentiment_zscore is None  # not enough baseline days


def test_rebuild_daily_is_upsert_not_duplicate(session):
    morning = datetime(2026, 8, 4, 14, 0, tzinfo=timezone.utc)
    add_message(session, "AAPL", morning, provider=1.0)
    rebuild_daily(session, CAL, date(2026, 8, 4), date(2026, 8, 4))
    rebuild_daily(session, CAL, date(2026, 8, 4), date(2026, 8, 4))
    assert session.query(SentimentDaily).count() == 1


def test_zscore_with_planted_baseline(session):
    # ~22 quiet trading days with mild variance (constant baselines have
    # std == 0, which by design yields z-score None), then a loud +1.0 day x5
    sessions = [d for d in (date(2026, 6, 1 + i) for i in range(0, 30))
                if CAL.is_trading_day(d)]
    for i, d in enumerate(sessions):
        dt = datetime(d.year, d.month, d.day, 15, 0, tzinfo=timezone.utc)
        quiet_score = 0.1 if i % 2 == 0 else -0.1
        add_message(session, "AAPL", dt, provider=quiet_score, author=f"u{i}", source_id=f"q{i}")
        if i % 2 == 0:  # vary volume too: alternate 1 vs 2 messages/day
            add_message(session, "AAPL", dt, provider=quiet_score, author=f"u{i}b", source_id=f"q{i}b")
    loud_day = date(2026, 7, 6)  # Monday
    assert CAL.is_trading_day(loud_day)
    for i in range(5):
        dt = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)
        add_message(session, "AAPL", dt, provider=1.0, author=f"loud{i}", source_id=f"l{i}")
    assert len(sessions) >= 20  # enough baseline rows for min_baseline_days
    rebuild_daily(session, CAL, sessions[0], loud_day, baseline_days=60, min_baseline_days=20)
    loud = (
        session.query(SentimentDaily)
        .filter(SentimentDaily.session_date == loud_day)
        .one()
    )
    assert loud.volume_zscore is not None and loud.volume_zscore > 2
    assert loud.sentiment_zscore is not None and loud.sentiment_zscore > 2


def test_volume_baseline_zero_fills_quiet_sessions(session):
    # Sparse mentions: real activity on only every OTHER trading session,
    # nothing at all (no row) on the rest — a rows-only baseline would only
    # ever see the constant "2 messages" days and (with std == 0) report
    # volume_zscore as None for the spike. Zero-filling the quiet sessions
    # is what makes the spike detectable at all.
    all_days = [d for d in (date(2026, 6, 1 + i) for i in range(0, 26)) if CAL.is_trading_day(d)]
    sessions = all_days[:-1]
    spike_day = all_days[-1]
    assert len(sessions) >= 10

    active_count = {}
    for i, d in enumerate(sessions):
        if i % 2 == 0:
            dt = datetime(d.year, d.month, d.day, 15, 0, tzinfo=timezone.utc)
            add_message(session, "AAPL", dt, provider=0.1, author=f"u{i}a", source_id=f"s{i}a")
            add_message(session, "AAPL", dt, provider=0.1, author=f"u{i}b", source_id=f"s{i}b")
            active_count[d] = 2

    spike_dt = datetime(spike_day.year, spike_day.month, spike_day.day, 15, 0, tzinfo=timezone.utc)
    for i in range(8):
        add_message(session, "AAPL", spike_dt, provider=0.5, author=f"spike{i}", source_id=f"spike{i}")

    rebuild_daily(session, CAL, sessions[0], spike_day, baseline_days=60, min_baseline_days=5)

    loud = (
        session.query(SentimentDaily)
        .filter(SentimentDaily.ticker == "AAPL", SentimentDaily.session_date == spike_day)
        .one()
    )
    assert loud.message_count == 8

    # Independently reconstruct the expected zero-filled baseline: every
    # session strictly before the spike, real count where a message landed,
    # 0.0 where it didn't (this is exactly what the fix is meant to produce —
    # the rows-only method would have silently dropped the zero days).
    expected_baseline = [float(active_count.get(d, 0)) for d in sessions]
    expected_mean = statistics.fmean(expected_baseline)
    expected_std = statistics.pstdev(expected_baseline)
    expected_z = (8.0 - expected_mean) / expected_std

    # Under the old rows-only method the baseline would have been a constant
    # [2.0, 2.0, ...] (std == 0), which by design yields volume_zscore=None —
    # the zero-fill is what makes this a real, non-None number.
    assert loud.volume_zscore is not None
    assert loud.volume_zscore == pytest.approx(expected_z)
    assert loud.volume_zscore > 2
