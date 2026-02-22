# Phase 3: Data-Dependent Strategies — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add 2 data-dependent strategies (Quality Value, Earnings Drift) to the multi-portfolio backtest. Both require historical fundamentals/earnings data beyond OHLCV bars. Data is fetched from yfinance and cached as JSON for reproducible backtests.

**Architecture:** A `FundamentalsCache` and `EarningsCache` load/save historical data as JSON files in `data/cache/`. Fetch scripts populate the cache from yfinance. Signal functions receive the cached data as lookup dicts (same pattern as `bars_by_ticker`). Each strategy gets its own `make_*_signals_fn()` factory and `PortfolioConfig` entry.

**Tech Stack:** Python 3.12, yfinance, numpy, pytest, existing backtest infrastructure

---

### Task 1: Create fundamentals data cache

**Files:**
- Create: `data/cache/.gitkeep`
- Create: `scripts/fetch_fundamentals.py`
- Create: `tests/backtest/test_fundamentals_cache.py`

**Step 1: Write the failing test**

Create `tests/backtest/test_fundamentals_cache.py`:

```python
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
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/backtest/test_fundamentals_cache.py -v`
Expected: FAIL with ImportError

**Step 3: Implement `scripts/fetch_fundamentals.py`**

```python
#!/usr/bin/env python3
"""Fetch and cache historical fundamentals data from yfinance.

Usage:
    python scripts/fetch_fundamentals.py [--tickers AAPL,MSFT,...] [--output data/cache/fundamentals.json]

Fetches quarterly financials and computes key ratios (PE, PB, ROE, D/E, margin).
Saves as JSON for reproducible backtests.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date
from typing import Any, Callable


# Sector mapping for S&P 500 top 100 (simplified)
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
    # Pre-sort each ticker's reports by date
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
        # Binary search for most recent report before as_of_date
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
            yf_ticker = yf.Ticker(ticker.replace(" ", "-"))  # BRK B -> BRK-B

            # Get quarterly financials
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

                    # Compute ratios
                    roe = net_income / total_equity if total_equity > 0 else 0.0
                    debt_equity = total_debt / total_equity if total_equity > 0 else 0.0
                    profit_margin = net_income / total_revenue if total_revenue > 0 else 0.0

                    # PE and PB require market price — we'll compute PE dynamically in the signal fn
                    # Store EPS for PE computation during backtest
                    eps_ttm = (net_income * 4) / 1e9  # Annualized, placeholder

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

        time.sleep(0.3)  # Rate limiting

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
        # Import from run_backtest
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from scripts.run_backtest import SP500_TOP100
        tickers = SP500_TOP100

    print(f"Fetching fundamentals for {len(tickers)} tickers...")
    fetch_fundamentals_from_yfinance(tickers, args.output)


if __name__ == "__main__":
    main()
```

**Step 4: Run tests**

Run: `pytest tests/backtest/test_fundamentals_cache.py -v`
Expected: PASS

**Step 5: Commit**

```bash
mkdir -p data/cache && touch data/cache/.gitkeep
git add scripts/fetch_fundamentals.py tests/backtest/test_fundamentals_cache.py data/cache/.gitkeep
git commit -m "feat: add fundamentals data cache with yfinance fetcher"
```

---

### Task 2: Create earnings data cache

**Files:**
- Create: `scripts/fetch_earnings.py`
- Create: `tests/backtest/test_earnings_cache.py`

**Step 1: Write the failing test**

Create `tests/backtest/test_earnings_cache.py`:

```python
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
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/backtest/test_earnings_cache.py -v`
Expected: FAIL with ImportError

**Step 3: Implement `scripts/fetch_earnings.py`**

```python
#!/usr/bin/env python3
"""Fetch and cache historical earnings data from yfinance.

Usage:
    python scripts/fetch_earnings.py [--tickers AAPL,MSFT,...] [--output data/cache/earnings.json]

Fetches earnings dates with actual vs estimate EPS.
Saves as JSON for reproducible backtests.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, timedelta
from typing import Callable


def save_earnings_cache(data: dict[str, list[dict]], path: str) -> None:
    """Save earnings data to JSON file."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_earnings_cache(path: str) -> dict[str, list[dict]]:
    """Load earnings data from JSON file. Returns empty dict if file missing."""
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def build_earnings_lookup(
    cache: dict[str, list[dict]],
    window_days: int = 2,
) -> Callable[[str, date], dict | None]:
    """Build an earnings event lookup function.

    Returns a function(ticker, as_of_date) -> dict | None that returns
    the earnings event if one occurred within window_days of as_of_date.
    Only returns events on or after the earnings date (not before).
    """
    # Pre-build date -> event mapping per ticker
    events_by_ticker: dict[str, dict[date, dict]] = {}
    for ticker, events in cache.items():
        date_map: dict[date, dict] = {}
        for e in events:
            ed = date.fromisoformat(e["earnings_date"]) if isinstance(e["earnings_date"], str) else e["earnings_date"]
            # Map the earnings date and the next window_days to this event
            for offset in range(window_days + 1):
                d = ed + timedelta(days=offset)
                if d not in date_map:  # first event wins if overlap
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
    output_path: str = "data/cache/earnings.json",
) -> dict[str, list[dict]]:
    """Fetch earnings history from yfinance and save to cache."""
    import yfinance as yf

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
                # Filter out future dates (no actual EPS yet)
                events = [e for e in events if e["actual_eps"] != 0 or e["estimate_eps"] != 0]
                cache[ticker] = sorted(events, key=lambda e: e["earnings_date"])
                print(f"{len(events)} events")
            else:
                print("no usable data")

        except Exception as e:
            print(f"FAILED: {e}")

        time.sleep(0.3)

    save_earnings_cache(cache, output_path)
    print(f"\nSaved {len(cache)} tickers to {output_path}")
    return cache


def main():
    parser = argparse.ArgumentParser(description="Fetch earnings data from yfinance")
    parser.add_argument("--tickers", type=str, default=None,
                        help="Comma-separated tickers (default: SP500_TOP100)")
    parser.add_argument("--output", default="data/cache/earnings.json",
                        help="Output JSON path")
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
```

**Step 4: Run tests**

Run: `pytest tests/backtest/test_earnings_cache.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add scripts/fetch_earnings.py tests/backtest/test_earnings_cache.py
git commit -m "feat: add earnings data cache with yfinance fetcher"
```

---

### Task 3: Add make_quality_value_signals_fn()

**Files:**
- Modify: `scripts/run_backtest.py`
- Create: `tests/backtest/test_quality_value_signals.py`

**Step 1: Write the failing test**

Create `tests/backtest/test_quality_value_signals.py`:

```python
from __future__ import annotations

from datetime import date, timedelta

from scripts.run_backtest import make_quality_value_signals_fn


def _make_fundamentals_lookup():
    """Create a fundamentals lookup with synthetic data.

    AAPL: high quality (high ROE, low D/E, good margin, low PE)
    MSFT: average quality
    AMZN: low quality (high PE, high D/E)
    All in Technology sector.
    """
    from scripts.fetch_fundamentals import build_fundamentals_lookup

    cache = {
        "AAPL": [{"report_date": "2024-01-01", "roe": 0.25, "debt_equity": 0.5,
                   "profit_margin": 0.30, "net_income": 1e9, "total_revenue": 4e9,
                   "total_equity": 4e9, "total_debt": 2e9, "sector": "Technology"}],
        "MSFT": [{"report_date": "2024-01-01", "roe": 0.15, "debt_equity": 1.0,
                   "profit_margin": 0.20, "net_income": 5e8, "total_revenue": 2.5e9,
                   "total_equity": 3e9, "total_debt": 3e9, "sector": "Technology"}],
        "AMZN": [{"report_date": "2024-01-01", "roe": 0.08, "debt_equity": 2.0,
                   "profit_margin": 0.05, "net_income": 2e8, "total_revenue": 4e9,
                   "total_equity": 2e9, "total_debt": 4e9, "sector": "Technology"}],
    }
    return build_fundamentals_lookup(cache)


def _make_bars(tickers: list[str], days: int = 100):
    """Create synthetic bars for testing."""
    bars = {}
    for ticker in tickers:
        ticker_bars = []
        price = 150.0
        for d in range(days):
            ticker_bars.append({
                "date": date(2024, 1, 1) + timedelta(days=d),
                "open": price, "high": price + 1, "low": price - 1,
                "close": price, "volume": 50000,
            })
        bars[ticker] = ticker_bars
    return bars


def test_quality_value_buys_high_quality():
    """Quality value buys stocks with best fundamentals."""
    tickers = ["AAPL", "MSFT", "AMZN"]
    bars = _make_bars(tickers)
    fundamentals_lookup = _make_fundamentals_lookup()

    signals_fn = make_quality_value_signals_fn(
        fundamentals_lookup=fundamentals_lookup,
        sector_map={"AAPL": "Technology", "MSFT": "Technology", "AMZN": "Technology"},
        top_n=1,
        position_size_pct=0.10,
        initial_capital=20_000,
    )

    # AAPL has best fundamentals (highest ROE, lowest D/E, best margin)
    result = signals_fn("AAPL", bars["AAPL"])
    assert result is not None
    assert result["action"] == "buy"
    assert result["signals"]["strategy"] == "quality_value"


def test_quality_value_skips_low_quality():
    """Quality value doesn't buy stocks with poor fundamentals."""
    tickers = ["AAPL", "MSFT", "AMZN"]
    bars = _make_bars(tickers)
    fundamentals_lookup = _make_fundamentals_lookup()

    signals_fn = make_quality_value_signals_fn(
        fundamentals_lookup=fundamentals_lookup,
        sector_map={"AAPL": "Technology", "MSFT": "Technology", "AMZN": "Technology"},
        top_n=1,
        position_size_pct=0.10,
        initial_capital=20_000,
    )

    # AMZN has worst fundamentals
    result = signals_fn("AMZN", bars["AMZN"])
    assert result is None


def test_quality_value_requires_fundamentals():
    """Returns None for tickers without fundamentals data."""
    bars = _make_bars(["UNKNOWN"])
    fundamentals_lookup = _make_fundamentals_lookup()

    signals_fn = make_quality_value_signals_fn(
        fundamentals_lookup=fundamentals_lookup,
        sector_map={},
        top_n=1,
        position_size_pct=0.10,
        initial_capital=20_000,
    )

    result = signals_fn("UNKNOWN", bars["UNKNOWN"])
    assert result is None
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/backtest/test_quality_value_signals.py -v`
Expected: FAIL with ImportError

**Step 3: Implement make_quality_value_signals_fn()**

Add to `scripts/run_backtest.py` after `make_thematic_momentum_signals_fn()` and before `compute_aggregate_metrics()`:

```python
def make_quality_value_signals_fn(
    fundamentals_lookup: Callable[[str, date], dict | None],
    sector_map: dict[str, str],
    top_n: int = 15,
    position_size_pct: float = 0.10,
    initial_capital: float = 100_000,
    trailing_stop_pct: float = 0.12,
):
    """Create a quality value signal function.

    Ranks stocks by a composite quality-value score (ROE, D/E, margin).
    Entry: composite score in top N within same-sector comparison.
    Exit: trailing stop or fundamentals deterioration.
    Rebalance: quarterly (checks fundamentals each bar, but only enters/exits on signal).
    """
    tracked: dict[str, list[dict]] = {}

    def _compute_quality_score(fundamentals: dict) -> float:
        """Compute composite quality-value score. Higher = better."""
        roe = fundamentals.get("roe", 0.0)
        debt_equity = fundamentals.get("debt_equity", 0.0)
        margin = fundamentals.get("profit_margin", 0.0)

        # ROE score: higher is better, scale so 20% ROE = 1.0
        roe_score = roe / 0.20

        # D/E score: lower is better, invert. D/E of 0 = 1.0, D/E of 2 = 0.0
        de_score = max(0.0, 1.0 - debt_equity / 2.0)

        # Margin score: higher is better, scale so 25% margin = 1.0
        margin_score = margin / 0.25

        return (roe_score + de_score + margin_score) / 3.0

    # Pre-compute all tickers' quality scores by date for ranking
    all_tickers = list(sector_map.keys())
    scores_cache: dict[str, float] = {}  # ticker -> latest score

    def signals_fn(ticker: str, bars: list[dict]) -> dict | None:
        if len(bars) < 5:
            return None

        current_price = bars[-1]["close"]
        current_date = bars[-1]["date"]
        bar_count = len(bars)
        lots = tracked.get(ticker, [])

        # Get fundamentals for this ticker
        fundamentals = fundamentals_lookup(ticker, current_date)
        if fundamentals is None:
            return None

        # Exit logic: trailing stop
        if lots:
            for lot in lots:
                lot["peak_price"] = max(lot["peak_price"], current_price)

            for lot in lots:
                peak = lot["peak_price"]
                entry = lot["entry_price"]
                if peak > entry and (peak - current_price) / peak >= trailing_stop_pct:
                    tracked.pop(ticker, None)
                    return {
                        "action": "sell",
                        "ticker": ticker,
                        "limit_price": current_price,
                        "quantity": 0,
                        "sector": sector_map.get(ticker, "Unknown"),
                        "exit_reason": "trailing_stop",
                    }

        # Compute quality score
        score = _compute_quality_score(fundamentals)
        scores_cache[ticker] = score

        # Rank against all tickers with known scores
        if len(scores_cache) < 3:
            return None  # Not enough data to rank

        ranked = sorted(scores_cache.items(), key=lambda x: x[1], reverse=True)
        top_tickers = [t for t, _ in ranked[:top_n]]

        # Entry: in top N and not already tracked
        if not lots and ticker in top_tickers:
            quantity = max(1, int(initial_capital * position_size_pct / current_price))
            tracked[ticker] = [{
                "entry_price": current_price,
                "entry_idx": bar_count,
                "peak_price": current_price,
            }]
            return {
                "action": "buy",
                "ticker": ticker,
                "limit_price": current_price,
                "quantity": quantity,
                "sector": sector_map.get(ticker, "Unknown"),
                "signals": {
                    "strategy": "quality_value",
                    "quality_score": round(score, 3),
                    "rank": top_tickers.index(ticker) + 1,
                },
            }

        return None

    return signals_fn
```

**Step 4: Run tests**

Run: `pytest tests/backtest/test_quality_value_signals.py -v`
Expected: PASS

**Step 5: Run full suite**

Run: `pytest tests/backtest/ -v`
Expected: All pass

**Step 6: Commit**

```bash
git add scripts/run_backtest.py tests/backtest/test_quality_value_signals.py
git commit -m "feat: add quality value signal function with fundamentals-based ranking"
```

---

### Task 4: Add make_earnings_drift_signals_fn()

**Files:**
- Modify: `scripts/run_backtest.py`
- Create: `tests/backtest/test_earnings_drift_signals.py`

**Step 1: Write the failing test**

Create `tests/backtest/test_earnings_drift_signals.py`:

```python
from __future__ import annotations

from datetime import date, timedelta

from scripts.run_backtest import make_earnings_drift_signals_fn


def _make_earnings_lookup():
    """Create an earnings lookup with synthetic data."""
    from scripts.fetch_earnings import build_earnings_lookup

    cache = {
        "AAPL": [
            {"earnings_date": "2024-02-01", "actual_eps": 2.20,
             "estimate_eps": 2.00, "surprise_pct": 10.0},
        ],
        "MSFT": [
            {"earnings_date": "2024-02-01", "actual_eps": 2.00,
             "estimate_eps": 2.10, "surprise_pct": -4.76},
        ],
    }
    return build_earnings_lookup(cache, window_days=2)


def _make_bars(ticker: str, days: int = 100) -> list[dict]:
    bars = []
    price = 150.0
    for d in range(days):
        bars.append({
            "date": date(2024, 1, 1) + timedelta(days=d),
            "open": price, "high": price + 1, "low": price - 1,
            "close": price, "volume": 50000,
        })
    return bars


def test_earnings_drift_buys_on_positive_surprise():
    """Buys within 2 days of a >5% earnings beat."""
    earnings_lookup = _make_earnings_lookup()

    signals_fn = make_earnings_drift_signals_fn(
        earnings_lookup=earnings_lookup,
        surprise_threshold_pct=5.0,
        position_size_pct=0.08,
        initial_capital=20_000,
    )

    # AAPL had 10% surprise on 2024-02-01 (day index 31)
    bars = _make_bars("AAPL")
    result = signals_fn("AAPL", bars[:32])  # bars up to Feb 1
    assert result is not None
    assert result["action"] == "buy"
    assert result["signals"]["strategy"] == "earnings_drift"
    assert result["signals"]["surprise_pct"] == 10.0


def test_earnings_drift_skips_negative_surprise():
    """Does not buy on earnings miss."""
    earnings_lookup = _make_earnings_lookup()

    signals_fn = make_earnings_drift_signals_fn(
        earnings_lookup=earnings_lookup,
        surprise_threshold_pct=5.0,
        position_size_pct=0.08,
        initial_capital=20_000,
    )

    # MSFT had -4.76% surprise (miss)
    bars = _make_bars("MSFT")
    result = signals_fn("MSFT", bars[:32])
    assert result is None


def test_earnings_drift_exits_after_hold_period():
    """Exits after max_hold_days."""
    earnings_lookup = _make_earnings_lookup()

    signals_fn = make_earnings_drift_signals_fn(
        earnings_lookup=earnings_lookup,
        surprise_threshold_pct=5.0,
        max_hold_days=20,
        position_size_pct=0.08,
        initial_capital=20_000,
    )

    bars = _make_bars("AAPL")

    # Enter on earnings day
    entry = signals_fn("AAPL", bars[:32])
    assert entry is not None and entry["action"] == "buy"

    # Hold for 21 more bars (exceeds max_hold_days=20)
    sell_found = False
    for end_idx in range(33, 55):
        result = signals_fn("AAPL", bars[:end_idx])
        if result and result["action"] == "sell":
            assert result["exit_reason"] == "time_exit"
            sell_found = True
            break

    assert sell_found, "Expected time_exit sell signal"
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/backtest/test_earnings_drift_signals.py -v`
Expected: FAIL with ImportError

**Step 3: Implement make_earnings_drift_signals_fn()**

Add to `scripts/run_backtest.py` after `make_quality_value_signals_fn()`:

```python
def make_earnings_drift_signals_fn(
    earnings_lookup: Callable[[str, date], dict | None],
    surprise_threshold_pct: float = 5.0,
    max_hold_days: int = 20,
    position_size_pct: float = 0.08,
    initial_capital: float = 100_000,
    trailing_stop_pct: float = 0.06,
):
    """Create an earnings drift (PEAD) signal function.

    Entry: Earnings surprise > threshold (beat estimate by N%+), within 2 days of announcement.
    Exit: Fixed hold period (20 trading days) or trailing stop 6%.
    """
    tracked: dict[str, dict] = {}  # ticker -> {entry_idx, entry_price, peak_price}

    def signals_fn(ticker: str, bars: list[dict]) -> dict | None:
        if len(bars) < 5:
            return None

        current_price = bars[-1]["close"]
        current_date = bars[-1]["date"]
        bar_count = len(bars)
        lot = tracked.get(ticker)

        # Exit logic
        if lot is not None:
            lot["peak_price"] = max(lot["peak_price"], current_price)
            bars_held = bar_count - lot["entry_idx"]

            # Time exit
            if bars_held >= max_hold_days:
                tracked.pop(ticker, None)
                return {
                    "action": "sell",
                    "ticker": ticker,
                    "limit_price": current_price,
                    "quantity": 0,
                    "sector": "Unknown",
                    "exit_reason": "time_exit",
                }

            # Trailing stop
            peak = lot["peak_price"]
            entry = lot["entry_price"]
            if peak > entry and (peak - current_price) / peak >= trailing_stop_pct:
                tracked.pop(ticker, None)
                return {
                    "action": "sell",
                    "ticker": ticker,
                    "limit_price": current_price,
                    "quantity": 0,
                    "sector": "Unknown",
                    "exit_reason": "trailing_stop",
                }

            return None

        # Entry logic: check for recent earnings event
        event = earnings_lookup(ticker, current_date)
        if event is None:
            return None

        surprise = event.get("surprise_pct", 0.0)
        if surprise < surprise_threshold_pct:
            return None

        quantity = max(1, int(initial_capital * position_size_pct / current_price))
        tracked[ticker] = {
            "entry_price": current_price,
            "entry_idx": bar_count,
            "peak_price": current_price,
        }
        return {
            "action": "buy",
            "ticker": ticker,
            "limit_price": current_price,
            "quantity": quantity,
            "sector": "Unknown",
            "signals": {
                "strategy": "earnings_drift",
                "surprise_pct": surprise,
                "actual_eps": event.get("actual_eps"),
                "estimate_eps": event.get("estimate_eps"),
            },
        }

    return signals_fn
```

**Step 4: Run tests**

Run: `pytest tests/backtest/test_earnings_drift_signals.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add scripts/run_backtest.py tests/backtest/test_earnings_drift_signals.py
git commit -m "feat: add earnings drift (PEAD) signal function"
```

---

### Task 5: Wire into main() and add data loading

**Files:**
- Modify: `scripts/run_backtest.py`
- Modify: `tests/backtest/test_multi_portfolio.py`

**Step 1: Add test**

Append to `tests/backtest/test_multi_portfolio.py`:

```python
def test_seven_portfolios_aggregate_correctly():
    """Seven portfolios with no-op signals produce correct aggregate starting capital."""
    from datetime import date

    from backtest.runner import BacktestRunner
    from backtest.simulator import SimulatedExecutor
    from scripts.run_backtest import PortfolioConfig, compute_aggregate_metrics
    from services.risk_management.engine import RiskEngine

    bars = {
        "AAPL": [
            {"date": date(2024, 1, d), "open": 150.0, "high": 152.0,
             "low": 149.0, "close": 151.0, "volume": 1000}
            for d in range(1, 6)
        ],
    }
    executor = SimulatedExecutor(slippage_bps=0, commission_per_share=0)

    allocations = {
        "mr": 12_000, "mom": 18_000, "sector": 12_000,
        "quality": 12_000, "earnings": 15_000,
        "st_mr": 10_000, "thematic": 11_000, "tail_risk": 10_000,
    }
    configs = {
        name: PortfolioConfig(name, capital, lambda t, b: None, RiskEngine())
        for name, capital in allocations.items()
    }

    results = {}
    for name, pc in configs.items():
        runner = BacktestRunner(executor=executor, initial_capital=pc.capital)
        results[name] = runner.run(bars, pc.signals_fn, pc.risk_engine)

    agg = compute_aggregate_metrics(results, configs)
    assert agg["portfolio_values"][0] == 100_000
    assert len(results) == 8
```

**Step 2: Update main() in run_backtest.py**

Make these changes:

a) Add import at top of file (after existing imports):
```python
from scripts.fetch_fundamentals import load_fundamentals_cache, build_fundamentals_lookup, SECTOR_MAP
from scripts.fetch_earnings import load_earnings_cache, build_earnings_lookup
```

b) Update universe in main():
```python
    all_tickers = get_union_universe([
        "mean_reversion", "momentum", "sector_rotation",
        "short_term_mr", "thematic_momentum",
        "quality_value", "earnings_drift",
    ])
```

c) After bars_by_ticker is loaded, add fundamentals/earnings loading:
```python
    # Load cached fundamentals and earnings data
    fundamentals_cache = load_fundamentals_cache("data/cache/fundamentals.json")
    earnings_cache = load_earnings_cache("data/cache/earnings.json")
    fundamentals_lookup = build_fundamentals_lookup(fundamentals_cache)
    earnings_lookup = build_earnings_lookup(earnings_cache, window_days=2)

    if fundamentals_cache:
        print(f"  Loaded fundamentals for {len(fundamentals_cache)} tickers")
    else:
        print("  WARNING: No fundamentals cache found. Run: python scripts/fetch_fundamentals.py")

    if earnings_cache:
        print(f"  Loaded earnings for {len(earnings_cache)} tickers")
    else:
        print("  WARNING: No earnings cache found. Run: python scripts/fetch_earnings.py")
```

d) Add signal functions after thematic_signals_fn:
```python
    qv_signals_fn = make_quality_value_signals_fn(
        fundamentals_lookup=fundamentals_lookup,
        sector_map=SECTOR_MAP,
        top_n=15,
        position_size_pct=0.10,
        initial_capital=args.capital * 0.12,
        trailing_stop_pct=0.12,
    )
    ed_signals_fn = make_earnings_drift_signals_fn(
        earnings_lookup=earnings_lookup,
        surprise_threshold_pct=5.0,
        max_hold_days=20,
        position_size_pct=0.08,
        initial_capital=args.capital * 0.15,
        trailing_stop_pct=0.06,
    )
```

e) Update portfolio allocations to 7 strategies (matching design doc ratios, normalized to sum to 100% without tail-risk hedge):

| Strategy | Design Doc | Normalized (w/o tail-risk) |
|---|---|---|
| Mean-Reversion | 12% | 14% |
| Momentum | 18% | 20% |
| Sector Rotation | 12% | 14% |
| Quality Value | 12% | 14% |
| Earnings Drift | 15% | 17% |
| Short-Term MR | 10% | 11% |
| Thematic Momentum | 11% | 10% |

Add to portfolios dict:
```python
        "quality_value": PortfolioConfig(
            name="quality_value",
            capital=args.capital * 0.14,
            signals_fn=qv_signals_fn,
            risk_engine=RiskEngine(
                position_entry_limit_pct=10.0,
                sector_concentration_pct=30.0,
                total_exposure_limit_pct=100.0,
                max_lots_per_ticker=1,
            ),
        ),
        "earnings_drift": PortfolioConfig(
            name="earnings_drift",
            capital=args.capital * 0.17,
            signals_fn=ed_signals_fn,
            risk_engine=RiskEngine(
                position_entry_limit_pct=8.0,
                sector_concentration_pct=30.0,
                total_exposure_limit_pct=100.0,
                max_lots_per_ticker=1,
            ),
        ),
```

And adjust existing allocations to match normalized ratios.

**Step 3: Run full test suite**

Run: `pytest tests/backtest/ -v`
Expected: All pass

**Step 4: Commit**

```bash
git add scripts/run_backtest.py tests/backtest/test_multi_portfolio.py
git commit -m "feat: wire quality value and earnings drift into main() with data loading"
```

---

### Task 6: Update documentation

**Files:**
- Modify: `docs/strategy.md`

**Step 1: Update strategy.md**

Update "Current Portfolio Configuration" table to show all 7 active strategies. Add brief descriptions of Quality Value and Earnings Drift strategies. Update the Implementation table with new functions.

**Step 2: Commit**

```bash
git add docs/strategy.md
git commit -m "docs: update strategy.md with 7 active portfolio configurations"
```

---

## Summary

| Task | What | Files | Tests |
|---|---|---|---|
| 1 | Fundamentals cache | `fetch_fundamentals.py`, `test_fundamentals_cache.py` | 3 new |
| 2 | Earnings cache | `fetch_earnings.py`, `test_earnings_cache.py` | 3 new |
| 3 | Quality value signals | `run_backtest.py`, `test_quality_value_signals.py` | 3 new |
| 4 | Earnings drift signals | `run_backtest.py`, `test_earnings_drift_signals.py` | 3 new |
| 5 | Wire into main() | `run_backtest.py`, `test_multi_portfolio.py` | 1 new |
| 6 | Update docs | `docs/strategy.md` | — |

Total: 6 tasks, 6 commits, 13 new tests, 2 new signal functions, 2 data cache modules.
