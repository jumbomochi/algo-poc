# tests/research/evaluation/test_multiple_testing.py
from __future__ import annotations

from research.evaluation.multiple_testing import (
    benjamini_hochberg,
    control,
    expected_max_sharpe,
    inv_norm,
)


def test_inv_norm_is_inverse_of_cdf_midpoints():
    assert round(inv_norm(0.5), 6) == 0.0
    assert round(inv_norm(0.975), 2) == 1.96


def test_expected_max_sharpe_grows_with_trial_count_and_spread():
    few = expected_max_sharpe([0.0, 0.1, 0.2])
    many = expected_max_sharpe([0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7])
    assert many > few >= 0.0


def test_benjamini_hochberg_selects_low_p_values():
    passed = benjamini_hochberg({"a": 0.001, "b": 0.2, "c": 0.9, "d": 0.04}, q=0.10)
    assert passed["a"] is True
    assert passed["c"] is False


def test_control_requires_both_gates():
    per_factor = {
        "strong": {"sr": 0.5, "n": 500, "skew": 0.0, "kurt": 3.0, "ic_p": 0.001},
        "noise": {"sr": 0.001, "n": 500, "skew": 0.0, "kurt": 3.0, "ic_p": 0.95},
    }
    verdicts = control(per_factor)
    assert verdicts["strong"].survives is True
    assert verdicts["noise"].survives is False
    assert verdicts["noise"].passes_fdr is False
