from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import pytest

from research.factors.contracts import FactorPanel, FactorSpec
from research.factors.registry import FactorRegistry


@dataclass(frozen=True)
class DummyFactor:
    spec: FactorSpec

    def compute(self, panel: FactorPanel) -> pd.DataFrame:
        return panel.field("close")


def make_factor(factor_id="dummy", version="1.0.0"):
    return DummyFactor(FactorSpec(
        factor_id=factor_id,
        version=version,
        family="test",
        description="test factor",
        economic_rationale="test rationale",
        prediction_horizon_days=1,
        required_fields=("close",),
        supported_sleeves=("momentum",),
        supported_universes=("test_universe",),
        lookback_days=1,
        direction=1,
        missing_data_policy="test missing-data policy",
        normalization_policy="test normalization policy",
        source="test fixture",
        license="test",
    ))


def test_registry_is_explicit_and_rejects_duplicate_factor_ids():
    registry = FactorRegistry()
    registry.register(make_factor())
    assert registry.get("dummy").spec.version == "1.0.0"
    with pytest.raises(ValueError, match="already registered"):
        registry.register(make_factor(version="2.0.0"))


def test_registry_get_unknown_factor_fails_loudly():
    with pytest.raises(KeyError, match="unknown factor"):
        FactorRegistry().get("missing")
