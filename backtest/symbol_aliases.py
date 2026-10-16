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

The IB probe of 2026-10-17 found the opposite failure too: a membership
ticker that IB prices as a DIFFERENT company for part of its membership,
because the symbol was reused (``PARA`` is Banzai International today, so the
backtest graded Paramount's 2022-25 membership on Banzai's prices; ``IR`` was
Ingersoll-Rand plc until 2020 but IB's ``IR`` is Gardner Denver's Ingersoll
Rand Inc back to its 2017 listing). ``BACKTEST_UNPRICED`` drops those bars
(all of them, or those before ``before``), so the days count as excluded
instead of being graded on another company. Splicing the old company's conId
in front was rejected: a position held across the seam would book the price
gap between two companies as a return.
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
    #: The continuity evidence, in one line.
    evidence: str


@dataclass(frozen=True)
class Unpriced:
    """A membership ticker whose IB series is another company."""

    ticker: str
    #: Bars before this session are dropped; None drops every bar.
    before: date | None
    #: What IB's own symbol resolves to, and why that is not the member.
    evidence: str


def _a(ticker, ib_symbol, con_id, long_name, until, evidence) -> tuple[str, BacktestAlias]:
    return ticker, BacktestAlias(ticker, ib_symbol, con_id, long_name, until, evidence)


#: Membership ticker -> alias. Every entry is verified (module docstring);
#: conIds and long names are IB's answers on 2026-10-17, coverage and moves
#: are over the old ticker's member sessions in the 2026-10-13 refresh window.
#: The record, and the names refused, are in
#: docs/decisions/backtest-symbol-aliases-2026-10.md.
BACKTEST_ALIASES: dict[str, BacktestAlias] = dict([
    _a("ABC", "COR", 1714996, "CENCORA INC", date(2023, 10, 1),
       "covers 1747/1750 member sessions; largest daily move 13% (2016-10-28)"),
    _a("ANTM", "ELV", 173962302, "ELEVANCE HEALTH INC", date(2022, 7, 1),
       "covers 1433/1436 member sessions; largest daily move 17% (2020-03-16)"),
    _a("ARNC", "HWM", 403121976, "HOWMET AEROSPACE INC", date(2020, 7, 1),
       "covers 879/879 member sessions; largest daily move 26% (2020-03-24)"),
    _a("BHGE", "BKR", 281316445, "BAKER HUGHES CO", date(2020, 1, 1),
       "covers 566/566 member sessions; largest daily move 9% (2017-11-06)"),
    _a("BK", "BNY", 44986804, "BANK OF NEW YORK MELLON CORP", date(2026, 7, 1),
       "covers 2435/2438 member sessions; largest daily move 16% (2020-03-13)"),
    _a("BLL", "BALL", 5052, "BALL CORP", date(2022, 7, 1),
       "covers 1433/1436 member sessions; largest daily move 12% (2020-03-24)"),
    _a("CBG", "CBRE", 95514904, "CBRE GROUP INC - A", date(2018, 4, 1),
       "covers 362/365 member sessions; largest daily move 8% (2017-02-10)"),
    _a("COH", "TPR", 292491487, "TAPESTRY INC", date(2018, 1, 1),
       "covers 301/304 member sessions; largest daily move 15% (2017-08-15)"),
    _a("CTL", "LUMN", 446613345, "LUMEN TECHNOLOGIES INC", date(2020, 10, 1),
       "covers 991/996 member sessions; largest daily move 14% (2020-03-24)"),
    _a("DLPH", "APTV", 748351585, "APTIV PLC", date(2018, 1, 1),
       "covers 301/304 member sessions; largest daily move 11% (2017-05-03)"),
    _a("FB", "META", 107113386, "META PLATFORMS INC-CLASS A", date(2022, 7, 1),
       "covers 1433/1436 member sessions; largest daily move 26% (2022-02-03)"),
    _a("FBHS", "FBIN", 94078796, "FORTUNE BRANDS INNOVATIONS I", None,
       "covers 1560/1563 member sessions; largest daily move 16% (2020-03-24)"),
    _a("FLT", "CPAY", 691994611, "CORPAY INC", date(2024, 4, 1),
       "covers 1445/1445 member sessions; largest daily move 16% (2020-11-09)"),
    _a("GPS", "GAP", 7765, "GAP INC/THE", None,
       "covers 1371/1374 member sessions; largest daily move 30% (2020-03-24)"),
    _a("HCN", "WELL", 207739931, "WELLTOWER INC", date(2018, 4, 1),
       "covers 361/365 member sessions; largest daily move 4% (2017-12-19)"),
    _a("HCP", "DOC", 389700396, "HEALTHPEAK PROPERTIES INC", date(2024, 4, 1),
       "covers 804/807 member sessions; largest daily move 5% (2018-12-24)"),
    _a("HFC", "DINO", 550324178, "HF SINCLAIR CORP", None,
       "covers 755/755 member sessions; largest daily move 28% (2020-11-09)"),
    _a("HRS", "LHX", 371886040, "L3HARRIS TECHNOLOGIES INC", date(2019, 7, 1),
       "covers 676/679 member sessions; largest daily move 12% (2018-10-15)"),
    _a("JEC", "J", 582332987, "JACOBS SOLUTIONS INC", date(2020, 1, 1),
       "covers 804/807 member sessions; largest daily move 11% (2018-11-20)"),
    _a("KORS", "CPRI", 347409760, "CAPRI HOLDINGS LTD", date(2019, 4, 1),
       "covers 613/616 member sessions; largest daily move 22% (2017-08-08)"),
    _a("LB", "BBWI", 502252263, "BATH & BODY WORKS INC", date(2021, 10, 1),
       "covers 1245/1248 member sessions; largest daily move 39% (2020-03-24)"),
    _a("LUK", "JEF", 318911976, "JEFFERIES FINANCIAL GROUP IN", date(2018, 7, 1),
       "covers 426/429 member sessions; largest daily move 12% (2018-04-09)"),
    _a("MYL", "VTRS", 454859695, "VIATRIS INC", date(2021, 1, 1),
       "covers 1057/1060 member sessions; largest daily move 24% (2019-05-07)"),
    _a("PCLN", "BKNG", 308728373, "BOOKING HOLDINGS INC", date(2018, 4, 1),
       "covers 361/365 member sessions; largest daily move 14% (2017-11-07)"),
    _a("PEAK", "DOC", 389700396, "HEALTHPEAK PROPERTIES INC", date(2024, 4, 1),
       "covers 1067/1067 member sessions; largest daily move 23% (2020-03-16)"),
    _a("PKI", "RVTY", 6737, "REVVITY INC", date(2023, 7, 1),
       "covers 1684/1687 member sessions; largest daily move 10% (2020-03-16)"),
    _a("RE", "EG", 6477725, "EVEREST GROUP LTD", date(2023, 10, 1),
       "covers 1572/1572 member sessions; largest daily move 14% (2020-03-16)"),
    _a("SATS", "ECHO", 47965865, "ECHOSTAR CORP", date(2026, 7, 1),
       "IB ECHO conId prices all 62 member sessions (1y fetch); no move above 10%"),
    _a("SYMC", "GEN", 390332324, "GEN DIGITAL INC", date(2023, 1, 1),
       "covers 804/807 member sessions; largest daily move 33% (2018-05-11)"),
    _a("TMK", "GL", 375868349, "GLOBE LIFE INC", date(2019, 10, 1),
       "covers 740/743 member sessions; largest daily move 5% (2018-12-04)"),
    _a("UTX", "RTX", 415342104, "RTX CORP", date(2020, 7, 1),
       "covers 929/932 member sessions; largest daily move 16% (2020-03-24)"),
    _a("WLTW", "WTW", 217947855, "WILLIS TOWERS WATSON PLC", date(2022, 4, 1),
       "covers 1371/1374 member sessions; largest daily move 15% (2020-03-24)"),
    _a("WYN", "TNL", 469015035, "TRAVEL + LEISURE CO", None,
       "covers 426/429 member sessions; largest daily move 9% (2017-04-26)"),
])

#: Membership ticker -> why IB's series for it is another company.
BACKTEST_UNPRICED: dict[str, Unpriced] = {
    "AA": Unpriced(
        "AA", None,
        "IB AA is conId 251962528 ALCOA CORP, history from 2016-10-18 (the 2016 "
        "separation); the member until 2016 was Alcoa Inc, now HWM, whose series "
        "falls 34% at the 2016-11-01 separation, so it is not aliased either",
    ),
    "IR": Unpriced(
        "IR", date(2020, 4, 1),
        "IB IR is conId 404509190 INGERSOLL-RAND INC (Gardner Denver), history from "
        "2017-05-12 at 21.10 while the member, Ingersoll-Rand plc (now TT, conId "
        "406709270), traded at 68.28; IR is that company from the first snapshot "
        "listing TT",
    ),
    "PARA": Unpriced(
        "PARA", None,
        "IB PARA is conId 880582719 BANZAI INTERNATIONAL INC, history from "
        "2021-02-12, closing 98,000 -> 130 (split-adjusted) across Paramount's "
        "2022-04..2025-07 membership; PSKY returns Error 200",
    ),
}


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


def drop_unpriced(
    ticker: str,
    bars: list[dict[str, Any]],
    unpriced: Mapping[str, Unpriced] | None,
) -> list[dict[str, Any]]:
    """``bars`` without the sessions on which they are another company's."""
    entry = (unpriced or {}).get(ticker)
    if entry is None:
        return bars
    if entry.before is None:
        return []
    return [b for b in bars if _day(b["date"]) >= entry.before]


def _day(value: Any) -> date:
    from datetime import datetime

    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
