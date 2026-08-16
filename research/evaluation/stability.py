# research/evaluation/stability.py
"""Parameter stability -- an edge that lives on one parameter value is not an edge.

A backtest optimum found by searching a grid is the maximum of a *noisy*
surface. If the momentum sleeve earns a Sharpe of 2.0 at a 126-day lookback and
0.1 at 120 and 132, nothing about the 126-day result is real: the search found
the highest bump in the noise, and out-of-sample testing on that single point
cannot tell you so, because the point itself is what was fitted. Direction doc
D10 requires the surface around the shipped value to be measured, not just the
value.

The instrument is deliberately dumb: it consumes a precomputed
``{parameter value -> metric}`` mapping and does arithmetic on it. It runs no
backtest and reads no file. That is what keeps it inside ``research/``, which
may not import ``backtest`` or ``scripts``
(``tests/research/test_architecture.py``); the sweep that produces the mapping
lives in ``scripts/run_stability_sweep.py`` and crosses the boundary as a file,
the way ``--bars-from-json`` already does. It is also what makes the verdict
testable without a ten-year replay.

The plateau criterion is pinned here rather than left to each caller:

* the neighborhood mean must be no more than ``plateau_tolerance`` below the
  center metric, measured as a fraction of the center's magnitude; **and**
* no neighbor may lose money while the center makes it.

A center that *outperforms* its neighbors by more than the tolerance is the
signature of fitting, and it fails. Note what this does not claim: a stable
surface is not a profitable one. A sleeve that loses money at every point on
the grid is perfectly stable, and ``is_plateau`` will say so. Profitability is
a separate gate.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from statistics import fmean, stdev

# 30%: wide enough that ordinary sampling noise in a Sharpe estimate does not
# fail a genuinely flat surface, tight enough that the 2.0-vs-0.1 knife's edge
# above cannot survive. Callers grading a capital decision may tighten it.
DEFAULT_PLATEAU_TOLERANCE = 0.30

# Grid values arrive as floats that were printed and re-parsed (0.135 does not
# round-trip through every path), so the center is matched with a tolerance
# rather than by identity. Verdicts must not turn on binary dust either.
_FLOAT_REL_TOL = 1e-9
_FLOAT_ABS_TOL = 1e-12


@dataclass(frozen=True)
class StabilityReport:
    """The surface around one parameter value, and the verdict on it."""

    parameter: str
    center: float
    #: Neighbor parameter values, ascending -- the center is excluded, because
    #: averaging it into its own baseline would flatten every knife's edge.
    neighborhood: list[float]
    #: Metric at each entry of ``neighborhood``, same order.
    metrics: list[float]
    center_metric: float
    neighborhood_mean: float
    neighborhood_std: float
    #: ``(center - neighborhood_mean) / |center|``. Positive means the center
    #: beats its surroundings, which is the direction that indicates fitting.
    relative_degradation: float
    is_plateau: bool
    verdict_reason: str


def _resolve_center(results: Mapping[float, float], center: float) -> float:
    """Return the key in ``results`` that *is* ``center``, or raise."""
    if center in results:
        return center
    matches = [
        key
        for key in results
        if math.isclose(key, center, rel_tol=_FLOAT_REL_TOL, abs_tol=_FLOAT_ABS_TOL)
    ]
    if len(matches) == 1:
        return matches[0]
    raise ValueError(
        f"the center value {center!r} was not scored in this surface "
        f"(scored: {sorted(results)!r}); a stability verdict is a claim about "
        "the shipped value, so it has to be one of the points measured"
    )


def parameter_stability(
    results: Mapping[float, float],
    *,
    center: float,
    parameter: str = "",
    plateau_tolerance: float = DEFAULT_PLATEAU_TOLERANCE,
) -> StabilityReport:
    """Judge whether ``center`` sits on a plateau of the metric surface.

    Args:
        results: Precomputed ``{parameter value: metric}``. Higher metric is
            better; Sharpe is the intended one. Produced outside ``research/``
            -- see ``scripts/run_stability_sweep.py``.
        center: The shipped parameter value. Must be one of the scored points.
        parameter: Name of the parameter, carried into the report for the
            artifact's benefit.
        plateau_tolerance: Fraction of the center metric the neighborhood mean
            may fall below before the center is called a knife's edge. The
            boundary is inclusive.

    Raises:
        ValueError: if the tolerance is negative, the center was not scored,
            the center metric is zero (relative degradation would be undefined
            and the verdict would divide by zero), or fewer than two neighbors
            were scored.
    """
    if plateau_tolerance < 0:
        raise ValueError(
            f"plateau_tolerance must be non-negative; got {plateau_tolerance!r}"
        )

    center_key = _resolve_center(results, center)
    center_metric = float(results[center_key])
    if center_metric == 0.0:
        raise ValueError(
            "relative degradation is undefined against a zero center metric; "
            "a parameter value that scores exactly zero has no edge to be "
            "stable about"
        )

    neighborhood = sorted(key for key in results if key != center_key)
    if len(neighborhood) < 2:
        raise ValueError(
            f"a stability verdict needs at least 2 neighbors around the "
            f"center; got {len(neighborhood)}. One neighbor cannot tell a "
            "plateau from a slope, and none cannot tell anything at all"
        )
    metrics = [float(results[key]) for key in neighborhood]

    mean = fmean(metrics)
    std = stdev(metrics)
    degradation = (center_metric - mean) / abs(center_metric)

    losing = [
        (value, metric)
        for value, metric in zip(neighborhood, metrics)
        if metric < 0
    ]
    within_tolerance = degradation <= plateau_tolerance or math.isclose(
        degradation, plateau_tolerance, rel_tol=_FLOAT_REL_TOL, abs_tol=_FLOAT_ABS_TOL
    )

    if center_metric > 0 and losing:
        is_plateau = False
        worst_value, worst_metric = min(losing, key=lambda pair: pair[1])
        reason = (
            f"neighbor {worst_value:g} scores {worst_metric:g} while the center "
            f"scores {center_metric:g} -- a parameter value one step away that "
            "loses money is not a plateau, whatever the neighborhood mean says"
        )
    elif not within_tolerance:
        is_plateau = False
        reason = (
            f"the neighborhood mean {mean:g} is {degradation:.1%} below the "
            f"center metric {center_metric:g}, past the {plateau_tolerance:.1%} "
            "tolerance -- the center is a knife's edge, which is what fitting "
            "the search to noise looks like"
        )
    else:
        is_plateau = True
        reason = (
            f"the neighborhood mean {mean:g} over {len(neighborhood)} neighbors "
            f"is within {plateau_tolerance:.1%} of the center metric "
            f"{center_metric:g} (degradation {degradation:.1%}) and no neighbor "
            "loses money"
        )

    return StabilityReport(
        parameter=parameter,
        center=center_key,
        neighborhood=neighborhood,
        metrics=metrics,
        center_metric=center_metric,
        neighborhood_mean=mean,
        neighborhood_std=std,
        relative_degradation=degradation,
        is_plateau=is_plateau,
        verdict_reason=reason,
    )
