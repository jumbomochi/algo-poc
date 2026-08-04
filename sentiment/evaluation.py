from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import pandas as pd
from scipy import stats


@dataclass(frozen=True)
class ICResult:
    horizon: int
    mean_ic: float
    t_stat: float
    n_days: int


@dataclass(frozen=True)
class EventStudyResult:
    horizon: int
    n_events: int
    mean_abnormal_return: float
    p_value: float


@dataclass(frozen=True)
class LagCorrelation:
    lag_days: int
    correlation: float


def forward_returns(
    bars_by_ticker: dict[str, list[dict]], horizon: int
) -> dict[str, dict[date, float]]:
    out: dict[str, dict[date, float]] = {}
    for ticker, bars in bars_by_ticker.items():
        by_day: dict[date, float] = {}
        for i in range(len(bars) - horizon):
            entry, exit_ = bars[i]["close"], bars[i + horizon]["close"]
            if entry:
                by_day[bars[i]["date"]] = exit_ / entry - 1
        out[ticker] = by_day
    return out


def information_coefficient(
    daily: pd.DataFrame,
    bars_by_ticker: dict[str, list[dict]],
    horizons: tuple[int, ...] = (1, 3, 5),
    min_tickers: int = 10,
) -> list[ICResult]:
    """Daily cross-sectional Spearman IC of score vs forward return."""
    results = []
    for horizon in horizons:
        fwd = forward_returns(bars_by_ticker, horizon)
        daily_ics = []
        for session_date, group in daily.groupby("session_date"):
            pairs = [
                (row["score"], fwd.get(row["ticker"], {}).get(session_date))
                for _, row in group.iterrows()
            ]
            pairs = [(s, r) for s, r in pairs if r is not None]
            if len(pairs) < min_tickers:
                continue
            ic, _ = stats.spearmanr([s for s, _ in pairs], [r for _, r in pairs])
            if not math.isnan(ic):
                daily_ics.append(ic)
        if not daily_ics:
            results.append(ICResult(horizon, 0.0, 0.0, 0))
            continue
        series = pd.Series(daily_ics)
        n = len(series)
        std = series.std(ddof=1)
        t_stat = float(series.mean() / (std / math.sqrt(n))) if std > 0 else 0.0
        results.append(ICResult(horizon, float(series.mean()), t_stat, n))
    return results


def event_study(
    daily: pd.DataFrame,
    bars_by_ticker: dict[str, list[dict]],
    horizons: tuple[int, ...] = (1, 3, 5),
    z_threshold: float = 2.0,
) -> list[EventStudyResult]:
    """Directional abnormal return after joint sentiment+volume spikes."""
    events = daily[
        (daily["sentiment_zscore"].abs() > z_threshold)
        & (daily["volume_zscore"] > z_threshold)
    ]
    results = []
    for horizon in horizons:
        fwd = forward_returns(bars_by_ticker, horizon)
        abnormal: list[float] = []
        for _, event in events.iterrows():
            session_date = event["session_date"]
            ticker_fwd = fwd.get(event["ticker"], {}).get(session_date)
            if ticker_fwd is None:
                continue
            universe_fwds = [
                by_day[session_date]
                for by_day in fwd.values()
                if session_date in by_day
            ]
            universe_mean = sum(universe_fwds) / len(universe_fwds)
            direction = 1.0 if event["score"] >= 0 else -1.0
            abnormal.append(direction * (ticker_fwd - universe_mean))
        if len(abnormal) < 2:
            results.append(EventStudyResult(horizon, len(abnormal), 0.0, 1.0))
            continue
        t_result = stats.ttest_1samp(abnormal, 0.0)
        results.append(
            EventStudyResult(
                horizon,
                len(abnormal),
                float(pd.Series(abnormal).mean()),
                float(t_result.pvalue),
            )
        )
    return results


def lead_lag(
    social_daily: pd.DataFrame, news_daily: pd.DataFrame, max_lag: int = 5
) -> list[LagCorrelation]:
    """Cross-correlation of universe-mean daily scores.

    Positive lag k: social score on day t vs news score on day t+k —
    high correlation at k > 0 means social leads news by k sessions.
    """
    social = social_daily.groupby("session_date")["score"].mean().sort_index()
    news = news_daily.groupby("session_date")["score"].mean().sort_index()
    results = []
    for lag in range(-max_lag, max_lag + 1):
        shifted_news = news.shift(-lag)
        aligned = pd.concat([social, shifted_news], axis=1, keys=["social", "news"]).dropna()
        if len(aligned) < 20:
            results.append(LagCorrelation(lag, 0.0))
            continue
        corr = aligned["social"].corr(aligned["news"])
        results.append(LagCorrelation(lag, float(corr) if not math.isnan(corr) else 0.0))
    return results
