"""KAN-23: the point-in-time membership snapshot builder.

The network fetch is a thin shell around four pure functions; these test the
pure parts, because a parser that silently returns the wrong table is exactly
how survivorship bias creeps back in without anyone noticing.
"""
from __future__ import annotations

from datetime import date

import pytest

from scripts.ops.build_membership_snapshot import (
    GICS_TO_REPO_SECTOR,
    build_envelope,
    normalize_symbol,
    parse_constituents,
    render_sector_module,
    repo_sector,
    sector_conflicts,
    snapshot_dates,
)


# ---------------------------------------------------------------------------
# Symbol normalisation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("wiki", "repo"),
    [
        ("MMM", "MMM"),
        # Wikipedia writes class shares with a dot; every universe list in this
        # repo writes them with a space ("BRK B" in SP500_TOP50).
        ("BRK.B", "BRK B"),
        ("BF.B", "BF B"),
        # Wikipedia used the hyphen spelling from 2015-07 to 2017-04. Left
        # unnormalised it reads as Berkshire *leaving* the index in 2015 and
        # rejoining in 2017 — a fabricated universe_removal round-trip in the
        # baseline, and two years of the name unpriceable at IB.
        ("BRK-B", "BRK B"),
        ("BF-B", "BF B"),
        ("  AAPL  ", "AAPL"),
        # Non-breaking space and soft hyphen leak out of wikitext regularly.
        ("GOOG ", "GOOG"),
    ],
)
def test_normalize_symbol(wiki, repo):
    assert normalize_symbol(wiki) == repo


# ---------------------------------------------------------------------------
# Table selection + parsing
# ---------------------------------------------------------------------------

# Two tables, in the order Wikipedia renders them: constituents first, then the
# component-changes table. The changes table also has a "Ticker" column, so a
# parser that just takes the first table with a symbol-ish header would still
# be right by luck — but one that takes the *last* would be silently wrong.
_MODERN_PAGE = """
<table class="wikitable sortable" id="constituents">
<tr><th>Symbol</th><th>Security</th><th>GICS Sector</th><th>GICS Sub-Industry</th></tr>
<tr><td><a href="/wiki/MMM">MMM</a></td><td>3M</td><td>Industrials</td><td>Conglomerates</td></tr>
<tr><td><a href="/wiki/BRK">BRK.B</a></td><td>Berkshire</td><td>Financials</td><td>Insurance</td></tr>
</table>
<table class="wikitable sortable">
<tr><th>Date</th><th>Added</th><th>Removed</th><th>Reason</th></tr>
<tr><th>Ticker</th><th>Security</th><th>Ticker</th><th>Security</th></tr>
<tr><td>2020-01-01</td><td>NEW</td><td>NewCo</td><td>OLD</td><td>OldCo</td></tr>
</table>
"""

# The 2015-era header wording. Same table, different column names — a parser
# pinned to the literal string "Symbol" reads nothing here and the whole
# pre-2018 half of the window comes back empty.
_LEGACY_PAGE = """
<table class="wikitable sortable">
<tr><th>Ticker symbol</th><th>Security</th><th>SEC filings</th><th>GICS Sector</th></tr>
<tr><td>MMM</td><td>3M Company</td><td>reports</td><td>Industrials</td></tr>
<tr><td>ETFC</td><td>E*Trade</td><td>reports</td><td>Financials</td></tr>
</table>
"""


def test_parse_constituents_reads_symbol_and_sector():
    assert parse_constituents(_MODERN_PAGE) == {
        "MMM": "Industrials",
        "BRK B": "Financials",
    }


def test_parse_constituents_handles_the_legacy_header_wording():
    """2015 revisions say "Ticker symbol", not "Symbol"."""
    assert parse_constituents(_LEGACY_PAGE) == {
        "MMM": "Industrials",
        "ETFC": "Financials",
    }


def test_parse_constituents_ignores_the_component_changes_table():
    parsed = parse_constituents(_MODERN_PAGE)
    assert "NEW" not in parsed and "OLD" not in parsed


def test_parse_constituents_rejects_a_page_with_no_constituents_table():
    """A silent empty parse would write a snapshot with no members, and
    MembershipCalendar makes *nothing* tradable on those dates. Fail instead."""
    with pytest.raises(ValueError, match="no constituents table"):
        parse_constituents("<p>Redirected somewhere else.</p>")


def test_parse_constituents_rejects_an_implausibly_small_table():
    tiny = _MODERN_PAGE.replace(
        "<tr><td><a href=\"/wiki/BRK\">BRK.B</a></td><td>Berkshire</td>"
        "<td>Financials</td><td>Insurance</td></tr>",
        "",
    )
    with pytest.raises(ValueError, match="only 1 constituents"):
        parse_constituents(tiny, minimum_rows=400)


# ---------------------------------------------------------------------------
# GICS -> repo sector labels
# ---------------------------------------------------------------------------

def test_repo_sector_translates_gics_wording_to_the_repo_labels():
    # SECTOR_MAP says "Healthcare"/"Technology"; GICS says "Health Care"/
    # "Information Technology". Mixing the two splits one real sector into two
    # buckets and the concentration limit stops meaning anything.
    assert repo_sector("Health Care") == "Healthcare"
    assert repo_sector("Information Technology") == "Technology"
    assert repo_sector("Consumer Discretionary") == "Consumer Discretionary"


def test_repo_sector_refuses_an_unrecognised_gics_label():
    """Falling back to "Unknown" here is the exact freeze this story exists to
    prevent — an unmapped name must break the build, not the risk engine."""
    with pytest.raises(KeyError, match="Transportation"):
        repo_sector("Transportation")


def test_repo_sector_accepts_the_pre_2018_gics_telecom_wording():
    """The Communication Services reshuffle landed in Sep 2018, so the first
    three-and-a-half years of the window use the old label."""
    assert repo_sector("Telecommunications Services") == "Communication Services"
    assert repo_sector("Telecommunication Services") == "Communication Services"


def test_every_gics_label_maps_to_a_repo_sector_already_in_use():
    from shared.universe import SECTOR_MAP

    in_use = set(SECTOR_MAP.values())
    assert set(GICS_TO_REPO_SECTOR.values()) <= in_use


# ---------------------------------------------------------------------------
# Snapshot cadence
# ---------------------------------------------------------------------------

def test_snapshot_dates_are_quarterly_and_cover_the_window_ends():
    days = snapshot_dates(date(2015, 1, 1), date(2016, 2, 15))
    assert days[0] == date(2015, 1, 1)
    assert date(2015, 4, 1) in days
    assert date(2016, 1, 1) in days
    # The end of the window must be represented, otherwise the last quarter of
    # the backtest runs on stale membership.
    assert days[-1] == date(2016, 2, 15)


def test_snapshot_dates_do_not_duplicate_an_end_that_is_already_on_cadence():
    days = snapshot_dates(date(2015, 1, 1), date(2015, 7, 1))
    assert days == [date(2015, 1, 1), date(2015, 4, 1), date(2015, 7, 1)]


# ---------------------------------------------------------------------------
# Envelope assembly
# ---------------------------------------------------------------------------

def _obs(day, members, revid=1, ts="2015-01-01T00:00:00Z"):
    return {
        "requested": day,
        "revid": revid,
        "timestamp": ts,
        "members": dict(members),
    }


def test_build_envelope_emits_the_documented_shape():
    env = build_envelope(
        [_obs(date(2015, 1, 1), {"AAPL": "Technology"})],
        generated_at=date(2026, 8, 17),
    )
    assert env["generated_at"] == "2026-08-17"
    assert "Wikipedia" in env["source"]
    assert env["snapshots"] == {"2015-01-01": ["AAPL"]}
    # Provenance: which revision each snapshot actually came from, so the file
    # can be regenerated byte-for-byte and audited against Wikipedia.
    assert env["revisions"]["2015-01-01"]["revid"] == 1


def test_build_envelope_collapses_consecutive_identical_membership():
    """A snapshot is "effective from this date until the next", so repeating an
    unchanged constituent list adds bytes and no information."""
    env = build_envelope(
        [
            _obs(date(2015, 1, 1), {"AAPL": "Technology"}, revid=1),
            _obs(date(2015, 4, 1), {"AAPL": "Technology"}, revid=2),
            _obs(date(2015, 7, 1), {"AAPL": "Technology", "MSFT": "Technology"}, revid=3),
        ],
        generated_at=date(2026, 8, 17),
    )
    assert list(env["snapshots"]) == ["2015-01-01", "2015-07-01"]


def test_build_envelope_keeps_the_first_snapshot_even_if_later_ones_repeat_it():
    env = build_envelope(
        [_obs(date(2015, 1, 1), {"AAPL": "Technology"})],
        generated_at=date(2026, 8, 17),
    )
    assert list(env["snapshots"]) == ["2015-01-01"]


def test_build_envelope_records_the_sector_of_every_ticker_it_ever_saw():
    """Including names that left the index — those are precisely the ones
    SECTOR_MAP is missing, and the ones that would land in Unknown."""
    env = build_envelope(
        [
            _obs(date(2015, 1, 1), {"ETFC": "Financials"}, revid=1),
            _obs(date(2015, 4, 1), {"AAPL": "Technology"}, revid=2),
        ],
        generated_at=date(2026, 8, 17),
    )
    assert env["sectors"] == {"AAPL": "Technology", "ETFC": "Financials"}


def test_build_envelope_rejects_an_empty_observation_list():
    with pytest.raises(ValueError, match="at least one"):
        build_envelope([], generated_at=date(2026, 8, 17))


# ---------------------------------------------------------------------------
# Generated sector module
# ---------------------------------------------------------------------------

_RENDER_KW = dict(
    generated_at=date(2026, 8, 17), start=date(2015, 1, 1), snapshots=48
)


def test_render_sector_module_emits_importable_python():
    src = render_sector_module(
        {"ETFC": "Financials", "AAPL": "Technology"},
        {"AAPL": "Technology"},
        **_RENDER_KW,
    )
    namespace: dict = {}
    exec(compile(src, "<generated>", "exec"), namespace)
    assert namespace["HISTORICAL_SECTOR_MAP"] == {"ETFC": "Financials"}


def test_render_sector_module_omits_names_the_curated_map_already_covers():
    """Emitting both would put two labels on one ticker in two files."""
    src = render_sector_module(
        {"AAPL": "Technology"}, {"AAPL": "Technology"}, **_RENDER_KW
    )
    assert "AAPL" not in src.split('HISTORICAL_SECTOR_MAP')[1]


def test_render_sector_module_is_deterministic_and_sorted():
    sectors = {"ZTS": "Healthcare", "AAL": "Industrials", "MMM": "Industrials"}
    src = render_sector_module(sectors, {}, **_RENDER_KW)
    body = src.split("HISTORICAL_SECTOR_MAP: dict[str, str] = {")[1]
    assert body.index("'AAL'") < body.index("'MMM'") < body.index("'ZTS'")
    assert src == render_sector_module(dict(reversed(sectors.items())), {},
                                       **_RENDER_KW)


def test_sector_conflicts_reports_disagreement_without_resolving_it():
    conflicts = sector_conflicts(
        {"TGT": "Consumer Staples", "AAPL": "Technology"},
        {"TGT": "Consumer Discretionary", "AAPL": "Technology"},
    )
    assert conflicts == {"TGT": ("Consumer Discretionary", "Consumer Staples")}


def test_sector_conflicts_ignores_names_outside_the_curated_map():
    assert sector_conflicts({"ETFC": "Financials"}, {"AAPL": "Technology"}) == {}
