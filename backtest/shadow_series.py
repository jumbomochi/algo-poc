"""Replay a sleeve over a rolling window to produce the divergence feed.

``scripts/divergence_monitor.py`` used to grade live against a pinned 10-year
backtest artifact. That artifact cannot score sessions later than its own last
bar, and the monitor takes the intersection of live and artifact dates
(``backtest.divergence.align_and_window``), so the comparison window froze at
the baseline's tail: six consecutive runs in August 2026 all scored
``2026-07-10 .. 2026-08-14`` and overwrote the same evidence row.

This module produces the replacement. Given the bars live just fetched, it
replays the sleeve's own signal function forward and returns the equity curve
the model would have produced over the window ending at the current session.

Two choices define what the resulting verdict means:

**The window is seeded at live's NAV, not at the sleeve's allocation.** Both
sides therefore start the window level, which is what makes an absolute
percentage-point gap interpretable, and it scopes the verdict to drift that
started *inside* the window rather than to everything accumulated since the
epoch began. The cost is that drift too slow to breach in any single window
never breaches; the breach *streak* in the evidence store is what covers that.

**Bars before the window feed the indicators but never trade.** A 126-session
momentum lookback needs history the window does not contain, and this is what
``BacktestRunner.run``'s ``trade_start_date`` already does: earlier bars reach
``signals_fn`` for warm-up while entries are refused until the window opens.
Without it the shadow would open positions live never held and the curves would
diverge for a reason that is purely an artifact of the replay.

The function is deliberately dependency-light: the caller supplies the built
``signals_fn`` and ``risk_engine``, so this module never has to know the sleeve
roster and cannot drift away from how ``scripts/run_paper.py`` configures them.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable

from backtest.costs import CostModel
from backtest.runner import BacktestRunner
from backtest.simulator import SimulatedExecutor


def replay_window(
    *,
    bars_by_ticker: dict[str, list[dict]],
    signals_fn: Callable[[str, list[dict]], dict | None],
    risk_engine: Any,
    seed_nav: float,
    window_start: date,
    cost_model: CostModel | None = None,
    whole_shares: bool = False,
) -> dict[date, float]:
    """Return ``{session: equity}`` for the model's own run over the window.

    Args:
        bars_by_ticker: Bars for this sleeve's universe, warm-up history
            included. Sessions before ``window_start`` reach ``signals_fn`` but
            cannot be traded.
        signals_fn: The sleeve's signal function, built exactly as the live path
            builds it.
        risk_engine: The sleeve's risk engine, same.
        seed_nav: Live's NAV on ``window_start``. The shadow starts level with
            live so the two curves are comparable.
        window_start: First session of the comparison window.
        cost_model: Fill costs. Defaults to the repo's standard model, which is
            what the live sleeves are charged against.
        whole_shares: Truncate fractional sizing, as live execution does.

    Returns:
        Equity by session for ``window_start`` onward. Empty when no session
        falls in the window — there is nothing to date a verdict by, and the
        caller must be able to tell that from a zero.
    """
    if not bars_by_ticker:
        return {}

    runner = BacktestRunner(
        SimulatedExecutor(cost_model or CostModel()),
        initial_capital=seed_nav,
        whole_shares=whole_shares,
    )
    result = runner.run(
        bars_by_ticker,
        signals_fn,
        risk_engine,
        trade_start_date=window_start,
    )

    # ``portfolio_values`` carries pre-day-0 capital at index 0, so element
    # i+1 is the end-of-day value for ``dates[i]`` — the same alignment
    # ``load_backtest_equity_series`` applies to the artifact.
    end_of_day = result.portfolio_values[1:]
    return {
        session: value
        for session, value in zip(result.dates, end_of_day)
        if session >= window_start
    }


def build_shadow_series(
    *,
    portfolios: dict[str, Any],
    bars_by_ticker: dict[str, list[dict]],
    live_equity: dict[str, dict[date, float]],
    window_sessions: int,
    cost_model: CostModel | None = None,
    whole_shares: bool = False,
) -> dict[str, dict[date, float]]:
    """Replay every sleeve over its rolling window.

    Args:
        portfolios: Sleeve name -> an object exposing ``signals_fn`` and
            ``risk_engine`` (``PortfolioConfig`` in production). Built with no
            live ``portfolio_context``, so the replay is the model's own
            counterfactual rather than a re-scoring of live's positions.
        bars_by_ticker: The union of bars the 04:15 run already fetched. Each
            sleeve's ``signals_fn`` scopes itself to its own universe.
        live_equity: Sleeve name -> live NAV by session, from
            ``equity_snapshots``.
        window_sessions: Comparison window length.

    Returns:
        Sleeve name -> ``{session: equity}``. A sleeve with no live history is
        **absent** rather than present-and-zero: the book has never recorded it,
        so there is nothing to seed from, and a zero curve would read as a total
        loss instead of as an ungradeable sleeve.

    The window is derived per sleeve from *live's* sessions, never from the
    bars. Bars can run ahead of the book — a session prints but the 04:15 job
    aborted before writing a snapshot — and grading a session live has no NAV
    for would compare a real number against nothing.
    """
    out: dict[str, dict[date, float]] = {}

    for name, sleeve in portfolios.items():
        live = live_equity.get(name) or {}
        if not live:
            continue

        sessions = sorted(live)
        window_start = sessions[-window_sessions:][0]

        series = replay_window(
            bars_by_ticker=bars_by_ticker,
            signals_fn=sleeve.signals_fn,
            risk_engine=sleeve.risk_engine,
            seed_nav=live[window_start],
            window_start=window_start,
            cost_model=cost_model,
            whole_shares=whole_shares,
        )
        # Live is the authority on which sessions are gradeable: the replay can
        # only produce a curve for sessions its bars cover, and the monitor
        # intersects the two sides anyway.
        clipped = {d: v for d, v in series.items() if d in live}
        if clipped:
            out[name] = clipped

    return out
