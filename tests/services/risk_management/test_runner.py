from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from types import SimpleNamespace

from services.risk_management.engine import PortfolioState, RiskDecision
from services.risk_management.runner import RiskServiceRunner
from shared.config import AppConfig, CurrencyConfig, RiskConfig
from shared.models import (
    Base,
    CapitalSnapshot,
    ExecutionFill,
    OrderIntent,
    OrderStatus,
    Position,
)
from shared.order_ledger import OrderLedger
from shared.observability import create_trading_metrics
from shared.schemas.messages import (
    KillMessage,
    RecommendationMessage,
)


def make_portfolio(
    nav: float = 100_000,
    peak_nav: float | None = None,
    positions: dict | None = None,
) -> PortfolioState:
    return PortfolioState(
        nav=nav,
        peak_nav=peak_nav if peak_nav is not None else nav,
        positions=positions or {},
        sector_exposure={},
        total_exposure_pct=0.0,
        margin_utilization_pct=0.0,
    )


def make_recommendation(
    ticker: str = "AAPL",
    action: str = "buy",
    confidence: float = 0.85,
) -> RecommendationMessage:
    return RecommendationMessage(
        ticker=ticker,
        timestamp=datetime.now(timezone.utc),
        action=action,
        confidence=confidence,
        top_features={"support_proximity": 0.3},
        recommendation_id=str(uuid.uuid4()),
    )


def make_execution_fill(
    *,
    executed_at: datetime,
    recommendation_id: str = "prior-buy",
    quantity: float = 9,
    price: float = 100,
    commission: float = 1,
) -> ExecutionFill:
    return ExecutionFill(
        account_id="DUTEST",
        execution_id=f"exec-{recommendation_id}-{executed_at.timestamp()}",
        ib_order_id="42",
        recommendation_id=recommendation_id,
        portfolio="quality_value",
        con_id=123,
        symbol="MSFT",
        exchange="SMART",
        currency="USD",
        side="BUY",
        quantity=quantity,
        price=price,
        commission=commission,
        commission_currency="USD",
        cumulative_quantity=quantity,
        executed_at=executed_at,
    )


def make_durable_buy_recommendation() -> RecommendationMessage:
    return RecommendationMessage(
        ticker="AAPL",
        timestamp=datetime.now(timezone.utc),
        action="buy",
        confidence=1,
        top_features={},
        recommendation_id="rec-risk",
        limit_price=100,
        quantity=10,
        portfolio="momentum",
    )


def make_buy_intent_proposal(
    recommendation_id: str,
    *,
    quantity: float,
    price: float,
    portfolio: str = "quality_value",
) -> SimpleNamespace:
    return SimpleNamespace(
        recommendation_id=recommendation_id,
        account_id="DUTEST",
        mode="paper",
        portfolio=portfolio,
        con_id=123,
        symbol="MSFT",
        exchange="SMART",
        currency="USD",
        action="BUY",
        quantity=quantity,
        limit_price=price,
        order_type="LMT",
    )


@pytest.fixture()
def mock_config():
    config = MagicMock(spec=AppConfig)
    config.mode = "paper"
    config.risk = RiskConfig()
    config.currency = CurrencyConfig()
    return config


@pytest.fixture()
def mock_redis():
    redis = AsyncMock()
    redis.publish = AsyncMock(return_value="msg-id-123")
    redis.create_consumer_group = AsyncMock()
    redis.read_group = AsyncMock(return_value=[])
    redis.ack = AsyncMock()
    redis.send_to_dead_letter = AsyncMock()
    redis.stream_length = AsyncMock(return_value=0)  # empty dlq by default
    return redis


@pytest.fixture()
def mock_portfolio():
    return make_portfolio(nav=100_000)


@pytest.fixture()
def runner(mock_config, mock_redis):
    r = RiskServiceRunner(
        config=mock_config,
        redis_client=mock_redis,
    )
    return r


class TestRecommendationProcessing:
    @pytest.mark.asyncio
    async def test_buy_without_durable_intent_fails_closed(
        self, runner, mock_redis, mock_portfolio
    ):
        """Approved buy recommendation -> published to stream:approved_orders."""
        runner._portfolio = mock_portfolio
        runner._current_prices["AAPL"] = 150.0  # buys without any price are rejected
        rec = make_recommendation(ticker="AAPL", action="buy")

        with (
            patch.object(
                runner._engine,
                "check_entry",
                return_value=RiskDecision(
                    approved=True, reason="ok", adjusted_quantity=50
                ),
            ),
            patch.object(
                runner._kill_switch,
                "check",
                return_value=RiskDecision(approved=True, reason="inactive"),
            ),
            patch.object(
                runner._engine,
                "check_portfolio_drawdown",
                return_value=RiskDecision(approved=True, reason="ok"),
            ),
        ):
            await runner.process_recommendation(rec)

        assert not any(
            call.args[0] == "stream:approved_orders"
            for call in mock_redis.publish.call_args_list
        )
    @pytest.mark.asyncio
    async def test_rejected_entry_does_not_publish(
        self, runner, mock_redis, mock_portfolio
    ):
        """Rejected entry -> nothing published to approved_orders."""
        runner._portfolio = mock_portfolio
        rec = make_recommendation(ticker="AAPL", action="buy")

        with (
            patch.object(
                runner._engine,
                "check_entry",
                return_value=RiskDecision(
                    approved=False, reason="sector limit", adjusted_quantity=0
                ),
            ),
            patch.object(
                runner._kill_switch,
                "check",
                return_value=RiskDecision(approved=True, reason="inactive"),
            ),
            patch.object(
                runner._engine,
                "check_portfolio_drawdown",
                return_value=RiskDecision(approved=True, reason="ok"),
            ),
        ):
            await runner.process_recommendation(rec)

        # Should publish alert but not approved order
        published_streams = [c[0][0] for c in mock_redis.publish.call_args_list]
        assert "stream:approved_orders" not in published_streams

    @pytest.mark.asyncio
    async def test_sell_without_durable_intent_fails_closed(
        self, runner, mock_redis, mock_portfolio
    ):
        """Sell recommendation should pass through risk checks."""
        runner._portfolio = mock_portfolio
        rec = make_recommendation(ticker="AAPL", action="sell")

        with (
            patch.object(
                runner._kill_switch,
                "check",
                return_value=RiskDecision(approved=True, reason="inactive"),
            ),
            patch.object(
                runner._engine,
                "check_portfolio_drawdown",
                return_value=RiskDecision(approved=True, reason="ok"),
            ),
        ):
            await runner.process_recommendation(rec)

        assert not any(
            call.args[0] == "stream:approved_orders"
            for call in mock_redis.publish.call_args_list
        )
    @pytest.mark.asyncio
    async def test_hold_recommendation_ignored(
        self, runner, mock_redis, mock_portfolio
    ):
        """Hold recommendations should not produce any orders."""
        runner._portfolio = mock_portfolio
        rec = make_recommendation(ticker="AAPL", action="hold")

        await runner.process_recommendation(rec)

        published_streams = [c[0][0] for c in mock_redis.publish.call_args_list]
        assert "stream:approved_orders" not in published_streams


class TestKillSwitchIntegration:
    @pytest.mark.asyncio
    async def test_kill_switch_active_rejects_buy(
        self, runner, mock_redis, mock_portfolio
    ):
        """Active kill switch should reject all entries."""
        runner._portfolio = mock_portfolio
        rec = make_recommendation(ticker="AAPL", action="buy")

        with patch.object(
            runner._kill_switch,
            "check",
            return_value=RiskDecision(
                approved=False, reason="Kill switch active: margin call"
            ),
        ):
            await runner.process_recommendation(rec)

        published_streams = [c[0][0] for c in mock_redis.publish.call_args_list]
        assert "stream:approved_orders" not in published_streams

    @pytest.mark.asyncio
    async def test_kill_message_activates_switch_and_liquidates(
        self, runner, mock_redis
    ):
        """Kill message should activate switch and emit sell orders for all positions."""
        runner._portfolio = make_portfolio(
            nav=100_000,
            positions={
                "AAPL": {"quantity": 100, "sector": "Technology"},
                "MSFT": {"quantity": 50, "sector": "Technology"},
            },
        )
        runner._current_prices = {"AAPL": 150.0, "MSFT": 300.0}

        kill_msg = KillMessage(
            timestamp=datetime.now(timezone.utc),
            triggered_by="admin",
            reason="emergency shutdown",
        )

        await runner.process_kill(kill_msg)

        assert runner._kill_switch.is_active is True
        # Should have published sell orders for both positions
        published_calls = mock_redis.publish.call_args_list
        order_calls = [
            c for c in published_calls if c[0][0] == "stream:approved_orders"
        ]
        assert len(order_calls) == 2


def _kill_msg(reason="emergency", triggered_by="admin***"):
    return KillMessage(
        timestamp=datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc),
        triggered_by=triggered_by,
        reason=reason,
    )


class TestKillLiquidation:
    """Kill liquidation must reload authoritative DB positions, route exits
    through the ledger with deterministic ids (idempotent on replay), always
    alert, and not abort on a single-position failure (review 1.3–1.5)."""

    @pytest.fixture
    def db_runner(self, mock_config, mock_redis):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = Session(engine)
        now = datetime(2026, 8, 1, tzinfo=timezone.utc)
        for ticker, con_id, qty in [("AAPL", 111, 10.0), ("MSFT", 222, 5.0)]:
            session.add(
                Position(
                    account_id="DUTEST",
                    ticker=ticker,
                    portfolio="momentum",
                    con_id=con_id,
                    exchange="SMART",
                    currency="USD",
                    quantity=qty,
                    avg_entry_price=100,
                    current_price=100,
                    peak_price=100,
                    highest_price_since_entry=100,
                    opened_at=now,
                    status="open",
                )
            )
        session.commit()
        ledger = OrderLedger(session)
        runner = RiskServiceRunner(
            config=mock_config,
            redis_client=mock_redis,
            db_session=session,
            order_ledger=ledger,
        )
        # Stale/empty in-memory book: the kill MUST reload from the DB, not this.
        runner._portfolio.positions = {}
        return runner, ledger, session

    @pytest.mark.asyncio
    async def test_kill_liquidates_authoritative_db_positions(
        self, db_runner, mock_redis
    ):
        runner, ledger, session = db_runner
        await runner.process_kill(_kill_msg())

        orders = _approved_orders(mock_redis)
        sells = {o.ticker: o for o in orders if o.action == "sell"}
        assert set(sells) == {"AAPL", "MSFT"}
        assert sells["AAPL"].quantity == pytest.approx(10.0)
        # Each exit is backed by a deterministic ledger intent so execution can
        # actually place it (not a synthetic id it would reject).
        intent = ledger.get(sells["AAPL"].recommendation_id)
        assert intent.action == "SELL"
        assert intent.con_id == 111
        session.rollback()

    @pytest.mark.asyncio
    async def test_replayed_kill_does_not_double_submit(self, db_runner, mock_redis):
        runner, ledger, session = db_runner
        await runner.process_kill(_kill_msg())
        first_ids = {o.recommendation_id for o in _approved_orders(mock_redis)}

        # Same kill message redelivered (crash-replay): must reuse the same
        # deterministic intents, not mint new ones.
        await runner.process_kill(_kill_msg())
        all_ids = {o.recommendation_id for o in _approved_orders(mock_redis)}
        assert all_ids == first_ids  # no new exit ids

        from shared.models import OrderIntent

        intent_count = session.query(OrderIntent).count()
        assert intent_count == 2  # two positions, not four
        session.rollback()

    @pytest.mark.asyncio
    async def test_kill_exit_intent_is_approved_before_publish(
        self, db_runner, mock_redis
    ):
        """Risk is the approver of its own exits (KAN-4).

        Publishing a PROPOSED intent means execution's record_submission does
        an illegal PROPOSED->SUBMITTED transition *after* the IB order is live.
        """
        runner, ledger, session = db_runner
        await runner.process_kill(_kill_msg())

        for order in _approved_orders(mock_redis):
            intent = ledger.get(order.recommendation_id)
            assert intent.status == OrderStatus.APPROVED.value
            assert intent.approved_at is not None
        session.rollback()

    @pytest.mark.asyncio
    async def test_kill_approves_pre_existing_proposed_intent(
        self, db_runner, mock_redis
    ):
        """A pre-fix leftover (intent created at PROPOSED by an older build)
        must be approved by the adopt branch, not left stranded."""
        runner, ledger, session = db_runner
        kill = _kill_msg()
        exit_id = runner._liquidation_exit_id("AAPL", int(kill.timestamp.timestamp()))
        ledger.create_intent(
            SimpleNamespace(
                recommendation_id=exit_id,
                account_id="DUTEST",
                mode="paper",
                portfolio="momentum",
                con_id=111,
                symbol="AAPL",
                exchange="SMART",
                currency="USD",
                action="SELL",
                quantity=10.0,
                limit_price=None,
                order_type="MKT",
            )
        )
        session.commit()

        await runner.process_kill(kill)

        intent = ledger.get(exit_id)
        assert intent.status == OrderStatus.APPROVED.value
        assert intent.approved_at is not None
        session.rollback()

    @pytest.mark.asyncio
    async def test_kill_leaves_already_submitted_intent_untouched(
        self, db_runner, mock_redis
    ):
        """A replayed kill whose exit is already live at the broker must not
        re-transition it (APPROVED/SUBMITTED are illegal sources for APPROVED)."""
        runner, ledger, session = db_runner
        kill = _kill_msg()
        exit_id = runner._liquidation_exit_id("AAPL", int(kill.timestamp.timestamp()))
        ledger.create_intent(
            SimpleNamespace(
                recommendation_id=exit_id,
                account_id="DUTEST",
                mode="paper",
                portfolio="momentum",
                con_id=111,
                symbol="AAPL",
                exchange="SMART",
                currency="USD",
                action="SELL",
                quantity=10.0,
                limit_price=None,
                order_type="MKT",
            )
        )
        ledger.transition(exit_id, OrderStatus.APPROVED)
        ledger.record_submission(exit_id, "77")
        session.commit()
        submitted_at = ledger.get(exit_id).submitted_at
        session.rollback()

        await runner.process_kill(kill)  # must not raise

        intent = ledger.get(exit_id)
        assert intent.status == OrderStatus.SUBMITTED.value
        assert intent.ib_order_id == "77"
        assert intent.submitted_at == submitted_at
        session.rollback()

    @pytest.mark.asyncio
    async def test_second_kill_while_halted_does_not_reliquidate(
        self, db_runner, mock_redis
    ):
        """Once halted, a second (distinct-epoch) kill for the same incident must
        latch, not re-liquidate a position that is still being flattened."""
        runner, ledger, session = db_runner
        await runner.process_kill(_kill_msg(reason="first"))
        first = len(_approved_orders(mock_redis))

        later = KillMessage(
            timestamp=datetime(2026, 8, 7, 13, 0, 0, tzinfo=timezone.utc),
            triggered_by="operator***",
            reason="second while still halted",
        )
        await runner.process_kill(later)

        assert len(_approved_orders(mock_redis)) == first  # no re-liquidation
        session.rollback()

    @pytest.mark.asyncio
    async def test_position_without_con_id_is_flagged_not_silently_dropped(
        self, mock_config, mock_redis
    ):
        """A position with no con_id cannot be routed to a ledger intent, so
        execution would reject it. It must raise a critical alert (manual
        action), not be published + reported as liquidated."""
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = Session(engine)
        session.add(
            Position(
                account_id="DUTEST",
                ticker="NOCID",
                portfolio="momentum",
                con_id=None,
                exchange="SMART",
                currency="USD",
                quantity=5.0,
                avg_entry_price=100,
                current_price=100,
                peak_price=100,
                highest_price_since_entry=100,
                opened_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
                status="open",
            )
        )
        session.commit()
        runner = RiskServiceRunner(
            config=mock_config,
            redis_client=mock_redis,
            db_session=session,
            order_ledger=OrderLedger(session),
        )

        await runner.process_kill(_kill_msg())

        # No doomed exit published for the un-routable position ...
        assert all(o.ticker != "NOCID" for o in _approved_orders(mock_redis))
        # ... but the operator is warned to act.
        alerts = [
            str(c.args[1])
            for c in mock_redis.publish.call_args_list
            if c.args[0] == "stream:alerts"
        ]
        assert any(
            "NOCID" in a and ("manual" in a.lower() or "unroutable" in a.lower())
            for a in alerts
        )
        session.rollback()

    @pytest.mark.asyncio
    async def test_kill_survives_position_load_failure_with_critical_alert(
        self, db_runner, mock_redis
    ):
        """A transient failure enumerating positions must not drop the kill — it
        must still halt and raise a critical alert (not vanish into the DLQ)."""
        runner, ledger, session = db_runner
        runner._authoritative_open_positions = MagicMock(
            side_effect=RuntimeError("DB unavailable")
        )

        await runner.process_kill(_kill_msg())  # must not raise

        assert runner._kill_switch.is_active is True
        alerts = [
            str(c.args[1])
            for c in mock_redis.publish.call_args_list
            if c.args[0] == "stream:alerts"
        ]
        assert any("critical" in a.lower() or "manual" in a.lower() for a in alerts)
        session.rollback()

    @pytest.mark.asyncio
    async def test_kill_always_alerts_even_with_no_positions(
        self, runner, mock_redis
    ):
        runner._portfolio.positions = {}
        await runner.process_kill(_kill_msg())

        alerts = [
            c.args[1]
            for c in mock_redis.publish.call_args_list
            if c.args[0] == "stream:alerts"
        ]
        assert any("kill" in str(a).lower() for a in alerts)

    @pytest.mark.asyncio
    async def test_kill_per_position_failure_does_not_abort_rest(
        self, db_runner, mock_redis
    ):
        runner, ledger, session = db_runner
        original = runner._emit_liquidation_exit

        async def flaky(pos, **kwargs):
            if pos["ticker"] == "AAPL":
                raise RuntimeError("IB disconnected for AAPL")
            return await original(pos, **kwargs)

        runner._emit_liquidation_exit = flaky
        await runner.process_kill(_kill_msg())

        orders = _approved_orders(mock_redis)
        assert any(o.ticker == "MSFT" for o in orders)  # MSFT still liquidated
        alerts = [
            c.args[1]
            for c in mock_redis.publish.call_args_list
            if c.args[0] == "stream:alerts"
        ]
        assert any("kill" in str(a).lower() for a in alerts)  # alert still fired
        session.rollback()


class TestCircuitBreakerLiquidation:
    """The 20% circuit breaker must liquidate, not merely pause buys (1.2)."""

    @pytest.fixture
    def db_runner(self, mock_config, mock_redis):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = Session(engine)
        now = datetime(2026, 8, 1, tzinfo=timezone.utc)
        session.add(
            Position(
                account_id="DUTEST",
                ticker="AAPL",
                portfolio="momentum",
                con_id=111,
                exchange="SMART",
                currency="USD",
                quantity=10.0,
                avg_entry_price=100,
                current_price=100,
                peak_price=100,
                highest_price_since_entry=100,
                opened_at=now,
                status="open",
            )
        )
        session.commit()
        runner = RiskServiceRunner(
            config=mock_config,
            redis_client=mock_redis,
            db_session=session,
            order_ledger=OrderLedger(session),
        )
        return runner, session

    @pytest.mark.asyncio
    async def test_breaker_liquidates_and_halts(self, db_runner, mock_redis):
        from shared.halt_state import HaltStateRepository

        runner, session = db_runner
        runner._portfolio.book_equity = 70_000  # 30% drawdown
        runner._portfolio.book_peak_equity = 100_000

        await runner._emit_drawdown_gauge()

        assert runner._kill_switch.is_active is True
        orders = _approved_orders(mock_redis)
        assert any(o.ticker == "AAPL" and o.action == "sell" for o in orders)
        halt = HaltStateRepository(session).load_active_halt(mode="paper")
        assert halt is not None and halt.source == "circuit_breaker"
        session.rollback()

    @pytest.mark.asyncio
    async def test_breaker_does_not_reliquidate_when_already_halted(
        self, db_runner, mock_redis
    ):
        runner, session = db_runner
        runner._portfolio.book_equity = 70_000
        runner._portfolio.book_peak_equity = 100_000

        await runner._emit_drawdown_gauge()
        first = len(_approved_orders(mock_redis))
        await runner._emit_drawdown_gauge()  # still drawn down, already halted
        assert len(_approved_orders(mock_redis)) == first  # no re-liquidation
        session.rollback()

    @pytest.mark.asyncio
    async def test_breaker_publishes_kill_so_execution_cancels_orders(
        self, db_runner, mock_redis
    ):
        """The breaker must also reach execution's kill path (which cancels
        resting orders) — a 'full liquidation' that leaves working buys is not
        one. It does so by publishing a KillMessage to stream:kill."""
        runner, session = db_runner
        runner._portfolio.book_equity = 70_000
        runner._portfolio.book_peak_equity = 100_000

        await runner._emit_drawdown_gauge()

        kills = [
            c for c in mock_redis.publish.call_args_list
            if c.args[0] == "stream:kill"
        ]
        assert len(kills) == 1
        session.rollback()

    @pytest.mark.asyncio
    async def test_pause_only_alerts_no_liquidation(self, db_runner, mock_redis):
        runner, session = db_runner
        runner._portfolio.book_equity = 88_000  # 12% -> pause, not breaker
        runner._portfolio.book_peak_equity = 100_000

        await runner._emit_drawdown_gauge()

        assert runner._kill_switch.is_active is False
        assert _approved_orders(mock_redis) == []
        session.rollback()


class TestKillFailsClosedOnRestart:
    """A restart after a kill must stay halted (review 1.1)."""

    def test_runner_reloads_persisted_halt_on_construction(
        self, mock_config, mock_redis
    ):
        from shared.halt_state import HaltStateRepository

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = Session(engine)
        # A prior process persisted an active halt for this mode.
        HaltStateRepository(session).record_halt(
            mode="paper",
            source="kill",
            reason="prior emergency",
            triggered_by="admin***",
            now=datetime.now(timezone.utc),
        )
        session.commit()

        # A fresh runner (simulating a restart) must come up already halted.
        runner = RiskServiceRunner(
            config=mock_config, redis_client=mock_redis, db_session=session
        )

        assert runner._kill_switch.is_active is True
        assert runner._kill_switch.check().approved is False
        session.close()


class TestDrawdownCheck:
    @pytest.mark.asyncio
    async def test_drawdown_rejects_new_buy(self, runner, mock_redis):
        """Portfolio drawdown above pause threshold rejects new buys."""
        runner._portfolio = make_portfolio(nav=85_000, peak_nav=100_000)
        rec = make_recommendation(ticker="AAPL", action="buy")

        with patch.object(
            runner._kill_switch,
            "check",
            return_value=RiskDecision(approved=True, reason="inactive"),
        ):
            await runner.process_recommendation(rec)

        published_streams = [c[0][0] for c in mock_redis.publish.call_args_list]
        assert "stream:approved_orders" not in published_streams


class TestPassiveMonitoring:
    @pytest.mark.asyncio
    async def test_passive_scan_publishes_alerts(self, runner, mock_redis):
        """Passive monitoring should publish alerts for breaches."""
        runner._portfolio = make_portfolio(
            nav=100_000,
            positions={
                "AAPL": {
                    "quantity": 200,
                    "sector": "Technology",
                },  # 20% of NAV -> hard trim
            },
        )
        runner._current_prices = {"AAPL": 100.0}

        await runner.run_passive_scan()

        published_calls = mock_redis.publish.call_args_list
        alert_calls = [c for c in published_calls if c[0][0] == "stream:alerts"]
        assert len(alert_calls) >= 1


def _approved_orders(mock_redis):
    """Reconstruct every ApprovedOrderMessage published to approved_orders."""
    from shared.schemas.messages import ApprovedOrderMessage

    return [
        ApprovedOrderMessage.from_stream_dict(c.args[1])
        for c in mock_redis.publish.call_args_list
        if c.args[0] == "stream:approved_orders"
    ]


class TestPeriodicRiskEnforcement:
    """T2: the periodic driver must actually fire stop-loss, hard-ceiling
    auto-trim, and a real-equity drawdown gauge on the live path — the
    mechanisms the review found were written but never invoked."""

    @pytest.mark.asyncio
    async def test_hard_ceiling_breach_auto_trims_to_soft(self, runner, mock_redis):
        """A position over the hard ceiling must emit a sell that reduces it to
        the soft ceiling — not merely an advisory alert."""
        runner._portfolio = make_portfolio(
            nav=100_000,
            positions={"AAPL": {"quantity": 200, "sector": "Technology"}},
        )
        runner._current_prices = {"AAPL": 100.0}  # 200*100 = 20% of NAV (>15% hard)

        await runner.run_passive_scan()

        orders = _approved_orders(mock_redis)
        trims = [o for o in orders if o.ticker == "AAPL" and o.action == "sell"]
        assert len(trims) == 1
        # Reduce 20% -> 7% soft ceiling: sell (20000 - 7000)/100 = 130 shares.
        assert trims[0].quantity == pytest.approx(130.0)

    @pytest.mark.asyncio
    async def test_soft_ceiling_breach_does_not_trim(self, runner, mock_redis):
        """A soft-ceiling breach is advisory only — no sell order is emitted."""
        runner._portfolio = make_portfolio(
            nav=100_000,
            positions={"AAPL": {"quantity": 100, "sector": "Technology"}},
        )
        runner._current_prices = {"AAPL": 100.0}  # 10% of NAV (soft<x<hard)

        await runner.run_passive_scan()

        assert _approved_orders(mock_redis) == []

    @pytest.mark.asyncio
    async def test_periodic_checks_drive_stop_loss(self, runner, mock_redis):
        """run_periodic_risk_checks must fire an intraday trailing stop without
        waiting for the daily EOD sleeve exit."""
        runner._portfolio = make_portfolio(
            nav=100_000,
            positions={
                "AAPL": {
                    "quantity": 10,
                    "sector": "Technology",
                    "highest_price_since_entry": 100.0,
                }
            },
        )
        runner._current_prices = {"AAPL": 84.0}  # 16% below high (>15% stop)

        await runner.run_periodic_risk_checks()

        orders = _approved_orders(mock_redis)
        sells = [o for o in orders if o.ticker == "AAPL" and o.action == "sell"]
        assert len(sells) == 1
        assert sells[0].quantity == pytest.approx(10.0)

    @pytest.mark.asyncio
    async def test_periodic_checks_drive_passive_trim(self, runner, mock_redis):
        """run_periodic_risk_checks must also run the passive scan / auto-trim."""
        runner._portfolio = make_portfolio(
            nav=100_000,
            positions={"AAPL": {"quantity": 200, "sector": "Technology"}},
        )
        runner._current_prices = {"AAPL": 100.0}

        await runner.run_periodic_risk_checks()

        orders = _approved_orders(mock_redis)
        assert any(o.ticker == "AAPL" and o.action == "sell" for o in orders)

    @pytest.mark.asyncio
    async def test_periodic_drawdown_gauge_uses_book_equity(self, runner, mock_redis):
        """A real book-equity drawdown must raise a drawdown alert even when nav
        (the capped deployment budget) shows none."""
        pf = make_portfolio(nav=100_000, peak_nav=100_000)
        pf.book_equity = 80_000  # 20% real drawdown
        pf.book_peak_equity = 100_000
        runner._portfolio = pf
        runner._current_prices = {}

        await runner.run_periodic_risk_checks()

        alerts = [
            c.args[1]
            for c in mock_redis.publish.call_args_list
            if c.args[0] == "stream:alerts"
        ]
        assert any(
            "drawdown" in str(a).lower() or "circuit breaker" in str(a).lower()
            for a in alerts
        )

    @pytest.mark.asyncio
    async def test_maybe_run_respects_interval(self, runner):
        """The driver runs once, then not again until the scan interval elapses."""
        runner.run_periodic_risk_checks = AsyncMock()
        interval = runner._config.risk.passive_scan_interval_minutes * 60

        await runner.maybe_run_periodic_checks(now=1000.0)  # first call -> runs
        await runner.maybe_run_periodic_checks(now=1000.0 + interval - 1)  # too soon
        await runner.maybe_run_periodic_checks(now=1000.0 + interval + 1)  # elapsed

        assert runner.run_periodic_risk_checks.await_count == 2

    @pytest.mark.asyncio
    async def test_periodic_failure_does_not_propagate(self, runner):
        """A raising periodic sweep must be swallowed — it must never propagate
        and tear down the main run() loop (which would stop processing
        recommendations, kills, and fills for the whole service)."""
        runner.run_periodic_risk_checks = AsyncMock(side_effect=RuntimeError("boom"))

        ran = await runner.maybe_run_periodic_checks(now=1000.0)

        assert ran is True  # timer advanced, exception swallowed, loop survives


class TestPeriodicRefreshDenominator:
    """The periodic scan's nav (its ceiling/sizing denominator) must stay on the
    deployment budget, consistent with the entry path — only the drawdown gauge
    moves to real book equity."""

    def test_refresh_keeps_nav_on_deployable_capital(self, mock_config, mock_redis):
        from datetime import date as date_cls

        from shared.models.equity_snapshot import EquitySnapshot
        from shared.models.portfolio_config import PortfolioConfig

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = Session(engine)
        now = datetime.now(timezone.utc)
        session.add(
            CapitalSnapshot(
                account_id="DUTEST",
                mode="paper",
                net_liquidation=1_000_000,
                deployment_fraction=1,
                max_deployable_usd=100_000,
                deployable_capital=100_000,  # budget pinned at the cap
                settled_cash_trading=40_000,
                sleeve_budgets={},
                reconciliation_status="ok",
                captured_at=now,
            )
        )
        session.add(
            PortfolioConfig(
                portfolio="momentum",
                capital=40_000,
                cash=40_000,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            Position(
                account_id="DUTEST",
                ticker="AAPL",
                portfolio="momentum",
                con_id=1,
                quantity=50,
                avg_entry_price=100,
                current_price=100,  # MTM 5,000
                peak_price=100,
                highest_price_since_entry=100,
                opened_at=now,
                status="open",
            )
        )
        session.add(
            EquitySnapshot(
                portfolio="momentum",
                date=date_cls(2026, 7, 1),
                equity=48_000,
                cash=43_000,
                market_value=5_000,
                created_at=now,
            )
        )
        session.commit()

        runner = RiskServiceRunner(
            config=mock_config, redis_client=mock_redis, db_session=session
        )
        runner._refresh_portfolio_from_db()

        # nav (ceiling / sizing basis) stays on the capped deployment budget.
        assert runner._portfolio.nav == pytest.approx(100_000)
        # drawdown gauge tracks real book equity (40k cash + 5k MTM) and its peak.
        assert runner._portfolio.book_equity == pytest.approx(45_000)
        assert runner._portfolio.book_peak_equity == pytest.approx(48_000)
        session.close()


class TestAuditLogging:
    @pytest.mark.asyncio
    async def test_decisions_are_logged(self, runner, mock_redis, mock_portfolio):
        """All risk decisions should be logged to audit."""
        runner._portfolio = mock_portfolio
        mock_logger = MagicMock()
        runner._logger = mock_logger
        rec = make_recommendation(ticker="AAPL", action="buy")

        with (
            patch.object(
                runner._engine,
                "check_entry",
                return_value=RiskDecision(
                    approved=True, reason="ok", adjusted_quantity=50
                ),
            ),
            patch.object(
                runner._kill_switch,
                "check",
                return_value=RiskDecision(approved=True, reason="inactive"),
            ),
            patch.object(
                runner._engine,
                "check_portfolio_drawdown",
                return_value=RiskDecision(approved=True, reason="ok"),
            ),
        ):
            await runner.process_recommendation(rec)

        # Logger should have been called (at least info level)
        assert mock_logger.info.call_count >= 1


class TestSteadyStatePoisonHandling:
    """A non-retryable error in the steady-state loop must DLQ + ack + alert —
    not silently leave the message parked in the PEL (review 3.3)."""

    def _msg(self, mid="1-0"):
        return SimpleNamespace(message_id=mid, data={"bad": "payload"})

    @pytest.mark.asyncio
    async def test_poison_message_is_dead_lettered_acked_and_alerted(
        self, runner, mock_redis
    ):
        mock_redis.read_group = AsyncMock(return_value=[self._msg("7-0")])
        mock_redis.send_to_dead_letter = AsyncMock()

        async def boom(_):
            raise ValueError("unparseable")

        await runner._consume_and_process(
            "stream:fills", lambda d: d, boom, count=1, block_ms=0
        )

        mock_redis.send_to_dead_letter.assert_awaited_once()
        mock_redis.ack.assert_awaited_once()
        alerts = [
            c for c in mock_redis.publish.call_args_list
            if c.args[0] == "stream:alerts"
        ]
        assert len(alerts) >= 1

    @pytest.mark.asyncio
    async def test_retryable_error_leaves_message_pending(self, runner, mock_redis):
        from services.risk_management.runner import RetryableRiskStateError

        mock_redis.read_group = AsyncMock(return_value=[self._msg("8-0")])
        mock_redis.send_to_dead_letter = AsyncMock()

        async def retry(_):
            raise RetryableRiskStateError("state not ready")

        await runner._consume_and_process(
            "stream:recommendations",
            lambda d: d,
            retry,
            count=1,
            block_ms=0,
            retryable_exc=(RetryableRiskStateError,),
        )

        mock_redis.ack.assert_not_awaited()
        mock_redis.send_to_dead_letter.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ack_failure_after_success_does_not_dead_letter(
        self, runner, mock_redis
    ):
        """A transient ack failure AFTER the handler succeeded must not label a
        processed message poison — redelivery + idempotency handles it."""
        mock_redis.read_group = AsyncMock(return_value=[self._msg("5-0")])
        mock_redis.send_to_dead_letter = AsyncMock()
        mock_redis.ack = AsyncMock(side_effect=ConnectionError("redis blip"))

        async def ok(_):
            return None

        await runner._consume_and_process(
            "stream:fills", lambda d: d, ok, count=1, block_ms=0
        )

        mock_redis.send_to_dead_letter.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_successful_message_is_acked(self, runner, mock_redis):
        mock_redis.read_group = AsyncMock(return_value=[self._msg("9-0")])
        mock_redis.send_to_dead_letter = AsyncMock()
        handled = []

        async def ok(d):
            handled.append(d)

        await runner._consume_and_process(
            "stream:fills", lambda d: d, ok, count=1, block_ms=0
        )

        assert handled == [{"bad": "payload"}]
        mock_redis.ack.assert_awaited_once()
        mock_redis.send_to_dead_letter.assert_not_awaited()


class TestDlqDepthMonitor:
    """A non-empty :dlq must raise an alert so parked poison never goes
    unnoticed (review 3.5)."""

    @pytest.mark.asyncio
    async def test_alerts_when_dlq_has_backlog(self, runner, mock_redis):
        mock_redis.stream_length = AsyncMock(return_value=2)

        await runner._check_dlq_depths()

        alerts = [
            c.args[1]
            for c in mock_redis.publish.call_args_list
            if c.args[0] == "stream:alerts"
        ]
        assert any(
            "dlq" in str(a).lower() or "dead-letter" in str(a).lower()
            for a in alerts
        )

    @pytest.mark.asyncio
    async def test_no_alert_when_dlq_empty(self, runner, mock_redis):
        mock_redis.stream_length = AsyncMock(return_value=0)

        await runner._check_dlq_depths()

        alerts = [
            c for c in mock_redis.publish.call_args_list
            if c.args[0] == "stream:alerts"
        ]
        assert alerts == []


class TestFillProcessing:
    """process_fill keeps the in-memory portfolio synced with executions."""

    def _fill(
        self, ticker="AAPL", side="buy", quantity=10.0, price=100.0, commission=0.5
    ):
        from shared.schemas.messages import FillMessage

        return FillMessage(
            ticker=ticker,
            timestamp=datetime.now(timezone.utc),
            side=side,
            quantity=quantity,
            fill_price=price,
            commission=commission,
            recommendation_id=str(uuid.uuid4()),
            order_id="order-1",
        )

    @pytest.mark.asyncio
    async def test_buy_fill_opens_position_and_debits_cash(self, runner):
        runner._cash = 10_000.0
        await runner.process_fill(self._fill(side="buy", quantity=10, price=100))

        pos = runner._portfolio.positions["AAPL"]
        assert pos["quantity"] == 10
        assert pos["avg_entry_price"] == 100
        assert runner._cash == pytest.approx(10_000 - 1_000 - 0.5)
        assert runner._portfolio.nav == pytest.approx(runner._cash + 1_000)

    @pytest.mark.asyncio
    async def test_sell_fill_closes_position_and_credits_cash(self, runner):
        runner._cash = 9_000.0
        runner._portfolio.positions["AAPL"] = {
            "quantity": 10.0,
            "avg_entry_price": 100.0,
            "current_price": 100.0,
            "highest_price_since_entry": 100.0,
            "sector": "Tech",
        }
        await runner.process_fill(self._fill(side="sell", quantity=10, price=110))

        assert "AAPL" not in runner._portfolio.positions
        assert runner._cash == pytest.approx(9_000 + 1_100 - 0.5)

    @pytest.mark.asyncio
    async def test_peak_nav_ratchets_up(self, runner):
        runner._cash = 10_000.0
        runner._portfolio.peak_nav = 5_000.0
        await runner.process_fill(self._fill(side="buy", quantity=1, price=100))
        assert runner._portfolio.peak_nav >= 10_000 - 0.5

    @pytest.mark.asyncio
    async def test_buy_fill_averages_into_existing_position(self, runner):
        runner._cash = 20_000.0
        await runner.process_fill(self._fill(side="buy", quantity=10, price=100))
        await runner.process_fill(self._fill(side="buy", quantity=10, price=120))
        pos = runner._portfolio.positions["AAPL"]
        assert pos["quantity"] == 20
        assert pos["avg_entry_price"] == pytest.approx(110.0)

    def _fill_with(self, **kw):
        from shared.schemas.messages import FillMessage

        base = dict(
            ticker="AAPL",
            timestamp=datetime.now(timezone.utc),
            side="buy",
            quantity=10.0,
            fill_price=100.0,
            commission=0.5,
            recommendation_id=str(uuid.uuid4()),
            order_id="order-1",
        )
        base.update(kw)
        return FillMessage(**base)

    @pytest.mark.asyncio
    async def test_replayed_fill_does_not_move_book(self, runner):
        """At-least-once delivery: the same execution replayed must not
        double-count NAV/cash/positions (review 3.1)."""
        runner._cash = 10_000.0
        fill = self._fill_with(execution_id="exec-1", quantity=10, price=100)

        await runner.process_fill(fill)
        cash_after_first = runner._cash
        qty_after_first = runner._portfolio.positions["AAPL"]["quantity"]

        await runner.process_fill(fill)  # replay

        assert runner._cash == cash_after_first
        assert runner._portfolio.positions["AAPL"]["quantity"] == qty_after_first

    @pytest.mark.asyncio
    async def test_fill_without_execution_id_still_processes(self, runner):
        runner._cash = 10_000.0
        await runner.process_fill(self._fill_with(execution_id=None, quantity=5))
        assert runner._portfolio.positions["AAPL"]["quantity"] == 5

    @pytest.mark.asyncio
    async def test_process_fill_uses_commission_trading_usd(self, runner):
        """Cash is USD; a native (e.g. SGD) commission must be applied via the
        USD-converted commission_trading, not the raw fill.commission (3.2)."""
        runner._cash = 10_000.0
        fill = self._fill_with(
            execution_id="exec-2",
            quantity=10,
            price=100,
            commission=13.5,  # native (e.g. SGD)
            commission_trading=10.0,  # USD-converted
            commission_currency="SGD",
        )
        await runner.process_fill(fill)
        # 10_000 - 1_000 notional - 10.0 USD commission (NOT 13.5)
        assert runner._cash == pytest.approx(10_000 - 1_000 - 10.0)

    @pytest.mark.asyncio
    async def test_non_usd_commission_without_conversion_is_excluded(self, runner):
        """If a native (SGD) commission has no USD conversion, it must NOT be
        subtracted as USD units — exclude it rather than corrupt USD cash."""
        runner._cash = 10_000.0
        fill = self._fill_with(
            execution_id="exec-3",
            quantity=10,
            price=100,
            commission=13.5,  # SGD, no conversion provided
            commission_trading=None,
            commission_currency="SGD",
        )
        await runner.process_fill(fill)
        assert runner._cash == pytest.approx(10_000 - 1_000)  # no commission applied


class TestSectorResolution:
    """Sector must resolve from shared.universe, not degrade to 'Unknown'.

    Positions written by the fill projector between 2026-07-19 and 2026-08-07
    have NULL sectors; new tickers are never in the positions dict at all.
    Both used to collapse into one 'Unknown' pseudo-sector whose exposure
    tripped the concentration limit and froze all new entries.
    """

    def test_get_sector_for_unheld_ticker_uses_universe_map(self, runner):
        runner._portfolio = make_portfolio()
        assert runner._get_sector("AAPL") == "Technology"

    def test_get_sector_prefers_position_sector(self, runner):
        runner._portfolio = make_portfolio(
            positions={"AAPL": {"quantity": 1.0, "sector": "Tech"}}
        )
        assert runner._get_sector("AAPL") == "Tech"

    def test_get_sector_falls_back_when_position_sector_unknown(self, runner):
        runner._portfolio = make_portfolio(
            positions={"AAPL": {"quantity": 1.0, "sector": "Unknown"}}
        )
        assert runner._get_sector("AAPL") == "Technology"

    @pytest.mark.asyncio
    async def test_buy_fill_records_real_sector(self, runner):
        runner._cash = 10_000.0
        fill = TestFillProcessing()._fill(side="buy", quantity=10, price=100)
        await runner.process_fill(fill)
        assert runner._portfolio.positions["AAPL"]["sector"] == "Technology"

class TestSleeveBridgeRecommendations:
    """Sleeve recommendations (run_paper --publish) carry price + sizing."""

    def _sleeve_rec(
        self,
        action="buy",
        limit_price=184.76,
        quantity=8.3243,
        ticker="PM",
        portfolio="quality_value",
    ):
        return RecommendationMessage(
            ticker=ticker,
            timestamp=datetime.now(timezone.utc),
            action=action,
            confidence=1.0,
            top_features={},
            recommendation_id=f"sleeve-2026-07-10-{portfolio}-{ticker}-{action}",
            limit_price=limit_price,
            quantity=quantity,
            portfolio=portfolio,
        )

    @pytest.mark.asyncio
    async def test_sleeve_buy_without_intent_fails_closed(self, runner, mock_redis):
        runner._portfolio = make_portfolio(nav=100_000)
        await runner.process_recommendation(self._sleeve_rec())

        published = [
            c
            for c in mock_redis.publish.call_args_list
            if c.args[0] == "stream:approved_orders"
        ]
        assert published == []

    @pytest.mark.asyncio
    async def test_buy_without_any_price_is_rejected(self, runner, mock_redis):
        """No sleeve price and no market price -> reject, never divide by zero."""
        runner._portfolio = make_portfolio(nav=100_000)
        rec = self._sleeve_rec(limit_price=None, quantity=None)
        await runner.process_recommendation(rec)

        streams = [c.args[0] for c in mock_redis.publish.call_args_list]
        assert "stream:approved_orders" not in streams
        assert "stream:alerts" in streams  # rejection alert


class TestDurableRiskLifecycle:
    @pytest.fixture
    def sleeve_sell_case(self, mock_config, mock_redis):
        sessions: list[Session] = []

        def build(
            *,
            momentum_quantity: float,
            quality_quantity: float,
            requested_quantity: float,
            momentum_pending: float = 0,
        ):
            engine = create_engine("sqlite:///:memory:")
            Base.metadata.create_all(engine)
            session = Session(engine)
            sessions.append(session)
            ledger = OrderLedger(session)
            now = datetime.now(timezone.utc)
            ledger.create_intent(
                SimpleNamespace(
                    recommendation_id="sell-current",
                    account_id="DUTEST",
                    mode="paper",
                    portfolio="momentum",
                    con_id=265598,
                    symbol="AAPL",
                    exchange="SMART",
                    currency="USD",
                    action="SELL",
                    quantity=requested_quantity,
                    limit_price=None,
                    order_type="MKT",
                )
            )
            if momentum_pending:
                ledger.create_intent(
                    SimpleNamespace(
                        recommendation_id="sell-momentum-pending",
                        account_id="DUTEST",
                        mode="paper",
                        portfolio="momentum",
                        con_id=265598,
                        symbol="AAPL",
                        exchange="SMART",
                        currency="USD",
                        action="SELL",
                        quantity=momentum_pending,
                        limit_price=None,
                        order_type="MKT",
                    )
                )
                ledger.transition("sell-momentum-pending", OrderStatus.APPROVED)
            session.add(
                CapitalSnapshot(
                    account_id="DUTEST",
                    mode="paper",
                    net_liquidation=100_000,
                    deployment_fraction=1,
                    max_deployable_usd=None,
                    deployable_capital=100_000,
                    settled_cash_trading=100_000,
                    sleeve_budgets={},
                    reconciliation_status="ok",
                    captured_at=now,
                )
            )
            for portfolio, quantity in (
                ("momentum", momentum_quantity),
                ("quality_value", quality_quantity),
            ):
                if quantity <= 0:
                    continue
                session.add(
                    Position(
                        account_id="DUTEST",
                        ticker="AAPL",
                        portfolio=portfolio,
                        con_id=265598,
                        quantity=quantity,
                        avg_entry_price=100,
                        current_price=100,
                        peak_price=100,
                        highest_price_since_entry=100,
                        opened_at=now,
                        status="open",
                    )
                )
            session.commit()
            runner = RiskServiceRunner(
                config=mock_config,
                redis_client=mock_redis,
                db_session=session,
                order_ledger=ledger,
            )
            rec = RecommendationMessage(
                ticker="AAPL",
                timestamp=now,
                action="sell",
                confidence=1,
                top_features={},
                recommendation_id="sell-current",
                limit_price=None,
                quantity=requested_quantity,
                portfolio="momentum",
            )
            return runner, ledger, rec

        yield build
        for session in sessions:
            session.close()

    @pytest.mark.asyncio
    async def test_sell_cannot_use_same_ticker_owned_by_another_sleeve(
        self, sleeve_sell_case, mock_redis
    ):
        runner, ledger, rec = sleeve_sell_case(
            momentum_quantity=0,
            quality_quantity=5,
            requested_quantity=5,
        )

        await runner.process_recommendation(rec)

        assert ledger.get("sell-current").status == OrderStatus.RISK_REJECTED.value
        assert not any(
            call.args[0] == "stream:approved_orders"
            for call in mock_redis.publish.call_args_list
        )

    @pytest.mark.asyncio
    async def test_sell_deducts_same_sleeve_pending_quantity(
        self, sleeve_sell_case, mock_redis
    ):
        runner, ledger, rec = sleeve_sell_case(
            momentum_quantity=5,
            quality_quantity=5,
            requested_quantity=4,
            momentum_pending=2,
        )

        await runner.process_recommendation(rec)

        assert ledger.get("sell-current").status == OrderStatus.RISK_REJECTED.value
        assert not any(
            call.args[0] == "stream:approved_orders"
            for call in mock_redis.publish.call_args_list
        )

    @pytest.mark.asyncio
    async def test_exact_sleeve_owned_exit_is_approved(
        self, sleeve_sell_case, mock_redis
    ):
        runner, ledger, rec = sleeve_sell_case(
            momentum_quantity=5,
            quality_quantity=5,
            requested_quantity=5,
        )

        await runner.process_recommendation(rec)

        assert ledger.get("sell-current").status == OrderStatus.APPROVED.value
        assert any(
            call.args[0] == "stream:approved_orders"
            for call in mock_redis.publish.call_args_list
        )

    @pytest.mark.asyncio
    async def test_refresh_risk_state_resolves_null_position_sectors(
        self, sleeve_sell_case
    ):
        """The fixture's Position rows carry sector=NULL (as the fill
        projector wrote them); the rebuilt portfolio must resolve them via
        shared.universe instead of one 'Unknown' bucket."""
        runner, ledger, _rec = sleeve_sell_case(
            momentum_quantity=10,
            quality_quantity=0,
            requested_quantity=5,
        )
        intent = ledger.session.scalar(
            select(OrderIntent).where(
                OrderIntent.recommendation_id == "sell-current"
            )
        )
        assert runner._refresh_risk_state(intent, "sell") is None
        assert runner._portfolio.positions["AAPL"]["sector"] == "Technology"
        assert "Unknown" not in runner._portfolio.sector_exposure

    @pytest.mark.asyncio
    async def test_corrupted_redis_price_mismatch_fails_closed(
        self, durable_runner, mock_redis
    ):
        runner, ledger, _ = durable_runner
        rec = RecommendationMessage(
            ticker="AAPL",
            timestamp=datetime.now(timezone.utc),
            action="buy",
            confidence=1,
            top_features={},
            recommendation_id="rec-risk",
            limit_price=101,
            quantity=10,
            portfolio="momentum",
        )

        await runner.process_recommendation(rec)

        assert ledger.get("rec-risk").status == OrderStatus.PROPOSED.value
        assert not any(
            call.args[0] == "stream:approved_orders"
            for call in mock_redis.publish.call_args_list
        )

    @pytest.mark.asyncio
    async def test_corrupted_redis_quantity_mismatch_fails_closed(
        self, durable_runner, mock_redis
    ):
        runner, ledger, _ = durable_runner
        rec = RecommendationMessage(
            ticker="AAPL",
            timestamp=datetime.now(timezone.utc),
            action="buy",
            confidence=1,
            top_features={},
            recommendation_id="rec-risk",
            limit_price=100,
            quantity=9,
            portfolio="momentum",
        )

        await runner.process_recommendation(rec)

        assert ledger.get("rec-risk").status == OrderStatus.PROPOSED.value
        assert not any(
            call.args[0] == "stream:approved_orders"
            for call in mock_redis.publish.call_args_list
        )

    @pytest.mark.asyncio
    async def test_stale_snapshot_does_not_terminally_reject_valid_exit(
        self, durable_runner, mock_redis
    ):
        runner, ledger, session = durable_runner
        snapshot = session.scalar(select(CapitalSnapshot))
        snapshot.captured_at = datetime.now(timezone.utc) - timedelta(days=3)
        now = datetime.now(timezone.utc)
        session.add(
            Position(
                account_id="DUTEST",
                ticker="AAPL",
                portfolio="momentum",
                con_id=265598,
                quantity=5,
                avg_entry_price=100,
                current_price=100,
                peak_price=100,
                highest_price_since_entry=100,
                opened_at=now,
                status="open",
            )
        )
        ledger.create_intent(
            SimpleNamespace(
                recommendation_id="sell-stale",
                account_id="DUTEST",
                mode="paper",
                portfolio="momentum",
                con_id=265598,
                symbol="AAPL",
                exchange="SMART",
                currency="USD",
                action="SELL",
                quantity=5,
                limit_price=None,
                order_type="MKT",
            )
        )
        session.commit()
        rec = RecommendationMessage(
            ticker="AAPL",
            timestamp=now,
            action="sell",
            confidence=1,
            top_features={},
            recommendation_id="sell-stale",
            limit_price=None,
            quantity=5,
            portfolio="momentum",
        )

        await runner.process_recommendation(rec)

        assert ledger.get("sell-stale").status == OrderStatus.APPROVED.value
        assert any(
            call.args[0] == "stream:approved_orders"
            for call in mock_redis.publish.call_args_list
        )

    @pytest.mark.asyncio
    async def test_unavailable_sell_validation_remains_retryable(
        self, mock_config, mock_redis
    ):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = Session(engine)
        ledger = OrderLedger(session)
        ledger.create_intent(
            SimpleNamespace(
                recommendation_id="sell-retry",
                account_id="DUTEST",
                mode="paper",
                portfolio="momentum",
                con_id=265598,
                symbol="AAPL",
                exchange="SMART",
                currency="USD",
                action="SELL",
                quantity=5,
                limit_price=None,
                order_type="MKT",
            )
        )
        session.commit()
        runner = RiskServiceRunner(
            config=mock_config,
            redis_client=mock_redis,
            db_session=session,
            order_ledger=ledger,
        )
        rec = RecommendationMessage(
            ticker="AAPL",
            timestamp=datetime.now(timezone.utc),
            action="sell",
            confidence=1,
            top_features={},
            recommendation_id="sell-retry",
            limit_price=None,
            quantity=5,
            portfolio="momentum",
        )

        with pytest.raises(RuntimeError, match="capital snapshot absent"):
            await runner.process_recommendation(rec)

        assert ledger.get("sell-retry").status == OrderStatus.PROPOSED.value
        session.close()

    @pytest.mark.asyncio
    async def test_pending_sell_with_unavailable_state_is_left_for_retry(
        self, mock_config, mock_redis
    ):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = Session(engine)
        ledger = OrderLedger(session)
        ledger.create_intent(
            SimpleNamespace(
                recommendation_id="sell-pending",
                account_id="DUTEST",
                mode="paper",
                portfolio="momentum",
                con_id=265598,
                symbol="AAPL",
                exchange="SMART",
                currency="USD",
                action="SELL",
                quantity=5,
                limit_price=None,
                order_type="MKT",
            )
        )
        session.commit()
        runner = RiskServiceRunner(
            config=mock_config,
            redis_client=mock_redis,
            db_session=session,
            order_ledger=ledger,
        )
        rec = RecommendationMessage(
            ticker="AAPL",
            timestamp=datetime.now(timezone.utc),
            action="sell",
            confidence=1,
            top_features={},
            recommendation_id="sell-pending",
            limit_price=None,
            quantity=5,
            portfolio="momentum",
        )
        pending = SimpleNamespace(message_id="1-0", data=rec.to_stream_dict())
        mock_redis.drain_pending.side_effect = [[pending], [], []]

        await runner.setup()

        mock_redis.ack.assert_not_awaited()
        mock_redis.send_to_dead_letter.assert_not_awaited()
        assert ledger.get("sell-pending").status == OrderStatus.PROPOSED.value
        session.close()

    def test_restart_refreshes_account_mode_lifecycle_state_counts(
        self, mock_config, mock_redis
    ):
        from prometheus_client import CollectorRegistry

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = Session(engine)
        ledger = OrderLedger(session)
        for recommendation_id, account_id in (
            ("one-proposed", "DUONE"),
            ("one-approved", "DUONE"),
            ("two-proposed", "DUTWO"),
        ):
            ledger.create_intent(
                SimpleNamespace(
                    recommendation_id=recommendation_id,
                    account_id=account_id,
                    mode="paper",
                    portfolio="momentum",
                    con_id=1,
                    symbol="AAPL",
                    exchange="SMART",
                    currency="USD",
                    action="BUY",
                    quantity=1,
                    limit_price=100,
                    order_type="LMT",
                )
            )
        ledger.transition("one-approved", OrderStatus.APPROVED)
        session.commit()
        metrics = create_trading_metrics(registry=CollectorRegistry())

        RiskServiceRunner(
            config=mock_config,
            redis_client=mock_redis,
            db_session=session,
            order_ledger=ledger,
            metrics=metrics,
        )

        assert (
            metrics.lifecycle_state.labels(
                account_id="DUONE", mode="paper", status="PROPOSED"
            )._value.get()
            == 1
        )
        assert (
            metrics.lifecycle_state.labels(
                account_id="DUONE", mode="paper", status="APPROVED"
            )._value.get()
            == 1
        )
        assert (
            metrics.lifecycle_state.labels(
                account_id="DUTWO", mode="paper", status="PROPOSED"
            )._value.get()
            == 1
        )
        session.close()

    @pytest.mark.asyncio
    async def test_missing_durable_intent_fails_closed(self, runner, mock_redis):
        runner._portfolio = make_portfolio(nav=100_000)
        await runner.process_recommendation(make_recommendation())

        assert not any(
            call.args[0] == "stream:approved_orders"
            for call in mock_redis.publish.call_args_list
        )

    @pytest.mark.asyncio
    async def test_refresh_is_account_and_mode_scoped_per_recommendation(
        self, mock_config, mock_redis
    ):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = Session(engine)
        now = datetime.now(timezone.utc)
        ledger = OrderLedger(session)
        ledger.create_intent(
            SimpleNamespace(
                recommendation_id="acct-one",
                account_id="DUONE",
                mode="paper",
                portfolio="momentum",
                con_id=1,
                symbol="AAPL",
                exchange="SMART",
                currency="USD",
                action="BUY",
                quantity=1,
                limit_price=100,
                order_type="LMT",
            )
        )
        session.add_all(
            [
                CapitalSnapshot(
                    account_id="DUONE",
                    mode="paper",
                    net_liquidation=100_000,
                    deployment_fraction=1,
                    max_deployable_usd=None,
                    deployable_capital=100_000,
                    settled_cash_trading=100_000,
                    sleeve_budgets={},
                    reconciliation_status="ok",
                    captured_at=now,
                ),
                CapitalSnapshot(
                    account_id="DUTWO",
                    mode="paper",
                    net_liquidation=900_000,
                    deployment_fraction=1,
                    max_deployable_usd=None,
                    deployable_capital=900_000,
                    settled_cash_trading=900_000,
                    sleeve_budgets={},
                    reconciliation_status="ok",
                    captured_at=now,
                ),
                Position(
                    account_id="DUONE",
                    ticker="AAPL",
                    portfolio="momentum",
                    con_id=1,
                    quantity=1,
                    avg_entry_price=100,
                    current_price=100,
                    peak_price=100,
                    highest_price_since_entry=100,
                    opened_at=now,
                    status="open",
                ),
                Position(
                    account_id="DUTWO",
                    ticker="AAPL",
                    portfolio="momentum",
                    con_id=1,
                    quantity=99,
                    avg_entry_price=100,
                    current_price=100,
                    peak_price=100,
                    highest_price_since_entry=100,
                    opened_at=now,
                    status="open",
                ),
            ]
        )
        session.commit()
        runner = RiskServiceRunner(
            config=mock_config,
            redis_client=mock_redis,
            db_session=session,
            order_ledger=ledger,
        )
        rec = RecommendationMessage(
            ticker="AAPL",
            timestamp=now,
            action="buy",
            confidence=1,
            top_features={},
            recommendation_id="acct-one",
            limit_price=100,
            quantity=1,
            portfolio="momentum",
        )
        with patch.object(
            runner._engine,
            "check_entry",
            return_value=RiskDecision(False, "capture", 0),
        ) as check:
            await runner.process_recommendation(rec)

        portfolio = check.call_args.kwargs["portfolio"]
        assert portfolio.nav == 100_000
        assert portfolio.positions["AAPL"]["quantity"] == 1
        session.close()

    @pytest.mark.asyncio
    async def test_drawdown_peak_excludes_uncapped_pre_rebaseline_snapshots(
        self, mock_config, mock_redis
    ):
        """Peak NAV for the drawdown breaker must ignore pre-re-baseline
        (uncapped, max_deployable_usd IS NULL) snapshots.

        Before the deployment cap existed, deployable_capital == full account
        NAV (~776k). After re-baselining and capping to 100k, an unfiltered
        max(deployable_capital) peak reads (776k-100k)/776k = ~87% phantom
        drawdown and liquidation-rejects every buy on an empty book.
        """
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = Session(engine)
        ledger = OrderLedger(session)
        now = datetime.now(timezone.utc)
        ledger.create_intent(
            SimpleNamespace(
                recommendation_id="cap-one", account_id="DUCAP", mode="paper",
                portfolio="momentum", con_id=1, symbol="AAPL", exchange="SMART",
                currency="USD", action="BUY", quantity=1, limit_price=100,
                order_type="LMT",
            )
        )
        session.add_all(
            [
                CapitalSnapshot(  # retired uncapped book (pre-re-baseline)
                    account_id="DUCAP", mode="paper", net_liquidation=776_000,
                    deployment_fraction=1, max_deployable_usd=None,
                    deployable_capital=776_000, settled_cash_trading=776_000,
                    sleeve_budgets={}, reconciliation_status="ok",
                    captured_at=now - timedelta(days=3),
                ),
                CapitalSnapshot(  # current capped book
                    account_id="DUCAP", mode="paper", net_liquidation=776_000,
                    deployment_fraction=1, max_deployable_usd=100_000,
                    deployable_capital=100_000, settled_cash_trading=100_000,
                    sleeve_budgets={}, reconciliation_status="ok", captured_at=now,
                ),
            ]
        )
        session.commit()
        runner = RiskServiceRunner(
            config=mock_config, redis_client=mock_redis,
            db_session=session, order_ledger=ledger,
        )
        rec = RecommendationMessage(
            ticker="AAPL", timestamp=now, action="buy", confidence=1,
            top_features={}, recommendation_id="cap-one", limit_price=100,
            quantity=1, portfolio="momentum",
        )
        with patch.object(
            runner._engine, "check_portfolio_drawdown",
            return_value=RiskDecision(False, "captured", 0),
        ) as drawdown:
            await runner.process_recommendation(rec)

        portfolio = drawdown.call_args.args[0]
        assert portfolio.nav == 100_000
        assert portfolio.peak_nav == 100_000  # NOT 776_000 (uncapped excluded)
        session.close()

    @pytest.mark.asyncio
    async def test_stale_capital_snapshot_fails_closed(
        self, durable_runner, mock_redis
    ):
        runner, ledger, session = durable_runner
        snapshot = session.scalar(select(CapitalSnapshot))
        snapshot.captured_at = datetime.now(timezone.utc) - timedelta(days=3)
        session.commit()
        rec = RecommendationMessage(
            ticker="AAPL",
            timestamp=datetime.now(timezone.utc),
            action="buy",
            confidence=1,
            top_features={},
            recommendation_id="rec-risk",
            limit_price=100,
            quantity=10,
            portfolio="momentum",
        )

        await runner.process_recommendation(rec)

        assert ledger.get("rec-risk").status == OrderStatus.RISK_REJECTED.value
        assert "stale" in ledger.get("rec-risk").reason
        assert not any(
            call.args[0] == "stream:approved_orders"
            for call in mock_redis.publish.call_args_list
        )

    @pytest.mark.asyncio
    async def test_breached_snapshot_rejects_buy_but_allows_durable_sell(
        self, durable_runner, mock_redis
    ):
        runner, ledger, session = durable_runner
        snapshot = session.scalar(select(CapitalSnapshot))
        snapshot.reconciliation_status = "major"
        sell = SimpleNamespace(
            recommendation_id="sell-risk",
            account_id="DUTEST",
            mode="paper",
            portfolio="momentum",
            con_id=265598,
            symbol="AAPL",
            exchange="SMART",
            currency="USD",
            action="SELL",
            quantity=3,
            limit_price=None,
            order_type="MKT",
        )
        ledger.create_intent(sell)
        now = datetime.now(timezone.utc)
        session.add(
            Position(
                account_id="DUTEST",
                ticker="AAPL",
                portfolio="momentum",
                con_id=265598,
                quantity=3,
                avg_entry_price=100,
                current_price=100,
                peak_price=100,
                highest_price_since_entry=100,
                opened_at=now,
                status="open",
            )
        )
        session.commit()
        buy_rec = RecommendationMessage(
            ticker="AAPL",
            timestamp=datetime.now(timezone.utc),
            action="buy",
            confidence=1,
            top_features={},
            recommendation_id="rec-risk",
            limit_price=100,
            quantity=10,
            portfolio="momentum",
        )
        sell_rec = RecommendationMessage(
            ticker="AAPL",
            timestamp=datetime.now(timezone.utc),
            action="sell",
            confidence=1,
            top_features={},
            recommendation_id="sell-risk",
            limit_price=None,
            quantity=3,
            portfolio="momentum",
        )

        await runner.process_recommendation(buy_rec)
        await runner.process_recommendation(sell_rec)

        assert ledger.get("rec-risk").status == OrderStatus.RISK_REJECTED.value
        assert ledger.get("sell-risk").status == OrderStatus.APPROVED.value
        approved = [
            call
            for call in mock_redis.publish.call_args_list
            if call.args[0] == "stream:approved_orders"
        ]
        assert float(approved[0].args[1]["quantity"]) == 3

    @pytest.mark.asyncio
    async def test_long_running_service_refreshes_new_capital_snapshot(
        self, durable_runner
    ):
        runner, _, session = durable_runner
        now = datetime.now(timezone.utc)
        session.add(
            CapitalSnapshot(
                account_id="DUTEST",
                mode="paper",
                net_liquidation=200_000,
                deployment_fraction=1,
                max_deployable_usd=None,
                deployable_capital=200_000,
                settled_cash_trading=200_000,
                sleeve_budgets={},
                reconciliation_status="ok",
                captured_at=now,
            )
        )
        session.commit()
        rec = RecommendationMessage(
            ticker="AAPL",
            timestamp=now,
            action="buy",
            confidence=1,
            top_features={},
            recommendation_id="rec-risk",
            limit_price=100,
            quantity=10,
            portfolio="momentum",
        )
        with patch.object(
            runner._engine,
            "check_entry",
            return_value=RiskDecision(False, "capture", 0),
        ) as check:
            await runner.process_recommendation(rec)

        assert check.call_args.kwargs["portfolio"].nav == 200_000

    def test_latest_deployable_capital_is_risk_nav(self, mock_config, mock_redis):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = Session(engine)
        session.add(
            CapitalSnapshot(
                account_id="DUTEST",
                mode="paper",
                net_liquidation=1_000_000,
                deployment_fraction=1,
                max_deployable_usd=None,
                deployable_capital=1_000_000,
                sleeve_budgets={"momentum": 230_800},
                reconciliation_status="ok",
                captured_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
            )
        )
        session.commit()

        runner = RiskServiceRunner(
            config=mock_config, redis_client=mock_redis, db_session=session
        )

        assert runner._portfolio.nav == pytest.approx(1_000_000)
        session.close()

    @pytest.fixture
    def durable_runner(self, mock_config, mock_redis):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = Session(engine)
        ledger = OrderLedger(session)
        proposal = SimpleNamespace(
            recommendation_id="rec-risk",
            account_id="DUTEST",
            mode="paper",
            portfolio="momentum",
            con_id=265598,
            symbol="AAPL",
            exchange="SMART",
            currency="USD",
            action="BUY",
            quantity=10.0,
            limit_price=100.0,
            order_type="LMT",
        )
        ledger.create_intent(proposal)
        session.add(
            CapitalSnapshot(
                account_id="DUTEST",
                mode="paper",
                net_liquidation=100_000,
                deployment_fraction=1,
                max_deployable_usd=None,
                deployable_capital=100_000,
                settled_cash_trading=100_000,
                sleeve_budgets={},
                reconciliation_status="ok",
                captured_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
        value = RiskServiceRunner(
            config=mock_config,
            redis_client=mock_redis,
            db_session=session,
            order_ledger=ledger,
        )
        value._portfolio = make_portfolio(nav=100_000)
        yield value, ledger, session
        session.close()

    @pytest.mark.asyncio
    async def test_approval_is_persisted_before_publish(
        self, durable_runner, mock_redis
    ):
        runner, ledger, session = durable_runner
        rec = RecommendationMessage(
            ticker="AAPL",
            timestamp=datetime.now(timezone.utc),
            action="buy",
            confidence=1,
            top_features={},
            recommendation_id="rec-risk",
            limit_price=100,
            quantity=10,
            portfolio="momentum",
        )

        async def publish(stream, payload):
            assert ledger.get("rec-risk").status == OrderStatus.APPROVED.value
            session.rollback()
            assert not session.in_transaction()
            return "1-0"

        mock_redis.publish.side_effect = publish
        await runner.process_recommendation(rec)

        assert ledger.get("rec-risk").status == OrderStatus.APPROVED.value

    @pytest.mark.asyncio
    async def test_rejection_is_persisted_and_not_published(
        self, durable_runner, mock_redis
    ):
        runner, ledger, _ = durable_runner
        rec = RecommendationMessage(
            ticker="AAPL",
            timestamp=datetime.now(timezone.utc),
            action="buy",
            confidence=1,
            top_features={},
            recommendation_id="rec-risk",
            limit_price=100,
            quantity=10,
            portfolio="momentum",
        )
        with patch.object(
            runner._engine,
            "check_entry",
            return_value=RiskDecision(False, "sector limit", 0),
        ):
            await runner.process_recommendation(rec)

        assert ledger.get("rec-risk").status == OrderStatus.RISK_REJECTED.value
        assert ledger.get("rec-risk").reason == "sector limit"
        assert not any(
            call.args[0] == "stream:approved_orders"
            for call in mock_redis.publish.call_args_list
        )

    @pytest.mark.asyncio
    async def test_active_reservation_is_passed_to_projected_risk(self, durable_runner):
        runner, ledger, session = durable_runner
        other = SimpleNamespace(
            recommendation_id="other",
            account_id="DUTEST",
            mode="paper",
            portfolio="momentum",
            con_id=123,
            symbol="MSFT",
            exchange="SMART",
            currency="USD",
            action="BUY",
            quantity=4,
            limit_price=250,
            order_type="LMT",
        )
        ledger.create_intent(other)
        ledger.transition("other", OrderStatus.APPROVED)
        session.commit()
        rec = RecommendationMessage(
            ticker="AAPL",
            timestamp=datetime.now(timezone.utc),
            action="buy",
            confidence=1,
            top_features={},
            recommendation_id="rec-risk",
            limit_price=100,
            quantity=10,
            portfolio="momentum",
        )
        with patch.object(
            runner._engine,
            "check_entry",
            return_value=RiskDecision(False, "reserved", 0),
        ) as check_entry:
            await runner.process_recommendation(rec)

        assert check_entry.call_args.kwargs["reserved_notional"] == pytest.approx(1000)

    @pytest.mark.asyncio
    async def test_account_wide_usd_funding_rejects_buy_despite_large_nav(
        self, durable_runner, mock_redis
    ):
        runner, ledger, session = durable_runner
        snapshot = session.scalar(select(CapitalSnapshot))
        snapshot.deployable_capital = 1_000_000
        snapshot.net_liquidation = 1_000_000
        snapshot.settled_cash_trading = 1_200
        ledger.create_intent(
            SimpleNamespace(
                recommendation_id="other-sleeve",
                account_id="DUTEST",
                mode="paper",
                portfolio="quality_value",
                con_id=123,
                symbol="MSFT",
                exchange="SMART",
                currency="USD",
                action="BUY",
                quantity=5,
                limit_price=100,
                order_type="LMT",
            )
        )
        ledger.transition("other-sleeve", OrderStatus.APPROVED)
        session.commit()
        rec = RecommendationMessage(
            ticker="AAPL",
            timestamp=datetime.now(timezone.utc),
            action="buy",
            confidence=1,
            top_features={},
            recommendation_id="rec-risk",
            limit_price=100,
            quantity=10,
            portfolio="momentum",
        )

        with patch.object(runner._engine, "check_entry") as check_entry:
            await runner.process_recommendation(rec)

        assert ledger.get("rec-risk").status == OrderStatus.RISK_REJECTED.value
        assert "settled USD cash" in ledger.get("rec-risk").reason
        check_entry.assert_not_called()
        assert not any(
            call.args[0] == "stream:approved_orders"
            for call in mock_redis.publish.call_args_list
        )
        assert any(
            call.args[0] == "stream:alerts"
            and call.args[1]["event_type"] == "entry_rejection"
            for call in mock_redis.publish.call_args_list
        )

    @pytest.mark.asyncio
    async def test_full_buy_fill_after_snapshot_remains_committed(
        self, durable_runner
    ):
        runner, ledger, session = durable_runner
        now = datetime.now(timezone.utc)
        snapshot = session.scalar(select(CapitalSnapshot))
        snapshot.captured_at = now - timedelta(minutes=5)
        snapshot.settled_cash_trading = 1_001
        prior = ledger.create_intent(
            make_buy_intent_proposal("prior-buy", quantity=9, price=100)
        )
        ledger.transition(prior.recommendation_id, OrderStatus.APPROVED)
        ledger.transition(prior.recommendation_id, OrderStatus.SUBMITTED)
        ledger.transition(prior.recommendation_id, OrderStatus.FILLED)
        prior.filled_quantity = 9
        session.add(make_execution_fill(executed_at=now))
        session.commit()

        with patch.object(runner._engine, "check_entry") as check_entry:
            await runner.process_recommendation(make_durable_buy_recommendation())

        assert ledger.get("rec-risk").status == OrderStatus.RISK_REJECTED.value
        assert "settled USD cash" in ledger.get("rec-risk").reason
        check_entry.assert_not_called()

    @pytest.mark.asyncio
    async def test_partial_buy_fill_keeps_fill_spend_and_remaining_order_reserved(
        self, durable_runner
    ):
        runner, ledger, session = durable_runner
        now = datetime.now(timezone.utc)
        snapshot = session.scalar(select(CapitalSnapshot))
        snapshot.captured_at = now - timedelta(minutes=5)
        snapshot.settled_cash_trading = 1_602
        prior = ledger.create_intent(
            make_buy_intent_proposal("prior-buy", quantity=10, price=100)
        )
        ledger.transition(prior.recommendation_id, OrderStatus.APPROVED)
        ledger.transition(prior.recommendation_id, OrderStatus.SUBMITTED)
        ledger.transition(prior.recommendation_id, OrderStatus.PARTIALLY_FILLED)
        prior.filled_quantity = 4
        session.add(
            make_execution_fill(
                executed_at=now,
                quantity=4,
                price=100,
                commission=0.5,
            )
        )
        session.commit()

        with patch.object(runner._engine, "check_entry") as check_entry:
            await runner.process_recommendation(make_durable_buy_recommendation())

        assert ledger.get("rec-risk").status == OrderStatus.RISK_REJECTED.value
        assert "settled USD cash" in ledger.get("rec-risk").reason
        check_entry.assert_not_called()

    @pytest.mark.asyncio
    async def test_newer_capital_snapshot_supersedes_prior_buy_fill_spend(
        self, durable_runner
    ):
        runner, ledger, session = durable_runner
        now = datetime.now(timezone.utc)
        old_snapshot = session.scalar(select(CapitalSnapshot))
        old_snapshot.captured_at = now - timedelta(minutes=10)
        session.add(make_execution_fill(executed_at=now - timedelta(minutes=5)))
        session.add(
            CapitalSnapshot(
                account_id="DUTEST",
                mode="paper",
                net_liquidation=100_000,
                deployment_fraction=1,
                max_deployable_usd=None,
                deployable_capital=100_000,
                settled_cash_trading=1_001,
                sleeve_budgets={},
                reconciliation_status="ok",
                captured_at=now,
            )
        )
        session.commit()

        await runner.process_recommendation(make_durable_buy_recommendation())

        assert ledger.get("rec-risk").status == OrderStatus.APPROVED.value

    @pytest.mark.asyncio
    async def test_active_order_commissions_can_make_second_buy_unaffordable(
        self, durable_runner
    ):
        runner, ledger, session = durable_runner
        snapshot = session.scalar(select(CapitalSnapshot))
        snapshot.settled_cash_trading = 2_001
        ledger.create_intent(
            make_buy_intent_proposal("prior-buy", quantity=10, price=100)
        )
        ledger.transition("prior-buy", OrderStatus.APPROVED)
        session.commit()

        with patch.object(runner._engine, "check_entry") as check_entry:
            await runner.process_recommendation(make_durable_buy_recommendation())

        assert ledger.get("rec-risk").status == OrderStatus.RISK_REJECTED.value
        assert "settled USD cash" in ledger.get("rec-risk").reason
        check_entry.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_settled_cash_rejects_buy_without_deleting_positions(
        self, durable_runner
    ):
        runner, ledger, session = durable_runner
        snapshot = session.scalar(select(CapitalSnapshot))
        snapshot.settled_cash_trading = None
        now = datetime.now(timezone.utc)
        session.add(
            Position(
                account_id="DUTEST",
                ticker="MSFT",
                portfolio="quality_value",
                con_id=123,
                quantity=2,
                avg_entry_price=200,
                current_price=210,
                peak_price=210,
                highest_price_since_entry=210,
                opened_at=now,
                status="open",
            )
        )
        session.commit()
        rec = RecommendationMessage(
            ticker="AAPL",
            timestamp=now,
            action="buy",
            confidence=1,
            top_features={},
            recommendation_id="rec-risk",
            limit_price=100,
            quantity=10,
            portfolio="momentum",
        )

        await runner.process_recommendation(rec)

        assert ledger.get("rec-risk").status == OrderStatus.RISK_REJECTED.value
        assert "invalid settled USD cash" in ledger.get("rec-risk").reason
        assert session.scalar(select(Position).where(Position.ticker == "MSFT"))

    @pytest.mark.asyncio
    async def test_sell_bypasses_invalid_settled_usd_funding(
        self, durable_runner, mock_redis
    ):
        runner, ledger, session = durable_runner
        snapshot = session.scalar(select(CapitalSnapshot))
        snapshot.settled_cash_trading = None
        sell = SimpleNamespace(
            recommendation_id="sell-risk",
            account_id="DUTEST",
            mode="paper",
            portfolio="momentum",
            con_id=265598,
            symbol="AAPL",
            exchange="SMART",
            currency="USD",
            action="SELL",
            quantity=3,
            limit_price=None,
            order_type="MKT",
        )
        ledger.create_intent(sell)
        now = datetime.now(timezone.utc)
        session.add(
            Position(
                account_id="DUTEST",
                ticker="AAPL",
                portfolio="momentum",
                con_id=265598,
                quantity=3,
                avg_entry_price=100,
                current_price=100,
                peak_price=100,
                highest_price_since_entry=100,
                opened_at=now,
                status="open",
            )
        )
        session.commit()
        rec = RecommendationMessage(
            ticker="AAPL",
            timestamp=now,
            action="sell",
            confidence=1,
            top_features={},
            recommendation_id="sell-risk",
            limit_price=None,
            quantity=3,
            portfolio="momentum",
        )

        await runner.process_recommendation(rec)

        assert ledger.get("sell-risk").status == OrderStatus.APPROVED.value
        assert any(
            call.args[0] == "stream:approved_orders"
            for call in mock_redis.publish.call_args_list
        )
