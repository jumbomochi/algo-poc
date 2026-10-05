"""KAN-103: which US session an equity snapshot valued.

Instants are written in SGT (UTC+8, no DST) where the case is "the 04:15 SGT
run", because that is how the runs are scheduled and logged.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from shared import session_dating
from shared.session_dating import (
    infer_session,
    last_closed_session,
    last_session_before,
    session_in_progress,
    stamp_session,
)

SGT = timezone(timedelta(hours=8))


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def warning(self, event: str, **kwargs) -> None:
        self.events.append((event, kwargs))

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.events]


@pytest.fixture
def log(monkeypatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(session_dating, "logger", recorder)
    return recorder


def sgt(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=SGT)


# ---------------------------------------------------------------- stamp_session


def test_a_tuesday_0415_sgt_run_stamps_monday(log):
    assert stamp_session(date(2026, 9, 28), sgt(2026, 9, 29, 4, 15)) == date(
        2026, 9, 28
    )
    assert log.events == []


def test_the_tuesday_after_labor_day_stamps_friday(log):
    # Tue 09-08 06:27 SGT run, valuation_at 09-07 18:20 ET; Labor Day had no bar.
    valuation = datetime(2026, 9, 7, 22, 20, tzinfo=timezone.utc)
    assert stamp_session(date(2026, 9, 4), valuation) == date(2026, 9, 4)
    assert log.events == []


def test_a_saturday_run_stamps_friday(log):
    assert stamp_session(date(2026, 10, 2), sgt(2026, 10, 3, 4, 15)) == date(
        2026, 10, 2
    )


def test_the_sunday_catch_up_stamps_friday(log):
    assert stamp_session(date(2026, 8, 7), sgt(2026, 8, 9, 22, 30)) == date(
        2026, 8, 7
    )
    assert log.events == []


def test_the_0843_sgt_catch_up_stamps_the_previous_session(log):
    # 10-02 08:43 SGT is 10-01 20:43 ET, after Thursday's close.
    assert stamp_session(date(2026, 10, 1), sgt(2026, 10, 2, 8, 43)) == date(
        2026, 10, 1
    )
    assert log.events == []


def test_a_half_day_valued_after_its_early_close_stamps_that_session(log):
    # 2026-11-27 closes 13:00 ET (18:00 UTC). 13:30 ET is after the bell.
    after = datetime(2026, 11, 27, 18, 30, tzinfo=timezone.utc)
    assert stamp_session(date(2026, 11, 27), after) == date(2026, 11, 27)
    # 12:30 ET is still the session.
    before = datetime(2026, 11, 27, 17, 30, tzinfo=timezone.utc)
    assert stamp_session(date(2026, 11, 27), before) is None


def test_the_first_winter_0415_sgt_run_stamps_none_for_its_partial_bar(log):
    # NYSE closes 16:00 EST = 05:00 SGT from 2026-11-02; 04:15 SGT is 45 min early.
    assert stamp_session(date(2026, 11, 2), sgt(2026, 11, 3, 4, 15)) is None
    assert log.names == ["session_stamp_partial_bar"]


def test_a_stale_bar_stamps_the_bar_session_and_logs_it(log):
    # Wed 04:15 SGT: Tuesday 09-29 has closed but IB returned Monday's bar.
    assert stamp_session(date(2026, 9, 28), sgt(2026, 9, 30, 4, 15)) == date(
        2026, 9, 28
    )
    assert log.names == ["session_stamp_stale_bars"]
    assert log.events[0][1]["last_closed_session"] == "2026-09-29"


def test_a_naive_valuation_instant_is_utc(log):
    naive = datetime(2026, 9, 28, 20, 15)
    assert stamp_session(date(2026, 9, 28), naive) == date(2026, 9, 28)


def test_a_bar_dated_on_a_non_session_stamps_none(log):
    assert stamp_session(date(2026, 9, 7), sgt(2026, 9, 8, 6, 27)) is None
    assert log.names == ["session_stamp_not_a_session"]


def test_no_bars_stamps_none(log):
    assert stamp_session(None, sgt(2026, 9, 29, 4, 15)) is None


def test_a_calendar_failure_stamps_none_instead_of_raising(log):
    assert stamp_session(date(1900, 1, 2), sgt(1900, 1, 3, 4, 15)) is None
    assert log.names == ["session_stamp_failed"]


# ---------------------------------------------------------------- helpers


def test_last_closed_session_honours_the_close_instant():
    # 16:00 EDT on 09-28 is 20:00 UTC.
    assert last_closed_session(
        datetime(2026, 9, 28, 19, 59, tzinfo=timezone.utc)
    ) == date(2026, 9, 25)
    assert last_closed_session(
        datetime(2026, 9, 28, 20, 0, tzinfo=timezone.utc)
    ) == date(2026, 9, 28)


def test_last_session_before_skips_weekends_and_holidays():
    assert last_session_before(date(2026, 9, 8)) == date(2026, 9, 4)
    assert last_session_before(date(2026, 9, 29)) == date(2026, 9, 28)
    assert last_session_before(date(2026, 8, 9)) == date(2026, 8, 7)


def test_session_in_progress_is_rth_only():
    assert session_in_progress(sgt(2026, 9, 29, 22, 0)) == date(2026, 9, 29)
    assert session_in_progress(sgt(2026, 9, 29, 4, 15)) is None
    assert session_in_progress(sgt(2026, 9, 8, 0, 30)) is None  # Labor Day
    # Winter: 04:15 SGT is 15:15 EST, inside the 11-02 session.
    assert session_in_progress(sgt(2026, 11, 3, 4, 15)) == date(2026, 11, 2)


# ---------------------------------------------------------------- infer_session


def test_infer_prefers_valuation_at():
    valuation = datetime(2026, 9, 7, 22, 20, tzinfo=timezone.utc)
    assert infer_session(date(2026, 9, 8), valuation) == date(2026, 9, 4)


def test_infer_falls_back_to_the_session_before_the_run_date():
    assert infer_session(date(2026, 8, 4), None) == date(2026, 8, 3)
    assert infer_session(date(2026, 8, 8), None) == date(2026, 8, 7)


@pytest.mark.parametrize(
    "run_date, valuation_at",
    [
        (date(2026, 9, 29), sgt(2026, 9, 29, 4, 15)),
        (date(2026, 9, 8), sgt(2026, 9, 8, 6, 27)),
        (date(2026, 10, 2), sgt(2026, 10, 2, 8, 43)),
        (date(2026, 9, 4), sgt(2026, 9, 4, 17, 42)),
        (date(2026, 8, 9), sgt(2026, 8, 9, 22, 30)),
    ],
)
def test_the_two_backfill_rules_agree_on_the_recorded_run_shapes(
    run_date, valuation_at
):
    assert infer_session(run_date, valuation_at) == infer_session(run_date, None)
