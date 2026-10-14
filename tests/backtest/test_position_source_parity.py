"""KAN-116 AC2: the backtest and live build a sleeve's view through the same path.

Live hydrates a ``PortfolioContext`` from the ``positions`` table every run
(``PaperTradingState.build_portfolio_context``), the table that fills write
through ``_apply_fill_accounting`` and whose peaks ``update_peak_prices`` raises
at each close, plus its still-working orders from the order ledger. A replay
now hands each sleeve a ``RunnerBook`` that ``BacktestRunner`` re-binds before
every session to a ``PortfolioContext`` of its own lots and stuck exits.

These tests replay all six production sleeves (``build_portfolios`` with no live
contexts, exactly as the rolling shadow builds them), then push every fill the
replay made — and every exit it decided but could not yet fill — through the
live paper state, session by session, and assert on every session that:

* the context live would hydrate equals the one the runner bound: every
  position field by field (quantity, fill price, peak, entry date) and every
  working order's quantity by ticker and side, and
* each sleeve's signal function, reading the live-hydrated context, emits
  exactly what the replay's did.

Each replay is required to contain the cases that make that comparison bite:
entries that expired unfilled (a sleeve that kept a phantom of one would hold
a position live does not), and — in the stuck-exit scenario — sells that stayed
working across sessions with no bar to fill against.
"""
from __future__ import annotations

from collections import defaultdict
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import scripts.run_paper as run_paper
from backtest.costs import CostModel
from backtest.portfolio_context import RunnerBook
from backtest.runner import BacktestRunner
from backtest.simulator import SimulatedExecutor
from scripts.paper_state import PaperTradingState
from shared.models.base import Base
from shared.universe import ACTIVE_SLEEVES, UNIVERSE_REGISTRY
from tests.scripts import kan116_live_scenario as scenario


class _RecordingBook(RunnerBook):
    def __init__(self) -> None:
        super().__init__()
        self.history = []

    def bind(self, context) -> None:
        super().bind(context)
        self.history.append(context)


def _roster(bars, regime, monkeypatch, book_cls):
    monkeypatch.setattr(run_paper, "RunnerBook", book_cls)
    return run_paper.build_portfolios(
        capital=100_000.0,
        bars_by_ticker=bars,
        regime_by_date=regime,
        fundamentals_lookup=scenario.fundamentals_lookup,
        earnings_lookup=scenario.earnings_lookup,
        portfolio_contexts=None,
    )


@pytest.fixture(scope="module")
def inputs():
    bars = scenario.bars_by_ticker()
    # A regime that turns every 40 sessions (and crashes once), so the
    # regime-driven paths — tail-risk rotation, bear-tightened stops, the
    # crash entry freeze — all fire inside the replay.
    sessions = sorted({b["date"] for rows in bars.values() for b in rows})
    cycle = ["bull", "neutral", "bear", "bull", "crash", "neutral"]
    return bars, {d: cycle[(i // 40) % len(cycle)] for i, d in enumerate(sessions)}


def _replay(sleeve, bars, regime, monkeypatch):
    replay = _roster(bars, regime, monkeypatch, _RecordingBook)[sleeve]
    emitted: dict = {}

    def recording(ticker, rows):
        signal = replay.signals_fn(ticker, rows)
        emitted[(rows[-1]["date"], ticker)] = signal
        return signal

    result = BacktestRunner(
        SimulatedExecutor(CostModel()), initial_capital=replay.capital
    ).run(bars, recording, replay.risk_engine, book=replay.book)
    return replay, emitted, result


def _assert_live_parity(sleeve, bars, regime, monkeypatch) -> dict:
    """Replay ``sleeve``, mirror it through the live paper state, compare every session."""
    replay, emitted, result = _replay(sleeve, bars, regime, monkeypatch)
    mirror = _roster(bars, regime, monkeypatch, RunnerBook)[sleeve]

    entries, exits = defaultdict(list), defaultdict(list)
    for t in result.trades:
        entries[t["entry_date"]].append((t["ticker"], t["quantity"], t["entry_price"]))
        exits[t["exit_date"]].append((t["ticker"], t["quantity"], t["exit_price"], t["exit_reason"]))
    for lot in result.open_positions:
        entries[lot["entry_date"]].append((lot["ticker"], lot["quantity"], lot["entry_price"]))

    # The exits live would still have working: decided on one session, not
    # filled by the next session's decisions because the name printed no bar.
    decided = sorted((d, t) for (d, t), s in emitted.items() if s and s["action"] == "sell")
    working = []  # (decided on, filled on, ticker, quantity)
    for t in result.trades:
        d0 = max((d for d, tk in decided if tk == t["ticker"] and d < t["exit_date"]), default=None)
        if d0 is not None:
            working.append((d0, t["exit_date"], t["ticker"], t["quantity"]))

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    state = PaperTradingState.create_new({sleeve: replay.capital}, session)

    index = {t: {b["date"]: i for i, b in enumerate(rows)} for t, rows in bars.items()}
    watched = set(UNIVERSE_REGISTRY[sleeve])
    stats = {"holdings": 0, "working_sells": 0}
    for session_date, runner_ctx in zip(result.dates, replay.book.history):
        # Today's fills, in the runner's order: exits at the open, then entries.
        for ticker, qty, price, reason in exits.get(session_date, []):
            state.record_fill(sleeve, ticker, "sell", qty, price, session_date, exit_reason=reason)
        for ticker, qty, price in entries.get(session_date, []):
            state.record_fill(sleeve, ticker, "buy", qty, price, session_date)

        intents = [
            SimpleNamespace(portfolio=sleeve, account_id=None, symbol=ticker, action="sell",
                            requested_quantity=qty, filled_quantity=0.0, limit_price=None,
                            recommendation_id=f"{ticker}-{d0}")
            for d0, filled_on, ticker, qty in working if d0 < session_date < filled_on
        ]
        live_ctx = state.build_portfolio_context(
            sleeve, pending_orders=intents, sleeve_budget=replay.capital, reserved_notional=0.0
        )
        assert set(live_ctx.positions) == set(runner_ctx.positions), session_date
        for ticker, live_pos in live_ctx.positions.items():
            ours = runner_ctx.positions[ticker]
            assert live_pos.quantity == pytest.approx(ours.quantity)
            assert live_pos.avg_entry_price == pytest.approx(ours.avg_entry_price)
            assert live_pos.peak_price == pytest.approx(ours.peak_price)
            assert live_pos.entry_date == ours.entry_date
            stats["holdings"] += 1
        tickers = {o.ticker for o in live_ctx.pending_orders.values()} | {
            o.ticker for o in runner_ctx.pending_orders.values()
        }
        for ticker in tickers:
            for side in ("buy", "sell"):
                assert live_ctx.pending_quantity(ticker, side) == pytest.approx(
                    runner_ctx.pending_quantity(ticker, side)
                ), (session_date, ticker, side)
        stats["working_sells"] += sum(1 for o in runner_ctx.pending_orders.values() if o.action == "sell")

        # The sleeve reading live's hydration decides what the replay decided.
        mirror.book.bind(live_ctx)
        for ticker in sorted(watched | set(live_ctx.positions)):
            i = index.get(ticker, {}).get(session_date)
            if i is None:
                continue
            assert mirror.signals_fn(ticker, bars[ticker][: i + 1]) == emitted[(session_date, ticker)], (
                session_date, ticker,
            )

        closes = {t: rows[index[t][session_date]]["close"]
                  for t, rows in bars.items() if session_date in index[t]}
        state.update_peak_prices(sleeve, closes)

    session.close()
    stats.update(result.entry_funnel)
    stats["entries"], stats["exits"] = len(entries), len(exits)
    return stats


@pytest.mark.parametrize("sleeve", ACTIVE_SLEEVES)
def test_the_replay_book_is_the_book_live_would_hydrate(sleeve, inputs, monkeypatch):
    bars, regime = inputs
    stats = _assert_live_parity(sleeve, bars, regime, monkeypatch)

    # Not hollow: the sleeve entered and exited, and at least one entry it
    # asked for expired unfilled — the case a phantom lot would corrupt.
    assert stats["entries"] and stats["exits"] and stats["holdings"], stats
    assert stats["expired"] >= 1, stats


@pytest.mark.parametrize("sleeve", ACTIVE_SLEEVES)
def test_a_stuck_exit_is_a_working_sell_in_both_books(sleeve, inputs, monkeypatch):
    """An exit decided on a name that then prints no bar stays working: the
    runner shows it as a pending sell (``_runner_context``), live as an active
    order in the ledger, and the sleeve must not ask to sell it twice."""
    bars, regime = inputs
    _, emitted, result = _replay(sleeve, bars, regime, monkeypatch)
    sessions = sorted({b["date"] for rows in bars.values() for b in rows})
    # The first exit the sleeve decided that filled at the next session's open.
    exit_trade = min(
        (t for t in result.trades if (sessions[sessions.index(t["exit_date"]) - 1], t["ticker"]) in emitted),
        key=lambda t: t["exit_date"],
    )
    gap = set(sessions[sessions.index(exit_trade["exit_date"]):][:2])
    gapped = {
        ticker: [b for b in rows if not (ticker == exit_trade["ticker"] and b["date"] in gap)]
        for ticker, rows in bars.items()
    }

    stats = _assert_live_parity(sleeve, gapped, regime, monkeypatch)

    assert stats["working_sells"] >= 2, stats  # working across both bar-less sessions
