#!/usr/bin/env python3
"""Daily paper trading runner.

Reuses the exact same signal functions from the backtest system.
Fetches latest bars from IB Gateway, runs all 8 signal functions,
and prints resulting signals. State persists to PostgreSQL between runs.

Usage:
    python scripts/run_paper.py --init            # Initialize fresh state
    python scripts/run_paper.py --status           # Print current positions
    python scripts/run_paper.py                    # Daily signal run (requires IB)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from shared.config import load_config
from shared.models.portfolio_config import PortfolioConfig as PortfolioConfigModel
from shared.models.portfolio import Position, Trade
from shared.models.equity_snapshot import EquitySnapshot

from scripts.paper_state import PaperTradingState
from scripts.run_backtest import (
    BEAR_TICKERS,
    PortfolioConfig,
    compute_regime_by_date,
    fetch_bars_from_ib,
    get_union_universe,
    make_crash_freeze_signals_fn,
    make_earnings_drift_signals_fn,
    make_momentum_signals_fn,
    make_quality_value_signals_fn,
    make_sector_rotation_signals_fn,
    make_tail_risk_hedge_signals_fn,
    make_thematic_momentum_signals_fn,
)
from scripts.fetch_fundamentals import load_fundamentals_cache, build_fundamentals_lookup, SECTOR_MAP
from scripts.fetch_earnings import load_earnings_cache, build_earnings_lookup
from backtest._portfolio_state import SimplePortfolioState
from backtest.aggregate_risk import AggregateRiskMonitor
from services.risk_management.engine import RiskEngine

# Capital allocations across the 6 active sleeves.
# mean_reversion and short_term_mr were dropped 2026-05-26 after both posted
# negative trade-level expectancy over the 9.97-year backtest. Their $22K
# combined allocation was redistributed proportionally across the survivors
# (each weight scaled by 100/78). See docs/strategies/mean-reversion-failure-
# analysis.md for the analysis and the macro conditions under which to revive.
CAPITAL_ALLOCATIONS = {
    "momentum": 0.2308,
    "sector_rotation": 0.1538,
    "thematic_momentum": 0.1410,
    "quality_value": 0.1538,
    "earnings_drift": 0.1923,
    "tail_risk_hedge": 0.1283,
}


def build_portfolios(
    capital: float,
    bars_by_ticker: dict[str, list[dict]],
    regime_by_date: dict,
    fundamentals_lookup,
    earnings_lookup,
) -> dict[str, PortfolioConfig]:
    """Build the 6 active portfolio configs (same params as backtest main()).

    mean_reversion and short_term_mr were dropped 2026-05-26 — their signal-fn
    definitions remain in scripts/run_backtest.py for future revival but are
    no longer instantiated here. See docs/strategies/mean-reversion-failure-
    analysis.md for revival conditions.
    """
    portfolios = {}

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
            trailing_stop_pct=0.12, regime_by_date=regime_by_date,
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
            trailing_stop_pct=0.06, regime_by_date=regime_by_date,
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
            trailing_stop_pct=0.10, regime_by_date=regime_by_date,
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

    # Level 3: Crash entry freeze — block new buys during crash regime
    for name, pc in list(portfolios.items()):
        if name == "tail_risk_hedge":
            continue
        portfolios[name] = PortfolioConfig(
            name=pc.name,
            capital=pc.capital,
            signals_fn=make_crash_freeze_signals_fn(pc.signals_fn, regime_by_date),
            risk_engine=pc.risk_engine,
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

    for name in state.get_portfolio_names():
        capital = state.get_capital(name)
        cash = state.get_cash(name)
        positions = state.get_positions(name)
        n_pos = len(positions)
        total_positions += n_pos
        total_capital += capital

        market_value = sum(
            pos["quantity"] * pos["avg_entry_price"]
            for pos in positions.values()
        )
        equity = cash + market_value
        total_equity += equity
        pnl = equity - capital
        n_trades = len(state.get_trades(name))

        print(f"\n  --- {name} ---")
        print(f"    Capital:    ${capital:>12,.2f}")
        print(f"    Cash:       ${cash:>12,.2f}")
        print(f"    Equity:     ${equity:>12,.2f}")
        print(f"    P&L:        ${pnl:>+12,.2f}")
        print(f"    Positions:  {n_pos}")
        print(f"    Trades:     {n_trades}")

        if positions:
            for ticker, pos in positions.items():
                print(f"      {ticker:>6s}  {pos['quantity']:>8.4f} shares @ ${pos['avg_entry_price']:.2f}")

    print(f"\n  --- TOTAL ---")
    print(f"    Capital:    ${total_capital:>12,.2f}")
    print(f"    Equity:     ${total_equity:>12,.2f}")
    print(f"    P&L:        ${total_equity - total_capital:>+12,.2f}")
    print(f"    Positions:  {total_positions}")

    # Risk monitoring
    risk_monitor = AggregateRiskMonitor(
        alert_drawdown_pct=15.0,
        circuit_breaker_pct=22.0,
    )
    # Check aggregate drawdown from capital
    aggregate_values = [total_capital, total_equity]
    risk_alerts = risk_monitor.check_aggregate_drawdown(aggregate_values)
    if risk_alerts:
        print(f"\n  RISK ALERTS:")
        for alert in risk_alerts:
            icon = "!!" if alert["level"] == "critical" else " >"
            print(f"    {icon} [{alert['level'].upper()}] {alert['message']}")

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

    # Build current prices from latest bar close
    current_prices: dict[str, float] = {}
    for ticker, bars in bars_by_ticker.items():
        if bars:
            current_prices[ticker] = bars[-1]["close"]

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
                signal["ticker"] = ticker

                action = signal["action"]
                price = signal["limit_price"]
                qty = signal.get("quantity", 0)

                if action == "buy":
                    from shared.universe import ETF_SECTORS

                    sector = SECTOR_MAP.get(ticker) or ETF_SECTORS.get(ticker)

                    # Gate through the sleeve's RiskEngine exactly like the
                    # backtest runner does — without it the sim books every
                    # buy unconstrained (quality_value went to -$43K cash /
                    # 3.8x leverage on day one).
                    positions = state.get_positions(name)
                    sleeve_cash = state.get_cash(name)
                    nav = sleeve_cash + sum(
                        p["quantity"] * current_prices.get(t, p["avg_entry_price"])
                        for t, p in positions.items()
                    )
                    portfolio_state = SimplePortfolioState(
                        nav=nav,
                        peak_nav=nav,
                        positions={
                            t: {"quantity": p["quantity"]} for t, p in positions.items()
                        },
                        # {} matches the backtest gate (sector limits are
                        # enforced account-level by the risk service instead)
                        sector_exposure={},
                        # market value / nav — same computation as the
                        # backtest's _make_simple_portfolio
                        total_exposure_pct=(
                            ((nav - sleeve_cash) / nav) * 100.0 if nav > 0 else 0.0
                        ),
                        margin_utilization_pct=0.0,
                    )
                    decision = pc.risk_engine.check_entry(
                        ticker=ticker,
                        quantity=qty,
                        price=price,
                        sector=sector or "Unknown",
                        portfolio=portfolio_state,
                        existing_lots=1 if ticker in positions else 0,
                    )
                    if not decision.approved:
                        print(f"  SKIP {ticker:>6s}  {qty:>8.4f} @ ${price:>8.2f}  [{name}] ({decision.reason})")
                        continue
                    if decision.adjusted_quantity and decision.adjusted_quantity != qty:
                        qty = decision.adjusted_quantity
                        signal["quantity"] = qty

                    signals_generated.append(signal)
                    print(f"  BUY  {ticker:>6s}  {qty:>8.4f} @ ${price:>8.2f}  [{name}]")
                elif action == "sell":
                    # Exits are never gated (matching the backtest runner:
                    # "Sell signals for existing positions are always processed").
                    signals_generated.append(signal)
                    reason = signal.get("exit_reason", "signal")
                    print(f"  SELL {ticker:>6s}             @ ${price:>8.2f}  [{name}] ({reason})")

        # After all signals for this portfolio, update peaks and record snapshot
        state.update_peak_prices(name, current_prices)
        equity = state.compute_equity(name, current_prices)
        cash = state.get_cash(name)
        market_value = equity - cash
        state.record_equity_snapshot(name, today, equity, cash, market_value)

    # Record aggregate equity snapshot
    total_equity = 0.0
    total_cash = 0.0
    for name in state.get_portfolio_names():
        total_equity += state.compute_equity(name, current_prices)
        total_cash += state.get_cash(name)
    total_market_value = total_equity - total_cash
    state.record_equity_snapshot("_aggregate", today, total_equity, total_cash, total_market_value)

    return signals_generated


def publish_recommendations(signals: list[dict], redis_url: str, run_date: date) -> int:
    """Publish sleeve signals to stream:recommendations (the pipeline bridge).

    Each signal becomes a RecommendationMessage carrying the sleeve's own
    limit price and sizing; the risk service gates it and the execution
    service places a real IB paper order. Deterministic recommendation ids
    (date+portfolio+ticker+action) make re-runs idempotent downstream.

    Returns the number of recommendations published.
    """
    import redis as redis_sync

    from shared.schemas.messages import RecommendationMessage

    conn = redis_sync.Redis.from_url(redis_url)
    published = 0
    try:
        for signal in signals:
            action = signal.get("action")
            if action not in ("buy", "sell"):
                continue
            message = RecommendationMessage(
                ticker=signal["ticker"],
                timestamp=datetime.now(timezone.utc),
                action=action,
                confidence=1.0,  # rule-based sleeves carry no model confidence
                top_features={},
                recommendation_id=(
                    f"sleeve-{run_date}-{signal['portfolio']}-"
                    f"{signal['ticker']}-{action}"
                ),
                limit_price=signal.get("limit_price"),
                quantity=signal.get("quantity") or None,
                portfolio=signal.get("portfolio"),
            )
            conn.xadd("stream:recommendations", message.to_stream_dict())
            published += 1
    finally:
        conn.close()
    return published


# The four tables that hold all paper trading state, in delete-safe order.
STATE_TABLES = (EquitySnapshot, Trade, Position, PortfolioConfigModel)


def dump_paper_state(session: Session, out_path: Path) -> Path:
    """Serialize all paper state tables to JSON before any destructive change."""
    payload = {}
    for model in STATE_TABLES:
        columns = [c.name for c in model.__table__.columns]
        payload[model.__table__.name] = [
            {col: getattr(row, col) for col in columns}
            for row in session.query(model).all()
        ]
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    return out_path


def reset_paper_state(session: Session) -> None:
    """Delete all rows from the paper state tables. Callers MUST have already
    confirmed interactively and written a backup (see the --reset CLI path)."""
    for model in STATE_TABLES:
        session.execute(model.__table__.delete())
    session.commit()


def make_db_session(db_url: str) -> Session:
    """Create a SQLAlchemy session from a database URL."""
    engine = create_engine(db_url)
    session_factory = sessionmaker(bind=engine)
    return session_factory()


def main():
    # Load defaults from config (may fail if config file missing, that's OK)
    try:
        _config = load_config("config/default.yaml")
        default_db_url = _config.database.url
        default_redis_url = _config.redis.url
    except Exception:
        default_db_url = "postgresql://algo:algo@localhost:5432/algo_poc"
        default_redis_url = "redis://localhost:6379/0"

    parser = argparse.ArgumentParser(description="Daily paper trading runner")
    parser.add_argument("--capital", type=float, default=100_000,
                        help="Total capital (default: 100000)")
    parser.add_argument("--db-url", default=default_db_url,
                        help="PostgreSQL database URL")
    parser.add_argument("--years", type=int, default=1,
                        help="Years of historical bars for signal warmup (default: 1)")
    parser.add_argument("--init", action="store_true",
                        help="Initialize fresh paper trading state")
    parser.add_argument("--status", action="store_true",
                        help="Print current status and exit")
    parser.add_argument("--reset", action="store_true",
                        help="Wipe all paper trading state tables")
    parser.add_argument("--ib-host", default="127.0.0.1")
    parser.add_argument("--ib-port", type=int, default=7497)
    parser.add_argument("--publish", action="store_true",
                        help="Also publish signals to stream:recommendations so the "
                             "risk/execution services place real IB paper orders")
    parser.add_argument("--redis-url", default=default_redis_url,
                        help="Redis URL for --publish")
    args = parser.parse_args()

    session = make_db_session(args.db_url)

    # --reset: wipe all paper state tables. Interactive-only by design: on
    # 2026-07-10 an agent piped `echo yes |` past the old prompt and wiped the
    # live paper book. A human at a real terminal is the only accepted input.
    if args.reset:
        if not sys.stdin.isatty():
            print("Refusing --reset: stdin is not a TTY. This wipes all paper "
                  "trading state and must be run by a human in an interactive "
                  "terminal — piping confirmation is not accepted. "
                  "(See CLAUDE.md/AGENTS.md 'Destructive Actions'.)")
            session.close()
            sys.exit(2)
        confirm = input("This will DELETE all paper trading data. Type 'yes' to confirm: ")
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            session.close()
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup = dump_paper_state(session, Path("output") / f"paper_state_pre_reset_{stamp}.json")
        print(f"Pre-reset backup written to {backup}")
        reset_paper_state(session)
        print("All paper trading state wiped.")
        session.close()
        return

    # --init: create fresh state
    if args.init:
        capitals = {name: args.capital * pct for name, pct in CAPITAL_ALLOCATIONS.items()}
        PaperTradingState.create_new(capitals, session)
        session.commit()
        print(f"Initialized paper trading state in database")
        print(f"Total capital: ${args.capital:,.0f}")
        for name, cap in capitals.items():
            print(f"  {name}: ${cap:,.0f}")
        session.close()
        return

    # --status: print current state
    if args.status:
        try:
            state = PaperTradingState.load(session)
        except ValueError as e:
            print(str(e))
            sys.exit(1)
        print_status(state)
        session.close()
        return

    # Daily run
    try:
        state = PaperTradingState.load(session)
    except ValueError as e:
        print(str(e))
        sys.exit(1)

    print(f"Paper Trading Daily Run - {date.today()}")
    print(f"State loaded from database")

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

    # Signal evaluation never projects fills.  Actual IB executions are the
    # only input allowed to mutate durable cash and positions.
    print(f"\nRunning signals across {len(portfolios)} portfolios...")
    try:
        signals = run_daily(state, portfolios, bars_by_ticker)
        session.commit()
    except Exception:
        session.rollback()
        print("\nERROR: daily run failed; database changes rolled back")
        raise
    finally:
        session.close()

    if signals:
        print(f"\n{len(signals)} signals generated")
    else:
        print("\nNo signals generated today")
    print(f"\nState committed to database")

    # Bridge to the service pipeline: publish the same signals as
    # recommendations so risk gates them and execution places real IB paper
    # orders. Deliberately after the DB commit — the simulated book (the
    # divergence benchmark) is never blocked by the pipeline being down.
    if args.publish and signals:
        try:
            count = publish_recommendations(signals, args.redis_url, date.today())
            print(f"{count} recommendations published to stream:recommendations")
        except Exception as e:
            print(f"WARNING: publish to pipeline failed ({e}); simulated state is committed")


if __name__ == "__main__":
    main()
