"""KAN-109's data-gap register: the decision, its dates, and how they were derived.

Modelled on ``tests/shared/test_absent_sessions.py``. The register is a claim
about the past, so the tests pin the claim (which sleeves, which periods, why)
and re-derive each date from the facts it rests on — the 2026-03-26 caches'
contents are encoded here as the shapes the derivation needs, because CI has
no copy of the files themselves.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from scripts.fetch_fundamentals import DEFAULT_FILING_LAG_DAYS, build_fundamentals_lookup
from shared.data_cache import assess_fundamentals, read_cache_document, write_cache_document
from shared.data_gaps import (
    BACKTEST_COVERAGE,
    DECISION,
    EMPTY,
    KNOWN_DATA_GAPS,
    STALE,
    data_gaps_in,
    describe_gaps,
)
from shared.market_calendar import MarketCalendar
from shared.session_dating import last_session_before

#: The deploy clone's cut-over (KAN-72): 2026-09-24 02:08 SGT. The 04:15 run
#: that morning was the first from the clone.
FIRST_CLONE_RUN = date(2026, 9, 24)


def _gap(sleeve: str, kind: str):
    [gap] = [g for g in KNOWN_DATA_GAPS if g.sleeve == sleeve and g.kind == kind]
    return gap


def test_the_register_covers_exactly_the_two_cache_driven_sleeves():
    assert {g.sleeve for g in KNOWN_DATA_GAPS} == {"quality_value", "earnings_drift"}
    assert {(g.sleeve, g.cache) for g in KNOWN_DATA_GAPS} == {
        ("quality_value", "fundamentals"), ("earnings_drift", "earnings"),
    }


def test_the_empty_period_starts_with_the_first_run_from_the_clone():
    """KAN-103 convention: the US session that run valued, not its SGT date."""
    first_empty = last_session_before(FIRST_CLONE_RUN)
    assert first_empty == date(2026, 9, 23)
    for sleeve in ("quality_value", "earnings_drift"):
        empty = _gap(sleeve, EMPTY)
        assert empty.start == first_empty
        assert empty.end is None, "open until fresh caches ship (KAN-110)"
        assert _gap(sleeve, STALE).end == date(2026, 9, 22)


def test_fundamentals_went_stale_when_the_next_quarter_became_available():
    """82 of 97 tickers' latest period was 2025-12-31. The next quarter ends
    2026-03-31 and the lookup makes it available 45 days later."""
    assert date(2026, 3, 31) + timedelta(days=DEFAULT_FILING_LAG_DAYS) == date(2026, 5, 15)
    assert _gap("quality_value", STALE).start == date(2026, 5, 15)


def test_the_kan_109_check_would_have_fired_on_the_stale_fundamentals(tmp_path):
    """The shape of the 2026-03-26 cache — 82 tickers at 2025-12-31, 12 at
    2026-01-31, 2 at 2026-02-28, 1 at 2025-11-30 — trips the period check
    from 2026-05-18 (137 days past 2025-12-31 is a Sunday) and not before."""
    periods = (["2025-12-31"] * 82 + ["2026-01-31"] * 12 + ["2026-02-28"] * 2
               + ["2025-11-30"])
    rows = {f"T{i}": [{"report_date": p}] for i, p in enumerate(periods)}
    path = tmp_path / "fundamentals.json"
    write_cache_document(path, rows, fetched_at=datetime(2026, 3, 26, tzinfo=timezone.utc))
    doc = read_cache_document(path)

    def stale_on(day: date) -> bool:
        moment = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
        return not assess_fundamentals(doc, as_of=moment, max_fetch_age_days=1e9).fresh

    assert not stale_on(date(2026, 5, 15))
    assert stale_on(date(2026, 5, 18))


def test_earnings_went_stale_at_the_first_announcement_without_a_reported_eps():
    assert _gap("earnings_drift", STALE).start == date(2026, 4, 10)
    assert "2026-03-19" in _gap("earnings_drift", STALE).cause


def test_every_boundary_is_a_real_nyse_session():
    calendar = MarketCalendar()
    for gap in KNOWN_DATA_GAPS:
        assert calendar.is_trading_day(gap.start), gap
        if gap.end is not None:
            assert calendar.is_trading_day(gap.end), gap
            assert gap.end >= gap.start


def test_each_sleeve_s_periods_are_contiguous_and_do_not_overlap():
    calendar = MarketCalendar()
    for sleeve in ("quality_value", "earnings_drift"):
        stale, empty = _gap(sleeve, STALE), _gap(sleeve, EMPTY)
        between = calendar.trading_sessions(stale.end + timedelta(days=1),
                                            empty.start - timedelta(days=1))
        assert between == [], (sleeve, between)


def test_every_entry_carries_a_cause_and_a_reference():
    for gap in KNOWN_DATA_GAPS:
        assert gap.cause.strip() and gap.reference == "KAN-109"


def test_the_decision_names_every_date_and_the_backtest_consequence():
    for text in ("2026-05-15", "2026-04-10", "2026-09-23", "2026-09-24",
                 "2026-03-26", "2024-08-14", "KAN-33", "not rewritten", "36"):
        assert text in DECISION or text.lower() in DECISION.lower(), text
    assert "not changed" in DECISION


def test_backtest_coverage_follows_from_the_cache_contents():
    by_sleeve = {c.sleeve: c for c in BACKTEST_COVERAGE}
    qv = by_sleeve["quality_value"]
    lag = timedelta(days=DEFAULT_FILING_LAG_DAYS)
    assert qv.first_available == date(2024, 6, 30) + lag
    assert qv.fully_covered_from == date(2025, 1, 31) + lag
    assert qv.last_data == date(2026, 2, 28)

    ed = by_sleeve["earnings_drift"]
    assert ed.first_available.year == 2020
    assert ed.last_data == date(2026, 3, 19)


def test_a_quality_value_lookup_on_that_coverage_has_nothing_before_2024_08_14():
    """What 'no fundamentals before mid-2024' means for a 10-year backtest."""
    lookup = build_fundamentals_lookup({"AAA": [{"report_date": "2024-06-30", "roe": 0.2}]})
    assert lookup("AAA", date(2024, 8, 13)) is None
    assert lookup("AAA", date(2024, 8, 14)) is not None


def test_data_gaps_in_is_range_and_sleeve_bounded():
    assert data_gaps_in(date(2026, 3, 1), date(2026, 4, 9)) == []
    assert [g.sleeve for g in data_gaps_in(date(2026, 4, 1), date(2026, 4, 30))] == [
        "earnings_drift"
    ]
    october = data_gaps_in(date(2026, 10, 1), date(2026, 10, 8))
    assert {(g.sleeve, g.kind) for g in october} == {
        ("quality_value", EMPTY), ("earnings_drift", EMPTY),
    }
    assert data_gaps_in(date(2026, 10, 1), date(2026, 10, 8), sleeves={"momentum"}) == []


def test_describe_names_sleeve_period_and_register():
    text = describe_gaps(data_gaps_in(date(2026, 10, 1), date(2026, 10, 8),
                                      sleeves={"quality_value"}))
    assert "quality_value EMPTY 2026-09-23..open" in text
    assert "shared/data_gaps.py" in text


def test_the_register_is_sorted():
    keys = [(g.sleeve, g.start) for g in KNOWN_DATA_GAPS]
    assert keys == sorted(keys)


def test_the_register_writes_nothing():
    """Recorded, not rewritten: nothing here constructs a row."""
    import pathlib

    source = pathlib.Path(__file__).resolve().parents[2] / "shared" / "data_gaps.py"
    text = source.read_text()
    for writer in ("EquitySnapshot(", "DivergenceDaily(", "session.add", ".commit("):
        assert writer not in text
    json.dumps([g.describe() for g in KNOWN_DATA_GAPS])
