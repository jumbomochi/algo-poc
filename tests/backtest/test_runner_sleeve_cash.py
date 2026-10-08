"""KAN-111: the backtest (and so the rolling shadow) funds entries from cash.

Before this the runner gated entries only through the risk engine's NAV
limits, so a sleeve's simulated cash could go negative up to the 150% exposure
limit — leverage live can never take, because the fill projector refuses any
fill that overdraws a sleeve. Live, the shadow and the backtest now size with
the same function, and these tests pin that they agree.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backtest.costs import CostModel
from backtest.runner import BacktestRunner
from backtest.shadow_series import replay_window
from backtest.simulator import SimulatedExecutor
from scripts.paper_state import PaperTradingState
from scripts.run_backtest import PortfolioConfig
from scripts.run_paper import run_daily
from services.risk_management.engine import RiskEngine
from services.risk_management.funding import sleeve_buy_cost_usd
from shared.models.base import Base

DAYS = [date(2025, 1, 6 + i) for i in range(5)]
D1, D2, D3, D4, D5 = DAYS


def _flat(price: float, days=DAYS) -> list[dict]:
    return [
        {"date": d, "open": price, "high": price, "low": price, "close": price}
        for d in days
    ]


def _permissive() -> RiskEngine:
    return RiskEngine(
        position_entry_limit_pct=100.0,
        sector_concentration_pct=1_000.0,
        total_exposure_limit_pct=1_000.0,
    )


def _runner(capital: float, *, whole_shares: bool = False) -> BacktestRunner:
    # Defaults: 10 bps slippage on these tickers, $0.005/share, $1 minimum.
    return BacktestRunner(
        SimulatedExecutor(CostModel()),
        initial_capital=capital,
        whole_shares=whole_shares,
    )


def _signals(by_key: dict):
    def fn(ticker, bars):
        return by_key.get((ticker, bars[-1]["date"]))

    return fn


def _buy(qty: float, px: float) -> dict:
    return {"action": "buy", "limit_price": px, "quantity": qty}


def _held(result) -> dict[str, float]:
    return {p["ticker"]: p["quantity"] for p in result.open_positions}


def test_entries_beyond_cash_are_downsized_then_skipped_and_cash_never_goes_negative():
    bars = {t: _flat(100.0) for t in ("AAA", "BBB", "CCC")}
    # Three entries of 60% each on a 10,000 sleeve, decided on one close.
    signals = _signals({(t, D1): _buy(60.0, 100.0) for t in bars})

    result = _runner(10_000.0).run(bars, signals, _permissive())

    held = _held(result)
    assert held["AAA"] == 60.0
    # 10,000 - 6,016 (60 * 100.25 + 1) leaves 3,984 for BBB.
    assert held["BBB"] == pytest.approx(39.7307, abs=1e-4)
    assert "CCC" not in held
    assert result.sleeve_cash == {"downsized": 1, "skipped": 1}
    cash = result.portfolio_values[-1] - sum(q * 100.0 for q in held.values())
    assert cash >= 0.0


def test_ample_cash_backtest_is_unchanged():
    bars = {t: _flat(100.0) for t in ("AAA", "BBB")}
    signals = _signals({(t, D1): _buy(10.0, 100.0) for t in bars})

    result = _runner(10_000.0).run(bars, signals, _permissive())

    assert _held(result) == {"AAA": 10.0, "BBB": 10.0}
    assert result.sleeve_cash == {"downsized": 0, "skipped": 0}


def test_same_session_exit_does_not_fund_a_same_session_entry():
    """Live books a sale only when it fills; the replay must not assume the
    proceeds either. The rotation completes one session later."""
    bars = {"AAA": _flat(100.0), "BBB": _flat(100.0)}
    signals = _signals({
        ("AAA", D1): _buy(99.0, 100.0),
        ("AAA", D2): {"action": "sell", "limit_price": 100.0, "quantity": 0,
                      "exit_reason": "rank_replacement"},
        ("BBB", D2): _buy(99.0, 100.0),  # decided with AAA's exit
        ("BBB", D3): _buy(99.0, 100.0),  # decided after AAA's exit filled
    })

    result = _runner(10_000.0, whole_shares=True).run(bars, signals, _permissive())

    # D2: AAA's 89.10 of leftover cash buys no share of BBB.
    assert result.sleeve_cash == {"downsized": 0, "skipped": 1}
    [aaa] = result.trades
    assert aaa["exit_date"] == D3
    [bbb] = result.open_positions
    assert (bbb["ticker"], bbb["entry_date"], bbb["quantity"]) == ("BBB", D4, 99.0)


def test_whole_share_replay_downsizes_in_whole_shares():
    bars = {"AAA": _flat(167.04), "BBB": _flat(167.04)}
    signals = _signals({(t, D1): _buy(11.0, 167.04) for t in bars})

    result = _runner(2_000.0, whole_shares=True).run(bars, signals, _permissive())

    # 11 @ 167.04 costs 1,843.04 with buffer and commission; 156.96 is left,
    # below one share (168.46) — skipped, not booked as a fraction.
    assert _held(result) == {"AAA": 11.0}
    assert result.sleeve_cash == {"downsized": 0, "skipped": 1}


# --------------------------------------------------------------- parity


@pytest.mark.parametrize("fractional", [True, False])
@pytest.mark.parametrize("cash", [500.0, 2_000.0, 2_500.0, 50_000.0])
def test_live_and_backtest_size_the_same_buys_identically(fractional, cash):
    """Same sleeve cash, same candidate buys decided on one close: the paper
    run emits exactly the quantities the backtest books."""
    orders = {"AAA": (11.1691, 167.04), "BBB": (7.5, 98.31), "CCC": (3.25, 401.2)}
    bars = {t: _flat(px) for t, (_, px) in orders.items()}

    result = _runner(cash, whole_shares=not fractional).run(
        bars,
        _signals({(t, D1): _buy(q, px) for t, (q, px) in orders.items()}),
        _permissive(),
    )

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    state = PaperTradingState.create_new({"sleeve": cash}, session)
    live = run_daily(
        state,
        {"sleeve": PortfolioConfig(
            name="sleeve", capital=cash, risk_engine=_permissive(),
            signals_fn=lambda t, b: _buy(*orders[t]),
        )},
        {t: _flat(px, [D1]) for t, (_, px) in orders.items()},
        settled_cash_trading=1e9,
        active_buy_reservations_usd=0.0,
        commission_per_share_usd=0.005,
        minimum_commission_usd=1.0,
        minimum_settled_usd_reserve=0.0,
        fractional_orders=fractional,
    )
    session.close()

    live_qty = {s["ticker"]: s["quantity"] for s in live}
    if not fractional:
        # Execution floors a fitting fractional request to whole shares; the
        # whole-share backtest truncates before sizing. Compare what fills.
        live_qty = {t: float(int(q)) for t, q in live_qty.items() if int(q) > 0}
    assert _held(result) == pytest.approx(live_qty)
    spent = sum(
        sleeve_buy_cost_usd(q, orders[t][1], per_share=0.005, minimum=1.0,
                            buffer_bps=25.0)
        for t, q in live_qty.items()
    )
    assert spent <= cash


def test_shadow_replay_funds_entries_from_its_seed():
    bars = {t: _flat(100.0) for t in ("AAA", "BBB")}
    signals = _signals({(t, D2): _buy(60.0, 100.0) for t in bars})

    curve = replay_window(
        bars_by_ticker=bars,
        signals_fn=signals,
        risk_engine=_permissive(),
        seed_nav=10_000.0,
        window_start=D2,
    )

    # With 150%-style borrowing the shadow would hold 12,000 of stock on a
    # 10,000 seed; funded from cash it holds at most the seed less costs.
    assert all(value <= 10_000.0 for value in curve.values())
    assert min(curve.values()) > 9_900.0
