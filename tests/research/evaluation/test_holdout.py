# tests/research/evaluation/test_holdout.py
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from research.evaluation.folds import nested_walk_forward
from research.evaluation.holdout import (
    HoldoutAlreadyEvaluated,
    HoldoutProtocol,
)

HORIZON = 21
EMBARGO = 21
N_DATES = 400
HOLDOUT_INDEX = 300


def _dates(n: int = N_DATES) -> list[date]:
    start = date(2024, 1, 1)
    return [start + timedelta(days=i) for i in range(n)]


def _registered(tmp_path, *, horizon=HORIZON, embargo=EMBARGO, index=HOLDOUT_INDEX):
    path = tmp_path / "holdout_registry.json"
    path.write_text(json.dumps({"version": 1, "splits": [], "evaluations": []}))
    protocol = HoldoutProtocol.load(path)
    protocol.register(
        split_id="s",
        holdout_start=_dates()[index].isoformat(),
        horizon=horizon,
        embargo=embargo,
        note="fixture",
        registered_at="2026-08-16T00:00:00+00:00",
    )
    return protocol


def test_purge_width_matches_the_walk_forward_gap(tmp_path):
    # The holdout and the nested walk-forward must not drift: both purge
    # horizon + embargo rows between training and evaluation.
    split = _registered(tmp_path).resolve("s", _dates())
    folds = nested_walk_forward(N_DATES, 4, 3, HORIZON, EMBARGO)
    assert split.gap == HORIZON + EMBARGO
    assert split.gap == folds[0].test[0] - folds[0].train[1]


def test_boundary_rows_are_purged_from_training(tmp_path):
    split = _registered(tmp_path).resolve("s", _dates())
    assert split.holdout == (HOLDOUT_INDEX, N_DATES)
    assert split.purge == (HOLDOUT_INDEX - 42, HOLDOUT_INDEX)
    assert split.train == (0, HOLDOUT_INDEX - 42)
    assert set(range(*split.train)).isdisjoint(range(*split.holdout))
    assert set(range(*split.train)).isdisjoint(range(*split.purge))


def test_registration_is_a_date_so_a_growing_panel_cannot_move_it(tmp_path):
    # New bars arrive daily. An index-registered boundary would slide through
    # the data as the panel grows, which is the one thing pre-registration
    # must not do.
    protocol = _registered(tmp_path)
    short = protocol.resolve("s", _dates(N_DATES))
    long = protocol.resolve("s", _dates(N_DATES + 50))
    assert short.train == long.train
    assert short.holdout[0] == long.holdout[0]
    assert long.holdout[1] == N_DATES + 50


def test_second_evaluation_of_the_same_split_raises(tmp_path):
    protocol = _registered(tmp_path)
    protocol.evaluate("s", _dates(), label="incumbent sleeves", evaluated_at="2026-08-16T01:00:00+00:00")
    with pytest.raises(HoldoutAlreadyEvaluated, match="incumbent sleeves"):
        protocol.evaluate("s", _dates(), label="second look", evaluated_at="2026-08-16T02:00:00+00:00")


def test_the_burn_survives_a_reload(tmp_path):
    # A holdout you can re-run by restarting the process is not a holdout.
    protocol = _registered(tmp_path)
    protocol.evaluate("s", _dates(), label="first", evaluated_at="2026-08-16T01:00:00+00:00")
    reloaded = HoldoutProtocol.load(tmp_path / "holdout_registry.json")
    assert reloaded.is_burned("s") is True
    with pytest.raises(HoldoutAlreadyEvaluated):
        reloaded.evaluate("s", _dates(), label="second", evaluated_at="2026-08-16T03:00:00+00:00")


def test_re_registering_a_burned_split_is_refused(tmp_path):
    protocol = _registered(tmp_path)
    protocol.evaluate("s", _dates(), label="first", evaluated_at="2026-08-16T01:00:00+00:00")
    with pytest.raises(HoldoutAlreadyEvaluated):
        protocol.register(
            split_id="s", holdout_start="2024-06-01", horizon=HORIZON, embargo=EMBARGO,
            note="moving the goalposts", registered_at="2026-08-16T04:00:00+00:00",
        )


def test_saving_preserves_the_file_prose(tmp_path):
    # The committed registry carries a top-level note explaining the protocol.
    # Recording a burn must not silently strip it.
    path = tmp_path / "holdout_registry.json"
    path.write_text(json.dumps(
        {"version": 1, "note": "read me first", "splits": [], "evaluations": []}))
    protocol = HoldoutProtocol.load(path)
    protocol.register(
        split_id="s", holdout_start=_dates()[HOLDOUT_INDEX].isoformat(),
        horizon=HORIZON, embargo=EMBARGO, registered_at="2026-08-16T00:00:00+00:00",
    )
    assert json.loads(path.read_text())["note"] == "read me first"


def test_a_burn_recorded_by_another_process_is_honoured(tmp_path):
    # Two holders of the same registry file. The registry is the record of a
    # spent holdout, so a burn must never be lost to last-writer-wins.
    path = tmp_path / "holdout_registry.json"
    first = _registered(tmp_path)
    second = HoldoutProtocol.load(path)
    first.evaluate("s", _dates(), label="first", evaluated_at="2026-08-16T01:00:00+00:00")
    with pytest.raises(HoldoutAlreadyEvaluated):
        second.evaluate("s", _dates(), label="racing", evaluated_at="2026-08-16T01:00:01+00:00")
    assert [row.label for row in HoldoutProtocol.load(path).evaluations("s")] == ["first"]


def test_a_malformed_date_string_is_rejected(tmp_path):
    # resolve() compares ISO dates lexicographically; a non-ISO boundary would
    # silently pick the wrong row rather than fail.
    path = tmp_path / "holdout_registry.json"
    path.write_text(json.dumps({"version": 1, "splits": [], "evaluations": []}))
    protocol = HoldoutProtocol.load(path)
    with pytest.raises(ValueError):
        protocol.register(
            split_id="s", holdout_start="6/1/2026", horizon=HORIZON, embargo=EMBARGO,
            registered_at="2026-08-16T00:00:00+00:00",
        )


def test_unregistered_split_is_rejected(tmp_path):
    with pytest.raises(KeyError, match="unknown"):
        _registered(tmp_path).resolve("nope", _dates())


def test_too_little_history_before_the_boundary_raises(tmp_path):
    protocol = _registered(tmp_path, index=10)
    with pytest.raises(ValueError, match="not enough dates"):
        protocol.resolve("s", _dates())


def test_a_boundary_after_the_last_bar_raises(tmp_path):
    protocol = _registered(tmp_path)
    with pytest.raises(ValueError, match="no dates on or after"):
        protocol.resolve("s", _dates(HOLDOUT_INDEX))


def test_committed_registry_pre_registers_the_incumbent_sleeve_holdout():
    registration = HoldoutProtocol.load().registration("incumbent_sleeves_2026")
    assert registration.holdout_start
    assert registration.registered_at
    assert registration.horizon >= 1 and registration.embargo >= 1
