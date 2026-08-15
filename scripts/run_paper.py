#!/usr/bin/env python3
"""Daily paper trading runner.

Reuses the exact same signal functions from the backtest system.
Fetches latest bars from IB Gateway, runs all 8 signal functions,
and prints resulting signals. State persists to PostgreSQL between runs.

Usage:
    python scripts/run_paper.py --init            # Initialize fresh state
    python scripts/run_paper.py --status           # Print current positions
    python scripts/run_paper.py                    # Daily signal run (requires IB)

    # Epoch drill: books into a synthetic sleeve the graded readers exclude, so
    # a drill's real fills never enter the evidence record. The six graded
    # sleeves are not evaluated at all on a tagged run.
    # See docs/operations/drill-evidence-isolation.md.
    python scripts/run_paper.py --portfolio-tag __drill__ --portfolio-tag-capital 500
"""

from __future__ import annotations

# ruff: noqa: E402 -- direct-script execution needs the repo root first.

import argparse
import asyncio
import json
import os
import sys
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Mapping

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
from services.risk_management.funding import (
    check_settled_usd_funding,
    estimate_commission_usd,
)
from shared.order_ledger import OrderLedger
from shared.capital import CapitalBudget, calculate_capital_budget
from shared.broker_state import BrokerAccountSnapshot
from shared.models import CapitalSnapshot, OrderIntent, OrderStatus
from shared.observability import DEFAULT_TRADING_METRICS
from services.execution.ib_account import IBAccountReader
from services.execution.reconciliation import ReconciliationResult
from shared.logging import get_logger
from shared.universe import DRILL_PORTFOLIO, is_excluded_portfolio

if TYPE_CHECKING:
    from research.shadow import CandidateObserver


logger = get_logger("run_paper")

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


DRILL_BASE_SLEEVE = "momentum"


def build_drill_portfolio(
    tag: str,
    capital: float,
    bars_by_ticker: dict[str, list[dict]],
    portfolio_context: PortfolioContext | None = None,
) -> PortfolioConfig:
    """Build the single sleeve a ``--portfolio-tag`` run trades.

    Drills need a sleeve that reliably opens a position and then exits it (the
    synthetic stop-loss drill), so this mirrors the ``momentum`` sleeve's
    parameters over the same liquid universe — but under the tag's own name, so
    its positions, cash, and pending orders are the tag's and never a graded
    sleeve's. The crash-entry freeze is deliberately *not* applied: a drill must
    be able to place its order on any trading day, and its results are excluded
    from the evidence record either way (see
    docs/operations/drill-evidence-isolation.md).
    """
    return PortfolioConfig(
        name=tag,
        capital=capital,
        signals_fn=make_momentum_signals_fn(
            bars_by_ticker=bars_by_ticker,
            top_n=5,
            lookback_days=126,
            position_size_pct=0.12,
            initial_capital=capital,
            trailing_stop_pct=0.10,
            bear_tickers=BEAR_TICKERS,
            portfolio_context=portfolio_context,
        ),
        risk_engine=RiskEngine(
            position_entry_limit_pct=12.0,
            sector_concentration_pct=30.0,
            total_exposure_limit_pct=150.0,
            max_lots_per_ticker=1,
        ),
    )


def ensure_tagged_portfolio(
    state: PaperTradingState,
    session: Session,
    tag: str,
    capital: float,
) -> bool:
    """Fund the tagged sleeve if it has no PortfolioConfig row yet.

    Returns True when a row was created. Funding is explicit rather than
    derived from CAPITAL_ALLOCATIONS: a drill sleeve is not part of the graded
    split and must not shrink it. An existing row is left alone — re-running a
    drill must not silently top its cash back up.
    """
    if tag in state.get_portfolio_names():
        return False
    PaperTradingState.create_new({tag: capital}, session)
    return True


def build_portfolio_contexts(
    state: PaperTradingState,
    session: Session,
    capital: float | None = None,
    *,
    sleeve_budgets: Mapping[str, float] | None = None,
    account_id: str | None = None,
    portfolio_names: list[str] | None = None,
) -> dict[str, PortfolioContext]:
    """Build per-sleeve strategy state before creating signal factories.

    ``portfolio_names`` defaults to the six graded sleeves. A ``--portfolio-tag``
    run passes just the tag, so no graded sleeve's state is hydrated at all.
    """
    ledger = OrderLedger(session)
    pending_orders = ledger.load_pending_orders(account_id=account_id)
    names = list(portfolio_names or CAPITAL_ALLOCATIONS)
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
        for name in names
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
    capital = calculate_capital_budget(
        broker_snapshot,
        config.mode,
        config.capital,
        config.currency,
        CAPITAL_ALLOCATIONS,
    )
    reconciliation, _ = reconcile_snapshot(session, broker_snapshot)
    capital_snapshot = CapitalSnapshot(
        account_id=broker_snapshot.account_id,
        mode=config.mode,
        net_liquidation=capital.net_liquidation_trading_equivalent,
        base_currency=capital.base_currency,
        trading_currency=capital.trading_currency,
        net_liquidation_base=capital.net_liquidation_base,
        net_liquidation_trading_equivalent=(
            capital.net_liquidation_trading_equivalent
        ),
        fx_base_per_trading=capital.fx_base_per_trading,
        fx_captured_at=capital.fx_captured_at,
        fractional_base=capital.fractional_base,
        settled_cash_trading=capital.settled_cash_trading,
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


def account_buy_commitments_after_snapshot(
    session: Session,
    account_id: str,
    *,
    snapshot_captured_at: datetime | None,
    commission_per_share: float,
    minimum_commission: float,
) -> float:
    """Return durable USD buy commitments not reflected by the snapshot."""
    ledger = OrderLedger(session)
    try:
        active = ledger.active_buy_reservations_for_account(
            account_id,
            commission_per_share=commission_per_share,
            minimum_commission=minimum_commission,
        )
        filled = ledger.buy_fill_spend_for_account_since(
            account_id,
            captured_after=snapshot_captured_at,
        )
        return active + filled
    except (TypeError, ValueError):
        return float("nan")


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
    settled_cash_trading: float | None,
    active_buy_reservations_usd: float,
    commission_per_share_usd: float,
    minimum_commission_usd: float,
    minimum_settled_usd_reserve: float,
    candidate_observer: CandidateObserver | None = None,
    record_aggregate: bool = True,
    capital: CapitalBudget | None = None,
) -> list[dict]:
    """Run one daily cycle: generate signals for all portfolios.

    Returns list of signals generated (for logging/review).

    ``record_aggregate`` writes the "_aggregate" rollup snapshot. A tagged
    (drill) run passes False: it fetches only the drill sleeve's universe, so
    graded positions outside that universe would be marked at cost
    (``compute_equity`` falls back to ``avg_entry_price``) and the rollup would
    record an equity figure that was never true.

    ``capital`` supplies the currency context stamped onto every equity
    snapshot. It is the same budget the funding check already consumes, so
    the FX rate costs no extra IB call and introduces no new failure mode.
    When it is absent (a bare test harness) the currency columns stay NULL,
    which is what every row before KAN-44 looks like.
    """
    signals_generated: list[dict] = []
    currency_context: dict[str, Any] = (
        {
            "base_currency": capital.base_currency,
            "trading_currency": capital.trading_currency,
            "fx_base_per_trading": capital.fx_base_per_trading,
            # The instant IB quoted the rate, which is also the instant the
            # broker snapshot this run is marked against was captured.
            "valuation_at": capital.fx_captured_at,
        }
        if capital is not None
        else {}
    )
    accepted_buy_notional: dict[str, float] = {}
    accepted_account_buy_reservations_usd = 0.0
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
                    from shared.universe import lookup_sector

                    sector = lookup_sector(ticker)

                    try:
                        estimated_commission = estimate_commission_usd(
                            qty,
                            per_share=commission_per_share_usd,
                            minimum=minimum_commission_usd,
                        )
                    except (TypeError, ValueError):
                        estimated_commission = float("nan")
                    funding = check_settled_usd_funding(
                        order_notional_usd=qty * price,
                        settled_cash_usd=settled_cash_trading,
                        active_reservations_usd=(
                            active_buy_reservations_usd
                            + accepted_account_buy_reservations_usd
                        ),
                        estimated_commission_usd=estimated_commission,
                        minimum_reserve_usd=minimum_settled_usd_reserve,
                    )
                    if not funding.approved:
                        print(
                            f"  SKIP {ticker:>6s}  {qty:>8.4f} @ "
                            f"${price:>8.2f}  [{name}] ({funding.reason})"
                        )
                        continue

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
                        sector=sector,
                        portfolio=portfolio_state,
                        existing_lots=1 if ticker in positions else 0,
                        reserved_notional=float(
                            (reservations_by_portfolio or {}).get(name, 0.0)
                        )
                        + accepted_buy_notional.get(name, 0.0),
                    )
                    if candidate_observer is not None:
                        try:
                            candidate_observer.observe(
                                portfolio=name,
                                ticker=ticker,
                                as_of=_bar_date(bars[-1]["date"]),
                                signal=deepcopy(signal),
                                risk_approved=bool(decision.approved),
                                risk_reason=str(decision.reason),
                            )
                        except Exception:
                            logger.exception(
                                "Research shadow observer failed; paper trading is unchanged"
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
                    accepted_account_buy_reservations_usd += (
                        qty * price
                        + estimate_commission_usd(
                            qty,
                            per_share=commission_per_share_usd,
                            minimum=minimum_commission_usd,
                        )
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
        state.record_equity_snapshot(
            name, today, equity, cash, market_value, **currency_context
        )

    # Record aggregate equity snapshot. Synthetic portfolios (the "__drill__"
    # tag) are excluded: the rollup is the graded book's NAV, and a drill's
    # funding would inflate it.
    if record_aggregate:
        total_equity = 0.0
        total_cash = 0.0
        for name in state.get_portfolio_names():
            if is_excluded_portfolio(name):
                continue
            total_equity += state.compute_equity(name, current_prices)
            total_cash += state.get_cash(name)
        total_market_value = total_equity - total_cash
        state.record_equity_snapshot(
            "_aggregate",
            today,
            total_equity,
            total_cash,
            total_market_value,
            **currency_context,
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


def _bar_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


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
    *,
    host: str,
    port: int,
    client_id: int,
    mode: str,
    expected_base_currency: str,
    trading_currency: str,
) -> BrokerAccountSnapshot:
    """Read account truth before any signal or capital decision."""
    from ib_insync import IB

    ib = IB()
    try:
        await ib.connectAsync(host, port, clientId=client_id, readonly=True, timeout=15)
        return await IBAccountReader(
            ib,
            expected_mode=mode,
            expected_base_currency=expected_base_currency,
            trading_currency=trading_currency,
        ).snapshot()
    finally:
        if ib.isConnected():
            ib.disconnect()


def resolve_contract_details_from_ib(
    tickers: list[str], *, host: str, port: int, client_id: int
) -> dict[str, Any]:
    """Qualify signal contracts so intents always carry durable conIds."""
    from ib_insync import IB

    from shared.universe import make_stock_contract

    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, readonly=True, timeout=15)
        result: dict[str, Any] = {}
        for ticker in sorted(set(tickers)):
            qualified = ib.qualifyContracts(make_stock_contract(ticker))
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
    parser.add_argument(
        "--research-shadow",
        action="store_true",
        help="Record observational factor snapshots for raw buy candidates",
    )
    parser.add_argument(
        "--portfolio-tag",
        default=None,
        help=(
            f"Book this run into a synthetic sleeve instead of the six graded "
            f"ones (e.g. {DRILL_PORTFOLIO} for an epoch drill). Only names the "
            f"graded readers already exclude are accepted — a name without the "
            f"'_' prefix is refused so a typo cannot pollute the book."
        ),
    )
    parser.add_argument(
        "--portfolio-tag-capital",
        type=float,
        default=None,
        help="Capital to fund a new --portfolio-tag sleeve with (required with it)",
    )
    return parser


def validate_portfolio_tag(tag: str | None, tag_capital: float | None) -> str | None:
    """Return the validated tag, or raise ValueError explaining the refusal.

    The direction of this check is the whole point and is easy to get backwards:
    a tag is accepted **only if** it is a name the graded readers already
    exclude. ``--portfolio-tag momentum`` would write real fills into a graded
    sleeve, so it is refused before anything touches the database.
    """
    if tag is None:
        return None
    if not is_excluded_portfolio(tag):
        raise ValueError(
            f"Refusing --portfolio-tag '{tag}': a tagged run books real fills, "
            f"and only portfolios excluded from the evidence record may receive "
            f"them. Use a name starting with '_' (e.g. {DRILL_PORTFOLIO}). "
            f"See docs/operations/drill-evidence-isolation.md."
        )
    if tag_capital is None or tag_capital <= 0:
        raise ValueError(
            f"Refusing --portfolio-tag '{tag}': --portfolio-tag-capital is "
            f"required and must be positive, so the drill sleeve is funded "
            f"explicitly rather than borrowing a graded sleeve's budget."
        )
    return tag


def _create_research_shadow(
    *,
    enabled: bool,
    bars_by_ticker: dict[str, list[dict]],
    factor_ids: list[str] | None,
    db_url: str,
) -> tuple[CandidateObserver | None, Session | None]:
    """Build optional paper shadow scoring without entering the paper session.

    Every setup failure is observational: the caller receives no observer and
    proceeds with the established paper run. A session created before recorder
    construction fails is closed here; successful sessions are returned for
    the caller to close after signal execution.
    """
    if not enabled:
        return None, None

    research_session: Session | None = None
    try:
        from research.factors.catalog import (
            DEFAULT_FACTOR_IDS,
            build_default_registry,
        )
        from research.factors.engine import FactorEngine
        from research.factors.panel import build_factor_panel
        from research.shadow import SQLShadowRecorder

        selected_factor_ids = (
            list(factor_ids) if factor_ids is not None else list(DEFAULT_FACTOR_IDS)
        )
        snapshots = FactorEngine(build_default_registry()).compute(
            build_factor_panel(bars_by_ticker), selected_factor_ids
        )
        research_session = make_db_session(db_url)
        observer = SQLShadowRecorder(research_session, snapshots)
        return observer, research_session
    except Exception:
        if research_session is not None:
            try:
                research_session.close()
            except Exception:
                logger.exception("Research shadow setup session close failed")
        logger.exception(
            "Research shadow setup failed; paper trading is unchanged"
        )
        return None, None


def main():
    # Load defaults from config (may fail if config file missing, that's OK)
    try:
        _config = load_config("config/default.yaml")
        default_db_url = _config.database.url
        default_redis_url = _config.redis.url
    except Exception:
        _config = AppConfig()
        # Honour the env overrides even when the config file can't be loaded, so
        # a missing/unreadable config can't silently point at the wrong DB (the
        # launchd wrappers always export ALGO_DATABASE_URL / ALGO_REDIS_URL).
        default_db_url = os.environ.get(
            "ALGO_DATABASE_URL", "postgresql://algo:algo@localhost:5432/algo_poc"
        )
        default_redis_url = os.environ.get(
            "ALGO_REDIS_URL", "redis://localhost:6379/0"
        )

    args = _parser(default_db_url, default_redis_url).parse_args()

    # Validated before the database is opened, so a refused tag writes nothing.
    try:
        portfolio_tag = validate_portfolio_tag(
            args.portfolio_tag, args.portfolio_tag_capital
        )
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(2)

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

    if portfolio_tag is not None:
        created = ensure_tagged_portfolio(
            state, session, portfolio_tag, args.portfolio_tag_capital
        )
        session.commit()
        print(
            f"Tagged run: trading '{portfolio_tag}' only "
            f"({'funded with' if created else 'existing sleeve, requested'} "
            f"${args.portfolio_tag_capital:,.2f}). The six graded sleeves are "
            f"not evaluated and their books are untouched."
        )

    # Broker truth is the first input to a daily run. Reconciliation and the
    # NAV-derived capital snapshot are committed before signal evaluation.
    broker_snapshot = asyncio.run(
        read_broker_snapshot(
            host=args.ib_host,
            port=args.ib_port,
            client_id=args.ib_client_id,
            mode=_config.mode,
            expected_base_currency=_config.currency.expected_base_currency,
            trading_currency=_config.currency.trading_currency,
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
    capital = preparation.capital
    print(
        f"Broker NAV: SGD {capital.net_liquidation_base:,.2f} "
        f"(USD {capital.net_liquidation_trading_equivalent:,.2f}); "
        f"FX: {capital.fx_base_per_trading:.7f} SGD/USD; "
        f"settled USD: {capital.settled_cash_trading:,.2f}; "
        f"deployable USD: {capital.deployable_capital:,.2f}; "
        f"reconciliation: {preparation.reconciliation.severity}; "
        f"entries: {'disabled' if entries_disabled else 'enabled'}"
    )

    # Fetch bars from IB. A tagged run only needs the drill sleeve's universe.
    all_tickers = get_union_universe(
        [DRILL_BASE_SLEEVE] if portfolio_tag else list(CAPITAL_ALLOCATIONS.keys())
    )
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
    # A tagged run hydrates and trades the tag alone: the graded sleeves are
    # untouched by construction, not by filtering downstream.
    if portfolio_tag is not None:
        portfolio_contexts = build_portfolio_contexts(
            state,
            session,
            sleeve_budgets={portfolio_tag: args.portfolio_tag_capital},
            account_id=broker_snapshot.account_id,
            portfolio_names=[portfolio_tag],
        )
        portfolios = {
            portfolio_tag: build_drill_portfolio(
                tag=portfolio_tag,
                capital=args.portfolio_tag_capital,
                bars_by_ticker=bars_by_ticker,
                portfolio_context=portfolio_contexts[portfolio_tag],
            )
        }
    else:
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
    candidate_observer, research_session = _create_research_shadow(
        enabled=args.research_shadow,
        bars_by_ticker=bars_by_ticker,
        factor_ids=_config.research.factor_ids if _config is not None else None,
        db_url=args.db_url,
    )
    try:
        reservations = {
            name: context.reserved_notional
            for name, context in portfolio_contexts.items()
        }
        sell_availability = build_sell_availability(session, broker_snapshot)
        account_buy_reservations = account_buy_commitments_after_snapshot(
            session,
            broker_snapshot.account_id,
            snapshot_captured_at=preparation.capital_snapshot.captured_at,
            commission_per_share=_config.currency.commission_per_share_usd,
            minimum_commission=_config.currency.minimum_commission_usd,
        )
        signals = run_daily(
            state,
            portfolios,
            bars_by_ticker,
            reconciliation=preparation.reconciliation,
            entries_disabled=entries_disabled,
            reservations_by_portfolio=reservations,
            sell_availability=sell_availability,
            settled_cash_trading=preparation.capital.settled_cash_trading,
            active_buy_reservations_usd=account_buy_reservations,
            commission_per_share_usd=(
                _config.currency.commission_per_share_usd
            ),
            minimum_commission_usd=_config.currency.minimum_commission_usd,
            minimum_settled_usd_reserve=(
                _config.currency.minimum_settled_usd_reserve
            ),
            candidate_observer=candidate_observer,
            record_aggregate=portfolio_tag is None,
            capital=preparation.capital,
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
    finally:
        # Only the research shadow session is closed here; the paper `session`
        # stays open for the publish bridge below and is closed at the end.
        if research_session is not None:
            try:
                research_session.close()
            except Exception:
                logger.exception(
                    "Research shadow session close failed; paper trading is unchanged"
                )

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
