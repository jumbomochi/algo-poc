from __future__ import annotations

import json
import os
import tempfile
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from scripts.train_signal_model import (
    DEFAULT_EMBARGO_DAYS,
    assert_ml_filter_out_of_sample,
    build_model_metadata,
    metadata_path_for_model,
    purged_train_mask,
    train_final_model,
    walk_forward_evaluate,
    write_model_metadata,
    _prepare_for_lgb,
)


def _make_training_data(
    n: int = 300, holding_days: int = 5
) -> tuple[pd.DataFrame, pd.Series, pd.Series, pd.Series]:
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
    dates = pd.to_datetime(dates)
    exit_dates = dates + pd.Timedelta(days=holding_days)
    return features, labels, dates, exit_dates


def test_walk_forward_returns_fold_results():
    """Walk-forward should return per-fold accuracy metrics."""
    features, labels, dates, exit_dates = _make_training_data(300)
    results = walk_forward_evaluate(
        features, labels, dates, n_splits=2, exit_dates=exit_dates
    )

    assert len(results) >= 1
    for r in results:
        assert "accuracy" in r
        assert "train_size" in r
        assert "test_size" in r
        assert 0.0 <= r["accuracy"] <= 1.0


def test_walk_forward_train_before_test():
    """Training data should be chronologically before test data."""
    features, labels, dates, exit_dates = _make_training_data(300)
    results = walk_forward_evaluate(
        features, labels, dates, n_splits=2, exit_dates=exit_dates
    )

    # Each fold's test period should come after training period
    for r in results:
        assert r["train_size"] > 0
        assert r["test_size"] > 0


class TestPurgedTrainMask:
    """Finding 4.5: the walk-forward had no purge for the holding period.

    A trade entered before the train/test boundary but exited *after* it has a
    label that was only knowable inside the test window. Training on it leaks
    the future.
    """

    BOUNDARY = pd.Timestamp("2024-06-30")
    TEST_START = pd.Timestamp("2024-07-01")

    def _mask(self, entries: list[str], exits: list[str], embargo_days: int = 0):
        return purged_train_mask(
            dates=pd.to_datetime(pd.Series(entries)),
            exit_dates=pd.to_datetime(pd.Series(exits)),
            train_end_date=self.BOUNDARY,
            test_start_date=self.TEST_START,
            embargo_days=embargo_days,
        )

    def test_trade_closed_before_the_boundary_is_kept(self):
        mask = self._mask(["2024-05-01"], ["2024-05-20"])
        assert bool(mask.iloc[0]) is True

    def test_trade_whose_label_lands_in_the_test_window_is_purged(self):
        mask = self._mask(["2024-06-20"], ["2024-07-10"])
        assert bool(mask.iloc[0]) is False

    def test_trade_entered_after_the_boundary_is_not_training_data(self):
        mask = self._mask(["2024-07-05"], ["2024-07-10"])
        assert bool(mask.iloc[0]) is False

    def test_exit_exactly_on_the_test_start_is_purged(self):
        mask = self._mask(["2024-06-01"], ["2024-07-01"])
        assert bool(mask.iloc[0]) is False

    def test_embargo_drops_entries_immediately_before_the_test_window(self):
        entries = ["2024-05-01", "2024-06-25"]
        exits = ["2024-05-10", "2024-06-27"]
        mask = self._mask(entries, exits, embargo_days=10)
        assert bool(mask.iloc[0]) is True
        assert bool(mask.iloc[1]) is False

    def test_default_embargo_is_non_zero(self):
        assert DEFAULT_EMBARGO_DAYS > 0


class TestWalkForwardPurging:
    """Uses a contiguous date axis so trades actually straddle fold boundaries."""

    def _contiguous(self, n: int = 300, holding_days: int = 5):
        features, labels, _, _ = _make_training_data(n)
        dates = pd.to_datetime(
            pd.Series([date(2020, 1, 1) + timedelta(days=i) for i in range(n)])
        )
        return features, labels, dates, dates + pd.Timedelta(days=holding_days)

    def test_fold_results_report_purged_and_embargoed_counts(self):
        features, labels, dates, exit_dates = self._contiguous(holding_days=20)
        results = walk_forward_evaluate(
            features, labels, dates, n_splits=2, exit_dates=exit_dates,
            embargo_days=5,
        )

        assert results
        for r in results:
            assert r["purged"] >= 0
            assert r["embargoed"] >= 0
        # A 20-day holding period straddles the fold boundary, so the trades
        # still open at the boundary must have been purged.
        assert sum(r["purged"] + r["embargoed"] for r in results) > 0

    def test_long_holding_periods_shrink_the_training_set(self):
        features, labels, dates, exit_dates = self._contiguous(holding_days=1)
        tight = walk_forward_evaluate(
            features, labels, dates, n_splits=2, exit_dates=exit_dates, embargo_days=0
        )
        overlapping = walk_forward_evaluate(
            features, labels, dates, n_splits=2,
            exit_dates=dates + pd.Timedelta(days=60),
            embargo_days=0,
        )

        assert tight and overlapping
        assert overlapping[0]["train_size"] < tight[0]["train_size"]

    def test_no_training_label_closes_inside_the_test_window(self):
        features, labels, dates, exit_dates = self._contiguous(holding_days=30)
        n = len(dates)
        split_size = n // 3
        train_end = dates.iloc[2 * split_size - 1]
        test_start = dates.iloc[2 * split_size]

        mask = purged_train_mask(
            dates=dates,
            exit_dates=exit_dates,
            train_end_date=train_end,
            test_start_date=test_start,
            embargo_days=5,
        )

        assert mask.any()
        assert (exit_dates[mask] < test_start).all()

    def test_exit_dates_are_required(self):
        features, labels, dates, _ = _make_training_data(300)
        with pytest.raises(TypeError):
            walk_forward_evaluate(features, labels, dates, n_splits=2)


class TestModelMetadata:
    """Finding 4.5: the ML filter could be applied to its own training window."""

    def test_metadata_records_the_training_window_and_embargo(self):
        _, _, dates, exit_dates = _make_training_data(300, holding_days=5)

        meta = build_model_metadata(
            dates=dates, exit_dates=exit_dates, embargo_days=7, n_trades=300
        )

        assert meta["train_start_date"] == dates.min().date().isoformat()
        assert meta["train_end_date"] == exit_dates.max().date().isoformat()
        assert meta["embargo_days"] == 7
        assert meta["n_trades"] == 300
        # The model must not be applied before this date.
        assert meta["out_of_sample_from"] == (
            exit_dates.max().date() + timedelta(days=8)
        ).isoformat()

    def test_metadata_path_sits_next_to_the_model(self):
        assert metadata_path_for_model("data/models/signal_quality_model.txt") == (
            "data/models/signal_quality_model.meta.json"
        )

    def test_write_and_read_roundtrip(self, tmp_path):
        model_path = tmp_path / "signal_quality_model.txt"
        model_path.write_text("dummy")
        meta = {
            "train_start_date": "2015-01-02",
            "train_end_date": "2024-12-31",
            "embargo_days": 5,
            "out_of_sample_from": "2025-01-06",
            "n_trades": 10,
        }

        written = write_model_metadata(str(model_path), meta)

        assert json.loads(open(written).read()) == meta


class TestOutOfSampleGuard:
    META = {
        "train_start_date": "2015-01-02",
        "train_end_date": "2024-12-31",
        "embargo_days": 5,
        "out_of_sample_from": "2025-01-06",
        "n_trades": 10,
    }

    def _model(self, tmp_path, meta=None):
        model_path = tmp_path / "signal_quality_model.txt"
        model_path.write_text("dummy")
        if meta is not None:
            write_model_metadata(str(model_path), meta)
        return str(model_path)

    def test_accepts_a_backtest_that_starts_after_the_training_window(self, tmp_path):
        model_path = self._model(tmp_path, self.META)
        meta = assert_ml_filter_out_of_sample(model_path, date(2025, 2, 1))
        assert meta["train_end_date"] == "2024-12-31"

    def test_rejects_a_backtest_that_overlaps_the_training_window(self, tmp_path):
        model_path = self._model(tmp_path, self.META)
        with pytest.raises(ValueError, match="in-sample"):
            assert_ml_filter_out_of_sample(model_path, date(2020, 1, 1))

    def test_rejects_a_backtest_inside_the_embargo(self, tmp_path):
        model_path = self._model(tmp_path, self.META)
        with pytest.raises(ValueError, match="in-sample"):
            assert_ml_filter_out_of_sample(model_path, date(2025, 1, 2))

    def test_rejects_an_unbounded_backtest(self, tmp_path):
        """No --start-date means the backtest covers the training window."""
        model_path = self._model(tmp_path, self.META)
        with pytest.raises(ValueError, match="in-sample"):
            assert_ml_filter_out_of_sample(model_path, None)

    def test_rejects_a_model_with_no_metadata(self, tmp_path):
        model_path = self._model(tmp_path, meta=None)
        with pytest.raises(ValueError, match="metadata"):
            assert_ml_filter_out_of_sample(model_path, date(2025, 2, 1))


def test_train_final_model_returns_booster():
    """train_final_model should return a LightGBM Booster."""
    import lightgbm as lgb

    features, labels, _, _ = _make_training_data(300)
    model = train_final_model(features, labels)

    assert isinstance(model, lgb.Booster)


def test_train_final_model_can_predict():
    """Trained model should produce predictions in [0, 1]."""
    features, labels, _, _ = _make_training_data(300)
    model = train_final_model(features, labels)

    preds = model.predict(_prepare_for_lgb(features.head(5)))
    assert len(preds) == 5
    assert all(0.0 <= p <= 1.0 for p in preds)
