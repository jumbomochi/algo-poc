from __future__ import annotations

from datetime import date, timedelta

from backtest.portfolio_context import HeldPosition, PendingOrder, PortfolioContext
from backtest.portfolio_context import RunnerBook
from scripts.run_backtest import make_earnings_drift_signals_fn
from tests.backtest.books import empty_context, filled


def _make_earnings_lookup():
    """Create an earnings lookup with synthetic data."""
    from scripts.fetch_earnings import build_earnings_lookup

    cache = {
        "AAPL": [
            {"earnings_date": "2024-02-01", "actual_eps": 2.20,
             "estimate_eps": 2.00, "surprise_pct": 10.0},
        ],
        "MSFT": [
            {"earnings_date": "2024-02-01", "actual_eps": 2.00,
             "estimate_eps": 2.10, "surprise_pct": -4.76},
        ],
    }
    return build_earnings_lookup(cache, window_days=2)


def _make_bars(ticker: str, days: int = 100) -> list[dict]:
    bars = []
    price = 150.0
    for d in range(days):
        bars.append({
            "date": date(2024, 1, 1) + timedelta(days=d),
            "open": price, "high": price + 1, "low": price - 1,
            "close": price, "volume": 50000,
        })
    return bars


def test_earnings_drift_buys_on_positive_surprise():
    """Buys within 2 days of a >5% earnings beat."""
    earnings_lookup = _make_earnings_lookup()

    signals_fn = make_earnings_drift_signals_fn(
        earnings_lookup=earnings_lookup,
        surprise_threshold_pct=5.0,
        position_size_pct=0.08,
        initial_capital=20_000,
        portfolio_context=empty_context(),
    )

    # AAPL had 10% surprise on 2024-02-01 (day index 31)
    bars = _make_bars("AAPL")
    result = signals_fn("AAPL", bars[:32])  # bars up to Feb 1
    assert result is not None
    assert result["action"] == "buy"
    assert result["signals"]["strategy"] == "earnings_drift"
    assert result["signals"]["surprise_pct"] == 10.0


def test_earnings_drift_skips_negative_surprise():
    """Does not buy on earnings miss."""
    earnings_lookup = _make_earnings_lookup()

    signals_fn = make_earnings_drift_signals_fn(
        earnings_lookup=earnings_lookup,
        surprise_threshold_pct=5.0,
        position_size_pct=0.08,
        initial_capital=20_000,
        portfolio_context=empty_context(),
    )

    # MSFT had -4.76% surprise (miss)
    bars = _make_bars("MSFT")
    result = signals_fn("MSFT", bars[:32])
    assert result is None


def test_earnings_drift_exits_after_hold_period():
    """Exits after max_hold_days sessions counted from the FILL.

    The book holds the lot a runner fills at the open after the signal, and
    the clock counts sessions after that entry date — live's clock. Before
    KAN-116 the replay counted from the signal bar, one session sooner.
    """
    earnings_lookup = _make_earnings_lookup()
    book = RunnerBook()

    signals_fn = make_earnings_drift_signals_fn(
        earnings_lookup=earnings_lookup,
        surprise_threshold_pct=5.0,
        max_hold_days=20,
        position_size_pct=0.08,
        initial_capital=20_000,
        portfolio_context=book,
    )

    bars = _make_bars("AAPL")

    # Enter on earnings day (bars[:32] covers up to 2024-02-01)
    book.bind(empty_context())
    entry = signals_fn("AAPL", bars[:32])
    assert entry is not None and entry["action"] == "buy"

    # Filled at bars[32]'s open; hold until the time exit triggers
    sells = []
    for end_idx in range(33, 60):
        book.bind(filled("AAPL", bars, signal_index=31, decision_index=end_idx - 1))
        result = signals_fn("AAPL", bars[:end_idx])
        if result is not None:
            assert result["action"] == "sell"
            assert result["exit_reason"] == "time_exit"
            sells.append(end_idx)

    # 20 sessions after the 2024-02-02 fill: decided on bars[52] (2024-02-22)
    assert sells and sells[0] == 53, sells


def test_earnings_drift_hydrated_time_exit_uses_entry_date_and_full_quantity():
    bars = _make_bars("AAPL")
    context = PortfolioContext(
        positions={"AAPL": HeldPosition(6, 150, 150, date(2024, 1, 1))},
        pending_orders={
            "sell": PendingOrder("AAPL", "sell", 2, 150, "sell")
        }, sleeve_budget=20_000, reserved_notional=0,
    )
    fn = make_earnings_drift_signals_fn(
        _make_earnings_lookup(), max_hold_days=20, portfolio_context=context,
    )
    signal = fn("AAPL", bars[:32])
    assert signal["action"] == "sell"
    assert signal["quantity"] == 4
    assert signal["exit_reason"] == "time_exit"
