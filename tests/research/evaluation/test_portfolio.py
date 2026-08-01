# tests/research/evaluation/test_portfolio.py
from __future__ import annotations

import pandas as pd

from research.evaluation.portfolio import quantile_long_only


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
