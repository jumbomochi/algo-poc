from __future__ import annotations

from datetime import date, timedelta

from scripts.run_backtest import make_quality_value_signals_fn


def _make_fundamentals_lookup():
    """Create a fundamentals lookup with synthetic data."""
    from scripts.fetch_fundamentals import build_fundamentals_lookup

    cache = {
        "AAPL": [{"report_date": "2024-01-01", "roe": 0.25, "debt_equity": 0.5,
                   "profit_margin": 0.30, "net_income": 1e9, "total_revenue": 4e9,
                   "total_equity": 4e9, "total_debt": 2e9, "sector": "Technology"}],
        "MSFT": [{"report_date": "2024-01-01", "roe": 0.15, "debt_equity": 1.0,
                   "profit_margin": 0.20, "net_income": 5e8, "total_revenue": 2.5e9,
                   "total_equity": 3e9, "total_debt": 3e9, "sector": "Technology"}],
        "AMZN": [{"report_date": "2024-01-01", "roe": 0.08, "debt_equity": 2.0,
                   "profit_margin": 0.05, "net_income": 2e8, "total_revenue": 4e9,
                   "total_equity": 2e9, "total_debt": 4e9, "sector": "Technology"}],
    }
    return build_fundamentals_lookup(cache)


def _make_bars(tickers: list[str], days: int = 100):
    bars = {}
    for ticker in tickers:
        ticker_bars = []
        price = 150.0
        for d in range(days):
            ticker_bars.append({
                "date": date(2024, 1, 1) + timedelta(days=d),
                "open": price, "high": price + 1, "low": price - 1,
                "close": price, "volume": 50000,
            })
        bars[ticker] = ticker_bars
    return bars


def test_quality_value_buys_high_quality():
    """Quality value buys stocks with best fundamentals."""
    tickers = ["AAPL", "MSFT", "AMZN"]
    bars = _make_bars(tickers)
    fundamentals_lookup = _make_fundamentals_lookup()

    signals_fn = make_quality_value_signals_fn(
        fundamentals_lookup=fundamentals_lookup,
        sector_map={"AAPL": "Technology", "MSFT": "Technology", "AMZN": "Technology"},
        top_n=1,
        position_size_pct=0.10,
        initial_capital=20_000,
    )

    # Call all tickers first to populate scores_cache for ranking
    for ticker in tickers:
        signals_fn(ticker, bars[ticker])

    # Reset tracked state and call again — AAPL should be top ranked
    # Actually, since tracked state persists, we need a fresh function
    signals_fn2 = make_quality_value_signals_fn(
        fundamentals_lookup=fundamentals_lookup,
        sector_map={"AAPL": "Technology", "MSFT": "Technology", "AMZN": "Technology"},
        top_n=1,
        position_size_pct=0.10,
        initial_capital=20_000,
    )

    # Warm up scores by calling each ticker once.
    # Process AAPL last so that the scores_cache has >= 3 entries when AAPL is evaluated.
    results = {}
    for ticker in ["MSFT", "AMZN", "AAPL"]:
        results[ticker] = signals_fn2(ticker, bars[ticker])

    # AAPL has best fundamentals (highest ROE=0.25, lowest D/E=0.5, best margin=0.30)
    assert results["AAPL"] is not None
    assert results["AAPL"]["action"] == "buy"
    assert results["AAPL"]["signals"]["strategy"] == "quality_value"


def test_quality_value_skips_low_quality():
    """Quality value doesn't buy stocks with poor fundamentals."""
    tickers = ["AAPL", "MSFT", "AMZN"]
    bars = _make_bars(tickers)
    fundamentals_lookup = _make_fundamentals_lookup()

    signals_fn = make_quality_value_signals_fn(
        fundamentals_lookup=fundamentals_lookup,
        sector_map={"AAPL": "Technology", "MSFT": "Technology", "AMZN": "Technology"},
        top_n=1,
        position_size_pct=0.10,
        initial_capital=20_000,
    )

    # Call all tickers to populate scores
    results = {}
    for ticker in tickers:
        results[ticker] = signals_fn(ticker, bars[ticker])

    # AMZN has worst fundamentals (lowest ROE=0.08, highest D/E=2.0, worst margin=0.05)
    assert results["AMZN"] is None


def test_quality_value_requires_fundamentals():
    """Returns None for tickers without fundamentals data."""
    bars = _make_bars(["UNKNOWN"])
    fundamentals_lookup = _make_fundamentals_lookup()

    signals_fn = make_quality_value_signals_fn(
        fundamentals_lookup=fundamentals_lookup,
        sector_map={},
        top_n=1,
        position_size_pct=0.10,
        initial_capital=20_000,
    )

    result = signals_fn("UNKNOWN", bars["UNKNOWN"])
    assert result is None
