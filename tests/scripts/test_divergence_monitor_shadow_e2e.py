"""End to end: the monitor grades a session, instead of refusing to.

This is the whole point of the workstream. Against the pinned artifact the
monitor exits 3 every night with "no drift detection is running", because a
coverage figure measured over 2016-2020 marks the baseline not-like-for-like.
Against a shadow it grades, and it grades today's session rather than a window
frozen at the baseline's last bar.
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


def _seed(session: Session) -> None:
    PaperTradingState.create_new(
        portfolio_capitals={"momentum": 23080.0, "sector_rotation": 15380.0},
        session=session,
    )
    now = datetime.now(timezone.utc)
    mom, sec = 23080.0, 15380.0
    for d in SESSIONS:
        session.add(EquitySnapshot(portfolio="momentum", date=d, equity=mom,
                                   cash=mom, market_value=0.0, created_at=now))
        session.add(EquitySnapshot(portfolio="sector_rotation", date=d, equity=sec,
                                   cash=sec, market_value=0.0, created_at=now))
        mom *= 1.002
        sec *= 1.001
    session.commit()


def _db(tmp_path: Path, name: str) -> str:
    url = f"sqlite:///{tmp_path / (name + '.db')}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    _seed(s)
    s.close()
    return url


def _shadow(tmp_path: Path, *, session_date=GRADED, drift=1.0) -> Path:
    """A model curve that tracks live, scaled by ``drift``."""
    mom, sec = 23080.0, 15380.0
    series: dict[str, dict[date, float]] = {"momentum": {}, "sector_rotation": {}}
    for d in SESSIONS:
        series["momentum"][d] = mom
        series["sector_rotation"][d] = sec
        mom *= 1.002 * drift
        sec *= 1.001
    path = tmp_path / "shadow_20260525.json"
    dump_shadow(path, series=series, shadow_id="shadow:aaaabbbbccccdddd",
                window_sessions=5, session_date=session_date)
    return path


def _run(monkeypatch, *, db_url, shadow, output, window="5") -> int:
    from scripts import divergence_monitor

    monkeypatch.setattr(divergence_monitor.sys, "argv", [
        "divergence_monitor.py",
        "--shadow", str(shadow),
        "--db-url", db_url,
        "--window", window,
        "--output", str(output),
    ])
    return divergence_monitor.main()


def test_a_fresh_shadow_produces_a_graded_verdict(tmp_path, monkeypatch) -> None:
    """The headline. Not exit 3, and not NO_DATA."""
    out = tmp_path / "divergence.json"

    code = _run(monkeypatch, db_url=_db(tmp_path, "graded"),
                shadow=_shadow(tmp_path), output=out)

    assert code == 0
    statuses = {r["portfolio"]: r["status"]
                for r in json.loads(out.read_text())["reports"]}
    assert statuses["momentum"] != "NO_DATA", statuses
    assert statuses["sector_rotation"] != "NO_DATA", statuses


def test_a_diverging_sleeve_breaches_instead_of_being_suppressed(
    tmp_path, monkeypatch
) -> None:
    """The failure the frozen feed could not report at all."""
    out = tmp_path / "divergence.json"

    code = _run(monkeypatch, db_url=_db(tmp_path, "breach"),
                shadow=_shadow(tmp_path, drift=0.97), output=out)

    statuses = {r["portfolio"]: r["status"]
                for r in json.loads(out.read_text())["reports"]}
    assert statuses["momentum"] == "BREACH", statuses
    assert code == 1


def test_a_stale_shadow_is_refused_rather_than_graded(tmp_path, monkeypatch) -> None:
    """04:15 failed and yesterday's artifact is still on disk."""
    out = tmp_path / "divergence.json"

    _run(monkeypatch, db_url=_db(tmp_path, "stale"),
         shadow=_shadow(tmp_path, session_date=SESSIONS[0]), output=out)

    reports = json.loads(out.read_text())["reports"]
    momentum = next(r for r in reports if r["portfolio"] == "momentum")
    assert momentum["status"] == "NO_DATA"
    assert any("stale" in n for n in momentum["notes"])


def test_the_graded_window_ends_at_the_current_session(tmp_path, monkeypatch) -> None:
    """The defect this replaces: a pinned artifact froze window_end at its own
    last bar, so six consecutive runs all reported 2026-08-14."""
    out = tmp_path / "divergence.json"

    _run(monkeypatch, db_url=_db(tmp_path, "window"),
         shadow=_shadow(tmp_path), output=out)

    momentum = next(r for r in json.loads(out.read_text())["reports"]
                    if r["portfolio"] == "momentum")
    assert momentum["window_end"] == GRADED.isoformat()


def test_shadow_and_pinned_together_are_refused(tmp_path, monkeypatch) -> None:
    from scripts import divergence_monitor

    monkeypatch.setattr(divergence_monitor.sys, "argv", [
        "divergence_monitor.py",
        "--shadow", str(_shadow(tmp_path)),
        "--backtest", "whatever.json", "--pinned",
        "--db-url", _db(tmp_path, "both"),
    ])

    assert divergence_monitor.main() == 2


def test_the_shadow_path_needs_no_backtest_artifact(tmp_path, monkeypatch) -> None:
    """A shadow run must not require output/backtest_multi_*.json to exist.

    Regression: the first wiring resolved a backtest path before branching on
    --shadow, so it errored with "No backtest JSON found" on any machine
    without a baseline in output/. It passed locally purely because the
    developer's output/ had artifacts in it, and CI caught it. Pinned here with
    the resolver stubbed out, so the test does not depend on what happens to be
    on disk either.
    """
    from scripts import divergence_monitor

    monkeypatch.setattr(
        divergence_monitor, "find_latest_backtest_json", lambda: None
    )
    out = tmp_path / "divergence.json"

    code = _run(monkeypatch, db_url=_db(tmp_path, "nobacktest"),
                shadow=_shadow(tmp_path), output=out)

    assert code == 0, "the shadow path demanded a backtest artifact"
    assert out.is_file()


def test_the_report_records_the_shadow_as_its_source(tmp_path, monkeypatch) -> None:
    """A report that names no source cannot be audited later. On the shadow
    path there is no backtest artifact, so ``backtest_source`` must name the
    shadow rather than being null."""
    out = tmp_path / "divergence.json"
    shadow = _shadow(tmp_path)

    _run(monkeypatch, db_url=_db(tmp_path, "source"), shadow=shadow, output=out)

    assert json.loads(out.read_text())["backtest_source"] == str(shadow)
