#!/usr/bin/env python3
"""Walk-forward model training for signal quality scoring.

Trains a LightGBM binary classifier to predict whether a trade signal
will be profitable, using features from entry_signals + bar-derived data.

Usage:
    # First run a backtest and save results:
    python scripts/run_backtest.py --years 10 --save data/backtest_results.json

    # Then train the model:
    python scripts/train_signal_model.py --results data/backtest_results.json
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import date

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd

from backtest.feature_extractor import extract_features


def _prepare_for_lgb(df: pd.DataFrame) -> pd.DataFrame:
    """Convert object columns to category for LightGBM."""
    df = df.copy()
    for col in df.select_dtypes(include=["object"]).columns:
        df[col] = df[col].astype("category")
    return df


def walk_forward_evaluate(
    features: pd.DataFrame,
    labels: pd.Series,
    dates: pd.Series,
    n_splits: int = 3,
) -> list[dict]:
    """Walk-forward cross-validation.

    Splits data chronologically: train on earlier periods, test on later.
    Returns per-fold metrics.
    """
    sorted_unique = np.sort(dates.unique())
    split_size = len(sorted_unique) // (n_splits + 1)

    if split_size < 1:
        return []

    results = []
    for i in range(n_splits):
        train_end_idx = (i + 2) * split_size - 1
        test_end_idx = min((i + 3) * split_size - 1, len(sorted_unique) - 1)

        train_end_date = sorted_unique[train_end_idx]
        test_end_date = sorted_unique[test_end_idx]

        train_mask = dates <= train_end_date
        test_mask = (dates > train_end_date) & (dates <= test_end_date)

        X_train = _prepare_for_lgb(features[train_mask])
        y_train = labels[train_mask]
        X_test = _prepare_for_lgb(features[test_mask])
        y_test = labels[test_mask]

        if len(X_train) < 50 or len(X_test) < 10:
            continue

        cat_cols = X_train.select_dtypes(include=["category"]).columns.tolist()
        train_data = lgb.Dataset(
            X_train, label=y_train, categorical_feature=cat_cols, free_raw_data=False,
        )

        params = {
            "objective": "binary",
            "metric": "binary_logloss",
            "verbosity": -1,
            "num_leaves": 15,
            "learning_rate": 0.05,
            "feature_fraction": 0.8,
            "bagging_fraction": 0.8,
            "bagging_freq": 5,
            "seed": 42,
        }

        model = lgb.train(params, train_data, num_boost_round=100)

        y_pred_proba = model.predict(X_test)
        y_pred = (y_pred_proba > 0.5).astype(int)

        accuracy = float((y_pred == y_test.values).mean())
        baseline_win_rate = float(y_test.mean())

        # Win rate when model says "buy" (confidence > 0.5)
        buy_mask = y_pred_proba > 0.5
        filtered_win_rate = (
            float(y_test.values[buy_mask].mean()) if buy_mask.sum() > 0 else 0.0
        )

        results.append({
            "fold": i + 1,
            "train_size": int(len(X_train)),
            "test_size": int(len(X_test)),
            "accuracy": accuracy,
            "baseline_win_rate": baseline_win_rate,
            "filtered_win_rate": filtered_win_rate,
            "signals_passed_pct": float(buy_mask.mean()),
        })

    return results


def train_final_model(
    features: pd.DataFrame,
    labels: pd.Series,
) -> lgb.Booster:
    """Train a final model on all available data."""
    features = _prepare_for_lgb(features)
    cat_cols = features.select_dtypes(include=["category"]).columns.tolist()

    train_data = lgb.Dataset(
        features, label=labels, categorical_feature=cat_cols, free_raw_data=False,
    )

    params = {
        "objective": "binary",
        "metric": "binary_logloss",
        "verbosity": -1,
        "num_leaves": 15,
        "learning_rate": 0.05,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "seed": 42,
    }

    return lgb.train(params, train_data, num_boost_round=100)


def main():
    parser = argparse.ArgumentParser(
        description="Train signal quality model from backtest results"
    )
    parser.add_argument(
        "--results", required=True,
        help="Path to backtest results JSON (from run_backtest.py --save)",
    )
    parser.add_argument(
        "--output-dir", default="data/models",
        help="Directory to save model and metrics (default: data/models)",
    )
    parser.add_argument(
        "--n-splits", type=int, default=3,
        help="Number of walk-forward splits (default: 3)",
    )
    args = parser.parse_args()

    # Load backtest results
    with open(args.results) as f:
        data = json.load(f)

    # Collect all trades from all portfolios
    all_trades = []
    if "portfolios" in data:
        for name, pf in data["portfolios"].items():
            for trade in pf.get("trades", []):
                trade["portfolio"] = name
                all_trades.append(trade)
    elif "trades" in data:
        all_trades = data["trades"]

    if not all_trades:
        print("No trades found in results file.")
        return

    print(f"Loaded {len(all_trades)} trades from {args.results}")

    # Extract features
    features, labels = extract_features(all_trades)
    print(f"Feature matrix: {features.shape[0]} samples x {features.shape[1]} features")
    print(f"Baseline win rate: {labels.mean():.1%}")

    # Parse entry dates for walk-forward split
    dates = pd.Series([
        trade.get("entry_date", "2020-01-01") for trade in all_trades
    ])
    dates = pd.to_datetime(dates)

    # Walk-forward evaluation
    print(f"\nWalk-forward evaluation ({args.n_splits} splits):")
    print("-" * 60)
    fold_results = walk_forward_evaluate(features, labels, dates, args.n_splits)

    for r in fold_results:
        print(f"  Fold {r['fold']}: "
              f"train={r['train_size']}, test={r['test_size']}, "
              f"acc={r['accuracy']:.1%}, "
              f"baseline_wr={r['baseline_win_rate']:.1%}, "
              f"filtered_wr={r['filtered_win_rate']:.1%}, "
              f"passed={r['signals_passed_pct']:.0%}")

    if fold_results:
        avg_acc = np.mean([r["accuracy"] for r in fold_results])
        avg_improvement = np.mean([
            r["filtered_win_rate"] - r["baseline_win_rate"]
            for r in fold_results
        ])
        print(f"\n  Avg accuracy: {avg_acc:.1%}")
        print(f"  Avg win rate improvement: {avg_improvement:+.1%}")

    # Train final model on all data
    print("\nTraining final model on all data...")
    model = train_final_model(features, labels)

    # Feature importance
    importance = model.feature_importance(importance_type="gain")
    feature_names = features.columns.tolist()
    imp_sorted = sorted(
        zip(feature_names, importance), key=lambda x: x[1], reverse=True
    )
    print("\nTop 10 features by importance:")
    for name, imp in imp_sorted[:10]:
        print(f"  {name}: {imp:.1f}")

    # Save
    os.makedirs(args.output_dir, exist_ok=True)
    model_path = os.path.join(args.output_dir, "signal_quality_model.txt")
    model.save_model(model_path)

    metrics_path = os.path.join(args.output_dir, "signal_quality_metrics.json")
    metrics = {
        "total_trades": len(all_trades),
        "features": feature_names,
        "baseline_win_rate": float(labels.mean()),
        "walk_forward_folds": fold_results,
        "feature_importance": {n: float(i) for n, i in imp_sorted},
    }
    with open(metrics_path, "w") as f:
        json.dump(metrics, f, indent=2)

    print(f"\nModel saved to {model_path}")
    print(f"Metrics saved to {metrics_path}")


if __name__ == "__main__":
    main()
