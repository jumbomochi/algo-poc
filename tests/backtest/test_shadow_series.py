"""The rolling shadow series the divergence monitor grades live against.

A pinned 10-year artifact cannot score future sessions: the monitor intersects
live dates with the artifact's, so ``window_end`` freezes at the baseline's last
bar. Measured 2026-08-31, six consecutive daily runs all reported
``window_start=2026-07-10 window_end=2026-08-14`` and overwrote the same
evidence row. Un-blinding that feed would have produced a confident permanently
stale verdict.

The shadow replaces the feed. Each night it replays the sleeve's own signal
function over the bars live just fetched and produces an equity curve through
the current session. The verdict then answers the question a daily monitor is
for: *did live track the model over this window?*

The properties that carry the design, pinned here:

* **the curve starts at live's NAV** — same starting level on both sides is
  what makes an absolute pp gap mean anything;
* **the window opens on the model's own book (KAN-105)** — the replay trades a
  warm-up before the window, and the signal function is never called before
  the warm-up starts. The previous design fed it the pre-window bars and
  refused the buys, and because every sleeve records a lot in its private
  ``tracked`` book when it *emits* a buy, the window opened on a phantom book
  in cash: the slow-turnover regression below is 0% invested at the window's
  start on the old code;
* **bars too short for the warm-up are refused, loudly** — a shorter warm-up
  would be a different model under the same ``baseline_id``.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from backtest.costs import CostModel
from backtest.runner import BacktestRunner
from backtest.shadow_series import (
    SESSIONS_PER_FETCH_YEAR,
    SHADOW_LOOKBACK_SESSIONS,
    SHADOW_WARMUP_SESSIONS,
    ShadowWarmupTruncated,
    replay_window,
    replay_window_detail,
    shadow_fetch_years,
)
from backtest.simulator import SimulatedExecutor


SESSIONS = [date(2026, 1, 5) + timedelta(days=i) for i in range(60)]
WINDOW_START = SESSIONS[-10]
#: Small stand-ins for the production N = 160 / lookback = 200, which these
#: 60-session fixtures cannot hold. warm_from = SESSIONS[50 - 20] = SESSIONS[30].
WARMUP = 20
LOOKBACK = 10
WARM_FROM = SESSIONS[30]


def _bars(closes: list[float], sessions: list[date] = SESSIONS) -> list[dict]:
    return [
        {
            "date": day,
            "open": close,
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
            "volume": 1_000_000,
        }
        for day, close in zip(sessions, closes)
    ]


def _rising(rate: float, sessions: list[date] = SESSIONS) -> list[dict]:
    return _bars([100.0 * (1.0 + rate * i) for i in range(len(sessions))],
                 sessions)


class _AlwaysApproves:
    """Minimal risk engine: the shadow is a model curve, not a risk rehearsal."""

    def check_entry(self, ticker, quantity, price, sector, portfolio, **kwargs):
        class _D:
            approved = True
            adjusted_quantity = quantity
            reason = "ok"

        return _D()


def _never_trades(ticker: str, bars: list[dict]) -> dict | None:
    return None


def _buys_on(target: date, quantity: float = 10.0):
    """A signal fn that emits exactly one buy, on ``target``."""

    def signals_fn(ticker: str, bars: list[dict]) -> dict | None:
        if bars[-1]["date"] != target or ticker != "AAA":
            return None
        return {
            "action": "buy",
            "ticker": ticker,
            "limit_price": bars[-1]["close"] * 1.05,
            "quantity": quantity,
            "sector": "Tech",
        }

    return signals_fn


def _replay(signals_fn=_never_trades, **overrides):
    kwargs = dict(
        bars_by_ticker={"AAA": _rising(0.01)},
        signals_fn=signals_fn,
        risk_engine=_AlwaysApproves(),
        seed_nav=10_000.0,
        window_start=WINDOW_START,
        warmup_sessions=WARMUP,
        lookback_sessions=LOOKBACK,
    )
    kwargs.update(overrides)
    return replay_window(**kwargs)


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------


def test_the_series_starts_at_the_seeded_live_nav() -> None:
    """The whole point of the rolling anchor: both sides start level."""
    series = _replay(seed_nav=41_234.56)

    assert series[WINDOW_START] == pytest.approx(41_234.56)


def test_the_series_starts_at_live_nav_after_a_warm_up_that_moved_the_book() -> None:
    """KAN-105: a warm-up that bought a rising name ends above the seed; the
    CURVE is scaled back to live's NAV, so curve[ws] == seed_nav exactly."""
    detail = replay_window_detail(
        bars_by_ticker={"AAA": _rising(0.01)},
        signals_fn=_buys_on(WARM_FROM, quantity=250.0),
        risk_engine=_AlwaysApproves(),
        seed_nav=41_234.56,
        window_start=WINDOW_START,
        warmup_sessions=WARMUP,
        lookback_sessions=LOOKBACK,
    )

    assert detail.curve[WINDOW_START] == pytest.approx(41_234.56, rel=1e-12)
    assert detail.invested_at_start > 0.4
    # The window still moves with the book the warm-up built.
    assert detail.curve[SESSIONS[-1]] > detail.curve[WINDOW_START]


def test_the_series_covers_the_window_through_the_last_session() -> None:
    series = _replay()

    assert min(series) == WINDOW_START
    assert max(series) == SESSIONS[-1]
    assert len(series) == 10


def test_no_session_before_the_window_appears_in_the_series() -> None:
    """The warm-up trades, but it is never reported as the window."""
    series = _replay(signals_fn=_buys_on(WARM_FROM))

    assert not [d for d in series if d < WINDOW_START]


def test_a_buy_inside_the_window_moves_the_curve() -> None:
    """Guards against a series that is seeded correctly and then inert."""
    series = _replay(signals_fn=_buys_on(WINDOW_START))

    assert series[SESSIONS[-1]] != pytest.approx(10_000.0)


def test_a_window_start_after_every_session_yields_nothing() -> None:
    """No session to date the verdict by — the caller must see empty, not zero."""
    series = _replay(window_start=SESSIONS[-1] + timedelta(days=30))

    assert series == {}


def test_empty_bars_yield_nothing() -> None:
    assert _replay(bars_by_ticker={}) == {}


# ---------------------------------------------------------------------------
# The warm-up (KAN-105)
# ---------------------------------------------------------------------------


def test_the_signal_function_is_never_called_before_the_warm_up() -> None:
    """The fix itself. A call the runner refuses to trade still records a lot
    in a real sleeve's private book; so there is no call at all."""
    seen: list[date] = []

    def recording_fn(ticker: str, bars: list[dict]) -> dict | None:
        seen.append(bars[-1]["date"])
        return None

    _replay(signals_fn=recording_fn)

    assert min(seen) == WARM_FROM
    assert seen == SESSIONS[30:]


def test_the_first_call_carries_the_full_history() -> None:
    """Gating the call is not truncating the bars: a 126-session lookback on
    the first warm-up session needs everything before it."""
    first: list[list[dict]] = []

    def recording_fn(ticker: str, bars: list[dict]) -> dict | None:
        if not first:
            first.append(list(bars))
        return None

    _replay(signals_fn=recording_fn)

    assert [b["date"] for b in first[0]] == SESSIONS[:31]


def test_a_buy_signalled_inside_the_warm_up_is_taken() -> None:
    """The old design refused it, and the signal fn's book kept the phantom.
    Now it fills, and the window opens holding it."""
    flat = replay_window_detail(
        bars_by_ticker={"AAA": _rising(0.01)},
        signals_fn=_never_trades,
        risk_engine=_AlwaysApproves(),
        seed_nav=10_000.0,
        window_start=WINDOW_START,
        warmup_sessions=WARMUP,
        lookback_sessions=LOOKBACK,
    )
    warm = replay_window_detail(
        bars_by_ticker={"AAA": _rising(0.01)},
        signals_fn=_buys_on(SESSIONS[35]),
        risk_engine=_AlwaysApproves(),
        seed_nav=10_000.0,
        window_start=WINDOW_START,
        warmup_sessions=WARMUP,
        lookback_sessions=LOOKBACK,
    )

    assert flat.invested_at_start == 0.0
    assert warm.invested_at_start > 0.1
    assert warm.curve != flat.curve
    assert warm.warm_from == flat.warm_from == WARM_FROM


def test_a_buy_signalled_before_the_warm_up_is_never_seen() -> None:
    """Before ``warm_from`` the signal fn is not called, so neither the
    position nor a phantom of it can exist."""
    flat = _replay()
    early = _replay(signals_fn=_buys_on(SESSIONS[5]))

    assert early == flat


def test_a_zero_warm_up_is_a_clean_cold_start() -> None:
    """N = 0 gates the signal fn before the window and trades from it."""
    seen: list[date] = []

    def recording_fn(ticker: str, bars: list[dict]) -> dict | None:
        seen.append(bars[-1]["date"])
        return None

    _replay(signals_fn=recording_fn, warmup_sessions=0)

    assert min(seen) == WINDOW_START


def test_bars_too_short_for_the_warm_up_raise() -> None:
    """50 sessions precede the window; 45 + 10 do not fit. Degrading to a
    shorter warm-up would grade a different model under the same id."""
    with pytest.raises(ShadowWarmupTruncated, match="45-session warm-up"):
        _replay(warmup_sessions=45, lookback_sessions=10)


def test_bars_too_short_for_the_indicator_lookback_raise() -> None:
    with pytest.raises(ShadowWarmupTruncated):
        _replay(warmup_sessions=20, lookback_sessions=31)


def test_bars_exactly_long_enough_are_accepted() -> None:
    series = _replay(warmup_sessions=40, lookback_sessions=10)

    assert min(series) == WINDOW_START


def test_the_production_defaults_refuse_a_one_year_fetch() -> None:
    """One year (~250 sessions) cannot hold 160 + 200 before a window that
    ends on its last session, which is why the fetch is derived from N."""
    year = [date(2025, 1, 6) + timedelta(days=i) for i in range(250)]
    with pytest.raises(ShadowWarmupTruncated):
        replay_window(
            bars_by_ticker={"AAA": _rising(0.001, year)},
            signals_fn=_never_trades,
            risk_engine=_AlwaysApproves(),
            seed_nav=10_000.0,
            window_start=year[-30],
        )


def test_the_fetch_covers_window_warm_up_and_lookback() -> None:
    """30 + 160 + 200 = 390 sessions: 2 years, not 1."""
    assert SHADOW_WARMUP_SESSIONS == 160
    assert SHADOW_LOOKBACK_SESSIONS == 200
    years = shadow_fetch_years(window_sessions=30)
    assert years == 2
    assert years * SESSIONS_PER_FETCH_YEAR >= 30 + 160 + 200
    assert shadow_fetch_years(window_sessions=30, warmup_sessions=0,
                              lookback_sessions=0) == 1
    assert shadow_fetch_years(window_sessions=30, warmup_sessions=400) == 3


# ---------------------------------------------------------------------------
# The slow-turnover regression (KAN-105)
# ---------------------------------------------------------------------------
#
# A stand-in for momentum / quality_value: rank a universe by trailing return,
# buy the top N at first eligibility, and sell only on a 12% trailing stop. The
# lot is recorded in a module-style ``tracked`` dict when the buy is EMITTED,
# exactly as scripts/run_backtest.py's sleeves do. On the pre-KAN-105 replay
# this signal function emitted its whole book during the refused warm-up, held
# it as phantoms, and never bought it inside the window: 0% invested at ws.

RANK_LOOKBACK = 20
TOP_N = 4
SLOW_WARMUP = 40
SLOW_WINDOW = 15
SLOW_SEED = 50_000.0
#: Bars start exactly RANK_LOOKBACK sessions before warm_from, so nothing can
#: be signalled before it: one continuous run from the first bar IS the
#: model's own path, and the warmed window must be its slice.
SLOW_SESSIONS = [
    date(2025, 6, 2) + timedelta(days=i)
    for i in range(RANK_LOOKBACK + SLOW_WARMUP + SLOW_WINDOW)
]
SLOW_WS = SLOW_SESSIONS[RANK_LOOKBACK + SLOW_WARMUP]


def _slow_path(index: int, n: int) -> float:
    """Ticker ``n`` drifts up at its own rate; T2 (a top-N name) drops 15% in
    the warm-up, which fires its stop and frees cash for the next-ranked name."""
    price = 100.0 * (1.0 + 0.001 * (n + 1)) ** index
    if n == 6 and 35 <= index < 45:
        price *= 0.85
    return price


SLOW_BARS = {
    f"T{n}": [
        {"date": d, "open": _slow_path(i, n), "high": _slow_path(i, n) * 1.01,
         "low": _slow_path(i, n) * 0.99, "close": _slow_path(i, n),
         "volume": 1_000_000}
        for i, d in enumerate(SLOW_SESSIONS)
    ]
    for n in range(8)
}


def _slow_turnover_signals():
    """A fresh stateful top-N signal function."""
    closes = {
        t: {b["date"]: b["close"] for b in bars} for t, bars in SLOW_BARS.items()
    }
    tracked: dict[str, dict] = {}

    def top_n(day: date) -> set[str]:
        i = SLOW_SESSIONS.index(day)
        past = SLOW_SESSIONS[i - RANK_LOOKBACK]
        ranked = sorted(closes, key=lambda t: closes[t][day] / closes[t][past],
                        reverse=True)
        return set(ranked[:TOP_N])

    def signals_fn(ticker: str, bars: list[dict]) -> dict | None:
        if len(bars) <= RANK_LOOKBACK:
            return None
        close = bars[-1]["close"]
        lot = tracked.get(ticker)
        if lot is not None:
            lot["peak"] = max(lot["peak"], close)
            if close < lot["peak"] * 0.88:
                del tracked[ticker]
                return {"action": "sell", "ticker": ticker,
                        "exit_reason": "stop_loss"}
            return None
        if ticker not in top_n(bars[-1]["date"]):
            return None
        tracked[ticker] = {"peak": close}  # on EMIT, as the real sleeves do
        return {"action": "buy", "ticker": ticker,
                "limit_price": close * 1.02,
                "quantity": SLOW_SEED / TOP_N * 0.97 / close,
                "sector": "Tech"}

    return signals_fn


def _slow_replay(**overrides):
    kwargs = dict(
        bars_by_ticker=SLOW_BARS,
        signals_fn=_slow_turnover_signals(),
        risk_engine=_AlwaysApproves(),
        seed_nav=SLOW_SEED,
        window_start=SLOW_WS,
        warmup_sessions=SLOW_WARMUP,
        lookback_sessions=RANK_LOOKBACK,
    )
    kwargs.update(overrides)
    return kwargs


def test_a_slow_turnover_sleeve_opens_the_window_invested() -> None:
    """AC1. The pre-KAN-105 replay opened this window at 0%."""
    detail = replay_window_detail(**_slow_replay())

    assert detail.invested_at_start >= 0.90
    assert detail.warm_from == SLOW_SESSIONS[RANK_LOOKBACK]


def test_a_slow_turnover_window_is_the_slice_of_one_continuous_run() -> None:
    """The warmed window is the model's own path: one continuous backtest from
    the first bar, sliced at ws and scaled to live's NAV there. The old replay
    was flat cash here except for names that newly entered the top N."""
    series = replay_window(**_slow_replay())

    result = BacktestRunner(SimulatedExecutor(CostModel()),
                            initial_capital=SLOW_SEED).run(
        SLOW_BARS, _slow_turnover_signals(), _AlwaysApproves(),
    )
    eod = dict(zip(result.dates, result.portfolio_values[1:]))
    scale = SLOW_SEED / eod[SLOW_WS]
    expected = {d: v * scale for d, v in eod.items() if d >= SLOW_WS}

    # The stop fired in the warm-up, so the path is not trivially buy-and-hold.
    assert any(t["exit_reason"] == "stop_loss" for t in result.trades)
    assert series.keys() == expected.keys()
    for session in expected:
        assert series[session] == pytest.approx(expected[session], rel=1e-12)
    assert series[SLOW_WS] == pytest.approx(SLOW_SEED)
