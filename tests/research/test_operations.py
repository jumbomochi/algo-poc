from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.factors.operations import (
    cross_sectional_rank,
    rolling_dollar_volume,
    rolling_volatility,
    trailing_return,
)


def test_cross_sectional_rank_is_per_date_and_bounded():
    frame = pd.DataFrame(
        {"A": [1.0, 3.0], "B": [2.0, 1.0]},
        index=pd.to_datetime(["2026-01-02", "2026-01-05"]),
    )
    ranked = cross_sectional_rank(frame)
    assert ranked.loc["2026-01-02", "B"] == 1.0
    assert ranked.loc["2026-01-05", "A"] == 1.0
    assert ranked.min().min() >= 0.0


def test_trailing_return_does_not_read_future_rows():
    original = pd.DataFrame(
        {"A": [100.0, 110.0, 121.0]},
        index=pd.date_range("2026-01-01", periods=3),
    )
    mutated = original.copy()
    mutated.loc[mutated.index[-1], "A"] = 9999.0
    before = trailing_return(original, periods=1)
    after = trailing_return(mutated, periods=1)
    pd.testing.assert_series_equal(before.iloc[:2, 0], after.iloc[:2, 0])


def test_rolling_volatility_uses_trailing_returns_and_annualizes():
    close = pd.DataFrame(
        {"A": [100.0, 110.0, 99.0]},
        index=pd.date_range("2026-01-01", periods=3),
    )

    result = rolling_volatility(close, periods=2)

    assert result.iloc[:2, 0].isna().all()
    expected = np.std([0.1, -0.1], ddof=1) * np.sqrt(252.0)
    assert result.iloc[2, 0] == pytest.approx(expected)


def test_rolling_dollar_volume_uses_trailing_mean():
    index = pd.date_range("2026-01-01", periods=3)
    close = pd.DataFrame({"A": [10.0, 20.0, 30.0]}, index=index)
    volume = pd.DataFrame({"A": [100.0, 200.0, 300.0]}, index=index)

    result = rolling_dollar_volume(close, volume, periods=2)

    expected = pd.DataFrame({"A": [np.nan, 2500.0, 6500.0]}, index=index)
    pd.testing.assert_frame_equal(result, expected)


@pytest.mark.parametrize("operation", [rolling_volatility, rolling_dollar_volume])
def test_rolling_operations_reject_non_positive_periods(operation):
    frame = pd.DataFrame({"A": [1.0]})
    args = (frame,) if operation is rolling_volatility else (frame, frame)

    with pytest.raises(ValueError, match="periods must be at least 1"):
        operation(*args, periods=0)
