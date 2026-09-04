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
from datetime import date, timedelta

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd

from backtest.feature_extractor import extract_features


# Calendar days of embargo between a fold's training data and its test window.
# On top of purging by holding period this drops training entries taken right
# before the test window, whose features overlap the same market conditions.
DEFAULT_EMBARGO_DAYS = 5


def _prepare_for_lgb(df: pd.DataFrame) -> pd.DataFrame:
    """Convert object columns to category for LightGBM."""
    df = df.copy()
    for col in df.select_dtypes(include=["object"]).columns:
        df[col] = df[col].astype("category")
    return df


def purged_train_mask(
    dates: pd.Series,
    exit_dates: pd.Series,
    train_end_date,
    test_start_date,
    embargo_days: int = DEFAULT_EMBARGO_DAYS,
) -> pd.Series:
    """Training rows for one fold, purged of label overlap and embargoed.

    A trade's label is only knowable once the trade closes. A trade entered
    before the train/test boundary but exited on or after ``test_start_date``
    therefore carries information from inside the test window, and training on
    it leaks the future — the defect behind finding 4.5 of the 2026-08-06
    review, where a signal filter could remove trades it already knew had lost.

    Kept rows must satisfy all of:

    - entered on or before ``train_end_date`` (chronological split);
    - closed strictly before ``test_start_date`` (purge);
    - entered at least ``embargo_days`` before ``test_start_date`` (embargo).
    """
    embargo_cutoff = pd.Timestamp(test_start_date) - pd.Timedelta(days=embargo_days)
    return (
        (dates <= pd.Timestamp(train_end_date))
        & (exit_dates < pd.Timestamp(test_start_date))
        & (dates < embargo_cutoff)
    )


def walk_forward_evaluate(
    features: pd.DataFrame,
    labels: pd.Series,
    dates: pd.Series,
    n_splits: int = 3,
    *,
    exit_dates: pd.Series,
    embargo_days: int = DEFAULT_EMBARGO_DAYS,
) -> list[dict]:
    """Purged, embargoed walk-forward cross-validation.

    Splits data chronologically: train on earlier periods, test on later. Each
    fold's training set is purged of trades whose holding period runs into the
    test window and embargoed for ``embargo_days`` before it — ``exit_dates`` is
    required precisely so that purge can never be skipped by accident.

    Returns per-fold metrics, including how many training rows each fold lost
    to the purge and the embargo.
    """
    dates = pd.to_datetime(dates)
    exit_dates = pd.to_datetime(exit_dates)
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

        test_mask = (dates > train_end_date) & (dates <= test_end_date)
        if not test_mask.any():
            continue
        test_start_date = dates[test_mask].min()

        chronological_mask = dates <= train_end_date
        train_mask = purged_train_mask(
            dates=dates,
            exit_dates=exit_dates,
            train_end_date=train_end_date,
            test_start_date=test_start_date,
            embargo_days=embargo_days,
        )
        # Split the dropped rows into the two reasons, for reporting.
        purged = int(
            (chronological_mask & (exit_dates >= pd.Timestamp(test_start_date))).sum()
        )
        embargoed = int((chronological_mask & ~train_mask).sum()) - purged

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
            "purged": purged,
            "embargoed": embargoed,
            "accuracy": accuracy,
            "baseline_win_rate": baseline_win_rate,
            "filtered_win_rate": filtered_win_rate,
            "signals_passed_pct": float(buy_mask.mean()),
        })

    return results


def build_model_metadata(
    dates: pd.Series,
    exit_dates: pd.Series,
    embargo_days: int,
    n_trades: int,
    categorical_features: list[str] | None = None,
) -> dict:
    """Provenance for a trained model: what window it has already seen.

    ``out_of_sample_from`` is the first date on which applying this model is
    genuinely out of sample — the day after its last training label closed,
    plus the embargo. ``assert_ml_filter_out_of_sample`` enforces it.
    """
    dates = pd.to_datetime(dates)
    exit_dates = pd.to_datetime(exit_dates)
    train_end = exit_dates.max().date()
    return {
        "train_start_date": dates.min().date().isoformat(),
        "train_end_date": train_end.isoformat(),
        "embargo_days": int(embargo_days),
        "out_of_sample_from": (
            train_end + timedelta(days=embargo_days + 1)
        ).isoformat(),
        "n_trades": int(n_trades),
        # Which features LightGBM treated as categorical, in frame order.
        # Needed at PREDICT time: a one-row frame has only the object columns
        # that happen to be present, so the categorical count never matches
        # training's and LightGBM refuses with "train and valid dataset
        # categorical_feature do not match". The model file records the LEVELS
        # (Booster.pandas_categorical) but not which columns they belong to.
        "categorical_features": list(categorical_features or []),
    }


def metadata_path_for_model(model_path: str) -> str:
    """Sidecar metadata path for a saved model file."""
    root, _ = os.path.splitext(model_path)
    return f"{root}.meta.json"


def write_model_metadata(model_path: str, metadata: dict) -> str:
    """Write the sidecar metadata next to the model. Returns its path."""
    path = metadata_path_for_model(model_path)
    with open(path, "w") as f:
        json.dump(metadata, f, indent=2)
    return path


def assert_ml_filter_out_of_sample(model_path: str, trade_start_date) -> dict:
    """Refuse to apply a signal-quality model to its own training window.

    A model trained on a backtest's trades will happily filter out the losers
    it already memorised, which is not a result — it is the training set read
    back (finding 4.5). Applying the filter therefore requires the backtest's
    first trading date to fall after the model's training window plus embargo.

    Returns the model metadata on success; raises ``ValueError`` otherwise.
    """
    meta_path = metadata_path_for_model(model_path)
    if not os.path.exists(meta_path):
        raise ValueError(
            f"No training metadata at {meta_path}. A signal-quality model may "
            "only be applied when its training window is known — retrain with "
            "scripts/train_signal_model.py to generate the sidecar."
        )
    with open(meta_path) as f:
        metadata = json.load(f)

    out_of_sample_from = date.fromisoformat(metadata["out_of_sample_from"])
    if trade_start_date is None or trade_start_date < out_of_sample_from:
        described = trade_start_date.isoformat() if trade_start_date else "unbounded"
        raise ValueError(
            f"Refusing an in-sample ML filter: {model_path} trained through "
            f"{metadata['train_end_date']} (embargo {metadata['embargo_days']}d), "
            f"so it may only be applied from {out_of_sample_from.isoformat()}; "
            f"this backtest starts trading {described}. Re-run with "
            f"--start-date {out_of_sample_from.isoformat()} or later, or train "
            "a model on an earlier slice of history."
        )
    return metadata


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
    parser.add_argument(
        "--embargo-days", type=int, default=DEFAULT_EMBARGO_DAYS,
        help=(
            "Calendar days of embargo between each fold's training data and its "
            f"test window (default: {DEFAULT_EMBARGO_DAYS})"
        ),
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

    # Entry and exit dates: entries drive the chronological split, exits drive
    # the purge (a trade's label is only knowable once it closes).
    dates = pd.to_datetime(
        pd.Series([trade.get("entry_date", "2020-01-01") for trade in all_trades])
    )
    exit_dates = pd.to_datetime(
        pd.Series([
            trade.get("exit_date") or trade.get("entry_date", "2020-01-01")
            for trade in all_trades
        ])
    )

    # Walk-forward evaluation
    print(
        f"\nPurged walk-forward evaluation ({args.n_splits} splits, "
        f"{args.embargo_days}d embargo):"
    )
    print("-" * 60)
    fold_results = walk_forward_evaluate(
        features, labels, dates, args.n_splits,
        exit_dates=exit_dates, embargo_days=args.embargo_days,
    )

    for r in fold_results:
        print(f"  Fold {r['fold']}: "
              f"train={r['train_size']}, test={r['test_size']}, "
              f"purged={r['purged']}, embargoed={r['embargoed']}, "
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

    # Provenance sidecar: run_backtest refuses to apply a filter in-sample, and
    # it needs to know what window this model already saw.
    metadata = build_model_metadata(
        dates=dates,
        exit_dates=exit_dates,
        embargo_days=args.embargo_days,
        n_trades=len(all_trades),
        categorical_features=(
            # OBJECT, not "category": _prepare_for_lgb does the
            # conversion inside train_final_model, so this frame still
            # holds the pre-conversion dtypes.
            features.select_dtypes(include=["object"]).columns.tolist()
        ),
    )
    metadata_path = write_model_metadata(model_path, metadata)

    metrics_path = os.path.join(args.output_dir, "signal_quality_metrics.json")
    metrics = {
        "total_trades": len(all_trades),
        "features": feature_names,
        "baseline_win_rate": float(labels.mean()),
        "walk_forward_folds": fold_results,
        "feature_importance": {n: float(i) for n, i in imp_sorted},
        "training_window": metadata,
    }
    with open(metrics_path, "w") as f:
        json.dump(metrics, f, indent=2)

    print(f"\nModel saved to {model_path}")
    print(f"Metrics saved to {metrics_path}")
    print(f"Training-window metadata saved to {metadata_path}")
    print(
        f"  This model is only out of sample from "
        f"{metadata['out_of_sample_from']} — run_backtest.py --ml-filter "
        f"requires --start-date on or after that date."
    )


if __name__ == "__main__":
    main()
