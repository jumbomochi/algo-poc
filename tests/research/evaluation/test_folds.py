# tests/research/evaluation/test_folds.py
from __future__ import annotations

import pytest

from research.evaluation.folds import nested_walk_forward


def test_no_test_index_appears_in_training_and_gap_is_respected():
    folds = nested_walk_forward(n_dates=100, n_outer=3, n_inner=2, horizon=5, embargo=5)
    assert len(folds) == 3
    gap = 10
    for outer in folds:
        tr_start, tr_end = outer.train
        te_start, te_end = outer.test
        assert tr_end > tr_start  # training span must be non-empty
        train_idx = set(range(tr_start, tr_end))
        test_idx = set(range(te_start, te_end))
        assert train_idx.isdisjoint(test_idx)
        assert tr_end <= te_start - gap  # purge + embargo before test
        for inner in outer.inner:
            it_start, it_end = inner.train
            iv_start, iv_end = inner.validate
            assert it_end > it_start  # inner training span must be non-empty
            assert it_start >= tr_start and iv_end <= tr_end  # inner inside outer-train
            assert it_end <= iv_start - gap


def test_insufficient_history_raises():
    with pytest.raises(ValueError, match="not enough dates"):
        nested_walk_forward(n_dates=5, n_outer=3, n_inner=2, horizon=5, embargo=5)


def test_first_outer_fold_train_width_grows_with_more_history():
    # A zero-length (or fixed-width) fold-0 training span is useless for model
    # selection: more history must translate into more usable training data
    # for the very first outer fold, not just for later, larger folds.
    small = nested_walk_forward(n_dates=100, n_outer=3, n_inner=2, horizon=5, embargo=5)
    large = nested_walk_forward(n_dates=1000, n_outer=3, n_inner=2, horizon=5, embargo=5)

    small_width = small[0].train[1] - small[0].train[0]
    large_width = large[0].train[1] - large[0].train[0]

    assert small_width > 0
    assert large_width > small_width
