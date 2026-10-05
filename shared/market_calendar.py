# shared/market_calendar.py
from __future__ import annotations

from datetime import datetime, date
from zoneinfo import ZoneInfo

import pandas as pd
import exchange_calendars as xcals

ET = ZoneInfo("America/New_York")


class MarketCalendar:
    def __init__(self, exchange: str = "XNYS"):
        self._cal = xcals.get_calendar(exchange)

    def is_market_open(self, dt: datetime) -> bool:
        if not self._is_session(dt):
            return False
        ts = pd.Timestamp(dt.astimezone(ET)).tz_convert("UTC")
        return self._cal.is_open_on_minute(ts)

    def _is_session(self, dt: datetime) -> bool:
        d = dt.astimezone(ET).date()
        return self._cal.is_session(d)

    def is_trading_day(self, d: date) -> bool:
        return self._cal.is_session(d)

    def get_last_session_close(self, dt: datetime) -> datetime:
        d = dt.astimezone(ET).date()
        prev = self._cal.previous_close(d)
        return prev.to_pydatetime().astimezone(ET)

    def get_next_market_close(self, dt: datetime) -> datetime:
        d = dt.astimezone(ET).date()
        # date_to_session(direction="next") returns d itself when d is
        # already a session, otherwise the next session after d — this
        # also handles weekends/holidays, which next_session() rejects.
        session = self._cal.date_to_session(d, direction="next")
        close = self._cal.session_close(session)
        return close.to_pydatetime().astimezone(ET)

    def session_bounds(self, d: date) -> tuple[datetime, datetime] | None:
        """``(open, close)`` of session ``d`` in ET, or None if ``d`` is not a
        session. The close is the real one, so a half day (the day after
        Thanksgiving, Christmas Eve) closes at 13:00 ET, not 16:00."""
        if not self._cal.is_session(d):
            return None
        return (
            self._cal.session_open(d).to_pydatetime().astimezone(ET),
            self._cal.session_close(d).to_pydatetime().astimezone(ET),
        )

    def trading_sessions(self, start: date, end: date) -> list[date]:
        """Trading session dates in [start, end], ascending. Used to build a
        zero-filled baseline (sentiment/aggregate.py) that includes quiet
        sessions the raw archive has no row for."""
        return [ts.date() for ts in self._cal.sessions_in_range(start, end)]
