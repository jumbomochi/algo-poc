from __future__ import annotations

from datetime import date

from scripts.run_backtest import make_crash_freeze_signals_fn


def test_crash_freeze_blocks_buy_signals():
    """Buy signals should be blocked when regime is 'crash'."""
    regime_by_date = {date(2024, 3, 1): "crash"}

    def inner_fn(ticker, bars):
        return {
            "action": "buy", "ticker": ticker, "limit_price": 100.0,
            "quantity": 10, "sector": "Tech",
        }

    frozen_fn = make_crash_freeze_signals_fn(inner_fn, regime_by_date)
    bars = [{"date": date(2024, 3, 1), "close": 100.0}]
    result = frozen_fn("AAPL", bars)

    assert result is None


def test_crash_freeze_passes_sell_signals():
    """Sell signals should always pass through, even during crash regime."""
    regime_by_date = {date(2024, 3, 1): "crash"}

    def inner_fn(ticker, bars):
        return {
            "action": "sell", "ticker": ticker, "limit_price": 100.0,
            "quantity": 0, "exit_reason": "trailing_stop",
        }

    frozen_fn = make_crash_freeze_signals_fn(inner_fn, regime_by_date)
    bars = [{"date": date(2024, 3, 1), "close": 100.0}]
    result = frozen_fn("AAPL", bars)

    assert result is not None
    assert result["action"] == "sell"


def test_crash_freeze_passes_buys_in_non_crash():
    """Buy signals should pass through when regime is 'bear' (not crash)."""
    regime_by_date = {date(2024, 3, 1): "bear"}

    def inner_fn(ticker, bars):
        return {
            "action": "buy", "ticker": ticker, "limit_price": 100.0,
            "quantity": 10, "sector": "Tech",
        }

    frozen_fn = make_crash_freeze_signals_fn(inner_fn, regime_by_date)
    bars = [{"date": date(2024, 3, 1), "close": 100.0}]
    result = frozen_fn("AAPL", bars)

    assert result is not None
    assert result["action"] == "buy"


def test_crash_freeze_passes_none_through():
    """If inner function returns None, freeze wrapper returns None."""
    regime_by_date = {date(2024, 3, 1): "crash"}

    def inner_fn(ticker, bars):
        return None

    frozen_fn = make_crash_freeze_signals_fn(inner_fn, regime_by_date)
    bars = [{"date": date(2024, 3, 1), "close": 100.0}]
    result = frozen_fn("AAPL", bars)

    assert result is None


def test_crash_freeze_defaults_to_neutral():
    """Missing regime date should default to neutral (no freeze)."""
    regime_by_date = {}  # empty — date not present

    def inner_fn(ticker, bars):
        return {
            "action": "buy", "ticker": ticker, "limit_price": 100.0,
            "quantity": 10, "sector": "Tech",
        }

    frozen_fn = make_crash_freeze_signals_fn(inner_fn, regime_by_date)
    bars = [{"date": date(2024, 3, 1), "close": 100.0}]
    result = frozen_fn("AAPL", bars)

    assert result is not None
    assert result["action"] == "buy"
