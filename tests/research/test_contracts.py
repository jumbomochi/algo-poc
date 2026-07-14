from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from research.factors.contracts import FactorPanel, FactorSpec


def make_spec() -> FactorSpec:
    return FactorSpec(
        factor_id="price_momentum_126d",
        version="1.0.0",
        family="momentum",
        description="126-day total return",
        economic_rationale="Persistent price trends may continue over the prediction horizon",
        prediction_horizon_days=21,
        required_fields=("close",),
        supported_sleeves=("momentum",),
        supported_universes=("us_equities",),
        lookback_days=126,
        direction=1,
        missing_data_policy="require_complete_lookback",
        normalization_policy="cross_sectional_zscore",
        source="Jegadeesh and Titman",
        license="formula",
    )


def test_factor_spec_requires_semantic_identity_and_positive_lookback():
    spec = make_spec()
    assert spec.key == "price_momentum_126d@1.0.0"
    assert spec.economic_rationale == (
        "Persistent price trends may continue over the prediction horizon"
    )
    assert spec.prediction_horizon_days == 21
    assert spec.supported_universes == ("us_equities",)
    assert spec.missing_data_policy == "require_complete_lookback"
    assert spec.normalization_policy == "cross_sectional_zscore"

    with pytest.raises(ValueError, match="lookback_days"):
        FactorSpec(**{**spec.__dict__, "lookback_days": 0})


def test_factor_spec_rejects_non_semantic_version():
    spec = make_spec()

    with pytest.raises(ValueError, match="version.*MAJOR.MINOR.PATCH"):
        FactorSpec(**{**spec.__dict__, "version": "latest"})


def test_factor_panel_rejects_misaligned_fields():
    close = pd.DataFrame({"AAPL": [100.0]}, index=pd.to_datetime(["2026-01-02"]))
    volume = pd.DataFrame({"MSFT": [10]}, index=close.index)
    with pytest.raises(ValueError, match="same index and columns"):
        FactorPanel(fields={"close": close, "volume": volume}, as_of=date(2026, 1, 2))
