"""Tests for shared/universe.py — the single source of truth for tickers."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from shared.universe import (
    ACTIVE_SLEEVES,
    DRILL_PORTFOLIO,
    EXCLUDED_PORTFOLIO_PREFIX,
    SP500_TOP100,
    UNIVERSE_REGISTRY,
    MembershipCalendar,
    contract_conid_for,
    get_union_universe,
    is_excluded_portfolio,
    make_stock_contract,
    resolve_watchlist,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


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


class TestCommittedMembershipSnapshot:
    """KAN-23 AC1/AC2 — the committed point-in-time universe.

    The divergence monitor exits 3 (BLIND) against a survivorship-biased
    baseline, so the paper track record accruing toward the go-live gate has no
    fidelity check behind it. These pin the file that fixes that, and they are
    the reason a future snapshot regeneration cannot silently reintroduce an
    unmapped name.
    """

    SNAPSHOT = "data/universe/sp500_membership.json"

    @pytest.fixture(scope="class")
    def calendar(self):
        from scripts.run_backtest import ALWAYS_TRADABLE

        return MembershipCalendar.from_json_file(
            str(REPO_ROOT / self.SNAPSHOT), always=ALWAYS_TRADABLE
        )

    def test_the_snapshot_is_committed_and_loads(self, calendar):
        assert calendar.all_tickers()

    def test_it_covers_the_full_ten_year_backtest_window(self, calendar):
        """AC1. ``run_backtest.py --years 10`` starts at ``today - 10*365``;
        MembershipCalendar makes *nothing* tradable before its first snapshot,
        so a file that starts late silently produces an empty early window."""
        today = date.today()
        start = today - timedelta(days=10 * 365)
        assert calendar.first_snapshot_date <= start, (
            f"snapshot starts {calendar.first_snapshot_date}, after the 10-year "
            f"window opens at {start} — regenerate with an earlier --start"
        )
        # The tail matters as much: membership frozen months before today means
        # recent index changes are invisible to the baseline.
        assert calendar.last_snapshot_date >= today - timedelta(days=120), (
            f"newest snapshot is {calendar.last_snapshot_date}; regenerate with "
            "scripts/ops/build_membership_snapshot.py"
        )

    def test_every_snapshot_ticker_resolves_to_a_real_sector(self, calendar):
        """AC2 — the guard that makes this permanent.

        Historical members that resolve to "Unknown" all land in one
        pseudo-sector; once it crosses ``sector_concentration_pct`` the risk
        engine rejects every entry in every unmapped name (the 2026-08-07
        freeze). Regenerating the snapshot regenerates
        shared/historical_sectors.py alongside it, and this test fails if
        someone updates one without the other.
        """
        from shared.universe import lookup_sector

        unmapped = sorted(
            t for t in calendar.all_tickers() if lookup_sector(t) == "Unknown"
        )
        assert unmapped == [], (
            f"{len(unmapped)} snapshot tickers have no sector: {unmapped[:20]}. "
            "Re-run scripts/ops/build_membership_snapshot.py to regenerate "
            "shared/historical_sectors.py from the same revisions."
        )

    def test_it_carries_the_provenance_needed_to_audit_it(self):
        """A membership file with no stated source cannot be checked against
        the index, and this one feeds gate evidence."""
        payload = json.loads((REPO_ROOT / self.SNAPSHOT).read_text())
        assert "Wikipedia" in payload["source"]
        assert payload["generator"].endswith("build_membership_snapshot.py")
        # Every snapshot names the exact revision it was read from.
        assert set(payload["revisions"]) == set(payload["snapshots"])
        for entry in payload["revisions"].values():
            assert isinstance(entry["revid"], int)

    def test_delisted_members_are_present_not_just_survivors(self, calendar):
        """The whole point of a point-in-time universe: names that left the
        index are still in ``all_tickers()`` so their bars get fetched."""
        tickers = set(calendar.all_tickers())
        survivors = set(SP500_TOP100)
        assert len(tickers - survivors) > 300, (
            "a point-in-time file over 10 years should carry hundreds of names "
            "that are no longer members"
        )

    def test_the_curated_map_wins_over_the_generated_one(self):
        """Precedence, asserted: nothing generated from Wikipedia can change
        how a currently-traded name is bucketed by the live risk engine."""
        from shared.historical_sectors import HISTORICAL_SECTOR_MAP
        from shared.universe import SECTOR_MAP

        assert set(HISTORICAL_SECTOR_MAP) & set(SECTOR_MAP) == set()

    def test_no_ticker_carries_a_separator_ib_cannot_resolve(self, calendar):
        """One spelling per company, or the same name reads as two.

        Wikipedia wrote Berkshire as BRK.B, BRK-B and BRK B at different points
        in this window. Any spelling that survives normalisation as a second
        symbol produces a fabricated index removal + re-addition (the backtest
        liquidates at the next open with exit_reason: universe_removal) and is
        unpriceable at IB, so it also inflates the coverage exclusions.
        """
        bad = sorted(
            t for t in calendar.all_tickers()
            if not all(c.isalpha() or c == " " for c in t)
        )
        assert bad == [], (
            f"tickers with an unnormalised separator: {bad}. Fix "
            "normalize_symbol in scripts/ops/build_membership_snapshot.py and "
            "regenerate."
        )

    def test_the_class_share_spelling_matches_the_static_universe(self, calendar):
        tickers = set(calendar.all_tickers())
        assert "BRK B" in tickers
        assert "BRK-B" not in tickers and "BRK.B" not in tickers

    def test_the_generated_module_matches_the_snapshot_it_came_from(self):
        """I5 — the pair must be regenerated together.

        Without this, editing one file by hand or regenerating only one passes
        CI, because the AC2 test asks "does every ticker have *a* sector" and
        both files are produced by the same parse: any ticker the parser
        invents also gets a sector, so that test cannot fail on a parse defect.
        This one compares the two artifacts against each other instead.
        """
        from shared.historical_sectors import HISTORICAL_SECTOR_MAP
        from shared.universe import SECTOR_MAP

        payload = json.loads((REPO_ROOT / self.SNAPSHOT).read_text())
        expected = {
            t: s for t, s in payload["sectors"].items() if t not in SECTOR_MAP
        }
        assert HISTORICAL_SECTOR_MAP == expected, (
            "shared/historical_sectors.py is out of sync with "
            f"{self.SNAPSHOT}; re-run scripts/ops/build_membership_snapshot.py"
        )

    def test_every_snapshot_ticker_has_a_sector_recorded_in_the_envelope(self):
        payload = json.loads((REPO_ROOT / self.SNAPSHOT).read_text())
        in_snapshots = {t for members in payload["snapshots"].values() for t in members}
        assert in_snapshots <= set(payload["sectors"])

    def test_conid_pinned_names_use_the_repo_spelling(self, calendar):
        """C2 — a rename must not smuggle a name past CONTRACT_CONID_OVERRIDES.

        MMC and FI are pinned by conId because the IB gateway cannot resolve
        them by symbol (2026-08-09 stale-contract incident). Wikipedia carries
        the post-rename spellings MRSH and FISV, which have no override — so
        without aliasing, the two names the override exists to rescue are
        exactly the two it would miss.
        """
        from shared.universe import CONTRACT_CONID_OVERRIDES

        tickers = set(calendar.all_tickers())
        for pinned in CONTRACT_CONID_OVERRIDES:
            assert pinned in tickers, f"{pinned} missing from the PIT universe"
        for alias in ("MRSH", "FISV"):
            assert alias not in tickers, (
                f"{alias} is an un-aliased rename of a conId-pinned name; add "
                "it to TICKER_ALIASES and regenerate"
            )

    def test_a_conid_pinned_name_is_contiguous_across_its_rename(self, calendar):
        """The rename must read as one continuous membership, not a removal.

        A gap would make the backtest liquidate at the next open with
        exit_reason: universe_removal and book a fabricated round-trip.
        """
        payload = json.loads((REPO_ROOT / self.SNAPSHOT).read_text())
        days = list(payload["snapshots"])
        present = [d for d in days if "MMC" in payload["snapshots"][d]]
        first, last = days.index(present[0]), days.index(present[-1])
        assert len(present) == last - first + 1, (
            "MMC's membership is not contiguous — the MRSH rename is being "
            "read as an index removal and re-addition"
        )


class TestSectorPrecedence:
    def test_the_curated_map_is_consulted_before_the_generated_one(self, monkeypatch):
        """Precedence for real, not just key-disjointness.

        The disjointness test would still pass if lookup_sector consulted
        HISTORICAL_SECTOR_MAP first; this fails in that case. It matters
        because the curated label decides how a currently-held name is bucketed
        by the live sector-concentration limit.
        """
        import shared.universe as u

        monkeypatch.setitem(u.SECTOR_MAP, "ZZZDUP", "Energy")
        monkeypatch.setitem(u.HISTORICAL_SECTOR_MAP, "ZZZDUP", "Utilities")
        assert u.lookup_sector("ZZZDUP") == "Energy"

    def test_the_generated_map_still_covers_names_the_curated_one_lacks(self, monkeypatch):
        import shared.universe as u

        monkeypatch.setitem(u.HISTORICAL_SECTOR_MAP, "ZZZONLY", "Utilities")
        assert u.lookup_sector("ZZZONLY") == "Utilities"
