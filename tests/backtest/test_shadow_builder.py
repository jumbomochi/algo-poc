"""Assembling the per-sleeve shadow the monitor grades against.

``replay_window`` handles one sleeve. This is the layer above it: pick the
window from live's own session history, seed each sleeve at its own live NAV on
that session, and hand back the ``{sleeve: {date: equity}}`` mapping the
monitor already knows how to consume.

The window is chosen from **live's** sessions, not from the bars. Bars can run
ahead of the book (a bar prints for a session the 04:15 job never recorded, if
it aborted), and grading a session live has no NAV for would compare a real
number against nothing.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from backtest.shadow_series import build_shadow_series


SESSIONS = [date(2026, 1, 5) + timedelta(days=i) for i in range(60)]
#: A test-sized warm-up (production: 160 sessions after 200 of lookback),
#: which these 60-session fixtures can hold for every window below but the
#: young-book one.
WARM = {"warmup_sessions": 20, "lookback_sessions": 10}
COLD = {"warmup_sessions": 0, "lookback_sessions": 0}


def _bars(closes: list[float]) -> list[dict]:
    return [
        {
            "date": day, "open": c, "high": c * 1.02,
            "low": c * 0.98, "close": c, "volume": 1_000_000,
        }
        for day, c in zip(SESSIONS, closes)
    ]


def _rising(rate: float) -> list[dict]:
    return _bars([100.0 * (1.0 + rate * i) for i in range(len(SESSIONS))])


class _AlwaysApproves:
    def check_entry(self, ticker, quantity, price, sector, portfolio, **kwargs):
        class _D:
            approved = True
            adjusted_quantity = quantity
            reason = "ok"

        return _D()


class _Sleeve:
    """Stands in for PortfolioConfig — only these three fields are read."""

    def __init__(self, name, signals_fn):
        self.name = name
        self.signals_fn = signals_fn
        self.risk_engine = _AlwaysApproves()


def _never(ticker, bars):
    return None


def _live(nav_by_session: dict[date, float]) -> dict[date, float]:
    return dict(nav_by_session)


def test_the_window_is_taken_from_live_sessions_not_from_bars() -> None:
    """Bars can run ahead of the book; a session live never recorded is not
    gradeable, so it must not define the window."""
    live = _live({d: 10_000.0 for d in SESSIONS[:50]})  # book stops 10 early

    out = build_shadow_series(
        portfolios={"momentum": _Sleeve("momentum", _never)},
        bars_by_ticker={"AAA": _rising(0.01)},
        live_equity={"momentum": live},
        window_sessions=10,
        **WARM,
    )

    assert max(out["momentum"]) == SESSIONS[49]


def test_each_sleeve_is_seeded_at_its_own_live_nav() -> None:
    live_mom = _live({d: 20_000.0 for d in SESSIONS})
    live_sec = _live({d: 33_333.0 for d in SESSIONS})

    out = build_shadow_series(
        portfolios={
            "momentum": _Sleeve("momentum", _never),
            "sector_rotation": _Sleeve("sector_rotation", _never),
        },
        bars_by_ticker={"AAA": _rising(0.01)},
        live_equity={"momentum": live_mom, "sector_rotation": live_sec},
        window_sessions=10,
        **WARM,
    )

    window_start = SESSIONS[-10]
    assert out["momentum"][window_start] == pytest.approx(20_000.0)
    assert out["sector_rotation"][window_start] == pytest.approx(33_333.0)


def test_the_window_length_is_honoured() -> None:
    live = _live({d: 10_000.0 for d in SESSIONS})

    out = build_shadow_series(
        portfolios={"momentum": _Sleeve("momentum", _never)},
        bars_by_ticker={"AAA": _rising(0.01)},
        live_equity={"momentum": live},
        window_sessions=15,
        **WARM,
    )

    assert len(out["momentum"]) == 15
    assert min(out["momentum"]) == SESSIONS[-15]


def test_a_shorter_live_history_than_the_window_uses_what_exists() -> None:
    """Early in an epoch the book is younger than the window. That is not an
    error — it is the ``Only N overlapping days`` note the monitor already
    renders."""
    live = _live({d: 10_000.0 for d in SESSIONS[:4]})

    out = build_shadow_series(
        portfolios={"momentum": _Sleeve("momentum", _never)},
        bars_by_ticker={"AAA": _rising(0.01)},
        live_equity={"momentum": live},
        window_sessions=30,
        **COLD,
    )

    assert len(out["momentum"]) == 4


def test_a_sleeve_with_no_live_history_is_skipped_not_zeroed() -> None:
    """A sleeve the book has never recorded cannot be graded. Emitting a zero
    curve would read as a 100% drawdown."""
    out = build_shadow_series(
        portfolios={"momentum": _Sleeve("momentum", _never)},
        bars_by_ticker={"AAA": _rising(0.01)},
        live_equity={},
        window_sessions=10,
        **WARM,
    )

    assert "momentum" not in out


def test_no_live_history_at_all_yields_nothing() -> None:
    out = build_shadow_series(
        portfolios={"momentum": _Sleeve("momentum", _never)},
        bars_by_ticker={"AAA": _rising(0.01)},
        live_equity={"momentum": {}},
        window_sessions=10,
        **WARM,
    )

    assert out == {}


def test_the_warm_up_reaches_every_sleeve_and_each_starts_level() -> None:
    """KAN-105: each sleeve's replay trades its warm-up (its signal fn is first
    called ``warmup_sessions`` before its window) and its curve is still
    normalised to its own live NAV on the window's first session."""
    first_seen: dict[str, date] = {}

    def buys_once(name):
        def fn(ticker, bars):
            first_seen.setdefault(name, bars[-1]["date"])
            if len(bars) == 31:  # the first warm-up session
                return {"action": "buy", "ticker": ticker, "quantity": 50.0,
                        "limit_price": bars[-1]["close"] * 1.05,
                        "sector": "Tech"}
            return None
        return fn

    live = {
        "momentum": _live({d: 20_000.0 for d in SESSIONS}),
        "sector_rotation": _live({d: 33_333.0 for d in SESSIONS}),
    }
    out = build_shadow_series(
        portfolios={n: _Sleeve(n, buys_once(n)) for n in live},
        bars_by_ticker={"AAA": _rising(0.01)},
        live_equity=live,
        window_sessions=10,
        **WARM,
    )

    window_start = SESSIONS[-10]
    assert first_seen == {n: SESSIONS[30] for n in live}
    assert out["momentum"][window_start] == pytest.approx(20_000.0)
    assert out["sector_rotation"][window_start] == pytest.approx(33_333.0)
    # The warm book is held through the window: the rising name moves it.
    assert out["momentum"][SESSIONS[-1]] > 20_000.0
