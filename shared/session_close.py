"""Has the NYSE session we are about to price actually closed? — KAN-104.

The launchd schedule is fixed in SGT, which has no daylight saving time. The
NYSE close is fixed in ET, which does. So the same SGT slot sits a different
distance from the close in each half of the year:

=========================  ==================  =================
US clock                   NYSE close in SGT   old 04:15 slot
=========================  ==================  =================
EDT (Mar -> Nov)           04:00               15 min after
EST (Nov -> Mar)           05:00               45 min BEFORE
=========================  ==================  =================

From Tue 2026-11-03 to Sat 2027-03-13 the old 04:15 run would have fetched the
in-progress daily bar and priced signals, marks, stops and orders on a session
45 minutes short of its close — every run, for about nineteen weeks. Nothing
checked. The schedule moved to 05:15 SGT year-round, which is after the close
in both seasons; this module is the guard that makes the schedule a
convenience rather than the only defence, because a catch-up run started by
hand, a host that wakes late and runs a missed job, or the next person to move
a plist can all put a run back inside a session.

Two questions, answered against ``shared.market_calendar`` (exchange_calendars
underneath, so holidays and early closes are the exchange's, not ours):

* :func:`session_in_progress` — is a session open *right now*? Asked before a
  run touches the broker or the book.
* :func:`unclosed_session` — had a given session closed at a given instant?
  Asked of the newest bar a fetch returned, against the instant the fetch
  STARTED (a fetch that begins at 15:58 ET and ends at 16:06 holds partial bars
  for its first tickers, so its end time proves nothing).

Every instant must be timezone-aware. A naive datetime is refused rather than
assumed to be UTC or local, because "which clock is this?" is the whole defect.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from shared.market_calendar import ET, MarketCalendar

#: How long after the official close a daily bar is trusted to be final. The
#: closing auction prints at 16:00 ET and IB's daily bar picks up the official
#: close shortly after; five minutes is margin for that, not a schedule. The
#: 05:15 SGT slot clears it by ten minutes in EST and seventy in EDT.
CLOSE_SETTLE = timedelta(minutes=5)


@dataclass(frozen=True)
class UnclosedSession:
    """A session that had not closed (plus the settle margin) at ``now``."""

    session: date
    #: The session's real close in ET — 13:00 on a half day.
    closes_at: datetime
    #: The instant that was checked, in ET.
    now: datetime
    settle: timedelta = CLOSE_SETTLE
    #: True when ``session`` is not a trading day at all and lies after
    #: ``now``'s ET date — only a bar from the future could claim it.
    from_the_future: bool = False

    @property
    def safe_after(self) -> datetime:
        return self.closes_at + self.settle

    def describe(self) -> str:
        if self.from_the_future:
            return (
                f"a bar is dated {self.session}, which is not an NYSE session "
                f"and is after today's ET date ({self.now:%Y-%m-%d %H:%M %Z}). "
                f"Refusing to price data that cannot be from a closed session "
                f"(KAN-104)."
            )
        minutes = max(0, int((self.safe_after - self.now).total_seconds() // 60))
        return (
            f"the NYSE session of {self.session} closes at "
            f"{self.closes_at:%H:%M %Z} ({self.closes_at:%Y-%m-%d}), and it was "
            f"{self.now:%Y-%m-%d %H:%M %Z} — {minutes} min short of the close "
            f"plus its {int(self.settle.total_seconds() // 60)}-minute settle "
            f"margin. Its daily bar is still forming, so pricing it would "
            f"trade on a partial session (KAN-104)."
        )


def _require_aware(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError(
            f"naive datetime {now!r}: the close guard needs a timezone-aware "
            "instant — a naive clock is exactly the DST defect it guards against"
        )
    return now.astimezone(ET)


def unclosed_session(
    session: date,
    now: datetime,
    *,
    calendar: MarketCalendar | None = None,
    settle: timedelta = CLOSE_SETTLE,
) -> UnclosedSession | None:
    """``session`` had not closed by ``now``, or None if it had.

    A date that is not a session (weekend, holiday) has nothing to close, so it
    is None — unless it lies after ``now``'s own ET date, which only a bar from
    the future could claim and which is therefore refused as unclosed.
    """
    now_et = _require_aware(now)
    calendar = calendar or MarketCalendar()
    bounds = calendar.session_bounds(session)
    if bounds is None:
        if session > now_et.date():
            # No close to name; midnight ET at the start of the next day is the
            # earliest instant that date can be over.
            end_of_day = datetime.combine(
                session + timedelta(days=1), datetime.min.time(), tzinfo=ET
            )
            return UnclosedSession(
                session, end_of_day, now_et, timedelta(0), from_the_future=True
            )
        return None
    _, close = bounds
    if now_et < close + settle:
        return UnclosedSession(session, close, now_et, settle)
    return None


def session_in_progress(
    now: datetime,
    *,
    calendar: MarketCalendar | None = None,
    settle: timedelta = CLOSE_SETTLE,
) -> UnclosedSession | None:
    """The session that has opened but not yet closed at ``now``, or None.

    Before the open there is nothing in progress — the newest bar IB can return
    is the previous, complete, session — so a pre-open run passes here and is
    caught, if its fetch runs into the open, by :func:`unclosed_session` on the
    bars themselves.
    """
    now_et = _require_aware(now)
    calendar = calendar or MarketCalendar()
    today = now_et.date()
    bounds = calendar.session_bounds(today)
    if bounds is None:
        return None
    open_, _ = bounds
    if now_et < open_:
        return None
    return unclosed_session(today, now_et, calendar=calendar, settle=settle)


def newest_bar_session(bars_by_ticker: dict[str, list[dict]]) -> date | None:
    """The newest session any ticker's bars claim to cover, or None if empty.

    The newest across the universe, not per ticker: one partial bar is enough
    to make the run's marks and signals wrong for that ticker.
    """
    newest: date | None = None
    for bars in bars_by_ticker.values():
        for bar in bars or ():
            d = _as_date(bar["date"])
            if newest is None or d > newest:
                newest = d
    return newest


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])
