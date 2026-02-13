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
        return prev.to_pydatetime().replace(tzinfo=ET)

    def get_next_market_close(self, dt: datetime) -> datetime:
        d = dt.astimezone(ET).date()
        if self._cal.is_session(d):
            close = self._cal.session_close(d)
            return close.to_pydatetime().replace(tzinfo=ET)
        next_session = self._cal.next_session(d)
        close = self._cal.session_close(next_session)
        return close.to_pydatetime().replace(tzinfo=ET)
