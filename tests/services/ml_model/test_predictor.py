from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from services.ml_model.trainer import ModelTrainer
from services.ml_model.predictor import ModelPredictor


@pytest.fixture()
def trained_model():
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
    model, _ = trainer.train(features, targets)
    return model


def test_predict_returns_valid_action(trained_model):
    predictor = ModelPredictor(trained_model)
    features = {
        "support_proximity": 0.5,
        "support_strength": 0.3,
        "support_trend": 0.1,
        "valuation": 0.2,
        "quality": 0.4,
        "growth": -0.1,
        "earnings_surprise": 0.3,
        "news_sentiment": 0.6,
        "insider_activity": -0.2,
    }
    action, confidence, top_features = predictor.predict(features)

    assert action in ("buy", "sell", "hold")


def test_predict_returns_valid_confidence(trained_model):
    predictor = ModelPredictor(trained_model)
    features = {
        "support_proximity": 0.5,
        "support_strength": 0.3,
        "support_trend": 0.1,
        "valuation": 0.2,
        "quality": 0.4,
        "growth": -0.1,
        "earnings_surprise": 0.3,
        "news_sentiment": 0.6,
        "insider_activity": -0.2,
    }
    _, confidence, _ = predictor.predict(features)

    assert 0.0 <= confidence <= 1.0


def test_predict_returns_top_features(trained_model):
    predictor = ModelPredictor(trained_model)
    features = {
        "support_proximity": 0.5,
        "support_strength": 0.3,
        "support_trend": 0.1,
        "valuation": 0.2,
        "quality": 0.4,
        "growth": -0.1,
        "earnings_surprise": 0.3,
        "news_sentiment": 0.6,
        "insider_activity": -0.2,
    }
    _, _, top_features = predictor.predict(features)

    assert isinstance(top_features, dict)
    assert len(top_features) > 0
    # Top features should be a subset of input features
    assert all(k in features for k in top_features)
    # All values should be floats
    assert all(isinstance(v, float) for v in top_features.values())


def test_predict_top_features_limited_to_top_n(trained_model):
    predictor = ModelPredictor(trained_model, top_n=3)
    features = {
        "support_proximity": 0.5,
        "support_strength": 0.3,
        "support_trend": 0.1,
        "valuation": 0.2,
        "quality": 0.4,
        "growth": -0.1,
        "earnings_surprise": 0.3,
        "news_sentiment": 0.6,
        "insider_activity": -0.2,
    }
    _, _, top_features = predictor.predict(features)

    assert len(top_features) <= 3
