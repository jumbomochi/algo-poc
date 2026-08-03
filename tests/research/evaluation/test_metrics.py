# tests/research/evaluation/test_metrics.py
from __future__ import annotations

import math

import pandas as pd

from research.evaluation.metrics import (
    annualized_turnover,
    ic_summary,
    max_drawdown,
    norm_cdf,
    probabilistic_sharpe,
    sharpe,
    sharpe_stats,
)


def test_norm_cdf_known_points():
    assert round(norm_cdf(0.0), 6) == 0.5
    assert round(norm_cdf(1.96), 3) == 0.975


def test_sharpe_and_drawdown():
    r = pd.Series([0.01, 0.02, 0.015, 0.012])
    assert sharpe(r, periods_per_year=252) > 0
    dd = max_drawdown(pd.Series([0.1, -0.5, 0.0]))
    assert round(dd, 3) == -0.5


def test_sharpe_zero_variance_is_zero():
    assert sharpe(pd.Series([0.01, 0.01, 0.01, 0.01]), periods_per_year=252) == 0.0
    assert sharpe(pd.Series([0.0, 0.0, 0.0, 0.0]), periods_per_year=252) == 0.0


def test_probabilistic_sharpe_rises_with_stronger_track_record():
    strong = probabilistic_sharpe(sr=0.3, n=252, skew=0.0, kurt=3.0, sr_star=0.0)
    weak = probabilistic_sharpe(sr=0.02, n=252, skew=0.0, kurt=3.0, sr_star=0.0)
    assert strong > weak
    assert 0.0 <= strong <= 1.0


def test_ic_summary_perfectly_positive_series():
    summary = ic_summary(pd.Series([0.2, 0.3, 0.25, 0.28]))
    assert summary["mean"] > 0
    assert summary["hit_rate"] == 1.0
    assert 0.0 <= summary["p_value"] <= 1.0


def test_annualized_turnover():
    assert annualized_turnover(pd.Series([0.5, 0.5]), periods_per_year=12) == 6.0
