from __future__ import annotations

import random
from datetime import date, timedelta

import pandas as pd
import pytest

from sentiment.evaluation import (
    event_study,
    forward_returns,
    information_coefficient,
    lead_lag,
)


def make_bars(closes: list[float], start: date = date(2026, 1, 5)) -> list[dict]:
    out, d = [], start
    for close in closes:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append({"date": d, "close": close})
        d += timedelta(days=1)
    return out


def test_forward_returns():
    bars = {"AAPL": make_bars([100.0, 110.0, 121.0])}
    fwd = forward_returns(bars, horizon=1)
    first_day = bars["AAPL"][0]["date"]
    assert fwd["AAPL"][first_day] == pytest.approx(0.10)


def _planted_universe(n_days=120, n_tickers=30, signal=0.03, seed=7):
    """Tickers whose next-day return follows today's score -> IC > 0."""
    rng = random.Random(seed)
    tickers = [f"T{i:02d}" for i in range(n_tickers)]
    start = date(2026, 1, 5)
    days = []
    d = start
    while len(days) < n_days + 6:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    rows, bars = [], {}
    scores = {t: [rng.uniform(-1, 1) for _ in range(n_days)] for t in tickers}
    for t in tickers:
        closes = [100.0]
        for i in range(n_days + 5):
            drift = signal * scores[t][i] if 0 <= i < n_days else 0.0
            closes.append(closes[-1] * (1 + drift + rng.gauss(0, 0.005)))
        bars[t] = [{"date": days[i], "close": closes[i]} for i in range(len(days))]
        for i in range(n_days):
            rows.append({"ticker": t, "session_date": days[i], "score": scores[t][i]})
    return pd.DataFrame(rows), bars


def test_ic_detects_planted_signal():
    daily, bars = _planted_universe()
    results = information_coefficient(daily, bars, horizons=(1,))
    assert results[0].horizon == 1
    assert results[0].mean_ic > 0.1
    assert results[0].t_stat > 2
    assert results[0].n_days > 50


def test_ic_near_zero_on_noise():
    daily, bars = _planted_universe(signal=0.0, seed=11)
    results = information_coefficient(daily, bars, horizons=(1,))
    assert abs(results[0].mean_ic) < 0.05


def test_event_study_detects_planted_spikes():
    daily, bars = _planted_universe(signal=0.0, seed=3)
    daily["sentiment_zscore"] = 0.0
    daily["volume_zscore"] = 0.0
    # plant 40 positive-spike events with a +2% next-day pop
    spikes = daily.sample(n=40, random_state=1).index
    daily.loc[spikes, ["sentiment_zscore", "volume_zscore"]] = 3.0
    daily.loc[spikes, "score"] = 1.0
    for idx in spikes:
        row = daily.loc[idx]
        ticker_bars = bars[row["ticker"]]
        dates = [b["date"] for b in ticker_bars]
        i = dates.index(row["session_date"])
        bump = 1.02
        for bar in ticker_bars[i + 1:]:
            bar["close"] *= bump
    results = event_study(daily, bars, horizons=(1,))
    assert results[0].n_events == 40
    assert results[0].mean_abnormal_return > 0.005
    assert results[0].p_value < 0.05


def test_lead_lag_detects_social_leading_news():
    rng = random.Random(5)
    start = date(2026, 1, 5)
    days = []
    d = start
    while len(days) < 100:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    base = [rng.uniform(-1, 1) for _ in range(102)]
    social = pd.DataFrame(
        {"ticker": "AAPL", "session_date": days, "score": base[2:102]}
    )
    news = pd.DataFrame(  # news repeats social with a 2-day delay
        {"ticker": "AAPL", "session_date": days, "score": base[0:100]}
    )
    results = lead_lag(social, news, max_lag=5)
    best = max(results, key=lambda r: r.correlation)
    assert best.lag_days == 2
    assert best.correlation > 0.9
