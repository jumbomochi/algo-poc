"""KAN-116: the live 05:15 roster decides exactly as it did before the change.

KAN-116 deleted each sleeve's private ``tracked`` record and made every replay
read a ``RunnerBook`` the runner refreshes from its own fills. Live already read
its positions from a hydrated ``PortfolioContext``, and must not notice: the
golden file was produced by ``kan116_live_scenario.live_signals()`` on the
parent commit (origin/main 6b93d6a), and the same scenario must produce it
byte for byte here — every buy, sell, quantity, limit and exit reason, for
every sleeve and ticker on three sessions, with holdings past a stop, fresh
and old holdings, a pending buy and a partly covering pending sell.
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.run_paper import build_portfolios
from shared.universe import ACTIVE_SLEEVES
from tests.scripts import kan116_live_scenario as scenario

GOLDEN = Path(__file__).parent / "fixtures" / "kan116_live_signals_6b93d6a.json"


def test_the_live_roster_signals_exactly_as_before_kan116() -> None:
    expected = json.loads(GOLDEN.read_text())
    actual = scenario.live_signals()

    # The scenario is not hollow: every sleeve both buys and sells in it.
    for sleeve in ACTIVE_SLEEVES:
        actions = {v["action"] for k, v in expected.items() if k.split("|")[1] == sleeve}
        assert actions == {"buy", "sell"}, (sleeve, actions)

    assert actual == expected


def test_a_live_roster_is_built_on_its_contexts_not_on_runner_books() -> None:
    """Live passes the hydrated book; no RunnerBook may be substituted for it."""
    bars = scenario.bars_by_ticker()
    contexts = scenario.contexts(bars)
    roster = build_portfolios(
        capital=100_000.0,
        bars_by_ticker=bars,
        regime_by_date={},
        fundamentals_lookup=scenario.fundamentals_lookup,
        earnings_lookup=scenario.earnings_lookup,
        portfolio_contexts=contexts,
    )
    assert set(roster) == set(ACTIVE_SLEEVES)
    assert all(config.book is None for config in roster.values())


def test_a_replay_roster_gets_one_runner_book_per_sleeve() -> None:
    from backtest.portfolio_context import RunnerBook

    roster = build_portfolios(
        capital=100_000.0,
        bars_by_ticker=scenario.bars_by_ticker(),
        regime_by_date={},
        fundamentals_lookup=scenario.fundamentals_lookup,
        earnings_lookup=scenario.earnings_lookup,
        portfolio_contexts=None,
    )
    books = [config.book for config in roster.values()]
    assert all(isinstance(book, RunnerBook) for book in books)
    assert len({id(book) for book in books}) == len(books)
