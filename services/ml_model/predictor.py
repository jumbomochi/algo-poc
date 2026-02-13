from __future__ import annotations

from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd


# Map integer class indices back to string labels.
LABEL_MAP: dict[int, str] = {0: "sell", 1: "hold", 2: "buy"}


class ModelPredictor:
    """Wraps a trained LightGBM Booster to produce predictions with
    confidence scores and top contributing features."""

    def __init__(self, model: lgb.Booster, top_n: int = 5):
        self._model = model
        self._top_n = top_n

    def predict(
        self, features: dict[str, float]
    ) -> tuple[str, float, dict[str, float]]:
        """Predict action, confidence, and top features for a single sample.

        Args:
            features: Dict mapping feature names to float values.

        Returns:
            Tuple of (action, confidence, top_features) where:
                action: "buy", "sell", or "hold"
                confidence: 0.0 to 1.0 (probability of the predicted class)
                top_features: Dict of the top_n most important features
                    with their importance scores.
        """
        feature_names = list(features.keys())
        feature_values = [features[k] for k in feature_names]
        df = pd.DataFrame([feature_values], columns=feature_names)

        # Predict class probabilities
        proba = self._model.predict(df)[0]  # shape: (num_class,)
        predicted_class = int(np.argmax(proba))
        confidence = float(proba[predicted_class])
        action = LABEL_MAP[predicted_class]

        # Get feature importance (gain-based)
        importance = self._model.feature_importance(importance_type="gain")
        model_feature_names = self._model.feature_name()

        # Build importance dict
        importance_dict = {
            name: float(imp)
            for name, imp in zip(model_feature_names, importance)
        }

        # Sort by importance descending and take top_n
        sorted_features = sorted(
            importance_dict.items(), key=lambda x: x[1], reverse=True
        )
        top_features = {k: v for k, v in sorted_features[: self._top_n]}

        return action, confidence, top_features
