from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SimplePortfolioState:
    """Simplified portfolio state for backtest risk engine calls.

    Mirrors the interface of services.risk_management.engine.PortfolioState
    but kept internal to avoid coupling the backtest package to service internals.
    """

    nav: float
    peak_nav: float
    positions: dict[str, Any] = field(default_factory=dict)
    sector_exposure: dict[str, float] = field(default_factory=dict)
    total_exposure_pct: float = 0.0
    margin_utilization_pct: float = 0.0
