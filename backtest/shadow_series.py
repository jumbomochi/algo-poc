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

**Capital flows (KAN-113) are replayed, not rescaled.** When the operator
credits or transfers sleeve cash inside the window, the shadow is seeded at
live's RAW NAV on the window's first session — what live actually had — and
receives the same cash on the same session live's snapshots first include it
(``shared.capital_flows.flow_steps``). Its capacity therefore matches live's:
the pre-flow book is sized on the pre-flow capital, the credit becomes
available to size from that session on (KAN-111's cash cap sees it), and if
the strategy does not deploy it, it sits idle on both sides. Both curves are
then compared on the flow-adjusted (time-weighted) basis: the shadow's curve
is returned with its own applied flows removed, exactly as the monitor removes
live's. Seeding at a rescaled NAV instead ran the whole window on the larger
capital and manufactured divergence of the size of the credit times the move.
A window with no flow inside it is replayed exactly as before.

The function is deliberately dependency-light: the caller supplies the built
``signals_fn`` and ``risk_engine``, so this module never has to know the sleeve
roster and cannot drift away from how ``scripts/run_paper.py`` configures them.
"""
from __future__ import annotations

from datetime import date
from collections.abc import Mapping
from typing import Any, Callable

from backtest.costs import CostModel
from backtest.runner import BacktestRunner
from backtest.simulator import SimulatedExecutor
from services.risk_management.funding import DEFAULT_SLEEVE_CASH_BUFFER_BPS
from shared.capital_flows import flow_adjust


def replay_window(
    *,
    bars_by_ticker: dict[str, list[dict]],
    signals_fn: Callable[[str, list[dict]], dict | None],
    risk_engine: Any,
    seed_nav: float,
    window_start: date,
    cost_model: CostModel | None = None,
    whole_shares: bool = False,
    cash_buffer_bps: float = DEFAULT_SLEEVE_CASH_BUFFER_BPS,
    cash_flows: Mapping[date, float] | None = None,
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
        cash_buffer_bps: Live's ``currency.sleeve_cash_buffer_bps``. Entries
            are funded from the replay's own cash exactly as the paper run
            funds them (KAN-111; see ``BacktestRunner``).
        cash_flows: KAN-113. ``{session: signed USD}`` — the sleeve's recorded
            capital flows, keyed by the session whose value first includes
            each. Only flows strictly after ``window_start`` are replayed (one
            on ``window_start`` is already in ``seed_nav``). When any is
            applied, the returned curve is flow-adjusted (time-weighted,
            anchored at its last session), the basis the monitor grades live
            on. A withdrawal the replay's cash cannot cover is clamped by
            ``BacktestRunner`` (it cannot sell to fund it).

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
        cash_buffer_bps=cash_buffer_bps,
    )
    inside = {
        session: float(amount)
        for session, amount in (cash_flows or {}).items()
        if session > window_start and amount
    }
    if inside:
        result = runner.run(
            bars_by_ticker,
            signals_fn,
            risk_engine,
            trade_start_date=window_start,
            cash_flows=inside,
        )
    else:
        # Exactly the pre-KAN-113 call: a window without flows is unchanged.
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
    curve = {
        session: value
        for session, value in zip(result.dates, end_of_day)
        if session >= window_start
    }
    if not result.cash_flows_applied:
        return curve
    running = 0.0
    included: dict[date, float] = {}
    applied = sorted(result.cash_flows_applied.items())
    for session in sorted(curve):
        while applied and applied[0][0] <= session:
            running += applied.pop(0)[1]
        included[session] = running
    return flow_adjust(curve, included)


def build_shadow_series(
    *,
    portfolios: dict[str, Any],
    bars_by_ticker: dict[str, list[dict]],
    live_equity: dict[str, dict[date, float]],
    window_sessions: int,
    cost_model: CostModel | None = None,
    whole_shares: bool = False,
    cash_buffer_bps: float = DEFAULT_SLEEVE_CASH_BUFFER_BPS,
    cash_flows: Mapping[str, Mapping[date, float]] | None = None,
) -> dict[str, dict[date, float]]:
    """Replay every sleeve over its rolling window.

    Args:
        portfolios: Sleeve name -> an object exposing ``signals_fn`` and
            ``risk_engine`` (``PortfolioConfig`` in production). Built with no
            live ``portfolio_context``, so the replay is the model's own
            counterfactual rather than a re-scoring of live's positions.
        bars_by_ticker: The union of bars the 05:15 run already fetched. Each
            sleeve's ``signals_fn`` scopes itself to its own universe.
        live_equity: Sleeve name -> live NAV by session, from
            ``equity_snapshots`` — RAW, not flow-adjusted: the seed is what
            live actually held on the window's first session.
        window_sessions: Comparison window length.
        cash_flows: KAN-113. Sleeve name -> ``{session: signed USD}`` of its
            recorded capital flows (``capital_flow_steps_by_sleeve`` in
            scripts/run_paper.py); replayed into the shadow on the same
            sessions (see :func:`replay_window`).

    Returns:
        Sleeve name -> ``{session: equity}``. A sleeve with no live history is
        **absent** rather than present-and-zero: the book has never recorded it,
        so there is nothing to seed from, and a zero curve would read as a total
        loss instead of as an ungradeable sleeve.

    The window is derived per sleeve from *live's* sessions, never from the
    bars. Bars can run ahead of the book — a session prints but the 05:15 job
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
            cash_buffer_bps=cash_buffer_bps,
            cash_flows=(cash_flows or {}).get(name),
        )
        # Live is the authority on which sessions are gradeable: the replay can
        # only produce a curve for sessions its bars cover, and the monitor
        # intersects the two sides anyway.
        clipped = {d: v for d, v in series.items() if d in live}
        if clipped:
            out[name] = clipped

    return out
