# research/evaluation/multiple_testing.py
from __future__ import annotations

import math
from dataclasses import dataclass

from research.evaluation.metrics import probabilistic_sharpe

_EULER = 0.5772156649015329


def inv_norm(p: float) -> float:
    """Inverse standard normal CDF via Peter Acklam's rational approximation."""
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
        (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1))


def expected_max_sharpe(trial_srs: list[float]) -> float:
    m = len(trial_srs)
    if m < 2:
        return 0.0
    mean = sum(trial_srs) / m
    var = sum((s - mean) ** 2 for s in trial_srs) / (m - 1)
    if var <= 0:
        return 0.0
    std = math.sqrt(var)
    a = inv_norm(1 - 1.0 / m)
    b = inv_norm(1 - 1.0 / (m * math.e))
    return std * ((1 - _EULER) * a + _EULER * b)


def benjamini_hochberg(p_values: dict[str, float], q: float) -> dict[str, bool]:
    items = sorted(p_values.items(), key=lambda kv: kv[1])
    m = len(items)
    max_rank = 0
    for rank, (_, p) in enumerate(items, start=1):
        if p <= (rank / m) * q:
            max_rank = rank
    selected = {fid for rank, (fid, _) in enumerate(items, start=1) if rank <= max_rank}
    return {fid: fid in selected for fid in p_values}


@dataclass(frozen=True)
class MultipleTestingVerdict:
    deflated_sharpe: float
    passes_dsr: bool
    passes_fdr: bool
    survives: bool


def control(
    per_factor: dict[str, dict], q: float = 0.10, dsr_threshold: float = 0.95
) -> dict[str, MultipleTestingVerdict]:
    sr_star = expected_max_sharpe([v["sr"] for v in per_factor.values()])
    fdr = benjamini_hochberg({fid: v["ic_p"] for fid, v in per_factor.items()}, q)
    verdicts: dict[str, MultipleTestingVerdict] = {}
    for fid, v in per_factor.items():
        dsr = probabilistic_sharpe(v["sr"], v["n"], v["skew"], v["kurt"], sr_star)
        passes_dsr = dsr >= dsr_threshold
        passes_fdr = fdr[fid]
        verdicts[fid] = MultipleTestingVerdict(
            deflated_sharpe=dsr,
            passes_dsr=passes_dsr,
            passes_fdr=passes_fdr,
            survives=passes_dsr and passes_fdr,
        )
    return verdicts
