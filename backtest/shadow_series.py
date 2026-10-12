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

Three choices define what the resulting verdict means:

**The window starts level with live.** The returned curve is normalised so its
value on the window's first session is live's NAV there. Both sides therefore
start the window level, which is what makes an absolute percentage-point gap
interpretable, and it scopes the verdict to drift that started *inside* the
window rather than to everything accumulated since the epoch began. The cost is
that drift too slow to breach in any single window never breaches; the breach
*streak* in the evidence store is what covers that.

**The window opens on the model's own book, not on cash (KAN-105).** The
replay trades a warm-up of :data:`SHADOW_WARMUP_SESSIONS` sessions before the
window, so on the window's first session the shadow holds what the model would
hold. Until KAN-105 the bars before the window reached ``signals_fn`` but its
buys were refused (``BacktestRunner.run``'s ``trade_start_date``). That was
not a cold start, it was a phantom one: every sleeve's signal function records
a lot in its private ``tracked`` book when it *emits* a buy, not when the buy
fills, so after ~230 warm-up sessions it believed it held the whole top-N while
the replay held cash — and it never bought those names inside the window.
Measured on the 2026-10-09 run, sector_rotation held all 11 of its ETFs as
phantoms and took its first entry 18 sessions into a 30-session window, and
momentum's recorded BREACH streak was an artifact of it
(``docs/decisions/shadow-seeding-2026-10.md``). Now:

* ``signals_fn`` is **never called** for a bar dated before ``warm_from``, the
  session :data:`SHADOW_WARMUP_SESSIONS` before the window on the bars' own
  calendar. Its first call still receives the full history in ``bars``, so
  every lookback is warm; the only state the earlier calls used to build was
  the phantom book.
* The replay **trades from** ``warm_from``, seeded at live's NAV there.
* The curve from the window's first session on is **multiplied** by
  ``k = seed_nav / nav(window_start)``. The book itself is not rebased: under
  whole shares, flooring small lots to a rescaled NAV distorts the very
  exposure the warm-up built (measured: quality_value 63% -> 33% invested).
  Grading uses returns only, and the monitor's AGGREGATE sums levels, so a
  scaled curve is both sufficient and necessary.
* Bars too short to hold the warm-up plus :data:`SHADOW_LOOKBACK_SESSIONS` of
  indicator history raise :class:`ShadowWarmupTruncated`. A shorter warm-up
  would silently grade a different model under the same ``baseline_id``.
  :func:`build_shadow_replays` contains the failure to that sleeve and names
  it, so the monitor records the sleeve NO_DATA with the reason.

**Capital flows (KAN-113) are replayed, not rescaled away.** When the operator
credits or transfers sleeve cash inside the window, the shadow receives it on
the same session live's snapshots first include it
(``shared.capital_flows.flow_steps``), sized by ``1/k`` so it is the same
fraction of the shadow's book as of live's. Its capacity therefore matches
live's: the pre-flow book is sized on the pre-flow capital, the credit becomes
available to size from that session on (KAN-111's cash cap sees it), and if
the strategy does not deploy it, it sits idle on both sides. Both curves are
then compared on the flow-adjusted (time-weighted) basis: the shadow's curve
is returned with its own applied flows removed, exactly as the monitor removes
live's. Flows on or before the window's first session are already inside
live's NAV there, which the normalisation absorbs (as is one dated before the
first session the bars hold, when they lack live's first; see
:func:`replay_window_detail`).

The function is deliberately dependency-light: the caller supplies the built
``signals_fn`` and ``risk_engine``, so this module never has to know the sleeve
roster and cannot drift away from how ``scripts/run_paper.py`` configures them.
"""
from __future__ import annotations

import logging
import math
from bisect import bisect_left
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from backtest.costs import CostModel
from backtest.runner import BacktestResult, BacktestRunner, collect_sorted_dates
from backtest.simulator import SimulatedExecutor
from services.risk_management.funding import DEFAULT_SLEEVE_CASH_BUFFER_BPS
from shared.capital_flows import flow_adjust

logger = logging.getLogger(__name__)

#: KAN-105. Sessions the replay trades before the window opens, the same for
#: every sleeve. 160 is the smallest round N at which momentum (0.02 pp),
#: quality_value (0.19 pp), earnings_drift and tail_risk_hedge (<0.02 pp) grade
#: within noise of the longest warm-up two years of bars allow, and it covers
#: the p90 holding-path convergence of five of six sleeves. sector_rotation,
#: which exits only on an 8% trailing stop, never fully converges and keeps
#: ~0.5-0.9 pp of path dependence at any practical N. The measurement is in
#: ``docs/decisions/shadow-seeding-2026-10.md``. Configured as
#: ``divergence.shadow_warmup_sessions``; it enters the shadow's fingerprint,
#: so changing it files verdicts under a new ``baseline_id``.
SHADOW_WARMUP_SESSIONS = 160

#: Sessions of history every indicator needs before the warm-up's first
#: decision: the 200-session moving average behind the breadth regime every
#: sleeve gates on (``scripts.run_backtest.compute_regime_by_date``) and,
#: inside that, momentum's 126-session lookback.
SHADOW_LOOKBACK_SESSIONS = 200

#: NYSE sessions in one year of ``fetch_bars_from_ib(years=...)`` (365
#: calendar days): 250-252. The low end, so a derived fetch never comes up
#: short.
SESSIONS_PER_FETCH_YEAR = 250


class ShadowWarmupTruncated(ValueError):
    """The bars cannot hold the warm-up plus its indicator history.

    Raised rather than degraded: a replay that quietly warmed up for fewer
    sessions would be a different model graded under the same ``baseline_id``.
    """


def shadow_fetch_years(
    *,
    window_sessions: int,
    warmup_sessions: int = SHADOW_WARMUP_SESSIONS,
    lookback_sessions: int = SHADOW_LOOKBACK_SESSIONS,
) -> int:
    """Years of daily bars the paper run must fetch for the shadow.

    ``window + warmup + lookback`` sessions, in whole years of
    :data:`SESSIONS_PER_FETCH_YEAR`. At the defaults that is 30 + 160 + 200 =
    390 sessions, so 2 years (~500 sessions). Never less than 1, which is what
    the live signals alone need.
    """
    needed = window_sessions + warmup_sessions + lookback_sessions
    return max(1, math.ceil(needed / SESSIONS_PER_FETCH_YEAR))


@dataclass(frozen=True)
class WindowReplay:
    """One sleeve's replay: the graded curve and how it was seeded."""

    #: Equity by session from the window's first session on, normalised to
    #: live's NAV there (and flow-adjusted when a flow was replayed).
    curve: dict[date, float]
    #: First session ``signals_fn`` saw and the replay could trade.
    warm_from: date | None = None
    #: Market value over NAV at the window's first session's close: how
    #: invested the model's own book was when grading began.
    invested_at_start: float | None = None
    #: Why the replay failed (``"<ExceptionType>: <message>"``), when it did;
    #: ``curve`` is then empty. Set only by :func:`build_shadow_replays`.
    error: str | None = None


def _gated(
    signals_fn: Callable[[str, list[dict]], dict | None], warm_from: date
) -> Callable[[str, list[dict]], dict | None]:
    """``signals_fn``, never called for a bar dated before ``warm_from``.

    Returning ``None`` there instead of calling it is the fix: a call the
    runner then refuses to trade still records a lot in the signal function's
    private book (see the module docstring).
    """

    def gated(ticker: str, bars: list[dict]) -> dict | None:
        if bars[-1]["date"] < warm_from:
            return None
        return signals_fn(ticker, bars)

    return gated


def _invested_at(
    result: BacktestResult,
    bars_by_ticker: Mapping[str, list[dict]],
    session: date,
    nav: float,
) -> float:
    """Market value over NAV at ``session``'s close, from the run's own lots.

    A lot is held at a close when it filled at or before that session's open
    and had not exited by then (exits fill at an open, so a lot exiting on
    ``session`` is already gone at its close).
    """
    held: list[tuple[str, float]] = [
        (t["ticker"], t["quantity"])
        for t in result.trades
        if t["entry_date"] <= session < t["exit_date"]
    ]
    held.extend(
        (lot["ticker"], lot["quantity"])
        for lot in result.open_positions
        if lot["entry_date"] <= session
    )
    market_value = 0.0
    for ticker, quantity in held:
        closes = [b["close"] for b in bars_by_ticker.get(ticker, [])
                  if b["date"] <= session]
        if closes:
            market_value += closes[-1] * quantity
    return market_value / nav if nav > 0 else 0.0


def replay_window_detail(
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
    warmup_sessions: int = SHADOW_WARMUP_SESSIONS,
    lookback_sessions: int = SHADOW_LOOKBACK_SESSIONS,
) -> WindowReplay:
    """:func:`replay_window`, plus how the window was seeded.

    Raises:
        ShadowWarmupTruncated: Fewer than ``lookback_sessions`` sessions of
            bars precede ``warm_from``.
    """
    if not bars_by_ticker:
        return WindowReplay(curve={})

    # The session calendar is the union of bar dates — exactly the sessions
    # the runner replays — so N sessions back means N replayed sessions.
    sessions = collect_sorted_dates(bars_by_ticker)
    start_index = bisect_left(sessions, window_start)
    if start_index == len(sessions):
        return WindowReplay(curve={})
    # Normally window_start itself; the first bar session after it when the
    # bars lack live's first session.
    anchor = sessions[start_index]
    warm_index = start_index - warmup_sessions
    if warm_index < lookback_sessions:
        raise ShadowWarmupTruncated(
            f"the bars hold {start_index} session(s) before the window opening "
            f"{window_start}; a {warmup_sessions}-session warm-up needs "
            f"{warmup_sessions + lookback_sessions} "
            f"({lookback_sessions} of indicator history before it). Fetch at "
            "least shadow_fetch_years() of bars."
        )
    warm_from = sessions[warm_index]

    # Only flows strictly after the ANCHOR are replayed. The anchor is
    # window_start unless the bars lack that session; a flow dated in
    # (window_start, anchor] then has no replayed session before the anchor's
    # close to land on, and the scale it needs is learned at that close. It is
    # treated as part of the opening level instead: the monitor intersects the
    # two curves, so the first session it compares is the anchor, where live's
    # value already contains the flow and the shadow's is the normalised
    # opening level. Every return it grades runs from the anchor on, and a
    # level shift before the first compared session moves none of them. What
    # the shadow does not reproduce is the flow's share of idle cash in that
    # one case — a sizing approximation, bounded to bars missing live's first
    # window session. Replaying it instead raised in the runner and, before
    # build_shadow_replays isolated sleeves, blanked every sleeve's feed.
    inside = {
        session: float(amount)
        for session, amount in (cash_flows or {}).items()
        if session > anchor and amount
    }
    runner = BacktestRunner(
        SimulatedExecutor(cost_model or CostModel()),
        initial_capital=seed_nav,
        whole_shares=whole_shares,
        cash_buffer_bps=cash_buffer_bps,
    )
    if inside:
        # Live's flows, sized to this book on the window's first session:
        # scaling by 1/k keeps each one the same fraction of the book.
        result = runner.run(
            bars_by_ticker,
            _gated(signals_fn, warm_from),
            risk_engine,
            trade_start_date=warm_from,
            cash_flows=inside,
            cash_flow_basis=(anchor, seed_nav),
        )
    else:
        result = runner.run(
            bars_by_ticker,
            _gated(signals_fn, warm_from),
            risk_engine,
            trade_start_date=warm_from,
        )

    # ``portfolio_values`` carries pre-day-0 capital at index 0, so element
    # i+1 is the end-of-day value for ``dates[i]`` — the same alignment
    # ``load_backtest_equity_series`` applies to the artifact.
    end_of_day = dict(zip(result.dates, result.portfolio_values[1:]))
    nav_at_start = end_of_day[anchor]
    if nav_at_start <= 0:
        raise ValueError(
            f"the warm-up ended with NAV {nav_at_start} on {anchor}; a curve "
            "cannot be normalised to live from a non-positive value"
        )
    scale = seed_nav / nav_at_start
    curve = {
        session: value * scale
        for session, value in end_of_day.items()
        if session >= window_start
    }
    invested = _invested_at(result, bars_by_ticker, anchor, nav_at_start)
    if not result.cash_flows_applied:
        return WindowReplay(curve, warm_from, invested)
    running = 0.0
    included: dict[date, float] = {}
    applied = sorted(result.cash_flows_applied.items())
    for session in sorted(curve):
        while applied and applied[0][0] <= session:
            running += applied.pop(0)[1]
        included[session] = running * scale
    return WindowReplay(flow_adjust(curve, included), warm_from, invested)


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
    warmup_sessions: int = SHADOW_WARMUP_SESSIONS,
    lookback_sessions: int = SHADOW_LOOKBACK_SESSIONS,
) -> dict[date, float]:
    """Return ``{session: equity}`` for the model's own run over the window.

    Args:
        bars_by_ticker: Bars for this sleeve's universe, history included:
            at least ``lookback_sessions + warmup_sessions`` sessions before
            ``window_start``.
        signals_fn: The sleeve's signal function, built exactly as the live path
            builds it, fresh: it is called once per session per ticker from
            ``warm_from`` on, never before it.
        risk_engine: The sleeve's risk engine, same.
        seed_nav: Live's NAV on ``window_start``. The replay starts the
            warm-up with it, and the returned curve is normalised to it on
            ``window_start`` so the two curves start level.
        window_start: First session of the comparison window.
        cost_model: Fill costs. Defaults to the repo's standard model, which is
            what the live sleeves are charged against.
        whole_shares: Truncate fractional sizing, as live execution does.
        cash_buffer_bps: Live's ``currency.sleeve_cash_buffer_bps``. Entries
            are funded from the replay's own cash exactly as the paper run
            funds them (KAN-111; see ``BacktestRunner``).
        cash_flows: KAN-113. ``{session: signed USD}`` — the sleeve's recorded
            capital flows, keyed by the session whose value first includes
            each. Only flows strictly after the window's first bar session
            are replayed (one on or before it is already in the opening
            level), each scaled to the shadow's book (KAN-105, see the module
            docstring). When any is
            applied, the returned curve is flow-adjusted (time-weighted,
            anchored at its last session), the basis the monitor grades live
            on. A withdrawal the replay's cash cannot cover is clamped by
            ``BacktestRunner`` (it cannot sell to fund it).
        warmup_sessions: KAN-105. Sessions the replay trades before the
            window. ``0`` is a clean cold start: signals gated before the
            window and trading from it.
        lookback_sessions: Indicator history required before the warm-up.

    Returns:
        Equity by session for ``window_start`` onward. Empty when no session
        falls in the window — there is nothing to date a verdict by, and the
        caller must be able to tell that from a zero.

    Raises:
        ShadowWarmupTruncated: The bars cannot hold the warm-up plus its
            indicator history.
    """
    return replay_window_detail(
        bars_by_ticker=bars_by_ticker,
        signals_fn=signals_fn,
        risk_engine=risk_engine,
        seed_nav=seed_nav,
        window_start=window_start,
        cost_model=cost_model,
        whole_shares=whole_shares,
        cash_buffer_bps=cash_buffer_bps,
        cash_flows=cash_flows,
        warmup_sessions=warmup_sessions,
        lookback_sessions=lookback_sessions,
    ).curve


def build_shadow_replays(
    *,
    portfolios: dict[str, Any],
    bars_by_ticker: dict[str, list[dict]],
    live_equity: dict[str, dict[date, float]],
    window_sessions: int,
    cost_model: CostModel | None = None,
    whole_shares: bool = False,
    cash_buffer_bps: float = DEFAULT_SLEEVE_CASH_BUFFER_BPS,
    cash_flows: Mapping[str, Mapping[date, float]] | None = None,
    warmup_sessions: int = SHADOW_WARMUP_SESSIONS,
    lookback_sessions: int = SHADOW_LOOKBACK_SESSIONS,
) -> dict[str, WindowReplay]:
    """:func:`build_shadow_series`, keeping each sleeve's seeding detail.

    **A failure is isolated to its sleeve.** One sleeve's replay raising —
    a warm-up its bars cannot hold (each sleeve's window comes from its own
    live history, so truncation need not hit every sleeve), a flow it cannot
    place, a defect in one signal function — is logged and returned as a
    :class:`WindowReplay` with an empty curve and ``error`` set, and the other
    sleeves are replayed as usual. Letting it propagate blanked all six feeds
    for one sleeve's fault (the monitor's exit 3, "nothing could be graded").
    The failure is not silent: the artifact names it per sleeve
    (``replay_failed``), and the monitor records that sleeve NO_DATA with the
    reason first, which reaches its exit-5 alert.
    """
    out: dict[str, WindowReplay] = {}

    for name, sleeve in portfolios.items():
        live = live_equity.get(name) or {}
        if not live:
            continue

        sessions = sorted(live)
        window_start = sessions[-window_sessions:][0]

        try:
            replay = replay_window_detail(
                bars_by_ticker=bars_by_ticker,
                signals_fn=sleeve.signals_fn,
                risk_engine=sleeve.risk_engine,
                seed_nav=live[window_start],
                window_start=window_start,
                cost_model=cost_model,
                whole_shares=whole_shares,
                cash_buffer_bps=cash_buffer_bps,
                cash_flows=(cash_flows or {}).get(name),
                warmup_sessions=warmup_sessions,
                lookback_sessions=lookback_sessions,
            )
        except Exception as exc:  # noqa: BLE001 — isolated, logged, named
            logger.exception("Shadow replay failed for sleeve %s", name)
            out[name] = WindowReplay(
                curve={}, error=f"{type(exc).__name__}: {exc}"
            )
            continue
        # Live is the authority on which sessions are gradeable: the replay can
        # only produce a curve for sessions its bars cover, and the monitor
        # intersects the two sides anyway.
        clipped = {d: v for d, v in replay.curve.items() if d in live}
        if clipped:
            out[name] = WindowReplay(
                clipped, replay.warm_from, replay.invested_at_start
            )

    return out


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
    warmup_sessions: int = SHADOW_WARMUP_SESSIONS,
    lookback_sessions: int = SHADOW_LOOKBACK_SESSIONS,
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
            ``equity_snapshots`` — RAW, not flow-adjusted: the curve is
            normalised to what live actually held on the window's first
            session.
        window_sessions: Comparison window length.
        cash_flows: KAN-113. Sleeve name -> ``{session: signed USD}`` of its
            recorded capital flows (``capital_flow_steps_by_sleeve`` in
            scripts/run_paper.py); replayed into the shadow on the same
            sessions (see :func:`replay_window`).
        warmup_sessions: KAN-105. Sessions each replay trades before its
            window (see :func:`replay_window`).
        lookback_sessions: Indicator history required before the warm-up.

    Returns:
        Sleeve name -> ``{session: equity}``. A sleeve with no live history is
        **absent** rather than present-and-zero: the book has never recorded it,
        so there is nothing to seed from, and a zero curve would read as a total
        loss instead of as an ungradeable sleeve.

    A sleeve whose replay failed (see :func:`build_shadow_replays`) is absent
    here; use :func:`build_shadow_replays` to see why.

    The window is derived per sleeve from *live's* sessions, never from the
    bars. Bars can run ahead of the book — a session prints but the 05:15 job
    aborted before writing a snapshot — and grading a session live has no NAV
    for would compare a real number against nothing.
    """
    replays = build_shadow_replays(
        portfolios=portfolios,
        bars_by_ticker=bars_by_ticker,
        live_equity=live_equity,
        window_sessions=window_sessions,
        cost_model=cost_model,
        whole_shares=whole_shares,
        cash_buffer_bps=cash_buffer_bps,
        cash_flows=cash_flows,
        warmup_sessions=warmup_sessions,
        lookback_sessions=lookback_sessions,
    )
    return {
        name: replay.curve for name, replay in replays.items() if replay.curve
    }
