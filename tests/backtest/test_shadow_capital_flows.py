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
divergence below is manufactured by the seeding rule alone. Those scenarios
run with no warm-up (a clean cold start at the window), which is where the
KAN-113 rule was defined.

KAN-105 adds a warm-up: the replay trades before the window, so its NAV on the
window's first session is no longer live's, and the curve is normalised by
``k = seed / nav(ws)``. A flow inside the window is then scaled by ``1/k`` so
it is the same fraction of the shadow's book as of live's. The last section
pins that against the two-pass definition (learn nav(ws), then replay with the
scaled flow) built from FRESH signal functions — the replay does it in one
pass, because a second pass over the same stateful signal function would
start from the first pass's private book.
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
from backtest.shadow_series import (
    build_shadow_series,
    replay_window,
    replay_window_detail,
)
from backtest.simulator import SimulatedExecutor
from shared.capital_flows import flow_adjust

TICKER = "XYZ"
DAYS = [date(2026, 9, 1) + timedelta(days=i) for i in range(20)]
WINDOW_START = DAYS[2]
FLOW_SESSION = DAYS[8]
SEED = 10_000.0
#: The KAN-113 scenarios are defined on a cold start at the window.
COLD = {"warmup_sessions": 0, "lookback_sessions": 0}
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
        **COLD,
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
            live_equity=live, window_sessions=len(DAYS) - 2, **COLD,
            **kwargs,
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
        **COLD,
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


# ---------------------------------------------------------------------------
# KAN-105: a flow inside a WARMED window
# ---------------------------------------------------------------------------

WARM_DAYS = [date(2026, 6, 1) + timedelta(days=i) for i in range(40)]
WARM_N = 10
LOOKBACK = 5
WARM_WS = WARM_DAYS[LOOKBACK + WARM_N]
WARM_FROM = WARM_DAYS[LOOKBACK]
WARM_FLOW = WARM_DAYS[25]


def _warm_price(index: int, *, rally: bool) -> float:
    """+1%/session through the warm-up (so the shadow's NAV at ws is NOT the
    seed), flat to the flow, then optionally a +15% rally."""
    ws = WARM_DAYS.index(WARM_WS)
    flow = WARM_DAYS.index(WARM_FLOW)
    base = 100.0 * 1.01 ** min(index, ws)
    if rally and index > flow:
        base *= 1 + 0.15 * (index - flow) / (len(WARM_DAYS) - 1 - flow)
    return base


def _warm_bars(*, rally: bool) -> dict[str, list[dict]]:
    return {TICKER: [
        {"date": d, "open": _warm_price(i, rally=rally),
         "high": _warm_price(i, rally=rally), "low": _warm_price(i, rally=rally),
         "close": _warm_price(i, rally=rally), "volume": 1_000_000}
        for i, d in enumerate(WARM_DAYS)
    ]}


def _enter_once():
    """Everything in at the warm-up's first session; never again."""
    def fn(ticker, bars):
        if bars[-1]["date"] != WARM_FROM:
            return None
        return {"action": "buy", "ticker": ticker, "quantity": 1e9,
                "limit_price": bars[-1]["close"] * 1.02, "sector": "Tech"}
    return fn


def _enter_every_session():
    """Buys with all its cash every session: deploys a credit the next open."""
    def fn(ticker, bars):
        return {"action": "buy", "ticker": ticker, "quantity": 1e9,
                "limit_price": bars[-1]["close"] * 1.02, "sector": "Tech"}
    return fn


def _warm_replay(signals_fn, bars, flows=None):
    return replay_window_detail(
        bars_by_ticker=bars, signals_fn=signals_fn, risk_engine=_Approve(),
        seed_nav=SEED, window_start=WARM_WS, cash_flows=flows,
        warmup_sessions=WARM_N, lookback_sessions=LOOKBACK,
    )


def test_a_warmed_shadow_returns_a_flow_free_curve():
    """Flat prices through the window and the credit left idle: the raw book
    steps up by the credit, the returned curve does not move at all."""
    replay = _warm_replay(_enter_once(), _warm_bars(rally=False),
                          {WARM_FLOW: CREDIT})

    assert replay.warm_from == WARM_FROM
    curve = replay.curve
    days = sorted(curve)
    for prev, day in zip(days, days[1:]):
        assert curve[day] / curve[prev] == pytest.approx(1.0, abs=1e-12)


def test_a_flow_inside_a_warmed_window_keeps_its_capacity_ratio():
    """Live's credit is 25% of live's NAV at ws; the shadow receives 25% of
    ITS NAV at ws, which the warm-up has grown away from the seed."""
    bars = _warm_bars(rally=False)
    result = BacktestRunner(SimulatedExecutor(CostModel()),
                            initial_capital=SEED).run(
        bars, _enter_once(), _Approve(), trade_start_date=WARM_FROM,
        cash_flows={WARM_FLOW: CREDIT}, cash_flow_basis=(WARM_WS, SEED),
    )
    eod = dict(zip(result.dates, result.portfolio_values[1:]))

    assert eod[WARM_WS] > SEED * 1.05  # the warm-up moved the book
    [(session, applied)] = result.cash_flows_applied.items()
    assert session == WARM_FLOW
    assert applied / eod[WARM_WS] == pytest.approx(CREDIT / SEED, rel=1e-12)


def test_the_one_pass_replay_equals_the_two_pass_definition():
    """The investigation's definition: replay once to learn nav(ws), then
    replay again with each in-window flow scaled by nav(ws)/seed, normalise,
    flow-adjust. Built here from FRESH signal functions for each pass."""
    bars = _warm_bars(rally=True)

    def run(flows):
        return BacktestRunner(SimulatedExecutor(CostModel()),
                              initial_capital=SEED).run(
            bars, _enter_every_session_from_warm(), _Approve(),
            trade_start_date=WARM_FROM, cash_flows=flows,
        )

    first = run(None)
    nav_ws = dict(zip(first.dates, first.portfolio_values[1:]))[WARM_WS]
    k = SEED / nav_ws
    second = run({WARM_FLOW: CREDIT / k})
    raw = {d: v * k for d, v in zip(second.dates, second.portfolio_values[1:])
           if d >= WARM_WS}
    included = {d: (CREDIT if d >= WARM_FLOW else 0.0) for d in raw}
    expected = flow_adjust(raw, included)

    got = _warm_replay(_enter_every_session_from_warm(), bars,
                       {WARM_FLOW: CREDIT}).curve

    assert got.keys() == expected.keys()
    for day in expected:
        assert got[day] == pytest.approx(expected[day], rel=1e-12)
    # And the scaling is load-bearing: the unscaled credit is a different book.
    unscaled = run({WARM_FLOW: CREDIT})
    assert unscaled.portfolio_values[-1] != pytest.approx(
        second.portfolio_values[-1])


def _enter_every_session_from_warm():
    fn = _enter_every_session()

    def gated(ticker, bars):
        return None if bars[-1]["date"] < WARM_FROM else fn(ticker, bars)
    return gated


def test_flows_on_or_before_the_window_are_absorbed_by_the_normalisation():
    """Live's NAV at ws already contains them; replaying them would count
    them twice."""
    bars = _warm_bars(rally=True)
    plain = _warm_replay(_enter_once(), bars).curve

    assert _warm_replay(_enter_once(), bars,
                        {WARM_WS: CREDIT, WARM_DAYS[8]: 500.0}).curve == plain


def test_a_flow_the_runner_cannot_scale_yet_is_refused():
    """The scale is learned at the basis session's close; a flow applied on
    or before it would be sized on a number that does not exist yet."""
    with pytest.raises(ValueError, match="basis session"):
        BacktestRunner(SimulatedExecutor(CostModel()),
                       initial_capital=SEED).run(
            _warm_bars(rally=False), _enter_once(), _Approve(),
            cash_flows={WARM_WS: CREDIT}, cash_flow_basis=(WARM_WS, SEED),
        )


def test_a_flow_between_live_s_first_session_and_the_bars_first_is_absorbed():
    """KAN-105 review. When the bars lack live's first window session, the
    replay's anchor is the next bar session. A flow dated in (ws, anchor] has
    no replayed session before the anchor's close, and replaying it raised
    "cash flow applied on X, on or before the basis session X" — which, with
    no per-sleeve isolation, blanked every sleeve. It is part of the opening
    level instead: the monitor's comparison starts at the anchor."""
    bars = _warm_bars(rally=True)
    ws = WARM_WS
    anchor = WARM_DAYS[WARM_DAYS.index(ws) + 1]
    gapped = {TICKER: [b for b in bars[TICKER] if b["date"] != ws]}

    plain = _warm_replay(_enter_once(), gapped).curve
    absorbed = _warm_replay(_enter_once(), gapped, {anchor: CREDIT}).curve

    assert min(plain) == anchor
    assert absorbed == plain
    assert absorbed[anchor] == pytest.approx(SEED)
    # A later flow in the same gapped window is still replayed.
    later = _warm_replay(_enter_once(), gapped, {WARM_FLOW: CREDIT}).curve
    assert later.keys() == plain.keys()
