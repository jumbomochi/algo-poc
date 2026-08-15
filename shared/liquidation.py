"""Shared liquidation helpers for the kill switch / circuit breaker.

Both the risk service (authoritative liquidator) and the execution service
(defense-in-depth net) must agree on the deterministic exit id and the set of
positions to flatten, or they would double-sell. Keeping that logic here means
they cannot drift apart.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.portfolio import Position


def liquidation_exit_id(mode: str, ticker: str, epoch: int) -> str:
    """Deterministic exit id for one position in one kill/breaker event.

    ``epoch`` is the kill event's timestamp (or the halt's activation time), so
    the same event converges on the same id across services and across a replay,
    while a distinct later event re-liquidates.

    Deliberately ticker-scoped and distinct from :func:`exit_intent_id`: a kill
    is one event across the whole book, and the execution service's
    defense-in-depth net keys its own exits by ticker too — both sides must
    derive the same id or they would double-sell.
    """
    return f"liq-{mode}-{ticker}-{epoch}"


def exit_intent_id(
    kind: str,
    account_id: str,
    portfolio: str,
    con_id: int,
    trading_date: date,
    seq: int,
) -> str:
    """Deterministic exit id for a recurring, identity-scoped exit.

    ``{kind}-{account}-{portfolio}-{conid}-{trading_date}-{seq}``

    Deterministic within one breach episode (so a replay is a no-op) and
    collision-free across legitimate repeat exits — a post-fill re-entry or a
    same-day second breach gets ``seq + 1``. Distinct from
    :func:`liquidation_exit_id`, which is epoch-based because a kill fires once
    while stop-losses recur.
    """
    return f"{kind}-{account_id}-{portfolio}-{con_id}-{trading_date.isoformat()}-{seq}"


def load_liquidation_targets(session: Session, *, account_id: str | None = None) -> list[dict[str, Any]]:
    """Return open positions to flatten, aggregated by broker identity scope
    ``{account_id, portfolio, con_id}``, with the contract fields an
    :class:`OrderIntent` needs (con_id/exchange/currency/portfolio).

    Aggregating by ticker instead would let one sleeve absorb another's exit:
    the six-sleeve portfolio routinely holds the same ticker in two sleeves, and
    a ticker-keyed row carries the first sleeve's ``account_id``/``portfolio``/
    ``con_id`` while claiming the combined quantity. The row shape is unchanged
    (same seven keys); there are simply more rows when a ticker is held twice,
    each with truthful identity.

    A position with a NULL ``con_id`` aggregates under ``(account, portfolio,
    None)`` and is still returned — the caller's missing-con_id guard is what
    rejects it, so an unroutable position is alerted on rather than silently
    dropped here. ``ticker`` is part of the key so that two *different* tickers
    both missing a con_id in the same sleeve cannot collapse into one row; for a
    populated con_id it is redundant (a con_id identifies one contract).

    Optionally scoped to ``account_id``.
    """
    stmt = select(Position).where(Position.status == "open")
    if account_id is not None:
        stmt = stmt.where(Position.account_id == account_id)

    aggregated: dict[tuple[str | None, str, int | None, str], dict[str, Any]] = {}
    for row in session.scalars(stmt):
        key = (row.account_id, row.portfolio, row.con_id, row.ticker)
        agg = aggregated.get(key)
        if agg is None:
            aggregated[key] = {
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
