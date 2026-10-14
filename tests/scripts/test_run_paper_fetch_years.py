"""KAN-105: the 05:15 fetch is sized for the shadow's warm-up, not for live.

The rolling shadow trades a 160-session warm-up before its 30-session window,
and the warm-up's first decision needs 200 sessions of indicator history (the
breadth regime's 200-session MA). That is 390 sessions; one year of bars holds
~250. So the paper run's fetch is DERIVED from
``divergence.shadow_warmup_sessions`` — 2 years at N = 160 — instead of being a
literal ``--years 1`` that a change of N would silently leave short.

The cost of the deeper fetch is time (+1 IB request per ticker). What it must
NOT cost is a different live decision. Every lookback the live signals use is a
trailing window inside one year, so the same last year of bars must produce the
same regime and the same signals whether or not a second year precedes it.
That is pinned below on the production signal functions.
"""
from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

import pytest

from backtest.shadow_series import (
    SHADOW_LOOKBACK_SESSIONS,
    SHADOW_WARMUP_SESSIONS,
    shadow_fetch_years,
)
from scripts.run_backtest import compute_regime_by_date
from scripts.run_paper import (
    LIVE_SIGNAL_YEARS,
    SHADOW_WINDOW_SESSIONS,
    build_portfolios,
    resolve_fetch_years,
)
from shared.config import DivergenceConfig, load_config
from shared.market_calendar import MarketCalendar
from shared.universe import ACTIVE_SLEEVES
from tests.backtest.books import empty_context

ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# The fetch
# ---------------------------------------------------------------------------


def test_a_graded_run_fetches_two_years_at_the_default_warm_up() -> None:
    assert resolve_fetch_years(None, portfolio_tag=None,
                               warmup_sessions=160) == 2


def test_the_fetch_follows_the_configured_warm_up() -> None:
    """A larger N cannot leave the fetch short: it is derived, not a literal."""
    assert resolve_fetch_years(None, portfolio_tag=None,
                               warmup_sessions=400) == 3
    assert resolve_fetch_years(None, portfolio_tag=None,
                               warmup_sessions=0) == 1


def test_an_explicit_years_wins() -> None:
    assert resolve_fetch_years(1, portfolio_tag=None, warmup_sessions=160) == 1
    assert resolve_fetch_years(3, portfolio_tag=None, warmup_sessions=160) == 3


def test_a_tagged_run_produces_no_shadow_and_fetches_one_year() -> None:
    assert resolve_fetch_years(None, portfolio_tag="drill_x",
                               warmup_sessions=160) == LIVE_SIGNAL_YEARS == 1


@pytest.mark.parametrize("end", [date(2026, 10, 9), date(2027, 3, 12),
                                 date(2026, 1, 2)])
def test_two_years_of_calendar_days_hold_window_warm_up_and_lookback(end) -> None:
    """``fetch_bars_from_ib(years=2)`` asks for ``2 * 365`` calendar days. The
    NYSE sessions in that span must cover 30 + 160 + 200, holidays included."""
    years = resolve_fetch_years(None, portfolio_tag=None,
                                warmup_sessions=SHADOW_WARMUP_SESSIONS)
    sessions = MarketCalendar().trading_sessions(
        end - timedelta(days=years * 365), end
    )
    needed = SHADOW_WINDOW_SESSIONS + SHADOW_WARMUP_SESSIONS + SHADOW_LOOKBACK_SESSIONS
    assert needed == 390
    assert len(sessions) >= needed
    assert years == shadow_fetch_years(window_sessions=SHADOW_WINDOW_SESSIONS)


def test_the_config_key_matches_the_code_default() -> None:
    """``divergence.shadow_warmup_sessions`` is what the 05:15 run passes; the
    module default is what every other caller gets. They must agree, or the
    fingerprint and the fetch would describe different models."""
    assert DivergenceConfig().shadow_warmup_sessions == SHADOW_WARMUP_SESSIONS
    config = load_config(str(ROOT / "config/default.yaml"))
    assert config.divergence.shadow_warmup_sessions == SHADOW_WARMUP_SESSIONS


# ---------------------------------------------------------------------------
# Live decisions are unchanged by the second year
# ---------------------------------------------------------------------------


def _business_days(end: date, count: int) -> list[date]:
    days: list[date] = []
    day = end
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return sorted(days)


SESSIONS = _business_days(date(2026, 10, 9), 504)
ONE_YEAR = 252


def _two_year_bars() -> dict[str, list[dict]]:
    """A seeded random walk per ticker with an upward drift, so breadth sits
    in "bull" and the sleeves have something to buy."""
    from shared.universe import ACTIVE_SLEEVES, UNIVERSE_REGISTRY

    rng = random.Random(105)
    universe = sorted({t for s in ACTIVE_SLEEVES for t in UNIVERSE_REGISTRY[s]})
    bars: dict[str, list[dict]] = {}
    for ticker in universe:
        drift = rng.uniform(0.0002, 0.0015)
        price = rng.uniform(20.0, 300.0)
        rows = []
        for day in SESSIONS:
            price *= 1.0 + drift + rng.gauss(0.0, 0.015)
            rows.append({"date": day, "open": price * 0.998,
                         "high": price * 1.01, "low": price * 0.99,
                         "close": price, "volume": 1_000_000})
        bars[ticker] = rows
    return bars


def _live_signals(bars_by_ticker: dict[str, list[dict]]) -> dict:
    """What ``run_daily`` asks each sleeve on the run's last session."""
    regime = compute_regime_by_date(bars_by_ticker)
    portfolios = build_portfolios(
        capital=100_000.0,
        bars_by_ticker=bars_by_ticker,
        regime_by_date=regime,
        fundamentals_lookup=lambda t, d: {
            "roe": 0.3, "debt_equity": 0.4, "profit_margin": 0.2,
        },
        earnings_lookup=lambda t, d: {"surprise_pct": 8.0},
        # run_daily hydrates every sleeve's book before building it; an empty
        # one asks each sleeve the entry question for every ticker.
        portfolio_contexts={name: empty_context() for name in ACTIVE_SLEEVES},
    )
    out = {}
    for name, sleeve in portfolios.items():
        for ticker, bars in bars_by_ticker.items():
            signal = sleeve.signals_fn(ticker, bars)
            if signal is not None:
                out[(name, ticker)] = signal
    return out


def test_the_second_year_changes_no_live_decision() -> None:
    two_years = _two_year_bars()
    one_year = {t: rows[-ONE_YEAR:] for t, rows in two_years.items()}

    regime_2y = compute_regime_by_date(two_years)
    regime_1y = compute_regime_by_date(one_year)
    shared = set(regime_1y) & set(regime_2y)
    assert shared, "one year of bars defines no regime at all"
    assert {d: regime_1y[d] for d in shared} == {d: regime_2y[d] for d in shared}
    assert regime_1y[SESSIONS[-1]] == regime_2y[SESSIONS[-1]]

    signals_2y = _live_signals(two_years)
    signals_1y = _live_signals(one_year)

    # Not vacuous: every sleeve decides something on this session.
    from shared.universe import ACTIVE_SLEEVES

    assert {name for name, _ in signals_1y} == set(ACTIVE_SLEEVES)
    assert signals_2y == signals_1y
