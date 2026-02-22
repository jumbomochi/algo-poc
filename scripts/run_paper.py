#!/usr/bin/env python3
"""Daily paper trading runner.

Reuses the exact same signal functions from the backtest system.
Fetches latest bars from IB Gateway, runs all 8 signal functions,
and prints resulting signals. State persists to JSON between runs.

Usage:
    python scripts/run_paper.py --init            # Initialize fresh state
    python scripts/run_paper.py --status           # Print current positions
    python scripts/run_paper.py                    # Daily signal run (requires IB)
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from scripts.paper_state import PaperTradingState
from scripts.run_backtest import (
    BEAR_TICKERS,
    PortfolioConfig,
    compute_regime_by_date,
    fetch_bars_from_ib,
    get_union_universe,
    make_earnings_drift_signals_fn,
    make_momentum_signals_fn,
    make_quality_value_signals_fn,
    make_sector_rotation_signals_fn,
    make_short_term_mr_signals_fn,
    make_signals_fn,
    make_tail_risk_hedge_signals_fn,
    make_thematic_momentum_signals_fn,
)
from scripts.fetch_fundamentals import load_fundamentals_cache, build_fundamentals_lookup, SECTOR_MAP
from scripts.fetch_earnings import load_earnings_cache, build_earnings_lookup
from services.risk_management.engine import RiskEngine


DEFAULT_STATE_PATH = "data/paper_state.json"

CAPITAL_ALLOCATIONS = {
    "mean_reversion": 0.12,
    "momentum": 0.18,
    "sector_rotation": 0.12,
    "quality_value": 0.12,
    "earnings_drift": 0.15,
    "short_term_mr": 0.10,
    "thematic_momentum": 0.11,
    "tail_risk_hedge": 0.10,
}


def build_portfolios(
    capital: float,
    bars_by_ticker: dict[str, list[dict]],
    regime_by_date: dict,
    fundamentals_lookup,
    earnings_lookup,
) -> dict[str, PortfolioConfig]:
    """Build all 8 portfolio configs (same params as backtest main())."""
    portfolios = {}

    mr_cap = capital * CAPITAL_ALLOCATIONS["mean_reversion"]
    portfolios["mean_reversion"] = PortfolioConfig(
        name="mean_reversion",
        capital=mr_cap,
        signals_fn=make_signals_fn(
            position_size_pct=0.12, initial_capital=mr_cap, trailing_stop_pct=0.10,
        ),
        risk_engine=RiskEngine(
            position_entry_limit_pct=15.0, sector_concentration_pct=30.0,
            total_exposure_limit_pct=120.0, max_lots_per_ticker=2,
        ),
    )

    mom_cap = capital * CAPITAL_ALLOCATIONS["momentum"]
    portfolios["momentum"] = PortfolioConfig(
        name="momentum",
        capital=mom_cap,
        signals_fn=make_momentum_signals_fn(
            bars_by_ticker=bars_by_ticker, top_n=5, lookback_days=126,
            position_size_pct=0.12, initial_capital=mom_cap,
            trailing_stop_pct=0.10, bear_tickers=BEAR_TICKERS,
        ),
        risk_engine=RiskEngine(
            position_entry_limit_pct=12.0, sector_concentration_pct=30.0,
            total_exposure_limit_pct=150.0, max_lots_per_ticker=1,
        ),
    )

    sec_cap = capital * CAPITAL_ALLOCATIONS["sector_rotation"]
    portfolios["sector_rotation"] = PortfolioConfig(
        name="sector_rotation",
        capital=sec_cap,
        signals_fn=make_sector_rotation_signals_fn(
            bars_by_ticker=bars_by_ticker, top_n=3, lookback_days=63,
            position_size_pct=0.20, initial_capital=sec_cap, trailing_stop_pct=0.08,
        ),
        risk_engine=RiskEngine(
            position_entry_limit_pct=20.0, sector_concentration_pct=50.0,
            total_exposure_limit_pct=100.0, max_lots_per_ticker=1,
        ),
    )

    qv_cap = capital * CAPITAL_ALLOCATIONS["quality_value"]
    portfolios["quality_value"] = PortfolioConfig(
        name="quality_value",
        capital=qv_cap,
        signals_fn=make_quality_value_signals_fn(
            fundamentals_lookup=fundamentals_lookup, sector_map=SECTOR_MAP,
            top_n=15, position_size_pct=0.10, initial_capital=qv_cap,
            trailing_stop_pct=0.12,
        ),
        risk_engine=RiskEngine(
            position_entry_limit_pct=10.0, sector_concentration_pct=30.0,
            total_exposure_limit_pct=100.0, max_lots_per_ticker=1,
        ),
    )

    ed_cap = capital * CAPITAL_ALLOCATIONS["earnings_drift"]
    portfolios["earnings_drift"] = PortfolioConfig(
        name="earnings_drift",
        capital=ed_cap,
        signals_fn=make_earnings_drift_signals_fn(
            earnings_lookup=earnings_lookup, surprise_threshold_pct=5.0,
            max_hold_days=20, position_size_pct=0.08, initial_capital=ed_cap,
            trailing_stop_pct=0.06,
        ),
        risk_engine=RiskEngine(
            position_entry_limit_pct=8.0, sector_concentration_pct=30.0,
            total_exposure_limit_pct=100.0, max_lots_per_ticker=1,
        ),
    )

    stmr_cap = capital * CAPITAL_ALLOCATIONS["short_term_mr"]
    portfolios["short_term_mr"] = PortfolioConfig(
        name="short_term_mr",
        capital=stmr_cap,
        signals_fn=make_short_term_mr_signals_fn(
            position_size_pct=0.08, initial_capital=stmr_cap, max_hold_days=5,
        ),
        risk_engine=RiskEngine(
            position_entry_limit_pct=8.0, sector_concentration_pct=30.0,
            total_exposure_limit_pct=100.0, max_lots_per_ticker=1,
        ),
    )

    them_cap = capital * CAPITAL_ALLOCATIONS["thematic_momentum"]
    portfolios["thematic_momentum"] = PortfolioConfig(
        name="thematic_momentum",
        capital=them_cap,
        signals_fn=make_thematic_momentum_signals_fn(
            bars_by_ticker=bars_by_ticker, top_n=8, lookback_days=63,
            position_size_pct=0.15, initial_capital=them_cap,
            trailing_stop_pct=0.10,
        ),
        risk_engine=RiskEngine(
            position_entry_limit_pct=15.0, sector_concentration_pct=50.0,
            total_exposure_limit_pct=120.0, max_lots_per_ticker=1,
        ),
    )

    tr_cap = capital * CAPITAL_ALLOCATIONS["tail_risk_hedge"]
    portfolios["tail_risk_hedge"] = PortfolioConfig(
        name="tail_risk_hedge",
        capital=tr_cap,
        signals_fn=make_tail_risk_hedge_signals_fn(
            regime_by_date=regime_by_date, position_size_pct=0.25,
            initial_capital=tr_cap,
        ),
        risk_engine=RiskEngine(
            position_entry_limit_pct=25.0, sector_concentration_pct=50.0,
            total_exposure_limit_pct=100.0, max_lots_per_ticker=1,
        ),
    )

    return portfolios


def print_status(state: PaperTradingState) -> None:
    """Print current paper trading status."""
    print("\n" + "=" * 60)
    print("  PAPER TRADING STATUS")
    print("=" * 60)

    total_equity = 0.0
    total_capital = 0.0
    total_positions = 0

    for name, pf in state.portfolios.items():
        cash = pf["cash"]
        positions = pf["positions"]
        n_pos = len(positions)
        total_positions += n_pos
        total_capital += pf["capital"]

        market_value = sum(
            pos["quantity"] * pos["entry_price"]
            for pos in positions.values()
        )
        equity = cash + market_value
        total_equity += equity
        pnl = equity - pf["capital"]
        n_trades = len(pf["trades"])

        print(f"\n  --- {name} ---")
        print(f"    Capital:    ${pf['capital']:>12,.2f}")
        print(f"    Cash:       ${cash:>12,.2f}")
        print(f"    Equity:     ${equity:>12,.2f}")
        print(f"    P&L:        ${pnl:>+12,.2f}")
        print(f"    Positions:  {n_pos}")
        print(f"    Trades:     {n_trades}")

        if positions:
            for ticker, pos in positions.items():
                print(f"      {ticker:>6s}  {pos['quantity']:>4d} shares @ ${pos['entry_price']:.2f}")

    print(f"\n  --- TOTAL ---")
    print(f"    Capital:    ${total_capital:>12,.2f}")
    print(f"    Equity:     ${total_equity:>12,.2f}")
    print(f"    P&L:        ${total_equity - total_capital:>+12,.2f}")
    print(f"    Positions:  {total_positions}")
    print("=" * 60)


def run_daily(
    state: PaperTradingState,
    portfolios: dict[str, PortfolioConfig],
    bars_by_ticker: dict[str, list[dict]],
) -> list[dict]:
    """Run one daily cycle: generate signals for all portfolios.

    Returns list of signals generated (for logging/review).
    """
    signals_generated: list[dict] = []
    today = date.today()

    for name, pc in portfolios.items():
        universe = list(bars_by_ticker.keys())

        for ticker in universe:
            bars = bars_by_ticker.get(ticker, [])
            if not bars:
                continue

            signal = pc.signals_fn(ticker, bars)
            if signal is not None:
                signal["portfolio"] = name
                signal["date"] = str(today)
                signals_generated.append(signal)

                action = signal["action"]
                price = signal["limit_price"]
                qty = signal.get("quantity", 0)

                if action == "buy":
                    print(f"  BUY  {ticker:>6s}  {qty:>4d} @ ${price:>8.2f}  [{name}]")
                elif action == "sell":
                    reason = signal.get("exit_reason", "signal")
                    print(f"  SELL {ticker:>6s}             @ ${price:>8.2f}  [{name}] ({reason})")

    return signals_generated


def main():
    parser = argparse.ArgumentParser(description="Daily paper trading runner")
    parser.add_argument("--capital", type=float, default=100_000,
                        help="Total capital (default: 100000)")
    parser.add_argument("--state-file", default=DEFAULT_STATE_PATH,
                        help="Path to state JSON file")
    parser.add_argument("--years", type=int, default=1,
                        help="Years of historical bars for signal warmup (default: 1)")
    parser.add_argument("--init", action="store_true",
                        help="Initialize fresh paper trading state")
    parser.add_argument("--status", action="store_true",
                        help="Print current status and exit")
    parser.add_argument("--ib-host", default="127.0.0.1")
    parser.add_argument("--ib-port", type=int, default=7497)
    args = parser.parse_args()

    # --init: create fresh state
    if args.init:
        capitals = {name: args.capital * pct for name, pct in CAPITAL_ALLOCATIONS.items()}
        state = PaperTradingState.create_new(capitals, args.state_file)
        state.save()
        print(f"Initialized paper trading state at {args.state_file}")
        print(f"Total capital: ${args.capital:,.0f}")
        for name, cap in capitals.items():
            print(f"  {name}: ${cap:,.0f}")
        return

    # --status: print current state
    if args.status:
        try:
            state = PaperTradingState.load(args.state_file)
        except FileNotFoundError:
            print(f"No state file found at {args.state_file}")
            print("Run with --init to create one.")
            sys.exit(1)
        print_status(state)
        return

    # Daily run
    try:
        state = PaperTradingState.load(args.state_file)
    except FileNotFoundError:
        print(f"No state file found at {args.state_file}")
        print("Run with --init to create one.")
        sys.exit(1)

    print(f"Paper Trading Daily Run - {date.today()}")
    print(f"State loaded from {args.state_file}")

    # Fetch bars from IB
    all_tickers = get_union_universe(list(CAPITAL_ALLOCATIONS.keys()))
    print(f"\nFetching bars for {len(all_tickers)} tickers ({args.years} year)...")
    bars_by_ticker = fetch_bars_from_ib(
        tickers=all_tickers,
        years=args.years,
        host=args.ib_host,
        port=args.ib_port,
    )

    if not bars_by_ticker:
        print("ERROR: No data fetched. Is IB Gateway running?")
        sys.exit(1)

    # Load caches
    fundamentals_cache = load_fundamentals_cache("data/cache/fundamentals.json")
    earnings_cache = load_earnings_cache("data/cache/earnings.json")
    fundamentals_lookup = build_fundamentals_lookup(fundamentals_cache)
    earnings_lookup = build_earnings_lookup(earnings_cache, window_days=2)

    # Compute regime
    regime_by_date = compute_regime_by_date(bars_by_ticker)

    # Build portfolios
    portfolios = build_portfolios(
        capital=args.capital,
        bars_by_ticker=bars_by_ticker,
        regime_by_date=regime_by_date,
        fundamentals_lookup=fundamentals_lookup,
        earnings_lookup=earnings_lookup,
    )

    # Run signals
    print(f"\nRunning signals across {len(portfolios)} portfolios...")
    signals = run_daily(state, portfolios, bars_by_ticker)

    if signals:
        print(f"\n{len(signals)} signals generated")
    else:
        print("\nNo signals generated today")

    # Save state
    state.save()
    print(f"\nState saved to {args.state_file}")


if __name__ == "__main__":
    main()
