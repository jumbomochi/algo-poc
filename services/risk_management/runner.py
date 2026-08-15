from __future__ import annotations

import asyncio
import math
from collections import deque
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, or_, select

from services.risk_management.correlation import CorrelationMonitor
from services.risk_management.engine import PortfolioState, RiskEngine
from services.risk_management.funding import (
    FundingDecision,
    check_settled_usd_funding,
    estimate_commission_usd,
)
from services.risk_management.kill_switch import KillSwitch
from services.risk_management.passive_monitor import PassiveBreachMonitor
from shared.config import AppConfig
from shared.halt_state import HaltStateRepository
from shared.heartbeat import register_heartbeat_collector, write_heartbeat
from shared.liquidation import (
    exit_intent_prefix,
    liquidation_exit_id,
    load_liquidation_targets,
)
from shared.logging import get_logger
from shared.models import CapitalSnapshot, OrderIntent, OrderStatus, Position
from shared.order_ledger import (
    ConflictingOrderIntent,
    OrderIntentNotFound,
    OrderLedger,
)
from shared.universe import lookup_sector
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
SELL_ATTRIBUTION_REJECTION = "sell sleeve attribution headroom insufficient"


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
        # Durable kill switch: persist halts so a restart stays halted
        # (fail-closed) instead of silently resuming trading (review 1.1).
        halt_store = (
            HaltStateRepository(db_session) if db_session is not None else None
        )
        self._kill_switch = KillSwitch(
            logger=self._logger, halt_store=halt_store, mode=config.mode
        )
        self._kill_switch.reload_from_store()
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
                # Drawdown is judged on the real marked book (cash + MTM), not
                # the capped deployment budget in nav. load_portfolio_state
                # already computes both from PortfolioConfig.cash + positions
                # and the EquitySnapshot high-water mark.
                book_equity=state["nav"],
                book_peak_equity=state["peak_nav"],
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

        # Periodic risk driver: reuse the passive-scan cadence to run the
        # intraday stop-loss, hard-ceiling auto-trim, and drawdown gauge. Track
        # the last run on a monotonic clock so the interval survives wall-clock
        # jumps. ``None`` means "run on the first loop iteration".
        self._passive_scan_interval_seconds = max(
            1, int(risk_cfg.passive_scan_interval_minutes) * 60
        )
        self._last_periodic_scan_at: float | None = None

        # Dedup fills by execution_id so a redelivered fill never double-counts
        # the in-memory book. Bounded (FIFO eviction) so it can't grow without
        # limit over a long-running process; the DB projector is the durable
        # dedup, this only needs to cover the redelivery window.
        self._processed_execution_ids: set[str] = set()
        self._processed_execution_order: deque[str] = deque(maxlen=10_000)

        # Last-alerted depth per dlq, so a persistent backlog doesn't re-alert
        # every scan — only a fresh backlog or a growing one does.
        self._dlq_alerted_depth: dict[str, int] = {}

        # Exit intents the previous scan had to re-publish (KAN-8). One
        # re-publish is a recovered crash and needs nobody; the same id needing
        # it again means the publish is not sticking, which pages. Both sets are
        # in-process, so a service that crash-loops between scans never sees a
        # repeat — the durable half of that is left to KAN-9's re-fire ledger.
        self._republished_exit_ids: set[str] = set()
        # Orphans already escalated as too old to re-publish, so a row nobody
        # has cleared does not re-page every scan.
        self._stale_orphan_ids: set[str] = set()

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
        # At-least-once delivery: the same execution can be redelivered (crash
        # replay via drain_pending, or a steady-state retry). The DB projector
        # dedups durably by execution_id; the in-memory book must too, or a
        # replay double-counts NAV/cash/positions.
        if fill.execution_id is not None:
            if fill.execution_id in self._processed_execution_ids:
                self._logger.info(
                    "Ignoring already-processed fill (replay)",
                    execution_id=fill.execution_id,
                    ticker=fill.ticker,
                )
                return
            self._remember_execution_id(fill.execution_id)

        positions = self._portfolio.positions
        self._current_prices[fill.ticker] = fill.fill_price

        # Cash is denominated in the trading currency (USD). Use the USD-converted
        # commission; only fall back to the raw amount when it is already USD.
        # A native (e.g. SGD) commission with no conversion is excluded rather
        # than subtracted as if it were USD (the projector rejects that case).
        if fill.commission_trading is not None:
            commission_usd = fill.commission_trading
        elif fill.commission_currency in (None, "USD"):
            commission_usd = fill.commission
        else:
            commission_usd = 0.0
            self._logger.warning(
                "Non-USD commission without USD conversion; excluded from the "
                "in-memory book",
                ticker=fill.ticker,
                currency=fill.commission_currency,
            )

        if fill.side == "buy":
            pos = positions.get(fill.ticker)
            if pos is None:
                positions[fill.ticker] = {
                    "quantity": fill.quantity,
                    "avg_entry_price": fill.fill_price,
                    "current_price": fill.fill_price,
                    "highest_price_since_entry": fill.fill_price,
                    "sector": lookup_sector(fill.ticker),
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
            self._cash -= fill.fill_price * fill.quantity + commission_usd
        else:
            pos = positions.get(fill.ticker)
            if pos is not None:
                pos["quantity"] -= fill.quantity
                pos["current_price"] = fill.fill_price
                if pos["quantity"] <= 0:
                    del positions[fill.ticker]
            self._cash += fill.fill_price * fill.quantity - commission_usd

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

    def _remember_execution_id(self, execution_id: str) -> None:
        """Record a processed execution_id, evicting the oldest past the cap."""
        if len(self._processed_execution_order) == self._processed_execution_order.maxlen:
            oldest = self._processed_execution_order[0]
            self._processed_execution_ids.discard(oldest)
        self._processed_execution_order.append(execution_id)
        self._processed_execution_ids.add(execution_id)

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
            terminal_rejection = (
                rec.action == "buy" or refresh_error == SELL_ATTRIBUTION_REJECTION
            )
            if terminal_rejection:
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
            if rec.action == "sell" and not terminal_rejection:
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

            funding_decision = self._check_settled_usd_funding(
                intent, quantity=default_qty, price=price
            )
            if not funding_decision.approved:
                self._persist_risk_rejection(
                    rec.recommendation_id, funding_decision.reason
                )
                self._logger.warning(
                    "Settled USD funding rejected buy",
                    ticker=rec.ticker,
                    reason=funding_decision.reason,
                    required_usd=funding_decision.required_usd,
                    remaining_usd=funding_decision.remaining_usd,
                )
                await self._publish_alert(
                    event_type="entry_rejection",
                    priority="medium",
                    message=(
                        f"Entry rejected buy {rec.ticker}: "
                        f"{funding_decision.reason}"
                    ),
                    context={
                        "ticker": rec.ticker,
                        "recommendation_id": rec.recommendation_id,
                        "required_usd": funding_decision.required_usd,
                        "remaining_usd": funding_decision.remaining_usd,
                    },
                )
                return

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
        if self._order_ledger is not None:
            # Keeps the KAN-8 invariant honest on the entry path too: an
            # APPROVED sell with published_at NULL means "orphan". Usually a
            # no-op, because run_paper's outbox marks the row when it publishes
            # the recommendation; it is load-bearing for the window where that
            # publish landed but its mark did not.
            #
            # Never fatal. The order is already on the stream, so raising here
            # would dead-letter a recommendation that was in fact processed
            # (see the ack comment in _consume_and_process for the same
            # reasoning). An unmarked row is recovered by the next sweep.
            try:
                self._order_ledger.mark_published(rec.recommendation_id)
                self._order_ledger.session.commit()
            except Exception:
                self._order_ledger.session.rollback()
                self._logger.exception(
                    "Could not mark an approved order published; the periodic "
                    "sweep will reconcile it",
                    recommendation_id=rec.recommendation_id,
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
                    "sector": row.sector or lookup_sector(row.ticker),
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
            sleeve_held_quantity = sum(
                float(row.quantity)
                for row in rows
                if row.ticker == intent.symbol
                and row.portfolio == intent.portfolio
                and row.con_id == intent.con_id
            )
            other_sell_remaining = (
                OrderIntent.requested_quantity - OrderIntent.filled_quantity
            )
            reserved = float(
                session.scalar(
                    select(func.coalesce(func.sum(other_sell_remaining), 0.0)).where(
                        OrderIntent.account_id == intent.account_id,
                        OrderIntent.mode == intent.mode,
                        OrderIntent.symbol == intent.symbol,
                        OrderIntent.con_id == intent.con_id,
                        OrderIntent.portfolio == intent.portfolio,
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
            uncovered = max(0.0, sleeve_held_quantity - reserved)
            if float(intent.requested_quantity) > uncovered + 1e-6:
                session.rollback()
                return SELL_ATTRIBUTION_REJECTION

        nav = float(snapshot.deployable_capital)
        market_value = sum(
            position["quantity"] * position["current_price"]
            for position in positions.values()
        )
        # Drawdown high-water mark must ignore pre-re-baseline snapshots: before
        # max_deployable_usd existed, deployable_capital == full account NAV
        # (~776k), so an unfiltered peak makes a capped deployment (e.g. 100k)
        # read as an ~87% phantom drawdown. Only capped-era (post-re-baseline)
        # snapshots represent the current book. Falls back to nav when none.
        peak_nav = float(
            session.scalar(
                select(func.max(CapitalSnapshot.deployable_capital)).where(
                    CapitalSnapshot.account_id == intent.account_id,
                    CapitalSnapshot.mode == intent.mode,
                    CapitalSnapshot.max_deployable_usd.is_not(None),
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
        # Drawdown is judged on real marked book equity, not the capped
        # deployment budget (nav). Position sizing still uses nav below.
        book_equity, book_peak_equity = self._book_equity_from_db(session)
        self._portfolio = PortfolioState(
            nav=nav,
            peak_nav=max(nav, peak_nav),
            positions=positions,
            sector_exposure=sector_exposure,
            total_exposure_pct=(market_value / nav * 100.0 if nav > 0 else 0.0),
            margin_utilization_pct=0.0,
            book_equity=book_equity,
            book_peak_equity=book_peak_equity,
        )
        self._current_prices = {
            ticker: position["current_price"] for ticker, position in positions.items()
        }
        session.rollback()
        return None

    def _book_equity_from_db(self, session: Any) -> tuple[float | None, float | None]:
        """Return (book_equity, book_peak_equity) from the DB, or (None, None).

        Book equity is real cash across sleeves plus mark-to-market positions;
        the peak is the highest daily aggregate equity ever snapshotted. This is
        the denominator the drawdown check needs — unlike ``deployable_capital``,
        it actually falls when the book loses money.
        """
        from shared.position_loader import load_portfolio_state

        try:
            state = load_portfolio_state(session)
        except Exception:  # pragma: no cover - defensive; never block the gate
            self._logger.exception("Book-equity load failed; drawdown falls back to nav")
            return None, None
        return state["nav"], state["peak_nav"]

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

    def _check_settled_usd_funding(
        self, intent: OrderIntent, *, quantity: float, price: float
    ) -> FundingDecision:
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
        settled_cash = snapshot.settled_cash_trading if snapshot is not None else None
        try:
            reservations = self._order_ledger.active_buy_reservations_for_account(
                intent.account_id,
                exclude_recommendation_id=intent.recommendation_id,
                commission_per_share=(
                    self._config.currency.commission_per_share_usd
                ),
                minimum_commission=self._config.currency.minimum_commission_usd,
            )
            fill_spend = self._order_ledger.buy_fill_spend_for_account_since(
                intent.account_id,
                captured_after=(snapshot.captured_at if snapshot is not None else None),
            )
            committed_usd = reservations + fill_spend
        except (TypeError, ValueError):
            committed_usd = math.nan
        finally:
            session.rollback()
        try:
            commission = estimate_commission_usd(
                quantity,
                per_share=self._config.currency.commission_per_share_usd,
                minimum=self._config.currency.minimum_commission_usd,
            )
        except (TypeError, ValueError):
            commission = math.nan
        return check_settled_usd_funding(
            order_notional_usd=quantity * price,
            settled_cash_usd=settled_cash,
            active_reservations_usd=committed_usd,
            estimated_commission_usd=commission,
            minimum_reserve_usd=self._config.currency.minimum_settled_usd_reserve,
        )

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
        # Latch: if already halted, this kill (or the breaker) is a distinct
        # event for an incident whose positions may still be flattening. Re-affirm
        # the halt but do NOT re-liquidate — a second liquidation with a different
        # epoch would mint fresh exit ids and could oversell / flip short.
        was_active = self._kill_switch.is_active
        self._kill_switch.activate(
            reason=kill_msg.reason,
            triggered_by=kill_msg.triggered_by,
            source="kill",
        )
        if was_active:
            self._logger.warning(
                "Kill received while already halted — re-affirmed, not re-liquidating",
                reason=kill_msg.reason,
                triggered_by=kill_msg.triggered_by,
            )
            return

        self._logger.critical(
            "Kill switch activated — liquidating all positions",
            reason=kill_msg.reason,
            triggered_by=kill_msg.triggered_by,
        )

        # Deterministic per-kill epoch: both risk and execution receive the same
        # KillMessage, so exits for one kill converge on the same ledger ids
        # (replay is idempotent) while a later kill re-liquidates.
        epoch = int(kill_msg.timestamp.timestamp())
        await self._liquidate_all(
            epoch=epoch,
            reason=kill_msg.reason,
            triggered_by=kill_msg.triggered_by,
            event_type="kill_switch_activated",
        )

    async def _liquidate_all(
        self,
        *,
        epoch: int,
        reason: str,
        triggered_by: str,
        event_type: str,
    ) -> None:
        """Flatten the whole book. Reloads authoritative positions, routes each
        exit through the ledger with a deterministic id, guards each position so
        one failure never aborts the rest, and always publishes a critical alert.
        """
        try:
            positions = self._authoritative_open_positions()
        except Exception:
            # Never drop a kill/breaker on a transient DB error: the halt is
            # already persisted (fail-closed), so alert critically for manual
            # liquidation rather than letting process_kill raise into the DLQ.
            self._logger.exception(
                "Could not load positions for liquidation", event_type=event_type
            )
            await self._publish_alert(
                event_type="liquidation_failed",
                priority="critical",
                message=(
                    f"{event_type}: could not load positions to liquidate — "
                    "MANUAL ACTION REQUIRED"
                ),
                context={"reason": reason, "triggered_by": triggered_by},
            )
            return
        liquidated = 0
        for pos in positions:
            try:
                if await self._emit_liquidation_exit(pos, epoch=epoch, reason=reason):
                    liquidated += 1
            except Exception:
                self._logger.exception(
                    "Liquidation emit failed for a position; continuing",
                    ticker=pos.get("ticker"),
                )

        # Always alert, even on zero positions or partial failure — the operator
        # must always learn a kill/breaker fired.
        await self._publish_alert(
            event_type=event_type,
            priority="critical",
            message=f"{event_type} by {triggered_by}: {reason}",
            context={
                "triggered_by": triggered_by,
                "reason": reason,
                "positions_seen": len(positions),
                "positions_liquidated": liquidated,
            },
        )

    def _authoritative_open_positions(self) -> list[dict[str, Any]]:
        """Reload open positions from the DB (broker truth), aggregated by
        ticker, so liquidation acts on real holdings — not a stale or empty
        in-memory book (review 1.4). Falls back to the in-memory book when no DB
        session is wired (unit paths)."""
        if self._order_ledger is None:
            return [
                {
                    "ticker": ticker,
                    "quantity": (
                        pos.get("quantity", 0) if isinstance(pos, dict) else 0
                    ),
                    "con_id": None,
                    "account_id": None,
                    "exchange": None,
                    "currency": None,
                    "portfolio": None,
                }
                for ticker, pos in self._portfolio.positions.items()
            ]

        targets = load_liquidation_targets(self._order_ledger.session)
        self._order_ledger.session.rollback()
        return self._merge_by_ticker(targets)

    @staticmethod
    def _merge_by_ticker(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Collapse identity-scoped rows back to one row per ticker for the
        kill path only (KAN-6).

        ``load_liquidation_targets`` returns one row per
        {account, portfolio, con_id} so a stop-loss can exit one sleeve without
        touching another. The kill path cannot use that granularity yet: its
        exit id is ``liq-{mode}-{ticker}-{epoch}``, and the execution service's
        defense-in-depth net derives the identical id from its own ticker-keyed
        book — both sides must agree or a kill double-sells. Feeding two
        same-ticker rows into the emitter would mint one id with two different
        economics, so the second raises ConflictingOrderIntent and that sleeve's
        shares go un-flattened mid-kill.

        A kill flattens the whole book, so summing the ticker is the right
        quantity; the surviving inaccuracy is which sleeve the exit is booked
        against. Fixing that requires an identity-scoped kill id on both
        services (see KAN-7's :func:`exit_intent_id`) — out of scope here.
        """
        merged: dict[str, dict[str, Any]] = {}
        for row in targets:
            existing = merged.get(row["ticker"])
            if existing is None:
                merged[row["ticker"]] = dict(row)
            else:
                existing["quantity"] += row["quantity"]
        return list(merged.values())

    def _liquidation_exit_id(self, ticker: str, epoch: int) -> str:
        return liquidation_exit_id(self._config.mode, ticker, epoch)

    @staticmethod
    def _trading_date() -> date:
        return datetime.now(timezone.utc).date()

    def _exit_targets(self, ticker: str) -> list[dict[str, Any]]:
        """Identity-scoped rows for one ticker, for the recurring exit paths.

        The in-memory ``self._portfolio.positions`` is keyed by ticker and
        carries no broker identity, so a breach detected there cannot be turned
        into an order intent — which is exactly why stop-loss sells published
        with a uuid4 id and dead-lettered. Resolving through
        ``load_liquidation_targets`` (KAN-6) gives each holding its real
        ``{account_id, portfolio, con_id}``, and a ticker held by two sleeves
        comes back as two rows so each sleeve exits its own shares.

        Without a ledger (unit paths, backtests) there is no DB to resolve
        against; fall back to the in-memory quantity with null identity, which
        the emitter publishes unbacked exactly as it did before.
        """
        if self._order_ledger is None:
            pos = self._portfolio.positions.get(ticker)
            quantity = pos.get("quantity", 0) if isinstance(pos, dict) else 0
            return [
                {
                    "ticker": ticker,
                    "quantity": quantity,
                    "con_id": None,
                    "account_id": None,
                    "exchange": None,
                    "currency": None,
                    "portfolio": None,
                }
            ]
        targets = [
            row
            for row in load_liquidation_targets(self._order_ledger.session)
            if row["ticker"] == ticker
        ]
        self._order_ledger.session.rollback()
        return targets

    def _has_pending_sell(self, target: dict[str, Any]) -> bool:
        """True when the ledger already holds an unfinished sell for this scope."""
        if self._order_ledger is None:
            return False
        pending = self._order_ledger.nonterminal_sell_exists(
            account_id=target.get("account_id"),
            portfolio=target.get("portfolio"),
            con_id=target.get("con_id"),
            # KAN-8: an exit that was committed but never published is not in
            # flight — it is waiting for the re-publish sweep that ran at the
            # top of this scan. Counting it as in-flight is what muted the
            # ticker.
            exclude_unpublished_exits=True,
        )
        self._order_ledger.session.rollback()
        return pending

    def _next_exit_id(self, kind: str, target: dict[str, Any]) -> str:
        """Deterministic id for the next exit of ``kind`` on this scope.

        ``seq`` is the number of exits already minted for this
        {kind, scope, day}. Callers reach here only once every earlier exit in
        the family is terminal (the suppression rule holds the nonterminal
        case), so the count names a free id and a same-day re-entry that
        breaches again does not collide with the filled exit.

        The day is the UTC date, which is one-to-one with an NYSE session for
        regular trading hours (00:00 UTC is 20:00 ET, after the close).

        With no con_id — only reachable on the no-ledger fallback, since the
        emitter refuses to publish an unroutable position — the ticker stands in
        for it so two identity-less tickers cannot share one id.
        """
        con_id = target.get("con_id")
        prefix = exit_intent_prefix(
            kind,
            target.get("account_id") or "",
            target.get("portfolio") or "__unscoped__",
            con_id if con_id is not None else target["ticker"],
            self._trading_date(),
        )
        seq = (
            0
            if self._order_ledger is None
            else self._order_ledger.count_intents_with_id_prefix(prefix)
        )
        if self._order_ledger is not None:
            self._order_ledger.session.rollback()
        return f"{prefix}{seq}"

    def _approve_exit_intent(self, intent: OrderIntent) -> None:
        """Commit a risk-side exit intent as APPROVED.

        A replay that adopted an already APPROVED/SUBMITTED/terminal intent is
        a no-op (APPROVED->APPROVED is illegal per ALLOWED_TRANSITIONS). Mirrors
        the metrics side of :meth:`_persist_risk_approval` so a kill-path
        approval is as visible on the operator dashboard as an entry approval.
        """
        approving = OrderStatus(intent.status) is OrderStatus.PROPOSED
        account_id, mode = intent.account_id, intent.mode
        if approving:
            self._order_ledger.transition(
                intent.recommendation_id, OrderStatus.APPROVED
            )
        self._order_ledger.session.commit()
        if approving:
            self._metrics.lifecycle_transitions.labels(
                status=OrderStatus.APPROVED.value
            ).inc()
            self._refresh_lifecycle_metrics(account_id, mode)

    async def _emit_liquidation_exit(
        self, pos: dict[str, Any], *, epoch: int, reason: str
    ) -> bool:
        """Kill/breaker path: flatten one position through the shared emitter.

        Caller #1 of :meth:`_emit_ledgered_exit`. The deterministic id makes a
        replay a no-op; the id scheme itself is unchanged here (KAN-6 revisits
        it), and the default ``risk_adjustments`` keep the published payload
        byte-identical to what shipped before the extraction.
        """
        return await self._emit_ledgered_exit(
            "liq",
            pos,
            exit_id=self._liquidation_exit_id(pos["ticker"], epoch),
            reason=reason,
        )

    async def _emit_ledgered_exit(
        self,
        kind: str,
        target: dict[str, Any],
        *,
        exit_id: str,
        reason: str,
        quantity: float | None = None,
        risk_adjustments: dict[str, Any] | None = None,
        suppress_if_pending: bool = False,
    ) -> bool:
        """Create an APPROVED ledger intent for a risk-side exit and publish it.

        THE single publish site for every risk-side sell. Adding another
        ``self._redis.publish(APPROVED_ORDERS_STREAM, ...)`` anywhere in this
        class is the bug this method exists to prevent: an order published
        without a backing intent is rejected by execution (its first act on an
        approved order is a ledger lookup) and dead-letters silently, which is
        how stop-loss sells went unexecuted for weeks.

        ``kind`` (``"liq"``/``"stop-loss"``/``"passive-trim"``) names the
        mechanism in the logs and in the unroutable alert, so an operator can
        tell which control could not route. ``quantity`` defaults to flattening
        the whole target; a partial exit (passive trim) passes its own.

        The exit-intent lifecycle, end to end (design §T2)::

            breach detected (risk, every scan)
                   |
                   v
            pending sell for {account, portfolio, con_id}? --yes--> emit nothing
                   | no                                              (this scan)
                   v
            create_intent(exit_id)  PROPOSED
                   |                    |
                   |                    +-- same economics --> adopt existing row
                   |                    +-- different       --> unroutable alert,
                   |                                            publish nothing
                   v
            transition -> APPROVED  (risk is the approver of its own exits)
                   v
            publish stream:approved_orders
                   v
            execution: ledger lookup -> submit_exit -> SUBMITTED
                   v
            IB fill -> FILLED (terminal; a later breach may exit again, seq+1)

        Everything above the publish happens in one transaction, so an exit is
        either backed by an APPROVED intent or not published at all. A crash
        between the APPROVED commit and the publish leaves an orphan: KAN-8
        closed that by marking ``published_at`` after the publish
        (:meth:`_publish_approved_exit`), re-publishing anything unmarked at the
        top of the next scan (:meth:`_republish_unpublished_exits`), and
        teaching the suppression rule below to ignore the unpublished.

        ``suppress_if_pending`` turns on the first branch, for the recurring
        controls (stop-loss, passive trim) that re-evaluate the same breach
        every scan. The kill path leaves it off deliberately: a kill must
        flatten the whole book, and skipping a position because a partial trim
        is in flight would leave the rest of it held through the emergency.

        Returns True when an exit was published. The ledger intent is what lets
        execution actually place the sell (a synthetic id with no intent is
        rejected), and a deterministic id makes a replay a no-op.
        """
        if quantity is None:
            quantity = target["quantity"]
        if quantity is None or quantity <= 0:
            return False

        if suppress_if_pending and self._has_pending_sell(target):
            self._logger.info(
                "Exit suppressed: a sell is already outstanding for this scope",
                kind=kind,
                ticker=target["ticker"],
                portfolio=target.get("portfolio"),
                con_id=target.get("con_id"),
                recommendation_id=exit_id,
            )
            return False

        # With a ledger, a missing con_id means we cannot create the backing
        # intent, so execution would reject the exit — the position would go
        # silently un-liquidated while the summary alert claimed success. Flag it
        # for manual action instead of publishing a doomed order.
        if self._order_ledger is not None and target.get("con_id") is None:
            self._logger.critical(
                "Cannot auto-liquidate position: missing con_id",
                kind=kind,
                ticker=target["ticker"],
                quantity=quantity,
            )
            await self._publish_alert(
                event_type="liquidation_unroutable",
                priority="critical",
                message=(
                    f"Cannot auto-liquidate {target['ticker']} ({quantity}): missing "
                    "con_id — manual action required"
                ),
                context={
                    "kind": kind,
                    "ticker": target["ticker"],
                    "quantity": quantity,
                },
            )
            return False

        if self._order_ledger is not None and target.get("con_id") is not None:
            proposal = SimpleNamespace(
                recommendation_id=exit_id,
                account_id=target["account_id"] or "",
                mode=self._config.mode,
                portfolio=target["portfolio"] or "__liquidation__",
                con_id=target["con_id"],
                symbol=target["ticker"],
                exchange=target["exchange"] or "SMART",
                currency=target["currency"] or "USD",
                action="SELL",
                quantity=quantity,
                limit_price=None,
                order_type="MKT",
            )
            try:
                # Risk IS the approver of its own exits: publishing a PROPOSED
                # intent makes execution's record_submission an illegal
                # PROPOSED->SUBMITTED transition *after* the IB order is live.
                # Create and approve in one transaction. A same-economics
                # replay is adopted here (create_intent returns the existing
                # row) and left alone unless it is a pre-fix PROPOSED leftover.
                self._approve_exit_intent(
                    self._order_ledger.create_intent(proposal)
                )
            except ConflictingOrderIntent:
                # Only _ensure_same_economics raises this, so a row for this
                # kill epoch exists but disagrees with the position we are
                # flattening (quantity, con_id, account, ...). Execution submits
                # the published quantity verbatim, so approving that row and
                # publishing anyway would leave a broker order the ledger
                # contradicts — open_order_mismatch, the divergence class that
                # blocks buys. Same call as a missing con_id: flag it loudly and
                # publish nothing, rather than place an order we cannot back.
                self._order_ledger.session.rollback()
                self._logger.critical(
                    "Cannot auto-liquidate position: exit intent conflicts",
                    kind=kind,
                    ticker=target["ticker"],
                    quantity=quantity,
                    recommendation_id=exit_id,
                )
                await self._publish_alert(
                    event_type="liquidation_unroutable",
                    priority="critical",
                    message=(
                        f"Cannot auto-liquidate {target['ticker']} ({quantity}): "
                        "exit intent conflicts on economics — manual action "
                        "required"
                    ),
                    context={
                        "kind": kind,
                        "ticker": target["ticker"],
                        "quantity": quantity,
                        "recommendation_id": exit_id,
                    },
                )
                return False
            except Exception:
                self._order_ledger.session.rollback()
                raise

        order = ApprovedOrderMessage(
            ticker=target["ticker"],
            timestamp=datetime.now(timezone.utc),
            action="sell",
            quantity=quantity,
            order_type="market",
            limit_price=None,
            recommendation_id=exit_id,
            risk_adjustments=(
                {"kill_switch": True, "reason": reason}
                if risk_adjustments is None
                else risk_adjustments
            ),
        )
        await self._publish_approved_exit(order)
        self._logger.info(
            "Liquidation order published",
            kind=kind,
            ticker=target["ticker"],
            quantity=quantity,
            recommendation_id=exit_id,
        )
        return True

    async def _publish_approved_exit(self, order: ApprovedOrderMessage) -> None:
        """Put an already-APPROVED exit on the stream and record that it went.

        THE publish site for risk-side sells — both the emitter and the
        re-publish sweep go through here, so there is one place where "on the
        stream" and ``published_at`` are kept in step.

        The order of the two statements is the whole point (KAN-8). Publishing
        first and marking second means a crash in between leaves an intent that
        looks unpublished but is not: the next sweep re-publishes it, execution
        recognises the id and no-ops, and the mark finally lands. Marking first
        would produce the opposite, unrecoverable orphan — an intent the ledger
        swears is published that execution never saw and no sweep will revisit.
        """
        await self._redis.publish(APPROVED_ORDERS_STREAM, order.to_stream_dict())
        if self._order_ledger is None:
            return
        self._order_ledger.mark_published(order.recommendation_id)
        self._order_ledger.session.commit()

    async def _republish_unpublished_exits(self) -> None:
        """Re-publish exits that were committed but never reached the stream.

        Runs at the top of every periodic scan, before any breach is evaluated,
        because the suppression rule downstream needs the answer to "is an exit
        in flight?" to be true or false — not "yes, but nobody has it".

        Only exits approved *today* are re-published. An exit is a decision
        about a price that has since moved on: re-publishing one approved days
        ago would fire a stale stop-loss at today's market, and if the position
        was flattened by hand in the meantime the fill goes short. Older
        orphans are escalated instead (:meth:`_alert_stale_orphans`) and left
        inert — they still do not suppress, so today's breach gets a fresh,
        correctly-sized exit rather than a stale one.

        Fail-fast on purpose: if a publish raises, the exception propagates and
        aborts the whole scan (``maybe_run_periodic_checks`` logs it and the
        service carries on). Swallowing it and continuing to the breach
        evaluation would be the dangerous option — the orphan would no longer
        suppress, so the stop-loss would mint a *second* exit intent for the
        same shares, and both would sell once the stream came back.
        """
        if self._order_ledger is None:
            return
        orphans = self._order_ledger.unpublished_exit_intents(
            mode=self._config.mode
        )
        today = self._trading_date()
        cutoff = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
        pending: list[tuple[str, ApprovedOrderMessage]] = []
        stale: list[dict[str, Any]] = []
        for intent in orphans:
            if self._approved_before(intent, cutoff):
                stale.append(
                    {
                        "recommendation_id": intent.recommendation_id,
                        "ticker": intent.symbol,
                        "quantity": float(intent.requested_quantity),
                    }
                )
                continue
            pending.append(
                (
                    intent.recommendation_id,
                    ApprovedOrderMessage(
                        ticker=intent.symbol,
                        timestamp=datetime.now(timezone.utc),
                        action=intent.action.lower(),
                        quantity=float(intent.requested_quantity),
                        # Always market: the clause selects SELLs only, every
                        # risk exit is created MKT, and execution routes every
                        # non-buy through submit_exit as a market order anyway.
                        order_type="market",
                        limit_price=None,
                        recommendation_id=intent.recommendation_id,
                        # The original risk_adjustments are not persisted, and
                        # execution reads none of them (they are operator-facing
                        # provenance). Say plainly what this message is instead
                        # of guessing which control minted it.
                        risk_adjustments={
                            "republished": True,
                            "reason": intent.reason or "unpublished exit intent",
                        },
                        portfolio=intent.portfolio,
                    ),
                )
            )
        self._order_ledger.session.rollback()

        await self._alert_stale_orphans(stale)

        if not pending:
            self._republished_exit_ids = set()
            return

        repeated = sorted(
            self._republished_exit_ids.intersection(exit_id for exit_id, _ in pending)
        )
        self._republished_exit_ids = {exit_id for exit_id, _ in pending}
        # Alert *before* the publishes, not after. The failure this names —
        # publishes that are not sticking — is exactly the one where the loop
        # below raises, so an alert underneath it would never be reached and
        # a permanently broken sweep would page nobody.
        if repeated:
            await self._publish_alert(
                event_type="exit_republish_repeated",
                priority="high",
                message=(
                    "Exit intents needed re-publishing on two consecutive "
                    f"scans: {', '.join(repeated)} — publishes are not "
                    "sticking, manual investigation required"
                ),
                context={"recommendation_ids": repeated},
            )
        for exit_id, order in pending:
            self._logger.warning(
                "Re-publishing an exit intent that was never published",
                ticker=order.ticker,
                quantity=order.quantity,
                recommendation_id=exit_id,
            )
            await self._publish_approved_exit(order)

    @staticmethod
    def _approved_before(intent: OrderIntent, cutoff: datetime) -> bool:
        """True when this intent was approved before ``cutoff``.

        A missing ``approved_at`` (pre-KAN-4 rows) counts as before: an exit
        that cannot be dated is exactly the one not to fire automatically.
        Sqlite hands back naive datetimes for a ``DateTime(timezone=True)``
        column, so normalise to UTC before comparing.
        """
        approved_at = intent.approved_at
        if approved_at is None:
            return True
        if approved_at.tzinfo is None:
            approved_at = approved_at.replace(tzinfo=timezone.utc)
        return approved_at < cutoff

    async def _alert_stale_orphans(self, stale: list[dict[str, Any]]) -> None:
        """Escalate orphaned exits too old to fire, once per intent.

        These are the rows the sweep deliberately will not publish. They need a
        human — flatten by hand, or terminalise the intent — so they alert at
        critical, but only when newly seen: an orphan nobody has cleared would
        otherwise re-page every scan, which is how alerts get muted.
        """
        fresh = [
            row
            for row in stale
            if row["recommendation_id"] not in self._stale_orphan_ids
        ]
        self._stale_orphan_ids = {row["recommendation_id"] for row in stale}
        if not fresh:
            return
        names = ", ".join(
            f"{row['ticker']} ({row['recommendation_id']})" for row in fresh
        )
        self._logger.critical(
            "Unpublished exit intents are too old to re-publish",
            recommendation_ids=[row["recommendation_id"] for row in fresh],
        )
        await self._publish_alert(
            event_type="exit_orphan_stale",
            priority="critical",
            message=(
                f"Exit intents approved before today were never published: {names} "
                "— too old to fire automatically, manual action required"
            ),
            context={"orphans": fresh},
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
            # IMPORTANT fix (T6 review): the other half of "risk breaches" —
            # lifecycle_transitions{status=RISK_REJECTED} only covers new
            # orders rejected at entry, not a breach on an already-held
            # position. Single line, smallest footprint on this file.
            self._metrics.risk_breach_total.labels(breach_type=breach.action_type).inc()
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
            # A hard-ceiling breach on a real position is auto-trimmed back to
            # the soft ceiling; a soft breach is advisory only. The margin
            # sentinel ("__margin__") has no single position to sell, so it
            # stays alert-only until margin trimming is designed.
            if breach.action_type == "trim" and breach.ticker != "__margin__":
                await self._trim_position_to_target(breach)

        if breaches:
            self._logger.info(
                "Passive scan completed",
                breach_count=len(breaches),
            )

    async def _trim_position_to_target(self, breach: Any) -> None:
        """Trim a hard-ceiling breach back to its target, one sleeve at a time.

        The breach is measured on the ticker (the ceiling is a share of NAV, and
        NAV does not care which sleeve holds what), but the sell cannot be: an
        order needs a con_id and an account, and a ticker held twice has two of
        each. So the ticker-level overage is computed once and then split across
        the scopes pro rata to what each actually holds — selling the ticker
        total out of one sleeve would flatten a holding that was never in
        breach.
        """
        price = self._current_prices.get(breach.ticker, 0.0)
        nav = self._portfolio.nav
        if price <= 0 or nav <= 0:
            return

        targets = self._exit_targets(breach.ticker)
        held = sum(t["quantity"] or 0.0 for t in targets)
        if held <= 0:
            return

        target_value = nav * breach.target_pct / 100.0
        overage = round((held * price - target_value) / price, 4)
        if overage <= 0:
            return

        for target in targets:
            share = round(overage * (target["quantity"] / held), 4)
            if share <= 0:
                continue
            published = await self._emit_ledgered_exit(
                "passive-trim",
                target,
                exit_id=self._next_exit_id("passive-trim", target),
                reason=breach.message,
                quantity=share,
                risk_adjustments={
                    "passive_trim": True,
                    "target_pct": breach.target_pct,
                    "current_pct": breach.current_pct,
                },
                suppress_if_pending=True,
            )
            if published:
                self._logger.warning(
                    "Hard-ceiling auto-trim order published",
                    ticker=breach.ticker,
                    portfolio=target.get("portfolio"),
                    sell_quantity=share,
                    current_pct=breach.current_pct,
                    target_pct=breach.target_pct,
                )

    async def run_stop_loss_check(self) -> None:
        """Check all positions for trailing stop-loss triggers.

        The trigger is a price fact, so it is evaluated once per ticker against
        the trailing high. The *exit* is per identity scope: each sleeve holding
        the ticker is flattened through its own ledgered intent, because that is
        the only form execution can route.
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
            if decision.approved:
                continue

            for target in self._exit_targets(ticker):
                published = await self._emit_ledgered_exit(
                    "stop-loss",
                    target,
                    exit_id=self._next_exit_id("stop-loss", target),
                    reason=decision.reason,
                    risk_adjustments={
                        "stop_loss": True,
                        "reason": decision.reason,
                    },
                    suppress_if_pending=True,
                )
                if not published:
                    # Suppressed (a sell is already working) or unroutable — the
                    # emitter has already logged/alerted. Raising the high-
                    # priority trigger alert anyway would page every scan for as
                    # long as the breach lasts, which is how alerts get muted.
                    continue
                await self._publish_alert(
                    event_type="stop_loss_triggered",
                    priority="high",
                    message=decision.reason,
                    context={
                        "ticker": ticker,
                        "quantity": target["quantity"],
                        "portfolio": target.get("portfolio"),
                    },
                )

    async def run_periodic_risk_checks(self) -> None:
        """Drive the intraday safety controls the daily sleeve run does not.

        Refresh marks from the DB, fire trailing stop-losses, auto-trim
        hard-ceiling breaches, and raise a drawdown gauge measured on real book
        equity. These mechanisms exist and are tested but had zero live callers
        before this driver (review findings 2.1–2.3).
        """
        # Reconcile the durable halt first: adopt an out-of-band halt, or resume
        # after the admin clear endpoint has cleared it in the DB.
        self._kill_switch.sync_from_store()
        self._refresh_portfolio_from_db()
        # Recover orphaned exits before evaluating anything new: an intent that
        # is committed but unpublished neither reaches execution nor suppresses,
        # so the sweep has to settle it before the breach checks read the ledger.
        await self._republish_unpublished_exits()
        await self.run_stop_loss_check()
        await self.run_passive_scan()
        await self._emit_drawdown_gauge()
        await self._check_dlq_depths()

    async def maybe_run_periodic_checks(self, now: float) -> bool:
        """Run the periodic checks if the scan interval has elapsed.

        ``now`` is a monotonic timestamp (seconds). Returns True when the checks
        ran. The first call always runs.
        """
        last = self._last_periodic_scan_at
        if (
            last is not None
            and (now - last) < self._passive_scan_interval_seconds
        ):
            return False
        self._last_periodic_scan_at = now
        # The sweep is best-effort: a failure here must never propagate and tear
        # down the main run() loop, which would halt recommendation, kill, and
        # fill processing for the whole service.
        try:
            await self.run_periodic_risk_checks()
        except Exception:
            self._logger.exception("Periodic risk checks failed; continuing")
        return True

    def _refresh_portfolio_from_db(self) -> None:
        """Reload positions, marks, cash and book equity from the DB.

        The risk service has no market-data feed, so the freshest available
        marks are the ones the data pipeline has written to ``positions``. This
        refresh lets the periodic stop-loss and ceiling scan act on them. It
        no-ops without a DB session (unit tests drive the in-memory book
        directly).
        """
        if self._db_session is None:
            return
        from shared.position_loader import load_portfolio_state

        try:
            state = load_portfolio_state(self._db_session)
            positions = state["positions"]
            book_equity = float(state["nav"])
            book_peak = float(state["peak_nav"])
            # The ceiling / sizing denominator stays on the deployment budget,
            # consistent with the entry path (_refresh_risk_state). Only the
            # drawdown gauge (book_equity/book_peak) moves to real book equity.
            snapshot = self._db_session.scalar(
                select(CapitalSnapshot)
                .where(CapitalSnapshot.mode == self._config.mode)
                .order_by(
                    CapitalSnapshot.captured_at.desc(), CapitalSnapshot.id.desc()
                )
                .limit(1)
            )
            nav = (
                float(snapshot.deployable_capital)
                if snapshot is not None
                else book_equity
            )
            market_value = sum(
                p["quantity"] * p["current_price"] for p in positions.values()
            )
            self._cash = nav - market_value
            self._current_prices = {
                ticker: p["current_price"] for ticker, p in positions.items()
            }
            self._portfolio = PortfolioState(
                nav=nav,
                peak_nav=nav,
                positions=positions,
                sector_exposure=state["sector_exposure"],
                total_exposure_pct=(
                    market_value / nav * 100.0 if nav > 0 else 0.0
                ),
                margin_utilization_pct=0.0,
                book_equity=book_equity,
                book_peak_equity=book_peak,
            )
        except Exception:  # pragma: no cover - defensive
            self._logger.exception(
                "Periodic portfolio refresh failed; using last in-memory state"
            )
        finally:
            try:
                self._db_session.rollback()
            except Exception:  # pragma: no cover - defensive
                pass

    async def _emit_drawdown_gauge(self) -> None:
        """Act on a real book-equity drawdown breach.

        The pause already rejects new buys inside ``process_recommendation``; here
        it raises a high alert. The 20% circuit breaker **liquidates** — it halts
        (fail-closed, persisted) and flattens the book, once per incident.
        """
        decision = self._engine.check_portfolio_drawdown(self._portfolio)
        if decision.approved:
            return
        breaker = "circuit breaker" in decision.reason.lower()
        if breaker:
            # Fire once per halt incident: an already-active (persisted) halt
            # means we have already liquidated — don't re-sell every scan.
            if not self._kill_switch.is_active:
                self._kill_switch.activate(
                    reason=decision.reason,
                    triggered_by="circuit_breaker",
                    source="circuit_breaker",
                )
                activated = self._kill_switch.activated_at or datetime.now(
                    timezone.utc
                )
                await self._liquidate_all(
                    epoch=int(activated.timestamp()),
                    reason=decision.reason,
                    triggered_by="circuit_breaker",
                    event_type="circuit_breaker_liquidation",
                )
                # Also drive execution's kill path so it cancels resting orders
                # (a liquidation that leaves working buys is not one). Same
                # timestamp -> execution derives the same epoch/exit ids and
                # dedups against the exits just published above. Risk re-consumes
                # this but latches (was_active) instead of re-liquidating.
                await self._redis.publish(
                    KILL_STREAM,
                    KillMessage(
                        timestamp=activated,
                        triggered_by="circuit_breaker",
                        reason=decision.reason,
                    ).to_stream_dict(),
                )
            return
        await self._publish_alert(
            event_type="drawdown_pause",
            priority="high",
            message=decision.reason,
            context={
                "book_equity": self._portfolio.book_equity,
                "book_peak_equity": self._portfolio.book_peak_equity,
            },
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
        """Look up the sector for a ticker.

        Prefers the held position's stored sector; falls back to the shared
        universe maps for new tickers (or legacy NULL-sector rows), so a buy
        of an unheld ticker is judged against its real sector instead of an
        "Unknown" bucket shared with the whole book.
        """
        pos = self._portfolio.positions.get(ticker, {})
        if isinstance(pos, dict):
            sector = pos.get("sector")
            if sector and sector != "Unknown":
                return sector
        return lookup_sector(ticker)

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
                # T6: heartbeat for the container healthcheck — see docker-compose.yml.
                write_heartbeat()
                # Intraday safety sweep: stop-loss, hard-ceiling trim, drawdown
                # gauge — gated to the passive-scan interval on a monotonic clock.
                await self.maybe_run_periodic_checks(
                    asyncio.get_running_loop().time()
                )

                # A retryable state error on a durable sell must stay pending;
                # any other error is a poison message → DLQ + ack + alert.
                await self._consume_and_process(
                    RECOMMENDATIONS_STREAM,
                    RecommendationMessage.from_stream_dict,
                    self.process_recommendation,
                    count=10,
                    block_ms=2000,
                    retryable_exc=(RetryableRiskStateError,),
                )
                await self._consume_and_process(
                    KILL_STREAM,
                    KillMessage.from_stream_dict,
                    self.process_kill,
                    count=1,
                    block_ms=500,
                )
                await self._consume_and_process(
                    FILLS_STREAM,
                    FillMessage.from_stream_dict,
                    self.process_fill,
                    count=10,
                    block_ms=500,
                )
        except (KeyboardInterrupt, Exception):
            self._logger.info("Risk management service interrupted")

    async def _consume_and_process(
        self,
        stream: str,
        parser: Any,
        handler: Any,
        *,
        count: int,
        block_ms: int,
        retryable_exc: tuple[type[BaseException], ...] = (),
    ) -> None:
        """Read a batch and process each message with steady-state poison
        discipline: ack on success, leave pending on a retryable error, and
        DLQ + ack + alert on any other (poison) error — so a non-retryable
        message is never silently parked in the PEL."""
        messages = await self._redis.read_group(
            stream, CONSUMER_GROUP, CONSUMER_NAME, count=count, block_ms=block_ms
        )
        for msg in messages:
            try:
                await handler(parser(msg.data))
            except retryable_exc as exc:
                self._logger.warning(
                    "Retryable error; leaving message pending",
                    stream=stream,
                    message_id=msg.message_id,
                    reason=str(exc),
                )
                continue
            except Exception as exc:
                await self._dead_letter(stream, msg, exc)
                continue
            # Ack is separate from the poison path: a transient ack failure after
            # a successful handler must NOT dead-letter an already-processed
            # message — redelivery + idempotency (execution_id dedup, ledger)
            # handles the reprocess safely.
            try:
                await self._redis.ack(stream, CONSUMER_GROUP, msg.message_id)
            except Exception:
                self._logger.exception(
                    "Ack failed after processing; relying on redelivery + "
                    "idempotency",
                    stream=stream,
                    message_id=msg.message_id,
                )

    async def _check_dlq_depths(self) -> None:
        """Alert when any consumed stream's dead-letter queue has a backlog, so
        parked poison messages are noticed rather than accumulating silently."""
        from shared.redis_client import DEAD_LETTER_SUFFIX

        for stream in (RECOMMENDATIONS_STREAM, KILL_STREAM, FILLS_STREAM):
            dlq = stream + DEAD_LETTER_SUFFIX
            try:
                depth = await self._redis.stream_length(dlq)
            except Exception:  # pragma: no cover - defensive (dlq may not exist)
                continue
            # Alert only on a new or growing backlog; reset once it clears so the
            # next backlog re-alerts. Avoids re-alerting every scan on a static
            # backlog.
            if depth and depth > self._dlq_alerted_depth.get(dlq, 0):
                await self._publish_alert(
                    event_type="dlq_backlog",
                    priority="high",
                    message=f"Dead-letter backlog on {dlq}: {depth} message(s)",
                    context={"stream": dlq, "depth": depth},
                )
                self._dlq_alerted_depth[dlq] = depth
            elif not depth:
                self._dlq_alerted_depth[dlq] = 0

    async def _dead_letter(self, stream: str, msg: Any, exc: Exception) -> None:
        """DLQ + ack + alert a poison message (matches setup()'s replay path)."""
        self._logger.exception(
            "Poison message; sending to DLQ",
            stream=stream,
            message_id=msg.message_id,
        )
        try:
            await self._redis.send_to_dead_letter(stream, msg, str(exc))
            await self._redis.ack(stream, CONSUMER_GROUP, msg.message_id)
        except Exception:
            self._logger.exception(
                "Failed to dead-letter poison message",
                stream=stream,
                message_id=msg.message_id,
            )
        await self._publish_alert(
            event_type="poison_message",
            priority="high",
            message=f"Poison message on {stream} dead-lettered: {exc}",
            context={"stream": stream, "message_id": str(msg.message_id)},
        )


if __name__ == "__main__":
    import asyncio

    from shared.config import load_config

    config = load_config("config/default.yaml")

    async def main() -> None:
        import redis.asyncio as aioredis
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from shared.observability import setup_metrics
        from shared.redis_client import RedisStreamClient

        setup_metrics("risk-management", port=config.observability.prometheus_port)
        register_heartbeat_collector()

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
