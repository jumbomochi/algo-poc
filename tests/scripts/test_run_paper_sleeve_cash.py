"""KAN-111: run_paper sizes buys against the sleeve's ledger cash.

Before this, a buy was sized and approved against the sleeve's NAV budget and
an exposure limit of up to 150% — never against ``portfolio_configs.cash``,
which is the one number the fill projector refuses to overdraw. On 2026-10-07
thematic_momentum held $24.83 and was ~99% invested, ``BUY ARKW 11.1691 @
167.04`` was approved, filled at IB, and the projector dead-lettered the fill.

These tests pin the cap end to end through ``run_daily``:

* the 10-07 replay emits no order and says why;
* a partly funded buy is downsized (whole shares unless fractional orders);
* open orders and this run's earlier buys count against the cash;
* every emitted buy, filled at its limit plus the buffer, is accepted by the
  real projector — the invariant, checked across random books;
* a sleeve with ample cash sees exactly what it saw before.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from scripts.paper_state import PaperTradingState
from scripts.run_backtest import PortfolioConfig
from scripts.run_paper import run_daily as _run_daily, sleeve_buy_commitments
from services.risk_management.engine import RiskEngine
from services.risk_management.funding import (
    DEFAULT_SLEEVE_CASH_BUFFER_BPS,
    estimate_commission_usd,
)
from shared.models.base import Base
from shared.order_ledger import OrderLedger
from shared.models import OrderStatus

SLEEVE = "thematic_momentum"
ACCOUNT = "DUTEST"
BUFFER = 1 + DEFAULT_SLEEVE_CASH_BUFFER_BPS / 10_000


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _book(session, *, budget: float, cash: float, invested: float = 0.0):
    """A sleeve with ``invested`` in one holding and ``cash`` left over."""
    state = PaperTradingState.create_new({SLEEVE: budget}, session)
    if invested:
        state.record_fill(
            SLEEVE, "ARKG", "buy", 1_000.0, invested / 1_000.0,
            datetime(2026, 9, 1, tzinfo=timezone.utc).date(),
        )
    state._update_cash(SLEEVE, cash - state.get_cash(SLEEVE))
    assert state.get_cash(SLEEVE) == pytest.approx(cash)
    return state


def _bars(close: float) -> list[dict]:
    return [
        {"date": f"2026-10-0{i + 1}", "open": close, "high": close,
         "low": close, "close": close, "volume": 1}
        for i in range(6)
    ]


def _thematic(budget: float, orders: dict[str, tuple[float, float]]):
    """thematic_momentum's real risk limits, buying ``{ticker: (qty, px)}``."""

    def signals_fn(ticker, bars):
        if ticker not in orders:
            return None
        qty, px = orders[ticker]
        return {"action": "buy", "limit_price": px, "quantity": qty}

    return {
        SLEEVE: PortfolioConfig(
            name=SLEEVE,
            capital=budget,
            signals_fn=signals_fn,
            risk_engine=RiskEngine(
                position_entry_limit_pct=15.0,
                sector_concentration_pct=50.0,
                total_exposure_limit_pct=120.0,
                max_lots_per_ticker=1,
            ),
        )
    }


def run_daily(state, portfolios, bars, **kwargs):
    kwargs.setdefault("settled_cash_trading", 10_000_000)
    kwargs.setdefault("active_buy_reservations_usd", 0)
    kwargs.setdefault("commission_per_share_usd", 0.005)
    kwargs.setdefault("minimum_commission_usd", 1.0)
    kwargs.setdefault("minimum_settled_usd_reserve", 0)
    return _run_daily(state, portfolios, bars, **kwargs)


def _cost(qty: float, px: float) -> float:
    return qty * px * BUFFER + estimate_commission_usd(qty, per_share=0.005, minimum=1.0)


# ---------------------------------------------------------------- 2026-10-07


def test_replay_2026_10_07_fully_invested_sleeve_emits_no_arkw_buy(session, capsys):
    state = _book(session, budget=14_100.0, cash=24.83, invested=14_075.17)
    portfolios = _thematic(14_100.0, {"ARKW": (11.1691, 167.04)})

    # The regression: the NAV limits alone approve this buy (99.8% invested
    # against a 120% limit leaves ~$2.8k of headroom).
    held = state.get_positions(SLEEVE)
    decision = portfolios[SLEEVE].risk_engine.check_entry(
        ticker="ARKW", quantity=11.1691, price=167.04, sector="Unknown",
        portfolio=SimpleNamespace(
            nav=14_100.0, peak_nav=14_100.0,
            positions={t: {"quantity": p["quantity"]} for t, p in held.items()},
            sector_exposure={}, total_exposure_pct=14_075.17 / 14_100.0 * 100,
            margin_utilization_pct=0.0,
        ),
    )
    assert decision.approved

    signals = run_daily(state, portfolios, {"ARKW": _bars(167.04)})

    assert signals == []
    out = capsys.readouterr().out
    assert (
        "SKIP   ARKW   11.1691 @ $  167.04  [thematic_momentum] "
        "(insufficient sleeve cash: have $24.83, need $1,871.35; "
        "1 share needs $168.46)"
    ) in out


# ------------------------------------------------------------ downsizing


def test_partly_funded_buy_is_downsized_to_whole_shares(session, capsys):
    state = _book(session, budget=14_100.0, cash=1_000.0, invested=13_100.0)

    [signal] = run_daily(
        state, _thematic(14_100.0, {"ARKW": (11.1691, 167.04)}),
        {"ARKW": _bars(167.04)},
    )

    # 5 shares: 5 * 167.04 * 1.0025 + 1 = 838.30; 6 would need 1,005.76.
    assert signal["quantity"] == 5.0
    assert _cost(6, 167.04) > 1_000.0 >= _cost(5, 167.04)
    assert "CAP    ARKW   11.1691 -> 5.0000  [thematic_momentum]" in capsys.readouterr().out


def test_fractional_account_downsizes_to_the_risk_engines_step(session):
    state = _book(session, budget=14_100.0, cash=1_000.0, invested=13_100.0)

    [signal] = run_daily(
        state, _thematic(14_100.0, {"ARKW": (11.1691, 167.04)}),
        {"ARKW": _bars(167.04)}, fractional_orders=True,
    )

    assert signal["quantity"] == pytest.approx(5.9656)
    assert _cost(signal["quantity"], 167.04) <= 1_000.0
    assert _cost(signal["quantity"] + 0.0001, 167.04) > 1_000.0


def test_open_buy_orders_count_against_the_cash(session):
    state = _book(session, budget=14_100.0, cash=2_000.0, invested=12_100.0)

    [signal] = run_daily(
        state, _thematic(14_100.0, {"ARKW": (11.1691, 167.04)}),
        {"ARKW": _bars(167.04)},
        sleeve_buy_commitments_usd={SLEEVE: 1_000.0},
    )

    assert signal["quantity"] == 5.0


def test_two_buys_in_one_run_cannot_jointly_exceed_the_cash(session, capsys):
    state = _book(session, budget=14_100.0, cash=2_000.0, invested=12_100.0)
    portfolios = _thematic(
        14_100.0, {"ARKW": (11.1691, 167.04), "ARKK": (11.1691, 167.04)}
    )

    signals = run_daily(
        state, portfolios, {"ARKK": _bars(167.04), "ARKW": _bars(167.04)}
    )

    # The first fits whole (1,871.35); the second has 128.65 left, below
    # one share's 168.46.
    assert [(s["ticker"], s["quantity"]) for s in signals] == [("ARKK", 11.1691)]
    assert sum(_cost(s["quantity"], s["limit_price"]) for s in signals) <= 2_000.0
    assert "SKIP   ARKW" in capsys.readouterr().out


def test_same_day_sell_proceeds_do_not_fund_the_same_days_buy(session):
    """The sale is credited only when it fills; the buy can fill first."""
    state = _book(session, budget=14_100.0, cash=24.83, invested=14_075.17)

    def rotate(ticker, bars):
        if ticker == "ARKG":
            return {"action": "sell", "limit_price": 14.07517, "quantity": 0,
                    "exit_reason": "rank_replacement"}
        return {"action": "buy", "limit_price": 167.04, "quantity": 11.1691}

    portfolios = _thematic(14_100.0, {})
    portfolios[SLEEVE].signals_fn = rotate

    signals = run_daily(
        state, portfolios, {"ARKG": _bars(14.07517), "ARKW": _bars(167.04)}
    )

    assert [(s["action"], s["ticker"]) for s in signals] == [("sell", "ARKG")]


def test_sleeve_commitments_are_the_sleeves_own_open_buys_with_commission(session):
    ledger = OrderLedger(session)

    def intent(rec, portfolio, action="BUY", qty=10.0, px=100.0):
        ledger.create_intent(SimpleNamespace(
            recommendation_id=rec, account_id=ACCOUNT, mode="paper",
            portfolio=portfolio, con_id=sum(map(ord, rec)), symbol=rec.upper(),
            exchange="SMART", currency="USD", action=action, quantity=qty,
            limit_price=px if action == "BUY" else None,
            order_type="LMT" if action == "BUY" else "MKT",
        ))

    intent("approved", SLEEVE)
    ledger.transition("approved", OrderStatus.APPROVED)
    intent("published", SLEEVE, qty=2.0)  # proposed, already sent to risk
    ledger.mark_published("published")
    intent("unpublished", SLEEVE)  # never left this process: not committed
    intent("other-sleeve", "momentum")
    ledger.transition("other-sleeve", OrderStatus.APPROVED)
    intent("a-sell", SLEEVE, action="SELL")
    ledger.transition("a-sell", OrderStatus.APPROVED)
    session.flush()

    assert sleeve_buy_commitments(
        session, ACCOUNT, [SLEEVE, "momentum", "quality_value"],
        commission_per_share=0.005, minimum_commission=1.0,
    ) == {
        SLEEVE: pytest.approx(1_000.0 + 1.0 + 200.0 + 1.0),
        "momentum": pytest.approx(1_001.0),
        "quality_value": 0.0,
    }


# ------------------------------------------------------------ regression


def test_ample_cash_sleeve_is_unchanged(session):
    state = _book(session, budget=14_100.0, cash=14_100.0)
    portfolios = _thematic(
        14_100.0, {"ARKW": (11.1691, 167.04), "ARKK": (7.25, 50.0)}
    )

    signals = run_daily(
        state, portfolios, {"ARKK": _bars(50.0), "ARKW": _bars(167.04)}
    )

    assert [(s["ticker"], s["quantity"]) for s in signals] == [
        ("ARKK", 7.25), ("ARKW", 11.1691),
    ]


# ------------------------------------------------- projector invariant


def _project_buy(state, *, ticker, con_id, quantity, price, commission):
    state._apply_fill_accounting(
        account_id=ACCOUNT,
        portfolio=SLEEVE,
        ticker=ticker,
        action="buy",
        quantity=quantity,
        price=price,
        fill_datetime=datetime(2026, 10, 7, 14, tzinfo=timezone.utc),
        commission=commission,
        con_id=con_id,
        exchange="SMART",
        currency="USD",
        strict_quantity=True,
    )


@pytest.mark.parametrize("seed", range(150))
def test_no_emitted_buy_can_overdraw_the_sleeve_at_its_limit_plus_buffer(seed):
    """Whatever the book, every buy the run emits is one the projector books.

    Random cash, open buy orders (real ledger rows, counted through the same
    helper ``main`` uses) and a batch of candidate buys. Then the worst case is
    projected: every open order fills in full at its limit, and every emitted
    buy fills at its limit grown by the buffer, with the estimated commission.
    The real projector must accept all of them.
    """
    rng = random.Random(seed)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    budget = rng.uniform(2_000, 30_000)
    cash = rng.choice([0.0, rng.uniform(0, 500), rng.uniform(0, budget)])
    state = PaperTradingState.create_new({SLEEVE: budget}, session)
    state._update_cash(SLEEVE, cash - budget)

    ledger = OrderLedger(session)
    open_orders = []
    unreserved = cash
    for i in range(rng.randint(0, 3)):
        qty, px = float(rng.randint(1, 8)), round(rng.uniform(5, 300), 2)
        reserved = qty * px + estimate_commission_usd(qty, per_share=0.005, minimum=1.0)
        if reserved > unreserved:
            continue  # an earlier run's cap would not have emitted it
        unreserved -= reserved
        rec = f"open-{i}"
        ledger.create_intent(SimpleNamespace(
            recommendation_id=rec, account_id=ACCOUNT, mode="paper",
            portfolio=SLEEVE, con_id=9_000 + i, symbol=f"OPN{i}",
            exchange="SMART", currency="USD", action="BUY", quantity=qty,
            limit_price=px, order_type="LMT",
        ))
        ledger.transition(rec, OrderStatus.APPROVED)
        open_orders.append((f"OPN{i}", 9_000 + i, qty, px))
    session.flush()
    commitments = sleeve_buy_commitments(
        session, ACCOUNT, [SLEEVE], commission_per_share=0.005,
        minimum_commission=1.0,
    )

    orders = {
        f"T{i}": (round(rng.uniform(0.5, 40), 4), round(rng.uniform(5, 400), 2))
        for i in range(rng.randint(1, 6))
    }
    portfolios = _thematic(budget, orders)
    portfolios[SLEEVE].risk_engine.position_entry_limit_pct = 100.0
    portfolios[SLEEVE].risk_engine.total_exposure_limit_pct = 1_000.0
    fractional = rng.random() < 0.5

    signals = run_daily(
        state, portfolios,
        {t: _bars(px) for t, (_, px) in orders.items()},
        sleeve_buy_commitments_usd=commitments,
        fractional_orders=fractional,
    )

    for ticker, con_id, qty, px in open_orders:
        _project_buy(
            state, ticker=ticker, con_id=con_id, quantity=qty, price=px,
            commission=estimate_commission_usd(qty, per_share=0.005, minimum=1.0),
        )
    for n, signal in enumerate(signals):
        qty = signal["quantity"]
        _project_buy(  # raises "fill would make sleeve cash negative" on a breach
            state, ticker=signal["ticker"], con_id=n + 1, quantity=qty,
            price=signal["limit_price"] * BUFFER,
            commission=estimate_commission_usd(qty, per_share=0.005, minimum=1.0),
        )
    assert state.get_cash(SLEEVE) >= -1e-9
    session.close()
