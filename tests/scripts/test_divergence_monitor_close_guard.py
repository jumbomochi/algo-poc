"""The divergence monitor refuses a shadow priced before its session closed — KAN-104.

The monitor prices nothing: every number it grades was written by the paper
run. So whether it "grades an unclosed session" is a property of the shadow it
reads, not of when the monitor itself runs — a catch-up during US hours grades
the morning's closed session and must keep working. The paper run now stamps
each shadow with the newest session its bars cover (``bars_session``) and the
instant the fetch of those bars started (``priced_at``); the monitor refuses
with exit 2 (nothing judged, no dead-man beat) when the first had not closed by
the second.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backtest.shadow_artifact import dump_shadow, load_shadow
from scripts.paper_state import PaperTradingState
from shared.market_calendar import ET
from shared.models.base import Base
from shared.models.equity_snapshot import EquitySnapshot

SGT = ZoneInfo("Asia/Singapore")
SESSIONS = [date(2026, 10, 27) + timedelta(days=i) for i in range(7)]


def _db(tmp_path: Path) -> str:
    url = f"sqlite:///{tmp_path / 'paper.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    PaperTradingState.create_new(portfolio_capitals={"momentum": 23080.0}, session=s)
    now = datetime.now(timezone.utc)
    equity = 23080.0
    for d in SESSIONS:
        s.add(EquitySnapshot(portfolio="momentum", date=d, equity=equity,
                             cash=equity, market_value=0.0, created_at=now))
        equity *= 1.002
    s.commit()
    s.close()
    return url


def _shadow(tmp_path: Path, **provenance) -> Path:
    equity = 23080.0
    curve = {}
    for d in SESSIONS:
        curve[d] = equity
        equity *= 1.002
    path = tmp_path / "shadow.json"
    dump_shadow(path, series={"momentum": curve}, shadow_id="shadow:aaaabbbbccccdddd",
                window_sessions=5, session_date=SESSIONS[-1],
                produced_on=date.today(), **provenance)
    return path


def _run(monkeypatch, tmp_path, shadow) -> int:
    from scripts import divergence_monitor

    monkeypatch.setattr(divergence_monitor.sys, "argv", [
        "divergence_monitor.py",
        "--live-history-from", "all",
        "--shadow", str(shadow),
        "--db-url", _db(tmp_path),
        "--window", "5",
        "--output", str(tmp_path / "divergence.json"),
    ])
    return divergence_monitor.main()


def test_a_shadow_priced_at_the_old_est_slot_is_refused(tmp_path, monkeypatch, capsys):
    """Bars through Mon 2026-11-02, fetched at Tue 04:16 SGT = 15:16 EST."""
    shadow = _shadow(
        tmp_path,
        bars_session=date(2026, 11, 2),
        priced_at=datetime(2026, 11, 3, 4, 16, tzinfo=SGT),
    )

    code = _run(monkeypatch, tmp_path, shadow)

    assert code == 2
    out = capsys.readouterr().out
    assert "refusing to grade" in out
    assert "2026-11-02" in out
    assert not (tmp_path / "divergence.json").exists(), "a verdict was written anyway"


def test_a_shadow_priced_on_a_half_day_before_13_00_is_refused(tmp_path, monkeypatch):
    shadow = _shadow(
        tmp_path,
        bars_session=date(2026, 11, 27),
        priced_at=datetime(2026, 11, 27, 12, 55, tzinfo=ET),
    )
    assert _run(monkeypatch, tmp_path, shadow) == 2


def test_a_shadow_priced_after_the_close_is_graded(tmp_path, monkeypatch):
    """The new 05:15 SGT slot, EST and EDT."""
    for priced_at, bars_session in [
        (datetime(2026, 11, 3, 5, 16, tzinfo=SGT), date(2026, 11, 2)),
        (datetime(2026, 10, 6, 5, 16, tzinfo=SGT), date(2026, 10, 5)),
    ]:
        out_dir = tmp_path / priced_at.strftime("%Y%m%d")
        out_dir.mkdir()
        shadow = _shadow(out_dir, bars_session=bars_session, priced_at=priced_at)
        code = _run(monkeypatch, out_dir, shadow)
        assert code == 0, priced_at
        assert json.loads((out_dir / "divergence.json").read_text())["reports"]


def test_a_shadow_without_provenance_is_graded_with_a_note(tmp_path, monkeypatch, capsys):
    """Artifacts written before KAN-104 carry no provenance. Refusing them would
    blind the monitor for the first night after the deploy, for a check whose
    producer is itself guarded."""
    code = _run(monkeypatch, tmp_path, _shadow(tmp_path))

    assert code == 0
    assert "no KAN-104 provenance" in capsys.readouterr().out


def test_provenance_round_trips_through_the_artifact(tmp_path):
    priced_at = datetime(2026, 11, 3, 5, 16, tzinfo=SGT)
    loaded = load_shadow(_shadow(tmp_path, bars_session=date(2026, 11, 2), priced_at=priced_at))
    assert loaded.bars_session == date(2026, 11, 2)
    assert loaded.priced_at == priced_at
    assert loaded.priced_at.utcoffset() is not None


def test_an_old_artifact_loads_with_no_provenance(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({
        "shadow_id": "shadow:x", "window_sessions": 5,
        "session_date": "2026-10-02", "produced_on": "2026-10-03",
        "series": {"momentum": {"2026-10-02": 1.0}},
    }))
    loaded = load_shadow(path)
    assert loaded.bars_session is None and loaded.priced_at is None
