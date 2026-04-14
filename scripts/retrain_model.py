#!/usr/bin/env python3
"""Automated model retraining pipeline.

Queries completed trades from the DB, extracts features, runs walk-forward
evaluation, and conditionally promotes a new model if it outperforms the
current active ModelVersion.

Usage:
    python -m scripts.retrain_model --db-url sqlite:///data/paper.db
    python -m scripts.retrain_model --dry-run
    python -m scripts.retrain_model --force
"""
from __future__ import annotations

import argparse
import os
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backtest.feature_extractor import extract_features
from scripts.train_signal_model import (
    _prepare_for_lgb,
    train_final_model,
    walk_forward_evaluate,
)
from shared.models.ml_models import ModelVersion
from shared.models.portfolio import Trade


def load_trades_from_db(session: Session) -> list[dict]:
    """Load completed trades from the DB, excluding those without entry_signals.

    Returns a list of dicts with keys suitable for ML feature extraction:
    ticker, portfolio, entry_price, exit_price, quantity, entry_date,
    exit_date, pnl, exit_reason, entry_signals, bar_features.
    """
    stmt = select(Trade).where(Trade.entry_signals.isnot(None)).order_by(Trade.executed_at)
    rows = session.execute(stmt).scalars().all()

    trades = []
    for t in rows:
        # Double-check at Python level: SQLite JSON may pass IS NOT NULL
        # even when the deserialized value is None.
        if t.entry_signals is None:
            continue
        trades.append({
            "ticker": t.ticker,
            "portfolio": t.portfolio,
            "entry_price": t.entry_price,
            "exit_price": t.price,
            "quantity": t.quantity,
            "entry_date": str(t.entry_date),
            "exit_date": str(t.executed_at.date()),
            "pnl": t.pnl,
            "exit_reason": t.exit_reason,
            "entry_signals": t.entry_signals,
            "bar_features": t.bar_features,
        })
    return trades


def get_active_model_metrics(session: Session) -> dict | None:
    """Return metrics dict for the currently active ModelVersion, or None."""
    stmt = select(ModelVersion).where(ModelVersion.is_active == True).limit(1)
    model = session.execute(stmt).scalar_one_or_none()
    if model is None:
        return None
    return model.metrics


def compare_models(
    current_metrics: dict | None,
    new_metrics: dict,
) -> bool:
    """Decide whether the new model should be promoted.

    Returns True if:
    - There is no current model (first model always promoted), or
    - New accuracy >= current accuracy AND new filtered_win_rate >= current.
    """
    if current_metrics is None:
        return True
    return (
        new_metrics.get("accuracy", 0) >= current_metrics.get("accuracy", 0)
        and new_metrics.get("filtered_win_rate", 0) >= current_metrics.get("filtered_win_rate", 0)
    )


def run_retraining_pipeline(
    session: Session,
    min_samples: int = 200,
    output_dir: str = "data/models",
    n_splits: int = 3,
    dry_run: bool = False,
    force: bool = False,
) -> dict:
    """Run the full retraining pipeline.

    Steps:
    1. Load trades from DB
    2. Check minimum sample count
    3. Extract features
    4. Walk-forward evaluate
    5. (optionally) Train final model and compare with active model
    6. Save model and record ModelVersion in DB

    Returns a dict with status and metadata.
    """
    # 1. Load trades
    trades = load_trades_from_db(session)

    # 2. Check minimum sample count
    if len(trades) < min_samples:
        return {
            "status": "skipped",
            "reason": f"Insufficient trades: {len(trades)} < {min_samples} min_training_samples",
        }

    # 3. Extract features
    features, labels = extract_features(trades)

    # 4. Parse dates for walk-forward splitting
    dates = pd.Series([t.get("entry_date", "2020-01-01") for t in trades])
    dates = pd.to_datetime(dates)

    # 5. Walk-forward evaluation
    fold_results = walk_forward_evaluate(features, labels, dates, n_splits)

    if not fold_results:
        return {
            "status": "skipped",
            "reason": "Walk-forward produced no valid folds",
        }

    # 6. Compute aggregate metrics
    avg_accuracy = float(np.mean([r["accuracy"] for r in fold_results]))
    avg_filtered_wr = float(np.mean([r["filtered_win_rate"] for r in fold_results]))
    avg_baseline_wr = float(np.mean([r["baseline_win_rate"] for r in fold_results]))

    new_metrics = {
        "accuracy": avg_accuracy,
        "filtered_win_rate": avg_filtered_wr,
        "baseline_win_rate": avg_baseline_wr,
        "n_folds": len(fold_results),
        "n_trades": len(trades),
        "fold_details": fold_results,
    }

    # 7. Dry-run: return metrics without training or saving
    if dry_run:
        return {
            "status": "dry_run",
            "metrics": new_metrics,
        }

    # 8. Train final model on all data
    model = train_final_model(features, labels)

    # 9. Promotion decision
    current_metrics = get_active_model_metrics(session)
    should_promote = force or compare_models(current_metrics, new_metrics)

    # 10. Save model file
    today = date.today().isoformat()
    version = f"v{today}"
    os.makedirs(output_dir, exist_ok=True)

    versioned_path = os.path.join(output_dir, f"signal_quality_{version}.txt")
    model.save_model(versioned_path)

    # Also save as the canonical path if promoted (for paper trader)
    if should_promote:
        canonical_path = os.path.join(output_dir, "signal_quality_model.txt")
        model.save_model(canonical_path)

    # 11. Deactivate current model if promoting
    if should_promote:
        stmt = (
            select(ModelVersion).where(ModelVersion.is_active == True)
        )
        active_models = session.execute(stmt).scalars().all()
        for m in active_models:
            m.is_active = False

    # 12. Parse training window from trade dates
    all_dates = [t["entry_date"] for t in trades]
    window_start = min(all_dates)
    window_end = max(all_dates)

    # 13. Record ModelVersion in DB
    mv = ModelVersion(
        version=version,
        training_window_start=date.fromisoformat(window_start),
        training_window_end=date.fromisoformat(window_end),
        metrics=new_metrics,
        model_path=versioned_path,
        is_active=should_promote,
        created_at=datetime.now(timezone.utc),
    )
    session.add(mv)
    session.flush()

    status = "promoted" if should_promote else "skipped"
    return {
        "status": status,
        "version": version,
        "metrics": new_metrics,
        "model_path": versioned_path,
    }


def main() -> None:
    """CLI entry point for automated retraining."""
    parser = argparse.ArgumentParser(
        description="Retrain signal quality model from paper trading DB"
    )
    parser.add_argument(
        "--db-url",
        default=None,
        help="Database URL (default: from config/default.yaml)",
    )
    parser.add_argument(
        "--output-dir",
        default="data/models",
        help="Directory to save model files (default: data/models)",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=200,
        help="Minimum number of completed trades required (default: 200)",
    )
    parser.add_argument(
        "--n-splits",
        type=int,
        default=3,
        help="Number of walk-forward splits (default: 3)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force promotion even if new model isn't strictly better",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate only; don't train final model or save",
    )
    args = parser.parse_args()

    # Resolve DB URL
    db_url = args.db_url
    if db_url is None:
        from shared.config import load_config
        config = load_config("config/default.yaml")
        db_url = config.database.url

    engine = create_engine(db_url)
    factory = sessionmaker(bind=engine)
    session = factory()

    try:
        result = run_retraining_pipeline(
            session=session,
            min_samples=args.min_samples,
            output_dir=args.output_dir,
            n_splits=args.n_splits,
            dry_run=args.dry_run,
            force=args.force,
        )

        print(f"Retraining result: {result['status']}")
        if "reason" in result:
            print(f"  Reason: {result['reason']}")
        if "metrics" in result:
            m = result["metrics"]
            print(f"  Accuracy: {m.get('accuracy', 0):.1%}")
            print(f"  Filtered win rate: {m.get('filtered_win_rate', 0):.1%}")
            print(f"  Baseline win rate: {m.get('baseline_win_rate', 0):.1%}")
        if "version" in result:
            print(f"  Version: {result['version']}")
        if "model_path" in result:
            print(f"  Model path: {result['model_path']}")

        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
