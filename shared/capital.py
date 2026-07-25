from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from shared.broker_state import BrokerAccountSnapshot
from shared.config import CapitalConfig, CurrencyConfig


class CapitalDisabledError(RuntimeError):
    """Raised when capital deployment is disabled for the selected mode."""


@dataclass(frozen=True)
class CapitalBudget:
    base_currency: str
    trading_currency: str
    net_liquidation_base: float
    net_liquidation_trading_equivalent: float
    fx_base_per_trading: float
    fx_captured_at: datetime
    fractional_base: float
    deployment_fraction: float
    max_deployable_usd: float | None
    settled_cash_trading: float
    deployable_capital: float
    sleeve_budgets: dict[str, float]


def calculate_capital_budget(
    snapshot: BrokerAccountSnapshot,
    mode: str,
    capital_config: CapitalConfig,
    currency_config: CurrencyConfig,
    sleeve_weights: Mapping[str, float],
    now: datetime | None = None,
) -> CapitalBudget:
    selected = capital_config.live if mode == "live" else capital_config.paper
    if snapshot.base_currency != currency_config.expected_base_currency:
        raise ValueError(
            "snapshot base currency does not match configured base currency"
        )
    if snapshot.trading_currency != currency_config.trading_currency:
        raise ValueError(
            "snapshot trading currency does not match configured trading currency"
        )
    if (
        not math.isfinite(snapshot.net_liquidation_base)
        or snapshot.net_liquidation_base <= 0
    ):
        raise ValueError("IB NetLiquidation must be positive")
    if (
        not math.isfinite(snapshot.fx_base_per_trading)
        or snapshot.fx_base_per_trading <= 0
    ):
        raise ValueError("FX rate must be positive")

    evaluated_at = now if now is not None else datetime.now(UTC)
    fx_age = (evaluated_at - snapshot.fx_captured_at).total_seconds()
    if fx_age < 0:
        raise ValueError("FX quote age cannot be negative")
    if fx_age > currency_config.max_fx_age_seconds:
        raise ValueError("FX quote is stale")

    if mode == "live" and (
        selected.deployment_fraction <= 0
        or selected.max_deployable_usd is None
        or selected.max_deployable_usd <= 0
    ):
        raise CapitalDisabledError("live fraction and cap must both be positive")

    fractional_base = (
        snapshot.net_liquidation_base * selected.deployment_fraction
    )
    fractional_trading = fractional_base / snapshot.fx_base_per_trading
    deployable = (
        min(fractional_trading, selected.max_deployable_usd)
        if selected.max_deployable_usd is not None
        else fractional_trading
    )
    if not math.isclose(sum(sleeve_weights.values()), 1.0, abs_tol=1e-6):
        raise ValueError("sleeve weights must sum to 1.0")

    return CapitalBudget(
        base_currency=snapshot.base_currency,
        trading_currency=snapshot.trading_currency,
        net_liquidation_base=snapshot.net_liquidation_base,
        net_liquidation_trading_equivalent=(
            snapshot.net_liquidation_trading_equivalent
        ),
        fx_base_per_trading=snapshot.fx_base_per_trading,
        fx_captured_at=snapshot.fx_captured_at,
        fractional_base=fractional_base,
        deployment_fraction=selected.deployment_fraction,
        max_deployable_usd=selected.max_deployable_usd,
        settled_cash_trading=snapshot.settled_cash_trading,
        deployable_capital=deployable,
        sleeve_budgets={
            key: deployable * weight for key, weight in sleeve_weights.items()
        },
    )
