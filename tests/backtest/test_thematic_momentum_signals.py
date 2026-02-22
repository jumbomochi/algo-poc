from __future__ import annotations

from datetime import date, timedelta

from scripts.run_backtest import make_thematic_momentum_signals_fn


def _make_trending_bars(tickers: list[str], days: int = 100, base_price: float = 50.0):
    """Create bars where later tickers have stronger uptrends."""
    bars = {}
    for i, ticker in enumerate(tickers):
        price = base_price
        daily_return = 0.001 * (i + 1)
        ticker_bars = []
        for d in range(days):
            price *= (1 + daily_return)
            ticker_bars.append({
                "date": date(2024, 1, 1) + timedelta(days=d),
                "open": price * 0.999,
                "high": price * 1.005,
                "low": price * 0.995,
                "close": price,
                "volume": 30000 + d * 100,
            })
        bars[ticker] = ticker_bars
    return bars


def test_thematic_momentum_buys_top_ranked():
    """Thematic momentum buys top-ranked ETFs above 50-day MA."""
    tickers = ["ARKK", "TAN", "HACK", "BOTZ", "LIT"]
    bars = _make_trending_bars(tickers, days=100)

    signals_fn = make_thematic_momentum_signals_fn(
        bars_by_ticker=bars,
        top_n=3,
        lookback_days=63,
        position_size_pct=0.15,
        initial_capital=20_000,
    )

    # LIT (index 4) has strongest trend, should get buy signal
    result = signals_fn("LIT", bars["LIT"])
    assert result is not None
    assert result["action"] == "buy"
    assert result["signals"]["strategy"] == "thematic_momentum"


def test_thematic_momentum_requires_above_ma():
    """Does not buy if price is below 50-day MA."""
    tickers = ["ARKK", "TAN", "HACK"]
    bars = {}
    for ticker in tickers:
        price = 100.0
        ticker_bars = []
        for d in range(100):
            if d < 80:
                price *= 1.002
            else:
                price *= 0.98
            ticker_bars.append({
                "date": date(2024, 1, 1) + timedelta(days=d),
                "open": price, "high": price + 1, "low": price - 1,
                "close": price, "volume": 30000,
            })
        bars[ticker] = ticker_bars

    signals_fn = make_thematic_momentum_signals_fn(
        bars_by_ticker=bars,
        top_n=3,
        lookback_days=63,
        ma_period=50,
        position_size_pct=0.15,
        initial_capital=20_000,
    )

    for ticker in tickers:
        result = signals_fn(ticker, bars[ticker])
        assert result is None


def test_thematic_momentum_requires_min_bars():
    """Returns None when not enough data."""
    tickers = ["ARKK", "TAN"]
    bars = _make_trending_bars(tickers, days=30)

    signals_fn = make_thematic_momentum_signals_fn(
        bars_by_ticker=bars,
        top_n=2,
        lookback_days=63,
        position_size_pct=0.15,
        initial_capital=20_000,
    )

    result = signals_fn("ARKK", bars["ARKK"])
    assert result is None
