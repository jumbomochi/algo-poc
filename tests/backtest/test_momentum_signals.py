from __future__ import annotations

from datetime import date, timedelta

from scripts.run_backtest import make_momentum_signals_fn


def _make_bars(ticker: str, days: int, base_price: float, daily_return: float):
    """Generate synthetic bars with a steady return."""
    bars = []
    price = base_price
    for i in range(days):
        d = date(2024, 1, 2) + timedelta(days=i)
        bars.append({
            "date": d,
            "open": price,
            "high": price * 1.01,
            "low": price * 0.99,
            "close": price,
            "volume": 500_000,
        })
        price *= (1 + daily_return)
    return bars


def test_momentum_buys_top_ranked_ticker():
    """Momentum should generate a buy for the strongest performer."""
    # WINNER has +50% over 6 months, LOSER has -10%
    bars_by_ticker = {
        "WINNER": _make_bars("WINNER", 200, 100.0, 0.003),  # ~+80% over 200 days
        "LOSER": _make_bars("LOSER", 200, 100.0, -0.001),   # ~-18% over 200 days
    }
    fn = make_momentum_signals_fn(
        bars_by_ticker=bars_by_ticker,
        top_n=1,
        lookback_days=126,
        position_size_pct=0.07,
        initial_capital=100_000,
        trailing_stop_pct=0.10,
    )
    # Feed enough bars for ranking (>126)
    winner_bars = bars_by_ticker["WINNER"]
    signal = fn("WINNER", winner_bars[:150])
    assert signal is not None
    assert signal["action"] == "buy"
    assert signal["signals"]["strategy"] == "momentum"

    # LOSER should NOT get a buy
    loser_bars = bars_by_ticker["LOSER"]
    signal = fn("LOSER", loser_bars[:150])
    assert signal is None


def test_momentum_requires_min_bars():
    """Momentum should return None with insufficient history."""
    bars_by_ticker = {
        "SHORT": _make_bars("SHORT", 50, 100.0, 0.003),
    }
    fn = make_momentum_signals_fn(
        bars_by_ticker=bars_by_ticker,
        top_n=1,
        lookback_days=126,
    )
    signal = fn("SHORT", bars_by_ticker["SHORT"])
    assert signal is None


def test_momentum_trailing_stop_exits():
    """Momentum should sell when trailing stop triggers after profit."""
    bars_by_ticker = {
        "TEST": _make_bars("TEST", 200, 100.0, 0.003),
    }
    fn = make_momentum_signals_fn(
        bars_by_ticker=bars_by_ticker,
        top_n=1,
        lookback_days=126,
        trailing_stop_pct=0.05,
    )
    # Enter at bar 150
    bars = bars_by_ticker["TEST"]
    signal = fn("TEST", bars[:150])
    assert signal is not None and signal["action"] == "buy"

    # Price keeps rising — update peak (no sell)
    signal = fn("TEST", bars[:170])
    assert signal is None or signal["action"] != "sell"

    # Now simulate a 6% drop from peak
    peak_price = bars[169]["close"]
    drop_bars = list(bars[:170])
    for i in range(5):
        d = bars[169]["date"] + timedelta(days=i + 1)
        drop_price = peak_price * (0.93 + i * 0.001)  # stays below 95% of peak
        drop_bars.append({
            "date": d, "open": drop_price, "high": drop_price,
            "low": drop_price, "close": drop_price, "volume": 500_000,
        })
    signal = fn("TEST", drop_bars)
    assert signal is not None
    assert signal["action"] == "sell"
    assert signal["exit_reason"] == "trailing_stop"
