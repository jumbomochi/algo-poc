# tests/research/evaluation/test_multiple_testing.py
from __future__ import annotations

import pytest

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


def _two_candidates() -> dict[str, dict]:
    return {
        "strong": {"sr": 0.5, "n": 500, "skew": 0.0, "kurt": 3.0, "ic_p": 0.001},
        "noise": {"sr": 0.001, "n": 500, "skew": 0.0, "kurt": 3.0, "ic_p": 0.95},
    }


def test_expected_max_sharpe_uses_the_declared_count_not_the_sample_size():
    # The spread estimate still comes from the observed SRs -- that is the only
    # sample there is -- but the E[max] benchmark must scale with the number of
    # trials actually searched, which is larger than the number in this run.
    srs = [0.0, 0.2]
    assert expected_max_sharpe(srs, n_trials=8) > expected_max_sharpe(srs)


def test_explicit_trial_count_deflates_harder_than_a_smaller_one():
    # This is the whole point of KAN-38: deflating against the four candidates
    # in today's run understates a search that has really tried eight.
    four = control(_two_candidates(), n_trials=4)
    eight = control(_two_candidates(), n_trials=8)
    assert eight["strong"].deflated_sharpe < four["strong"].deflated_sharpe


def test_declared_trial_count_below_the_run_is_rejected():
    with pytest.raises(ValueError, match="declared trial count"):
        control(_two_candidates(), n_trials=1)


def test_declaring_a_search_the_sample_cannot_support_is_refused():
    # One candidate gives no estimate of the spread of trial Sharpes, so SR*
    # collapses to zero and the deflation silently becomes a no-op -- while
    # the run card still records the declared count. For a capital gate the
    # only safe direction is to fail loudly.
    lone = {"strong": {"sr": 0.5, "n": 500, "skew": 0.0, "kurt": 3.0, "ic_p": 0.001}}
    with pytest.raises(ValueError, match="at least 2 candidates"):
        control(lone, n_trials=12)


def test_expected_max_sharpe_refuses_a_declared_count_it_cannot_deflate():
    with pytest.raises(ValueError, match="at least 2 candidates"):
        expected_max_sharpe([0.4], n_trials=12)


def test_declaring_the_run_size_reproduces_the_implicit_default():
    # AC2, pinned as an invariant rather than by the absence of failures:
    # the default path is exactly n_trials == the number of candidates.
    per_factor = _two_candidates()
    implicit = control(per_factor)
    explicit = control(per_factor, n_trials=len(per_factor))
    assert implicit == explicit
