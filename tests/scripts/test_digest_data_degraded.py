"""KAN-109 in the weekly evidence digest.

Two lines. "📉 DATA-DEGRADED" says which sleeves cannot enter right now because
their fundamentals/earnings cache is missing or stale — loud, because the
symptom is silence. "◻️ DATA GAP (on record)" names the registered periods the
week overlaps, so the week's verdicts for those sleeves are not read as
strategy results. Both are absent on a clean week.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from scripts.ops.evidence_digest import (
    DigestSnapshot,
    Sources,
    collect_snapshot,
    data_degraded_source,
    render_digest,
)
from shared.data_cache import write_cache_document
from shared.data_gaps import data_gaps_in

AS_OF = date(2026, 10, 2)
WINDOW_START = date(2026, 9, 25)


def _snapshot(**overrides) -> DigestSnapshot:
    defaults = dict(
        as_of=AS_OF, window_start=WINDOW_START, epoch=None, blind=None,
        sleeves=[], equity=None, dlq={}, alerts=None, drills_due=[],
    )
    return DigestSnapshot(**{**defaults, **overrides})


def test_degraded_sleeves_are_named_with_their_reason():
    body = render_digest(_snapshot(data_degraded={
        "quality_value": "fundamentals cache MISSING: /x/fundamentals.json does not exist",
        "earnings_drift": "earnings cache STALE: fetch time unknown",
    }))
    [line] = [ln for ln in body.splitlines() if ln.startswith("📉")]
    assert line.startswith("📉 DATA-DEGRADED — no new entries from: earnings_drift (")
    assert "quality_value (fundamentals cache MISSING" in line


def test_a_long_reason_is_cut_for_telegram():
    body = render_digest(_snapshot(data_degraded={"quality_value": "x" * 500}))
    assert "x" * 89 + "…" in body and "x" * 91 not in body


def test_registered_gaps_overlapping_the_week_are_named():
    gaps = data_gaps_in(WINDOW_START, AS_OF)
    body = render_digest(_snapshot(data_gaps=gaps))
    [line] = [ln for ln in body.splitlines() if ln.startswith("◻️ DATA GAP")]
    assert "quality_value empty 2026-09-23..open" in line
    assert "earnings_drift empty 2026-09-23..open" in line
    assert "KAN-109" in line


def test_a_clean_week_says_nothing_about_data():
    body = render_digest(_snapshot(data_degraded={}, data_gaps=[]))
    assert "DATA" not in body


def test_the_degraded_line_ranks_above_the_epoch_clock():
    body = render_digest(_snapshot(data_degraded={"quality_value": "MISSING"}))
    lines = body.splitlines()
    assert lines.index(next(ln for ln in lines if ln.startswith("📉"))) < lines.index(
        "No epoch running"
    )


def _sources(**extra) -> Sources:
    return Sources(
        epoch=lambda: None, blind=lambda: None, sleeves=lambda: [],
        equity=lambda: None, dlq=lambda: {}, alerts=lambda: None,
        drills=lambda: [], **extra,
    )


def test_collect_reads_both_sources():
    snapshot = collect_snapshot(
        _sources(
            data_degraded=lambda: {"earnings_drift": "STALE"},
            data_gaps=lambda: data_gaps_in(WINDOW_START, AS_OF),
        ),
        as_of=AS_OF, window_start=WINDOW_START,
    )
    assert snapshot.data_degraded == {"earnings_drift": "STALE"}
    assert {g.sleeve for g in snapshot.data_gaps} == {"quality_value", "earnings_drift"}


def test_a_failing_cache_check_is_a_missing_source_not_a_lost_digest():
    def boom():
        raise OSError("cache dir unreadable")

    snapshot = collect_snapshot(
        _sources(data_degraded=boom), as_of=AS_OF, window_start=WINDOW_START
    )
    assert snapshot.data_degraded is None
    assert any(m.startswith("data (OSError") for m in snapshot.missing)


def test_the_real_source_judges_the_cache_files(tmp_path):
    now = datetime(2026, 10, 5, tzinfo=timezone.utc)
    read = data_degraded_source(now=lambda: now, cache_dir=str(tmp_path))
    assert set(read()) == {"quality_value", "earnings_drift"}

    write_cache_document(tmp_path / "fundamentals.json",
                         {"AAA": [{"report_date": "2026-06-30"}]}, fetched_at=now)
    write_cache_document(tmp_path / "earnings.json",
                         {"AAA": [{"earnings_date": "2026-07-20", "surprise_pct": 6.0}]},
                         fetched_at=now - timedelta(hours=1))
    assert read() == {}
