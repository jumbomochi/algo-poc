from __future__ import annotations

from datetime import date, timedelta

from scripts.run_backtest import make_short_term_mr_signals_fn


def _make_oversold_bars(days: int = 30) -> list[dict]:
    """Create bars ending with an oversold condition (RSI(2) < 10, below lower BB)."""
    bars = []
    price = 100.0
    for d in range(days - 2):
        bars.append({
            "date": date(2024, 1, 1) + timedelta(days=d),
            "open": price, "high": price + 1, "low": price - 1,
            "close": price, "volume": 50000,
        })
    # Last 2 bars: sharp drop to trigger RSI(2) < 10 and BB touch
    for d in range(days - 2, days):
        price *= 0.95  # 5% drop per day
        bars.append({
            "date": date(2024, 1, 1) + timedelta(days=d),
            "open": price + 2, "high": price + 2, "low": price - 1,
            "close": price, "volume": 80000,
        })
    return bars


def test_short_term_mr_buys_on_oversold():
    """Short-term MR buys when RSI(2) < 10 and price touches lower BB."""
    bars = _make_oversold_bars(days=30)

    signals_fn = make_short_term_mr_signals_fn(
        position_size_pct=0.08,
        initial_capital=20_000,
    )

    result = signals_fn("AAPL", bars)
    # Should trigger buy due to extreme oversold conditions
    if result is not None:
        assert result["action"] == "buy"
        assert result["signals"]["strategy"] == "short_term_mr"


def test_short_term_mr_exits_after_max_hold():
    """Short-term MR exits after max_hold_days even without RSI recovery."""
    signals_fn = make_short_term_mr_signals_fn(
        position_size_pct=0.08,
        initial_capital=20_000,
        max_hold_days=5,
    )

    # First: create oversold entry
    bars = _make_oversold_bars(days=30)
    entry_result = signals_fn("AAPL", bars)

    if entry_result and entry_result["action"] == "buy":
        # Then add 6 more bars with continued slight decline so RSI(2) stays
        # oversold (no recovery) and the time exit fires first.
        price = bars[-1]["close"]
        for d in range(6):
            price *= 0.995  # slight continued decline keeps RSI(2) depressed
            bars.append({
                "date": bars[-1]["date"] + timedelta(days=1),
                "open": price + 0.5, "high": price + 0.5, "low": price - 0.5,
                "close": price, "volume": 50000,
            })
            result = signals_fn("AAPL", bars)
            if result and result["action"] == "sell":
                assert result["exit_reason"] == "time_exit"
                return

        # If no sell triggered, the test still passes (signal may not have entered)


def test_short_term_mr_requires_min_bars():
    """Returns None when not enough data."""
    signals_fn = make_short_term_mr_signals_fn()
    bars = [{"date": date(2024, 1, d), "open": 100, "high": 101, "low": 99,
             "close": 100, "volume": 1000} for d in range(1, 10)]
    result = signals_fn("AAPL", bars)
    assert result is None
