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
from shared.models import Base, CapitalSnapshot, OrderStatus, Position
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
