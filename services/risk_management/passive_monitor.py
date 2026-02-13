from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.risk_management.engine import PortfolioState
from shared.config import RiskConfig


@dataclass
class BreachAction:
    """Describes a breach detected during passive monitoring."""

    ticker: str
    action_type: str  # "notify" | "trim"
    target_pct: float
    current_pct: float
    message: str


class PassiveBreachMonitor:
    """Scans portfolio positions for soft and hard ceiling breaches.

    Soft ceiling (default 7% NAV): notify only.
    Hard ceiling (default 15% NAV): auto-trim to soft ceiling via market order.
    Margin utilization: warning at 70%, critical at 85%.

    All thresholds are configurable via RiskConfig.
    """

    def __init__(self, config: RiskConfig) -> None:
        self._config = config

    def scan_positions(
        self,
        portfolio: PortfolioState,
        current_prices: dict[str, float],
    ) -> list[BreachAction]:
        """Scan all positions and margin for breaches.

        Args:
            portfolio: Current portfolio state with positions dict.
                Each position value should have at least a ``quantity`` key.
            current_prices: Mapping of ticker -> current market price.

        Returns:
            List of BreachAction instances for any detected breaches.
        """
        breaches: list[BreachAction] = []

        # Check each position against soft/hard ceilings
        for ticker, pos_data in portfolio.positions.items():
            quantity = pos_data.get("quantity", 0) if isinstance(pos_data, dict) else 0
            price = current_prices.get(ticker, 0.0)
            if portfolio.nav <= 0 or price <= 0:
                continue

            position_value = quantity * price
            position_pct = (position_value / portfolio.nav) * 100.0

            if position_pct >= self._config.hard_ceiling_pct:
                breaches.append(
                    BreachAction(
                        ticker=ticker,
                        action_type="trim",
                        target_pct=self._config.soft_ceiling_pct,
                        current_pct=position_pct,
                        message=(
                            f"{ticker} at {position_pct:.1f}% of NAV exceeds "
                            f"hard ceiling {self._config.hard_ceiling_pct:.1f}%. "
                            f"Auto-trimming to {self._config.soft_ceiling_pct:.1f}%."
                        ),
                    )
                )
            elif position_pct >= self._config.soft_ceiling_pct:
                breaches.append(
                    BreachAction(
                        ticker=ticker,
                        action_type="notify",
                        target_pct=self._config.soft_ceiling_pct,
                        current_pct=position_pct,
                        message=(
                            f"{ticker} at {position_pct:.1f}% of NAV exceeds "
                            f"soft ceiling {self._config.soft_ceiling_pct:.1f}%. "
                            f"Advisory: consider reducing position."
                        ),
                    )
                )

        # Check margin utilization
        margin_pct = portfolio.margin_utilization_pct
        if margin_pct >= self._config.margin_critical_pct:
            breaches.append(
                BreachAction(
                    ticker="__margin__",
                    action_type="trim",
                    target_pct=self._config.margin_warning_pct,
                    current_pct=margin_pct,
                    message=(
                        f"Margin utilization at {margin_pct:.1f}% exceeds "
                        f"critical threshold {self._config.margin_critical_pct:.1f}%. "
                        f"Immediate action required."
                    ),
                )
            )
        elif margin_pct >= self._config.margin_warning_pct:
            breaches.append(
                BreachAction(
                    ticker="__margin__",
                    action_type="notify",
                    target_pct=self._config.margin_warning_pct,
                    current_pct=margin_pct,
                    message=(
                        f"Margin utilization at {margin_pct:.1f}% exceeds "
                        f"warning threshold {self._config.margin_warning_pct:.1f}%. "
                        f"Monitor closely."
                    ),
                )
            )

        return breaches
