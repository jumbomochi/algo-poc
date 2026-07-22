from __future__ import annotations

from datetime import date, timedelta

from backtest.portfolio_context import HeldPosition, PendingOrder, PortfolioContext
from scripts.run_backtest import make_tail_risk_hedge_signals_fn


def _make_bars(n: int, start_price: float = 100.0, start_date: date = date(2024, 1, 1)):
    """Generate n daily bars."""
    bars = []
    price = start_price
    for i in range(n):
        d = start_date + timedelta(days=i)
        bars.append({
            "date": d,
            "open": price,
            "high": price * 1.01,
            "low": price * 0.99,
            "close": price,
            "volume": 1_000_000,
        })
        price *= 1.001
    return bars


def test_tail_risk_hedge_buys_bull_allocation():
    """In bull regime, GLD and TLT get buy signals, SH does not."""
    regime_by_date = {date(2024, 1, 1) + timedelta(days=i): "bull" for i in range(30)}

    signals_fn = make_tail_risk_hedge_signals_fn(
        regime_by_date=regime_by_date,
        position_size_pct=0.25,
        initial_capital=100_000,
    )

    bars = _make_bars(10)

    gld_result = signals_fn("GLD", bars)
    assert gld_result is not None
    assert gld_result["action"] == "buy"
    assert gld_result["signals"]["strategy"] == "tail_risk_hedge"
    assert gld_result["signals"]["regime"] == "bull"
    assert gld_result["signals"]["weight"] == 0.50

    tlt_result = signals_fn("TLT", bars)
    assert tlt_result is not None
    assert tlt_result["action"] == "buy"
    assert tlt_result["signals"]["weight"] == 0.50

    sh_result = signals_fn("SH", bars)
    assert sh_result is None


def test_tail_risk_hedge_buys_bear_allocation():
    """In bear regime, SH/PSQ/SDS/GLD get buy signals, TLT does not."""
    regime_by_date = {date(2024, 1, 1) + timedelta(days=i): "bear" for i in range(30)}

    signals_fn = make_tail_risk_hedge_signals_fn(
        regime_by_date=regime_by_date,
        position_size_pct=0.25,
        initial_capital=100_000,
    )

    bars = _make_bars(10)

    sh_result = signals_fn("SH", bars)
    assert sh_result is not None
    assert sh_result["action"] == "buy"
    assert sh_result["signals"]["regime"] == "bear"
    assert sh_result["signals"]["weight"] == 0.40

    psq_result = signals_fn("PSQ", bars)
    assert psq_result is not None
    assert psq_result["action"] == "buy"
    assert psq_result["signals"]["weight"] == 0.30

    sds_result = signals_fn("SDS", bars)
    assert sds_result is not None
    assert sds_result["action"] == "buy"
    assert sds_result["signals"]["weight"] == 0.20

    gld_result = signals_fn("GLD", bars)
    assert gld_result is not None
    assert gld_result["action"] == "buy"
    assert gld_result["signals"]["weight"] == 0.10

    tlt_result = signals_fn("TLT", bars)
    assert tlt_result is None


def test_tail_risk_hedge_regime_change_sells():
    """When regime changes from bull to bear, existing GLD position gets sold."""
    # First 10 days bull, then bear from day 10 onward
    regime_by_date = {}
    for i in range(20):
        d = date(2024, 1, 1) + timedelta(days=i)
        regime_by_date[d] = "bull" if i < 10 else "bear"

    signals_fn = make_tail_risk_hedge_signals_fn(
        regime_by_date=regime_by_date,
        position_size_pct=0.25,
        initial_capital=100_000,
    )

    # Buy GLD in bull regime (bars up to day 9)
    bull_bars = _make_bars(10)
    buy_result = signals_fn("GLD", bull_bars)
    assert buy_result is not None
    assert buy_result["action"] == "buy"

    # Now present bars that include a bear-regime day (day 10)
    bear_bars = _make_bars(11)
    sell_result = signals_fn("GLD", bear_bars)
    assert sell_result is not None
    assert sell_result["action"] == "sell"
    assert sell_result["exit_reason"] == "regime_change"


def test_tail_risk_hedge_hydrated_obsolete_position_sells_full_quantity():
    bars = _make_bars(10)
    regime = {bar["date"]: "bull" for bar in bars}
    context = PortfolioContext(
        positions={"SH": HeldPosition(11, 100, 100, date(2024, 1, 1))},
        pending_orders={
            "sell": PendingOrder("SH", "sell", 5, 100, "sell")
        }, sleeve_budget=100_000, reserved_notional=0,
    )
    fn = make_tail_risk_hedge_signals_fn(regime, portfolio_context=context)
    signal = fn("SH", bars)
    assert signal["action"] == "sell"
    assert signal["quantity"] == 6


def test_tail_risk_hedge_hydrates_regime_at_entry_for_shared_asset():
    bars = _make_bars(11)
    regime = {bar["date"]: ("bull" if i < 10 else "bear")
              for i, bar in enumerate(bars)}
    context = PortfolioContext(
        positions={"GLD": HeldPosition(8, 100, 100, bars[0]["date"])},
        pending_orders={}, sleeve_budget=100_000, reserved_notional=0,
    )
    fn = make_tail_risk_hedge_signals_fn(regime, portfolio_context=context)
    signal = fn("GLD", bars)
    assert signal["action"] == "sell"
    assert signal["quantity"] == 8
