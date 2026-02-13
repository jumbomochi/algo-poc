from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from services.ml_model.trainer import ModelTrainer


def test_train_produces_model_and_metrics():
    np.random.seed(42)
    n = 500
    features = pd.DataFrame({
        "support_proximity": np.random.randn(n),
        "support_strength": np.random.randn(n),
        "support_trend": np.random.randn(n),
        "valuation": np.random.randn(n),
        "quality": np.random.randn(n),
        "growth": np.random.randn(n),
        "earnings_surprise": np.random.randn(n),
        "news_sentiment": np.random.randn(n),
        "insider_activity": np.random.randn(n),
    })
    targets = pd.Series(np.random.choice(["sell", "hold", "buy"], n))
    trainer = ModelTrainer()
    model, metrics = trainer.train(features, targets)
    assert model is not None
    assert "accuracy" in metrics
    assert "feature_importance" in metrics
    assert len(metrics["feature_importance"]) == 9


def test_train_rejects_insufficient_samples():
    trainer = ModelTrainer(min_samples=200)
    features = pd.DataFrame({"a": [1, 2, 3]})
    targets = pd.Series(["buy", "sell", "hold"])
    with pytest.raises(ValueError, match="Insufficient"):
        trainer.train(features, targets)


def test_train_accuracy_is_valid_float():
    np.random.seed(99)
    n = 300
    features = pd.DataFrame({
        "support_proximity": np.random.randn(n),
        "support_strength": np.random.randn(n),
        "support_trend": np.random.randn(n),
        "valuation": np.random.randn(n),
        "quality": np.random.randn(n),
        "growth": np.random.randn(n),
        "earnings_surprise": np.random.randn(n),
        "news_sentiment": np.random.randn(n),
        "insider_activity": np.random.randn(n),
    })
    targets = pd.Series(np.random.choice(["sell", "hold", "buy"], n))
    trainer = ModelTrainer()
    _, metrics = trainer.train(features, targets)
    assert 0.0 <= metrics["accuracy"] <= 1.0


def test_feature_importance_keys_match_columns():
    np.random.seed(7)
    n = 250
    features = pd.DataFrame({
        "support_proximity": np.random.randn(n),
        "support_strength": np.random.randn(n),
        "support_trend": np.random.randn(n),
        "valuation": np.random.randn(n),
        "quality": np.random.randn(n),
        "growth": np.random.randn(n),
        "earnings_surprise": np.random.randn(n),
        "news_sentiment": np.random.randn(n),
        "insider_activity": np.random.randn(n),
    })
    targets = pd.Series(np.random.choice(["sell", "hold", "buy"], n))
    trainer = ModelTrainer()
    _, metrics = trainer.train(features, targets)
    assert set(metrics["feature_importance"].keys()) == set(features.columns)
