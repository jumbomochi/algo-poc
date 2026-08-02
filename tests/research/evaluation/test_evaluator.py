from __future__ import annotations

import os
import subprocess
import sys
from datetime import date, timedelta

from research.evaluation.evaluator import EvaluationConfig, evaluate_factors


def _trending_bars(n_days=400):
    start = date(2024, 1, 1)
    tickers = {"A": 1.0, "B": 0.6, "C": 0.3, "D": -0.2, "E": -0.5, "F": -0.9}
    bars = {}
    for ticker, drift in tickers.items():
        price = 100.0
        rows = []
        for i in range(n_days):
            price = max(1.0, price + drift)
            rows.append({"date": start + timedelta(days=i), "open": price, "high": price + 1,
                         "low": price - 1, "close": price, "volume": 1_000 + i})
        bars[ticker] = rows
    return bars


def test_evaluate_returns_per_factor_evidence_and_provenance():
    config = EvaluationConfig(horizon=5, n_outer=3, n_inner=2, embargo=5, min_names=3)
    result = evaluate_factors(_trending_bars(), config=config)
    assert set(result["factors"]) == {"price_momentum_126d", "high_52w", "low_volatility_63d", "liquidity_20d"}
    momentum = result["factors"]["price_momentum_126d"]
    assert {"sharpe", "deflated_sharpe", "survives_multiple_testing", "ic_mean", "chosen_quantile"} <= set(momentum)
    assert isinstance(result["snapshot_identity"], str) and result["snapshot_identity"]
    assert "data_cutoff" in result["provenance"]


def test_determinism_same_bars_same_result():
    config = EvaluationConfig(horizon=5, n_outer=3, n_inner=2, embargo=5, min_names=3)
    bars = _trending_bars()
    assert evaluate_factors(bars, config=config) == evaluate_factors(bars, config=config)


_CROSS_SEED_SCRIPT = """
import json
from datetime import date, timedelta

from research.evaluation.evaluator import EvaluationConfig, evaluate_factors


def _trending_bars(n_days=400):
    start = date(2024, 1, 1)
    tickers = {"A": 1.0, "B": 0.6, "C": 0.3, "D": -0.2, "E": -0.5, "F": -0.9}
    bars = {}
    for ticker, drift in tickers.items():
        price = 100.0
        rows = []
        for i in range(n_days):
            price = max(1.0, price + drift)
            rows.append({"date": start + timedelta(days=i), "open": price, "high": price + 1,
                         "low": price - 1, "close": price, "volume": 1_000 + i})
        bars[ticker] = rows
    return bars


config = EvaluationConfig(horizon=5, n_outer=3, n_inner=2, embargo=5, min_names=3)
result = evaluate_factors(_trending_bars(), config=config)
print(json.dumps(result, sort_keys=True))
"""


def test_evaluate_is_deterministic_across_hash_seeds():
    outputs = []
    for seed in ("0", "1"):
        proc = subprocess.run(
            [sys.executable, "-c", _CROSS_SEED_SCRIPT],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        outputs.append(proc.stdout)
    assert outputs[0] == outputs[1]
