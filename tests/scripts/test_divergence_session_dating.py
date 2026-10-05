"""KAN-103: divergence and blindness are graded by the US session valued.

``equity_snapshots.date`` is the SGT run date; ``session_date`` is the US
session whose closes marked the book. A Tuesday 04:15 SGT run values Monday,
so the monitor must file Monday's verdict on Tuesday morning, and a week of
runs must leave no Monday BLIND.

The fixture is a real fortnight: runs Tue 2026-09-15 .. Sat 09-19 and
Tue 09-22 .. Tue 09-29, each valuing the previous US session.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backtest.shadow_artifact import dump_shadow
from backtest.shadow_series import build_shadow_series
from scripts.paper_state import PaperTradingState
from scripts.run_paper import live_equity_by_sleeve
from services.risk_management.engine import RiskEngine
from shared.evidence_store import blindness
from shared.market_calendar import MarketCalendar
from shared.models.base import Base
from shared.models.equity_snapshot import EquitySnapshot
from shared.models.evidence import DivergenceDaily
from shared.session_dating import last_session_before

SLEEVES = ["momentum", "sector_rotation"]
SHADOW_ID = "shadow:5e55105da7ed0001"
CAL = MarketCalendar()

#: SGT run dates, Tue–Sat. 09-21 (Mon) and 09-28 (Mon) have no run.
RUN_DATES = [
    d for d in (date(2026, 9, 15) + timedelta(days=i) for i in range(15))
    if d.weekday() in (1, 2, 3, 4, 5)
]
#: The US session each run valued: Mon–Fri, Mondays included.
SESSION_OF = {run: last_session_before(run) for run in RUN_DATES}
SESSIONS = sorted(SESSION_OF.values())
MONDAY = date(2026, 9, 28)


def _equity(sleeve: str, session: date) -> float:
    """A distinct value per session, so a one-session shift is visible."""
    base = 20_000.0 if sleeve == "momentum" else 15_000.0
    return base * (1.0 + 0.003 * SESSIONS.index(session))


def _db(tmp_path: Path, name: str, *, null_sleeve: str | None = None) -> str:
    url = f"sqlite:///{tmp_path / (name + '.db')}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    PaperTradingState.create_new(
        portfolio_capitals={"momentum": 20_000.0, "sector_rotation": 15_000.0},
        session=s,
    )
    now = datetime.now(timezone.utc)
    for run in RUN_DATES:
        session = SESSION_OF[run]
        for sleeve in SLEEVES:
            value = _equity(sleeve, session)
            s.add(EquitySnapshot(
                portfolio=sleeve, date=run,
                session_date=None if sleeve == null_sleeve else session,
                equity=value, cash=value, market_value=0.0, created_at=now,
            ))
    s.commit()
    s.close()
    return url


def _shadow(tmp_path: Path, *, through: date) -> Path:
    """A model curve that tracks live exactly, keyed by session as the replay is."""
    series = {
        sleeve: {d: _equity(sleeve, d) for d in SESSIONS if d <= through}
        for sleeve in SLEEVES
    }
    path = tmp_path / f"shadow_{through:%Y%m%d}.json"
    dump_shadow(path, series=series, shadow_id=SHADOW_ID, window_sessions=5,
                session_date=through, produced_on=date.today())
    return path


def _run(monkeypatch, *, db_url: str, shadow: Path, output: Path) -> int:
    from scripts import divergence_monitor

    monkeypatch.setattr(divergence_monitor.sys, "argv", [
        "divergence_monitor.py",
        "--live-history-from", "all",
        "--shadow", str(shadow),
        "--db-url", db_url,
        "--window", "5",
        "--output", str(output),
    ])
    return divergence_monitor.main()


def _verdicts(db_url: str) -> list[DivergenceDaily]:
    s = sessionmaker(bind=create_engine(db_url))()
    try:
        return s.execute(select(DivergenceDaily)).scalars().all()
    finally:
        s.close()


def test_the_fixture_is_the_real_shape():
    assert MONDAY in SESSIONS
    assert SESSION_OF[date(2026, 9, 29)] == MONDAY
    assert all(CAL.is_trading_day(d) for d in SESSIONS)


def test_a_tuesday_run_files_a_monday_verdict(tmp_path, monkeypatch):
    """End to end: Tue 09-29's snapshots value Monday 09-28, so does the verdict."""
    db_url = _db(tmp_path, "monday")
    out = tmp_path / "divergence.json"

    code = _run(monkeypatch, db_url=db_url,
                shadow=_shadow(tmp_path, through=MONDAY), output=out)

    assert code == 0
    reports = {r["portfolio"]: r for r in json.loads(out.read_text())["reports"]}
    for sleeve in SLEEVES:
        assert reports[sleeve]["window_end"] == MONDAY.isoformat()
        assert reports[sleeve]["status"] == "OK"
    rows = _verdicts(db_url)
    assert {r.session_date for r in rows} == {MONDAY}
    assert {r.sleeve for r in rows} == set(SLEEVES)


def test_live_tracking_its_model_exactly_measures_zero_drift(tmp_path, monkeypatch):
    """Aligned, an identical curve is a perfect match. Off by one it was not."""
    out = tmp_path / "divergence.json"

    _run(monkeypatch, db_url=_db(tmp_path, "aligned"),
         shadow=_shadow(tmp_path, through=MONDAY), output=out)

    momentum = next(r for r in json.loads(out.read_text())["reports"]
                    if r["portfolio"] == "momentum")
    assert momentum["absolute_divergence_pp"] == pytest.approx(0.0, abs=1e-12)
    assert momentum["daily_correlation"] == pytest.approx(1.0)


def test_a_week_of_runs_leaves_no_monday_blind(tmp_path, monkeypatch):
    """Tue..Sat SGT runs grade Mon..Fri; blindness expects exactly those."""
    db_url = _db(tmp_path, "week")
    week = [d for d in SESSIONS if date(2026, 9, 21) <= d <= date(2026, 9, 25)]
    for session in week:
        _run(monkeypatch, db_url=db_url,
             shadow=_shadow(tmp_path, through=session),
             output=tmp_path / f"divergence_{session:%Y%m%d}.json")

    s = sessionmaker(bind=create_engine(db_url))()
    try:
        result = blindness(
            s, start=week[0], end=week[-1], sleeves=SLEEVES,
            baseline_id=SHADOW_ID, calendar=CAL,
        )
    finally:
        s.close()
    assert week[0].weekday() == 0  # Monday 09-21
    assert result.blind_sessions == []
    assert result.partial_sessions == []
    assert result.longest_consecutive == 0


def test_a_labor_day_duplicate_dedupes_and_the_tuesday_rescores_friday(
    tmp_path, monkeypatch
):
    """Sat 09-05 and Tue 09-08 both value Friday 09-04: one key, one verdict."""
    from scripts.divergence_monitor import load_live_equity_series

    url = f"sqlite:///{tmp_path / 'labor.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    state = PaperTradingState.create_new(
        portfolio_capitals={"momentum": 20_000.0, "sector_rotation": 15_000.0},
        session=s,
    )
    sessions = [date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 2),
                date(2026, 9, 3), date(2026, 9, 4)]
    runs = {date(2026, 9, 1): sessions[0], date(2026, 9, 2): sessions[1],
            date(2026, 9, 3): sessions[2], date(2026, 9, 4): sessions[3],
            date(2026, 9, 5): sessions[4], date(2026, 9, 8): sessions[4]}
    now = datetime.now(timezone.utc)
    for run, session in runs.items():
        for sleeve in SLEEVES:
            value = 10_000.0 + sessions.index(session)
            if run == date(2026, 9, 8):
                value += 0.5  # the later run's row is the one of record
            s.add(EquitySnapshot(
                portfolio=sleeve, date=run, session_date=session,
                equity=value, cash=value, market_value=0.0, created_at=now,
            ))
    s.commit()

    live = load_live_equity_series(state, "momentum")
    assert sorted(live) == sessions
    assert live[date(2026, 9, 4)] == 10_004.5
    s.close()

    series = {sleeve: {d: 10_000.0 + i for i, d in enumerate(sessions)}
              for sleeve in SLEEVES}
    shadow = tmp_path / "shadow.json"
    dump_shadow(shadow, series=series, shadow_id=SHADOW_ID, window_sessions=5,
                session_date=sessions[-1], produced_on=date.today())
    # Saturday's monitor, then Tuesday's: the same session, re-scored in place.
    _run(monkeypatch, db_url=url, shadow=shadow, output=tmp_path / "sat.json")
    _run(monkeypatch, db_url=url, shadow=shadow, output=tmp_path / "tue.json")

    rows = _verdicts(url)
    assert {r.session_date for r in rows} == {date(2026, 9, 4)}
    assert len(rows) == len(SLEEVES)


def test_an_unstamped_sleeve_is_excluded_and_reads_no_data(
    tmp_path, monkeypatch, capsys
):
    """NULL session_date is ungradeable, never guessed from the run date."""
    db_url = _db(tmp_path, "nulls", null_sleeve="sector_rotation")
    out = tmp_path / "divergence.json"

    _run(monkeypatch, db_url=db_url,
         shadow=_shadow(tmp_path, through=MONDAY), output=out)

    reports = {r["portfolio"]: r for r in json.loads(out.read_text())["reports"]}
    assert reports["sector_rotation"]["status"] == "NO_DATA"
    assert reports["momentum"]["status"] == "OK"
    printed = capsys.readouterr().out
    assert "'sector_rotation': 11 snapshot(s) carry no session_date" in printed
    assert "backfill_snapshot_sessions.py" in printed
    # No verdict can be dated for the unstamped sleeve.
    assert {r.sleeve for r in _verdicts(db_url)} == {"momentum"}


def test_a_partial_bar_row_drops_out_of_the_live_series(tmp_path):
    from scripts.divergence_monitor import load_live_equity_series

    url = _db(tmp_path, "partial")
    s = sessionmaker(bind=create_engine(url))()
    state = PaperTradingState(s)
    s.add(EquitySnapshot(
        portfolio="momentum", date=date(2026, 9, 30), session_date=None,
        equity=1.0, cash=1.0, market_value=0.0,
        created_at=datetime.now(timezone.utc),
    ))
    s.commit()

    live = load_live_equity_series(state, "momentum")
    s.close()
    assert max(live) == MONDAY
    assert 1.0 not in live.values()


# ------------------------------------------------------------------ shadow


class _FlatSleeve:
    """Never trades: the replay's equity is its seed NAV on every session."""

    def __init__(self) -> None:
        self.signals_fn = lambda *_a, **_k: None
        self.risk_engine = RiskEngine(
            position_entry_limit_pct=10.0,
            sector_concentration_pct=100.0,
            total_exposure_limit_pct=100.0,
            max_lots_per_ticker=1,
        )


def test_the_shadow_is_seeded_at_live_s_value_for_the_same_session(tmp_path):
    url = _db(tmp_path, "seed")
    s = sessionmaker(bind=create_engine(url))()
    live = live_equity_by_sleeve(PaperTradingState(s))
    s.close()
    assert sorted(live["momentum"]) == SESSIONS

    bars = {"SPY": [
        {"date": d, "open": 100.0, "high": 100.0, "low": 100.0,
         "close": 100.0, "volume": 1}
        for d in SESSIONS
    ]}
    series = build_shadow_series(
        portfolios={"momentum": _FlatSleeve()},
        bars_by_ticker=bars,
        live_equity={"momentum": live["momentum"]},
        window_sessions=5,
    )

    curve = series["momentum"]
    day0 = min(curve)
    assert day0 == SESSIONS[-5] == date(2026, 9, 22)
    assert curve[day0] == pytest.approx(live["momentum"][day0])
    assert curve[day0] == pytest.approx(_equity("momentum", day0))
    # Monday is in the shadow now that live is keyed by session.
    assert MONDAY in curve
