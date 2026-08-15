# tests/research/evaluation/test_trial_registry.py
from __future__ import annotations

import json

import pytest

from research.evaluation.trial_registry import declared_trial_count, load_trial_registry


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
