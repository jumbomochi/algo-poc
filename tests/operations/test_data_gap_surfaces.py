"""KAN-109 on the gate surfaces and the daily report.

* ``epoch_progress`` (the ladder) names a registered data gap the epoch spans,
  for the epoch's sleeves, and changes no criterion over it.
* ``PostgresGateDataSource`` lists the gaps for the go-live report, and refuses
  a backtest artifact run on degraded data as gate-8 evidence.
* The 05:52 daily summary says whether the caches degrade a sleeve today —
  from the cache files, so it is right even when the paper log is not.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from scripts.ops.gate_data_source import GateDataUnavailable, PostgresGateDataSource
from scripts.ops.go_live_gate import GateResult, render_json, render_text
from scripts.ops.pipeline_report_summary import RunFacts, render_summary
from shared.evidence_store import epoch_progress
from shared.models.base import Base
from shared.models.evidence import GateEpoch

class _Calendar:
    def trading_sessions(self, start: date, end: date) -> list[date]:
        days, day = [], start
        while day <= end:
            if day.weekday() < 5:
                days.append(day)
            day += timedelta(days=1)
        return days

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()


def _epoch(db, *, sleeves, started: date) -> GateEpoch:
    # A manifest validate_manifest accepts, with this test's sleeve set.
    from tests.shared.test_evidence_store import MANIFEST

    epoch = GateEpoch(label="v2-" + "-".join(sleeves), rung=0,
                      manifest={**MANIFEST, "sleeves": sleeves},
                      started_at=datetime.combine(started, datetime.min.time(),
                                                  tzinfo=timezone.utc))
    db.add(epoch)
    db.flush()
    return epoch


def _progress(db, epoch, as_of):
    return epoch_progress(db, epoch_id=epoch.id, as_of=as_of, calendar=_Calendar(),
                          now=datetime(2026, 10, 8, 12, tzinfo=timezone.utc))


def test_an_epoch_spanning_the_gap_names_it_for_its_sleeves(session):
    epoch = _epoch(session, sleeves=["momentum", "quality_value", "earnings_drift"],
                   started=date(2026, 9, 14))
    progress = _progress(session, epoch, date(2026, 10, 2))

    [note] = [n for n in progress.blocking if "data gap on record" in n.lower()]
    assert "quality_value STALE 2026-05-15..2026-09-22" in note
    assert "quality_value EMPTY 2026-09-23..open" in note
    assert "earnings_drift EMPTY 2026-09-23..open" in note
    assert "change no criterion" in note


def test_the_note_changes_no_criterion(session):
    with_gap = _epoch(session, sleeves=["momentum", "quality_value"],
                      started=date(2026, 9, 14))
    without = _epoch(session, sleeves=["momentum"], started=date(2026, 9, 14))
    a = _progress(session, with_gap, date(2026, 10, 2))
    b = _progress(session, without, date(2026, 10, 2))
    assert a.criteria == b.criteria
    assert (a.sessions_elapsed, a.sessions_paused) == (b.sessions_elapsed, b.sessions_paused)
    assert not any("data gap" in n.lower() for n in b.blocking)


# ---------------------------------------------------------------------------
# go-live gate
# ---------------------------------------------------------------------------

NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)


def _source(session, tmp_path, **kw):
    return PostgresGateDataSource(session, output_dir=tmp_path, now=lambda: NOW, **kw)


def test_the_gate_lists_the_gaps_in_its_paper_window(session, tmp_path):
    notes = _source(session, tmp_path, paper_start=date(2026, 8, 1)).get_data_gap_notes()
    assert any(n.startswith("quality_value STALE 2026-05-15..2026-09-22") for n in notes)
    assert any(n.startswith("earnings_drift EMPTY 2026-09-23..open") for n in notes)

    before = _source(session, tmp_path, paper_start=date(2026, 3, 1))
    assert len(before.get_data_gap_notes()) == 4


def test_the_gate_report_renders_the_gaps_as_context_not_a_gate():
    results = [GateResult(name="drawdown_bound", passed=True, message="ok")]
    gaps = ["quality_value EMPTY 2026-09-23..open (fundamentals: x — KAN-109)"]
    text = render_text(results, mode="paper", evaluated_at=NOW, data_gaps=gaps)
    assert "Data gaps on record" in text and gaps[0] in text
    assert "1/1 gates pass." in text
    assert json.loads(render_json(results, mode="paper", evaluated_at=NOW,
                                  data_gaps=gaps))["data_gaps"] == gaps
    assert "Data gaps" not in render_text(results, mode="paper", evaluated_at=NOW)


def _artifact(tmp_path, config):
    (tmp_path / "backtest_multi_20261006_063000.json").write_text(json.dumps({
        "config": config,
        "aggregate": {"metrics": {"sharpe_ratio": 1.2, "max_drawdown": 0.1,
                                  "win_rate": 0.55}},
    }))


def test_a_degraded_backtest_artifact_is_not_gate_evidence(session, tmp_path):
    _artifact(tmp_path, {"data_degraded": {"quality_value": "fundamentals cache MISSING"}})
    with pytest.raises(GateDataUnavailable, match="quality_value"):
        _source(session, tmp_path).get_backtest_metrics()


def test_a_clean_backtest_artifact_still_reads(session, tmp_path):
    _artifact(tmp_path, {})
    assert _source(session, tmp_path).get_backtest_metrics()["sharpe"] == 1.2


# ---------------------------------------------------------------------------
# The daily report
# ---------------------------------------------------------------------------

FACTS = RunFacts(halt_active=False, halt_source=None, halt_reason=None,
                 fills=0, risk_rejected=0, submission_failed=0)


def test_the_daily_summary_names_degraded_sleeves():
    line = render_summary(FACTS.__class__(**{
        **FACTS.__dict__, "data_checked": True,
        "data_degraded": {"quality_value": "MISSING", "earnings_drift": "MISSING"},
    }))
    assert "📉 data-degraded (no entries): earnings_drift, quality_value" in line


def test_the_daily_summary_says_ok_or_unknown_but_never_omits_a_check_it_ran():
    ok = render_summary(FACTS.__class__(**{**FACTS.__dict__, "data_checked": True,
                                           "data_degraded": {}}))
    assert "data: ok" in ok
    unknown = render_summary(FACTS.__class__(**{**FACTS.__dict__, "data_checked": True,
                                                "data_degraded": None}))
    assert "data: unknown ⚠" in unknown


def test_unchecked_facts_render_as_before():
    assert "data" not in render_summary(FACTS)


def test_the_summary_check_reads_the_configured_cache_dir(tmp_path, monkeypatch):
    from scripts.ops import pipeline_report_summary

    monkeypatch.setenv("ALGO_DATA_CACHE_DIR", str(tmp_path))
    assert set(pipeline_report_summary._data_degraded()) == {"quality_value", "earnings_drift"}
