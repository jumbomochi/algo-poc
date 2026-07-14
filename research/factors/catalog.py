from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from research.factors.contracts import FactorPanel, FactorSpec
from research.factors.operations import (
    rolling_dollar_volume,
    rolling_volatility,
    trailing_return,
)
from research.factors.registry import FactorRegistry

ALL_SLEEVES = (
    "momentum",
    "earnings_drift",
    "sector_rotation",
    "quality_value",
    "thematic_momentum",
    "tail_risk_hedge",
)
SUPPORTED_UNIVERSES = ("sp500", "russell1000")
DEFAULT_FACTOR_IDS = (
    "price_momentum_126d",
    "high_52w",
    "low_volatility_63d",
    "liquidity_20d",
)


@dataclass(frozen=True)
class PriceMomentum126d:
    spec: FactorSpec = FactorSpec(
        factor_id="price_momentum_126d",
        version="1.0.0",
        family="momentum",
        description="126-day total return",
        economic_rationale=(
            "Persistent price trends may continue over the prediction horizon"
        ),
        prediction_horizon_days=21,
        required_fields=("close",),
        supported_sleeves=ALL_SLEEVES,
        supported_universes=SUPPORTED_UNIVERSES,
        lookback_days=126,
        direction=1,
        missing_data_policy="require_complete_lookback",
        normalization_policy="cross_sectional_zscore",
        source="Jegadeesh and Titman",
        license="formula",
    )

    def compute(self, panel: FactorPanel) -> pd.DataFrame:
        return trailing_return(panel.field("close"), 126)


@dataclass(frozen=True)
class High52Week:
    spec: FactorSpec = FactorSpec(
        factor_id="high_52w",
        version="1.0.0",
        family="momentum",
        description="Distance to trailing 252-day high",
        economic_rationale=(
            "Prices near their annual high may underreact to favorable information"
        ),
        prediction_horizon_days=21,
        required_fields=("close",),
        supported_sleeves=ALL_SLEEVES,
        supported_universes=SUPPORTED_UNIVERSES,
        lookback_days=252,
        direction=1,
        missing_data_policy="require_complete_lookback",
        normalization_policy="cross_sectional_zscore",
        source="George and Hwang",
        license="formula",
    )

    def compute(self, panel: FactorPanel) -> pd.DataFrame:
        close = panel.field("close")
        return close / close.rolling(252, min_periods=252).max() - 1.0


@dataclass(frozen=True)
class LowVolatility63d:
    spec: FactorSpec = FactorSpec(
        factor_id="low_volatility_63d",
        version="1.0.0",
        family="risk",
        description="Negative 63-day annualized volatility",
        economic_rationale=(
            "Lower-volatility equities may deliver superior risk-adjusted returns"
        ),
        prediction_horizon_days=21,
        required_fields=("close",),
        supported_sleeves=ALL_SLEEVES,
        supported_universes=SUPPORTED_UNIVERSES,
        lookback_days=63,
        direction=1,
        missing_data_policy="require_complete_lookback",
        normalization_policy="cross_sectional_zscore",
        source="Ang et al.",
        license="formula",
    )

    def compute(self, panel: FactorPanel) -> pd.DataFrame:
        return -rolling_volatility(panel.field("close"), 63)


@dataclass(frozen=True)
class Liquidity20d:
    spec: FactorSpec = FactorSpec(
        factor_id="liquidity_20d",
        version="1.0.0",
        family="liquidity",
        description="Log 20-day average dollar volume",
        economic_rationale=(
            "Higher dollar volume indicates greater execution capacity"
        ),
        prediction_horizon_days=1,
        required_fields=("close", "volume"),
        supported_sleeves=ALL_SLEEVES,
        supported_universes=SUPPORTED_UNIVERSES,
        lookback_days=20,
        direction=1,
        missing_data_policy="require_complete_lookback",
        normalization_policy="cross_sectional_zscore",
        source="execution-capacity control",
        license="formula",
    )

    def compute(self, panel: FactorPanel) -> pd.DataFrame:
        value = rolling_dollar_volume(
            panel.field("close"), panel.field("volume"), 20
        )
        return np.log(value.where(value > 0))


def build_default_registry() -> FactorRegistry:
    registry = FactorRegistry()
    for factor in (
        PriceMomentum126d(),
        High52Week(),
        LowVolatility63d(),
        Liquidity20d(),
    ):
        registry.register(factor)
    return registry
