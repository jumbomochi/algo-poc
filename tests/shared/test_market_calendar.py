# tests/shared/test_market_calendar.py
from datetime import datetime, date
from zoneinfo import ZoneInfo
from shared.market_calendar import MarketCalendar

ET = ZoneInfo("America/New_York")


def test_is_market_open_during_trading_hours():
    cal = MarketCalendar()
    dt = datetime(2025, 1, 6, 10, 0, tzinfo=ET)
    assert cal.is_market_open(dt) is True


def test_is_market_closed_on_weekend():
    cal = MarketCalendar()
    dt = datetime(2025, 1, 4, 10, 0, tzinfo=ET)  # Saturday
    assert cal.is_market_open(dt) is False


def test_is_market_closed_on_holiday():
    cal = MarketCalendar()
    dt = datetime(2025, 1, 20, 10, 0, tzinfo=ET)  # MLK Day
    assert cal.is_market_open(dt) is False


def test_get_last_session_close():
    cal = MarketCalendar()
    dt = datetime(2025, 1, 7, 8, 0, tzinfo=ET)  # Tuesday 8 AM
    last_close = cal.get_last_session_close(dt)
    assert last_close.date() == date(2025, 1, 6)  # Monday


def test_get_next_market_close():
    cal = MarketCalendar()
    dt = datetime(2025, 1, 6, 10, 0, tzinfo=ET)  # Monday 10 AM
    next_close = cal.get_next_market_close(dt)
    assert next_close.date() == date(2025, 1, 6)
