from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select

from services.risk_management.correlation import CorrelationMonitor
from services.risk_management.engine import PortfolioState, RiskEngine
from services.risk_management.kill_switch import KillSwitch
from services.risk_management.passive_monitor import PassiveBreachMonitor
from shared.config import AppConfig
from shared.logging import get_logger
from shared.models import CapitalSnapshot, OrderIntent, OrderStatus, Position
from shared.order_ledger import OrderIntentNotFound, OrderLedger
from shared.observability import DEFAULT_TRADING_METRICS
from shared.schemas.messages import (
    AlertMessage,
    ApprovedOrderMessage,
    FillMessage,
    KillMessage,
    RecommendationMessage,
)

RECOMMENDATIONS_STREAM = "stream:recommendations"
APPROVED_ORDERS_STREAM = "stream:approved_orders"
ALERTS_STREAM = "stream:alerts"
KILL_STREAM = "stream:kill"
FILLS_STREAM = "stream:fills"
CAPITAL_SNAPSHOT_MAX_AGE = timedelta(hours=36)


class RetryableRiskStateError(RuntimeError):
    """A durable recommendation must remain pending until state is available."""


CONSUMER_GROUP = "risk_management"
CONSUMER_NAME = "risk_worker_1"


class RiskServiceRunner:
    """Orchestrates the risk management service.

    Subscribes to ``stream:recommendations`` and ``stream:kill``.
    Gates every recommendation through risk checks before forwarding
    approved orders to ``stream:approved_orders``.

    Decision precedence:
    1. Kill switch / circuit breaker
    2. Critical margin protection
    3. Stop-loss exits
    4. Hard compliance constraints (position, sector, total exposure)
    5. Soft/advisory controls
    """

    def __init__(
        self,
        config: AppConfig,
        redis_client: Any,
        db_session: Any = None,
        order_ledger: OrderLedger | None = None,
        metrics: Any = None,
    ) -> None:
        self._config = config
        self._redis = redis_client
        self._logger = get_logger("risk_management")
        self._order_ledger = order_ledger or (
            OrderLedger(db_session) if db_session is not None else None
        )
        self._db_session = db_session
        self._metrics = metrics or DEFAULT_TRADING_METRICS

        risk_cfg = config.risk

        self._engine = RiskEngine(
            position_entry_limit_pct=risk_cfg.position_entry_limit_pct,
            sector_concentration_pct=risk_cfg.sector_concentration_pct,
            total_exposure_limit_pct=risk_cfg.total_exposure_limit_pct,
            stop_loss_trailing_pct=risk_cfg.stop_loss_trailing_pct,
            drawdown_pause_pct=risk_cfg.drawdown_pause_pct,
            drawdown_circuit_breaker_pct=risk_cfg.drawdown_circuit_breaker_pct,
        )
        self._kill_switch = KillSwitch(logger=self._logger)
        self._passive_monitor = PassiveBreachMonitor(config=risk_cfg)
        self._correlation_monitor = CorrelationMonitor()

        # Portfolio state — loaded from the DB when a session is provided so
        # kill liquidation and stop-loss scans act on real holdings instead
        # of an empty dict.
        self._cash = 0.0
        if db_session is not None:
            from shared.position_loader import load_portfolio_state

            state = load_portfolio_state(db_session)
            latest_capital = db_session.scalar(
                select(CapitalSnapshot)
                .where(CapitalSnapshot.mode == config.mode)
                .order_by(
                    CapitalSnapshot.captured_at.desc(),
                    CapitalSnapshot.id.desc(),
                )
                .limit(1)
            )
            risk_nav = (
                float(latest_capital.deployable_capital)
                if latest_capital is not None
                else state["nav"]
            )
            market_value = sum(
                position["quantity"] * position["current_price"]
                for position in state["positions"].values()
            )
            self._cash = risk_nav - market_value
            sector_exposure: dict[str, float] = {}
            if risk_nav > 0:
                for position in state["positions"].values():
                    sector = position.get("sector") or "Unknown"
                    value = position["quantity"] * position["current_price"]
                    sector_exposure[sector] = (
                        sector_exposure.get(sector, 0.0) + value / risk_nav * 100.0
                    )
            self._portfolio = PortfolioState(
                nav=risk_nav,
                peak_nav=max(state["peak_nav"], risk_nav),
                positions=state["positions"],
                sector_exposure=sector_exposure,
                total_exposure_pct=(
                    market_value / risk_nav * 100.0 if risk_nav > 0 else 0.0
                ),
                margin_utilization_pct=0.0,
            )
            self._logger.info(
                "Portfolio state loaded from DB",
                nav=risk_nav,
                peak_nav=max(state["peak_nav"], risk_nav),
                open_positions=len(state["positions"]),
            )
        else:
            self._portfolio = PortfolioState(
                nav=0.0,
                peak_nav=0.0,
                positions={},
                sector_exposure={},
                total_exposure_pct=0.0,
                margin_utilization_pct=0.0,
            )
        self._current_prices: dict[str, float] = {
            t: p["current_price"] for t, p in self._portfolio.positions.items()
        }
        if self._order_ledger is not None:
            pairs = list(
                self._order_ledger.session.execute(
                    select(OrderIntent.account_id, OrderIntent.mode).distinct()
                )
            )
            for account_id, mode in pairs:
                self._refresh_lifecycle_metrics(account_id, mode)
            self._order_ledger.session.rollback()

    async def setup(self) -> None:
        """Create consumer groups and replay pending messages.

        Delivered-but-unacked messages from before a crash are never
        re-delivered by the normal ``">"`` read; replay them so no
        recommendation, kill, or fill is silently lost across a restart.
        """
        await self._redis.create_consumer_group(RECOMMENDATIONS_STREAM, CONSUMER_GROUP)
        await self._redis.create_consumer_group(KILL_STREAM, CONSUMER_GROUP)
        await self._redis.create_consumer_group(FILLS_STREAM, CONSUMER_GROUP)

        replayed = 0
        for stream, parser, handler in (
            (
                RECOMMENDATIONS_STREAM,
                RecommendationMessage.from_stream_dict,
                self.process_recommendation,
            ),
            (KILL_STREAM, KillMessage.from_stream_dict, self.process_kill),
            (FILLS_STREAM, FillMessage.from_stream_dict, self.process_fill),
        ):
            pending = await self._redis.drain_pending(
                stream, CONSUMER_GROUP, CONSUMER_NAME
            )
            for msg in pending:
                try:
                    await handler(parser(msg.data))
                    await self._redis.ack(stream, CONSUMER_GROUP, msg.message_id)
                except RetryableRiskStateError as exc:
                    self._logger.warning(
                        "Retryable risk state unavailable; leaving message pending",
                        stream=stream,
                        message_id=msg.message_id,
                        reason=str(exc),
                    )
                except Exception as exc:
                    self._logger.exception(
                        "Error replaying pending message; sending to DLQ",
                        stream=stream,
                        message_id=msg.message_id,
                    )
                    await self._redis.send_to_dead_letter(stream, msg, str(exc))
                    await self._redis.ack(stream, CONSUMER_GROUP, msg.message_id)
            replayed += len(pending)

        if replayed:
            self._logger.warning(
                "Replayed pending messages from a prior crash", count=replayed
            )
        self._logger.info("Risk service consumer groups created")

    async def process_fill(self, fill: FillMessage) -> None:
        """Update the in-memory portfolio from an execution fill.

        Keeps positions, cash, nav, and peak_nav current between DB reloads
        so risk checks act on what the account actually holds.
        """
        positions = self._portfolio.positions
        self._current_prices[fill.ticker] = fill.fill_price

        if fill.side == "buy":
            pos = positions.get(fill.ticker)
            if pos is None:
                positions[fill.ticker] = {
                    "quantity": fill.quantity,
                    "avg_entry_price": fill.fill_price,
                    "current_price": fill.fill_price,
                    "highest_price_since_entry": fill.fill_price,
                    "sector": "Unknown",
                }
            else:
                total = pos["quantity"] + fill.quantity
                if total > 0:
                    pos["avg_entry_price"] = (
                        pos["avg_entry_price"] * pos["quantity"]
                        + fill.fill_price * fill.quantity
                    ) / total
                pos["quantity"] = total
                pos["current_price"] = fill.fill_price
            self._cash -= fill.fill_price * fill.quantity + fill.commission
        else:
            pos = positions.get(fill.ticker)
            if pos is not None:
                pos["quantity"] -= fill.quantity
                pos["current_price"] = fill.fill_price
                if pos["quantity"] <= 0:
                    del positions[fill.ticker]
            self._cash += fill.fill_price * fill.quantity - fill.commission

        market_value = sum(
            p["quantity"] * self._current_prices.get(t, p["current_price"])
            for t, p in positions.items()
        )
        self._portfolio.nav = self._cash + market_value
        self._portfolio.peak_nav = max(self._portfolio.peak_nav, self._portfolio.nav)

        self._logger.info(
            "Portfolio updated from fill",
            ticker=fill.ticker,
            side=fill.side,
            quantity=fill.quantity,
            nav=self._portfolio.nav,
            open_positions=len(positions),
        )

    async def process_recommendation(self, rec: RecommendationMessage) -> None:
        """Process a single recommendation through the risk gate.

        Decision precedence:
        1. Kill switch check
        2. Portfolio drawdown check
        3. Entry compliance check (for buys)
        4. Forward approved order
        """
        self._logger.info(
            "Processing recommendation",
            ticker=rec.ticker,
            action=rec.action,
            recommendation_id=rec.recommendation_id,
        )

        # Hold recommendations are ignored
        if rec.action == "hold":
            self._logger.info(
                "Ignoring hold recommendation",
                ticker=rec.ticker,
                recommendation_id=rec.recommendation_id,
            )
            return

        intent = self._durable_intent(rec)
        if intent is None:
            self._logger.error(
                "Rejecting recommendation without a matching durable intent",
                recommendation_id=rec.recommendation_id,
            )
            await self._publish_alert(
                event_type="durable_intent_missing",
                priority="high",
                message=(
                    f"Rejected {rec.action} {rec.ticker}: missing or mismatched "
                    "durable order intent"
                ),
                context={"recommendation_id": rec.recommendation_id},
            )
            return

        refresh_error = self._refresh_risk_state(intent, rec.action)
        if refresh_error is not None:
            if rec.action == "buy":
                self._persist_risk_rejection(rec.recommendation_id, refresh_error)
            self._logger.error(
                "Risk state refresh failed closed",
                recommendation_id=rec.recommendation_id,
                reason=refresh_error,
            )
            await self._publish_alert(
                event_type="risk_state_unavailable",
                priority="high",
                message=f"Rejected {rec.action} {rec.ticker}: {refresh_error}",
                context={"recommendation_id": rec.recommendation_id},
            )
            if rec.action == "sell":
                raise RetryableRiskStateError(refresh_error)
            return

        # 1. Kill switch — highest precedence
        kill_decision = self._kill_switch.check()
        if not kill_decision.approved:
            self._persist_risk_rejection(rec.recommendation_id, kill_decision.reason)
            self._logger.warning(
                "Kill switch rejected recommendation",
                ticker=rec.ticker,
                reason=kill_decision.reason,
            )
            await self._publish_alert(
                event_type="kill_switch_rejection",
                priority="critical",
                message=f"Kill switch rejected {rec.action} {rec.ticker}: {kill_decision.reason}",
                context={
                    "ticker": rec.ticker,
                    "recommendation_id": rec.recommendation_id,
                },
            )
            return

        # 2. Portfolio drawdown check (for buys only)
        if rec.action == "buy":
            drawdown_decision = self._engine.check_portfolio_drawdown(self._portfolio)
            if not drawdown_decision.approved:
                self._persist_risk_rejection(
                    rec.recommendation_id, drawdown_decision.reason
                )
                self._logger.warning(
                    "Drawdown check rejected buy",
                    ticker=rec.ticker,
                    reason=drawdown_decision.reason,
                )
                await self._publish_alert(
                    event_type="drawdown_rejection",
                    priority="high",
                    message=f"Drawdown rejected buy {rec.ticker}: {drawdown_decision.reason}",
                    context={
                        "ticker": rec.ticker,
                        "recommendation_id": rec.recommendation_id,
                    },
                )
                return

        # 3. Entry compliance check (for buys)
        risk_adjustments: dict[str, Any] = {}
        quantity: int = 0

        if rec.action == "buy":
            # Sleeve recommendations carry their own limit price; ML-path
            # ones rely on the last seen market price.
            price = intent.limit_price or self._current_prices.get(intent.symbol, 0.0)
            if price <= 0:
                self._persist_risk_rejection(rec.recommendation_id, "no usable price")
                self._logger.warning(
                    "Rejecting buy with no usable price",
                    ticker=rec.ticker,
                    recommendation_id=rec.recommendation_id,
                )
                await self._publish_alert(
                    event_type="entry_rejection",
                    priority="medium",
                    message=f"Rejected buy {rec.ticker}: no usable price",
                    context={
                        "ticker": rec.ticker,
                        "recommendation_id": rec.recommendation_id,
                    },
                )
                return
            self._current_prices[intent.symbol] = price
            sector = self._get_sector(intent.symbol)
            # Sleeve-sized quantity when provided, else estimate from the
            # config position limit.
            default_qty = float(intent.requested_quantity)

            reserved_notional = self._active_reservations(rec)
            entry_decision = self._engine.check_entry(
                ticker=intent.symbol,
                quantity=default_qty,
                price=price,
                sector=sector,
                portfolio=self._portfolio,
                reserved_notional=reserved_notional,
            )

            if not entry_decision.approved:
                self._persist_risk_rejection(
                    rec.recommendation_id, entry_decision.reason
                )
                self._logger.warning(
                    "Entry check rejected buy",
                    ticker=rec.ticker,
                    reason=entry_decision.reason,
                )
                await self._publish_alert(
                    event_type="entry_rejection",
                    priority="medium",
                    message=f"Entry rejected buy {rec.ticker}: {entry_decision.reason}",
                    context={
                        "ticker": rec.ticker,
                        "recommendation_id": rec.recommendation_id,
                    },
                )
                return

            quantity = entry_decision.adjusted_quantity
            if entry_decision.adjusted_quantity != default_qty:
                risk_adjustments["position_scaled"] = {
                    "original": default_qty,
                    "adjusted": entry_decision.adjusted_quantity,
                    "reason": entry_decision.reason,
                }

        elif rec.action == "sell":
            # Sleeve exits carry their own account/sleeve-scoped quantity.
            quantity = float(intent.requested_quantity)
            pos = self._portfolio.positions.get(intent.symbol, {})
            if quantity <= 0:
                quantity = pos.get("quantity", 0) if isinstance(pos, dict) else 0
            if quantity <= 0:
                quantity = 1  # minimum sell quantity

        if intent.portfolio:
            risk_adjustments["portfolio"] = intent.portfolio

        # 4. Publish approved order
        order = ApprovedOrderMessage(
            ticker=intent.symbol,
            timestamp=datetime.now(timezone.utc),
            action=intent.action.lower(),
            quantity=quantity,
            order_type="limit" if intent.action.upper() == "BUY" else "market",
            limit_price=self._current_prices.get(intent.symbol)
            if intent.action.upper() == "BUY"
            else None,
            recommendation_id=rec.recommendation_id,
            risk_adjustments=risk_adjustments,
            portfolio=intent.portfolio,
        )

        if not self._persist_risk_approval(rec.recommendation_id):
            return

        await self._redis.publish(
            APPROVED_ORDERS_STREAM,
            order.to_stream_dict(),
        )

        self._logger.info(
            "Approved order published",
            ticker=rec.ticker,
            action=rec.action,
            quantity=quantity,
            recommendation_id=rec.recommendation_id,
        )

    def _durable_intent(self, rec: RecommendationMessage) -> OrderIntent | None:
        if self._order_ledger is None:
            return None
        try:
            intent = self._order_ledger.get(rec.recommendation_id)
        except OrderIntentNotFound:
            self._order_ledger.session.rollback()
            return None
        valid_status = intent.status in {
            OrderStatus.PROPOSED.value,
            OrderStatus.APPROVED.value,
        }
        if intent.limit_price is None or rec.limit_price is None:
            price_matches = intent.limit_price is None and rec.limit_price is None
        else:
            intent_price = float(intent.limit_price)
            payload_price = float(rec.limit_price)
            price_matches = (
                math.isfinite(intent_price)
                and math.isfinite(payload_price)
                and math.isclose(
                    intent_price, payload_price, rel_tol=1e-9, abs_tol=1e-6
                )
            )
        if rec.quantity is None:
            quantity_matches = False
        else:
            intent_quantity = float(intent.requested_quantity)
            payload_quantity = float(rec.quantity)
            quantity_matches = (
                math.isfinite(intent_quantity)
                and math.isfinite(payload_quantity)
                and math.isclose(
                    intent_quantity, payload_quantity, rel_tol=1e-9, abs_tol=1e-6
                )
            )
        matches = (
            intent.symbol == rec.ticker
            and intent.action.lower() == rec.action
            and intent.portfolio == rec.portfolio
            and price_matches
            and quantity_matches
        )
        if not valid_status or not matches:
            self._order_ledger.session.rollback()
            return None
        return intent

    def _refresh_risk_state(self, intent: OrderIntent, action: str) -> str | None:
        session = self._order_ledger.session
        snapshot = session.scalar(
            select(CapitalSnapshot)
            .where(
                CapitalSnapshot.account_id == intent.account_id,
                CapitalSnapshot.mode == intent.mode,
            )
            .order_by(CapitalSnapshot.captured_at.desc(), CapitalSnapshot.id.desc())
            .limit(1)
        )
        if snapshot is None:
            session.rollback()
            return "matching capital snapshot absent"
        captured_at = snapshot.captured_at
        if captured_at.tzinfo is None:
            captured_at = captured_at.replace(tzinfo=timezone.utc)
        if (
            action == "buy"
            and datetime.now(timezone.utc) - captured_at > CAPITAL_SNAPSHOT_MAX_AGE
        ):
            session.rollback()
            return "matching capital snapshot is stale"
        if action == "buy" and snapshot.reconciliation_status.lower() != "ok":
            session.rollback()
            return "latest reconciliation is breached"

        rows = list(
            session.scalars(
                select(Position).where(
                    Position.account_id == intent.account_id,
                    Position.status == "open",
                )
            )
        )
        positions: dict[str, dict[str, Any]] = {}
        for row in rows:
            current = positions.get(row.ticker)
            if current is None:
                positions[row.ticker] = {
                    "quantity": float(row.quantity),
                    "avg_entry_price": float(row.avg_entry_price),
                    "current_price": float(row.current_price),
                    "highest_price_since_entry": float(row.highest_price_since_entry),
                    "sector": row.sector or "Unknown",
                }
            else:
                prior_quantity = current["quantity"]
                total_quantity = prior_quantity + float(row.quantity)
                if total_quantity > 0:
                    current["avg_entry_price"] = (
                        current["avg_entry_price"] * prior_quantity
                        + float(row.avg_entry_price) * float(row.quantity)
                    ) / total_quantity
                current["quantity"] = total_quantity
                current["current_price"] = float(row.current_price)
                current["highest_price_since_entry"] = max(
                    current["highest_price_since_entry"],
                    float(row.highest_price_since_entry),
                )

        if action == "sell":
            held_quantity = float(positions.get(intent.symbol, {}).get("quantity", 0.0))
            other_sell_remaining = (
                OrderIntent.requested_quantity - OrderIntent.filled_quantity
            )
            reserved = float(
                session.scalar(
                    select(func.coalesce(func.sum(other_sell_remaining), 0.0)).where(
                        OrderIntent.account_id == intent.account_id,
                        OrderIntent.mode == intent.mode,
                        OrderIntent.symbol == intent.symbol,
                        OrderIntent.recommendation_id != intent.recommendation_id,
                        func.upper(OrderIntent.action) == "SELL",
                        or_(
                            OrderIntent.status.in_(
                                (
                                    OrderStatus.APPROVED.value,
                                    OrderStatus.SUBMITTED.value,
                                    OrderStatus.PARTIALLY_FILLED.value,
                                )
                            ),
                            (
                                (OrderIntent.status == OrderStatus.PROPOSED.value)
                                & OrderIntent.published_at.is_not(None)
                            ),
                        ),
                    )
                )
                or 0.0
            )
            uncovered = max(0.0, held_quantity - reserved)
            if float(intent.requested_quantity) > uncovered + 1e-6:
                session.rollback()
                return "sell validation headroom unavailable"

        nav = float(snapshot.deployable_capital)
        market_value = sum(
            position["quantity"] * position["current_price"]
            for position in positions.values()
        )
        peak_nav = float(
            session.scalar(
                select(func.max(CapitalSnapshot.deployable_capital)).where(
                    CapitalSnapshot.account_id == intent.account_id,
                    CapitalSnapshot.mode == intent.mode,
                )
            )
            or nav
        )
        sector_exposure: dict[str, float] = {}
        if nav > 0:
            for position in positions.values():
                sector = position["sector"]
                sector_exposure[sector] = sector_exposure.get(sector, 0.0) + (
                    position["quantity"] * position["current_price"] / nav * 100.0
                )
        self._cash = nav - market_value
        self._portfolio = PortfolioState(
            nav=nav,
            peak_nav=max(nav, peak_nav),
            positions=positions,
            sector_exposure=sector_exposure,
            total_exposure_pct=(market_value / nav * 100.0 if nav > 0 else 0.0),
            margin_utilization_pct=0.0,
        )
        self._current_prices = {
            ticker: position["current_price"] for ticker, position in positions.items()
        }
        session.rollback()
        return None

    def _active_reservations(self, rec: RecommendationMessage) -> float:
        if self._order_ledger is None or not rec.portfolio:
            return 0.0
        try:
            intent = self._order_ledger.get(rec.recommendation_id)
            value = self._order_ledger.active_reservations(
                rec.portfolio,
                account_id=intent.account_id,
                exclude_recommendation_id=rec.recommendation_id,
            )
            # Do not carry a read-only transaction across broker/Redis awaits.
            self._order_ledger.session.rollback()
            return value
        except OrderIntentNotFound:
            self._order_ledger.session.rollback()
            return 0.0

    def _persist_risk_rejection(self, recommendation_id: str, reason: str) -> bool:
        if self._order_ledger is None:
            return True
        try:
            intent = self._order_ledger.get(recommendation_id)
            status = OrderStatus(intent.status)
            if status is OrderStatus.RISK_REJECTED:
                self._order_ledger.session.rollback()
                return False
            if status is not OrderStatus.PROPOSED:
                self._order_ledger.session.rollback()
                return False
            self._order_ledger.transition(
                recommendation_id, OrderStatus.RISK_REJECTED, reason=reason
            )
            self._order_ledger.session.commit()
            self._metrics.lifecycle_transitions.labels(
                status=OrderStatus.RISK_REJECTED.value
            ).inc()
            self._refresh_lifecycle_metrics(intent.account_id, intent.mode)
            return True
        except OrderIntentNotFound:
            self._order_ledger.session.rollback()
            return True

    def _persist_risk_approval(self, recommendation_id: str) -> bool:
        if self._order_ledger is None:
            return True
        try:
            intent = self._order_ledger.get(recommendation_id)
            status = OrderStatus(intent.status)
            if status is OrderStatus.APPROVED:
                self._order_ledger.session.rollback()
                return True
            if status is not OrderStatus.PROPOSED:
                self._order_ledger.session.rollback()
                return False
            self._order_ledger.transition(recommendation_id, OrderStatus.APPROVED)
            self._order_ledger.session.commit()
            self._metrics.lifecycle_transitions.labels(
                status=OrderStatus.APPROVED.value
            ).inc()
            self._refresh_lifecycle_metrics(intent.account_id, intent.mode)
            return True
        except OrderIntentNotFound:
            self._order_ledger.session.rollback()
            return True

    def _refresh_lifecycle_metrics(self, account_id: str, mode: str) -> None:
        if self._order_ledger is None:
            return
        rows = self._order_ledger.session.execute(
            select(OrderIntent.status, func.count(OrderIntent.id))
            .where(
                OrderIntent.account_id == account_id,
                OrderIntent.mode == mode,
            )
            .group_by(OrderIntent.status)
        )
        counts = {status: int(count) for status, count in rows}
        for status in OrderStatus:
            self._metrics.lifecycle_state.labels(
                account_id=account_id,
                mode=mode,
                status=status.value,
            ).set(counts.get(status.value, 0))
        self._order_ledger.session.rollback()

    async def process_kill(self, kill_msg: KillMessage) -> None:
        """Process a kill message: activate switch and liquidate all positions.

        Args:
            kill_msg: The kill message with reason and trigger info.
        """
        self._kill_switch.activate(
            reason=kill_msg.reason,
            triggered_by=kill_msg.triggered_by,
        )

        self._logger.critical(
            "Kill switch activated — liquidating all positions",
            reason=kill_msg.reason,
            triggered_by=kill_msg.triggered_by,
        )

        # Emit market sell orders for all open positions
        for ticker, pos_data in self._portfolio.positions.items():
            quantity = pos_data.get("quantity", 0) if isinstance(pos_data, dict) else 0
            if quantity <= 0:
                continue

            order = ApprovedOrderMessage(
                ticker=ticker,
                timestamp=datetime.now(timezone.utc),
                action="sell",
                quantity=quantity,
                order_type="market",
                limit_price=None,
                recommendation_id=f"kill-{uuid.uuid4()}",
                risk_adjustments={"kill_switch": True, "reason": kill_msg.reason},
            )

            await self._redis.publish(
                APPROVED_ORDERS_STREAM,
                order.to_stream_dict(),
            )

            self._logger.info(
                "Kill liquidation order published",
                ticker=ticker,
                quantity=quantity,
            )

        await self._publish_alert(
            event_type="kill_switch_activated",
            priority="critical",
            message=f"Kill switch activated by {kill_msg.triggered_by}: {kill_msg.reason}",
            context={"triggered_by": kill_msg.triggered_by},
        )

    async def run_passive_scan(self) -> None:
        """Run passive breach monitoring scan.

        Scans all positions for soft/hard ceiling breaches and margin
        utilization warnings. Publishes alerts for any breaches found.
        """
        breaches = self._passive_monitor.scan_positions(
            self._portfolio, self._current_prices
        )

        for breach in breaches:
            priority = "high" if breach.action_type == "trim" else "medium"
            await self._publish_alert(
                event_type=f"passive_breach_{breach.action_type}",
                priority=priority,
                message=breach.message,
                context={
                    "ticker": breach.ticker,
                    "action_type": breach.action_type,
                    "current_pct": breach.current_pct,
                    "target_pct": breach.target_pct,
                },
            )

        if breaches:
            self._logger.info(
                "Passive scan completed",
                breach_count=len(breaches),
            )

    async def run_stop_loss_check(self) -> None:
        """Check all positions for trailing stop-loss triggers.

        For each position, checks if the current price has dropped
        beyond the trailing stop threshold from the highest price since entry.
        """
        for ticker, pos_data in self._portfolio.positions.items():
            if not isinstance(pos_data, dict):
                continue
            current_price = self._current_prices.get(ticker)
            highest = pos_data.get("highest_price_since_entry", current_price)
            if current_price is None or highest is None:
                continue

            decision = self._engine.check_stop_loss(
                ticker=ticker,
                current_price=current_price,
                highest_price_since_entry=highest,
            )

            if not decision.approved:
                # Emit sell order
                quantity = pos_data.get("quantity", 0)
                order = ApprovedOrderMessage(
                    ticker=ticker,
                    timestamp=datetime.now(timezone.utc),
                    action="sell",
                    quantity=quantity,
                    order_type="market",
                    limit_price=None,
                    recommendation_id=f"stop-loss-{uuid.uuid4()}",
                    risk_adjustments={"stop_loss": True, "reason": decision.reason},
                )

                await self._redis.publish(
                    APPROVED_ORDERS_STREAM,
                    order.to_stream_dict(),
                )

                await self._publish_alert(
                    event_type="stop_loss_triggered",
                    priority="high",
                    message=decision.reason,
                    context={"ticker": ticker, "quantity": quantity},
                )

    def _estimate_buy_quantity(self, ticker: str, price: float) -> int:
        """Estimate the number of shares to buy based on position limit.

        Uses the position entry limit percentage of NAV as target.
        """
        if price <= 0 or self._portfolio.nav <= 0:
            return 0
        max_value = self._portfolio.nav * (
            self._config.risk.position_entry_limit_pct / 100.0
        )
        return max(1, int(max_value / price))

    def _get_sector(self, ticker: str) -> str:
        """Look up the sector for a ticker from existing positions.

        Falls back to "Unknown" if not found.
        """
        pos = self._portfolio.positions.get(ticker, {})
        if isinstance(pos, dict):
            return pos.get("sector", "Unknown")
        return "Unknown"

    async def _publish_alert(
        self,
        event_type: str,
        priority: str,
        message: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Publish an alert message to the alerts stream."""
        alert = AlertMessage(
            timestamp=datetime.now(timezone.utc),
            event_type=event_type,
            priority=priority,
            message=message,
            context=context or {},
        )
        await self._redis.publish(ALERTS_STREAM, alert.to_stream_dict())

    async def run(self) -> None:
        """Main event loop: read from streams and dispatch."""
        await self.setup()
        self._logger.info("Risk management service started")

        try:
            while True:
                # Read recommendations
                messages = await self._redis.read_group(
                    RECOMMENDATIONS_STREAM,
                    CONSUMER_GROUP,
                    CONSUMER_NAME,
                    count=10,
                    block_ms=2000,
                )
                for msg in messages:
                    try:
                        rec = RecommendationMessage.from_stream_dict(msg.data)
                        await self.process_recommendation(rec)
                        await self._redis.ack(
                            RECOMMENDATIONS_STREAM, CONSUMER_GROUP, msg.message_id
                        )
                    except Exception:
                        self._logger.exception(
                            "Error processing recommendation", message_id=msg.message_id
                        )

                # Read kill stream
                kill_messages = await self._redis.read_group(
                    KILL_STREAM, CONSUMER_GROUP, CONSUMER_NAME, count=1, block_ms=500
                )
                for msg in kill_messages:
                    try:
                        kill_msg = KillMessage.from_stream_dict(msg.data)
                        await self.process_kill(kill_msg)
                        await self._redis.ack(
                            KILL_STREAM, CONSUMER_GROUP, msg.message_id
                        )
                    except Exception:
                        self._logger.exception(
                            "Error processing kill message", message_id=msg.message_id
                        )

                # Read fills to keep the in-memory portfolio current
                fill_messages = await self._redis.read_group(
                    FILLS_STREAM, CONSUMER_GROUP, CONSUMER_NAME, count=10, block_ms=500
                )
                for msg in fill_messages:
                    try:
                        fill = FillMessage.from_stream_dict(msg.data)
                        await self.process_fill(fill)
                        await self._redis.ack(
                            FILLS_STREAM, CONSUMER_GROUP, msg.message_id
                        )
                    except Exception:
                        self._logger.exception(
                            "Error processing fill message", message_id=msg.message_id
                        )
        except (KeyboardInterrupt, Exception):
            self._logger.info("Risk management service interrupted")


if __name__ == "__main__":
    import asyncio

    from shared.config import load_config

    config = load_config("config/default.yaml")

    async def main() -> None:
        import redis.asyncio as aioredis
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from shared.redis_client import RedisStreamClient

        redis_conn = aioredis.from_url(config.redis.url)
        redis_client = RedisStreamClient(redis_conn)

        # Load real holdings at startup — stop-loss and kill liquidation
        # must act on actual positions, not an empty dict.
        engine = create_engine(config.database.url)
        session = sessionmaker(bind=engine)()
        runner = RiskServiceRunner(
            config=config,
            redis_client=redis_client,
            db_session=session,
            order_ledger=OrderLedger(session),
        )
        try:
            await runner.run()
        finally:
            session.close()

    asyncio.run(main())
