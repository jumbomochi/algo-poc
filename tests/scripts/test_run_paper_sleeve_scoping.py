"""Every live sleeve must trade only the universe it declares.

``scripts/run_backtest.py`` scopes each ranking sleeve with ``eligible_tickers``
and the docstring at ``make_sector_rotation_signals_fn`` spells out what happens
without it: *"the pool is every ticker in bars_by_ticker (the union of all
sleeves), which lets the sector sleeve buy individual equities."*

``scripts/run_paper.py`` passed that argument for only two of six sleeves, so
live ranked over the whole union while the backtest ranked over each sleeve's
own list. On 2026-08-31 the live book showed the consequence directly:
``sector_rotation`` held HUM and LLY, neither of them SPDR sector ETFs.

The unit tests under ``tests/backtest/`` already prove the *mechanism* works
when ``eligible_tickers`` is supplied. Nothing proved production supplied it.
This module tests the wiring instead, and it is deliberately driven off
``ACTIVE_SLEEVES`` so a seventh sleeve fails here until it is scoped too.

The intruder is made maximally attractive on every axis a sleeve could rank on
— the strongest momentum in the run, a huge earnings surprise, ideal
fundamentals — so a sleeve that declines to buy it must be declining on
universe grounds and not because the bait was weak.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from backtest.portfolio_context import HeldPosition, PortfolioContext
from scripts.run_paper import (
    DRILL_BASE_SLEEVE,
    build_drill_portfolio,
    build_portfolios,
)
from shared.universe import ACTIVE_SLEEVES, UNIVERSE_REGISTRY

#: A ticker in no sleeve's declared universe. Asserted, not assumed — a future
#: universe edit that adopted this symbol would silently defang every test here.
INTRUDER = "ZZZZ"

#: Long enough to satisfy the deepest lookback in the book (momentum, 126).
SESSIONS = [date(2026, 1, 5) + timedelta(days=i) for i in range(140)]


def _bars(closes: list[float]) -> list[dict]:
    return [
        {
            "date": day,
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 1_000_000,
        }
        for day, close in zip(SESSIONS, closes)
    ]


def _rising(rate: float) -> list[dict]:
    return _bars([100.0 * (1.0 + rate * i) for i in range(len(SESSIONS))])


def _ordinary(nth: int) -> list[dict]:
    """A declared ticker: genuinely rankable, and distinct from its peers.

    Perfectly flat series give a return-ranked sleeve nothing to choose between,
    so it declines to buy anything and the universe assertions pass hollow. Each
    ticker gets its own gentle slope instead, which keeps the ranking total and
    deterministic without approaching the intruder.
    """
    return _rising(0.0005 * (nth + 1))


def _soaring() -> list[dict]:
    """Rises ~13x across the window, so it wins any return-ranked top-N."""
    return _rising(0.09)


def _every_declared_ticker() -> list[str]:
    seen: dict[str, None] = {}
    for sleeve in ACTIVE_SLEEVES:
        for ticker in UNIVERSE_REGISTRY[sleeve]:
            seen.setdefault(ticker, None)
    return list(seen)


def _collapsing() -> list[dict]:
    """Rises for most of the window, then halves — a clear trailing-stop exit."""
    rise = [100.0 * (1.0 + 0.09 * i) for i in range(len(SESSIONS) - 10)]
    return _bars(rise + [rise[-1] * 0.5] * 10)


def _build(sleeve: str, *, intruder_bars=None, contexts=None):
    """Build the live portfolios with the intruder present and irresistible."""
    bars_by_ticker = {
        ticker: _ordinary(nth)
        for nth, ticker in enumerate(_every_declared_ticker())
    }
    bars_by_ticker[INTRUDER] = intruder_bars or _soaring()

    return build_portfolios(
        capital=100_000.0,
        bars_by_ticker=bars_by_ticker,
        portfolio_contexts=contexts,
        # Never "crash": the entry freeze would suppress buys for a reason that
        # has nothing to do with the universe, and the test would pass hollow.
        regime_by_date={day: "bull" for day in SESSIONS},
        fundamentals_lookup=lambda ticker, as_of: {
            "roe": 0.60, "debt_equity": 0.0, "profit_margin": 0.75,
        },
        earnings_lookup=lambda ticker, as_of: {"surprise_pct": 50.0},
    ), bars_by_ticker


def test_the_intruder_belongs_to_no_sleeve() -> None:
    """Guards the premise every other test in this module rests on."""
    assert INTRUDER not in _every_declared_ticker()


@pytest.mark.parametrize("sleeve", ACTIVE_SLEEVES)
def test_a_sleeve_never_buys_outside_its_declared_universe(sleeve: str) -> None:
    portfolios, bars_by_ticker = _build(sleeve)

    signal = portfolios[sleeve].signals_fn(INTRUDER, bars_by_ticker[INTRUDER])

    assert signal is None or signal.get("action") != "buy", (
        f"{sleeve} would buy {INTRUDER}, which is not in its declared universe "
        f"({len(UNIVERSE_REGISTRY[sleeve])} tickers). The sleeve is ranking over "
        "the whole bars_by_ticker union instead of its own list — see "
        "make_sector_rotation_signals_fn's docstring in scripts/run_backtest.py."
    )


@pytest.mark.parametrize("sleeve", ACTIVE_SLEEVES)
def test_a_held_out_of_universe_position_stays_sellable(sleeve: str) -> None:
    """Scoping must gate entries, never strand a position the sleeve owns.

    On 2026-08-31 ``sector_rotation`` held HUM and LLY, bought while the sleeve
    ranked the whole union. Scoping the pool to SECTOR_ETFS fixes the entries,
    but if it also removes those names from the sleeve's view then nothing will
    ever emit a sell for them and the position becomes unsellable by the only
    sleeve that owns it.

    The intruder here has run up and then halved, which is past every sleeve's
    trailing stop, so a sleeve that can see its own holding must want out.
    """
    bars = _collapsing()
    context = PortfolioContext(
        positions={
            INTRUDER: HeldPosition(
                quantity=10.0,
                avg_entry_price=100.0,
                peak_price=max(b["close"] for b in bars),
                entry_date=SESSIONS[0],
            )
        },
        pending_orders={},
        sleeve_budget=100_000.0,
        reserved_notional=0.0,
    )

    portfolios, bars_by_ticker = _build(
        sleeve, intruder_bars=bars, contexts={sleeve: context}
    )

    signal = portfolios[sleeve].signals_fn(INTRUDER, bars_by_ticker[INTRUDER])

    assert signal is not None and signal.get("action") == "sell", (
        f"{sleeve} holds {INTRUDER} but emits {signal!r} instead of a sell. "
        "Universe scoping has stranded a position the sleeve cannot exit."
    )


def test_the_drill_sleeve_is_scoped_like_the_sleeve_it_mirrors() -> None:
    """The drill is not in ACTIVE_SLEEVES, so nothing above covers it.

    ``build_drill_portfolio``'s docstring promises the drill mirrors momentum's
    parameters "over the same liquid universe". Unscoped it ranks the whole
    fetched union instead, so a drill meant to exercise the momentum sleeve can
    open its position in another sleeve's instrument and the drill proves
    nothing about the path it was supposed to rehearse.
    """
    bars_by_ticker = {
        ticker: _ordinary(nth)
        for nth, ticker in enumerate(_every_declared_ticker())
    }
    bars_by_ticker[INTRUDER] = _soaring()

    drill = build_drill_portfolio(
        tag="__drill__", capital=10_000.0, bars_by_ticker=bars_by_ticker
    )

    signal = drill.signals_fn(INTRUDER, bars_by_ticker[INTRUDER])

    assert signal is None or signal.get("action") != "buy", (
        f"the drill sleeve would buy {INTRUDER}, which is outside "
        f"{DRILL_BASE_SLEEVE}'s universe that its docstring claims to mirror"
    )


@pytest.mark.parametrize("sleeve", ACTIVE_SLEEVES)
def test_a_sleeve_still_trades_its_own_universe(sleeve: str) -> None:
    """The scoping must not be achieved by refusing to trade at all.

    Without this, ``eligible_tickers=[]`` would satisfy every assertion above.
    """
    portfolios, bars_by_ticker = _build(sleeve)
    own = UNIVERSE_REGISTRY[sleeve]

    considered = [
        t for t in own
        if portfolios[sleeve].signals_fn(t, bars_by_ticker[t]) is not None
    ]

    assert considered, (
        f"{sleeve} produced no signal for any of its own {len(own)} declared "
        "tickers, so its universe has been scoped to nothing"
    )
