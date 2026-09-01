"""The rolling shadow series the divergence monitor grades live against.

A pinned 10-year artifact cannot score future sessions: the monitor intersects
live dates with the artifact's, so ``window_end`` freezes at the baseline's last
bar. Measured 2026-08-31, six consecutive daily runs all reported
``window_start=2026-07-10 window_end=2026-08-14`` and overwrote the same
evidence row. Un-blinding that feed would have produced a confident permanently
stale verdict.

The shadow replaces the feed. Each night it replays the sleeve's own signal
function over the bars live just fetched, seeded at live's NAV N sessions back,
and produces an equity curve through the current session. The verdict then
answers the question a daily monitor is for: *did live track the model over
this window?*

Two properties carry the design and are pinned here:

* **the seed is live's NAV, not the sleeve's allocation** — same starting
  capital on both sides is what makes an absolute pp gap mean anything;
* **warm-up bars feed the indicators but never trade** — a 126-session momentum
  lookback needs history before the window, and an entry taken in that history
  would be a position live never had.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from backtest.shadow_series import replay_window


SESSIONS = [date(2026, 1, 5) + timedelta(days=i) for i in range(60)]
WINDOW_START = SESSIONS[-10]


def _bars(closes: list[float]) -> list[dict]:
    return [
        {
            "date": day,
            "open": close,
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
            "volume": 1_000_000,
        }
        for day, close in zip(SESSIONS, closes)
    ]


def _rising(rate: float) -> list[dict]:
    return _bars([100.0 * (1.0 + rate * i) for i in range(len(SESSIONS))])


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


def _buys_on(target: date):
    """A signal fn that emits exactly one buy, on ``target``."""

    def signals_fn(ticker: str, bars: list[dict]) -> dict | None:
        if bars[-1]["date"] != target or ticker != "AAA":
            return None
        return {
            "action": "buy",
            "ticker": ticker,
            "limit_price": bars[-1]["close"] * 1.05,
            "quantity": 10.0,
            "sector": "Tech",
        }

    return signals_fn


def test_the_series_starts_at_the_seeded_live_nav() -> None:
    """The whole point of the rolling anchor: both sides start level."""
    series = replay_window(
        bars_by_ticker={"AAA": _rising(0.01)},
        signals_fn=_never_trades,
        risk_engine=_AlwaysApproves(),
        seed_nav=41_234.56,
        window_start=WINDOW_START,
    )

    assert series[WINDOW_START] == pytest.approx(41_234.56)


def test_the_series_covers_the_window_through_the_last_session() -> None:
    series = replay_window(
        bars_by_ticker={"AAA": _rising(0.01)},
        signals_fn=_never_trades,
        risk_engine=_AlwaysApproves(),
        seed_nav=10_000.0,
        window_start=WINDOW_START,
    )

    assert min(series) == WINDOW_START
    assert max(series) == SESSIONS[-1]
    assert len(series) == 10


def test_no_session_before_the_window_appears_in_the_series() -> None:
    """Warm-up history is fed to the indicators, never reported as the window."""
    series = replay_window(
        bars_by_ticker={"AAA": _rising(0.01)},
        signals_fn=_never_trades,
        risk_engine=_AlwaysApproves(),
        seed_nav=10_000.0,
        window_start=WINDOW_START,
    )

    assert not [d for d in series if d < WINDOW_START]


def test_warmup_bars_reach_the_signal_function() -> None:
    """A 126-session lookback needs history the window does not contain."""
    seen: list[date] = []

    def recording_fn(ticker: str, bars: list[dict]) -> dict | None:
        seen.append(bars[-1]["date"])
        return None

    replay_window(
        bars_by_ticker={"AAA": _rising(0.01)},
        signals_fn=recording_fn,
        risk_engine=_AlwaysApproves(),
        seed_nav=10_000.0,
        window_start=WINDOW_START,
    )

    assert SESSIONS[0] in seen, "the signal fn never saw pre-window history"
    assert len([d for d in seen if d < WINDOW_START]) == 50


def test_a_buy_inside_the_window_moves_the_curve() -> None:
    """Guards against a series that is seeded correctly and then inert."""
    series = replay_window(
        bars_by_ticker={"AAA": _rising(0.01)},
        signals_fn=_buys_on(WINDOW_START),
        risk_engine=_AlwaysApproves(),
        seed_nav=10_000.0,
        window_start=WINDOW_START,
    )

    assert series[SESSIONS[-1]] != pytest.approx(10_000.0)


def test_a_buy_signalled_before_the_window_is_not_taken() -> None:
    """Entries in warm-up would be positions live never held."""
    flat = replay_window(
        bars_by_ticker={"AAA": _rising(0.01)},
        signals_fn=_never_trades,
        risk_engine=_AlwaysApproves(),
        seed_nav=10_000.0,
        window_start=WINDOW_START,
    )
    warmup_buy = replay_window(
        bars_by_ticker={"AAA": _rising(0.01)},
        signals_fn=_buys_on(SESSIONS[5]),
        risk_engine=_AlwaysApproves(),
        seed_nav=10_000.0,
        window_start=WINDOW_START,
    )

    assert warmup_buy == flat


def test_a_window_start_after_every_session_yields_nothing() -> None:
    """No session to date the verdict by — the caller must see empty, not zero."""
    series = replay_window(
        bars_by_ticker={"AAA": _rising(0.01)},
        signals_fn=_never_trades,
        risk_engine=_AlwaysApproves(),
        seed_nav=10_000.0,
        window_start=SESSIONS[-1] + timedelta(days=30),
    )

    assert series == {}


def test_empty_bars_yield_nothing() -> None:
    series = replay_window(
        bars_by_ticker={},
        signals_fn=_never_trades,
        risk_engine=_AlwaysApproves(),
        seed_nav=10_000.0,
        window_start=WINDOW_START,
    )

    assert series == {}
