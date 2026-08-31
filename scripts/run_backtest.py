#!/usr/bin/env python3
"""Run a backtest using historical data from IB Gateway.

Usage:
    python scripts/run_backtest.py [--tickers N] [--years N] [--capital N]

Connects to IB Gateway on paper port (7497), downloads daily OHLCV bars,
runs technical signal analysis, gates entries through the risk engine,
and prints performance metrics.
"""

# Direct script execution needs the worktree bootstrap below before local imports.
# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

# When invoked as ``python scripts/run_backtest.py``, prefer this worktree over
# any editable-package path installed from the primary checkout.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backtest.portfolio_context import PortfolioContext
from backtest.ranked_selection import (
    ReplacementPolicy,
    rank_complete_universe,
    target_deltas,
)

import numpy as np
import pandas as pd

from backtest.aggregate_risk import AggregateRiskMonitor
from backtest.costs import (
    DEFAULT_COMMISSION_MINIMUM,
    DEFAULT_COMMISSION_PER_SHARE,
    DEFAULT_SLIPPAGE_BPS,
    CostModel,
)
from backtest.divergence import NEXT_OPEN_FILL_MODEL
from backtest.feature_extractor import enrich_trades
from backtest.membership import (
    COVERAGE_BLOCKED,
    CoverageReport,
    measure_coverage,
    priced_days_from_bars,
)
from backtest.metrics import BacktestMetrics
from backtest.runner import BacktestResult, BacktestRunner, collect_sorted_dates
from backtest.simulator import SimulatedExecutor
from services.risk_management.engine import RiskEngine


@dataclass
class PortfolioConfig:
    """Configuration for a single portfolio in a multi-portfolio backtest."""

    name: str
    capital: float
    signals_fn: Callable[[str, list[dict]], dict | None]
    risk_engine: RiskEngine


def _context_exit_quantity(
    portfolio_context: PortfolioContext | None,
    ticker: str,
    held_quantity: float,
) -> float | None:
    """Return filled quantity not already covered by an active order.

    An active BUY is opposing flow and therefore suppresses a new SELL. Active
    SELL quantity only covers that portion of the durable filled position.
    """
    if portfolio_context is None:
        return 0.0
    if portfolio_context.pending_quantity(ticker, "buy") > 0:
        return None
    uncovered = max(
        float(held_quantity)
        - portfolio_context.pending_quantity(ticker, "sell"),
        0.0,
    )
    return uncovered if uncovered > 0 else None
from scripts.fetch_fundamentals import load_fundamentals_cache, build_fundamentals_lookup, SECTOR_MAP
from scripts.fetch_earnings import load_earnings_cache, build_earnings_lookup
from scripts.train_signal_model import assert_ml_filter_out_of_sample
from services.signal_generation.technical import (
    SupportProximitySignal,
    SupportStrengthSignal,
    SupportTrendSignal,
    RSISignal,
    VolumeSignal,
    find_support_levels,
)

# Ticker universes moved to shared/universe.py (single source of truth,
# also used by the data_ingestion service). Re-exported here so existing
# imports (run_paper.py, tests) keep working.
from shared.universe import (  # noqa: F401
    ACTIVE_SLEEVES,
    BEAR_TICKERS,
    DEFENSIVE_TICKERS,
    SECTOR_ETFS,
    SP500_TOP50,
    SP500_TOP100,
    THEMATIC_ETFS,
    UNIVERSE_REGISTRY,
    MembershipCalendar,
    get_union_universe,
    make_stock_contract,
)

# Instruments that are tradable on every date because they are not index
# constituents at all: the sector, thematic, inverse and defensive ETFs the
# non-equity sleeves are built on. Point-in-time S&P membership must not gate
# these or those sleeves would never trade.
ALWAYS_TRADABLE = (
    frozenset(SECTOR_ETFS)
    | frozenset(THEMATIC_ETFS)
    | frozenset(DEFENSIVE_TICKERS)
    | frozenset(BEAR_TICKERS)
)


def load_membership_calendar(path: str) -> MembershipCalendar:
    """Load point-in-time index membership for the equity sleeves."""
    return MembershipCalendar.from_json_file(path, always=ALWAYS_TRADABLE)


def resolve_backtest_universe(
    membership: MembershipCalendar | None,
) -> list[str]:
    """Tickers whose bars the backtest needs.

    With a membership calendar this is every name that was *ever* a member —
    including those later dropped or delisted — plus the ETF sleeves. Without
    one it falls back to the static present-day sleeve union, which carries
    survivorship bias (finding 4.1).
    """
    sleeve_union = get_union_universe(list(ACTIVE_SLEEVES))
    if membership is None:
        return sleeve_union
    seen: set[str] = set()
    universe: list[str] = []
    for ticker in list(membership.all_tickers()) + sleeve_union:
        if ticker not in seen:
            seen.add(ticker)
            universe.append(ticker)
    return universe


def build_cost_model(
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    commission_per_share: float = DEFAULT_COMMISSION_PER_SHARE,
    commission_minimum: float = DEFAULT_COMMISSION_MINIMUM,
) -> CostModel:
    """Cost model for the backtest: per-order floor + per-instrument slippage."""
    return CostModel.with_liquidity_tiers(
        slippage_bps=slippage_bps,
        commission_per_share=commission_per_share,
        commission_minimum=commission_minimum,
    )


def build_base_config(
    *,
    all_tickers: list[str],
    years: int,
    capital: float,
    cost_model: CostModel,
    replacement_policy: str,
    replacement_score_margin: float,
    portfolio_capitals: dict[str, float],
    point_in_time_universe: bool,
    coverage: CoverageReport | None = None,
    whole_shares: bool = False,
) -> dict:
    """Provenance block saved with the results.

    The execution model is declared explicitly so ``scripts/divergence_monitor``
    can tell whether these numbers are a like-for-like baseline for live
    trading, instead of assuming they are (finding 4.6).

    ``coverage`` is omitted entirely when the run had no membership calendar to
    measure against. Writing a zeroed block instead would make an unmeasured
    run indistinguishable from a fully-covered one — absence has to stay
    visible, because the reader treats it as unsafe.
    """
    config = {
        "tickers": all_tickers,
        "years": years,
        "initial_capital": capital,
        "fill_model": NEXT_OPEN_FILL_MODEL,
        "whole_shares": whole_shares,
        "point_in_time_universe": point_in_time_universe,
        "replacement_policy": replacement_policy,
        "replacement_score_margin": replacement_score_margin,
        "portfolios": portfolio_capitals,
        **cost_model.to_dict(),
    }
    if coverage is not None:
        config["coverage"] = coverage.to_dict()
    return config


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
    from ib_insync import IB

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
            contract = make_stock_contract(ticker)
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
    "crash": {"trailing_stop_pct": 0.04, "max_loss_pct": 0.02},
}


def compute_regime_by_date(
    bars_by_ticker: dict[str, list[dict]],
    ma_period: int = 200,
    bull_threshold: float = 0.60,
    bear_threshold: float = 0.40,
    crash_threshold: float = 0.10,
) -> dict:
    """Compute market regime for each date based on breadth.

    Bull: >60% of stocks above their 200-day MA.
    Neutral: 40-60%.
    Bear: <40% above their 200-day MA.
    Crash: <10% above their 200-day MA (>90% below).
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
        elif breadth < crash_threshold:
            regime_by_date[d] = "crash"
        elif breadth < bear_threshold:
            regime_by_date[d] = "bear"
        else:
            regime_by_date[d] = "neutral"

    return regime_by_date


class SkipLedger:
    """Records entry signals dropped because whole-share sizing hit zero.

    At Rung-0 capital a sleeve's per-position budget can be smaller than one
    share of the name it wants to buy. Live, that is an ``OrderSkippedError``
    from ``ib_executor._effective_quantity``; here it has to be counted rather
    than silently dropped, because the count *is* the finding — a sleeve that
    cannot open positions has no edge to measure, and a run that merely
    reports "few trades" hides why.

    One entry per rejected occurrence, not per unique ticker: the same name
    turned away on twenty consecutive days is twenty lost entries.
    """

    def __init__(self) -> None:
        self._signals: list[dict] = []
        self.sized = 0

    def count_sized(self) -> None:
        """Note one entry signal reaching sizing — the skip count's denominator.

        Counted in both modes, so "300 unfillable" can be read against how many
        entries the sleeve tried to open at all.
        """
        self.sized += 1

    def record(
        self,
        *,
        ticker: str,
        current_date: Any,
        fractional_quantity: float,
        price: float,
    ) -> None:
        self._signals.append({
            "ticker": ticker,
            "date": current_date.isoformat() if hasattr(current_date, "isoformat")
                    else str(current_date),
            "fractional_quantity": round(fractional_quantity, 4),
            "price": price,
        })

    def to_dict(self) -> dict:
        return {"count": len(self._signals), "signals": list(self._signals)}


EMPTY_SKIP_LEDGER = {"count": 0, "signals": []}


def _entry_quantity(
    *,
    initial_capital: float,
    position_size_pct: float,
    current_price: float,
    whole_shares: bool,
    skip_ledger: SkipLedger | None,
    ticker: str,
    current_date: Any,
    weight: float = 1.0,
) -> float | None:
    """Size one entry, returning ``None`` when the position cannot be opened.

    Default (``whole_shares=False``) reproduces the historical fractional
    formula exactly, so every existing invocation — including the weekly
    refresh — is unaffected. With ``whole_shares=True`` the quantity truncates
    toward zero the way ``ib_executor._effective_quantity`` does, and a zero
    is recorded on *skip_ledger* and reported to the caller as no signal.
    """
    if skip_ledger is not None:
        skip_ledger.count_sized()

    fractional = initial_capital * position_size_pct * weight / current_price
    if not whole_shares:
        return round(max(0.0001, fractional), 4)

    quantity = float(int(fractional))
    if quantity <= 0:
        if skip_ledger is not None:
            skip_ledger.record(
                ticker=ticker,
                current_date=current_date,
                fractional_quantity=fractional,
                price=current_price,
            )
        return None
    return quantity


def make_signals_fn(
    position_size_pct: float = 0.07,
    initial_capital: float = 100_000,
    trailing_stop_pct: float = 0.10,
    max_lots: int = 2,
    regime_by_date: dict | None = None,
    whole_shares: bool = False,
    skip_ledger: SkipLedger | None = None,
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
                quantity = _entry_quantity(
                    initial_capital=initial_capital,
                    position_size_pct=position_size_pct,
                    current_price=current_price,
                    whole_shares=whole_shares,
                    skip_ledger=skip_ledger,
                    ticker=ticker,
                    current_date=current_date,
                )
                if quantity is None:
                    return None
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
                quantity = _entry_quantity(
                    initial_capital=initial_capital,
                    position_size_pct=position_size_pct,
                    current_price=current_price,
                    whole_shares=whole_shares,
                    skip_ledger=skip_ledger,
                    ticker=ticker,
                    current_date=current_date,
                )
                if quantity is None:
                    return None
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
    portfolio_context: PortfolioContext | None = None,
    eligible_tickers: list[str] | None = None,
    membership: MembershipCalendar | None = None,
    whole_shares: bool = False,
    skip_ledger: SkipLedger | None = None,
):
    """Create a momentum signal function based on 6-month relative strength.

    Ranks the sleeve's own universe by return over the lookback period and buys
    the top N. Exits via trailing stop + max loss. In bear markets, inverse ETFs
    (bear_tickers) naturally rank high and get selected; when the regime turns
    non-bear they are force-exited.

    ``eligible_tickers`` scopes the ranking to the sleeve's universe. Without it
    the ranking pool is every ticker in ``bars_by_ticker`` — which in a
    multi-sleeve backtest is the union of *all* sleeves, letting momentum's top-N
    fill up with thematic ETFs it was never meant to hold.

    ``membership`` additionally restricts each date's ranking to names that were
    index members on that date. This matters because the runner blocks entries in
    non-members: without the per-date filter, top-N slots get taken by names the
    sleeve is not allowed to buy and it silently trades nothing.
    """
    # Pre-compute date -> {ticker: close_price} for ranking
    eligible = set(eligible_tickers or bars_by_ticker)
    price_by_date: dict[Any, dict[str, float]] = {}
    for ticker, bars in bars_by_ticker.items():
        if ticker not in eligible:
            continue
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
            if membership is not None and not membership.contains(ticker, d):
                continue
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
        held = portfolio_context.positions.get(ticker) if portfolio_context else None
        lots = ([{
            "entry_price": held.avg_entry_price,
            "peak_price": max(held.peak_price, current_price),
        }] if held else tracked.get(ticker, []))

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
                exit_quantity = _context_exit_quantity(
                    portfolio_context, ticker, held.quantity if held else 0
                )
                if exit_quantity is None:
                    return None
                if portfolio_context is None:
                    tracked.pop(ticker, None)
                return {
                    "action": "sell",
                    "ticker": ticker,
                    "limit_price": current_price,
                    "quantity": exit_quantity,
                    "sector": "Unknown",
                    "exit_reason": "regime_change",
                }

            if portfolio_context is None:
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
                exit_quantity = _context_exit_quantity(
                    portfolio_context, ticker, held.quantity if held else 0
                )
                if exit_quantity is None:
                    return None
                if portfolio_context is None:
                    tracked.pop(ticker, None)
                return {
                    "action": "sell",
                    "ticker": ticker,
                    "limit_price": current_price,
                    "quantity": exit_quantity,
                    "sector": "Unknown",
                    "exit_reason": exit_reason,
                }

        # === Entry logic: buy if in top N and not already tracked ===
        top_tickers = rankings_by_date.get(current_date, [])
        pending_buy_quantity = (
            portfolio_context.pending_quantity(ticker, "buy")
            if portfolio_context else 0.0
        )
        if not lots and pending_buy_quantity <= 0 and ticker in top_tickers:
            quantity = _entry_quantity(
                initial_capital=initial_capital,
                position_size_pct=position_size_pct,
                current_price=current_price,
                whole_shares=whole_shares,
                skip_ledger=skip_ledger,
                ticker=ticker,
                current_date=current_date,
            )
            if quantity is None:
                return None
            if portfolio_context is None:
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
    portfolio_context: PortfolioContext | None = None,
    eligible_tickers: list[str] | None = None,
    whole_shares: bool = False,
    skip_ledger: SkipLedger | None = None,
):
    """Create a sector rotation signal function.

    Ranks sector ETFs by 3-month return and buys the top N.
    In bear regime, rotates to defensive sectors only (XLU, XLP, XLV).
    Exits via trailing stop or when sector drops out of top N.

    ``eligible_tickers`` scopes the ranking to this sleeve's ETFs; without it the
    pool is every ticker in ``bars_by_ticker`` (the union of all sleeves), which
    lets the sector sleeve buy individual equities.
    """
    defensive_sectors = {"XLU", "XLP", "XLV"}

    # Pre-compute date -> {ticker: close_price} for ranking
    eligible = set(eligible_tickers or bars_by_ticker)
    price_by_date: dict[Any, dict[str, float]] = {}
    for ticker, bars in bars_by_ticker.items():
        if ticker not in eligible:
            continue
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
        held = portfolio_context.positions.get(ticker) if portfolio_context else None
        lots = ([{
            "entry_price": held.avg_entry_price,
            "peak_price": max(held.peak_price, current_price),
        }] if held else tracked.get(ticker, []))

        # Exit: trailing stop
        if lots:
            if portfolio_context is None:
                for lot in lots:
                    lot["peak_price"] = max(lot["peak_price"], current_price)

            for lot in lots:
                peak = lot["peak_price"]
                entry = lot["entry_price"]
                if peak > entry and (peak - current_price) / peak >= trailing_stop_pct:
                    exit_quantity = _context_exit_quantity(
                        portfolio_context, ticker, held.quantity if held else 0
                    )
                    if exit_quantity is None:
                        return None
                    if portfolio_context is None:
                        tracked.pop(ticker, None)
                    return {
                        "action": "sell",
                        "ticker": ticker,
                        "limit_price": current_price,
                        "quantity": exit_quantity,
                        "sector": "Unknown",
                        "exit_reason": "trailing_stop",
                    }

        # Entry: buy if in top N and not already tracked
        top_tickers = rankings_by_date.get(current_date, [])
        pending_buy_quantity = (
            portfolio_context.pending_quantity(ticker, "buy")
            if portfolio_context else 0.0
        )
        if not lots and pending_buy_quantity <= 0 and ticker in top_tickers:
            quantity = _entry_quantity(
                initial_capital=initial_capital,
                position_size_pct=position_size_pct,
                current_price=current_price,
                whole_shares=whole_shares,
                skip_ledger=skip_ledger,
                ticker=ticker,
                current_date=current_date,
            )
            if quantity is None:
                return None
            if portfolio_context is None:
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
    whole_shares: bool = False,
    skip_ledger: SkipLedger | None = None,
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
            quantity = _entry_quantity(
                initial_capital=initial_capital,
                position_size_pct=position_size_pct,
                current_price=current_price,
                whole_shares=whole_shares,
                skip_ledger=skip_ledger,
                ticker=ticker,
                current_date=bars[-1]["date"],
            )
            if quantity is None:
                return None
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
    eligible_tickers: list[str] | None = None,
    top_n: int = 8,
    lookback_days: int = 63,
    ma_period: int = 50,
    position_size_pct: float = 0.15,
    initial_capital: float = 100_000,
    trailing_stop_pct: float = 0.10,
    max_loss_pct: float = 0.08,
    regime_by_date: dict | None = None,
    portfolio_context: PortfolioContext | None = None,
    replacement_policy: ReplacementPolicy = ReplacementPolicy.TECHNICAL_ONLY,
    replacement_score_margin: float = 0.25,
    whole_shares: bool = False,
    skip_ledger: SkipLedger | None = None,
):
    """Create a thematic momentum signal function.

    Ranks thematic ETFs by 3-month return. Buys top N that are above
    their 50-day MA. Exits via trailing stop, max loss, or MA cross below.
    """
    # Pre-compute date -> {ticker: close_price}
    price_by_date: dict[Any, dict[str, float]] = {}
    eligible = set(eligible_tickers or bars_by_ticker)
    for ticker, bars in bars_by_ticker.items():
        if ticker not in eligible:
            continue
        for bar in bars:
            d = bar["date"]
            if d not in price_by_date:
                price_by_date[d] = {}
            price_by_date[d][ticker] = bar["close"]

    sorted_dates = sorted(price_by_date.keys())

    # Pre-compute date -> top N tickers by return
    scores_by_date: dict[Any, dict[str, float]] = {}
    rankings_by_date: dict[Any, list[str]] = {}
    for i, d in enumerate(sorted_dates):
        if i < lookback_days:
            continue
        past_date = sorted_dates[i - lookback_days]
        past_prices = price_by_date.get(past_date, {})
        current_prices = price_by_date[d]

        returns: dict[str, float] = {}
        for ticker in current_prices:
            if ticker in past_prices and past_prices[ticker] > 0:
                ret = (current_prices[ticker] - past_prices[ticker]) / past_prices[ticker]
                returns[ticker] = ret

        scores_by_date[d] = returns
        rankings_by_date[d] = rank_complete_universe(returns, top_n)

    tracked: dict[str, list[dict]] = {}

    def signals_fn(ticker: str, bars: list[dict]) -> dict | None:
        min_bars = max(lookback_days + 1, ma_period + 1)
        if len(bars) < min_bars:
            return None

        current_price = bars[-1]["close"]
        current_date = bars[-1]["date"]
        bar_count = len(bars)
        held = portfolio_context.positions.get(ticker) if portfolio_context else None
        lots = ([{
            "entry_price": held.avg_entry_price,
            "peak_price": max(held.peak_price, current_price),
        }] if held else tracked.get(ticker, []))

        # Determine regime-adjusted trailing stop and max loss
        effective_trailing = trailing_stop_pct
        effective_max_loss = max_loss_pct
        if regime_by_date:
            regime = regime_by_date.get(current_date, "neutral")
            if regime in ("bear", "crash"):
                effective_trailing = max(trailing_stop_pct - 0.02, 0.02)
                effective_max_loss = max(max_loss_pct - 0.02, 0.02)

        # Compute 50-day MA
        closes = [b["close"] for b in bars[-ma_period:]]
        ma_50 = sum(closes) / len(closes)
        above_ma = current_price > ma_50

        # Exit logic
        if lots:
            if portfolio_context is None:
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
                    if peak > entry and (peak - current_price) / peak >= effective_trailing:
                        should_sell = True
                        exit_reason = "trailing_stop"
                        break
                    if (entry - current_price) / entry >= effective_max_loss:
                        should_sell = True
                        exit_reason = "max_loss"
                        break

            if should_sell:
                exit_quantity = _context_exit_quantity(
                    portfolio_context, ticker, held.quantity if held else 0
                )
                if exit_quantity is None:
                    return None
                if portfolio_context is None:
                    tracked.pop(ticker, None)
                return {
                    "action": "sell",
                    "ticker": ticker,
                    "limit_price": current_price,
                    "quantity": exit_quantity,
                    "sector": "Unknown",
                    "exit_reason": exit_reason,
                }

        # Entry: in top N AND above 50-day MA. The eligibility gate sits here
        # rather than at the top of the function so a holding that is no longer
        # in the sleeve's universe keeps running the exit paths above — scoping
        # must stop new entries, never strand a position the sleeve owns and is
        # the only sleeve able to sell.
        if ticker not in eligible:
            return None

        top_tickers = rankings_by_date.get(current_date, [])
        scores = scores_by_date.get(current_date, {})
        held_tickers = (
            set(portfolio_context.positions)
            if portfolio_context is not None
            else set(tracked)
        )
        if lots and held_tickers | set(top_tickers) <= set(scores):
            replacements = target_deltas(
                held=held_tickers,
                selected=set(top_tickers),
                scores=scores,
                policy=replacement_policy,
                score_margin=replacement_score_margin,
            )
            replacement = next(
                (item for item in replacements if item.outgoing == ticker), None
            )
            if replacement is not None:
                exit_quantity = _context_exit_quantity(
                    portfolio_context, ticker, held.quantity if held else 0
                )
                if exit_quantity is None:
                    return None
                if portfolio_context is None:
                    tracked.pop(ticker, None)
                return {
                    "action": "sell",
                    "ticker": ticker,
                    "limit_price": current_price,
                    "quantity": exit_quantity,
                    "sector": "Unknown",
                    "exit_reason": "rank_replacement",
                    "replacement_ticker": replacement.incoming,
                    "score_improvement": replacement.score_improvement,
                }

        pending_buy_quantity = (
            portfolio_context.pending_quantity(ticker, "buy")
            if portfolio_context else 0.0
        )
        if (
            not lots
            and pending_buy_quantity <= 0
            and ticker in top_tickers
            and above_ma
        ):
            quantity = _entry_quantity(
                initial_capital=initial_capital,
                position_size_pct=position_size_pct,
                current_price=current_price,
                whole_shares=whole_shares,
                skip_ledger=skip_ledger,
                ticker=ticker,
                current_date=current_date,
            )
            if quantity is None:
                return None
            if portfolio_context is None:
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
    *,
    bars_by_ticker: dict[str, list[dict]],
    eligible_tickers: list[str] | None = None,
    top_n: int = 15,
    position_size_pct: float = 0.10,
    initial_capital: float = 100_000,
    trailing_stop_pct: float = 0.12,
    regime_by_date: dict | None = None,
    portfolio_context: PortfolioContext | None = None,
    replacement_policy: ReplacementPolicy = ReplacementPolicy.TECHNICAL_ONLY,
    replacement_score_margin: float = 0.25,
    membership: MembershipCalendar | None = None,
    whole_shares: bool = False,
    skip_ledger: SkipLedger | None = None,
):
    """Create a quality value signal function.

    Ranks stocks by a composite quality-value score (ROE, D/E, margin).
    Entry: composite score in top N.
    Exit: trailing stop.

    ``membership`` restricts each date's ranking to index members on that date,
    so top-N slots are not taken by names the runner will refuse to buy.
    """
    tracked: dict[str, list[dict]] = {}

    def _compute_quality_score(fundamentals: dict) -> float:
        """Compute composite quality-value score. Higher = better."""
        roe = fundamentals.get("roe", 0.0)
        debt_equity = fundamentals.get("debt_equity", 0.0)
        margin = fundamentals.get("profit_margin", 0.0)

        roe_score = roe / 0.20
        de_score = max(0.0, 1.0 - debt_equity / 2.0)
        margin_score = margin / 0.25

        return (roe_score + de_score + margin_score) / 3.0

    eligible = set(eligible_tickers or bars_by_ticker)
    tickers_by_date: dict[date, set[str]] = {}
    for candidate, candidate_bars in bars_by_ticker.items():
        if candidate not in eligible:
            continue
        for bar in candidate_bars:
            # Point-in-time: a name only competes for a top-N slot on dates it
            # was actually a member, otherwise non-members crowd out the names
            # the runner would let this sleeve buy.
            if membership is not None and not membership.contains(
                candidate, bar["date"]
            ):
                continue
            tickers_by_date.setdefault(bar["date"], set()).add(candidate)

    scores_by_date: dict[date, dict[str, float]] = {}
    rankings_by_date: dict[date, list[str]] = {}
    for as_of, available_tickers in tickers_by_date.items():
        complete_scores: dict[str, float] = {}
        for candidate in available_tickers:
            candidate_fundamentals = fundamentals_lookup(candidate, as_of)
            if candidate_fundamentals is not None:
                complete_scores[candidate] = _compute_quality_score(
                    candidate_fundamentals
                )
        scores_by_date[as_of] = complete_scores
        rankings_by_date[as_of] = rank_complete_universe(
            complete_scores, top_n
        )

    def signals_fn(ticker: str, bars: list[dict]) -> dict | None:
        if len(bars) < 5:
            return None

        current_price = bars[-1]["close"]
        current_date = bars[-1]["date"]
        bar_count = len(bars)
        held = portfolio_context.positions.get(ticker) if portfolio_context else None
        lots = ([{
            "entry_price": held.avg_entry_price,
            "peak_price": max(held.peak_price, current_price),
        }] if held else tracked.get(ticker, []))

        fundamentals = None
        if portfolio_context is None:
            fundamentals = fundamentals_lookup(ticker, current_date)
            if fundamentals is None:
                return None

        # Determine regime-adjusted trailing stop
        effective_trailing = trailing_stop_pct
        if regime_by_date:
            regime = regime_by_date.get(current_date, "neutral")
            if regime in ("bear", "crash"):
                effective_trailing = max(trailing_stop_pct - 0.02, 0.02)

        # Exit logic: trailing stop
        if lots:
            if portfolio_context is None:
                for lot in lots:
                    lot["peak_price"] = max(lot["peak_price"], current_price)

            for lot in lots:
                peak = lot["peak_price"]
                entry = lot["entry_price"]
                if peak > entry and (peak - current_price) / peak >= effective_trailing:
                    exit_quantity = _context_exit_quantity(
                        portfolio_context, ticker, held.quantity if held else 0
                    )
                    if exit_quantity is None:
                        return None
                    if portfolio_context is None:
                        tracked.pop(ticker, None)
                    return {
                        "action": "sell",
                        "ticker": ticker,
                        "limit_price": current_price,
                        "quantity": exit_quantity,
                        "sector": sector_map.get(ticker, "Unknown"),
                        "exit_reason": "trailing_stop",
                    }

        if fundamentals is None:
            fundamentals = fundamentals_lookup(ticker, current_date)
        if fundamentals is None:
            return None

        scores = scores_by_date.get(current_date, {})
        if ticker not in scores:
            return None
        score = scores[ticker]
        top_tickers = rankings_by_date.get(current_date, [])

        held_tickers = (
            set(portfolio_context.positions)
            if portfolio_context is not None
            else set(tracked)
        )
        if lots and held_tickers | set(top_tickers) <= set(scores):
            replacements = target_deltas(
                held=held_tickers,
                selected=set(top_tickers),
                scores=scores,
                policy=replacement_policy,
                score_margin=replacement_score_margin,
            )
            replacement = next(
                (item for item in replacements if item.outgoing == ticker), None
            )
            if replacement is not None:
                exit_quantity = _context_exit_quantity(
                    portfolio_context, ticker, held.quantity if held else 0
                )
                if exit_quantity is None:
                    return None
                if portfolio_context is None:
                    tracked.pop(ticker, None)
                return {
                    "action": "sell",
                    "ticker": ticker,
                    "limit_price": current_price,
                    "quantity": exit_quantity,
                    "sector": sector_map.get(ticker, "Unknown"),
                    "exit_reason": "rank_replacement",
                    "replacement_ticker": replacement.incoming,
                    "score_improvement": replacement.score_improvement,
                }

        pending_buy_quantity = (
            portfolio_context.pending_quantity(ticker, "buy")
            if portfolio_context else 0.0
        )
        if not lots and pending_buy_quantity <= 0 and ticker in top_tickers:
            quantity = _entry_quantity(
                initial_capital=initial_capital,
                position_size_pct=position_size_pct,
                current_price=current_price,
                whole_shares=whole_shares,
                skip_ledger=skip_ledger,
                ticker=ticker,
                current_date=current_date,
            )
            if quantity is None:
                return None
            if portfolio_context is None:
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
    regime_by_date: dict | None = None,
    portfolio_context: PortfolioContext | None = None,
    eligible_tickers: list[str] | None = None,
    whole_shares: bool = False,
    skip_ledger: SkipLedger | None = None,
):
    """Create an earnings drift (PEAD) signal function.

    Entry: Earnings surprise > threshold (beat estimate by N%+), within 2 days of announcement.
    Exit: Fixed hold period (20 trading days) or trailing stop 6%.

    ``eligible_tickers`` scopes entries to this sleeve's universe. Unlike the
    ranking sleeves this one has no top-N to crowd, so an unscoped run does not
    fail loudly — it just buys any name the runner offers it that happened to
    beat its estimate, which in live is the union of every sleeve's tickers.
    Exits are deliberately *not* scoped: a position already held has to remain
    sellable even if the universe changed underneath it.
    """
    eligible = frozenset(eligible_tickers) if eligible_tickers is not None else None
    tracked: dict[str, dict] = {}

    def signals_fn(ticker: str, bars: list[dict]) -> dict | None:
        if len(bars) < 5:
            return None

        current_price = bars[-1]["close"]
        current_date = bars[-1]["date"]
        bar_count = len(bars)
        held = portfolio_context.positions.get(ticker) if portfolio_context else None
        lot = ({
            "entry_price": held.avg_entry_price,
            "peak_price": max(held.peak_price, current_price),
            "entry_date": held.entry_date,
        } if held else tracked.get(ticker))

        # Determine regime-adjusted trailing stop
        effective_trailing = trailing_stop_pct
        if regime_by_date:
            regime = regime_by_date.get(current_date, "neutral")
            if regime in ("bear", "crash"):
                effective_trailing = max(trailing_stop_pct - 0.02, 0.02)

        # Exit logic
        if lot is not None:
            if portfolio_context is None:
                lot["peak_price"] = max(lot["peak_price"], current_price)
                bars_held = bar_count - lot["entry_idx"]
            else:
                bars_held = sum(
                    1 for bar in bars if bar["date"] > lot["entry_date"]
                )

            # Time exit
            if bars_held >= max_hold_days:
                exit_quantity = _context_exit_quantity(
                    portfolio_context, ticker, held.quantity if held else 0
                )
                if exit_quantity is None:
                    return None
                if portfolio_context is None:
                    tracked.pop(ticker, None)
                return {
                    "action": "sell",
                    "ticker": ticker,
                    "limit_price": current_price,
                    "quantity": exit_quantity,
                    "sector": "Unknown",
                    "exit_reason": "time_exit",
                }

            # Trailing stop
            peak = lot["peak_price"]
            entry = lot["entry_price"]
            if peak > entry and (peak - current_price) / peak >= effective_trailing:
                exit_quantity = _context_exit_quantity(
                    portfolio_context, ticker, held.quantity if held else 0
                )
                if exit_quantity is None:
                    return None
                if portfolio_context is None:
                    tracked.pop(ticker, None)
                return {
                    "action": "sell",
                    "ticker": ticker,
                    "limit_price": current_price,
                    "quantity": exit_quantity,
                    "sector": "Unknown",
                    "exit_reason": "trailing_stop",
                }

            return None

        # Entry logic: check for recent earnings event. Placed after every exit
        # path above so scoping can never strand a held position.
        if eligible is not None and ticker not in eligible:
            return None

        if (
            portfolio_context
            and portfolio_context.pending_quantity(ticker, "buy") > 0
        ):
            return None

        event = earnings_lookup(ticker, current_date)
        if event is None:
            return None

        surprise = event.get("surprise_pct", 0.0)
        if surprise < surprise_threshold_pct:
            return None

        quantity = _entry_quantity(
            initial_capital=initial_capital,
            position_size_pct=position_size_pct,
            current_price=current_price,
            whole_shares=whole_shares,
            skip_ledger=skip_ledger,
            ticker=ticker,
            current_date=current_date,
        )
        if quantity is None:
            return None
        if portfolio_context is None:
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
    portfolio_context: PortfolioContext | None = None,
    whole_shares: bool = False,
    skip_ledger: SkipLedger | None = None,
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

        held = portfolio_context.positions.get(ticker) if portfolio_context else None
        lot = tracked.get(ticker) if portfolio_context is None else held

        allocation = ALLOCATIONS.get(regime, {})

        hydrated_regime_changed = bool(
            held is not None
            and regime_by_date.get(held.entry_date, regime) != regime
        )
        if (
            portfolio_context is not None
            and held is not None
            and (ticker not in allocation or hydrated_regime_changed)
        ):
            exit_quantity = _context_exit_quantity(
                portfolio_context, ticker, held.quantity
            )
            if exit_quantity is None:
                return None
            return {
                "action": "sell",
                "ticker": ticker,
                "limit_price": current_price,
                "quantity": exit_quantity,
                "sector": "Unknown",
                "exit_reason": "regime_change",
            }

        # Detect regime change and sell existing positions
        if portfolio_context is None and lot is not None and lot["regime_at_entry"] != regime:
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
        pending_buy_quantity = (
            portfolio_context.pending_quantity(ticker, "buy")
            if portfolio_context else 0.0
        )
        if lot is None and pending_buy_quantity <= 0 and ticker in allocation:
            weight = allocation[ticker]
            quantity = _entry_quantity(
                initial_capital=initial_capital,
                position_size_pct=position_size_pct,
                current_price=current_price,
                weight=weight,
                whole_shares=whole_shares,
                skip_ledger=skip_ledger,
                ticker=ticker,
                current_date=current_date,
            )
            if quantity is None:
                return None
            if portfolio_context is None:
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


def make_crash_freeze_signals_fn(
    inner_fn: Callable[[str, list[dict]], dict | None],
    regime_by_date: dict,
) -> Callable[[str, list[dict]], dict | None]:
    """Wrap a signal function to freeze new entries during crash regime.

    Buy signals are suppressed when the current regime is 'crash'.
    Sell signals always pass through (exits are never blocked).
    """
    def signals_fn(ticker: str, bars: list[dict]) -> dict | None:
        signal = inner_fn(ticker, bars)
        if signal is None:
            return None

        if signal.get("action") == "buy":
            current_date = bars[-1]["date"]
            regime = regime_by_date.get(current_date, "neutral")
            if regime == "crash":
                return None

        return signal

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
    shadow_candidates: list[dict] | None = None,
    open_positions: list[dict] | None = None,
    skipped_signals: dict | None = None,
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
        "shadow_candidates": shadow_candidates or [],
        "open_positions": open_positions or [],
        "skipped_signals": skipped_signals or dict(EMPTY_SKIP_LEDGER),
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
    skipped_signals: dict[str, dict] | None = None,
    entry_signals_sized: dict[str, int] | None = None,
) -> str:
    """Serialize multi-portfolio backtest output to a timestamped JSON file.

    ``skipped_signals`` is written for *every* sleeve, empty block included, so
    a sleeve that skipped nothing reads differently from one that was never
    measured.
    """

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
            "shadow_candidates": result.shadow_candidates,
            "open_positions": result.open_positions,
            "skipped_signals": (skipped_signals or {}).get(
                name, dict(EMPTY_SKIP_LEDGER)
            ),
            "entry_signals_sized": (entry_signals_sized or {}).get(name, 0),
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


def _create_backtest_shadow_recorders(
    *,
    enabled: bool,
    bars_by_ticker: dict[str, list[dict]],
    portfolio_names: Iterable[str],
) -> dict[str, Any]:
    """Create isolated per-sleeve observers over one factor snapshot index."""
    if not enabled:
        return {}

    try:
        from research.factors.catalog import (
            DEFAULT_FACTOR_IDS,
            build_default_registry,
        )
        from research.factors.engine import FactorEngine
        from research.factors.panel import build_factor_panel
        from research.shadow import InMemoryShadowRecorder

        snapshots = FactorEngine(build_default_registry()).compute(
            build_factor_panel(bars_by_ticker), DEFAULT_FACTOR_IDS
        )
        return {
            name: InMemoryShadowRecorder(snapshots)
            for name in portfolio_names
        }
    except Exception as exc:
        print(
            "WARNING: Research shadow setup failed; backtest is unchanged "
            f"({exc})"
        )
        return {}


def main():
    parser = argparse.ArgumentParser(description="Run algo-poc backtest with IB data")
    parser.add_argument("--tickers", type=int, default=50,
                        help="Number of top S&P 500 tickers (default: 50)")
    parser.add_argument("--years", type=int, default=10,
                        help="Years of historical data (default: 10)")
    parser.add_argument("--capital", type=float, default=100_000,
                        help="Initial capital (default: 100000)")
    parser.add_argument("--slippage-bps", type=int, default=int(DEFAULT_SLIPPAGE_BPS),
                        help="Base slippage in basis points, widened per "
                             "liquidity tier for thin instruments "
                             f"(default: {int(DEFAULT_SLIPPAGE_BPS)})")
    parser.add_argument("--commission", type=float, default=DEFAULT_COMMISSION_PER_SHARE,
                        help=f"Commission per share (default: {DEFAULT_COMMISSION_PER_SHARE})")
    parser.add_argument("--commission-minimum", type=float,
                        default=DEFAULT_COMMISSION_MINIMUM,
                        help="Per-order commission floor in USD; IB charges "
                             "max($1, $0.005/share) and at this account size "
                             "the floor is usually what binds "
                             f"(default: {DEFAULT_COMMISSION_MINIMUM})")
    parser.add_argument(
        "--whole-shares",
        action="store_true",
        help="Size entries in whole shares, truncating toward zero the way "
             "live execution does (ib_executor._effective_quantity). A signal "
             "whose budget cannot buy one share opens no position and is "
             "recorded under skipped_signals. Off by default: every existing "
             "invocation stays fractional and byte-identical.",
    )
    parser.add_argument("--ib-host", default="127.0.0.1")
    parser.add_argument("--ib-port", type=int, default=7497)
    parser.add_argument("--output-dir", default="output",
                        help="Directory for output files (default: output)")
    parser.add_argument("--ml-filter", default=None,
                        help="Path to trained signal quality model (LightGBM .txt file)")
    parser.add_argument("--ml-threshold", type=float, default=0.55,
                        help="ML filter confidence threshold (default: 0.55)")
    parser.add_argument("--start-date", type=str, default=None,
                        help="Only open new trades on or after this date (YYYY-MM-DD). "
                             "Earlier data is still used for indicator warm-up.")
    parser.add_argument("--bars-from-json", type=str, default=None,
                        help="Path to a prior backtest results JSON. Skips the IB fetch "
                             "and loads the cached bars from that file. Useful when IB "
                             "Gateway is unavailable or for fast iteration.")
    parser.add_argument(
        "--replacement-policy",
        choices=[policy.value for policy in ReplacementPolicy],
        default=ReplacementPolicy.TECHNICAL_ONLY.value,
        help="Offline ranked-candidate replacement policy (default: technical_only)",
    )
    parser.add_argument(
        "--replacement-score-margin",
        type=float,
        default=0.25,
        help="Minimum incoming score improvement for score_margin (default: 0.25)",
    )
    parser.add_argument(
        "--universe-snapshots",
        default=None,
        help="Path to a point-in-time index membership JSON "
             "({\"YYYY-MM-DD\": [tickers]}, effective forward from each "
             "snapshot; see docs/operations/backtest-baseline.md). Without it "
             "the backtest uses today's static ticker list and is "
             "survivorship-biased.",
    )
    parser.add_argument(
        "--research-shadow",
        action="store_true",
        help="Record factor snapshots for every raw sleeve buy candidate",
    )
    args = parser.parse_args()

    replacement_policy = ReplacementPolicy(args.replacement_policy)
    if args.replacement_score_margin < 0:
        parser.error("--replacement-score-margin must be non-negative")

    trade_start_date = None
    if args.start_date:
        trade_start_date = date.fromisoformat(args.start_date)

    membership = None
    if args.universe_snapshots:
        membership = load_membership_calendar(args.universe_snapshots)

    # Refuse an in-sample ML filter before spending time on data.
    if args.ml_filter:
        try:
            ml_metadata = assert_ml_filter_out_of_sample(
                args.ml_filter, trade_start_date
            )
        except ValueError as exc:
            parser.error(str(exc))

    cost_model = build_cost_model(
        slippage_bps=args.slippage_bps,
        commission_per_share=args.commission,
        commission_minimum=args.commission_minimum,
    )

    tickers = SP500_TOP50[:args.tickers]
    print(f"Backtest Configuration:")
    print(f"  Tickers: {len(tickers)} (top S&P 500)")
    print(f"  History:  {args.years} years")
    print(f"  Capital:  ${args.capital:,.0f}")
    if trade_start_date:
        print(f"  Trade start: {trade_start_date}")
    print(f"  Slippage: {args.slippage_bps} bps base, "
          f"up to {max([args.slippage_bps, *cost_model.slippage_bps_by_ticker.values()]):.0f} bps for thin ETFs")
    print(f"  Commission: max(${args.commission_minimum:.2f}/order, ${args.commission}/share)")
    print(f"  Fills: {NEXT_OPEN_FILL_MODEL} (decision on close[t] fills at open[t+1])")
    if membership is not None:
        print(
            f"  Universe: point-in-time, {len(membership.all_tickers())} tickers ever "
            f"tradable from {membership.first_snapshot_date} "
            f"(snapshots through {membership.last_snapshot_date})"
        )
    else:
        print(
            "  Universe: STATIC present-day ticker list — SURVIVORSHIP BIASED. "
            "Every reported metric is inflated because the list is made of "
            "names that survived. Pass --universe-snapshots for a baseline "
            "you can act on (docs/operations/backtest-baseline.md)."
        )
    print()

    # 1. Fetch data from IB
    # NOTE: mean_reversion and short_term_mr removed 2026-05-26 — see
    # docs/strategies/mean-reversion-failure-analysis.md
    all_tickers = resolve_backtest_universe(membership)
    if args.bars_from_json:
        print(f"Step 1: Loading cached bars from {args.bars_from_json}...")
        with open(args.bars_from_json) as f:
            cached = json.load(f)
        bars_raw = cached.get("bars") or {}
        all_set = set(all_tickers)
        bars_by_ticker = {
            ticker: [
                {**b, "date": date.fromisoformat(b["date"])}
                for b in bars
            ]
            for ticker, bars in bars_raw.items()
            if ticker in all_set
        }
        missing = [t for t in all_tickers if t not in bars_by_ticker]
        if missing:
            print(f"  WARNING: {len(missing)} required tickers missing from cache: {', '.join(missing[:10])}{'...' if len(missing) > 10 else ''}")
    else:
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

    # Measure how much of the point-in-time universe these bars can actually
    # price. Names that cannot be priced are skipped by the runner, and the
    # ones that go missing are disproportionately the delistings — so an
    # unmeasured exclusion rate is survivorship bias re-entering after the
    # point-in-time universe supposedly removed it (direction doc D14).
    coverage: CoverageReport | None = None
    if membership is not None:
        coverage = measure_coverage(
            membership,
            sessions=collect_sorted_dates(bars_by_ticker),
            priced_tickers=priced_days_from_bars(bars_by_ticker),
        )
        print(
            f"  Universe coverage: {100 - coverage.excluded_pct:.2f}% of "
            f"{coverage.total_membership_days:,} membership-days priceable "
            f"({coverage.state}, floor {coverage.floor_pct:.1f}% excluded)"
        )
        if coverage.state == COVERAGE_BLOCKED:
            worst = sorted(
                coverage.excluded_tickers.items(),
                key=lambda kv: kv[1],
                reverse=True,
            )[:10]
            print(
                "  WARNING: coverage is BLOCKED — this baseline is NOT "
                "like-for-like and the divergence monitor will refuse it. "
                "Worst exclusions: "
                + ", ".join(f"{t} ({d}d)" for t, d in worst)
            )

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
    executor = SimulatedExecutor(cost_model)

    # Build portfolio configurations.
    # NOTE: mean_reversion and short_term_mr sleeves dropped 2026-05-26 after both posted
    # negative expectancy over the 9.97-year backtest. Their signal functions
    # (make_signals_fn, make_short_term_mr_signals_fn) remain in this file for future
    # revival. See docs/strategies/mean-reversion-failure-analysis.md for the failure
    # analysis and the conditions under which the sleeves should be re-enabled.
    # Each equity sleeve's candidate list: every historical index member when a
    # point-in-time calendar is available (the sleeve rankings and the runner
    # both gate per date), else the static present-day list. Scoping this matters
    # because bars_by_ticker is the union of *all* sleeves — an unscoped ranking
    # lets one sleeve buy another's instruments.
    if membership is not None:
        equity_eligible = [
            ticker for ticker in membership.all_tickers()
            if ticker not in ALWAYS_TRADABLE
        ]
        momentum_eligible = equity_eligible + sorted(BEAR_TICKERS)
    else:
        equity_eligible = list(UNIVERSE_REGISTRY["quality_value"])
        momentum_eligible = list(UNIVERSE_REGISTRY["momentum"])

    # One ledger per sleeve — the unfillable-signal count is a per-sleeve
    # verdict, not a portfolio total. Defaulted rather than fixed-key so
    # adding a seventh sleeve cannot crash the run on a missing ledger.
    skip_ledgers: defaultdict[str, SkipLedger] = defaultdict(SkipLedger)
    for name in (
        "momentum", "sector_rotation", "thematic_momentum",
        "quality_value", "earnings_drift", "tail_risk_hedge",
    ):
        skip_ledgers[name]
    if args.whole_shares:
        print("  Whole-share sizing ON (truncate toward zero, as live does)")

    mom_signals_fn = make_momentum_signals_fn(
        whole_shares=args.whole_shares,
        skip_ledger=skip_ledgers["momentum"],
        bars_by_ticker=bars_by_ticker,
        top_n=5,
        lookback_days=126,
        position_size_pct=0.12,
        initial_capital=args.capital * 0.2308,
        trailing_stop_pct=0.10,
        bear_tickers=BEAR_TICKERS,
        eligible_tickers=momentum_eligible,
        membership=membership,
    )
    sector_signals_fn = make_sector_rotation_signals_fn(
        whole_shares=args.whole_shares,
        skip_ledger=skip_ledgers["sector_rotation"],
        bars_by_ticker=bars_by_ticker,
        eligible_tickers=list(UNIVERSE_REGISTRY["sector_rotation"]),
        top_n=3,
        lookback_days=63,
        position_size_pct=0.20,
        initial_capital=args.capital * 0.1538,
        trailing_stop_pct=0.08,
    )
    thematic_signals_fn = make_thematic_momentum_signals_fn(
        whole_shares=args.whole_shares,
        skip_ledger=skip_ledgers["thematic_momentum"],
        bars_by_ticker=bars_by_ticker,
        eligible_tickers=UNIVERSE_REGISTRY["thematic_momentum"],
        top_n=8,
        lookback_days=63,
        position_size_pct=0.135,
        initial_capital=args.capital * 0.1410,
        trailing_stop_pct=0.10,
        regime_by_date=regime_by_date,
        replacement_policy=replacement_policy,
        replacement_score_margin=args.replacement_score_margin,
    )
    qv_signals_fn = make_quality_value_signals_fn(
        whole_shares=args.whole_shares,
        skip_ledger=skip_ledgers["quality_value"],
        fundamentals_lookup=fundamentals_lookup,
        sector_map=SECTOR_MAP,
        bars_by_ticker=bars_by_ticker,
        eligible_tickers=equity_eligible,
        top_n=15,
        position_size_pct=0.06,
        initial_capital=args.capital * 0.1538,
        trailing_stop_pct=0.12,
        regime_by_date=regime_by_date,
        replacement_policy=replacement_policy,
        replacement_score_margin=args.replacement_score_margin,
        membership=membership,
    )
    ed_signals_fn = make_earnings_drift_signals_fn(
        whole_shares=args.whole_shares,
        skip_ledger=skip_ledgers["earnings_drift"],
        earnings_lookup=earnings_lookup,
        surprise_threshold_pct=5.0,
        max_hold_days=20,
        position_size_pct=0.08,
        initial_capital=args.capital * 0.1923,
        trailing_stop_pct=0.06,
        regime_by_date=regime_by_date,
    )
    tail_risk_signals_fn = make_tail_risk_hedge_signals_fn(
        whole_shares=args.whole_shares,
        skip_ledger=skip_ledgers["tail_risk_hedge"],
        regime_by_date=regime_by_date,
        position_size_pct=0.25,
        initial_capital=args.capital * 0.1283,
    )
    portfolios: dict[str, PortfolioConfig] = {
        "momentum": PortfolioConfig(
            name="momentum",
            capital=args.capital * 0.2308,
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
            capital=args.capital * 0.1538,
            signals_fn=sector_signals_fn,
            risk_engine=RiskEngine(
                position_entry_limit_pct=20.0,
                sector_concentration_pct=50.0,
                total_exposure_limit_pct=100.0,
                max_lots_per_ticker=1,
            ),
        ),
        "thematic_momentum": PortfolioConfig(
            name="thematic_momentum",
            capital=args.capital * 0.1410,
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
            capital=args.capital * 0.1538,
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
            capital=args.capital * 0.1923,
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
            capital=args.capital * 0.1283,
            signals_fn=tail_risk_signals_fn,
            risk_engine=RiskEngine(
                position_entry_limit_pct=25.0,
                sector_concentration_pct=50.0,
                total_exposure_limit_pct=100.0,
                max_lots_per_ticker=1,
            ),
        ),
    }

    # Level 3: Crash entry freeze — block new buys during crash regime
    for name, pc in list(portfolios.items()):
        if name == "tail_risk_hedge":
            continue  # Tail-risk hedge operates during crashes
        portfolios[name] = PortfolioConfig(
            name=pc.name,
            capital=pc.capital,
            signals_fn=make_crash_freeze_signals_fn(pc.signals_fn, regime_by_date),
            risk_engine=pc.risk_engine,
        )

    # 2b. Apply ML signal filter if requested
    if args.ml_filter:
        import lightgbm as lgb
        ml_model = lgb.Booster(model_file=args.ml_filter)
        print(f"  ML filter: {args.ml_filter} (threshold={args.ml_threshold})")
        for name, pc in list(portfolios.items()):
            portfolios[name] = PortfolioConfig(
                name=pc.name,
                capital=pc.capital,
                signals_fn=make_ml_filtered_signals_fn(
                    pc.signals_fn, ml_model,
                    threshold=args.ml_threshold,
                    strategy_name=name,
                ),
                risk_engine=pc.risk_engine,
            )

    shadow_recorders = _create_backtest_shadow_recorders(
        enabled=args.research_shadow,
        bars_by_ticker=bars_by_ticker,
        portfolio_names=portfolios,
    )

    # 3. Run backtest for each portfolio
    print(f"Step 3: Running backtest ({len(portfolios)} portfolio(s))...")
    t0 = time.time()
    results: dict[str, BacktestResult] = {}
    for name, pc in portfolios.items():
        print(f"  Running portfolio '{name}' (${pc.capital:,.0f})...")
        runner = BacktestRunner(
            executor=executor,
            initial_capital=pc.capital,
            whole_shares=args.whole_shares,
            skip_ledger=skip_ledgers[name],
        )
        results[name] = runner.run(
            bars_by_ticker,
            pc.signals_fn,
            pc.risk_engine,
            trade_start_date=trade_start_date,
            candidate_observer=shadow_recorders.get(name),
            portfolio_name=name,
            membership=membership,
        )
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

        # Cross-portfolio risk monitoring
        risk_monitor = AggregateRiskMonitor(
            alert_drawdown_pct=15.0,
            circuit_breaker_pct=22.0,
        )
        strategy_drawdowns = {
            name: result.metrics.get("max_drawdown", 0.0)
            for name, result in results.items()
        }
        # Use 2x current drawdown as proxy for historical max in backtest
        # (in live trading, historical_max would come from saved benchmarks)
        historical_max = {name: dd * 0.6 for name, dd in strategy_drawdowns.items()}
        risk_alerts = risk_monitor.monitor(
            aggregate_values=aggregate["portfolio_values"],
            strategy_drawdowns=strategy_drawdowns,
            historical_max_drawdowns=historical_max,
        )
        if risk_alerts:
            print(f"\n  Risk Alerts ({len(risk_alerts)}):")
            for alert in risk_alerts:
                icon = "!!" if alert["level"] == "critical" else " >"
                print(f"    {icon} [{alert['level'].upper()}] {alert['message']}")

    if args.whole_shares:
        print("\n  Unfillable signals (budget < 1 share):")
        for name in portfolios:
            ledger = skip_ledgers[name].to_dict()
            sized = skip_ledgers[name].sized
            tickers = sorted({s["ticker"] for s in ledger["signals"]})
            pct = (ledger["count"] / sized * 100) if sized else 0.0
            print(f"    {name:<20} {ledger['count']:>6} / {sized:<6} "
                  f"({pct:5.1f}%, {len(tickers)} distinct names)")

    # 5. Save results to JSON
    print("\nStep 5: Saving results...")
    base_config = build_base_config(
        all_tickers=all_tickers,
        years=args.years,
        capital=args.capital,
        cost_model=cost_model,
        replacement_policy=replacement_policy.value,
        replacement_score_margin=args.replacement_score_margin,
        portfolio_capitals={name: pc.capital for name, pc in portfolios.items()},
        point_in_time_universe=membership is not None,
        coverage=coverage,
        whole_shares=args.whole_shares,
    )
    if args.ml_filter:
        base_config["ml_filter"] = {
            "model": args.ml_filter,
            "threshold": args.ml_threshold,
            "training_window": ml_metadata,
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
            shadow_candidates=result.shadow_candidates,
            open_positions=result.open_positions,
            skipped_signals=skip_ledgers[next(iter(portfolios))].to_dict(),
        )
    else:
        save_multi_portfolio_results(
            config=base_config,
            results=results,
            portfolio_configs=portfolios,
            aggregate=aggregate,
            bars=bars_by_ticker,
            output_dir=args.output_dir,
            skipped_signals={
                name: ledger.to_dict() for name, ledger in skip_ledgers.items()
            },
            entry_signals_sized={
                name: ledger.sized for name, ledger in skip_ledgers.items()
            },
        )


if __name__ == "__main__":
    main()
