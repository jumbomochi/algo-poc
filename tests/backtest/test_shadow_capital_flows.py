"""KAN-113 R1: the shadow receives live's capital flows, so capacity matches.

The reviewer's scenario: a sleeve fully invested in one name, a credit worth
20% of the post-credit book arriving mid-window, and a +15% rally after it.
Live's strategy does not deploy the credit (it only enters at the window's
start), so live holds the credit as idle cash through the rally.

* Seeding the shadow at live's flow-adjusted NAV (the previous fix) ran the
  whole window fully invested on the post-credit capital: ~20% relative
  divergence, the BREACH threshold, from a deposit.
* Seeding at live's raw NAV and injecting the credit on its session gives the
  shadow live's capacity: no divergence at all.

"Live" here is the same model run through the same runner with the credit
injected — the book live would hold if it followed the model exactly — so any
divergence below is manufactured by the seeding rule alone.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from backtest.costs import CostModel
from backtest.divergence import build_report
from backtest.runner import BacktestRunner
from backtest.shadow_artifact import dump_shadow
from backtest.shadow_series import build_shadow_series, replay_window
from backtest.simulator import SimulatedExecutor
from shared.capital_flows import flow_adjust

TICKER = "XYZ"
DAYS = [date(2026, 9, 1) + timedelta(days=i) for i in range(20)]
WINDOW_START = DAYS[2]
FLOW_SESSION = DAYS[8]
SEED = 10_000.0
#: 20% of the post-credit book: 2,500 / 12,500.
CREDIT = 2_500.0


def _price(index: int) -> float:
    """Flat through the credit, then a linear +15% rally."""
    rally_from = DAYS.index(FLOW_SESSION)
    if index <= rally_from:
        return 100.0
    return 100.0 * (1 + 0.15 * (index - rally_from) / (len(DAYS) - 1 - rally_from))


BARS = {
    TICKER: [
        {"date": d, "open": _price(i), "high": _price(i), "low": _price(i),
         "close": _price(i), "volume": 1_000_000}
        for i, d in enumerate(DAYS)
    ]
}


def _signals(ticker, bars):
    """Enter once, with everything, on the window's first session."""
    if bars[-1]["date"] != WINDOW_START:
        return None
    return {"action": "buy", "ticker": ticker, "quantity": 1e9,
            "limit_price": bars[-1]["close"] * 1.02, "sector": "Tech"}


class _Approve:
    def check_entry(self, ticker, quantity, price, sector, portfolio, **kw):
        return SimpleNamespace(approved=True, adjusted_quantity=quantity,
                               reason="ok")


def _replay(seed, cash_flows=None):
    return replay_window(
        bars_by_ticker=BARS, signals_fn=_signals, risk_engine=_Approve(),
        seed_nav=seed, window_start=WINDOW_START, cash_flows=cash_flows,
    )


def _live_twr() -> dict[date, float]:
    """The book live holds following the model, flow-adjusted like the
    monitor adjusts it."""
    result = BacktestRunner(SimulatedExecutor(CostModel()),
                            initial_capital=SEED).run(
        BARS, _signals, _Approve(), trade_start_date=WINDOW_START,
        cash_flows={FLOW_SESSION: CREDIT},
    )
    raw = {
        d: v for d, v in zip(result.dates, result.portfolio_values[1:])
        if d >= WINDOW_START
    }
    included = {d: (CREDIT if d >= FLOW_SESSION else 0.0) for d in raw}
    return flow_adjust(raw, included)


def test_the_old_seeding_manufactures_divergence_and_the_fix_does_not():
    live = _live_twr()

    # Previous fix: seed at live's flow-adjusted NAV, no flow in the replay.
    rescaled = _replay(live[WINDOW_START])
    old = build_report("sleeve", live, rescaled, [], window_days=len(live))
    assert old.relative_divergence == pytest.approx(-0.2, abs=0.01)
    assert old.status in {"WARNING", "BREACH"}

    # R1 fix: seed at the raw NAV, inject the credit on its session.
    matched = _replay(SEED, {FLOW_SESSION: CREDIT})
    new = build_report("sleeve", live, matched, [], window_days=len(live))
    assert new.absolute_divergence_pp == pytest.approx(0.0, abs=1e-9)
    assert new.status == "OK"
    assert new.days_compared == len(live)


def test_the_injected_cash_is_sized_from_its_session_on():
    """KAN-111's cash cap sees the credit from the session it arrives: a
    strategy that buys every session deploys it at the next open."""
    def every_day(ticker, bars):
        if bars[-1]["date"] < WINDOW_START:
            return None
        return {"action": "buy", "ticker": ticker, "quantity": 1e9,
                "limit_price": bars[-1]["close"] * 1.02, "sector": "Tech"}

    result = BacktestRunner(SimulatedExecutor(CostModel()),
                            initial_capital=SEED).run(
        BARS, every_day, _Approve(), trade_start_date=WINDOW_START,
        cash_flows={FLOW_SESSION: CREDIT},
    )
    assert result.cash_flows_applied == {FLOW_SESSION: CREDIT}
    entry_dates = sorted(
        lot["entry_date"] for lot in result.open_positions
    )
    # One lot from the seed, one from the credit on the session after it.
    assert entry_dates[0] == DAYS[3]
    assert DAYS[9] in entry_dates


def test_a_withdrawal_larger_than_the_shadows_cash_is_clamped():
    """A transfer out of a sleeve whose counterfactual book is fully
    invested: the replay cannot sell to fund it, so it takes the cash there
    is (never below zero), and its returns are adjusted by what it took."""
    result = BacktestRunner(SimulatedExecutor(CostModel()),
                            initial_capital=SEED).run(
        BARS, _signals, _Approve(), trade_start_date=WINDOW_START,
        cash_flows={FLOW_SESSION: -5_000.0},
    )
    [(session, applied)] = result.cash_flows_applied.items()
    assert session == FLOW_SESSION
    assert -5_000.0 < applied <= 0.0  # only the cash the book had left

    curve = _replay(SEED, {FLOW_SESSION: -5_000.0})
    days = sorted(curve)
    i = days.index(FLOW_SESSION)
    # No step at the withdrawal: the flat book stays flat, flow removed.
    assert curve[days[i]] / curve[days[i - 1]] == pytest.approx(1.0)


def test_a_window_without_flows_is_replayed_exactly_as_before():
    """None, {}, and flows on or before the window's first session (already
    in the seed) all take the pre-KAN-113 path: identical curves."""
    baseline = _replay(SEED)
    assert _replay(SEED, {}) == baseline
    assert _replay(SEED, {WINDOW_START: CREDIT, DAYS[0]: 1.0}) == baseline

    plain = BacktestRunner(SimulatedExecutor(CostModel()),
                           initial_capital=SEED).run(
        BARS, _signals, _Approve(), trade_start_date=WINDOW_START,
    )
    empty = BacktestRunner(SimulatedExecutor(CostModel()),
                           initial_capital=SEED).run(
        BARS, _signals, _Approve(), trade_start_date=WINDOW_START,
        cash_flows={},
    )
    assert plain.portfolio_values == empty.portfolio_values
    assert plain.trades == empty.trades
    assert plain.cash_flows_applied == {} == empty.cash_flows_applied


def test_a_no_flow_shadow_artifact_is_byte_identical(tmp_path):
    sleeve = SimpleNamespace(signals_fn=_signals, risk_engine=_Approve())
    live = {"momentum": {d: SEED for d in DAYS}}

    def artifact(name, **kwargs):
        series = build_shadow_series(
            portfolios={"momentum": sleeve}, bars_by_ticker=BARS,
            live_equity=live, window_sessions=len(DAYS) - 2, **kwargs,
        )
        path = tmp_path / name
        dump_shadow(path, series=series, shadow_id="shadow:abc",
                    window_sessions=len(DAYS) - 2, session_date=DAYS[-1],
                    produced_on=date(2026, 10, 9))
        return path.read_bytes()

    before = artifact("before.json")
    assert artifact("none.json", cash_flows=None) == before
    assert artifact("empty.json", cash_flows={}) == before
    assert artifact("other.json", cash_flows={"tail_risk_hedge": {
        DAYS[8]: CREDIT}}) == before
    assert artifact("flowed.json", cash_flows={"momentum": {
        DAYS[8]: CREDIT}}) != before
    assert json.loads(before)["shadow_id"] == "shadow:abc"


def test_a_transfer_is_injected_into_both_sleeves():
    sleeve = SimpleNamespace(signals_fn=_signals, risk_engine=_Approve())
    live = {
        "momentum": {d: SEED for d in DAYS},
        "sector_rotation": {d: SEED for d in DAYS},
    }
    series = build_shadow_series(
        portfolios={"momentum": sleeve, "sector_rotation": sleeve},
        bars_by_ticker=BARS, live_equity=live, window_sessions=len(DAYS) - 2,
        cash_flows={
            "momentum": {FLOW_SESSION: 1_000.0},
            "sector_rotation": {FLOW_SESSION: -1_000.0},
        },
    )
    for name in ("momentum", "sector_rotation"):
        curve = series[name]
        days = sorted(curve)
        i = days.index(FLOW_SESSION)
        assert curve[days[i]] / curve[days[i - 1]] == pytest.approx(1.0)
