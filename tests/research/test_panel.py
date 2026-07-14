from __future__ import annotations

from datetime import date

import pandas as pd

from research.factors.panel import build_factor_panel


def test_panel_aligns_tickers_and_clips_after_as_of():
    bars = {
        "A": [
            {
                "date": date(2026, 1, 2),
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10,
                "volume": 100,
            },
            {
                "date": date(2026, 1, 5),
                "open": 11,
                "high": 12,
                "low": 10,
                "close": 11,
                "volume": 110,
            },
        ],
        "B": [
            {
                "date": date(2026, 1, 5),
                "open": 20,
                "high": 21,
                "low": 19,
                "close": 20,
                "volume": 200,
            }
        ],
    }
    panel = build_factor_panel(bars, as_of=date(2026, 1, 2))
    assert list(panel.field("close").columns) == ["A", "B"]
    assert list(panel.field("close").index.date) == [date(2026, 1, 2)]
    assert pd.isna(panel.field("close").loc["2026-01-02", "B"])


def test_fundamentals_appear_only_on_or_after_effective_date():
    bars = {
        "A": [
            {
                "date": date(2026, 1, 2),
                "open": 10,
                "high": 10,
                "low": 10,
                "close": 10,
                "volume": 100,
            },
            {
                "date": date(2026, 1, 5),
                "open": 10,
                "high": 10,
                "low": 10,
                "close": 10,
                "volume": 100,
            },
            {
                "date": date(2026, 1, 6),
                "open": 10,
                "high": 10,
                "low": 10,
                "close": 10,
                "volume": 100,
            },
        ]
    }
    fundamentals = {
        "A": [
            {
                "effective_at": "2026-01-05T12:00:00+00:00",
                "earnings_yield": 0.04,
            }
        ]
    }
    panel = build_factor_panel(bars, fundamentals_by_ticker=fundamentals)
    values = panel.field("fund:earnings_yield")["A"]
    assert pd.isna(values.iloc[0])
    assert pd.isna(values.iloc[1])
    assert values.iloc[2] == 0.04
