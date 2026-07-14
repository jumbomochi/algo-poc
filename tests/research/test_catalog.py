from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from research.factors.catalog import build_default_registry
from research.factors.panel import build_factor_panel


def make_bars(days: int = 260) -> dict[str, list[dict[str, object]]]:
    start = date(2025, 1, 1)
    return {
        "A": [
            {
                "date": start + timedelta(days=i),
                "open": 100 + i,
                "high": 101 + i,
                "low": 99 + i,
                "close": 100 + i,
                "volume": 1_000 + i,
            }
            for i in range(days)
        ]
    }


def test_default_catalog_contains_four_reviewed_price_factors() -> None:
    ids = {spec.factor_id for spec in build_default_registry().list_specs()}
    assert ids == {
        "price_momentum_126d",
        "high_52w",
        "low_volatility_63d",
        "liquidity_20d",
    }


def test_default_catalog_populates_canonical_factor_metadata() -> None:
    specs = build_default_registry().list_specs()

    assert all(spec.economic_rationale for spec in specs)
    assert all(spec.prediction_horizon_days > 0 for spec in specs)
    assert all(spec.supported_universes == ("sp500", "russell1000") for spec in specs)
    assert all(spec.missing_data_policy for spec in specs)
    assert all(spec.normalization_policy for spec in specs)
    assert all(spec.source for spec in specs)
    assert all(spec.license for spec in specs)


def test_catalog_outputs_before_t_are_unchanged_when_future_prices_mutate() -> None:
    bars = make_bars()
    panel_before = build_factor_panel(bars)
    mutated = make_bars()
    mutated["A"][-1]["close"] = 999_999.0
    panel_after = build_factor_panel(mutated)
    factor = build_default_registry().get("price_momentum_126d")

    before = factor.compute(panel_before)
    after = factor.compute(panel_after)

    pd.testing.assert_series_equal(before.iloc[:-1, 0], after.iloc[:-1, 0])
