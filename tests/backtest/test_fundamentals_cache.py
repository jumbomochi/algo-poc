from __future__ import annotations

import json
import os
import tempfile
from datetime import date

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

    lookup = build_fundamentals_lookup(cache)

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
