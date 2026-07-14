from __future__ import annotations

import numpy as np
import pandas as pd


def cross_sectional_rank(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.rank(axis=1, method="average", pct=True)


def trailing_return(close: pd.DataFrame, periods: int) -> pd.DataFrame:
    if periods < 1:
        raise ValueError("periods must be at least 1")
    return close / close.shift(periods) - 1.0


def rolling_volatility(close: pd.DataFrame, periods: int) -> pd.DataFrame:
    if periods < 1:
        raise ValueError("periods must be at least 1")
    returns = close.pct_change(fill_method=None)
    return returns.rolling(periods, min_periods=periods).std() * np.sqrt(252.0)


def rolling_dollar_volume(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    periods: int,
) -> pd.DataFrame:
    if periods < 1:
        raise ValueError("periods must be at least 1")
    return (close * volume).rolling(periods, min_periods=periods).mean()
