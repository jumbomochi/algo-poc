from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from research.factors.catalog import DEFAULT_FACTOR_IDS, build_default_registry
from research.factors.panel import build_factor_panel

LOW_VOL_RETURNS = np.array(([0.01, -0.01] * 31) + [0.01])
LOW_VOL_CLOSES = [100.0] + list(100.0 * np.cumprod(1.0 + LOW_VOL_RETURNS))
LOW_VOL_EXPECTED = -float(LOW_VOL_RETURNS.std(ddof=1) * np.sqrt(252.0))


def make_bars(
    closes_by_ticker: dict[str, list[float]],
    volumes_by_ticker: dict[str, list[float]] | None = None,
) -> dict[str, list[dict[str, object]]]:
    start = date(2025, 1, 1)
    volumes_by_ticker = volumes_by_ticker or {}
    return {
        ticker: [
            {
                "date": start + timedelta(days=i),
                "open": close,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": volumes_by_ticker.get(ticker, [1_000.0] * len(closes))[i],
            }
            for i, close in enumerate(closes)
        ]
        for ticker, closes in closes_by_ticker.items()
    }


def standard_bars() -> dict[str, list[dict[str, object]]]:
    return make_bars(
        {
            "A": [100.0 + i for i in range(260)],
            "B": [300.0 - (i / 2.0) for i in range(260)],
        },
        {
            "A": [1_000.0 + i for i in range(260)],
            "B": [2_000.0 + (2 * i) for i in range(260)],
        },
    )


def test_default_catalog_contains_four_reviewed_price_factors() -> None:
    ids = {spec.factor_id for spec in build_default_registry().list_specs()}
    assert ids == set(DEFAULT_FACTOR_IDS) == {
        "price_momentum_126d",
        "high_52w",
        "low_volatility_63d",
        "liquidity_20d",
    }


@pytest.mark.parametrize(
    ("factor_id", "closes", "volumes", "missing_rows", "expected"),
    [
        pytest.param(
            "price_momentum_126d",
            [1.0] + [2.0] * 125 + [4.0],
            None,
            126,
            3.0,
            id="126-day-window-return",
        ),
        pytest.param(
            "high_52w",
            [100.0] + [200.0] * 250 + [150.0],
            None,
            251,
            -0.25,
            id="252-day-high-distance",
        ),
        pytest.param(
            "low_volatility_63d",
            LOW_VOL_CLOSES,
            None,
            63,
            LOW_VOL_EXPECTED,
            id="negative-63-day-volatility",
        ),
        pytest.param(
            "liquidity_20d",
            [2.0] * 20,
            [5.0] * 20,
            19,
            np.log(10.0),
            id="log-20-day-dollar-volume",
        ),
    ],
)
def test_catalog_formulas_use_reviewed_windows_signs_and_log_transform(
    factor_id: str,
    closes: list[float],
    volumes: list[float] | None,
    missing_rows: int,
    expected: float,
) -> None:
    volumes_by_ticker = {"A": volumes} if volumes is not None else None
    panel = build_factor_panel(make_bars({"A": closes}, volumes_by_ticker))

    output = build_default_registry().get(factor_id).compute(panel)

    assert output.shape == panel.field("close").shape
    assert output.iloc[:missing_rows, 0].isna().all()
    assert output.iloc[missing_rows:, 0].notna().all()
    assert output.iloc[-1, 0] == pytest.approx(expected)
    if factor_id in {"high_52w", "low_volatility_63d"}:
        assert (output.dropna().to_numpy() <= 0.0).all()


@pytest.mark.parametrize(
    ("factor_id", "missing_field"),
    [
        ("price_momentum_126d", "close"),
        ("high_52w", "close"),
        ("low_volatility_63d", "close"),
        ("liquidity_20d", "close"),
        ("liquidity_20d", "volume"),
    ],
)
def test_catalog_factors_propagate_missing_required_inputs(
    factor_id: str,
    missing_field: str,
) -> None:
    bars = standard_bars()
    bars["A"][-1][missing_field] = np.nan
    panel = build_factor_panel(bars)

    output = build_default_registry().get(factor_id).compute(panel)

    assert pd.isna(output.iloc[-1]["A"])


@pytest.mark.parametrize("factor_id", DEFAULT_FACTOR_IDS)
def test_each_catalog_factor_is_unchanged_before_mutated_future_data(
    factor_id: str,
) -> None:
    bars = standard_bars()
    panel_before = build_factor_panel(bars)
    mutated = standard_bars()
    factor = build_default_registry().get(factor_id)
    for field in factor.spec.required_fields:
        mutated["A"][-1][field] = 999_999.0
    panel_after = build_factor_panel(mutated)

    before = factor.compute(panel_before)
    after = factor.compute(panel_after)

    pd.testing.assert_frame_equal(before.iloc[:-1], after.iloc[:-1])


@pytest.mark.parametrize("factor_id", DEFAULT_FACTOR_IDS)
def test_each_catalog_factor_is_deterministic(factor_id: str) -> None:
    panel = build_factor_panel(standard_bars())
    factor = build_default_registry().get(factor_id)

    first = factor.compute(panel)
    second = factor.compute(panel)

    pd.testing.assert_frame_equal(first, second)


@pytest.mark.parametrize("factor_id", DEFAULT_FACTOR_IDS)
def test_each_price_factor_is_invariant_to_other_ticker_columns(
    factor_id: str,
) -> None:
    bars = standard_bars()
    full_panel = build_factor_panel(bars)
    single_panel = build_factor_panel({"A": bars["A"]})
    factor = build_default_registry().get(factor_id)

    full_output = factor.compute(full_panel)
    single_output = factor.compute(single_panel)

    pd.testing.assert_series_equal(full_output["A"], single_output["A"])


def test_default_catalog_populates_explicit_source_and_license_metadata() -> None:
    metadata = {
        spec.factor_id: (spec.source, spec.license)
        for spec in build_default_registry().list_specs()
    }

    assert metadata == {
        "price_momentum_126d": ("Jegadeesh and Titman", "formula"),
        "high_52w": ("George and Hwang", "formula"),
        "low_volatility_63d": ("Ang et al.", "formula"),
        "liquidity_20d": ("execution-capacity control", "formula"),
    }


def test_default_catalog_populates_canonical_factor_metadata() -> None:
    specs = build_default_registry().list_specs()

    assert all(spec.economic_rationale for spec in specs)
    assert all(spec.prediction_horizon_days > 0 for spec in specs)
    assert all(
        spec.supported_universes == ("sp500", "russell1000") for spec in specs
    )
    assert all(
        spec.missing_data_policy == "require_complete_lookback" for spec in specs
    )
    assert all(
        spec.normalization_policy == "cross_sectional_zscore" for spec in specs
    )
