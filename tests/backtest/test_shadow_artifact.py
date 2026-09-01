"""The shadow artifact: what 04:15 writes and 04:45 reads.

The 04:15 job holds the fetched bars, the sleeve configs and the risk engines,
so it produces the shadow for ~0.06s of work on data it already has. It writes
the result; the monitor reads it. A second IB fetch at 04:45 was rejected
deliberately — the gateway is the dependency that has already killed scheduled
runs twice.

The load-bearing field is ``shadow_id``. ``shared.evidence_store.breach_streak``
keys on ``(sleeve, session_date, baseline_id)`` and treats rows under any other
baseline as invisible history. If the shadow's identity changed every night,
every session would land under a fresh id, no streak could ever reach two, and
the monitor would be exactly as useless as the frozen pin it replaces — just
noisier. So ``shadow_id`` identifies the **model**, not the run: same sleeve
roster and same parameters means the same id tomorrow, and a parameter change
is what starts a new one.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from backtest.shadow_artifact import (
    dump_shadow,
    load_shadow,
    shadow_id_for,
)


SESSIONS = [date(2026, 8, 3) + timedelta(days=i) for i in range(5)]

SERIES = {
    "momentum": {d: 20_000.0 + i for i, d in enumerate(SESSIONS)},
    "sector_rotation": {d: 15_000.0 + i for i, d in enumerate(SESSIONS)},
}


class _Sleeve:
    def __init__(self, name, params):
        self.name = name
        self.shadow_params = params


def _roster(**overrides):
    base = {
        "momentum": _Sleeve("momentum", {"top_n": 5, "lookback_days": 126}),
        "sector_rotation": _Sleeve("sector_rotation", {"top_n": 3}),
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# shadow_id — the streak depends on this being stable
# ---------------------------------------------------------------------------


def test_the_same_model_yields_the_same_id_on_a_different_day() -> None:
    """If this drifts nightly, breach_streak can never reach 2."""
    assert shadow_id_for(_roster()) == shadow_id_for(_roster())


def test_changing_a_sleeve_parameter_changes_the_id() -> None:
    """A different model is different evidence; D13 restarts the epoch on a
    baseline change, and that can only fire if the id actually moves."""
    changed = _roster(
        momentum=_Sleeve("momentum", {"top_n": 8, "lookback_days": 126})
    )

    assert shadow_id_for(_roster()) != shadow_id_for(changed)


def test_adding_a_sleeve_changes_the_id() -> None:
    extra = _roster(earnings_drift=_Sleeve("earnings_drift", {"top_n": 15}))

    assert shadow_id_for(_roster()) != shadow_id_for(extra)


def test_roster_ordering_does_not_change_the_id() -> None:
    """Dict ordering is not a model change."""
    forward = _roster()
    reversed_ = dict(reversed(list(forward.items())))

    assert shadow_id_for(forward) == shadow_id_for(reversed_)


def test_a_sleeve_without_shadow_params_is_refused_not_defaulted() -> None:
    """Defaulting to {} would make the id track the roster and nothing else, so
    editing top_n from 5 to 8 would leave the id — and therefore the epoch and
    the breach streak — untouched. D13 requires a model change to restart the
    epoch, and that can only hold if a missing fingerprint is loud."""

    class _Unfingerprinted:
        name = "momentum"

    with pytest.raises(ValueError, match="shadow_params"):
        shadow_id_for({"momentum": _Unfingerprinted()})


def test_the_id_is_prefixed_so_a_reader_knows_what_it_is() -> None:
    """Evidence rows carry this next to pinned-artifact ids; they must not be
    mistakable for one another."""
    assert shadow_id_for(_roster()).startswith("shadow:")


# ---------------------------------------------------------------------------
# round trip
# ---------------------------------------------------------------------------


def test_the_series_round_trips(tmp_path) -> None:
    path = tmp_path / "shadow_20260807.json"
    dump_shadow(path, series=SERIES, shadow_id="shadow:abc123", window_sessions=30)

    loaded = load_shadow(path)

    assert loaded.series == SERIES


def test_the_artifact_carries_its_identity_and_window(tmp_path) -> None:
    path = tmp_path / "shadow_20260807.json"
    dump_shadow(path, series=SERIES, shadow_id="shadow:abc123", window_sessions=30)

    loaded = load_shadow(path)

    assert loaded.shadow_id == "shadow:abc123"
    assert loaded.window_sessions == 30


def test_dates_are_written_as_iso_strings(tmp_path) -> None:
    """The artifact is read by tools other than this one."""
    path = tmp_path / "shadow_20260807.json"
    dump_shadow(path, series=SERIES, shadow_id="shadow:abc123", window_sessions=30)

    raw = json.loads(path.read_text())

    assert "2026-08-03" in raw["series"]["momentum"]


def test_an_artifact_with_no_sleeves_round_trips_as_empty(tmp_path) -> None:
    """Every sleeve ungradeable is a real state, distinct from a missing file."""
    path = tmp_path / "shadow_20260807.json"
    dump_shadow(path, series={}, shadow_id="shadow:abc123", window_sessions=30)

    assert load_shadow(path).series == {}


def test_a_missing_artifact_raises_rather_than_reading_as_empty(tmp_path) -> None:
    """A shadow that was never written means the 04:15 job did not run. That is
    the blind signal, and it must not be laundered into 'no sleeves'."""
    with pytest.raises(FileNotFoundError):
        load_shadow(tmp_path / "absent.json")
