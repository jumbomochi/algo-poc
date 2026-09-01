"""Every live sleeve must declare the parameters that identify its model.

``backtest.shadow_artifact.shadow_id_for`` fingerprints the model from each
sleeve's ``shadow_params`` and refuses a sleeve that declares none. That refusal
exists because the fingerprint is what makes a model change visible: evidence
rows key on ``(sleeve, session_date, baseline_id)``, so if editing ``top_n``
from 5 to 8 left the id untouched, the epoch would not restart (direction doc
D13) and the breach streak would carry across two different models as though
they were one.

The trap this module guards is the crash-entry freeze at the end of
``build_portfolios``: it rebuilds every ``PortfolioConfig`` to wrap the signal
function, and a field it forgets to carry is silently lost. The sleeve still
works, the shadow still runs, and the id quietly stops tracking the model.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from backtest.shadow_artifact import shadow_id_for
from scripts.run_paper import build_portfolios
from shared.universe import ACTIVE_SLEEVES, UNIVERSE_REGISTRY


SESSIONS = [date(2026, 1, 5) + timedelta(days=i) for i in range(140)]


def _bars(nth: int) -> list[dict]:
    rate = 0.0005 * (nth + 1)
    return [
        {
            "date": day, "open": c, "high": c * 1.01,
            "low": c * 0.99, "close": c, "volume": 1_000_000,
        }
        for day, c in zip(SESSIONS, (100.0 * (1.0 + rate * i) for i in range(len(SESSIONS))))
    ]


def _build():
    universe = sorted({t for s in ACTIVE_SLEEVES for t in UNIVERSE_REGISTRY[s]})
    return build_portfolios(
        capital=100_000.0,
        bars_by_ticker={t: _bars(i) for i, t in enumerate(universe)},
        regime_by_date={d: "bull" for d in SESSIONS},
        fundamentals_lookup=lambda t, d: {
            "roe": 0.3, "debt_equity": 0.4, "profit_margin": 0.2,
        },
        earnings_lookup=lambda t, d: {"surprise_pct": 8.0},
    )


@pytest.mark.parametrize("sleeve", ACTIVE_SLEEVES)
def test_every_sleeve_declares_shadow_params(sleeve: str) -> None:
    built = _build()

    assert built[sleeve].shadow_params is not None, (
        f"{sleeve} declares no shadow_params, so shadow_id_for refuses the "
        "roster and no shadow can be produced"
    )
    assert built[sleeve].shadow_params, f"{sleeve}'s shadow_params is empty"


def test_shadow_params_survive_the_crash_freeze_rewrap() -> None:
    """build_portfolios rebuilds every PortfolioConfig to wrap signals_fn in
    the crash-entry freeze. A field dropped there is lost silently."""
    built = _build()

    # The rewrap applies to everything except tail_risk_hedge, so proving the
    # field survives on a rewrapped sleeve is the case that matters.
    assert built["momentum"].shadow_params is not None
    assert built["momentum"].shadow_params


def test_the_roster_produces_a_shadow_id() -> None:
    """The end-to-end property: a live roster is fingerprintable."""
    shadow_id = shadow_id_for(_build())

    assert shadow_id.startswith("shadow:")


def test_shadow_params_carry_the_values_that_change_behaviour() -> None:
    """A fingerprint of the wrong fields is no fingerprint at all. These are
    the momentum parameters build_portfolios actually passes to its signal
    factory, so a future edit to any of them must move the id."""
    params = _build()["momentum"].shadow_params

    assert params["top_n"] == 5
    assert params["lookback_days"] == 126
    assert params["position_size_pct"] == pytest.approx(0.12)
    assert params["trailing_stop_pct"] == pytest.approx(0.10)


def test_the_universe_is_part_of_the_model_fingerprint() -> None:
    """Scoping a sleeve to a different universe changes what it would do, so it
    has to move the id — otherwise the 2026-08-31 scoping fix would have been
    invisible to the epoch."""
    params = _build()["sector_rotation"].shadow_params

    assert params["universe"] == sorted(UNIVERSE_REGISTRY["sector_rotation"])
