from __future__ import annotations

from datetime import date
from typing import Any, Mapping

import pandas as pd

from research.factors.contracts import FactorPanel

PRICE_FIELDS = ("open", "high", "low", "close", "volume")


def build_factor_panel(
    bars_by_ticker: Mapping[str, list[dict[str, Any]]],
    fundamentals_by_ticker: Mapping[str, list[dict[str, Any]]] | None = None,
    as_of: date | None = None,
) -> FactorPanel:
    tickers = sorted(bars_by_ticker)
    dates = sorted(
        {
            pd.Timestamp(bar["date"]).date()
            for bars in bars_by_ticker.values()
            for bar in bars
        }
    )
    cutoff = as_of or (dates[-1] if dates else date.min)
    index = pd.DatetimeIndex([day for day in dates if day <= cutoff])
    fields = {
        name: pd.DataFrame(index=index, columns=tickers, dtype=float)
        for name in PRICE_FIELDS
    }

    for ticker, bars in bars_by_ticker.items():
        for bar in bars:
            timestamp = pd.Timestamp(bar["date"])
            if timestamp.date() > cutoff or timestamp not in index:
                continue
            for field in PRICE_FIELDS:
                fields[field].at[timestamp, ticker] = float(bar[field])

    metric_names = sorted(
        {
            key
            for rows in (fundamentals_by_ticker or {}).values()
            for row in rows
            for key in row
            if key
            not in {"effective_at", "ingested_at", "source_revision", "report_date"}
            and isinstance(row[key], (int, float))
        }
    )
    for metric in metric_names:
        frame = pd.DataFrame(index=index, columns=tickers, dtype=float)
        for ticker, rows in (fundamentals_by_ticker or {}).items():
            for row in sorted(
                rows,
                key=lambda item: pd.Timestamp(item["effective_at"]),
            ):
                effective_day = pd.Timestamp(row["effective_at"]).date()
                if effective_day <= cutoff and metric in row:
                    # Conservative daily-bar policy: a filing becomes usable on
                    # the first trading date strictly after its effective date.
                    # This prevents an after-close filing from contaminating
                    # the same day's close-based signal.
                    frame.loc[frame.index.date > effective_day, ticker] = float(
                        row[metric]
                    )
        fields[f"fund:{metric}"] = frame

    return FactorPanel(fields=fields, as_of=cutoff)
