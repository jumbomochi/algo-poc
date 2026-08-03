# research/evaluation/evaluator.py
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from research.evaluation.folds import nested_walk_forward
from research.evaluation.forward_returns import forward_excess_returns
from research.evaluation.metrics import (
    annualized_turnover,
    ic_summary,
    max_drawdown,
    sharpe,
    sharpe_stats,
)
from research.evaluation.multiple_testing import control
from research.evaluation.overlap import attribute, baseline_selections_from_records
from research.evaluation.portfolio import quantile_long_only, top_quantile_names
from research.factors.catalog import DEFAULT_FACTOR_IDS, build_default_registry
from research.factors.engine import FactorEngine
from research.factors.panel import build_factor_panel


@dataclass(frozen=True)
class EvaluationConfig:
    horizon: int = 21
    n_outer: int = 4
    n_inner: int = 3
    embargo: int = 21
    quantiles: tuple[float, ...] = (0.2, 0.3)
    fdr_q: float = 0.10
    min_names: int = 5
    seed: int = 7


def _slice(frame: pd.DataFrame, bounds: tuple[int, int]) -> pd.DataFrame:
    start, end = bounds
    return frame.iloc[start:end]


def _select_quantile(scores, forward, inner, config) -> float:
    best_q, best_ic = config.quantiles[0], float("-inf")
    for quantile in config.quantiles:
        ics: list[float] = []
        for fold in inner:
            v_scores = _slice(scores, fold.validate)
            v_forward = _slice(forward, fold.validate)
            series = quantile_long_only(v_scores, v_forward, quantile, config.horizon, config.min_names)
            if len(series.ic):
                ics.append(float(series.ic.mean()))
        mean_ic = sum(ics) / len(ics) if ics else float("-inf")
        if mean_ic > best_ic:
            best_ic, best_q = mean_ic, quantile
    return best_q


def evaluate_factors(
    bars_by_ticker: dict,
    factor_ids: tuple[str, ...] | None = None,
    baseline_records: list[dict] | None = None,
    config: EvaluationConfig | None = None,
) -> dict:
    config = config or EvaluationConfig()
    factor_ids = tuple(factor_ids) if factor_ids is not None else DEFAULT_FACTOR_IDS
    registry = build_default_registry()
    panel = build_factor_panel(bars_by_ticker)
    # Ranking uses RAW factor scores. This is equivalent to the engine's normalized
    #  frames ONLY while the catalog is price-only with normalization_policy='none'
    #  (cross-sectional rank is invariant to monotone normalization). A future
    #  cross_sectional_zscore factor masks to universe members, so before Phase 4
    #  introduces such factors the evaluator must rank on the engine's normalized
    #  frames, not raw compute().
    engine_snapshot = FactorEngine(registry).compute(panel, factor_ids)
    forward = forward_excess_returns(panel, config.horizon)
    n_dates = len(panel.field("close").index)
    folds = nested_walk_forward(n_dates, config.n_outer, config.n_inner, config.horizon, config.embargo)
    periods_per_year = 252.0 / config.horizon

    baseline = baseline_selections_from_records(baseline_records or [])
    per_factor_stats: dict[str, dict] = {}
    per_factor_series: dict[str, dict] = {}

    for factor_id in factor_ids:
        # Raw scores used for ranking — see the normalization-assumption note
        # above the FactorEngine.compute() call.
        scores = registry.get(factor_id).compute(panel).astype(float)
        oos_returns: list[pd.Series] = []
        oos_ic: list[pd.Series] = []
        oos_turnover: list[pd.Series] = []
        factor_selection: dict = {}
        quantile = config.quantiles[0]
        for outer in folds:
            # Nested selection: each outer fold picks its own quantile from its own
            # inner folds (never from another outer fold's data), then scores its
            # test span exactly once with that cutoff. `quantile` is left set to the
            # last (most recent / largest-training-window) outer fold's pick and
            # reported below as the factor-level "chosen_quantile" summary.
            quantile = _select_quantile(_slice(scores, outer.train), _slice(forward, outer.train), outer.inner, config)
            test_scores = _slice(scores, outer.test)
            test_forward = _slice(forward, outer.test)
            series = quantile_long_only(test_scores, test_forward, quantile, config.horizon, config.min_names)
            oos_returns.append(series.returns)
            oos_ic.append(series.ic)
            oos_turnover.append(series.turnover)
            for i in range(0, len(test_scores.index), config.horizon):
                day = test_scores.index[i]
                names = top_quantile_names(
                    test_scores.loc[day], test_forward.loc[day], quantile, config.min_names
                )
                if names:
                    factor_selection[day.date()] = set(names)
        returns = pd.concat(oos_returns) if oos_returns else pd.Series(dtype=float)
        ic = pd.concat(oos_ic) if oos_ic else pd.Series(dtype=float)
        turnover = pd.concat(oos_turnover) if oos_turnover else pd.Series(dtype=float)
        stats = sharpe_stats(returns)
        ic_stat = ic_summary(ic)
        per_factor_stats[factor_id] = {
            "sr": stats["sr"], "n": stats["n"], "skew": stats["skew"],
            "kurt": stats["kurt"], "ic_p": ic_stat["p_value"],
        }
        per_factor_series[factor_id] = {
            "returns": returns, "ic": ic, "turnover": turnover,
            "ic_stat": ic_stat, "selection": factor_selection, "chosen_quantile": quantile,
        }

    verdicts = control(per_factor_stats, q=config.fdr_q)

    factors: dict[str, dict] = {}
    for factor_id in factor_ids:
        s = per_factor_series[factor_id]
        verdict = verdicts[factor_id]
        overlap = attribute(s["selection"], baseline, forward)
        factors[factor_id] = {
            "chosen_quantile": s["chosen_quantile"],
            "sharpe": sharpe(s["returns"], periods_per_year),
            "deflated_sharpe": verdict.deflated_sharpe,
            "passes_dsr": verdict.passes_dsr,
            "passes_fdr": verdict.passes_fdr,
            "survives_multiple_testing": verdict.survives,
            "max_drawdown": max_drawdown(s["returns"]),
            "annual_turnover": annualized_turnover(s["turnover"], periods_per_year),
            "ic_mean": s["ic_stat"]["mean"],
            "ic_t_stat": s["ic_stat"]["t_stat"],
            "n_observations": int(len(s["returns"])),
            "overlap_counts": dict(sorted(overlap.counts.items())),
            "overlap_returns": dict(sorted(overlap.cohort_returns.items())),
        }

    return {
        "factors": factors,
        "snapshot_identity": engine_snapshot.snapshot_identity,
        "provenance": dict(engine_snapshot.provenance.to_mapping()),
        "config": {
            "horizon": config.horizon, "n_outer": config.n_outer, "n_inner": config.n_inner,
            "embargo": config.embargo, "quantiles": list(config.quantiles),
            "fdr_q": config.fdr_q, "min_names": config.min_names, "seed": config.seed,
        },
    }
