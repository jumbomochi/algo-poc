"""Load open positions and portfolio state from the database.

Used by the risk-management and execution services at startup so their
in-memory views reflect reality. Without this, kill liquidation iterates an
empty dict and stop-loss scans have nothing to scan — safety mechanisms that
look wired but protect nothing.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shared.capital_flows import (
    CapitalFlow,
    flow_adjust,
    flows_by_portfolio,
    included_flow,
    recorded_flows,
)
from shared.models.equity_snapshot import EquitySnapshot
from shared.models.portfolio import Position
from shared.models.portfolio_config import PortfolioConfig
from shared.universe import EXCLUDED_PORTFOLIO_PREFIX, lookup_sector


def load_open_positions(session: Session) -> dict[str, dict[str, Any]]:
    """Return open positions aggregated across portfolios, keyed by ticker.

    The risk and execution services operate at account level, so per-portfolio
    rows for the same ticker are combined: quantities sum, entry prices are
    quantity-weighted, and the highest-since-entry is the max.
    """
    rows = (
        session.execute(
            select(Position).where(
                Position.status == "open",
                ~Position.portfolio.startswith("_", autoescape=True),
            )
        )
        .scalars()
        .all()
    )

    positions: dict[str, dict[str, Any]] = {}
    for pos in rows:
        agg = positions.get(pos.ticker)
        if agg is None:
            positions[pos.ticker] = {
                "quantity": pos.quantity,
                "avg_entry_price": pos.avg_entry_price,
                "current_price": pos.current_price,
                "highest_price_since_entry": pos.highest_price_since_entry,
                # NULL-sector rows (written by the sector-blind fill projector,
                # 2026-07-19 – 2026-08-07) resolve via the universe maps.
                "sector": pos.sector or lookup_sector(pos.ticker),
            }
        else:
            total_qty = agg["quantity"] + pos.quantity
            if total_qty > 0:
                agg["avg_entry_price"] = (
                    agg["avg_entry_price"] * agg["quantity"]
                    + pos.avg_entry_price * pos.quantity
                ) / total_qty
            agg["quantity"] = total_qty
            agg["current_price"] = pos.current_price
            agg["highest_price_since_entry"] = max(
                agg["highest_price_since_entry"], pos.highest_price_since_entry
            )
    return positions


def load_portfolio_state(session: Session) -> dict[str, Any]:
    """Return account-level state: nav, peak_nav, cash, positions, sector exposure.

    - ``nav``: total cash across portfolios plus market value of open positions.
    - ``peak_nav``: the highest aggregate daily equity ever snapshotted
      (falls back to current nav on a fresh database). Flow-adjusted when a
      capital flow is recorded (KAN-113), so a sleeve-cash credit is never
      read as a recovery from a drawdown.
    - ``sector_exposure``: percent of nav per sector.
    """
    positions = load_open_positions(session)

    # Exclude synthetic portfolios (the "_aggregate" rollup row, the
    # "__drill__" tag) — including them double-counts: peak_nav read 2x NAV
    # and tripped the circuit breaker. Prefix owned by
    # shared.universe.is_excluded_portfolio; the SQL form is used here so the
    # filter runs in the database rather than over a full table scan.
    total_cash = session.execute(
        select(func.coalesce(func.sum(PortfolioConfig.cash), 0.0)).where(
            ~PortfolioConfig.portfolio.startswith(
                EXCLUDED_PORTFOLIO_PREFIX, autoescape=True
            )
        )
    ).scalar_one()

    market_value = sum(
        p["quantity"] * p["current_price"] for p in positions.values()
    )
    nav = float(total_cash) + market_value

    flows = [
        flow for flow in recorded_flows(session)
        if not flow.portfolio.startswith(EXCLUDED_PORTFOLIO_PREFIX)
    ]
    if flows:
        peak_nav = _flow_adjusted_peak(session, nav, flows)
    else:
        peak_nav = _raw_peak(session, nav)

    sector_exposure: dict[str, float] = {}
    if nav > 0:
        for p in positions.values():
            sector = p.get("sector") or "Unknown"
            value = p["quantity"] * p["current_price"]
            sector_exposure[sector] = (
                sector_exposure.get(sector, 0.0) + (value / nav) * 100.0
            )

    return {
        "nav": nav,
        "peak_nav": peak_nav,
        "cash": float(total_cash),
        "positions": positions,
        "sector_exposure": sector_exposure,
    }


def _raw_peak(session: Session, nav: float) -> float:
    # Highest aggregate equity across snapshot dates (excluding synthetic
    # portfolios like "_aggregate", which would double the total, and
    # "__drill__", whose equity is not part of the graded record).
    peak_row = session.execute(
        select(func.sum(EquitySnapshot.equity).label("total"))
        .where(
            ~EquitySnapshot.portfolio.startswith(
                EXCLUDED_PORTFOLIO_PREFIX, autoescape=True
            )
        )
        .group_by(EquitySnapshot.date)
        .order_by(func.sum(EquitySnapshot.equity).desc())
        .limit(1)
    ).scalar_one_or_none()
    return max(float(peak_row) if peak_row is not None else 0.0, nav)


def _flow_adjusted_peak(
    session: Session, nav: float, flows: list[CapitalFlow]
) -> float:
    """The drawdown peak with recorded capital flows removed (KAN-113).

    A sleeve-cash credit raises NAV without any gain. Taken raw it would also
    raise the peak — or, after a loss, close the drawdown — and the circuit
    breaker would read a top-up as a recovery. Instead the snapshot history,
    plus today's NAV as its newest point (every recorded flow is in today's
    cash), is flow-adjusted and anchored at today's NAV: the peak is then the
    highest time-weighted level, in today's capital.
    """
    rows = session.execute(
        select(
            EquitySnapshot.date,
            EquitySnapshot.portfolio,
            EquitySnapshot.equity,
            EquitySnapshot.created_at,
        ).where(
            ~EquitySnapshot.portfolio.startswith(
                EXCLUDED_PORTFOLIO_PREFIX, autoescape=True
            )
        )
    ).all()
    by_portfolio = flows_by_portfolio(flows)
    totals: dict[date, float] = {}
    included: dict[date, float] = {}
    for run_date, portfolio, equity, created_at in rows:
        totals[run_date] = totals.get(run_date, 0.0) + float(equity or 0.0)
        included[run_date] = included.get(run_date, 0.0) + included_flow(
            by_portfolio.get(portfolio, ()), created_at
        )
    totals[date.max] = nav
    included[date.max] = sum(flow.amount for flow in flows)
    return max(flow_adjust(totals, included).values())
