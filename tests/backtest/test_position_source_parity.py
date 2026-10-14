"""KAN-116 AC2: the backtest and live build a sleeve's view through the same path.

Live hydrates a ``PortfolioContext`` from the ``positions`` table every run
(``PaperTradingState.build_portfolio_context``), the table that fills write
through ``_apply_fill_accounting`` and whose peaks ``update_peak_prices`` raises
at each close. A replay now hands each sleeve a ``RunnerBook`` that
``BacktestRunner`` re-binds before every session to a ``PortfolioContext`` of
its own lots.

This test replays all six production sleeves (``build_portfolios`` with no live
contexts, exactly as the rolling shadow builds them), then pushes every fill the
replay made through the live paper state, session by session, and asserts on
every session that:

* the context live would hydrate equals the one the runner bound, position by
  position and field by field (quantity, fill price, peak, entry date), and
* each sleeve's signal function, reading the live-hydrated context, emits
  exactly what the replay's did.
"""
from __future__ import annotations

from collections import defaultdict

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


@pytest.mark.parametrize("sleeve", ACTIVE_SLEEVES)
def test_the_replay_book_is_the_book_live_would_hydrate(sleeve, inputs, monkeypatch):
    bars, regime = inputs
    replay = _roster(bars, regime, monkeypatch, _RecordingBook)[sleeve]
    mirror = _roster(bars, regime, monkeypatch, RunnerBook)[sleeve]

    emitted: dict = {}

    def recording(ticker, rows):
        signal = replay.signals_fn(ticker, rows)
        emitted[(rows[-1]["date"], ticker)] = signal
        return signal

    result = BacktestRunner(
        SimulatedExecutor(CostModel()), initial_capital=replay.capital
    ).run(bars, recording, replay.risk_engine, book=replay.book)

    entries, exits = defaultdict(list), defaultdict(list)
    for t in result.trades:
        entries[t["entry_date"]].append((t["ticker"], t["quantity"], t["entry_price"]))
        exits[t["exit_date"]].append((t["ticker"], t["quantity"], t["exit_price"], t["exit_reason"]))
    for lot in result.open_positions:
        entries[lot["entry_date"]].append((lot["ticker"], lot["quantity"], lot["entry_price"]))
    # Not hollow: the sleeve both entered and exited over the replay.
    assert entries and exits, sleeve

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    state = PaperTradingState.create_new({sleeve: replay.capital}, session)

    index = {t: {b["date"]: i for i, b in enumerate(rows)} for t, rows in bars.items()}
    watched = set(UNIVERSE_REGISTRY[sleeve])
    compared_holdings = 0
    for session_date, runner_ctx in zip(result.dates, replay.book.history):
        # Today's fills, in the runner's order: exits at the open, then entries.
        for ticker, qty, price, reason in exits.get(session_date, []):
            state.record_fill(sleeve, ticker, "sell", qty, price, session_date, exit_reason=reason)
        for ticker, qty, price in entries.get(session_date, []):
            state.record_fill(sleeve, ticker, "buy", qty, price, session_date)

        live_ctx = state.build_portfolio_context(
            sleeve, pending_orders=[], sleeve_budget=replay.capital, reserved_notional=0.0
        )
        assert set(live_ctx.positions) == set(runner_ctx.positions), session_date
        for ticker, live_pos in live_ctx.positions.items():
            ours = runner_ctx.positions[ticker]
            assert live_pos.quantity == pytest.approx(ours.quantity)
            assert live_pos.avg_entry_price == pytest.approx(ours.avg_entry_price)
            assert live_pos.peak_price == pytest.approx(ours.peak_price)
            assert live_pos.entry_date == ours.entry_date
            compared_holdings += 1
        assert dict(runner_ctx.pending_orders) == {}

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

    assert compared_holdings > 0
    session.close()
