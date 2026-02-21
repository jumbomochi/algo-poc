from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RiskDecision:
    """Result of a risk check."""

    approved: bool
    reason: str
    adjusted_quantity: int = 0


@dataclass
class PortfolioState:
    """Snapshot of current portfolio metrics used by risk checks."""

    nav: float
    peak_nav: float
    positions: dict[str, Any]
    sector_exposure: dict[str, float]
    total_exposure_pct: float
    margin_utilization_pct: float


class RiskEngine:
    """Core risk engine that gates trade entries and monitors portfolio health.

    Decision precedence (highest to lowest):
    1. Kill switch / circuit breaker
    2. Critical margin protection
    3. Stop-loss exits
    4. Hard compliance constraints (position, sector, total exposure)
    5. Soft/advisory controls
    """

    def __init__(
        self,
        position_entry_limit_pct: float = 5.0,
        sector_concentration_pct: float = 20.0,
        total_exposure_limit_pct: float = 150.0,
        stop_loss_trailing_pct: float = 15.0,
        drawdown_pause_pct: float = 10.0,
        drawdown_circuit_breaker_pct: float = 20.0,
        max_lots_per_ticker: int | None = None,
    ):
        self.position_entry_limit_pct = position_entry_limit_pct
        self.sector_concentration_pct = sector_concentration_pct
        self.total_exposure_limit_pct = total_exposure_limit_pct
        self.stop_loss_trailing_pct = stop_loss_trailing_pct
        self.drawdown_pause_pct = drawdown_pause_pct
        self.drawdown_circuit_breaker_pct = drawdown_circuit_breaker_pct
        self.max_lots_per_ticker = max_lots_per_ticker

    def check_entry(
        self,
        ticker: str,
        quantity: int,
        price: float,
        sector: str,
        portfolio: PortfolioState,
        existing_lots: int = 0,
    ) -> RiskDecision:
        """Check whether a proposed entry trade is allowed.

        Checks (in precedence order):
        1. Total exposure limit
        2. Max lots per ticker
        3. Sector concentration limit
        4. Position entry limit (scales down if over)

        Returns:
            RiskDecision with approved=True and adjusted_quantity, or
            approved=False with reason.
        """
        # Check total exposure first
        if portfolio.total_exposure_pct >= self.total_exposure_limit_pct:
            return RiskDecision(
                approved=False,
                reason=(
                    f"Total exposure {portfolio.total_exposure_pct:.1f}% "
                    f"exceeds limit {self.total_exposure_limit_pct:.1f}%"
                ),
                adjusted_quantity=0,
            )

        # Check max lots per ticker
        if self.max_lots_per_ticker is not None and existing_lots >= self.max_lots_per_ticker:
            return RiskDecision(
                approved=False,
                reason=(
                    f"Max lots per ticker reached for {ticker}: "
                    f"{existing_lots} >= {self.max_lots_per_ticker}"
                ),
                adjusted_quantity=0,
            )

        # Check sector concentration
        current_sector_pct = portfolio.sector_exposure.get(sector, 0.0)
        if current_sector_pct >= self.sector_concentration_pct:
            return RiskDecision(
                approved=False,
                reason=(
                    f"Sector '{sector}' exposure {current_sector_pct:.1f}% "
                    f"exceeds concentration limit {self.sector_concentration_pct:.1f}%"
                ),
                adjusted_quantity=0,
            )

        # Check position entry limit and scale down if needed
        max_position_value = portfolio.nav * (self.position_entry_limit_pct / 100.0)
        proposed_value = quantity * price
        if proposed_value > max_position_value:
            adjusted_quantity = int(math.floor(max_position_value / price))
            if adjusted_quantity <= 0:
                return RiskDecision(
                    approved=False,
                    reason=(
                        f"Position value exceeds {self.position_entry_limit_pct:.1f}% "
                        f"of NAV and cannot scale down"
                    ),
                    adjusted_quantity=0,
                )
            return RiskDecision(
                approved=True,
                reason=(
                    f"Scaled down from {quantity} to {adjusted_quantity} shares "
                    f"to stay within {self.position_entry_limit_pct:.1f}% position limit"
                ),
                adjusted_quantity=adjusted_quantity,
            )

        return RiskDecision(
            approved=True,
            reason="Entry approved",
            adjusted_quantity=quantity,
        )

    def check_stop_loss(
        self,
        ticker: str,
        current_price: float,
        highest_price_since_entry: float,
        stop_loss_trailing_pct: float | None = None,
    ) -> RiskDecision:
        """Check whether a trailing stop-loss should trigger.

        Args:
            ticker: Stock ticker.
            current_price: Current market price.
            highest_price_since_entry: Highest price observed since position was opened.
            stop_loss_trailing_pct: Override for trailing stop percentage.
                If None, uses the engine default.

        Returns:
            RiskDecision with approved=False if stop triggered (meaning
            the position should be exited).
        """
        pct = stop_loss_trailing_pct if stop_loss_trailing_pct is not None else self.stop_loss_trailing_pct
        drop_pct = ((highest_price_since_entry - current_price) / highest_price_since_entry) * 100.0

        if drop_pct >= pct:
            return RiskDecision(
                approved=False,
                reason=(
                    f"Trailing stop-loss triggered for {ticker}: "
                    f"price dropped {drop_pct:.1f}% from high "
                    f"(current={current_price}, high={highest_price_since_entry}, "
                    f"threshold={pct:.1f}%)"
                ),
                adjusted_quantity=0,
            )

        return RiskDecision(
            approved=True,
            reason=f"Stop-loss not triggered for {ticker}: drop {drop_pct:.1f}% < {pct:.1f}%",
            adjusted_quantity=0,
        )

    def check_portfolio_drawdown(self, portfolio: PortfolioState) -> RiskDecision:
        """Check portfolio drawdown from peak NAV.

        Two levels:
        - drawdown_pause_pct (default 10%): pause new buys
        - drawdown_circuit_breaker_pct (default 20%): full liquidation

        Returns:
            RiskDecision with approved=False and appropriate reason.
        """
        if portfolio.peak_nav <= 0:
            return RiskDecision(
                approved=True,
                reason="No peak NAV recorded",
                adjusted_quantity=0,
            )

        drawdown_pct = ((portfolio.peak_nav - portfolio.nav) / portfolio.peak_nav) * 100.0

        # Circuit breaker is highest precedence (20% default)
        if drawdown_pct >= self.drawdown_circuit_breaker_pct:
            return RiskDecision(
                approved=False,
                reason=(
                    f"Circuit breaker triggered: portfolio drawdown {drawdown_pct:.1f}% "
                    f"exceeds {self.drawdown_circuit_breaker_pct:.1f}% threshold. "
                    f"Full liquidation required."
                ),
                adjusted_quantity=0,
            )

        # Drawdown pause (10% default)
        if drawdown_pct >= self.drawdown_pause_pct:
            return RiskDecision(
                approved=False,
                reason=(
                    f"Drawdown pause: portfolio drawdown {drawdown_pct:.1f}% "
                    f"exceeds {self.drawdown_pause_pct:.1f}% threshold. "
                    f"New buys paused."
                ),
                adjusted_quantity=0,
            )

        return RiskDecision(
            approved=True,
            reason=f"Portfolio drawdown {drawdown_pct:.1f}% within limits",
            adjusted_quantity=0,
        )
