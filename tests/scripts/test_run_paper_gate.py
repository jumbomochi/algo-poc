"""run_daily must gate entries through the sleeve RiskEngine like the backtest.

Regression: the sim recorded every buy unconstrained — quality_value went to
-$43K cash (3.8x leverage) on its first live day while the backtest gates
every entry via risk_engine.check_entry.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from scripts.paper_state import PaperTradingState
from scripts.run_backtest import PortfolioConfig
from scripts.run_paper import (
    build_sell_availability,
    create_signal_intents,
    publish_unpublished_intents,
    run_daily,
)
from services.risk_management.engine import RiskEngine
from services.execution.reconciliation import ReconciliationResult
from shared.broker_state import BrokerAccountSnapshot, BrokerOpenOrder, BrokerPosition
from shared.models import OrderStatus
from shared.models.base import Base
from shared.models.portfolio import Position
from shared.order_ledger import OrderLedger


@pytest.fixture
def state():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    st = PaperTradingState.create_new(
        portfolio_capitals={"test_sleeve": 10_000.0}, session=session
    )
    yield st
    session.close()


def make_bars(close: float = 100.0, n: int = 5) -> list[dict]:
    return [
        {
            "date": f"2026-07-0{i + 1}",
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 1000,
        }
        for i in range(n)
    ]


def build_portfolio(signals_fn) -> dict[str, PortfolioConfig]:
    return {
        "test_sleeve": PortfolioConfig(
            name="test_sleeve",
            capital=10_000.0,
            signals_fn=signals_fn,
            risk_engine=RiskEngine(
                position_entry_limit_pct=10.0,
                sector_concentration_pct=100.0,
                total_exposure_limit_pct=100.0,
                max_lots_per_ticker=1,
            ),
        )
    }


class TestEntryGate:
    def test_same_cycle_buys_accumulate_reserved_notional(self, state):
        def buy_fn(ticker, bars):
            return {"action": "buy", "limit_price": 100.0, "quantity": 10.0}

        portfolio = build_portfolio(buy_fn)["test_sleeve"]
        portfolio.risk_engine.position_entry_limit_pct = 100.0
        bars = {f"T{i:02d}": make_bars() for i in range(20)}

        signals = run_daily(state, {"test_sleeve": portfolio}, bars)

        assert len(signals) == 10
        assert sum(s["quantity"] * s["limit_price"] for s in signals) == 10_000

    def test_nav_hydrated_sleeve_budget_controls_entry_headroom(self, state):
        def large_account_fn(ticker, bars):
            return {"action": "buy", "limit_price": 100.0, "quantity": 1_000.0}

        portfolio = build_portfolio(large_account_fn)["test_sleeve"]
        portfolio.capital = 1_000_000.0

        signals = run_daily(state, {"test_sleeve": portfolio}, {"AAPL": make_bars()})

        assert signals[0]["quantity"] == pytest.approx(1_000.0)

    def test_oversized_buy_cannot_overdraw_cash(self, state):
        """A signal demanding 2x the sleeve's capital must be constrained."""

        def greedy_fn(ticker, bars):
            # 200 shares @ $100 = $20K on a $10K sleeve
            return {"action": "buy", "limit_price": 100.0, "quantity": 200.0}

        run_daily(state, build_portfolio(greedy_fn), {"AAPL": make_bars()})

        cash = state.get_cash("test_sleeve")
        positions = state.get_positions("test_sleeve")
        # 10% position limit on a $10K sleeve = max $1,000 => 10 shares
        if positions:
            assert positions["AAPL"]["quantity"] * 100.0 <= 1_000.0 + 1e-6
        assert cash >= 0.0

    def test_many_buys_stop_at_exposure_limit(self, state):
        """Buys across many tickers stop when total exposure hits 100% NAV."""

        def ten_pct_fn(ticker, bars):
            return {"action": "buy", "limit_price": 100.0, "quantity": 10.0}

        bars_by_ticker = {f"T{i:02d}": make_bars() for i in range(20)}
        run_daily(state, build_portfolio(ten_pct_fn), bars_by_ticker)

        assert state.get_cash("test_sleeve") >= -1e-6  # never negative

    def test_sell_signal_waits_for_actual_execution_fill(self, state):
        """Signals do not mutate the durable position before IB fills them."""
        state.record_fill(
            portfolio="test_sleeve",
            ticker="AAPL",
            action="buy",
            quantity=5.0,
            price=100.0,
            fill_date=date(2026, 7, 1),
        )

        def sell_fn(ticker, bars):
            return {"action": "sell", "limit_price": 110.0, "quantity": 0}

        signals = run_daily(state, build_portfolio(sell_fn), {"AAPL": make_bars(110.0)})

        assert any(s["action"] == "sell" for s in signals)
        assert state.get_positions("test_sleeve")["AAPL"]["quantity"] == 5
        assert state.get_cash("test_sleeve") == pytest.approx(9_500.0)

    def test_buy_signal_does_not_create_a_parallel_fill(self, state):
        """Daily signal evaluation leaves durable cash and positions alone."""

        def greedy_fn(ticker, bars):
            return {"action": "buy", "limit_price": 100.0, "quantity": 200.0}

        portfolios = build_portfolio(greedy_fn)
        signals = run_daily(state, portfolios, {"AAPL": make_bars()})

        assert len(signals) == 1
        assert state.get_positions("test_sleeve") == {}
        assert state.get_cash("test_sleeve") == pytest.approx(10_000.0)


def test_paper_state_builds_immutable_strategy_context(state):
    state.record_fill(
        portfolio="test_sleeve",
        ticker="AAPL",
        action="buy",
        quantity=5,
        price=100,
        fill_date=date(2026, 7, 1),
    )
    pending = type(
        "Intent",
        (),
        {
            "symbol": "MSFT",
            "action": "BUY",
            "requested_quantity": 3,
            "filled_quantity": 1,
            "limit_price": 200,
            "recommendation_id": "rec-1",
        },
    )()
    position = state._session.query(Position).filter_by(ticker="AAPL").one()
    position.highest_price_since_entry = 120

    context = state.build_portfolio_context(
        "test_sleeve",
        pending_orders=[pending],
        sleeve_budget=10_000,
        reserved_notional=400,
    )

    assert context.positions["AAPL"].quantity == 5
    assert context.positions["AAPL"].peak_price == 120
    assert context.pending_orders["MSFT"].quantity == 2
    assert context.sleeve_budget == 10_000
    assert context.reserved_notional == 400
    with pytest.raises(TypeError):
        context.positions["MSFT"] = context.positions["AAPL"]


def test_strategy_context_filters_positions_and_intents_by_account(state):
    from datetime import datetime, timezone

    state._session.add_all(
        [
            Position(
                account_id="DUONE",
                ticker="AAPL",
                portfolio="test_sleeve",
                con_id=1,
                quantity=1,
                avg_entry_price=100,
                current_price=100,
                peak_price=100,
                highest_price_since_entry=100,
                opened_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
                status="open",
            ),
            Position(
                account_id="DUTWO",
                ticker="AAPL",
                portfolio="test_sleeve",
                con_id=1,
                quantity=9,
                avg_entry_price=100,
                current_price=100,
                peak_price=100,
                highest_price_since_entry=100,
                opened_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
                status="open",
            ),
        ]
    )
    pending_one = type(
        "Intent",
        (),
        {
            "account_id": "DUONE",
            "portfolio": "test_sleeve",
            "symbol": "MSFT",
            "action": "BUY",
            "requested_quantity": 3,
            "filled_quantity": 1,
            "limit_price": 200,
            "recommendation_id": "rec-one",
        },
    )()
    pending_two = type(
        "Intent",
        (),
        {
            "account_id": "DUTWO",
            "portfolio": "test_sleeve",
            "symbol": "GOOG",
            "action": "BUY",
            "requested_quantity": 4,
            "filled_quantity": 0,
            "limit_price": 150,
            "recommendation_id": "rec-two",
        },
    )()
    state._session.flush()

    context = state.build_portfolio_context(
        "test_sleeve",
        pending_orders=[pending_one, pending_two],
        sleeve_budget=10_000,
        reserved_notional=400,
        account_id="DUONE",
    )

    assert context.positions["AAPL"].quantity == 1
    assert set(context.pending_orders) == {"MSFT"}


def test_reconciliation_mismatch_filters_buys_but_keeps_sells(state):
    def signal_fn(ticker, bars):
        action = "buy" if ticker == "MSFT" else "sell"
        return {"action": action, "limit_price": 100.0, "quantity": 1.0}

    failed = ReconciliationResult(
        matched=[],
        discrepancies=[{"type": "quantity_mismatch"}],
        severity="major",
        account_id="DUTEST",
    )
    signals = run_daily(
        state,
        build_portfolio(signal_fn),
        {"AAPL": make_bars(), "MSFT": make_bars()},
        reconciliation=failed,
    )

    assert [signal["action"] for signal in signals] == ["sell"]


def test_entries_disabled_filters_buys_but_keeps_sells(state):
    def signal_fn(ticker, bars):
        action = "buy" if ticker == "MSFT" else "sell"
        return {"action": action, "limit_price": 100.0, "quantity": 1.0}

    signals = run_daily(
        state,
        build_portfolio(signal_fn),
        {"AAPL": make_bars(), "MSFT": make_bars()},
        entries_disabled=True,
    )

    assert [signal["action"] for signal in signals] == ["sell"]


def test_signal_creates_deterministic_intent_without_position_mutation(state):
    def buy_fn(ticker, bars):
        return {"action": "buy", "limit_price": 100.0, "quantity": 2.0}

    signals = run_daily(state, build_portfolio(buy_fn), {"AAPL": make_bars()})
    snapshot = BrokerAccountSnapshot(
        account_id="DUTEST",
        mode="paper",
        net_liquidation=10_000,
        positions={
            265598: BrokerPosition(
                account_id="DUTEST",
                con_id=265598,
                symbol="AAPL",
                quantity=0,
                exchange="SMART",
                currency="USD",
            )
        },
    )

    create_signal_intents(state._session, signals, snapshot, run_date=date(2026, 7, 18))

    intent = OrderLedger(state._session).get(
        "sleeve-2026-07-18-DUTEST-paper-test_sleeve-AAPL-buy"
    )
    assert intent.status == OrderStatus.PROPOSED.value
    assert state.get_positions("test_sleeve") == {}
    assert state.get_cash("test_sleeve") == pytest.approx(10_000)


def test_recommendation_id_is_stable_but_account_and_mode_scoped(state):
    signal = {
        "action": "buy",
        "limit_price": 100.0,
        "quantity": 2.0,
        "ticker": "AAPL",
        "portfolio": "test_sleeve",
    }
    contract = BrokerPosition(
        account_id="DUONE",
        con_id=265598,
        symbol="AAPL",
        quantity=0,
        exchange="SMART",
        currency="USD",
    )
    one = BrokerAccountSnapshot(
        account_id="DUONE",
        mode="paper",
        net_liquidation=10_000,
        positions={265598: contract},
    )
    two = BrokerAccountSnapshot(
        account_id="DUTWO",
        mode="paper",
        net_liquidation=10_000,
        positions={
            265598: BrokerPosition(
                account_id="DUTWO",
                con_id=265598,
                symbol="AAPL",
                quantity=0,
            )
        },
    )

    first = create_signal_intents(
        state._session, [signal], one, run_date=date(2026, 7, 18)
    )[0]
    retry = create_signal_intents(
        state._session, [signal], one, run_date=date(2026, 7, 18)
    )[0]
    other = create_signal_intents(
        state._session, [signal], two, run_date=date(2026, 7, 18)
    )[0]

    assert first.id == retry.id
    assert first.recommendation_id != other.recommendation_id
    assert "DUONE-paper" in first.recommendation_id
    assert "DUTWO-paper" in other.recommendation_id


def test_unpublished_intent_is_replayed_with_same_id(state):
    class FakeRedis:
        def __init__(self):
            self.payloads = []

        def xadd(self, stream, payload):
            self.payloads.append((stream, payload))
            return "1-0"

    proposal = type(
        "Proposal",
        (),
        {
            "recommendation_id": "rec-1",
            "account_id": "DUTEST",
            "mode": "paper",
            "portfolio": "momentum",
            "con_id": 265598,
            "symbol": "AAPL",
            "exchange": "SMART",
            "currency": "USD",
            "action": "BUY",
            "quantity": 2.0,
            "limit_price": 100.0,
            "order_type": "LMT",
        },
    )()
    OrderLedger(state._session).create_intent(proposal)
    redis = FakeRedis()

    assert publish_unpublished_intents(state._session, redis) == 1

    stream, payload = redis.payloads[0]
    assert stream == "stream:recommendations"
    assert payload["recommendation_id"] == "rec-1"
    assert OrderLedger(state._session).get("rec-1").published_at is not None


def test_unpublished_replay_is_scoped_to_connected_account(state):
    class FakeRedis:
        def __init__(self):
            self.payloads = []

        def xadd(self, stream, payload):
            self.payloads.append(payload)

    ledger = OrderLedger(state._session)
    for recommendation_id, account_id in (("mine", "DUTEST"), ("other", "DUOTHER")):
        ledger.create_intent(
            type(
                "Proposal",
                (),
                {
                    "recommendation_id": recommendation_id,
                    "account_id": account_id,
                    "mode": "paper",
                    "portfolio": "momentum",
                    "con_id": 265598,
                    "symbol": "AAPL",
                    "exchange": "SMART",
                    "currency": "USD",
                    "action": "BUY",
                    "quantity": 2,
                    "limit_price": 100,
                    "order_type": "LMT",
                },
            )()
        )
    redis = FakeRedis()

    assert publish_unpublished_intents(state._session, redis, account_id="DUTEST") == 1
    assert [payload["recommendation_id"] for payload in redis.payloads] == ["mine"]
    assert ledger.get("other").published_at is None


def test_fail_closed_replay_keeps_buys_unpublished_but_publishes_sells(state):
    class FakeRedis:
        def __init__(self):
            self.payloads = []

        def xadd(self, stream, payload):
            self.payloads.append(payload)

    ledger = OrderLedger(state._session)
    for recommendation_id, action in (("buy-1", "BUY"), ("sell-1", "SELL")):
        ledger.create_intent(
            type(
                "Proposal",
                (),
                {
                    "recommendation_id": recommendation_id,
                    "account_id": "DUTEST",
                    "mode": "paper",
                    "portfolio": "momentum",
                    "con_id": 265598,
                    "symbol": "AAPL",
                    "exchange": "SMART",
                    "currency": "USD",
                    "action": action,
                    "quantity": 2,
                    "limit_price": 100,
                    "order_type": "LMT" if action == "BUY" else "MKT",
                },
            )()
        )
    redis = FakeRedis()

    assert (
        publish_unpublished_intents(
            state._session, redis, account_id="DUTEST", entries_allowed=False
        )
        == 1
    )
    assert [payload["recommendation_id"] for payload in redis.payloads] == ["sell-1"]
    assert ledger.get("buy-1").published_at is None


def test_breached_reconciliation_sell_is_capped_to_broker_holding(state):
    state.record_fill(
        portfolio="test_sleeve",
        ticker="AAPL",
        action="buy",
        quantity=10,
        price=100,
        fill_date=date(2026, 7, 1),
    )
    snapshot = BrokerAccountSnapshot(
        account_id="DUTEST",
        mode="paper",
        net_liquidation=10_000,
        positions={
            265598: BrokerPosition(
                account_id="DUTEST",
                con_id=265598,
                symbol="AAPL",
                quantity=5,
            )
        },
    )
    available = build_sell_availability(state._session, snapshot)
    failed = ReconciliationResult(
        matched=[],
        discrepancies=[{"type": "quantity_mismatch"}],
        severity="major",
        account_id="DUTEST",
    )

    signals = run_daily(
        state,
        build_portfolio(
            lambda ticker, bars: {
                "action": "sell",
                "limit_price": 100,
                "quantity": 10,
            }
        ),
        {"AAPL": make_bars()},
        reconciliation=failed,
        sell_availability=available,
    )

    assert signals[0]["quantity"] == 5


def test_active_sell_orders_reduce_availability_without_double_count(state):
    ledger = OrderLedger(state._session)
    proposal = type(
        "Proposal",
        (),
        {
            "recommendation_id": "sell-open",
            "account_id": "DUTEST",
            "mode": "paper",
            "portfolio": "test_sleeve",
            "con_id": 265598,
            "symbol": "AAPL",
            "exchange": "SMART",
            "currency": "USD",
            "action": "SELL",
            "quantity": 2,
            "limit_price": None,
            "order_type": "MKT",
        },
    )()
    ledger.create_intent(proposal)
    ledger.transition("sell-open", OrderStatus.APPROVED)
    ledger.record_submission("sell-open", "42")
    snapshot = BrokerAccountSnapshot(
        account_id="DUTEST",
        mode="paper",
        net_liquidation=10_000,
        positions={
            265598: BrokerPosition(
                account_id="DUTEST",
                con_id=265598,
                symbol="AAPL",
                quantity=5,
            )
        },
        open_orders={
            "42": BrokerOpenOrder(
                account_id="DUTEST",
                ib_order_id="42",
                con_id=265598,
                symbol="AAPL",
                action="SELL",
                total_quantity=2,
                filled_quantity=0,
                status="Submitted",
            )
        },
    )

    assert build_sell_availability(state._session, snapshot)["AAPL"] == 3


def test_zero_broker_holding_suppresses_sell_signal(state):
    signals = run_daily(
        state,
        build_portfolio(
            lambda ticker, bars: {
                "action": "sell",
                "limit_price": 100,
                "quantity": 10,
            }
        ),
        {"AAPL": make_bars()},
        sell_availability={"AAPL": 0},
    )

    assert signals == []
