from __future__ import annotations

from datetime import datetime, timedelta


class StalenessChecker:
    """Determines whether signal data is stale based on signal type.

    Uses calendar-aware thresholds for market data (avoids false positives
    on weekends/holidays) and simple time deltas for fundamentals and events.
    """

    def __init__(
        self,
        calendar,
        grace_hours: int,
        fundamentals_days: int,
        events_hours: int,
    ):
        self._calendar = calendar
        self._grace_hours = grace_hours
        self._fundamentals_days = fundamentals_days
        self._events_hours = events_hours

    def is_stale(
        self,
        signal_type: str,
        last_update: datetime,
        now: datetime,
    ) -> bool:
        """Check if data for the given signal type is stale.

        Args:
            signal_type: One of "market_data", "fundamentals", "events".
            last_update: Timestamp of the most recent data update.
            now: Current timestamp.

        Returns:
            True if the data is stale and should be refreshed.
        """
        if signal_type == "market_data":
            last_close = self._calendar.get_last_session_close(now)
            deadline = last_close + timedelta(hours=self._grace_hours)
            # Data is stale if it predates the last session close AND
            # we are past the grace window after that close.
            return last_update < last_close and now > deadline
        elif signal_type == "fundamentals":
            return (now - last_update) > timedelta(days=self._fundamentals_days)
        elif signal_type == "events":
            return (now - last_update) > timedelta(hours=self._events_hours)
        return False
