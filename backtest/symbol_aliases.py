"""Price a former index member under the symbol it trades as today (KAN-119).

BACKTEST BAR FETCHES ONLY. The membership file names each constituent by the
ticker it carried at the time (``FB``, ``ANTM``, ``PCLN``). IB knows a renamed
company only by its current symbol, so the backtest asked for ``FB`` and got
IB's Error 200 — and the coverage measure counted a still-trading company as
if it had been delisted. An alias fetches the bars of the SAME instrument
under its current conId and stores them under the membership ticker, so the
universe, sector and membership logic never see the new name.

Nothing here may reach live trading: ``shared.universe.make_stock_contract``
(live execution, the paper run) never reads this table, and
``scripts.run_backtest.fetch_bars_from_ib`` uses it only when its caller passes
it — which only ``run_backtest``'s own entry point does. A test pins both.

An alias is admitted only with evidence that it is the same instrument (see
``docs/decisions/backtest-symbol-aliases-2026-10.md``):

* IB's conId and long name for the current symbol;
* a ticker change dated inside the quarter in which the membership file
  switches from the old ticker to the new one (or after the old ticker left
  the index, when the new one never joined);
* IB's history for that conId reaching back over the old ticker's membership
  days, and continuous across the change (no gap beyond a normal daily move).

Mergers and spin-offs (UTX -> RTX, DWDP -> DD, CBS -> PARA) are admitted only
where IB's series before the event is the legal successor's own, not the
merged entity's. Where in doubt a name is left excluded: a missing name is a
known bias the coverage figure reports; a wrong series is a silent one.

No double counting. When the membership file also lists the new ticker, the
alias's bars stop at ``until`` — the first snapshot that lists the new
ticker — so no session prices one conId under two tickers.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping


@dataclass(frozen=True)
class BacktestAlias:
    #: The membership ticker the bars are stored under.
    ticker: str
    #: The symbol the same instrument trades as today, for the record.
    ib_symbol: str
    #: IB's conId for that instrument; the fetch is pinned to it.
    con_id: int
    #: IB's long name for ``con_id`` when the alias was verified.
    long_name: str
    #: First session the membership file lists ``ib_symbol`` itself; the
    #: alias's bars stop before it. None when the new ticker never joined.
    until: date | None
    #: The change, and the continuity evidence, in one line.
    evidence: str


#: Membership ticker -> alias. Every entry is verified (module docstring).
BACKTEST_ALIASES: dict[str, BacktestAlias] = {}


def backtest_contract(ticker: str, aliases: Mapping[str, BacktestAlias] | None):
    """The contract the backtest fetches for ``ticker``: pinned to the alias's
    conId when there is one, else exactly what live trading builds."""
    alias = (aliases or {}).get(ticker)
    if alias is None:
        from shared.universe import make_stock_contract

        return make_stock_contract(ticker)
    from ib_insync import Stock

    return Stock(conId=alias.con_id, exchange="SMART", currency="USD")


def clip_to_alias(
    ticker: str,
    bars: list[dict[str, Any]],
    aliases: Mapping[str, BacktestAlias] | None,
) -> list[dict[str, Any]]:
    """``bars`` without the sessions on or after the alias's ``until``."""
    alias = (aliases or {}).get(ticker)
    if alias is None or alias.until is None:
        return bars
    return [b for b in bars if _day(b["date"]) < alias.until]


def _day(value: Any) -> date:
    from datetime import datetime

    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
