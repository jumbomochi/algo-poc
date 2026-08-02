# research/evaluation/portfolio.py
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class PortfolioSeries:
    returns: pd.Series
    turnover: pd.Series
    ic: pd.Series


def quantile_long_only(
    scores: pd.DataFrame,
    forward: pd.DataFrame,
    quantile: float,
    rebalance: int,
    min_names: int = 5,
) -> PortfolioSeries:
    if not 0.0 < quantile <= 1.0:
        raise ValueError("quantile must be in (0, 1]")
    if rebalance < 1:
        raise ValueError("rebalance must be at least 1")

    dates = scores.index
    rets: list[float] = []
    turns: list[float] = []
    ics: list[float] = []
    index: list[pd.Timestamp] = []
    prev_held: set[str] = set()

    for i in range(0, len(dates), rebalance):
        day = dates[i]
        score_row = scores.loc[day].dropna()
        fwd_row = forward.loc[day].dropna()
        valid = score_row.index.intersection(fwd_row.index)
        if len(valid) < min_names:
            continue
        score_row = score_row[valid]
        fwd_row = fwd_row[valid]
        k = max(1, int(len(score_row) * quantile))
        held = set(score_row.sort_values(ascending=False).index[:k])
        rets.append(float(fwd_row[sorted(held)].mean()))
        denom = max(1, len(held))
        turns.append(len(held.symmetric_difference(prev_held)) / denom)
        ics.append(float(score_row.rank().corr(fwd_row.rank())))
        index.append(day)
        prev_held = held

    return PortfolioSeries(
        returns=pd.Series(rets, index=index, dtype=float),
        turnover=pd.Series(turns, index=index, dtype=float),
        ic=pd.Series(ics, index=index, dtype=float),
    )
