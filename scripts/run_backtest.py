#!/usr/bin/env python3
"""Run a backtest using historical data from IB Gateway.

Usage:
    python scripts/run_backtest.py [--tickers N] [--years N] [--capital N]

Connects to IB Gateway on paper port (7497), downloads daily OHLCV bars,
runs technical signal analysis, gates entries through the risk engine,
and prints performance metrics.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

import numpy as np

from backtest.runner import BacktestRunner
from backtest.simulator import SimulatedExecutor
from services.risk_management.engine import RiskEngine
from services.signal_generation.technical import (
    SupportProximitySignal,
    SupportStrengthSignal,
    SupportTrendSignal,
)

# Top 50 S&P 500 by market cap (as of early 2025)
SP500_TOP50 = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "BRK B", "LLY",
    "AVGO", "JPM", "TSLA", "UNH", "XOM", "V", "MA", "PG", "COST",
    "JNJ", "HD", "ABBV", "WMT", "NFLX", "CRM", "BAC", "CVX",
    "MRK", "KO", "AMD", "PEP", "TMO", "LIN", "ACN", "CSCO", "ADBE",
    "MCD", "ABT", "WFC", "DHR", "TXN", "PM", "GE", "QCOM", "ISRG",
    "INTU", "CMCSA", "AMAT", "VZ", "NOW", "IBM", "AMGN",
]


def fetch_bars_from_ib(
    tickers: list[str],
    years: int,
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 10,
) -> dict[str, list[dict]]:
    """Fetch daily OHLCV bars from IB Gateway.

    IB limits historical data requests, so we pace them carefully.
    For 10 years of daily bars, we request in 1-year chunks.
    """
    import asyncio as _asyncio

    _asyncio.set_event_loop(_asyncio.new_event_loop())
    from ib_insync import IB, Stock

    ib = IB()
    ib.connect(host, port, clientId=client_id, timeout=15)
    print(f"Connected to IB Gateway. Account: {ib.managedAccounts()}")

    end_date = date.today()
    start_date = end_date - timedelta(days=years * 365)
    bars_by_ticker: dict[str, list[dict]] = {}
    failed: list[str] = []

    for i, ticker in enumerate(tickers):
        print(f"  [{i+1}/{len(tickers)}] Fetching {ticker}...", end=" ", flush=True)
        t0 = time.time()

        try:
            contract = Stock(ticker, "SMART", "USD")
            ib.qualifyContracts(contract)

            # Request in 1-year chunks to stay within IB limits
            all_bars: list[dict] = []
            chunk_end = end_date
            for _ in range(years):
                duration = "1 Y"
                bars = ib.reqHistoricalData(
                    contract,
                    endDateTime=chunk_end.strftime("%Y%m%d 23:59:59"),
                    durationStr=duration,
                    barSizeSetting="1 day",
                    whatToShow="TRADES",
                    useRTH=True,
                    formatDate=1,
                )
                for b in bars:
                    bar_date = b.date if isinstance(b.date, date) else date.fromisoformat(str(b.date))
                    all_bars.append({
                        "date": bar_date,
                        "open": float(b.open),
                        "high": float(b.high),
                        "low": float(b.low),
                        "close": float(b.close),
                        "volume": int(b.volume),
                    })
                # Move back for next chunk
                chunk_end = chunk_end - timedelta(days=365)
                ib.sleep(0.5)  # pace requests

            # Deduplicate and sort by date
            seen_dates = set()
            unique_bars = []
            for bar in sorted(all_bars, key=lambda b: b["date"]):
                if bar["date"] not in seen_dates:
                    seen_dates.add(bar["date"])
                    unique_bars.append(bar)

            # Filter to requested range
            unique_bars = [b for b in unique_bars if b["date"] >= start_date]

            elapsed = time.time() - t0
            print(f"{len(unique_bars)} bars ({elapsed:.1f}s)")
            bars_by_ticker[ticker] = unique_bars

        except Exception as e:
            elapsed = time.time() - t0
            print(f"FAILED ({elapsed:.1f}s): {e}")
            failed.append(ticker)

        # IB pacing: max ~6 historical data requests per 2 seconds
        ib.sleep(1.0)

    ib.disconnect()

    if failed:
        print(f"\nFailed tickers ({len(failed)}): {', '.join(failed)}")
    print(f"Successfully fetched data for {len(bars_by_ticker)} tickers")

    return bars_by_ticker


def make_signals_fn():
    """Create a signal function that uses technical support-level analysis.

    Returns a callable matching BacktestRunner's signals_fn signature:
        signals_fn(ticker, bars_so_far) -> signal dict or None

    Tracks positions internally to enable profit-taking, stop-loss, and
    time-based exits alongside the technical sell signal.
    """
    proximity_signal = SupportProximitySignal()
    strength_signal = SupportStrengthSignal()
    trend_signal = SupportTrendSignal()

    # Internal position tracking for exit logic
    tracked: dict[str, dict] = {}  # ticker -> {entry_price, entry_idx}

    PROFIT_TARGET_PCT = 0.08   # 8% profit target
    STOP_LOSS_PCT = -0.05      # 5% stop loss
    MAX_HOLDING_BARS = 40      # ~2 months max holding

    def signals_fn(ticker: str, bars: list[dict]) -> dict | None:
        # Need at least 60 trading days of data
        if len(bars) < 60:
            return None

        current_price = bars[-1]["close"]
        bar_count = len(bars)
        pos = tracked.get(ticker)

        # === Exit logic (when we're tracking a position) ===
        if pos is not None:
            entry_price = pos["entry_price"]
            holding_bars = bar_count - pos["entry_idx"]
            pct_change = (current_price - entry_price) / entry_price

            should_sell = False

            # 1. Profit target hit
            if pct_change >= PROFIT_TARGET_PCT:
                should_sell = True

            # 2. Stop loss hit
            elif pct_change <= STOP_LOSS_PCT:
                should_sell = True

            # 3. Time-based exit
            elif holding_bars >= MAX_HOLDING_BARS:
                should_sell = True

            # 4. Technical breakdown (more sensitive than original)
            else:
                data = _build_data(bars)
                try:
                    proximity = proximity_signal.compute(data)
                    trend = trend_signal.compute(data)
                    if proximity.value < -0.1 and trend.value < -0.1:
                        should_sell = True
                except Exception:
                    pass

            if should_sell:
                del tracked[ticker]
                return {
                    "action": "sell",
                    "ticker": ticker,
                    "limit_price": current_price,
                    "quantity": 0,  # full position
                    "sector": "Unknown",
                }
            return None

        # === Buy logic (no position tracked) ===
        data = _build_data(bars)
        try:
            proximity = proximity_signal.compute(data)
            strength = strength_signal.compute(data)
            trend = trend_signal.compute(data)
        except Exception:
            return None

        if (
            proximity.value > 0.4
            and proximity.confidence > 0.2
            and strength.value > 0.0
            and strength.confidence > 0.2
            and trend.value > 0.0
        ):
            limit_price = current_price * 1.003
            quantity = max(1, int(5000 / current_price))
            tracked[ticker] = {
                "entry_price": current_price,
                "entry_idx": bar_count,
            }
            return {
                "action": "buy",
                "ticker": ticker,
                "limit_price": limit_price,
                "quantity": quantity,
                "sector": "Unknown",
            }

        return None

    return signals_fn


def _build_data(bars: list[dict]) -> dict[str, list]:
    """Build the data dict expected by signal classes."""
    return {
        "open": [b["open"] for b in bars],
        "high": [b["high"] for b in bars],
        "low": [b["low"] for b in bars],
        "close": [b["close"] for b in bars],
        "volume": [b["volume"] for b in bars],
    }


def print_results(result, elapsed_seconds: float) -> None:
    """Print backtest results in a readable format."""
    m = result.metrics

    print("\n" + "=" * 60)
    print("  BACKTEST RESULTS")
    print("=" * 60)
    print(f"  Total Return:          {m['total_return']:>10.2%}")
    print(f"  Sharpe Ratio:          {m['sharpe_ratio']:>10.2f}")
    print(f"  Max Drawdown:          {m['max_drawdown']:>10.2%}")
    print(f"  Win Rate:              {m['win_rate']:>10.2%}")
    print(f"  Total Trades:          {m['total_trades']:>10d}")
    print(f"  Avg Holding Period:    {m['avg_holding_period_days']:>10.1f} days")
    print(f"  Runtime:               {elapsed_seconds:>10.1f}s")
    print("=" * 60)

    if result.trades:
        # Top winners
        sorted_trades = sorted(result.trades, key=lambda t: t["pnl"], reverse=True)
        print("\n  Top 5 Winners:")
        for t in sorted_trades[:5]:
            print(f"    {t['ticker']:>6s}  {t['pnl']:>+10.2f}  "
                  f"({t['entry_date']} -> {t['exit_date']})")

        print("\n  Top 5 Losers:")
        for t in sorted_trades[-5:]:
            print(f"    {t['ticker']:>6s}  {t['pnl']:>+10.2f}  "
                  f"({t['entry_date']} -> {t['exit_date']})")

    # Portfolio value curve summary
    values = result.portfolio_values
    if len(values) > 1:
        print(f"\n  Starting Capital:      ${values[0]:>12,.2f}")
        print(f"  Final Value:           ${values[-1]:>12,.2f}")
        print(f"  P&L:                   ${values[-1] - values[0]:>+12,.2f}")


def main():
    parser = argparse.ArgumentParser(description="Run algo-poc backtest with IB data")
    parser.add_argument("--tickers", type=int, default=50,
                        help="Number of top S&P 500 tickers (default: 50)")
    parser.add_argument("--years", type=int, default=10,
                        help="Years of historical data (default: 10)")
    parser.add_argument("--capital", type=float, default=100_000,
                        help="Initial capital (default: 100000)")
    parser.add_argument("--slippage-bps", type=int, default=10,
                        help="Slippage in basis points (default: 10)")
    parser.add_argument("--commission", type=float, default=0.005,
                        help="Commission per share (default: 0.005)")
    parser.add_argument("--ib-host", default="127.0.0.1")
    parser.add_argument("--ib-port", type=int, default=7497)
    args = parser.parse_args()

    tickers = SP500_TOP50[:args.tickers]
    print(f"Backtest Configuration:")
    print(f"  Tickers: {len(tickers)} (top S&P 500)")
    print(f"  History:  {args.years} years")
    print(f"  Capital:  ${args.capital:,.0f}")
    print(f"  Slippage: {args.slippage_bps} bps")
    print(f"  Commission: ${args.commission}/share")
    print()

    # 1. Fetch data from IB
    print("Step 1: Fetching historical data from IB Gateway...")
    bars_by_ticker = fetch_bars_from_ib(
        tickers=tickers,
        years=args.years,
        host=args.ib_host,
        port=args.ib_port,
    )

    if not bars_by_ticker:
        print("ERROR: No data fetched. Is IB Gateway running?")
        sys.exit(1)

    total_bars = sum(len(v) for v in bars_by_ticker.values())
    print(f"\nTotal bars loaded: {total_bars:,} across {len(bars_by_ticker)} tickers")

    # 2. Set up backtest components
    print("\nStep 2: Initializing backtest engine...")
    executor = SimulatedExecutor(
        slippage_bps=args.slippage_bps,
        commission_per_share=args.commission,
    )
    runner = BacktestRunner(executor=executor, initial_capital=args.capital)
    risk_engine = RiskEngine(
        position_entry_limit_pct=5.0,
        sector_concentration_pct=20.0,
        total_exposure_limit_pct=150.0,
    )
    signals_fn = make_signals_fn()

    # 3. Run backtest
    print("Step 3: Running backtest...")
    t0 = time.time()
    result = runner.run(bars_by_ticker, signals_fn, risk_engine)
    elapsed = time.time() - t0

    # 4. Print results
    print_results(result, elapsed)


if __name__ == "__main__":
    main()
