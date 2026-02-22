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
import pandas as pd

from backtest.feature_extractor import enrich_trades
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
from scripts.fetch_fundamentals import load_fundamentals_cache, build_fundamentals_lookup, SECTOR_MAP
from scripts.fetch_earnings import load_earnings_cache, build_earnings_lookup
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


def make_thematic_momentum_signals_fn(
    bars_by_ticker: dict[str, list[dict]],
    top_n: int = 8,
    lookback_days: int = 63,
    ma_period: int = 50,
    position_size_pct: float = 0.15,
    initial_capital: float = 100_000,
    trailing_stop_pct: float = 0.10,
    max_loss_pct: float = 0.08,
):
    """Create a thematic momentum signal function.

    Ranks thematic ETFs by 3-month return. Buys top N that are above
    their 50-day MA. Exits via trailing stop, max loss, or MA cross below.
    """
    # Pre-compute date -> {ticker: close_price}
    price_by_date: dict[Any, dict[str, float]] = {}
    for ticker, bars in bars_by_ticker.items():
        for bar in bars:
            d = bar["date"]
            if d not in price_by_date:
                price_by_date[d] = {}
            price_by_date[d][ticker] = bar["close"]

    sorted_dates = sorted(price_by_date.keys())

    # Pre-compute date -> top N tickers by return
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

    tracked: dict[str, list[dict]] = {}

    def signals_fn(ticker: str, bars: list[dict]) -> dict | None:
        min_bars = max(lookback_days + 1, ma_period + 1)
        if len(bars) < min_bars:
            return None

        current_price = bars[-1]["close"]
        current_date = bars[-1]["date"]
        bar_count = len(bars)
        lots = tracked.get(ticker, [])

        # Compute 50-day MA
        closes = [b["close"] for b in bars[-ma_period:]]
        ma_50 = sum(closes) / len(closes)
        above_ma = current_price > ma_50

        # Exit logic
        if lots:
            for lot in lots:
                lot["peak_price"] = max(lot["peak_price"], current_price)

            should_sell = False
            exit_reason = "unknown"

            # MA cross below: exit if price drops below 50-day MA
            if not above_ma:
                should_sell = True
                exit_reason = "ma_cross_below"

            if not should_sell:
                for lot in lots:
                    peak = lot["peak_price"]
                    entry = lot["entry_price"]
                    if peak > entry and (peak - current_price) / peak >= trailing_stop_pct:
                        should_sell = True
                        exit_reason = "trailing_stop"
                        break
                    if (entry - current_price) / entry >= max_loss_pct:
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

        # Entry: in top N AND above 50-day MA
        top_tickers = rankings_by_date.get(current_date, [])
        if not lots and ticker in top_tickers and above_ma:
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
                    "strategy": "thematic_momentum",
                    "rank": top_tickers.index(ticker) + 1,
                    "lookback_days": lookback_days,
                    "above_ma_50": True,
                },
            }

        return None

    return signals_fn


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
    Entry: composite score in top N.
    Exit: trailing stop.
    """
    tracked: dict[str, list[dict]] = {}
    scores_cache: dict[str, float] = {}

    def _compute_quality_score(fundamentals: dict) -> float:
        """Compute composite quality-value score. Higher = better."""
        roe = fundamentals.get("roe", 0.0)
        debt_equity = fundamentals.get("debt_equity", 0.0)
        margin = fundamentals.get("profit_margin", 0.0)

        roe_score = roe / 0.20
        de_score = max(0.0, 1.0 - debt_equity / 2.0)
        margin_score = margin / 0.25

        return (roe_score + de_score + margin_score) / 3.0

    def signals_fn(ticker: str, bars: list[dict]) -> dict | None:
        if len(bars) < 5:
            return None

        current_price = bars[-1]["close"]
        current_date = bars[-1]["date"]
        bar_count = len(bars)
        lots = tracked.get(ticker, [])

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

        # Compute quality score and update cache
        score = _compute_quality_score(fundamentals)
        scores_cache[ticker] = score

        if len(scores_cache) < 3:
            return None

        ranked = sorted(scores_cache.items(), key=lambda x: x[1], reverse=True)
        top_tickers = [t for t, _ in ranked[:top_n]]

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
    tracked: dict[str, dict] = {}

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


def make_tail_risk_hedge_signals_fn(
    regime_by_date: dict,
    position_size_pct: float = 0.25,
    initial_capital: float = 100_000,
):
    """Create a tail-risk hedge signal function.

    Rotates between inverse ETFs and defensive assets based on market regime.
    Bull: 50% GLD + 50% TLT
    Neutral: 40% GLD + 40% TLT + 20% SH
    Bear: 40% SH + 30% PSQ + 20% SDS + 10% GLD
    Regime change triggers full rotation (sell all, re-buy per new allocation).
    """
    ALLOCATIONS = {
        "bull": {"GLD": 0.50, "TLT": 0.50},
        "neutral": {"GLD": 0.40, "TLT": 0.40, "SH": 0.20},
        "bear": {"SH": 0.40, "PSQ": 0.30, "SDS": 0.20, "GLD": 0.10},
    }

    tracked: dict[str, dict] = {}  # ticker -> {entry_price, regime_at_entry}

    def signals_fn(ticker: str, bars: list[dict]) -> dict | None:
        if len(bars) < 2:
            return None

        current_price = bars[-1]["close"]
        current_date = bars[-1]["date"]
        regime = regime_by_date.get(current_date, "bull")

        lot = tracked.get(ticker)

        # Detect regime change and sell existing positions
        if lot is not None and lot["regime_at_entry"] != regime:
            tracked.pop(ticker, None)
            return {
                "action": "sell",
                "ticker": ticker,
                "limit_price": current_price,
                "quantity": 0,
                "sector": "Unknown",
                "exit_reason": "regime_change",
            }

        # Entry: buy if ticker is in current regime allocation and not already held
        allocation = ALLOCATIONS.get(regime, {})
        if lot is None and ticker in allocation:
            weight = allocation[ticker]
            quantity = max(1, int(initial_capital * position_size_pct * weight / current_price))
            tracked[ticker] = {
                "entry_price": current_price,
                "regime_at_entry": regime,
            }
            return {
                "action": "buy",
                "ticker": ticker,
                "limit_price": current_price,
                "quantity": quantity,
                "sector": "Unknown",
                "signals": {
                    "strategy": "tail_risk_hedge",
                    "regime": regime,
                    "weight": weight,
                },
            }

        return None

    return signals_fn


def make_ml_filtered_signals_fn(
    inner_fn: Callable[[str, list[dict]], dict | None],
    model,
    threshold: float = 0.5,
    strategy_name: str = "unknown",
) -> Callable[[str, list[dict]], dict | None]:
    """Wrap a signal function with ML quality scoring.

    Buy signals are scored by the model. If P(profitable) < threshold,
    the signal is suppressed. Sell signals and None always pass through.

    Args:
        inner_fn: Original signal function to wrap.
        model: Trained LightGBM Booster with predict() method.
        threshold: Minimum model confidence to pass a buy signal.
        strategy_name: Portfolio/strategy name for the feature vector.
    """
    def filtered_signals_fn(ticker: str, bars: list[dict]) -> dict | None:
        signal = inner_fn(ticker, bars)
        if signal is None:
            return None

        # Always pass sell signals
        if signal.get("action") != "buy":
            return signal

        # Build feature vector for the model
        row: dict = {"portfolio": strategy_name}

        # Flatten signal features
        signals = signal.get("signals", {})
        for key, val in signals.items():
            if isinstance(val, dict):
                for subkey, subval in val.items():
                    if isinstance(subval, (int, float)):
                        row[f"signal_{key}_{subkey}"] = subval
            elif isinstance(val, (int, float)):
                row[f"signal_{key}"] = val

        # Bar-derived features (from recent bars)
        if len(bars) >= 21:
            closes = [b["close"] for b in bars[-21:]]
            volumes = [b["volume"] for b in bars[-21:]]

            row["bar_return_5d"] = (closes[-1] - closes[-6]) / closes[-6]
            row["bar_return_20d"] = (closes[-1] - closes[0]) / closes[0]

            daily_rets = [
                (closes[i] - closes[i - 1]) / closes[i - 1]
                for i in range(1, len(closes))
            ]
            row["bar_vol_20d"] = float(np.std(daily_rets))

            avg_vol = np.mean(volumes[:-1])
            row["bar_volume_ratio"] = float(volumes[-1] / avg_vol) if avg_vol > 0 else 1.0

        # Create DataFrame matching model's expected features
        feature_names = model.feature_name()
        feature_row = {name: row.get(name, np.nan) for name in feature_names}
        df = pd.DataFrame([feature_row])

        # Convert categorical columns
        for col in df.select_dtypes(include=["object"]).columns:
            df[col] = df[col].astype("category")

        # Score
        score = model.predict(df)[0]

        if score >= threshold:
            return signal
        return None

    return filtered_signals_fn


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


def simulate_rebalancer(
    strategy_curves: dict[str, list[float]],
    initial_weights: dict[str, float],
    rebalance_interval_days: int = 21,
    lookback_days: int = 126,
    max_shift_pct: float = 0.05,
    floor_pct: float = 0.05,
    ceiling_pct: float = 0.25,
    special_floors: dict[str, float] | None = None,
) -> dict:
    """Simulate monthly performance-adaptive capital reallocation.

    Takes per-strategy daily equity curves (all same length) and initial
    weights, then every *rebalance_interval_days* (after a warm-up of
    *lookback_days*) shifts capital toward strategies with above-median
    trailing Sharpe ratios, subject to floor/ceiling constraints.

    Returns a dict with:
      - rebalanced_values: combined equity curve (list[float])
      - weights_history: list of {day_index, weights} dicts
    """
    if special_floors is None:
        special_floors = {}

    strategy_names = list(strategy_curves.keys())
    n_strategies = len(strategy_names)
    n_days = len(next(iter(strategy_curves.values())))

    # --- Compute daily returns for each strategy ---
    # returns[s][d] is the return on day d (d=0 corresponds to day index 1)
    returns: dict[str, list[float]] = {}
    for name in strategy_names:
        curve = strategy_curves[name]
        strat_returns = []
        for d in range(1, n_days):
            if curve[d - 1] != 0:
                strat_returns.append(curve[d] / curve[d - 1] - 1.0)
            else:
                strat_returns.append(0.0)
        returns[name] = strat_returns

    # --- Initialise weights ---
    current_weights = {name: initial_weights[name] for name in strategy_names}
    weights_history: list[dict] = [
        {"day_index": 0, "weights": dict(current_weights)},
    ]

    # --- Build combined equity curve ---
    combined_value = sum(
        strategy_curves[name][0] * current_weights[name]
        for name in strategy_names
    )
    rebalanced_values: list[float] = [combined_value]

    for d in range(1, n_days):
        # d is the day index; returns index is d-1
        daily_combined_return = sum(
            current_weights[name] * returns[name][d - 1]
            for name in strategy_names
        )
        combined_value *= 1.0 + daily_combined_return
        rebalanced_values.append(combined_value)

        # --- Rebalance check ---
        if d >= lookback_days and d % rebalance_interval_days == 0:
            # Compute trailing Sharpe for each strategy
            sharpes: dict[str, float] = {}
            for name in strategy_names:
                window = returns[name][d - lookback_days : d]
                mean_ret = sum(window) / len(window)
                variance = sum((r - mean_ret) ** 2 for r in window) / len(window)
                std_ret = variance ** 0.5
                if std_ret > 1e-12:
                    sharpes[name] = (mean_ret / std_ret) * (252 ** 0.5)
                else:
                    sharpes[name] = 0.0

            # Find median Sharpe
            sorted_sharpes = sorted(sharpes.values())
            mid = n_strategies // 2
            if n_strategies % 2 == 1:
                median_sharpe = sorted_sharpes[mid]
            else:
                median_sharpe = (sorted_sharpes[mid - 1] + sorted_sharpes[mid]) / 2.0

            # Adjust weights
            for name in strategy_names:
                diff = sharpes[name] - median_sharpe
                adjustment = min(max_shift_pct, abs(diff) * 0.01)
                if diff > 0:
                    current_weights[name] += adjustment
                elif diff < 0:
                    current_weights[name] -= adjustment

            # Enforce floor/ceiling and normalise (two passes)
            for _pass in range(2):
                for name in strategy_names:
                    floor = special_floors.get(name, floor_pct)
                    current_weights[name] = max(current_weights[name], floor)
                    current_weights[name] = min(current_weights[name], ceiling_pct)

                total = sum(current_weights.values())
                if total > 0:
                    for name in strategy_names:
                        current_weights[name] /= total

            weights_history.append(
                {"day_index": d, "weights": dict(current_weights)},
            )

    return {
        "rebalanced_values": rebalanced_values,
        "weights_history": weights_history,
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
    all_tickers = get_union_universe([
        "mean_reversion", "momentum", "sector_rotation",
        "short_term_mr", "thematic_momentum",
        "quality_value", "earnings_drift",
        "tail_risk_hedge",
    ])
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

    # Compute market regime for regime-dependent strategies
    regime_by_date = compute_regime_by_date(bars_by_ticker)
    print(f"  Computed regime for {len(regime_by_date)} trading days")

    # 2. Set up backtest components
    print("\nStep 2: Initializing backtest engine...")
    executor = SimulatedExecutor(
        slippage_bps=args.slippage_bps,
        commission_per_share=args.commission,
    )

    # Build portfolio configurations
    mr_signals_fn = make_signals_fn(
        position_size_pct=0.12,
        initial_capital=args.capital * 0.12,
        trailing_stop_pct=0.10,
    )
    mom_signals_fn = make_momentum_signals_fn(
        bars_by_ticker=bars_by_ticker,
        top_n=5,
        lookback_days=126,
        position_size_pct=0.12,
        initial_capital=args.capital * 0.18,
        trailing_stop_pct=0.10,
        bear_tickers=BEAR_TICKERS,
    )
    sector_signals_fn = make_sector_rotation_signals_fn(
        bars_by_ticker=bars_by_ticker,
        top_n=3,
        lookback_days=63,
        position_size_pct=0.20,
        initial_capital=args.capital * 0.12,
        trailing_stop_pct=0.08,
    )
    st_mr_signals_fn = make_short_term_mr_signals_fn(
        position_size_pct=0.08,
        initial_capital=args.capital * 0.10,
        max_hold_days=5,
    )
    thematic_signals_fn = make_thematic_momentum_signals_fn(
        bars_by_ticker=bars_by_ticker,
        top_n=8,
        lookback_days=63,
        position_size_pct=0.15,
        initial_capital=args.capital * 0.11,
        trailing_stop_pct=0.10,
    )
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
    tail_risk_signals_fn = make_tail_risk_hedge_signals_fn(
        regime_by_date=regime_by_date,
        position_size_pct=0.25,
        initial_capital=args.capital * 0.10,
    )
    portfolios: dict[str, PortfolioConfig] = {
        "mean_reversion": PortfolioConfig(
            name="mean_reversion",
            capital=args.capital * 0.12,
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
            capital=args.capital * 0.18,
            signals_fn=mom_signals_fn,
            risk_engine=RiskEngine(
                position_entry_limit_pct=12.0,
                sector_concentration_pct=30.0,
                total_exposure_limit_pct=150.0,
                max_lots_per_ticker=1,
            ),
        ),
        "sector_rotation": PortfolioConfig(
            name="sector_rotation",
            capital=args.capital * 0.12,
            signals_fn=sector_signals_fn,
            risk_engine=RiskEngine(
                position_entry_limit_pct=20.0,
                sector_concentration_pct=50.0,
                total_exposure_limit_pct=100.0,
                max_lots_per_ticker=1,
            ),
        ),
        "short_term_mr": PortfolioConfig(
            name="short_term_mr",
            capital=args.capital * 0.10,
            signals_fn=st_mr_signals_fn,
            risk_engine=RiskEngine(
                position_entry_limit_pct=8.0,
                sector_concentration_pct=30.0,
                total_exposure_limit_pct=100.0,
                max_lots_per_ticker=1,
            ),
        ),
        "thematic_momentum": PortfolioConfig(
            name="thematic_momentum",
            capital=args.capital * 0.11,
            signals_fn=thematic_signals_fn,
            risk_engine=RiskEngine(
                position_entry_limit_pct=15.0,
                sector_concentration_pct=50.0,
                total_exposure_limit_pct=120.0,
                max_lots_per_ticker=1,
            ),
        ),
        "quality_value": PortfolioConfig(
            name="quality_value",
            capital=args.capital * 0.12,
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
            capital=args.capital * 0.15,
            signals_fn=ed_signals_fn,
            risk_engine=RiskEngine(
                position_entry_limit_pct=8.0,
                sector_concentration_pct=30.0,
                total_exposure_limit_pct=100.0,
                max_lots_per_ticker=1,
            ),
        ),
        "tail_risk_hedge": PortfolioConfig(
            name="tail_risk_hedge",
            capital=args.capital * 0.10,
            signals_fn=tail_risk_signals_fn,
            risk_engine=RiskEngine(
                position_entry_limit_pct=25.0,
                sector_concentration_pct=50.0,
                total_exposure_limit_pct=100.0,
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

    # Enrich trades with bar-derived features (for ML training)
    for name, result in results.items():
        enrich_trades(result.trades, bars_by_ticker, regime_by_date)

    # 4. Print results
    if len(portfolios) == 1:
        # Single portfolio: backward-compatible output
        result = next(iter(results.values()))
        print_results(result, elapsed)
    else:
        aggregate = compute_aggregate_metrics(results, portfolios)
        print_multi_portfolio_results(results, portfolios, aggregate, elapsed)

        # Run rebalancer simulation
        strategy_curves = {name: result.portfolio_values for name, result in results.items()}
        total_capital = sum(pc.capital for pc in portfolios.values())
        initial_weights = {name: pc.capital / total_capital for name, pc in portfolios.items()}
        rebalancer_result = simulate_rebalancer(
            strategy_curves=strategy_curves,
            initial_weights=initial_weights,
            rebalance_interval_days=21,
            lookback_days=126,
            max_shift_pct=0.05,
            floor_pct=0.05,
            ceiling_pct=0.25,
            special_floors={"tail_risk_hedge": 0.08},
        )

        # Print rebalancer comparison
        if rebalancer_result["weights_history"]:
            reb_values = rebalancer_result["rebalanced_values"]
            if len(reb_values) > 1:
                reb_return = (reb_values[-1] - reb_values[0]) / reb_values[0]
                print(f"\n  Rebalancer simulation:")
                print(f"    Static total return:      {aggregate['metrics']['total_return']:>10.2%}")
                print(f"    Rebalanced total return:  {reb_return:>10.2%}")
                final_w = rebalancer_result["weights_history"][-1]["weights"]
                print(f"    Final weights: {', '.join(f'{n}: {w:.1%}' for n, w in sorted(final_w.items()))}")

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
