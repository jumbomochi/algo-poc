from __future__ import annotations

import numpy as np
import pandas as pd


def cross_sectional_rank(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.rank(axis=1, method="average", pct=True)


def cross_sectional_zscore(
    frame: pd.DataFrame,
    *,
    min_count: int = 2,
) -> pd.DataFrame:
    """Normalize each date across tickers without filling missing inputs.

    Rows below ``min_count`` finite observations remain entirely missing. When
    the observed values have zero dispersion, their normalized values are zero
    while originally missing cells remain missing.
    """
    if min_count < 2:
        raise ValueError("min_count must be at least 2")
    numeric = frame.astype(float)
    counts = numeric.count(axis=1)
    means = numeric.mean(axis=1)
    dispersion = numeric.std(axis=1, ddof=0)
    result = numeric.sub(means, axis=0).div(dispersion, axis=0)
    zero_dispersion = dispersion.eq(0) & counts.ge(min_count)
    result.loc[zero_dispersion] = 0.0
    result.loc[counts.lt(min_count)] = np.nan
    return result.where(numeric.notna())


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
