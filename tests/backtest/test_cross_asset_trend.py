"""The cross-asset trend sleeve, as pre-registered (docs/decisions/cross-asset-trend-2026-10.md).

Pinned: decisions happen only on the first session of a month; the SMA rule
holds an asset above its 210-session average and the TSMOM rule above its
close 252 sessions back; the trend rule is the only exit; ineligible tickers
and short histories produce nothing.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from scripts.run_backtest import (
    CROSS_ASSET_ETFS,
    make_cross_asset_trend_signals_fn,
)
from tests.backtest.books import empty_context, holding


def _weekdays(start: date, n: int) -> list[date]:
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


DATES = _weekdays(date(2023, 1, 2), 320)


def _bars(closes: list[float]) -> list[dict]:
    return [
        {"date": d, "open": c, "high": c, "low": c, "close": c, "volume": 1_000_000}
        for d, c in zip(DATES, closes)
    ]


RISING = _bars([100 + 0.1 * i for i in range(len(DATES))])
FALLING = _bars([200 - 0.1 * i for i in range(len(DATES))])


def _first_of_month_after(index: int) -> int:
    """The first index past ``index`` that opens a new calendar month."""
    for i in range(index + 1, len(DATES)):
        if DATES[i].month != DATES[i - 1].month:
            return i
    raise AssertionError("no month boundary in the fixture")


def _mid_month_after(index: int) -> int:
    i = _first_of_month_after(index) + 5
    assert DATES[i].month == DATES[i - 1].month
    return i


def _fn(bars_by_ticker, context, rule="sma"):
    return make_cross_asset_trend_signals_fn(
        bars_by_ticker=bars_by_ticker,
        eligible_tickers=list(bars_by_ticker),
        portfolio_context=context,
        rule=rule,
    )


def test_the_universe_is_the_pre_registered_one():
    assert CROSS_ASSET_ETFS == ("VTI", "EFA", "AGG", "VNQ", "DBC")


def test_an_uptrend_is_bought_on_the_first_session_of_a_month():
    day = _first_of_month_after(250)
    signal = _fn({"VTI": RISING}, empty_context())("VTI", RISING[: day + 1])
    assert signal is not None and signal["action"] == "buy"
    assert signal["quantity"] == pytest.approx(100_000 * 0.19 / RISING[day]["close"], rel=1e-3)


def test_nothing_is_decided_mid_month():
    day = _mid_month_after(250)
    assert _fn({"VTI": RISING}, empty_context())("VTI", RISING[: day + 1]) is None


def test_a_downtrend_is_not_bought():
    day = _first_of_month_after(250)
    assert _fn({"VTI": FALLING}, empty_context())("VTI", FALLING[: day + 1]) is None


def test_a_held_asset_below_its_average_is_sold_on_the_decision_day():
    day = _first_of_month_after(250)
    book = holding("VTI", entry_price=150.0, entry_date=DATES[0], quantity=7)
    signal = _fn({"VTI": FALLING}, book)("VTI", FALLING[: day + 1])
    assert signal is not None and signal["action"] == "sell"
    assert signal["quantity"] == 7 and signal["exit_reason"] == "trend_off"


def test_a_held_asset_in_trend_is_kept_through_a_deep_drop_mid_month():
    """No trailing or max-loss stop: the moving average is the only exit."""
    closes = [100 + 0.2 * i for i in range(len(DATES))]
    day = _mid_month_after(250)
    closes[day] = closes[day - 1] * 0.85
    bars = _bars(closes)
    book = holding("VTI", entry_price=100.0, entry_date=DATES[0], peak_price=closes[day - 1])
    assert _fn({"VTI": bars}, book)("VTI", bars[: day + 1]) is None


def test_a_held_asset_in_trend_is_kept_on_the_decision_day():
    day = _first_of_month_after(250)
    book = holding("VTI", entry_price=100.0, entry_date=DATES[0])
    assert _fn({"VTI": RISING}, book)("VTI", RISING[: day + 1]) is None


def test_the_tsmom_rule_compares_with_the_close_252_sessions_back():
    """Above the 210-session SMA but below the close a year ago: SMA buys, TSMOM does not."""
    day = _first_of_month_after(260)
    closes = [150.0] * len(DATES)
    closes[day - 252] = 160.0
    for i in range(day - 251, day + 1):  # fell to 100, then climbed to ~112.6
        closes[i] = 100.0 + 0.05 * (i - (day - 252))
    bars = _bars(closes)
    assert closes[day] > sum(closes[day - 209: day + 1]) / 210 and closes[day] < closes[day - 252]
    sma_signal = _fn({"VTI": bars}, empty_context(), rule="sma")("VTI", bars[: day + 1])
    tsmom_signal = _fn({"VTI": bars}, empty_context(), rule="tsmom")("VTI", bars[: day + 1])
    assert sma_signal is not None and sma_signal["action"] == "buy"
    assert tsmom_signal is None


def test_too_little_history_decides_nothing():
    day = _first_of_month_after(20)
    assert _fn({"VTI": RISING}, empty_context())("VTI", RISING[: day + 1]) is None


def test_an_ineligible_ticker_is_ignored():
    day = _first_of_month_after(250)
    fn = make_cross_asset_trend_signals_fn(
        bars_by_ticker={"VTI": RISING, "AAPL": RISING},
        eligible_tickers=["VTI"],
        portfolio_context=empty_context(),
    )
    assert fn("AAPL", RISING[: day + 1]) is None


def test_an_unknown_rule_is_refused():
    with pytest.raises(ValueError):
        _fn({"VTI": RISING}, empty_context(), rule="ema")
