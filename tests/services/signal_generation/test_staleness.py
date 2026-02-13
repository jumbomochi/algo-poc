import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from services.signal_generation.staleness import StalenessChecker


def test_fresh_signal_is_not_stale():
    cal = MagicMock()
    cal.get_last_session_close.return_value = datetime(2025, 1, 6, 16, 0, tzinfo=timezone.utc)
    checker = StalenessChecker(calendar=cal, grace_hours=4, fundamentals_days=7, events_hours=48)
    now = datetime(2025, 1, 6, 18, 0, tzinfo=timezone.utc)
    assert checker.is_stale("market_data", datetime(2025, 1, 6, 16, 30, tzinfo=timezone.utc), now) is False


def test_stale_market_data_after_grace_window():
    cal = MagicMock()
    cal.get_last_session_close.return_value = datetime(2025, 1, 6, 16, 0, tzinfo=timezone.utc)
    checker = StalenessChecker(calendar=cal, grace_hours=4, fundamentals_days=7, events_hours=48)
    now = datetime(2025, 1, 7, 12, 0, tzinfo=timezone.utc)
    assert checker.is_stale("market_data", datetime(2025, 1, 6, 10, 0, tzinfo=timezone.utc), now) is True


def test_fundamentals_stale_after_7_days():
    cal = MagicMock()
    checker = StalenessChecker(calendar=cal, grace_hours=4, fundamentals_days=7, events_hours=48)
    now = datetime(2025, 1, 15, 12, 0, tzinfo=timezone.utc)
    assert checker.is_stale("fundamentals", datetime(2025, 1, 5, 12, 0, tzinfo=timezone.utc), now) is True


def test_weekend_does_not_trigger_false_positive():
    cal = MagicMock()
    cal.get_last_session_close.return_value = datetime(2025, 1, 3, 16, 0, tzinfo=timezone.utc)
    checker = StalenessChecker(calendar=cal, grace_hours=4, fundamentals_days=7, events_hours=48)
    now = datetime(2025, 1, 4, 10, 0, tzinfo=timezone.utc)
    assert checker.is_stale("market_data", datetime(2025, 1, 3, 16, 30, tzinfo=timezone.utc), now) is False


def test_events_stale_after_48_hours():
    cal = MagicMock()
    checker = StalenessChecker(calendar=cal, grace_hours=4, fundamentals_days=7, events_hours=48)
    now = datetime(2025, 1, 10, 12, 0, tzinfo=timezone.utc)
    assert checker.is_stale("events", datetime(2025, 1, 8, 10, 0, tzinfo=timezone.utc), now) is True


def test_events_fresh_within_48_hours():
    cal = MagicMock()
    checker = StalenessChecker(calendar=cal, grace_hours=4, fundamentals_days=7, events_hours=48)
    now = datetime(2025, 1, 8, 12, 0, tzinfo=timezone.utc)
    assert checker.is_stale("events", datetime(2025, 1, 7, 12, 0, tzinfo=timezone.utc), now) is False


def test_unknown_signal_type_returns_false():
    cal = MagicMock()
    checker = StalenessChecker(calendar=cal, grace_hours=4, fundamentals_days=7, events_hours=48)
    now = datetime(2025, 1, 10, 12, 0, tzinfo=timezone.utc)
    assert checker.is_stale("unknown_type", datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc), now) is False
