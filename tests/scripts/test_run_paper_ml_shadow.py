"""Wiring the ML shadow into the 04:15 run.

The trained model would suppress 80% of buy signals. Shadow mode exists so that
number can be checked against the live book before anything acts on it — every
signal passes through, and the verdict is recorded for joining against fills.

The wiring must satisfy two things that pull in opposite directions: the shadow
has to see the sleeves' real signals, and it must be incapable of changing what
they produce.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from scripts.run_paper import build_portfolios, load_ml_shadow_model


SESSIONS = [date(2026, 1, 5) + timedelta(days=i) for i in range(140)]


def _bars(nth: int) -> list[dict]:
    rate = 0.0005 * (nth + 1)
    return [
        {"date": d, "open": c, "high": c * 1.01, "low": c * 0.99,
         "close": c, "volume": 1_000_000}
        for d, c in zip(SESSIONS, (100.0 * (1 + rate * i) for i in range(len(SESSIONS))))
    ]


def _build(**kw):
    from shared.universe import ACTIVE_SLEEVES, UNIVERSE_REGISTRY

    universe = sorted({t for s in ACTIVE_SLEEVES for t in UNIVERSE_REGISTRY[s]})
    kwargs = dict(
        capital=100_000.0,
        bars_by_ticker={t: _bars(i) for i, t in enumerate(universe)},
        regime_by_date={d: "bull" for d in SESSIONS},
        fundamentals_lookup=lambda t, d: {"roe": 0.3, "debt_equity": 0.4, "profit_margin": 0.2},
        earnings_lookup=lambda t, d: {"surprise_pct": 8.0},
    )
    kwargs.update(kw)
    return build_portfolios(**kwargs)


def test_without_a_model_the_sleeves_are_unchanged() -> None:
    """The shadow is opt-in. No model, no wrapper, no behaviour change."""
    from shared.universe import ACTIVE_SLEEVES

    plain = _build()
    assert set(plain) == set(ACTIVE_SLEEVES)


def test_the_shadow_does_not_change_what_a_sleeve_signals() -> None:
    """The defining property. Identical signals with and without the shadow."""
    class _Model:
        def feature_name(self):
            return ["portfolio"]

        def predict(self, df):
            return [0.0]  # would suppress everything

    universe_bars = _build().__class__  # noqa: F841 - readability
    plain = _build()
    shadowed = _build(ml_shadow=(_Model(), 0.5, [], lambda r: None))

    from shared.universe import UNIVERSE_REGISTRY

    for sleeve in plain:
        bars = _bars(0)
        for ticker in UNIVERSE_REGISTRY[sleeve][:3]:
            assert plain[sleeve].signals_fn(ticker, bars) == (
                shadowed[sleeve].signals_fn(ticker, bars)
            )


def test_a_broken_model_does_not_change_what_a_sleeve_signals() -> None:
    """A scoring failure must cost an observation, never an entry."""
    class _Broken:
        def feature_name(self):
            return ["portfolio"]

        def predict(self, df):
            raise RuntimeError("boom")

    from shared.universe import UNIVERSE_REGISTRY

    plain = _build()
    shadowed = _build(ml_shadow=(_Broken(), 0.5, [], lambda r: None))

    bars = _bars(0)
    for sleeve in plain:
        for ticker in UNIVERSE_REGISTRY[sleeve][:2]:
            assert plain[sleeve].signals_fn(ticker, bars) == (
                shadowed[sleeve].signals_fn(ticker, bars)
            )


def test_the_shadow_survives_the_crash_freeze_rewrap() -> None:
    """build_portfolios rebuilds every PortfolioConfig to wrap signals_fn in
    the crash-entry freeze. A shadow applied before that must not be lost, and
    a shadow applied after must not sit outside it."""
    seen: list[dict] = []

    class _Model:
        def feature_name(self):
            return ["portfolio"]

        def predict(self, df):
            return [0.9]

    from shared.universe import UNIVERSE_REGISTRY

    built = _build(ml_shadow=(_Model(), 0.5, [], seen.append))
    for ticker in UNIVERSE_REGISTRY["momentum"]:
        built["momentum"].signals_fn(ticker, _bars(0))

    assert seen, "no signal was scored — the shadow was lost in the rewrap"


# ---------------------------------------------------------------------------
# loading the model
# ---------------------------------------------------------------------------


def test_a_missing_model_yields_none_rather_than_raising(tmp_path) -> None:
    """No model is the normal state before one is trained. It must not stop the
    04:15 run."""
    assert load_ml_shadow_model(str(tmp_path / "absent.txt")) is None


def test_a_corrupt_model_yields_none_rather_than_raising(tmp_path) -> None:
    bad = tmp_path / "signal_quality_model.txt"
    bad.write_text("not a lightgbm model")

    assert load_ml_shadow_model(str(bad)) is None


def test_the_real_model_loads_with_its_categoricals() -> None:
    from pathlib import Path

    real = Path(__file__).resolve().parents[2] / "models" / "signal_quality_model.txt"
    if not real.exists():
        pytest.skip("models/ absent")

    loaded = load_ml_shadow_model(str(real))

    assert loaded is not None
    model, categoricals = loaded
    assert "portfolio" in categoricals
