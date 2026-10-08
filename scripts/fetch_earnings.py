#!/usr/bin/env python3
"""Fetch and cache historical earnings data from yfinance.

Usage:
    python scripts/fetch_earnings.py [--tickers AAPL,MSFT,...] [--output PATH]

The default output is ``earnings.json`` in ``data.cache_dir`` (config/default.yaml,
resolved against the repo root), the same file the paper run and the backtest
read. The file is written in the KAN-109 envelope, stamped with ``fetched_at``.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

# Run by path (``python scripts/fetch_*.py``), which puts scripts/ rather than
# the repo root on sys.path; pin the root so ``shared`` is THIS checkout's.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


from shared.data_cache import (  # noqa: E402
    EARNINGS_FILE,
    read_cache_document,
    resolve_cache_dir,
    write_cache_document,
)


def save_earnings_cache(
    data: dict[str, list[dict]],
    path: str,
    *,
    fetched_at: datetime | None = None,
    source: str | None = None,
) -> None:
    """Save earnings rows, stamped with when they were fetched (KAN-109).

    ``fetched_at`` defaults to now; pass the instant the fetch STARTED.
    """
    write_cache_document(path, data, fetched_at=fetched_at, source=source)


def load_earnings_cache(path: str) -> dict[str, list[dict]]:
    """Load earnings rows. Returns empty dict if file missing.

    Reads both the KAN-109 envelope and the legacy bare ``{ticker: rows}``
    mapping, and returns only the rows, so the lookup builders never see the
    metadata. Use :func:`shared.data_cache.read_cache_document` for the fetch
    time and whether the file was there at all — this function cannot tell a
    missing cache from an empty one, which is how KAN-109 went unnoticed.
    """
    return read_cache_document(path).data


def build_earnings_lookup(
    cache: dict[str, list[dict]],
    window_days: int = 2,
) -> Callable[[str, date], dict | None]:
    """Build an earnings event lookup function.

    Returns a function(ticker, as_of_date) -> dict | None that returns
    the earnings event if one occurred within window_days of as_of_date.
    Only returns events on or after the earnings date (not before).
    """
    events_by_ticker: dict[str, dict[date, dict]] = {}
    for ticker, events in cache.items():
        date_map: dict[date, dict] = {}
        for e in events:
            ed = date.fromisoformat(e["earnings_date"]) if isinstance(e["earnings_date"], str) else e["earnings_date"]
            for offset in range(window_days + 1):
                d = ed + timedelta(days=offset)
                if d not in date_map:
                    date_map[d] = e
        events_by_ticker[ticker] = date_map

    def lookup(ticker: str, as_of_date: date) -> dict | None:
        date_map = events_by_ticker.get(ticker)
        if not date_map:
            return None
        return date_map.get(as_of_date)

    return lookup


def fetch_earnings_from_yfinance(
    tickers: list[str],
    output_path: str | None = None,
) -> dict[str, list[dict]]:
    """Fetch earnings history from yfinance and save to cache."""
    import yfinance as yf

    if output_path is None:
        output_path = str(resolve_cache_dir() / EARNINGS_FILE)
    fetched_at = datetime.now(timezone.utc)

    cache: dict[str, list[dict]] = {}

    for i, ticker in enumerate(tickers):
        print(f"  [{i+1}/{len(tickers)}] Fetching {ticker}...", end=" ", flush=True)
        try:
            yf_ticker = yf.Ticker(ticker.replace(" ", "-"))
            earnings_dates = yf_ticker.earnings_dates

            if earnings_dates is None or earnings_dates.empty:
                print("no data")
                continue

            events = []
            for idx, row in earnings_dates.iterrows():
                try:
                    actual = float(row.get("Reported EPS", 0) or 0)
                    estimate = float(row.get("EPS Estimate", 0) or 0)

                    if estimate == 0 and actual == 0:
                        continue

                    surprise_pct = ((actual - estimate) / abs(estimate) * 100) if estimate != 0 else 0.0
                    earnings_date = idx.date() if hasattr(idx, 'date') else idx

                    events.append({
                        "earnings_date": str(earnings_date),
                        "actual_eps": round(actual, 4),
                        "estimate_eps": round(estimate, 4),
                        "surprise_pct": round(surprise_pct, 2),
                    })
                except (TypeError, ValueError):
                    continue

            if events:
                events = [e for e in events if e["actual_eps"] != 0 or e["estimate_eps"] != 0]
                cache[ticker] = sorted(events, key=lambda e: e["earnings_date"])
                print(f"{len(events)} events")
            else:
                print("no usable data")

        except Exception as e:
            print(f"FAILED: {e}")

        time.sleep(0.3)

    save_earnings_cache(cache, output_path, fetched_at=fetched_at, source="yfinance")
    print(f"\nSaved {len(cache)} tickers to {output_path}")
    return cache


def main():
    parser = argparse.ArgumentParser(description="Fetch earnings data from yfinance")
    parser.add_argument("--tickers", type=str, default=None,
                        help="Comma-separated tickers (default: SP500_TOP100)")
    parser.add_argument("--output", default=None,
                        help="Output JSON path (default: earnings.json in data.cache_dir)")
    args = parser.parse_args()

    if args.tickers:
        tickers = [t.strip() for t in args.tickers.split(",")]
    else:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from scripts.run_backtest import SP500_TOP100
        tickers = SP500_TOP100

    print(f"Fetching earnings for {len(tickers)} tickers...")
    fetch_earnings_from_yfinance(tickers, args.output)


if __name__ == "__main__":
    main()
