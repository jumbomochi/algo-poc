# research/evaluation/portfolio.py
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class PortfolioSeries:
    returns: pd.Series
    turnover: pd.Series
    ic: pd.Series


def top_quantile_names(
    score_row: pd.Series, forward_row: pd.Series, quantile: float, min_names: int
) -> list[str]:
    """Sorted top-quantile ticker names actually holdable (valid score AND forward).

    Returns [] if the number of names with both a valid score and a valid
    forward return falls below ``min_names``. This is the single source of
    truth for "what would the portfolio actually hold on this date", shared
    between the portfolio construction path and the evaluator's recorded
    selection so the two never diverge.
    """
    valid = score_row.dropna().index.intersection(forward_row.dropna().index)
    if len(valid) < min_names:
        return []
    row = score_row[valid]
    k = max(1, int(len(row) * quantile))
    return sorted(row.sort_values(ascending=False).index[:k])


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
        score_row = scores.loc[day]
        fwd_row = forward.loc[day]
        names = top_quantile_names(score_row, fwd_row, quantile, min_names)
        if not names:
            continue
        held = set(names)
        valid = score_row.dropna().index.intersection(fwd_row.dropna().index)
        valid_score_row = score_row[valid]
        valid_fwd_row = fwd_row[valid]
        rets.append(float(fwd_row[sorted(held)].mean()))
        denom = max(1, len(held))
        turns.append(len(held.symmetric_difference(prev_held)) / denom)
        ics.append(float(valid_score_row.rank().corr(valid_fwd_row.rank())))
        index.append(day)
        prev_held = held

    return PortfolioSeries(
        returns=pd.Series(rets, index=index, dtype=float),
        turnover=pd.Series(turns, index=index, dtype=float),
        ic=pd.Series(ics, index=index, dtype=float),
    )
