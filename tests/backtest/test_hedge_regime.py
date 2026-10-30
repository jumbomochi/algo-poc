from __future__ import annotations

from datetime import date, timedelta

import pytest

from scripts.run_backtest import (
    TAIL_RISK_HEDGE_VARIANTS,
    compute_breadth_by_date,
    compute_hedge_regime_by_date,
    compute_regime_by_date,
    hedge_regime_for_variant,
)

START = date(2026, 1, 1)


def _series(*breadths: float) -> dict:
    return {START + timedelta(days=i): b for i, b in enumerate(breadths)}


def _labels(breadths: tuple[float, ...], band: float) -> list[str]:
    return list(compute_hedge_regime_by_date(_series(*breadths), band=band).values())


def _bars_by_ticker(n_tickers: int, n_days: int) -> dict[str, list[dict]]:
    """Tickers that cross their 200-day MA on staggered days."""
    out = {}
    for t in range(n_tickers):
        bars = []
        for i in range(n_days):
            price = 100.0 + (5.0 if (i // (7 + t)) % 2 else -5.0)
            bars.append({"date": START + timedelta(days=i), "close": price})
        out[f"T{t:02d}"] = bars
    return out


def test_breadth_is_what_the_plain_regime_labels():
    bars = _bars_by_ticker(12, 260)
    breadth = compute_breadth_by_date(bars)
    regime = compute_regime_by_date(bars)
    assert breadth.keys() == regime.keys()
    for d, b in breadth.items():
        expected = (
            "bull" if b > 0.60 else "crash" if b < 0.10
            else "bear" if b < 0.40 else "neutral"
        )
        assert regime[d] == expected


def test_breadth_on_a_threshold_holds_the_label_instead_of_flipping():
    # 10-26..10-29 live: 60.7%, 60.7%, 60.0%, 62.1%.
    assert _labels((0.607, 0.607, 0.600, 0.621), band=0.05) == ["bull"] * 4
    assert _labels((0.607, 0.607, 0.600, 0.621), band=0.0) == [
        "bull", "bull", "neutral", "bull"
    ]


@pytest.mark.parametrize(
    ("breadths", "expected"),
    [
        # Leaving bull needs breadth at or below 55%.
        ((0.70, 0.56, 0.55), ["bull", "bull", "neutral"]),
        # Entering bull from neutral needs breadth above 65%.
        ((0.50, 0.64, 0.66), ["neutral", "neutral", "bull"]),
        # Entering bear from neutral needs breadth below 35%.
        ((0.50, 0.36, 0.34), ["neutral", "neutral", "bear"]),
        # Leaving bear needs breadth at or above 45%.
        ((0.30, 0.44, 0.45), ["bear", "bear", "neutral"]),
        # A collapse goes straight from bull to bear.
        ((0.70, 0.20), ["bull", "bear"]),
    ],
)
def test_the_band_sits_on_the_far_side_of_each_edge(breadths, expected):
    assert _labels(breadths, band=0.05) == expected


def test_a_crash_is_a_bear_for_the_hedge():
    breadths = (0.30, 0.05, 0.02, 0.30)
    assert set(_labels(breadths, band=0.05)) == {"bear"}
    assert set(_labels(breadths, band=0.0)) == {"bear"}


def test_a_zero_band_is_the_plain_labels_with_crash_read_as_bear():
    bars = _bars_by_ticker(12, 260)
    plain = compute_regime_by_date(bars)
    hedge = hedge_regime_for_variant("diag-crash", bars, plain)
    assert hedge == {
        d: "bear" if label == "crash" else label for d, label in plain.items()
    }


def test_v1_reads_the_plain_series_every_sleeve_reads():
    bars = _bars_by_ticker(12, 260)
    plain = compute_regime_by_date(bars)
    assert hedge_regime_for_variant("v1", bars, plain) is plain


def test_the_pre_registered_band_is_five_points():
    # docs/decisions/tail-risk-hedge-v2-2026-10.md: changing it is a new trial.
    assert TAIL_RISK_HEDGE_VARIANTS == {"v1": None, "v2": 0.05, "diag-crash": 0.0}
