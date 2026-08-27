"""The bridge from sleeve equity curves to the edge-validation framework.

Story KAN-40. ``research/evaluation`` ranks names cross-sectionally by factor
score; a sleeve is a strategy producing a return series. The driver under test
is the bridge: it reads the per-sleeve curves out of a saved backtest artifact,
normalises the two packages' sign and annualisation conventions, and hands the
result to the same DSR / PSR / BH-FDR / holdout / stability machinery a factor
goes through.

The normalisation is the part that fails silently if it is wrong -- ``backtest``
reports max drawdown as a positive fraction and ``research`` as a negative one
-- so it is covered by parity tests against both real implementations rather
than against a hand-written expectation.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from backtest.bias_acceptance import (
    ADMISSIBLE,
    ADMISSIBLE_WITH_ACCEPTED_BIAS,
    INADMISSIBLE,
)
from backtest.metrics import BacktestMetrics
from research.evaluation import metrics as research_metrics
from scripts.run_sleeve_evaluation import (
    ANNUALIZATION_DAYS,
    HOLDOUT_REGISTRY_PATH,
    SLEEVE_PARAMETERS,
    build_mapping,
    demote_one_stage,
    evaluate_mapping,
    main,
    normalized_metrics,
    returns_from_equity_curve,
    sleeve_trial_count,
    to_backtest_max_drawdown,
    to_research_equity_returns,
    to_research_max_drawdown,
)
from shared.universe import ACTIVE_SLEEVES


# --------------------------------------------------------------------------
# Fixtures: a saved multi-portfolio backtest artifact, shrunk to test size.
# --------------------------------------------------------------------------

LIKE_FOR_LIKE_CONFIG = {
    "fill_model": "next_open",
    "slippage_bps": 10.0,
    "commission_per_share": 0.005,
    "commission_minimum": 1.0,
    "point_in_time_universe": True,
    "coverage": {"state": "OK"},
}


def _sessions(n: int, start: date = date(2025, 1, 2)) -> list[date]:
    """``n`` weekday dates -- close enough to a session index for arithmetic."""
    out: list[date] = []
    day = start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def _curve(seed: int, sessions: int, drift: float) -> list[float]:
    """A deterministic equity curve with ``sessions + 1`` points.

    ``portfolio_values[0]`` is the pre-day-0 initial capital, exactly as
    ``backtest.runner`` writes it, so the curve is one longer than ``dates``.
    """
    value = 10_000.0
    values = [value]
    for i in range(sessions):
        # A repeatable wobble around the drift; no RNG, so the fixture is
        # byte-identical across runs and platforms.
        wobble = math.sin((i + seed) * 0.7) * 0.008
        value *= 1.0 + drift + wobble
        values.append(value)
    return values


#: Drifts chosen so the fixture spans the verdict boundary: ``momentum`` is
#: strong enough to clear DSR 0.95 against a search of 8, ``tail_risk_hedge``
#: loses money. A fixture where every sleeve lands on the same side of the
#: verdict would let an inverted ``passes`` flag through green.
FIXTURE_DRIFTS = {
    "momentum": 0.0030,
    "sector_rotation": 0.0006,
    "thematic_momentum": 0.0004,
    "quality_value": 0.0002,
    "earnings_drift": -0.0001,
    "tail_risk_hedge": -0.0004,
}


def _artifact(
    sessions: int = 400, config: dict | None = None, drifts: dict | None = None
) -> dict:
    dates = [d.isoformat() for d in _sessions(sessions)]
    drifts = FIXTURE_DRIFTS if drifts is None else drifts
    portfolios = {}
    for seed, sleeve in enumerate(ACTIVE_SLEEVES):
        values = _curve(seed, sessions, drifts[sleeve])
        portfolios[sleeve] = {
            "config": {"capital": 10_000.0},
            "trades": [],
            "portfolio_values": values,
            "dates": list(dates),
            "metrics": BacktestMetrics.compute(values, []),
            "shadow_candidates": [],
        }
    aggregate_values = [
        sum(p["portfolio_values"][i] for p in portfolios.values())
        for i in range(sessions + 1)
    ]
    return {
        "config": dict(LIKE_FOR_LIKE_CONFIG if config is None else config),
        "portfolios": portfolios,
        "aggregate": {
            "portfolio_values": aggregate_values,
            "trades": [],
            "dates": list(dates),
            "metrics": BacktestMetrics.compute(aggregate_values, []),
        },
        "bars": {},
    }


@pytest.fixture
def artifact_path(tmp_path):
    path = tmp_path / "backtest_multi_20260817_000000.json"
    path.write_text(json.dumps(_artifact()))
    return path


@pytest.fixture
def holdout_registry(tmp_path):
    """A registered, unspent split whose boundary lands inside the fixture."""
    path = tmp_path / "holdout_registry.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "splits": [
                    {
                        "split_id": "incumbent_sleeves_2026",
                        "holdout_start": "2026-04-01",
                        "horizon": 21,
                        "embargo": 21,
                        "registered_at": "2026-01-01T00:00:00+00:00",
                        "note": "test split",
                    }
                ],
                "evaluations": [],
            }
        )
    )
    return path


# --------------------------------------------------------------------------
# AC2 -- sign and annualisation normalisation, tested as parity
# --------------------------------------------------------------------------


def test_sharpe_is_identical_through_the_backtest_and_research_paths():
    """One curve, two packages, one number.

    ``backtest._sharpe_ratio`` builds daily returns off the equity curve and
    annualises by a fixed 252; ``research.evaluation.metrics.sharpe`` takes a
    return series and a caller-supplied periods-per-year. They agree only if
    the bridge hands over daily returns and 252 -- pass 252/21 (the factor
    evaluator's horizon-scaled value) and the two disagree by a factor of 3.5.
    """
    sessions = 200
    values = _curve(seed=3, sessions=sessions, drift=0.0007)
    dates = _sessions(sessions)

    backtest_sharpe = BacktestMetrics.compute(values, [])["sharpe_ratio"]
    returns = returns_from_equity_curve(values, dates)
    research_sharpe = research_metrics.sharpe(
        pd.Series(returns, index=pd.to_datetime(dates)), ANNUALIZATION_DAYS
    )

    assert ANNUALIZATION_DAYS == 252.0
    assert research_sharpe == pytest.approx(backtest_sharpe, rel=1e-12, abs=1e-12)
    assert normalized_metrics(values, dates)["sharpe"] == pytest.approx(
        backtest_sharpe, rel=1e-12, abs=1e-12
    )


def test_max_drawdown_is_same_magnitude_opposite_sign_across_the_two_packages():
    """``backtest`` returns +0.16 for the same drawdown ``research`` calls -0.16.

    A bridge that forwarded the number unchanged would report a *positive*
    max drawdown into a package whose comparisons assume the negative
    convention, and every "worse than" test would silently invert.
    """
    sessions = 200
    values = _curve(seed=11, sessions=sessions, drift=-0.0003)
    dates = _sessions(sessions)

    backtest_dd = BacktestMetrics.compute(values, [])["max_drawdown"]
    returns = returns_from_equity_curve(values, dates)
    research_dd = research_metrics.max_drawdown(
        pd.Series(to_research_equity_returns(returns))
    )

    assert backtest_dd > 0
    assert research_dd < 0
    assert abs(backtest_dd) == pytest.approx(abs(research_dd), rel=1e-9, abs=1e-12)
    assert to_research_max_drawdown(backtest_dd) == pytest.approx(research_dd, abs=1e-12)
    assert to_backtest_max_drawdown(research_dd) == pytest.approx(backtest_dd, abs=1e-12)
    assert normalized_metrics(values, dates)["max_drawdown"] == pytest.approx(
        research_dd, abs=1e-12
    )


def test_a_drawdown_that_starts_on_day_one_is_not_lost_in_translation():
    """``research.max_drawdown`` starts its equity curve at the first *return*.

    So a curve whose peak is its initial capital -- it falls on day 1 and never
    recovers past the start -- measures zero drawdown through the naive research
    path and a real one through the backtest path. Padding a leading ``0.0``
    return restores the day-0 peak, and the two agree again. Without the pad
    every sleeve that opened badly would report a flattered drawdown into the
    gate.
    """
    dates = _sessions(2)
    values = [100.0, 90.0, 100.0]

    returns = returns_from_equity_curve(values, dates)
    naive = research_metrics.max_drawdown(pd.Series(returns))
    padded = research_metrics.max_drawdown(pd.Series(to_research_equity_returns(returns)))

    assert BacktestMetrics.compute(values, [])["max_drawdown"] == pytest.approx(0.10)
    assert naive == pytest.approx(0.0)
    assert padded == pytest.approx(-0.10)
    assert normalized_metrics(values, dates)["max_drawdown"] == pytest.approx(-0.10)


def test_returns_are_aligned_to_dates_not_to_the_initial_capital_point():
    """``portfolio_values`` is one longer than ``dates``; the extra point is day -1.

    Zipping the two naively shifts every return back one session, which is a
    look-ahead of exactly one day through the whole evaluation.
    """
    dates = _sessions(3)
    values = [100.0, 110.0, 121.0, 121.0]

    returns = returns_from_equity_curve(values, dates)

    assert returns == pytest.approx([0.10, 0.10, 0.0])
    assert len(returns) == len(dates)


def test_a_curve_that_does_not_match_its_date_index_is_rejected():
    with pytest.raises(ValueError, match="portfolio_values"):
        returns_from_equity_curve([100.0, 101.0, 102.0], _sessions(7))


# --------------------------------------------------------------------------
# The mapping file -- the artifact that crosses the architectural boundary
# --------------------------------------------------------------------------


def test_build_mapping_produces_a_well_formed_file_from_a_saved_artifact(artifact_path):
    payload = json.loads(artifact_path.read_text())

    mapping = build_mapping(payload, str(artifact_path))

    assert set(mapping["sleeves"]) == set(ACTIVE_SLEEVES)
    assert mapping["conventions"]["periods_per_year"] == 252.0
    assert mapping["conventions"]["max_drawdown_sign"] == "negative"
    assert mapping["baseline"]["is_like_for_like"] is True
    for sleeve, row in mapping["sleeves"].items():
        assert len(row["returns"]) == len(row["dates"]) == 400
        # The mapping carries research-convention numbers only; a consumer that
        # finds a positive max drawdown here is reading a stale file.
        assert row["normalized_metrics"]["max_drawdown"] <= 0.0
        assert row["backtest_metrics"]["max_drawdown"] >= 0.0
        assert row["normalized_metrics"]["sharpe"] == pytest.approx(
            row["backtest_metrics"]["sharpe_ratio"], rel=1e-12, abs=1e-12
        )


def test_build_mapping_refuses_an_artifact_missing_an_active_sleeve(artifact_path):
    payload = json.loads(artifact_path.read_text())
    payload["portfolios"].pop("tail_risk_hedge")

    with pytest.raises(ValueError, match="tail_risk_hedge"):
        build_mapping(payload, str(artifact_path))


def test_build_mapping_records_why_a_legacy_baseline_is_not_like_for_like():
    payload = _artifact(sessions=120, config={"slippage_bps": 10.0})

    mapping = build_mapping(payload, "output/legacy.json")

    assert mapping["baseline"]["is_like_for_like"] is False
    reasons = " ".join(mapping["baseline"]["unmet_requirements"])
    assert "next_open" in reasons
    assert "survivorship" in reasons


# --------------------------------------------------------------------------
# AC1 -- the declared trial count is 8, and it comes from the committed history
# --------------------------------------------------------------------------


def test_sleeve_trial_count_is_eight_from_the_committed_registry():
    """Eight candidate sleeves were searched; six survived (2026-05-26).

    The registry's *total* is larger because it also carries the factor
    catalog's four -- a search the sleeves could not have drawn from, so
    deflating against it would be over-deflation. The count is scoped to the
    sleeve-selection entry, and that entry is the memo's cited justification.
    """
    assert sleeve_trial_count() == 8


def test_sleeve_trial_count_refuses_a_registry_with_no_sleeve_search(tmp_path):
    path = tmp_path / "trial_registry.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {
                        "searched_at": "2026-08-02",
                        "what": "factors only",
                        "n_trials": 4,
                        "source": "research/factors/catalog.py",
                    }
                ],
            }
        )
    )

    with pytest.raises(ValueError, match="sleeve-selection"):
        sleeve_trial_count(path)


# --------------------------------------------------------------------------
# AC3/AC5 -- the six-sleeve evaluation, end to end
# --------------------------------------------------------------------------


def test_every_active_sleeve_has_a_named_load_bearing_parameter():
    """AC3 requires a stability report per sleeve, which requires a named parameter.

    Pinning the table against ``ACTIVE_SLEEVES`` means adding a seventh sleeve
    fails here rather than producing a memo that quietly covers six of seven.
    """
    assert set(SLEEVE_PARAMETERS) == set(ACTIVE_SLEEVES)
    for sleeve, spec in SLEEVE_PARAMETERS.items():
        assert spec.parameter
        assert spec.why
        assert isinstance(spec.shipped_value, (int, float))


def test_six_sleeve_evaluation_runs_end_to_end(artifact_path, holdout_registry):
    payload = json.loads(artifact_path.read_text())
    mapping = build_mapping(payload, str(artifact_path))

    result = evaluate_mapping(
        mapping,
        n_trials=8,
        holdout_registry_path=holdout_registry,
        holdout_split_id="incumbent_sleeves_2026",
    )

    assert result["n_trials"] == 8
    assert set(result["sleeves"]) == set(ACTIVE_SLEEVES)
    assert result["holdout"]["split_id"] == "incumbent_sleeves_2026"
    assert result["holdout"]["n_sessions"] > 0
    for sleeve, row in result["sleeves"].items():
        for key in (
            "sharpe",
            "probabilistic_sharpe",
            "deflated_sharpe",
            "passes_dsr",
            "passes_fdr",
            "survives_multiple_testing",
            "max_drawdown",
            "verdict",
        ):
            assert key in row, f"{sleeve} is missing {key}"
        # Deflating against a search of 8 can only make the claim harder.
        assert row["deflated_sharpe"] <= row["probabilistic_sharpe"] + 1e-12
        assert row["holdout"]["n_sessions"] == result["holdout"]["n_sessions"]
        assert row["stability"]["available"] is False
        assert row["stability"]["parameter"] == SLEEVE_PARAMETERS[sleeve].parameter
        assert row["verdict"] in ("PASS", "FAIL")


def test_the_verdict_follows_the_multiple_testing_result_in_both_directions(
    artifact_path, holdout_registry
):
    """The fixture deliberately spans the boundary.

    Without a passing sleeve *and* a failing one, inverting ``passes`` in the
    driver leaves the suite green while every recommendation flips.
    """
    mapping = build_mapping(json.loads(artifact_path.read_text()), str(artifact_path))

    result = evaluate_mapping(
        mapping,
        n_trials=8,
        holdout_registry_path=holdout_registry,
        holdout_split_id="incumbent_sleeves_2026",
    )

    strong = result["sleeves"]["momentum"]
    weak = result["sleeves"]["tail_risk_hedge"]

    assert strong["passes_dsr"] is True
    assert strong["deflated_sharpe"] >= 0.95
    assert strong["survives_multiple_testing"] is True
    assert strong["verdict"] == "PASS"
    assert strong["recommended_stage"] == "paper"

    assert weak["passes_dsr"] is False
    assert weak["survives_multiple_testing"] is False
    assert weak["verdict"] == "FAIL"
    # AC5: one stage down, recorded, and nothing deleted.
    assert weak["current_stage"] == "paper"
    assert weak["recommended_stage"] == "shadow"
    assert "operator" in weak["recommendation"].lower()


def test_the_in_sample_figure_is_the_purged_training_span(
    artifact_path, holdout_registry
):
    """The full-sample Sharpe contains the holdout; the comparator must not.

    Reporting only ``sharpe`` beside ``holdout.sharpe`` invites the reader to
    treat an overlapping pair as in-sample vs out-of-sample.
    """
    mapping = build_mapping(json.loads(artifact_path.read_text()), str(artifact_path))

    result = evaluate_mapping(
        mapping,
        n_trials=8,
        holdout_registry_path=holdout_registry,
        holdout_split_id="incumbent_sleeves_2026",
    )

    holdout = result["holdout"]
    total = len(mapping["sleeves"]["momentum"]["returns"])
    row = result["sleeves"]["momentum"]
    # train + purge(gap) + holdout partitions the index exactly.
    assert row["in_sample"]["n_sessions"] + holdout["gap"] + holdout["n_sessions"] == total
    assert row["in_sample"]["n_sessions"] < total


def test_the_holdout_window_starts_on_the_registered_boundary(
    artifact_path, holdout_registry
):
    mapping = build_mapping(json.loads(artifact_path.read_text()), str(artifact_path))

    result = evaluate_mapping(
        mapping,
        n_trials=8,
        holdout_registry_path=holdout_registry,
        holdout_split_id="incumbent_sleeves_2026",
    )

    holdout = result["holdout"]
    dates = mapping["sleeves"]["momentum"]["dates"]
    assert holdout["first_session"] >= holdout["holdout_start"]
    assert holdout["last_session"] == dates[-1]
    # Nothing before the boundary leaked into the window.
    window = dates[-holdout["n_sessions"]:]
    assert all(d >= holdout["holdout_start"] for d in window)
    assert dates[-holdout["n_sessions"] - 1] < holdout["holdout_start"]


def test_sleeves_on_different_calendars_are_refused():
    """One split, one calendar. Otherwise every sleeve gets a different window.

    ``build_mapping`` validates each curve against its own dates but the
    holdout indices are resolved once and applied to all six, so a short sleeve
    would silently be sliced to a shorter window and reported under the long
    one's session count.
    """
    payload = _artifact(sessions=400)
    short = _artifact(sessions=340)
    payload["portfolios"]["tail_risk_hedge"] = short["portfolios"]["tail_risk_hedge"]

    with pytest.raises(ValueError, match="date index"):
        build_mapping(payload, "output/ragged.json")


def test_the_holdout_is_spent_exactly_once(artifact_path, holdout_registry):
    payload = json.loads(artifact_path.read_text())
    mapping = build_mapping(payload, str(artifact_path))

    evaluate_mapping(
        mapping,
        n_trials=8,
        holdout_registry_path=holdout_registry,
        holdout_split_id="incumbent_sleeves_2026",
    )
    burns = json.loads(holdout_registry.read_text())["evaluations"]
    assert len(burns) == 1

    from research.evaluation.holdout import HoldoutAlreadyEvaluated

    with pytest.raises(HoldoutAlreadyEvaluated):
        evaluate_mapping(
            mapping,
            n_trials=8,
            holdout_registry_path=holdout_registry,
            holdout_split_id="incumbent_sleeves_2026",
        )


def _registry_with(tmp_path, registered_at: str):
    tmp_path.mkdir(parents=True, exist_ok=True)
    registry = tmp_path / "holdout_registry.json"
    registry.write_text(
        json.dumps(
            {
                "version": 1,
                "splits": [
                    {
                        "split_id": "incumbent_sleeves_2026",
                        "holdout_start": "2026-04-01",
                        "horizon": 21,
                        "embargo": 21,
                        "registered_at": registered_at,
                        "note": "test split",
                    }
                ],
                "evaluations": [],
            }
        )
    )
    return registry


def test_evaluation_refuses_a_holdout_registered_after_the_run(
    artifact_path, tmp_path
):
    """AC4 -- the registration must predate the look, or it is not one.

    ``registered_at`` is caller-supplied and therefore backdatable, so this is
    not proof; it is the one direction the code *can* refuse, and a future
    timestamp means the registry was written during the run.
    """
    registry = _registry_with(tmp_path, "2099-01-01T00:00:00+00:00")
    mapping = build_mapping(json.loads(artifact_path.read_text()), str(artifact_path))

    with pytest.raises(ValueError, match="registered"):
        evaluate_mapping(
            mapping,
            n_trials=8,
            holdout_registry_path=registry,
            holdout_split_id="incumbent_sleeves_2026",
        )
    assert json.loads(registry.read_text())["evaluations"] == []


def test_the_registration_check_compares_instants_not_strings(
    artifact_path, tmp_path
):
    """A non-UTC offset breaks a lexical compare in both directions.

    ``2026-...T09:00+13:00`` is 20:00Z the previous day -- genuinely past --
    but sorts *above* a UTC "now" string. The mirror case, an offset that
    hides a future timestamp below "now", is the dangerous one: it would let a
    registration written during the run through and spend the split on it.
    """
    now = datetime.now(timezone.utc)
    honest_past = (now - timedelta(hours=6)).astimezone(
        timezone(timedelta(hours=13))
    ).isoformat()
    disguised_future = (now + timedelta(hours=6)).astimezone(
        timezone(timedelta(hours=-11))
    ).isoformat()
    mapping = build_mapping(json.loads(artifact_path.read_text()), str(artifact_path))

    # Sorts above "now" as a string, but is really in the past: accepted.
    accepted = _registry_with(tmp_path / "a", honest_past)
    evaluate_mapping(
        mapping,
        n_trials=8,
        holdout_registry_path=accepted,
        holdout_split_id="incumbent_sleeves_2026",
    )

    # Sorts below "now" as a string, but is really in the future: refused.
    refused = _registry_with(tmp_path / "b", disguised_future)
    assert disguised_future < now.isoformat(), "fixture no longer exercises the trap"
    with pytest.raises(ValueError, match="in the future"):
        evaluate_mapping(
            mapping,
            n_trials=8,
            holdout_registry_path=refused,
            holdout_split_id="incumbent_sleeves_2026",
        )


def _write_sweep(stability_dir, sleeve: str, filename: str | None = None, **overrides):
    stability_dir.mkdir(parents=True, exist_ok=True)
    spec = SLEEVE_PARAMETERS[sleeve]
    artifact = {
        "sleeve": sleeve,
        "parameter": spec.parameter,
        "center": float(spec.shipped_value),
        "metric": "sharpe_ratio",
        "point_in_time_universe": True,
        "stability": {
            "parameter": spec.parameter,
            "center": spec.shipped_value,
            "is_plateau": True,
            "relative_degradation": 0.05,
            "verdict_reason": "flat enough",
        },
    }
    artifact.update(overrides)
    name = filename or f"{sleeve}-{spec.parameter}.json"
    (stability_dir / name).write_text(json.dumps(artifact))
    return stability_dir / name


def test_stability_artifacts_are_attached_to_the_sleeve_that_produced_them(
    artifact_path, holdout_registry, tmp_path
):
    """Matched on the artifact's ``sleeve`` field, not on its filename.

    ``run_stability_sweep.py`` takes a free-form ``--out``, so a correct sweep
    saved under an unexpected name must still be found.
    """
    stability_dir = tmp_path / "stability"
    _write_sweep(stability_dir, "momentum", filename="sweep-run-7.json")
    mapping = build_mapping(json.loads(artifact_path.read_text()), str(artifact_path))

    result = evaluate_mapping(
        mapping,
        n_trials=8,
        holdout_registry_path=holdout_registry,
        holdout_split_id="incumbent_sleeves_2026",
        stability_dir=stability_dir,
    )

    momentum = result["sleeves"]["momentum"]["stability"]
    assert momentum["available"] is True
    assert momentum["is_plateau"] is True
    assert momentum["center"] == float(SLEEVE_PARAMETERS["momentum"].shipped_value)
    assert result["sleeves"]["quality_value"]["stability"]["available"] is False
    assert result["gate_valid"] == ADMISSIBLE


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        # A sweep of a parameter the memo does not name is not the memo's
        # evidence.
        ({"parameter": "position_size_pct", "center": 0.12}, "position_size_pct"),
        # ``is_plateau`` is a claim about the neighbourhood of the center, so a
        # surface centered elsewhere answers a different question. --center is
        # the easiest of the five sweep arguments to forget to change when
        # copying the command for the next sleeve.
        ({"center": 63.0}, "centered on"),
    ],
)
def test_a_stability_artifact_that_does_not_match_the_named_parameter_is_refused(
    artifact_path, holdout_registry, tmp_path, overrides, expected
):
    stability_dir = tmp_path / "stability"
    _write_sweep(stability_dir, "momentum", **overrides)
    mapping = build_mapping(json.loads(artifact_path.read_text()), str(artifact_path))

    with pytest.raises(ValueError, match=expected):
        evaluate_mapping(
            mapping,
            n_trials=8,
            holdout_registry_path=holdout_registry,
            holdout_split_id="incumbent_sleeves_2026",
            stability_dir=stability_dir,
        )


def test_a_rejected_stability_artifact_does_not_cost_the_holdout(
    artifact_path, holdout_registry, tmp_path
):
    """The burn is irreversible; a mistyped sweep must cost a re-run, not the split.

    Following the memo's own runbook, an operator who re-runs one sweep with
    the wrong ``--parameter`` and then runs the evaluation would otherwise
    permanently spend ``incumbent_sleeves_2026`` and get no output at all --
    and un-burning it by hand is exactly what the protocol exists to prevent.
    """
    stability_dir = tmp_path / "stability"
    _write_sweep(stability_dir, "momentum", parameter="position_size_pct", center=0.12)
    mapping = build_mapping(json.loads(artifact_path.read_text()), str(artifact_path))

    with pytest.raises(ValueError):
        evaluate_mapping(
            mapping,
            n_trials=8,
            holdout_registry_path=holdout_registry,
            holdout_split_id="incumbent_sleeves_2026",
            stability_dir=stability_dir,
        )

    assert json.loads(holdout_registry.read_text())["evaluations"] == []


def test_two_artifacts_claiming_the_same_sleeve_are_refused(
    artifact_path, holdout_registry, tmp_path
):
    stability_dir = tmp_path / "stability"
    _write_sweep(stability_dir, "momentum", filename="run-a.json")
    _write_sweep(stability_dir, "momentum", filename="run-b.json")
    mapping = build_mapping(json.loads(artifact_path.read_text()), str(artifact_path))

    with pytest.raises(ValueError, match="claim sleeve"):
        evaluate_mapping(
            mapping,
            n_trials=8,
            holdout_registry_path=holdout_registry,
            holdout_split_id="incumbent_sleeves_2026",
            stability_dir=stability_dir,
        )


def test_a_survivorship_biased_surface_taints_the_run(
    artifact_path, holdout_registry, tmp_path
):
    """The driver refuses a biased *baseline*; a biased *surface* is no better.

    Every point on a surface swept over a present-day ticker list is inflated,
    so a plateau measured on it is not evidence either.
    """
    stability_dir = tmp_path / "stability"
    _write_sweep(stability_dir, "momentum", point_in_time_universe=False)
    mapping = build_mapping(json.loads(artifact_path.read_text()), str(artifact_path))

    result = evaluate_mapping(
        mapping,
        n_trials=8,
        holdout_registry_path=holdout_registry,
        holdout_split_id="incumbent_sleeves_2026",
        stability_dir=stability_dir,
    )

    assert result["sleeves"]["momentum"]["stability"]["available"] is True
    assert result["sleeves"]["momentum"]["stability"]["admissible"] is False
    assert result["inadmissible_stability_surfaces"] == ["momentum"]
    assert result["gate_valid"] == INADMISSIBLE


def test_demotion_is_one_stage_at_a_time_and_never_deletion():
    assert demote_one_stage("live") == "paper"
    assert demote_one_stage("paper") == "shadow"
    # Below shadow there is nothing but retirement, which needs a logged
    # decision -- the evaluation never recommends it.
    assert demote_one_stage("shadow") == "shadow"


# --------------------------------------------------------------------------
# The CLI, including the refusal that keeps a biased baseline out of the gate
# --------------------------------------------------------------------------


def test_cli_refuses_a_baseline_that_is_not_like_for_like(tmp_path, capsys):
    artifact = tmp_path / "backtest_multi_legacy.json"
    artifact.write_text(json.dumps(_artifact(config={"slippage_bps": 10.0})))

    code = main(
        [
            "--backtest",
            str(artifact),
            "--output-dir",
            str(tmp_path / "out"),
            "--holdout-registry",
            str(tmp_path / "missing.json"),
        ]
    )

    assert code == 3
    out = capsys.readouterr().out
    # "not like-for-like" is no longer the reason for refusing: since KAN-68 a
    # baseline can be not-like-for-like and still admissible, so the refusal
    # names admissibility instead.
    assert "not admissible evidence" in out
    assert "same_bar" in out
    assert not (tmp_path / "out").exists()


def test_cli_writes_a_mapping_and_an_evaluation(
    artifact_path, holdout_registry, tmp_path, capsys
):
    out_dir = tmp_path / "out"

    code = main(
        [
            "--backtest",
            str(artifact_path),
            "--output-dir",
            str(out_dir),
            "--holdout-registry",
            str(holdout_registry),
        ]
    )

    assert code == 0
    mapping_files = list(out_dir.glob("sleeve_returns_*.json"))
    evaluation_files = list(out_dir.glob("sleeve_evaluation_*.json"))
    assert len(mapping_files) == 1
    assert len(evaluation_files) == 1

    evaluation = json.loads(evaluation_files[0].read_text())
    assert evaluation["n_trials"] == 8
    assert evaluation["gate_valid"] == ADMISSIBLE
    assert evaluation["mapping"] == str(mapping_files[0])
    assert set(evaluation["sleeves"]) == set(ACTIVE_SLEEVES)
    assert "divergence-fidelity is not edge" in evaluation["disclaimer"]

    out = capsys.readouterr().out
    for sleeve in ACTIVE_SLEEVES:
        assert sleeve in out


def test_cli_taints_the_output_when_the_override_is_used(tmp_path, holdout_registry):
    artifact = tmp_path / "backtest_multi_legacy.json"
    artifact.write_text(json.dumps(_artifact(config={"slippage_bps": 10.0})))
    out_dir = tmp_path / "out"

    code = main(
        [
            "--backtest",
            str(artifact),
            "--output-dir",
            str(out_dir),
            "--holdout-registry",
            str(holdout_registry),
            "--allow-non-comparable-baseline",
        ]
    )

    assert code == 0
    evaluation = json.loads(next(out_dir.glob("sleeve_evaluation_*.json")).read_text())
    assert evaluation["gate_valid"] == INADMISSIBLE
    assert evaluation["baseline"]["is_like_for_like"] is False


def test_cli_will_not_spend_the_registry_of_record_on_a_gate_invalid_run(
    tmp_path, capsys
):
    """The refusal names the harm; the override must not then cause it.

    Without ``--holdout-registry`` the default is the committed
    ``research/holdout_registry.json``, so "just seeing the numbers" against a
    biased baseline would permanently spend the split of record.
    """
    artifact = tmp_path / "backtest_multi_legacy.json"
    artifact.write_text(json.dumps(_artifact(config={"slippage_bps": 10.0})))

    code = main(
        [
            "--backtest",
            str(artifact),
            "--output-dir",
            str(tmp_path / "out"),
            "--allow-non-comparable-baseline",
        ]
    )

    assert code == 3
    assert "REFUSING the override too" in capsys.readouterr().out
    assert json.loads(HOLDOUT_REGISTRY_PATH.read_text())["evaluations"] == []


def test_cli_refuses_a_trial_count_below_the_declared_search(
    artifact_path, holdout_registry, tmp_path
):
    """The override models a *larger* search; shrinking it shrinks SR*.

    ``control()`` only rejects a count below the number of candidates scored
    (6), so ``--n-trials 6`` would otherwise be honoured and make every sleeve
    easier to pass.
    """
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "--backtest",
                str(artifact_path),
                "--output-dir",
                str(tmp_path / "out"),
                "--holdout-registry",
                str(holdout_registry),
                "--n-trials",
                "6",
            ]
        )

    assert excinfo.value.code == 2
    assert json.loads(holdout_registry.read_text())["evaluations"] == []


# --------------------------------------------------------------------------
# Accepted bias (KAN-68) -- the narrow path between refusal and the blanket
# override
# --------------------------------------------------------------------------

#: Like-for-like in every respect except the coverage floor. These are the real
#: figures D18 accepted, measured on the KAN-52 PIT baseline.
BLOCKED_CONFIG = {
    **LIKE_FOR_LIKE_CONFIG,
    "coverage": {
        "state": "BLOCKED",
        "total_membership_days": 1_265_893,
        "excluded_membership_days": 142_856,
        "excluded_pct": 11.284998021159765,
        "floor_pct": 5.0,
    },
}


@pytest.fixture
def blocked_artifact(tmp_path):
    path = tmp_path / "backtest_multi_20260819_183451.json"
    path.write_text(json.dumps(_artifact(config=BLOCKED_CONFIG)))
    return path


def _acceptance_registry(tmp_path, artifact, **overrides):
    """A committed-shaped registry accepting ``artifact``'s coverage bias."""
    entry = {
        "decision": "D18",
        "requirement": "coverage_floor",
        "source_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "excluded_pct": 11.28,
        "floor_pct": 5.0,
        "accepted_at": "2026-08-26",
        "direction": "survivorship-biased upward by an unmeasured amount",
        "re_evidence": "3 years of forward capture",
        "doc": "docs/designs/project-direction.md",
    }
    entry.update(overrides)
    path = tmp_path / "bias_acceptances.json"
    path.write_text(
        json.dumps({"note": "the commit is the evidence", "version": 1,
                    "acceptances": [entry]})
    )
    return path


def test_cli_refuses_a_coverage_blocked_baseline_with_no_acceptance(
    blocked_artifact, tmp_path, capsys
):
    """The pre-KAN-68 refusal is unchanged when nothing accepts the bias."""
    empty = tmp_path / "empty_acceptances.json"
    empty.write_text(json.dumps({"note": "none", "version": 1, "acceptances": []}))

    code = main(
        [
            "--backtest", str(blocked_artifact),
            "--output-dir", str(tmp_path / "out"),
            "--holdout-registry", str(tmp_path / "missing.json"),
            "--bias-acceptances", str(empty),
        ]
    )

    assert code == 3
    assert not (tmp_path / "out").exists()


def test_cli_evaluates_a_coverage_blocked_baseline_the_registry_accepts(
    blocked_artifact, holdout_registry, tmp_path, capsys
):
    """The whole point of the story: this run is admissible, and says why."""
    out_dir = tmp_path / "out"

    code = main(
        [
            "--backtest", str(blocked_artifact),
            "--output-dir", str(out_dir),
            "--holdout-registry", str(holdout_registry),
            "--bias-acceptances", str(_acceptance_registry(tmp_path, blocked_artifact)),
        ]
    )

    assert code == 0
    evaluation = json.loads(next(out_dir.glob("sleeve_evaluation_*.json")).read_text())
    assert evaluation["gate_valid"] == ADMISSIBLE_WITH_ACCEPTED_BIAS

    # D18: the bias is accepted in the record, NOT by relaxing the gate.
    assert evaluation["baseline"]["is_like_for_like"] is False
    assert evaluation["baseline"]["coverage_state"] == "BLOCKED"

    stamp = evaluation["baseline"]["accepted_bias"]
    assert stamp["decision"] == "D18"
    assert stamp["excluded_pct"] == pytest.approx(11.28, abs=0.01)
    assert stamp["floor_pct"] == 5.0
    assert "survivorship-biased upward" in stamp["direction"]

    out = capsys.readouterr().out
    assert "ACCEPTED BIAS" in out
    assert "D18" in out
    assert "11.28" in out


def test_cli_refuses_an_acceptance_pinned_to_a_different_artifact(
    blocked_artifact, tmp_path, capsys
):
    """An acceptance is spent on one artifact; it cannot bless the next one."""
    registry = _acceptance_registry(
        tmp_path, blocked_artifact, source_sha256="0" * 64
    )

    code = main(
        [
            "--backtest", str(blocked_artifact),
            "--output-dir", str(tmp_path / "out"),
            "--holdout-registry", str(tmp_path / "missing.json"),
            "--bias-acceptances", str(registry),
        ]
    )

    assert code == 3
    assert "sha256" in capsys.readouterr().out


def test_cli_refuses_a_same_bar_baseline_even_when_an_acceptance_matches(
    tmp_path, capsys
):
    """The narrowing rule, end to end.

    A same-bar baseline holding a valid coverage acceptance must still be
    refused: what was accepted was the coverage floor, not "whatever else is
    wrong with this artifact".
    """
    artifact = tmp_path / "backtest_multi_same_bar.json"
    artifact.write_text(
        json.dumps(_artifact(config={**BLOCKED_CONFIG, "fill_model": "same_bar"}))
    )

    code = main(
        [
            "--backtest", str(artifact),
            "--output-dir", str(tmp_path / "out"),
            "--holdout-registry", str(tmp_path / "missing.json"),
            "--bias-acceptances", str(_acceptance_registry(tmp_path, artifact)),
        ]
    )

    assert code == 3
    assert not (tmp_path / "out").exists()


def test_the_gate_invalid_banner_still_prints_now_that_the_state_is_a_string(
    tmp_path, holdout_registry, capsys
):
    """Regression guard on the bool -> string change.

    ``_print_summary`` used to read ``if not evaluation["gate_valid"]``. Every
    one of the three states is a non-empty string, so a naive migration leaves
    that condition permanently false and silently stops printing the banner
    that exists to stop an inadmissible run being cited.
    """
    artifact = tmp_path / "backtest_multi_legacy.json"
    artifact.write_text(json.dumps(_artifact(config={"slippage_bps": 10.0})))

    code = main(
        [
            "--backtest", str(artifact),
            "--output-dir", str(tmp_path / "out"),
            "--holdout-registry", str(holdout_registry),
            "--allow-non-comparable-baseline",
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "GATE-INVALID" in out
