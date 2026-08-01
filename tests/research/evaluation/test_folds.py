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
        train_idx = set(range(tr_start, tr_end))
        test_idx = set(range(te_start, te_end))
        assert train_idx.isdisjoint(test_idx)
        assert tr_end <= te_start - gap  # purge + embargo before test
        for inner in outer.inner:
            it_start, it_end = inner.train
            iv_start, iv_end = inner.validate
            assert it_start >= tr_start and iv_end <= tr_end  # inner inside outer-train
            assert it_end <= iv_start - gap


def test_insufficient_history_raises():
    with pytest.raises(ValueError, match="not enough dates"):
        nested_walk_forward(n_dates=5, n_outer=3, n_inner=2, horizon=5, embargo=5)
