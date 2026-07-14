from __future__ import annotations

import pandas as pd

from research.factors.operations import cross_sectional_rank, trailing_return


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
