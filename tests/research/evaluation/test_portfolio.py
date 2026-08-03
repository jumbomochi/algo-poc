# tests/research/evaluation/test_portfolio.py
from __future__ import annotations

import pandas as pd

from research.evaluation.portfolio import quantile_long_only, top_quantile_names


def _frame(rows: dict[str, list[float]]):
    idx = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"])
    return pd.DataFrame(rows, index=idx)


def test_top_quantile_book_return_and_monotone_ic():
    # Higher score -> higher forward return, so IC should be +1 each date.
    scores = _frame({"A": [3.0, 3.0, 3.0], "B": [2.0, 2.0, 2.0], "C": [1.0, 1.0, 1.0], "D": [0.0, 0.0, 0.0]})
    forward = _frame({"A": [0.30, 0.30, 0.30], "B": [0.20, 0.20, 0.20], "C": [0.10, 0.10, 0.10], "D": [0.00, 0.00, 0.00]})
    series = quantile_long_only(scores, forward, quantile=0.5, rebalance=1, min_names=4)
    # top 50% of 4 names = A,B -> mean forward 0.25
    assert round(series.returns.iloc[0], 6) == 0.25
    assert round(series.ic.iloc[0], 6) == 1.0
    assert series.turnover.iloc[0] == 1.0  # first rebalance: everything is new


def test_skips_dates_below_min_coverage():
    scores = _frame({"A": [1.0, 1.0, 1.0], "B": [2.0, 2.0, 2.0]})
    forward = _frame({"A": [0.1, 0.1, 0.1], "B": [0.2, 0.2, 0.2]})
    series = quantile_long_only(scores, forward, quantile=0.5, rebalance=1, min_names=5)
    assert len(series.returns) == 0


def test_selection_matches_portfolio_holdings_with_trailing_nan_forward():
    # E has the highest score on the target date but no forward return (e.g. a
    # trailing-horizon row / missing bar). The held set must be computed from
    # score AND forward validity together, so E must be excluded from the pick.
    idx = pd.to_datetime(["2026-01-05"])
    scores = pd.DataFrame(
        {"E": [5.0], "A": [4.0], "B": [3.0], "C": [2.0], "D": [1.0]}, index=idx
    )
    forward = pd.DataFrame(
        {"E": [float("nan")], "A": [0.30], "B": [0.20], "C": [0.10], "D": [0.05]}, index=idx
    )
    score_row = scores.loc[idx[0]]
    forward_row = forward.loc[idx[0]]

    # top 40% of the 4 holdable names (A,B,C,D) = 1 name -> A, never E.
    held = top_quantile_names(score_row, forward_row, quantile=0.4, min_names=4)
    assert held == ["A"]
    assert "E" not in held

    # Below-coverage (fewer than min_names holdable names) returns [].
    assert top_quantile_names(score_row, forward_row, quantile=0.4, min_names=5) == []

    # quantile_long_only must agree: the actually-scored book excludes E too.
    series = quantile_long_only(scores, forward, quantile=0.4, rebalance=1, min_names=4)
    assert round(series.returns.iloc[0], 6) == 0.30
