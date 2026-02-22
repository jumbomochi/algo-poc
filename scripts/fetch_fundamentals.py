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
from datetime import date
from typing import Any, Callable


SECTOR_MAP: dict[str, str] = {
    "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology", "AMZN": "Consumer Discretionary",
    "GOOGL": "Communication Services", "META": "Communication Services", "BRK B": "Financials",
    "LLY": "Healthcare", "AVGO": "Technology", "JPM": "Financials", "TSLA": "Consumer Discretionary",
    "UNH": "Healthcare", "XOM": "Energy", "V": "Financials", "MA": "Financials",
    "PG": "Consumer Staples", "COST": "Consumer Staples", "JNJ": "Healthcare", "HD": "Consumer Discretionary",
    "ABBV": "Healthcare", "WMT": "Consumer Staples", "NFLX": "Communication Services",
    "CRM": "Technology", "BAC": "Financials", "CVX": "Energy", "MRK": "Healthcare",
    "KO": "Consumer Staples", "AMD": "Technology", "PEP": "Consumer Staples",
    "TMO": "Healthcare", "LIN": "Materials", "ACN": "Technology", "CSCO": "Technology",
    "ADBE": "Technology", "MCD": "Consumer Discretionary", "ABT": "Healthcare",
    "WFC": "Financials", "DHR": "Healthcare", "TXN": "Technology", "PM": "Consumer Staples",
    "GE": "Industrials", "QCOM": "Technology", "ISRG": "Healthcare", "INTU": "Technology",
    "CMCSA": "Communication Services", "AMAT": "Technology", "VZ": "Communication Services",
    "NOW": "Technology", "IBM": "Technology", "AMGN": "Healthcare",
    "CAT": "Industrials", "MS": "Financials", "NEE": "Utilities", "LOW": "Consumer Discretionary",
    "UPS": "Industrials", "SPGI": "Financials", "RTX": "Industrials", "HON": "Industrials",
    "ELV": "Healthcare", "BLK": "Financials", "SYK": "Healthcare", "BKNG": "Consumer Discretionary",
    "MDLZ": "Consumer Staples", "ADP": "Industrials", "VRTX": "Healthcare",
    "SCHW": "Financials", "GILD": "Healthcare", "AMT": "Real Estate", "REGN": "Healthcare",
    "LRCX": "Technology", "PANW": "Technology", "BSX": "Healthcare", "CB": "Financials",
    "MMC": "Financials", "KLAC": "Technology", "TMUS": "Communication Services",
    "SHW": "Materials", "SO": "Utilities", "EQIX": "Real Estate", "MO": "Consumer Staples",
    "PGR": "Financials", "ZTS": "Healthcare", "CME": "Financials", "CI": "Healthcare",
    "DUK": "Utilities", "ICE": "Financials", "SNPS": "Technology", "CL": "Consumer Staples",
    "AON": "Financials", "MCO": "Financials", "WM": "Industrials", "CDNS": "Technology",
    "TGT": "Consumer Discretionary", "BDX": "Healthcare", "NOC": "Industrials",
    "APH": "Technology", "ITW": "Industrials", "FI": "Financials", "HUM": "Healthcare",
}


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
) -> Callable[[str, date], dict | None]:
    """Build a point-in-time fundamentals lookup function.

    Returns a function(ticker, as_of_date) -> dict | None that returns
    the most recent fundamentals report filed before as_of_date.
    Avoids look-ahead bias by only using data available at the time.
    """
    sorted_cache: dict[str, list[tuple[date, dict]]] = {}
    for ticker, reports in cache.items():
        entries = []
        for r in reports:
            rd = date.fromisoformat(r["report_date"]) if isinstance(r["report_date"], str) else r["report_date"]
            entries.append((rd, r))
        entries.sort(key=lambda x: x[0])
        sorted_cache[ticker] = entries

    def lookup(ticker: str, as_of_date: date) -> dict | None:
        entries = sorted_cache.get(ticker)
        if not entries:
            return None
        result = None
        for rd, report in entries:
            if rd <= as_of_date:
                result = report
            else:
                break
        return result

    return lookup


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
