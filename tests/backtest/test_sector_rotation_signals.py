from __future__ import annotations

from datetime import date

from scripts.run_backtest import make_sector_rotation_signals_fn


def _make_bars(tickers: list[str], days: int = 100, base_price: float = 100.0, daily_return: float = 0.001):
    """Create synthetic bars with a steady uptrend for each ticker."""
    bars = {}
    for i, ticker in enumerate(tickers):
        price = base_price + i * 10
        ticker_bars = []
        for d in range(days):
            price *= (1 + daily_return * (i + 1))
            ticker_bars.append({
                "date": date(2024, 1, 1) + __import__("datetime").timedelta(days=d),
                "open": price * 0.999,
                "high": price * 1.005,
                "low": price * 0.995,
                "close": price,
                "volume": 50000,
            })
        bars[ticker] = ticker_bars
    return bars


def test_sector_rotation_buys_top_ranked():
    """Sector rotation buys top-ranked sector ETFs by 3-month return."""
    tickers = ["XLK", "XLE", "XLF", "XLV", "XLY"]
    bars = _make_bars(tickers, days=100, daily_return=0.002)

    signals_fn = make_sector_rotation_signals_fn(
        bars_by_ticker=bars,
        top_n=3,
        lookback_days=63,
        position_size_pct=0.20,
        initial_capital=20_000,
    )

    # XLY has highest return (index 4, fastest growth), should get buy signal
    result = signals_fn("XLY", bars["XLY"])
    assert result is not None
    assert result["action"] == "buy"
    assert result["signals"]["strategy"] == "sector_rotation"


def test_sector_rotation_skips_low_ranked():
    """Sector rotation doesn't buy low-ranked ETFs."""
    tickers = ["XLK", "XLE", "XLF", "XLV", "XLY"]
    bars = _make_bars(tickers, days=100, daily_return=0.002)

    signals_fn = make_sector_rotation_signals_fn(
        bars_by_ticker=bars,
        top_n=2,
        lookback_days=63,
        position_size_pct=0.20,
        initial_capital=20_000,
    )

    # XLK has lowest return (index 0, slowest growth), should NOT get buy signal
    result = signals_fn("XLK", bars["XLK"])
    assert result is None


def test_sector_rotation_requires_min_bars():
    """Returns None when not enough bars for lookback."""
    tickers = ["XLK", "XLE", "XLF"]
    bars = _make_bars(tickers, days=30, daily_return=0.002)

    signals_fn = make_sector_rotation_signals_fn(
        bars_by_ticker=bars,
        top_n=2,
        lookback_days=63,
        position_size_pct=0.20,
        initial_capital=20_000,
    )

    result = signals_fn("XLK", bars["XLK"])
    assert result is None
