from __future__ import annotations

import os
import tempfile
from datetime import date

import pytest

from scripts.fetch_fundamentals import load_fundamentals_cache, save_fundamentals_cache


def test_save_and_load_fundamentals_cache():
    """Round-trip save and load of fundamentals cache."""
    data = {
        "AAPL": [
            {
                "report_date": "2024-03-31",
                "pe_ratio": 28.5,
                "pb_ratio": 45.2,
                "roe": 0.171,
                "debt_equity": 1.73,
                "profit_margin": 0.264,
                "sector": "Technology",
            },
        ],
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "fundamentals.json")
        save_fundamentals_cache(data, path)
        loaded = load_fundamentals_cache(path)

    assert "AAPL" in loaded
    assert len(loaded["AAPL"]) == 1
    assert loaded["AAPL"][0]["pe_ratio"] == 28.5
    assert loaded["AAPL"][0]["report_date"] == "2024-03-31"


def test_load_missing_cache_returns_empty():
    """Loading a non-existent cache file returns empty dict."""
    loaded = load_fundamentals_cache("/nonexistent/path.json")
    assert loaded == {}


def test_build_fundamentals_lookup():
    """Build date-indexed lookup from cached fundamentals."""
    from scripts.fetch_fundamentals import build_fundamentals_lookup

    cache = {
        "AAPL": [
            {"report_date": "2024-01-15", "pe_ratio": 25.0, "roe": 0.15,
             "debt_equity": 1.5, "profit_margin": 0.25, "pb_ratio": 40.0, "sector": "Technology"},
            {"report_date": "2024-04-15", "pe_ratio": 28.0, "roe": 0.17,
             "debt_equity": 1.4, "profit_margin": 0.26, "pb_ratio": 42.0, "sector": "Technology"},
        ],
    }

    # No filing lag: availability keys straight off the period-end date.
    lookup = build_fundamentals_lookup(cache, filing_lag_days=0)

    # Before first report: no data
    assert lookup("AAPL", date(2024, 1, 10)) is None

    # After first report, before second: use first report
    result = lookup("AAPL", date(2024, 2, 15))
    assert result is not None
    assert result["pe_ratio"] == 25.0

    # After second report: use second report
    result = lookup("AAPL", date(2024, 5, 1))
    assert result is not None
    assert result["pe_ratio"] == 28.0

    # Unknown ticker
    assert lookup("MSFT", date(2024, 5, 1)) is None


class TestFilingLag:
    """A fiscal period ending on date D is not public knowledge on date D.

    Finding 4.4 of the 2026-08-06 review: the lookup keyed availability off the
    fiscal period-end, so both the backtest and live paper trading read figures
    weeks before they were filed.
    """

    CACHE = {
        "AAPL": [
            {"report_date": "2024-03-31", "roe": 0.15},
            {"report_date": "2024-06-30", "roe": 0.17},
        ],
    }

    def test_period_end_is_not_an_availability_date(self):
        from scripts.fetch_fundamentals import build_fundamentals_lookup

        lookup = build_fundamentals_lookup(self.CACHE, filing_lag_days=45)

        # The quarter ended 2024-03-31 but was not filed on 2024-03-31.
        assert lookup("AAPL", date(2024, 3, 31)) is None
        assert lookup("AAPL", date(2024, 5, 14)) is None
        # 45 days after period end it is assumed filed and usable.
        assert lookup("AAPL", date(2024, 5, 15))["roe"] == 0.15

    def test_lag_defaults_to_the_10q_filing_deadline(self):
        from scripts.fetch_fundamentals import (
            DEFAULT_FILING_LAG_DAYS,
            build_fundamentals_lookup,
        )

        assert DEFAULT_FILING_LAG_DAYS >= 40  # SEC 10-Q deadline, large filers
        lookup = build_fundamentals_lookup(self.CACHE)
        assert lookup("AAPL", date(2024, 3, 31)) is None

    def test_explicit_filing_date_overrides_the_lag(self):
        from scripts.fetch_fundamentals import build_fundamentals_lookup

        cache = {
            "AAPL": [
                {"report_date": "2024-03-31", "filing_date": "2024-04-20", "roe": 0.15},
            ],
        }
        lookup = build_fundamentals_lookup(cache, filing_lag_days=45)

        assert lookup("AAPL", date(2024, 4, 19)) is None
        assert lookup("AAPL", date(2024, 4, 20))["roe"] == 0.15

    def test_reports_are_ordered_by_availability_not_period_end(self):
        """A late-filed earlier period must not shadow an already-public one."""
        from scripts.fetch_fundamentals import build_fundamentals_lookup

        cache = {
            "AAPL": [
                # Restated Q1, filed months late.
                {"report_date": "2024-03-31", "filing_date": "2024-09-30", "roe": 0.10},
                {"report_date": "2024-06-30", "filing_date": "2024-07-31", "roe": 0.17},
            ],
        }
        lookup = build_fundamentals_lookup(cache)

        assert lookup("AAPL", date(2024, 8, 15))["roe"] == 0.17
        assert lookup("AAPL", date(2024, 10, 1))["roe"] == 0.10

    def test_negative_lag_rejected(self):
        from scripts.fetch_fundamentals import build_fundamentals_lookup

        with pytest.raises(ValueError, match="filing_lag_days"):
            build_fundamentals_lookup(self.CACHE, filing_lag_days=-1)
