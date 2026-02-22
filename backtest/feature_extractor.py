from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd


def enrich_trades(
    trades: list[dict],
    bars_by_ticker: dict[str, list[dict]],
    regime_by_date: dict | None = None,
) -> None:
    """Add bar-derived features to each trade dict (mutates in place).

    Features added to trade["bar_features"]:
    - return_5d: 5-trading-day return ending at entry_date
    - return_20d: 20-trading-day return ending at entry_date
    - vol_20d: 20-day daily return standard deviation
    - volume_ratio: entry-day volume / 20-day average volume
    - regime: market regime at entry_date (if regime_by_date provided)
    """
    # Build sorted bar lists and date-to-index lookup per ticker
    bar_index: dict[str, dict[date, int]] = {}
    sorted_bars: dict[str, list[dict]] = {}
    for ticker, bars in bars_by_ticker.items():
        s = sorted(bars, key=lambda b: b["date"])
        sorted_bars[ticker] = s
        bar_index[ticker] = {b["date"]: i for i, b in enumerate(s)}

    for trade in trades:
        ticker = trade["ticker"]
        entry_date = trade["entry_date"]
        features: dict = {}

        idx_map = bar_index.get(ticker, {})
        entry_idx = idx_map.get(entry_date)
        bars = sorted_bars.get(ticker, [])

        if entry_idx is not None and entry_idx >= 20:
            closes = [b["close"] for b in bars[entry_idx - 20 : entry_idx + 1]]
            volumes = [b["volume"] for b in bars[entry_idx - 20 : entry_idx + 1]]

            # Recent returns
            if len(closes) >= 6:
                features["return_5d"] = (closes[-1] - closes[-6]) / closes[-6]
            if len(closes) >= 21:
                features["return_20d"] = (closes[-1] - closes[0]) / closes[0]

            # Volatility (std of daily returns over 20 days)
            daily_rets = [
                (closes[i] - closes[i - 1]) / closes[i - 1]
                for i in range(1, len(closes))
            ]
            features["vol_20d"] = float(np.std(daily_rets)) if daily_rets else 0.0

            # Volume ratio
            avg_vol = np.mean(volumes[:-1]) if len(volumes) > 1 else 1.0
            features["volume_ratio"] = (
                float(volumes[-1] / avg_vol) if avg_vol > 0 else 1.0
            )

        # Regime
        if regime_by_date and entry_date in regime_by_date:
            features["regime"] = regime_by_date[entry_date]

        trade["bar_features"] = features


def extract_features(trades: list[dict]) -> tuple[pd.DataFrame, pd.Series]:
    """Extract feature DataFrame and binary labels from enriched trades.

    Features include:
    - portfolio (categorical)
    - signal_* (flattened entry_signals, NaN where missing)
    - bar_* (bar-derived features from enrich_trades)

    Labels: 1 if trade PnL > 0, else 0.
    """
    records: list[dict] = []
    labels: list[int] = []

    for trade in trades:
        row: dict = {}
        row["portfolio"] = trade.get("portfolio", "unknown")

        # Flatten entry_signals
        signals = trade.get("entry_signals", {})
        for key, val in signals.items():
            if isinstance(val, dict):
                for subkey, subval in val.items():
                    if isinstance(subval, (int, float)):
                        row[f"signal_{key}_{subkey}"] = subval
            elif isinstance(val, (int, float)):
                row[f"signal_{key}"] = val

        # Bar-derived features
        bar_features = trade.get("bar_features", {})
        for key, val in bar_features.items():
            if key == "regime":
                row["bar_regime"] = val
            elif isinstance(val, (int, float)):
                row[f"bar_{key}"] = val

        records.append(row)
        labels.append(1 if trade.get("pnl", 0) > 0 else 0)

    df = pd.DataFrame(records)
    return df, pd.Series(labels, name="profitable")
