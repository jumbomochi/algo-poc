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


class TestSectorLookup:
    def test_equity_resolves_from_sector_map(self):
        from shared.universe import lookup_sector

        assert lookup_sector("AAPL") == "Technology"
        assert lookup_sector("HUM") == "Healthcare"

    def test_etf_resolves_from_etf_sectors(self):
        from shared.universe import lookup_sector

        assert lookup_sector("XLK") == "Technology"
        assert lookup_sector("HACK") == "Thematic ETF"
        assert lookup_sector("TLT") == "Bonds"

    def test_unmapped_ticker_falls_back_to_unknown(self):
        from shared.universe import lookup_sector

        assert lookup_sector("ZZZTEST") == "Unknown"

    def test_fetch_fundamentals_reexports_the_same_map(self):
        """scripts must re-export shared.universe.SECTOR_MAP, not fork it."""
        from scripts.fetch_fundamentals import SECTOR_MAP as legacy_map
        import shared.universe as u

        assert legacy_map is u.SECTOR_MAP

    def test_every_active_universe_ticker_has_a_sector(self):
        """Every ticker a sleeve can buy must resolve to a real sector —
        otherwise the account-level concentration limit degrades back into
        one big 'Unknown' bucket."""
        from shared.universe import ACTIVE_SLEEVES, get_union_universe, lookup_sector

        unmapped = [
            t for t in get_union_universe(ACTIVE_SLEEVES)
            if lookup_sector(t) == "Unknown"
        ]
        assert unmapped == []
