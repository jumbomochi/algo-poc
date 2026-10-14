"""KAN-116 AC1: a buy the runner does not fill leaves no lot and is retried.

Every sleeve's signal function used to record a lot in a private ``tracked``
dict when it *emitted* a buy. An entry the runner then did not fill — a DAY
limit the next bar never reached, a risk refusal, a sleeve-cash refusal (KAN-111)
or a crash-freeze suppression — stayed "held" in the signal function and was
never retried: 20-49% of buy signals per sleeve in a 2y replay. Each sleeve now
reads a ``RunnerBook`` that the runner re-binds to its own filled lots before
every session, so the unfilled entry is simply re-evaluated on the next bar.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from backtest.costs import CostModel
from backtest.portfolio_context import PortfolioContext, RunnerBook
from backtest.runner import BacktestRunner, _Lot, _runner_context
from backtest.simulator import SimulatedExecutor
from scripts.run_backtest import (
    make_crash_freeze_signals_fn,
    make_earnings_drift_signals_fn,
    make_momentum_signals_fn,
    make_quality_value_signals_fn,
    make_sector_rotation_signals_fn,
    make_tail_risk_hedge_signals_fn,
    make_thematic_momentum_signals_fn,
)
from services.risk_management.engine import RiskDecision, RiskEngine
from tests.backtest.books import empty_context

DAYS = 40
FAMILIES = [
    "momentum", "sector_rotation", "thematic_momentum", "quality_value",
    "earnings_drift", "tail_risk_hedge",
]


def _bars(daily: float, gap_at: int | None = None) -> list[dict]:
    """A steady trend; ``gap_at`` opens 5% above the prior close and never trades back down."""
    out, close = [], 100.0
    for i in range(DAYS):
        d = date(2024, 1, 1) + timedelta(days=i)
        if i == gap_at:
            px = close * 1.05
            out.append({"date": d, "open": px, "high": px, "low": px, "close": px, "volume": 1e6})
            close = px
            continue
        nxt = close * (1 + daily)
        out.append({"date": d, "open": close, "high": max(close, nxt) * 1.001,
                    "low": min(close, nxt) * 0.999, "close": nxt, "volume": 1e6})
        close = nxt
    return out


def _universe(family: str, gap_at: int | None = None) -> tuple[dict, str]:
    """Bars for a two-ticker universe, and the ticker the sleeve should buy."""
    target = "GLD" if family == "tail_risk_hedge" else "WIN"
    return {target: _bars(0.004, gap_at), "LOSE": _bars(-0.002)}, target


def dates_of(bars: dict, target: str) -> list[date]:
    return [b["date"] for b in bars[target]]


def _factory(family: str, bars: dict, book, regime: dict | None = None):
    regime = regime or {b["date"]: "bull" for rows in bars.values() for b in rows}
    if family == "momentum":
        return make_momentum_signals_fn(bars_by_ticker=bars, top_n=1, lookback_days=5,
                                        position_size_pct=0.1, portfolio_context=book)
    if family == "sector_rotation":
        return make_sector_rotation_signals_fn(bars_by_ticker=bars, top_n=1, lookback_days=5,
                                               position_size_pct=0.1, portfolio_context=book)
    if family == "thematic_momentum":
        return make_thematic_momentum_signals_fn(bars_by_ticker=bars, top_n=1, lookback_days=5,
                                                 ma_period=5, position_size_pct=0.1,
                                                 portfolio_context=book)
    if family == "quality_value":
        good = {"roe": 0.3, "debt_equity": 0.1, "profit_margin": 0.3}
        bad = {"roe": 0.01, "debt_equity": 2.0, "profit_margin": 0.01}
        return make_quality_value_signals_fn(lambda t, d: good if t == "WIN" else bad, {},
                                             bars_by_ticker=bars, top_n=1,
                                             position_size_pct=0.1, portfolio_context=book)
    if family == "earnings_drift":
        event = {"surprise_pct": 10.0, "actual_eps": 1.1, "estimate_eps": 1.0}
        return make_earnings_drift_signals_fn(lambda t, d: event if t == "WIN" else None,
                                              position_size_pct=0.1, portfolio_context=book)
    if family == "tail_risk_hedge":
        return make_tail_risk_hedge_signals_fn(regime, position_size_pct=0.1,
                                               portfolio_context=book)
    raise AssertionError(family)


def _first_buy_index(family: str) -> int:
    """The first session an empty book's sleeve signals its entry."""
    bars, target = _universe(family)
    fn = _factory(family, bars, empty_context())
    for i in range(1, DAYS):
        sig = fn(target, bars[target][: i + 1])
        if sig and sig["action"] == "buy":
            return i
    raise AssertionError("no buy")


def _risk() -> RiskEngine:
    return RiskEngine(position_entry_limit_pct=100, sector_concentration_pct=100,
                      total_exposure_limit_pct=100, max_lots_per_ticker=1)


def _replay(family: str, bars: dict, target: str, *, risk=None, capital=100_000.0,
            cash_flows=None, wrap=None):
    book = RunnerBook()
    fn = _factory(family, bars, book)
    if wrap is not None:
        fn = wrap(fn)
    signals: list[date] = []

    def recording(ticker, rows):
        signal = fn(ticker, rows)
        if ticker == target and signal and signal["action"] == "buy":
            signals.append(rows[-1]["date"])
        return signal

    result = BacktestRunner(SimulatedExecutor(CostModel()), initial_capital=capital).run(
        bars, recording, risk or _risk(), book=book, cash_flows=cash_flows)
    entries = sorted(
        {t["entry_date"] for t in result.trades if t["ticker"] == target}
        | {lot["entry_date"] for lot in result.open_positions if lot["ticker"] == target}
    )
    dates = [b["date"] for b in bars[target]]
    return dates, signals, entries, result


@pytest.mark.parametrize("family", FAMILIES)
def test_an_unreached_day_limit_leaves_no_lot_and_is_retried_next_bar(family):
    first = _first_buy_index(family)
    bars, target = _universe(family, gap_at=first + 1)  # the entry's working session gaps away
    dates, signals, entries, result = _replay(family, bars, target)

    assert signals[:2] == [dates[first], dates[first + 1]]
    assert entries[0] == dates[first + 2]
    assert result.entry_funnel["expired"] >= 1


class _RefuseFirst:
    """A risk engine that refuses the first entry it sees, then defers to a real one."""

    def __init__(self):
        self.inner, self.refused = _risk(), 0

    def check_entry(self, *args, **kwargs):
        decision = self.inner.check_entry(*args, **kwargs)
        if self.refused == 0:
            self.refused = 1
            return RiskDecision(approved=False, reason="test refusal")
        return decision


@pytest.mark.parametrize("family", FAMILIES)
def test_a_risk_refusal_leaves_no_lot_and_is_retried_next_bar(family):
    first = _first_buy_index(family)
    bars, target = _universe(family)
    risk = _RefuseFirst()
    dates, signals, entries, result = _replay(family, bars, target, risk=risk)

    assert risk.refused == 1 and result.entry_funnel["risk_refused"] == 1
    assert signals[:2] == [dates[first], dates[first + 1]]
    assert entries[0] == dates[first + 2]


class _ApproveAll:
    """Risk that approves as asked, so only the sleeve's cash can refuse."""

    def check_entry(self, ticker, quantity, *args, **kwargs):
        return RiskDecision(approved=True, reason="ok", adjusted_quantity=quantity)


@pytest.mark.parametrize("family", FAMILIES)
def test_a_sleeve_cash_refusal_leaves_no_lot_and_is_retried_next_bar(family):
    """KAN-111: no cash, no order. The cash arrives the next session (a KAN-113
    flow) and the entry the sleeve asked for the day before is asked again."""
    first = _first_buy_index(family)
    bars, target = _universe(family)
    dates, signals, entries, result = _replay(
        family, bars, target, risk=_ApproveAll(), capital=0.01,
        cash_flows={dates_of(bars, target)[first + 1]: 100_000.0},
    )

    assert result.entry_funnel["cash_skipped"] >= 1
    assert signals[:2] == [dates[first], dates[first + 1]]
    assert entries[0] == dates[first + 2]


@pytest.mark.parametrize("family", [f for f in FAMILIES if f != "tail_risk_hedge"])
def test_a_crash_freeze_suppression_leaves_no_lot_and_is_retried_when_it_lifts(family):
    """The freeze wrapper drops a buy AFTER the sleeve decided it; the sleeve
    must not believe it holds the name. (Tail-risk hedge is never frozen.)"""
    first = _first_buy_index(family)
    bars, target = _universe(family)
    crash_day = dates_of(bars, target)[first]
    regime = {d: ("crash" if d == crash_day else "bull") for d in dates_of(bars, target)}
    dates, signals, entries, _ = _replay(
        family, bars, target, wrap=lambda fn: make_crash_freeze_signals_fn(fn, regime),
    )

    assert signals[0] == dates[first + 1]  # the crash-day buy never reached the runner
    assert entries[0] == dates[first + 2]


@pytest.mark.parametrize("family", FAMILIES)
def test_a_sleeve_without_a_position_source_refuses_to_build(family):
    bars, _ = _universe(family)
    with pytest.raises(TypeError, match="portfolio_context"):
        _factory(family, bars, None)


def test_a_book_the_runner_never_bound_refuses_to_be_read():
    book = RunnerBook()
    with pytest.raises(RuntimeError, match="bound"):
        book.positions
    fn = _factory("momentum", _universe("momentum")[0], book)
    with pytest.raises(RuntimeError, match="bound"):
        fn("WIN", _universe("momentum")[0]["WIN"])


def test_a_replay_is_repeatable_on_the_same_signal_function():
    """No state survives a run: the same function replays identically twice
    (before KAN-116 a second pass started from the first pass's phantom book)."""
    bars, _ = _universe("momentum")
    book = RunnerBook()
    fn = _factory("momentum", bars, book)

    def run():
        return BacktestRunner(SimulatedExecutor(CostModel()), initial_capital=100_000).run(
            bars, fn, _risk(), book=book)

    first, second = run(), run()
    assert first.portfolio_values == second.portfolio_values
    assert first.trades == second.trades


def test_the_runner_context_is_the_lots_in_the_live_shape():
    lots = {
        "AAA": [
            _Lot("AAA", 2.0, 10.0, date(2024, 1, 3), 1.0, peak_price=12.0),
            _Lot("AAA", 6.0, 14.0, date(2024, 1, 5), 1.0, peak_price=15.0),
        ],
        "BBB": [_Lot("BBB", 1.0, 50.0, date(2024, 1, 4), 1.0)],
        "EMPTY": [],
    }
    ctx = _runner_context(lots, {"BBB": "trailing_stop", "GONE": "x"}, 1_000.0)

    assert isinstance(ctx, PortfolioContext)
    assert set(ctx.positions) == {"AAA", "BBB"}
    aaa = ctx.positions["AAA"]
    assert aaa.quantity == 8.0
    assert aaa.avg_entry_price == pytest.approx((2 * 10 + 6 * 14) / 8)
    assert aaa.peak_price == 15.0
    assert aaa.entry_date == date(2024, 1, 3)
    assert ctx.positions["BBB"].peak_price == 50.0  # a fresh lot's peak is its fill
    # A queued exit that has not filled covers the held quantity, so the
    # sleeve does not emit a duplicate sell; one for a ticker not held is dropped.
    assert ctx.pending_quantity("BBB", "sell") == 1.0
    assert ctx.pending_quantity("GONE", "sell") == 0.0
    assert ctx.sleeve_budget == 1_000.0


@pytest.mark.parametrize("family", FAMILIES)
def test_no_sleeve_keeps_a_private_record_of_its_lots(family):
    """The defect's shape, guarded: a signal function closing over its own lot
    record (the old ``tracked`` dict) would again believe what it asked for."""
    fn = _factory(family, _universe(family)[0], empty_context())
    assert "tracked" not in fn.__code__.co_freevars
    assert "portfolio_context" in fn.__code__.co_freevars
