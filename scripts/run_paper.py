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

# ruff: noqa: E402 -- direct-script execution needs the repo root first.

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

# When invoked as ``python scripts/run_paper.py``, Python otherwise resolves
# editable-package imports from the primary checkout instead of this worktree.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from shared.config import AppConfig, load_config
from shared.models.portfolio_config import PortfolioConfig as PortfolioConfigModel
from shared.models.portfolio import Position, Trade
from shared.models.equity_snapshot import EquitySnapshot

from scripts.paper_state import PaperTradingState
from scripts.run_backtest import (
    BEAR_TICKERS,
    PortfolioConfig,
    UNIVERSE_REGISTRY,
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
from scripts.fetch_fundamentals import (
    load_fundamentals_cache,
    build_fundamentals_lookup,
    SECTOR_MAP,
)
from scripts.fetch_earnings import load_earnings_cache, build_earnings_lookup
from backtest._portfolio_state import SimplePortfolioState
from backtest.portfolio_context import PortfolioContext
from backtest.ranked_selection import ReplacementPolicy
from backtest.aggregate_risk import AggregateRiskMonitor
from services.risk_management.engine import RiskEngine
from shared.order_ledger import OrderLedger
from shared.capital import CapitalBudget, calculate_capital_budget
from shared.broker_state import BrokerAccountSnapshot
from shared.models import CapitalSnapshot, OrderIntent, OrderStatus
from shared.observability import DEFAULT_TRADING_METRICS
from services.execution.ib_account import IBAccountReader
from services.execution.reconciliation import ReconciliationResult

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
    portfolio_contexts: dict[str, PortfolioContext] | None = None,
) -> dict[str, PortfolioConfig]:
    """Build the 6 active portfolio configs (same params as backtest main()).

    mean_reversion and short_term_mr were dropped 2026-05-26 — their signal-fn
    definitions remain in scripts/run_backtest.py for future revival but are
    no longer instantiated here. See docs/strategies/mean-reversion-failure-
    analysis.md for revival conditions.
    """
    portfolios = {}
    contexts = portfolio_contexts or {}

    mom_cap = capital * CAPITAL_ALLOCATIONS["momentum"]
    portfolios["momentum"] = PortfolioConfig(
        name="momentum",
        capital=mom_cap,
        signals_fn=make_momentum_signals_fn(
            bars_by_ticker=bars_by_ticker,
            top_n=5,
            lookback_days=126,
            position_size_pct=0.12,
            initial_capital=mom_cap,
            trailing_stop_pct=0.10,
            bear_tickers=BEAR_TICKERS,
            portfolio_context=contexts.get("momentum"),
        ),
        risk_engine=RiskEngine(
            position_entry_limit_pct=12.0,
            sector_concentration_pct=30.0,
            total_exposure_limit_pct=150.0,
            max_lots_per_ticker=1,
        ),
    )

    sec_cap = capital * CAPITAL_ALLOCATIONS["sector_rotation"]
    portfolios["sector_rotation"] = PortfolioConfig(
        name="sector_rotation",
        capital=sec_cap,
        signals_fn=make_sector_rotation_signals_fn(
            bars_by_ticker=bars_by_ticker,
            top_n=3,
            lookback_days=63,
            position_size_pct=0.20,
            initial_capital=sec_cap,
            trailing_stop_pct=0.08,
            portfolio_context=contexts.get("sector_rotation"),
        ),
        risk_engine=RiskEngine(
            position_entry_limit_pct=20.0,
            sector_concentration_pct=50.0,
            total_exposure_limit_pct=100.0,
            max_lots_per_ticker=1,
        ),
    )

    qv_cap = capital * CAPITAL_ALLOCATIONS["quality_value"]
    portfolios["quality_value"] = PortfolioConfig(
        name="quality_value",
        capital=qv_cap,
        signals_fn=make_quality_value_signals_fn(
            fundamentals_lookup=fundamentals_lookup,
            sector_map=SECTOR_MAP,
            bars_by_ticker=bars_by_ticker,
            eligible_tickers=UNIVERSE_REGISTRY["quality_value"],
            top_n=15,
            position_size_pct=0.06,
            initial_capital=qv_cap,
            trailing_stop_pct=0.12,
            regime_by_date=regime_by_date,
            portfolio_context=contexts.get("quality_value"),
            replacement_policy=ReplacementPolicy.TECHNICAL_ONLY,
        ),
        risk_engine=RiskEngine(
            position_entry_limit_pct=10.0,
            sector_concentration_pct=30.0,
            total_exposure_limit_pct=100.0,
            max_lots_per_ticker=1,
        ),
    )

    ed_cap = capital * CAPITAL_ALLOCATIONS["earnings_drift"]
    portfolios["earnings_drift"] = PortfolioConfig(
        name="earnings_drift",
        capital=ed_cap,
        signals_fn=make_earnings_drift_signals_fn(
            earnings_lookup=earnings_lookup,
            surprise_threshold_pct=5.0,
            max_hold_days=20,
            position_size_pct=0.08,
            initial_capital=ed_cap,
            trailing_stop_pct=0.06,
            regime_by_date=regime_by_date,
            portfolio_context=contexts.get("earnings_drift"),
        ),
        risk_engine=RiskEngine(
            position_entry_limit_pct=8.0,
            sector_concentration_pct=30.0,
            total_exposure_limit_pct=100.0,
            max_lots_per_ticker=1,
        ),
    )

    them_cap = capital * CAPITAL_ALLOCATIONS["thematic_momentum"]
    portfolios["thematic_momentum"] = PortfolioConfig(
        name="thematic_momentum",
        capital=them_cap,
        signals_fn=make_thematic_momentum_signals_fn(
            bars_by_ticker=bars_by_ticker,
            eligible_tickers=UNIVERSE_REGISTRY["thematic_momentum"],
            top_n=8,
            lookback_days=63,
            position_size_pct=0.135,
            initial_capital=them_cap,
            trailing_stop_pct=0.10,
            regime_by_date=regime_by_date,
            portfolio_context=contexts.get("thematic_momentum"),
            replacement_policy=ReplacementPolicy.TECHNICAL_ONLY,
        ),
        risk_engine=RiskEngine(
            position_entry_limit_pct=15.0,
            sector_concentration_pct=50.0,
            total_exposure_limit_pct=120.0,
            max_lots_per_ticker=1,
        ),
    )

    tr_cap = capital * CAPITAL_ALLOCATIONS["tail_risk_hedge"]
    portfolios["tail_risk_hedge"] = PortfolioConfig(
        name="tail_risk_hedge",
        capital=tr_cap,
        signals_fn=make_tail_risk_hedge_signals_fn(
            regime_by_date=regime_by_date,
            position_size_pct=0.25,
            initial_capital=tr_cap,
            portfolio_context=contexts.get("tail_risk_hedge"),
        ),
        risk_engine=RiskEngine(
            position_entry_limit_pct=25.0,
            sector_concentration_pct=50.0,
            total_exposure_limit_pct=100.0,
            max_lots_per_ticker=1,
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


def build_portfolio_contexts(
    state: PaperTradingState,
    session: Session,
    capital: float | None = None,
    *,
    sleeve_budgets: Mapping[str, float] | None = None,
    account_id: str | None = None,
) -> dict[str, PortfolioContext]:
    """Build per-sleeve strategy state before creating signal factories."""
    ledger = OrderLedger(session)
    pending_orders = ledger.load_pending_orders(account_id=account_id)
    budgets = dict(sleeve_budgets or {})
    if not budgets:
        if capital is None:
            raise ValueError("capital or sleeve_budgets is required")
        budgets = {
            name: capital * weight for name, weight in CAPITAL_ALLOCATIONS.items()
        }
    return {
        name: state.build_portfolio_context(
            name,
            pending_orders=pending_orders,
            sleeve_budget=budgets[name],
            reserved_notional=ledger.active_reservations(name, account_id=account_id),
            account_id=account_id,
        )
        for name in CAPITAL_ALLOCATIONS
    }


@dataclass(frozen=True)
class DailyRunPreparation:
    broker_snapshot: BrokerAccountSnapshot
    reconciliation: ReconciliationResult
    capital: CapitalBudget
    capital_snapshot: CapitalSnapshot


def prepare_daily_run(
    *, broker_snapshot: BrokerAccountSnapshot, config: Any, session: Session
) -> DailyRunPreparation:
    """Reconcile broker truth and persist the NAV-derived daily budget."""
    from scripts.reconcile_paper import reconcile_snapshot

    if broker_snapshot.mode != config.mode:
        raise ValueError(
            f"broker snapshot mode {broker_snapshot.mode!r} does not match "
            f"configured mode {config.mode!r}"
        )
    reconciliation, _ = reconcile_snapshot(session, broker_snapshot)
    capital = calculate_capital_budget(
        broker_snapshot.net_liquidation_trading_equivalent,
        config.mode,
        config.capital,
        CAPITAL_ALLOCATIONS,
    )
    capital_snapshot = CapitalSnapshot(
        account_id=broker_snapshot.account_id,
        mode=config.mode,
        net_liquidation=capital.net_liquidation,
        deployment_fraction=capital.deployment_fraction,
        max_deployable_usd=capital.max_deployable_usd,
        deployable_capital=capital.deployable_capital,
        sleeve_budgets=capital.sleeve_budgets,
        reconciliation_status=reconciliation.severity,
        captured_at=broker_snapshot.captured_at,
    )
    session.add(capital_snapshot)
    session.flush()
    DEFAULT_TRADING_METRICS.deployable_capital.set(capital.deployable_capital)
    DEFAULT_TRADING_METRICS.reconciliation_entries_allowed.set(
        1 if reconciliation.entries_allowed else 0
    )
    ledger = OrderLedger(session)
    for portfolio, budget in capital.sleeve_budgets.items():
        DEFAULT_TRADING_METRICS.sleeve_budget.labels(portfolio=portfolio).set(budget)
        DEFAULT_TRADING_METRICS.reserved_notional.labels(portfolio=portfolio).set(
            ledger.active_reservations(portfolio, account_id=broker_snapshot.account_id)
        )
    return DailyRunPreparation(
        broker_snapshot=broker_snapshot,
        reconciliation=reconciliation,
        capital=capital,
        capital_snapshot=capital_snapshot,
    )


def build_sell_availability(
    session: Session, broker_snapshot: BrokerAccountSnapshot
) -> dict[str, float]:
    """Return broker-held quantity not already covered by active sells."""
    by_con_id = {
        con_id: max(0.0, float(position.quantity))
        for con_id, position in broker_snapshot.positions.items()
    }
    broker_order_ids: set[str] = set()
    for order_id, order in broker_snapshot.open_orders.items():
        if str(order.action).upper() != "SELL":
            continue
        broker_order_ids.add(str(order_id))
        by_con_id[order.con_id] = max(
            0.0, by_con_id.get(order.con_id, 0.0) - order.remaining_quantity
        )

    pending_sells = session.scalars(
        select(OrderIntent).where(
            OrderIntent.account_id == broker_snapshot.account_id,
            OrderIntent.mode == broker_snapshot.mode,
            func.upper(OrderIntent.action) == "SELL",
            or_(
                OrderIntent.status.in_(
                    (
                        OrderStatus.APPROVED.value,
                        OrderStatus.SUBMITTED.value,
                        OrderStatus.PARTIALLY_FILLED.value,
                    )
                ),
                (
                    (OrderIntent.status == OrderStatus.PROPOSED.value)
                    & OrderIntent.published_at.is_not(None)
                ),
            ),
        )
    )
    for intent in pending_sells:
        if (
            intent.ib_order_id is not None
            and str(intent.ib_order_id) in broker_order_ids
        ):
            continue
        remaining = max(
            0.0, float(intent.requested_quantity) - float(intent.filled_quantity)
        )
        by_con_id[intent.con_id] = max(
            0.0, by_con_id.get(intent.con_id, 0.0) - remaining
        )

    return {
        position.symbol: by_con_id.get(con_id, 0.0)
        for con_id, position in broker_snapshot.positions.items()
    }


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
            pos["quantity"] * pos["avg_entry_price"] for pos in positions.values()
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
                print(
                    f"      {ticker:>6s}  {pos['quantity']:>8.4f} shares @ ${pos['avg_entry_price']:.2f}"
                )

    print("\n  --- TOTAL ---")
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
        print("\n  RISK ALERTS:")
        for alert in risk_alerts:
            icon = "!!" if alert["level"] == "critical" else " >"
            print(f"    {icon} [{alert['level'].upper()}] {alert['message']}")

    print("=" * 60)


def run_daily(
    state: PaperTradingState,
    portfolios: dict[str, PortfolioConfig],
    bars_by_ticker: dict[str, list[dict]],
    *,
    reconciliation: ReconciliationResult | None = None,
    entries_disabled: bool = False,
    reservations_by_portfolio: Mapping[str, float] | None = None,
    sell_availability: Mapping[str, float] | None = None,
) -> list[dict]:
    """Run one daily cycle: generate signals for all portfolios.

    Returns list of signals generated (for logging/review).
    """
    signals_generated: list[dict] = []
    accepted_buy_notional: dict[str, float] = {}
    remaining_sell_quantity = dict(sell_availability or {})
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
                    if entries_disabled or (
                        reconciliation is not None
                        and not reconciliation.entries_allowed
                    ):
                        reason = (
                            "entries disabled"
                            if entries_disabled
                            else "reconciliation mismatch"
                        )
                        print(
                            f"  SKIP {ticker:>6s}  {qty:>8.4f} @ "
                            f"${price:>8.2f}  [{name}] ({reason})"
                        )
                        continue
                    from shared.universe import ETF_SECTORS

                    sector = SECTOR_MAP.get(ticker) or ETF_SECTORS.get(ticker)

                    # Gate through the sleeve's RiskEngine exactly like the
                    # backtest runner does — without it the sim books every
                    # buy unconstrained (quality_value went to -$43K cash /
                    # 3.8x leverage on day one).
                    positions = state.get_positions(name)
                    current_market_value = sum(
                        p["quantity"] * current_prices.get(t, p["avg_entry_price"])
                        for t, p in positions.items()
                    )
                    # The daily broker-NAV budget, carried by PortfolioConfig,
                    # is the exposure basis. Durable PaperTradingState cash may
                    # reflect an older initialization amount and must not shrink
                    # a $1m broker account back to the historical $100k seed.
                    nav = float(pc.capital)
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
                            (current_market_value / nav) * 100.0 if nav > 0 else 0.0
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
                        reserved_notional=float(
                            (reservations_by_portfolio or {}).get(name, 0.0)
                        )
                        + accepted_buy_notional.get(name, 0.0),
                    )
                    if not decision.approved:
                        print(
                            f"  SKIP {ticker:>6s}  {qty:>8.4f} @ ${price:>8.2f}  [{name}] ({decision.reason})"
                        )
                        continue
                    if decision.adjusted_quantity and decision.adjusted_quantity != qty:
                        qty = decision.adjusted_quantity
                        signal["quantity"] = qty

                    signals_generated.append(signal)
                    accepted_buy_notional[name] = (
                        accepted_buy_notional.get(name, 0.0) + qty * price
                    )
                    print(
                        f"  BUY  {ticker:>6s}  {qty:>8.4f} @ ${price:>8.2f}  [{name}]"
                    )
                elif action == "sell":
                    if sell_availability is not None:
                        uncovered = max(
                            0.0, float(remaining_sell_quantity.get(ticker, 0.0))
                        )
                        requested = float(qty or uncovered)
                        qty = min(requested, uncovered)
                        if qty <= 0:
                            print(
                                f"  SKIP {ticker:>6s}  [{name}] "
                                "(no uncovered broker holding to sell)"
                            )
                            continue
                        signal["quantity"] = qty
                        remaining_sell_quantity[ticker] = uncovered - qty
                    # Exits are never gated (matching the backtest runner:
                    # "Sell signals for existing positions are always processed").
                    signals_generated.append(signal)
                    reason = signal.get("exit_reason", "signal")
                    print(
                        f"  SELL {ticker:>6s}             @ ${price:>8.2f}  [{name}] ({reason})"
                    )

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
    state.record_equity_snapshot(
        "_aggregate", today, total_equity, total_cash, total_market_value
    )

    return signals_generated


def _contract_by_symbol(
    broker_snapshot: BrokerAccountSnapshot,
    contract_details: Mapping[str, Any] | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        position.symbol: position for position in broker_snapshot.positions.values()
    }
    result.update(contract_details or {})
    return result


def create_signal_intents(
    session: Session,
    signals: list[dict],
    broker_snapshot: BrokerAccountSnapshot,
    *,
    run_date: date,
    contract_details: Mapping[str, Any] | None = None,
) -> list[OrderIntent]:
    """Durably record deterministic proposals without projecting fills."""
    ledger = OrderLedger(session)
    contracts = _contract_by_symbol(broker_snapshot, contract_details)
    intents: list[OrderIntent] = []
    for signal in signals:
        action = str(signal.get("action", "")).lower()
        if action not in {"buy", "sell"}:
            continue
        ticker = str(signal["ticker"])
        contract = contracts.get(ticker)
        if contract is None:
            raise ValueError(f"no qualified IB contract for signal {ticker}")
        con_id = int(
            getattr(contract, "con_id", None) or getattr(contract, "conId", None)
        )
        if con_id <= 0:
            raise ValueError(f"invalid IB contract id for signal {ticker}")
        recommendation_id = (
            f"sleeve-{run_date}-{broker_snapshot.account_id}-"
            f"{broker_snapshot.mode}-{signal['portfolio']}-{ticker}-{action}"
        )
        limit_price = signal.get("limit_price")
        proposal = SimpleNamespace(
            recommendation_id=recommendation_id,
            account_id=broker_snapshot.account_id,
            mode=broker_snapshot.mode,
            portfolio=signal["portfolio"],
            con_id=con_id,
            symbol=ticker,
            exchange=getattr(contract, "exchange", None) or "SMART",
            currency=getattr(contract, "currency", None) or "USD",
            action=action.upper(),
            quantity=float(signal.get("quantity") or 0.0),
            limit_price=float(limit_price) if limit_price is not None else None,
            order_type="LMT" if action == "buy" else "MKT",
        )
        if proposal.quantity <= 0:
            raise ValueError(f"signal {recommendation_id} has no quantity")
        intents.append(ledger.create_intent(proposal))
    return intents


def publish_unpublished_intents(
    session: Session,
    redis_client: Any,
    *,
    account_id: str | None = None,
    entries_allowed: bool = True,
    broker_snapshot: BrokerAccountSnapshot | None = None,
) -> int:
    """Replay the durable proposal outbox with stable recommendation IDs."""
    from shared.schemas.messages import RecommendationMessage

    stmt = select(OrderIntent).where(
        OrderIntent.status == OrderStatus.PROPOSED.value,
        OrderIntent.published_at.is_(None),
    )
    if account_id is not None:
        stmt = stmt.where(OrderIntent.account_id == account_id)
    if not entries_allowed:
        stmt = stmt.where(func.upper(OrderIntent.action) == "SELL")
    intents = list(session.scalars(stmt.order_by(OrderIntent.id)))
    ledger = OrderLedger(session)
    published = 0
    sell_availability = (
        build_sell_availability(session, broker_snapshot)
        if broker_snapshot is not None
        else None
    )
    for intent in intents:
        if intent.action.upper() == "SELL":
            if broker_snapshot is None:
                continue
            if (
                intent.account_id != broker_snapshot.account_id
                or intent.mode != broker_snapshot.mode
            ):
                continue
            available = max(0.0, float(sell_availability.get(intent.symbol, 0.0)))
            requested = float(intent.requested_quantity)
            if available <= 0:
                ledger.transition(
                    intent.recommendation_id,
                    OrderStatus.CANCELLED,
                    reason="no uncovered broker holding at outbox replay",
                )
                session.commit()
                continue
            if requested > available + 1e-6:
                original_id = intent.recommendation_id
                ledger.transition(
                    original_id,
                    OrderStatus.CANCELLED,
                    reason=("superseded by broker-capped sell intent at outbox replay"),
                )
                quantity_key = f"{available:.4f}".rstrip("0").rstrip(".")
                replacement_id = f"{original_id}-capped-{quantity_key}"
                intent = ledger.create_intent(
                    SimpleNamespace(
                        recommendation_id=replacement_id,
                        account_id=intent.account_id,
                        mode=intent.mode,
                        portfolio=intent.portfolio,
                        con_id=intent.con_id,
                        symbol=intent.symbol,
                        exchange=intent.exchange,
                        currency=intent.currency,
                        action=intent.action,
                        quantity=available,
                        limit_price=intent.limit_price,
                        order_type=intent.order_type,
                    )
                )
                session.commit()
        message = RecommendationMessage(
            ticker=intent.symbol,
            timestamp=intent.created_at,
            action=intent.action.lower(),
            confidence=1.0,
            top_features={},
            recommendation_id=intent.recommendation_id,
            limit_price=intent.limit_price,
            quantity=intent.requested_quantity,
            portfolio=intent.portfolio,
        )
        redis_client.xadd("stream:recommendations", message.to_stream_dict())
        ledger.mark_published(intent.recommendation_id)
        session.commit()
        published += 1
        if intent.action.upper() == "SELL" and sell_availability is not None:
            sell_availability[intent.symbol] = max(
                0.0,
                float(sell_availability.get(intent.symbol, 0.0))
                - float(intent.requested_quantity),
            )
    return published


async def read_broker_snapshot(
    *, host: str, port: int, client_id: int, mode: str
) -> BrokerAccountSnapshot:
    """Read account truth before any signal or capital decision."""
    from ib_insync import IB

    ib = IB()
    try:
        await ib.connectAsync(host, port, clientId=client_id, readonly=True, timeout=15)
        return await IBAccountReader(ib, expected_mode=mode).snapshot()
    finally:
        if ib.isConnected():
            ib.disconnect()


def resolve_contract_details_from_ib(
    tickers: list[str], *, host: str, port: int, client_id: int
) -> dict[str, Any]:
    """Qualify signal contracts so intents always carry durable conIds."""
    from ib_insync import IB, Stock

    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, readonly=True, timeout=15)
        result: dict[str, Any] = {}
        for ticker in sorted(set(tickers)):
            qualified = ib.qualifyContracts(Stock(ticker, "SMART", "USD"))
            if len(qualified) != 1 or int(qualified[0].conId) <= 0:
                raise ValueError(f"could not uniquely qualify IB contract {ticker}")
            result[ticker] = qualified[0]
        return result
    finally:
        if ib.isConnected():
            ib.disconnect()


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


def _parser(default_db_url: str, default_redis_url: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Daily paper trading runner")
    parser.add_argument(
        "--capital",
        type=float,
        default=100_000,
        help="Total capital used only by --init (default: 100000)",
    )
    parser.add_argument(
        "--db-url", default=default_db_url, help="PostgreSQL database URL"
    )
    parser.add_argument(
        "--years",
        type=int,
        default=1,
        help="Years of historical bars for signal warmup (default: 1)",
    )
    parser.add_argument(
        "--init", action="store_true", help="Initialize fresh paper trading state"
    )
    parser.add_argument(
        "--status", action="store_true", help="Print current status and exit"
    )
    parser.add_argument(
        "--reset", action="store_true", help="Wipe all paper trading state tables"
    )
    parser.add_argument("--ib-host", default="127.0.0.1")
    parser.add_argument("--ib-port", type=int, default=7497)
    parser.add_argument("--ib-client-id", type=int, default=58)
    parser.add_argument(
        "--entries-disabled",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Block new buys while retaining sells (default: enabled; use "
            "--no-entries-disabled only after reconciliation verification)"
        ),
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Publish durable intents so risk/execution can place IB paper orders",
    )
    parser.add_argument(
        "--redis-url", default=default_redis_url, help="Redis URL for --publish"
    )
    return parser


def main():
    # Load defaults from config (may fail if config file missing, that's OK)
    try:
        _config = load_config("config/default.yaml")
        default_db_url = _config.database.url
        default_redis_url = _config.redis.url
    except Exception:
        _config = AppConfig()
        default_db_url = "postgresql://algo:algo@localhost:5432/algo_poc"
        default_redis_url = "redis://localhost:6379/0"

    args = _parser(default_db_url, default_redis_url).parse_args()

    session = make_db_session(args.db_url)

    # --reset: wipe all paper state tables. Interactive-only by design: on
    # 2026-07-10 an agent piped `echo yes |` past the old prompt and wiped the
    # live paper book. A human at a real terminal is the only accepted input.
    if args.reset:
        if not sys.stdin.isatty():
            print(
                "Refusing --reset: stdin is not a TTY. This wipes all paper "
                "trading state and must be run by a human in an interactive "
                "terminal — piping confirmation is not accepted. "
                "(See CLAUDE.md/AGENTS.md 'Destructive Actions'.)"
            )
            session.close()
            sys.exit(2)
        confirm = input(
            "This will DELETE all paper trading data. Type 'yes' to confirm: "
        )
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            session.close()
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup = dump_paper_state(
            session, Path("output") / f"paper_state_pre_reset_{stamp}.json"
        )
        print(f"Pre-reset backup written to {backup}")
        reset_paper_state(session)
        print("All paper trading state wiped.")
        session.close()
        return

    # --init: create fresh state
    if args.init:
        capitals = {
            name: args.capital * pct for name, pct in CAPITAL_ALLOCATIONS.items()
        }
        PaperTradingState.create_new(capitals, session)
        session.commit()
        print("Initialized paper trading state in database")
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
    print("State loaded from database")

    # Broker truth is the first input to a daily run. Reconciliation and the
    # NAV-derived capital snapshot are committed before signal evaluation.
    broker_snapshot = asyncio.run(
        read_broker_snapshot(
            host=args.ib_host,
            port=args.ib_port,
            client_id=args.ib_client_id,
            mode=_config.mode,
        )
    )
    try:
        preparation = prepare_daily_run(
            broker_snapshot=broker_snapshot,
            config=_config,
            session=session,
        )
        session.commit()
    except Exception:
        session.rollback()
        session.close()
        raise
    mode_capital = getattr(_config.capital, _config.mode)
    entries_disabled = args.entries_disabled or not mode_capital.entries_enabled
    if not preparation.reconciliation.entries_allowed:
        entries_disabled = True
    print(
        f"Broker NAV: ${preparation.capital.net_liquidation:,.2f}; "
        f"deployable: ${preparation.capital.deployable_capital:,.2f}; "
        f"reconciliation: {preparation.reconciliation.severity}; "
        f"entries: {'disabled' if entries_disabled else 'enabled'}"
    )

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

    # Hydrate durable positions and pending broker orders before factory
    # creation so restarts preserve strategy exit state.
    portfolio_contexts = build_portfolio_contexts(
        state,
        session,
        sleeve_budgets=preparation.capital.sleeve_budgets,
        account_id=broker_snapshot.account_id,
    )

    # Build portfolios
    portfolios = build_portfolios(
        capital=preparation.capital.deployable_capital,
        bars_by_ticker=bars_by_ticker,
        regime_by_date=regime_by_date,
        fundamentals_lookup=fundamentals_lookup,
        earnings_lookup=earnings_lookup,
        portfolio_contexts=portfolio_contexts,
    )

    # Signal evaluation never projects fills.  Actual IB executions are the
    # only input allowed to mutate durable cash and positions.
    print(f"\nRunning signals across {len(portfolios)} portfolios...")
    try:
        reservations = {
            name: context.reserved_notional
            for name, context in portfolio_contexts.items()
        }
        sell_availability = build_sell_availability(session, broker_snapshot)
        signals = run_daily(
            state,
            portfolios,
            bars_by_ticker,
            reconciliation=preparation.reconciliation,
            entries_disabled=entries_disabled,
            reservations_by_portfolio=reservations,
            sell_availability=sell_availability,
        )
        if args.publish and signals:
            contracts = resolve_contract_details_from_ib(
                [signal["ticker"] for signal in signals],
                host=args.ib_host,
                port=args.ib_port,
                client_id=args.ib_client_id + 1,
            )
            create_signal_intents(
                session,
                signals,
                broker_snapshot,
                run_date=date.today(),
                contract_details=contracts,
            )
        session.commit()
    except Exception:
        session.rollback()
        print("\nERROR: daily run failed; database changes rolled back")
        raise

    if signals:
        print(f"\n{len(signals)} signals generated")
    else:
        print("\nNo signals generated today")
    print("\nState committed to database")

    # Bridge to the service pipeline: publish the same signals as
    # recommendations so risk gates them and execution places real IB paper
    # orders. Deliberately after the DB commit — the simulated book (the
    # divergence benchmark) is never blocked by the pipeline being down.
    if args.publish:
        try:
            import redis as redis_sync

            conn = redis_sync.Redis.from_url(args.redis_url)
            try:
                count = publish_unpublished_intents(
                    session,
                    conn,
                    account_id=broker_snapshot.account_id,
                    entries_allowed=not entries_disabled,
                    broker_snapshot=broker_snapshot,
                )
            finally:
                conn.close()
            print(f"{count} recommendations published to stream:recommendations")
        except Exception as e:
            session.rollback()
            print(
                f"WARNING: publish to pipeline failed ({e}); intents remain replayable"
            )
    session.close()


if __name__ == "__main__":
    main()
