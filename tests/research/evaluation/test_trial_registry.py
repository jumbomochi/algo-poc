# tests/research/evaluation/test_trial_registry.py
from __future__ import annotations

import json
import math

import pytest

from research.evaluation.multiple_testing import control, expected_max_sharpe
from research.evaluation.trial_registry import (
    declared_trial_count,
    load_trial_registry,
    spread_sharpes,
)


def _write(path, entries) -> None:
    path.write_text(json.dumps({"version": 1, "entries": entries}))


def test_committed_registry_declares_at_least_the_sleeve_search():
    # Six survivors after dropping two losers is a search of at least eight.
    assert declared_trial_count() >= 8


def test_committed_registry_entries_are_attributable():
    registry = load_trial_registry()
    assert registry.entries
    for entry in registry.entries:
        assert entry.searched_at and entry.what and entry.source
        assert entry.n_trials >= 1


def test_count_is_the_sum_of_the_entries(tmp_path):
    path = tmp_path / "registry.json"
    _write(path, [
        {"searched_at": "2026-05-26", "what": "sleeves", "n_trials": 8, "source": "a.md"},
        {"searched_at": "2026-08-02", "what": "factors", "n_trials": 4, "source": "b.py"},
    ])
    assert declared_trial_count(path) == 12


def test_empty_registry_is_rejected(tmp_path):
    # A registry that declares nothing would deflate against nothing, which is
    # worse than the implicit count it replaced.
    path = tmp_path / "registry.json"
    _write(path, [])
    with pytest.raises(ValueError, match="no trial-registry entries"):
        declared_trial_count(path)


def test_non_positive_trial_counts_are_rejected(tmp_path):
    path = tmp_path / "registry.json"
    _write(path, [{"searched_at": "2026-05-26", "what": "s", "n_trials": 0, "source": "a.md"}])
    with pytest.raises(ValueError, match="n_trials"):
        declared_trial_count(path)


# --- D22: SR*'s spread from every return-objective trial on record ----------

_ROOT = math.sqrt(252.0)


def _trial(name, sharpe, objective="return"):
    return {"name": name, "sharpe": sharpe, "objective": objective}


def _registry(path, trials, n_trials=None):
    _write(path, [{
        "searched_at": "2026-05-26", "what": "s", "source": "a.md",
        "n_trials": n_trials or len(trials), "trials": trials,
    }])
    return path


def test_spread_includes_dropped_trials_and_excludes_insurance(tmp_path):
    path = _registry(tmp_path / "r.json", [
        _trial("kept", 0.6), _trial("dropped", -0.6), _trial("hedge", -0.6, "insurance"),
    ])
    got = spread_sharpes({}, periods_per_year=252.0, path=path)
    assert sorted(round(s * _ROOT, 6) for s in got) == [-0.6, 0.6]


def test_an_in_run_sharpe_replaces_the_recorded_one(tmp_path):
    path = _registry(tmp_path / "r.json", [_trial("kept", 0.6), _trial("cand", None)])
    got = spread_sharpes({"kept": 0.05, "cand": 0.04}, periods_per_year=252.0, path=path)
    assert got == [0.05, 0.04]


def test_an_unregistered_candidate_is_refused(tmp_path):
    # The registry is where its objective is declared; scoring first and
    # declaring after is how an insurance label gets chosen for convenience.
    path = _registry(tmp_path / "r.json", [_trial("kept", 0.6)])
    with pytest.raises(ValueError, match="not in the trial registry"):
        spread_sharpes({"new": 0.04}, periods_per_year=252.0, path=path)


def test_an_unscored_trial_outside_the_run_is_refused(tmp_path):
    path = _registry(tmp_path / "r.json", [_trial("kept", 0.6), _trial("cand", None)])
    with pytest.raises(ValueError, match="no Sharpe"):
        spread_sharpes({}, periods_per_year=252.0, path=path)


def test_unknown_objective_and_duplicate_names_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="objective"):
        load_trial_registry(_registry(tmp_path / "a.json", [_trial("x", 0.1, "hedge")]))
    with pytest.raises(ValueError, match="recorded twice"):
        load_trial_registry(_registry(tmp_path / "b.json", [_trial("x", 0.1), _trial("x", 0.2)]))


def test_more_recorded_trials_than_declared_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="declares only"):
        load_trial_registry(_registry(tmp_path / "r.json", [_trial("a", 0.1), _trial("b", 0.2)], 1))


def test_committed_registry_reproduces_the_d22_figure():
    """docs/decisions/sr-star-spread-2026-10.md: 12 return-objective trials on
    record plus a 0.64 candidate, n = 14, gives SR* 0.649 annualised."""
    sample = spread_sharpes({}, periods_per_year=252.0) + [0.64 / _ROOT]
    assert len(sample) == 13
    assert expected_max_sharpe(sample, 14) * _ROOT == pytest.approx(0.649, abs=0.001)


def test_control_uses_the_supplied_spread(tmp_path):
    row = {"sr": 0.04, "n": 2500, "skew": 0.0, "kurt": 3.0, "ic_p": 0.01}
    candidates = {"a": dict(row), "b": dict(row, sr=0.03)}
    default = control(candidates, n_trials=10)
    wide = control(candidates, n_trials=10, spread_srs=[0.04, 0.03, -0.04, -0.03])
    assert wide["a"].deflated_sharpe < default["a"].deflated_sharpe
