"""KAN-109 on the divergence surface.

Two things the monitor now says about quality_value and earnings_drift:

1. A sleeve the 05:15 run marked data-degraded is recorded as ungraded
   (NO_DATA) with that reason first — it is never graded against a shadow it
   did not replay, and the reason reaches the exit-5 alert.
2. A window that overlaps a registered data gap (``shared/data_gaps.py``)
   says so in the sleeve's notes, without changing its status.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backtest.divergence import PortfolioDivergenceReport
from backtest.shadow_artifact import dump_shadow, load_shadow
from scripts.divergence_monitor import (
    EXIT_PARTIALLY_GRADED,
    apply_data_degraded,
    note_data_gaps,
)
from scripts.paper_state import PaperTradingState
from shared.models.base import Base
from shared.models.equity_snapshot import EquitySnapshot
from shared.models.evidence import DivergenceDaily

SESSIONS = [date(2026, 5, 19) + timedelta(days=i) for i in range(7)]
REASON = "fundamentals cache MISSING: /x/data/cache/fundamentals.json does not exist"


def _db(tmp_path: Path) -> str:
    url = f"sqlite:///{tmp_path / 'monitor.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    caps = {"momentum": 23080.0, "sector_rotation": 15380.0, "quality_value": 15380.0}
    PaperTradingState.create_new(portfolio_capitals=dict(caps), session=session)
    now = datetime.now(timezone.utc)
    for d in SESSIONS:
        for name in caps:
            session.add(EquitySnapshot(portfolio=name, date=d, session_date=d,
                                       equity=caps[name], cash=caps[name],
                                       market_value=0.0, created_at=now))
            caps[name] *= 1.001
    session.commit()
    session.close()
    return url


def _shadow(tmp_path: Path) -> Path:
    caps = {"momentum": 23080.0, "sector_rotation": 15380.0}
    series = {name: {} for name in caps}
    for d in SESSIONS:
        for name in caps:
            series[name][d] = caps[name]
            caps[name] *= 1.001
    path = tmp_path / "shadow.json"
    dump_shadow(path, series=series, shadow_id="shadow:aaaabbbbccccdddd",
                window_sessions=5, session_date=SESSIONS[-1],
                produced_on=date.today(),
                data_degraded={"quality_value": REASON})
    return path


def _run(monkeypatch, *, db_url, shadow, output) -> int:
    from scripts import divergence_monitor

    monkeypatch.setattr(divergence_monitor.sys, "argv", [
        "divergence_monitor.py", "--live-history-from", "all",
        "--shadow", str(shadow), "--db-url", db_url, "--window", "5",
        "--output", str(output),
    ])
    return divergence_monitor.main()


def test_the_artifact_round_trips_the_degraded_sleeves(tmp_path):
    assert load_shadow(_shadow(tmp_path)).data_degraded == {"quality_value": REASON}


def test_an_old_artifact_has_no_degraded_sleeves(tmp_path):
    path = _shadow(tmp_path)
    raw = json.loads(path.read_text())
    del raw["data_degraded"]
    path.write_text(json.dumps(raw))
    assert load_shadow(path).data_degraded == {}


def test_a_degraded_sleeve_is_recorded_ungraded_with_its_reason_first(tmp_path, monkeypatch):
    out = tmp_path / "divergence.json"
    db_url = _db(tmp_path)

    code = _run(monkeypatch, db_url=db_url, shadow=_shadow(tmp_path), output=out)

    reports = {r["portfolio"]: r for r in json.loads(out.read_text())["reports"]}
    qv = reports["quality_value"]
    assert qv["status"] == "NO_DATA"
    assert qv["notes"][0].startswith("data-degraded (KAN-109): " + REASON)
    # The others are graded as usual; a partial, not an outage.
    assert reports["momentum"]["status"] != "NO_DATA"
    assert code == EXIT_PARTIALLY_GRADED

    # Persisted as a recorded NO_DATA — "ran, could not judge" — not skipped.
    session = sessionmaker(bind=create_engine(db_url))()
    rows = session.scalars(
        select(DivergenceDaily).where(DivergenceDaily.sleeve == "quality_value")
    ).all()
    assert [r.status for r in rows] == ["NO_DATA"]


def _report(name: str, start: date | None, end: date | None) -> PortfolioDivergenceReport:
    return PortfolioDivergenceReport(
        portfolio=name, window_start=start, window_end=end, days_compared=5,
        live_return=0.01, backtest_return=0.01, absolute_divergence_pp=0.0,
        relative_divergence=0.0, daily_correlation=1.0, live_trades_in_window=0,
        realized_slippage_total=0.0, realized_slippage_bps=None,
        realized_commission_total=0.0, assumed_commission_total=0.0,
        status="OK",
    )


def test_apply_data_degraded_leads_with_the_reason():
    report = _report("earnings_drift", None, None)
    report.notes.append("an earlier note")
    apply_data_degraded(report, "earnings cache STALE")
    assert report.status == "NO_DATA" and report.baseline_comparable is False
    assert report.notes[0].startswith("data-degraded (KAN-109): earnings cache STALE")


def test_a_window_inside_a_registered_gap_is_named_without_regrading():
    report = _report("quality_value", date(2026, 9, 21), date(2026, 10, 2))
    note_data_gaps(report, {}, window_sessions=30)
    assert report.status == "OK", "the register classifies, it never scores"
    [note] = report.notes
    assert "quality_value STALE 2026-05-15..2026-09-22" in note
    assert "quality_value EMPTY 2026-09-23..open" in note


def test_the_window_falls_back_to_the_live_sessions():
    report = _report("earnings_drift", None, None)
    live = {date(2026, 10, 1) + timedelta(days=i): 1.0 for i in range(3)}
    note_data_gaps(report, live, window_sessions=30)
    assert any("earnings_drift EMPTY" in n for n in report.notes)


def test_a_sleeve_or_window_outside_the_register_gets_no_note():
    momentum = _report("momentum", date(2026, 9, 21), date(2026, 10, 2))
    note_data_gaps(momentum, {}, window_sessions=30)
    early = _report("quality_value", date(2026, 3, 2), date(2026, 3, 31))
    note_data_gaps(early, {}, window_sessions=30)
    assert momentum.notes == [] and early.notes == []
