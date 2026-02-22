from __future__ import annotations

import json
import os
import tempfile
from datetime import date

from scripts.fetch_earnings import load_earnings_cache, save_earnings_cache, build_earnings_lookup


def test_save_and_load_earnings_cache():
    """Round-trip save and load of earnings cache."""
    data = {
        "AAPL": [
            {
                "earnings_date": "2024-01-25",
                "actual_eps": 2.18,
                "estimate_eps": 2.10,
                "surprise_pct": 3.81,
            },
        ],
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "earnings.json")
        save_earnings_cache(data, path)
        loaded = load_earnings_cache(path)

    assert "AAPL" in loaded
    assert len(loaded["AAPL"]) == 1
    assert loaded["AAPL"][0]["actual_eps"] == 2.18


def test_load_missing_earnings_cache_returns_empty():
    """Loading a non-existent cache file returns empty dict."""
    loaded = load_earnings_cache("/nonexistent/path.json")
    assert loaded == {}


def test_build_earnings_lookup():
    """Build date-indexed earnings lookup."""
    cache = {
        "AAPL": [
            {"earnings_date": "2024-01-25", "actual_eps": 2.18,
             "estimate_eps": 2.10, "surprise_pct": 3.81},
            {"earnings_date": "2024-04-25", "actual_eps": 1.53,
             "estimate_eps": 1.50, "surprise_pct": 2.0},
        ],
    }

    lookup = build_earnings_lookup(cache, window_days=2)

    # On earnings day: should find the event
    result = lookup("AAPL", date(2024, 1, 25))
    assert result is not None
    assert result["actual_eps"] == 2.18

    # 1 day after: still within window
    result = lookup("AAPL", date(2024, 1, 26))
    assert result is not None

    # 3 days after: outside window
    result = lookup("AAPL", date(2024, 1, 28))
    assert result is None

    # Before earnings: no event
    result = lookup("AAPL", date(2024, 1, 24))
    assert result is None

    # Unknown ticker
    assert lookup("MSFT", date(2024, 1, 25)) is None
