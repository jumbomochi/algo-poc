from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from scripts.sentiment_eval import (
    GAP_FRACTION_LIMIT,
    GATE_MAX_P,
    GATE_MIN_ABNORMAL,
    GATE_MIN_EVENTS,
    GATE_MIN_IC,
    GATE_MIN_SESSIONS,
    GATE_MIN_TSTAT,
    evaluate_source,
    gap_report,
    judge,
)
from sentiment.evaluation import EventStudyResult, ICResult
from shared.models import Base, SentimentDaily

import pandas as pd

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_gate_constants_are_the_spec_values():
    assert GATE_MIN_IC == 0.03
    assert GATE_MIN_TSTAT == 2.0
    assert GATE_MIN_ABNORMAL == 0.003
    assert GATE_MAX_P == 0.05
    assert GATE_MIN_EVENTS == 30


def test_judge_pass_via_ic():
    ic = [ICResult(horizon=1, mean_ic=0.05, t_stat=2.5, n_days=60)]
    events = [EventStudyResult(horizon=1, n_events=3, mean_abnormal_return=0.0, p_value=0.9)]
    assert judge(ic, events, n_sessions_with_data=60) == "PASS"


def test_judge_pass_via_event_study():
    ic = [ICResult(horizon=1, mean_ic=0.0, t_stat=0.1, n_days=60)]
    events = [EventStudyResult(horizon=3, n_events=35, mean_abnormal_return=0.004, p_value=0.01)]
    assert judge(ic, events, n_sessions_with_data=60) == "PASS"


def test_judge_fail():
    ic = [ICResult(horizon=1, mean_ic=0.01, t_stat=0.5, n_days=60)]
    events = [EventStudyResult(horizon=1, n_events=35, mean_abnormal_return=0.001, p_value=0.4)]
    assert judge(ic, events, n_sessions_with_data=60) == "FAIL"


def test_judge_insufficient_data():
    ic = [ICResult(horizon=1, mean_ic=0.10, t_stat=3.0, n_days=5)]
    events = []
    assert judge(ic, events, n_sessions_with_data=5) == "INSUFFICIENT_DATA"
    assert GATE_MIN_SESSIONS > 5


def test_gap_report():
    daily = pd.DataFrame(
        {
            "ticker": ["AAPL", "AAPL"],
            "session_date": [date(2026, 8, 3), date(2026, 8, 5)],
            "score": [0.1, 0.2],
        }
    )
    sessions = [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5), date(2026, 8, 6)]
    n_gaps, fraction = gap_report(daily, sessions)
    assert n_gaps == 2
    assert fraction == 0.5


def _weekdays(n: int, start: date = date(2026, 1, 5)) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def test_evaluate_source_trailing_darkness_is_a_gap(db_session):
    """A source that collects cleanly then goes permanently dark must show
    up as a growing gap fraction for the un-covered trailing sessions —
    not get clipped to its own last-seen date, which would hide the
    outage and let it silently escape the exit-code-2 collection alarm."""
    sessions = _weekdays(2 * GATE_MIN_SESSIONS)
    bars = {"AAPL": [{"date": d, "close": 100.0 + i} for i, d in enumerate(sessions)]}

    # Source collects for the first half of the window, then goes dark.
    covered = sessions[:GATE_MIN_SESSIONS]
    for i, d in enumerate(covered):
        db_session.add(
            SentimentDaily(
                ticker="AAPL",
                session_date=d,
                source="reddit",
                message_count=1,
                mean_score=0.1,
                weighted_score=0.1,
                score_std=0.0,
                unique_authors=1,
                sentiment_zscore=0.0,
                volume_zscore=0.0,
                computed_at=NOW,
            )
        )
    db_session.commit()

    verdict = evaluate_source(db_session, "reddit", bars)

    assert verdict.gap_fraction == pytest.approx(0.5)
    assert verdict.gap_fraction > GAP_FRACTION_LIMIT
    assert verdict.verdict != "INSUFFICIENT_DATA"
