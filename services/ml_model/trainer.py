from __future__ import annotations

from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score


class ModelTrainer:
    """Trains a LightGBM multiclass classifier for buy/sell/hold prediction.

    The model maps 9 signal features to one of three target classes:
    "buy", "sell", or "hold".
    """

    def __init__(self, min_samples: int = 200):
        self._min_samples = min_samples

    def train(
        self,
        features: pd.DataFrame,
        targets: pd.Series,
    ) -> tuple[lgb.Booster, dict[str, Any]]:
        """Train a LightGBM model on the provided features and targets.

        Args:
            features: DataFrame with signal feature columns.
            targets: Series with target labels ("buy", "sell", "hold").

        Returns:
            Tuple of (trained Booster model, metrics dict with accuracy
            and feature_importance).

        Raises:
            ValueError: If the number of samples is below min_samples.
        """
        if len(features) < self._min_samples:
            raise ValueError(
                f"Insufficient training samples: {len(features)} "
                f"(minimum {self._min_samples})"
            )

        # Encode string targets to integers
        label_map = {"sell": 0, "hold": 1, "buy": 2}
        encoded_targets = targets.map(label_map)

        # Train/validation split
        X_train, X_val, y_train, y_val = train_test_split(
            features, encoded_targets, test_size=0.2, random_state=42
        )

        train_data = lgb.Dataset(X_train, label=y_train)
        val_data = lgb.Dataset(X_val, label=y_val, reference=train_data)

        params = {
            "objective": "multiclass",
            "num_class": 3,
            "metric": "multi_logloss",
            "verbosity": -1,
            "num_leaves": 31,
            "learning_rate": 0.05,
            "feature_fraction": 0.9,
            "bagging_fraction": 0.8,
            "bagging_freq": 5,
            "seed": 42,
        }

        model = lgb.train(
            params,
            train_data,
            num_boost_round=100,
            valid_sets=[val_data],
            callbacks=[lgb.log_evaluation(period=0)],
        )

        # Evaluate on validation set
        y_pred_proba = model.predict(X_val)
        y_pred = np.argmax(y_pred_proba, axis=1)
        acc = accuracy_score(y_val, y_pred)

        # Feature importance
        importance = model.feature_importance(importance_type="gain")
        feature_names = features.columns.tolist()
        importance_dict = {
            name: float(imp) for name, imp in zip(feature_names, importance)
        }

        metrics = {
            "accuracy": float(acc),
            "feature_importance": importance_dict,
        }

        return model, metrics
