from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

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
                "ingested_at": "2026-01-05T13:00:00+00:00",
                "source_revision": "1",
                "earnings_yield": 0.04,
            }
        ]
    }
    panel = build_factor_panel(bars, fundamentals_by_ticker=fundamentals)
    values = panel.field("fund:earnings_yield")["A"]
    assert pd.isna(values.iloc[0])
    assert pd.isna(values.iloc[1])
    assert values.iloc[2] == 0.04


def test_later_ingested_fundamental_revision_does_not_rewrite_history():
    bars = {
        "A": [
            {
                "date": day,
                "open": 10,
                "high": 10,
                "low": 10,
                "close": 10,
                "volume": 100,
            }
            for day in pd.date_range("2026-01-05", "2026-01-09").date
        ]
    }
    fundamentals = {
        "A": [
            {
                "effective_at": "2026-01-05T12:00:00+00:00",
                "ingested_at": "2026-01-05T13:00:00+00:00",
                "source_revision": "1",
                "earnings_yield": 0.04,
            },
            {
                "effective_at": "2026-01-05T12:00:00+00:00",
                "ingested_at": "2026-01-08T10:00:00+00:00",
                "source_revision": "2",
                "earnings_yield": 0.05,
            },
        ]
    }

    values = build_factor_panel(
        bars,
        fundamentals_by_ticker=fundamentals,
    ).field("fund:earnings_yield")["A"]

    assert pd.isna(values.loc["2026-01-05"])
    assert values.loc["2026-01-06"] == 0.04
    assert values.loc["2026-01-07"] == 0.04
    assert values.loc["2026-01-08"] == 0.04
    assert values.loc["2026-01-09"] == 0.05


def test_equal_availability_with_distinct_source_revisions_is_rejected():
    bars = {
        "A": [
            {
                "date": day,
                "open": 10,
                "high": 10,
                "low": 10,
                "close": 10,
                "volume": 100,
            }
            for day in (date(2026, 1, 5), date(2026, 1, 6))
        ]
    }
    fundamentals = {
        "A": [
            {
                "effective_at": "2026-01-05T12:00:00+00:00",
                "ingested_at": "2026-01-05T13:00:00+00:00",
                "source_revision": "2",
                "earnings_yield": 0.05,
            },
            {
                "effective_at": "2026-01-05T12:00:00+00:00",
                "ingested_at": "2026-01-05T13:00:00+00:00",
                "source_revision": "1",
                "earnings_yield": 0.04,
            },
        ]
    }

    with pytest.raises(ValueError, match="ambiguous.*source_revision.*availability"):
        build_factor_panel(bars, fundamentals_by_ticker=fundamentals)


def test_universe_snapshots_project_forward_without_backfill_and_track_removals():
    bars = {
        ticker: [
            {
                "date": day,
                "open": 10,
                "high": 10,
                "low": 10,
                "close": 10,
                "volume": 100,
            }
            for day in (
                date(2026, 1, 2),
                date(2026, 1, 5),
                date(2026, 1, 6),
            )
        ]
        for ticker in ("A", "B")
    }

    panel = build_factor_panel(
        bars,
        universe_membership_by_date={
            "2026-01-05": {"A", "B"},
            date(2026, 1, 6): {"B"},
        },
    )
    membership = panel.field("universe:member")

    assert membership.loc["2026-01-02"].isna().all()
    assert membership.loc["2026-01-05"].to_dict() == {"A": 1.0, "B": 1.0}
    assert membership.loc["2026-01-06"].to_dict() == {"A": 0.0, "B": 1.0}
    assert membership.index.equals(panel.field("close").index)
    assert membership.columns.equals(panel.field("close").columns)


def test_regime_labels_project_forward_and_respect_as_of_cutoff():
    bars = {
        ticker: [
            {
                "date": day,
                "open": 10,
                "high": 10,
                "low": 10,
                "close": 10,
                "volume": 100,
            }
            for day in (
                date(2026, 1, 2),
                date(2026, 1, 5),
                date(2026, 1, 6),
                date(2026, 1, 7),
            )
        ]
        for ticker in ("A", "B")
    }

    panel = build_factor_panel(
        bars,
        as_of=date(2026, 1, 6),
        regime_labels_by_date={
            "2026-01-05": "bull",
            date(2026, 1, 7): "bear",
        },
    )
    regime = panel.field("regime:label")

    assert regime.loc["2026-01-02"].isna().all()
    assert regime.loc["2026-01-05"].to_dict() == {"A": "bull", "B": "bull"}
    assert regime.loc["2026-01-06"].to_dict() == {"A": "bull", "B": "bull"}
    assert date(2026, 1, 7) not in regime.index.date
    assert regime.index.equals(panel.field("close").index)
    assert regime.columns.equals(panel.field("close").columns)
