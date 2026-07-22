from __future__ import annotations

from datetime import date, timedelta

from backtest.portfolio_context import HeldPosition, PendingOrder, PortfolioContext
from backtest.ranked_selection import ReplacementPolicy
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
        bars_by_ticker=bars,
        top_n=1,
        position_size_pct=0.10,
        initial_capital=20_000,
    )

    # AAPL has best fundamentals (highest ROE=0.25, lowest D/E=0.5, best margin=0.30)
    result = signals_fn("AAPL", bars["AAPL"])
    assert result is not None
    assert result["action"] == "buy"
    assert result["signals"]["strategy"] == "quality_value"


def test_quality_value_skips_low_quality():
    """Quality value doesn't buy stocks with poor fundamentals."""
    tickers = ["AAPL", "MSFT", "AMZN"]
    bars = _make_bars(tickers)
    fundamentals_lookup = _make_fundamentals_lookup()

    signals_fn = make_quality_value_signals_fn(
        fundamentals_lookup=fundamentals_lookup,
        sector_map={"AAPL": "Technology", "MSFT": "Technology", "AMZN": "Technology"},
        bars_by_ticker=bars,
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
        bars_by_ticker=bars,
        top_n=1,
        position_size_pct=0.10,
        initial_capital=20_000,
    )

    result = signals_fn("UNKNOWN", bars["UNKNOWN"])
    assert result is None


def test_quality_value_hydrated_exit_uses_full_quantity():
    bars = _make_bars(["AAPL"])
    bars["AAPL"][-1]["close"] = 85.0
    context = PortfolioContext(
        positions={"AAPL": HeldPosition(4, 100, 110, date(2024, 1, 1))},
        pending_orders={
            "sell": PendingOrder("AAPL", "sell", 1, 85, "sell")
        }, sleeve_budget=20_000, reserved_notional=0,
    )
    fn = make_quality_value_signals_fn(
        _make_fundamentals_lookup(), {"AAPL": "Technology"},
        bars_by_ticker=bars, trailing_stop_pct=0.12, portfolio_context=context,
    )
    assert fn("AAPL", bars["AAPL"])["quantity"] == 3


def test_quality_value_hydrated_exit_does_not_require_fundamentals():
    bars = _make_bars(["AAPL"])
    bars["AAPL"][-1]["close"] = 85.0
    context = PortfolioContext(
        positions={"AAPL": HeldPosition(4, 100, 110, date(2024, 1, 1))},
        pending_orders={}, sleeve_budget=20_000, reserved_notional=0,
    )
    fn = make_quality_value_signals_fn(
        lambda ticker, as_of: None, {"AAPL": "Technology"},
        bars_by_ticker=bars, trailing_stop_pct=0.12, portfolio_context=context,
    )
    assert fn("AAPL", bars["AAPL"])["quantity"] == 4


def test_quality_value_complete_ranking_does_not_depend_on_call_order():
    bars = _make_bars(["AAPL", "MSFT", "AMZN"])
    fn = make_quality_value_signals_fn(
        _make_fundamentals_lookup(),
        {ticker: "Technology" for ticker in bars},
        bars_by_ticker=bars,
        top_n=1,
        initial_capital=20_000,
    )

    assert fn("AAPL", bars["AAPL"])["action"] == "buy"


def test_quality_value_weakest_policy_exits_rank_dropped_holding():
    bars = _make_bars(["AAPL", "MSFT", "AMZN"])
    context = PortfolioContext(
        positions={"AMZN": HeldPosition(4, 150, 150, date(2024, 1, 1))},
        pending_orders={},
        sleeve_budget=20_000,
        reserved_notional=0,
    )
    fn = make_quality_value_signals_fn(
        _make_fundamentals_lookup(),
        {ticker: "Technology" for ticker in bars},
        bars_by_ticker=bars,
        top_n=1,
        portfolio_context=context,
        replacement_policy=ReplacementPolicy.WEAKEST,
    )

    signal = fn("AMZN", bars["AMZN"])
    assert signal["action"] == "sell"
    assert signal["quantity"] == 4
    assert signal["exit_reason"] == "rank_replacement"
