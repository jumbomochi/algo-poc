"""The risk service's account trailing stop, modelled in the backtest.

docs/decisions/qv-risk-stop-overlay-2026-11.md. Live sells any holding whose
mark falls ``stop_loss_trailing_pct`` below its high since entry
(``RiskManagementRunner.run_stop_loss_check``); no backtest modelled it, so a
sleeve's record assumed it kept positions the live book sold. The overlay is
opt-in: an unset ``risk_stop_pct`` must leave every run as it was.
"""
from __future__ import annotations

from datetime import date

import pytest

from backtest.costs import CostModel
from backtest.runner import RISK_STOP_EXIT_REASON, BacktestRunner
from backtest.simulator import SimulatedExecutor
from scripts.run_backtest import risk_stop_pct_arg
from services.risk_management.engine import RiskEngine

DAYS = [date(2025, 1, 6 + i) for i in range(10)]


def _bars(closes: list[float], *, opens: list[float] | None = None) -> list[dict]:
    """One bar per close, opening at the previous close unless given."""
    opens = opens or [closes[0], *closes[:-1]]
    return [
        {"date": d, "open": o, "high": max(o, c), "low": min(o, c), "close": c}
        for d, o, c in zip(DAYS, opens, closes)
    ]


def _runner(risk_stop_pct: float | None) -> BacktestRunner:
    executor = SimulatedExecutor(
        CostModel(slippage_bps=0, commission_per_share=0.0, commission_minimum=0.0)
    )
    return BacktestRunner(
        executor=executor, initial_capital=100_000.0, risk_stop_pct=risk_stop_pct
    )


def _risk() -> RiskEngine:
    # Permissive entry limits and one lot per ticker, so a sleeve that keeps
    # signalling a buy holds one position; check_stop_loss is live's own.
    return RiskEngine(position_entry_limit_pct=100, sector_concentration_pct=100,
                      total_exposure_limit_pct=100, max_lots_per_ticker=1)


def _buy_once(day: date = DAYS[0], sells: dict | None = None):
    """Buy 10 shares on ``day``'s close; sell on the days in ``sells``."""
    sells = sells or {}

    def signals_fn(ticker: str, rows: list[dict]) -> dict | None:
        today = rows[-1]["date"]
        if today in sells:
            return {"action": "sell", "ticker": ticker, "exit_reason": sells[today]}
        if today == day:
            return {"action": "buy", "ticker": ticker, "limit_price": rows[-1]["close"],
                    "quantity": 10, "sector": "Technology"}
        return None

    return signals_fn


def _run(closes, risk_stop_pct, signals_fn=None, **bars_kwargs):
    bars = {"NVDA": _bars(closes, **bars_kwargs)}
    return _runner(risk_stop_pct).run(bars, signals_fn or _buy_once(), _risk())


def test_without_the_overlay_a_holding_falling_from_entry_is_kept():
    # Bought at 100 (D1's open), closes down to 80: 20% below entry.
    result = _run([100, 100, 95, 90, 85, 80], risk_stop_pct=None)

    assert result.trades == []
    assert [lot["ticker"] for lot in result.open_positions] == ["NVDA"]


def test_a_holding_that_falls_straight_from_entry_is_stopped_at_the_next_open():
    # The NVDA case: the high never rises above the fill price, so the high
    # since entry IS the entry price. 84 is 16% below 100; the exit is queued
    # on that close and fills at the next session's open, as live's market
    # sell does.
    result = _run([100, 100, 95, 90, 84, 83], risk_stop_pct=15.0,
                  opens=[100, 100, 99, 94, 89, 82])

    (trade,) = result.trades
    assert trade["exit_reason"] == RISK_STOP_EXIT_REASON
    assert trade["entry_date"] == DAYS[1]
    assert trade["exit_date"] == DAYS[5]
    assert trade["exit_price"] == pytest.approx(82.0)
    assert result.open_positions == []


def test_the_high_is_the_highest_close_since_entry():
    # Rises to 120, then 101 is 15.8% below that high though above entry.
    result = _run([100, 100, 110, 120, 101, 100], risk_stop_pct=15.0)

    (trade,) = result.trades
    assert trade["exit_reason"] == RISK_STOP_EXIT_REASON
    assert trade["exit_date"] == DAYS[5]


def test_a_drop_just_short_of_the_threshold_does_not_stop():
    # 85.1 is 14.9% below 100.
    result = _run([100, 100, 95, 90, 85.1, 86], risk_stop_pct=15.0)

    assert result.trades == []


def test_a_sell_the_sleeve_already_queued_keeps_the_sleeves_reason():
    # Live suppresses the stop while a sell is working; the exit is the
    # sleeve's, not a second one.
    signals = _buy_once(sells={DAYS[4]: "trailing_stop"})
    result = _run([100, 100, 95, 90, 84, 83], risk_stop_pct=15.0,
                  signals_fn=signals)

    (trade,) = result.trades
    assert trade["exit_reason"] == "trailing_stop"


def test_the_sleeve_can_buy_a_stopped_name_straight_back():
    # What live did with NVDA on 2026-11-12: the stop sells, and a sleeve
    # that still ranks the name buys it again on its next decision.
    def always_buy(ticker: str, rows: list[dict]) -> dict | None:
        return {"action": "buy", "ticker": ticker, "limit_price": rows[-1]["close"],
                "quantity": 10, "sector": "Technology"}

    result = _run([100, 100, 95, 90, 84, 83, 83, 83], risk_stop_pct=15.0,
                  signals_fn=always_buy)

    (stopped,) = result.trades
    assert stopped["exit_reason"] == RISK_STOP_EXIT_REASON
    # Exit fills at D5's open; D5's close decides the re-buy, filled at D6.
    (lot,) = result.open_positions
    assert lot["entry_date"] == DAYS[6]


@pytest.mark.parametrize("pct", [0.0, -5.0, 100.0, 150.0])
def test_a_threshold_outside_0_to_100_is_refused(pct):
    with pytest.raises(ValueError, match="risk_stop_pct"):
        _runner(pct)


def test_the_cli_reads_a_percentage():
    assert risk_stop_pct_arg("15") == 15.0
    assert risk_stop_pct_arg("30.5") == 30.5


@pytest.mark.parametrize("raw", ["0", "100", "-1", "nan", "x"])
def test_the_cli_refuses_anything_but_a_percentage_in_0_to_100(raw):
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        risk_stop_pct_arg(raw)
