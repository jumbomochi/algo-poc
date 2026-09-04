"""The AGGREGATE row is the whole-book verdict, so it needs the whole book.

Verified before this existed: with a stale shadow — every sleeve refused, the
monitor blind, exit 3 — the console printed

    ·   momentum                   5   ...
    ·   sector_rotation            5   ...
    ✓   AGGREGATE                  5   ...

a green tick on the bottom line, the one a reader takes as the summary, while
the same run was alerting that no drift detection was happening at all.

The cause: ``apply_shadow_comparability`` runs inside the per-sleeve loop, and
the aggregate is built afterwards by ``aggregate_reports`` with
``execution_model=None`` — which falls back to ``DEFAULT_EXECUTION_MODEL``,
whose ``is_like_for_like`` is True, so it always graded.

T7 stopped the aggregate voting on the exit code. This stops it claiming a
verdict in the report.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backtest.shadow_artifact import dump_shadow
from scripts.paper_state import PaperTradingState
from shared.models.base import Base
from shared.models.equity_snapshot import EquitySnapshot


SESSIONS = [date(2026, 5, 19) + timedelta(days=i) for i in range(7)]
GRADED = SESSIONS[-1]


def _seed(session: Session) -> dict[str, dict[date, float]]:
    PaperTradingState.create_new(
        portfolio_capitals={"momentum": 23080.0, "sector_rotation": 15380.0},
        session=session,
    )
    now = datetime.now(timezone.utc)
    mom, sec = 23080.0, 15380.0
    series: dict[str, dict[date, float]] = {"momentum": {}, "sector_rotation": {}}
    for d in SESSIONS:
        session.add(EquitySnapshot(portfolio="momentum", date=d, equity=mom,
                                   cash=mom, market_value=0.0, created_at=now))
        session.add(EquitySnapshot(portfolio="sector_rotation", date=d, equity=sec,
                                   cash=sec, market_value=0.0, created_at=now))
        series["momentum"][d] = mom
        series["sector_rotation"][d] = sec
        mom *= 1.002
        sec *= 1.001
    session.commit()
    return series


def _db(tmp_path: Path, name: str) -> tuple[str, dict]:
    url = f"sqlite:///{tmp_path / (name + '.db')}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    series = _seed(s)
    s.close()
    return url, series


def _run(monkeypatch, *, db_url, shadow, output) -> int:
    from scripts import divergence_monitor

    monkeypatch.setattr(divergence_monitor.sys, "argv", [
        "divergence_monitor.py", "--shadow", str(shadow),
        "--db-url", db_url, "--window", "5", "--output", str(output),
    ])
    return divergence_monitor.main()


def _aggregate(output: Path) -> dict:
    reports = json.loads(output.read_text())["reports"]
    return next(r for r in reports if r["portfolio"] == "AGGREGATE")


def test_a_stale_shadow_does_not_leave_a_graded_aggregate(
    tmp_path, monkeypatch
) -> None:
    """The headline: no green tick while the monitor is blind."""
    db_url, series = _db(tmp_path, "stale")
    shadow = tmp_path / "shadow.json"
    # Stale = written on an earlier DAY. The session it covers being older
    # is normal at 04:15 SGT and is not what staleness means.
    dump_shadow(shadow, series=series, shadow_id="shadow:x",
                window_sessions=5, session_date=GRADED,
                produced_on=date(2026, 5, 19))
    out = tmp_path / "div.json"

    _run(monkeypatch, db_url=db_url, shadow=shadow, output=out)

    agg = _aggregate(out)
    assert agg["status"] == "NO_DATA", agg
    assert agg["baseline_comparable"] is False, agg


def test_the_aggregate_names_why_it_could_not_be_graded(
    tmp_path, monkeypatch
) -> None:
    """A bare NO_DATA on the summary line explains nothing."""
    db_url, series = _db(tmp_path, "reason")
    shadow = tmp_path / "shadow.json"
    # Stale = written on an earlier DAY. The session it covers being older
    # is normal at 04:15 SGT and is not what staleness means.
    dump_shadow(shadow, series=series, shadow_id="shadow:x",
                window_sessions=5, session_date=GRADED,
                produced_on=date(2026, 5, 19))
    out = tmp_path / "div.json"

    _run(monkeypatch, db_url=db_url, shadow=shadow, output=out)

    notes = " ".join(_aggregate(out)["notes"])
    assert "momentum" in notes and "sector_rotation" in notes, notes


def test_the_aggregate_arithmetic_survives_the_refusal(
    tmp_path, monkeypatch
) -> None:
    """Same contract the per-sleeve rows keep: refuse to grade, still show the
    gap, or nobody can judge how far apart the curves were."""
    db_url, series = _db(tmp_path, "arith")
    shadow = tmp_path / "shadow.json"
    # Stale = written on an earlier DAY. The session it covers being older
    # is normal at 04:15 SGT and is not what staleness means.
    dump_shadow(shadow, series=series, shadow_id="shadow:x",
                window_sessions=5, session_date=GRADED,
                produced_on=date(2026, 5, 19))
    out = tmp_path / "div.json"

    _run(monkeypatch, db_url=db_url, shadow=shadow, output=out)

    agg = _aggregate(out)
    assert agg["live_return"] is not None
    assert agg["backtest_return"] is not None


def test_a_fully_graded_book_still_grades_the_aggregate(
    tmp_path, monkeypatch
) -> None:
    """No false refusals: the healthy path must keep its verdict."""
    db_url, series = _db(tmp_path, "healthy")
    shadow = tmp_path / "shadow.json"
    dump_shadow(shadow, series=series, shadow_id="shadow:x",
                window_sessions=5, session_date=GRADED,
                produced_on=date.today())
    out = tmp_path / "div.json"

    code = _run(monkeypatch, db_url=db_url, shadow=shadow, output=out)

    agg = _aggregate(out)
    assert agg["baseline_comparable"] is True, agg
    assert agg["status"] != "NO_DATA", agg
    assert code == 0


def test_one_ungraded_sleeve_ungrades_the_aggregate(
    tmp_path, monkeypatch
) -> None:
    """The partial case. AGGREGATE claims the whole book, so it needs the whole
    book — otherwise it would silently mean a different book each day and its
    figures would stop being comparable run to run."""
    db_url, series = _db(tmp_path, "partial")
    # sector_rotation drops to a single session: too little overlap to grade.
    series["sector_rotation"] = {GRADED: series["sector_rotation"][GRADED]}
    shadow = tmp_path / "shadow.json"
    dump_shadow(shadow, series=series, shadow_id="shadow:x",
                window_sessions=5, session_date=GRADED,
                produced_on=date.today())
    out = tmp_path / "div.json"

    _run(monkeypatch, db_url=db_url, shadow=shadow, output=out)

    reports = {r["portfolio"]: r for r in json.loads(out.read_text())["reports"]}
    assert reports["momentum"]["baseline_comparable"] is True
    assert reports["sector_rotation"]["baseline_comparable"] is False
    assert reports["AGGREGATE"]["baseline_comparable"] is False
    assert "sector_rotation" in " ".join(reports["AGGREGATE"]["notes"])
