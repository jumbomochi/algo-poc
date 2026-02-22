from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import numpy as np

from scripts.run_backtest import make_ml_filtered_signals_fn


def _make_mock_model(always_score: float = 0.8):
    """Create a mock LightGBM model that always returns the same score."""
    model = MagicMock()
    model.predict = MagicMock(return_value=np.array([always_score]))
    model.feature_name = MagicMock(return_value=["portfolio", "signal_rank"])
    return model


def _make_bars(n: int = 30) -> list[dict]:
    """Generate n synthetic bars."""
    return [
        {"date": date(2024, 1, 2 + i), "open": 100.0, "high": 101.0,
         "low": 99.0, "close": 100.0 + i * 0.1, "volume": 1_000_000}
        for i in range(n)
    ]


def test_ml_filter_passes_high_confidence():
    """Signals with model confidence > threshold should pass through."""
    model = _make_mock_model(always_score=0.8)

    def inner_fn(ticker, bars):
        return {
            "action": "buy", "ticker": ticker, "limit_price": 100.0,
            "quantity": 10, "sector": "Tech",
            "signals": {"rank": 1, "strategy": "momentum"},
        }

    filtered_fn = make_ml_filtered_signals_fn(inner_fn, model, threshold=0.6)
    result = filtered_fn("AAPL", _make_bars())

    assert result is not None
    assert result["action"] == "buy"


def test_ml_filter_blocks_low_confidence():
    """Signals with model confidence < threshold should be blocked."""
    model = _make_mock_model(always_score=0.3)

    def inner_fn(ticker, bars):
        return {
            "action": "buy", "ticker": ticker, "limit_price": 100.0,
            "quantity": 10, "sector": "Tech",
            "signals": {"rank": 5, "strategy": "momentum"},
        }

    filtered_fn = make_ml_filtered_signals_fn(inner_fn, model, threshold=0.6)
    result = filtered_fn("AAPL", _make_bars())

    assert result is None


def test_ml_filter_always_passes_sell_signals():
    """Sell signals should never be blocked by ML filter."""
    model = _make_mock_model(always_score=0.1)  # very low confidence

    def inner_fn(ticker, bars):
        return {
            "action": "sell", "ticker": ticker, "limit_price": 100.0,
            "quantity": 0, "exit_reason": "trailing_stop",
        }

    filtered_fn = make_ml_filtered_signals_fn(inner_fn, model, threshold=0.9)
    result = filtered_fn("AAPL", _make_bars())

    assert result is not None
    assert result["action"] == "sell"


def test_ml_filter_passes_none_through():
    """If inner function returns None, filter returns None."""
    model = _make_mock_model(always_score=0.9)

    def inner_fn(ticker, bars):
        return None

    filtered_fn = make_ml_filtered_signals_fn(inner_fn, model, threshold=0.5)
    result = filtered_fn("AAPL", _make_bars())

    assert result is None
