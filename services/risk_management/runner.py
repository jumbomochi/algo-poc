from __future__ import annotations

import asyncio
import math
import uuid
from datetime import datetime, timedelta, timezone
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
from shared.liquidation import liquidation_exit_id, load_liquidation_targets
from shared.logging import get_logger
from shared.models import CapitalSnapshot, OrderIntent, OrderStatus, Position
from shared.order_ledger import (
    ConflictingOrderIntent,
    OrderIntentNotFound,
    OrderLedger,
)
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
        positions = self._authoritative_open_positions()
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
        return targets

    def _liquidation_exit_id(self, ticker: str, epoch: int) -> str:
        return liquidation_exit_id(self._config.mode, ticker, epoch)

    async def _emit_liquidation_exit(
        self, pos: dict[str, Any], *, epoch: int, reason: str
    ) -> bool:
        """Create the deterministic exit intent (idempotent) and publish it.

        Returns True when an exit was published. The ledger intent is what lets
        execution actually place the sell (a synthetic id with no intent is
        rejected), and the deterministic id makes a replay a no-op.
        """
        quantity = pos["quantity"]
        if quantity is None or quantity <= 0:
            return False
        exit_id = self._liquidation_exit_id(pos["ticker"], epoch)

        # With a ledger, a missing con_id means we cannot create the backing
        # intent, so execution would reject the exit — the position would go
        # silently un-liquidated while the summary alert claimed success. Flag it
        # for manual action instead of publishing a doomed order.
        if self._order_ledger is not None and pos.get("con_id") is None:
            self._logger.critical(
                "Cannot auto-liquidate position: missing con_id",
                ticker=pos["ticker"],
                quantity=quantity,
            )
            await self._publish_alert(
                event_type="liquidation_unroutable",
                priority="critical",
                message=(
                    f"Cannot auto-liquidate {pos['ticker']} ({quantity}): missing "
                    "con_id — manual action required"
                ),
                context={"ticker": pos["ticker"], "quantity": quantity},
            )
            return False

        if self._order_ledger is not None and pos.get("con_id") is not None:
            proposal = SimpleNamespace(
                recommendation_id=exit_id,
                account_id=pos["account_id"] or "",
                mode=self._config.mode,
                portfolio=pos["portfolio"] or "__liquidation__",
                con_id=pos["con_id"],
                symbol=pos["ticker"],
                exchange=pos["exchange"] or "SMART",
                currency=pos["currency"] or "USD",
                action="SELL",
                quantity=quantity,
                limit_price=None,
                order_type="MKT",
            )
            try:
                self._order_ledger.create_intent(proposal)
                self._order_ledger.session.commit()
            except ConflictingOrderIntent:
                # An intent for this deterministic id already exists (a replay or
                # the concurrent execution-side net). Keep it — that is exactly
                # the idempotency guarantee.
                self._order_ledger.session.rollback()
            except Exception:
                self._order_ledger.session.rollback()
                raise

        order = ApprovedOrderMessage(
            ticker=pos["ticker"],
            timestamp=datetime.now(timezone.utc),
            action="sell",
            quantity=quantity,
            order_type="market",
            limit_price=None,
            recommendation_id=exit_id,
            risk_adjustments={"kill_switch": True, "reason": reason},
        )
        await self._redis.publish(APPROVED_ORDERS_STREAM, order.to_stream_dict())
        self._logger.info(
            "Liquidation order published",
            ticker=pos["ticker"],
            quantity=quantity,
            recommendation_id=exit_id,
        )
        return True

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
        """Emit a market sell that reduces a position to its target ceiling."""
        pos = self._portfolio.positions.get(breach.ticker)
        price = self._current_prices.get(breach.ticker, 0.0)
        nav = self._portfolio.nav
        if pos is None or price <= 0 or nav <= 0:
            return
        quantity = pos.get("quantity", 0) if isinstance(pos, dict) else 0
        if quantity <= 0:
            return
        target_value = nav * breach.target_pct / 100.0
        sell_quantity = round((quantity * price - target_value) / price, 4)
        if sell_quantity <= 0:
            return

        order = ApprovedOrderMessage(
            ticker=breach.ticker,
            timestamp=datetime.now(timezone.utc),
            action="sell",
            quantity=sell_quantity,
            order_type="market",
            limit_price=None,
            recommendation_id=f"passive-trim-{uuid.uuid4()}",
            risk_adjustments={
                "passive_trim": True,
                "target_pct": breach.target_pct,
                "current_pct": breach.current_pct,
            },
        )
        await self._redis.publish(APPROVED_ORDERS_STREAM, order.to_stream_dict())
        self._logger.warning(
            "Hard-ceiling auto-trim order published",
            ticker=breach.ticker,
            sell_quantity=sell_quantity,
            current_pct=breach.current_pct,
            target_pct=breach.target_pct,
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
        await self.run_stop_loss_check()
        await self.run_passive_scan()
        await self._emit_drawdown_gauge()

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
                # Intraday safety sweep: stop-loss, hard-ceiling trim, drawdown
                # gauge — gated to the passive-scan interval on a monotonic clock.
                await self.maybe_run_periodic_checks(
                    asyncio.get_running_loop().time()
                )

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
