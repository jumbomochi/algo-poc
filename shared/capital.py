from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

from shared.config import CapitalConfig


class CapitalDisabledError(RuntimeError):
    """Raised when capital deployment is disabled for the selected mode."""


@dataclass(frozen=True)
class CapitalBudget:
    net_liquidation: float
    deployment_fraction: float
    max_deployable_usd: float | None
    deployable_capital: float
    sleeve_budgets: dict[str, float]


def calculate_capital_budget(
    net_liquidation: float,
    mode: str,
    config: CapitalConfig,
    sleeve_weights: Mapping[str, float],
) -> CapitalBudget:
    selected = config.live if mode == "live" else config.paper
    if net_liquidation <= 0:
        raise ValueError("IB NetLiquidation must be positive")
    if mode == "live" and (
        selected.deployment_fraction <= 0
        or selected.max_deployable_usd is None
        or selected.max_deployable_usd <= 0
    ):
        raise CapitalDisabledError("live fraction and cap must both be positive")

    fractional = net_liquidation * selected.deployment_fraction
    deployable = (
        min(fractional, selected.max_deployable_usd)
        if selected.max_deployable_usd is not None
        else fractional
    )
    if not math.isclose(sum(sleeve_weights.values()), 1.0, abs_tol=1e-6):
        raise ValueError("sleeve weights must sum to 1.0")

    return CapitalBudget(
        net_liquidation=net_liquidation,
        deployment_fraction=selected.deployment_fraction,
        max_deployable_usd=selected.max_deployable_usd,
        deployable_capital=deployable,
        sleeve_budgets={key: deployable * weight for key, weight in sleeve_weights.items()},
    )
