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


@pytest.mark.parametrize("factor_id", ["", "  ", None])
def test_factor_spec_rejects_blank_factor_id(factor_id):
    spec = make_spec()

    with pytest.raises(ValueError, match="factor_id"):
        FactorSpec(**{**spec.__dict__, "factor_id": factor_id})


@pytest.mark.parametrize(
    "field",
    [
        "family",
        "description",
        "economic_rationale",
        "missing_data_policy",
        "normalization_policy",
        "source",
        "license",
    ],
)
def test_factor_spec_rejects_blank_required_text(field):
    spec = make_spec()

    for value in ("  ", None):
        with pytest.raises(ValueError, match=field):
            FactorSpec(**{**spec.__dict__, field: value})


@pytest.mark.parametrize(
    "field", ["required_fields", "supported_sleeves", "supported_universes"]
)
def test_factor_spec_rejects_empty_or_blank_required_tuples(field):
    spec = make_spec()

    with pytest.raises(ValueError, match=field):
        FactorSpec(**{**spec.__dict__, field: ()})
    with pytest.raises(ValueError, match=field):
        FactorSpec(**{**spec.__dict__, field: ("",)})
    with pytest.raises(ValueError, match=field):
        FactorSpec(**{**spec.__dict__, field: ["value"]})


def test_factor_spec_requires_positive_prediction_horizon():
    spec = make_spec()

    with pytest.raises(ValueError, match="prediction_horizon_days"):
        FactorSpec(**{**spec.__dict__, "prediction_horizon_days": 0})


def test_factor_panel_rejects_misaligned_fields():
    close = pd.DataFrame({"AAPL": [100.0]}, index=pd.to_datetime(["2026-01-02"]))
    volume = pd.DataFrame({"MSFT": [10]}, index=close.index)
    with pytest.raises(ValueError, match="same index and columns"):
        FactorPanel(fields={"close": close, "volume": volume}, as_of=date(2026, 1, 2))


def test_factor_panel_deep_owns_inputs_and_returns_isolated_fields():
    close = pd.DataFrame({"AAPL": [100.0]}, index=pd.to_datetime(["2026-01-02"]))
    fields = {"close": close}
    panel = FactorPanel(fields=fields, as_of=date(2026, 1, 2))

    close.iloc[0, 0] = 999.0
    fields.clear()
    first = panel.field("close")
    first.iloc[0, 0] = -1.0

    assert panel.field("close").iloc[0, 0] == 100.0
    assert not hasattr(panel, "fields")


def test_generic_panel_implicit_eligibility_ignores_broadcast_metadata():
    timestamp = pd.Timestamp("2026-01-02")
    baseline = FactorPanel(
        fields={
            "custom:observation": pd.DataFrame({"A": [1.0]}, index=[timestamp]),
            "regime:label": pd.DataFrame({"A": ["bull"]}, index=[timestamp]),
        },
        as_of=date(2026, 1, 2),
    )
    appended = FactorPanel(
        fields={
            "custom:observation": pd.DataFrame(
                {"A": [1.0], "B": [float("nan")]}, index=[timestamp]
            ),
            "regime:label": pd.DataFrame(
                {"A": ["bull"], "B": ["bull"]}, index=[timestamp]
            ),
        },
        as_of=date(2026, 1, 2),
    )

    assert appended.universe_snapshot_id() == baseline.universe_snapshot_id()
    assert appended.input_artifact_checksum() == baseline.input_artifact_checksum()
