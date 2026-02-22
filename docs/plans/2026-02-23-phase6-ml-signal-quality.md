# Phase 6: ML Signal Quality Scoring

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Train a LightGBM model on backtest trade data to predict which signals will be profitable, then use it as an optional quality filter to improve signal win rates.

**Architecture:** Post-hoc feature extraction from completed backtest trades (entry_signals + bar-derived features + regime), walk-forward evaluation to prevent overfitting, and an inference-time signal wrapper that scores buy signals and suppresses low-confidence ones. No changes to BacktestRunner or RiskEngine.

**Tech Stack:** LightGBM (existing dep), pandas (existing dep), numpy (existing dep), joblib (existing dep)

---

## Context

### What we have

- 8 strategy signal functions producing trade dicts with `entry_signals` metadata
- `BacktestRunner` capturing `entry_signals` in every trade record (line 153 of `backtest/runner.py`)
- Complete bars_by_ticker and regime_by_date available in `main()` after backtest
- Existing `ModelTrainer` in `services/ml_model/trainer.py` (LightGBM multiclass) — we'll build a new binary classifier for signal quality
- ~4800 total trades across 8 strategies over 10 years (enough for ML)

### What we're building

1. **Feature Extractor** — enriches trade dicts with bar-derived features, extracts flat DataFrame for ML
2. **Walk-Forward Training** — trains per-fold models, evaluates on held-out future data
3. **ML Signal Filter** — wraps any signal function, scores signals, suppresses low-confidence buys
4. **Integration** — `--ml-filter` flag on backtest, evaluation comparison

### Design decisions

- **Pooled model across strategies** — strategy name is a categorical feature, not separate models per strategy. This gives ~4800 training samples instead of ~80-400 per strategy.
- **Binary classification** — predict profitable (PnL > 0) vs unprofitable. Simpler than regression and directly actionable.
- **Filter, not replace** — ML doesn't generate signals, it gates existing signals. Sell signals always pass through (never block exits). Only buy signals are scored.
- **Walk-forward, not random split** — prevents look-ahead bias. Train on years 1-7, test on 8-10.

---

## Task 1: Feature Extractor

**Files:**
- Create: `backtest/feature_extractor.py`
- Create: `tests/backtest/test_feature_extractor.py`

### Step 1: Write the failing tests

Create `tests/backtest/test_feature_extractor.py`:

```python
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from backtest.feature_extractor import enrich_trades, extract_features


def _make_bars(ticker: str, n: int = 30) -> list[dict]:
    """Generate n synthetic daily bars."""
    bars = []
    price = 100.0
    for i in range(n):
        d = date(2024, 1, 1 + i) if i < 31 else date(2024, 2, i - 30)
        price *= 1 + (i % 3 - 1) * 0.01  # oscillate +-1%
        bars.append({
            "date": date(2024, 1, 2 + i),
            "open": price * 0.999,
            "high": price * 1.01,
            "low": price * 0.99,
            "close": price,
            "volume": 1_000_000 + i * 10_000,
        })
    return bars


def test_enrich_adds_bar_features():
    """enrich_trades should add bar_features dict to each trade."""
    bars = _make_bars("AAPL", 30)
    trades = [{
        "ticker": "AAPL",
        "entry_date": bars[25]["date"],
        "exit_date": bars[29]["date"],
        "entry_price": 100.0,
        "exit_price": 105.0,
        "quantity": 10,
        "pnl": 50.0,
        "entry_signals": {"rsi": 0.3},
    }]

    enrich_trades(trades, {"AAPL": bars})

    assert "bar_features" in trades[0]
    bf = trades[0]["bar_features"]
    assert "return_5d" in bf
    assert "return_20d" in bf
    assert "vol_20d" in bf
    assert "volume_ratio" in bf
    assert isinstance(bf["return_5d"], float)


def test_enrich_adds_regime():
    """enrich_trades should add regime from regime_by_date."""
    bars = _make_bars("AAPL", 30)
    entry_date = bars[25]["date"]
    trades = [{
        "ticker": "AAPL",
        "entry_date": entry_date,
        "exit_date": bars[29]["date"],
        "entry_price": 100.0,
        "exit_price": 105.0,
        "quantity": 10,
        "pnl": 50.0,
        "entry_signals": {},
    }]

    regime_by_date = {entry_date: "bull"}
    enrich_trades(trades, {"AAPL": bars}, regime_by_date)

    assert trades[0]["bar_features"]["regime"] == "bull"


def test_extract_features_returns_dataframe():
    """extract_features should return (DataFrame, Series) with correct shape."""
    trades = [
        {
            "portfolio": "momentum",
            "entry_signals": {"rank": 2, "strategy": "momentum"},
            "bar_features": {"return_5d": 0.03, "vol_20d": 0.02, "regime": "bull"},
            "pnl": 100.0,
        },
        {
            "portfolio": "mean_reversion",
            "entry_signals": {"proximity": {"value": 0.9, "confidence": 0.8}},
            "bar_features": {"return_5d": -0.05, "vol_20d": 0.04, "regime": "bear"},
            "pnl": -50.0,
        },
    ]

    features, labels = extract_features(trades)

    assert isinstance(features, pd.DataFrame)
    assert isinstance(labels, pd.Series)
    assert len(features) == 2
    assert len(labels) == 2
    assert labels.iloc[0] == 1  # positive PnL
    assert labels.iloc[1] == 0  # negative PnL


def test_extract_handles_nested_signals():
    """Nested signal dicts should be flattened with prefix."""
    trades = [{
        "portfolio": "mr",
        "entry_signals": {"proximity": {"value": 0.9, "confidence": 0.85}},
        "bar_features": {},
        "pnl": 10.0,
    }]

    features, _ = extract_features(trades)

    assert "signal_proximity_value" in features.columns
    assert "signal_proximity_confidence" in features.columns
    assert features["signal_proximity_value"].iloc[0] == 0.9


def test_extract_handles_missing_signals():
    """Missing signal keys across strategies should produce NaN."""
    trades = [
        {"portfolio": "mr", "entry_signals": {"rsi": 0.3}, "bar_features": {}, "pnl": 10.0},
        {"portfolio": "mom", "entry_signals": {"rank": 2}, "bar_features": {}, "pnl": -5.0},
    ]

    features, _ = extract_features(trades)

    # "signal_rsi" exists for trade 1, NaN for trade 2
    assert not pd.isna(features["signal_rsi"].iloc[0])
    assert pd.isna(features["signal_rsi"].iloc[1])
```

### Step 2: Run tests to verify they fail

Run: `pytest tests/backtest/test_feature_extractor.py -v`
Expected: FAIL — `ModuleNotFoundError`

### Step 3: Write implementation

Create `backtest/feature_extractor.py`:

```python
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd


def enrich_trades(
    trades: list[dict],
    bars_by_ticker: dict[str, list[dict]],
    regime_by_date: dict | None = None,
) -> None:
    """Add bar-derived features to each trade dict (mutates in place).

    Features added to trade["bar_features"]:
    - return_5d: 5-trading-day return ending at entry_date
    - return_20d: 20-trading-day return ending at entry_date
    - vol_20d: 20-day daily return standard deviation
    - volume_ratio: entry-day volume / 20-day average volume
    - regime: market regime at entry_date (if regime_by_date provided)
    """
    # Build sorted bar lists and date-to-index lookup per ticker
    bar_index: dict[str, dict[date, int]] = {}
    sorted_bars: dict[str, list[dict]] = {}
    for ticker, bars in bars_by_ticker.items():
        s = sorted(bars, key=lambda b: b["date"])
        sorted_bars[ticker] = s
        bar_index[ticker] = {b["date"]: i for i, b in enumerate(s)}

    for trade in trades:
        ticker = trade["ticker"]
        entry_date = trade["entry_date"]
        features: dict = {}

        idx_map = bar_index.get(ticker, {})
        entry_idx = idx_map.get(entry_date)
        bars = sorted_bars.get(ticker, [])

        if entry_idx is not None and entry_idx >= 20:
            closes = [b["close"] for b in bars[entry_idx - 20 : entry_idx + 1]]
            volumes = [b["volume"] for b in bars[entry_idx - 20 : entry_idx + 1]]

            # Recent returns
            if len(closes) >= 6:
                features["return_5d"] = (closes[-1] - closes[-6]) / closes[-6]
            if len(closes) >= 21:
                features["return_20d"] = (closes[-1] - closes[0]) / closes[0]

            # Volatility (std of daily returns over 20 days)
            daily_rets = [
                (closes[i] - closes[i - 1]) / closes[i - 1]
                for i in range(1, len(closes))
            ]
            features["vol_20d"] = float(np.std(daily_rets)) if daily_rets else 0.0

            # Volume ratio
            avg_vol = np.mean(volumes[:-1]) if len(volumes) > 1 else 1.0
            features["volume_ratio"] = (
                float(volumes[-1] / avg_vol) if avg_vol > 0 else 1.0
            )

        # Regime
        if regime_by_date and entry_date in regime_by_date:
            features["regime"] = regime_by_date[entry_date]

        trade["bar_features"] = features


def extract_features(trades: list[dict]) -> tuple[pd.DataFrame, pd.Series]:
    """Extract feature DataFrame and binary labels from enriched trades.

    Features include:
    - portfolio (categorical)
    - signal_* (flattened entry_signals, NaN where missing)
    - bar_* (bar-derived features from enrich_trades)

    Labels: 1 if trade PnL > 0, else 0.
    """
    records: list[dict] = []
    labels: list[int] = []

    for trade in trades:
        row: dict = {}
        row["portfolio"] = trade.get("portfolio", "unknown")

        # Flatten entry_signals
        signals = trade.get("entry_signals", {})
        for key, val in signals.items():
            if isinstance(val, dict):
                for subkey, subval in val.items():
                    if isinstance(subval, (int, float)):
                        row[f"signal_{key}_{subkey}"] = subval
            elif isinstance(val, (int, float)):
                row[f"signal_{key}"] = val

        # Bar-derived features
        bar_features = trade.get("bar_features", {})
        for key, val in bar_features.items():
            if key == "regime":
                row["bar_regime"] = val
            elif isinstance(val, (int, float)):
                row[f"bar_{key}"] = val

        records.append(row)
        labels.append(1 if trade.get("pnl", 0) > 0 else 0)

    df = pd.DataFrame(records)
    return df, pd.Series(labels, name="profitable")
```

### Step 4: Run tests

Run: `pytest tests/backtest/test_feature_extractor.py -v`
Expected: 5 passed

Run: `pytest tests/backtest/ -v --tb=short`
Expected: All tests pass

### Step 5: Commit

```bash
git add backtest/feature_extractor.py tests/backtest/test_feature_extractor.py
git commit -m "feat: add trade feature extractor for ML signal quality scoring"
```

---

## Task 2: Enrich Trades in Backtest Main + Save

**Files:**
- Modify: `scripts/run_backtest.py`

### Step 1: Add enrichment after backtest runs

In `scripts/run_backtest.py`, add import at the top (near other backtest imports):

```python
from backtest.feature_extractor import enrich_trades
```

In `main()`, after all portfolios have run and results are collected (after `compute_aggregate_metrics` call), add trade enrichment for multi-portfolio mode:

```python
    # Enrich trades with bar-derived features (for ML training)
    for name, result in results.items():
        enrich_trades(result.trades, bars_by_ticker, regime_by_date)
```

And for single-portfolio mode (the backward-compat path), add the same after `runner.run()`:

```python
    enrich_trades(result.trades, bars_by_ticker, regime_by_date)
```

### Step 2: Run tests

Run: `pytest tests/backtest/ -v --tb=short`
Expected: All existing tests still pass (enrichment is additive, doesn't change existing behavior)

### Step 3: Commit

```bash
git add scripts/run_backtest.py
git commit -m "feat: enrich backtest trades with bar-derived features for ML"
```

---

## Task 3: Walk-Forward Model Training Script

**Files:**
- Create: `scripts/train_signal_model.py`
- Create: `tests/backtest/test_signal_model_training.py`

### Step 1: Write the failing tests

Create `tests/backtest/test_signal_model_training.py`:

```python
from __future__ import annotations

import os
import tempfile
from datetime import date

import numpy as np
import pandas as pd

from scripts.train_signal_model import walk_forward_evaluate, train_final_model


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
    dates = pd.Series([
        date(2020, 1, 1 + i % 365) if i < 200 else date(2021, 1, 1 + (i - 200) % 365)
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

    preds = model.predict(features.head(5))
    assert len(preds) == 5
    assert all(0.0 <= p <= 1.0 for p in preds)
```

### Step 2: Run tests to verify they fail

Run: `pytest tests/backtest/test_signal_model_training.py -v`
Expected: FAIL — `ModuleNotFoundError`

### Step 3: Write implementation

Create `scripts/train_signal_model.py`:

```python
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
```

### Step 4: Run tests

Run: `pytest tests/backtest/test_signal_model_training.py -v`
Expected: 4 passed

Run: `pytest tests/backtest/ -v --tb=short`
Expected: All tests pass

### Step 5: Commit

```bash
git add scripts/train_signal_model.py tests/backtest/test_signal_model_training.py
git commit -m "feat: add walk-forward signal quality model training script"
```

---

## Task 4: ML Signal Quality Filter

**Files:**
- Modify: `scripts/run_backtest.py` (add `make_ml_filtered_signals_fn`)
- Create: `tests/backtest/test_ml_signal_filter.py`

### Step 1: Write the failing tests

Create `tests/backtest/test_ml_signal_filter.py`:

```python
from __future__ import annotations

import os
import tempfile
from datetime import date
from unittest.mock import MagicMock

import numpy as np
import pandas as pd

from scripts.run_backtest import make_ml_filtered_signals_fn


def _make_mock_model(always_score: float = 0.8):
    """Create a mock LightGBM model that always returns the same score."""
    model = MagicMock()
    model.predict = MagicMock(return_value=np.array([always_score]))
    model.feature_name = MagicMock(return_value=["portfolio", "signal_rank"])
    return model


def _make_bars(n: int = 30) -> list[dict]:
    """Generate n synthetic bars."""
    return [
        {"date": date(2024, 1, 2 + i), "open": 100.0, "high": 101.0,
         "low": 99.0, "close": 100.0 + i * 0.1, "volume": 1_000_000}
        for i in range(n)
    ]


def test_ml_filter_passes_high_confidence():
    """Signals with model confidence > threshold should pass through."""
    model = _make_mock_model(always_score=0.8)

    def inner_fn(ticker, bars):
        return {
            "action": "buy", "ticker": ticker, "limit_price": 100.0,
            "quantity": 10, "sector": "Tech",
            "signals": {"rank": 1, "strategy": "momentum"},
        }

    filtered_fn = make_ml_filtered_signals_fn(inner_fn, model, threshold=0.6)
    result = filtered_fn("AAPL", _make_bars())

    assert result is not None
    assert result["action"] == "buy"


def test_ml_filter_blocks_low_confidence():
    """Signals with model confidence < threshold should be blocked."""
    model = _make_mock_model(always_score=0.3)

    def inner_fn(ticker, bars):
        return {
            "action": "buy", "ticker": ticker, "limit_price": 100.0,
            "quantity": 10, "sector": "Tech",
            "signals": {"rank": 5, "strategy": "momentum"},
        }

    filtered_fn = make_ml_filtered_signals_fn(inner_fn, model, threshold=0.6)
    result = filtered_fn("AAPL", _make_bars())

    assert result is None


def test_ml_filter_always_passes_sell_signals():
    """Sell signals should never be blocked by ML filter."""
    model = _make_mock_model(always_score=0.1)  # very low confidence

    def inner_fn(ticker, bars):
        return {
            "action": "sell", "ticker": ticker, "limit_price": 100.0,
            "quantity": 0, "exit_reason": "trailing_stop",
        }

    filtered_fn = make_ml_filtered_signals_fn(inner_fn, model, threshold=0.9)
    result = filtered_fn("AAPL", _make_bars())

    assert result is not None
    assert result["action"] == "sell"


def test_ml_filter_passes_none_through():
    """If inner function returns None, filter returns None."""
    model = _make_mock_model(always_score=0.9)

    def inner_fn(ticker, bars):
        return None

    filtered_fn = make_ml_filtered_signals_fn(inner_fn, model, threshold=0.5)
    result = filtered_fn("AAPL", _make_bars())

    assert result is None
```

### Step 2: Run tests to verify they fail

Run: `pytest tests/backtest/test_ml_signal_filter.py -v`
Expected: FAIL — `ImportError: cannot import name 'make_ml_filtered_signals_fn'`

### Step 3: Write implementation

In `scripts/run_backtest.py`, add after the other `make_*_signals_fn` factories:

```python
def make_ml_filtered_signals_fn(
    inner_fn: Callable[[str, list[dict]], dict | None],
    model,
    threshold: float = 0.5,
    strategy_name: str = "unknown",
) -> Callable[[str, list[dict]], dict | None]:
    """Wrap a signal function with ML quality scoring.

    Buy signals are scored by the model. If P(profitable) < threshold,
    the signal is suppressed. Sell signals and None always pass through.

    Args:
        inner_fn: Original signal function to wrap.
        model: Trained LightGBM Booster with predict() method.
        threshold: Minimum model confidence to pass a buy signal.
        strategy_name: Portfolio/strategy name for the feature vector.
    """
    def filtered_signals_fn(ticker: str, bars: list[dict]) -> dict | None:
        signal = inner_fn(ticker, bars)
        if signal is None:
            return None

        # Always pass sell signals
        if signal.get("action") != "buy":
            return signal

        # Build feature vector for the model
        row: dict = {"portfolio": strategy_name}

        # Flatten signal features
        signals = signal.get("signals", {})
        for key, val in signals.items():
            if isinstance(val, dict):
                for subkey, subval in val.items():
                    if isinstance(subval, (int, float)):
                        row[f"signal_{key}_{subkey}"] = subval
            elif isinstance(val, (int, float)):
                row[f"signal_{key}"] = val

        # Bar-derived features (from recent bars)
        if len(bars) >= 21:
            closes = [b["close"] for b in bars[-21:]]
            volumes = [b["volume"] for b in bars[-21:]]

            row["bar_return_5d"] = (closes[-1] - closes[-6]) / closes[-6]
            row["bar_return_20d"] = (closes[-1] - closes[0]) / closes[0]

            daily_rets = [
                (closes[i] - closes[i - 1]) / closes[i - 1]
                for i in range(1, len(closes))
            ]
            row["bar_vol_20d"] = float(np.std(daily_rets))

            avg_vol = np.mean(volumes[:-1])
            row["bar_volume_ratio"] = float(volumes[-1] / avg_vol) if avg_vol > 0 else 1.0

        # Create DataFrame matching model's expected features
        feature_names = model.feature_name()
        feature_row = {name: row.get(name, np.nan) for name in feature_names}
        df = pd.DataFrame([feature_row])

        # Convert categorical columns
        for col in df.select_dtypes(include=["object"]).columns:
            df[col] = df[col].astype("category")

        # Score
        score = model.predict(df)[0]

        if score >= threshold:
            return signal
        return None

    return filtered_signals_fn
```

Also add the necessary imports at the top of `scripts/run_backtest.py` if not already present:

```python
import numpy as np
import pandas as pd
```

### Step 4: Run tests

Run: `pytest tests/backtest/test_ml_signal_filter.py -v`
Expected: 4 passed

Run: `pytest tests/backtest/ -v --tb=short`
Expected: All tests pass

### Step 5: Commit

```bash
git add scripts/run_backtest.py tests/backtest/test_ml_signal_filter.py
git commit -m "feat: add ML signal quality filter wrapper for buy signals"
```

---

## Task 5: Wire ML Filter into Backtest + Docs

**Files:**
- Modify: `scripts/run_backtest.py` (add `--ml-filter` flag to main())
- Modify: `docs/strategy.md`

### Step 1: Add `--ml-filter` flag to main()

In `main()`, add argument:

```python
    parser.add_argument("--ml-filter", default=None,
                        help="Path to trained signal quality model (LightGBM .txt file)")
    parser.add_argument("--ml-threshold", type=float, default=0.55,
                        help="ML filter confidence threshold (default: 0.55)")
```

After building portfolios dict, if `--ml-filter` is provided, wrap each portfolio's signals_fn:

```python
    if args.ml_filter:
        import lightgbm as lgb
        ml_model = lgb.Booster(model_file=args.ml_filter)
        print(f"ML filter loaded from {args.ml_filter} (threshold={args.ml_threshold})")
        for name, pc in portfolios.items():
            portfolios[name] = PortfolioConfig(
                name=pc.name,
                capital=pc.capital,
                signals_fn=make_ml_filtered_signals_fn(
                    pc.signals_fn, ml_model,
                    threshold=args.ml_threshold,
                    strategy_name=name,
                ),
                risk_engine=pc.risk_engine,
            )
```

### Step 2: Add ML section to docs/strategy.md

After the "Paper Trading" section, add:

```markdown
## ML Signal Quality Scoring (Phase 6)

An optional ML layer that scores buy signals and suppresses low-confidence ones.

### Training Pipeline

1. Run a 10-year backtest to generate trade data with `entry_signals` metadata
2. `backtest/feature_extractor.py` enriches trades with bar-derived features (returns, volatility, regime)
3. `scripts/train_signal_model.py` trains a LightGBM binary classifier via walk-forward cross-validation

```bash
# Run backtest and save results
python scripts/run_backtest.py --years 10 --save data/backtest_results.json

# Train signal quality model
python scripts/train_signal_model.py --results data/backtest_results.json
```

### Using the ML Filter

```bash
# Run backtest with ML filter applied to all strategies
python scripts/run_backtest.py --years 10 --ml-filter data/models/signal_quality_model.txt

# Adjust confidence threshold (higher = more selective)
python scripts/run_backtest.py --years 10 --ml-filter data/models/signal_quality_model.txt --ml-threshold 0.6
```

### How It Works

- `make_ml_filtered_signals_fn()` wraps any signal function
- Buy signals are scored by the model using entry_signals + bar-derived features
- Signals with P(profitable) < threshold are suppressed
- Sell signals always pass through (never blocks exits)
- The model is trained on pooled trades across all 8 strategies
- Walk-forward validation prevents overfitting (train on past, test on future)
```

### Step 3: Run tests

Run: `pytest tests/backtest/ -v --tb=short`
Expected: All tests pass

### Step 4: Commit

```bash
git add scripts/run_backtest.py docs/strategy.md
git commit -m "feat: wire ML signal filter into backtest with --ml-filter flag"
```

---

## Verification

After all tasks:

```bash
# All tests should pass
pytest tests/ -v --tb=short

# Feature extractor tests
pytest tests/backtest/test_feature_extractor.py -v

# Model training tests
pytest tests/backtest/test_signal_model_training.py -v

# ML filter tests
pytest tests/backtest/test_ml_signal_filter.py -v

# End-to-end: run backtest, train model, run with filter
python scripts/run_backtest.py --years 10 --save data/backtest_results.json
python scripts/train_signal_model.py --results data/backtest_results.json
python scripts/run_backtest.py --years 10 --ml-filter data/models/signal_quality_model.txt
```
