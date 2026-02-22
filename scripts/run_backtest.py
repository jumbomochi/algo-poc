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
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

import numpy as np

from backtest.metrics import BacktestMetrics
from backtest.runner import BacktestResult, BacktestRunner
from backtest.simulator import SimulatedExecutor
from services.risk_management.engine import RiskEngine


@dataclass
class PortfolioConfig:
    """Configuration for a single portfolio in a multi-portfolio backtest."""

    name: str
    capital: float
    signals_fn: Callable[[str, list[dict]], dict | None]
    risk_engine: RiskEngine
from services.signal_generation.technical import (
    SupportProximitySignal,
    SupportStrengthSignal,
    SupportTrendSignal,
    RSISignal,
    VolumeSignal,
    find_support_levels,
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

# Inverse ETFs for bear market plays
BEAR_TICKERS = {"SH", "PSQ"}  # SH = inverse S&P 500, PSQ = inverse NASDAQ-100

# Inverse/defensive ETFs for tail-risk hedge
DEFENSIVE_TICKERS = ["SH", "PSQ", "SDS", "TLT", "GLD"]

# SPDR sector ETFs
SECTOR_ETFS = [
    "XLK", "XLE", "XLF", "XLV", "XLY", "XLP",
    "XLI", "XLB", "XLU", "XLRE", "XLC",
]

# Thematic ETFs
THEMATIC_ETFS = [
    "ARKK", "TAN", "HACK", "BOTZ", "LIT", "CIBR", "SKYY", "DRIV",
    "FINX", "GAMR", "HERO", "IDRV", "CLOU", "WCLD", "SNSR", "PRNT",
    "IZRL", "GNOM", "ARKG", "ARKQ", "ARKW", "ARKF", "ICLN", "QCLN", "PBW",
]

# S&P 500 extended (top 100 for short-term MR)
SP500_TOP100 = SP500_TOP50 + [
    "CAT", "MS", "NEE", "LOW", "UPS", "SPGI", "RTX", "HON", "ELV",
    "BLK", "SYK", "BKNG", "MDLZ", "ADP", "VRTX", "SCHW", "GILD",
    "AMT", "REGN", "LRCX", "PANW", "BSX", "CB", "MMC", "KLAC",
    "TMUS", "SHW", "SO", "EQIX", "MO", "PGR", "ZTS", "CME",
    "CI", "DUK", "ICE", "SNPS", "CL", "AON", "MCO", "WM",
    "CDNS", "TGT", "BDX", "NOC", "APH", "ITW", "FI", "HUM",
]

# Per-strategy ticker universes
UNIVERSE_REGISTRY: dict[str, list[str]] = {
    "mean_reversion": SP500_TOP50,
    "momentum": SP500_TOP50 + [t for t in sorted(BEAR_TICKERS) if t not in SP500_TOP50],
    "sector_rotation": SECTOR_ETFS,
    "quality_value": SP500_TOP100,
    "earnings_drift": SP500_TOP100,
    "short_term_mr": SP500_TOP100,
    "thematic_momentum": THEMATIC_ETFS,
    "tail_risk_hedge": DEFENSIVE_TICKERS,
}


def get_union_universe(strategy_names: list[str]) -> list[str]:
    """Return deduplicated union of tickers across the given strategies."""
    seen: set[str] = set()
    result: list[str] = []
    for name in strategy_names:
        for ticker in UNIVERSE_REGISTRY[name]:
            if ticker not in seen:
                seen.add(ticker)
                result.append(ticker)
    return result


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


REGIME_PARAMS = {
    "bull": {"trailing_stop_pct": 0.15, "max_loss_pct": 0.10},
    "neutral": {"trailing_stop_pct": 0.12, "max_loss_pct": 0.08},
    "bear": {"trailing_stop_pct": 0.08, "max_loss_pct": 0.05},
}


def compute_regime_by_date(
    bars_by_ticker: dict[str, list[dict]],
    ma_period: int = 200,
    bull_threshold: float = 0.60,
    bear_threshold: float = 0.40,
) -> dict:
    """Compute market regime for each date based on breadth.

    Bull: >60% of stocks above their 200-day MA.
    Bear: <40% above their 200-day MA.
    Neutral: 40-60%.
    """
    above_ma: dict[Any, list[bool]] = {}
    for ticker, bars in bars_by_ticker.items():
        if len(bars) < ma_period:
            continue
        closes = [b["close"] for b in bars]
        dates = [b["date"] for b in bars]
        ma = np.convolve(closes, np.ones(ma_period) / ma_period, mode="valid")
        for i, ma_val in enumerate(ma):
            d = dates[ma_period - 1 + i]
            if d not in above_ma:
                above_ma[d] = []
            above_ma[d].append(closes[ma_period - 1 + i] > ma_val)

    regime_by_date = {}
    for d, above_list in above_ma.items():
        breadth = sum(above_list) / len(above_list)
        if breadth > bull_threshold:
            regime_by_date[d] = "bull"
        elif breadth < bear_threshold:
            regime_by_date[d] = "bear"
        else:
            regime_by_date[d] = "neutral"

    return regime_by_date


def make_signals_fn(
    position_size_pct: float = 0.07,
    initial_capital: float = 100_000,
    trailing_stop_pct: float = 0.10,
    max_lots: int = 2,
    regime_by_date: dict | None = None,
):
    """Create a signal function implementing mean-reversion on large-cap support levels.

    Entry (first lot): support proximity + RSI < 35 + volume > 1.5x avg + rising supports.
    Entry (add-on lot): in profit + new support signal + RSI < 40 + volume confirmation.
    Exit: regime-adaptive trailing stop from peak. No max loss (mean-reversion buys at
    support — a further drop is expected before recovery).
    """
    proximity_signal = SupportProximitySignal()
    strength_signal = SupportStrengthSignal()
    trend_signal = SupportTrendSignal()
    rsi_signal = RSISignal()
    volume_signal = VolumeSignal()

    # Per-ticker lot tracking: ticker -> list of {entry_price, entry_idx, peak_price}
    tracked: dict[str, list[dict]] = {}

    def signals_fn(ticker: str, bars: list[dict]) -> dict | None:
        if len(bars) < 60:
            return None

        current_price = bars[-1]["close"]
        current_date = bars[-1]["date"]
        bar_count = len(bars)
        lots = tracked.get(ticker, [])

        # Determine regime-adjusted trailing stop
        if regime_by_date:
            regime = regime_by_date.get(current_date, "neutral")
            effective_trailing = REGIME_PARAMS[regime]["trailing_stop_pct"]
        else:
            effective_trailing = trailing_stop_pct

        # === Exit logic: trailing stop only (no max loss for mean-reversion) ===
        if lots:
            # Update peak prices
            for lot in lots:
                lot["peak_price"] = max(lot["peak_price"], current_price)

            should_sell = False
            exit_reason = "unknown"

            for lot in lots:
                peak = lot["peak_price"]
                entry = lot["entry_price"]

                # Trailing stop: only activates after position has been profitable.
                if peak > entry and (peak - current_price) / peak >= effective_trailing:
                    should_sell = True
                    exit_reason = "trailing_stop"
                    break

            if should_sell:
                tracked.pop(ticker, None)
                return {
                    "action": "sell",
                    "ticker": ticker,
                    "limit_price": current_price,
                    "quantity": 0,
                    "sector": "Unknown",
                    "exit_reason": exit_reason,
                }

        # === Compute signals ===
        data = _build_data(bars)
        try:
            proximity = proximity_signal.compute(data)
            strength = strength_signal.compute(data)
            trend = trend_signal.compute(data)
            rsi = rsi_signal.compute(data)
            volume = volume_signal.compute(data)
        except Exception:
            return None

        signal_snapshot = {
            "proximity": {"value": proximity.value, "confidence": proximity.confidence},
            "strength": {"value": strength.value, "confidence": strength.confidence},
            "trend": {"value": trend.value, "confidence": trend.confidence},
            "rsi": {"value": rsi.value, "confidence": rsi.confidence},
            "volume": {"value": volume.value, "confidence": volume.confidence},
        }

        # === Add-on entry (already have lots, in profit) ===
        if lots and len(lots) < max_lots:
            avg_entry = sum(l["entry_price"] for l in lots) / len(lots)
            in_profit = current_price > avg_entry

            if (
                in_profit
                and proximity.value > 0.8
                and strength.confidence > 0.7
                and rsi.value > 0.3  # RSI < 35 (relaxed vs first entry)
                and volume.value > 0.5  # volume > 2x avg
                and trend.value > 0.0
            ):
                support_levels = find_support_levels(data)
                limit_price = support_levels[0] if support_levels else current_price
                quantity = max(1, int(initial_capital * position_size_pct / current_price))
                lots.append({
                    "entry_price": current_price,
                    "entry_idx": bar_count,
                    "peak_price": current_price,
                })
                return {
                    "action": "buy",
                    "ticker": ticker,
                    "limit_price": limit_price,
                    "quantity": quantity,
                    "sector": "Unknown",
                    "signals": signal_snapshot,
                }

        # === First entry (no lots) ===
        if not lots:
            if (
                proximity.value > 0.8
                and strength.confidence > 0.7
                and rsi.value > 0.4  # RSI < 30 (deeply oversold)
                and volume.value > 0.5  # volume > 2x avg
                and trend.value > 0.0  # supports must be rising
            ):
                support_levels = find_support_levels(data)
                limit_price = support_levels[0] if support_levels else current_price
                quantity = max(1, int(initial_capital * position_size_pct / current_price))
                tracked[ticker] = [{
                    "entry_price": current_price,
                    "entry_idx": bar_count,
                    "peak_price": current_price,
                }]
                return {
                    "action": "buy",
                    "ticker": ticker,
                    "limit_price": limit_price,
                    "quantity": quantity,
                    "sector": "Unknown",
                    "signals": signal_snapshot,
                }

        return None

    return signals_fn


def make_momentum_signals_fn(
    bars_by_ticker: dict[str, list[dict]],
    top_n: int = 5,
    lookback_days: int = 126,
    position_size_pct: float = 0.07,
    initial_capital: float = 100_000,
    trailing_stop_pct: float = 0.10,
    max_loss_pct: float = 0.08,
    max_lots: int = 2,
    regime_by_date: dict | None = None,
    bear_tickers: set[str] | None = None,
):
    """Create a momentum signal function based on 6-month relative strength.

    Ranks all tickers by their return over the lookback period.
    Buys the top N performers. Exits via trailing stop + max loss.
    In bear markets, inverse ETFs (bear_tickers) naturally rank high and get selected.
    When regime changes away from bear, inverse ETFs are force-exited.
    """
    # Pre-compute date -> {ticker: close_price} for ranking
    price_by_date: dict[Any, dict[str, float]] = {}
    for ticker, bars in bars_by_ticker.items():
        for bar in bars:
            d = bar["date"]
            if d not in price_by_date:
                price_by_date[d] = {}
            price_by_date[d][ticker] = bar["close"]

    sorted_dates = sorted(price_by_date.keys())

    # Pre-compute date -> list of top N tickers ranked by return descending
    rankings_by_date: dict[Any, list[str]] = {}
    for i, d in enumerate(sorted_dates):
        if i < lookback_days:
            continue
        past_date = sorted_dates[i - lookback_days]
        past_prices = price_by_date.get(past_date, {})
        current_prices = price_by_date[d]

        returns = []
        for ticker in current_prices:
            if ticker in past_prices and past_prices[ticker] > 0:
                ret = (current_prices[ticker] - past_prices[ticker]) / past_prices[ticker]
                returns.append((ticker, ret))

        returns.sort(key=lambda x: x[1], reverse=True)
        rankings_by_date[d] = [t for t, _ in returns[:top_n]]

    # Per-ticker lot tracking for exits
    tracked: dict[str, list[dict]] = {}

    def signals_fn(ticker: str, bars: list[dict]) -> dict | None:
        if len(bars) < lookback_days + 1:
            return None

        current_bar = bars[-1]
        current_price = current_bar["close"]
        current_date = current_bar["date"]
        bar_count = len(bars)
        lots = tracked.get(ticker, [])

        # Determine regime-adjusted parameters
        if regime_by_date:
            regime = regime_by_date.get(current_date, "neutral")
            effective_trailing = REGIME_PARAMS[regime]["trailing_stop_pct"]
            effective_max_loss = REGIME_PARAMS[regime]["max_loss_pct"]
        else:
            regime = "neutral"
            effective_trailing = trailing_stop_pct
            effective_max_loss = max_loss_pct

        # === Exit logic: trailing stop + max loss + regime-change exit ===
        if lots:
            # Force-exit inverse ETFs when regime turns non-bear
            is_bear_ticker = bear_tickers and ticker in bear_tickers
            if is_bear_ticker and regime != "bear":
                tracked.pop(ticker, None)
                return {
                    "action": "sell",
                    "ticker": ticker,
                    "limit_price": current_price,
                    "quantity": 0,
                    "sector": "Unknown",
                    "exit_reason": "regime_change",
                }

            for lot in lots:
                lot["peak_price"] = max(lot["peak_price"], current_price)

            should_sell = False
            exit_reason = "trailing_stop"
            for lot in lots:
                peak = lot["peak_price"]
                entry = lot["entry_price"]
                if peak > entry and (peak - current_price) / peak >= effective_trailing:
                    should_sell = True
                    exit_reason = "trailing_stop"
                    break
                if (entry - current_price) / entry >= effective_max_loss:
                    should_sell = True
                    exit_reason = "max_loss"
                    break

            if should_sell:
                tracked.pop(ticker, None)
                return {
                    "action": "sell",
                    "ticker": ticker,
                    "limit_price": current_price,
                    "quantity": 0,
                    "sector": "Unknown",
                    "exit_reason": exit_reason,
                }

        # === Entry logic: buy if in top N and not already tracked ===
        top_tickers = rankings_by_date.get(current_date, [])
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
                "sector": "Unknown",
                "signals": {
                    "strategy": "momentum",
                    "rank": top_tickers.index(ticker) + 1,
                    "lookback_days": lookback_days,
                },
            }

        return None

    return signals_fn


def make_combined_signals_fn(
    mean_reversion_fn: Callable[[str, list[dict]], dict | None],
    momentum_fn: Callable[[str, list[dict]], dict | None],
) -> Callable[[str, list[dict]], dict | None]:
    """Compose mean-reversion and momentum signal functions.

    Priority: sell signals first, then mean-reversion buys, then momentum buys.
    """
    def combined_fn(ticker: str, bars: list[dict]) -> dict | None:
        mr_signal = mean_reversion_fn(ticker, bars)
        mom_signal = momentum_fn(ticker, bars)

        # Sell signals take highest priority from either strategy
        if mr_signal and mr_signal.get("action") == "sell":
            return mr_signal
        if mom_signal and mom_signal.get("action") == "sell":
            return mom_signal

        # Buy: mean-reversion first (more selective), then momentum
        if mr_signal and mr_signal.get("action") == "buy":
            return mr_signal
        if mom_signal and mom_signal.get("action") == "buy":
            return mom_signal

        return None

    return combined_fn


def _build_data(bars: list[dict]) -> dict[str, list]:
    """Build the data dict expected by signal classes."""
    return {
        "open": [b["open"] for b in bars],
        "high": [b["high"] for b in bars],
        "low": [b["low"] for b in bars],
        "close": [b["close"] for b in bars],
        "volume": [b["volume"] for b in bars],
    }


def make_sector_rotation_signals_fn(
    bars_by_ticker: dict[str, list[dict]],
    top_n: int = 3,
    lookback_days: int = 63,
    position_size_pct: float = 0.20,
    initial_capital: float = 100_000,
    trailing_stop_pct: float = 0.08,
    regime_by_date: dict | None = None,
):
    """Create a sector rotation signal function.

    Ranks sector ETFs by 3-month return and buys the top N.
    In bear regime, rotates to defensive sectors only (XLU, XLP, XLV).
    Exits via trailing stop or when sector drops out of top N.
    """
    defensive_sectors = {"XLU", "XLP", "XLV"}

    # Pre-compute date -> {ticker: close_price} for ranking
    price_by_date: dict[Any, dict[str, float]] = {}
    for ticker, bars in bars_by_ticker.items():
        for bar in bars:
            d = bar["date"]
            if d not in price_by_date:
                price_by_date[d] = {}
            price_by_date[d][ticker] = bar["close"]

    sorted_dates = sorted(price_by_date.keys())

    # Pre-compute date -> list of top N tickers ranked by return
    rankings_by_date: dict[Any, list[str]] = {}
    for i, d in enumerate(sorted_dates):
        if i < lookback_days:
            continue
        past_date = sorted_dates[i - lookback_days]
        past_prices = price_by_date.get(past_date, {})
        current_prices = price_by_date[d]

        returns = []
        for ticker in current_prices:
            if ticker in past_prices and past_prices[ticker] > 0:
                ret = (current_prices[ticker] - past_prices[ticker]) / past_prices[ticker]
                returns.append((ticker, ret))

        # In bear regime, only consider defensive sectors
        regime = "neutral"
        if regime_by_date:
            regime = regime_by_date.get(d, "neutral")

        if regime == "bear":
            returns = [(t, r) for t, r in returns if t in defensive_sectors]

        returns.sort(key=lambda x: x[1], reverse=True)
        rankings_by_date[d] = [t for t, _ in returns[:top_n]]

    tracked: dict[str, list[dict]] = {}

    def signals_fn(ticker: str, bars: list[dict]) -> dict | None:
        if len(bars) < lookback_days + 1:
            return None

        current_price = bars[-1]["close"]
        current_date = bars[-1]["date"]
        bar_count = len(bars)
        lots = tracked.get(ticker, [])

        # Exit: trailing stop
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
                        "sector": "Unknown",
                        "exit_reason": "trailing_stop",
                    }

        # Entry: buy if in top N and not already tracked
        top_tickers = rankings_by_date.get(current_date, [])
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
                "sector": "Unknown",
                "signals": {
                    "strategy": "sector_rotation",
                    "rank": top_tickers.index(ticker) + 1,
                    "lookback_days": lookback_days,
                },
            }

        return None

    return signals_fn


def make_short_term_mr_signals_fn(
    position_size_pct: float = 0.08,
    initial_capital: float = 100_000,
    max_hold_days: int = 5,
    rsi_period: int = 2,
    rsi_entry_threshold: float = 0.8,
    bb_period: int = 20,
    bb_num_std: float = 2.0,
):
    """Create a short-term mean-reversion signal function.

    Entry: RSI(2) < 10 AND price touches lower Bollinger Band AND volume > 1.5x avg.
    Exit: RSI(2) > 70 OR max_hold_days elapsed (whichever first). No trailing stop.
    """
    from services.signal_generation.technical import BollingerBandSignal

    rsi_signal = RSISignal(period=rsi_period)
    bb_signal = BollingerBandSignal(period=bb_period, num_std=bb_num_std)
    volume_signal = VolumeSignal()

    tracked: dict[str, dict] = {}  # ticker -> {entry_idx, entry_price}

    def signals_fn(ticker: str, bars: list[dict]) -> dict | None:
        min_bars = max(bb_period + 1, 25)
        if len(bars) < min_bars:
            return None

        current_price = bars[-1]["close"]
        bar_count = len(bars)
        lot = tracked.get(ticker)

        # Exit logic
        if lot is not None:
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

            # RSI recovery exit
            data = _build_data(bars)
            rsi = rsi_signal.compute(data)
            if rsi.value < -0.4:  # RSI(2) > 70
                tracked.pop(ticker, None)
                return {
                    "action": "sell",
                    "ticker": ticker,
                    "limit_price": current_price,
                    "quantity": 0,
                    "sector": "Unknown",
                    "exit_reason": "rsi_recovery",
                }

            return None

        # Entry logic
        data = _build_data(bars)
        try:
            rsi = rsi_signal.compute(data)
            bb = bb_signal.compute(data)
            volume = volume_signal.compute(data)
        except Exception:
            return None

        # RSI(2) < 10 maps to rsi.value > 0.8
        # BB touch: bb.value > 0.5 means price is near/below lower band
        # Volume > 1.5x avg: volume.value > 0.25
        if (
            rsi.value > rsi_entry_threshold
            and bb.value > 0.5
            and volume.value > 0.25
        ):
            quantity = max(1, int(initial_capital * position_size_pct / current_price))
            tracked[ticker] = {
                "entry_price": current_price,
                "entry_idx": bar_count,
            }
            return {
                "action": "buy",
                "ticker": ticker,
                "limit_price": current_price,
                "quantity": quantity,
                "sector": "Unknown",
                "signals": {
                    "strategy": "short_term_mr",
                    "rsi_2": rsi.value,
                    "bb": bb.value,
                    "volume": volume.value,
                },
            }

        return None

    return signals_fn


def compute_aggregate_metrics(
    results: dict[str, BacktestResult],
    portfolio_configs: dict[str, PortfolioConfig],
) -> dict:
    """Aggregate metrics across multiple portfolio backtest results.

    Sums portfolio_values element-wise, pools all trades (tagged with portfolio
    name), and computes combined metrics from the aggregate equity curve.

    Returns dict with keys: portfolio_values, trades, dates, metrics.
    """
    if not results:
        return {
            "portfolio_values": [],
            "trades": [],
            "dates": [],
            "metrics": {},
        }

    # All portfolios share the same bar data, so dates are identical.
    # Use the first result's dates as reference.
    first_result = next(iter(results.values()))
    dates = first_result.dates

    # Sum portfolio_values element-wise across all portfolios
    combined_values = [0.0] * len(first_result.portfolio_values)
    for result in results.values():
        for i, v in enumerate(result.portfolio_values):
            combined_values[i] += v

    # Pool all trades, tagging each with its portfolio name
    combined_trades: list[dict] = []
    for name, result in results.items():
        for trade in result.trades:
            tagged = dict(trade)
            tagged["portfolio"] = name
            combined_trades.append(tagged)

    # Compute metrics from the combined equity curve and pooled trades
    metrics = BacktestMetrics.compute(
        portfolio_values=combined_values,
        trades=combined_trades,
    )

    return {
        "portfolio_values": combined_values,
        "trades": combined_trades,
        "dates": dates,
        "metrics": metrics,
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


def print_multi_portfolio_results(
    results: dict[str, BacktestResult],
    portfolio_configs: dict[str, PortfolioConfig],
    aggregate: dict,
    elapsed_seconds: float,
) -> None:
    """Print multi-portfolio backtest results."""
    print("\n" + "=" * 70)
    print("  MULTI-PORTFOLIO BACKTEST RESULTS")
    print("=" * 70)

    # Per-portfolio summary
    for name, result in results.items():
        m = result.metrics
        config = portfolio_configs[name]
        values = result.portfolio_values
        pnl = values[-1] - values[0] if len(values) > 1 else 0.0
        print(f"\n  --- {name} (${config.capital:,.0f} capital) ---")
        print(f"    Total Return:        {m['total_return']:>10.2%}")
        print(f"    Sharpe Ratio:        {m['sharpe_ratio']:>10.2f}")
        print(f"    Max Drawdown:        {m['max_drawdown']:>10.2%}")
        print(f"    Win Rate:            {m['win_rate']:>10.2%}")
        print(f"    Total Trades:        {m['total_trades']:>10d}")
        print(f"    P&L:                 ${pnl:>+12,.2f}")

    # Aggregate section
    agg_m = aggregate["metrics"]
    agg_values = aggregate["portfolio_values"]
    if agg_m:
        print(f"\n  --- AGGREGATE ---")
        print(f"    Total Return:        {agg_m['total_return']:>10.2%}")
        print(f"    Sharpe Ratio:        {agg_m['sharpe_ratio']:>10.2f}")
        print(f"    Max Drawdown:        {agg_m['max_drawdown']:>10.2%}")
        print(f"    Win Rate:            {agg_m['win_rate']:>10.2%}")
        print(f"    Total Trades:        {agg_m['total_trades']:>10d}")
        if len(agg_values) > 1:
            print(f"    Starting Capital:    ${agg_values[0]:>12,.2f}")
            print(f"    Final Value:         ${agg_values[-1]:>12,.2f}")
            print(f"    P&L:                 ${agg_values[-1] - agg_values[0]:>+12,.2f}")

    # Top winners/losers from pooled trades
    all_trades = aggregate["trades"]
    if all_trades:
        sorted_trades = sorted(all_trades, key=lambda t: t["pnl"], reverse=True)
        print(f"\n  Top 5 Winners (all portfolios):")
        for t in sorted_trades[:5]:
            print(f"    {t['ticker']:>6s}  {t['pnl']:>+10.2f}  "
                  f"[{t['portfolio']}]  ({t['entry_date']} -> {t['exit_date']})")
        print(f"\n  Top 5 Losers (all portfolios):")
        for t in sorted_trades[-5:]:
            print(f"    {t['ticker']:>6s}  {t['pnl']:>+10.2f}  "
                  f"[{t['portfolio']}]  ({t['entry_date']} -> {t['exit_date']})")

    print(f"\n  Runtime:               {elapsed_seconds:>10.1f}s")
    print("=" * 70)


def save_results(
    config: dict,
    trades: list[dict],
    portfolio_values: list[float],
    dates: list,
    metrics: dict,
    bars: dict[str, list[dict]],
    output_dir: str = "output",
) -> str:
    """Serialize backtest output to a timestamped JSON file.

    Creates *output_dir* if it does not already exist, writes a JSON file
    named ``backtest_YYYYMMDD_HHMMSS.json``, and returns the file path.
    """

    def _json_serializer(obj: Any) -> str:
        if isinstance(obj, date):
            return obj.isoformat()
        raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"backtest_{timestamp}.json"
    path = os.path.join(output_dir, filename)

    payload = {
        "config": config,
        "trades": trades,
        "portfolio_values": portfolio_values,
        "dates": dates,
        "metrics": metrics,
        "bars": bars,
    }

    with open(path, "w") as f:
        json.dump(payload, f, indent=2, default=_json_serializer)

    print(f"Results saved to {path}")
    return path


def save_multi_portfolio_results(
    config: dict,
    results: dict[str, BacktestResult],
    portfolio_configs: dict[str, PortfolioConfig],
    aggregate: dict,
    bars: dict[str, list[dict]],
    output_dir: str = "output",
) -> str:
    """Serialize multi-portfolio backtest output to a timestamped JSON file."""

    def _json_serializer(obj: Any) -> str:
        if isinstance(obj, date):
            return obj.isoformat()
        raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"backtest_multi_{timestamp}.json"
    path = os.path.join(output_dir, filename)

    portfolios_payload = {}
    for name, result in results.items():
        pc = portfolio_configs[name]
        portfolios_payload[name] = {
            "config": {"capital": pc.capital},
            "trades": result.trades,
            "portfolio_values": result.portfolio_values,
            "dates": result.dates,
            "metrics": result.metrics,
        }

    payload = {
        "config": config,
        "portfolios": portfolios_payload,
        "aggregate": aggregate,
        "bars": bars,
    }

    with open(path, "w") as f:
        json.dump(payload, f, indent=2, default=_json_serializer)

    print(f"Results saved to {path}")
    return path


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
    parser.add_argument("--output-dir", default="output",
                        help="Directory for output files (default: output)")
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
    all_tickers = get_union_universe(["mean_reversion", "momentum"])
    print(f"Step 1: Fetching historical data from IB Gateway ({len(all_tickers)} tickers)...")
    bars_by_ticker = fetch_bars_from_ib(
        tickers=all_tickers,
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

    # Build portfolio configurations
    mr_signals_fn = make_signals_fn(
        position_size_pct=0.12,
        initial_capital=args.capital,
        trailing_stop_pct=0.10,
    )
    mom_signals_fn = make_momentum_signals_fn(
        bars_by_ticker=bars_by_ticker,
        top_n=5,
        lookback_days=126,
        position_size_pct=0.12,
        initial_capital=args.capital,
        trailing_stop_pct=0.10,
        bear_tickers=BEAR_TICKERS,
    )
    portfolios: dict[str, PortfolioConfig] = {
        "mean_reversion": PortfolioConfig(
            name="mean_reversion",
            capital=args.capital * 0.40,
            signals_fn=mr_signals_fn,
            risk_engine=RiskEngine(
                position_entry_limit_pct=15.0,
                sector_concentration_pct=30.0,
                total_exposure_limit_pct=120.0,
                max_lots_per_ticker=2,
            ),
        ),
        "momentum": PortfolioConfig(
            name="momentum",
            capital=args.capital * 0.60,
            signals_fn=mom_signals_fn,
            risk_engine=RiskEngine(
                position_entry_limit_pct=12.0,
                sector_concentration_pct=30.0,
                total_exposure_limit_pct=150.0,
                max_lots_per_ticker=1,
            ),
        ),
    }

    # 3. Run backtest for each portfolio
    print(f"Step 3: Running backtest ({len(portfolios)} portfolio(s))...")
    t0 = time.time()
    results: dict[str, BacktestResult] = {}
    for name, pc in portfolios.items():
        print(f"  Running portfolio '{name}' (${pc.capital:,.0f})...")
        runner = BacktestRunner(executor=executor, initial_capital=pc.capital)
        results[name] = runner.run(bars_by_ticker, pc.signals_fn, pc.risk_engine)
    elapsed = time.time() - t0

    # 4. Print results
    if len(portfolios) == 1:
        # Single portfolio: backward-compatible output
        result = next(iter(results.values()))
        print_results(result, elapsed)
    else:
        aggregate = compute_aggregate_metrics(results, portfolios)
        print_multi_portfolio_results(results, portfolios, aggregate, elapsed)

    # 5. Save results to JSON
    print("\nStep 5: Saving results...")
    base_config = {
        "tickers": all_tickers,
        "years": args.years,
        "initial_capital": args.capital,
        "slippage_bps": args.slippage_bps,
        "commission_per_share": args.commission,
        "portfolios": {name: pc.capital for name, pc in portfolios.items()},
    }
    if len(portfolios) == 1:
        result = next(iter(results.values()))
        save_results(
            config=base_config,
            trades=result.trades,
            portfolio_values=result.portfolio_values,
            dates=result.dates,
            metrics=result.metrics,
            bars=bars_by_ticker,
            output_dir=args.output_dir,
        )
    else:
        save_multi_portfolio_results(
            config=base_config,
            results=results,
            portfolio_configs=portfolios,
            aggregate=aggregate,
            bars=bars_by_ticker,
            output_dir=args.output_dir,
        )


if __name__ == "__main__":
    main()
