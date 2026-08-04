from __future__ import annotations

from datetime import date

from scripts.sentiment_eval import (
    GATE_MAX_P,
    GATE_MIN_ABNORMAL,
    GATE_MIN_EVENTS,
    GATE_MIN_IC,
    GATE_MIN_SESSIONS,
    GATE_MIN_TSTAT,
    gap_report,
    judge,
)
from sentiment.evaluation import EventStudyResult, ICResult

import pandas as pd


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
