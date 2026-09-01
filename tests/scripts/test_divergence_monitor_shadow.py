"""The monitor reading the rolling shadow instead of the frozen pin.

``load_backtest_equity_series`` parses a 261 MB artifact whose last bar caps the
comparison window. ``load_shadow_equity_series`` reads the ~100 KB file the
04:15 run wrote, whose last session is today.

The aggregate is derived here rather than read, because direction-doc D15 says
it is a roll-up and the digest recomputes it. It is summed only over sessions
present in **every** scored sleeve, matching ``load_live_aggregate_series`` on
the live side — a sum that silently drops a sleeve on the days it is missing
would step down and read as a loss the book never took.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from backtest.shadow_artifact import dump_shadow
from scripts.divergence_monitor import load_shadow_equity_series


SESSIONS = [date(2026, 8, 3) + timedelta(days=i) for i in range(4)]


def _write(tmp_path, series, shadow_id="shadow:abc123", window=30):
    path = tmp_path / "shadow_20260806.json"
    dump_shadow(
        path, series=series, shadow_id=shadow_id,
        window_sessions=window, session_date=SESSIONS[-1],
    )
    return path


def test_per_sleeve_series_round_trip(tmp_path) -> None:
    path = _write(tmp_path, {
        "momentum": {d: 20_000.0 + i for i, d in enumerate(SESSIONS)},
    })

    per_sleeve, _ = load_shadow_equity_series(path)

    assert per_sleeve["momentum"][SESSIONS[0]] == pytest.approx(20_000.0)
    assert per_sleeve["momentum"][SESSIONS[3]] == pytest.approx(20_003.0)


def test_the_aggregate_is_the_sum_across_sleeves(tmp_path) -> None:
    path = _write(tmp_path, {
        "momentum": {d: 20_000.0 for d in SESSIONS},
        "sector_rotation": {d: 15_000.0 for d in SESSIONS},
    })

    _, aggregate = load_shadow_equity_series(path)

    assert aggregate[SESSIONS[0]] == pytest.approx(35_000.0)


def test_the_aggregate_only_covers_sessions_every_sleeve_has(tmp_path) -> None:
    """A sum that drops a sleeve on its missing days steps down and reads as a
    loss the book never took."""
    path = _write(tmp_path, {
        "momentum": {d: 20_000.0 for d in SESSIONS},
        "sector_rotation": {d: 15_000.0 for d in SESSIONS[:2]},  # short
    })

    _, aggregate = load_shadow_equity_series(path)

    assert set(aggregate) == set(SESSIONS[:2])


def test_no_aggregate_when_there_are_no_sleeves(tmp_path) -> None:
    path = _write(tmp_path, {})

    per_sleeve, aggregate = load_shadow_equity_series(path)

    assert per_sleeve == {}
    assert aggregate == {}


def test_a_missing_shadow_raises(tmp_path) -> None:
    """Absence means the 04:15 job did not run — the blind signal. It must not
    read as an empty book."""
    with pytest.raises(FileNotFoundError):
        load_shadow_equity_series(tmp_path / "absent.json")


def test_synthetic_portfolios_are_excluded_from_the_aggregate(tmp_path) -> None:
    """Same contract as the live side: a drill's book is not the graded book."""
    path = _write(tmp_path, {
        "momentum": {d: 20_000.0 for d in SESSIONS},
        "__drill__": {d: 99_000.0 for d in SESSIONS},
    })

    per_sleeve, aggregate = load_shadow_equity_series(path)

    assert "__drill__" not in per_sleeve
    assert aggregate[SESSIONS[0]] == pytest.approx(20_000.0)
