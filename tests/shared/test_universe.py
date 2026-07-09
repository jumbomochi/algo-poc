"""Tests for shared/universe.py — the single source of truth for tickers."""
from __future__ import annotations

import pytest

from shared.universe import (
    ACTIVE_SLEEVES,
    UNIVERSE_REGISTRY,
    get_union_universe,
    resolve_watchlist,
)


class TestRegistry:
    def test_every_active_sleeve_has_a_universe(self):
        for sleeve in ACTIVE_SLEEVES:
            assert sleeve in UNIVERSE_REGISTRY
            assert len(UNIVERSE_REGISTRY[sleeve]) > 0

    def test_active_sleeves_matches_run_paper_allocations(self):
        """The sleeves list must agree with run_paper.py's capital dict."""
        from scripts.run_paper import CAPITAL_ALLOCATIONS

        assert set(ACTIVE_SLEEVES) == set(CAPITAL_ALLOCATIONS.keys())

    def test_run_backtest_reexports_are_the_same_objects(self):
        """run_backtest.py must re-export shared.universe, not fork it."""
        from scripts import run_backtest
        import shared.universe as u

        assert run_backtest.UNIVERSE_REGISTRY is u.UNIVERSE_REGISTRY
        assert run_backtest.get_union_universe is u.get_union_universe


class TestUnionUniverse:
    def test_dedupes_across_sleeves(self):
        union = get_union_universe(ACTIVE_SLEEVES)
        assert len(union) == len(set(union))

    def test_active_union_is_the_140_ticker_universe(self):
        # The known size of the 6-sleeve union (matches the backtest fetch).
        assert len(get_union_universe(ACTIVE_SLEEVES)) == 140


class TestResolveWatchlist:
    def test_sleeves_source_returns_active_union(self):
        assert resolve_watchlist("sleeves", []) == get_union_universe(ACTIVE_SLEEVES)

    def test_sp500_source(self):
        result = resolve_watchlist("sp500", [])
        assert "AAPL" in result and "HUM" in result
        assert len(result) == 99  # SP500_TOP100 actual length (50 + 49)

    def test_custom_source_only_custom(self):
        assert resolve_watchlist("custom", ["FOO", "BAR"]) == ["FOO", "BAR"]

    def test_custom_tickers_additive_and_deduped(self):
        result = resolve_watchlist("sleeves", ["AAPL", "ZZZTEST"])
        assert result.count("AAPL") == 1
        assert result[-1] == "ZZZTEST"

    def test_unknown_source_raises(self):
        with pytest.raises(ValueError, match="watchlist_source"):
            resolve_watchlist("sp5000", [])
