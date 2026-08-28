#!/usr/bin/env python3
"""Evaluate the six incumbent sleeves with the edge-validation framework.

Usage:
    python scripts/run_sleeve_evaluation.py \\
        --backtest output/backtest_multi_20260901_120000.json \\
        --stability-dir output/stability \\
        --output-dir output/edge

Direction doc D10: the six live sleeves were selected post-hoc -- two more were
tried and dropped in May 2026 after negative expectancy -- so reporting the
survivors' backtest Sharpe is precisely the bias deflated Sharpe corrects for.
This driver is the bridge that lets the existing framework judge them.

**Why a bridge is needed.** ``research/evaluation`` scores *factors*: it ranks
names cross-sectionally by a factor score and measures the forward return of
the top quantile. A sleeve is not that. A sleeve is a strategy that already
made its own sizing and timing decisions and produced one equity curve. The
statistics downstream of that curve -- DSR, PSR, BH-FDR, the pre-registered
holdout, parameter stability -- are identical for both; only the front end
differs. So this driver converts curves to return series and feeds them to the
same machinery, rather than duplicating the machinery for sleeves.

**Why it lives in ``scripts/``.** ``research/`` may not import ``backtest`` or
``scripts`` (``tests/research/test_architecture.py``). The per-sleeve curves
live in a saved backtest artifact, so something outside ``research/`` has to
read them. The driver writes an explicit *mapping file* -- return series plus
declared conventions -- and the analysis consumes that, crossing the boundary
as a file exactly the way ``scripts/run_stability_sweep.py`` already does.

**The two conventions that must be normalised.** ``backtest`` and ``research``
disagree twice, both silently:

* ``backtest.metrics._max_drawdown`` returns a *positive* fraction;
  ``research.evaluation.metrics.max_drawdown`` returns a *negative* one. A
  forwarded number inverts every "worse than" comparison downstream.
* ``backtest`` annualises Sharpe by a fixed 252; ``research`` takes
  periods-per-year from the caller, and the factor evaluator passes
  ``252 / horizon`` (12 at the default 21-day horizon). Daily sleeve returns
  need 252, and using the factor evaluator's value would inflate every sleeve
  Sharpe by roughly 4.6x.

There is a third, subtler one. ``research.max_drawdown`` builds its equity
curve from the returns alone, so the curve starts at the *first* return rather
than at the initial capital -- a sleeve whose worst point is a day-1 fall
measures zero drawdown. :func:`to_research_equity_returns` pads a leading
``0.0`` to restore the day-0 peak, which is what makes the two paths agree
exactly. See ``tests/scripts/test_sleeve_evaluation.py``.

This driver runs no backtest. It reads one that was already saved, so it is
cheap to re-run -- with one exception: the pre-registered holdout is spent the
first time and cannot be spent again (that is the point of it). Point
``--holdout-registry`` at a copy to rehearse.
"""

# Direct script execution needs the worktree bootstrap below before local imports.
# ruff: noqa: E402

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

# When invoked as ``python scripts/run_sleeve_evaluation.py``, prefer this
# worktree over any editable-package path installed from another checkout.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backtest.bias_acceptance import (
    ACCEPTANCE_REGISTRY_PATH,
    ADMISSIBLE_WITH_ACCEPTED_BIAS,
    INADMISSIBLE,
    BiasAcceptance,
    load_acceptances,
    resolve_admissibility,
)
from backtest.divergence import execution_model_from_backtest_config
from research.evaluation.holdout import (
    DEFAULT_REGISTRY_PATH as HOLDOUT_REGISTRY_PATH,
)
from research.evaluation.holdout import (
    HoldoutProtocol,
    HoldoutRegistration,
    HoldoutSplit,
)
from research.evaluation.metrics import (
    max_drawdown as research_max_drawdown,
)
from research.evaluation.metrics import (
    probabilistic_sharpe,
    sharpe,
    sharpe_stats,
)
from research.evaluation.multiple_testing import control
from research.evaluation.trial_registry import load_trial_registry
from shared.universe import ACTIVE_SLEEVES

#: Sleeve returns are daily, so the annualisation is the trading-day count --
#: not the factor evaluator's ``252 / horizon``.
ANNUALIZATION_DAYS = 252.0

#: The registry entry whose count this evaluation deflates against. The
#: registry's *total* also carries the 2026-08-02 factor-catalog search, which
#: the sleeves could not have drawn from; counting it would be over-deflation
#: (edge-validation-framework.md, "the sum is unscoped"). The sleeve figure is
#: 8: eight candidates searched, six retained on 2026-05-26.
SLEEVE_SELECTION_SOURCE = "docs/strategies/mean-reversion-failure-analysis.md"

#: The promotion ladder, best stage first. Retirement is not on it: deleting a
#: sleeve requires a logged operator decision (direction doc D3.3), so the
#: worst thing this evaluation can recommend is ``shadow``.
DEMOTION_LADDER = ("live", "paper", "shadow")

#: Where the six sleeves sit today: grandfathered into the pipeline at the
#: paper stage via epoch v2 (direction doc D9).
CURRENT_STAGE = "paper"

DISCLAIMER = (
    "divergence-fidelity is not edge, and edge is not divergence-fidelity. "
    "The divergence monitor establishes that live execution reproduces the "
    "baseline backtest; this evaluation establishes whether the baseline had "
    "an edge to reproduce. Neither substitutes for the other, and a sleeve "
    "needs both."
)


@dataclass(frozen=True)
class SleeveParameter:
    """The one load-bearing parameter this evaluation demands a surface for."""

    parameter: str
    shipped_value: float
    why: str


#: One load-bearing parameter per sleeve, named here so the memo cannot pick a
#: convenient one after seeing the sweep. Values mirror the shipped call sites
#: in ``scripts/run_backtest.py`` (:2310-2369).
SLEEVE_PARAMETERS: dict[str, SleeveParameter] = {
    "momentum": SleeveParameter(
        "lookback_days", 126,
        "the ranking window is the signal; every holding decision is a "
        "function of the 126-day return and nothing else",
    ),
    "sector_rotation": SleeveParameter(
        "lookback_days", 63,
        "same ranking window, one quarter long -- it decides which three "
        "sector ETFs are held",
    ),
    "thematic_momentum": SleeveParameter(
        "lookback_days", 63,
        "ranks the thematic ETF basket; the replacement policy only reshuffles "
        "what this window already scored",
    ),
    "quality_value": SleeveParameter(
        "top_n", 15,
        "the sleeve is a fundamental ranking with no lookback -- breadth is "
        "the knob that decides how far down the ranking capital goes",
    ),
    "earnings_drift": SleeveParameter(
        "surprise_threshold_pct", 5.0,
        "the surprise cutoff *is* the entry signal; every other parameter "
        "shapes an already-taken position",
    ),
    "tail_risk_hedge": SleeveParameter(
        "position_size_pct", 0.25,
        "entries are driven by the external regime series, so hedge size is "
        "the only parameter the sleeve itself owns",
    ),
}

#: The evaluation refuses to grade against a baseline live execution could not
#: have matched. Exit code mirrors ``scripts/divergence_monitor.py``'s BLIND.
EXIT_NOT_LIKE_FOR_LIKE = 3


# ---------------------------------------------------------------------------
# Convention normalisation
# ---------------------------------------------------------------------------


def returns_from_equity_curve(
    portfolio_values: Sequence[float], dates: Sequence[Any]
) -> list[float]:
    """Daily simple returns aligned to ``dates``.

    ``backtest.runner`` seeds ``portfolio_values`` with the initial capital and
    appends one value per session, so the curve is one longer than the date
    index and ``portfolio_values[i + 1]`` is the close of ``dates[i]``. Zipping
    the two directly would shift every return back one session -- a one-day
    look-ahead through the entire evaluation -- so the length is checked rather
    than accommodated.
    """
    expected = len(dates) + 1
    if len(portfolio_values) != expected:
        raise ValueError(
            f"portfolio_values has {len(portfolio_values)} points for "
            f"{len(dates)} dates; expected {expected} (the first point is the "
            "pre-session-0 initial capital). A mismatched curve cannot be "
            "aligned to its dates without guessing which end to drop."
        )
    returns: list[float] = []
    for i in range(len(dates)):
        previous = float(portfolio_values[i])
        if previous == 0.0:
            raise ValueError(
                f"portfolio value is zero at index {i}; a return off a zero "
                "base is undefined and the sleeve is bust, not flat"
            )
        returns.append(float(portfolio_values[i + 1]) / previous - 1.0)
    return returns


def to_research_max_drawdown(backtest_max_drawdown: float) -> float:
    """``backtest``'s positive fraction as ``research``'s negative one."""
    return -abs(float(backtest_max_drawdown))


def to_backtest_max_drawdown(research_max_drawdown_value: float) -> float:
    """``research``'s negative fraction as ``backtest``'s positive one."""
    return abs(float(research_max_drawdown_value))


def to_research_equity_returns(returns: Sequence[float]) -> list[float]:
    """Pad a leading ``0.0`` so the research equity curve starts at day 0.

    ``research.evaluation.metrics.max_drawdown`` does ``(1 + r).cumprod()``,
    which puts the first *return* at the start of the curve -- the initial
    capital is not a point on it, so it can never be the peak. A sleeve that
    fell on day one and never came back would report no drawdown at all. The
    pad reinstates the day-0 peak and makes the drawdown identical to
    ``backtest.metrics``'s, which measures from the initial capital.
    """
    return [0.0, *(float(r) for r in returns)]


def normalized_metrics(
    portfolio_values: Sequence[float], dates: Sequence[Any]
) -> dict:
    """Research-convention metrics for one sleeve's equity curve."""
    returns = returns_from_equity_curve(portfolio_values, dates)
    series = pd.Series(returns, dtype=float)
    return {
        "sharpe": sharpe(series, ANNUALIZATION_DAYS),
        "max_drawdown": research_max_drawdown(
            pd.Series(to_research_equity_returns(returns), dtype=float)
        ),
        # ``returns_from_equity_curve`` above already refused an empty curve
        # and a zero opening value, so this division is safe.
        "total_return": float(portfolio_values[-1]) / float(portfolio_values[0]) - 1.0,
        "n_observations": len(returns),
    }


# ---------------------------------------------------------------------------
# The declared trial count
# ---------------------------------------------------------------------------


def sleeve_trial_count(registry_path: Path | str | None = None) -> int:
    """The size of the search that produced the six sleeves: 8.

    Read from ``research/trial_registry.json`` rather than hardcoded, so the
    number stays auditable in git -- but scoped to the sleeve-selection entry.
    The registry total also counts the factor-catalog search, and a sleeve
    chosen in May 2026 was not selected against factors specified in August.
    """
    registry = load_trial_registry(registry_path)
    entries = [e for e in registry.entries if e.source == SLEEVE_SELECTION_SOURCE]
    if not entries:
        raise ValueError(
            "no sleeve-selection entry in the trial registry (expected one "
            f"sourced from {SLEEVE_SELECTION_SOURCE!r}); the deflation has "
            "nothing to deflate against, and defaulting to the number of "
            "sleeves in the run would understate the search that dropped two"
        )
    return sum(entry.n_trials for entry in entries)


# ---------------------------------------------------------------------------
# The mapping file -- what crosses the architectural boundary
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git_revision() -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], text=True
        ).strip()
    except Exception:
        return "unknown"


def _checksum(path: str) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return "unknown"


def build_mapping(
    payload: dict,
    source_path: str,
    sleeves: Sequence[str] = ACTIVE_SLEEVES,
    acceptances: Sequence[BiasAcceptance] | None = None,
) -> dict:
    """Convert a saved multi-portfolio backtest into a sleeve-returns mapping.

    Every number in the result is in the *research* convention, and the
    conventions are declared in the file rather than assumed by the reader --
    a consumer that finds a positive max drawdown here knows it is holding a
    stale artifact.
    """
    portfolios = payload.get("portfolios") or {}
    missing = [name for name in sleeves if name not in portfolios]
    if missing:
        raise ValueError(
            f"the backtest artifact has no portfolio for {', '.join(missing)}; "
            "an evaluation of the six incumbents cannot skip one of them "
            "silently -- re-run the baseline with every active sleeve"
        )

    model = execution_model_from_backtest_config(payload.get("config"))
    source_sha256 = _checksum(source_path)
    # Admissibility is resolved once, here, so the artifact records the answer
    # rather than each reader re-deriving it from a registry that may since
    # have changed.
    admissibility = resolve_admissibility(
        model,
        source_sha256=source_sha256,
        coverage=(payload.get("config") or {}).get("coverage"),
        acceptances=load_acceptances() if acceptances is None else acceptances,
    )
    mapping_sleeves: dict[str, dict] = {}
    reference: tuple[str, list] | None = None
    for name in sleeves:
        portfolio = portfolios[name]
        dates = list(portfolio["dates"])
        values = list(portfolio["portfolio_values"])
        # The holdout is resolved once, against one date index, and the
        # resulting *indices* slice every sleeve. Sleeves on different
        # calendars would each get a different window reported under one
        # length -- a 16-session Sharpe presented as a 76-session one.
        if reference is None:
            reference = (name, dates)
        elif dates != reference[1]:
            raise ValueError(
                f"sleeve {name!r} has a different date index from "
                f"{reference[0]!r} ({len(dates)} vs {len(reference[1])} "
                "sessions, or differing dates). One holdout split is resolved "
                "against one calendar and applied to all six, so a shared "
                "index is not optional."
            )
        mapping_sleeves[name] = {
            "dates": dates,
            "returns": returns_from_equity_curve(values, dates),
            "initial_capital": float(values[0]),
            "final_capital": float(values[-1]),
            # Kept verbatim so a reader can see both conventions side by side
            # and check the normalisation rather than trust it.
            "backtest_metrics": dict(portfolio.get("metrics") or {}),
            "normalized_metrics": normalized_metrics(values, dates),
        }

    return {
        "version": 1,
        "generated_at": _now(),
        "git_revision": _git_revision(),
        "source": source_path,
        "source_sha256": source_sha256,
        "conventions": {
            "returns": (
                "daily simple returns; returns[i] is the return of dates[i], "
                "derived as portfolio_values[i+1]/portfolio_values[i] - 1"
            ),
            "periods_per_year": ANNUALIZATION_DAYS,
            "max_drawdown_sign": "negative",
            "max_drawdown_note": (
                "measured from the initial capital: the returns are padded "
                "with a leading 0.0 before (1+r).cumprod() so day 0 can be "
                "the peak, matching backtest.metrics"
            ),
        },
        "baseline": {
            "fill_model": model.fill_model,
            "slippage_bps": model.slippage_bps,
            "commission_per_share": model.commission_per_share,
            "commission_minimum": model.commission_minimum,
            "point_in_time_universe": model.point_in_time_universe,
            "coverage_state": model.coverage_state,
            # Kept verbatim and still strict: D18 accepted the coverage bias
            # in the record, not at the gate, so a reader who opens this file
            # still sees False and can go find out why it was spent anyway.
            "is_like_for_like": model.is_like_for_like,
            "unmet_requirements": model.unmet_requirements(),
            "admissibility": admissibility.state,
            "accepted_bias": admissibility.accepted_bias,
            "admissibility_notes": admissibility.notes,
        },
        "sleeves": mapping_sleeves,
    }


# ---------------------------------------------------------------------------
# The evaluation
# ---------------------------------------------------------------------------


def demote_one_stage(stage: str) -> str:
    """One stage down the ladder, and never past ``shadow``."""
    if stage not in DEMOTION_LADDER:
        raise ValueError(
            f"unknown stage {stage!r}; the ladder is {list(DEMOTION_LADDER)}"
        )
    index = DEMOTION_LADDER.index(stage)
    return DEMOTION_LADDER[min(index + 1, len(DEMOTION_LADDER) - 1)]


def _load_stability(
    stability_dir: Path | None, sleeve: str, spec: SleeveParameter
) -> dict:
    """Attach the sweep artifact for this sleeve's named parameter, if it ran.

    Absence is reported, not defaulted: a sleeve with no surface has an
    unmeasured parameter, which is a different verdict from a flat one.
    """
    unavailable = {
        "available": False,
        "parameter": spec.parameter,
        "center": spec.shipped_value,
        "reason": (
            f"no sweep artifact for {sleeve}.{spec.parameter}; run "
            f"scripts/run_stability_sweep.py --sleeve {sleeve} "
            f"--parameter {spec.parameter} --center {spec.shipped_value:g}"
        ),
    }
    if stability_dir is None:
        return unavailable

    # Matched on the artifact's own ``sleeve``/``parameter`` fields rather than
    # on its filename: ``run_stability_sweep.py`` takes a free-form ``--out``,
    # so a correct sweep saved under an unexpected name would silently report
    # as unmeasured, and a mis-named one would be attributed to the wrong
    # sleeve. The file name is a convenience; the body is the claim.
    matches = []
    for path in sorted(Path(stability_dir).glob("*.json")):
        try:
            artifact = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path} is not readable as JSON: {exc}") from None
        if str(artifact.get("sleeve", "")) == sleeve:
            matches.append((path, artifact))
    if not matches:
        return unavailable
    if len(matches) > 1:
        raise ValueError(
            f"{len(matches)} stability artifacts in {stability_dir} claim "
            f"sleeve {sleeve!r} ({', '.join(str(p) for p, _ in matches)}); "
            "which surface the verdict rests on cannot be guessed"
        )
    path, artifact = matches[0]

    swept = str(artifact.get("parameter", ""))
    if swept != spec.parameter:
        raise ValueError(
            f"{path} sweeps {sleeve}.{swept!r}, but the named load-bearing "
            f"parameter for {sleeve} is {spec.parameter!r}. A surface for a "
            "parameter the memo does not name is not the memo's evidence -- "
            "re-run the sweep or update SLEEVE_PARAMETERS deliberately."
        )
    center = float(artifact.get("center"))
    if not math.isclose(center, float(spec.shipped_value), rel_tol=1e-9, abs_tol=1e-12):
        # ``is_plateau`` is a claim about the neighbourhood of the *center*, so
        # a surface centered anywhere else answers a question nobody asked --
        # and it is the easiest of the sweep's five arguments to forget to
        # change when copying the command for the next sleeve.
        raise ValueError(
            f"{path} is centered on {swept}={center:g}, but {sleeve} ships "
            f"{spec.shipped_value:g}. A stability verdict is a claim about the "
            "shipped value; re-run the sweep with --center "
            f"{spec.shipped_value:g}."
        )
    report = dict(artifact.get("stability") or {})
    point_in_time = bool(artifact.get("point_in_time_universe", False))
    return {
        "available": True,
        "parameter": spec.parameter,
        "center": center,
        "source": str(path),
        "point_in_time_universe": point_in_time,
        # A surface traced over present-day survivors is inflated at every
        # point, exactly like a survivorship-biased baseline. The driver
        # refuses one of those; it reports this one and taints the run rather
        # than discarding a sweep that took hours.
        "admissible": point_in_time,
        "is_plateau": bool(report.get("is_plateau", False)),
        "relative_degradation": report.get("relative_degradation"),
        "verdict_reason": report.get("verdict_reason", ""),
    }


def _validate_registration(
    protocol: HoldoutProtocol, split_id: str
) -> HoldoutRegistration:
    """Check the pre-registration before anything is spent.

    ``registered_at`` is caller-supplied and therefore backdatable, so this
    proves nothing about honesty. It catches the one direction code can catch:
    a registration that postdates the run, which means the registry was
    written during it.
    """
    registration = protocol.registration(split_id)
    try:
        registered_at = datetime.fromisoformat(registration.registered_at)
    except ValueError:
        raise ValueError(
            f"holdout {split_id!r} has an unparseable registered_at "
            f"{registration.registered_at!r}; the pre-registration cannot be "
            "dated, so it is not one"
        ) from None
    if registered_at.tzinfo is None:
        registered_at = registered_at.replace(tzinfo=timezone.utc)
    # Compared as instants, not strings: "2026-08-17T09:00+13:00" sorts above
    # "2026-08-17T02:00+00:00" and is six hours *earlier*, so a lexical compare
    # both refuses honest registrations and accepts future-dated ones.
    if registered_at > datetime.now(timezone.utc):
        raise ValueError(
            f"holdout {split_id!r} is registered at {registration.registered_at}, "
            "which is in the future. A pre-registration that postdates the run "
            "is not one; the registry was written during the look."
        )
    return registration


def _spend_holdout(
    mapping: dict,
    protocol: HoldoutProtocol,
    registration: HoldoutRegistration,
    label: str,
) -> tuple[HoldoutSplit, dict]:
    """Resolve and spend the pre-registered split against the sleeve dates.

    All six sleeves share the baseline's date index (``build_mapping`` refuses
    an artifact where they do not), so the split is resolved once and spent
    once -- six burns for one look would be theatre.

    Nothing that can raise may run after this call: the burn is recorded to
    disk immediately and cannot be undone, so a later failure would destroy
    the split and produce no result at all.
    """
    split_id = registration.split_id
    dates = [
        date.fromisoformat(d)
        for d in next(iter(mapping["sleeves"].values()))["dates"]
    ]
    split = protocol.evaluate(split_id, dates, label=label)
    start, end = split.holdout
    return split, {
        "split_id": split_id,
        "holdout_start": registration.holdout_start,
        "registered_at": registration.registered_at,
        "horizon": registration.horizon,
        "embargo": registration.embargo,
        # Inherited from the registration and applied by ``resolve()`` to
        # separate train from holdout. This driver fits nothing, so the purge
        # protects the in-sample figure reported beside the holdout, not the
        # holdout itself.
        "gap": split.gap,
        "n_sessions": end - start,
        "first_session": dates[start].isoformat(),
        "last_session": dates[end - 1].isoformat(),
        "note": registration.note,
    }


def evaluate_mapping(
    mapping: dict,
    *,
    n_trials: int,
    holdout_registry_path: Path | str | None = None,
    holdout_split_id: str = "incumbent_sleeves_2026",
    holdout_label: str = "KAN-40 incumbent sleeve evaluation",
    stability_dir: Path | str | None = None,
    fdr_q: float = 0.10,
    dsr_threshold: float = 0.95,
) -> dict:
    """Run DSR / PSR / BH-FDR / holdout / stability over every sleeve.

    ``p`` for the FDR step is ``1 - PSR(sr, n, skew, kurt, 0)``: the
    probability the sleeve's true Sharpe is *not* above zero, computed with
    the same skew and kurtosis adjustment as the deflation. Reusing the
    Gaussian IC t-test the factor path uses would be wrong here -- a sleeve has
    no cross-sectional information coefficient, and strategy returns are the
    case Bailey & Lopez de Prado's moment correction exists for.

    **Ordering matters.** The holdout is single-use and its burn is written to
    disk the moment it is spent. Everything that can fail -- loading and
    validating all six stability surfaces, checking the registration -- runs
    *before* that point, so a mis-named sweep artifact or a mistyped split id
    costs a re-run rather than destroying the split with no output to show for
    it.
    """
    sleeves = list(mapping["sleeves"])
    per_sleeve: dict[str, dict] = {}
    for name in sleeves:
        returns = pd.Series(mapping["sleeves"][name]["returns"], dtype=float)
        stats = sharpe_stats(returns)
        psr = probabilistic_sharpe(
            stats["sr"], stats["n"], stats["skew"], stats["kurt"], 0.0
        )
        per_sleeve[name] = {
            "sr": stats["sr"],
            "n": stats["n"],
            "skew": stats["skew"],
            "kurt": stats["kurt"],
            "psr": psr,
            # ``control`` expects the key ``ic_p``; for a strategy this is the
            # one-sided p-value that the mean return is not positive.
            "ic_p": 1.0 - psr,
        }

    verdicts = control(
        {name: dict(row) for name, row in per_sleeve.items()},
        q=fdr_q,
        dsr_threshold=dsr_threshold,
        n_trials=n_trials,
    )

    # --- everything that can raise, before the burn ------------------------
    stability_by_sleeve = {
        name: _load_stability(
            None if stability_dir is None else Path(stability_dir),
            name,
            SLEEVE_PARAMETERS[name],
        )
        for name in sleeves
    }
    protocol = HoldoutProtocol.load(holdout_registry_path)
    registration = _validate_registration(protocol, holdout_split_id)

    # --- the burn ----------------------------------------------------------
    split, holdout_summary = _spend_holdout(
        mapping, protocol, registration, holdout_label
    )
    start, end = split.holdout
    train_end = split.train[1]

    results: dict[str, dict] = {}
    for name in sleeves:
        row = mapping["sleeves"][name]
        stats = per_sleeve[name]
        verdict = verdicts[name]
        spec = SLEEVE_PARAMETERS[name]
        stability = stability_by_sleeve[name]

        holdout_returns = pd.Series(row["returns"][start:end], dtype=float)
        passes = bool(verdict.survives)
        results[name] = {
            "sharpe": row["normalized_metrics"]["sharpe"],
            "max_drawdown": row["normalized_metrics"]["max_drawdown"],
            "total_return": row["normalized_metrics"]["total_return"],
            "n_observations": stats["n"],
            "sr_per_period": stats["sr"],
            "skew": stats["skew"],
            "kurtosis": stats["kurt"],
            "probabilistic_sharpe": stats["psr"],
            "p_value": stats["ic_p"],
            "deflated_sharpe": verdict.deflated_sharpe,
            "passes_dsr": verdict.passes_dsr,
            "passes_fdr": verdict.passes_fdr,
            "survives_multiple_testing": passes,
            # The full-sample Sharpe above *contains* the holdout window, so
            # reading it as the in-sample counterpart to the figure below
            # overstates the contrast. This is the honest comparator: the
            # training span, purged by the registered gap.
            "in_sample": {
                "n_sessions": train_end,
                "sharpe": sharpe(
                    pd.Series(row["returns"][:train_end], dtype=float),
                    ANNUALIZATION_DAYS,
                ),
            },
            "holdout": {
                "n_sessions": end - start,
                "sharpe": sharpe(holdout_returns, ANNUALIZATION_DAYS),
                "max_drawdown": research_max_drawdown(
                    pd.Series(
                        to_research_equity_returns(row["returns"][start:end]),
                        dtype=float,
                    )
                ),
                "total_return": float((1.0 + holdout_returns).prod() - 1.0)
                if len(holdout_returns)
                else 0.0,
            },
            "stability": stability,
            "parameter": {
                "name": spec.parameter,
                "shipped_value": spec.shipped_value,
                "why": spec.why,
            },
            "verdict": "PASS" if passes else "FAIL",
            "current_stage": CURRENT_STAGE,
            "recommended_stage": (
                CURRENT_STAGE if passes else demote_one_stage(CURRENT_STAGE)
            ),
            "recommendation": (
                f"holds its stage at {CURRENT_STAGE}"
                if passes
                else (
                    f"recorded as failing the edge framework; the recommended "
                    f"stage is {demote_one_stage(CURRENT_STAGE)}, one step down "
                    "the ladder. Nothing is deleted and nothing moves "
                    "automatically -- the operator decides, per direction doc "
                    "D3.3."
                )
            ),
        }

    # A surface traced over present-day survivors is inflated at every point,
    # so a run resting on one is no more admissible than a run against a
    # survivorship-biased baseline.
    biased_surfaces = [
        name
        for name, s in stability_by_sleeve.items()
        if s["available"] and not s["admissible"]
    ]
    return {
        "version": 1,
        "generated_at": _now(),
        "git_revision": mapping.get("git_revision", "unknown"),
        "source": mapping.get("source"),
        "n_trials": n_trials,
        "n_trials_source": SLEEVE_SELECTION_SOURCE,
        "fdr_q": fdr_q,
        "dsr_threshold": dsr_threshold,
        "baseline": dict(mapping["baseline"]),
        "inadmissible_stability_surfaces": biased_surfaces,
        # Tri-state, not a bool: VALID_WITH_ACCEPTED_BIAS has to survive into
        # the artifact, because a reader who finds ``true`` learns nothing
        # about what was excused to get there.
        "gate_valid": (
            INADMISSIBLE if biased_surfaces else mapping["baseline"]["admissibility"]
        ),
        "holdout": holdout_summary,
        "sleeves": results,
        "disclaimer": DISCLAIMER,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_summary(evaluation: dict) -> None:
    holdout = evaluation["holdout"]
    print(
        f"\nDeflated against {evaluation['n_trials']} trials "
        f"({evaluation['n_trials_source']}); holdout "
        f"{holdout['split_id']} from {holdout['holdout_start']} "
        f"= {holdout['n_sessions']} sessions"
    )
    if holdout["n_sessions"] < 60:
        print(
            f"  NOTE: {holdout['n_sessions']} sessions is a short holdout. "
            "Report the length beside the result; a window too small to be "
            "significant is evidence of nothing."
        )
    header = (
        f"{'sleeve':<20}{'sharpe':>8}{'maxDD':>9}{'PSR':>7}{'DSR':>7}"
        f"{'FDR':>6}{'inSR':>7}{'holdSR':>8}{'stable':>9}  verdict"
    )
    print(f"\n{header}")
    print("-" * len(header))
    for name, row in evaluation["sleeves"].items():
        stability = row["stability"]
        stable = (
            ("plateau" if stability["is_plateau"] else "knife")
            if stability["available"]
            else "—"
        )
        print(
            f"{name:<20}{row['sharpe']:>8.2f}{row['max_drawdown']:>9.2%}"
            f"{row['probabilistic_sharpe']:>7.2f}{row['deflated_sharpe']:>7.2f}"
            f"{('yes' if row['passes_fdr'] else 'no'):>6}"
            f"{row['in_sample']['sharpe']:>7.2f}"
            f"{row['holdout']['sharpe']:>8.2f}{stable:>9}  {row['verdict']}"
        )
    print(
        "  (sharpe is the FULL sample and contains the holdout; inSR is the "
        "purged training span, the honest comparator for holdSR)"
    )
    failing = [n for n, r in evaluation["sleeves"].items() if r["verdict"] == "FAIL"]
    if failing:
        print(
            f"\n{len(failing)} sleeve(s) failed: {', '.join(failing)}. "
            f"Recommended stage {demote_one_stage(CURRENT_STAGE)} for each; "
            "nothing is deleted and nothing moves automatically."
        )
    accepted = evaluation["baseline"].get("accepted_bias")
    if evaluation["gate_valid"] == ADMISSIBLE_WITH_ACCEPTED_BIAS and accepted:
        print(
            f"\nACCEPTED BIAS ({accepted['decision']}): this run is admissible "
            f"only because {accepted['decision']} accepted its coverage bias in "
            f"writing. {accepted['excluded_pct']:.2f}% of point-in-time "
            f"membership-days could not be priced against a "
            f"{accepted['floor_pct']:.2f}% floor, leaving the baseline "
            f"{accepted['direction']}. Every verdict citing this run must cite "
            f"that limitation -- see {accepted['doc']}."
        )
    # Compared against the state, never for truthiness: all three states are
    # non-empty strings, so ``if not evaluation["gate_valid"]`` would be
    # permanently false and this banner would silently stop printing.
    if evaluation["gate_valid"] == INADMISSIBLE:
        reasons = list(evaluation["baseline"]["unmet_requirements"])
        for name in evaluation["inadmissible_stability_surfaces"]:
            reasons.append(
                f"{name}'s stability surface was swept on a static present-day "
                "universe (survivorship-biased)"
            )
        print(
            "\nGATE-INVALID: this run is not admissible evidence for Rung 0. "
            + "; ".join(reasons)
        )
    print(f"\n{DISCLAIMER}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Edge-framework evaluation of the six incumbent sleeves",
    )
    parser.add_argument(
        "--backtest", required=True,
        help="Saved multi-portfolio backtest JSON (output/backtest_multi_*.json)",
    )
    parser.add_argument(
        "--output-dir", default="output/edge",
        help="Where the mapping and evaluation artifacts are written",
    )
    parser.add_argument(
        "--stability-dir", default=None,
        help="Directory of run_stability_sweep.py artifacts; each is matched "
             "to a sleeve by its own 'sleeve' field, not by filename",
    )
    parser.add_argument(
        "--holdout-registry", default=None,
        help="Holdout registry path. Defaults to research/holdout_registry.json "
             "-- the split is SPENT on the first run; point this at a copy to "
             "rehearse.",
    )
    parser.add_argument("--holdout-split-id", default="incumbent_sleeves_2026")
    parser.add_argument(
        "--n-trials", type=int, default=None,
        help="Override the declared search size upward. Defaults to the "
             "sleeve-selection count in research/trial_registry.json (8); a "
             "smaller count is refused.",
    )
    parser.add_argument("--fdr-q", type=float, default=0.10)
    parser.add_argument("--dsr-threshold", type=float, default=0.95)
    parser.add_argument(
        "--bias-acceptances", default=None,
        help="Accepted-bias registry path. Defaults to "
             f"{ACCEPTANCE_REGISTRY_PATH.name} -- an entry there admits ONE "
             "artifact, pinned by sha256, and only for the coverage floor. "
             "Point this elsewhere to rehearse against a copy.",
    )
    parser.add_argument(
        "--allow-non-comparable-baseline", action="store_true",
        help="Evaluate anyway against a baseline live execution could not "
             "match, stamping the output gate_valid=false. Requires an "
             "explicit --holdout-registry so the split of record is not spent "
             "on a run that cannot count.",
    )
    args = parser.parse_args(argv)

    declared = sleeve_trial_count()
    n_trials = args.n_trials if args.n_trials is not None else declared
    if n_trials < declared:
        parser.error(
            f"--n-trials {n_trials} is below the declared sleeve-selection "
            f"count of {declared}. The override exists to model a *larger* "
            "search than the registry records; shrinking it shrinks SR* and "
            "makes every sleeve easier to pass, which is the failure the "
            "declared count exists to prevent."
        )

    payload = json.loads(Path(args.backtest).read_text())
    mapping = build_mapping(
        payload,
        args.backtest,
        acceptances=load_acceptances(
            args.bias_acceptances or ACCEPTANCE_REGISTRY_PATH
        ),
    )

    if mapping["baseline"]["admissibility"] == INADMISSIBLE:
        reasons = "; ".join(mapping["baseline"]["unmet_requirements"])
        print(
            f"REFUSING: {args.backtest} is not admissible evidence — {reasons}.\n"
            "An edge evaluation of a survivorship-biased or same-bar baseline "
            "measures the bias, not the edge, and it would spend the "
            "single-use holdout doing it. Regenerate the baseline (see "
            "docs/operations/backtest-baseline.md), or pass "
            "--allow-non-comparable-baseline to produce a gate-invalid run."
        )
        for note in mapping["baseline"]["admissibility_notes"]:
            print(f"  note: {note}")
        if not args.allow_non_comparable_baseline:
            return EXIT_NOT_LIKE_FOR_LIKE
        if args.holdout_registry is None:
            # The refusal above says the harm out loud -- "it would spend the
            # single-use holdout doing it" -- so the override must not then go
            # and spend the split of record by default.
            print(
                "REFUSING the override too: --allow-non-comparable-baseline "
                "without --holdout-registry would spend "
                f"{HOLDOUT_REGISTRY_PATH} on a run that cannot count. Point it "
                "at a copy."
            )
            return EXIT_NOT_LIKE_FOR_LIKE

    # The output directory is created before the holdout is spent: an
    # unwritable path must not cost the split.
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    evaluation = evaluate_mapping(
        mapping,
        n_trials=n_trials,
        holdout_registry_path=args.holdout_registry,
        holdout_split_id=args.holdout_split_id,
        stability_dir=args.stability_dir,
        fdr_q=args.fdr_q,
        dsr_threshold=args.dsr_threshold,
    )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    mapping_path = out_dir / f"sleeve_returns_{stamp}.json"
    mapping_path.write_text(json.dumps(mapping, indent=2))
    evaluation["mapping"] = str(mapping_path)
    evaluation_path = out_dir / f"sleeve_evaluation_{stamp}.json"
    evaluation_path.write_text(json.dumps(evaluation, indent=2))

    _print_summary(evaluation)
    print(f"\nWrote {mapping_path}")
    print(f"Wrote {evaluation_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
