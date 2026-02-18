from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from services.risk_management.correlation import CorrelationMonitor
from services.risk_management.engine import PortfolioState, RiskDecision, RiskEngine
from services.risk_management.kill_switch import KillSwitch
from services.risk_management.passive_monitor import PassiveBreachMonitor
from shared.config import AppConfig
from shared.logging import get_logger
from shared.schemas.messages import (
    AlertMessage,
    ApprovedOrderMessage,
    KillMessage,
    RecommendationMessage,
)

RECOMMENDATIONS_STREAM = "stream:recommendations"
APPROVED_ORDERS_STREAM = "stream:approved_orders"
ALERTS_STREAM = "stream:alerts"
KILL_STREAM = "stream:kill"

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
    ) -> None:
        self._config = config
        self._redis = redis_client
        self._logger = get_logger("risk_management")

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

        # Portfolio state — in production this would be loaded from DB
        self._portfolio = PortfolioState(
            nav=0.0,
            peak_nav=0.0,
            positions={},
            sector_exposure={},
            total_exposure_pct=0.0,
            margin_utilization_pct=0.0,
        )
        self._current_prices: dict[str, float] = {}

    async def setup(self) -> None:
        """Create consumer groups for the streams we subscribe to."""
        await self._redis.create_consumer_group(
            RECOMMENDATIONS_STREAM, CONSUMER_GROUP
        )
        await self._redis.create_consumer_group(KILL_STREAM, CONSUMER_GROUP)
        self._logger.info("Risk service consumer groups created")

    async def process_recommendation(
        self, rec: RecommendationMessage
    ) -> None:
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

        # 1. Kill switch — highest precedence
        kill_decision = self._kill_switch.check()
        if not kill_decision.approved:
            self._logger.warning(
                "Kill switch rejected recommendation",
                ticker=rec.ticker,
                reason=kill_decision.reason,
            )
            await self._publish_alert(
                event_type="kill_switch_rejection",
                priority="critical",
                message=f"Kill switch rejected {rec.action} {rec.ticker}: {kill_decision.reason}",
                context={"ticker": rec.ticker, "recommendation_id": rec.recommendation_id},
            )
            return

        # 2. Portfolio drawdown check (for buys only)
        if rec.action == "buy":
            drawdown_decision = self._engine.check_portfolio_drawdown(self._portfolio)
            if not drawdown_decision.approved:
                self._logger.warning(
                    "Drawdown check rejected buy",
                    ticker=rec.ticker,
                    reason=drawdown_decision.reason,
                )
                await self._publish_alert(
                    event_type="drawdown_rejection",
                    priority="high",
                    message=f"Drawdown rejected buy {rec.ticker}: {drawdown_decision.reason}",
                    context={"ticker": rec.ticker, "recommendation_id": rec.recommendation_id},
                )
                return

        # 3. Entry compliance check (for buys)
        risk_adjustments: dict[str, Any] = {}
        quantity: int = 0

        if rec.action == "buy":
            price = self._current_prices.get(rec.ticker, 0.0)
            sector = self._get_sector(rec.ticker)
            # Default quantity estimation based on config position limit
            default_qty = self._estimate_buy_quantity(rec.ticker, price)

            entry_decision = self._engine.check_entry(
                ticker=rec.ticker,
                quantity=default_qty,
                price=price,
                sector=sector,
                portfolio=self._portfolio,
            )

            if not entry_decision.approved:
                self._logger.warning(
                    "Entry check rejected buy",
                    ticker=rec.ticker,
                    reason=entry_decision.reason,
                )
                await self._publish_alert(
                    event_type="entry_rejection",
                    priority="medium",
                    message=f"Entry rejected buy {rec.ticker}: {entry_decision.reason}",
                    context={"ticker": rec.ticker, "recommendation_id": rec.recommendation_id},
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
            # For sells, use the position quantity
            pos = self._portfolio.positions.get(rec.ticker, {})
            quantity = pos.get("quantity", 0) if isinstance(pos, dict) else 0
            if quantity <= 0:
                quantity = 1  # minimum sell quantity

        # 4. Publish approved order
        order = ApprovedOrderMessage(
            ticker=rec.ticker,
            timestamp=datetime.now(timezone.utc),
            action=rec.action,
            quantity=quantity,
            order_type="limit" if rec.action == "buy" else "market",
            limit_price=self._current_prices.get(rec.ticker) if rec.action == "buy" else None,
            recommendation_id=rec.recommendation_id,
            risk_adjustments=risk_adjustments,
        )

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
                        await self._redis.ack(KILL_STREAM, CONSUMER_GROUP, msg.message_id)
                    except Exception:
                        self._logger.exception(
                            "Error processing kill message", message_id=msg.message_id
                        )
        except (KeyboardInterrupt, Exception):
            self._logger.info("Risk management service interrupted")


if __name__ == "__main__":
    import asyncio

    from shared.config import load_config

    config = load_config("config/default.yaml")

    async def main() -> None:
        import redis.asyncio as aioredis

        from shared.redis_client import RedisStreamClient

        redis_conn = aioredis.from_url(config.redis.url)
        redis_client = RedisStreamClient(redis_conn)
        runner = RiskServiceRunner(config=config, redis_client=redis_client)
        await runner.run()

    asyncio.run(main())
