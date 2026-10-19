"""Momentum v2's two changes, each off by default (docs/decisions/momentum-v2-2026-10.md).

v1 is what every existing caller gets and what the paper run trades, so the
tests pin both halves: the v2 behaviour when asked for, and v1 unchanged when
not.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from scripts.run_backtest import MOMENTUM_VARIANTS, make_momentum_signals_fn
from tests.backtest.books import empty_context, holding


def _bars(days: int, daily_return: float) -> list[dict]:
    bars, price = [], 100.0
    for i in range(days):
        bars.append({
            "date": date(2024, 1, 2) + timedelta(days=i),
            "open": price, "high": price * 1.01, "low": price * 0.99,
            "close": price, "volume": 500_000,
        })
        price *= 1 + daily_return
    return bars


#: Twelve names, strictly ranked by trend: R01 strongest, R12 weakest.
LADDER = {f"R{i:02d}": _bars(200, 0.004 - i * 0.0003) for i in range(1, 13)}
DAY = 150  # bars[:DAY] has a 126-session ranking


def _momentum(context, **kwargs):
    return make_momentum_signals_fn(
        bars_by_ticker=LADDER, top_n=5, lookback_days=126,
        portfolio_context=context, **kwargs,
    )


def _held_at_today(ticker: str):
    today = LADDER[ticker][DAY - 1]
    # Entered at today's price, so neither stop can fire: only the rank can.
    return holding(ticker, entry_price=today["close"], entry_date=today["date"])


def test_a_holding_outside_the_rank_band_is_sold():
    signal = _momentum(_held_at_today("R12"), rank_exit_n=10)("R12", LADDER["R12"][:DAY])
    assert signal is not None and signal["action"] == "sell"
    assert signal["exit_reason"] == "rank_exit"


def test_a_holding_inside_the_band_but_outside_the_top_n_is_kept():
    """Rank 7: no longer a buy, not yet a sell. That gap is the hysteresis."""
    assert _momentum(_held_at_today("R07"), rank_exit_n=10)("R07", LADDER["R07"][:DAY]) is None


def test_v1_never_sells_on_rank():
    assert _momentum(_held_at_today("R12"))("R12", LADDER["R12"][:DAY]) is None


def test_the_rank_band_does_not_widen_the_entry_set():
    """The ranking is kept 10 deep for the exit; entries still take the top 5."""
    fn = _momentum(empty_context(), rank_exit_n=10)
    assert fn("R05", LADDER["R05"][:DAY])["action"] == "buy"
    assert fn("R06", LADDER["R06"][:DAY]) is None


# --- the regime -------------------------------------------------------------

INVERSE = {"PSQ": _bars(200, 0.006), "SPY": _bars(200, 0.001)}


def _inverse_momentum(regime: str | None):
    regime_by_date = (
        None if regime is None else {b["date"]: regime for b in INVERSE["PSQ"]}
    )
    return make_momentum_signals_fn(
        bars_by_ticker=INVERSE, top_n=1, lookback_days=126,
        portfolio_context=empty_context(), bear_tickers={"PSQ"},
        regime_by_date=regime_by_date,
    )


@pytest.mark.parametrize("regime", ["neutral", "bull", "crash"])
def test_an_inverse_etf_is_not_bought_outside_a_bear_regime(regime):
    """v1 bought it here and force-sold it the next session (the 2022 churn)."""
    assert _inverse_momentum(regime)("PSQ", INVERSE["PSQ"][:DAY]) is None


def test_an_inverse_etf_is_bought_in_a_bear_regime():
    signal = _inverse_momentum("bear")("PSQ", INVERSE["PSQ"][:DAY])
    assert signal is not None and signal["action"] == "buy"


def test_v1_still_buys_the_inverse_etf_it_will_force_sell():
    """Pins the incumbent's behaviour: the defect v2 exists to fix."""
    signal = _inverse_momentum(None)("PSQ", INVERSE["PSQ"][:DAY])
    assert signal is not None and signal["action"] == "buy"


def test_the_variants_are_the_pre_registered_ones():
    """Changing v2 here is a new trial, not an edit (the decision doc)."""
    assert MOMENTUM_VARIANTS["v1"] == (False, None)
    assert MOMENTUM_VARIANTS["v2"] == (True, 10)


def test_a_held_inverse_etf_in_a_bear_regime_is_still_subject_to_the_rank_exit():
    """In a bear regime an inverse ETF is a momentum holding like any other: v2
    sells it when it falls out of the top 10: the pre-registered rule names no
    exception for it."""
    bars = dict(LADDER, PSQ=_bars(200, -0.002))  # ranks last of 13
    today = bars["PSQ"][DAY - 1]
    fn = make_momentum_signals_fn(
        bars_by_ticker=bars, top_n=5, lookback_days=126, bear_tickers={"PSQ"},
        regime_by_date={b["date"]: "bear" for b in bars["PSQ"]}, rank_exit_n=10,
        portfolio_context=holding("PSQ", entry_price=today["close"], entry_date=today["date"]),
    )
    signal = fn("PSQ", bars["PSQ"][:DAY])
    assert signal is not None and signal["exit_reason"] == "rank_exit"
