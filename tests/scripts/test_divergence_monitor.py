"""Integration tests for ``scripts/divergence_monitor.py``.

The pure math is covered in ``tests/backtest/test_divergence.py``. This file
covers the I/O layer: backtest-JSON parsing, DB loaders against an in-memory
SQLite, and the end-to-end orchestration.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from scripts.divergence_monitor import (
    find_latest_backtest_json,
    load_backtest_equity_series,
    load_live_aggregate_series,
    load_live_equity_series,
    write_json_report,
)
from scripts.paper_state import PaperTradingState
from shared.models.base import Base
from shared.models.equity_snapshot import EquitySnapshot
from shared.models.portfolio import Trade


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    yield session
    session.close()


def _seed_state(session: Session) -> PaperTradingState:
    """Seed a paper-trading state with two portfolios and a week of snapshots."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"momentum": 23080.0, "sector_rotation": 15380.0},
        session=session,
    )

    # Seven days of equity snapshots, +0.2%/day for momentum, +0.1%/day for sector_rotation.
    base = date(2026, 5, 19)
    now = datetime.now(timezone.utc)
    mom_v = 23080.0
    sec_v = 15380.0
    for i in range(7):
        d = date.fromordinal(base.toordinal() + i)
        session.add(EquitySnapshot(
            portfolio="momentum", date=d,
            equity=mom_v, cash=mom_v, market_value=0.0, created_at=now,
        ))
        session.add(EquitySnapshot(
            portfolio="sector_rotation", date=d,
            equity=sec_v, cash=sec_v, market_value=0.0, created_at=now,
        ))
        mom_v *= 1.002
        sec_v *= 1.001
    session.flush()
    return state


def _write_backtest_json(tmp_path: Path, label: str = "20260525_000000") -> Path:
    """Write a minimal valid backtest results JSON for the loader to parse."""
    # Seven dates matching the seeded equity snapshots.
    dates = [date(2026, 5, 19 + i).isoformat() for i in range(7)]
    # portfolio_values is len(dates) + 1; first element is pre-day-0 initial capital.
    mom_pv = [23080.0] + [23080.0 * (1.002 ** (i + 1)) for i in range(7)]
    sec_pv = [15380.0] + [15380.0 * (1.001 ** (i + 1)) for i in range(7)]
    agg_pv = [a + b for a, b in zip(mom_pv, sec_pv)]
    data = {
        "config": {
            "initial_capital": 38460.0,
            "portfolios": {"momentum": 23080.0, "sector_rotation": 15380.0},
        },
        "portfolios": {
            "momentum": {
                "config": {"capital": 23080.0},
                "trades": [],
                "portfolio_values": mom_pv,
                "dates": dates,
                "metrics": {"total_return": 0.0141},
            },
            "sector_rotation": {
                "config": {"capital": 15380.0},
                "trades": [],
                "portfolio_values": sec_pv,
                "dates": dates,
                "metrics": {"total_return": 0.0070},
            },
        },
        "aggregate": {
            "portfolio_values": agg_pv,
            "trades": [],
            "dates": dates,
            "metrics": {"total_return": 0.0113},
        },
        "bars": {},
    }
    path = tmp_path / f"backtest_multi_{label}.json"
    path.write_text(json.dumps(data))
    return path


# ---------------------------------------------------------------------------
# Loader tests
# ---------------------------------------------------------------------------


def test_find_latest_backtest_json_picks_alphabetically_last(tmp_path: Path):
    """File names embed YYYYMMDD_HHMMSS so alphabetical == chronological."""
    (tmp_path / "backtest_multi_20260101_120000.json").write_text("{}")
    (tmp_path / "backtest_multi_20260601_120000.json").write_text("{}")
    (tmp_path / "backtest_multi_20260301_120000.json").write_text("{}")
    latest = find_latest_backtest_json(output_dir=str(tmp_path))
    assert latest is not None
    assert latest.endswith("20260601_120000.json")


def test_find_latest_backtest_json_returns_none_when_empty(tmp_path: Path):
    assert find_latest_backtest_json(output_dir=str(tmp_path)) is None


def test_load_backtest_equity_series_drops_initial_capital(tmp_path: Path):
    """``portfolio_values[0]`` is pre-day-0 capital — must NOT be aligned with dates[0]."""
    path = _write_backtest_json(tmp_path)
    per_p, agg = load_backtest_equity_series(str(path))

    assert set(per_p.keys()) == {"momentum", "sector_rotation"}

    mom = per_p["momentum"]
    # 7 dates, 8 portfolio_values -> 7 dated values after dropping the first.
    assert len(mom) == 7

    # First dated value should be END-of-day value for date 0, not the initial capital.
    first_date = date(2026, 5, 19)
    assert mom[first_date] == pytest.approx(23080.0 * 1.002)  # +0.2% after first day

    # Aggregate equals sum of sleeves on every date.
    for d in mom:
        assert agg[d] == pytest.approx(mom[d] + per_p["sector_rotation"][d])


def test_load_live_equity_series_returns_dict_of_dates_to_equity(db_session: Session):
    state = _seed_state(db_session)
    live = load_live_equity_series(state, "momentum")
    assert len(live) == 7
    assert min(live.keys()) == date(2026, 5, 19)
    assert max(live.keys()) == date(2026, 5, 25)
    # First snapshot was the unmutated initial capital.
    assert live[date(2026, 5, 19)] == pytest.approx(23080.0)


def test_load_live_aggregate_series_sums_per_portfolio(db_session: Session):
    _seed_state(db_session)
    state = PaperTradingState.load(db_session)
    per_p = {
        name: load_live_equity_series(state, name)
        for name in state.get_portfolio_names()
    }
    agg = load_live_aggregate_series(per_p)
    assert len(agg) == 7
    for d, v in agg.items():
        assert v == pytest.approx(per_p["momentum"][d] + per_p["sector_rotation"][d])


def test_load_live_aggregate_series_intersects_dates_only():
    """If one portfolio is missing a date, the aggregate should also skip it."""
    p = {
        "a": {date(2026, 5, 1): 100.0, date(2026, 5, 2): 110.0},
        "b": {date(2026, 5, 1): 200.0},  # missing 5/2
    }
    agg = load_live_aggregate_series(p)
    assert list(agg.keys()) == [date(2026, 5, 1)]
    assert agg[date(2026, 5, 1)] == pytest.approx(300.0)


# ---------------------------------------------------------------------------
# End-to-end orchestration
# ---------------------------------------------------------------------------


def test_end_to_end_produces_ok_reports_when_live_matches_backtest(
    db_session: Session, tmp_path: Path
):
    """Live = backtest (same growth) ⇒ all reports OK and exit 0 from any_breach."""
    from backtest.divergence import any_breach, build_report

    _seed_state(db_session)
    state = PaperTradingState.load(db_session)
    bt_path = _write_backtest_json(tmp_path)
    bt_per_p, bt_agg = load_backtest_equity_series(str(bt_path))

    live_per_p = {
        name: load_live_equity_series(state, name)
        for name in state.get_portfolio_names()
    }

    reports = []
    for name in state.get_portfolio_names():
        if name not in bt_per_p:
            continue
        report = build_report(
            portfolio=name,
            live=live_per_p[name],
            backtest=bt_per_p[name],
            trades=state.get_trades(name),
            window_days=30,
        )
        reports.append(report)

    assert all(r.status in ("OK", "WARNING") for r in reports)
    assert not any_breach(reports)
    # Live and backtest were synthesized identically — divergence should be ~0.
    for r in reports:
        assert r.absolute_divergence_pp == pytest.approx(0.0, abs=1e-9)


def test_write_json_report_round_trip(tmp_path: Path):
    """The JSON output should be valid and contain ISO-date strings."""
    from backtest.divergence import PortfolioDivergenceReport
    report = PortfolioDivergenceReport(
        portfolio="momentum",
        window_start=date(2026, 5, 1),
        window_end=date(2026, 5, 30),
        days_compared=22,
        live_return=0.05,
        backtest_return=0.045,
        absolute_divergence_pp=0.005,
        relative_divergence=0.111,
        daily_correlation=0.93,
        live_trades_in_window=12,
        realized_slippage_total=8.50,
        realized_slippage_bps=9.2,
        realized_commission_total=1.25,
        assumed_commission_total=1.20,
        status="OK",
        notes=[],
    )
    output_path = tmp_path / "div.json"
    write_json_report(
        [report], str(output_path),
        backtest_path="dummy.json",
        window_days=30,
        threshold=0.20,
    )

    loaded = json.loads(output_path.read_text())
    assert loaded["window_days"] == 30
    assert loaded["threshold"] == 0.20
    assert len(loaded["reports"]) == 1
    r = loaded["reports"][0]
    assert r["portfolio"] == "momentum"
    assert r["window_start"] == "2026-05-01"
    assert r["window_end"] == "2026-05-30"
    assert r["status"] == "OK"
