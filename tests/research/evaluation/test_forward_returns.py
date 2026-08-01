from __future__ import annotations

from datetime import date

import pandas as pd

from research.evaluation.forward_returns import forward_excess_returns
from research.factors.panel import build_factor_panel


def _bars(closes: dict[str, list[float]], start=date(2026, 1, 5)):
    days = pd.bdate_range(start, periods=max(len(v) for v in closes.values()))
    return {
        ticker: [
            {"date": days[i].date(), "open": c, "high": c, "low": c, "close": c, "volume": 1_000}
            for i, c in enumerate(series)
        ]
        for ticker, series in closes.items()
    }


def test_excess_is_relative_to_equal_weight_universe():
    # A doubles (+100%), B flat (0%). Universe mean over 1-day fwd = +50% on day 0.
    panel = build_factor_panel(_bars({"A": [10, 20], "B": [10, 10]}))
    excess = forward_excess_returns(panel, horizon=1)
    assert excess.loc[panel.field("close").index[0], "A"] == 0.5
    assert excess.loc[panel.field("close").index[0], "B"] == -0.5
    # last row has no t+1 bar
    assert excess.iloc[-1].isna().all()


def test_future_mutation_cannot_change_earlier_forward_returns():
    base = forward_excess_returns(build_factor_panel(_bars({"A": [10, 11, 12, 13, 14], "B": [10, 10, 10, 10, 10]})), horizon=1)
    mutated = _bars({"A": [10, 11, 12, 13, 99999.0], "B": [10, 10, 10, 10, 10]})
    after = forward_excess_returns(build_factor_panel(mutated), horizon=1)
    # rows strictly before (len-1-horizon) index cannot see the mutated final close
    pd.testing.assert_frame_equal(base.iloc[:2], after.iloc[:2])
