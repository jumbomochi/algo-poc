"""The NYSE close guard, driven with faked clocks on both sides of DST — KAN-104.

The launchd slots are SGT, which has no DST; the close is 16:00 ET, which does.
So the instants below are written the way the scheduler sees them — SGT wall
clock — and the guard is asked whether the session the run would price has
closed. The headline cases are the two edges of the EST window the old 04:15
slot fell into: Tue 2026-11-03 (first run after the fall-back) and Sat
2027-03-13 (last run before the spring-forward).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from shared.market_calendar import ET, MarketCalendar
from shared.session_close import (
    CLOSE_SETTLE,
    newest_bar_session,
    session_in_progress,
    unclosed_session,
)

SGT = ZoneInfo("Asia/Singapore")
CAL = MarketCalendar()


def sgt(y, m, d, hh, mm) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=SGT)


# ---------------------------------------------------------------------------
# The old 04:15 slot against the new 05:15 slot, in each season
# ---------------------------------------------------------------------------

def test_the_old_slot_is_inside_the_session_on_the_first_est_run():
    """Tue 2026-11-03 04:15 SGT is Mon 11-02 15:15 EST: 45 min before the close.
    This is the run the ticket exists for."""
    problem = session_in_progress(sgt(2026, 11, 3, 4, 15), calendar=CAL)
    assert problem is not None
    assert problem.session == date(2026, 11, 2)
    assert problem.closes_at == datetime(2026, 11, 2, 16, 0, tzinfo=ET)
    assert "2026-11-02" in problem.describe()
    assert "KAN-104" in problem.describe()


def test_the_new_slot_is_after_the_close_on_the_first_est_run():
    assert session_in_progress(sgt(2026, 11, 3, 5, 15), calendar=CAL) is None


def test_the_old_slot_is_inside_the_session_on_the_last_est_run():
    """Sat 2027-03-13 04:15 SGT is Fri 03-12 15:15 EST."""
    problem = session_in_progress(sgt(2027, 3, 13, 4, 15), calendar=CAL)
    assert problem is not None
    assert problem.session == date(2027, 3, 12)


def test_the_new_slot_is_after_the_close_on_the_last_est_run():
    assert session_in_progress(sgt(2027, 3, 13, 5, 15), calendar=CAL) is None


@pytest.mark.parametrize(
    "instant",
    [
        sgt(2026, 10, 6, 4, 15),   # Mon 10-05 16:15 EDT — the summer the slot was right
        sgt(2026, 10, 6, 5, 15),   # Mon 10-05 17:15 EDT — the new slot in summer
        sgt(2027, 3, 16, 4, 15),   # Mon 03-15 16:15 EDT — back to summer time
        sgt(2027, 3, 16, 5, 15),
    ],
)
def test_edt_runs_are_after_the_close(instant):
    assert session_in_progress(instant, calendar=CAL) is None


def test_the_fall_back_weekend_itself():
    """Sat 2026-10-31 covers Fri 10-30 under EDT (close 04:00 SGT): the old slot
    was fine that morning. The change bites from the first EST session."""
    assert session_in_progress(sgt(2026, 10, 31, 4, 15), calendar=CAL) is None
    assert session_in_progress(sgt(2026, 11, 3, 4, 15), calendar=CAL) is not None


# ---------------------------------------------------------------------------
# The settle margin
# ---------------------------------------------------------------------------

def test_the_minutes_just_after_the_close_are_still_refused():
    """05:02 SGT in EST is 16:02 ET — closed, but inside the settle margin
    while the closing auction's print lands in the daily bar."""
    problem = session_in_progress(sgt(2026, 11, 3, 5, 2), calendar=CAL)
    assert problem is not None
    assert problem.safe_after == datetime(2026, 11, 2, 16, 0, tzinfo=ET) + CLOSE_SETTLE


def test_the_settle_margin_is_inclusive_at_its_end():
    at = datetime(2026, 11, 2, 16, 0, tzinfo=ET) + CLOSE_SETTLE
    assert session_in_progress(at, calendar=CAL) is None
    assert session_in_progress(at - timedelta(seconds=1), calendar=CAL) is not None


# ---------------------------------------------------------------------------
# Half days, holidays, weekends, pre-open
# ---------------------------------------------------------------------------

def test_a_half_day_closes_at_13_00_not_16_00():
    """Fri 2026-11-27 (after Thanksgiving) closes at 13:00 ET. A run at 13:30 ET
    is after the close; a guard that assumed 16:00 would refuse it."""
    after = datetime(2026, 11, 27, 13, 30, tzinfo=ET)
    assert session_in_progress(after, calendar=CAL) is None
    assert unclosed_session(date(2026, 11, 27), after, calendar=CAL) is None


def test_a_half_day_is_refused_before_its_early_close():
    during = datetime(2026, 11, 27, 12, 30, tzinfo=ET)
    problem = session_in_progress(during, calendar=CAL)
    assert problem is not None
    assert problem.closes_at == datetime(2026, 11, 27, 13, 0, tzinfo=ET)


def test_christmas_eve_is_a_half_day_too():
    assert session_in_progress(
        datetime(2026, 12, 24, 13, 10, tzinfo=ET), calendar=CAL
    ) is None


def test_a_holiday_has_no_session_in_progress():
    """Thanksgiving 2026-11-26: no session, nothing to wait for."""
    assert session_in_progress(datetime(2026, 11, 26, 12, 0, tzinfo=ET), calendar=CAL) is None


def test_a_weekend_has_no_session_in_progress():
    assert session_in_progress(datetime(2026, 11, 7, 12, 0, tzinfo=ET), calendar=CAL) is None


def test_before_the_open_nothing_is_in_progress():
    """09:00 ET: the newest bar IB can serve is yesterday's complete session."""
    assert session_in_progress(datetime(2026, 11, 2, 9, 0, tzinfo=ET), calendar=CAL) is None


# ---------------------------------------------------------------------------
# Asking the bars: had the session they cover closed when the fetch started?
# ---------------------------------------------------------------------------

def test_a_bar_for_today_fetched_before_the_close_is_refused():
    """A run that started pre-open (09:26 ET) and fetched into the session: the
    clock check passed, but today's forming bar came back."""
    started = datetime(2026, 11, 2, 9, 26, tzinfo=ET)
    problem = unclosed_session(date(2026, 11, 2), started, calendar=CAL)
    assert problem is not None
    assert problem.session == date(2026, 11, 2)


def test_yesterdays_bar_fetched_pre_open_is_fine():
    started = datetime(2026, 11, 3, 9, 26, tzinfo=ET)
    assert unclosed_session(date(2026, 11, 2), started, calendar=CAL) is None


def test_the_normal_post_close_run_prices_the_session_that_just_closed():
    """Tue 2026-11-03 05:15 SGT, bars through Mon 11-02: the everyday case."""
    assert unclosed_session(date(2026, 11, 2), sgt(2026, 11, 3, 5, 15), calendar=CAL) is None


def test_a_bar_dated_after_today_on_a_non_session_is_refused():
    """Saturday-dated bar seen on a Friday: cannot be from a closed session."""
    problem = unclosed_session(
        date(2026, 11, 7), datetime(2026, 11, 6, 17, 0, tzinfo=ET), calendar=CAL
    )
    assert problem is not None
    assert problem.from_the_future
    assert "not an NYSE session" in problem.describe()


def test_a_past_non_session_date_has_nothing_to_close():
    assert unclosed_session(
        date(2026, 11, 26), datetime(2026, 11, 27, 17, 0, tzinfo=ET), calendar=CAL
    ) is None


# ---------------------------------------------------------------------------
# Clocks
# ---------------------------------------------------------------------------

def test_a_naive_clock_is_refused_not_guessed():
    """'Which clock is this?' is the defect. Assuming UTC or local would
    silently re-introduce it."""
    with pytest.raises(ValueError, match="naive"):
        session_in_progress(datetime(2026, 11, 2, 15, 15))
    with pytest.raises(ValueError, match="naive"):
        unclosed_session(date(2026, 11, 2), datetime(2026, 11, 2, 15, 15))


def test_the_same_instant_in_any_zone_gives_the_same_answer():
    instant = sgt(2026, 11, 3, 4, 15)
    assert session_in_progress(instant.astimezone(ZoneInfo("UTC")), calendar=CAL) is not None
    assert session_in_progress(instant.astimezone(ET), calendar=CAL) is not None


def test_session_bounds_reports_the_real_close():
    open_, close = CAL.session_bounds(date(2026, 11, 27))
    assert open_ == datetime(2026, 11, 27, 9, 30, tzinfo=ET)
    assert close == datetime(2026, 11, 27, 13, 0, tzinfo=ET)
    assert CAL.session_bounds(date(2026, 11, 26)) is None


# ---------------------------------------------------------------------------
# newest_bar_session
# ---------------------------------------------------------------------------

def test_newest_bar_session_takes_the_newest_across_the_universe():
    bars = {
        "AAA": [{"date": date(2026, 10, 30)}, {"date": date(2026, 11, 2)}],
        "BBB": [{"date": "2026-10-30"}],
        "CCC": [],
    }
    assert newest_bar_session(bars) == date(2026, 11, 2)


def test_newest_bar_session_reads_datetimes_and_strings():
    bars = {"AAA": [{"date": datetime(2026, 11, 2, 0, 0)}], "BBB": [{"date": "2026-11-03"}]}
    assert newest_bar_session(bars) == date(2026, 11, 3)


def test_newest_bar_session_of_nothing_is_none():
    assert newest_bar_session({}) is None
    assert newest_bar_session({"AAA": []}) is None
