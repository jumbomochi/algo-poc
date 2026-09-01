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
from scripts.divergence_monitor import (
    load_shadow_equity_series,
    shadow_baseline_id,
)


SESSIONS = [date(2026, 8, 3) + timedelta(days=i) for i in range(4)]


def _write(tmp_path, series, shadow_id="shadow:abc123", window=30):
    tmp_path.mkdir(parents=True, exist_ok=True)
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


# ---------------------------------------------------------------------------
# baseline identity — the streak depends on this NOT being the filename
# ---------------------------------------------------------------------------


def test_the_baseline_id_is_the_model_id_not_the_filename(tmp_path) -> None:
    """``baseline_id_for`` returns a basename, which is right for a pinned
    artifact and catastrophic for a shadow: shadow_YYYYMMDD.json changes every
    night, so every session would land under its own baseline, breach_streak
    would treat each one as unrelated history, and no streak could ever reach
    the 10-session trigger — the exact failure the frozen pin already caused.

    The shadow's identity is its model fingerprint, which is stable night to
    night and moves only when the model does.
    """
    path = _write(tmp_path, {"momentum": {d: 1.0 for d in SESSIONS}},
                  shadow_id="shadow:deadbeefdeadbeef")

    assert shadow_baseline_id(path) == "shadow:deadbeefdeadbeef"


def test_two_nights_of_the_same_model_share_a_baseline_id(tmp_path) -> None:
    """The property the streak actually needs."""
    monday = _write(tmp_path / "mon", {"momentum": {d: 1.0 for d in SESSIONS}},
                    shadow_id="shadow:deadbeefdeadbeef")
    tuesday = _write(tmp_path / "tue", {"momentum": {d: 1.0 for d in SESSIONS}},
                     shadow_id="shadow:deadbeefdeadbeef")

    assert shadow_baseline_id(monday) == shadow_baseline_id(tuesday)
