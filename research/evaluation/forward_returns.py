from __future__ import annotations

import pandas as pd

from research.factors.contracts import FactorPanel


def forward_excess_returns(panel: FactorPanel, horizon: int) -> pd.DataFrame:
    """h-day forward return per ticker, minus the equal-weight universe forward return.

    Causal: the value anchored at date t uses close[t] and close[t+horizon] only.
    Rows without a t+horizon bar are NaN.
    """
    if horizon < 1:
        raise ValueError("horizon must be at least 1")
    close = panel.field("close")
    forward = close.shift(-horizon) / close - 1.0
    universe = forward.mean(axis=1, skipna=True)
    return forward.sub(universe, axis=0)
