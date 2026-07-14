from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import date
from typing import Any

import pandas as pd

from research.factors.contracts import FactorPanel

PRICE_FIELDS = ("open", "high", "low", "close", "volume")


def build_factor_panel(
    bars_by_ticker: Mapping[str, list[dict[str, Any]]],
    fundamentals_by_ticker: Mapping[str, list[dict[str, Any]]] | None = None,
    as_of: date | None = None,
    universe_membership_by_date: Mapping[date | str, Collection[str]] | None = None,
    regime_labels_by_date: Mapping[date | str, str] | None = None,
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
                key=lambda item: (
                    _fundamental_available_at(item),
                    str(item["source_revision"]),
                ),
            ):
                available_day = _fundamental_available_at(row).date()
                if available_day <= cutoff and metric in row:
                    # Conservative daily-bar policy: a filing becomes usable on
                    # the first trading date strictly after both its effective
                    # and ingestion timestamps. Later revisions therefore do
                    # not rewrite values observed before the revision arrived.
                    frame.loc[frame.index.date > available_day, ticker] = float(
                        row[metric]
                    )
        fields[f"fund:{metric}"] = frame

    if universe_membership_by_date is not None:
        membership = pd.DataFrame(index=index, columns=tickers, dtype=float)
        snapshots = sorted(
            (
                (pd.Timestamp(snapshot_date).date(), set(members))
                for snapshot_date, members in universe_membership_by_date.items()
            ),
            key=lambda item: item[0],
        )
        for snapshot_day, members in snapshots:
            if snapshot_day > cutoff:
                continue
            applicable = membership.index.date >= snapshot_day
            membership.loc[applicable, :] = 0.0
            known_members = [ticker for ticker in tickers if ticker in members]
            membership.loc[applicable, known_members] = 1.0
        fields["universe:member"] = membership

    if regime_labels_by_date is not None:
        regime = pd.DataFrame(index=index, columns=tickers, dtype=object)
        labels = sorted(
            (
                (pd.Timestamp(label_date).date(), label)
                for label_date, label in regime_labels_by_date.items()
            ),
            key=lambda item: item[0],
        )
        for label_day, label in labels:
            if label_day > cutoff:
                continue
            regime.loc[regime.index.date >= label_day, :] = label
        fields["regime:label"] = regime

    return FactorPanel(fields=fields, as_of=cutoff)


def _fundamental_available_at(row: Mapping[str, Any]) -> pd.Timestamp:
    effective_at = _as_utc_timestamp(row["effective_at"])
    ingested_at = _as_utc_timestamp(row["ingested_at"])
    return max(effective_at, ingested_at)


def _as_utc_timestamp(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")
