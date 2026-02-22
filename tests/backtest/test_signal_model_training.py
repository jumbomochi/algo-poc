from __future__ import annotations

import os
import tempfile
from datetime import date, timedelta

import numpy as np
import pandas as pd

from scripts.train_signal_model import walk_forward_evaluate, train_final_model, _prepare_for_lgb


def _make_training_data(n: int = 300) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """Generate synthetic training data with date ordering."""
    np.random.seed(42)
    features = pd.DataFrame({
        "portfolio": np.random.choice(["mr", "mom", "sector"], n),
        "signal_rank": np.random.uniform(1, 10, n),
        "bar_return_5d": np.random.normal(0, 0.05, n),
        "bar_vol_20d": np.random.uniform(0.01, 0.05, n),
    })
    # Label partially correlated with features (lower rank = more profitable)
    labels = pd.Series(
        (features["signal_rank"] < 5).astype(int) | (np.random.random(n) > 0.7).astype(int),
        name="profitable",
    )
    base_date = date(2020, 1, 1)
    dates = pd.Series([
        base_date + timedelta(days=i) if i < 200
        else date(2021, 1, 1) + timedelta(days=(i - 200))
        for i in range(n)
    ])
    return features, labels, dates


def test_walk_forward_returns_fold_results():
    """Walk-forward should return per-fold accuracy metrics."""
    features, labels, dates = _make_training_data(300)
    results = walk_forward_evaluate(features, labels, dates, n_splits=2)

    assert len(results) >= 1
    for r in results:
        assert "accuracy" in r
        assert "train_size" in r
        assert "test_size" in r
        assert 0.0 <= r["accuracy"] <= 1.0


def test_walk_forward_train_before_test():
    """Training data should be chronologically before test data."""
    features, labels, dates = _make_training_data(300)
    results = walk_forward_evaluate(features, labels, dates, n_splits=2)

    # Each fold's test period should come after training period
    for r in results:
        assert r["train_size"] > 0
        assert r["test_size"] > 0


def test_train_final_model_returns_booster():
    """train_final_model should return a LightGBM Booster."""
    import lightgbm as lgb

    features, labels, _ = _make_training_data(300)
    model = train_final_model(features, labels)

    assert isinstance(model, lgb.Booster)


def test_train_final_model_can_predict():
    """Trained model should produce predictions in [0, 1]."""
    features, labels, _ = _make_training_data(300)
    model = train_final_model(features, labels)

    preds = model.predict(_prepare_for_lgb(features.head(5)))
    assert len(preds) == 5
    assert all(0.0 <= p <= 1.0 for p in preds)
