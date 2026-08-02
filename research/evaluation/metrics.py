# research/evaluation/metrics.py
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def sharpe(returns: pd.Series, periods_per_year: float) -> float:
    r = returns.dropna()
    if len(r) < 2:
        return 0.0
    sd = r.std(ddof=1)
    mean = r.mean()
    if sd == 0:
        # Zero variance yields an undefined Sharpe ratio regardless of mean
        # (constant series, positive or zero). Returning 0.0 keeps this
        # consistent with sharpe_stats() and avoids astronomically large or
        # non-finite values that would break JSON-serialized run cards.
        return 0.0
    return float(mean / sd * math.sqrt(periods_per_year))


def max_drawdown(returns: pd.Series) -> float:
    r = returns.dropna()
    if len(r) == 0:
        return 0.0
    equity = (1.0 + r).cumprod()
    drawdown = equity / equity.cummax() - 1.0
    return float(drawdown.min())


def annualized_turnover(turnover: pd.Series, periods_per_year: float) -> float:
    t = turnover.dropna()
    if len(t) == 0:
        return 0.0
    return float(t.mean() * periods_per_year)


def sharpe_stats(returns: pd.Series) -> dict:
    r = np.asarray(returns.dropna(), dtype=float)
    n = int(r.size)
    if n < 4:
        return {"n": n, "sr": 0.0, "skew": 0.0, "kurt": 3.0}
    mu = float(r.mean())
    sd = float(r.std(ddof=1))
    if sd == 0:
        return {"n": n, "sr": 0.0, "skew": 0.0, "kurt": 3.0}
    s0 = float(r.std(ddof=0))
    skew = float(((r - mu) ** 3).mean() / s0**3)
    kurt = float(((r - mu) ** 4).mean() / s0**4)
    return {"n": n, "sr": mu / sd, "skew": skew, "kurt": kurt}


def probabilistic_sharpe(sr: float, n: int, skew: float, kurt: float, sr_star: float) -> float:
    """Bailey & Lopez de Prado PSR: probability the true Sharpe exceeds sr_star."""
    if n < 4:
        return 0.0
    variance = 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr**2
    denom = math.sqrt(max(1e-12, variance))
    z = (sr - sr_star) * math.sqrt(n - 1) / denom
    return norm_cdf(z)


def ic_summary(ic: pd.Series) -> dict:
    values = ic.dropna()
    n = len(values)
    if n < 2:
        return {"mean": 0.0, "t_stat": 0.0, "p_value": 1.0, "hit_rate": 0.0}
    mean = float(values.mean())
    sd = float(values.std(ddof=1))
    t_stat = 0.0 if sd == 0 else mean / (sd / math.sqrt(n))
    p_value = 2.0 * (1.0 - norm_cdf(abs(t_stat)))
    hit_rate = float((values > 0).mean())
    return {"mean": mean, "t_stat": float(t_stat), "p_value": float(p_value), "hit_rate": hit_rate}
