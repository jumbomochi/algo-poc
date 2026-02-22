from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from backtest.feature_extractor import enrich_trades, extract_features


def _make_bars(ticker: str, n: int = 30) -> list[dict]:
    """Generate n synthetic daily bars."""
    bars = []
    price = 100.0
    for i in range(n):
        d = date(2024, 1, 1 + i) if i < 31 else date(2024, 2, i - 30)
        price *= 1 + (i % 3 - 1) * 0.01  # oscillate +-1%
        bars.append({
            "date": date(2024, 1, 2 + i),
            "open": price * 0.999,
            "high": price * 1.01,
            "low": price * 0.99,
            "close": price,
            "volume": 1_000_000 + i * 10_000,
        })
    return bars


def test_enrich_adds_bar_features():
    """enrich_trades should add bar_features dict to each trade."""
    bars = _make_bars("AAPL", 30)
    trades = [{
        "ticker": "AAPL",
        "entry_date": bars[25]["date"],
        "exit_date": bars[29]["date"],
        "entry_price": 100.0,
        "exit_price": 105.0,
        "quantity": 10,
        "pnl": 50.0,
        "entry_signals": {"rsi": 0.3},
    }]

    enrich_trades(trades, {"AAPL": bars})

    assert "bar_features" in trades[0]
    bf = trades[0]["bar_features"]
    assert "return_5d" in bf
    assert "return_20d" in bf
    assert "vol_20d" in bf
    assert "volume_ratio" in bf
    assert isinstance(bf["return_5d"], float)


def test_enrich_adds_regime():
    """enrich_trades should add regime from regime_by_date."""
    bars = _make_bars("AAPL", 30)
    entry_date = bars[25]["date"]
    trades = [{
        "ticker": "AAPL",
        "entry_date": entry_date,
        "exit_date": bars[29]["date"],
        "entry_price": 100.0,
        "exit_price": 105.0,
        "quantity": 10,
        "pnl": 50.0,
        "entry_signals": {},
    }]

    regime_by_date = {entry_date: "bull"}
    enrich_trades(trades, {"AAPL": bars}, regime_by_date)

    assert trades[0]["bar_features"]["regime"] == "bull"


def test_extract_features_returns_dataframe():
    """extract_features should return (DataFrame, Series) with correct shape."""
    trades = [
        {
            "portfolio": "momentum",
            "entry_signals": {"rank": 2, "strategy": "momentum"},
            "bar_features": {"return_5d": 0.03, "vol_20d": 0.02, "regime": "bull"},
            "pnl": 100.0,
        },
        {
            "portfolio": "mean_reversion",
            "entry_signals": {"proximity": {"value": 0.9, "confidence": 0.8}},
            "bar_features": {"return_5d": -0.05, "vol_20d": 0.04, "regime": "bear"},
            "pnl": -50.0,
        },
    ]

    features, labels = extract_features(trades)

    assert isinstance(features, pd.DataFrame)
    assert isinstance(labels, pd.Series)
    assert len(features) == 2
    assert len(labels) == 2
    assert labels.iloc[0] == 1  # positive PnL
    assert labels.iloc[1] == 0  # negative PnL


def test_extract_handles_nested_signals():
    """Nested signal dicts should be flattened with prefix."""
    trades = [{
        "portfolio": "mr",
        "entry_signals": {"proximity": {"value": 0.9, "confidence": 0.85}},
        "bar_features": {},
        "pnl": 10.0,
    }]

    features, _ = extract_features(trades)

    assert "signal_proximity_value" in features.columns
    assert "signal_proximity_confidence" in features.columns
    assert features["signal_proximity_value"].iloc[0] == 0.9


def test_extract_handles_missing_signals():
    """Missing signal keys across strategies should produce NaN."""
    trades = [
        {"portfolio": "mr", "entry_signals": {"rsi": 0.3}, "bar_features": {}, "pnl": 10.0},
        {"portfolio": "mom", "entry_signals": {"rank": 2}, "bar_features": {}, "pnl": -5.0},
    ]

    features, _ = extract_features(trades)

    # "signal_rsi" exists for trade 1, NaN for trade 2
    assert not pd.isna(features["signal_rsi"].iloc[0])
    assert pd.isna(features["signal_rsi"].iloc[1])
