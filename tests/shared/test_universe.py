"""Tests for shared/universe.py — the single source of truth for tickers."""
from __future__ import annotations

import json
from datetime import date

import pytest

from shared.universe import (
    ACTIVE_SLEEVES,
    UNIVERSE_REGISTRY,
    MembershipCalendar,
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
