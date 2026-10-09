"""KAN-113 review: a capital flow must not shrink, reset or blind a window.

Two sleeves over ten NYSE sessions; momentum takes a +10,000 credit that
enters on the sixth. The shadow is produced exactly as the 05:15 run produces
it — seeded from ``live_equity_by_sleeve`` — and the monitor runs end to end.

Before the fix the shadow window restarted at the flow: momentum's window
was one session (NO_DATA), and the AGGREGATE, which intersects sessions
across sleeves, collapsed with it; on the next session a 2-session OK cleared
any running BREACH streak.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backtest.shadow_artifact import dump_shadow
from scripts.paper_state import PaperTradingState
from scripts.run_paper import capital_flow_steps_by_sleeve, live_equity_by_sleeve
from shared.capital_flows import flow_adjust
from shared.evidence_store import breach_streak
from shared.market_calendar import MarketCalendar
from shared.models import CapitalAdjustment
from shared.models.base import Base
from shared.models.equity_snapshot import EquitySnapshot
from shared.models.evidence import DivergenceDaily

SESSIONS = MarketCalendar().trading_sessions(date(2026, 9, 14), date(2026, 9, 25))
GRADED = SESSIONS[-1]
FLOW_SESSION = SESSIONS[5]
CREDIT = 10_000.0
SHADOW_ID = "shadow:aaaabbbbccccdddd"
WINDOW = len(SESSIONS)


def _written(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + (
        timedelta(hours=20, minutes=15)
    )


def _db(tmp_path: Path) -> str:
    url = f"sqlite:///{tmp_path / 'flow.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as s:
        PaperTradingState.create_new(
            portfolio_capitals={"momentum": 23_080.0, "sector_rotation": 15_380.0},
            session=s,
        )
        mom, sec = 23_080.0, 15_380.0
        for day in SESSIONS:
            if day == FLOW_SESSION:
                mom += CREDIT
            for name, value in (("momentum", mom), ("sector_rotation", sec)):
                s.add(EquitySnapshot(
                    portfolio=name, date=day + timedelta(days=1),
                    session_date=day, equity=value, cash=value,
                    market_value=0.0, created_at=_written(day),
                ))
            mom *= 1.002
            sec *= 1.001
        s.add(CapitalAdjustment(
            account_id="DUN551088", portfolio="momentum", amount=CREDIT,
            reason="KAN-113 credit flow abc123: test",
            # Between the previous session's snapshot and this one's.
            created_at=_written(FLOW_SESSION) - timedelta(hours=12),
        ))
        s.commit()
    return url


def _shadow(tmp_path: Path, db_url: str, *, drift: float = 1.0) -> Path:
    """The 05:15 run's shadow, under replay_window's contract: seeded at
    live's RAW NAV, the same flows injected on the same sessions, a model
    that earns live's flow-free return (times ``drift`` for momentum), and
    the curve returned flow-adjusted."""
    with sessionmaker(bind=create_engine(db_url))() as s:
        state = PaperTradingState(s)
        live = live_equity_by_sleeve(state)
        steps = capital_flow_steps_by_sleeve(state)
    series: dict[str, dict[date, float]] = {}
    for name, curve in live.items():
        days = sorted(curve)
        flows = steps.get(name, {})
        value = curve[days[0]]
        raw = {days[0]: value}
        included = {days[0]: 0.0}
        for prev, day in zip(days, days[1:]):
            flow = flows.get(day, 0.0)
            step = (curve[day] - flow) / curve[prev]
            if name == "momentum":
                step *= drift
            value = value * step + flow
            raw[day] = value
            included[day] = included[prev] + flow
        series[name] = flow_adjust(raw, included)
    path = tmp_path / "shadow.json"
    dump_shadow(path, series=series, shadow_id=SHADOW_ID,
                window_sessions=WINDOW, session_date=GRADED,
                produced_on=date.today())
    return path


def _run(monkeypatch, *, db_url, shadow, output) -> int:
    from scripts import divergence_monitor

    monkeypatch.setattr(divergence_monitor.sys, "argv", [
        "divergence_monitor.py",
        "--live-history-from", "all",
        "--shadow", str(shadow),
        "--db-url", db_url,
        "--window", str(WINDOW),
        "--output", str(output),
    ])
    return divergence_monitor.main()


def _reports(output: Path) -> dict[str, dict]:
    return {r["portfolio"]: r for r in json.loads(output.read_text())["reports"]}


def test_the_shadow_seeds_raw_and_is_handed_the_flow_on_its_session(tmp_path):
    db_url = _db(tmp_path)
    with sessionmaker(bind=create_engine(db_url))() as s:
        state = PaperTradingState(s)
        live = live_equity_by_sleeve(state)
        steps = capital_flow_steps_by_sleeve(state)
    # Full length, and raw: the seed is what live actually held.
    assert sorted(live["momentum"]) == SESSIONS
    i = SESSIONS.index(FLOW_SESSION)
    jump = live["momentum"][SESSIONS[i]] - live["momentum"][SESSIONS[i - 1]] * 1.002
    assert abs(jump - CREDIT) < 1e-6
    # The flow, on the session live's value first includes it.
    assert steps == {"momentum": {FLOW_SESSION: CREDIT}}


def test_a_credit_mid_window_leaves_every_window_full_and_graded(
    tmp_path, monkeypatch
):
    db_url = _db(tmp_path)
    out = tmp_path / "divergence.json"

    code = _run(monkeypatch, db_url=db_url,
                shadow=_shadow(tmp_path, db_url), output=out)

    assert code == 0
    reports = _reports(out)
    for name in ("momentum", "sector_rotation", "AGGREGATE"):
        report = reports[name]
        assert report["days_compared"] == WINDOW, (name, report)
        assert report["status"] == "OK", (name, report)
        assert not any("too short" in n for n in report["notes"]), report
    # The flow is named where it lies inside the window — and only there.
    flow_note = (
        "capital flow(s) inside this window (KAN-113): momentum "
        f"+10,000.00 USD valued from {FLOW_SESSION.isoformat()}"
    )
    assert any(flow_note in n for n in reports["momentum"]["notes"])
    assert any(flow_note in n for n in reports["AGGREGATE"]["notes"])
    assert not any("KAN-113" in n for n in reports["sector_rotation"]["notes"])
    assert abs(reports["momentum"]["live_return"] - (1.002 ** (WINDOW - 1) - 1)) < 1e-9


def test_a_breach_streak_carries_across_the_flow(tmp_path, monkeypatch):
    """A sleeve already in BREACH stays in BREACH after a credit: the streak
    neither resets (no 2-session OK) nor pauses (no NO_DATA)."""
    db_url = _db(tmp_path)
    engine = create_engine(db_url)
    with Session(engine) as s:
        for day in SESSIONS[-3:-1]:
            s.add(DivergenceDaily(
                sleeve="momentum", session_date=day, status="BREACH",
                baseline_id=SHADOW_ID, window_sessions=WINDOW,
                threshold=0.25, metric_value=0.5,
                created_at=_written(day),
            ))
        s.commit()

    code = _run(monkeypatch, db_url=db_url,
                shadow=_shadow(tmp_path, db_url, drift=0.97),
                output=tmp_path / "divergence.json")

    assert code == 1
    with Session(engine) as s:
        status = s.scalar(select(DivergenceDaily.status).where(
            DivergenceDaily.sleeve == "momentum",
            DivergenceDaily.session_date == GRADED,
        ))
        streak = breach_streak(
            s, sleeve="momentum", as_of=GRADED, baseline_id=SHADOW_ID,
            scoring_floor=SESSIONS[0],
        )
    assert status == "BREACH"
    assert streak.length == 3
    assert streak.started_on == SESSIONS[-3]
