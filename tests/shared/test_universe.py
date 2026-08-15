"""Tests for shared/universe.py — the single source of truth for tickers."""
from __future__ import annotations

import json
from datetime import date

import pytest

from shared.universe import (
    ACTIVE_SLEEVES,
    DRILL_PORTFOLIO,
    EXCLUDED_PORTFOLIO_PREFIX,
    UNIVERSE_REGISTRY,
    MembershipCalendar,
    contract_conid_for,
    get_union_universe,
    is_excluded_portfolio,
    make_stock_contract,
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


class TestContractConIdOverride:
    def test_stale_symbols_pin_a_conid(self):
        # The gateway's contract DB carries pre-corporate-action symbols for
        # these two, so they must be pinned by the (stable) IB conId instead.
        assert contract_conid_for("MMC") == 9705
        assert contract_conid_for("FI") == 269315

    def test_normal_symbols_have_no_override(self):
        assert contract_conid_for("AAPL") is None
        assert contract_conid_for("ZZZTEST") is None

    def test_make_stock_contract_pins_conid_for_overrides(self):
        pytest.importorskip("ib_insync")
        c = make_stock_contract("MMC")
        assert c.conId == 9705
        assert c.exchange == "SMART"

    def test_make_stock_contract_uses_symbol_for_normal_tickers(self):
        pytest.importorskip("ib_insync")
        c = make_stock_contract("AAPL")
        assert c.symbol == "AAPL"
        assert c.exchange == "SMART"
        assert not c.conId  # 0 / unset until qualified


SNAPSHOTS = {
    date(2015, 1, 2): ["AAPL", "OLDCO"],
    date(2018, 6, 1): ["AAPL", "NEWCO"],
}


class TestMembershipCalendar:
    def test_members_effective_forward_from_each_snapshot(self):
        cal = MembershipCalendar(SNAPSHOTS)
        assert cal.members_as_of(date(2015, 1, 2)) == frozenset({"AAPL", "OLDCO"})
        assert cal.members_as_of(date(2017, 7, 4)) == frozenset({"AAPL", "OLDCO"})
        assert cal.members_as_of(date(2018, 6, 1)) == frozenset({"AAPL", "NEWCO"})
        assert cal.members_as_of(date(2026, 1, 1)) == frozenset({"AAPL", "NEWCO"})

    def test_nothing_is_a_member_before_the_first_snapshot(self):
        """No membership data means no tradable universe — never a back-filled guess.

        Back-filling the earliest snapshot backwards would reintroduce exactly
        the survivorship bias the calendar exists to remove.
        """
        cal = MembershipCalendar(SNAPSHOTS)
        assert cal.members_as_of(date(2014, 12, 31)) == frozenset()
        assert cal.first_snapshot_date == date(2015, 1, 2)

    def test_contains_is_point_in_time(self):
        cal = MembershipCalendar(SNAPSHOTS)
        assert cal.contains("OLDCO", date(2016, 1, 4)) is True
        # OLDCO was removed from the index in the 2018 snapshot (delisted).
        assert cal.contains("OLDCO", date(2019, 1, 4)) is False
        assert cal.contains("NEWCO", date(2016, 1, 4)) is False

    def test_all_tickers_includes_names_that_left_the_index(self):
        cal = MembershipCalendar(SNAPSHOTS)
        assert cal.all_tickers() == ["AAPL", "NEWCO", "OLDCO"]

    def test_always_members_are_outside_the_index(self):
        """ETFs the sleeves trade are not index constituents but stay tradable."""
        cal = MembershipCalendar(SNAPSHOTS, always=["XLK", "SH"])
        assert cal.contains("XLK", date(2014, 1, 2)) is True
        assert cal.contains("SH", date(2026, 1, 2)) is True
        assert "XLK" in cal.all_tickers()

    def test_iso_string_snapshot_keys_are_accepted(self):
        cal = MembershipCalendar({"2015-01-02": ["AAPL"]})
        assert cal.contains("AAPL", date(2015, 6, 1)) is True

    def test_empty_calendar_rejected(self):
        with pytest.raises(ValueError, match="at least one snapshot"):
            MembershipCalendar({})

    def test_from_json_file(self, tmp_path):
        path = tmp_path / "membership.json"
        path.write_text(json.dumps({"2015-01-02": ["AAPL", "OLDCO"]}))
        cal = MembershipCalendar.from_json_file(str(path))
        assert cal.contains("OLDCO", date(2015, 3, 1)) is True

    def test_from_json_file_accepts_snapshots_envelope(self):
        """A file may wrap the snapshots in metadata (source, generated_at)."""
        cal = MembershipCalendar.from_mapping(
            {
                "source": "test",
                "snapshots": {"2015-01-02": ["AAPL"]},
            }
        )
        assert cal.contains("AAPL", date(2015, 3, 1)) is True


class TestExcludedPortfolios:
    """Synthetic portfolios must be identifiable from one predicate.

    Drills place real paper orders and take real fills, so their rows land in
    the same tables the go-live gate reads. The exclusion contract for every
    reader lives in docs/operations/drill-evidence-isolation.md.
    """

    def test_synthetic_names_are_excluded(self):
        assert is_excluded_portfolio(DRILL_PORTFOLIO) is True
        assert is_excluded_portfolio("_aggregate") is True
        assert is_excluded_portfolio("__liquidation__") is True
        assert is_excluded_portfolio("_smoke") is True

    def test_every_graded_sleeve_is_not_excluded(self):
        for sleeve in ACTIVE_SLEEVES:
            assert is_excluded_portfolio(sleeve) is False, sleeve

    def test_drill_tag_uses_the_established_prefix(self):
        """A second mechanism would silently bypass the three existing filters."""
        assert DRILL_PORTFOLIO.startswith(EXCLUDED_PORTFOLIO_PREFIX)
