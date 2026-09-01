"""Producing the shadow artifact from the 04:15 run.

The 04:15 job holds the fetched bars, the sleeve configs and the risk engines,
so it produces the shadow for ~0.06s of work on data it already has. The 04:45
monitor reads what it writes.

That coupling has a cost worth naming: shadow code now runs inside the job that
trades the book. So the production path is built to be *skippable* — a failure
writes no artifact and lets the paper run continue, exactly the posture
``run_paper.py`` already takes with the research shadow observer ("Research
shadow observer failed; paper trading is unchanged"). A missing artifact is not
a silent loss either: the monitor treats absence as the blind signal.

The counterfactual is built with **no live portfolio context**. Feeding live
positions in would re-score the book live actually holds, which is not a
comparison — it is the same book twice.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from backtest.shadow_artifact import load_shadow
from scripts.run_paper import produce_shadow_artifact


SESSIONS = [date(2026, 1, 5) + timedelta(days=i) for i in range(140)]
WINDOW = 30


def _bars(nth: int) -> list[dict]:
    rate = 0.0005 * (nth + 1)
    return [
        {
            "date": day, "open": c, "high": c * 1.01,
            "low": c * 0.99, "close": c, "volume": 1_000_000,
        }
        for day, c in zip(SESSIONS, (100.0 * (1.0 + rate * i) for i in range(len(SESSIONS))))
    ]


def _universe_bars():
    from shared.universe import ACTIVE_SLEEVES, UNIVERSE_REGISTRY

    universe = sorted({t for s in ACTIVE_SLEEVES for t in UNIVERSE_REGISTRY[s]})
    return {t: _bars(i) for i, t in enumerate(universe)}


def _live_equity():
    from shared.universe import ACTIVE_SLEEVES

    return {
        sleeve: {d: 10_000.0 for d in SESSIONS}
        for sleeve in ACTIVE_SLEEVES
    }


def _produce(tmp_path, **overrides):
    kwargs = dict(
        output_path=tmp_path / "shadow_20260524.json",
        capital=100_000.0,
        bars_by_ticker=_universe_bars(),
        regime_by_date={d: "bull" for d in SESSIONS},
        fundamentals_lookup=lambda t, d: {
            "roe": 0.3, "debt_equity": 0.4, "profit_margin": 0.2,
        },
        earnings_lookup=lambda t, d: {"surprise_pct": 8.0},
        live_equity=_live_equity(),
        window_sessions=WINDOW,
    )
    kwargs.update(overrides)
    return produce_shadow_artifact(**kwargs)


def test_it_writes_a_readable_artifact(tmp_path) -> None:
    path = _produce(tmp_path)

    artifact = load_shadow(path)

    assert artifact.window_sessions == WINDOW
    assert artifact.shadow_id.startswith("shadow:")
    assert artifact.series, "no sleeve produced a curve"


def test_every_sleeve_with_live_history_gets_a_curve(tmp_path) -> None:
    from shared.universe import ACTIVE_SLEEVES

    artifact = load_shadow(_produce(tmp_path))

    assert set(artifact.series) == set(ACTIVE_SLEEVES)


def test_each_curve_is_seeded_at_that_sleeve_s_live_nav(tmp_path) -> None:
    live = _live_equity()
    live["momentum"] = {d: 41_234.56 for d in SESSIONS}

    artifact = load_shadow(_produce(tmp_path, live_equity=live))

    window_start = SESSIONS[-WINDOW]
    assert artifact.series["momentum"][window_start] == pytest.approx(41_234.56)


def test_the_curve_spans_the_window_not_the_whole_history(tmp_path) -> None:
    artifact = load_shadow(_produce(tmp_path))

    curve = artifact.series["momentum"]
    assert len(curve) == WINDOW
    assert max(curve) == SESSIONS[-1]


def test_the_id_is_stable_across_two_runs_of_the_same_model(tmp_path) -> None:
    """If it moves nightly, breach_streak can never reach its trigger."""
    first = load_shadow(_produce(tmp_path, output_path=tmp_path / "a.json"))
    second = load_shadow(_produce(tmp_path, output_path=tmp_path / "b.json"))

    assert first.shadow_id == second.shadow_id


def test_a_sleeve_with_no_live_history_is_absent_from_the_artifact(tmp_path) -> None:
    live = _live_equity()
    del live["earnings_drift"]

    artifact = load_shadow(_produce(tmp_path, live_equity=live))

    assert "earnings_drift" not in artifact.series
    assert "momentum" in artifact.series


def test_no_aggregate_is_stored(tmp_path) -> None:
    """D15: the aggregate is a derived roll-up the digest recomputes. A stored
    one would be a second authority able to disagree with its own parts."""
    raw = json.loads(_produce(tmp_path).read_text())

    assert "aggregate" not in raw
    assert "AGGREGATE" not in raw.get("series", {})


def test_no_live_history_at_all_writes_an_empty_but_valid_artifact(tmp_path) -> None:
    """Distinct from a missing file: the run happened, nothing was gradeable."""
    artifact = load_shadow(_produce(tmp_path, live_equity={}))

    assert artifact.series == {}
    assert artifact.shadow_id.startswith("shadow:")
