"""Shared liquidation helpers for the kill switch / circuit breaker.

Both the risk service (authoritative liquidator) and the execution service
(defense-in-depth net) must agree on the deterministic exit id and the set of
positions to flatten, or they would double-sell. Keeping that logic here means
they cannot drift apart.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.portfolio import Position


def liquidation_exit_id(mode: str, ticker: str, epoch: int) -> str:
    """Deterministic exit id for one position in one kill/breaker event.

    ``epoch`` is the kill event's timestamp (or the halt's activation time), so
    the same event converges on the same id across services and across a replay,
    while a distinct later event re-liquidates.
    """
    return f"liq-{mode}-{ticker}-{epoch}"


def load_liquidation_targets(session: Session, *, account_id: str | None = None) -> list[dict[str, Any]]:
    """Return open positions to flatten, aggregated by ticker, with the contract
    fields an :class:`OrderIntent` needs (con_id/exchange/currency/portfolio).

    Optionally scoped to ``account_id``.
    """
    stmt = select(Position).where(Position.status == "open")
    if account_id is not None:
        stmt = stmt.where(Position.account_id == account_id)

    aggregated: dict[str, dict[str, Any]] = {}
    for row in session.scalars(stmt):
        agg = aggregated.get(row.ticker)
        if agg is None:
            aggregated[row.ticker] = {
                "ticker": row.ticker,
                "quantity": float(row.quantity),
                "con_id": row.con_id,
                "account_id": row.account_id,
                "exchange": row.exchange,
                "currency": row.currency,
                "portfolio": row.portfolio,
            }
        else:
            agg["quantity"] += float(row.quantity)
    return list(aggregated.values())
