from __future__ import annotations

from datetime import date, timedelta

import pytest

from scripts.run_backtest import REGIME_PARAMS, compute_regime_by_date


def _make_bars(start: date, num_days: int, prices: list[float]) -> list[dict]:
    """Build bar dicts from a list of close prices starting at *start*.

    If *prices* has fewer entries than *num_days*, the last price is repeated.
    Each bar gets open=high=low=close for simplicity.
    """
    bars: list[dict] = []
    for i in range(num_days):
        p = prices[i] if i < len(prices) else prices[-1]
        d = start + timedelta(days=i)
        bars.append({"date": d, "open": p, "high": p, "low": p, "close": p})
    return bars


class TestCrashRegimeDetection:
    """Tests for the crash regime (breadth < 10%)."""

    def test_crash_regime_at_low_breadth(self):
        """When all tickers crash below their 200-day MA, the regime should be 'crash'.

        20 tickers with prices that start at 100 for 200 days then drop to 50
        for 10 more days.  Because 0/20 tickers are above their 200-day MA
        (breadth = 0%), the last dates must be classified as crash.
        """
        start = date(2020, 1, 1)
        num_days = 210
        # 200 days at 100, then 10 days at 50 → well below the 200-day MA
        prices = [100.0] * 200 + [50.0] * 10
        bars_by_ticker = {
            f"T{i:02d}": _make_bars(start, num_days, prices) for i in range(20)
        }

        regime = compute_regime_by_date(bars_by_ticker)

        # The last 10 dates should all be "crash" (breadth = 0%)
        last_dates = sorted(regime.keys())[-10:]
        for d in last_dates:
            assert regime[d] == "crash", (
                f"Expected 'crash' on {d}, got '{regime[d]}'"
            )

    def test_crash_regime_not_triggered_in_bear(self):
        """30% breadth should be 'bear', NOT 'crash'.

        7 of 10 tickers crash below MA (breadth = 30%).  That is below the
        bear threshold (40%) but above the crash threshold (10%).
        """
        start = date(2020, 1, 1)
        num_days = 210

        bars_by_ticker: dict[str, list[dict]] = {}
        # 3 tickers stay well above their MA (price rises)
        above_prices = [100.0] * 200 + [150.0] * 10
        for i in range(3):
            bars_by_ticker[f"UP{i}"] = _make_bars(start, num_days, above_prices)

        # 7 tickers drop below their MA
        below_prices = [100.0] * 200 + [50.0] * 10
        for i in range(7):
            bars_by_ticker[f"DN{i}"] = _make_bars(start, num_days, below_prices)

        regime = compute_regime_by_date(bars_by_ticker)

        last_dates = sorted(regime.keys())[-10:]
        for d in last_dates:
            assert regime[d] == "bear", (
                f"Expected 'bear' on {d}, got '{regime[d]}'"
            )

    def test_regime_params_has_crash(self):
        """REGIME_PARAMS must include a 'crash' key with tighter stops than bear."""
        assert "crash" in REGIME_PARAMS, "REGIME_PARAMS missing 'crash' entry"
        crash = REGIME_PARAMS["crash"]
        bear = REGIME_PARAMS["bear"]
        assert crash["trailing_stop_pct"] < bear["trailing_stop_pct"], (
            "Crash trailing stop should be tighter (smaller) than bear"
        )
        assert crash["max_loss_pct"] < bear["max_loss_pct"], (
            "Crash max loss should be tighter (smaller) than bear"
        )
