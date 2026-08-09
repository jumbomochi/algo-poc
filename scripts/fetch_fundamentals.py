#!/usr/bin/env python3
"""Fetch and cache historical fundamentals data from yfinance.

Usage:
    python scripts/fetch_fundamentals.py [--tickers AAPL,MSFT,...] [--output data/cache/fundamentals.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, timedelta
from typing import Any, Callable


# The equity sector map moved to shared/universe.py so services (risk
# management, fill projector) can resolve sectors too. Re-exported here
# for existing importers (run_paper.py, validate_replacement_policy.py).
from shared.universe import SECTOR_MAP  # noqa: F401

# Calendar days after a fiscal period-end before its figures are assumed to be
# public. yfinance's quarterly statements carry the period-end only, so this
# stands in for the filing date. 45 days clears the SEC's 40-day 10-Q deadline
# for large accelerated filers with a small margin; the annual 10-K deadline is
# 60 days, so pass a larger lag if the cache mixes in fiscal-year-end periods
# and you want to be strict about those too. Rows that do carry an explicit
# ``filing_date`` use it instead of this lag.
DEFAULT_FILING_LAG_DAYS = 45


def save_fundamentals_cache(data: dict[str, list[dict]], path: str) -> None:
    """Save fundamentals data to JSON file."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_fundamentals_cache(path: str) -> dict[str, list[dict]]:
    """Load fundamentals data from JSON file. Returns empty dict if file missing."""
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def build_fundamentals_lookup(
    cache: dict[str, list[dict]],
    filing_lag_days: int = DEFAULT_FILING_LAG_DAYS,
) -> Callable[[str, date], dict | None]:
    """Build a point-in-time fundamentals lookup function.

    Returns a function(ticker, as_of_date) -> dict | None giving the most
    recent report **available** on ``as_of_date``.

    Availability is the filing date, not the fiscal period-end: a quarter
    ending 31 March is not public knowledge on 31 March. Each row's
    availability is:

    - its explicit ``filing_date``, when the data source provides one; else
    - ``report_date + filing_lag_days`` (see ``DEFAULT_FILING_LAG_DAYS``).

    Keying off ``report_date`` — as this function used to — leaks figures weeks
    early into both the backtest and live paper trading (finding 4.4 of the
    2026-08-06 implementation review). Reports are ordered by availability, so
    a late-filed or restated earlier period cannot shadow one that was already
    public.
    """
    if filing_lag_days < 0:
        raise ValueError(f"filing_lag_days must be >= 0, got {filing_lag_days}")

    lag = timedelta(days=filing_lag_days)
    sorted_cache: dict[str, list[tuple[date, dict]]] = {}
    for ticker, reports in cache.items():
        entries = []
        for r in reports:
            filed = r.get("filing_date")
            if filed:
                available = _as_date(filed)
            else:
                available = _as_date(r["report_date"]) + lag
            entries.append((available, r))
        entries.sort(key=lambda x: x[0])
        sorted_cache[ticker] = entries

    def lookup(ticker: str, as_of_date: date) -> dict | None:
        entries = sorted_cache.get(ticker)
        if not entries:
            return None
        result = None
        for available_date, report in entries:
            if available_date <= as_of_date:
                result = report
            else:
                break
        return result

    return lookup


def _as_date(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def fetch_fundamentals_from_yfinance(
    tickers: list[str],
    output_path: str = "data/cache/fundamentals.json",
) -> dict[str, list[dict]]:
    """Fetch quarterly fundamentals from yfinance and save to cache."""
    import yfinance as yf

    cache: dict[str, list[dict]] = {}

    for i, ticker in enumerate(tickers):
        print(f"  [{i+1}/{len(tickers)}] Fetching {ticker}...", end=" ", flush=True)
        try:
            yf_ticker = yf.Ticker(ticker.replace(" ", "-"))
            income = yf_ticker.quarterly_income_stmt
            balance = yf_ticker.quarterly_balance_sheet

            if income.empty or balance.empty:
                print("no data")
                continue

            reports = []
            for col_date in income.columns:
                report_date = col_date.date() if hasattr(col_date, 'date') else col_date
                try:
                    net_income = float(income.loc["Net Income", col_date]) if "Net Income" in income.index else 0.0
                    total_revenue = float(income.loc["Total Revenue", col_date]) if "Total Revenue" in income.index else 0.0

                    total_equity = 0.0
                    total_debt = 0.0
                    if col_date in balance.columns:
                        if "Stockholders Equity" in balance.index:
                            total_equity = float(balance.loc["Stockholders Equity", col_date])
                        elif "Total Equity Gross Minority Interest" in balance.index:
                            total_equity = float(balance.loc["Total Equity Gross Minority Interest", col_date])
                        if "Total Debt" in balance.index:
                            total_debt = float(balance.loc["Total Debt", col_date])

                    roe = net_income / total_equity if total_equity > 0 else 0.0
                    debt_equity = total_debt / total_equity if total_equity > 0 else 0.0
                    profit_margin = net_income / total_revenue if total_revenue > 0 else 0.0

                    reports.append({
                        "report_date": str(report_date),
                        "roe": round(roe, 4),
                        "debt_equity": round(debt_equity, 4),
                        "profit_margin": round(profit_margin, 4),
                        "net_income": net_income,
                        "total_revenue": total_revenue,
                        "total_equity": total_equity,
                        "total_debt": total_debt,
                        "sector": SECTOR_MAP.get(ticker, "Unknown"),
                    })
                except (KeyError, TypeError, ValueError):
                    continue

            if reports:
                cache[ticker] = sorted(reports, key=lambda r: r["report_date"])
                print(f"{len(reports)} reports")
            else:
                print("no usable data")

        except Exception as e:
            print(f"FAILED: {e}")

        time.sleep(0.3)

    save_fundamentals_cache(cache, output_path)
    print(f"\nSaved {len(cache)} tickers to {output_path}")
    return cache


def main():
    parser = argparse.ArgumentParser(description="Fetch fundamentals data from yfinance")
    parser.add_argument("--tickers", type=str, default=None,
                        help="Comma-separated tickers (default: SP500_TOP100)")
    parser.add_argument("--output", default="data/cache/fundamentals.json",
                        help="Output JSON path")
    args = parser.parse_args()

    if args.tickers:
        tickers = [t.strip() for t in args.tickers.split(",")]
    else:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from scripts.run_backtest import SP500_TOP100
        tickers = SP500_TOP100

    print(f"Fetching fundamentals for {len(tickers)} tickers...")
    fetch_fundamentals_from_yfinance(tickers, args.output)


if __name__ == "__main__":
    main()
