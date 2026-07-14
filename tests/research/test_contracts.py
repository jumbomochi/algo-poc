from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from research.factors.contracts import FactorPanel, FactorSpec


def test_factor_spec_requires_semantic_identity_and_positive_lookback():
    spec = FactorSpec(
        factor_id="price_momentum_126d",
        version="1.0.0",
        family="momentum",
        description="126-day total return",
        required_fields=("close",),
        supported_sleeves=("momentum",),
        lookback_days=126,
        direction=1,
        source="Jegadeesh and Titman",
        license="formula",
    )
    assert spec.key == "price_momentum_126d@1.0.0"

    with pytest.raises(ValueError, match="lookback_days"):
        FactorSpec(**{**spec.__dict__, "lookback_days": 0})


def test_factor_panel_rejects_misaligned_fields():
    close = pd.DataFrame({"AAPL": [100.0]}, index=pd.to_datetime(["2026-01-02"]))
    volume = pd.DataFrame({"MSFT": [10]}, index=close.index)
    with pytest.raises(ValueError, match="same index and columns"):
        FactorPanel(fields={"close": close, "volume": volume}, as_of=date(2026, 1, 2))
